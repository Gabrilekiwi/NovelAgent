from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from core.autonomy.common import (
    AutonomyContractError,
    canonical_hash,
    now_utc,
    positive_int,
    required_text,
    safe_id,
    sha256_digest,
    validate_mapping,
)
from core.autonomy.profiles import TrustedProfiles, TrustedProfilesError
from core.context_budget import (
    CJK_CHARACTER_OUTPUT_ESTIMATOR,
    preview_chinese_output_compatibility,
)


_SELECTOR_KINDS = {
    "story": "story_projects",
    "provider": "provider_models",
    "delivery": "file_deliveries",
    "budget": "budgets",
    "quality": "quality_policies",
}
_FORBIDDEN_INSTRUCTION = re.compile(
    r"(?ix)(?:"
    r"notion|api[_ -]?key|credential|password|secret|access[_ -]?token|"
    r"env(?:ironment)?\s+var(?:iable)?s?|file://|"
    r"环境变量|凭据|密钥|密码|令牌|"
    r"(?:^|\s)(?:env|path|root|directory|file)\s*=|"
    r"(?:[a-z]:[\\/])|(?:\\\\)|(?:\.\.[\\/])|(?:~[\\/])|"
    r"(?:^|\s)/(?:[^/\s]+/)*[^/\s]+|"
    r"提高.{0,4}预算|增加.{0,4}预算|预算.{0,4}无上限|"
    r"(?:increase|raise|higher|larger).{0,8}budget|"
    r"unlimited.{0,8}budget|max_(?:input|output|calls|wall)\s*="
    r")"
)
_SELECTOR = re.compile(
    r"(?<![A-Za-z0-9_.-])(story|provider|delivery|budget|quality)\s*=\s*"
    r"([A-Za-z0-9][A-Za-z0-9._:-]{0,159})",
    re.IGNORECASE,
)
_CHAPTER_COUNT = re.compile(r"(?<!\d)(\d{1,6})\s*(?:章|chapters?\b)", re.IGNORECASE)


class AutonomyPlanError(AutonomyContractError):
    pass


def build_source_snapshot(
    *,
    book_id: str,
    root_uuid: str,
    authority_epoch: int,
    authority_head_event_hash: str | None,
    canonical_next_chapter: int,
    source_digest: str,
    captured_at: str | None = None,
) -> dict[str, Any]:
    snapshot = {
        "schema_version": "1.0",
        "book_id": safe_id("book_id", book_id),
        "root_uuid": safe_id("root_uuid", root_uuid),
        "authority_epoch": positive_int("authority_epoch", authority_epoch, minimum=0),
        "authority_head_event_hash": sha256_digest(
            "authority_head_event_hash", authority_head_event_hash, optional=True
        ),
        "canonical_next_chapter": positive_int(
            "canonical_next_chapter", canonical_next_chapter
        ),
        "source_digest": sha256_digest("source_digest", source_digest),
        "captured_at": captured_at or now_utc(),
    }
    snapshot["snapshot_hash"] = canonical_hash(
        snapshot, exclude_fields=("captured_at", "snapshot_hash")
    )
    return validate_source_snapshot(snapshot)


def validate_source_snapshot(value: Any) -> dict[str, Any]:
    snapshot = validate_mapping(value, "autonomy_source_snapshot.schema.json", "SourceSnapshot")
    safe_id("book_id", snapshot["book_id"])
    safe_id("root_uuid", snapshot["root_uuid"])
    positive_int("authority_epoch", snapshot["authority_epoch"], minimum=0)
    sha256_digest(
        "authority_head_event_hash", snapshot["authority_head_event_hash"], optional=True
    )
    positive_int("canonical_next_chapter", snapshot["canonical_next_chapter"])
    sha256_digest("source_digest", snapshot["source_digest"])
    sha256_digest("snapshot_hash", snapshot["snapshot_hash"])
    expected = canonical_hash(snapshot, exclude_fields=("captured_at", "snapshot_hash"))
    if snapshot["snapshot_hash"] != expected:
        raise AutonomyPlanError(
            "source_snapshot_hash_mismatch", "StoryProject source snapshot was modified"
        )
    return snapshot


def compile_instruction_plan(
    instruction: str,
    *,
    trusted_profiles: TrustedProfiles,
    source_snapshot: Mapping[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    text = required_text("instruction", instruction)
    _assert_instruction_safe(text)
    source = validate_source_snapshot(source_snapshot)
    selectors = _selectors(text)

    selections = {
        "story_project": trusted_profiles.public_snapshot(
            "story_projects", selectors.get("story")
        ),
        "provider_model": trusted_profiles.public_snapshot(
            "provider_models", selectors.get("provider")
        ),
        "file_delivery": trusted_profiles.public_snapshot(
            "file_deliveries", selectors.get("delivery")
        ),
        "budget": trusted_profiles.public_snapshot("budgets", selectors.get("budget")),
        "quality_policy": trusted_profiles.public_snapshot(
            "quality_policies", selectors.get("quality")
        ),
    }
    if selections["story_project"]["book_id"] != source["book_id"]:
        raise AutonomyPlanError(
            "instruction_story_project_mismatch", "selected StoryProject does not own the source snapshot"
        )
    if selections["story_project"]["root_uuid"] != source["root_uuid"]:
        raise AutonomyPlanError(
            "instruction_story_project_mismatch", "selected StoryProject root UUID changed"
        )
    count = _requested_chapters(text)
    maximum = int(selections["budget"]["max_chapters"])
    if count > maximum:
        raise AutonomyPlanError(
            "instruction_budget_escalation",
            f"requested {count} chapters exceeds trusted budget profile limit {maximum}",
        )
    start = int(source["canonical_next_chapter"])
    plan = {
        "schema_version": "1.0",
        "plan_id": "pending",
        "plan_hash": "0" * 64,
        "state": "preview",
        "intent": "generate_contiguous_canonical_chapters",
        "instruction_digest": canonical_hash({"instruction": text}),
        "profile_set_id": trusted_profiles.profile_set_id,
        "profile_set_hash": trusted_profiles.profile_set_hash,
        "source_snapshot": copy.deepcopy(source),
        "selections": selections,
        "requested_chapter_count": count,
        "chapter_start": start,
        "chapter_end": start + count - 1,
        "created_at": created_at or now_utc(),
    }
    plan_hash = canonical_hash(plan, exclude_fields=("plan_id", "plan_hash"))
    plan["plan_hash"] = plan_hash
    plan["plan_id"] = f"plan_{plan_hash[:24]}"
    return validate_instruction_plan(plan, trusted_profiles=trusted_profiles)


def validate_instruction_plan(
    value: Any,
    *,
    trusted_profiles: TrustedProfiles | None = None,
    current_source_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    plan = validate_mapping(value, "instruction_plan.schema.json", "InstructionPlan")
    safe_id("plan_id", plan["plan_id"])
    sha256_digest("plan_hash", plan["plan_hash"])
    sha256_digest("instruction_digest", plan["instruction_digest"])
    sha256_digest("profile_set_hash", plan["profile_set_hash"])
    source = validate_source_snapshot(plan["source_snapshot"])
    expected_hash = canonical_hash(plan, exclude_fields=("plan_id", "plan_hash"))
    expected_id = f"plan_{expected_hash[:24]}"
    if plan["plan_hash"] != expected_hash or plan["plan_id"] != expected_id:
        raise AutonomyPlanError(
            "instruction_plan_hash_mismatch", "InstructionPlan content was modified after preview"
        )
    count = positive_int("requested_chapter_count", plan["requested_chapter_count"])
    start = positive_int("chapter_start", plan["chapter_start"])
    end = positive_int("chapter_end", plan["chapter_end"])
    output_compatibility = preview_chinese_output_compatibility(
        int(plan["selections"]["provider_model"]["max_output_tokens"]),
        calibrated_estimator=CJK_CHARACTER_OUTPUT_ESTIMATOR,
    )
    if not output_compatibility["compatible"]:
        raise AutonomyPlanError(
            "instruction_output_budget_incompatible",
            "trusted provider output cap cannot cover the 3000-4500 Chinese-character target "
            f"with the calibrated safety margin; shortfall={output_compatibility['shortfall_tokens']} tokens",
        )
    if start != source["canonical_next_chapter"] or end != start + count - 1:
        raise AutonomyPlanError(
            "instruction_plan_range_invalid", "plan chapters must be one contiguous canonical-next range"
        )
    if trusted_profiles is not None:
        if plan["profile_set_id"] != trusted_profiles.profile_set_id:
            raise AutonomyPlanError(
                "instruction_profile_set_mismatch", "plan belongs to another trusted profile set"
            )
        if plan["profile_set_hash"] != trusted_profiles.profile_set_hash:
            raise AutonomyPlanError(
                "instruction_profile_set_drift", "trusted profiles changed after plan preview"
            )
        for key, kind in (
            ("story_project", "story_projects"),
            ("provider_model", "provider_models"),
            ("file_delivery", "file_deliveries"),
            ("budget", "budgets"),
            ("quality_policy", "quality_policies"),
        ):
            trusted_profiles.assert_snapshot(kind, plan["selections"][key])
        if count > int(plan["selections"]["budget"]["max_chapters"]):
            raise AutonomyPlanError(
                "instruction_budget_escalation", "plan exceeds its trusted budget snapshot"
            )
    if current_source_snapshot is not None:
        current = validate_source_snapshot(current_source_snapshot)
        if current["snapshot_hash"] != source["snapshot_hash"]:
            raise AutonomyPlanError(
                "instruction_source_snapshot_stale",
                "StoryProject authority, canonical next chapter, or source bytes changed",
            )
    return plan


def _assert_instruction_safe(text: str) -> None:
    match = _FORBIDDEN_INSTRUCTION.search(text)
    if match is not None:
        raise AutonomyPlanError(
            "instruction_capability_forbidden",
            "instruction may not authorize paths, Notion, environment/credentials, or higher budgets",
        )


def _selectors(text: str) -> dict[str, str]:
    selected: dict[str, str] = {}
    for match in _SELECTOR.finditer(text):
        kind = match.group(1).lower()
        profile_id = match.group(2)
        previous = selected.get(kind)
        if previous is not None and previous != profile_id:
            raise AutonomyPlanError(
                "instruction_selector_ambiguous", f"multiple {kind} profiles were requested"
            )
        selected[kind] = profile_id
    return selected


def _requested_chapters(text: str) -> int:
    matches = {int(match.group(1)) for match in _CHAPTER_COUNT.finditer(text)}
    if len(matches) > 1:
        raise AutonomyPlanError(
            "instruction_chapter_count_ambiguous", "instruction contains conflicting chapter counts"
        )
    return next(iter(matches)) if matches else 1


__all__ = [
    "AutonomyPlanError",
    "build_source_snapshot",
    "compile_instruction_plan",
    "validate_instruction_plan",
    "validate_source_snapshot",
]
