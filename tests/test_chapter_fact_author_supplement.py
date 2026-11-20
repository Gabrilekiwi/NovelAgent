"""Author additions retain model evidence and share the existing review CAS."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from core.engine.chapter_fact_candidate import normalize_fact_proposal
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_chapter_publication_api import api_case
from tests.test_chapter_publication_service import case, ready, service_case
from tests.test_reviewed_fact_support_workflow import editable_review, supplements
from tests.test_workbench_chapter_candidates import PROSE, _formal_inventory, _unlock
from tests.test_workbench_chapter_fact_extractions import _prepare_draft, _setup, _start_extraction
from tests.test_workbench_chapter_facts import _fact_proposals
from workbench.chapter_facts import ChapterFactReviewConflict, ChapterFactReviewIntegrity, _author_update_transition


def author_addition():
    value = copy.deepcopy(_fact_proposals()[0])
    value.update(local_key="author_record_check", summary="周岚核对控制器记录。",
                 evidence_quotes=["周岚核对控制器记录"])
    value["proposal"].update(event_type="record_check", occurrence_location=None,
                               onsite_participants=[],
                               related_entities=[{"entity_id": "zhou_lan", "relation": "reference"}])
    fact = normalize_fact_proposal({"schema_version": "1.0", "facts": [value]}, chapter_text=PROSE, origin="author")[0]
    # This metadata is derived by the server, not trusted from the browser.
    fact["review"]["author_edited"] = False
    return fact


def test_author_additions_preserve_model_originals_history_and_cas(tmp_path):
    root, candidate_factory, fact_factory, app, project = _setup(tmp_path)
    formal_before = _formal_inventory(root)
    with TestClient(app) as client:
        _unlock(client)
        draft = _prepare_draft(client, project["id"], key="author-supplement-candidate")
        extraction = _start_extraction(client, draft, key="author-supplement-extraction")
        assert extraction.status_code == 200, extraction.text
        review = extraction.json()["fact_extraction"]["fact_review"]
        service = app.state.chapter_facts
        original = copy.deepcopy(review["facts"])
        context_response = client.get(f"/api/fact-reviews/{review['id']}/editor-context")
        assert context_response.status_code == 200, context_response.text
        context = context_response.json()
        assert context["original_model_facts"] == original
        assert context["source_text"] == PROSE
        assert context["support_supported"] is False and context["baseline_state"] is None
        assert set(context["payload_contracts"]) == {"event", "character_state", "relationship", "roster", "location", "inventory", "counter"}
        assert context["edit_history"] == [] and context["edit_history_total"] == 0
        assert context["publication_authorized"] is False

        proposed = copy.deepcopy(original) + [author_addition()]
        proposed[0]["summary"] = "作者核实两人进入控制室。"
        proposed[0]["review"].update(decision="accepted", reason="正文证据明确")
        path = f"/api/fact-reviews/{review['id']}"
        response = client.put(path, headers={"origin": "http://testserver"},
                              json={"facts": proposed, "expected_revision": review["revision"]})
        assert response.status_code == 200, response.text
        updated = response.json()["fact_review"]
        assert updated["revision"] == 2
        assert updated["facts"][0]["review"]["origin"] == "model"
        assert updated["facts"][0]["review"]["author_edited"] is True
        assert updated["facts"][-1]["review"] == {"origin": "author", "decision": "pending", "author_edited": True, "reason": None}
        context = service.editor_context(review["id"])
        assert context["revision"] == updated["revision"]
        assert context["original_model_facts"] == original
        assert context["edit_history"] == [{"from_revision": 1, "to_revision": 2,
            "before_facts": original, "after_facts": updated["facts"], "created_at": updated["updated_at"]}]

        with pytest.raises(ChapterFactReviewConflict):
            service.update(review["id"], facts=original, expected_revision=1)
        with pytest.raises(ChapterFactReviewConflict):
            service.update(review["id"], facts=updated["facts"], expected_revision=3)
        with pytest.raises(ChapterFactReviewConflict, match="cannot be removed"):
            service.update(review["id"], facts=original, expected_revision=2)
        assert service.reviews.edit_history(review["id"]) == context["edit_history"]

        for malformed in ("invented_quote", "duplicate_card", "unknown_source_event"):
            invalid = copy.deepcopy(updated["facts"])
            if malformed == "invented_quote":
                invalid[-1]["evidence_spans"][0]["quote"] = "正文没有发生的事件"
            elif malformed == "duplicate_card":
                invalid.append(copy.deepcopy(invalid[-1]))
            else:
                invalid[1]["proposal"]["source_event_key"] = "missing_event"
            rejected = client.put(path, headers={"origin": "http://testserver"},
                                  json={"facts": invalid, "expected_revision": 2})
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["error"]["code"] == "chapter_fact_candidate_invalid"
        assert service.reviews.get(review["id"]).revision == 2
        assert service.reviews.edit_history(review["id"]) == context["edit_history"]

        decided = copy.deepcopy(updated["facts"])
        for fact in decided:
            fact["review"].update(decision="accepted", reason="作者逐条核对正文", author_edited=False)
        decided[1]["review"].update(decision="rejected", reason="author_deleted")
        second = service.update(review["id"], facts=decided, expected_revision=2)
        assert second["facts"][0]["review"]["author_edited"] is True
        assert second["facts"][-1]["review"]["author_edited"] is True
        confirmed = service.confirm(review["id"], expected_revision=second["revision"],
            expected_facts_sha256=second["facts_sha256"], expected_draft_revision=draft["revision"],
            expected_draft_sha256=draft["sha256"], author_confirmed=True)
        assert confirmed["state"] == "confirmed"
        with pytest.raises(ChapterFactReviewConflict, match="immutable"):
            service.update(review["id"], facts=second["facts"], expected_revision=3)
        context = service.editor_context(review["id"])
        assert context["original_model_facts"] == original and context["edit_history_total"] == 2
        assert context["edit_history"][1]["before_facts"] == updated["facts"]
        assert context["edit_history"][1]["after_facts"] == second["facts"]

        app.state.chapter_candidates.drafts.update(draft["id"], text=PROSE + "作者后续改稿。", expected_revision=1)
        stale = service.editor_context(review["id"])
        assert stale["fresh"] is False and stale["stale_reason"] == "candidate_draft_changed"
        assert stale["source_text"] == PROSE and stale["original_model_facts"] == original
        assert stale["support_supported"] is False and stale["support_preview"] is None

        # Real immutable extraction artifacts, never the edited card array, are
        # the model comparison source. Corruption must not silently fall back.
        proposal_path = ChapterFactExtractionStore(root).proposal_path(review["source"]["extraction_id"])
        proposal_path.write_text("{}", encoding="utf-8")
        with pytest.raises(ChapterFactReviewIntegrity, match="original model"):
            service.editor_context(review["id"])

    assert candidate_factory.generator_calls == fact_factory.calls == 1
    assert _formal_inventory(root) == formal_before


@pytest.mark.parametrize("mutation", ["new_model", "changed_origin", "removed_original"])
def test_transition_cannot_relabel_or_hide_original_suggestions(mutation):
    current = normalize_fact_proposal({"schema_version": "1.0", "facts": _fact_proposals()}, chapter_text=PROSE)
    proposed = copy.deepcopy(current) + [author_addition()]
    if mutation == "new_model":
        proposed[-1]["review"]["origin"] = "model"
    elif mutation == "changed_origin":
        proposed[0]["review"]["origin"] = "author"
    else:
        proposed.pop(0)
    with pytest.raises(ChapterFactReviewConflict):
        _author_update_transition(current, proposed)


def test_reviewed_editor_context_has_verified_directory_and_support_preview(api_case):
    c = api_case
    draft, review = editable_review(c)
    service = c["reviews"]
    before = (c["root"] / RELATIVE_PATH).read_bytes()
    context_response = c["client"].get(f"/api/fact-reviews/{review['id']}/editor-context")
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert context["support_supported"] is True and context["entity_directory"]
    assert context["original_model_facts"] == [] and context["support_preview"] is None
    assert "赵原" not in {e["name"] for e in context["entity_directory"]}
    updated = service.update_support(review["id"], support=supplements(),
        expected_revision=review["revision"], author_confirmed=True)
    prepared = service.editor_context(review["id"])
    assert prepared["revision"] == updated["revision"]
    assert prepared["support_preview"]["publication_authorized"] is False
    assert prepared["baseline_state"] == context["baseline_state"]
    assert len(prepared["support_preview"]["identity_plan"]["declarations"]) == 3
    assert (c["root"] / RELATIVE_PATH).read_bytes() == before
    # Supplement updates consume review revisions without losing fact edits.
    assert prepared["edit_history"] == context["edit_history"]
    c["drafts"].update(draft.id, text=draft.text + "新的改稿。", expected_revision=draft.revision)
    stale = service.editor_context(review["id"])
    assert stale["source_text"] == draft.text
    assert stale["support_supported"] is False and stale["baseline_state"] is None


def test_edit_history_detects_snapshot_corruption(api_case):
    c = api_case
    _, review = editable_review(c)
    history = c["reviews"].reviews.edit_history(review["id"])
    assert len(history) == 1
    changed = copy.deepcopy(history[0]["before_facts"])
    changed[0]["summary"] = "损坏的旧快照"
    with c["db"].transaction() as connection:
        connection.execute("UPDATE chapter_fact_review_edits SET before_facts_json=? WHERE review_id=?",
                           (json.dumps(changed), review["id"]))
    with pytest.raises(ChapterFactReviewIntegrity, match="history"):
        c["reviews"].editor_context(review["id"])
