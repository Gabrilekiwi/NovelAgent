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

from api.contracts import CHAPTER_CONTRACT, validate_language_output
from core.config import get_config
from core.context_budget import RunBudgetLimits, RunBudgetTracker
from core.director.director import validate_decision
from core.engine.chapter_candidate_context import (
    CHAPTER_CANDIDATE_CONTEXT_VERSION,
    ChapterCandidateContextBuilder,
)
from core.engine.execution_activity import execution_activity
from core.engine.chapter_candidate_resolution import (
    ChapterCandidateProviderResolutionConflict,
    build_candidate_provider_resolution,
    candidate_provider_uncertainty_sha256,
    load_candidate_provider_resolution,
    publish_candidate_provider_resolution,
)
from core.local_file_io import local_io_path
from core.engine.persistence import atomic_create_json, atomic_create_text
from core.engine.provider_safety import (
    list_unresolved_provider_calls,
    provider_execution_lock,
)
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, assert_safe_local_tree
from core.engine.story_project_context import StoryProjectContextService
from core.execution_provenance import (
    ExecutionProvenance,
    capture_execution_provenance,
)
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import (
    ModelCallRuntimeContext,
    ProviderCallUncertainError,
    use_model_call_runtime,
)
from core.model_calls import ModelCallStore
from core.project_profile import project_language
from core.runtime_paths import RuntimePaths
from core.schema import SchemaValidationError, validate_schema
from core.state.authoritative import seed_authoritative_state_from_snapshot
from core.state.builder import build_snapshot_state_with_audit
from core.state.generation_state_view import (
    apply_generation_state_view_to_snapshot,
    build_generation_state_view,
)
from core.state.input_pack import build_input_pack
from core.story_project.authority import AUTHORITY_MODE_EVENT
from core.story_project.read_set import (
    StoryProjectSourceDriftError,
)
from modules.chapter_generator.generator import generate_chapter


CHAPTER_CANDIDATE_SCHEMA_VERSION = "1.1"
AUTHOR_CANDIDATE_VERSION = "author_chapter_candidate_v1"
_CANDIDATE_ID = re.compile(r"^candidate_[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_EXECUTION_KINDS = frozenset({"model", "deterministic"})


class ChapterCandidateError(RuntimeError):
    code = "chapter_candidate_error"


class ChapterCandidateConflictError(ChapterCandidateError):
    code = "chapter_candidate_conflict"


class ChapterCandidateIntegrityError(ChapterCandidateError):
    code = "chapter_candidate_integrity_error"


class ChapterCandidateExecutionError(ChapterCandidateError):
    code = "chapter_candidate_execution_failed"

    def __init__(
        self,
        *,
        candidate_id: str,
        status: str,
        record: Mapping[str, Any],
        cause: BaseException,
    ) -> None:
        self.candidate_id = candidate_id
        self.status = status
        self.record = copy.deepcopy(dict(record))
        self.cause = cause
        super().__init__(
            f"{self.code}: candidate {candidate_id} ended as {status}: "
            f"{type(cause).__name__}: {cause}"
        )


class ChapterCandidateStore:
    """Append-only storage for editable prose candidates.

    Candidate artifacts are deliberately outside every formal publication and
    canonical-state directory. Provider evidence remains under ``runs`` so the
    existing project-wide uncertain-call fence can see it.
    """

    def __init__(self, story_project_root: str | Path) -> None:
        self.story_project_root = Path(story_project_root).resolve()
        if not self.story_project_root.is_dir():
            raise ChapterCandidateIntegrityError(
                f"StoryProject root is not a directory: {self.story_project_root}"
            )
        self.paths = RuntimePaths.for_story_project(self.story_project_root)
        self.root = self.paths.runtime_dir / "candidates"
        assert_safe_local_tree(self.root)

    def candidate_dir(self, candidate_id: str) -> Path:
        candidate_id = _validate_candidate_id(candidate_id)
        candidate_dir = self.root / candidate_id
        assert_safe_local_tree(candidate_dir)
        return candidate_dir

    def intent_path(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / "intent.json"

    def record_path(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / "record.json"

    def has_intent(self, candidate_id: str) -> bool:
        return _safe_artifact_file_exists(self.intent_path(candidate_id))

    def has_record(self, candidate_id: str) -> bool:
        return _safe_artifact_file_exists(self.record_path(candidate_id))

    def claim(
        self,
        *,
        input_pack: str,
        intent: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        checked = _validate_candidate_intent(intent)
        candidate_id = checked["candidate_id"]
        input_path = self._runtime_artifact_path(checked["input"]["artifact_ref"])
        expected_input_path = self.candidate_dir(candidate_id) / "input.md"
        if input_path != expected_input_path:
            raise ChapterCandidateIntegrityError("candidate input artifact reference is not canonical")
        if _sha256_text(input_pack) != checked["input"]["sha256"]:
            raise ChapterCandidateIntegrityError("candidate input hash does not match the claimed text")
        if len(input_pack) != checked["input"]["chars"]:
            raise ChapterCandidateIntegrityError("candidate input character count does not match")

        provenance_payload = validate_schema(
            copy.deepcopy(dict(provenance)),
            "execution_provenance.schema.json",
        )
        try:
            verified_provenance = ExecutionProvenance.from_dict(provenance_payload)
        except Exception as exc:
            raise ChapterCandidateIntegrityError(
                f"candidate execution provenance is invalid: {exc}"
            ) from exc
        if provenance_payload["provenance_hash"] != checked["execution"]["provenance_hash"]:
            raise ChapterCandidateIntegrityError("candidate provenance hash does not match its intent")
        if verified_provenance.provenance_hash != provenance_payload["provenance_hash"]:
            raise ChapterCandidateIntegrityError("candidate provenance canonical hash mismatch")
        provenance_path = self._run_artifact_path(
            checked["execution"]["provenance_artifact_ref"]
        )

        # Publish source text and provenance before the Intent that makes the
        # candidate executable. A crash can therefore leave only harmless,
        # idempotently reusable orphan artifacts.
        _create_text_idempotent(input_path, input_pack)
        _create_json_idempotent(
            provenance_path,
            provenance_payload,
            contract="candidate execution provenance",
        )
        return _create_json_idempotent(
            self.intent_path(candidate_id),
            checked,
            contract="chapter candidate intent",
        )

    def publish_draft(self, candidate_id: str, prose: str) -> dict[str, Any]:
        candidate_id = _validate_candidate_id(candidate_id)
        draft = str(prose)
        if not draft:
            raise ChapterCandidateIntegrityError("candidate draft must not be empty")
        path = self.candidate_dir(candidate_id) / "draft.md"
        _create_text_idempotent(path, draft)
        return {
            "artifact_ref": f"candidates/{candidate_id}/draft.md",
            "sha256": _sha256_text(draft),
            "chars": len(draft),
        }

    def publish_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        checked = _validate_candidate_record(record)
        candidate_id = checked["candidate_id"]
        # A malformed append-only terminal record cannot be repaired in place.
        # Prove its Intent linkage and every referenced artifact before the
        # no-clobber publication makes it durable.
        intent = self.load_intent(candidate_id)
        _require_record_links_intent(checked, intent)
        self._verify_record_artifacts(checked, intent)
        return _create_json_idempotent(
            self.record_path(candidate_id),
            checked,
            contract="chapter candidate record",
        )

    def load_intent(self, candidate_id: str) -> dict[str, Any]:
        candidate_id = _validate_candidate_id(candidate_id)
        payload = self._read_json(self.intent_path(candidate_id), "chapter candidate intent")
        checked = _validate_candidate_intent(payload)
        if checked["candidate_id"] != candidate_id:
            raise ChapterCandidateIntegrityError("candidate intent identity does not match its directory")
        self._verify_intent_artifacts(checked)
        return checked

    def load_record(self, candidate_id: str) -> dict[str, Any]:
        candidate_id = _validate_candidate_id(candidate_id)
        payload = self._read_json(self.record_path(candidate_id), "chapter candidate record")
        checked = _validate_candidate_record(payload)
        if checked["candidate_id"] != candidate_id:
            raise ChapterCandidateIntegrityError("candidate record identity does not match its directory")
        intent = self.load_intent(candidate_id)
        _require_record_links_intent(checked, intent)
        self._verify_record_artifacts(checked, intent)
        return checked

    def load_input(self, candidate_id: str) -> str:
        intent = self.load_intent(candidate_id)
        return self._read_bound_text(intent["input"], label="candidate input")

    def load_prose(self, candidate_id: str) -> str | None:
        record = self.load_record(candidate_id)
        if record["prose"]["artifact_ref"] is None:
            return None
        return self._read_bound_text(record["prose"], label="candidate draft")

    def load(self, candidate_id: str) -> dict[str, Any]:
        intent = self.load_intent(candidate_id)
        record = self.load_record(candidate_id) if self.has_record(candidate_id) else None
        prose = self.load_prose(candidate_id) if isinstance(record, dict) else None
        return {
            "candidate_id": intent["candidate_id"],
            "status": record["status"] if isinstance(record, dict) else "running",
            "intent": intent,
            "record": record,
            "input_pack": self._read_bound_text(intent["input"], label="candidate input"),
            "prose": prose,
        }

    def list_for_chapter(
        self,
        *,
        book_id: str,
        chapter_index: int,
    ) -> tuple[dict[str, Any], ...]:
        """Discover verified Candidate summaries without returning input-pack text.

        Bound input artifacts are still read and hashed while each Intent is
        verified.  Discovery must never trade integrity for a lighter DTO.
        """

        expected_book_id = _require_nonempty(book_id, "book_id")
        if (
            isinstance(chapter_index, bool)
            or not isinstance(chapter_index, int)
            or chapter_index < 1
        ):
            raise ChapterCandidateIntegrityError(
                "chapter_index must be a positive integer"
            )
        assert_safe_local_tree(self.root)
        if not self.root.is_dir():
            return ()
        try:
            candidate_paths = tuple(self.root.iterdir())
        except OSError as exc:
            raise ChapterCandidateIntegrityError(
                f"cannot enumerate candidate storage: {self.root}: {exc}"
            ) from exc
        items: list[dict[str, Any]] = []
        for path in sorted(candidate_paths, key=lambda item: item.name):
            if _CANDIDATE_ID.fullmatch(path.name) is None:
                continue
            _assert_safe_artifact_directory(path)
            if not self.has_intent(path.name):
                # A crash before the append-only Intent publication may leave
                # harmless source/provenance artifacts.  They are not Candidates.
                if self.has_record(path.name):
                    raise ChapterCandidateIntegrityError(
                        f"candidate record exists without its Intent: {path}"
                    )
                continue
            intent = self.load_intent(path.name)
            if (
                intent["book_id"] != expected_book_id
                or intent["chapter_index"] != chapter_index
            ):
                continue
            record = self.load_record(path.name) if self.has_record(path.name) else None
            items.append({"intent": intent, "record": record})
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    str(item["intent"]["created_at"]),
                    str(item["intent"]["candidate_id"]),
                ),
                reverse=True,
            )
        )

    def model_call_store(self, intent: Mapping[str, Any]) -> ModelCallStore:
        return ModelCallStore(
            self._run_artifact_path(str(intent["execution"]["model_calls_ref"]))
        )

    def partial_chapter_responses(self, intent, record, *, include_text=False):
        """Expose interrupted chapter prose only, never a plan, prompt or fact response."""
        if record['status'] != 'failed' or 'partial_response_failed' not in record['evidence']['receipt_statuses']:
            return []
        calls = self.model_call_store(intent)
        fragments = []
        for path in sorted(local_io_path(calls.receipts_dir).glob('*.json')):
            receipt = calls.load_receipt(path.stem)
            if receipt['status'] != 'partial_response_failed' or receipt['receipt_hash'] not in record['evidence']['receipt_hashes']:
                continue
            call = calls.load_intent(path.stem)
            if call['stage'] != 'chapter_generation' or call['intent_hash'] not in record['evidence']['intent_hashes']:
                continue
            if receipt['response_artifact_ref'] != f'responses/{path.stem}.txt':
                raise ChapterCandidateIntegrityError('unexpected partial chapter response reference')
            fragment = {'sha256': receipt['response_artifact_hash'], 'attempt_id': path.stem,
                        'finish_reason': receipt['finish_reason']}
            if include_text:
                artifact = _resolve_relative_artifact(calls.root, receipt['response_artifact_ref'])
                try:
                    raw = local_io_path(artifact).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != fragment['sha256']:
                        raise ChapterCandidateIntegrityError('partial chapter response hash mismatch')
                    fragment['text'] = raw.decode('utf-8')
                except (OSError, UnicodeError) as exc:
                    raise ChapterCandidateIntegrityError('partial chapter response unreadable') from exc
            fragments.append(fragment)
        return fragments

    def _verify_intent_artifacts(self, intent: dict[str, Any]) -> None:
        candidate_id = intent["candidate_id"]
        input_ref = f"candidates/{candidate_id}/input.md"
        if intent["input"]["artifact_ref"] != input_ref:
            raise ChapterCandidateIntegrityError("candidate input reference changed")
        self._read_bound_text(intent["input"], label="candidate input")

        if intent['schema_version'] == AUTHOR_CANDIDATE_VERSION:
            if self._read_bound_text(intent['input'], label='author import input') != json.dumps(
                    intent['author_import'], ensure_ascii=False, sort_keys=True):
                raise ChapterCandidateIntegrityError('author import input binding changed')
            return

        execution_id = _execution_id(candidate_id)
        execution = intent["execution"]
        if execution["execution_id"] != execution_id:
            raise ChapterCandidateIntegrityError("candidate execution identity changed")
        if execution["provenance_artifact_ref"] != f"executions/{execution_id}/provenance.json":
            raise ChapterCandidateIntegrityError("candidate provenance reference changed")
        if execution["model_calls_ref"] != f"executions/{execution_id}/model_calls":
            raise ChapterCandidateIntegrityError("candidate model-call reference changed")
        provenance_path = self._run_artifact_path(execution["provenance_artifact_ref"])
        provenance = ExecutionProvenance.from_dict(
            self._read_json(provenance_path, "candidate execution provenance")
        )
        if provenance.provenance_hash != execution["provenance_hash"]:
            raise ChapterCandidateIntegrityError("candidate execution provenance changed")

    def _verify_record_artifacts(
        self,
        record: dict[str, Any],
        intent: dict[str, Any],
    ) -> None:
        prose = record["prose"]
        prose_text: str | None = None
        if prose["artifact_ref"] is None:
            if prose["sha256"] is not None or prose["chars"] != 0:
                raise ChapterCandidateIntegrityError("empty candidate prose metadata is inconsistent")
            if record["status"] in {"completed", "stale"}:
                raise ChapterCandidateIntegrityError(
                    f"candidate status {record['status']} requires a draft artifact"
                )
        else:
            expected_ref = f"candidates/{record['candidate_id']}/draft.md"
            if prose["artifact_ref"] != expected_ref:
                raise ChapterCandidateIntegrityError("candidate draft reference changed")
            prose_text = self._read_bound_text(prose, label="candidate draft")

        if intent['schema_version'] == AUTHOR_CANDIDATE_VERSION:
            if (prose['sha256'] != intent['author_import']['prose_sha256']
                    or prose['chars'] != intent['author_import']['prose_chars']):
                raise ChapterCandidateIntegrityError('author supplied prose binding changed')
            return
        store = self.model_call_store(intent)
        actual = self._call_evidence(store)
        evidence = record["evidence"]
        if evidence["intent_hashes"] != actual["intent_hashes"]:
            raise ChapterCandidateIntegrityError("candidate model-call intent evidence changed")
        if evidence["receipt_hashes"] != actual["receipt_hashes"]:
            raise ChapterCandidateIntegrityError("candidate model-call receipt evidence changed")
        if evidence["receipt_statuses"] != actual["receipt_statuses"]:
            raise ChapterCandidateIntegrityError("candidate model-call receipt status evidence changed")
        if evidence["unresolved_calls"] != actual["unresolved_calls"]:
            raise ChapterCandidateIntegrityError("candidate unresolved-call evidence changed")
        if record["status"] in {"completed", "stale"} and record["execution"]["execution_kind"] == "model":
            if prose_text is None:
                raise ChapterCandidateIntegrityError("model candidate has no prose to bind")
            selected_response = self._selected_response(store, prose_text)
            if evidence["selected_response"] != selected_response:
                raise ChapterCandidateIntegrityError(
                    "candidate selected Provider response evidence changed"
                )
        if record["status"] in {"completed", "stale", "cancelled"} and actual["unresolved_calls"]:
            raise ChapterCandidateIntegrityError(
                f"candidate status {record['status']} cannot hide an unresolved provider call"
            )

    def _read_bound_text(self, metadata: Mapping[str, Any], *, label: str) -> str:
        path = self._runtime_artifact_path(str(metadata["artifact_ref"]))
        try:
            text = self._read_text(path)
        except (OSError, UnicodeError) as exc:
            raise ChapterCandidateIntegrityError(f"cannot read {label}: {path}: {exc}") from exc
        if _sha256_text(text) != metadata["sha256"]:
            raise ChapterCandidateIntegrityError(f"{label} hash mismatch")
        if len(text) != metadata["chars"]:
            raise ChapterCandidateIntegrityError(f"{label} character count mismatch")
        return text

    def _read_json(self, path: Path, label: str):
        return _load_json(path, label)

    def _read_text(self, path: Path):
        return path.read_text(encoding="utf-8")

    def _call_evidence(self, store):
        return _model_call_evidence(store)

    def _selected_response(self, store, prose):
        return _selected_response_binding(store, prose)

    def _runtime_artifact_path(self, artifact_ref: str) -> Path:
        return _resolve_relative_artifact(self.paths.runtime_dir, artifact_ref)

    def _run_artifact_path(self, artifact_ref: str) -> Path:
        return _resolve_relative_artifact(self.paths.run_dir, artifact_ref)


class ChapterCandidateExecutor:
    """Generate one recoverable prose candidate without touching formal state."""

    def __init__(
        self,
        *,
        story_project_root: str | Path,
        generator: Callable[[str], str] | None = None,
        execution_kind: str = "model",
        repository_root: str | Path | None = None,
        run_budget_limits: RunBudgetLimits | None = None,
        provenance_factory: Callable[[], ExecutionProvenance] | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_injector: Callable[[str, str], None] | None = None,
        execution_budget=None,
        execution_budget_factory=None,
    ) -> None:
        if execution_kind not in _ALLOWED_EXECUTION_KINDS:
            raise ValueError(
                f"execution_kind must be one of: {', '.join(sorted(_ALLOWED_EXECUTION_KINDS))}"
            )
        self.story_project_root = Path(story_project_root).resolve()
        self.store = ChapterCandidateStore(self.story_project_root)
        self.paths = self.store.paths
        self.context_builder = ChapterCandidateContextBuilder(self.story_project_root)
        self.generator = generator or (lambda input_pack: generate_chapter(input_pack, dry_run=False))
        self.execution_kind = execution_kind
        self.repository_root = (
            Path(repository_root).resolve()
            if repository_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.run_budget_limits = run_budget_limits or RunBudgetLimits()
        self.provenance_factory = provenance_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_injector = fault_injector
        self.execution_budget = execution_budget
        self.execution_budget_factory = execution_budget_factory
        self._captured_provenance: ExecutionProvenance | None = None

    def execute(
        self,
        *,
        chapter_index: int,
        confirmation: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        prepared = self._prepare_request(
            chapter_index=chapter_index,
            confirmation=confirmation,
        )
        with provider_execution_lock(self.paths.run_dir):
            candidate_id = _candidate_id(book_id=prepared["identity"]["book_id"],
                chapter_index=prepared["chapter_index"], idempotency_key=idempotency_key)
            with execution_activity(self.paths.run_dir, _execution_id(candidate_id)):
                return self._execute_prepared(prepared, idempotency_key=idempotency_key)

    def prepare_generation_estimate(self, *, chapter_index, confirmation):
        """Run the normal confirmation and prompt checks, stopping before any execution."""
        from modules.chapter_generator.generator import prepare_chapter_messages

        prepared = self._prepare_request(chapter_index=chapter_index, confirmation=confirmation)
        from api.openai_client import estimate_chapter_request
        messages, _ = prepare_chapter_messages(prepared["input_pack"])
        from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
        budget = ChapterBudgetFreezeRegistry(self.story_project_root, book_id=prepared['identity']['book_id']).status(chapter_index)['budget']
        monetary = budget is not None and budget['amount_limit'] is not None
        return estimate_chapter_request(messages, monetary=monetary)

    def prepare_policy_request(self, *, chapter_index, chapter_intent):
        """Read-only preparation; this does not create execution permission."""
        trusted=self.context_builder.build(chapter_index)
        return self._prepare_trusted_request(trusted,chapter_intent=chapter_intent)

    def execute_policy(self, *, chapter_index, chapter_intent, policy_authorization, idempotency_key):
        from core.engine.reviewed_policy_generation import require_policy_generation_scope
        require_policy_generation_scope(policy_authorization)
        prepared=self.prepare_policy_request(chapter_index=chapter_index,chapter_intent=chapter_intent)
        prepared['policy_authorization']=require_policy_generation_scope(policy_authorization,prepared=prepared)
        with provider_execution_lock(self.paths.run_dir):
            candidate_id=_candidate_id(book_id=prepared['identity']['book_id'],chapter_index=chapter_index,idempotency_key=idempotency_key)
            if _execution_id(candidate_id)!=policy_authorization['execution_id']:
                raise ChapterCandidateConflictError('policy candidate execution differs from original step')
            with execution_activity(self.paths.run_dir,_execution_id(candidate_id)):
                return self._execute_prepared(prepared,idempotency_key=idempotency_key)

    def _execute_prepared(
        self,
        prepared: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity = prepared["identity"]
        policy=prepared.get('policy_authorization')
        if policy is not None:
            from core.engine.reviewed_policy_generation import require_policy_generation_scope
            require_policy_generation_scope(policy,prepared=prepared)
            if self._select_execution_budget(identity['book_id'],prepared['chapter_index']) is None:
                raise ChapterCandidateConflictError('policy generation requires the original chapter budget')
        candidate_id = _candidate_id(
            book_id=identity["book_id"],
            chapter_index=prepared["chapter_index"],
            idempotency_key=idempotency_key,
        )
        from core.chapter_budget_continuation import require_candidate_binding
        require_candidate_binding(self._select_execution_budget(identity['book_id'], prepared['chapter_index']),
            _execution_id(candidate_id), prepared['input_sha256'], prepared.get('author_confirmation'))
        self._raise_if_abandoned(candidate_id)
        request_sha256 = _candidate_request_hash(
            book_id=identity["book_id"],
            chapter_index=prepared["chapter_index"],
            input_sha256=prepared["input_sha256"],
            source=prepared["source"],
            authority=prepared["authority"],
            author_confirmation=prepared.get("author_confirmation"),
            policy_authorization=policy,
            chapter_intent=prepared["chapter_intent"],
        )

        if self.store.has_record(candidate_id):
            record = self.store.load_record(candidate_id)
            _require_request_match(record, request_sha256)
            return record

        if self.store.has_intent(candidate_id):
            intent = self.store.load_intent(candidate_id)
            _require_request_match(intent, request_sha256)
            if intent["input"]["sha256"] != prepared["input_sha256"]:
                raise ChapterCandidateConflictError(
                    "candidate input changed for the same idempotency key"
                )
            return self._execute_claimed(
                intent,
                input_pack=self.store.load_input(candidate_id),
                before_verification=prepared["source_verification"],
            )

        self._select_execution_budget(identity['book_id'],prepared['chapter_index'])
        self._raise_for_unresolved_provider_call()
        provenance = self._capture_provenance()
        execution_id = _execution_id(candidate_id)
        execution = {
            "execution_id": execution_id,
            "execution_kind": self.execution_kind,
            "operation_key": canonical_json_hash(
                {
                    "candidate_id": candidate_id,
                    "request_sha256": request_sha256,
                    "stage": "chapter_candidate_generation",
                },
                exclude_environment_fields=False,
            ),
            "provenance_hash": provenance.provenance_hash,
            "provenance_artifact_ref": f"executions/{execution_id}/provenance.json",
            "model_calls_ref": f"executions/{execution_id}/model_calls",
        }
        intent = {
            "schema_version": "1.2" if policy is not None else CHAPTER_CANDIDATE_SCHEMA_VERSION,
            "record_type": "chapter_candidate_intent",
            "candidate_id": candidate_id,
            "book_id": identity["book_id"],
            "chapter_index": prepared["chapter_index"],
            "created_at": _timestamp(self.clock()),
            "idempotency_key_sha256": _sha256_text(_require_nonempty(idempotency_key, "idempotency_key")),
            "request_sha256": request_sha256,
            "input": {
                "artifact_ref": f"candidates/{candidate_id}/input.md",
                "sha256": prepared["input_sha256"],
                "chars": len(prepared["input_pack"]),
            },
            "source": prepared["source"],
            "authority": prepared["authority"],
            "chapter_intent": prepared["chapter_intent"],
            "execution": execution,
        }
        intent.update({'policy_authorization':policy} if policy is not None else {'author_confirmation':prepared['author_confirmation']})
        intent["intent_hash"] = _record_hash(intent, "intent_hash")
        claimed = self.store.claim(
            input_pack=prepared["input_pack"],
            intent=intent,
            provenance=provenance.to_dict(),
        )
        return self._execute_claimed(
            claimed,
            input_pack=prepared["input_pack"],
            before_verification=prepared["source_verification"],
        )

    def resume(self, candidate_id: str) -> dict[str, Any]:
        candidate_id = _validate_candidate_id(candidate_id)
        with provider_execution_lock(self.paths.run_dir), execution_activity(self.paths.run_dir, _execution_id(candidate_id)):
            self._raise_if_abandoned(candidate_id)
            if self.store.has_record(candidate_id):
                return self.store.load_record(candidate_id)
            intent = self.store.load_intent(candidate_id)
            self._require_executor_kind(intent)
            return self._execute_claimed(
                intent,
                input_pack=self.store.load_input(candidate_id),
                before_verification={"ok": True, "phase": "resume_pending"},
                local_recovery=True,
            )

    def cancel(self, candidate_id: str) -> dict[str, Any]:
        """Cancel an unfinalized candidate without resolving Provider uncertainty."""

        candidate_id = _validate_candidate_id(candidate_id)
        with provider_execution_lock(self.paths.run_dir):
            self._raise_if_abandoned(candidate_id)
            if self.store.has_record(candidate_id):
                return self.store.load_record(candidate_id)
            intent = self.store.load_intent(candidate_id)
            evidence = _model_call_evidence(self.store.model_call_store(intent))
            if evidence["unresolved_calls"]:
                first = evidence["unresolved_calls"][0]
                raise ProviderCallUncertainError(
                    call_id=str(first["call_id"]),
                    attempt_id=str(first["attempt_id"]),
                )
            tracker = RunBudgetTracker(self.run_budget_limits)
            ModelCallRuntimeContext(
                self.store.model_call_store(intent),
                tracker=tracker,
            ).hydrate_tracker_from_store()
            record = self._build_record(
                intent,
                status="cancelled",
                prose_metadata=_empty_prose_metadata(),
                before_verification={"ok": True, "phase": "cancel"},
                after_verification=None,
                evidence={**evidence, "budget": tracker.report()},
                error={
                    "type": "AuthorCancelled",
                    "code": "author_cancelled",
                    "message": "The author cancelled this candidate before publication.",
                },
            )
            return self.store.publish_record(record)

    def abandon_provider_uncertainty(
        self,
        candidate_id: str,
        *,
        expected_intent_hash: str,
        expected_record_hash: str | None,
        expected_uncertainty_sha256: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        """Accept the risk of one unknown Provider outcome without rewriting it.

        This creates a separate append-only marker.  It never fabricates a
        Receipt, adopts an orphan response, or changes Candidate/formal state.
        """

        candidate_id = _validate_candidate_id(candidate_id)
        expected_intent_hash = _require_sha256_value(
            expected_intent_hash,
            "expected_intent_hash",
        )
        if expected_record_hash is not None:
            expected_record_hash = _require_sha256_value(
                expected_record_hash,
                "expected_record_hash",
            )
        expected_uncertainty_sha256 = _require_sha256_value(
            expected_uncertainty_sha256,
            "expected_uncertainty_sha256",
        )
        if author_confirmed is not True:
            raise ChapterCandidateConflictError(
                "explicit author confirmation is required to abandon Provider uncertainty"
            )

        with provider_execution_lock(self.paths.run_dir):
            existing = load_candidate_provider_resolution(
                self.paths.run_dir,
                candidate_id,
            )
            if existing is not None:
                if (
                    existing["candidate_intent_hash"] != expected_intent_hash
                    or existing["candidate_record_hash"] != expected_record_hash
                    or existing["uncertainty_sha256"]
                    != expected_uncertainty_sha256
                ):
                    raise ChapterCandidateProviderResolutionConflict(
                        "existing Candidate Provider resolution does not match the request"
                    )
                return existing

            intent = self.store.load_intent(candidate_id)
            if intent["intent_hash"] != expected_intent_hash:
                raise ChapterCandidateConflictError(
                    "Candidate Intent changed before Provider uncertainty was abandoned"
                )
            record: dict[str, Any] | None = None
            if self.store.has_record(candidate_id):
                record = self.store.load_record(candidate_id)
                if record["status"] != "provider_uncertain":
                    raise ChapterCandidateConflictError(
                        "only a provider-uncertain Candidate can be abandoned"
                    )
                if record["record_hash"] != expected_record_hash:
                    raise ChapterCandidateConflictError(
                        "Candidate record changed before Provider uncertainty was abandoned"
                    )
            elif expected_record_hash is not None:
                raise ChapterCandidateConflictError(
                    "Candidate has no terminal record matching the request"
                )

            unresolved_calls = self.store.model_call_store(intent).list_uncertain_calls()
            if not unresolved_calls:
                raise ChapterCandidateConflictError(
                    "Candidate no longer has an unresolved Provider attempt"
                )
            current_uncertainty = candidate_provider_uncertainty_sha256(
                intent["execution"]["execution_id"],
                unresolved_calls,
            )
            if current_uncertainty != expected_uncertainty_sha256:
                raise ChapterCandidateConflictError(
                    "Provider uncertainty changed before the author confirmed abandonment"
                )
            if record is not None:
                recorded_uncertainty = candidate_provider_uncertainty_sha256(
                    intent["execution"]["execution_id"],
                    record["evidence"]["unresolved_calls"],
                )
                if recorded_uncertainty != current_uncertainty:
                    raise ChapterCandidateIntegrityError(
                        "Candidate record does not match current Provider uncertainty"
                    )

            resolution = build_candidate_provider_resolution(
                created_at=_timestamp(self.clock()),
                book_id=intent["book_id"],
                chapter_index=intent["chapter_index"],
                candidate_id=candidate_id,
                execution_id=intent["execution"]["execution_id"],
                candidate_intent_hash=intent["intent_hash"],
                candidate_record_hash=(
                    record["record_hash"] if record is not None else None
                ),
                unresolved_calls=unresolved_calls,
            )
            return publish_candidate_provider_resolution(
                self.paths.run_dir,
                resolution,
            )

    def authorize_budgeted_continuation(self, *, source_candidate_id, chapter_index,
            confirmation, idempotency_key, expected_ledger_sha256, author_confirmed):
        """Authorize one new same-input Candidate without settling the old call.

        The author must first acknowledge the exact old Candidate using the
        existing risk-resolution operation. Batch/policy execution is excluded.
        """
        from core.chapter_budget_continuation import authorize
        if author_confirmed is not True:
            raise ChapterCandidateConflictError('explicit continuation approval is required')
        prepared = self._prepare_request(chapter_index=chapter_index, confirmation=confirmation)
        with provider_execution_lock(self.paths.run_dir):
            source = self.store.load_intent(source_candidate_id)
            resolution = load_candidate_provider_resolution(self.paths.run_dir, source_candidate_id)
            if resolution is None:
                raise ChapterCandidateConflictError('acknowledge the exact uncertain Candidate before continuation')
            if ('policy_authorization' in source or source['book_id'] != prepared['identity']['book_id']
                    or source['chapter_index'] != chapter_index
                    or source['input']['sha256'] != prepared['input_sha256']
                    or source['author_confirmation'] != prepared['author_confirmation']):
                raise ChapterCandidateConflictError('continuation must retain the same chapter, context and author intent')
            scope = self._select_execution_budget(source['book_id'], chapter_index)
            if scope is None:
                raise ChapterCandidateConflictError('continuation requires the original chapter budget')
            target = _candidate_id(book_id=source['book_id'], chapter_index=chapter_index,
                idempotency_key=idempotency_key)
            return authorize(scope.store, target_execution_id=_execution_id(target),
                source_execution_id=source['execution']['execution_id'],
                source_resolution_hash=resolution['resolution_hash'],
                candidate_input_sha256=prepared['input_sha256'],
                candidate_confirmation_sha256=prepared['author_confirmation']['confirmation_sha256'],
                expected_ledger_sha256=expected_ledger_sha256, author_confirmed=True)

    def _prepare_request(
        self,
        *,
        chapter_index: int,
        confirmation: Mapping[str, Any],
    ) -> dict[str, Any]:
        author_confirmation = _validate_author_confirmation(confirmation)
        trusted = self.context_builder.build(chapter_index)
        context = copy.deepcopy(trusted.story_project_context)
        chapter_index, identity, read_set = self._validate_prepared_context(context)
        if author_confirmation["chapter_index"] != chapter_index:
            raise ChapterCandidateConflictError(
                "author confirmation is bound to a different chapter"
            )
        if author_confirmation["book_id"] != identity.book_id:
            raise ChapterCandidateConflictError(
                "author confirmation is bound to a different book"
            )
        if author_confirmation["context_sha256"] != trusted.context_sha256:
            raise ChapterCandidateConflictError(
                "trusted chapter inputs changed after author confirmation"
            )
        return self._prepare_trusted_request(trusted,chapter_intent=author_confirmation['chapter_intent'],
            author_confirmation={k:author_confirmation[k] for k in ('schema_version','card_sha256','context_sha256','confirmation_sha256')})

    def _prepare_trusted_request(self,trusted,*,chapter_intent,author_confirmation=None):
        context=copy.deepcopy(trusted.story_project_context)
        chapter_index,identity,read_set=self._validate_prepared_context(context)
        before = copy.deepcopy(trusted.source_verification)

        base_snapshot = copy.deepcopy(trusted.snapshot)
        memory = copy.deepcopy(trusted.memory_context)
        applied_snapshot, applied_memory = self._apply_candidate_context(context, base_snapshot, memory)
        state_result = build_snapshot_state_with_audit(applied_snapshot, applied_memory)
        runtime_snapshot = StoryProjectContextService.apply_authority(
            context,
            state_result["snapshot"],
        )
        applied_memory["snapshot_builder_audit"] = state_result["audit"]

        generation_state_view = _generation_state_view(context, runtime_snapshot)
        if generation_state_view is not None:
            runtime_snapshot = apply_generation_state_view_to_snapshot(
                runtime_snapshot,
                generation_state_view,
            )
        chapter_intent = _normalize_confirmed_intent(
            chapter_intent,
            chapter_index,
        )
        decision = validate_decision(
            {
                "chapter_index": chapter_index,
                "goal": chapter_intent["goal"],
                "actions": ["generate_chapter", "validate"],
                "validation_focus": chapter_intent["validation_focus"],
                "max_repair_attempts": 0,
                "notes": chapter_intent["notes"],
            }
        )
        if runtime_snapshot.get("chapter_index") != chapter_index:
            raise ChapterCandidateIntegrityError(
                "prepared runtime snapshot chapter does not match the confirmed chapter"
            )
        input_pack = build_input_pack(
            runtime_snapshot,
            decision,
            applied_memory,
            story_project_context=context,
            generation_state_view=generation_state_view,
            prose_only=True,
        )
        authority = _authority_binding(identity, read_set)
        source = {
            "story_project_read_set": copy.deepcopy(read_set),
            "context_digest": read_set["context_digest"],
            "membership_fingerprint": read_set["membership_fingerprint"],
            "context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
            "runtime_cache_policy": "bypass",
            "context_sha256": trusted.context_sha256,
            "authority_projection_sha256": trusted.authority_projection_sha256,
            "generation_state_view_sha256": (
                generation_state_view.get("projection_sha256")
                if isinstance(generation_state_view, dict)
                else None
            ),
        }
        return {
            "chapter_index": chapter_index,
            "identity": identity.to_dict(),
            "authority": authority,
            "source": source,
            **({'author_confirmation':copy.deepcopy(author_confirmation)} if author_confirmation is not None else {}),
            "chapter_intent": chapter_intent,
            "input_pack": input_pack,
            "input_sha256": _sha256_text(input_pack),
            "source_verification": before,
        }

    def _validate_prepared_context(
        self,
        context: dict[str, Any],
    ) -> tuple[int, Any, dict[str, Any]]:
        root = context.get("story_project_root")
        if not isinstance(root, str) or Path(root).resolve() != self.story_project_root:
            raise ChapterCandidateIntegrityError(
                "prepared StoryProject context is not bound to this project root"
            )
        chapter_index = context.get("chapter_index")
        if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
            raise ChapterCandidateIntegrityError("prepared StoryProject chapter is invalid")
        if context.get("story_state_mode") != "strict":
            raise ChapterCandidateIntegrityError(
                "chapter candidates require a strict StoryProject context"
            )
        identity_payload = context.get("project_identity")
        identity = self.context_builder.validate_identity(identity_payload)
        authority = identity.authority if isinstance(identity.authority, dict) else {}
        event_authority = authority.get("mode") == AUTHORITY_MODE_EVENT
        if identity.ephemeral or (
            not event_authority and identity.story_state_mode != "strict"
        ):
            raise ChapterCandidateIntegrityError(
                "chapter candidates require a persisted, strict ProjectIdentity"
            )
        current = self.context_builder._load_persisted_identity()
        if current is None or current.to_dict() != identity.to_dict():
            raise ChapterCandidateIntegrityError(
                "prepared StoryProject identity differs from the current persisted identity"
            )
        read_set_value = context.get("read_set")
        if not isinstance(read_set_value, dict):
            raise ChapterCandidateIntegrityError(
                "prepared StoryProject context requires a complete read set"
            )
        try:
            read_set = validate_schema(
                copy.deepcopy(read_set_value),
                "story_project_read_set.schema.json",
            )
        except SchemaValidationError as exc:
            raise ChapterCandidateIntegrityError(str(exc)) from exc
        if read_set["book_id"] != identity.book_id or read_set["chapter_index"] != chapter_index:
            raise ChapterCandidateIntegrityError(
                "prepared StoryProject read set has a different book or chapter"
            )
        read_root = Path(read_set["root_identity"]["resolved_path"]).resolve()
        if read_root != self.story_project_root:
            raise ChapterCandidateIntegrityError(
                "prepared StoryProject read set is bound to another root"
            )
        return chapter_index, identity, read_set

    def _apply_candidate_context(self, context, snapshot, memory):
        return StoryProjectContextService.apply_context(context, snapshot, memory,
            snapshot_path=self.paths.snapshot_path, allow_legacy_snapshot_adoption=False)

    def _execute_claimed(
        self,
        intent: dict[str, Any],
        *,
        input_pack: str,
        before_verification: dict[str, Any],
        local_recovery: bool = False,
    ) -> dict[str, Any]:
        if intent['schema_version'] == AUTHOR_CANDIDATE_VERSION:
            raise ChapterCandidateConflictError('author supplied drafts cannot dispatch generation')
        if 'policy_authorization' in intent:
            from core.engine.reviewed_policy_generation import require_policy_generation_scope
            require_policy_generation_scope(intent['policy_authorization'])
            if self._select_execution_budget(intent['book_id'],intent['chapter_index']) is None:
                raise ChapterCandidateConflictError('policy generation requires the original chapter budget')
        self._raise_if_abandoned(intent["candidate_id"])
        self._require_executor_kind(intent)
        self._require_current_provenance(intent)
        model_store = self.store.model_call_store(intent)
        tracker = RunBudgetTracker(self.run_budget_limits)
        runtime = ModelCallRuntimeContext(model_store, tracker=tracker, clock=self.clock,
            local_recovery_requested=local_recovery)
        budget=self._select_execution_budget(intent['book_id'],intent['chapter_index'])
        if budget is not None:
            with budget.activate(runtime,intent):
                return self._execute_claimed_with_runtime(intent,input_pack=input_pack,
                    before_verification=before_verification,runtime=runtime)
        return self._execute_claimed_with_runtime(intent,input_pack=input_pack,
            before_verification=before_verification,runtime=runtime)

    def _execute_claimed_with_runtime(self,intent,*,input_pack,before_verification,runtime):
        self._raise_for_unresolved_provider_call()
        model_store=runtime.store
        tracker=runtime.tracker
        runtime.hydrate_tracker_from_store()
        try:
            # Close the preparation/provenance window before spending a Provider
            # call. A later change is checked again after the draft is durable.
            before_verification = self._verify_trusted_request(
                intent,
                phase="prepare",
            )
            if runtime.local_recovery_response is not None:
                prose = runtime.local_recovery_response.text
            else:
                with use_model_call_runtime(runtime):
                    with runtime.bind_operation(intent["execution"]["operation_key"]):
                        prose = self.generator(input_pack)
            prose = validate_language_output(
                prose,
                CHAPTER_CONTRACT,
                language=_language_from_input_pack(input_pack),
            )
            evidence = _model_call_evidence(model_store)
            if self.execution_kind == "model":
                _require_successful_model_evidence(evidence)
                evidence["selected_response"] = _selected_response_binding(
                    model_store,
                    prose,
                )
            prose_metadata = self.store.publish_draft(intent["candidate_id"], prose)
            self._inject_fault("after_draft_before_source_recheck", intent["candidate_id"])
            try:
                after = self._verify_trusted_request(
                    intent,
                    phase="pre_apply",
                )
            except StoryProjectSourceDriftError as exc:
                record = self._build_record(
                    intent,
                    status="stale",
                    prose_metadata=prose_metadata,
                    before_verification=before_verification,
                    after_verification={
                        "ok": False,
                        "phase": exc.phase,
                        "differences": copy.deepcopy(exc.differences),
                    },
                    evidence={**evidence, "budget": tracker.report()},
                    error=_error_payload(exc),
                )
                return self.store.publish_record(record)
            record = self._build_record(
                intent,
                status="completed",
                prose_metadata=prose_metadata,
                before_verification=before_verification,
                after_verification=after,
                evidence={**evidence, "budget": tracker.report()},
                error=None,
            )
            self._inject_fault("after_generation_before_record", intent["candidate_id"])
            return self.store.publish_record(record)
        except Exception as exc:
            if isinstance(exc, StoryProjectSourceDriftError):
                before_verification = {
                    "ok": False,
                    "phase": exc.phase,
                    "differences": copy.deepcopy(exc.differences),
                }
            evidence = _model_call_evidence(model_store)
            status = "provider_uncertain" if evidence["unresolved_calls"] else "failed"
            record = self._build_record(
                intent,
                status=status,
                prose_metadata=_empty_prose_metadata(),
                before_verification=before_verification,
                after_verification=None,
                evidence={**evidence, "budget": tracker.report()},
                error=_error_payload(exc),
            )
            persisted = self.store.publish_record(record)
            raise ChapterCandidateExecutionError(
                candidate_id=intent["candidate_id"],
                status=status,
                record=persisted,
                cause=exc,
            ) from exc

    def _select_execution_budget(self,book_id,chapter):
        if self.execution_budget_factory is not None:
            return self.execution_budget_factory(self.story_project_root,book_id=book_id,chapter=chapter)
        return self.execution_budget

    def _verify_trusted_request(
        self,
        intent: Mapping[str, Any],
        *,
        phase: str,
    ) -> dict[str, Any]:
        try:
            verification = self.context_builder.verify_binding(
                chapter_index=int(intent["chapter_index"]),
                source=intent["source"],
                authority=intent["authority"],
                phase=phase,
            )
        except StoryProjectSourceDriftError:
            raise
        except (ChapterCandidateIntegrityError, OSError, ValueError) as exc:
            raise StoryProjectSourceDriftError(
                phase,
                [
                    {
                        "field": "trusted_candidate_context",
                        "code": _exception_code(
                            exc,
                            fallback="candidate_context_rebuild_failed",
                        ),
                    }
                ],
            ) from exc

        return {
            **verification,
            "input_sha256": intent["input"]["sha256"],
        }

    def _build_record(
        self,
        intent: Mapping[str, Any],
        *,
        status: str,
        prose_metadata: Mapping[str, Any],
        before_verification: Mapping[str, Any],
        after_verification: Mapping[str, Any] | None,
        evidence: Mapping[str, Any],
        error: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        record = {
            "schema_version": intent["schema_version"],
            "record_type": "chapter_candidate_record",
            "candidate_id": intent["candidate_id"],
            "book_id": intent["book_id"],
            "chapter_index": intent["chapter_index"],
            "status": status,
            "started_at": intent["created_at"],
            "finished_at": _timestamp(self.clock()),
            "intent_hash": intent["intent_hash"],
            "request_sha256": intent["request_sha256"],
            "input": copy.deepcopy(intent["input"]),
            "authority": copy.deepcopy(intent["authority"]),
            "source_verification": {
                "before": copy.deepcopy(dict(before_verification)),
                "after": (
                    copy.deepcopy(dict(after_verification))
                    if after_verification is not None
                    else None
                ),
            },
            "execution": copy.deepcopy(intent["execution"]),
            "prose": copy.deepcopy(dict(prose_metadata)),
            "evidence": copy.deepcopy(dict(evidence)),
            "error": copy.deepcopy(dict(error)) if error is not None else None,
        }
        record["record_hash"] = _record_hash(record, "record_hash")
        return _validate_candidate_record(record)

    def _capture_provenance(self) -> ExecutionProvenance:
        if self._captured_provenance is not None:
            return self._captured_provenance
        if self.provenance_factory is not None:
            provenance = self.provenance_factory()
            if not isinstance(provenance, ExecutionProvenance):
                raise ChapterCandidateIntegrityError(
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
                    "chapter_candidate": True,
                    "formal_state_write": False,
                },
            )
        self._captured_provenance = provenance
        return provenance

    def _require_current_provenance(self, intent: Mapping[str, Any]) -> None:
        current = self._capture_provenance()
        expected = str(intent["execution"]["provenance_hash"])
        if current.provenance_hash != expected:
            raise ChapterCandidateConflictError(
                "candidate execution provenance changed; resume with the original code and public configuration"
            )

    def _require_executor_kind(self, intent: Mapping[str, Any]) -> None:
        stored = str(intent["execution"]["execution_kind"])
        if stored != self.execution_kind:
            raise ChapterCandidateConflictError(
                f"candidate execution kind is {stored!r}, executor is {self.execution_kind!r}"
            )

    def _raise_for_unresolved_provider_call(self) -> None:
        unresolved = list_unresolved_provider_calls(self.paths.run_dir)
        if not unresolved:
            return
        first = unresolved[0]
        raise ProviderCallUncertainError(
            call_id=str(first["call_id"]),
            attempt_id=str(first["attempt_id"]),
        )

    def _raise_if_abandoned(self, candidate_id: str) -> None:
        from workbench.restore_execution_policy import require_restored_execution_paths
        require_restored_execution_paths(self.store.candidate_dir(candidate_id))
        if load_candidate_provider_resolution(self.paths.run_dir, candidate_id) is not None:
            raise ChapterCandidateConflictError(
                "Candidate Provider uncertainty was abandoned and cannot be resumed or reused"
            )

    def _inject_fault(self, event: str, candidate_id: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(event, candidate_id)


def _exception_code(exc: BaseException, *, fallback: str) -> str:
    explicit = getattr(exc, "code", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    message_code = str(exc).split(":", 1)[0]
    if re.fullmatch(r"[a-z][a-z0-9_]*", message_code):
        return message_code
    return fallback


def _generation_state_view(
    context: Mapping[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    blueprint = context.get("chapter_blueprint")
    read_set = blueprint.get("chapter_context_read_set") if isinstance(blueprint, dict) else None
    if not isinstance(read_set, dict):
        return None
    authority = seed_authoritative_state_from_snapshot(snapshot)
    return build_generation_state_view(authority, read_set)


def _normalize_confirmed_intent(
    value: Mapping[str, Any],
    chapter_index: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterCandidateIntegrityError("confirmed_intent must be an object")
    unknown = set(value) - {"goal", "validation_focus", "notes"}
    if unknown:
        raise ChapterCandidateIntegrityError(
            f"confirmed_intent contains unsupported fields: {sorted(unknown)}"
        )
    goal = _require_nonempty(value.get("goal"), "confirmed_intent.goal")
    focus = value.get("validation_focus", ["continuity", "spatial", "logic"])
    notes = value.get("notes", [])
    decision = validate_decision(
        {
            "chapter_index": chapter_index,
            "goal": goal,
            "actions": ["generate_chapter", "validate"],
            "validation_focus": list(focus) if isinstance(focus, (list, tuple)) else focus,
            "max_repair_attempts": 0,
            "notes": list(notes) if isinstance(notes, (list, tuple)) else notes,
        }
    )
    return {
        "goal": decision["goal"],
        "validation_focus": list(decision["validation_focus"]),
        "notes": list(decision["notes"]),
    }


def _validate_author_confirmation(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterCandidateIntegrityError("confirmation must be an object")
    payload = copy.deepcopy(dict(value))
    try:
        validate_schema(payload, "chapter_intent_confirmation.schema.json")
    except (SchemaValidationError, TypeError) as exc:
        raise ChapterCandidateIntegrityError(
            f"author confirmation contract is invalid: {exc}"
        ) from exc
    if _record_hash(payload, "confirmation_sha256") != payload["confirmation_sha256"]:
        raise ChapterCandidateIntegrityError("author confirmation hash mismatch")
    normalized_intent = _normalize_confirmed_intent(
        payload["chapter_intent"],
        payload["chapter_index"],
    )
    if normalized_intent != payload["chapter_intent"]:
        raise ChapterCandidateIntegrityError(
            "author confirmation chapter intent is not canonical"
        )
    return payload


def _authority_binding(identity: Any, read_set: Mapping[str, Any]) -> dict[str, Any]:
    authority = identity.authority if isinstance(identity.authority, dict) else {}
    return {
        "mode": str(authority.get("mode") or "legacy_markdown_v1"),
        "authority_epoch": int(authority.get("authority_epoch") or 0),
        "head_event_hash": authority.get("head_event_hash"),
        "identity_revision": str(read_set["identity_revision"]),
    }


def _candidate_id(*, book_id: str, chapter_index: int, idempotency_key: str) -> str:
    key = _require_nonempty(idempotency_key, "idempotency_key")
    digest = canonical_json_hash(
        {
            "book_id": book_id,
            "chapter_index": chapter_index,
            "idempotency_key_sha256": _sha256_text(key),
        },
        exclude_environment_fields=False,
    )
    return f"candidate_{digest[:40]}"


def _execution_id(candidate_id: str) -> str:
    candidate_id = _validate_candidate_id(candidate_id)
    # Keep the durable namespace at 128 bits while leaving enough room for the
    # ModelCall attempt filename under the legacy Windows MAX_PATH boundary.
    return f"execution_candidate_{candidate_id.removeprefix('candidate_')[:32]}"


def _candidate_request_hash(
    *,
    book_id: str,
    chapter_index: int,
    input_sha256: str,
    source: Mapping[str, Any],
    authority: Mapping[str, Any],
    author_confirmation: Mapping[str, Any] | None,
    chapter_intent: Mapping[str, Any],
    policy_authorization: Mapping[str, Any] | None = None,
) -> str:
    return canonical_json_hash(
        {
            "book_id": book_id,
            "chapter_index": chapter_index,
            "input_sha256": input_sha256,
            "source_context_digest": source["context_digest"],
            "source_membership_fingerprint": source["membership_fingerprint"],
            "context_builder_version": source["context_builder_version"],
            "runtime_cache_policy": source["runtime_cache_policy"],
            "context_sha256": source["context_sha256"],
            "authority_projection_sha256": source["authority_projection_sha256"],
            "generation_state_view_sha256": source["generation_state_view_sha256"],
            "authority": copy.deepcopy(dict(authority)),
            **({'policy_authorization':copy.deepcopy(dict(policy_authorization))} if policy_authorization is not None
               else {'author_confirmation':copy.deepcopy(dict(author_confirmation))}),
            "chapter_intent": copy.deepcopy(dict(chapter_intent)),
        },
        exclude_environment_fields=False,
    )


def _validate_candidate_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(value))
    is_author = payload.get('schema_version') == AUTHOR_CANDIDATE_VERSION
    if payload.get("schema_version") == "1.0":
        raise ChapterCandidateIntegrityError(
            "legacy Candidate Intent 1.0 is preserved but cannot be resumed because it lacks an author-confirmation binding"
        )
    try:
        validate_schema(payload, 'author_chapter_candidate_intent.schema.json' if is_author else
            "chapter_policy_candidate_intent.schema.json" if payload.get('schema_version')=='1.2' else "chapter_candidate_intent.schema.json")
        validate_schema(
            payload["source"]["story_project_read_set"],
            "story_project_read_set.schema.json",
        )
    except (SchemaValidationError, KeyError, TypeError) as exc:
        raise ChapterCandidateIntegrityError(str(exc)) from exc
    if _record_hash(payload, "intent_hash") != payload["intent_hash"]:
        raise ChapterCandidateIntegrityError("chapter candidate intent hash mismatch")
    id_factory = _author_candidate_id if is_author else _candidate_id_from_digest
    candidate_id = id_factory(
        book_id=payload["book_id"],
        chapter_index=payload["chapter_index"],
        idempotency_key_sha256=payload["idempotency_key_sha256"],
    )
    if candidate_id != payload["candidate_id"]:
        raise ChapterCandidateIntegrityError("chapter candidate id binding mismatch")
    source = payload["source"]
    read_set = source["story_project_read_set"]
    if (
        read_set["book_id"] != payload["book_id"]
        or read_set["chapter_index"] != payload["chapter_index"]
        or source["context_digest"] != read_set["context_digest"]
        or source["membership_fingerprint"] != read_set["membership_fingerprint"]
        or payload["authority"]["identity_revision"] != read_set["identity_revision"]
    ):
        raise ChapterCandidateIntegrityError("chapter candidate source binding mismatch")
    if payload['schema_version']=='1.2':
        from core.engine.reviewed_policy_generation import validate_policy_generation_authorization,generation_input_sha256
        from core.engine.reviewed_batch_control import BatchControlError
        try:policy=validate_policy_generation_authorization(payload['policy_authorization'])
        except BatchControlError as exc:raise ChapterCandidateIntegrityError(str(exc)) from exc
        if (policy['book_id']!=payload['book_id'] or policy['chapter']!=payload['chapter_index']
                or policy['execution_id']!=payload['execution']['execution_id']
                or policy['context_sha256']!=source['context_sha256']
                or policy['chapter_intent_sha256']!=canonical_json_hash(payload['chapter_intent'],exclude_environment_fields=False)
                or policy['input_sha256']!=generation_input_sha256(dict(chapter_index=payload['chapter_index'],
                    identity={'book_id':payload['book_id']},authority=payload['authority'],source=source,
                    chapter_intent=payload['chapter_intent'],input_sha256=payload['input']['sha256']))):
            raise ChapterCandidateIntegrityError('policy candidate original source binding mismatch')
    else:
        _validate_candidate_author_confirmation(payload,source)
    event_authority = payload["authority"]["mode"] == AUTHORITY_MODE_EVENT
    projection_sha256 = source["authority_projection_sha256"]
    if (event_authority and projection_sha256 is None) or (
        not event_authority and projection_sha256 is not None
    ):
        raise ChapterCandidateIntegrityError(
            "chapter candidate authority projection binding mismatch"
        )
    expected_request = _candidate_request_hash(
        book_id=payload["book_id"],
        chapter_index=payload["chapter_index"],
        input_sha256=payload["input"]["sha256"],
        source=source,
        authority=payload["authority"],
        author_confirmation=payload.get("author_confirmation"),
        policy_authorization=payload.get('policy_authorization'),
        chapter_intent=payload["chapter_intent"],
    )
    if expected_request != payload["request_sha256"]:
        raise ChapterCandidateIntegrityError("chapter candidate request hash mismatch")
    if is_author:
        return payload
    execution_id = _execution_id(payload["candidate_id"])
    execution = payload["execution"]
    if (
        execution["execution_id"] != execution_id
        or execution["provenance_artifact_ref"]
        != f"executions/{execution_id}/provenance.json"
        or execution["model_calls_ref"] != f"executions/{execution_id}/model_calls"
    ):
        raise ChapterCandidateIntegrityError("chapter candidate execution reference mismatch")
    expected_operation = canonical_json_hash(
        {"candidate_id": payload["candidate_id"],"request_sha256": payload["request_sha256"],"stage": "chapter_candidate_generation"},
        exclude_environment_fields=False,
    )
    if payload["execution"]["operation_key"] != expected_operation:
        raise ChapterCandidateIntegrityError("chapter candidate operation key mismatch")
    return payload


def _validate_candidate_author_confirmation(payload,source):
    author_confirmation=payload['author_confirmation']
    if author_confirmation["context_sha256"] != source["context_sha256"]:
        raise ChapterCandidateIntegrityError(
            "chapter candidate author confirmation context binding mismatch"
        )
    reconstructed_confirmation = {
        "schema_version": author_confirmation["schema_version"],
        "record_type": "chapter_intent_confirmation",
        "book_id": payload["book_id"],
        "chapter_index": payload["chapter_index"],
        "confirmed": True,
        "card_sha256": author_confirmation["card_sha256"],
        "context_sha256": author_confirmation["context_sha256"],
        "chapter_intent": copy.deepcopy(payload["chapter_intent"]),
        "confirmation_sha256": author_confirmation["confirmation_sha256"],
    }
    validated_confirmation = _validate_author_confirmation(reconstructed_confirmation)
    if validated_confirmation["confirmation_sha256"] != author_confirmation["confirmation_sha256"]:
        raise ChapterCandidateIntegrityError(
            "chapter candidate author confirmation hash binding mismatch"
        )


def _validate_candidate_record(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(value))
    try:
        validate_schema(payload, 'author_chapter_candidate_record.schema.json' if
            payload.get('schema_version') == AUTHOR_CANDIDATE_VERSION else "chapter_candidate_record.schema.json")
    except (SchemaValidationError, TypeError) as exc:
        raise ChapterCandidateIntegrityError(str(exc)) from exc
    if _record_hash(payload, "record_hash") != payload["record_hash"]:
        raise ChapterCandidateIntegrityError("chapter candidate record hash mismatch")
    if payload.get('schema_version') == AUTHOR_CANDIDATE_VERSION:
        if any(not isinstance(payload['source_verification'][phase],dict) or
                payload['source_verification'][phase].get('ok') is not True for phase in ('before','after')):
            raise ChapterCandidateIntegrityError('author import sources were not verified')
    for digest in [
        *payload["evidence"]["intent_hashes"],
        *payload["evidence"]["receipt_hashes"],
    ]:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ChapterCandidateIntegrityError("candidate evidence contains an invalid hash")
    status = payload["status"]
    prose_present = payload["prose"]["artifact_ref"] is not None
    unresolved = payload["evidence"]["unresolved_calls"]
    error = payload["error"]
    after = payload["source_verification"]["after"]
    if status == "completed" and (
        not prose_present
        or error is not None
        or not isinstance(after, dict)
        or after.get("ok") is not True
        or unresolved
    ):
        raise ChapterCandidateIntegrityError("completed candidate terminal fields are inconsistent")
    if status == "stale" and (
        not prose_present
        or not isinstance(error, dict)
        or not isinstance(after, dict)
        or after.get("ok") is not False
        or unresolved
    ):
        raise ChapterCandidateIntegrityError("stale candidate terminal fields are inconsistent")
    if status == "provider_uncertain" and (
        prose_present or not isinstance(error, dict) or not unresolved or after is not None
    ):
        raise ChapterCandidateIntegrityError(
            "provider-uncertain candidate terminal fields are inconsistent"
        )
    if status in {"failed", "cancelled"} and (
        prose_present or not isinstance(error, dict) or after is not None
    ):
        raise ChapterCandidateIntegrityError(f"{status} candidate terminal fields are inconsistent")
    if status == "cancelled" and unresolved:
        raise ChapterCandidateIntegrityError("cancelled candidate cannot resolve Provider uncertainty")
    if (
        status in {"completed", "stale"}
        and payload["execution"].get("execution_kind") == "model"
        and (
            not payload["evidence"]["intent_hashes"]
            or not payload["evidence"]["receipt_hashes"]
            or "succeeded" not in payload["evidence"]["receipt_statuses"]
        )
    ):
        raise ChapterCandidateIntegrityError(
            "successful model candidate lacks durable Provider evidence"
        )
    selected_response = payload["evidence"]["selected_response"]
    if status in {"completed", "stale"} and payload["execution"]["execution_kind"] == "model":
        if not isinstance(selected_response, dict):
            raise ChapterCandidateIntegrityError(
                "successful model candidate lacks a selected Provider response"
            )
        if selected_response["receipt_hash"] not in payload["evidence"]["receipt_hashes"]:
            raise ChapterCandidateIntegrityError(
                "selected Provider Receipt is not present in candidate evidence"
            )
        if selected_response["prose_sha256"] != payload["prose"]["sha256"]:
            raise ChapterCandidateIntegrityError(
                "selected Provider response is not bound to the candidate prose hash"
            )
    elif selected_response is not None:
        raise ChapterCandidateIntegrityError(
            "only a successful model candidate may select a Provider response"
        )
    return payload


def _require_record_links_intent(
    record: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> None:
    for field in (
        "schema_version",
        "candidate_id",
        "book_id",
        "chapter_index",
        "intent_hash",
        "request_sha256",
        "input",
        "authority",
        "execution",
    ):
        expected = intent[field] if field in intent else None
        if record[field] != expected:
            raise ChapterCandidateIntegrityError(
                f"chapter candidate record {field} does not match its intent"
            )


def _model_call_evidence(store: ModelCallStore) -> dict[str, Any]:
    intents: list[str] = []
    receipts: list[str] = []
    receipt_statuses: list[str] = []
    if store.intents_dir.is_dir():
        for path in sorted(store.intents_dir.glob("*.json")):
            intent = store.load_intent(path.stem)
            intents.append(str(intent["intent_hash"]))
    if store.receipts_dir.is_dir():
        for path in sorted(store.receipts_dir.glob("*.json")):
            receipt = store.load_receipt(path.stem)
            receipts.append(str(receipt["receipt_hash"]))
            receipt_statuses.append(str(receipt["status"]))
    return {
        "intent_hashes": intents,
        "receipt_hashes": receipts,
        "unresolved_calls": store.list_uncertain_calls(),
        "receipt_statuses": receipt_statuses,
        "selected_response": None,
    }


def _selected_response_binding(
    store: ModelCallStore,
    prose: str,
    *, _responses=None,
) -> dict[str, Any]:
    """Bind normalized prose to exactly one hash-verified successful response."""

    matches: list[dict[str, Any]] = []
    if _responses is not None:
        responses = _responses
    else:
        responses = _successful_candidate_responses(store)
    for receipt, response_text in responses:
        if response_text.strip() != prose:
            continue
        matches.append(
            {
                "call_id": receipt["call_id"],
                "attempt_id": receipt["attempt_id"],
                "receipt_hash": receipt["receipt_hash"],
                "response_artifact_hash": receipt["response_artifact_hash"],
                "response_artifact_ref": receipt["response_artifact_ref"],
                "normalization": "utf8-strip-v1",
                "prose_sha256": _sha256_text(prose),
            }
        )
    if not matches:
        raise ChapterCandidateIntegrityError(
            "candidate prose is not bound to a successful Provider response"
        )
    if len(matches) != 1:
        raise ChapterCandidateIntegrityError(
            "candidate prose ambiguously matches multiple successful Provider responses"
        )
    return matches[0]


def _successful_candidate_responses(store):
    responses = []
    paths = sorted(store.receipts_dir.glob("*.json")) if store.receipts_dir.is_dir() else []
    for path in paths:
        receipt = store.load_receipt(path.stem)
        if receipt["status"] != "succeeded":
            continue
        artifact_ref = receipt.get("response_artifact_ref")
        if not isinstance(artifact_ref, str) or not artifact_ref:
            raise ChapterCandidateIntegrityError(
                "successful Provider Receipt has no response artifact"
            )
        artifact_path = _resolve_relative_artifact(store.root, artifact_ref)
        try:
            content = local_io_path(artifact_path).read_bytes()
            response_text = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ChapterCandidateIntegrityError(
                f"cannot read selected Provider response artifact: {artifact_path}: {exc}"
            ) from exc
        response_hash = hashlib.sha256(content).hexdigest()
        if response_hash != receipt["response_artifact_hash"]:
            raise ChapterCandidateIntegrityError(
                "successful Provider response artifact hash mismatch"
            )
        responses.append((receipt, response_text))
    return responses


def _require_successful_model_evidence(evidence: Mapping[str, Any]) -> None:
    if not evidence["intent_hashes"]:
        raise ChapterCandidateIntegrityError(
            "model_call_evidence_missing: model execution produced no durable Intent"
        )
    if evidence["unresolved_calls"]:
        first = evidence["unresolved_calls"][0]
        raise ProviderCallUncertainError(
            call_id=str(first["call_id"]),
            attempt_id=str(first["attempt_id"]),
        )
    if not evidence["receipt_hashes"] or "succeeded" not in evidence["receipt_statuses"]:
        raise ChapterCandidateIntegrityError(
            "model_call_evidence_missing: model execution has no successful Receipt"
        )


def _error_payload(error: BaseException) -> dict[str, Any]:
    code = getattr(error, "code", None) or getattr(error, "failure_category", None)
    payload: dict[str, Any] = {
        "type": type(error).__name__,
        "code": str(code or "candidate_generation_failed"),
        "message": str(error)[:4000],
    }
    if isinstance(error, StoryProjectSourceDriftError):
        payload["details"] = {
            "phase": error.phase,
            "differences": copy.deepcopy(error.differences),
        }
    return payload


def _empty_prose_metadata() -> dict[str, Any]:
    return {"artifact_ref": None, "sha256": None, "chars": 0}


def _require_request_match(value: Mapping[str, Any], request_sha256: str) -> None:
    if value.get("request_sha256") != request_sha256:
        raise ChapterCandidateConflictError(
            "candidate request changed for the same idempotency key"
        )


def _candidate_id_from_digest(
    *,
    book_id: str,
    chapter_index: int,
    idempotency_key_sha256: str,
) -> str:
    digest = canonical_json_hash(
        {
            "book_id": book_id,
            "chapter_index": chapter_index,
            "idempotency_key_sha256": idempotency_key_sha256,
        },
        exclude_environment_fields=False,
    )
    return f"candidate_{digest[:40]}"


def _author_candidate_id(**binding):
    digest = canonical_json_hash({'kind': AUTHOR_CANDIDATE_VERSION, **binding}, exclude_environment_fields=False)
    return 'candidate_' + digest[:40]


def _validate_candidate_id(candidate_id: str) -> str:
    value = str(candidate_id)
    if _CANDIDATE_ID.fullmatch(value) is None:
        raise ChapterCandidateIntegrityError(f"unsafe candidate id: {value!r}")
    return value


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
        raise ChapterCandidateIntegrityError(f"unsafe candidate artifact reference: {artifact_ref!r}")
    resolved_root = root.absolute()
    candidate = resolved_root / Path(*pure.parts)
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(candidate)), os.path.normcase(str(resolved_root)))
        )
    except ValueError as exc:
        raise ChapterCandidateIntegrityError("candidate artifact escapes its storage root") from exc
    if common != os.path.normcase(str(resolved_root)):
        raise ChapterCandidateIntegrityError("candidate artifact escapes its storage root")
    assert_safe_local_tree(candidate.parent)
    _assert_safe_artifact_file(candidate)
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
        _assert_safe_artifact_file(path)
        if not path.is_file():
            raise
        existing = _load_json(path, contract)
        if existing == candidate:
            return existing
        raise ChapterCandidateConflictError(
            f"{contract} already exists with different immutable content: {path}"
        )


def _create_text_idempotent(path: Path, text: str) -> None:
    try:
        atomic_create_text(path, text)
        return
    except OSError:
        _assert_safe_artifact_file(path)
        if not path.is_file():
            raise
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ChapterCandidateIntegrityError(f"cannot read existing candidate artifact: {path}") from exc
        if existing == text:
            return
        raise ChapterCandidateConflictError(
            f"candidate artifact already exists with different immutable content: {path}"
        )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    _assert_safe_artifact_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterCandidateIntegrityError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ChapterCandidateIntegrityError(f"{label} must contain a JSON object: {path}")
    return value


def _assert_safe_artifact_file(path: Path) -> None:
    if not os.path.lexists(local_io_path(path)):
        return
    try:
        info = os.lstat(local_io_path(path))
    except OSError as exc:
        raise ChapterCandidateIntegrityError(f"cannot inspect candidate artifact: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ChapterCandidateIntegrityError(
            f"links and reparse points are forbidden for candidate artifacts: {path}"
        )


def _safe_artifact_file_exists(path: Path) -> bool:
    if not os.path.lexists(local_io_path(path)):
        return False
    _assert_safe_artifact_file(path)
    try:
        info = os.lstat(local_io_path(path))
    except OSError as exc:
        raise ChapterCandidateIntegrityError(
            f"cannot inspect candidate artifact: {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ChapterCandidateIntegrityError(
            f"candidate artifact must be a plain regular file: {path}"
        )
    return True


def _assert_safe_artifact_directory(path: Path) -> None:
    try:
        info = os.lstat(local_io_path(path))
    except OSError as exc:
        raise ChapterCandidateIntegrityError(
            f"cannot inspect candidate directory: {path}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise ChapterCandidateIntegrityError(
            f"candidate path must be a plain directory: {path}"
        )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _require_nonempty(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ChapterCandidateIntegrityError(f"{name} must be a non-empty string")
    return text


def _require_sha256_value(value: Any, name: str) -> str:
    text = str(value or "")
    if _SHA256.fullmatch(text) is None:
        raise ChapterCandidateIntegrityError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return text


def _language_from_input_pack(input_pack: str) -> str:
    # The full input is already immutable evidence. Parsing its compact project
    # profile avoids persisting another mutable language input beside it.
    marker = "# Project Profile\n"
    start = input_pack.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = input_pack.find("\n\n# Director Decision", start)
    if end < 0:
        return ""
    try:
        profile = json.loads(input_pack[start:end])
    except (TypeError, ValueError):
        return ""
    return project_language({"project_profile": profile})


__all__ = [
    "CHAPTER_CANDIDATE_SCHEMA_VERSION",
    "ChapterCandidateConflictError",
    "ChapterCandidateError",
    "ChapterCandidateExecutionError",
    "ChapterCandidateExecutor",
    "ChapterCandidateIntegrityError",
    "ChapterCandidateStore",
]
