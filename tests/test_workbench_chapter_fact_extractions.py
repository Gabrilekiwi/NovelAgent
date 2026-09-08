from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

from api.contracts import MODEL_ENDPOINT_UNKNOWN, ModelResponse
from core.engine.chapter_fact_extraction import ChapterFactExtractionExecutor
from core.execution_provenance import build_execution_provenance
from core.model_call_runtime import ModelCallRuntimeContext, ProviderCallUncertainError
from core.model_calls import ModelCallStore
from tests.test_workbench_chapter_candidates import (
    DeterministicExecutorFactory,
    PROSE,
    REPOSITORY,
    _confirm_chapter,
    _formal_inventory,
    _generate_candidate,
    _import_project,
    _strict_book,
    _unlock,
)
from tests.test_workbench_chapter_facts import _fact_proposals
from workbench.app import create_app


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)


def _fact_provenance():
    return build_execution_provenance(
        code_bundle_hash="4" * 64,
        code_file_count=1,
        git_commit=None,
        git_dirty=True,
        prompt_hashes={"fact-extraction-contract": "5" * 64},
        schema_hashes={"chapter_fact_proposal": "6" * 64},
        dependency_versions={},
        provider="test",
        model="deterministic-fact-provider",
        config={"test_mode": True},
        feature_flags={
            "chapter_fact_extraction": True,
            "formal_state_write": False,
        },
    )


def _raw_fact_proposal() -> str:
    proposals = []
    for item in _fact_proposals():
        proposals.append(
            {
                "local_key": item["local_key"],
                "kind": item["kind"],
                "summary": item["summary"],
                "evidence_quotes": copy.deepcopy(item["evidence_quotes"]),
                "proposal": copy.deepcopy(item["proposal"]),
            }
        )
    return json.dumps(
        {"schema_version": "1.0", "facts": proposals},
        ensure_ascii=False,
    )


class FactExtractionExecutorFactory:
    """Durable fake Provider boundary; it never performs network I/O."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.uncertain = False
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []
        self.before_return: Callable[[], None] | None = None

    def __call__(self, root: Path) -> ChapterFactExtractionExecutor:
        project_root = Path(root)

        def provider(
            messages: list[dict[str, str]],
            call_id: str,
            evidence_root: Path,
        ) -> str:
            self.calls += 1
            self.messages.append(copy.deepcopy(messages))
            runtime = ModelCallRuntimeContext(
                ModelCallStore(evidence_root, clock=lambda: NOW),
                clock=lambda: NOW,
            )

            def operation():
                if self.uncertain:
                    raise ProviderCallUncertainError(
                        call_id=call_id,
                        attempt_id=f"{call_id}-a1",
                    )
                if self.before_return is not None:
                    callback = self.before_return
                    self.before_return = None
                    callback()
                return ModelResponse(
                    self.raw,
                    usage={
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 30,
                    },
                    finish_reason="stop",
                    actual_model="deterministic-fact-provider",
                    endpoint_type=MODEL_ENDPOINT_UNKNOWN,
                )

            response = runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="test",
                model="deterministic-fact-provider",
                stage="chapter_fact_extraction",
                endpoint_type=MODEL_ENDPOINT_UNKNOWN,
                request={
                    "messages_sha256": hashlib.sha256(
                        json.dumps(
                            messages,
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()
                },
                max_output_tokens=1000,
                operation=operation,
            )
            return response.text

        return ChapterFactExtractionExecutor(
            story_project_root=project_root,
            provider=provider,
            execution_kind="model",
            repository_root=REPOSITORY,
            provenance_factory=_fact_provenance,
            clock=lambda: NOW,
        )


def _setup(tmp_path: Path, *, fact_raw: str | None = None):
    root = _strict_book(tmp_path)
    candidate_factory = DeterministicExecutorFactory(lambda _root, _input: PROSE)
    fact_factory = FactExtractionExecutorFactory(
        _raw_fact_proposal() if fact_raw is None else fact_raw
    )
    app = create_app(
        database_path=tmp_path / "workbench.db",
        runtime_root=tmp_path / "job-runtime",
        allowed_workspaces=[root.parent],
        repo_root=REPOSITORY,
        start_worker=False,
        auto_import_active=False,
        chapter_candidate_executor_factory=candidate_factory,
        chapter_fact_extraction_executor_factory=fact_factory,
    )
    project = _import_project(app, root)
    return root, candidate_factory, fact_factory, app, project


def _prepare_draft(client: TestClient, project_id: str, *, key: str) -> dict:
    confirmation = _confirm_chapter(client, project_id)
    response = _generate_candidate(client, project_id, confirmation, key=key)
    assert response.status_code == 200, response.text
    return response.json()["candidate"]["draft"]


def _start_extraction(
    client: TestClient,
    draft: dict,
    *,
    key: str,
    confirmed: bool = True,
):
    return client.post(
        f"/api/candidate-drafts/{draft['id']}/fact-extractions",
        headers={
            "origin": "http://testserver",
            "Idempotency-Key": key,
        },
        json={
            "expected_draft_revision": draft["revision"],
            "expected_draft_sha256": draft["sha256"],
            "author_confirmed": confirmed,
        },
    )


def test_extraction_creates_model_origin_review_without_formal_writes_or_cohort(
    tmp_path: Path,
) -> None:
    root, candidate_factory, fact_factory, app, project = _setup(tmp_path)
    formal_before = _formal_inventory(root)

    with TestClient(app) as client:
        _unlock(client)
        draft = _prepare_draft(
            client,
            project["id"],
            key="fact-extraction-candidate-001",
        )

        denied = _start_extraction(
            client,
            draft,
            key="fact-extraction-001",
            confirmed=False,
        )
        assert denied.status_code == 409, denied.text
        assert fact_factory.calls == 0

        response = _start_extraction(
            client,
            draft,
            key="fact-extraction-001",
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        extraction = payload["fact_extraction"]
        review = extraction["fact_review"]
        assert extraction["status"] == "completed"
        assert extraction["source_state"] == "current"
        assert extraction["proposal"]["fact_count"] == 2
        assert review["state"] == "editing"
        assert review["source"]["extraction_id"] == extraction["extraction_id"]
        assert review["source"]["extraction_record_hash"] == extraction["record_hash"]
        assert {item["review"]["origin"] for item in review["facts"]} == {"model"}
        assert {item["review"]["decision"] for item in review["facts"]} == {
            "pending"
        }
        event = review["facts"][0]["proposal"]
        assert [item["entity_id"] for item in event["onsite_participants"]] == [
            "lin_che",
            "zhou_lan",
        ]
        assert event["remote_participants"] == []
        assert event["transported_items"] == []
        assert event["related_entities"] == []
        assert "尚未写入权威状态" in payload["message"]

        listed = client.get(
            f"/api/candidate-drafts/{draft['id']}/fact-extractions"
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["proposal"]["fact_count"] == 2
        assert "facts" not in listed.json()["items"][0]["proposal"]

        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_fact_reviews"
            ).fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

    assert candidate_factory.generator_calls == 1
    assert fact_factory.calls == 1
    assert len(fact_factory.messages) == 1
    assert "作者当前工作台正文" in fact_factory.messages[0][1]["content"]
    assert "Authoritative State" not in fact_factory.messages[0][1]["content"]
    assert _formal_inventory(root) == formal_before


def test_invalid_extraction_output_is_terminal_and_not_retried(
    tmp_path: Path,
) -> None:
    root, _candidate_factory, fact_factory, app, project = _setup(
        tmp_path,
        fact_raw="not-json",
    )
    formal_before = _formal_inventory(root)

    with TestClient(app) as client:
        _unlock(client)
        draft = _prepare_draft(
            client,
            project["id"],
            key="fact-extraction-candidate-invalid",
        )
        first = _start_extraction(client, draft, key="fact-extraction-invalid")
        assert first.status_code == 200, first.text
        extraction = first.json()["fact_extraction"]
        assert extraction["status"] == "failed"
        assert extraction["error"]["code"] == "chapter_fact_candidate_json_invalid"
        assert extraction["fact_review"] is None

        repeated = _start_extraction(client, draft, key="fact-extraction-invalid")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["fact_extraction"]["extraction_id"] == extraction[
            "extraction_id"
        ]
        assert repeated.json()["fact_extraction"]["status"] == "failed"

    assert fact_factory.calls == 1
    assert _formal_inventory(root) == formal_before


def test_provider_uncertainty_requires_exact_author_abandonment_before_new_call(
    tmp_path: Path,
) -> None:
    root, _candidate_factory, fact_factory, app, project = _setup(tmp_path)
    formal_before = _formal_inventory(root)
    fact_factory.uncertain = True

    with TestClient(app) as client:
        _unlock(client)
        draft = _prepare_draft(
            client,
            project["id"],
            key="fact-extraction-candidate-uncertain",
        )
        response = _start_extraction(client, draft, key="fact-extraction-uncertain")
        assert response.status_code == 200, response.text
        extraction = response.json()["fact_extraction"]
        assert extraction["status"] == "provider_uncertain"
        assert extraction["uncertainty"]["count"] == 1
        assert extraction["fact_review"] is None
        assert fact_factory.calls == 1

        blocked = _start_extraction(client, draft, key="fact-extraction-blocked")
        assert blocked.status_code == 409, blocked.text
        assert fact_factory.calls == 1

        abandon_path = (
            f"/api/candidate-drafts/{draft['id']}/fact-extractions/"
            f"{extraction['extraction_id']}/abandon-provider"
        )
        body = {
            "expected_intent_hash": extraction["intent_hash"],
            "expected_record_hash": extraction["record_hash"],
            "expected_uncertainty_sha256": extraction["uncertainty"]["sha256"],
            "author_confirmed": False,
        }
        denied = client.post(
            abandon_path,
            headers={"origin": "http://testserver"},
            json=body,
        )
        assert denied.status_code == 409, denied.text

        body["author_confirmed"] = True
        abandoned = client.post(
            abandon_path,
            headers={"origin": "http://testserver"},
            json=body,
        )
        assert abandoned.status_code == 200, abandoned.text
        assert abandoned.json()["fact_extraction"]["status"] == "abandoned"

        fact_factory.uncertain = False
        completed = _start_extraction(client, draft, key="fact-extraction-after-abandon")
        assert completed.status_code == 200, completed.text
        assert completed.json()["fact_extraction"]["status"] == "completed"

    assert fact_factory.calls == 2
    assert _formal_inventory(root) == formal_before


def test_draft_change_during_extraction_keeps_proposal_read_only_and_stale(
    tmp_path: Path,
) -> None:
    root, _candidate_factory, fact_factory, app, project = _setup(tmp_path)
    formal_before = _formal_inventory(root)

    with TestClient(app) as client:
        _unlock(client)
        draft = _prepare_draft(
            client,
            project["id"],
            key="fact-extraction-candidate-stale",
        )

        def mutate_draft() -> None:
            app.state.chapter_candidates.update_editable_draft(
                draft["id"],
                text=PROSE + "控制室内的警报灯随即亮起。",
                expected_revision=draft["revision"],
            )

        fact_factory.before_return = mutate_draft
        response = _start_extraction(client, draft, key="fact-extraction-stale")
        assert response.status_code == 200, response.text
        extraction = response.json()["fact_extraction"]
        assert extraction["status"] == "completed"
        assert extraction["source_state"] == "stale"
        assert extraction["proposal"]["available"] is True
        assert extraction["fact_review"] is None
        assert extraction["error"]["code"] == "chapter_fact_extraction_stale"

        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_fact_reviews"
            ).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

    assert fact_factory.calls == 1
    assert _formal_inventory(root) == formal_before
