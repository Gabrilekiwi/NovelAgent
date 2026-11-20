"""Actual app routes on isolated, upgraded and explicitly enrolled books."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_chapter_publication_service import case, ready, service_case, PowerLoss
from workbench.app import create_app


@contextmanager
def open_workbench(c):
    app = create_app(database_path=c["db"].path, allowed_workspaces=[c["root"].parent],
        runtime_root=c["root"].parent / "jobs", start_worker=False, auto_import_active=False,
        chapter_publication_machine_store=c["machine"])
    with TestClient(app) as client:
        client.get("/")
        route = c["route"]
        body = {k: v for k, v in route.items() if k not in {"project_id", "chapter", "idempotency_key"}}
        yield {**c, "client": client, "app": app, "body": body,
            "headers": {"Idempotency-Key": route["idempotency_key"]},
            "path": f"/api/projects/{route['project_id']}/chapters/{route['chapter']}"}


@pytest.fixture
def api_case(service_case):
    with open_workbench(service_case) as c:
        yield c


def send(c, suffix, *, commit=False, **kwargs):
    body = dict(c["body"])
    if commit:
        body["author_confirmed"] = True
    return c["client"].post(c["path"] + "/publication" + suffix,
        json=kwargs.pop("body", body), headers=kwargs.pop("headers", c["headers"]), **kwargs)


def test_preview_is_read_only_and_api_publishes_exactly_once(api_case):
    c = api_case
    before = (c["root"] / RELATIVE_PATH).read_bytes()
    response = send(c, "-preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["publication_authorized"] is False and preview["quality"]["quality_coverage_complete"]
    assert (c["root"] / RELATIVE_PATH).read_bytes() == before and infer_next_chapter(c["root"]) == 2
    assert not (c["root"] / ".novelagent/runtime/reviewed/intents").exists()
    first = send(c, "", commit=True)
    assert first.status_code == 200, first.text
    assert first.json()["publication"]["publication_verified"] is True
    assert send(c, "", commit=True).json() == first.json()
    assert send(c, "-status").json() == first.json()
    assert c["calls"] == [1] and infer_next_chapter(c["root"]) == 3
    conflict = send(c, "", commit=True, body={**c["body"], "author_confirmed": True, "expected_draft_revision": 2})
    assert conflict.status_code == 409


def test_status_of_never_submitted_request_does_not_publish(api_case):
    c = api_case
    result = send(c, "-status")
    assert result.status_code == 200, result.text
    assert result.json()["publication"]["state"] == "not_started"
    assert infer_next_chapter(c["root"]) == 2
    assert not (c["root"] / ".novelagent/runtime/reviewed/intents").exists()


@pytest.mark.parametrize("event,committed", [("after_prepare", False), ("after_file_commit_before_index", True)])
def test_status_recovers_original_request_without_preparing_again(api_case, event, committed):
    c = api_case
    service = c["app"].state.chapter_publication
    def crash(name, payload):
        if name == event:
            raise PowerLoss()
    service.fault_injector = crash
    with pytest.raises(PowerLoss):
        service.commit(**c["route"])
    service.fault_injector = None
    service.preparations.prepare = lambda **kwargs: pytest.fail("status must never prepare or generate")
    result = send(c, "-status")
    assert result.status_code == 200, result.text
    record = result.json()["publication"]
    assert record["publication_verified"] is committed
    if not committed:
        assert record["state"] == "rolled_back" and record["retry_requires_new_key"] and record["committed"] is False
    assert c["calls"] == [1]


def test_preview_pending_requires_explicit_original_recovery(api_case):
    c = api_case
    def crash(name, payload):
        if name == "after_prepare":
            raise PowerLoss()
    c["publication"].fault_injector = crash
    with pytest.raises(PowerLoss):
        c["publication"].commit(**c["route"])
    response = send(c, "-preview")
    assert response.status_code == 409 and response.json()["error"]["code"] == "chapter_commit_recovery_required"
    recovered = c["client"].post(f"/api/projects/{c['project'].id}/publication-recovery", json={"author_confirmed": True})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["recovery_verified"] and recovered.json()["next_chapter"] == 2
    assert recovered.json()["publications"] == []


def test_source_edit_is_conflict_and_draft_is_still_readable(api_case):
    c = api_case
    source = c["root"] / "追踪/上下文.md"
    source.write_bytes(source.read_bytes() + b"\nmanual edit")
    result = send(c, "-preview")
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "chapter_fact_authority_changed"
    view = c["client"].get(c["path"] + "/publication-input", params={"review_id": c["review"]["id"]})
    assert view.status_code == 200, view.text
    assert view.json()["final_draft"] == c["draft"].text


def test_api_blocks_missing_ownership_and_retained_recovery_marker(api_case):
    c = api_case
    recovery = c["root"] / ".novelagent/runtime/reviewed/p/registry/recovery_required"
    recovery.mkdir(parents=True, exist_ok=True)
    marker = recovery / "corrupt"
    marker.write_bytes(b"retain me")
    for suffix in ("-preview", "-status", ""):
        response = send(c, suffix, commit=suffix == "")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "chapter_commit_recovery_required"
    assert marker.read_bytes() == b"retain me" and infer_next_chapter(c["root"]) == 2
    c["app"].state.chapter_publication.machine_store = c["root"].parent / "no-owner"
    assert send(c, "", commit=True).json()["error"]["code"] == "book_ownership_invalid"


def test_api_rejects_forged_results_bad_cas_and_missing_session(api_case):
    c = api_case
    for suffix in ("-preview", "", "-status"):
        assert send(c, suffix, body={**c["body"], "quality": {"passed": True}}).status_code == 422
        assert send(c, suffix, headers={}).status_code == 422
    assert send(c, "", body={**c["body"], "author_confirmed": False}).status_code == 422
    assert c["client"].post(f"/api/projects/{c['project'].id}/publication-recovery",
        json={"author_confirmed": 1}).status_code == 422
    assert send(c, "-preview", body={**c["body"], "expected_draft_revision": True}).status_code == 400
    c["client"].cookies.clear()
    assert send(c, "", commit=True).status_code == 403
    assert infer_next_chapter(c["root"]) == 2 and c["calls"] == [1]


def test_explicit_app_card_dispatch_keeps_ordinary_registry_gate(api_case):
    from core.story_project.identity import ProjectIdentityError
    from workbench.projects import ProjectRegistry
    c = api_case
    response = c["client"].get(c["path"] + "/card")
    assert response.status_code == 200, response.text
    ordinary = ProjectRegistry(c["db"], allowed_workspaces=[c["root"].parent])
    with pytest.raises(ProjectIdentityError):
        ordinary.get(c["project"].id)
    assert infer_next_chapter(c["root"]) == 2 and c["calls"] == [1]


def test_unregistered_damaged_journal_cannot_be_skipped_by_new_key(api_case):
    c = api_case
    service = c["app"].state.chapter_publication
    def crash(event, position, path):
        if event == "after_journal_publish":
            raise PowerLoss()
    service.persistence_fault_injector = crash
    with pytest.raises(PowerLoss):
        service.commit(**c["route"])
    service.persistence_fault_injector = None
    tx_root = c["root"] / ".novelagent/runtime/reviewed/p"
    assert not list((tx_root / "registry/pending").glob("*.json"))
    manifest = next(path / "manifest.json" for path in (tx_root / "journals").iterdir() if path.name.startswith("commit_"))
    manifest.write_bytes(b"damaged before pending registration")
    for suffix in ("-preview", "-status", ""):
        response = send(c, suffix, commit=suffix == "", headers={"Idempotency-Key": "new-after-journal-gap"})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "chapter_commit_recovery_required"
    assert manifest.read_bytes() == b"damaged before pending registration"
    assert infer_next_chapter(c["root"]) == 2 and c["calls"] == [1]
