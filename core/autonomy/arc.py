from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    atomic_replace_json,
    canonical_hash,
    load_json_object,
    now_utc,
    positive_int,
    required_text,
    safe_id,
    sha256_digest,
    state_lock,
    validate_mapping,
)
from core.autonomy.plans import validate_instruction_plan


_GOAL_FIELDS = (
    "mainline",
    "relationship",
    "escalation",
    "resource_cost",
    "foreshadowing",
)


class ArcPlanError(AutonomyContractError):
    pass


def build_run_arc_plan(
    instruction_plan: Mapping[str, Any],
    *,
    session_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    plan = validate_instruction_plan(instruction_plan)
    resolved_session = safe_id("session_id", session_id)
    arc_seed = canonical_hash(
        {"plan_hash": plan["plan_hash"], "session_id": resolved_session, "kind": "run_arc"}
    )
    targets = []
    count = int(plan["requested_chapter_count"])
    for offset, chapter_index in enumerate(
        range(int(plan["chapter_start"]), int(plan["chapter_end"]) + 1), start=1
    ):
        planned = {
            "mainline": f"推进本次区间主线节点 {offset}/{count}",
            "relationship": f"落实本次区间关系变化 {offset}/{count}",
            "escalation": f"完成本次区间升级节奏 {offset}/{count}",
            "resource_cost": f"记录本次区间资源代价 {offset}/{count}",
            "foreshadowing": f"处理本次区间伏笔播种或回收 {offset}/{count}",
        }
        targets.append(
            {
                "chapter_index": chapter_index,
                "planned": planned,
                "target_hash": canonical_hash(
                    {"chapter_index": chapter_index, "planned": planned}
                ),
                "fulfilled": None,
                "differences": [],
                "completion_receipt_hash": None,
                "adjustment_note": None,
            }
        )
    arc_plan = {
        "schema_version": "1.0",
        "arc_plan_id": f"arc_{arc_seed[:24]}",
        "instruction_plan_id": plan["plan_id"],
        "instruction_plan_hash": plan["plan_hash"],
        "session_id": resolved_session,
        "book_id": plan["source_snapshot"]["book_id"],
        "source_snapshot_hash": plan["source_snapshot"]["snapshot_hash"],
        "chapter_start": plan["chapter_start"],
        "chapter_end": plan["chapter_end"],
        "revision": 1,
        "previous_arc_plan_hash": None,
        "targets": targets,
        "adjustments": [],
        "created_at": created_at or now_utc(),
        "updated_at": created_at or now_utc(),
    }
    arc_plan["arc_plan_hash"] = canonical_hash(
        arc_plan, exclude_fields=("arc_plan_hash",)
    )
    return validate_run_arc_plan(arc_plan)


def validate_run_arc_plan(value: Any) -> dict[str, Any]:
    plan = validate_mapping(value, "run_arc_plan.schema.json", "RunArcPlan")
    for field in ("arc_plan_id", "instruction_plan_id", "session_id", "book_id"):
        safe_id(field, plan[field])
    for field in (
        "arc_plan_hash",
        "instruction_plan_hash",
        "source_snapshot_hash",
    ):
        sha256_digest(field, plan[field])
    sha256_digest(
        "previous_arc_plan_hash", plan["previous_arc_plan_hash"], optional=True
    )
    start = positive_int("chapter_start", plan["chapter_start"])
    end = positive_int("chapter_end", plan["chapter_end"])
    revision = positive_int("revision", plan["revision"])
    if end < start or len(plan["targets"]) != end - start + 1:
        raise ArcPlanError("arc_plan_range_invalid", "arc targets must cover the complete range")
    if (revision == 1) != (plan["previous_arc_plan_hash"] is None):
        raise ArcPlanError(
            "arc_plan_revision_invalid", "only the first revision may omit its predecessor hash"
        )
    expected_chapter = start
    seen_adjustments: set[tuple[int, int]] = set()
    for target in plan["targets"]:
        if target["chapter_index"] != expected_chapter:
            raise ArcPlanError(
                "arc_plan_range_invalid", "arc target chapters must be contiguous and ordered"
            )
        _validate_goal("planned", target["planned"])
        expected_target_hash = canonical_hash(
            {"chapter_index": target["chapter_index"], "planned": target["planned"]}
        )
        if target["target_hash"] != expected_target_hash:
            raise ArcPlanError("arc_target_hash_mismatch", "planned target was modified")
        sha256_digest("target_hash", target["target_hash"])
        fulfilled = target["fulfilled"]
        if fulfilled is not None:
            _validate_goal("fulfilled", fulfilled)
            sha256_digest("completion_receipt_hash", target["completion_receipt_hash"])
            expected_differences = [
                field for field in _GOAL_FIELDS if fulfilled[field] != target["planned"][field]
            ]
            if target["differences"] != expected_differences:
                raise ArcPlanError(
                    "arc_fulfillment_diff_invalid", "fulfilled differences must be deterministically derived"
                )
        elif target["completion_receipt_hash"] is not None or target["differences"]:
            raise ArcPlanError(
                "arc_fulfillment_invalid", "unfulfilled target cannot carry completion evidence"
            )
        expected_chapter += 1
    for adjustment in plan["adjustments"]:
        chapter = positive_int("adjustment.chapter_index", adjustment["chapter_index"])
        adjustment_revision = positive_int("adjustment.revision", adjustment["revision"])
        if chapter < start or chapter > end or adjustment_revision > revision:
            raise ArcPlanError("arc_adjustment_invalid", "adjustment is outside the arc revision")
        _validate_goal("adjustment.before", adjustment["before"])
        _validate_goal("adjustment.after", adjustment["after"])
        required_text("adjustment.reason", adjustment["reason"])
        key = (adjustment_revision, chapter)
        if key in seen_adjustments:
            raise ArcPlanError("arc_adjustment_duplicate", "duplicate adjustment revision/chapter")
        seen_adjustments.add(key)
    expected_hash = canonical_hash(plan, exclude_fields=("arc_plan_hash",))
    if plan["arc_plan_hash"] != expected_hash:
        raise ArcPlanError("arc_plan_hash_mismatch", "RunArcPlan content was modified")
    return plan


class ArcPlanStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def create(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_run_arc_plan(plan)
        directory = self._directory(validated["arc_plan_id"])
        revision_path = directory / "revisions" / "000001.json"
        head_path = directory / "head.json"
        with state_lock(self.root, head_path):
            atomic_append_json(revision_path, validated)
            if head_path.exists():
                existing = self._load_reconciled_fenced(validated["arc_plan_id"])
                if existing != validated:
                    raise ArcPlanError("arc_plan_create_conflict", "arc plan id already differs")
                return existing
            atomic_replace_json(head_path, _head(validated))
        return validated

    def load(self, arc_plan_id: str) -> dict[str, Any]:
        resolved = safe_id("arc_plan_id", arc_plan_id)
        with state_lock(self.root, self._directory(resolved) / "head.json"):
            return self._load_reconciled_fenced(resolved)

    def reconcile(self, arc_plan_id: str) -> dict[str, Any]:
        """Roll the unique contiguous revision chain into its mutable head."""

        return self.load(arc_plan_id)

    def _load_reconciled_fenced(self, arc_plan_id: str) -> dict[str, Any]:
        directory = self._directory(arc_plan_id)
        revision_paths = sorted(
            (directory / "revisions").glob(
                "[0-9][0-9][0-9][0-9][0-9][0-9].json"
            )
        )
        if not revision_paths:
            raise ArcPlanError("arc_plan_revision_missing", "arc plan has no durable revision")
        revisions: list[dict[str, Any]] = []
        previous: str | None = None
        for sequence, path in enumerate(revision_paths, start=1):
            if path.name != f"{sequence:06d}.json":
                raise ArcPlanError(
                    "arc_plan_revision_gap", "arc plan revision sequence has a gap"
                )
            plan = validate_run_arc_plan(load_json_object(path))
            if (
                plan["arc_plan_id"] != arc_plan_id
                or int(plan["revision"]) != sequence
                or plan["previous_arc_plan_hash"] != previous
            ):
                raise ArcPlanError(
                    "arc_plan_revision_chain_broken",
                    "arc plan revision changed scope or predecessor",
                )
            revisions.append(plan)
            previous = plan["arc_plan_hash"]
        head_path = directory / "head.json"
        if head_path.exists():
            head = load_json_object(head_path)
            if head.get("arc_plan_id") != arc_plan_id:
                raise ArcPlanError(
                    "arc_plan_head_mismatch", "arc plan head changed scope"
                )
            revision = positive_int("head.revision", head.get("revision"))
            expected_hash = sha256_digest(
                "head.arc_plan_hash", head.get("arc_plan_hash")
            )
            if revision > len(revisions) or revisions[revision - 1][
                "arc_plan_hash"
            ] != expected_hash:
                raise ArcPlanError(
                    "arc_plan_head_mismatch", "arc plan head does not match revision"
                )
        latest = revisions[-1]
        if not head_path.exists() or load_json_object(head_path) != _head(latest):
            atomic_replace_json(head_path, _head(latest))
        return latest

    def adjust_uncommitted(
        self,
        arc_plan_id: str,
        *,
        chapter_index: int,
        planned: Mapping[str, Any],
        reason: str,
        expected_arc_plan_hash: str,
        committed_chapters: set[int] | frozenset[int],
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        directory = self._directory(arc_plan_id)
        head_path = directory / "head.json"
        with state_lock(self.root, head_path):
            current = self._load_reconciled_fenced(arc_plan_id)
            replacement = _validate_goal("planned", planned)
            chapter = positive_int("chapter_index", chapter_index)
            target = _target_for(current, chapter)
            resolved_reason = required_text("reason", reason)
            if (
                target["fulfilled"] is None
                and target["planned"] == replacement
                and target["adjustment_note"] == resolved_reason
            ):
                return current
            if current["arc_plan_hash"] != sha256_digest(
                "expected_arc_plan_hash", expected_arc_plan_hash
            ):
                raise ArcPlanError("arc_plan_cas_failed", "arc plan head changed")
            if chapter in committed_chapters or target["fulfilled"] is not None:
                raise ArcPlanError(
                    "arc_target_already_committed", "a committed chapter target cannot be rewritten"
                )
            revised = copy.deepcopy(current)
            revised["revision"] += 1
            revised["previous_arc_plan_hash"] = current["arc_plan_hash"]
            revised_target = _target_for(revised, chapter)
            before = copy.deepcopy(revised_target["planned"])
            revised_target["planned"] = replacement
            revised_target["target_hash"] = canonical_hash(
                {"chapter_index": chapter, "planned": replacement}
            )
            revised_target["adjustment_note"] = resolved_reason
            timestamp = recorded_at or now_utc()
            revised["adjustments"].append(
                {
                    "revision": revised["revision"],
                    "chapter_index": chapter,
                    "before": before,
                    "after": copy.deepcopy(replacement),
                    "reason": resolved_reason,
                    "recorded_at": timestamp,
                }
            )
            revised["updated_at"] = timestamp
            revised["arc_plan_hash"] = canonical_hash(
                revised, exclude_fields=("arc_plan_hash",)
            )
            validated = validate_run_arc_plan(revised)
            self._publish_revision(directory, validated)
            atomic_replace_json(head_path, _head(validated))
            return validated

    def record_fulfillment(
        self,
        arc_plan_id: str,
        *,
        chapter_index: int,
        fulfilled: Mapping[str, Any],
        completion_receipt_hash: str,
        expected_arc_plan_hash: str,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        directory = self._directory(arc_plan_id)
        head_path = directory / "head.json"
        with state_lock(self.root, head_path):
            current = self._load_reconciled_fenced(arc_plan_id)
            chapter = positive_int("chapter_index", chapter_index)
            target = _target_for(current, chapter)
            evidence = sha256_digest("completion_receipt_hash", completion_receipt_hash)
            resolved = _validate_goal("fulfilled", fulfilled)
            if target["fulfilled"] is not None:
                if target["fulfilled"] == resolved and target["completion_receipt_hash"] == evidence:
                    return current
                raise ArcPlanError(
                    "arc_target_already_committed", "chapter fulfillment is immutable"
                )
            if current["arc_plan_hash"] != sha256_digest(
                "expected_arc_plan_hash", expected_arc_plan_hash
            ):
                raise ArcPlanError("arc_plan_cas_failed", "arc plan head changed")
            revised = copy.deepcopy(current)
            revised["revision"] += 1
            revised["previous_arc_plan_hash"] = current["arc_plan_hash"]
            revised_target = _target_for(revised, chapter)
            revised_target["fulfilled"] = resolved
            revised_target["differences"] = [
                field
                for field in _GOAL_FIELDS
                if resolved[field] != revised_target["planned"][field]
            ]
            revised_target["completion_receipt_hash"] = evidence
            revised["updated_at"] = recorded_at or now_utc()
            revised["arc_plan_hash"] = canonical_hash(
                revised, exclude_fields=("arc_plan_hash",)
            )
            validated = validate_run_arc_plan(revised)
            self._publish_revision(directory, validated)
            atomic_replace_json(head_path, _head(validated))
            return validated

    def _directory(self, arc_plan_id: str) -> Path:
        return self.root / "arc_plans" / safe_id("arc_plan_id", arc_plan_id)

    @staticmethod
    def _publish_revision(directory: Path, plan: Mapping[str, Any]) -> None:
        path = directory / "revisions" / f"{int(plan['revision']):06d}.json"
        atomic_append_json(path, plan)


def _head(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "arc_plan_id": plan["arc_plan_id"],
        "revision": plan["revision"],
        "arc_plan_hash": plan["arc_plan_hash"],
    }


def _target_for(plan: Mapping[str, Any], chapter_index: int) -> dict[str, Any]:
    for target in plan["targets"]:
        if target["chapter_index"] == chapter_index:
            return target
    raise ArcPlanError("arc_target_unknown", f"chapter {chapter_index} is outside this arc")


def _validate_goal(label: str, value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_GOAL_FIELDS):
        raise ArcPlanError(
            "arc_goal_invalid", f"{label} must contain exactly {', '.join(_GOAL_FIELDS)}"
        )
    return {field: required_text(f"{label}.{field}", value[field]) for field in _GOAL_FIELDS}


__all__ = [
    "ArcPlanError",
    "ArcPlanStore",
    "build_run_arc_plan",
    "validate_run_arc_plan",
]
