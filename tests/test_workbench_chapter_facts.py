from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from tests.test_workbench_chapter_candidates import (
    DeterministicExecutorFactory,
    PROSE,
    _confirm_chapter,
    _create_app,
    _formal_inventory,
    _generate_candidate,
    _import_project,
    _strict_book,
    _unlock,
)
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_facts import (
    ChapterFactReviewConflict,
    ChapterFactReviewStore,
)
from workbench.database import WorkbenchDatabase


def _fact_proposals() -> list[dict]:
    return [
        {
            "local_key": "control_room_entry",
            "kind": "event",
            "summary": "林澈与周岚进入控制室。",
            "evidence_quotes": ["沿北侧入口进入控制室"],
            "proposal": {
                "event_type": "location_entry",
                "status": "completed",
                "occurrence_location": "control_room",
                "onsite_participants": [
                    {"entity_id": "lin_che", "role": "actor"},
                    {"entity_id": "zhou_lan", "role": "actor"},
                ],
                "remote_participants": [],
                "transported_items": [],
                "related_entities": [],
            },
        },
        {
            "local_key": "lin_che_location",
            "kind": "location",
            "summary": "林澈从北侧入口移动到控制室。",
            "evidence_quotes": ["林澈贴着闸门辨认回声"],
            "proposal": {
                "entity_id": "lin_che",
                "from_location": "north_entrance",
                "to_location": "control_room",
                "source_event_key": "control_room_entry",
            },
        },
    ]


def _setup_completed_candidate(tmp_path: Path):
    root = _strict_book(tmp_path)
    factory = DeterministicExecutorFactory(lambda _root, _input: PROSE)
    app = _create_app(tmp_path=tmp_path, root=root, factory=factory)
    project = _import_project(app, root)
    return root, factory, app, project


def _create_review(client: TestClient, draft: dict, *, proposals=None):
    return client.post(
        f"/api/candidate-drafts/{draft['id']}/fact-reviews",
        headers={"origin": "http://testserver"},
        json={
            "expected_draft_revision": draft["revision"],
            "expected_draft_sha256": draft["sha256"],
            "proposals": _fact_proposals() if proposals is None else proposals,
            "author_confirmed": True,
        },
    )


def _decide_all(facts: list[dict]) -> list[dict]:
    decided = copy.deepcopy(facts)
    decided[0]["review"].update(
        {"decision": "accepted", "reason": "正文证据明确"}
    )
    decided[1]["review"].update(
        {"decision": "rejected", "reason": "author_deleted"}
    )
    return decided


def test_fact_review_api_requires_explicit_decisions_and_never_writes_formal_state(
    tmp_path: Path,
) -> None:
    root, factory, app, project = _setup_completed_candidate(tmp_path)
    project_id = project["id"]
    formal_before = _formal_inventory(root)

    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project_id)
        candidate_response = _generate_candidate(
            client,
            project_id,
            confirmation,
            key="fact-review-candidate-001",
        )
        assert candidate_response.status_code == 200, candidate_response.text
        draft = candidate_response.json()["candidate"]["draft"]

        unconfirmed = client.post(
            f"/api/candidate-drafts/{draft['id']}/fact-reviews",
            headers={"origin": "http://testserver"},
            json={
                "expected_draft_revision": draft["revision"],
                "expected_draft_sha256": draft["sha256"],
                "proposals": _fact_proposals(),
                "author_confirmed": False,
            },
        )
        assert unconfirmed.status_code == 409, unconfirmed.text
        assert unconfirmed.json()["error"]["code"] == "chapter_fact_review_conflict"

        created_response = _create_review(client, draft)
        assert created_response.status_code == 201, created_response.text
        review = created_response.json()["fact_review"]
        assert review["state"] == "editing"
        assert review["stored_state"] == "editing"
        assert review["source"] == {
            "draft_revision": 1,
            "draft_sha256": draft["sha256"],
            "candidate_intent_hash": review["source"]["candidate_intent_hash"],
            "context_sha256": review["source"]["context_sha256"],
        }
        assert review["fact_counts"] == {
            "total": 2,
            "pending": 2,
            "accepted": 0,
            "rejected": 0,
        }
        event = review["facts"][0]
        assert event["proposal"]["occurrence_location"] == "control_room"
        assert [item["entity_id"] for item in event["proposal"]["onsite_participants"]] == [
            "lin_che",
            "zhou_lan",
        ]
        assert event["proposal"]["remote_participants"] == []
        assert event["proposal"]["transported_items"] == []
        assert event["proposal"]["related_entities"] == []
        assert "source_text" not in review
        assert str(root) not in json.dumps(created_response.json(), ensure_ascii=False)

        pending_confirmation = client.post(
            f"/api/fact-reviews/{review['id']}/confirm",
            headers={"origin": "http://testserver"},
            json={
                "expected_revision": review["revision"],
                "expected_facts_sha256": review["facts_sha256"],
                "expected_draft_revision": draft["revision"],
                "expected_draft_sha256": draft["sha256"],
                "author_confirmed": True,
            },
        )
        assert pending_confirmation.status_code == 409, pending_confirmation.text
        assert pending_confirmation.json()["error"]["code"] == "chapter_fact_review_incomplete"

        decided_response = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": _decide_all(review["facts"]),
                "expected_revision": review["revision"],
            },
        )
        assert decided_response.status_code == 200, decided_response.text
        decided = decided_response.json()["fact_review"]
        assert decided["revision"] == 2
        assert decided["fact_counts"] == {
            "total": 2,
            "pending": 0,
            "accepted": 1,
            "rejected": 1,
        }
        assert decided["facts"][1]["review"]["reason"] == "author_deleted"

        confirmed_response = client.post(
            f"/api/fact-reviews/{review['id']}/confirm",
            headers={"origin": "http://testserver"},
            json={
                "expected_revision": decided["revision"],
                "expected_facts_sha256": decided["facts_sha256"],
                "expected_draft_revision": draft["revision"],
                "expected_draft_sha256": draft["sha256"],
                "author_confirmed": True,
            },
        )
        assert confirmed_response.status_code == 200, confirmed_response.text
        confirmed = confirmed_response.json()["fact_review"]
        assert confirmed["state"] == "confirmed"
        assert confirmed["stored_state"] == "confirmed"
        assert confirmed["confirmation_sha256"]

        listed_response = client.get(
            f"/api/candidate-drafts/{draft['id']}/fact-reviews"
        )
        assert listed_response.status_code == 200, listed_response.text
        listed = listed_response.json()["items"]
        assert len(listed) == 1
        assert "facts" not in listed[0]
        detail_response = client.get(f"/api/fact-reviews/{review['id']}")
        assert detail_response.status_code == 200, detail_response.text
        assert detail_response.json()["fact_review"]["facts"] == confirmed["facts"]

        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_fact_reviews"
            ).fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

        assert factory.generator_calls == 1

    assert _formal_inventory(root) == formal_before


def test_fact_review_rejects_physical_deletion_and_stale_cas(tmp_path: Path) -> None:
    root, _factory, app, project = _setup_completed_candidate(tmp_path)
    formal_before = _formal_inventory(root)
    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-002",
        ).json()["candidate"]
        draft = candidate["draft"]
        review_response = _create_review(client, draft)
        assert review_response.status_code == 201, review_response.text
        review = review_response.json()["fact_review"]

        removed_card = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": [review["facts"][0]],
                "expected_revision": review["revision"],
            },
        )
        assert removed_card.status_code == 409, removed_card.text
        assert removed_card.json()["error"]["code"] == "chapter_fact_review_conflict"

        decided_response = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": _decide_all(review["facts"]),
                "expected_revision": review["revision"],
            },
        )
        assert decided_response.status_code == 200, decided_response.text
        decided = decided_response.json()["fact_review"]

        stale_write = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": review["facts"],
                "expected_revision": review["revision"],
            },
        )
        assert stale_write.status_code == 409, stale_write.text
        assert stale_write.json()["error"]["code"] == "chapter_fact_review_conflict"
        current = client.get(f"/api/fact-reviews/{review['id']}").json()["fact_review"]
        assert current["revision"] == decided["revision"]
        assert current["facts"] == decided["facts"]

    assert _formal_inventory(root) == formal_before


def test_fact_review_becomes_readable_stale_evidence_after_draft_edit(
    tmp_path: Path,
) -> None:
    root, _factory, app, project = _setup_completed_candidate(tmp_path)
    formal_before = _formal_inventory(root)
    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-003",
        ).json()["candidate"]
        draft = candidate["draft"]
        review_response = _create_review(client, draft)
        assert review_response.status_code == 201, review_response.text
        review = review_response.json()["fact_review"]

        edited_text = PROSE + "\n\n作者在事实审核前补充了一句。"
        edited_response = client.put(
            f"/api/candidate-drafts/{draft['id']}",
            headers={"origin": "http://testserver"},
            json={"text": edited_text, "expected_revision": draft["revision"]},
        )
        assert edited_response.status_code == 200, edited_response.text

        stale_response = client.get(f"/api/fact-reviews/{review['id']}")
        assert stale_response.status_code == 200, stale_response.text
        stale = stale_response.json()["fact_review"]
        assert stale["state"] == "stale"
        assert stale["stored_state"] == "editing"
        assert stale["stale_reason"] == "candidate_draft_changed"
        assert stale["facts"] == review["facts"]

        blocked_update = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": _decide_all(review["facts"]),
                "expected_revision": review["revision"],
            },
        )
        assert blocked_update.status_code == 409, blocked_update.text
        assert blocked_update.json()["error"]["code"] == "chapter_fact_source_draft_changed"

        new_draft = edited_response.json()["draft"]
        new_review_response = _create_review(client, new_draft)
        assert new_review_response.status_code == 201, new_review_response.text
        new_review = new_review_response.json()["fact_review"]
        assert new_review["id"] != review["id"]
        listed = client.get(
            f"/api/candidate-drafts/{draft['id']}/fact-reviews"
        ).json()["items"]
        assert len(listed) == 2
        assert {item["state"] for item in listed} == {"editing", "stale"}

    assert _formal_inventory(root) == formal_before


def test_fact_review_db_tampering_fails_closed(tmp_path: Path) -> None:
    root, _factory, app, project = _setup_completed_candidate(tmp_path)
    formal_before = _formal_inventory(root)
    with TestClient(app, raise_server_exceptions=False) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-004",
        ).json()["candidate"]
        review_response = _create_review(client, candidate["draft"])
        assert review_response.status_code == 201, review_response.text
        review = review_response.json()["fact_review"]

        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE chapter_fact_reviews SET facts_json = '[]' WHERE id = ?",
                (review["id"],),
            )

        corrupted = client.get(f"/api/fact-reviews/{review['id']}")
        assert corrupted.status_code == 500, corrupted.text
        assert corrupted.json()["error"]["code"] == "chapter_fact_review_integrity_error"

    assert _formal_inventory(root) == formal_before


def test_fact_review_stales_when_trusted_story_source_changes(tmp_path: Path) -> None:
    root, _factory, app, project = _setup_completed_candidate(tmp_path)
    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-005",
        ).json()["candidate"]
        draft = candidate["draft"]
        review_response = _create_review(client, draft)
        assert review_response.status_code == 201, review_response.text
        review = review_response.json()["fact_review"]

        outline = root / "大纲" / "细纲_第002章.md"
        outline.write_text(
            outline.read_text(encoding="utf-8") + "\n作者在审核期间改动了可信章纲。\n",
            encoding="utf-8",
        )

        stale_response = client.get(f"/api/fact-reviews/{review['id']}")
        assert stale_response.status_code == 200, stale_response.text
        stale = stale_response.json()["fact_review"]
        assert stale["state"] == "stale"
        assert stale["stored_state"] == "editing"
        assert stale["stale_reason"] == "authority_or_source_changed"

        blocked = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": _decide_all(review["facts"]),
                "expected_revision": review["revision"],
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "chapter_fact_authority_changed"


def test_fact_review_id_tampering_fails_closed(tmp_path: Path) -> None:
    _root, _factory, app, project = _setup_completed_candidate(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-006",
        ).json()["candidate"]
        review_response = _create_review(client, candidate["draft"])
        assert review_response.status_code == 201, review_response.text
        review = review_response.json()["fact_review"]
        forged_id = "fact_review_" + "f" * 32
        assert forged_id != review["id"]

        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE chapter_fact_reviews SET id = ? WHERE id = ?",
                (forged_id, review["id"]),
            )

        corrupted = client.get(f"/api/fact-reviews/{forged_id}")
        assert corrupted.status_code == 500, corrupted.text
        assert corrupted.json()["error"]["code"] == "chapter_fact_review_integrity_error"


def test_fact_review_numeric_tampering_fails_closed(tmp_path: Path) -> None:
    _root, _factory, app, project = _setup_completed_candidate(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-007",
        ).json()["candidate"]
        review = _create_review(client, candidate["draft"]).json()["fact_review"]

        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE chapter_fact_reviews SET chapter = 'corrupt' WHERE id = ?",
                (review["id"],),
            )

        corrupted = client.get(f"/api/fact-reviews/{review['id']}")
        assert corrupted.status_code == 500, corrupted.text
        assert corrupted.json()["error"]["code"] == "chapter_fact_review_integrity_error"


def test_store_rechecks_draft_binding_inside_review_write_transaction(
    tmp_path: Path,
) -> None:
    _root, _factory, app, project = _setup_completed_candidate(tmp_path)
    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-008",
        ).json()["candidate"]
        draft = candidate["draft"]
        review = _create_review(client, draft).json()["fact_review"]

        app.state.chapter_candidates.drafts.update(
            draft["id"],
            text=PROSE + "\n\n另一标签页已经保存了新正文。",
            expected_revision=draft["revision"],
        )
        with pytest.raises(ChapterFactReviewConflict) as raised:
            app.state.chapter_facts.reviews.update_facts(
                review["id"],
                facts=_decide_all(review["facts"]),
                expected_revision=review["revision"],
            )
        assert raised.value.code == "chapter_fact_source_draft_changed"
        stored = app.state.chapter_facts.reviews.get(review["id"])
        assert stored.revision == review["revision"]
        assert all(item["review"]["decision"] == "pending" for item in stored.facts)


def test_concurrent_identical_fact_confirmation_is_idempotent(tmp_path: Path) -> None:
    _root, _factory, app, project = _setup_completed_candidate(tmp_path)
    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        candidate = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="fact-review-candidate-009",
        ).json()["candidate"]
        draft = candidate["draft"]
        review = _create_review(client, draft).json()["fact_review"]
        decided = client.put(
            f"/api/fact-reviews/{review['id']}",
            headers={"origin": "http://testserver"},
            json={
                "facts": _decide_all(review["facts"]),
                "expected_revision": review["revision"],
            },
        ).json()["fact_review"]

        def confirm_once():
            return app.state.chapter_facts.reviews.confirm(
                review["id"],
                expected_revision=decided["revision"],
                expected_facts_sha256=decided["facts_sha256"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: confirm_once(), range(2)))
        assert [item.state for item in results] == ["confirmed", "confirmed"]
        assert results[0].confirmation_sha256 == results[1].confirmation_sha256


def test_fact_review_store_adds_record_version_columns_to_legacy_table(
    tmp_path: Path,
) -> None:
    database = WorkbenchDatabase(tmp_path / "legacy-workbench.db")
    database.initialize()
    CandidateDraftStore(database)
    with database.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE chapter_fact_reviews (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter INTEGER NOT NULL,
                candidate_id TEXT NOT NULL,
                candidate_intent_hash TEXT NOT NULL,
                draft_id TEXT NOT NULL,
                source_draft_revision INTEGER NOT NULL,
                source_draft_sha256 TEXT NOT NULL,
                source_text TEXT NOT NULL,
                context_sha256 TEXT NOT NULL,
                authority_json TEXT NOT NULL,
                authority_sha256 TEXT NOT NULL,
                facts_json TEXT NOT NULL,
                facts_sha256 TEXT NOT NULL,
                revision INTEGER NOT NULL,
                state TEXT NOT NULL,
                confirmation_sha256 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT
            );
            """
        )

    ChapterFactReviewStore(database)
    with database.connection() as connection:
        columns = {
            str(row["name"]): row
            for row in connection.execute(
                "PRAGMA table_info(chapter_fact_reviews)"
            ).fetchall()
        }
    assert columns["record_type"]["dflt_value"] == "'chapter_fact_review'"
    assert columns["schema_version"]["dflt_value"] == "'1.0'"
