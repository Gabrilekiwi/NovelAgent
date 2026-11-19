"""Real upgraded identity-3 chapter transaction; isolated deterministic inputs."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from core.engine.chapter_commit_request import chapter_commit_request
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.persistence_v2 import reconcile_pending_persistence_v2
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.reviewed_chapter_commit import prepare_reviewed_chapter_transaction
from core.story_project.identity import ProjectIdentityError, load_project_identity
from core.story_project.paths import infer_next_chapter, resolve_prose
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import RELATIVE_PATH
from core.story_project.reviewed_chapter_transition import ReviewedChapterTransitionError
from tests.test_reviewed_candidate_context import registered_upgrade, author_confirmation
from tests.test_reviewed_upgrade import case
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_commits import ChapterCommitPreparationService
from workbench.chapter_facts import ChapterFactReviewService
from workbench.chapter_commit_index import ChapterCommitIndex


class PowerLoss(BaseException):
    pass


@pytest.fixture
def ready(case):
    root = case["root"]
    # Prepare next chapter's outline before generation: source membership is frozen.
    outline = next((root / "大纲").glob("*.md"))
    (root / "大纲/细纲_第003章.md").write_text(outline.read_text(encoding="utf-8").replace("第2章", "第3章").replace("第002章", "第003章"), encoding="utf-8")
    # Upgrade inputs were captured in the base fixture; refresh after this author edit.
    from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
    case["inputs"]["trusted_context"] = ChapterCandidateContextBuilder(root).build(2)
    db, projects, project, upgrade = registered_upgrade(case)
    # Freeze before deterministic generation. These fixtures never dispatch a
    # paid Provider; publication now shares the same chapter active-time clock.
    from core.chapter_budget_contract import freeze_chapter_budget
    from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
    ChapterBudgetFreezeRegistry(root,book_id=project.book_id).freeze(freeze_chapter_budget(
        book_id=project.book_id,chapter=2,max_provider_calls=10,max_input_tokens=1000000,
        max_output_tokens=100000,max_active_seconds='900'),before_write=lambda:None)
    builder = ReviewedChapterCandidateContextBuilder
    confirmation = author_confirmation(projects, project)
    example = fact_contract_example()
    blueprint = builder(root).build(2).story_project_context["chapter_blueprint"]
    good = Path("tests/fixtures/chapter_quality/good_chapter.md").read_text(encoding="utf-8").replace("林雪", "林澈").replace("陈岚", "周禾")
    text = example["chapter_text"] + "\n\n" + good + "\n\n" + "。".join(b["text"] for b in blueprint["required_beats"]) + "。\n\n" + blueprint["ending_pressure"] + "。"
    calls = []
    executor = ReviewedChapterCandidateExecutor(story_project_root=root, generator=lambda _: calls.append(1) or text,
        execution_kind="deterministic", provenance_factory=_fixed_provenance)
    record = executor.execute(chapter_index=2, confirmation=confirmation, idempotency_key="tx-candidate")
    drafts = CandidateDraftStore(db)
    draft = drafts.create_from_candidate(project_id=project.id, chapter=2, candidate_id=record["candidate_id"],
        source_sha256=record["prose"]["sha256"], source_artifact_ref=record["prose"]["artifact_ref"], text=text)
    reviews = ChapterFactReviewService(database=db, projects=projects, candidate_drafts=drafts, context_builder_factory=builder)
    review = reviews.create(draft.id, expected_draft_revision=draft.revision, expected_draft_sha256=draft.sha256,
        proposals=[f for f in example["proposal"]["facts"] if f["kind"] in {"event", "counter"}], author_confirmed=True)
    facts = review["facts"]
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    review = reviews.update(review["id"], facts=facts, expected_revision=review["revision"])
    review = reviews.confirm(review["id"], expected_revision=review["revision"], expected_facts_sha256=review["facts_sha256"],
        expected_draft_revision=draft.revision, expected_draft_sha256=draft.sha256, author_confirmed=True)
    service = ChapterCommitPreparationService(database=db, projects=projects, candidate_drafts=drafts,
        fact_reviews=reviews, context_builder_factory=builder)
    route = dict(project_id=project.id, chapter=2, review_id=review["id"], expected_draft_revision=draft.revision,
        expected_review_revision=review["revision"], expected_draft_sha256=draft.sha256,
        expected_facts_sha256=review["facts_sha256"], idempotency_key="tx-commit")
    preparation = service.prepare(**route)
    trusted = builder(root).build(2)
    request = chapter_commit_request(book_id=project.book_id, request=route)
    return locals()


def transaction(ready, **kwargs):
    return prepare_reviewed_chapter_transaction(trusted_context=ready["trusted"], chapter_preparation=ready["preparation"],
        request=ready["request"], run_id="chapter-2", **kwargs)


def test_actual_v3_chapter_commits_and_next_context_cold_reads_full_state(ready):
    root = ready["root"]
    identity_before = (root / ".novelagent/project.json").read_bytes()
    tx = transaction(ready)
    assert infer_next_chapter(root) == 2
    result = tx.commit()
    assert result["committed"], result
    assert infer_next_chapter(root) == 3
    assert resolve_prose(root, 2).path.read_text(encoding="utf-8") == ready["preparation"]["final_prose"]
    verified = load_verified_reviewed_identity(root)
    assert verified["history"]["value"]["current_state"] == ready["preparation"]["fact_preparation"]["state_after"]
    next_context = ReviewedChapterCandidateContextBuilder(root).build(3)
    assert next_context.story_project_context["reviewed_state"]["authoritative_state"] == verified["history"]["value"]["current_state"]
    actual = next_context.story_project_context["reviewed_state"]["previous_chapter_facts"]
    reviewed = verified["history"]["value"]["last_transition"]["facts"]
    assert actual == {"chapter": 2, "facts": [
        {"summary": f["summary"], "quotes": [s["quote"] for s in f["evidence_spans"]]}
        for f in reviewed if f["review"]["decision"] == "accepted"]}
    assert actual["facts"]
    code = "import json,sys; from pathlib import Path; from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder; print(json.dumps(ReviewedChapterCandidateContextBuilder(Path(sys.argv[1])).build(3).to_dict(),ensure_ascii=True))"
    child = subprocess.run([sys.executable, "-B", "-c", code, str(root)], capture_output=True, text=True, timeout=40, check=True)
    assert json.loads(child.stdout) == next_context.to_dict()
    store = ChapterCommitIndex(database=ready["db"], projects=ready["projects"])
    published = store.find_published(project_id=ready["project"].id, root_map=tx.root_map,
        baseline_document_sha256=ready["upgrade"]["binding"]["baseline_document_sha256"], request=ready["route"])
    assert published["publication_verified"] and published["chapter"] == 2 and ready["calls"] == [1]
    assert (root / ".novelagent/project.json").read_bytes() == identity_before
    with pytest.raises(ProjectIdentityError):
        load_project_identity(root)


@pytest.mark.parametrize("event,index,committed", [
    ("after_apply_target", 0, False), ("after_apply_target", 1, False),
    ("before_commit_marker", None, False), ("after_commit_marker", None, True),
    ("before_publication_receipt", None, True),
])
def test_actual_v3_power_loss_reconciles_original_transaction(ready, event, index, committed):
    root = ready["root"]
    before = (root / RELATIVE_PATH).read_bytes()
    def fault(actual_event, actual_index, path):
        if actual_event == event and actual_index == index:
            raise PowerLoss()
    tx = transaction(ready, fault_injector=fault)
    with pytest.raises(PowerLoss):
        tx.commit()
    results = reconcile_pending_persistence_v2(tx.transaction_root, expected_book_id=ready["project"].book_id)
    assert results["ok"] and results["transaction_count"] == 1, results
    assert results["transactions"][0]["committed"] is committed, results
    assert infer_next_chapter(root) == (3 if committed else 2)
    assert load_verified_reviewed_identity(root)["history"]["value"]["committed_through"] == (2 if committed else 1)
    assert ready["calls"] == [1]
    if not committed:
        assert (root / RELATIVE_PATH).read_bytes() == before


@pytest.mark.parametrize("damage", ["missing_quality", "wrong_quality_binding", "wrong_prose", "extra_apply",
    "missing_transition", "wrong_context", "wrong_checkpoint", "wrong_request"])
def test_typed_real_prepare_rejects_malformed_bundles_before_formal_write(ready, monkeypatch, damage):
    from core.engine.persistence_v2 import PersistenceV2Transaction, PersistenceV2Error
    from core.engine.reviewed_upgrade import json_bytes
    from core.story_project.reviewed_state_document import digest
    original = PersistenceV2Transaction.prepare
    def changed(self, **kwargs):
        artifacts = list(kwargs["artifacts"])
        if damage == "missing_quality":
            kwargs["artifacts"] = [t for t in artifacts if t.target_id != "reviewed_commit_quality"]
        elif damage in {"wrong_quality_binding", "wrong_request"}:
            target_id = "reviewed_commit_quality" if damage == "wrong_quality_binding" else "reviewed_commit_request"
            target = next(t for t in artifacts if t.target_id == target_id)
            value = json.loads(target.content_bytes())
            if damage == "wrong_quality_binding":
                value["binding"]["chapter_preparation_sha256"] = "0" * 64
                value["quality_sha256"] = digest({k: v for k, v in value.items() if k != "quality_sha256"})
            else:
                value["request"]["request"]["expected_review_revision"] += 1
                value["request"]["client_request_sha256"] = digest({k: v for k, v in value["request"].items() if k != "client_request_sha256"})
                value["evidence_sha256"] = digest({k: v for k, v in value.items() if k != "evidence_sha256"})
            kwargs["artifacts"] = [replace(t, content=json_bytes(value)) if t is target else t for t in artifacts]
        elif damage in {"wrong_prose", "extra_apply"}:
            apply = list(kwargs["apply_targets"])
            if damage == "wrong_prose":
                apply[1] = replace(apply[1], content=apply[1].content_bytes() + "未审核改动。".encode("utf-8"))
            else:
                apply.append(replace(apply[1], target_id="unexpected", path_ref={**apply[1].path_ref.to_dict(), "relative_path": "正文/第003章.md"}))
            kwargs["apply_targets"] = apply
        elif damage == "missing_transition":
            for declaration in self.read_set_declared_writes:
                declaration.pop("transition", None)
        elif damage == "wrong_context":
            kwargs["context_digest"] = "0" * 64
        elif damage == "wrong_checkpoint":
            kwargs["story_project_source_revision_after"] = {"wrong": True}
        return original(self, **kwargs)
    monkeypatch.setattr(PersistenceV2Transaction, "prepare", changed)
    before = (ready["root"] / RELATIVE_PATH).read_bytes()
    with pytest.raises((ValueError, PersistenceV2Error)):
        transaction(ready)
    assert (ready["root"] / RELATIVE_PATH).read_bytes() == before and infer_next_chapter(ready["root"]) == 2
    assert not (ready["root"] / ".novelagent/runtime/reviewed/p/journals/chapter-2").exists()


def test_strict_quality_cannot_stage_a_transaction_without_required_model_check(ready):
    with pytest.raises(ReviewedChapterTransitionError, match="complete passing"):
        transaction(ready, quality_policy="strict")
    assert infer_next_chapter(ready["root"]) == 2
    assert not (ready["root"] / ".novelagent/runtime/reviewed/p/journals/chapter-2").exists()


@pytest.mark.parametrize("when", ["before_commit", "before_marker"])
def test_source_changes_block_publication_and_roll_back_applied_chapter(ready, when):
    root = ready["root"]
    target = root / "追踪/上下文.md"
    before = (root / RELATIVE_PATH).read_bytes()
    def change(*args):
        target.write_bytes(target.read_bytes() + b"\nmanual change")
    tx = transaction(ready)
    if when == "before_commit":
        change()
    else:
        tx._fault_injector = lambda event, index, path: change() if event == "before_commit_marker" else None
    result = tx.commit()
    assert result["committed"] is False, result
    assert infer_next_chapter(root) == 2 and (root / RELATIVE_PATH).read_bytes() == before
    assert not tx.marker_path.exists()
    assert target.read_bytes().endswith(b"manual change") and ready["calls"] == [1]


def test_published_quality_archive_is_required_by_cold_identity_and_index(ready):
    tx = transaction(ready)
    assert tx.commit()["committed"]
    artifact = ready["root"] / ".novelagent/runtime/reviewed/quality/chapter-2.json"
    artifact.write_bytes(artifact.read_bytes() + b"\nchanged")
    with pytest.raises(ValueError):
        load_verified_reviewed_identity(ready["root"])
    store = ChapterCommitIndex(database=ready["db"], projects=ready["projects"])
    with pytest.raises(ValueError):
        store.rebuild(project_id=ready["project"].id, root_map=tx.root_map,
            baseline_document_sha256=ready["upgrade"]["binding"]["baseline_document_sha256"])


@pytest.mark.parametrize("damage", ["quality", "parent_backup"])
def test_staged_evidence_corruption_is_rejected_before_any_apply(ready, damage):
    from core.engine.persistence_v2 import load_persistence_manifest_v2
    tx = transaction(ready)
    manifest = load_persistence_manifest_v2(tx.manifest_path)
    target = next(t for t in manifest["immutable"]["targets"] if t["target_id"] ==
        ("reviewed_commit_quality" if damage == "quality" else "reviewed_state"))
    path = tx.journal_dir / target["staged_relative_path" if damage == "quality" else "backup_relative_path"]
    path.write_bytes(path.read_bytes() + b"\nchanged")
    before = (ready["root"] / RELATIVE_PATH).read_bytes()
    result = tx.commit()
    assert result["committed"] is False and not tx.marker_path.exists(), result
    assert infer_next_chapter(ready["root"]) == 2 and (ready["root"] / RELATIVE_PATH).read_bytes() == before
