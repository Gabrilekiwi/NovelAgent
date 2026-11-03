from __future__ import annotations
from core.engine.execution_activity import execution_is_active

import copy
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from core.engine.chapter_candidate_context import (
    ChapterCandidateContextBuilder,
    ChapterCandidateContextError,
)
from core.engine.chapter_fact_context import (
    ChapterFactContextError,
    build_chapter_fact_context,
)
from core.engine.chapter_fact_extraction import (
    ChapterFactExtractionConflictError,
    ChapterFactExtractionExecutionError,
    ChapterFactExtractionExecutor,
    ChapterFactExtractionIntegrityError,
    ChapterFactExtractionStore,
    _extraction_id_from_digest,
    _execution_id,
)
from core.engine.chapter_fact_extraction_resolution import (
    ChapterFactExtractionProviderResolutionConflict,
    ChapterFactExtractionProviderResolutionError,
    fact_extraction_provider_uncertainty_sha256,
    load_fact_extraction_provider_resolution,
)
from core.engine.safe_paths import SafePathError
from core.model_call_runtime import ProviderCallUncertainError
from core.model_calls import ModelCallEvidenceError
from core.schema import SchemaValidationError
from core.story_project.identity import ProjectIdentityError
from core.story_project.read_set import StoryProjectSourceDriftError
from workbench.chapter_candidates import (
    CandidateDraftRecord,
    CandidateDraftStore,
    ChapterCandidateWorkbenchError,
)
from workbench.chapter_facts import (
    ChapterFactReviewConflict,
    ChapterFactReviewError,
    ChapterFactReviewIntegrity,
    ChapterFactReviewService,
)
from workbench.projects import ProjectRegistry
from workbench.chapter_budget_scope import chapter_service_operation


_EXTRACTION_ID = re.compile(r"^fact_extraction_[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")

ExecutorFactory = Callable[[Path], ChapterFactExtractionExecutor]


class ChapterFactExtractionWorkbenchError(RuntimeError):
    code = "chapter_fact_extraction_workbench_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class ChapterFactExtractionWorkbenchConflict(ChapterFactExtractionWorkbenchError):
    code = "chapter_fact_extraction_conflict"


class ChapterFactExtractionWorkbenchNotFound(ChapterFactExtractionWorkbenchError):
    code = "chapter_fact_extraction_not_found"


class ChapterFactExtractionWorkbenchIntegrity(ChapterFactExtractionWorkbenchError):
    code = "chapter_fact_extraction_integrity_error"


class ChapterFactExtractionService:
    """Workbench orchestration for recoverable, non-authoritative extraction."""

    def __init__(
        self,
        *,
        projects: ProjectRegistry,
        candidate_drafts: CandidateDraftStore,
        chapter_facts: ChapterFactReviewService,
        executor_factory: ExecutorFactory | None = None,
        context_builder_factory=None,
    ) -> None:
        self.projects = projects
        self.candidate_drafts = candidate_drafts
        self.chapter_facts = chapter_facts
        self.context_builder_factory = context_builder_factory or ChapterCandidateContextBuilder
        self.executor_factory = executor_factory or (
            lambda root: ChapterFactExtractionExecutor(story_project_root=root)
        )

    def start(
        self,
        draft_id: str,
        *,
        expected_draft_revision: int,
        expected_draft_sha256: str,
        idempotency_key: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterFactExtractionWorkbenchConflict(
                "explicit author confirmation is required to extract fact candidates"
            )
        key = _validated_idempotency_key(idempotency_key)
        draft=self.candidate_drafts.get(draft_id)
        project=self.projects.get(draft.project_id)
        executor=self.executor_factory(project.root)
        extraction_id=_extraction_id_from_digest(book_id=project.book_id,chapter_index=draft.chapter,
            idempotency_key_sha256=hashlib.sha256(key.encode('utf-8')).hexdigest())
        with chapter_service_operation(executor,project,draft.chapter,_execution_id(extraction_id),
                completed=ChapterFactExtractionStore(project.root).has_record(extraction_id)):
            return self._start_in_operation(draft_id,expected_draft_revision,expected_draft_sha256,key,executor)

    def _start_in_operation(self,draft_id,expected_draft_revision,expected_draft_sha256,key,executor):
        draft, intent, trusted, project = self._prepare_current_draft(
            draft_id,
            expected_revision=expected_draft_revision,
            expected_sha256=expected_draft_sha256,
        )
        try:
            view = build_chapter_fact_context(trusted, draft.text)
        except ChapterFactContextError as exc:
            raise ChapterFactExtractionWorkbenchConflict(
                "当前权威状态无法生成精简事实视图；事实提取尚未启动。",
                code=exc.code,
            ) from exc
        with _extraction_error_boundary():
            try:
                record = executor.execute(
                    book_id=project.book_id,
                    chapter_index=draft.chapter,
                    candidate_binding={
                        "candidate_id": draft.candidate_id,
                        "intent_hash": intent["intent_hash"],
                    },
                    draft_binding={
                        "draft_id": draft.id,
                        "revision": draft.revision,
                        "sha256": draft.sha256,
                    },
                    context_sha256=intent["source"]["context_sha256"],
                    authority_binding=intent["authority"],
                    chapter_text=draft.text,
                    authority_view=view.to_dict(),
                    idempotency_key=key,
                    author_confirmed=True,
                )
            except ChapterFactExtractionExecutionError as exc:
                record = exc.record
            return self._finalize(
                draft,
                record["extraction_id"],
                include_facts=True,
            )

    def resume(
        self,
        draft_id: str,
        extraction_id: str,
        *,
        expected_intent_hash: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterFactExtractionWorkbenchConflict(
                "explicit author confirmation is required to resume fact extraction"
            )
        draft = self.candidate_drafts.get(draft_id)
        project = self.projects.get(draft.project_id)
        with _extraction_error_boundary():
            intent = self._load_for_draft(project.root, draft, extraction_id)["intent"]
            if intent["intent_hash"] != _validated_sha256(
                expected_intent_hash,
                "expected_intent_hash",
            ):
                raise ChapterFactExtractionWorkbenchConflict(
                    "fact-extraction Intent changed before resume"
                )
            executor=self.executor_factory(project.root)
            store=ChapterFactExtractionStore(project.root)
            with chapter_service_operation(executor,project,draft.chapter,_execution_id(extraction_id),
                    completed=store.has_record(extraction_id),resume=True):
                try:
                    executor.resume(extraction_id)
                except ChapterFactExtractionExecutionError:
                    pass
                return self._finalize(draft, extraction_id, include_facts=True)

    def open_review(
        self,
        draft_id: str,
        extraction_id: str,
        *,
        expected_record_hash: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        """Adopt a completed local result without constructing an executor.

        A process can stop after publishing its terminal extraction record and
        before creating the Workbench review. Reads keep that gap visible; this
        explicit action completes only the local review import. _finalize also
        returns any existing review without replacing the author's edits.
        """
        if author_confirmed is not True:
            raise ChapterFactExtractionWorkbenchConflict(
                "explicit author confirmation is required to open extracted fact cards"
            )
        draft = self.candidate_drafts.get(draft_id)
        project = self.projects.get(draft.project_id)
        with _extraction_error_boundary():
            loaded = self._load_for_draft(project.root, draft, extraction_id)
            record = loaded["record"]
            if not isinstance(record, Mapping) or record["status"] != "completed":
                raise ChapterFactExtractionWorkbenchConflict(
                    "only a completed fact extraction can open review cards"
                )
            if record["record_hash"] != _validated_sha256(
                expected_record_hash, "expected_record_hash"
            ):
                raise ChapterFactExtractionWorkbenchConflict(
                    "fact-extraction record changed before review import"
                )
            fresh, _reason = self._source_freshness(draft, loaded["intent"])
            if not fresh:
                raise ChapterFactExtractionWorkbenchConflict(
                    "fact extraction is stale; preserve it and extract from the current draft",
                    code="chapter_fact_extraction_stale",
                )
            return self._finalize(draft, extraction_id, include_facts=True)

    def cancel(
        self,
        draft_id: str,
        extraction_id: str,
        *,
        expected_intent_hash: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterFactExtractionWorkbenchConflict(
                "explicit author confirmation is required to cancel fact extraction"
            )
        draft = self.candidate_drafts.get(draft_id)
        project = self.projects.get(draft.project_id)
        with _extraction_error_boundary():
            intent = self._load_for_draft(project.root, draft, extraction_id)["intent"]
            if intent["intent_hash"] != _validated_sha256(
                expected_intent_hash,
                "expected_intent_hash",
            ):
                raise ChapterFactExtractionWorkbenchConflict(
                    "fact-extraction Intent changed before cancellation"
                )
            self.executor_factory(project.root).cancel(extraction_id)
            return self._public(draft, extraction_id, include_facts=True)

    def abandon_provider_uncertainty(
        self,
        draft_id: str,
        extraction_id: str,
        *,
        expected_intent_hash: str,
        expected_record_hash: str | None,
        expected_uncertainty_sha256: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        draft = self.candidate_drafts.get(draft_id)
        project = self.projects.get(draft.project_id)
        with _extraction_error_boundary():
            self._load_for_draft(project.root, draft, extraction_id)
            self.executor_factory(project.root).abandon_provider_uncertainty(
                extraction_id,
                expected_intent_hash=expected_intent_hash,
                expected_record_hash=expected_record_hash,
                expected_uncertainty_sha256=expected_uncertainty_sha256,
                author_confirmed=author_confirmed,
            )
            return self._public(draft, extraction_id, include_facts=True)

    def get(self, draft_id: str, extraction_id: str) -> dict[str, Any]:
        draft = self.candidate_drafts.get(draft_id)
        with _extraction_error_boundary():
            return self._public(draft, extraction_id, include_facts=True)

    def list_for_draft(self, draft_id: str) -> tuple[dict[str, Any], ...]:
        draft = self.candidate_drafts.get(draft_id)
        project = self.projects.get(draft.project_id)
        with _extraction_error_boundary():
            store = ChapterFactExtractionStore(project.root)
            return tuple(
                self._public(
                    draft,
                    item["intent"]["extraction_id"],
                    include_facts=False,
                )
                for item in store.list_for_draft(draft.id)
            )

    def _prepare_current_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        expected_sha256: str,
    ):
        try:
            draft, intent = self.chapter_facts.verify_draft_source(draft_id)
        except ChapterFactReviewConflict as exc:
            raise ChapterFactExtractionWorkbenchConflict(
                "可信章节输入或权威状态已经变化；请先重新生成正文候选。",
                code=exc.code,
            ) from exc
        expected_revision = _positive_revision(expected_revision)
        expected_sha256 = _validated_sha256(
            expected_sha256,
            "expected_draft_sha256",
        )
        if (
            draft.revision != expected_revision
            or draft.sha256 != expected_sha256
        ):
            raise ChapterFactExtractionWorkbenchConflict(
                "editable draft changed before fact extraction",
                code="chapter_fact_source_draft_changed",
            )
        project = self.projects.get(draft.project_id)
        try:
            trusted = self.context_builder_factory(project.root).build(draft.chapter)
        except StoryProjectSourceDriftError as exc:
            raise ChapterFactExtractionWorkbenchConflict(
                "可信章节输入在读取期间发生变化；事实提取尚未启动。",
                code="chapter_fact_authority_changed",
            ) from exc
        except ChapterCandidateContextError as exc:
            raise ChapterFactExtractionWorkbenchConflict(
                "当前章节上下文或权威状态不可用；事实提取尚未启动。",
                code=exc.code,
            ) from exc
        if trusted.context_sha256 != intent["source"]["context_sha256"]:
            raise ChapterFactExtractionWorkbenchConflict(
                "章节上下文已不同于正文 Candidate；请重新生成正文。",
                code="chapter_fact_authority_changed",
            )
        return draft, intent, trusted, project

    def _finalize(
        self,
        draft: CandidateDraftRecord,
        extraction_id: str,
        *,
        include_facts: bool,
    ) -> dict[str, Any]:
        project = self.projects.get(draft.project_id)
        store = ChapterFactExtractionStore(project.root)
        loaded = self._load_for_draft(project.root, draft, extraction_id)
        record = loaded["record"]
        review: dict[str, Any] | None = self._review_for_extraction(
            draft.id,
            extraction_id,
        )
        review_creation_error: dict[str, str] | None = None
        if (
            review is None
            and isinstance(record, Mapping)
            and record["status"] == "completed"
        ):
            try:
                review = self.chapter_facts.create_from_extraction(
                    draft.id,
                    extraction_id,
                    expected_extraction_record_hash=record["record_hash"],
                    author_confirmed=True,
                )
            except ChapterFactReviewConflict as exc:
                if exc.code not in {
                    "chapter_fact_extraction_stale",
                    "chapter_fact_source_draft_changed",
                    "chapter_fact_authority_changed",
                }:
                    raise
                review_creation_error = {
                    "code": exc.code,
                    "message": "正文或权威状态在提取期间变化；候选已保留为只读证据，请从最新正文重新提取。",
                }
            except ChapterFactReviewIntegrity:
                raise
        result = self._public(draft, extraction_id, include_facts=include_facts)
        result["fact_review"] = review
        if review_creation_error is not None:
            result["source_state"] = "stale"
            result["stale_reason"] = review_creation_error["code"]
            result["error"] = review_creation_error
        # Force one final verified read after review creation.
        store.load(extraction_id)
        return result

    def _public(
        self,
        draft: CandidateDraftRecord,
        extraction_id: str,
        *,
        include_facts: bool,
    ) -> dict[str, Any]:
        project = self.projects.get(draft.project_id)
        loaded = self._load_for_draft(project.root, draft, extraction_id)
        intent = loaded["intent"]
        record = loaded["record"]
        proposal = loaded["proposal"]
        store = ChapterFactExtractionStore(project.root)
        resolution = load_fact_extraction_provider_resolution(
            store.paths.run_dir,
            extraction_id,
        )
        unresolved = store.model_call_store(intent).list_uncertain_calls()
        active = False
        if not isinstance(record, Mapping):
            active = execution_is_active(store.paths.run_dir, intent["execution"]["execution_id"])
            if not active:
                # A terminal result can appear while the read probes activity.
                loaded = self._load_for_draft(project.root, draft, extraction_id)
                record, proposal = loaded["record"], loaded["proposal"]
                unresolved = store.model_call_store(intent).list_uncertain_calls()
        if isinstance(record, Mapping):
            raw_status = str(record["status"])
        elif active:
            raw_status = "running"
        elif unresolved:
            raw_status = "provider_uncertain"
        else:
            raw_status = "needs_recovery"
        status = "abandoned" if resolution is not None else raw_status
        source_current, stale_reason = self._source_freshness(draft, intent)
        uncertainty = None
        if unresolved:
            uncertainty = {
                "count": len(unresolved),
                "sha256": fact_extraction_provider_uncertainty_sha256(
                    intent["execution"]["execution_id"],
                    unresolved,
                ),
                "abandoned": resolution is not None,
            }
        proposal_payload: dict[str, Any] = {
            "available": isinstance(proposal, Mapping),
            "sha256": (
                proposal.get("proposal_hash")
                if isinstance(proposal, Mapping)
                else None
            ),
            "fact_count": (
                len(proposal.get("facts", []))
                if isinstance(proposal, Mapping)
                else 0
            ),
        }
        if include_facts and isinstance(proposal, Mapping):
            proposal_payload["facts"] = copy.deepcopy(proposal["facts"])
        error = _public_error(
            record.get("error") if isinstance(record, Mapping) else None,
            raw_status,
        )
        return {
            "extraction_id": extraction_id,
            "project_id": draft.project_id,
            "chapter_index": draft.chapter,
            "status": status,
            "raw_status": raw_status,
            "source_state": "current" if source_current else "stale",
            "stale_reason": stale_reason,
            "created_at": intent["created_at"],
            "finished_at": (
                record.get("finished_at") if isinstance(record, Mapping) else None
            ),
            "intent_hash": intent["intent_hash"],
            "record_hash": (
                record.get("record_hash") if isinstance(record, Mapping) else None
            ),
            "source": {
                "candidate_id": intent["candidate"]["candidate_id"],
                "candidate_intent_hash": intent["candidate"]["intent_hash"],
                "draft_id": intent["draft"]["draft_id"],
                "draft_revision": intent["draft"]["revision"],
                "draft_sha256": intent["draft"]["sha256"],
                "context_sha256": intent["context"]["context_sha256"],
                "authority_view_sha256": intent["authority_view"]["sha256"],
            },
            "proposal": proposal_payload,
            "uncertainty": uncertainty,
            "resolution": (
                {
                    "action": resolution["action"],
                    "created_at": resolution["created_at"],
                    "resolution_hash": resolution["resolution_hash"],
                }
                if resolution is not None
                else None
            ),
            "fact_review": self._review_for_extraction(draft.id, extraction_id),
            "error": error,
        }

    def _load_for_draft(
        self,
        project_root: Path,
        draft: CandidateDraftRecord,
        extraction_id: str,
    ) -> dict[str, Any]:
        extraction_id = _validated_extraction_id(extraction_id)
        store = ChapterFactExtractionStore(project_root)
        if not store.has_intent(extraction_id):
            raise ChapterFactExtractionWorkbenchNotFound(
                "unknown fact extraction"
            )
        loaded = store.load(extraction_id)
        intent = loaded["intent"]
        if (
            intent["draft"]["draft_id"] != draft.id
            or intent["candidate"]["candidate_id"] != draft.candidate_id
            or intent["chapter_index"] != draft.chapter
        ):
            raise ChapterFactExtractionWorkbenchNotFound(
                "fact extraction does not belong to this editable draft"
            )
        return loaded

    def _source_freshness(
        self,
        draft: CandidateDraftRecord,
        intent: Mapping[str, Any],
    ) -> tuple[bool, str | None]:
        try:
            current = self.candidate_drafts.get(draft.id)
        except ChapterCandidateWorkbenchError as exc:
            raise ChapterFactExtractionWorkbenchIntegrity(
                "fact-extraction source draft is missing or corrupt"
            ) from exc
        if (
            current.revision != intent["draft"]["revision"]
            or current.sha256 != intent["draft"]["sha256"]
        ):
            return False, "candidate_draft_changed"
        try:
            _draft, candidate_intent = self.chapter_facts.verify_draft_source(draft.id)
        except ChapterFactReviewConflict:
            return False, "authority_or_source_changed"
        if (
            candidate_intent["intent_hash"] != intent["candidate"]["intent_hash"]
            or candidate_intent["source"]["context_sha256"]
            != intent["context"]["context_sha256"]
        ):
            return False, "candidate_or_context_changed"
        return True, None

    def _review_for_extraction(
        self,
        draft_id: str,
        extraction_id: str,
    ) -> dict[str, Any] | None:
        for item in self.chapter_facts.list_for_draft(draft_id):
            if item.get("source", {}).get("extraction_id") == extraction_id:
                return self.chapter_facts.get(str(item["id"]))
        return None


def _validated_extraction_id(value: Any) -> str:
    text = str(value or "")
    if _EXTRACTION_ID.fullmatch(text) is None:
        raise ChapterFactExtractionWorkbenchError(
            "unsafe fact-extraction id",
            code="chapter_fact_extraction_request_invalid",
        )
    return text


def _validated_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ChapterFactExtractionWorkbenchError(
            "idempotency_key must be a safe non-empty identifier",
            code="chapter_fact_extraction_request_invalid",
        )
    return value


def _validated_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if _SHA256.fullmatch(text) is None:
        raise ChapterFactExtractionWorkbenchError(
            f"{label} must be a lowercase SHA-256 value",
            code="chapter_fact_extraction_request_invalid",
        )
    return text


def _positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterFactExtractionWorkbenchError(
            "expected_draft_revision must be a positive integer",
            code="chapter_fact_extraction_request_invalid",
        )
    return value


def _public_error(
    error: Mapping[str, Any] | None,
    raw_status: str,
) -> dict[str, str] | None:
    if raw_status == "provider_uncertain":
        return {
            "code": "provider_call_uncertain",
            "message": "Provider 结果不确定；系统已停止新的模型调用。正文草稿、正式正文和权威状态均未修改。",
        }
    if raw_status == "needs_recovery":
        return {
            "code": "fact_extraction_needs_recovery",
            "message": "事实提取已有持久 Intent，但没有终态记录；系统不会自动恢复或重试。",
        }
    if raw_status == "cancelled":
        return {
            "code": "author_cancelled",
            "message": "事实提取已取消；正文草稿、正式正文和权威状态均未修改。",
        }
    if raw_status == "failed":
        code = str(error.get("code") if isinstance(error, Mapping) else "chapter_fact_extraction_failed")
        return {
            "code": code,
            "message": "事实候选提取失败且未自动重试；正文草稿、正式正文和权威状态均未修改。",
        }
    return None


@contextmanager
def _extraction_error_boundary() -> Iterator[None]:
    try:
        yield
    except ChapterFactExtractionWorkbenchError:
        raise
    except ChapterFactReviewIntegrity as exc:
        raise ChapterFactExtractionWorkbenchIntegrity(str(exc)) from exc
    except ChapterFactReviewConflict as exc:
        raise ChapterFactExtractionWorkbenchConflict(str(exc), code=exc.code) from exc
    except ChapterFactReviewError as exc:
        raise ChapterFactExtractionWorkbenchError(str(exc), code=exc.code) from exc
    except ProviderCallUncertainError as exc:
        raise ChapterFactExtractionWorkbenchConflict(
            "存在尚未处理的 Provider 不确定调用；系统不会自动重试。"
        ) from exc
    except (
        ChapterFactExtractionConflictError,
        ChapterFactExtractionProviderResolutionConflict,
    ) as exc:
        raise ChapterFactExtractionWorkbenchConflict(str(exc)) from exc
    except (
        ChapterFactExtractionIntegrityError,
        ChapterFactExtractionProviderResolutionError,
        ModelCallEvidenceError,
        ChapterCandidateContextError,
        StoryProjectSourceDriftError,
        ProjectIdentityError,
        SchemaValidationError,
        SafePathError,
        OSError,
        ValueError,
    ) as exc:
        raise ChapterFactExtractionWorkbenchIntegrity(
            "事实提取证据完整性校验失败；系统已停止读取、恢复和生成，以免覆盖证据。"
        ) from exc


__all__ = [
    "ChapterFactExtractionService",
    "ChapterFactExtractionWorkbenchConflict",
    "ChapterFactExtractionWorkbenchError",
    "ChapterFactExtractionWorkbenchIntegrity",
    "ChapterFactExtractionWorkbenchNotFound",
]
