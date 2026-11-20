from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api.contracts import MODEL_ENDPOINT_UNKNOWN, ModelResponse
from core.engine.chapter_fact_extraction import (
    ChapterFactExtractionConflictError,
    ChapterFactExtractionExecutionError,
    ChapterFactExtractionExecutor,
    ChapterFactExtractionIntegrityError,
    ChapterFactExtractionStore,
)
from core.engine.chapter_fact_extraction_resolution import (
    fact_extraction_provider_uncertainty_sha256,
)
from core.engine.provider_safety import list_unresolved_provider_calls
from core.execution_provenance import build_execution_provenance
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import ModelCallRuntimeContext, ProviderCallUncertainError
from core.model_calls import ModelCallStore


NOW = datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc)
BOOK_ID = "book-fact-extraction-test"
CONTEXT_SHA = "c" * 64
IDENTITY_REVISION = "d" * 64
CANDIDATE_ID = "candidate_" + "a" * 40
CANDIDATE_INTENT_HASH = "b" * 64
DRAFT_ID = "candidate_draft_" + "e" * 32
PROSE = "林岚在北塔通过电台收到苏桥的指令，货箱正在前往南港。"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority() -> dict:
    return {
        "mode": "legacy",
        "authority_epoch": 0,
        "head_event_hash": None,
        "identity_revision": IDENTITY_REVISION,
    }


def _authority_view(prose: str = PROSE) -> dict:
    view = {
        "schema_version": "1.0",
        "view_kind": "chapter_fact_authority_view",
        "policy": "test",
        "chapter_index": 27,
        "chapter_prose_sha256": _sha(prose),
        "authority_binding": {
            "book_id": BOOK_ID,
            "mode": "legacy",
            "authority_epoch": 0,
            "head_event_hash": None,
            "context_sha256": CONTEXT_SHA,
            "authority_projection_sha256": None,
            "semantic_source_digest": "f" * 64,
            "projection_revision": None,
        },
        "entity_directory": [],
        "matched_entity_refs": [],
        "current_state_details": [],
        "selection": {
            "authority_source": "test",
            "directory_count": 0,
            "matched_count": 0,
            "details_complete": True,
            "prose_is_authority": False,
        },
    }
    view["view_sha256"] = canonical_json_hash(
        view,
        exclude_environment_fields=False,
    )
    return view


def _provenance():
    return build_execution_provenance(
        code_bundle_hash="1" * 64,
        code_file_count=1,
        git_commit=None,
        git_dirty=True,
        prompt_hashes={},
        schema_hashes={},
        dependency_versions={},
        provider="test",
        model="deterministic-fake",
        feature_flags={
            "chapter_fact_extraction": True,
            "formal_state_write": False,
        },
    )


def _raw_empty() -> str:
    return json.dumps({"schema_version": "1.0", "facts": []})


class DurableFakeProvider:
    def __init__(self, raw: str, *, uncertain: bool = False) -> None:
        self.raw = raw
        self.uncertain = uncertain
        self.calls = 0

    def __call__(
        self,
        messages: list[dict[str, str]],
        call_id: str,
        evidence_root: Path,
    ) -> str:
        self.calls += 1
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
            return ModelResponse(
                self.raw,
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                finish_reason="stop",
                actual_model="deterministic-fake",
                endpoint_type=MODEL_ENDPOINT_UNKNOWN,
            )

        response = runtime.execute_attempt(
            call_id=call_id,
            attempt_number=1,
            provider="test",
            model="deterministic-fake",
            stage="chapter_fact_extraction",
            endpoint_type=MODEL_ENDPOINT_UNKNOWN,
            request={"messages": messages},
            max_output_tokens=1000,
            operation=operation,
        )
        return response.text


def _executor(
    root: Path,
    provider,
    *,
    fault_injector=None,
    execution_kind: str = "model",
) -> ChapterFactExtractionExecutor:
    return ChapterFactExtractionExecutor(
        story_project_root=root,
        provider=provider,
        execution_kind=execution_kind,
        provenance_factory=_provenance,
        clock=lambda: NOW,
        fault_injector=fault_injector,
    )


def _execute(
    executor: ChapterFactExtractionExecutor,
    *,
    key: str = "extract-27-a",
    prose: str = PROSE,
) -> dict:
    return executor.execute(
        book_id=BOOK_ID,
        chapter_index=27,
        candidate_binding={
            "candidate_id": CANDIDATE_ID,
            "intent_hash": CANDIDATE_INTENT_HASH,
        },
        draft_binding={
            "draft_id": DRAFT_ID,
            "revision": 3,
            "sha256": _sha(prose),
        },
        context_sha256=CONTEXT_SHA,
        authority_binding=_authority(),
        chapter_text=prose,
        authority_view=_authority_view(prose),
        idempotency_key=key,
        author_confirmed=True,
    )


def test_completed_extraction_is_only_an_append_only_review_proposal(tmp_path: Path) -> None:
    formal = tmp_path / "chapter-27.md"
    authority = tmp_path / "authority.json"
    formal.write_text("正式正文", encoding="utf-8")
    authority.write_text('{"stable":true}', encoding="utf-8")
    before = (formal.read_bytes(), authority.read_bytes())
    provider = DurableFakeProvider(_raw_empty())

    record = _execute(_executor(tmp_path, provider))

    assert record["status"] == "completed"
    assert record["proposal"]["fact_count"] == 0
    assert record["evidence"]["selected_response"] is not None
    assert provider.calls == 1
    loaded = ChapterFactExtractionStore(tmp_path).load(record["extraction_id"])
    assert loaded["proposal"]["facts"] == []
    assert (formal.read_bytes(), authority.read_bytes()) == before
    assert not list(tmp_path.rglob("*cohort*"))


def test_author_confirmation_is_required_before_intent_or_provider(tmp_path: Path) -> None:
    provider = DurableFakeProvider(_raw_empty())
    executor = _executor(tmp_path, provider)

    with pytest.raises(ChapterFactExtractionConflictError):
        executor.execute(
            book_id=BOOK_ID,
            chapter_index=27,
            candidate_binding={
                "candidate_id": CANDIDATE_ID,
                "intent_hash": CANDIDATE_INTENT_HASH,
            },
            draft_binding={
                "draft_id": DRAFT_ID,
                "revision": 3,
                "sha256": _sha(PROSE),
            },
            context_sha256=CONTEXT_SHA,
            authority_binding=_authority(),
            chapter_text=PROSE,
            authority_view=_authority_view(),
            idempotency_key="not-confirmed",
            author_confirmed=False,
        )

    assert provider.calls == 0
    assert not (tmp_path / ".novelagent" / "runtime" / "fact_extractions").exists()


def test_existing_unfinished_intent_requires_explicit_resume(tmp_path: Path) -> None:
    provider = DurableFakeProvider(_raw_empty())

    def crash(event: str, _extraction_id: str) -> None:
        if event == "after_intent_before_provider":
            raise KeyboardInterrupt("simulated process loss")

    with pytest.raises(KeyboardInterrupt):
        _execute(_executor(tmp_path, provider, fault_injector=crash))
    assert provider.calls == 0

    with pytest.raises(ChapterFactExtractionConflictError) as raised:
        _execute(_executor(tmp_path, provider))
    assert raised.value.code == "chapter_fact_extraction_recovery_required"
    extraction_id = next(
        (tmp_path / ".novelagent" / "runtime" / "fact_extractions").iterdir()
    ).name
    record = _executor(tmp_path, provider).resume(extraction_id)
    assert record["status"] == "completed"
    assert provider.calls == 1


def test_receipt_crash_resume_does_not_repeat_provider_call(tmp_path: Path) -> None:
    provider = DurableFakeProvider(_raw_empty())

    def crash(event: str, _extraction_id: str) -> None:
        if event == "after_provider_before_parse":
            raise KeyboardInterrupt("simulated crash after settled Receipt")

    with pytest.raises(KeyboardInterrupt):
        _execute(_executor(tmp_path, provider, fault_injector=crash))
    assert provider.calls == 1
    extraction_id = next(
        (tmp_path / ".novelagent" / "runtime" / "fact_extractions").iterdir()
    ).name

    record = _executor(tmp_path, provider).resume(extraction_id)

    assert record["status"] == "completed"
    assert provider.calls == 1


def test_invalid_provider_json_fails_once_without_repair_retry(tmp_path: Path) -> None:
    provider = DurableFakeProvider("not-json")

    with pytest.raises(ChapterFactExtractionExecutionError) as raised:
        _execute(_executor(tmp_path, provider))

    assert raised.value.status == "failed"
    assert raised.value.record["error"]["code"] == "chapter_fact_candidate_json_invalid"
    assert raised.value.record["evidence"]["selected_response"] is not None
    assert provider.calls == 1
    assert _execute(_executor(tmp_path, provider))["status"] == "failed"
    assert provider.calls == 1


def test_provider_uncertainty_stops_and_requires_exact_author_abandonment(
    tmp_path: Path,
) -> None:
    provider = DurableFakeProvider(_raw_empty(), uncertain=True)
    with pytest.raises(ChapterFactExtractionExecutionError) as raised:
        _execute(_executor(tmp_path, provider))
    record = raised.value.record
    assert record["status"] == "provider_uncertain"
    assert len(list_unresolved_provider_calls(tmp_path / ".novelagent" / "runtime" / "runs")) == 1
    intent = ChapterFactExtractionStore(tmp_path).load_intent(record["extraction_id"])
    uncertainty = fact_extraction_provider_uncertainty_sha256(
        intent["execution"]["execution_id"],
        record["evidence"]["unresolved_calls"],
    )

    with pytest.raises(ChapterFactExtractionConflictError):
        _executor(tmp_path, provider).abandon_provider_uncertainty(
            record["extraction_id"],
            expected_intent_hash=intent["intent_hash"],
            expected_record_hash=record["record_hash"],
            expected_uncertainty_sha256=uncertainty,
            author_confirmed=False,
        )
    resolution = _executor(tmp_path, provider).abandon_provider_uncertainty(
        record["extraction_id"],
        expected_intent_hash=intent["intent_hash"],
        expected_record_hash=record["record_hash"],
        expected_uncertainty_sha256=uncertainty,
        author_confirmed=True,
    )
    assert resolution["action"] == "abandon_uncertain_provider_result"
    assert list_unresolved_provider_calls(
        tmp_path / ".novelagent" / "runtime" / "runs"
    ) == []
    with pytest.raises(ChapterFactExtractionConflictError):
        _executor(tmp_path, provider).resume(record["extraction_id"])
    assert provider.calls == 1


def test_cancel_never_resolves_an_unsettled_provider_attempt(tmp_path: Path) -> None:
    provider = DurableFakeProvider(_raw_empty(), uncertain=True)
    with pytest.raises(ChapterFactExtractionExecutionError) as raised:
        _execute(_executor(tmp_path, provider))

    # Existing terminal uncertainty remains visible; cancel is idempotent read,
    # not a false Receipt or an abandonment marker.
    record = _executor(tmp_path, provider).cancel(raised.value.extraction_id)
    assert record["status"] == "provider_uncertain"
    assert list_unresolved_provider_calls(
        tmp_path / ".novelagent" / "runtime" / "runs"
    )


def test_tampered_proposal_fails_closed(tmp_path: Path) -> None:
    record = _execute(_executor(tmp_path, DurableFakeProvider(_raw_empty())))
    path = (
        tmp_path
        / ".novelagent"
        / "runtime"
        / "fact_extractions"
        / record["extraction_id"]
        / "proposal.json"
    )
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft_sha256"] = "0" * 64
    path.write_text(json.dumps(proposal), encoding="utf-8")

    with pytest.raises(ChapterFactExtractionIntegrityError):
        ChapterFactExtractionStore(tmp_path).load_record(record["extraction_id"])


def test_same_idempotency_key_cannot_bind_changed_draft(tmp_path: Path) -> None:
    provider = DurableFakeProvider(_raw_empty())
    executor = _executor(tmp_path, provider)
    _execute(executor, key="same-key")

    with pytest.raises(ChapterFactExtractionConflictError):
        _execute(executor, key="same-key", prose=PROSE + "新增一句。")
    assert provider.calls == 1


def test_deterministic_test_provider_cannot_smuggle_model_evidence(tmp_path: Path) -> None:
    provider = DurableFakeProvider(_raw_empty())
    with pytest.raises(ChapterFactExtractionIntegrityError) as raised:
        _execute(_executor(tmp_path, provider, execution_kind="deterministic"))
    assert "deterministic fact extraction cannot contain model-call evidence" in str(
        raised.value
    )
