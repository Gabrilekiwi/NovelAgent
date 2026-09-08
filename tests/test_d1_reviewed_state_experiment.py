"""Finite D1 source experiment. These checks do not certify production wiring."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.engine.chapter_fact_candidate import normalize_fact_proposal
from core.story_project.managed_block import build_managed_projection, write_managed_block
from scripts.experiments.d1_reviewed_state import (
    RELATIVE_PATH, SourceRuleError, apply_prepared, baseline_request, capture_sources,
    create_baseline, describe_source_conflict, digest, prepare_chapter,
    read_current, resolve_source_conflict, review_request,
)


REPO = Path(__file__).resolve().parents[1]
BOOK_ID = "synthetic-d1-source-book"


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "隔离试验 book"
    manual = root / "追踪/上下文.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("# 接入时人工记录\n林澈仍在仓库，目标为赶路，库存信标为 0。\n", encoding="utf-8")
    settings = root / "设定/规则.md"
    settings.parent.mkdir()
    settings.write_text("# 规则\n警戒等级范围为 0 至 100。\n", encoding="utf-8")
    state = json.loads((REPO / "tests/fixtures/chapter_fact_offline_v1/baseline.json").read_text(encoding="utf-8"))
    example = json.loads((REPO / "examples/chapter_fact_review/seven_kinds.json").read_text(encoding="utf-8"))
    facts = normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])
    for fact in facts:
        fact["review"]["decision"] = "accepted"  # Fixture action, not actual author evaluation.
    sources = capture_sources(root)
    confirmation = baseline_request(book_id=BOOK_ID, chapter=1, state=state, sources=sources)
    document = create_baseline(root=root, book_id=BOOK_ID, chapter=1, state=state,
                               confirmed_baseline=confirmation)
    inputs = dict(root=root, document=document, book_id=BOOK_ID, chapter=2,
                  chapter_text=example["chapter_text"], facts=facts,
                  draft_revision="draft-1", review_revision="review-1")
    inputs["review_binding"] = review_request(**{key: value for key, value in inputs.items()
                                               if key != "review_revision"})
    return dict(inputs=inputs, state=state, manual=manual, confirmation=confirmation)


def committed(case):
    inputs = case["inputs"]
    plan = prepare_chapter(**inputs)
    return apply_prepared(plan=plan, **inputs), plan


def test_seven_changes_preserve_manual_and_initial_values(case):
    manual_before = case["manual"].read_bytes()
    original_inputs = copy.deepcopy(case["inputs"])
    document, plan = committed(case)
    value = read_current(root=case["inputs"]["root"], document=document,
                         book_id=BOOK_ID, next_chapter=3)
    state = value["current_state"]
    assert state == plan["prepared"]["state_after"]
    assert state["characters"]["char_lin_che"]["current_goal"] == "守住闸门"
    assert state["relationships"]["rel_lin_zhou"]["trust"] == "互信"
    assert state["roster"]["roster_station_guards"]["declared_count"] == 2
    assert state["locations"]["char_lin_che"]["location_id"] == "location_old_station"
    assert state["inventory"]["beacon_ledger"]["quantity"] == 1
    assert state["numeric_counters"]["alert_level"]["current_value"] == 2
    assert len(state["events"]) == 1
    assert value["initial_baseline"]["state"] == case["state"]
    assert case["manual"].read_bytes() == manual_before
    assert case["inputs"] == original_inputs


def test_new_process_reads_all_seven_without_cache(case):
    document, plan = committed(case)
    root = case["inputs"]["root"]
    (root / RELATIVE_PATH).write_bytes(document)
    cache = root / "disposable-runtime-cache.json"
    cache.write_text('{"stale": true}', encoding="utf-8")
    cache.unlink()  # A single explicitly created fixture cache, never production data.
    program = (
        "import json, sys; from pathlib import Path; "
        "from scripts.experiments.d1_reviewed_state import read_current, RELATIVE_PATH; "
        "r=Path(sys.argv[1]); v=read_current(root=r, document=(r/RELATIVE_PATH).read_bytes(), "
        "book_id=sys.argv[2], next_chapter=3); print(json.dumps(v, ensure_ascii=True))"
    )
    result = subprocess.run([sys.executable, "-B", "-c", program, str(root), BOOK_ID],
                            cwd=REPO, capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(result.stdout)["current_state"] == plan["prepared"]["state_after"]
    assert not cache.exists()


def test_source_conflict_decision_persists_and_later_edit_invalidates_it(case):
    document, _ = committed(case)
    root = case["inputs"]["root"]
    case["manual"].write_text("# 人工后改\n林澈位置写为码头。\n", encoding="utf-8")
    with pytest.raises(SourceRuleError, match="manual_source_changed"):
        read_current(root=root, document=document, book_id=BOOK_ID, next_chapter=3)
    conflict = describe_source_conflict(root=root, document=document, book_id=BOOK_ID)
    assert [entry["path"] for entry in conflict["differences"]] == ["追踪/上下文.md"]
    decision = {"conflict": conflict, "action": "keep_reviewed_current", "review_source": "author",
                "reason": "合成操作：这次原文编辑不更改已确认的章节当前值。"}
    resolved = resolve_source_conflict(root=root, document=document, book_id=BOOK_ID, decision=decision)
    path = root / RELATIVE_PATH
    path.write_bytes(resolved)
    loaded = read_current(root=root, document=path.read_bytes(), book_id=BOOK_ID, next_chapter=3)
    assert loaded["source_decisions"] == [decision]
    assert loaded["current_state"]["locations"]["char_lin_che"]["location_id"] == "location_old_station"
    assert loaded["initial_baseline"]["state"] == case["state"]
    case["manual"].write_text("# 又一次后改\n林澈位置写为北门。\n", encoding="utf-8")
    with pytest.raises(SourceRuleError, match="manual_source_changed"):
        read_current(root=root, document=path.read_bytes(), book_id=BOOK_ID, next_chapter=3)
    with pytest.raises(SourceRuleError, match="source_decision_stale_or_invalid"):
        resolve_source_conflict(root=root, document=path.read_bytes(), book_id=BOOK_ID, decision=decision)


@pytest.mark.parametrize("mutation", ["draft_revision", "review_revision", "facts", "text"])
def test_edited_review_inputs_invalidate_preparation(case, mutation):
    inputs = case["inputs"]
    plan = prepare_chapter(**inputs)
    changed = copy.deepcopy(inputs)
    if mutation == "facts":
        changed["facts"][0]["review"]["note"] = "changed audit annotation"
    elif mutation == "text":
        changed["chapter_text"] += "\n远处传来一声汽笛。"
    else:
        changed[mutation] += "-new"
    with pytest.raises(ValueError):
        apply_prepared(plan=plan, **changed)


def test_edit_then_revert_text_still_requires_new_decisions(case):
    plan = prepare_chapter(**case["inputs"])
    with pytest.raises(SourceRuleError, match="fact_decisions_stale"):
        apply_prepared(plan=plan, **{**case["inputs"], "draft_revision": "draft-3-reverted"})


def test_reprepare_after_edit_cannot_reuse_old_fact_decisions(case):
    changed = {**case["inputs"], "draft_revision": "draft-2"}
    with pytest.raises(SourceRuleError, match="fact_decisions_stale"):
        prepare_chapter(**changed)


def test_source_edit_between_prepare_and_apply_blocks(case):
    plan = prepare_chapter(**case["inputs"])
    case["manual"].write_text("人工接入资料已修改", encoding="utf-8")
    with pytest.raises(SourceRuleError, match="manual_source_changed"):
        apply_prepared(plan=plan, **case["inputs"])


def test_new_or_removed_manual_file_invalidates_binding(case):
    inputs = case["inputs"]
    extra = inputs["root"] / "设定/新增.md"
    extra.write_text("新约束", encoding="utf-8")
    with pytest.raises(SourceRuleError, match="manual_source_changed"):
        prepare_chapter(**inputs)
    extra.unlink()
    (inputs["root"] / "设定/规则.md").unlink()
    with pytest.raises(SourceRuleError, match="manual_source_changed"):
        prepare_chapter(**inputs)


def test_wrong_book_and_wrong_chapter_block(case):
    with pytest.raises(SourceRuleError, match="identity_mismatch"):
        prepare_chapter(**{**case["inputs"], "book_id": "another-book"})
    with pytest.raises(SourceRuleError, match="next_chapter_mismatch"):
        prepare_chapter(**{**case["inputs"], "chapter": 3})


def test_baseline_confirmation_is_bound_to_sources_and_state(case):
    inputs = case["inputs"]
    case["manual"].write_text("新的人工设定", encoding="utf-8")
    with pytest.raises(SourceRuleError, match="baseline_confirmation_stale_or_missing"):
        create_baseline(root=inputs["root"], book_id=BOOK_ID, chapter=1, state=case["state"],
                        confirmed_baseline=case["confirmation"])


def test_changed_baseline_values_need_their_own_confirmation(case):
    state = copy.deepcopy(case["state"])
    state["inventory"]["beacon_ledger"]["quantity"] = 7
    with pytest.raises(SourceRuleError, match="baseline_confirmation_stale_or_missing"):
        create_baseline(root=case["inputs"]["root"], book_id=BOOK_ID, chapter=1, state=state,
                        confirmed_baseline=case["confirmation"])


def test_conflict_decision_cannot_be_reused_after_state_head_changes(case):
    root = case["inputs"]["root"]
    before = case["inputs"]["document"]
    after, _ = committed(case)
    case["manual"].write_text("人工资料后改", encoding="utf-8")
    old_conflict = describe_source_conflict(root=root, document=before, book_id=BOOK_ID)
    decision = {"conflict": old_conflict, "action": "keep_reviewed_current", "review_source": "author",
                "reason": "合成确认，绑定旧章节状态"}
    with pytest.raises(SourceRuleError, match="source_decision_stale_or_invalid"):
        resolve_source_conflict(root=root, document=after, book_id=BOOK_ID, decision=decision)


def test_existing_managed_value_update_does_not_look_like_manual_edit(case):
    root = case["inputs"]["root"]
    path = case["manual"]
    def projection(value):
        return build_managed_projection(scope="context", book_id=BOOK_ID, run_id="test", chapter=1,
            parser_version="shadow-1.0", base_revision="test", base_source_digest="a" * 64,
            owned_fields=["story_state.managed_only"], values={"story_state.managed_only": value})
    before = capture_sources(root)
    path.write_bytes(write_managed_block(path.read_bytes(), projection("before")))
    assert capture_sources(root) == before
    path.write_bytes(write_managed_block(path.read_bytes(), projection("after")))
    assert capture_sources(root) == before


def test_tampered_managed_payload_is_not_silently_read(case):
    document, _ = committed(case)
    assert b'"committed_through": 2' in document
    changed = document.replace(b'"committed_through": 2', b'"committed_through": 9')
    with pytest.raises(ValueError, match="hash"):
        read_current(root=case["inputs"]["root"], document=changed, book_id=BOOK_ID, next_chapter=3)
