from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from api.openai_client import chat_completion
from core.config import get_config
from core.engine.chapter_fact_candidate import (
    ChapterFactCandidateError,
    build_fact_extraction_messages,
    normalize_fact_review_items,
    parse_fact_proposal,
)
from core.engine.chapter_fact_extraction_resolution import (
    ChapterFactExtractionProviderResolutionConflict,
    build_fact_extraction_provider_resolution,
    fact_extraction_provider_uncertainty_sha256,
    load_fact_extraction_provider_resolution,
    publish_fact_extraction_provider_resolution,
)
from core.engine.persistence import atomic_create_json, atomic_create_text
from core.engine.provider_safety import (
    list_unresolved_provider_calls,
    provider_execution_lock,
)
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, assert_safe_local_tree
from core.execution_provenance import (
    ExecutionProvenance,
    capture_execution_provenance,
)
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import ModelCallRuntimeContext, ProviderCallUncertainError
from core.model_calls import ModelCallStore
from core.runtime_paths import RuntimePaths
from core.schema import SchemaValidationError, validate_schema


CHAPTER_FACT_EXTRACTION_SCHEMA_VERSION = "1.0"
CHAPTER_FACT_EXTRACTION_PURPOSE = "extract_reviewable_fact_candidates"

_EXTRACTION_ID = re.compile(r"^fact_extraction_[0-9a-f]{40}$")
_CANDIDATE_ID = re.compile(r"^candidate_[0-9a-f]{40}$")
_DRAFT_ID = re.compile(r"^candidate_draft_[0-9a-f]{32}$")
_EXECUTION_ID = re.compile(r"^execution_fx_[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_ALLOWED_EXECUTION_KINDS = frozenset({"model", "deterministic"})

FactProvider = Callable[[list[dict[str, str]], str, Path], str]


class ChapterFactExtractionError(RuntimeError):
    code = "chapter_fact_extraction_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class ChapterFactExtractionConflictError(ChapterFactExtractionError):
    code = "chapter_fact_extraction_conflict"


class ChapterFactExtractionIntegrityError(ChapterFactExtractionError):
    code = "chapter_fact_extraction_integrity_error"


class ChapterFactExtractionExecutionError(ChapterFactExtractionError):
    code = "chapter_fact_extraction_failed"

    def __init__(
        self,
        *,
        extraction_id: str,
        status: str,
        record: Mapping[str, Any],
        cause: BaseException,
    ) -> None:
        self.extraction_id = extraction_id
        self.status = status
        self.record = copy.deepcopy(dict(record))
        self.cause = cause
        super().__init__(
            f"fact extraction {extraction_id} ended as {status}: "
            f"{type(cause).__name__}: {cause}",
            code=self.code,
        )


class ChapterFactExtractionStore:
    """Append-only evidence for prose-to-fact proposals.

    The store is deliberately outside formal chapter and authority locations.
    A completed proposal is still only input to an author-owned review card.
    """

    def __init__(self, story_project_root: str | Path) -> None:
        self.story_project_root = Path(story_project_root).resolve()
        if not self.story_project_root.is_dir():
            raise ChapterFactExtractionIntegrityError(
                f"StoryProject root is not a directory: {self.story_project_root}"
            )
        self.paths = RuntimePaths.for_story_project(self.story_project_root)
        self.root = self.paths.runtime_dir / "fact_extractions"
        assert_safe_local_tree(self.root)

    def extraction_dir(self, extraction_id: str) -> Path:
        extraction_id = _validate_extraction_id(extraction_id)
        path = self.root / extraction_id
        assert_safe_local_tree(path)
        return path

    def intent_path(self, extraction_id: str) -> Path:
        return self.extraction_dir(extraction_id) / "intent.json"

    def record_path(self, extraction_id: str) -> Path:
        return self.extraction_dir(extraction_id) / "record.json"

    def proposal_path(self, extraction_id: str) -> Path:
        return self.extraction_dir(extraction_id) / "proposal.json"

    def has_intent(self, extraction_id: str) -> bool:
        return _safe_file_exists(self.intent_path(extraction_id))

    def has_record(self, extraction_id: str) -> bool:
        return _safe_file_exists(self.record_path(extraction_id))

    def claim(
        self,
        *,
        chapter_text: str,
        authority_view: Mapping[str, Any],
        intent: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        checked = _validate_extraction_intent(intent)
        extraction_id = checked["extraction_id"]
        source_path = self._runtime_artifact_path(checked["draft"]["artifact_ref"])
        view_path = self._runtime_artifact_path(
            checked["authority_view"]["artifact_ref"]
        )
        if source_path != self.extraction_dir(extraction_id) / "source.md":
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction source artifact reference is not canonical"
            )
        if view_path != self.extraction_dir(extraction_id) / "authority_view.json":
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction authority-view reference is not canonical"
            )

        prose = _validated_chapter_text(chapter_text)
        if _sha256_text(prose) != checked["draft"]["sha256"]:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction source text hash does not match its Intent"
            )
        if len(prose) != checked["draft"]["chars"]:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction source text length does not match its Intent"
            )
        view = _json_object(authority_view, "authority_view")
        view_text = _canonical_json(view)
        if canonical_json_hash(view, exclude_environment_fields=False) != checked[
            "authority_view"
        ]["sha256"]:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction authority view hash does not match its Intent"
            )
        if len(view_text) != checked["authority_view"]["chars"]:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction authority view length does not match its Intent"
            )
        _verify_authority_view_binding(
            view,
            book_id=checked["book_id"],
            draft_sha256=checked["draft"]["sha256"],
            context=checked["context"],
            authority=checked["authority"],
        )

        provenance_payload = validate_schema(
            copy.deepcopy(dict(provenance)),
            "execution_provenance.schema.json",
        )
        try:
            verified = ExecutionProvenance.from_dict(provenance_payload)
        except Exception as exc:
            raise ChapterFactExtractionIntegrityError(
                f"fact-extraction execution provenance is invalid: {exc}"
            ) from exc
        if verified.provenance_hash != checked["execution"]["provenance_hash"]:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction provenance hash does not match its Intent"
            )
        provenance_path = self._run_artifact_path(
            checked["execution"]["provenance_artifact_ref"]
        )

        # Publish immutable payloads before the executable Intent. A crash may
        # leave harmless orphan artifacts, never an unbound Provider request.
        _create_text_idempotent(source_path, prose)
        _create_json_idempotent(
            view_path,
            view,
            contract="chapter fact-extraction authority view",
        )
        _create_json_idempotent(
            provenance_path,
            provenance_payload,
            contract="chapter fact-extraction provenance",
        )
        return _create_json_idempotent(
            self.intent_path(extraction_id),
            checked,
            contract="chapter fact-extraction Intent",
        )

    def publish_proposal(
        self,
        extraction_id: str,
        facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        intent = self.load_intent(extraction_id)
        prose = self.load_source(extraction_id)
        try:
            normalized = normalize_fact_review_items(facts, chapter_text=prose)
        except ChapterFactCandidateError as exc:
            raise ChapterFactExtractionIntegrityError(str(exc)) from exc
        if normalized != facts:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction proposal is not in canonical normalized form"
            )
        proposal = {
            "schema_version": CHAPTER_FACT_EXTRACTION_SCHEMA_VERSION,
            "record_type": "chapter_fact_extraction_proposal",
            "extraction_id": extraction_id,
            "draft_sha256": intent["draft"]["sha256"],
            "facts": copy.deepcopy(normalized),
        }
        proposal["proposal_hash"] = _record_hash(proposal, "proposal_hash")
        persisted = _create_json_idempotent(
            self.proposal_path(extraction_id),
            proposal,
            contract="chapter fact-extraction proposal",
        )
        return {
            "artifact_ref": f"fact_extractions/{extraction_id}/proposal.json",
            "sha256": persisted["proposal_hash"],
            "fact_count": len(persisted["facts"]),
        }

    def publish_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        checked = _validate_extraction_record(record)
        intent = self.load_intent(checked["extraction_id"])
        _require_record_links_intent(checked, intent)
        self._verify_record_artifacts(checked, intent)
        return _create_json_idempotent(
            self.record_path(checked["extraction_id"]),
            checked,
            contract="chapter fact-extraction record",
        )

    def load_intent(self, extraction_id: str) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        payload = _load_json(
            self.intent_path(extraction_id),
            "chapter fact-extraction Intent",
        )
        checked = _validate_extraction_intent(payload)
        if checked["extraction_id"] != extraction_id:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction Intent identity does not match its directory"
            )
        self._verify_intent_artifacts(checked)
        return checked

    def load_record(self, extraction_id: str) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        payload = _load_json(
            self.record_path(extraction_id),
            "chapter fact-extraction record",
        )
        checked = _validate_extraction_record(payload)
        if checked["extraction_id"] != extraction_id:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction record identity does not match its directory"
            )
        intent = self.load_intent(extraction_id)
        _require_record_links_intent(checked, intent)
        self._verify_record_artifacts(checked, intent)
        return checked

    def load_source(self, extraction_id: str) -> str:
        intent = self.load_intent(extraction_id)
        return self._read_bound_text(intent["draft"], label="fact-extraction source")

    def load_authority_view(self, extraction_id: str) -> dict[str, Any]:
        intent = self.load_intent(extraction_id)
        return self._read_bound_json(
            intent["authority_view"],
            label="fact-extraction authority view",
        )

    def load_proposal(self, extraction_id: str) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        proposal = _load_json(
            self.proposal_path(extraction_id),
            "chapter fact-extraction proposal",
        )
        if set(proposal) != {
            "schema_version",
            "record_type",
            "extraction_id",
            "draft_sha256",
            "facts",
            "proposal_hash",
        }:
            raise ChapterFactExtractionIntegrityError(
                "chapter fact-extraction proposal fields changed"
            )
        if (
            proposal.get("schema_version") != CHAPTER_FACT_EXTRACTION_SCHEMA_VERSION
            or proposal.get("record_type") != "chapter_fact_extraction_proposal"
            or proposal.get("extraction_id") != extraction_id
            or proposal.get("proposal_hash")
            != _record_hash(proposal, "proposal_hash")
        ):
            raise ChapterFactExtractionIntegrityError(
                "chapter fact-extraction proposal hash or identity mismatch"
            )
        intent = self.load_intent(extraction_id)
        if proposal.get("draft_sha256") != intent["draft"]["sha256"]:
            raise ChapterFactExtractionIntegrityError(
                "chapter fact-extraction proposal draft binding mismatch"
            )
        raw_facts = proposal.get("facts")
        if not isinstance(raw_facts, list):
            raise ChapterFactExtractionIntegrityError(
                "chapter fact-extraction proposal facts must be an array"
            )
        try:
            normalized = normalize_fact_review_items(
                raw_facts,
                chapter_text=self.load_source(extraction_id),
            )
        except ChapterFactCandidateError as exc:
            raise ChapterFactExtractionIntegrityError(str(exc)) from exc
        if normalized != raw_facts:
            raise ChapterFactExtractionIntegrityError(
                "chapter fact-extraction proposal is not canonical"
            )
        return copy.deepcopy(proposal)

    def load(self, extraction_id: str) -> dict[str, Any]:
        intent = self.load_intent(extraction_id)
        record = self.load_record(extraction_id) if self.has_record(extraction_id) else None
        proposal = (
            self.load_proposal(extraction_id)
            if record is not None and record["proposal"]["artifact_ref"] is not None
            else None
        )
        return {"intent": intent, "record": record, "proposal": proposal}

    def list_for_draft(self, draft_id: str) -> list[dict[str, Any]]:
        draft_id = _validate_draft_id(draft_id)
        if not self.root.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("fact_extraction_*")):
            if not path.is_dir():
                raise ChapterFactExtractionIntegrityError(
                    f"fact-extraction storage entry must be a directory: {path}"
                )
            extraction_id = _validate_extraction_id(path.name)
            intent = self.load_intent(extraction_id)
            if intent["draft"]["draft_id"] == draft_id:
                results.append(self.load(extraction_id))
        return sorted(
            results,
            key=lambda item: (
                str(item["intent"]["created_at"]),
                str(item["intent"]["extraction_id"]),
            ),
        )

    def model_call_store(self, intent: Mapping[str, Any]) -> ModelCallStore:
        checked = _validate_extraction_intent(intent)
        path = self._run_artifact_path(checked["execution"]["model_calls_ref"])
        expected = (
            self.paths.run_dir
            / "executions"
            / checked["execution"]["execution_id"]
            / "model_calls"
        )
        if path != expected:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction model-call artifact reference is not canonical"
            )
        return ModelCallStore(path)

    def _verify_intent_artifacts(self, intent: Mapping[str, Any]) -> None:
        source = self._read_bound_text(intent["draft"], label="fact-extraction source")
        view = self._read_bound_json(
            intent["authority_view"],
            label="fact-extraction authority view",
        )
        _verify_authority_view_binding(
            view,
            book_id=intent["book_id"],
            draft_sha256=_sha256_text(source),
            context=intent["context"],
            authority=intent["authority"],
        )
        provenance_path = self._run_artifact_path(
            intent["execution"]["provenance_artifact_ref"]
        )
        provenance = _load_json(provenance_path, "fact-extraction provenance")
        try:
            checked = validate_schema(provenance, "execution_provenance.schema.json")
            verified = ExecutionProvenance.from_dict(checked)
        except Exception as exc:
            raise ChapterFactExtractionIntegrityError(
                f"fact-extraction provenance is invalid: {exc}"
            ) from exc
        if verified.provenance_hash != intent["execution"]["provenance_hash"]:
            raise ChapterFactExtractionIntegrityError(
                "fact-extraction provenance artifact hash mismatch"
            )
        self.model_call_store(intent)

    def _verify_record_artifacts(
        self,
        record: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> None:
        current_evidence = _model_call_evidence(self.model_call_store(intent))
        for field in (
            "intent_hashes",
            "receipt_hashes",
            "receipt_statuses",
            "unresolved_calls",
        ):
            if record["evidence"][field] != current_evidence[field]:
                raise ChapterFactExtractionIntegrityError(
                    f"fact-extraction {field} evidence changed"
                )

        selected = record["evidence"]["selected_response"]
        raw_response: str | None = None
        if selected is not None:
            raw_response = _load_selected_response(
                self.model_call_store(intent),
                selected,
            )
        if intent["execution"]["execution_kind"] == "deterministic":
            if any(
                record["evidence"][field]
                for field in (
                    "intent_hashes",
                    "receipt_hashes",
                    "receipt_statuses",
                    "unresolved_calls",
                )
            ) or selected is not None:
                raise ChapterFactExtractionIntegrityError(
                    "deterministic fact extraction cannot contain model-call evidence"
                )

        metadata = record["proposal"]
        if metadata["artifact_ref"] is None:
            if metadata != _empty_proposal_metadata():
                raise ChapterFactExtractionIntegrityError(
                    "empty fact-extraction proposal metadata is inconsistent"
                )
        else:
            expected_ref = (
                f"fact_extractions/{record['extraction_id']}/proposal.json"
            )
            if metadata["artifact_ref"] != expected_ref:
                raise ChapterFactExtractionIntegrityError(
                    "fact-extraction proposal reference is not canonical"
                )
            proposal = self.load_proposal(record["extraction_id"])
            if (
                metadata["sha256"] != proposal["proposal_hash"]
                or metadata["fact_count"] != len(proposal["facts"])
            ):
                raise ChapterFactExtractionIntegrityError(
                    "fact-extraction proposal metadata changed"
                )
            if raw_response is not None:
                try:
                    reparsed = parse_fact_proposal(
                        raw_response,
                        chapter_text=self.load_source(record["extraction_id"]),
                    )
                except ChapterFactCandidateError as exc:
                    raise ChapterFactExtractionIntegrityError(
                        "stored Provider response no longer yields the bound proposal"
                    ) from exc
                if reparsed != proposal["facts"]:
                    raise ChapterFactExtractionIntegrityError(
                        "stored fact proposal does not match its Provider response"
                    )

    def _read_bound_text(self, metadata: Mapping[str, Any], *, label: str) -> str:
        path = self._runtime_artifact_path(str(metadata["artifact_ref"]))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ChapterFactExtractionIntegrityError(
                f"cannot read {label}: {path}: {exc}"
            ) from exc
        if _sha256_text(text) != metadata["sha256"]:
            raise ChapterFactExtractionIntegrityError(f"{label} hash mismatch")
        if len(text) != metadata["chars"]:
            raise ChapterFactExtractionIntegrityError(f"{label} length mismatch")
        return text

    def _read_bound_json(
        self,
        metadata: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        path = self._runtime_artifact_path(str(metadata["artifact_ref"]))
        value = _load_json(path, label)
        if canonical_json_hash(value, exclude_environment_fields=False) != metadata[
            "sha256"
        ]:
            raise ChapterFactExtractionIntegrityError(f"{label} hash mismatch")
        if len(_canonical_json(value)) != metadata["chars"]:
            raise ChapterFactExtractionIntegrityError(f"{label} length mismatch")
        return value

    def _runtime_artifact_path(self, artifact_ref: str) -> Path:
        return _resolve_relative_artifact(self.paths.runtime_dir, artifact_ref)

    def _run_artifact_path(self, artifact_ref: str) -> Path:
        return _resolve_relative_artifact(self.paths.run_dir, artifact_ref)


class ChapterFactExtractionExecutor:
    """Run one recoverable fact-proposal operation without write authority."""

    def __init__(
        self,
        *,
        story_project_root: str | Path,
        provider: FactProvider | None = None,
        execution_kind: str = "model",
        repository_root: str | Path | None = None,
        provenance_factory: Callable[[], ExecutionProvenance] | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_injector: Callable[[str, str], None] | None = None,
    ) -> None:
        if execution_kind not in _ALLOWED_EXECUTION_KINDS:
            raise ValueError(
                f"execution_kind must be one of: {', '.join(sorted(_ALLOWED_EXECUTION_KINDS))}"
            )
        self.story_project_root = Path(story_project_root).resolve()
        self.store = ChapterFactExtractionStore(self.story_project_root)
        self.paths = self.store.paths
        self.provider = provider or _durable_openai_fact_provider
        self.execution_kind = execution_kind
        self.repository_root = (
            Path(repository_root).resolve()
            if repository_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.provenance_factory = provenance_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_injector = fault_injector
        self._captured_provenance: ExecutionProvenance | None = None

    def execute(
        self,
        *,
        book_id: str,
        chapter_index: int,
        candidate_binding: Mapping[str, Any],
        draft_binding: Mapping[str, Any],
        context_sha256: str,
        authority_binding: Mapping[str, Any],
        chapter_text: str,
        authority_view: Mapping[str, Any],
        idempotency_key: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        prepared = _prepare_request(
            book_id=book_id,
            chapter_index=chapter_index,
            candidate_binding=candidate_binding,
            draft_binding=draft_binding,
            context_sha256=context_sha256,
            authority_binding=authority_binding,
            chapter_text=chapter_text,
            authority_view=authority_view,
            idempotency_key=idempotency_key,
            author_confirmed=author_confirmed,
        )
        with provider_execution_lock(self.paths.run_dir):
            return self._execute_prepared(prepared)

    def _execute_prepared(self, prepared: Mapping[str, Any]) -> dict[str, Any]:
        extraction_id = prepared["extraction_id"]
        self._raise_if_abandoned(extraction_id)
        if self.store.has_record(extraction_id):
            record = self.store.load_record(extraction_id)
            _require_request_match(record, prepared["request_sha256"])
            return record
        if self.store.has_intent(extraction_id):
            intent = self.store.load_intent(extraction_id)
            _require_request_match(intent, prepared["request_sha256"])
            raise ChapterFactExtractionConflictError(
                "fact-extraction Intent already exists without a terminal record; explicit resume is required",
                code="chapter_fact_extraction_recovery_required",
            )

        self._raise_for_unresolved_provider_call()
        provenance = self._capture_provenance()
        execution_id = _execution_id(extraction_id)
        execution = {
            "execution_id": execution_id,
            "execution_kind": self.execution_kind,
            "operation_key": canonical_json_hash(
                {
                    "extraction_id": extraction_id,
                    "request_sha256": prepared["request_sha256"],
                    "stage": "chapter_fact_extraction",
                },
                exclude_environment_fields=False,
            ),
            "provenance_hash": provenance.provenance_hash,
            "provenance_artifact_ref": f"executions/{execution_id}/provenance.json",
            "model_calls_ref": f"executions/{execution_id}/model_calls",
        }
        intent = {
            "schema_version": CHAPTER_FACT_EXTRACTION_SCHEMA_VERSION,
            "record_type": "chapter_fact_extraction_intent",
            "extraction_id": extraction_id,
            "book_id": prepared["book_id"],
            "chapter_index": prepared["chapter_index"],
            "created_at": _timestamp(self.clock()),
            "idempotency_key_sha256": prepared["idempotency_key_sha256"],
            "request_sha256": prepared["request_sha256"],
            "candidate": copy.deepcopy(prepared["candidate"]),
            "draft": {
                **copy.deepcopy(prepared["draft"]),
                "artifact_ref": f"fact_extractions/{extraction_id}/source.md",
                "chars": len(prepared["chapter_text"]),
            },
            "context": copy.deepcopy(prepared["context"]),
            "authority": copy.deepcopy(prepared["authority"]),
            "authority_view": {
                "artifact_ref": f"fact_extractions/{extraction_id}/authority_view.json",
                "sha256": prepared["authority_view_sha256"],
                "chars": len(prepared["authority_view_text"]),
            },
            "author_confirmation": {
                "confirmed": True,
                "purpose": CHAPTER_FACT_EXTRACTION_PURPOSE,
            },
            "execution": execution,
        }
        intent["intent_hash"] = _record_hash(intent, "intent_hash")
        claimed = self.store.claim(
            chapter_text=prepared["chapter_text"],
            authority_view=prepared["authority_view"],
            intent=intent,
            provenance=provenance.to_dict(),
        )
        self._inject_fault("after_intent_before_provider", extraction_id)
        return self._execute_claimed(claimed)

    def resume(self, extraction_id: str) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        with provider_execution_lock(self.paths.run_dir):
            self._raise_if_abandoned(extraction_id)
            if self.store.has_record(extraction_id):
                return self.store.load_record(extraction_id)
            intent = self.store.load_intent(extraction_id)
            self._require_executor_kind(intent)
            self._require_current_provenance(intent)
            return self._execute_claimed(intent)

    def cancel(self, extraction_id: str) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        with provider_execution_lock(self.paths.run_dir):
            self._raise_if_abandoned(extraction_id)
            if self.store.has_record(extraction_id):
                return self.store.load_record(extraction_id)
            intent = self.store.load_intent(extraction_id)
            evidence = _model_call_evidence(self.store.model_call_store(intent))
            if evidence["unresolved_calls"]:
                first = evidence["unresolved_calls"][0]
                raise ProviderCallUncertainError(
                    call_id=str(first["call_id"]),
                    attempt_id=str(first["attempt_id"]),
                )
            record = self._build_record(
                intent,
                status="cancelled",
                proposal_metadata=_empty_proposal_metadata(),
                evidence={**evidence, "selected_response": None},
                error={
                    "type": "AuthorCancelled",
                    "code": "author_cancelled",
                    "message": "The author cancelled fact extraction before proposal publication.",
                },
            )
            return self.store.publish_record(record)

    def abandon_provider_uncertainty(
        self,
        extraction_id: str,
        *,
        expected_intent_hash: str,
        expected_record_hash: str | None,
        expected_uncertainty_sha256: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        extraction_id = _validate_extraction_id(extraction_id)
        expected_intent_hash = _require_sha256(
            expected_intent_hash,
            "expected_intent_hash",
        )
        if expected_record_hash is not None:
            expected_record_hash = _require_sha256(
                expected_record_hash,
                "expected_record_hash",
            )
        expected_uncertainty_sha256 = _require_sha256(
            expected_uncertainty_sha256,
            "expected_uncertainty_sha256",
        )
        if author_confirmed is not True:
            raise ChapterFactExtractionConflictError(
                "explicit author confirmation is required to abandon Provider uncertainty"
            )
        with provider_execution_lock(self.paths.run_dir):
            existing = load_fact_extraction_provider_resolution(
                self.paths.run_dir,
                extraction_id,
            )
            if existing is not None:
                if (
                    existing["fact_extraction_intent_hash"] != expected_intent_hash
                    or existing["fact_extraction_record_hash"] != expected_record_hash
                    or existing["uncertainty_sha256"]
                    != expected_uncertainty_sha256
                ):
                    raise ChapterFactExtractionProviderResolutionConflict(
                        "existing fact-extraction Provider resolution does not match the request"
                    )
                return existing

            intent = self.store.load_intent(extraction_id)
            if intent["intent_hash"] != expected_intent_hash:
                raise ChapterFactExtractionConflictError(
                    "fact-extraction Intent changed before Provider uncertainty was abandoned"
                )
            record: dict[str, Any] | None = None
            if self.store.has_record(extraction_id):
                record = self.store.load_record(extraction_id)
                if record["status"] != "provider_uncertain":
                    raise ChapterFactExtractionConflictError(
                        "only a provider-uncertain fact extraction can be abandoned"
                    )
                if record["record_hash"] != expected_record_hash:
                    raise ChapterFactExtractionConflictError(
                        "fact-extraction record changed before uncertainty was abandoned"
                    )
            elif expected_record_hash is not None:
                raise ChapterFactExtractionConflictError(
                    "fact extraction has no terminal record matching the request"
                )

            unresolved = self.store.model_call_store(intent).list_uncertain_calls()
            if not unresolved:
                raise ChapterFactExtractionConflictError(
                    "fact extraction no longer has an unresolved Provider attempt"
                )
            uncertainty = fact_extraction_provider_uncertainty_sha256(
                intent["execution"]["execution_id"],
                unresolved,
            )
            if uncertainty != expected_uncertainty_sha256:
                raise ChapterFactExtractionConflictError(
                    "Provider uncertainty changed before the author confirmed abandonment"
                )
            if record is not None:
                recorded = fact_extraction_provider_uncertainty_sha256(
                    intent["execution"]["execution_id"],
                    record["evidence"]["unresolved_calls"],
                )
                if recorded != uncertainty:
                    raise ChapterFactExtractionIntegrityError(
                        "fact-extraction record does not match current Provider uncertainty"
                    )
            resolution = build_fact_extraction_provider_resolution(
                created_at=_timestamp(self.clock()),
                book_id=intent["book_id"],
                chapter_index=intent["chapter_index"],
                extraction_id=extraction_id,
                execution_id=intent["execution"]["execution_id"],
                fact_extraction_intent_hash=intent["intent_hash"],
                fact_extraction_record_hash=(
                    record["record_hash"] if record is not None else None
                ),
                unresolved_calls=unresolved,
                author_confirmed=True,
            )
            return publish_fact_extraction_provider_resolution(
                self.paths.run_dir,
                resolution,
            )

    def _execute_claimed(self, intent: dict[str, Any]) -> dict[str, Any]:
        extraction_id = intent["extraction_id"]
        self._raise_if_abandoned(extraction_id)
        self._require_executor_kind(intent)
        self._require_current_provenance(intent)
        model_store = self.store.model_call_store(intent)
        evidence = _model_call_evidence(model_store)
        selected: dict[str, Any] | None = None
        raw_response: str | None = None

        try:
            if self.execution_kind == "deterministic":
                if any(
                    evidence[field]
                    for field in (
                        "intent_hashes",
                        "receipt_hashes",
                        "receipt_statuses",
                        "unresolved_calls",
                    )
                ):
                    raise ChapterFactExtractionIntegrityError(
                        "deterministic fact extraction has unexpected model-call evidence"
                    )
            else:
                if evidence["unresolved_calls"]:
                    first = evidence["unresolved_calls"][0]
                    raise ProviderCallUncertainError(
                        call_id=str(first["call_id"]),
                        attempt_id=str(first["attempt_id"]),
                    )
                if evidence["intent_hashes"]:
                    raw_response, selected = _load_single_successful_response(model_store)

            if raw_response is None:
                self._raise_for_unresolved_provider_call()
                messages = build_fact_extraction_messages(
                    chapter_index=intent["chapter_index"],
                    chapter_text=self.store.load_source(extraction_id),
                    authority_view=self.store.load_authority_view(extraction_id),
                )
                call_id = _model_call_id(extraction_id)
                raw_response = self.provider(messages, call_id, model_store.root)
                if not isinstance(raw_response, str):
                    raise ChapterFactExtractionIntegrityError(
                        "fact-extraction provider must return text"
                    )
                evidence = _model_call_evidence(model_store)
                if self.execution_kind == "model":
                    _require_successful_model_evidence(evidence)
                    selected = _selected_response_binding(model_store, raw_response)
                elif any(
                    evidence[field]
                    for field in (
                        "intent_hashes",
                        "receipt_hashes",
                        "receipt_statuses",
                        "unresolved_calls",
                    )
                ):
                    raise ChapterFactExtractionIntegrityError(
                        "deterministic fact extraction wrote model-call evidence"
                    )

            self._inject_fault("after_provider_before_parse", extraction_id)
            facts = parse_fact_proposal(
                raw_response,
                chapter_text=self.store.load_source(extraction_id),
            )
            proposal = self.store.publish_proposal(extraction_id, facts)
            self._inject_fault("after_proposal_before_record", extraction_id)
            record = self._build_record(
                intent,
                status="completed",
                proposal_metadata=proposal,
                evidence={**_model_call_evidence(model_store), "selected_response": selected},
                error=None,
            )
            return self.store.publish_record(record)
        except Exception as exc:
            evidence = _model_call_evidence(model_store)
            status = "provider_uncertain" if evidence["unresolved_calls"] else "failed"
            if selected is None and self.execution_kind == "model":
                selected = _best_effort_selected_response(model_store)
            record = self._build_record(
                intent,
                status=status,
                proposal_metadata=_empty_proposal_metadata(),
                evidence={**evidence, "selected_response": selected},
                error=_error_payload(exc),
            )
            persisted = self.store.publish_record(record)
            raise ChapterFactExtractionExecutionError(
                extraction_id=extraction_id,
                status=status,
                record=persisted,
                cause=exc,
            ) from exc

    def _build_record(
        self,
        intent: Mapping[str, Any],
        *,
        status: str,
        proposal_metadata: Mapping[str, Any],
        evidence: Mapping[str, Any],
        error: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        record = {
            "schema_version": CHAPTER_FACT_EXTRACTION_SCHEMA_VERSION,
            "record_type": "chapter_fact_extraction_record",
            "extraction_id": intent["extraction_id"],
            "book_id": intent["book_id"],
            "chapter_index": intent["chapter_index"],
            "status": status,
            "started_at": intent["created_at"],
            "finished_at": _timestamp(self.clock()),
            "intent_hash": intent["intent_hash"],
            "request_sha256": intent["request_sha256"],
            "candidate": copy.deepcopy(intent["candidate"]),
            "draft": copy.deepcopy(intent["draft"]),
            "context": copy.deepcopy(intent["context"]),
            "authority": copy.deepcopy(intent["authority"]),
            "authority_view": copy.deepcopy(intent["authority_view"]),
            "execution": copy.deepcopy(intent["execution"]),
            "proposal": copy.deepcopy(dict(proposal_metadata)),
            "evidence": copy.deepcopy(dict(evidence)),
            "error": copy.deepcopy(dict(error)) if error is not None else None,
        }
        record["record_hash"] = _record_hash(record, "record_hash")
        return _validate_extraction_record(record)

    def _capture_provenance(self) -> ExecutionProvenance:
        if self._captured_provenance is not None:
            return self._captured_provenance
        if self.provenance_factory is not None:
            provenance = self.provenance_factory()
            if not isinstance(provenance, ExecutionProvenance):
                raise ChapterFactExtractionIntegrityError(
                    "provenance_factory must return ExecutionProvenance"
                )
        else:
            config = get_config()
            provenance = capture_execution_provenance(
                self.repository_root,
                provider="openai",
                model=config.openai_model,
                config={
                    "openai_max_output_tokens": config.openai_max_output_tokens,
                    "provider_max_attempts": config.provider_max_attempts,
                    "provider_retry_deadline_seconds": config.provider_retry_deadline_seconds,
                },
                feature_flags={
                    "chapter_fact_extraction": True,
                    "formal_state_write": False,
                },
            )
        self._captured_provenance = provenance
        return provenance

    def _require_current_provenance(self, intent: Mapping[str, Any]) -> None:
        if self._capture_provenance().provenance_hash != intent["execution"][
            "provenance_hash"
        ]:
            raise ChapterFactExtractionConflictError(
                "fact-extraction execution provenance changed; resume with the original code and public configuration"
            )

    def _require_executor_kind(self, intent: Mapping[str, Any]) -> None:
        stored = intent["execution"]["execution_kind"]
        if stored != self.execution_kind:
            raise ChapterFactExtractionConflictError(
                f"fact-extraction execution kind is {stored!r}, executor is {self.execution_kind!r}"
            )

    def _raise_for_unresolved_provider_call(self) -> None:
        unresolved = list_unresolved_provider_calls(self.paths.run_dir)
        if unresolved:
            first = unresolved[0]
            raise ProviderCallUncertainError(
                call_id=str(first["call_id"]),
                attempt_id=str(first["attempt_id"]),
            )

    def _raise_if_abandoned(self, extraction_id: str) -> None:
        if (
            load_fact_extraction_provider_resolution(
                self.paths.run_dir,
                extraction_id,
            )
            is not None
        ):
            raise ChapterFactExtractionConflictError(
                "fact-extraction Provider uncertainty was abandoned and cannot be resumed or reused"
            )

    def _inject_fault(self, event: str, extraction_id: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(event, extraction_id)


def _prepare_request(
    *,
    book_id: str,
    chapter_index: int,
    candidate_binding: Mapping[str, Any],
    draft_binding: Mapping[str, Any],
    context_sha256: str,
    authority_binding: Mapping[str, Any],
    chapter_text: str,
    authority_view: Mapping[str, Any],
    idempotency_key: str,
    author_confirmed: bool,
) -> dict[str, Any]:
    book_id = _required_text(book_id, "book_id")
    chapter_index = _positive_integer(chapter_index, "chapter_index")
    key = str(idempotency_key or "")
    if _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise ChapterFactExtractionIntegrityError(
            "idempotency_key must be a safe non-empty identifier"
        )
    if author_confirmed is not True:
        raise ChapterFactExtractionConflictError(
            "explicit author confirmation is required to start fact extraction"
        )
    candidate = _validated_candidate_binding(candidate_binding)
    draft = _validated_draft_binding(draft_binding)
    prose = _validated_chapter_text(chapter_text)
    if _sha256_text(prose) != draft["sha256"]:
        raise ChapterFactExtractionConflictError(
            "editable draft changed before fact extraction"
        )
    context = {"context_sha256": _require_sha256(context_sha256, "context_sha256")}
    authority = _validated_authority_binding(authority_binding)
    view = _json_object(authority_view, "authority_view")
    _verify_authority_view_binding(
        view,
        book_id=book_id,
        draft_sha256=draft["sha256"],
        context=context,
        authority=authority,
    )
    view_text = _canonical_json(view)
    view_sha256 = canonical_json_hash(view, exclude_environment_fields=False)
    idempotency_key_sha256 = _sha256_text(key)
    extraction_id = _extraction_id_from_digest(
        book_id=book_id,
        chapter_index=chapter_index,
        idempotency_key_sha256=idempotency_key_sha256,
    )
    confirmation = {
        "confirmed": True,
        "purpose": CHAPTER_FACT_EXTRACTION_PURPOSE,
    }
    request_sha256 = _fact_extraction_request_hash(
        book_id=book_id,
        chapter_index=chapter_index,
        candidate=candidate,
        draft=draft,
        context=context,
        authority=authority,
        authority_view_sha256=view_sha256,
        authority_view_chars=len(view_text),
        author_confirmation=confirmation,
    )
    return {
        "book_id": book_id,
        "chapter_index": chapter_index,
        "extraction_id": extraction_id,
        "idempotency_key_sha256": idempotency_key_sha256,
        "request_sha256": request_sha256,
        "candidate": candidate,
        "draft": draft,
        "context": context,
        "authority": authority,
        "chapter_text": prose,
        "authority_view": view,
        "authority_view_text": view_text,
        "authority_view_sha256": view_sha256,
    }


def _validate_extraction_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = validate_schema(
            copy.deepcopy(dict(value)),
            "chapter_fact_extraction_intent.schema.json",
        )
    except (SchemaValidationError, TypeError, ValueError) as exc:
        raise ChapterFactExtractionIntegrityError(str(exc)) from exc
    if payload["intent_hash"] != _record_hash(payload, "intent_hash"):
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction Intent hash mismatch"
        )
    expected_id = _extraction_id_from_digest(
        book_id=payload["book_id"],
        chapter_index=payload["chapter_index"],
        idempotency_key_sha256=payload["idempotency_key_sha256"],
    )
    if payload["extraction_id"] != expected_id:
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction id binding mismatch"
        )
    if payload["execution"]["execution_id"] != _execution_id(expected_id):
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction execution id binding mismatch"
        )
    expected_operation = canonical_json_hash(
        {
            "extraction_id": expected_id,
            "request_sha256": payload["request_sha256"],
            "stage": "chapter_fact_extraction",
        },
        exclude_environment_fields=False,
    )
    if payload["execution"]["operation_key"] != expected_operation:
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction operation key mismatch"
        )
    expected_request = _fact_extraction_request_hash(
        book_id=payload["book_id"],
        chapter_index=payload["chapter_index"],
        candidate=payload["candidate"],
        draft={key: payload["draft"][key] for key in ("draft_id", "revision", "sha256")},
        context=payload["context"],
        authority=payload["authority"],
        authority_view_sha256=payload["authority_view"]["sha256"],
        authority_view_chars=payload["authority_view"]["chars"],
        author_confirmation=payload["author_confirmation"],
    )
    if payload["request_sha256"] != expected_request:
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction request hash mismatch"
        )
    expected_source = f"fact_extractions/{expected_id}/source.md"
    expected_view = f"fact_extractions/{expected_id}/authority_view.json"
    expected_provenance = (
        f"executions/{payload['execution']['execution_id']}/provenance.json"
    )
    expected_models = (
        f"executions/{payload['execution']['execution_id']}/model_calls"
    )
    if payload["draft"]["artifact_ref"] != expected_source:
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction source reference mismatch"
        )
    if payload["authority_view"]["artifact_ref"] != expected_view:
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction authority-view reference mismatch"
        )
    if (
        payload["execution"]["provenance_artifact_ref"] != expected_provenance
        or payload["execution"]["model_calls_ref"] != expected_models
    ):
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction execution artifact reference mismatch"
        )
    return payload


def _validate_extraction_record(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = validate_schema(
            copy.deepcopy(dict(value)),
            "chapter_fact_extraction_record.schema.json",
        )
    except (SchemaValidationError, TypeError, ValueError) as exc:
        raise ChapterFactExtractionIntegrityError(str(exc)) from exc
    if payload["record_hash"] != _record_hash(payload, "record_hash"):
        raise ChapterFactExtractionIntegrityError(
            "chapter fact-extraction record hash mismatch"
        )
    status = payload["status"]
    proposal = payload["proposal"]
    unresolved = payload["evidence"]["unresolved_calls"]
    error = payload["error"]
    if status == "completed":
        if (
            proposal["artifact_ref"] is None
            or proposal["sha256"] is None
            or unresolved
            or error is not None
        ):
            raise ChapterFactExtractionIntegrityError(
                "completed fact-extraction terminal fields are inconsistent"
            )
    elif status == "provider_uncertain":
        if proposal != _empty_proposal_metadata() or not unresolved or error is None:
            raise ChapterFactExtractionIntegrityError(
                "provider-uncertain fact-extraction fields are inconsistent"
            )
    else:
        if proposal != _empty_proposal_metadata() or unresolved or error is None:
            raise ChapterFactExtractionIntegrityError(
                f"{status} fact-extraction terminal fields are inconsistent"
            )
    return payload


def _require_record_links_intent(
    record: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> None:
    for field in (
        "extraction_id",
        "book_id",
        "chapter_index",
        "intent_hash",
        "request_sha256",
        "candidate",
        "draft",
        "context",
        "authority",
        "authority_view",
        "execution",
    ):
        expected = intent[field] if field in intent else None
        if record[field] != expected:
            raise ChapterFactExtractionIntegrityError(
                f"chapter fact-extraction record {field} does not match its Intent"
            )


def _fact_extraction_request_hash(
    *,
    book_id: str,
    chapter_index: int,
    candidate: Mapping[str, Any],
    draft: Mapping[str, Any],
    context: Mapping[str, Any],
    authority: Mapping[str, Any],
    authority_view_sha256: str,
    authority_view_chars: int,
    author_confirmation: Mapping[str, Any],
) -> str:
    return canonical_json_hash(
        {
            "book_id": book_id,
            "chapter_index": chapter_index,
            "candidate": copy.deepcopy(dict(candidate)),
            "draft": {
                "draft_id": draft["draft_id"],
                "revision": draft["revision"],
                "sha256": draft["sha256"],
            },
            "context": copy.deepcopy(dict(context)),
            "authority": copy.deepcopy(dict(authority)),
            "authority_view": {
                "sha256": authority_view_sha256,
                "chars": authority_view_chars,
            },
            "author_confirmation": copy.deepcopy(dict(author_confirmation)),
        },
        exclude_environment_fields=False,
    )


def _validated_candidate_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _exact_mapping(value, {"candidate_id", "intent_hash"}, "candidate_binding")
    candidate_id = str(candidate["candidate_id"])
    if _CANDIDATE_ID.fullmatch(candidate_id) is None:
        raise ChapterFactExtractionIntegrityError("candidate_id is invalid")
    return {
        "candidate_id": candidate_id,
        "intent_hash": _require_sha256(candidate["intent_hash"], "candidate intent_hash"),
    }


def _validated_draft_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = _exact_mapping(value, {"draft_id", "revision", "sha256"}, "draft_binding")
    return {
        "draft_id": _validate_draft_id(draft["draft_id"]),
        "revision": _positive_integer(draft["revision"], "draft revision"),
        "sha256": _require_sha256(draft["sha256"], "draft sha256"),
    }


def _validated_authority_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    authority = _exact_mapping(
        value,
        {"mode", "authority_epoch", "head_event_hash", "identity_revision"},
        "authority_binding",
    )
    head = authority["head_event_hash"]
    return {
        "mode": _required_text(authority["mode"], "authority mode"),
        "authority_epoch": _nonnegative_integer(
            authority["authority_epoch"],
            "authority_epoch",
        ),
        "head_event_hash": (
            _require_sha256(head, "head_event_hash") if head is not None else None
        ),
        "identity_revision": _require_sha256(
            authority["identity_revision"],
            "identity_revision",
        ),
    }


def _verify_authority_view_binding(
    view: Mapping[str, Any],
    *,
    book_id: str,
    draft_sha256: str,
    context: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    if view.get("chapter_prose_sha256") != draft_sha256:
        raise ChapterFactExtractionConflictError(
            "fact authority view is bound to a different editable draft"
        )
    binding = view.get("authority_binding")
    if not isinstance(binding, Mapping):
        raise ChapterFactExtractionIntegrityError(
            "fact authority view has no authority binding"
        )
    expected = {
        "book_id": book_id,
        "context_sha256": context["context_sha256"],
        "mode": authority["mode"],
        "authority_epoch": authority["authority_epoch"],
        "head_event_hash": authority["head_event_hash"],
    }
    for field, expected_value in expected.items():
        if binding.get(field) != expected_value:
            raise ChapterFactExtractionConflictError(
                f"fact authority view {field} binding changed"
            )
    claimed_view_hash = view.get("view_sha256")
    if claimed_view_hash is not None:
        claimed_view_hash = _require_sha256(claimed_view_hash, "authority view_sha256")
        actual = canonical_json_hash(
            view,
            exclude_fields=("view_sha256",),
            exclude_environment_fields=False,
        )
        if actual != claimed_view_hash:
            raise ChapterFactExtractionIntegrityError(
                "fact authority view self-hash mismatch"
            )


def _model_call_evidence(store: ModelCallStore) -> dict[str, Any]:
    intents: list[str] = []
    receipts: list[str] = []
    statuses: list[str] = []
    if store.intents_dir.is_dir():
        for path in sorted(store.intents_dir.glob("*.json")):
            intent = store.load_intent(path.stem)
            intents.append(str(intent["intent_hash"]))
    if store.receipts_dir.is_dir():
        for path in sorted(store.receipts_dir.glob("*.json")):
            receipt = store.load_receipt(path.stem)
            receipts.append(str(receipt["receipt_hash"]))
            statuses.append(str(receipt["status"]))
    return {
        "intent_hashes": intents,
        "receipt_hashes": receipts,
        "receipt_statuses": statuses,
        "unresolved_calls": store.list_uncertain_calls(),
        "selected_response": None,
    }


def _require_successful_model_evidence(evidence: Mapping[str, Any]) -> None:
    if not evidence["intent_hashes"]:
        raise ChapterFactExtractionIntegrityError(
            "model fact extraction produced no durable Provider Intent"
        )
    if evidence["unresolved_calls"]:
        first = evidence["unresolved_calls"][0]
        raise ProviderCallUncertainError(
            call_id=str(first["call_id"]),
            attempt_id=str(first["attempt_id"]),
        )
    if not evidence["receipt_hashes"] or "succeeded" not in evidence[
        "receipt_statuses"
    ]:
        raise ChapterFactExtractionIntegrityError(
            "model fact extraction has no successful Provider Receipt"
        )


def _selected_response_binding(
    store: ModelCallStore,
    raw_response: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for receipt, response in _successful_responses(store):
        if response.strip() != raw_response.strip():
            continue
        matches.append(_response_binding(receipt, raw_response))
    if not matches:
        raise ChapterFactExtractionIntegrityError(
            "fact proposal is not bound to a successful Provider response"
        )
    if len(matches) != 1:
        raise ChapterFactExtractionIntegrityError(
            "fact proposal ambiguously matches multiple Provider responses"
        )
    return matches[0]


def _load_single_successful_response(
    store: ModelCallStore,
) -> tuple[str, dict[str, Any]]:
    responses = _successful_responses(store)
    if len(responses) != 1:
        raise ChapterFactExtractionIntegrityError(
            "settled fact extraction must have exactly one successful Provider response"
        )
    receipt, raw = responses[0]
    return raw, _response_binding(receipt, raw)


def _best_effort_selected_response(store: ModelCallStore) -> dict[str, Any] | None:
    responses = _successful_responses(store)
    if len(responses) != 1:
        return None
    receipt, raw = responses[0]
    return _response_binding(receipt, raw)


def _successful_responses(
    store: ModelCallStore,
) -> list[tuple[dict[str, Any], str]]:
    results: list[tuple[dict[str, Any], str]] = []
    paths = sorted(store.receipts_dir.glob("*.json")) if store.receipts_dir.is_dir() else []
    for path in paths:
        receipt = store.load_receipt(path.stem)
        if receipt["status"] != "succeeded":
            continue
        artifact_ref = receipt.get("response_artifact_ref")
        if not isinstance(artifact_ref, str) or not artifact_ref:
            raise ChapterFactExtractionIntegrityError(
                "successful Provider Receipt has no response artifact"
            )
        artifact_path = _resolve_relative_artifact(store.root, artifact_ref)
        try:
            content = artifact_path.read_bytes()
            raw = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ChapterFactExtractionIntegrityError(
                f"cannot read Provider response artifact: {artifact_path}: {exc}"
            ) from exc
        if hashlib.sha256(content).hexdigest() != receipt["response_artifact_hash"]:
            raise ChapterFactExtractionIntegrityError(
                "Provider response artifact hash mismatch"
            )
        results.append((receipt, raw))
    return results


def _response_binding(
    receipt: Mapping[str, Any],
    raw_response: str,
) -> dict[str, Any]:
    normalized = raw_response.strip()
    return {
        "call_id": receipt["call_id"],
        "attempt_id": receipt["attempt_id"],
        "receipt_hash": receipt["receipt_hash"],
        "response_artifact_hash": receipt["response_artifact_hash"],
        "response_artifact_ref": receipt["response_artifact_ref"],
        "normalization": "utf8-strip-v1",
        "response_sha256": _sha256_text(normalized),
    }


def _load_selected_response(
    store: ModelCallStore,
    selected: Mapping[str, Any],
) -> str:
    attempt_id = str(selected.get("attempt_id") or "")
    receipt = store.load_receipt(attempt_id)
    for field in (
        "call_id",
        "attempt_id",
        "receipt_hash",
        "response_artifact_hash",
        "response_artifact_ref",
    ):
        if selected.get(field) != receipt.get(field):
            raise ChapterFactExtractionIntegrityError(
                f"selected Provider response {field} binding changed"
            )
    if receipt["status"] != "succeeded":
        raise ChapterFactExtractionIntegrityError(
            "selected Provider response Receipt did not succeed"
        )
    matches = [
        raw
        for candidate_receipt, raw in _successful_responses(store)
        if candidate_receipt["attempt_id"] == attempt_id
    ]
    if len(matches) != 1:
        raise ChapterFactExtractionIntegrityError(
            "selected Provider response artifact is unavailable"
        )
    raw = matches[0]
    if selected.get("normalization") != "utf8-strip-v1" or selected.get(
        "response_sha256"
    ) != _sha256_text(raw.strip()):
        raise ChapterFactExtractionIntegrityError(
            "selected Provider response normalization binding changed"
        )
    return raw


def _durable_openai_fact_provider(
    messages: list[dict[str, str]],
    call_id: str,
    evidence_root: Path,
) -> str:
    runtime = ModelCallRuntimeContext(ModelCallStore(evidence_root))
    response = chat_completion(
        messages,
        temperature=0.1,
        stage="chapter_fact_extraction",
        max_tokens=5000,
        model_call_runtime=runtime,
        call_id=call_id,
    )
    return response.text


def _validate_extraction_id(value: Any) -> str:
    text = str(value)
    if _EXTRACTION_ID.fullmatch(text) is None:
        raise ChapterFactExtractionIntegrityError(
            f"unsafe fact-extraction id: {text!r}"
        )
    return text


def _validate_draft_id(value: Any) -> str:
    text = str(value)
    if _DRAFT_ID.fullmatch(text) is None:
        raise ChapterFactExtractionIntegrityError("candidate draft id is invalid")
    return text


def _execution_id(extraction_id: str) -> str:
    digest = _validate_extraction_id(extraction_id).removeprefix("fact_extraction_")
    value = f"execution_fx_{digest[:24]}"
    if _EXECUTION_ID.fullmatch(value) is None:
        raise ChapterFactExtractionIntegrityError("fact-extraction execution id is invalid")
    return value


def _model_call_id(extraction_id: str) -> str:
    _validate_extraction_id(extraction_id)
    # ModelCallStore is already scoped by the extraction execution directory.
    # Keeping this stable logical id short also leaves headroom for Windows'
    # legacy path limit once ``-aN.json`` and response directories are added.
    return "fact"


def _extraction_id_from_digest(
    *,
    book_id: str,
    chapter_index: int,
    idempotency_key_sha256: str,
) -> str:
    digest = canonical_json_hash(
        {
            "book_id": _required_text(book_id, "book_id"),
            "chapter_index": _positive_integer(chapter_index, "chapter_index"),
            "idempotency_key_sha256": _require_sha256(
                idempotency_key_sha256,
                "idempotency_key_sha256",
            ),
        },
        exclude_environment_fields=False,
    )
    return f"fact_extraction_{digest[:40]}"


def _empty_proposal_metadata() -> dict[str, Any]:
    return {"artifact_ref": None, "sha256": None, "fact_count": 0}


def _require_request_match(value: Mapping[str, Any], expected: str) -> None:
    if value.get("request_sha256") != expected:
        raise ChapterFactExtractionConflictError(
            "fact-extraction request changed for the same idempotency key"
        )


def _record_hash(payload: Mapping[str, Any], field: str) -> str:
    return canonical_json_hash(
        payload,
        exclude_fields=(field,),
        exclude_environment_fields=False,
    )


def _resolve_relative_artifact(root: Path, artifact_ref: str) -> Path:
    pure = PurePosixPath(str(artifact_ref))
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or str(pure) != str(artifact_ref)
    ):
        raise ChapterFactExtractionIntegrityError(
            f"unsafe fact-extraction artifact reference: {artifact_ref!r}"
        )
    resolved_root = root.absolute()
    candidate = resolved_root / Path(*pure.parts)
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(candidate)), os.path.normcase(str(resolved_root)))
        )
    except ValueError as exc:
        raise ChapterFactExtractionIntegrityError(
            "fact-extraction artifact escapes its storage root"
        ) from exc
    if common != os.path.normcase(str(resolved_root)):
        raise ChapterFactExtractionIntegrityError(
            "fact-extraction artifact escapes its storage root"
        )
    assert_safe_local_tree(candidate.parent)
    _assert_not_link_or_reparse(candidate)
    return candidate


def _create_json_idempotent(
    path: Path,
    payload: Mapping[str, Any],
    *,
    contract: str,
) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(payload))
    try:
        atomic_create_json(path, candidate)
        return candidate
    except OSError:
        _assert_plain_file_or_missing(path)
        if not path.is_file():
            raise
        existing = _load_json(path, contract)
        if existing == candidate:
            return existing
        raise ChapterFactExtractionConflictError(
            f"{contract} already exists with different immutable content"
        )


def _create_text_idempotent(path: Path, value: str) -> None:
    try:
        atomic_create_text(path, value)
        return
    except OSError:
        _assert_plain_file_or_missing(path)
        if not path.is_file():
            raise
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ChapterFactExtractionIntegrityError(
                f"cannot read existing fact-extraction artifact: {path}"
            ) from exc
        if existing == value:
            return
        raise ChapterFactExtractionConflictError(
            "fact-extraction artifact already exists with different immutable content"
        )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    _assert_plain_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterFactExtractionIntegrityError(
            f"cannot read {label}: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ChapterFactExtractionIntegrityError(f"{label} must contain an object")
    return value


def _assert_plain_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ChapterFactExtractionIntegrityError(
            f"cannot inspect fact-extraction artifact: {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ChapterFactExtractionIntegrityError(
            f"links and reparse points are forbidden: {path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise ChapterFactExtractionIntegrityError(
            f"fact-extraction artifact must be a regular file: {path}"
        )


def _assert_plain_file_or_missing(path: Path) -> None:
    if os.path.lexists(path):
        _assert_plain_file(path)


def _assert_not_link_or_reparse(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ChapterFactExtractionIntegrityError(
            f"cannot inspect fact-extraction path: {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ChapterFactExtractionIntegrityError(
            f"links and reparse points are forbidden: {path}"
        )


def _safe_file_exists(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    _assert_plain_file(path)
    return True


def _error_payload(exc: BaseException) -> dict[str, Any]:
    code = getattr(exc, "code", None) or getattr(exc, "failure_category", None)
    return {
        "type": type(exc).__name__,
        "code": str(code or "chapter_fact_extraction_failed"),
        "message": str(exc)[:4000],
    }


def _exact_mapping(
    value: Mapping[str, Any],
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterFactExtractionIntegrityError(f"{label} must be an object")
    result = copy.deepcopy(dict(value))
    if set(result) != fields:
        raise ChapterFactExtractionIntegrityError(
            f"{label} fields differ; expected={sorted(fields)}, actual={sorted(result)}"
        )
    return result


def _json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterFactExtractionIntegrityError(f"{label} must be an object")
    try:
        return json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise ChapterFactExtractionIntegrityError(
            f"{label} must be finite canonical JSON"
        ) from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validated_chapter_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterFactExtractionIntegrityError(
            "chapter_text must be non-empty editable prose"
        )
    if len(value) > 500_000:
        raise ChapterFactExtractionIntegrityError("chapter_text is unexpectedly large")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ChapterFactExtractionIntegrityError(
            f"{label} must be a trimmed non-empty string"
        )
    return value


def _require_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if _SHA256.fullmatch(text) is None:
        raise ChapterFactExtractionIntegrityError(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return text


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterFactExtractionIntegrityError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChapterFactExtractionIntegrityError(
            f"{label} must be a non-negative integer"
        )
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "CHAPTER_FACT_EXTRACTION_PURPOSE",
    "CHAPTER_FACT_EXTRACTION_SCHEMA_VERSION",
    "ChapterFactExtractionConflictError",
    "ChapterFactExtractionError",
    "ChapterFactExtractionExecutionError",
    "ChapterFactExtractionExecutor",
    "ChapterFactExtractionIntegrityError",
    "ChapterFactExtractionStore",
]
