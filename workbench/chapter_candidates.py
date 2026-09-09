from __future__ import annotations

import copy
import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from core.engine.chapter_candidate import (
    ChapterCandidateConflictError,
    ChapterCandidateExecutionError,
    ChapterCandidateExecutor,
    ChapterCandidateIntegrityError,
    ChapterCandidateStore,
)
from core.engine.chapter_candidate_resolution import (
    ChapterCandidateProviderResolutionConflict,
    ChapterCandidateProviderResolutionError,
    candidate_provider_uncertainty_sha256,
    load_candidate_provider_resolution,
)
from core.model_call_runtime import ProviderCallUncertainError
from core.model_calls import ModelCallEvidenceError
from workbench.chapter_cards import ChapterCardService
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry


_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_CANDIDATE_ID = re.compile(r"^candidate_[0-9a-f]{40}$")
_CANDIDATE_DRAFT_ID = re.compile(r"^candidate_draft_[0-9a-f]{32}$")
ExecutorFactory = Callable[[Path], ChapterCandidateExecutor]


class ChapterCandidateWorkbenchError(RuntimeError):
    code = "chapter_candidate_workbench_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class ChapterCandidateWorkbenchConflict(ChapterCandidateWorkbenchError):
    code = "chapter_candidate_conflict"


class ChapterCandidateWorkbenchNotFound(ChapterCandidateWorkbenchError):
    code = "chapter_candidate_not_found"


class ChapterCandidateWorkbenchIntegrity(ChapterCandidateWorkbenchError):
    code = "chapter_candidate_integrity_error"


@dataclass(frozen=True)
class CandidateDraftRecord:
    id: str
    project_id: str
    chapter: int
    candidate_id: str
    source_sha256: str
    source_artifact_ref: str
    text: str
    sha256: str
    revision: int
    state: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "chapter": self.chapter,
            "candidate_id": self.candidate_id,
            "source_sha256": self.source_sha256,
            "source_artifact_ref": self.source_artifact_ref,
            "text": self.text,
            "sha256": self.sha256,
            "revision": self.revision,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class CandidateDraftStore:
    """Editable Workbench copies, isolated from formal recovery drafts."""

    def __init__(self, database: WorkbenchDatabase) -> None:
        self.database = database
        self.database.initialize()
        with self.database.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chapter_candidate_drafts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL CHECK (chapter >= 1),
                    candidate_id TEXT NOT NULL UNIQUE,
                    source_sha256 TEXT NOT NULL,
                    source_artifact_ref TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL CHECK (state IN ('editing')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chapter_candidate_drafts_project_chapter
                ON chapter_candidate_drafts(project_id, chapter, updated_at DESC);
                """
            )

    def create_from_candidate(
        self,
        *,
        project_id: str,
        chapter: int,
        candidate_id: str,
        source_sha256: str,
        source_artifact_ref: str,
        text: str,
    ) -> CandidateDraftRecord:
        candidate_id = _validated_candidate_id(candidate_id)
        normalized = _validated_draft_text(text)
        digest = _sha256_text(normalized)
        if digest != source_sha256:
            raise ChapterCandidateWorkbenchError(
                "Candidate prose does not match its immutable source hash",
                code="chapter_candidate_integrity_error",
            )
        now = _utc_now()
        draft_id = f"candidate_draft_{uuid.uuid4().hex}"
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM chapter_candidate_drafts WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                record = _candidate_draft_from_row(existing)
                if (
                    record.project_id != project_id
                    or record.chapter != chapter
                    or record.source_sha256 != source_sha256
                    or record.source_artifact_ref != source_artifact_ref
                ):
                    raise ChapterCandidateWorkbenchConflict(
                        "Candidate editable draft source binding changed"
                    )
                return record
            connection.execute(
                """
                INSERT INTO chapter_candidate_drafts(
                    id, project_id, chapter, candidate_id, source_sha256,
                    source_artifact_ref, text, sha256, revision, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'editing', ?, ?)
                """,
                (
                    draft_id,
                    project_id,
                    chapter,
                    candidate_id,
                    source_sha256,
                    source_artifact_ref,
                    normalized,
                    digest,
                    now,
                    now,
                ),
            )
        return self.get(draft_id)

    def get(self, draft_id: str, *, connection: Any | None = None) -> CandidateDraftRecord:
        draft_id = _validated_candidate_draft_id(draft_id)
        if connection is None:
            with self.database.connection() as current:
                return self.get(draft_id, connection=current)
        else:
            row = connection.execute(
                "SELECT * FROM chapter_candidate_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            raise ChapterCandidateWorkbenchNotFound(
                "unknown Candidate editable draft",
                code="chapter_candidate_draft_not_found",
            )
        return _candidate_draft_from_row(row)

    def get_for_candidate(
        self,
        project_id: str,
        candidate_id: str,
    ) -> CandidateDraftRecord | None:
        candidate_id = _validated_candidate_id(candidate_id)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM chapter_candidate_drafts
                WHERE project_id = ? AND candidate_id = ?
                """,
                (project_id, candidate_id),
            ).fetchone()
        return _candidate_draft_from_row(row) if row is not None else None

    def update(
        self,
        draft_id: str,
        *,
        text: str,
        expected_revision: int,
    ) -> CandidateDraftRecord:
        draft_id = _validated_candidate_draft_id(draft_id)
        expected_revision = _validated_revision(expected_revision)
        normalized = _validated_draft_text(text)
        digest = _sha256_text(normalized)
        now = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM chapter_candidate_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise ChapterCandidateWorkbenchNotFound(
                    "unknown Candidate editable draft",
                    code="chapter_candidate_draft_not_found",
                )
            if int(row["revision"]) != expected_revision:
                raise ChapterCandidateWorkbenchConflict(
                    f"Candidate draft revision changed: expected {expected_revision}, current {row['revision']}"
                )
            updated = connection.execute(
                """
                UPDATE chapter_candidate_drafts
                SET text = ?, sha256 = ?, revision = revision + 1,
                    state = 'editing', updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (normalized, digest, now, draft_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise ChapterCandidateWorkbenchConflict(
                    "Candidate editable draft was modified concurrently"
                )
        return self.get(draft_id)


class ChapterCandidateService:
    """Expose recoverable prose Candidates without granting them formal authority."""

    def __init__(
        self,
        *,
        projects: ProjectRegistry,
        chapter_cards: ChapterCardService,
        database: WorkbenchDatabase,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self.projects = projects
        self.chapter_cards = chapter_cards
        self.drafts = CandidateDraftStore(database)
        self.executor_factory = executor_factory or (
            lambda root: ChapterCandidateExecutor(story_project_root=root)
        )

    def generate(
        self,
        project_id: str,
        chapter_index: int,
        *,
        confirmation: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id)
        key = _validated_idempotency_key(idempotency_key)
        verified = self.chapter_cards.verify_confirmation(
            project.id,
            chapter_index,
            confirmation,
        )
        with _candidate_error_boundary():
            executor = self.executor_factory(project.root)
            try:
                record = executor.execute(
                    chapter_index=chapter_index,
                    confirmation=verified,
                    idempotency_key=key,
                )
            except ChapterCandidateExecutionError as exc:
                record = exc.record
            return self._public_candidate(
                project.id,
                project.root,
                record["candidate_id"],
                import_completed=True,
                include_text=True,
            )

    def resume(
        self,
        project_id: str,
        chapter_index: int,
        candidate_id: str,
        *,
        expected_intent_hash: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterCandidateWorkbenchConflict(
                "explicit author confirmation is required to resume generation"
            )
        project = self.projects.get(project_id)
        with _candidate_error_boundary():
            store = ChapterCandidateStore(project.root)
            intent = _load_candidate_intent(store, candidate_id)
            _require_candidate_route(intent, project.book_id, chapter_index)
            if intent["intent_hash"] != _validated_sha256(
                expected_intent_hash,
                "expected_intent_hash",
            ):
                raise ChapterCandidateWorkbenchConflict(
                    "Candidate Intent changed before generation was resumed"
                )
            executor = self.executor_factory(project.root)
            try:
                record = executor.resume(candidate_id)
            except ChapterCandidateExecutionError as exc:
                record = exc.record
            return self._public_candidate(
                project.id,
                project.root,
                record["candidate_id"],
                import_completed=True,
                include_text=True,
            )

    def create_editable_draft(
        self,
        project_id: str,
        chapter_index: int,
        candidate_id: str,
        *,
        expected_prose_sha256: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterCandidateWorkbenchConflict(
                "explicit author confirmation is required to create an editable draft"
            )
        project = self.projects.get(project_id)
        with _candidate_error_boundary():
            store = ChapterCandidateStore(project.root)
            intent = _load_candidate_intent(store, candidate_id)
            _require_candidate_route(intent, project.book_id, chapter_index)
            if not store.has_record(candidate_id):
                raise ChapterCandidateWorkbenchConflict(
                    "Candidate generation has not finished"
                )
            record = store.load_record(candidate_id)
            if record["status"] != "completed":
                raise ChapterCandidateWorkbenchConflict(
                    "only a completed, current Candidate can become an editable draft"
                )
            expected = _validated_sha256(
                expected_prose_sha256,
                "expected_prose_sha256",
            )
            if record["prose"]["sha256"] != expected:
                raise ChapterCandidateWorkbenchConflict(
                    "Candidate prose changed before the editable draft was created"
                )
            return self._public_candidate(
                project.id,
                project.root,
                candidate_id,
                import_completed=True,
                include_text=True,
            )

    def update_editable_draft(
        self,
        draft_id: str,
        *,
        text: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        draft = self.drafts.update(
            draft_id,
            text=text,
            expected_revision=expected_revision,
        )
        public = _public_draft(draft, include_text=True)
        assert public is not None
        return public

    def abandon_provider_uncertainty(
        self,
        project_id: str,
        chapter_index: int,
        candidate_id: str,
        *,
        expected_intent_hash: str,
        expected_record_hash: str | None,
        expected_uncertainty_sha256: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id)
        with _candidate_error_boundary():
            store = ChapterCandidateStore(project.root)
            intent = _load_candidate_intent(store, candidate_id)
            _require_candidate_route(intent, project.book_id, chapter_index)
            self.executor_factory(project.root).abandon_provider_uncertainty(
                candidate_id,
                expected_intent_hash=expected_intent_hash,
                expected_record_hash=expected_record_hash,
                expected_uncertainty_sha256=expected_uncertainty_sha256,
                author_confirmed=author_confirmed,
            )
            return self._public_candidate(
                project.id,
                project.root,
                candidate_id,
                include_text=True,
            )

    def get(
        self,
        project_id: str,
        chapter_index: int,
        candidate_id: str,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id)
        with _candidate_error_boundary():
            store = ChapterCandidateStore(project.root)
            intent = _load_candidate_intent(store, candidate_id)
            _require_candidate_route(intent, project.book_id, chapter_index)
            return self._public_candidate(
                project.id,
                project.root,
                candidate_id,
                include_text=True,
            )

    def list_for_chapter(
        self,
        project_id: str,
        chapter_index: int,
    ) -> tuple[dict[str, Any], ...]:
        project = self.projects.get(project_id)
        with _candidate_error_boundary():
            store = ChapterCandidateStore(project.root)
            return tuple(
                self._public_candidate(
                    project.id,
                    project.root,
                    item["intent"]["candidate_id"],
                    include_text=False,
                )
                for item in store.list_for_chapter(
                    book_id=project.book_id,
                    chapter_index=chapter_index,
                )
            )

    def _public_candidate(
        self,
        project_id: str,
        project_root: Path,
        candidate_id: str,
        *,
        import_completed: bool = False,
        include_text: bool = False,
    ) -> dict[str, Any]:
        store = ChapterCandidateStore(project_root)
        intent = store.load_intent(candidate_id)
        record = store.load_record(candidate_id) if store.has_record(candidate_id) else None
        resolution = load_candidate_provider_resolution(
            store.paths.run_dir,
            candidate_id,
        )
        unresolved_calls = store.model_call_store(intent).list_uncertain_calls()
        if isinstance(record, Mapping):
            raw_status = record["status"]
        elif unresolved_calls:
            raw_status = "provider_uncertain"
        else:
            raw_status = "needs_recovery"
        effective_status = "abandoned" if resolution is not None else raw_status
        draft = self.drafts.get_for_candidate(
            project_id,
            candidate_id,
        )
        draft_import: dict[str, Any] = {"status": "not_available"}

        prose_available = bool(
            isinstance(record, Mapping)
            and record["prose"]["artifact_ref"] is not None
        )
        prose_text = (
            store.load_prose(candidate_id)
            if prose_available and (include_text or import_completed)
            else None
        )
        if import_completed and raw_status == "completed" and isinstance(prose_text, str):
            draft = self.drafts.create_from_candidate(
                project_id=project_id,
                chapter=int(intent["chapter_index"]),
                candidate_id=candidate_id,
                source_sha256=str(record["prose"]["sha256"]),
                source_artifact_ref=str(record["prose"]["artifact_ref"]),
                text=prose_text,
            )
            draft_import = {"status": "ready"}
        elif draft is not None:
            draft_import = {"status": "ready"}
        elif raw_status == "completed" and prose_available:
            draft_import = {"status": "available"}

        uncertainty: dict[str, Any] | None = None
        if unresolved_calls:
            uncertainty = {
                "count": len(unresolved_calls),
                "sha256": candidate_provider_uncertainty_sha256(
                    intent["execution"]["execution_id"],
                    unresolved_calls,
                ),
                "abandoned": resolution is not None,
            }

        error = record.get("error") if isinstance(record, Mapping) else None
        public_error = _public_error(error, raw_status)
        prose_payload: dict[str, Any] = {
            "available": prose_available,
            "sha256": record["prose"]["sha256"] if isinstance(record, Mapping) else None,
            "chars": record["prose"]["chars"] if isinstance(record, Mapping) else 0,
            "immutable": True,
        }
        if include_text:
            prose_payload["text"] = prose_text
        return {
            "candidate_id": candidate_id,
            "project_id": project_id,
            "chapter_index": int(intent["chapter_index"]),
            "status": effective_status,
            "raw_status": raw_status,
            "created_at": intent["created_at"],
            "finished_at": record.get("finished_at") if isinstance(record, Mapping) else None,
            "intent_hash": intent["intent_hash"],
            "record_hash": record.get("record_hash") if isinstance(record, Mapping) else None,
            "confirmation": {
                "card_sha256": intent["author_confirmation"]["card_sha256"],
                "context_sha256": intent["author_confirmation"]["context_sha256"],
                "confirmation_sha256": intent["author_confirmation"]["confirmation_sha256"],
            },
            "chapter_intent": copy.deepcopy(intent["chapter_intent"]),
            "prose": prose_payload,
            "draft_import": draft_import,
            "draft": _public_draft(draft, include_text=include_text),
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
            "error": public_error,
        }


def _validated_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ChapterCandidateWorkbenchError(
            "idempotency_key must be a safe non-empty identifier",
            code="chapter_candidate_request_invalid",
        )
    return value


def _validated_candidate_id(value: Any) -> str:
    text = str(value or "")
    if _CANDIDATE_ID.fullmatch(text) is None:
        raise ChapterCandidateWorkbenchError(
            "unsafe Candidate id",
            code="chapter_candidate_request_invalid",
        )
    return text


def _validated_candidate_draft_id(value: Any) -> str:
    text = str(value or "")
    if _CANDIDATE_DRAFT_ID.fullmatch(text) is None:
        raise ChapterCandidateWorkbenchError(
            "unsafe Candidate draft id",
            code="chapter_candidate_request_invalid",
        )
    return text


def _validated_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ChapterCandidateWorkbenchError(
            f"{label} must be a lowercase SHA-256 value",
            code="chapter_candidate_request_invalid",
        )
    return text


def _validated_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterCandidateWorkbenchError(
            "expected_revision must be a positive integer",
            code="chapter_candidate_request_invalid",
        )
    return value


def _validated_draft_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterCandidateWorkbenchError(
            "Candidate editable draft must contain prose",
            code="chapter_candidate_draft_invalid",
        )
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_draft_from_row(row: Any) -> CandidateDraftRecord:
    record = CandidateDraftRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        chapter=int(row["chapter"]),
        candidate_id=str(row["candidate_id"]),
        source_sha256=str(row["source_sha256"]),
        source_artifact_ref=str(row["source_artifact_ref"]),
        text=str(row["text"]),
        sha256=str(row["sha256"]),
        revision=int(row["revision"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
    if _CANDIDATE_DRAFT_ID.fullmatch(record.id) is None:
        raise ChapterCandidateWorkbenchIntegrity(
            "工作台候选草稿标识无效；已停止读取以避免覆盖。"
        )
    if _CANDIDATE_ID.fullmatch(record.candidate_id) is None:
        raise ChapterCandidateWorkbenchIntegrity(
            "工作台候选草稿的 Candidate 绑定无效；已停止读取以避免覆盖。"
        )
    if record.chapter < 1 or record.revision < 1 or record.state != "editing":
        raise ChapterCandidateWorkbenchIntegrity(
            "工作台候选草稿元数据无效；已停止读取以避免覆盖。"
        )
    if re.fullmatch(r"[0-9a-f]{64}", record.source_sha256) is None:
        raise ChapterCandidateWorkbenchIntegrity(
            "工作台候选草稿来源哈希无效；已停止读取以避免覆盖。"
        )
    expected_ref = f"candidates/{record.candidate_id}/draft.md"
    if record.source_artifact_ref != expected_ref:
        raise ChapterCandidateWorkbenchIntegrity(
            "工作台候选草稿来源绑定无效；已停止读取以避免覆盖。"
        )
    if _sha256_text(record.text) != record.sha256:
        raise ChapterCandidateWorkbenchIntegrity(
            "工作台候选草稿内容哈希不匹配；已停止读取以避免覆盖。"
        )
    return record


def _public_draft(
    draft: CandidateDraftRecord | None,
    *,
    include_text: bool,
) -> dict[str, Any] | None:
    if draft is None:
        return None
    payload = draft.to_dict()
    if not include_text:
        payload.pop("text", None)
    # The artifact reference is an internal storage concern, never a browser API.
    payload.pop("source_artifact_ref", None)
    return payload


def _load_candidate_intent(
    store: ChapterCandidateStore,
    candidate_id: str,
) -> dict[str, Any]:
    candidate_id = _validated_candidate_id(candidate_id)
    if not store.has_intent(candidate_id):
        raise ChapterCandidateWorkbenchNotFound("unknown Candidate")
    return store.load_intent(candidate_id)


@contextmanager
def _candidate_error_boundary() -> Iterator[None]:
    try:
        yield
    except ChapterCandidateWorkbenchError:
        raise
    except ProviderCallUncertainError as exc:
        raise ChapterCandidateWorkbenchConflict(
            "存在尚未处理的 Provider 不确定调用；系统不会自动重试。请先在对应候选中确认放弃，或恢复其证据。"
        ) from exc
    except (
        ChapterCandidateConflictError,
        ChapterCandidateProviderResolutionConflict,
    ) as exc:
        raise ChapterCandidateWorkbenchConflict(str(exc)) from exc
    except (
        ChapterCandidateIntegrityError,
        ChapterCandidateProviderResolutionError,
        ModelCallEvidenceError,
        OSError,
    ) as exc:
        raise ChapterCandidateWorkbenchIntegrity(
            "候选证据完整性校验失败；系统已停止读取、恢复和生成，以免覆盖证据。"
        ) from exc


def _require_candidate_route(
    intent: Mapping[str, Any],
    book_id: str,
    chapter_index: int,
) -> None:
    if (
        intent.get("book_id") != book_id
        or intent.get("chapter_index") != chapter_index
    ):
        raise ChapterCandidateWorkbenchConflict(
            "Candidate does not belong to this project chapter"
        )


def _public_error(
    error: Mapping[str, Any] | None,
    raw_status: str,
) -> dict[str, str] | None:
    if raw_status == "provider_uncertain":
        code = "provider_call_uncertain"
        message = (
            "Provider 结果不确定；系统已停止新的模型调用。正式正文和权威状态均未修改。"
        )
    elif raw_status == "needs_recovery":
        code = "candidate_needs_recovery"
        message = "候选已有持久 Intent，但没有终态记录；系统不会自动恢复或重试。"
    elif raw_status == "stale":
        code = str(
            (error.get("code") or "candidate_source_stale")
            if isinstance(error, Mapping)
            else "candidate_source_stale"
        )
        message = "生成期间可信输入发生变化；候选已隔离保留，不能作为当前可编辑草稿采用。"
    elif raw_status == "cancelled":
        code = str(
            (error.get("code") or "author_cancelled")
            if isinstance(error, Mapping)
            else "author_cancelled"
        )
        message = "正文候选已取消；正式正文和权威状态均未修改。"
    else:
        if not isinstance(error, Mapping):
            return None
        code = str(error.get("code") or "candidate_generation_failed")
        message = "正文候选生成失败；正式正文和权威状态均未修改。"
    return {"code": code, "message": message}


__all__ = [
    "CandidateDraftRecord",
    "CandidateDraftStore",
    "ChapterCandidateService",
    "ChapterCandidateWorkbenchConflict",
    "ChapterCandidateWorkbenchError",
    "ChapterCandidateWorkbenchIntegrity",
    "ChapterCandidateWorkbenchNotFound",
]
