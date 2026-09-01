from __future__ import annotations

"""Collect reproducible, non-authoritative evidence for Strict calibration.

This command deliberately does not activate a StoryProject or emit a qualified
calibration report.  The existing Strict report schema contains only summary
metrics, so this companion audit preserves the source hashes, parser versions,
and per-chapter observations needed for an operator to review those metrics
honestly.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.engine.persistence import atomic_create_json  # noqa: E402
from core.story_project.activation import (  # noqa: E402
    build_story_state_calibration_report,
    semantic_fields_without_provenance,
)
from core.story_project.identity import load_project_identity, project_identity_for_operation  # noqa: E402
from core.story_project.managed_block import (  # noqa: E402
    build_managed_projection,
    compute_base_source_digest,
    parse_managed_block,
    write_managed_block,
)
from core.story_project.semantic_parser import (  # noqa: E402
    SEMANTIC_PARSER_VERSION,
    build_story_project_shadow_report,
    parse_story_project_semantic_state,
)


AUDIT_SCHEMA_VERSION = "1.1"


class CalibrationAuditError(RuntimeError):
    """The requested source set cannot be audited safely."""


def collect_calibration_audit(
    story_project_root: str | Path,
    *,
    start_chapter: int,
    chapter_count: int,
) -> dict[str, Any]:
    """Return source-bound Shadow observations without changing the project."""

    root = Path(story_project_root).resolve(strict=True)
    if not root.is_dir():
        raise CalibrationAuditError(f"StoryProject root is not a directory: {root}")
    if isinstance(start_chapter, bool) or not isinstance(start_chapter, int) or start_chapter < 1:
        raise CalibrationAuditError("start_chapter must be a positive integer")
    if isinstance(chapter_count, bool) or not isinstance(chapter_count, int) or chapter_count < 1:
        raise CalibrationAuditError("chapter_count must be a positive integer")

    identity = load_project_identity(root) or project_identity_for_operation(root, persist=False)
    chapters = list(range(start_chapter, start_chapter + chapter_count))
    observations: list[dict[str, Any]] = []
    source_kinds: set[str] = set()
    total_provenance = 0
    verified_provenance = 0
    total_unsupported = 0
    verified_unsupported = 0
    total_authoritative_values = 0
    matched_authoritative_values = 0
    total_optional_values = 0
    matched_optional_values = 0
    missing_provenance_fields: set[str] = set()
    blocking_conflict_count = 0
    parser_versions: set[str] = set()
    schema_versions: set[str] = set()
    layout_profiles: set[str] = set()

    for chapter in chapters:
        state = parse_story_project_semantic_state(root, chapter, project_identity=identity)
        shadow = build_story_project_shadow_report(state)
        provenance = list(state.get("provenance") or [])
        unsupported = list(state.get("unsupported_excerpts") or [])
        provenance_checks = _verify_provenance(root, provenance)
        unsupported_checks = _verify_unsupported(root, unsupported)
        authority_measurement = _measure_authoritative_text(state, root)
        optional_measurement = _measure_optional_context_recall(state, root)
        current_missing = semantic_fields_without_provenance(state)
        current_blocking = [
            item
            for item in state.get("conflicts", [])
            if isinstance(item, Mapping) and item.get("blocking") is True
        ]
        source_kinds.update(
            str(item.get("source_kind"))
            for item in provenance
            if isinstance(item, Mapping) and str(item.get("source_kind") or "").strip()
        )
        total_provenance += len(provenance_checks)
        verified_provenance += sum(item["ok"] for item in provenance_checks)
        total_unsupported += len(unsupported_checks)
        verified_unsupported += sum(item["ok"] for item in unsupported_checks)
        total_authoritative_values += authority_measurement["expected_count"]
        matched_authoritative_values += authority_measurement["matched_count"]
        total_optional_values += optional_measurement["expected_count"]
        matched_optional_values += optional_measurement["matched_count"]
        missing_provenance_fields.update(current_missing)
        blocking_conflict_count += len(current_blocking)
        parser_versions.add(str(state["parser_version"]))
        schema_versions.add(str(state["schema_version"]))
        layout_profiles.add(str(state["layout_profile_version"]))
        observations.append(
            {
                "chapter_index": chapter,
                "mode": shadow["mode"],
                "authoritative": shadow["authoritative"],
                "source_digest": state["source_digest"],
                "provenance_count": len(provenance_checks),
                "provenance_verified_count": sum(item["ok"] for item in provenance_checks),
                "unsupported_excerpt_count": len(unsupported_checks),
                "unsupported_verified_count": sum(item["ok"] for item in unsupported_checks),
                "authoritative_text_expected_count": authority_measurement["expected_count"],
                "authoritative_text_matched_count": authority_measurement["matched_count"],
                "optional_context_expected_count": optional_measurement["expected_count"],
                "optional_context_matched_count": optional_measurement["matched_count"],
                "missing_provenance_fields": current_missing,
                "blocking_conflict_count": len(current_blocking),
                "warning_count": len(state.get("parse_warnings") or []),
            }
        )

    latest_state = parse_story_project_semantic_state(
        root, chapters[-1], project_identity=identity
    )
    managed_round_trip = _managed_round_trip_probe(
        root,
        identity.book_id,
        chapter=chapters[-1],
        source_digest=latest_state["source_digest"],
    )
    evidence = {
        "target_sample_count": 1,
        "format_variant_count": len(source_kinds),
        "managed_round_trip_rate": 1.0 if managed_round_trip["ok"] else 0.0,
        "required_field_exact_match_rate": _rate(
            matched_authoritative_values, total_authoritative_values
        ),
        "authoritative_precision": _rate(
            matched_authoritative_values, total_authoritative_values
        ),
        "supported_optional_recall": _rate(matched_optional_values, total_optional_values),
        "unsupported_structure_count": total_unsupported,
        "unsupported_structure_captured_count": verified_unsupported,
        "consecutive_shadow_chapters": len(observations),
        "blocking_conflict_count": blocking_conflict_count,
        "missing_provenance_fields": sorted(missing_provenance_fields),
    }
    candidate_report = build_story_state_calibration_report(
        book_id=identity.book_id,
        parser_version=_single_value(parser_versions, "parser_version"),
        semantic_schema_version=_single_value(schema_versions, "semantic_schema_version"),
        target_layout_profile_version=_single_value(layout_profiles, "layout_profile_version"),
        evidence=evidence,
    )
    audit: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "story_state_calibration_audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "book_id": identity.book_id,
        "chapter_range": {"start": chapters[0], "end": chapters[-1], "count": len(chapters)},
        "parser_version": _single_value(parser_versions, "parser_version"),
        "semantic_schema_version": _single_value(schema_versions, "semantic_schema_version"),
        "layout_profile_versions": sorted(layout_profiles),
        "observations": observations,
        "automated_measurements": {
            "source_kind_variants": sorted(source_kinds),
            "provenance_span_integrity_rate": _rate(verified_provenance, total_provenance),
            "unsupported_excerpt_integrity_rate": _rate(verified_unsupported, total_unsupported),
            "authoritative_text_expected_count": total_authoritative_values,
            "authoritative_text_matched_count": matched_authoritative_values,
            "optional_context_expected_count": total_optional_values,
            "optional_context_matched_count": matched_optional_values,
        },
        "calibration_evidence": evidence,
        "measurement_definitions": {
            "required_field_exact_match_rate": "authoritative semantic text values found in their independently hash-verified source spans",
            "authoritative_precision": "same source-span measurement; non-textual IDs and derived status enums are excluded",
            "supported_optional_recall": "optional context labels and pending-thread list entries found by an independent Markdown scan and present in semantic state",
            "consecutive_shadow_chapters": "one non-authoritative semantic Shadow observation for every chapter in the requested consecutive range",
        },
        "managed_round_trip": managed_round_trip,
        "qualification": {
            "strict_eligible": candidate_report["strict_eligible"],
            "blockers": candidate_report["strict_blockers"],
            "reason": (
                "This report is derived from source-span and independent Markdown checks. "
                "Review the measurement definitions before using the companion calibration report."
            ),
        },
        "candidate_calibration_report": candidate_report,
        "audit_sha256": "",
    }
    audit["audit_sha256"] = _audit_sha256(audit)
    return audit


def write_calibration_audit(path: str | Path, audit: Mapping[str, Any]) -> Path:
    destination = Path(path).resolve(strict=False)
    if destination.suffix.lower() != ".json":
        raise CalibrationAuditError("audit output must use a .json extension")
    if destination.exists():
        raise CalibrationAuditError(f"refusing to overwrite existing audit: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_create_json(destination, dict(audit))
    return destination


def write_candidate_calibration_report(path: str | Path, audit: Mapping[str, Any]) -> Path:
    candidate = audit.get("candidate_calibration_report")
    if not isinstance(candidate, Mapping):
        raise CalibrationAuditError("audit has no candidate calibration report")
    if candidate.get("strict_eligible") is not True:
        raise CalibrationAuditError(
            "audit measurements do not qualify Strict: "
            + ", ".join(str(item) for item in candidate.get("strict_blockers") or [])
        )
    return write_calibration_audit(path, candidate)


def _verify_provenance(root: Path, items: Iterable[Any]) -> list[dict[str, Any]]:
    cache: dict[str, str] = {}
    checks: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            checks.append({"ok": False, "reason": "provenance_item_not_object"})
            continue
        source_path = str(raw.get("source_path") or "")
        start = raw.get("start_char")
        end = raw.get("end_char")
        expected_sha = str(raw.get("source_sha256") or "")
        try:
            source = _safe_source_path(root, source_path)
            text = cache.setdefault(source_path, source.read_text(encoding="utf-8-sig"))
            range_ok = isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
            hash_ok = hashlib.sha256(text.encode("utf-8")).hexdigest() == expected_sha
            checks.append({"ok": bool(range_ok and hash_ok), "source_path": source_path})
        except (OSError, UnicodeError, ValueError):
            checks.append({"ok": False, "source_path": source_path})
    return checks


def _verify_unsupported(root: Path, items: Iterable[Any]) -> list[dict[str, Any]]:
    cache: dict[str, str] = {}
    checks: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            checks.append({"ok": False, "reason": "unsupported_item_not_object"})
            continue
        source_path = str(raw.get("source_path") or "")
        start = raw.get("start_char")
        end = raw.get("end_char")
        expected_sha = str(raw.get("sha256") or "")
        try:
            source = _safe_source_path(root, source_path)
            text = cache.setdefault(source_path, source.read_text(encoding="utf-8-sig"))
            range_ok = isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
            actual_sha = hashlib.sha256(text[start:end].encode("utf-8")).hexdigest() if range_ok else ""
            checks.append({"ok": bool(range_ok and actual_sha == expected_sha), "source_path": source_path})
        except (OSError, UnicodeError, ValueError):
            checks.append({"ok": False, "source_path": source_path})
    return checks


def _managed_round_trip_probe(root: Path, book_id: str, *, chapter: int, source_digest: str) -> dict[str, Any]:
    context = root / "追踪" / "上下文.md"
    try:
        original = context.read_text(encoding="utf-8-sig")
        base_digest = compute_base_source_digest(original)
        projection = build_managed_projection(
            scope="context",
            book_id=book_id,
            run_id="calibration-audit-probe",
            chapter=chapter,
            parser_version=SEMANTIC_PARSER_VERSION,
            base_revision=hashlib.sha256(original.encode("utf-8")).hexdigest(),
            base_source_digest=base_digest,
            owned_fields=["story_state.calibration_audit_probe"],
            values={"story_state.calibration_audit_probe": {"source_digest": source_digest}},
        )
        parsed = parse_managed_block(write_managed_block(original, projection))
        ok = parsed is not None and parsed.projection == projection
        return {"ok": ok, "target": "追踪/上下文.md", "projection_sha256": projection["payload_sha256"]}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"ok": False, "target": "追踪/上下文.md", "error": str(exc)}


def _measure_authoritative_text(state: Mapping[str, Any], root: Path) -> dict[str, int]:
    provenance = [
        item
        for item in state.get("provenance", [])
        if isinstance(item, Mapping) and item.get("authority_class") == "authoritative"
    ]
    paths = {str(item.get("field_path") or "") for item in provenance}
    source_cache: dict[str, str] = {}
    expected = 0
    matched = 0
    for item in provenance:
        field_path = str(item.get("field_path") or "")
        if any(other != field_path and other.startswith(field_path + ".") for other in paths):
            continue
        value = _path_value(state, field_path)
        candidates = _text_values(value)
        if not candidates:
            continue
        source_path = str(item.get("source_path") or "")
        start = item.get("start_char")
        end = item.get("end_char")
        try:
            source = _safe_source_path(root, source_path)
            text = source_cache.setdefault(source_path, source.read_text(encoding="utf-8-sig"))
            excerpt = text[start:end] if isinstance(start, int) and isinstance(end, int) else ""
        except (OSError, UnicodeError, ValueError):
            excerpt = ""
        for candidate in candidates:
            expected += 1
            matched += int(_normalized(candidate) in _normalized(excerpt))
    return {"expected_count": expected, "matched_count": matched}


def _measure_optional_context_recall(state: Mapping[str, Any], root: Path) -> dict[str, int]:
    path = root / "追踪" / "上下文.md"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {"expected_count": 0, "matched_count": 0}
    labels = {
        "最近决定": "recent_decision",
        "上一章结尾": "previous_ending",
        "上一章收束": "previous_ending",
        "openingbridge": "opening_bridge",
    }
    expected_values: list[tuple[str, str]] = []
    in_pending = False
    pending_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Legacy writeback blocks can follow the human-maintained pending list
        # without another Markdown heading.  They are audit metadata, not
        # unresolved narrative threads.
        if "NovelAgent:story_project_writeback" in line:
            in_pending = False
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            in_pending = "待处理" in heading.group(1) or "未结" in heading.group(1)
            continue
        if in_pending:
            item = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
            if item:
                expected_values.append((f"story_state.pending_threads[{pending_index}]", item))
                pending_index += 1
            continue
        match = re.match(r"^(?:[-*+]\s*)?([^:=：]+?)\s*(?:[:：=])\s*(.+?)\s*$", line, re.IGNORECASE)
        if match is None:
            continue
        label = re.sub(r"\s+", "", match.group(1)).lower()
        field = labels.get(label)
        if field:
            expected_values.append((f"story_state.{field}", match.group(2).strip()))
    matched = sum(
        _normalized(str(_path_value(state, field_path) or "")) == _normalized(expected)
        for field_path, expected in expected_values
    )
    return {"expected_count": len(expected_values), "matched_count": matched}


def _path_value(value: Any, field_path: str) -> Any:
    current = value
    parts = re.sub(r"\[(\d+)\]", r".\1", field_path).split(".")
    for part in parts:
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list):
            current = next(
                (item for item in current if isinstance(item, Mapping) and str(item.get("id")) == part),
                None,
            ) if not part.isdigit() else (current[int(part)] if int(part) < len(current) else None)
        else:
            return None
    return current


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key, child in value.items():
            if key in {"id", "status"}:
                continue
            values.extend(_text_values(child))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for child in value:
            values.extend(_text_values(child))
        return values
    return []


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def _safe_source_path(root: Path, relative_path: str) -> Path:
    candidate = root / Path(PurePosixPath(relative_path))
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CalibrationAuditError(f"source path escapes StoryProject: {relative_path}") from exc
    return resolved


def _single_value(values: set[str], label: str) -> str:
    if len(values) != 1:
        raise CalibrationAuditError(f"{label} changed across the requested observations")
    return next(iter(values))


def _rate(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _audit_sha256(audit: Mapping[str, Any]) -> str:
    payload = dict(audit)
    payload.pop("audit_sha256", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect source-bound StoryProject calibration audit evidence.")
    parser.add_argument("--story-project", required=True)
    parser.add_argument("--start-chapter", type=int, required=True)
    parser.add_argument("--chapter-count", type=int, default=10)
    parser.add_argument("--out", required=True, help="New JSON path; existing files are never overwritten.")
    parser.add_argument("--report-out", help="New qualified report JSON path; written only if audit qualifies Strict.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    audit = collect_calibration_audit(
        args.story_project,
        start_chapter=args.start_chapter,
        chapter_count=args.chapter_count,
    )
    destination = write_calibration_audit(args.out, audit)
    report_destination = write_candidate_calibration_report(args.report_out, audit) if args.report_out else None
    print(json.dumps({"ok": True, "audit": str(destination), "audit_sha256": audit["audit_sha256"], "calibration_report": str(report_destination) if report_destination else None, "strict_eligible": audit["qualification"]["strict_eligible"], "blockers": audit["qualification"]["blockers"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
