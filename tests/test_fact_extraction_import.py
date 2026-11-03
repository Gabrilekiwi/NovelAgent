"""Interrupted extraction imports are explicit, local and preserve author edits."""
from __future__ import annotations

import copy
import json

import pytest

from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_chapter_publication_api import api_case
from tests.test_chapter_publication_service import case, ready, service_case
from tests.test_reviewed_workbench_workflow import new_candidate
from tests.test_workbench_chapter_fact_extractions import FactExtractionExecutorFactory
from workbench.chapter_fact_extractions import ChapterFactExtractionService


class ImportInterrupted(RuntimeError):
    pass


def _extract(c, monkeypatch, *, mode="completed"):
    draft = new_candidate(c)["draft"]
    raw = json.dumps({"schema_version": "1.0", "facts": [f
        for f in c["example"]["proposal"]["facts"] if f["kind"] in {"event", "counter"}]})
    factory = FactExtractionExecutorFactory("not-json" if mode == "failed" else raw)
    service = c["app"].state.chapter_fact_extractions.reviewed

    def build(root):
        executor = factory(root)
        if mode == "unfinished":
            def crash(event, extraction_id):
                if event == "after_intent_before_provider":
                    raise ImportInterrupted("simulated process loss before Provider")
            executor.fault_injector = crash
        return executor

    service.executor_factory = build
    path = f"/api/candidate-drafts/{draft['id']}/fact-extractions"
    with monkeypatch.context() as scoped:
        def crash_before_import(*args, **kwargs):
            raise ImportInterrupted("simulated process loss before local review creation")
        scoped.setattr(service, "_finalize", crash_before_import)
        with pytest.raises(ImportInterrupted):
            c["client"].post(path, headers={"Idempotency-Key": "interrupted-review-import"}, json={
                "expected_draft_revision": draft["revision"],
                "expected_draft_sha256": draft["sha256"], "author_confirmed": True})
    listed = c["client"].get(path)
    assert listed.status_code == 200, listed.text
    extraction = listed.json()["items"][0]
    assert extraction["fact_review"] is None
    return draft, extraction, factory, path + "/" + extraction["extraction_id"]


def _body(extraction, **changes):
    return {"expected_record_hash": extraction["record_hash"] or "0" * 64,
            "author_confirmed": True, **changes}


def _review_count(c, draft):
    with c["db"].connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM chapter_fact_reviews WHERE draft_id = ?",
                                  (draft["id"],)).fetchone()[0]


def _forbid_executor(root):
    pytest.fail("local review import must never construct an executor or call Provider")


def test_completed_import_after_interruption_is_read_only_until_explicit_and_preserves_edits(api_case, monkeypatch):
    c = api_case
    before_state = (c["root"] / RELATIVE_PATH).read_bytes()
    draft, extraction, factory, path = _extract(c, monkeypatch)
    assert extraction["status"] == "completed" and factory.calls == 1
    old = c["app"].state.chapter_fact_extractions.reviewed
    # Recreate the service from durable stores, as on application restart. The
    # executor factory deliberately cannot be used even for terminal results.
    cold = ChapterFactExtractionService(projects=old.projects, candidate_drafts=old.candidate_drafts,
        chapter_facts=old.chapter_facts, context_builder_factory=old.context_builder_factory,
        executor_factory=_forbid_executor)
    c["app"].state.chapter_fact_extractions.reviewed = cold
    monkeypatch.setattr(c["app"].state.chapter_fact_extractions.legacy, "open_review",
                        lambda *args, **kwargs: pytest.fail("v3 import must use owned dispatch"))
    store = ChapterFactExtractionStore(c["root"])
    before_evidence = {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    for _ in range(2):
        result = c["client"].get(path)
        assert result.status_code == 200 and result.json()["fact_extraction"]["fact_review"] is None
        assert _review_count(c, draft) == 0
    ownership_calls = []
    original_mutate = c["app"].state.reviewed_workflow.mutate
    def tracked_mutate(project_id, operation):
        ownership_calls.append(project_id)
        return original_mutate(project_id, operation)
    monkeypatch.setattr(c["app"].state.reviewed_workflow, "mutate", tracked_mutate)
    result = c["client"].post(path + "/review", json=_body(extraction))
    assert result.status_code == 200, result.text
    review = result.json()["fact_extraction"]["fact_review"]
    assert review["source"]["extraction_record_hash"] == extraction["record_hash"]
    assert review["state"] == "editing" and _review_count(c, draft) == 1
    assert ownership_calls == [draft["project_id"]]
    edited = copy.deepcopy(review["facts"])
    edited[0]["summary"] = "作者重新核实并补充了本条摘要。"
    edited[0]["review"]["decision"] = "accepted"
    response = c["client"].put(f"/api/fact-reviews/{review['id']}",
        json={"facts": edited, "expected_revision": review["revision"]})
    assert response.status_code == 200, response.text
    edited_review = response.json()["fact_review"]
    repeated = c["client"].post(path + "/review", json=_body(extraction))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["fact_extraction"]["fact_review"] == edited_review
    assert _review_count(c, draft) == 1 and factory.calls == 1
    assert {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before_evidence
    assert (c["root"] / RELATIVE_PATH).read_bytes() == before_state and infer_next_chapter(c["root"]) == 2


def test_import_requires_confirmation_exact_record_and_draft_binding(api_case, monkeypatch):
    c = api_case
    draft, extraction, factory, path = _extract(c, monkeypatch)
    c["app"].state.chapter_fact_extractions.reviewed.executor_factory = _forbid_executor
    for body in (_body(extraction, author_confirmed=False), _body(extraction, expected_record_hash="0" * 64)):
        response = c["client"].post(path + "/review", json=body)
        assert response.status_code == 409, response.text
    wrong_draft_path = f"/api/candidate-drafts/{c['draft'].id}/fact-extractions/{extraction['extraction_id']}/review"
    response = c["client"].post(wrong_draft_path, json=_body(extraction))
    assert response.status_code == 404, response.text
    assert _review_count(c, draft) == 0 and factory.calls == 1


def test_import_rejects_changed_draft_without_creating_review(api_case, monkeypatch):
    c = api_case
    draft, extraction, factory, path = _extract(c, monkeypatch)
    c["app"].state.chapter_fact_extractions.reviewed.executor_factory = _forbid_executor
    response = c["client"].put(f"/api/candidate-drafts/{draft['id']}",
        json={"text": draft["text"] + "作者继续修改了正文。", "expected_revision": draft["revision"]})
    assert response.status_code == 200, response.text
    response = c["client"].post(path + "/review", json=_body(extraction))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "chapter_fact_extraction_stale"
    assert c["client"].get(path).json()["fact_extraction"]["source_state"] == "stale"
    assert _review_count(c, draft) == 0 and factory.calls == 1


@pytest.mark.parametrize("mode,expected_calls", [("unfinished", 0), ("failed", 1)])
def test_import_cannot_execute_unfinished_or_failed_extraction(api_case, monkeypatch, mode, expected_calls):
    c = api_case
    draft, extraction, factory, path = _extract(c, monkeypatch, mode=mode)
    assert extraction["status"] == ("needs_recovery" if mode == "unfinished" else "failed")
    c["app"].state.chapter_fact_extractions.reviewed.executor_factory = _forbid_executor
    response = c["client"].post(path + "/review", json=_body(extraction))
    assert response.status_code == 409, response.text
    assert _review_count(c, draft) == 0 and factory.calls == expected_calls


def test_import_rejects_corrupt_completed_evidence(api_case, monkeypatch):
    c = api_case
    draft, extraction, factory, path = _extract(c, monkeypatch)
    c["app"].state.chapter_fact_extractions.reviewed.executor_factory = _forbid_executor
    store = ChapterFactExtractionStore(c["root"])
    store.proposal_path(extraction["extraction_id"]).write_text("{}", encoding="utf-8")
    response = c["client"].post(path + "/review", json=_body(extraction))
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "chapter_fact_extraction_integrity_error"
    assert _review_count(c, draft) == 0 and factory.calls == 1
