from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

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
from core.autonomy.plans import story_brief_for_plan, validate_instruction_plan


_GOAL_FIELDS = (
    "mainline",
    "relationship",
    "escalation",
    "resource_cost",
    "foreshadowing",
)

_GOAL_LABELS = {
    "mainline": "主线",
    "relationship": "关系",
    "escalation": "升级",
    "resource_cost": "代价",
    "foreshadowing": "伏笔",
}

_ARC_PHASE_GOALS = {
    "单章闭环": {
        "mainline": "建立、推进并完成一个可验证的核心因果闭环",
        "relationship": "让关键人物立场或信任发生可追踪变化",
        "escalation": "把威胁推至必须回应的局面并给出阶段结果",
        "resource_cost": "让选择产生明确且不可无故撤销的资源或状态代价",
        "foreshadowing": "播种或回收一项与核心因果直接相连的伏笔",
    },
    "起势": {
        "mainline": "建立本区间核心冲突、行动方向与因果起点",
        "relationship": "显露关键人物的初始立场、信任张力或合作条件",
        "escalation": "提出会持续施压的威胁、期限或两难选择",
        "resource_cost": "暴露本区间最先受到约束的资源、能力或安全边界",
        "foreshadowing": "播种后续推进所需的线索、承诺或未解问题",
    },
    "展开": {
        "mainline": "沿既定因果推进一次不可替换的行动结果",
        "relationship": "用共同目标或利益冲突推动人物关系发生增量变化",
        "escalation": "让既有威胁获得新条件、范围或紧迫性",
        "resource_cost": "支付与本章推进相称的资源、能力或机会成本",
        "foreshadowing": "推进已播种线索并保留可追踪的后续指向",
    },
    "转折": {
        "mainline": "用新事实或失败结果改变主线的下一步路径",
        "relationship": "让关键关系因真相、选择或背离出现方向性转折",
        "escalation": "把冲突提升到旧方案无法直接解决的新层级",
        "resource_cost": "让转折留下持续生效的损失、伤势或资源缺口",
        "foreshadowing": "兑现一项早期线索，同时把影响导向后续章节",
    },
    "逼近": {
        "mainline": "收拢分支并迫使主角接近本区间关键决断",
        "relationship": "让同盟、对立或信任条件在决断前明确站位",
        "escalation": "压缩时间与选择空间，使核心冲突逼近临界点",
        "resource_cost": "累积并显化决战前不可忽略的资源与状态代价",
        "foreshadowing": "把关键线索推进到可在区间末兑现的位置",
    },
    "兑现": {
        "mainline": "兑现本区间核心因果并形成通往下一阶段的新局面",
        "relationship": "让关键人物关系对本区间选择给出明确后果",
        "escalation": "完成本轮冲突升级并保留有依据的后续压力",
        "resource_cost": "结算本区间累计代价且不重置已发生的损失",
        "foreshadowing": "回收应兑现线索，并播种由本次结果自然产生的新问题",
    },
}

_RELATIONSHIP_SIGNALS = (
    "ally",
    "allied",
    "allies",
    "alliance",
    "betray",
    "betrayal",
    "betrayed",
    "bond",
    "bonded",
    "enemy",
    "enemies",
    "friend",
    "friends",
    "friendship",
    "hate",
    "hated",
    "love",
    "loved",
    "relationship",
    "relationships",
    "trust",
    "trusted",
    "关系",
    "信任",
    "背叛",
    "盟友",
    "敌人",
    "朋友",
    "和解",
    "决裂",
)
_RESOURCE_COST_SIGNALS = (
    "cost",
    "costly",
    "costs",
    "loss",
    "losses",
    "lost",
    "sacrifice",
    "sacrificed",
    "spend",
    "spending",
    "spent",
    "代价",
    "损失",
    "牺牲",
    "消耗",
    "付出",
)
_ESCALATION_SIGNALS = (
    "attack",
    "attacked",
    "choice",
    "choose",
    "chosen",
    "conflict",
    "conflicts",
    "crisis",
    "danger",
    "dangerous",
    "escalate",
    "escalation",
    "infected",
    "infection",
    "rescue",
    "rescued",
    "secret",
    "threat",
    "threatened",
    *_RESOURCE_COST_SIGNALS,
    "冲突",
    "危险",
    "选择",
    "威胁",
    "感染",
    "救援",
    "秘密",
)


class ArcPlanError(AutonomyContractError):
    pass


def derive_arc_fulfillment(
    *,
    chapter_body: str,
    chapter_body_sha256: str,
    analysis: Mapping[str, Any],
) -> dict[str, str]:
    """Return the evidence-backed actual values for compatibility callers."""

    return derive_arc_fulfillment_assessment(
        chapter_body=chapter_body,
        chapter_body_sha256=chapter_body_sha256,
        analysis=analysis,
    )["fulfilled"]


def derive_arc_fulfillment_assessment(
    *,
    chapter_body: str,
    chapter_body_sha256: str,
    analysis: Mapping[str, Any],
) -> dict[str, Any]:
    """Project Receipt-bound analysis evidence into actual values and gaps."""

    if not isinstance(chapter_body, str) or not chapter_body.strip():
        raise ArcPlanError("arc_fulfillment_evidence_invalid", "chapter prose is required")
    body_hash = sha256_digest("chapter_body_sha256", chapter_body_sha256)
    if hashlib.sha256(chapter_body.encode("utf-8")).hexdigest() != body_hash:
        raise ArcPlanError(
            "arc_fulfillment_evidence_invalid", "chapter prose hash does not match"
        )
    if not isinstance(analysis, Mapping):
        raise ArcPlanError(
            "arc_fulfillment_evidence_invalid", "chapter analysis must be an object"
        )
    evidence = analysis.get("fulfillment_evidence")
    if isinstance(evidence, Mapping):
        validated_evidence = validate_mapping(
            evidence,
            "arc_fulfillment_evidence.schema.json",
            "ArcFulfillmentEvidence",
        )
        evidence_payload = dict(validated_evidence)
        evidence_hash = evidence_payload.pop("evidence_hash")
        if evidence_hash != canonical_hash(evidence_payload):
            raise ArcPlanError(
                "arc_fulfillment_evidence_invalid",
                "structured fulfillment evidence hash changed",
            )
        evidence_values: dict[str, Any] = {
            field: validated_evidence[field] for field in _GOAL_FIELDS
        }
        evidence_root_hash = evidence_hash
    else:
        # Historical Final RunRecords retained only summary/counts.  They stay
        # readable, but missing categories are explicit gaps rather than prose
        # slices pretending to be structured evidence.
        conflict_count = int(analysis.get("conflict_count", 0) or 0)
        event_count = int(analysis.get("event_count", 0) or 0)
        world_change_count = int(analysis.get("world_change_count", 0) or 0)
        evidence_values = {
            "mainline": analysis.get("summary"),
            "relationship": [],
            "escalation": (
                {"conflict_count": conflict_count, "event_count": event_count}
                if conflict_count or event_count
                else {}
            ),
            "resource_cost": (
                {"world_change_count": world_change_count}
                if world_change_count
                else {}
            ),
            "foreshadowing": [],
        }
        evidence_root_hash = canonical_hash(
            {"legacy_analysis_summary": dict(analysis)}
        )
    fulfilled: dict[str, str] = {}
    differences: list[str] = []
    for field in _GOAL_FIELDS:
        value = evidence_values[field]
        present = _arc_evidence_present(
            field, value, evidence_values=evidence_values
        )
        compact = (
            _compact_value(value)
            if present
            else f"未检测到{_GOAL_LABELS[field]}的结构化兑现证据"
        )
        if not present:
            differences.append(field)
        evidence_hash = canonical_hash(
            {
                "field": field,
                "chapter_body_sha256": body_hash,
                "fulfillment_evidence_hash": evidence_root_hash,
                "analysis_evidence": value if present else None,
            }
        )
        fulfilled[field] = (
            f"{_GOAL_LABELS[field]}{'实际兑现' if present else '证据缺口'}"
            f"[正文:{body_hash[:12]}/证据:{evidence_hash[:12]}]：{compact}"
        )
    return {
        "fulfilled": _validate_goal("fulfilled", fulfilled),
        "differences": differences,
        "fulfillment_evidence_hash": evidence_root_hash,
    }


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
    story_brief = story_brief_for_plan(plan)
    for offset, chapter_index in enumerate(
        range(int(plan["chapter_start"]), int(plan["chapter_end"]) + 1), start=1
    ):
        phase = _arc_phase(offset, count)
        prefix = f"故事叙事意图「{story_brief}」；跨章阶段“{phase}” {offset}/{count}"
        planned = {
            field: f"{prefix}：{_ARC_PHASE_GOALS[phase][field]}"
            for field in _GOAL_FIELDS
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
                "fulfillment_assessment": None,
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


def _arc_phase(offset: int, count: int) -> str:
    if count == 1:
        return "单章闭环"
    if offset == 1:
        return "起势"
    if offset == count:
        return "兑现"
    progress = (offset - 1) / (count - 1)
    if progress <= 0.34:
        return "展开"
    if progress <= 0.66:
        return "转折"
    return "逼近"


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
            assessment = target.get("fulfillment_assessment")
            if assessment is not None:
                validated_assessment = _validate_fulfillment_assessment(assessment)
                expected_differences = [
                    field
                    for field in _GOAL_FIELDS
                    if field not in validated_assessment["evidenced_fields"]
                ]
                if (
                    validated_assessment["planned_target_hash"] != target["target_hash"]
                    or validated_assessment["completion_receipt_hash"]
                    != target["completion_receipt_hash"]
                ):
                    raise ArcPlanError(
                        "arc_fulfillment_assessment_invalid",
                        "fulfillment assessment is bound to another target or completion",
                    )
            else:
                # Historical Arc revisions compared the human-readable actual
                # projection with the planned text.  Keep those revisions
                # readable without pretending that they carry evidence gaps.
                expected_differences = [
                    field
                    for field in _GOAL_FIELDS
                    if fulfilled[field] != target["planned"][field]
                ]
            if target["differences"] != expected_differences:
                raise ArcPlanError(
                    "arc_fulfillment_diff_invalid", "fulfilled differences must be deterministically derived"
                )
        elif (
            target["completion_receipt_hash"] is not None
            or target["differences"]
            or target.get("fulfillment_assessment") is not None
        ):
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
        differences: Sequence[str] | None = None,
        fulfillment_evidence_hash: str | None = None,
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
            if (differences is None) != (fulfillment_evidence_hash is None):
                raise ArcPlanError(
                    "arc_fulfillment_assessment_invalid",
                    "structured differences and fulfillment evidence hash must be supplied together",
                )
            if differences is not None:
                resolved_differences = _validate_differences(differences)
                resolved_assessment = _build_fulfillment_assessment(
                    differences=resolved_differences,
                    fulfillment_evidence_hash=fulfillment_evidence_hash,
                    planned_target_hash=target["target_hash"],
                    completion_receipt_hash=evidence,
                )
            else:
                resolved_differences = [
                    field
                    for field in _GOAL_FIELDS
                    if resolved[field] != target["planned"][field]
                ]
                resolved_assessment = None
            if target["fulfilled"] is not None:
                replay_matches = (
                    target["fulfilled"] == resolved
                    and target["completion_receipt_hash"] == evidence
                    and target.get("fulfillment_assessment") == resolved_assessment
                    and target["differences"] == resolved_differences
                )
                if replay_matches:
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
            revised_target["differences"] = resolved_differences
            revised_target["completion_receipt_hash"] = evidence
            revised_target["fulfillment_assessment"] = resolved_assessment
            differences = list(revised_target["differences"])
            timestamp = recorded_at or now_utc()
            if differences:
                reason = (
                    f"chapter_{chapter}_actual_fulfillment_adjusted_"
                    + "_".join(differences)
                )
                # Carry the delta into the next uncommitted target only.  That
                # target becomes the audited input to the following chapter,
                # so any still-missing fields propagate one chapter at a time.
                # Rewriting every remaining target here makes each immutable
                # revision grow quadratically while adding no extra authority.
                for future_target in revised["targets"]:
                    future_chapter = int(future_target["chapter_index"])
                    if future_chapter <= chapter or future_target["fulfilled"] is not None:
                        continue
                    before = copy.deepcopy(future_target["planned"])
                    after = copy.deepcopy(before)
                    for field in differences:
                        after[field] = _carry_forward_goal(
                            field=field,
                            source_chapter=chapter,
                            fulfilled_value=resolved[field],
                            planned_value=before[field],
                        )
                    if after != before:
                        future_target["planned"] = after
                        future_target["target_hash"] = canonical_hash(
                            {"chapter_index": future_chapter, "planned": after}
                        )
                        future_target["adjustment_note"] = reason
                        revised["adjustments"].append(
                            {
                                "revision": revised["revision"],
                                "chapter_index": future_chapter,
                                "before": before,
                                "after": copy.deepcopy(after),
                                "reason": reason,
                                "recorded_at": timestamp,
                            }
                        )
                    break
            revised["updated_at"] = timestamp
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


def _compact_text(value: str, *, limit: int = 180) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _compact_value(value: Any) -> str:
    if value is None or value == [] or value == {} or value == "":
        return ""
    if isinstance(value, str):
        return _compact_text(value)
    return _compact_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _arc_evidence_present(
    field: str, value: Any, *, evidence_values: Mapping[str, Any]
) -> bool:
    if field == "mainline":
        escalation = evidence_values.get("escalation")
        events = escalation.get("events") if isinstance(escalation, Mapping) else None
        return _meaningful_text(value) and (
            isinstance(events, Sequence)
            and not isinstance(events, (str, bytes))
            and any(
                isinstance(item, Mapping) and _meaningful_text(item.get("text"))
                for item in events
            )
        )
    if field == "relationship" and isinstance(value, Sequence) and not isinstance(
        value, (str, bytes)
    ):
        return any(
            isinstance(item, Mapping)
            and _contains_signal(item, _RELATIONSHIP_SIGNALS)
            for item in value
        )
    if field == "escalation" and isinstance(value, Mapping):
        conflicts = value.get("conflicts")
        return (
            isinstance(conflicts, Sequence)
            and not isinstance(conflicts, (str, bytes))
            and any(
                _meaningful_text(item)
                and _contains_signal(item, _ESCALATION_SIGNALS)
                for item in conflicts
            )
        )
    if field == "resource_cost" and isinstance(value, Sequence) and not isinstance(
        value, (str, bytes)
    ):
        return any(
            isinstance(item, Mapping)
            and _contains_signal(item, _RESOURCE_COST_SIGNALS)
            for item in value
        )
    if field == "foreshadowing" and isinstance(value, Sequence) and not isinstance(
        value, (str, bytes)
    ):
        return any(_meaningful_text(item) for item in value)
    return False


def _meaningful_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _contains_signal(value: Any, signals: Sequence[str]) -> bool:
    if isinstance(value, Mapping):
        text = " ".join(str(item) for item in value.values())
    else:
        text = str(value)
    normalized = text.casefold()
    for signal in signals:
        candidate = signal.casefold()
        if candidate.isascii():
            if re.search(
                rf"(?<![a-z0-9_]){re.escape(candidate)}(?![a-z0-9_])",
                normalized,
            ):
                return True
        elif candidate in normalized:
            return True
    return False


def _validate_differences(value: Sequence[str]) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise ArcPlanError("arc_fulfillment_differences_invalid", "differences must be a list")
    supplied = [str(item) for item in value]
    if len(supplied) != len(set(supplied)) or any(
        field not in _GOAL_FIELDS for field in supplied
    ):
        raise ArcPlanError(
            "arc_fulfillment_differences_invalid",
            "differences must contain unique RunArc goal fields",
        )
    return [field for field in _GOAL_FIELDS if field in supplied]


def _build_fulfillment_assessment(
    *,
    differences: Sequence[str],
    fulfillment_evidence_hash: str | None,
    planned_target_hash: str,
    completion_receipt_hash: str,
) -> dict[str, Any]:
    resolved_differences = _validate_differences(differences)
    assessment: dict[str, Any] = {
        "schema_version": "1.0",
        "fulfillment_evidence_hash": sha256_digest(
            "fulfillment_evidence_hash", fulfillment_evidence_hash
        ),
        "planned_target_hash": sha256_digest(
            "planned_target_hash", planned_target_hash
        ),
        "completion_receipt_hash": sha256_digest(
            "completion_receipt_hash", completion_receipt_hash
        ),
        "evidenced_fields": [
            field for field in _GOAL_FIELDS if field not in resolved_differences
        ],
    }
    assessment["assessment_hash"] = canonical_hash(assessment)
    return _validate_fulfillment_assessment(assessment)


def _validate_fulfillment_assessment(value: Any) -> dict[str, Any]:
    assessment = value
    # The complete RunArcPlan schema validates shape before this helper is
    # reached.  Programmatic callers still receive explicit deterministic
    # checks here rather than relying on incidental KeyError/TypeError paths.
    if not isinstance(assessment, Mapping) or set(assessment) != {
        "schema_version",
        "fulfillment_evidence_hash",
        "planned_target_hash",
        "completion_receipt_hash",
        "evidenced_fields",
        "assessment_hash",
    }:
        raise ArcPlanError(
            "arc_fulfillment_assessment_invalid",
            "fulfillment assessment fields are invalid",
        )
    if assessment["schema_version"] != "1.0":
        raise ArcPlanError(
            "arc_fulfillment_assessment_invalid",
            "fulfillment assessment version is unsupported",
        )
    evidence_hash = sha256_digest(
        "fulfillment_evidence_hash", assessment["fulfillment_evidence_hash"]
    )
    planned_target_hash = sha256_digest(
        "planned_target_hash", assessment["planned_target_hash"]
    )
    completion_receipt_hash = sha256_digest(
        "completion_receipt_hash", assessment["completion_receipt_hash"]
    )
    assessment_hash = sha256_digest(
        "assessment_hash", assessment["assessment_hash"]
    )
    evidenced = assessment["evidenced_fields"]
    if isinstance(evidenced, (str, bytes)) or not isinstance(evidenced, Sequence):
        raise ArcPlanError(
            "arc_fulfillment_assessment_invalid", "evidenced_fields must be a list"
        )
    supplied = [str(item) for item in evidenced]
    canonical_evidenced = [field for field in _GOAL_FIELDS if field in supplied]
    if (
        len(supplied) != len(set(supplied))
        or any(field not in _GOAL_FIELDS for field in supplied)
        or supplied != canonical_evidenced
    ):
        raise ArcPlanError(
            "arc_fulfillment_assessment_invalid",
            "evidenced_fields must be unique and canonically ordered",
        )
    validated = {
        "schema_version": "1.0",
        "fulfillment_evidence_hash": evidence_hash,
        "planned_target_hash": planned_target_hash,
        "completion_receipt_hash": completion_receipt_hash,
        "evidenced_fields": canonical_evidenced,
        "assessment_hash": assessment_hash,
    }
    if assessment_hash != canonical_hash(
        validated, exclude_fields=("assessment_hash",)
    ):
        raise ArcPlanError(
            "arc_fulfillment_assessment_invalid",
            "fulfillment assessment hash changed",
        )
    return validated


def _carry_forward_goal(
    *, field: str, source_chapter: int, fulfilled_value: str, planned_value: str
) -> str:
    return _compact_text(
        f"承接第{source_chapter}章{_GOAL_LABELS[field]}实际结果："
        f"{_compact_text(fulfilled_value, limit=96)}；原定目标："
        f"{_compact_text(planned_value, limit=112)}",
        limit=240,
    )


__all__ = [
    "ArcPlanError",
    "ArcPlanStore",
    "build_run_arc_plan",
    "derive_arc_fulfillment",
    "derive_arc_fulfillment_assessment",
    "validate_run_arc_plan",
]
