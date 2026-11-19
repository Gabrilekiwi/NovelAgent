"""Main app service dispatch from real v3 Candidate through author publication."""
import json

import pytest

from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.story_project.paths import infer_next_chapter
from tests.test_chapter_publication_api import case, ready, service_case, api_case, open_workbench
from tests.test_reviewed_candidate_context import _fixed_provenance
from tests.test_workbench_chapter_fact_extractions import FactExtractionExecutorFactory


def new_candidate(c):
    calls = []
    c["app"].state.chapter_candidates.reviewed.executor_factory = lambda root: ReviewedChapterCandidateExecutor(
        story_project_root=root, generator=lambda _: calls.append(1) or c["text"],
        execution_kind="deterministic", provenance_factory=_fixed_provenance)
    response = c["client"].post(c["path"] + "/candidates", json={"confirmation": c["confirmation"]},
        headers={"Idempotency-Key":"main-reviewed-candidate"})
    assert response.status_code == 200, response.text
    assert response.json()["candidate"]["status"] == "completed", response.text
    assert calls == [1]
    return response.json()["candidate"]


def test_app_candidate_extraction_author_review_and_publication_use_reviewed_state(service_case):
    with open_workbench(service_case) as c:
        candidate = new_candidate(c)
        draft = candidate["draft"]
        edited = draft['text'].rstrip() + "\n\n他把那封信重新折好，放回靠近胸口的衣袋。"
        saved = c["client"].put(f"/api/candidate-drafts/{draft['id']}", json={"text": edited, "expected_revision": draft["revision"]})
        assert saved.status_code == 200, saved.text
        assert infer_next_chapter(c["root"]) == 2
        assert saved.json()["draft"]["revision"] == draft["revision"] + 1
        draft = saved.json()["draft"]
        factory = FactExtractionExecutorFactory(json.dumps({"schema_version":"1.0", "facts": [f
            for f in c["example"]["proposal"]["facts"] if f["kind"] in {"event", "counter"}]}, ensure_ascii=False))
        c["app"].state.chapter_fact_extractions.reviewed.executor_factory = factory
        response = c["client"].post(f"/api/candidate-drafts/{draft['id']}/fact-extractions",
            json={"expected_draft_revision":draft["revision"], "expected_draft_sha256":draft["sha256"], "author_confirmed":True},
            headers={"Idempotency-Key":"main-reviewed-extraction"})
        assert response.status_code == 200, response.text
        extraction = response.json()["fact_extraction"]
        assert extraction["status"] == "completed", response.text
        review = extraction["fact_review"]
        for fact in review["facts"]:
            fact["review"]["decision"] = "accepted"
        response = c["client"].put(f"/api/fact-reviews/{review['id']}",
            json={"facts":review["facts"], "expected_revision":review["revision"]})
        assert response.status_code == 200, response.text
        review = response.json()["fact_review"]
        response = c["client"].post(f"/api/fact-reviews/{review['id']}/confirm", json={
            "expected_revision":review["revision"], "expected_facts_sha256":review["facts_sha256"],
            "expected_draft_revision":draft["revision"], "expected_draft_sha256":draft["sha256"], "author_confirmed":True})
        assert response.status_code == 200, response.text
        review = response.json()["fact_review"]
        assert review["state"] == "confirmed"
        assert review["publication_url"].startswith("/publication?")
        body = {"review_id":review["id"], "expected_draft_revision":draft["revision"],
            "expected_draft_sha256":draft["sha256"], "expected_review_revision":review["revision"],
            "expected_facts_sha256":review["facts_sha256"]}
        preview = c["client"].post(c["path"] + "/publication-preview", json=body,
            headers={"Idempotency-Key":"main-reviewed-publish"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["quality"]["local_quality_passed"]
        published = c["client"].post(c["path"] + "/publication", json={**body,"author_confirmed":True},
            headers={"Idempotency-Key":"main-reviewed-publish"})
        assert published.status_code == 200, published.text
        assert published.json()["publication"]["publication_verified"] and infer_next_chapter(c["root"]) == 3
        assert factory.calls == 1

    # Discard the app, session and injected generators; reopen the persisted book.
    with open_workbench(service_case) as reopened:
        client = reopened["client"]
        chapter = client.get(reopened["path"]).json()
        assert chapter["publication"]["publication_verified"]
        assert edited in chapter["current_prose"]
        project = f"/api/projects/{reopened['project'].id}"
        assert client.get(project).json()["project"]["next_chapter"] == 3
        card = client.get(project + "/chapters/3/card").json()["chapter_card"]
        assert card["readiness"]["can_generate"]
        # Inspect the actual messages prepared for chapter 3 without dispatching them.
        from modules.chapter_generator.generator import prepare_chapter_messages
        executor = ReviewedChapterCandidateExecutor(story_project_root=reopened["root"])
        request = executor.prepare_policy_request(chapter_index=3, chapter_intent=card["intent_defaults"])
        messages, _ = prepare_chapter_messages(request["input_pack"])
        prompt = "\n".join(message["content"] for message in messages)
        assert "Previous Chapter Confirmed Facts" in prompt and review["facts"]
        for fact in review["facts"]:
            assert fact["summary"] in prompt
            assert all(span["quote"] in prompt for span in fact["evidence_spans"])


def test_app_views_use_verified_publication_without_a_browser_request_key(api_case):
    c = api_case
    listing = c["client"].get("/api/projects")
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"][0]["reviewed_format"]
    detail = c["client"].get(f"/api/projects/{c['project'].id}")
    assert detail.status_code == 200 and detail.json()["project"]["next_chapter"] == 2
    assert c["client"].get(f"/api/projects/{c['project'].id}/health").status_code == 200
    c["app"].state.chapter_publication.commit(**c["route"])
    chapter = c["client"].get(c["path"])
    assert chapter.status_code == 200, chapter.text
    value = chapter.json()
    assert value["publication"]["publication_verified"] and value["has_prose"]
    assert value["current_prose"] == c["preparation"]["final_prose"]
    # This is derived from the full retained receipt, without client storage or
    # an idempotency key. No subsequent Candidate needs to be generated.
    assert c["client"].get(f"/api/projects/{c['project'].id}").json()["project"]["next_chapter"] == 3
    exported = c["client"].get(f"/api/projects/{c['project'].id}/export")
    assert exported.status_code == 200 and c["preparation"]["final_prose"].rstrip() in exported.text


def test_app_pending_views_remain_readable_before_explicit_recovery(api_case):
    from tests.test_reviewed_chapter_commit import PowerLoss
    c = api_case
    service = c["app"].state.chapter_publication
    def crash(event, position, path):
        if event == "after_apply_target" and position == 0:
            raise PowerLoss()
    service.persistence_fault_injector = crash
    with pytest.raises(PowerLoss):
        service.commit(**c["route"])
    service.persistence_fault_injector = None
    response = c["client"].get(f"/api/projects/{c['project'].id}")
    assert response.status_code == 200, response.text
    assert response.json()["project"]["reviewed_status"]["state"] == "needs_recovery"
    assert response.json()["project"]["next_chapter"] is None
    assert c["client"].get(c["path"]).json()["status"] == "needs_recovery"
    recovered = c["client"].post(f"/api/projects/{c['project'].id}/publication-recovery", json={"author_confirmed":True})
    assert recovered.status_code == 200, recovered.text
    assert c["client"].get(f"/api/projects/{c['project'].id}").json()["project"]["next_chapter"] == 2


def test_unowned_book_allows_reading_evidence_but_refuses_new_work(api_case):
    c = api_case
    c["app"].state.chapter_publication.machine_store = c["root"].parent / "not-enrolled"
    response = c["client"].get(c["path"] + "/candidates")
    assert response.status_code == 200 and response.json()["items"]
    c["app"].state.chapter_candidates.reviewed.executor_factory = lambda _: pytest.fail("must not construct executor before ownership")
    response = c["client"].post(c["path"] + "/candidates", json={"confirmation":c["confirmation"]},
        headers={"Idempotency-Key":"unowned-candidate"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "book_ownership_invalid"
    response = c["client"].put(f"/api/candidate-drafts/{c['draft'].id}",
        json={"text":c["draft"].text+"extra", "expected_revision":c["draft"].revision})
    assert response.status_code == 409 and c["drafts"].get(c["draft"].id) == c["draft"]
    assert infer_next_chapter(c["root"]) == 2


def test_retained_recovery_blocker_prevents_draft_and_fact_mutations(api_case):
    c = api_case
    directory = c["root"] / ".novelagent/runtime/reviewed/p/registry/recovery_required"
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "retained"
    marker.write_bytes(b"retain")
    response = c["client"].put(f"/api/candidate-drafts/{c['draft'].id}",
        json={"text":c["draft"].text+"extra", "expected_revision":c["draft"].revision})
    assert response.status_code == 409 and response.json()["error"]["code"] == "chapter_commit_recovery_required"
    response = c["client"].put(f"/api/fact-reviews/{c['review']['id']}",
        json={"facts":[], "expected_revision":c["review"]["revision"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "chapter_commit_recovery_required"
    assert marker.read_bytes() == b"retain" and c["drafts"].get(c["draft"].id) == c["draft"]


def test_reviewed_provider_uncertainty_blocks_new_calls_but_allows_explicit_resolution(api_case):
    c = api_case
    candidate = new_candidate(c)
    draft = candidate["draft"]
    factory = FactExtractionExecutorFactory(json.dumps({"schema_version":"1.0", "facts": []}))
    factory.uncertain = True
    c["app"].state.chapter_fact_extractions.reviewed.executor_factory = factory
    path = f"/api/candidate-drafts/{draft['id']}/fact-extractions"
    body = {"expected_draft_revision":draft["revision"], "expected_draft_sha256":draft["sha256"], "author_confirmed":True}
    first = c["client"].post(path, json=body, headers={"Idempotency-Key":"reviewed-uncertain"})
    assert first.status_code == 200, first.text
    extraction = first.json()["fact_extraction"]
    assert extraction["status"] == "provider_uncertain" and factory.calls == 1
    blocked = c["client"].post(path, json=body, headers={"Idempotency-Key":"reviewed-blocked"})
    assert blocked.status_code == 409, blocked.text
    assert factory.calls == 1
    result = c["client"].post(path + f"/{extraction['extraction_id']}/abandon-provider", json={
        "expected_intent_hash":extraction["intent_hash"], "expected_record_hash":extraction["record_hash"],
        "expected_uncertainty_sha256":extraction["uncertainty"]["sha256"], "author_confirmed":True})
    assert result.status_code == 200, result.text
    assert result.json()["fact_extraction"]["status"] == "abandoned" and factory.calls == 1
    factory.uncertain = False
    completed = c["client"].post(path, json=body, headers={"Idempotency-Key":"reviewed-after-resolution"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["fact_extraction"]["status"] == "completed" and factory.calls == 2
    assert infer_next_chapter(c["root"]) == 2
