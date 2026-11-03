"""Actual identity-3 upgrade -> explicit candidate/review/quality, no paid calls."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from core.engine.chapter_candidate import ChapterCandidateExecutor, ChapterCandidateConflictError
from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.chapter_fact_context import build_chapter_fact_context
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_candidate_context import (
    ReviewedChapterCandidateContextBuilder,
    verify_reviewed_candidate_read_set,
)
from core.engine.reviewed_upgrade import prepare_reviewed_upgrade_transaction
from core.story_project.identity import ProjectIdentityError, load_project_identity
from core.story_project.read_set import StoryProjectSourceDriftError, verify_story_project_read_set
from core.story_project.reviewed_state_context import reviewed_state_snapshot
from core.story_project.reviewed_state_document import RELATIVE_PATH
from scripts.experiments.run_d2_b0_compatibility import inventory
from tests.test_reviewed_upgrade import case, prepared
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_cards import ChapterCardService
from workbench.chapter_commits import ChapterCommitPreparationService
from workbench.chapter_facts import ChapterFactReviewService
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry
from workbench.reviewed_projects import ReviewedProjectRegistry


def upgrade(case):
    p = prepared(case)
    assert prepare_reviewed_upgrade_transaction(preparation=p).commit()["committed"]
    return p


def registered_upgrade(case):
    root = case["root"]
    db = WorkbenchDatabase(root.parent / "workbench.db")
    ordinary = ProjectRegistry(db, allowed_workspaces=[root.parent])
    project = ordinary.import_project(root)
    p = upgrade(case)
    with pytest.raises(ProjectIdentityError):
        ordinary.get(project.id)
    reviewed = ReviewedProjectRegistry(db, allowed_workspaces=[root.parent])
    return db, reviewed, project, p


def author_confirmation(projects, project):
    cards = ChapterCardService(projects, context_builder_factory=ReviewedChapterCandidateContextBuilder)
    card = cards.get(project.id, 2).to_dict()
    return cards.confirm(project.id, 2, {"confirmed": True,
        "expected_card_sha256": card["confirmation"]["card_sha256"], "goal": "核验已审核状态",
        "validation_focus": ["continuity", "spatial", "logic"], "notes_text": ""})


def test_verified_v3_builds_real_state_without_caches_and_without_writes(case, monkeypatch):
    p = upgrade(case)
    root = case["root"]
    caches = root / ".novelagent/runtime/memory/v2"
    caches.mkdir(parents=True, exist_ok=True)
    (caches / "canonical_memory.json").write_text("invalid cache must not be read", encoding="utf-8")
    before = inventory(root)
    def deny(*args, **kwargs):
        pytest.fail("reader tried a network call")
    monkeypatch.setattr(socket.socket, "connect", deny)
    trusted = ReviewedChapterCandidateContextBuilder(root).build(2)
    context = trusted.story_project_context
    assert context["project_identity"] == p["new_identity"]
    assert context["project_identity"]["schema_version"] == "3.0"
    assert context["reviewed_state"]["authoritative_state"] == case["state"]
    assert context["memory_v2"]["projection"] is None
    assert reviewed_state_snapshot(context)["authoritative_state"] == case["state"]
    assert inventory(root) == before
    # Normal paths remain rejected; only the explicit reviewed reader is enabled.
    with pytest.raises(ProjectIdentityError):
        load_project_identity(root)
    with pytest.raises(ProjectIdentityError):
        ChapterCandidateContextBuilder(root).build(2)
    with pytest.raises(StoryProjectSourceDriftError):
        verify_story_project_read_set(context["read_set"], phase="prepare")
    assert verify_reviewed_candidate_read_set(context["read_set"], phase="prepare")["ok"]


def test_verified_v3_context_is_stable_in_a_fresh_process(case):
    upgrade(case)
    expected = ReviewedChapterCandidateContextBuilder(case["root"]).build(2)
    code = "from pathlib import Path; import json,sys; from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder; print(json.dumps(ReviewedChapterCandidateContextBuilder(Path(sys.argv[1])).build(2).to_dict(),ensure_ascii=True))"
    result = subprocess.run([sys.executable, "-B", "-c", code, str(case["root"])], cwd=Path.cwd(),
        capture_output=True, text=True, timeout=40, check=True)
    assert json.loads(result.stdout) == expected.to_dict()


@pytest.mark.parametrize("damage", ["manual_source", "state", "receipt", "identity", "prose"])
def test_upgraded_reader_rejects_actual_source_or_upgrade_corruption(case, damage):
    upgrade(case)
    paths = {"manual_source": "追踪/上下文.md", "state": RELATIVE_PATH,
        "receipt": ".novelagent/runtime/reviewed/receipts/upgrade-1.json", "identity": ".novelagent/project.json",
        "prose": "正文/第001章_门外.md"}
    if damage == "prose":
        from core.story_project.paths import resolve_prose
        path = resolve_prose(case["root"], 1).path
    else:
        path = case["root"] / paths[damage]
    path.write_bytes(path.read_bytes() + b"\nchanged")
    with pytest.raises((ValueError, RuntimeError)):
        ReviewedChapterCandidateContextBuilder(case["root"]).build(2)


@pytest.mark.parametrize("phase", ["during_apply", "pre_marker", "unknown"])
def test_explicit_reader_cannot_authorize_a_write_transition(case, phase):
    upgrade(case)
    trusted = ReviewedChapterCandidateContextBuilder(case["root"]).build(2)
    with pytest.raises(ValueError, match="write transition"):
        verify_reviewed_candidate_read_set(trusted.story_project_context["read_set"], phase=phase)


def test_ordinary_candidate_still_rejects_v3_before_any_provider_or_artifact(case):
    _, projects, project, _ = registered_upgrade(case)
    confirmation = author_confirmation(projects, project)
    before = inventory(case["root"])
    calls = []
    executor = ChapterCandidateExecutor(story_project_root=case["root"], generator=lambda _: calls.append(1) or "正文",
        execution_kind="deterministic", provenance_factory=_fixed_provenance)
    with pytest.raises(ProjectIdentityError):
        executor.execute(chapter_index=2, confirmation=confirmation, idempotency_key="ordinary-rejected")
    assert not calls and inventory(case["root"]) == before


def test_actual_v3_candidate_to_persisted_author_review_and_standard_quality(case, monkeypatch):
    root = case["root"]
    db, projects, project, p = registered_upgrade(case)
    factory = ReviewedChapterCandidateContextBuilder
    confirmation = author_confirmation(projects, project)
    example = fact_contract_example()
    blueprint = factory(root).build(2).story_project_context["chapter_blueprint"]
    good = Path("tests/fixtures/chapter_quality/good_chapter.md").read_text(encoding="utf-8").replace("林雪", "林澈").replace("陈岚", "周禾")
    text = "第二章。\n" + example["chapter_text"] + "\n\n" + good + "\n\n" + "。".join(b["text"] for b in blueprint["required_beats"]) + "。\n\n" + blueprint["ending_pressure"] + "。"
    calls = []
    def deny(*args, **kwargs):
        pytest.fail("isolated v3 workflow attempted a network call")
    monkeypatch.setattr(socket.socket, "connect", deny)
    executor = ReviewedChapterCandidateExecutor(story_project_root=root,
        generator=lambda prompt: calls.append(prompt) or text, execution_kind="deterministic",
        repository_root=Path.cwd(), provenance_factory=_fixed_provenance)
    record = executor.execute(chapter_index=2, confirmation=confirmation, idempotency_key="actual-v3-candidate")
    assert record["status"] == "completed"
    assert record["authority"]["identity_revision"] != p["binding"]["old_identity_sha256"]
    assert executor.execute(chapter_index=2, confirmation=confirmation, idempotency_key="actual-v3-candidate") == record
    facts_view = build_chapter_fact_context(factory(root).build(2), text).to_dict()
    assert facts_view["selection"]["authority_source"] == "legacy_reviewed_state"
    drafts = CandidateDraftStore(db)
    draft = drafts.create_from_candidate(project_id=project.id, chapter=2, candidate_id=record["candidate_id"],
        source_sha256=record["prose"]["sha256"], source_artifact_ref=record["prose"]["artifact_ref"], text=text)
    reviews = ChapterFactReviewService(database=db, projects=projects, candidate_drafts=drafts, context_builder_factory=factory)
    review = reviews.create(draft.id, expected_draft_revision=draft.revision, expected_draft_sha256=draft.sha256,
        proposals=[item for item in example["proposal"]["facts"] if item["kind"] in {"event", "counter"}], author_confirmed=True)
    facts = copy.deepcopy(review["facts"])
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    review = reviews.update(review["id"], facts=facts, expected_revision=review["revision"])
    review = reviews.confirm(review["id"], expected_revision=review["revision"], expected_facts_sha256=review["facts_sha256"],
        expected_draft_revision=draft.revision, expected_draft_sha256=draft.sha256, author_confirmed=True)
    service = ChapterCommitPreparationService(database=db, projects=projects, candidate_drafts=drafts,
        fact_reviews=reviews, context_builder_factory=factory)
    request = dict(project_id=project.id, chapter=2, review_id=review["id"],
        expected_draft_revision=draft.revision, expected_review_revision=review["revision"],
        expected_draft_sha256=draft.sha256, expected_facts_sha256=review["facts_sha256"], idempotency_key="v3-commit")
    before_quality = inventory(root)
    result = service.prepare_quality(**request)
    assert result["preparation"]["binding"]["book_id"] == project.book_id
    assert result["quality"]["quality_coverage_complete"] is True
    assert result["quality"]["local_quality_passed"] is True, result["quality"]["local_policy_decision"]["findings"]
    assert result["publication_authorized"] is False
    strict = service.prepare_quality(**request, quality_policy="strict")["quality"]
    assert strict["quality_coverage_complete"] is False and strict["local_quality_passed"] is False
    assert strict["pending_gates"] == ["llm_validator"]
    assert inventory(root) == before_quality
    assert len(calls) == 1 and (root / RELATIVE_PATH).read_bytes() == p["baseline_document"].encode("utf-8")
    manual = root / "追踪/上下文.md"
    manual.write_bytes(manual.read_bytes() + b"\nchanged")
    with pytest.raises((ValueError, RuntimeError)):
        service.prepare_quality(**request)
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["manual_source", "old_prose"])
def test_v3_generation_retains_stale_draft_if_verified_sources_change(case, damage):
    _, projects, project, _ = registered_upgrade(case)
    confirmation = author_confirmation(projects, project)
    from core.story_project.paths import resolve_prose
    target = (case["root"] / "追踪/上下文.md" if damage == "manual_source"
              else resolve_prose(case["root"], 1).path)
    calls = []
    def generate(prompt):
        calls.append(prompt)
        target.write_bytes(target.read_bytes() + b"\nchanged")
        return fact_contract_example()["chapter_text"]
    executor = ReviewedChapterCandidateExecutor(story_project_root=case["root"], generator=generate,
        execution_kind="deterministic", provenance_factory=_fixed_provenance)
    record = executor.execute(chapter_index=2, confirmation=confirmation, idempotency_key="drifting-source")
    assert record["status"] == "stale"
    assert record["source_verification"]["after"]["ok"] is False
    assert executor.store.load(record["candidate_id"])["prose"] == fact_contract_example()["chapter_text"]
    assert executor.resume(record["candidate_id"]) == record and len(calls) == 1


def test_v3_changed_outline_rejects_old_confirmation_before_generation(case):
    _, projects, project, _ = registered_upgrade(case)
    confirmation = author_confirmation(projects, project)
    outline = next((case["root"] / "大纲").glob("*.md"))
    outline.write_bytes(outline.read_bytes() + "\n补充作者备注。\n".encode("utf-8"))
    before = inventory(case["root"])
    calls = []
    executor = ReviewedChapterCandidateExecutor(story_project_root=case["root"],
        generator=lambda _: calls.append(1) or "正文", execution_kind="deterministic", provenance_factory=_fixed_provenance)
    with pytest.raises(ChapterCandidateConflictError, match="inputs changed"):
        executor.execute(chapter_index=2, confirmation=confirmation, idempotency_key="old-confirmation")
    assert not calls and inventory(case["root"]) == before
