from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from core.chapter_contexts import resolve_story_project_previous_chapter
from core.story_project.activation import activate_story_state, build_story_state_calibration_report
from core.story_project.amendments import read_current_chapter_text
from core.story_project.identity import load_project_identity
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from workbench.amendments import AmendmentService, deterministic_fact_check
from workbench.cohorts import CohortError, CohortService, SCORE_DIMENSIONS
from workbench.database import WorkbenchDatabase
from workbench.jobs import JobStore
from workbench.outlines import (
    OutlineBatchConflict,
    OutlineBatchService,
    OutlineItem,
    render_canonical_outline,
)
from workbench.planning import OutlinePlanner, OutlinePlanningError, parse_outline_proposal
from workbench.projects import ProjectRegistry


def product_case(name: str):
    root = Path.cwd() / ".tmp" / "test_workbench_product" / f"{name}_{uuid.uuid4().hex}"
    workspace = root / "books"
    workspace.mkdir(parents=True)
    database = WorkbenchDatabase(root / "workbench.db")
    projects = ProjectRegistry(database, allowed_workspaces=[workspace])
    project = projects.create_project(workspace / "book", name="测试长篇")
    outlines = OutlineBatchService(database, projects)
    return root, database, projects, project, outlines


def proposal_payload(start: int = 1, count: int = 10) -> str:
    return json.dumps(
        {
            "chapters": [
                {
                    "chapter_index": chapter,
                    "title": f"第{chapter}次抉择",
                    "core_event": f"队伍完成第{chapter}个阶段目标",
                    "required_beats": [f"确认线索{chapter}", f"付出代价{chapter}"],
                    "ending_pressure": f"第{chapter}个倒计时启动",
                }
                for chapter in range(start, start + count)
            ]
        },
        ensure_ascii=False,
    )


def test_outline_planner_uses_idempotent_provider_evidence_boundary() -> None:
    _, database, _, project, outlines = product_case("planner")
    calls: list[str] = []

    def provider(messages, call_id, evidence_root):
        assert messages[-1]["role"] == "user"
        assert evidence_root.is_relative_to(project.root)
        calls.append(call_id)
        return proposal_payload()

    planner = OutlinePlanner(database=database, outlines=outlines, provider=provider)
    operation, batch = planner.propose(
        project.id,
        direction="未来十章让队伍撤离旧城区并揭示内奸。",
        idempotency_key="week-one",
    )
    repeated_operation, repeated_batch = planner.propose(
        project.id,
        direction="未来十章让队伍撤离旧城区并揭示内奸。",
        idempotency_key="week-one",
    )

    assert operation.state == "completed"
    assert batch.start_chapter == 1
    assert batch.end_chapter == 10
    assert repeated_operation.id == operation.id
    assert repeated_batch.id == batch.id
    assert calls == [f"outline-plan-{operation.id}"]


def test_outline_planner_derives_idempotency_and_refuses_mid_call_source_drift() -> None:
    _, database, _, project, outlines = product_case("planner_default_key")
    calls: list[str] = []

    def provider(messages, call_id, evidence_root):
        calls.append(call_id)
        return proposal_payload()

    planner = OutlinePlanner(database=database, outlines=outlines, provider=provider)
    first_operation, first_batch = planner.propose(project.id, direction="推进撤离计划。")
    repeated_operation, repeated_batch = planner.propose(project.id, direction="推进撤离计划。")
    assert repeated_operation.id == first_operation.id
    assert repeated_batch.id == first_batch.id
    assert len(calls) == 1

    _, drift_database, _, drift_project, drift_outlines = product_case("planner_drift")

    def drifting_provider(messages, call_id, evidence_root):
        (drift_project.root / "设定" / "世界.md").write_text(
            "模型返回前作者改变了世界设定。", encoding="utf-8"
        )
        return proposal_payload()

    drifting = OutlinePlanner(
        database=drift_database,
        outlines=drift_outlines,
        provider=drifting_provider,
    )
    with pytest.raises(OutlinePlanningError, match="changed while the model was planning"):
        drifting.propose(drift_project.id, direction="推进撤离计划。")
    assert drift_outlines.list_batches(drift_project.id) == ()


def test_outline_batch_adopts_canonical_existing_files_without_overwriting_them() -> None:
    _, _, _, project, outlines = product_case("outline_adoption")
    proposal = json.loads(proposal_payload())["chapters"]
    for raw in proposal[:8]:
        item = OutlineItem(
            chapter_index=raw["chapter_index"],
            title=f"保留{raw['title']}",
            core_event=raw["core_event"],
            required_beats=tuple(raw["required_beats"]),
            ending_pressure=raw["ending_pressure"],
        )
        canonical_outline_path(project.root, item.chapter_index).write_text(
            render_canonical_outline(item),
            encoding="utf-8",
        )

    batch = outlines.create_batch(project.id, proposal)
    original = {
        chapter: canonical_outline_path(project.root, chapter).read_bytes()
        for chapter in range(1, 9)
    }

    approved = outlines.adopt_existing_and_approve_batch(
        batch.id,
        expected_revision=batch.revision,
        expected_sha256=batch.proposal_sha256,
    )

    assert approved.status == "approved"
    assert approved.revision == 2
    assert all(
        canonical_outline_path(project.root, chapter).read_bytes() == source
        for chapter, source in original.items()
    )
    assert canonical_outline_path(project.root, 9).is_file()
    assert canonical_outline_path(project.root, 10).is_file()
    manifest = json.loads(Path(approved.approval_manifest_path).read_text(encoding="utf-8"))
    assert manifest["adopted_existing_chapters"] == list(range(1, 9))


def test_outline_batch_refuses_to_adopt_noncanonical_author_outline() -> None:
    _, _, _, project, outlines = product_case("outline_adoption_noncanonical")
    proposal = json.loads(proposal_payload())["chapters"]
    canonical_outline_path(project.root, 1).write_text(
        "# 作者自由编写的细纲\n\n这一份文件绝不能被工作台覆盖。\n",
        encoding="utf-8",
    )
    batch = outlines.create_batch(project.id, proposal)

    with pytest.raises(OutlineBatchConflict, match="not in canonical workbench format"):
        outlines.adopt_existing_and_approve_batch(
            batch.id,
            expected_revision=batch.revision,
            expected_sha256=batch.proposal_sha256,
        )

    assert canonical_outline_path(project.root, 1).read_text(encoding="utf-8").startswith(
        "# 作者自由编写的细纲"
    )
    assert not canonical_outline_path(project.root, 9).exists()


def test_outline_parser_rejects_missing_or_non_contiguous_chapters() -> None:
    payload = json.loads(proposal_payload(count=2))
    payload["chapters"][1]["chapter_index"] = 3
    with pytest.raises(OutlinePlanningError, match="contiguous"):
        parse_outline_proposal(json.dumps(payload, ensure_ascii=False), start_chapter=1, chapter_count=2)
    with pytest.raises(OutlinePlanningError, match="exactly 10"):
        parse_outline_proposal(proposal_payload(count=2), start_chapter=1, chapter_count=10)


def test_safe_amendment_becomes_current_context_without_overwriting_formal_prose() -> None:
    _, database, projects, project, _ = product_case("amendment")
    original = (
        "第一节\n\n"
        + "林澈沿着走廊向前，确认门外没有脚步声。他压低呼吸，示意同伴停下。\n\n" * 20
        + "走廊尽头的灯忽然熄灭。\n"
    )
    formal_path = canonical_prose_path(project.root, 1, "停电前夜")
    formal_path.write_text(original, encoding="utf-8")
    formal_bytes = formal_path.read_bytes()
    base_sha = hashlib.sha256(formal_bytes).hexdigest()
    revised = original.replace("他压低呼吸，示意同伴停下。", "他放轻呼吸，抬手示意同伴停步。")
    calls: list[str] = []

    def provider(messages, call_id, evidence_root):
        assert "原版本" in messages[-1]["content"]
        calls.append(call_id)
        return json.dumps(
            {
                "equivalent": True,
                "confidence": 0.98,
                "critical_changes": [],
                "uncertain": False,
                "summary": "仅措辞润色。",
            },
            ensure_ascii=False,
        )

    service = AmendmentService(database=database, projects=projects, provider=provider)
    amendment = service.submit(
        project.id,
        1,
        text=revised,
        expected_base_sha256=base_sha,
        idempotency_key="copyedit-1",
    )
    repeated = service.submit(
        project.id,
        1,
        text=revised,
        expected_base_sha256=base_sha,
        idempotency_key="copyedit-1",
    )

    assert amendment.status == "active"
    assert repeated.id == amendment.id
    assert calls == [f"amendment-check-{amendment.id}"]
    assert formal_path.read_bytes() == formal_bytes
    current, current_path, current_sha, source_kind = read_current_chapter_text(project.root, 1)
    assert current == revised.strip() + "\n"
    assert current_path != formal_path
    assert current_sha == amendment.content_sha256
    assert source_kind == "amendment_current"
    previous = resolve_story_project_previous_chapter(project.root, 2)
    assert previous is not None
    assert previous.source_kind == "amendment_current"
    assert "抬手示意" in previous.generation_excerpt["text"]


def test_fact_change_rejects_amendment_without_model_call() -> None:
    _, database, projects, project, _ = product_case("amendment_reject")
    original = ("仓库里有3支步枪，足够小队支撑2天。\n\n" * 30).strip() + "\n"
    formal_path = canonical_prose_path(project.root, 1, "仓库")
    formal_path.write_text(original, encoding="utf-8")
    base_sha = hashlib.sha256(formal_path.read_bytes()).hexdigest()
    revised = original.replace("3支步枪", "4支步枪")

    def forbidden_provider(*args):
        raise AssertionError("deterministic rejection must not call a model")

    service = AmendmentService(database=database, projects=projects, provider=forbidden_provider)
    amendment = service.submit(
        project.id,
        1,
        text=revised,
        expected_base_sha256=base_sha,
        idempotency_key="unsafe-copyedit",
    )

    assert amendment.status == "rejected"
    assert amendment.semantic_check["provider_calls"] == 0
    current, path, _, source_kind = read_current_chapter_text(project.root, 1)
    assert current.replace("\r\n", "\n") == original
    assert path == formal_path
    assert source_kind == "story_project_prose"
    assert deterministic_fact_check(original, revised)["ok"] is False


def test_production_cohort_requires_strict_and_freezes_outline_and_code_baseline() -> None:
    root, database, projects, project, outlines = product_case("cohort")
    batch = outlines.create_batch(project.id, json.loads(proposal_payload())["chapters"])
    outlines.approve_batch(
        batch.id,
        expected_revision=batch.revision,
        expected_sha256=batch.proposal_sha256,
    )
    cohorts = CohortService(
        database=database,
        projects=projects,
        jobs=JobStore(database),
        repo_root=Path.cwd(),
    )
    config = {
        "quality_policy": "strict",
        "llm_validator": True,
        "review_gate": "needs_revision",
        "review_auto_repair": True,
        "review_repair_max_attempts": 1,
        "allow_shadow": False,
    }
    with pytest.raises(CohortError, match="strict"):
        cohorts.create(
            project.id,
            start_chapter=1,
            end_chapter=10,
            weekly_budget=100,
            generation_config=config,
        )

    identity = load_project_identity(project.root)
    report = build_story_state_calibration_report(
        book_id=identity.book_id,
        parser_version=SEMANTIC_PARSER_VERSION,
        semantic_schema_version=STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
        target_layout_profile_version="canonical-zh-1",
        evidence={
            "target_sample_count": 1,
            "format_variant_count": 2,
            "managed_round_trip_rate": 1.0,
            "required_field_exact_match_rate": 1.0,
            "authoritative_precision": 1.0,
            "supported_optional_recall": 0.95,
            "unsupported_structure_count": 0,
            "unsupported_structure_captured_count": 0,
            "consecutive_shadow_chapters": 10,
            "blocking_conflict_count": 0,
            "missing_provenance_fields": [],
        },
    )
    activate_story_state(project.root, report)
    context_path = project.root / "追踪" / "上下文.md"
    context_path.write_text(
        "<!-- NovelAgent:story_project_writeback -->\n",
        encoding="utf-8",
    )
    with pytest.raises(CohortError, match="no longer satisfies"):
        cohorts.create(
            project.id,
            start_chapter=1,
            end_chapter=10,
            weekly_budget=100,
            generation_config=config,
        )
    context_path.unlink()
    cohort = cohorts.create(
        project.id,
        start_chapter=1,
        end_chapter=10,
        weekly_budget=100,
        generation_config=config,
    )
    assert cohort.status == "active"
    assert len(cohort.baseline["outlines"]) == 10

    scores = {dimension: 4 for dimension in SCORE_DIMENSIONS}
    for chapter in range(1, 11):
        evaluation = cohorts.record_evaluation(
            cohort.id,
            chapter,
            scores=scores,
            edit_minutes=10,
            actual_cost=1,
            severe_defects=[],
            expected_revision=0,
        )
        assert evaluation.revision == 1
    result = cohorts.evaluate(cohort.id)
    assert result["criteria"]["all_ten_human_evaluations_present"] is True
    assert result["criteria"]["human_score_mean_at_least_3_8"] is True
    assert result["criteria"]["ten_of_ten_committed_with_receipt"] is False
    assert result["passed"] is False

    receipt_path = Path(cohort.baseline["outline_batch"]["outline_manifest_path"]).with_name(
        "approval_receipt.json"
    )
    receipt_bytes = receipt_path.read_bytes()
    receipt_path.write_text("{}", encoding="utf-8")
    receipt_drift = cohorts.evaluate(cohort.id)
    assert receipt_drift["invalidated"] is True
    assert "approved_outline_binding_invalid" in receipt_drift["invalidation_reasons"]
    assert receipt_drift["metrics"]["outline_binding_errors"]
    receipt_path.write_bytes(receipt_bytes)

    first_outline = Path(project.root) / cohort.baseline["outlines"][0]["path"]
    first_outline.write_text(first_outline.read_text(encoding="utf-8") + "\n外部改动\n", encoding="utf-8")
    drifted = cohorts.evaluate(cohort.id)
    assert drifted["invalidated"] is True
    assert "outline_changed" in drifted["invalidation_reasons"]
