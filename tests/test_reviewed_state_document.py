"""Synthetic codec and actual v2.1 receipt integration; no format activation."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from core.engine.chapter_commit_prepare import PREPARATION_VERSION, normalize_commit_prose
from core.engine.chapter_fact_candidate import normalize_fact_proposal
from core.engine.chapter_fact_prepare import prepare_confirmed_fact_changes
from core.engine.persistence_v2 import (
    PersistenceV2Target, PersistenceV2Transaction, bind_final_run_record_receipt,
    reconcile_pending_persistence_v2, verify_publication_receipt,
)
from core.path_refs import path_ref_for
from core.story_project.identity import ensure_project_identity, load_project_identity
from core.story_project.read_set import capture_story_project_read_set, declared_read_set_writes
from core.story_project.managed_block import managed_payload_sha256, parse_managed_block, write_managed_block
from core.story_project.reviewed_state_document import (
    FORMAT, RELATIVE_PATH, ReviewedStateDocumentError, baseline_request, capture_sources,
    create_baseline, decode_document, describe_source_conflict, digest, prepare_chapter_document,
    prepare_source_resolution, verify_current_sources, verify_transition,
)
from core.story_project.reviewed_state_store import load_verified_current, source_revision_after, verify_reviewed_history


BOOK_ID = "reviewed-codec-synthetic"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def case():
    # Short path avoids the independently tracked Windows path-budget issue.
    root = REPO / ".tmp/rs3" / uuid.uuid4().hex[:12] / "b"
    for name in ("追踪", "设定", "正文", "大纲", ".novelagent/runtime"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_project_identity(root, book_id=BOOK_ID)
    old_identity_sha = digest((root / ".novelagent/project.json").read_bytes())
    (root / "正文/第001章_基线.md").write_text("第一章合成基线。", encoding="utf-8")
    for chapter in (2, 3):
        (root / f"大纲/细纲_第{chapter:03d}章.md").write_text("合成章节大纲。", encoding="utf-8")
    manual = root / "追踪/上下文.md"
    manual.write_text("# 人工记录\n林澈在仓库。\n", encoding="utf-8")
    settings = root / "设定/规则.md"
    settings.write_text("# 世界规则\n警戒等级为 0 至 100。\n", encoding="utf-8")
    state = json.loads((REPO / "tests/fixtures/chapter_fact_offline_v1/baseline.json").read_text(encoding="utf-8"))
    example = json.loads((REPO / "examples/chapter_fact_review/seven_kinds.json").read_text(encoding="utf-8"))
    prose = normalize_commit_prose(example["chapter_text"])
    facts = normalize_fact_proposal(example["proposal"], chapter_text=prose)
    for fact in facts:
        fact["review"]["decision"] = "accepted"  # Synthetic unit input only.
    references = [{"relative_path": "设定/规则.md", "sha256": digest(settings.read_bytes()), "text": "警戒等级为 0 至 100。"}]
    confirmation = baseline_request(book_id=BOOK_ID, chapter=1, state=state, sources=capture_sources(root),
                                    old_identity_sha256=old_identity_sha, static_references=references)
    document = create_baseline(root=root, book_id=BOOK_ID, chapter=1, state=state,
        confirmed_baseline=confirmation, old_identity_sha256=old_identity_sha, run_id="baseline", static_references=references)
    runtime = root / ".novelagent/runtime"
    roots = {"story_project": root, "runtime": runtime}
    return locals()


def preparation(case, document=None):
    # This fixture exercises the codec, not author identity. The separately
    # tested server adapter supplies real confirmed records in production.
    document = document or case["document"]
    value = decode_document(document, book_id=BOOK_ID)
    binding = {"book_id": BOOK_ID, "chapter": value["committed_through"] + 1,
        "committed_through": value["committed_through"], "reviewed_source_sha256": digest(document),
        "manual_sources_sha256": value["source_revision"]["sha256"], "acceptance_kind": "author",
        "state_before_sha256": digest(value["current_state"]), "final_prose_sha256": digest(case["prose"].encode("utf-8")),
        "facts_sha256": digest(case["facts"]), "draft_id": "draft-1", "draft_revision": 1,
        "review_id": "review-1", "review_revision": 2, "confirmation_sha256": "2" * 64}
    p = {"preparation_version": PREPARATION_VERSION, "stage": "facts_prepared", "publication_authorized": False,
         "binding": binding, "facts": copy.deepcopy(case["facts"]), "final_prose": case["prose"],
         "fact_preparation": prepare_confirmed_fact_changes(facts=case["facts"], chapter_text=case["prose"],
                                                            base_state=value["current_state"]),
         "idempotency_scope_sha256": "3" * 64,
         "request_sha256": digest({"version": PREPARATION_VERSION, "binding": binding})}
    p["preparation_sha256"] = digest(p)
    return p


def chapter_document(case):
    return prepare_chapter_document(document=case["document"], book_id=BOOK_ID,
                                    preparation=preparation(case), run_id="chapter-2")


def mutate(document, operation):
    projection = copy.deepcopy(parse_managed_block(document).projection)
    operation(projection["values"]["reviewed_state"])
    projection["payload_sha256"] = managed_payload_sha256(projection)
    return write_managed_block(document, projection)


def publish(case, document, *, fault=None, final_override=None):
    """Real v2.1 apply/archive/receipt. Not the unfinished product commit API."""
    book_id = case.get("book_id", BOOK_ID)
    value = decode_document(document, book_id=book_id)
    head = value["head"]
    run_id = head["run_id"]
    root, runtime = case["root"], case["runtime"]
    def ref(path, root_id):
        return path_ref_for(path, root_id=root_id, root=case["roots"][root_id])
    target = root / RELATIVE_PATH
    apply = [PersistenceV2Target(target_id="reviewed_state", kind="story_project_update",
        path_ref=ref(target, "story_project"), content=document, expected_before_exists=target.exists(),
        expected_before_sha256=digest(target.read_bytes()) if target.exists() else None)]
    artifacts = [PersistenceV2Target(target_id="reviewed_state_archive", kind="review_artifact", phase="publication",
        path_ref=ref(runtime / f"reviewed/states/{run_id}.md", "runtime"), content=document)]
    if head["kind"] == "chapter":
        prose = case["prose"].encode("utf-8")
        apply.append(PersistenceV2Target(target_id="reviewed_chapter", kind="story_project_update",
            path_ref=ref(root / f"正文/第{value['committed_through']}章.md", "story_project"),
            content=prose, expected_before_exists=False))
        artifacts.append(PersistenceV2Target(target_id="reviewed_chapter_archive", kind="chapter_artifact", phase="publication",
            path_ref=ref(runtime / f"reviewed/chapters/{run_id}.md", "runtime"), content=prose))
    receipt_ref = ref(runtime / head["receipt_relative_path"], "runtime")
    final = {"record_type": "reviewed_legacy_commit", "id": run_id, "book_id": book_id, "kind": head["kind"],
             "request_sha256": head["request_sha256"], "revision": value["revision"], "committed": True,
             **(final_override or {})}
    final = bind_final_run_record_receipt(final, receipt_id=head["receipt_id"], receipt_path_ref=receipt_ref)
    chapter = value["committed_through"] if head["kind"] == "chapter" else value["committed_through"] + 1
    read_set = capture_story_project_read_set(root, chapter, project_identity=load_project_identity(root))
    declared = declared_read_set_writes(read_set, [(root / t.path_ref.relative_path, digest(t.content_bytes()),
                                                   len(t.content_bytes())) for t in apply])
    tx = PersistenceV2Transaction(transaction_root=runtime / "reviewed/p", run_id=run_id, book_id=book_id,
        root_map=case["roots"], fault_injector=fault, story_project_read_set=read_set, read_set_declared_writes=declared)
    tx.prepare(apply_targets=apply, artifacts=artifacts, final_run_record=final,
        final_run_path_ref=ref(runtime / f"reviewed/runs/{run_id}.json", "runtime"),
        receipt_id=head["receipt_id"], receipt_path_ref=receipt_ref, context_digest="4" * 64,
        generation_input_context_digest="5" * 64, story_project_source_revision_after=source_revision_after(document, book_id=book_id),
        candidate_result={"fixture": True})
    return tx.commit()


def load(case, chapter=3):
    return load_verified_current(root=case["root"], book_id=BOOK_ID, next_chapter=chapter, root_map=case["roots"],
                                 baseline_document_sha256=digest(case["document"]))


def test_formal_codec_preserves_seven_changes_baseline_and_manual(case):
    before = case["manual"].read_bytes()
    document = chapter_document(case)
    value = decode_document(document, book_id=BOOK_ID)
    verify_transition(document=document, previous_document=case["document"], book_id=BOOK_ID,
                      chapter_prose=case["prose"].encode("utf-8"))
    assert value["format"] == FORMAT and value["revision"] == 2
    assert value["current_state"] == preparation(case)["fact_preparation"]["state_after"]
    assert all(value["current_state"][key] != case["state"][key] for key in
        ("characters", "relationships", "roster", "locations", "inventory", "numeric_counters", "events"))
    assert value["initial_baseline"]["state"] == case["state"]
    assert case["manual"].read_bytes() == before
    assert not (case["root"] / RELATIVE_PATH).exists()


@pytest.mark.parametrize("field", ["characters", "relationships", "roster", "locations", "inventory", "numeric_counters", "events"])
def test_missing_state_collection_is_rejected_even_with_recomputed_managed_hash(case, field):
    document = mutate(case["document"], lambda v: v["current_state"].pop(field))
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_state_shape_invalid"):
        decode_document(document, book_id=BOOK_ID)


def test_receipt_required_even_for_valid_baseline(case):
    (case["root"] / RELATIVE_PATH).write_bytes(case["document"])
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_publication_missing"):
        load(case, 2)


def test_real_v21_receipts_read_all_seven_in_new_process(case):
    assert publish(case, case["document"])["committed"]
    assert publish(case, chapter_document(case))["committed"]
    value = load(case)
    assert value["receipt_count"] == 2
    expected = preparation(case)["fact_preparation"]["state_after"]
    assert value["value"]["current_state"] == expected
    code = "from pathlib import Path; import json,sys; from core.story_project.reviewed_state_store import load_verified_current; r=Path(sys.argv[1]); print(json.dumps(load_verified_current(root=r,book_id=sys.argv[2],next_chapter=3,root_map={'story_project':r,'runtime':r/'.novelagent/runtime'},baseline_document_sha256=sys.argv[3]),ensure_ascii=True))"
    child = subprocess.run([sys.executable, "-B", "-c", code, str(case["root"]), BOOK_ID, digest(case["document"])],
                            cwd=REPO, capture_output=True, text=True, check=True, timeout=25)
    assert json.loads(child.stdout)["value"]["current_state"] == expected


def test_old_valid_file_rollback_is_rejected_although_its_receipt_is_valid(case):
    publish(case, case["document"])
    publish(case, chapter_document(case))
    (case["root"] / RELATIVE_PATH).write_bytes(case["document"])
    receipt = case["runtime"] / "reviewed/receipts/baseline.json"
    assert verify_publication_receipt(receipt, root_map=case["roots"])["valid"]
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_current_state_is_not_tip"):
        load(case)


@pytest.mark.parametrize("path", ["reviewed/states/baseline.md", "reviewed/chapters/chapter-2.md", "reviewed/runs/chapter-2.json"])
def test_history_or_publication_corruption_is_rejected(case, path):
    publish(case, case["document"])
    publish(case, chapter_document(case))
    (case["runtime"] / path).write_bytes(b"corrupted fixture")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_publication_invalid"):
        load(case)


def test_actual_formal_prose_edit_is_detected_in_addition_to_archive(case):
    publish(case, case["document"])
    publish(case, chapter_document(case))
    (case["root"] / "正文/第2章.md").write_bytes(b"edited fixture prose")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_receipt_current_target_mismatch"):
        load(case)


def test_duplicate_chapter_file_is_not_a_valid_current_commit(case):
    publish(case, case["document"])
    publish(case, chapter_document(case))
    (case["root"] / "正文/第002章_重复.md").write_text(case["prose"], encoding="utf-8")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_chapter_path_invalid"):
        load(case)


def test_prose_added_without_reviewed_commit_does_not_advance_cursor(case):
    publish(case, case["document"])
    (case["root"] / "正文/第002章_外部.md").write_text("外部章节。", encoding="utf-8")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_prose_cursor_mismatch"):
        load(case, 2)


def test_valid_transaction_cannot_publish_an_unexplained_current_value(case):
    publish(case, case["document"])
    document = mutate(chapter_document(case), lambda v: v["current_state"]["numeric_counters"]["alert_level"].update(current_value=90))
    # Match the direct state hashes too: replay must still reject this value.
    document = mutate(document, lambda v: v["last_transition"].update(after_sha256=digest(v["current_state"])))
    publish(case, document)
    assert verify_publication_receipt(case["runtime"] / "reviewed/receipts/chapter-2.json", root_map=case["roots"])["valid"]
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_chapter_replay_mismatch"):
        load(case)


def test_baseline_anchor_and_typed_final_record_are_required(case):
    publish(case, case["document"], final_override={"record_type": "another_workflow"})
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_final_record_mismatch"):
        load(case, 2)


def test_other_baseline_anchor_is_rejected(case):
    publish(case, case["document"])
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_baseline_anchor_mismatch"):
        verify_reviewed_history(root=case["root"], book_id=BOOK_ID, root_map=case["roots"], baseline_document_sha256="0" * 64)


def test_lost_receipt_cannot_hide_a_later_archived_commit(case):
    publish(case, case["document"])
    publish(case, chapter_document(case))
    # Move one exact synthetic receipt aside to simulate accidental loss.
    receipt = case["runtime"] / "reviewed/receipts/chapter-2.json"
    receipt.rename(case["runtime"] / "lost-receipt.json")
    (case["root"] / RELATIVE_PATH).write_bytes(case["document"])
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_archive_receipt_coverage_mismatch"):
        load(case, 2)


def test_two_valid_source_transactions_cannot_fork_the_same_revision(case):
    publish(case, case["document"])
    case["manual"].write_text("# 人工修改\n林澈在码头。\n", encoding="utf-8")
    decision = {"conflict": describe_source_conflict(root=case["root"], document=case["document"], book_id=BOOK_ID),
                "action": "keep_reviewed_current", "review_source": "author", "reason": "合成决定"}
    for run_id in ("source-a", "source-b"):
        document = prepare_source_resolution(root=case["root"], document=case["document"], book_id=BOOK_ID,
                                             decision=decision, run_id=run_id)
        publish(case, document)
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_receipt_state_binding_invalid"):
        load(case, 2)


def test_modified_preparation_even_with_recomputed_checksum_cannot_change_result(case):
    p = preparation(case)
    p["fact_preparation"]["state_after"]["numeric_counters"]["alert_level"]["current_value"] = 90
    p["preparation_sha256"] = digest({k: v for k, v in p.items() if k != "preparation_sha256"})
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_preparation_result_mismatch"):
        prepare_chapter_document(document=case["document"], book_id=BOOK_ID, preparation=p, run_id="chapter-2")


def test_manual_conflict_requires_a_published_exact_decision(case):
    publish(case, case["document"])
    document = chapter_document(case)
    publish(case, document)
    case["manual"].write_text("# 人工后改\n林澈回到仓库。\n", encoding="utf-8")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_manual_source_changed"):
        load(case)
    decision = {"conflict": describe_source_conflict(root=case["root"], document=document, book_id=BOOK_ID),
                "action": "keep_reviewed_current", "review_source": "author", "reason": "合成审核：保留章节当前值。"}
    resolved = prepare_source_resolution(root=case["root"], document=document, book_id=BOOK_ID, decision=decision, run_id="source-3")
    publish(case, resolved)
    value = load(case)
    assert value["receipt_count"] == 3 and value["value"]["source_decisions"] == [decision]
    assert value["value"]["current_state"] == decode_document(document, book_id=BOOK_ID)["current_state"]
    (case["root"] / RELATIVE_PATH).write_bytes(document)
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_current_state_is_not_tip"):
        load(case)


def test_static_reference_edit_cannot_be_silently_accepted(case):
    case["settings"].write_text("# 已改变的规则\n", encoding="utf-8")
    decision = {"conflict": describe_source_conflict(root=case["root"], document=case["document"], book_id=BOOK_ID),
                "action": "keep_reviewed_current", "review_source": "author", "reason": "合成决定"}
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_static_reference_changed"):
        prepare_source_resolution(root=case["root"], document=case["document"], book_id=BOOK_ID, decision=decision, run_id="source-2")


def test_source_membership_and_hardlink_are_not_ignored(case):
    value = decode_document(case["document"], book_id=BOOK_ID)
    (case["root"] / "追踪/新增.md").write_text("新来源", encoding="utf-8")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_manual_source_changed"):
        verify_current_sources(root=case["root"], value=value, next_chapter=2)
    os.link(case["manual"], case["root"] / "追踪/硬链接.md")
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_source_hardlink_forbidden"):
        capture_sources(case["root"])


def test_uppercase_markdown_extension_remains_a_manual_source(case):
    path = case["root"] / "追踪/英文扩展名.MD"
    path.write_text("人工补充", encoding="utf-8")
    assert "追踪/英文扩展名.MD" in capture_sources(case["root"])["entries"]
    value = decode_document(case["document"], book_id=BOOK_ID)
    with pytest.raises(ReviewedStateDocumentError, match="reviewed_manual_source_changed"):
        verify_current_sources(root=case["root"], value=value, next_chapter=2)


@pytest.mark.parametrize("event", ["after_apply_target", "after_commit_marker"])
def test_marker_boundary_recovery_uses_original_bytes(case, event):
    publish(case, case["document"])
    document = chapter_document(case)
    class PowerLoss(BaseException):
        pass
    reached = []
    def fault(name, index, path):
        if name == event:
            reached.append(name)
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        publish(case, document, fault=fault)
    assert reached
    with pytest.raises(ReviewedStateDocumentError):
        load(case)
    result = reconcile_pending_persistence_v2(case["runtime"] / "reviewed/p", expected_book_id=BOOK_ID)
    assert result["ok"]
    if event == "after_apply_target":
        assert load(case, 2)["value"]["revision"] == 1
        assert not (case["root"] / "正文/第2章.md").exists()
    else:
        assert load(case)["value"]["revision"] == 2
        assert (case["root"] / RELATIVE_PATH).read_bytes() == document
