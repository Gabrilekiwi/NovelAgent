from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import uuid

import pytest

from core.engine.chapter_candidate import ChapterCandidateExecutor
from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_fact_extraction import ChapterFactExtractionIntegrityError, ChapterFactExtractionStore
from core.engine.chapter_fact_contract import fact_contract_example
from scripts.experiments.d1_reviewed_state import load_runtime_view
from scripts.experiments.run_d1_source_experiment import prepare_isolated_book
from scripts.experiments.run_d2_b0_compatibility import inventory
from tests.test_workbench_chapter_candidates import _fixed_provenance
from tests.test_workbench_chapter_fact_extractions import FactExtractionExecutorFactory
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_cards import ChapterCardService
from workbench.chapter_commits import ChapterCommitConflict, ChapterCommitIntegrityError, ChapterCommitPreparationService
from workbench.chapter_facts import ChapterFactReviewService, ChapterFactReviewIntegrity
from workbench.chapter_fact_extractions import ChapterFactExtractionService
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry


@pytest.fixture
def case(monkeypatch,request):
    # Short inherited Windows path; all generated records remain synthetic.
    directory = Path.cwd() / ".tmp/cp3" / uuid.uuid4().hex[:16]
    directory.mkdir(parents=True)
    original = prepare_isolated_book()
    root = directory / "b"
    shutil.copytree(original["root"],root)
    source_loader = load_runtime_view
    formal_case = None
    if getattr(request,"param",None) == "formal":
        from core.story_project.identity import load_project_identity
        from core.story_project.managed_block import parse_managed_block
        from core.story_project.paths import canonical_outline_path
        from core.story_project.reviewed_state_document import (
            RELATIVE_PATH, baseline_request, capture_sources, create_baseline, digest,
        )
        from core.story_project.reviewed_state_store import load_runtime_view as verified_runtime_view
        from tests.test_reviewed_state_document import publish

        # Convert only this synthetic fixture through the real transaction.
        # Production identity 3.0 upgrade/qualification is a separate gate.
        old = parse_managed_block((root / RELATIVE_PATH).read_bytes()).projection["values"]["reviewed_state"]
        book_id = load_project_identity(root).book_id
        old_identity_sha = digest((root / ".novelagent/project.json").read_bytes())
        confirmation = baseline_request(book_id=book_id,chapter=2,state=old["current_state"],sources=capture_sources(root),
            old_identity_sha256=old_identity_sha,static_references=old["static_references"])
        document = create_baseline(root=root,book_id=book_id,chapter=2,state=old["current_state"],
            confirmed_baseline=confirmation,old_identity_sha256=old_identity_sha,run_id="baseline",
            static_references=old["static_references"])
        runtime = root / ".novelagent/runtime"
        runtime.mkdir(parents=True,exist_ok=True)
        formal_case = {"root":root,"runtime":runtime,"roots":{"story_project":root,"runtime":runtime},
                       "book_id":book_id,"document":document}
        publish(formal_case,document)
        canonical_outline_path(root,4).write_text(canonical_outline_path(root,3).read_text(encoding="utf-8").replace("第003章","第004章"),encoding="utf-8")
        def source_loader(context):
            return verified_runtime_view(context,root_map=formal_case["roots"],baseline_document_sha256=digest(document))
    database = WorkbenchDatabase(directory / "workbench.db")
    projects = ProjectRegistry(database,allowed_workspaces=[directory])
    project = projects.import_project(root)
    drafts = CandidateDraftStore(database)
    reviews = ChapterFactReviewService(database=database,projects=projects,candidate_drafts=drafts)

    def builder(path):
        return ChapterCandidateContextBuilder(path,reviewed_state_loader=source_loader)

    # Explicit D1 loader injection only; no guard, authority or persistence
    # verification is patched. Normal entrypoints still do not discover D1.
    monkeypatch.setattr("workbench.chapter_cards.ChapterCandidateContextBuilder",builder)
    cards = ChapterCardService(projects)
    card = cards.get(project.id,3).to_dict()
    confirmation = cards.confirm(project.id,3,{
        "confirmed":True,"expected_card_sha256":card["confirmation"]["card_sha256"],
        "goal":"检查警戒状态","validation_focus":["logic"],"notes_text":""})
    example = fact_contract_example()
    text = "第三章。\n" + example["chapter_text"]
    calls = []
    executor = ChapterCandidateExecutor(story_project_root=root,
        generator=lambda prompt: calls.append(prompt) or text,execution_kind="deterministic",
        repository_root=Path.cwd(),provenance_factory=_fixed_provenance)
    executor.context_builder = builder(root)
    record = executor.execute(chapter_index=3,confirmation=confirmation,idempotency_key="cp3-candidate")
    assert record["status"] == "completed"
    prose = executor.store.load_prose(record["candidate_id"])
    draft = drafts.create_from_candidate(project_id=project.id,chapter=3,candidate_id=record["candidate_id"],
        source_sha256=record["prose"]["sha256"],source_artifact_ref=record["prose"]["artifact_ref"],text=prose)
    proposals = [p for p in example["proposal"]["facts"] if p["kind"] in {"event","counter"}]
    extraction = None
    if getattr(request,"param",None) == "extracted":
        monkeypatch.setattr("workbench.chapter_fact_extractions.ChapterCandidateContextBuilder",builder)
        extraction_factory = FactExtractionExecutorFactory(json.dumps({"schema_version":"1.0","facts":proposals},ensure_ascii=False))
        extraction_service = ChapterFactExtractionService(projects=projects,candidate_drafts=drafts,
            chapter_facts=reviews,executor_factory=extraction_factory)
        extraction = extraction_service.start(draft.id,expected_draft_revision=draft.revision,
            expected_draft_sha256=draft.sha256,idempotency_key="cp3-extraction",author_confirmed=True)
        review = reviews.create_from_extraction(draft.id,extraction_id=extraction["extraction_id"],
            expected_extraction_record_hash=extraction["record_hash"],author_confirmed=True)
    else:
        review = reviews.create(draft.id,expected_draft_revision=draft.revision,expected_draft_sha256=draft.sha256,
            proposals=proposals,author_confirmed=True)
    facts = copy.deepcopy(review["facts"])
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    reviewed = reviews.update(review["id"],facts=facts,expected_revision=review["revision"])
    confirmed = reviews.confirm(review["id"],expected_revision=reviewed["revision"],
        expected_facts_sha256=reviewed["facts_sha256"],expected_draft_revision=draft.revision,
        expected_draft_sha256=draft.sha256,author_confirmed=True)
    service = ChapterCommitPreparationService(database=database,projects=projects,candidate_drafts=drafts,
        fact_reviews=reviews,context_builder_factory=builder)
    request = {"project_id":project.id,"chapter":3,"review_id":review["id"],
        "expected_draft_revision":draft.revision,"expected_review_revision":confirmed["revision"],
        "expected_draft_sha256":draft.sha256,"expected_facts_sha256":confirmed["facts_sha256"],
        "idempotency_key":"cp3-commit"}
    return locals()


def test_server_preparation_reads_verified_candidate_and_database_without_writing(case):
    before = inventory(case["root"])
    with case["database"].connection() as connection:
        db_before = list(connection.iterdump())
    result = case["service"].prepare(**case["request"])
    assert result["binding"]["book_id"] == case["project"].book_id
    assert result["binding"]["candidate_id"] == case["record"]["candidate_id"]
    assert result["fact_preparation"]["state_after"]["numeric_counters"]["alert_level"]["current_value"] == 4
    assert result["publication_authorized"] is False
    assert case["service"].prepare(**case["request"]) == result
    assert len(case["calls"]) == 1
    assert inventory(case["root"]) == before
    with case["database"].connection() as connection:
        assert list(connection.iterdump()) == db_before


@pytest.mark.parametrize("field,value",[("chapter",4),("expected_draft_revision",2),
    ("expected_review_revision",7),("expected_draft_sha256","0"*64),("expected_facts_sha256","0"*64)])
def test_request_route_and_revision_cas(case,field,value):
    request = {**case["request"],field:value}
    with pytest.raises(ChapterCommitConflict):
        case["service"].prepare(**request)


def test_recomputed_draft_hash_does_not_make_old_confirmation_current(case):
    draft = case["draft"]
    changed = case["drafts"].update(draft.id,text=draft.text+"新句。",expected_revision=draft.revision)
    restored = case["drafts"].update(draft.id,text=draft.text,expected_revision=changed.revision)
    assert restored.sha256 == draft.sha256
    with pytest.raises(ChapterCommitConflict,match="changed"):
        case["service"].prepare(**case["request"])


@pytest.mark.parametrize("column,value",[("confirmation_sha256","0"*64),("facts_sha256","0"*64),
                                        ("record_type","automatic_policy_acceptance")])
def test_corrupt_database_confirmation_is_rejected(case,column,value):
    with case["database"].transaction() as connection:
        connection.execute(f"UPDATE chapter_fact_reviews SET {column}=? WHERE id=?",(value,case["review"]["id"]))
    with pytest.raises(ChapterFactReviewIntegrity):
        case["service"].prepare(**case["request"])


@pytest.mark.parametrize("artifact",["input.md","draft.md","record.json"])
def test_candidate_artifact_corruption_is_not_hidden_by_valid_review_hash(case,artifact):
    path = case["executor"].store.candidate_dir(case["record"]["candidate_id"]) / artifact
    path.write_text("corrupt",encoding="utf-8")
    with pytest.raises(ChapterFactReviewIntegrity):
        case["service"].prepare(**case["request"])
    assert len(case["calls"]) == 1


def test_edit_during_calculation_is_detected_after_read_snapshot_closes(case,monkeypatch):
    import workbench.chapter_commits as commits
    original = commits.prepare_reviewed_chapter
    def edit(**kwargs):
        result = original(**kwargs)
        draft = case["draft"]
        case["drafts"].update(draft.id,text=draft.text+"作者并发修改。",expected_revision=draft.revision)
        return result
    monkeypatch.setattr(commits,"prepare_reviewed_chapter",edit)
    with pytest.raises(ChapterCommitConflict,match="during preparation"):
        case["service"].prepare(**case["request"])


def test_read_pair_uses_one_sqlite_snapshot(case,monkeypatch):
    original = case["reviews"].reviews.get
    def edit_after_review_read(review_id,*,connection=None):
        record = original(review_id,connection=connection)
        assert connection is not None and connection.in_transaction
        draft = case["draft"]
        case["drafts"].update(draft.id,text=draft.text+"并发写入。",expected_revision=draft.revision)
        return record
    monkeypatch.setattr(case["reviews"].reviews,"get",edit_after_review_read)
    draft, review = case["service"]._read_pair(case["review"]["id"])
    assert draft.sha256 == review.source_draft_sha256 == case["draft"].sha256
    assert case["drafts"].get(draft.id).revision == 2


def test_default_builder_does_not_silently_enable_experimental_source(case):
    service = ChapterCommitPreparationService(database=case["database"],projects=case["projects"],
        candidate_drafts=case["drafts"],fact_reviews=case["reviews"])
    with pytest.raises(ChapterCommitConflict,match="current context differs"):
        service.prepare(**case["request"])


def test_store_from_another_database_is_rejected(case):
    other = CandidateDraftStore(WorkbenchDatabase(case["directory"] / "other.db"))
    with pytest.raises(ChapterCommitIntegrityError,match="same database"):
        ChapterCommitPreparationService(database=case["database"],projects=case["projects"],
            candidate_drafts=other,fact_reviews=case["reviews"])


def test_preparation_is_independent_of_fact_prompt_character_budget(case,monkeypatch):
    import core.engine.chapter_fact_context as context_module
    monkeypatch.setattr(context_module,"build_chapter_fact_context",lambda *a,**k: (_ for _ in ()).throw(AssertionError("no prompt generation during prepare")))
    assert case["service"].prepare(**case["request"])["stage"] == "facts_prepared"


@pytest.mark.parametrize("case",["extracted"],indirect=True)
def test_model_suggestions_keep_their_verified_extraction_evidence(case):
    result = case["service"].prepare(**case["request"])
    assert result["binding"]["acceptance_kind"] == "author"
    assert all(f["review"]["origin"] == "model" for f in result["facts"])
    assert case["extraction_factory"].calls == 1


@pytest.mark.parametrize("case",["extracted"],indirect=True)
def test_missing_extraction_record_prevents_preview_without_regeneration(case):
    store = ChapterFactExtractionStore(case["root"])
    path = store.record_path(case["extraction"]["extraction_id"])
    path.write_text("corrupt",encoding="utf-8")
    with pytest.raises(ChapterFactExtractionIntegrityError):
        case["service"].prepare(**case["request"])
    assert case["extraction_factory"].calls == 1


@pytest.mark.parametrize("case",["formal"],indirect=True)
def test_verified_formal_state_flows_through_actual_server_preparation_and_next_chapter(case):
    from core.story_project.reviewed_state_document import prepare_chapter_document
    from tests.test_reviewed_state_document import publish

    prepared = case["service"].prepare(**case["request"])
    formal = case["formal_case"]
    formal["prose"] = prepared["final_prose"]
    after = prepare_chapter_document(document=formal["document"],book_id=formal["book_id"],
                                     preparation=prepared,run_id="chapter-3")
    assert publish(formal,after)["committed"]
    next_context = case["builder"](case["root"]).build(4)
    view = next_context.story_project_context["reviewed_state"]
    assert view["committed_through"] == 3
    assert view["authoritative_state"] == prepared["fact_preparation"]["state_after"]
    assert view["authoritative_state"]["numeric_counters"]["alert_level"]["current_value"] == 4
    assert len(case["calls"]) == 1
