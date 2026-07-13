from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from core.engine.persistence import atomic_write_json
from core.schema import SchemaValidationError, validate_schema
from core.story_project.identity import (
    ProjectIdentity,
    ensure_project_identity,
    project_identity_path,
    validate_project_identity,
)
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION


CALIBRATION_REPORT_SCHEMA_VERSION = "1.0"


class StoryStateActivationError(ValueError):
    code = "story_state_activation_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


def calibration_report_sha256(report: Mapping[str, Any]) -> str:
    payload = dict(report)
    payload.pop("report_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_story_state_calibration_report(
    *,
    book_id: str,
    parser_version: str,
    semantic_schema_version: str,
    target_layout_profile_version: str,
    evidence: Mapping[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    normalized_evidence = _normalize_evidence(evidence)
    blockers = calibration_blockers(normalized_evidence)
    report: dict[str, Any] = {
        "schema_version": CALIBRATION_REPORT_SCHEMA_VERSION,
        "book_id": _required_text("book_id", book_id),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "parser_version": _required_text("parser_version", parser_version),
        "semantic_schema_version": _required_text(
            "semantic_schema_version", semantic_schema_version
        ),
        "target_layout_profile_version": _required_text(
            "target_layout_profile_version", target_layout_profile_version
        ),
        "evidence": normalized_evidence,
        "strict_eligible": not blockers,
        "strict_blockers": blockers,
        "report_sha256": "",
    }
    report["report_sha256"] = calibration_report_sha256(report)
    return validate_story_state_calibration_report(report)


def validate_story_state_calibration_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StoryStateActivationError("calibration report must be a JSON object")
    try:
        validated = validate_schema(value, "story_state_calibration_report.schema.json")
    except SchemaValidationError as exc:
        raise StoryStateActivationError(str(exc), code="calibration_report_invalid") from exc
    expected_hash = calibration_report_sha256(validated)
    if len(validated["report_sha256"]) != 64 or any(
        character not in "0123456789abcdef" for character in validated["report_sha256"]
    ):
        raise StoryStateActivationError(
            "report_sha256 must be a lowercase SHA-256 digest",
            code="calibration_report_invalid",
        )
    if validated["report_sha256"] != expected_hash:
        raise StoryStateActivationError(
            "calibration report hash does not match its canonical payload",
            code="calibration_report_hash_mismatch",
        )
    expected_blockers = calibration_blockers(validated["evidence"])
    if validated["evidence"] != _normalize_evidence(validated["evidence"]):
        raise StoryStateActivationError(
            "calibration evidence must be canonical and duplicate-free",
            code="calibration_report_invalid",
        )
    if validated["strict_blockers"] != expected_blockers:
        raise StoryStateActivationError(
            "strict_blockers must be derived from calibration evidence",
            code="calibration_report_invalid",
        )
    if validated["strict_eligible"] != (not expected_blockers):
        raise StoryStateActivationError(
            "strict_eligible must be derived from an empty blocker list",
            code="calibration_report_invalid",
        )
    return validated


def load_story_state_calibration_report(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoryStateActivationError(
            f"cannot read calibration report {source}: {exc}",
            code="calibration_report_unreadable",
        ) from exc
    return validate_story_state_calibration_report(payload)


def calibration_blockers(evidence: Mapping[str, Any]) -> list[str]:
    item = _normalize_evidence(evidence)
    blockers: list[str] = []
    if item["target_sample_count"] < 1:
        blockers.append("target_book_redacted_sample_missing")
    if item["format_variant_count"] < 2:
        blockers.append("format_variants_insufficient")
    if item["managed_round_trip_rate"] != 1.0:
        blockers.append("managed_round_trip_not_100_percent")
    if item["required_field_exact_match_rate"] != 1.0:
        blockers.append("required_field_exact_match_not_100_percent")
    if item["authoritative_precision"] != 1.0:
        blockers.append("authoritative_precision_not_100_percent")
    if item["supported_optional_recall"] < 0.95:
        blockers.append("supported_optional_recall_below_95_percent")
    if item["unsupported_structure_count"] != item["unsupported_structure_captured_count"]:
        blockers.append("unsupported_structure_not_captured")
    if item["consecutive_shadow_chapters"] < 10:
        blockers.append("consecutive_shadow_chapters_below_10")
    if item["blocking_conflict_count"]:
        blockers.append("blocking_semantic_conflicts_present")
    if item["missing_provenance_fields"]:
        blockers.append("production_field_provenance_incomplete")
    return blockers


def activate_story_state(
    story_project_root: str | Path,
    calibration_report: Mapping[str, Any] | str | Path,
    *,
    now: Callable[[], datetime] | None = None,
) -> ProjectIdentity:
    root = Path(story_project_root).resolve()
    report = (
        load_story_state_calibration_report(calibration_report)
        if isinstance(calibration_report, (str, Path))
        else validate_story_state_calibration_report(dict(calibration_report))
    )
    identity = ensure_project_identity(root, now=now)
    if report["book_id"] != identity.book_id:
        raise StoryStateActivationError(
            "calibration report belongs to another StoryProject",
            code="calibration_report_identity_mismatch",
        )
    if not report["strict_eligible"]:
        raise StoryStateActivationError(
            "strict activation is blocked: " + ", ".join(report["strict_blockers"]),
            code="strict_calibration_not_qualified",
        )

    from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION

    if report["parser_version"] != SEMANTIC_PARSER_VERSION:
        raise StoryStateActivationError(
            "calibration parser version does not match the runtime parser",
            code="strict_profile_version_mismatch",
        )
    if report["semantic_schema_version"] != STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION:
        raise StoryStateActivationError(
            "calibration semantic schema version does not match the runtime schema",
            code="strict_profile_version_mismatch",
        )
    activated_at = _utc_timestamp(now)
    activated = replace(
        identity,
        story_state_mode="strict",
        activation={
            "parser_version": report["parser_version"],
            "semantic_schema_version": report["semantic_schema_version"],
            "layout_profile_version": report["target_layout_profile_version"],
            "calibration_report_sha256": report["report_sha256"],
            "activated_at": activated_at,
        },
    )
    validate_project_identity(activated.to_dict())
    atomic_write_json(project_identity_path(root), activated.to_dict())
    return activated


def evaluate_story_state_activation(
    identity: ProjectIdentity,
    semantic_state: Mapping[str, Any],
    *,
    allow_shadow_downgrade: bool = False,
) -> dict[str, Any]:
    state = dict(semantic_state)
    if identity.story_state_mode != "strict":
        return {
            "configured_mode": identity.story_state_mode,
            "effective_mode": identity.story_state_mode,
            "authoritative": False,
            "profile_match": None,
            "downgraded": False,
            "ready_for_next_step": True,
            "blockers": [],
        }
    activation = identity.activation
    if not isinstance(activation, dict):
        raise StoryStateActivationError(
            "strict identity is missing activation metadata",
            code="strict_activation_metadata_missing",
        )
    mismatches: list[str] = []
    comparisons = (
        ("parser_version", state.get("parser_version")),
        ("semantic_schema_version", state.get("schema_version")),
        ("layout_profile_version", state.get("layout_profile_version")),
    )
    for field, actual in comparisons:
        if activation.get(field) != actual:
            mismatches.append(f"{field}:{activation.get(field)}!={actual}")
    if any(item.get("blocking") for item in state.get("conflicts", []) if isinstance(item, dict)):
        mismatches.append("blocking_semantic_conflict")
    missing_provenance = semantic_fields_without_provenance(state)
    if missing_provenance:
        mismatches.append("production_field_provenance_incomplete")
    if mismatches and not allow_shadow_downgrade:
        raise StoryStateActivationError(
            "strict StoryProject profile is no longer qualified: " + ", ".join(mismatches),
            code="strict_profile_version_mismatch",
        )
    if mismatches:
        return {
            "configured_mode": "strict",
            "effective_mode": "shadow",
            "authoritative": False,
            "profile_match": False,
            "downgraded": True,
            "ready_for_next_step": False,
            "blockers": mismatches,
        }
    return {
        "configured_mode": "strict",
        "effective_mode": "strict",
        "authoritative": True,
        "profile_match": True,
        "downgraded": False,
        "ready_for_next_step": True,
        "blockers": [],
    }


def semantic_fields_without_provenance(semantic_state: Mapping[str, Any]) -> list[str]:
    state = dict(semantic_state)
    provenance_paths = [
        _normalize_field_path(str(item.get("field_path")))
        for item in state.get("provenance", [])
        if isinstance(item, dict) and str(item.get("field_path") or "").strip()
    ]
    production_fields: list[str] = []
    for root in (
        "story_state",
        "world_state",
        "spatial_state",
        "characters",
        "timeline",
        "constraints",
        "foreshadowing",
    ):
        production_fields.extend(_semantic_leaf_paths(root, state.get(root)))
    return sorted(
        field
        for field in production_fields
        if not any(
            source == field
            or source.startswith(field + ".")
            or field.startswith(source + ".")
            for source in provenance_paths
        )
    )


def _normalize_field_path(value: str) -> str:
    import re

    return re.sub(r"\[(\d+)\]", r".\1", value)


def _semantic_leaf_paths(prefix: str, value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        if "id" in value and prefix.rsplit(".", 1)[-1].isdigit():
            prefix = prefix.rsplit(".", 1)[0] + "." + str(value["id"])
        paths: list[str] = []
        for key, child in value.items():
            if key == "id":
                continue
            paths.extend(_semantic_leaf_paths(f"{prefix}.{key}", child))
        return paths or [prefix]
    if isinstance(value, list):
        paths: list[str] = []
        for index, child in enumerate(value):
            child_id = child.get("id") if isinstance(child, dict) else None
            paths.extend(_semantic_leaf_paths(f"{prefix}.{child_id or index}", child))
        return paths or [prefix]
    return [prefix]


def _normalize_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise StoryStateActivationError(
            "calibration evidence must be an object",
            code="calibration_report_invalid",
        )
    return {
        "target_sample_count": _non_negative_int(evidence, "target_sample_count"),
        "format_variant_count": _non_negative_int(evidence, "format_variant_count"),
        "managed_round_trip_rate": _rate(evidence, "managed_round_trip_rate"),
        "required_field_exact_match_rate": _rate(
            evidence, "required_field_exact_match_rate"
        ),
        "authoritative_precision": _rate(evidence, "authoritative_precision"),
        "supported_optional_recall": _rate(evidence, "supported_optional_recall"),
        "unsupported_structure_count": _non_negative_int(
            evidence, "unsupported_structure_count"
        ),
        "unsupported_structure_captured_count": _non_negative_int(
            evidence, "unsupported_structure_captured_count"
        ),
        "consecutive_shadow_chapters": _non_negative_int(
            evidence, "consecutive_shadow_chapters"
        ),
        "blocking_conflict_count": _non_negative_int(evidence, "blocking_conflict_count"),
        "missing_provenance_fields": sorted(
            dict.fromkeys(
                str(item).strip()
                for item in evidence.get("missing_provenance_fields", [])
                if str(item).strip()
            )
        ),
    }


def _non_negative_int(evidence: Mapping[str, Any], field: str) -> int:
    value = evidence.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StoryStateActivationError(
            f"calibration evidence {field} must be a non-negative integer",
            code="calibration_report_invalid",
        )
    return value


def _rate(evidence: Mapping[str, Any], field: str) -> float:
    value = evidence.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoryStateActivationError(
            f"calibration evidence {field} must be a number from 0 to 1",
            code="calibration_report_invalid",
        )
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise StoryStateActivationError(
            f"calibration evidence {field} must be a number from 0 to 1",
            code="calibration_report_invalid",
        )
    return result


def _required_text(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise StoryStateActivationError(
            f"{field} is required",
            code="calibration_report_invalid",
        )
    return text


def _utc_timestamp(now: Callable[[], datetime] | None) -> str:
    value = now() if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "CALIBRATION_REPORT_SCHEMA_VERSION",
    "StoryStateActivationError",
    "activate_story_state",
    "build_story_state_calibration_report",
    "calibration_blockers",
    "calibration_report_sha256",
    "evaluate_story_state_activation",
    "load_story_state_calibration_report",
    "semantic_fields_without_provenance",
    "validate_story_state_calibration_report",
]
