from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import uuid

from core.story_project.paths import resolve_prose
from core.story_project.format_compatibility import assert_project_execution_compatible
from core.engine.locked_chapter import verified_final_recovery_draft
from core.engine.persistence_v2 import verify_publication_receipt
from core.engine.root_registry import RootRegistryService
from core.model_calls import load_model_call_intent, load_model_call_receipt
from core.runtime_paths import RuntimePaths
from workbench.database import WorkbenchDatabase
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for, MaintenanceBlocked, MaintenanceError
from workbench.review_status import resolve_review_status


TERMINAL_JOB_STATES = frozenset({"completed", "cancelled", "failed"})
PAUSED_JOB_STATES = frozenset({"awaiting_input", "needs_recovery"})
ACTIVE_JOB_STATES = frozenset({"starting", "running", "pause_requested"})
JOB_STATES = frozenset(
    {
        "held",
        "queued",
        *ACTIVE_JOB_STATES,
        *PAUSED_JOB_STATES,
        *TERMINAL_JOB_STATES,
    }
)
JOB_KINDS = frozenset({"generation_session", "locked_chapter_recovery"})
# A semantic execution contract, deliberately independent of Git commit strings.
# B0 rows have NULL; never stamp an old row with the current contract on upgrade.
CURRENT_JOB_EXECUTION_CONTRACT = "workbench_execution_v1"
PRE_PROVIDER_CONTEXT_BUDGET_ERROR = "pre_provider_context_budget_exceeded"
PRE_PROVIDER_PROVENANCE_ERROR = "pre_provider_provenance_unavailable"
REPAIR_NOT_CONVERGING_ERROR = "chapter_repair_not_converging"
STALE_SCENE_EVIDENCE_ERROR = "stale_scene_evidence"
_CONTEXT_BUDGET_PATTERN = re.compile(
    r"(?P<stage>[A-Za-z][A-Za-z0-9_-]*) input requires "
    r"(?P<required>[0-9]+) tokens; hard limit is (?P<hard>[0-9]+)"
)
_PROVENANCE_GIT_PATTERN = re.compile(
    r"core\.execution_provenance\.ProvenanceCaptureError: "
    r"Git (?P<scope>metadata|dirty-state) capture is unavailable"
)
_REPAIR_NOT_CONVERGING_PATTERN = re.compile(
    r"chapter_repair_not_converging: repair validation did not converge; "
    r"problem_counts=(?P<before>[0-9]+) -> (?P<after>[0-9]+); "
    r"reason=(?P<reason>[a-z][a-z0-9_]*); "
    r"repeated_problems=(?P<repeated>[^\r\n]+)"
)


def _formal_prose_exists(project_root: str | Path, chapter: int) -> bool:
    return resolve_prose(project_root, chapter).path is not None


class JobError(RuntimeError):
    """Raised when a workbench task transition would be unsafe."""


class ProcessMonitorError(JobError):
    """Execution outcome is unknown after local monitoring failed."""


class DraftSourceConflict(JobError):
    """Raised when a recovery source would overwrite an author-edited draft."""

    def __init__(
        self,
        *,
        draft_id: str,
        current_sha256: str,
        base_sha256: str,
        incoming_sha256: str,
    ) -> None:
        super().__init__("recovery source changed after the author edited the draft")
        self.draft_id = str(draft_id)
        self.current_sha256 = str(current_sha256)
        self.base_sha256 = str(base_sha256)
        self.incoming_sha256 = str(incoming_sha256)


@dataclass(frozen=True)
class JobRecord:
    id: str
    project_id: str
    kind: str
    state: str
    start_chapter: int
    end_chapter: int
    next_chapter: int
    config: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    manual_confirmation_required: bool
    pause_after_chapter: bool
    cancel_requested: bool
    revision: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    execution_contract: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "state": self.state,
            "start_chapter": self.start_chapter,
            "end_chapter": self.end_chapter,
            "next_chapter": self.next_chapter,
            "config": _json_copy(self.config),
            "result": _json_copy(self.result),
            "error": _json_copy(self.error),
            "manual_confirmation_required": self.manual_confirmation_required,
            "pause_after_chapter": self.pause_after_chapter,
            "cancel_requested": self.cancel_requested,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "execution_contract": self.execution_contract,
            "execution_compatible": self.execution_contract == CURRENT_JOB_EXECUTION_CONTRACT,
        }


@dataclass(frozen=True)
class DraftRecord:
    id: str
    project_id: str
    chapter: int
    source_run_id: str | None
    source_path: str | None
    base_sha256: str
    text: str
    sha256: str
    revision: int
    state: str
    validation: dict[str, Any] | None
    created_at: str
    updated_at: str

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "project_id": self.project_id,
            "chapter": self.chapter,
            "source_run_id": self.source_run_id,
            "source_path": self.source_path,
            "base_sha256": self.base_sha256,
            "sha256": self.sha256,
            "revision": self.revision,
            "state": self.state,
            "validation": _json_copy(self.validation),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_text:
            payload["text"] = self.text
        return payload


class JobStore:
    """Durable task, event and editable-draft state for the local UI."""

    def __init__(self, database: WorkbenchDatabase) -> None:
        self.database = database
        self.database.initialize()
        self.database.initialize_component(self.initialize)

    def initialize(self) -> None:
        with self.database.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    start_chapter INTEGER NOT NULL CHECK (start_chapter >= 1),
                    end_chapter INTEGER NOT NULL CHECK (end_chapter >= start_chapter),
                    next_chapter INTEGER NOT NULL CHECK (next_chapter >= 1),
                    config_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    manual_confirmation_required INTEGER NOT NULL DEFAULT 0,
                    pause_after_chapter INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_project_created
                ON jobs(project_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_jobs_state_created
                ON jobs(state, created_at);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, sequence),
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_job_events_job_id
                ON job_events(job_id, id);

                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL CHECK (chapter >= 1),
                    source_run_id TEXT,
                    source_path TEXT,
                    base_sha256 TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    validation_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, chapter),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                """
            )

            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "execution_contract" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN execution_contract TEXT")

    def quarantine_incompatible_jobs(self) -> int:
        """Retain original config/result/error; remove unsupported rows from scheduling."""
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM jobs WHERE (execution_contract IS NULL OR execution_contract != ?)
                   AND state NOT IN ('completed', 'cancelled', 'failed')
                   AND (state != 'needs_recovery' OR manual_confirmation_required = 0)""",
                (CURRENT_JOB_EXECUTION_CONTRACT,),
            ).fetchall()
            for row in rows:
                if row["state"] in ACTIVE_JOB_STATES:
                    # The caller must establish process ownership and settle restart
                    # uncertainty first. A database edit cannot stop a live writer.
                    raise JobError("在途执行尚未停止，不能隔离或升级任务。")
                connection.execute(
                    """UPDATE jobs SET state = 'needs_recovery', manual_confirmation_required = 1,
                       updated_at = ?, revision = revision + 1 WHERE id = ?""",
                    (timestamp, row["id"]),
                )
                self._append_event_in_transaction(
                    connection, str(row["id"]), "job.execution_contract_incompatible",
                    {"previous_state": row["state"], "stored_contract": row["execution_contract"],
                     "required_contract": CURRENT_JOB_EXECUTION_CONTRACT, "automatic_retry": False},
                    timestamp=timestamp)
        return len(rows)

    def recover_after_restart(self) -> int:
        """Fail closed: a dead web process must never replay a provider call."""

        timestamp = _utc_now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE state IN ('starting', 'running', 'pause_requested')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'needs_recovery', manual_confirmation_required = 1,
                        error_json = ?, updated_at = ?, revision = revision + 1
                    WHERE id = ?
                    """,
                    (
                        _canonical_json(
                            {
                                "code": "workbench_restarted_during_provider_operation",
                                "message": (
                                    "服务在任务运行期间退出；Provider 状态必须先恢复确认，"
                                    "工作台不会自动重试。"
                                ),
                            }
                        ),
                        timestamp,
                        row["id"],
                    ),
                )
                self._append_event_in_transaction(
                    connection,
                    str(row["id"]),
                    "job.needs_recovery",
                    {"reason": "service_restart", "automatic_retry": False},
                    timestamp=timestamp,
                )
        return len(rows)

    def reconcile_committed_after_restart(
        self,
        job_id: str,
        *,
        chapter: int,
        result: Mapping[str, Any],
        verification: Mapping[str, Any],
    ) -> JobRecord:
        """Advance a receipt-verified commit without authorizing another call."""

        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            if row["state"] != "needs_recovery":
                raise JobError("restart reconciliation requires needs_recovery state")
            if int(row["next_chapter"]) != int(chapter):
                raise JobError("restart reconciliation chapter does not match the job cursor")
            next_chapter = int(chapter) + 1
            finished = next_chapter > int(row["end_chapter"])
            connection.execute(
                """
                UPDATE jobs SET state = ?, next_chapter = ?, result_json = ?,
                    error_json = NULL, manual_confirmation_required = ?,
                    updated_at = ?, finished_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'needs_recovery'
                """,
                (
                    "completed" if finished else "awaiting_input",
                    next_chapter,
                    _canonical_json(_compact_result(result)),
                    0 if finished else 1,
                    timestamp,
                    timestamp if finished else None,
                    str(job_id),
                ),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "chapter.reconciled_after_restart",
                {
                    "chapter": chapter,
                    "run_id": _run_id(result),
                    "verification": _safe_event_payload(verification),
                    "automatic_provider_calls": 0,
                    "author_confirmation_required": not finished,
                },
                timestamp=timestamp,
            )
        record = self.get(job_id)
        self.archive_recovery_draft_for_committed_chapter(record.project_id, chapter)
        return record

    def create_job(
        self,
        *,
        project_id: str,
        kind: str,
        start_chapter: int,
        end_chapter: int,
        config: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        held: bool = False,
    ) -> JobRecord:
        if kind not in JOB_KINDS:
            raise JobError(f"unsupported job kind: {kind}")
        if isinstance(start_chapter, bool) or not isinstance(start_chapter, int) or start_chapter < 1:
            raise JobError("start_chapter must be a positive integer")
        if isinstance(end_chapter, bool) or not isinstance(end_chapter, int) or end_chapter < start_chapter:
            raise JobError("end_chapter must be at least start_chapter")
        safe_config = _safe_job_config(config or {})
        job_id = _job_id(
            project_id=str(project_id),
            kind=kind,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            config=safe_config,
            idempotency_key=idempotency_key,
        )
        initial_state = "held" if held else "queued"
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone() is None:
                raise JobError(f"unknown workbench project: {project_id}")
            prior = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if prior is not None:
                record = _job_from_row(prior)
                if (
                    record.project_id != str(project_id)
                    or record.kind != kind
                    or record.start_chapter != start_chapter
                    or record.end_chapter != end_chapter
                    or record.config != safe_config
                ):
                    raise JobError("idempotency key was already used for another job")
                return record
            parent_id = safe_config.get("parent_job_id")
            if parent_id is not None:
                parent = _require_job_row(connection, str(parent_id))
                _require_job_execution_contract(parent)
                if parent["project_id"] != str(project_id):
                    raise JobError("recovery parent belongs to another project")
            if kind == "generation_session":
                existing = connection.execute(
                    """
                    SELECT id, state FROM jobs
                    WHERE project_id = ? AND kind = 'generation_session'
                      AND state NOT IN ('completed', 'cancelled', 'failed')
                    LIMIT 1
                    """,
                    (str(project_id),),
                ).fetchone()
                if existing is not None:
                    raise JobError(
                        f"project already has unfinished session {existing['id']} ({existing['state']})"
                    )
            else:
                existing = connection.execute(
                    """
                    SELECT id, state FROM jobs
                    WHERE project_id = ? AND kind = 'locked_chapter_recovery'
                      AND start_chapter = ?
                      AND state NOT IN ('completed', 'cancelled', 'failed')
                    LIMIT 1
                    """,
                    (str(project_id), int(start_chapter)),
                ).fetchone()
                if existing is not None:
                    raise JobError(
                        f"chapter already has unfinished recovery {existing['id']} ({existing['state']})"
                    )
            connection.execute(
                """
                INSERT INTO jobs(
                    id, project_id, kind, state, start_chapter, end_chapter,
                    next_chapter, config_json, created_at, updated_at, execution_contract
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(project_id),
                    kind,
                    initial_state,
                    start_chapter,
                    end_chapter,
                    start_chapter,
                    _canonical_json(safe_config),
                    timestamp,
                    timestamp,
                    CURRENT_JOB_EXECUTION_CONTRACT,
                ),
            )
            self._append_event_in_transaction(
                connection,
                job_id,
                "job.held" if held else "job.queued",
                {
                    "kind": kind,
                    "start_chapter": start_chapter,
                    "end_chapter": end_chapter,
                    "requires_release": bool(held),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def release_held(self, job_id: str) -> JobRecord:
        """Make a fully bound cohort job visible to the worker."""

        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            _require_job_execution_contract(row)
            if row["state"] != "held":
                return _job_from_row(row)
            if bool(row["cancel_requested"]):
                raise JobError("cancelled held job cannot be released")
            updated = connection.execute(
                """
                UPDATE jobs SET state = 'queued', updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'held'
                """,
                (timestamp, str(job_id)),
            )
            if updated.rowcount != 1:
                raise JobError("held job changed before release")
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "job.queued",
                {
                    "start_chapter": int(row["start_chapter"]),
                    "end_chapter": int(row["end_chapter"]),
                    "released_after_binding": True,
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def get(self, job_id: str) -> JobRecord:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (str(job_id),)).fetchone()
        if row is None:
            raise JobError(f"unknown job: {job_id}")
        self.database.require_project_access(row['project_id'])
        return _job_from_row(row)

    def stream_snapshot(self, job_id: str) -> tuple[JobRecord, int]:
        """Read a job and its event cursor from one SQLite snapshot."""

        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT jobs.*,
                       COALESCE((
                           SELECT MAX(job_events.id)
                           FROM job_events
                           WHERE job_events.job_id = jobs.id
                       ), 0) AS last_event_id
                FROM jobs
                WHERE jobs.id = ?
                """,
                (str(job_id),),
            ).fetchone()
        if row is None:
            raise JobError(f"unknown job: {job_id}")
        self.database.require_project_access(row['project_id'])
        return _job_from_row(row), int(row["last_event_id"])

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        states: Iterable[str] | None = None,
        limit: int = 50,
    ) -> tuple[JobRecord, ...]:
        if self.database.restore_scope is not None:
            if project_id is not None:
                self.database.require_project_access(project_id)
            project_id = self.database.restore_scope.project_id
        bounded_limit = max(1, min(int(limit), 200))
        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(str(project_id))
        normalized_states = tuple(dict.fromkeys(str(item) for item in (states or ())))
        if normalized_states:
            if any(item not in JOB_STATES for item in normalized_states):
                raise JobError("invalid job state filter")
            clauses.append("state IN (" + ",".join("?" for _ in normalized_states) + ")")
            values.extend(normalized_states)
        query = "SELECT * FROM jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        values.append(bounded_limit)
        with self.database.connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def request_pause(self, job_id: str) -> JobRecord:
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            if row["state"] in TERMINAL_JOB_STATES | PAUSED_JOB_STATES:
                return _job_from_row(row)
            state = "pause_requested" if row["state"] in ACTIVE_JOB_STATES else row["state"]
            connection.execute(
                """
                UPDATE jobs SET state = ?, pause_after_chapter = 1,
                    updated_at = ?, revision = revision + 1 WHERE id = ?
                """,
                (state, timestamp, str(job_id)),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "job.pause_requested",
                {"finish_current_chapter": True},
                timestamp=timestamp,
            )
        return self.get(job_id)

    def pause_for_maintenance(self, job_id: str, *, chapter: int, operation_id: str) -> JobRecord:
        """Yield a claimed, settled chapter boundary without losing its receipt.

        The worker must still own admission and have proven its child exited.
        A running/uncertain chapter cannot be converted into this safe pause.
        """
        if not isinstance(operation_id,str) or re.fullmatch(r'[0-9a-f]{32}',operation_id) is None:
            raise JobError('maintenance operation identity is invalid')
        timestamp=_utc_now()
        with self.database.transaction() as connection:
            row=_require_job_row(connection,job_id)
            _require_job_execution_contract(row)
            if row['state'] in TERMINAL_JOB_STATES | PAUSED_JOB_STATES:
                return _job_from_row(row)
            if (row['kind']!='generation_session' or row['state'] not in {'starting','pause_requested'}
                    or row['next_chapter']!=chapter or row['error_json'] is not None):
                raise JobError('maintenance pause requires a settled chapter boundary')
            cancelled=bool(row['cancel_requested'])
            connection.execute('''UPDATE jobs SET state=?,manual_confirmation_required=?,
                pause_after_chapter=0,updated_at=?,finished_at=?,revision=revision+1 WHERE id=?''',
                ('cancelled' if cancelled else 'awaiting_input',0 if cancelled else 1,
                 timestamp,timestamp if cancelled else None,job_id))
            self._append_event_in_transaction(connection,job_id,'job.cancelled' if cancelled else 'job.paused',
                {'reason':'workbench_maintenance','operation_id':operation_id,'chapter':chapter,
                 'completed_chapters':chapter-int(row['start_chapter']),'automatic_resume':False},timestamp=timestamp)
        return self.get(job_id)

    def request_cancel(self, job_id: str) -> JobRecord:
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            if row["state"] in TERMINAL_JOB_STATES:
                return _job_from_row(row)
            if row["state"] in {"held", "queued"}:
                next_state = "cancelled"
                finished_at = timestamp
                manual = 0
            elif row["state"] in PAUSED_JOB_STATES:
                next_state = "cancelled"
                finished_at = timestamp
                manual = int(row["manual_confirmation_required"])
            else:
                next_state = row["state"]
                finished_at = None
                manual = 1
            connection.execute(
                """
                UPDATE jobs SET state = ?, cancel_requested = 1,
                    manual_confirmation_required = ?, updated_at = ?, finished_at = ?,
                    revision = revision + 1 WHERE id = ?
                """,
                (next_state, manual, timestamp, finished_at, str(job_id)),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "job.cancel_requested",
                {"provider_state_may_be_uncertain": next_state != "cancelled"},
                timestamp=timestamp,
            )
        return self.get(job_id)

    def resume(self, job_id: str, *, confirmed: bool) -> JobRecord:
        if not confirmed:
            raise JobError("resume requires explicit author confirmation")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            _require_job_execution_contract(row)
            if row["kind"] != "generation_session":
                raise JobError("only generation sessions can be resumed")
            if row["state"] not in PAUSED_JOB_STATES:
                raise JobError(f"job is not awaiting author action: {row['state']}")
            error = _stored_job_error(row)
            if _is_pre_provider_context_budget_error(error):
                if not _has_pre_provider_retry_evidence(connection, row):
                    raise JobError(
                        "pre-provider retry evidence is incomplete or changed; recovery remains required"
                    )
            elif _is_pre_provider_provenance_error(error):
                if not _has_pre_provider_provenance_retry_evidence(connection, row):
                    raise JobError(
                        "pre-provider provenance evidence changed or Git preflight is unavailable"
                    )
            else:
                recovery_required = row["state"] == "needs_recovery" or row["error_json"] is not None
                if recovery_required and not _has_completed_recovery_evidence(connection, row):
                    raise JobError(
                        "this chapter requires a completed, receipt-backed recovery before it can resume"
                    )
            connection.execute(
                """
                UPDATE jobs SET state = 'queued', manual_confirmation_required = 0,
                    pause_after_chapter = 0, cancel_requested = 0, error_json = NULL,
                    updated_at = ?, finished_at = NULL, revision = revision + 1
                WHERE id = ?
                """,
                (timestamp, str(job_id)),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "job.author_confirmed_resume",
                {
                    "chapter": int(row["next_chapter"]),
                    "reason": (
                        PRE_PROVIDER_CONTEXT_BUDGET_ERROR
                        if _is_pre_provider_context_budget_error(error)
                        else PRE_PROVIDER_PROVENANCE_ERROR
                        if _is_pre_provider_provenance_error(error)
                        else "receipt_backed_recovery"
                    ),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def resume_ready(self, job_id: str) -> bool:
        """Report whether a paused session can pass the same evidence gate as resume()."""

        with self.database.connection() as connection:
            row = _require_job_row(connection, job_id)
            if row["execution_contract"] != CURRENT_JOB_EXECUTION_CONTRACT:
                return False
            if row["kind"] != "generation_session" or row["state"] not in PAUSED_JOB_STATES:
                return False
            error = _stored_job_error(row)
            if _is_pre_provider_context_budget_error(error):
                return _has_pre_provider_retry_evidence(connection, row)
            if _is_pre_provider_provenance_error(error):
                return _has_pre_provider_provenance_retry_evidence(connection, row)
            recovery_required = row["state"] == "needs_recovery" or row["error_json"] is not None
            return not recovery_required or _has_completed_recovery_evidence(connection, row)

    def reconcile_pre_provider_context_failure(
        self,
        job_id: str,
        *,
        result: Mapping[str, Any],
        error: Mapping[str, Any],
    ) -> JobRecord:
        """Upgrade one legacy generic pause without authorizing another provider call."""

        if not _is_pre_provider_context_budget_error(error):
            raise JobError("pre-provider context reconciliation requires structured evidence")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            current_error = _stored_job_error(row)
            if (
                row["kind"] != "generation_session"
                or row["state"] != "awaiting_input"
                or current_error.get("code") != "engine_result_unreadable"
            ):
                raise JobError("job is not a legacy unreadable-engine pause")
            cursor = connection.execute(
                """
                UPDATE jobs SET result_json = ?, error_json = ?,
                    manual_confirmation_required = 1, updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'awaiting_input' AND revision = ?
                """,
                (
                    _canonical_json(_compact_result(result)),
                    _canonical_json(_safe_event_payload(error)),
                    timestamp,
                    str(job_id),
                    int(row["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("legacy context-budget reconciliation CAS failed")
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "chapter.pre_provider_context_budget_exceeded",
                {
                    "chapter": int(row["next_chapter"]),
                    "automatic_retry": False,
                    "reconciled_from": "engine_result_unreadable",
                    "error": _safe_event_payload(error),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def reconcile_pre_provider_provenance_failure(
        self,
        job_id: str,
        *,
        error: Mapping[str, Any],
        expected_legacy_error_sha256: str,
    ) -> JobRecord:
        """Atomically upgrade one proven legacy pre-provider Git failure.

        Reconciliation only changes the durable explanation and manual-action
        gate.  It never queues the task or invokes the engine.
        """

        if not _is_pre_provider_provenance_error(error):
            raise JobError("pre-provider provenance reconciliation requires structured evidence")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            current_error = _stored_job_error(row)
            current_error_sha256 = _sha256_text(_canonical_json(current_error))
            if (
                row["kind"] != "generation_session"
                or row["state"] != "awaiting_input"
                or row["result_json"] is not None
                or current_error.get("code") != "engine_result_unreadable"
                or current_error_sha256 != expected_legacy_error_sha256
            ):
                raise JobError("job is not the unchanged legacy provenance pause")
            cursor = connection.execute(
                """
                UPDATE jobs SET error_json = ?, manual_confirmation_required = 1,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'awaiting_input' AND revision = ?
                    AND result_json IS NULL AND error_json = ?
                """,
                (
                    _canonical_json(_safe_event_payload(error)),
                    timestamp,
                    str(job_id),
                    int(row["revision"]),
                    str(row["error_json"]),
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("legacy provenance reconciliation CAS failed")
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "chapter.pre_provider_provenance_unavailable",
                {
                    "chapter": int(row["next_chapter"]),
                    "automatic_retry": False,
                    "reconciled_from": "engine_result_unreadable",
                    "error": _safe_event_payload(error),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def reconcile_repair_not_converging_failure(
        self,
        job_id: str,
        *,
        result: Mapping[str, Any],
        error: Mapping[str, Any],
        expected_legacy_error_sha256: str,
    ) -> JobRecord:
        """Upgrade one unchanged generic pause into a durable recovery lane.

        This transition deliberately leaves the session paused.  It records
        what happened, but cannot authorize another Provider call; resuming is
        still guarded by a completed recovery bound to this failure revision.
        """

        if not _is_repair_not_converging_error(error):
            raise JobError("repair convergence reconciliation requires structured evidence")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            current_error = _stored_job_error(row)
            current_error_sha256 = _sha256_text(_canonical_json(current_error))
            if (
                row["kind"] != "generation_session"
                or row["state"] != "awaiting_input"
                or row["result_json"] is not None
                or current_error.get("code") != "engine_result_unreadable"
                or current_error_sha256 != expected_legacy_error_sha256
            ):
                raise JobError("job is not the unchanged legacy repair-convergence pause")
            cursor = connection.execute(
                """
                UPDATE jobs SET result_json = ?, error_json = ?,
                    manual_confirmation_required = 1, updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'awaiting_input' AND revision = ?
                    AND result_json IS NULL AND error_json = ?
                """,
                (
                    _canonical_json(_compact_result(result)),
                    _canonical_json(_safe_event_payload(error)),
                    timestamp,
                    str(job_id),
                    int(row["revision"]),
                    str(row["error_json"]),
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("legacy repair-convergence reconciliation CAS failed")
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "chapter.repair_not_converging",
                {
                    "chapter": int(row["next_chapter"]),
                    "automatic_retry": False,
                    "reconciled_from": "engine_result_unreadable",
                    "error": _safe_event_payload(error),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def reconcile_stale_scene_evidence_failure(
        self,
        job_id: str,
        *,
        result: Mapping[str, Any],
        error: Mapping[str, Any],
        expected_legacy_error_sha256: str,
    ) -> JobRecord:
        """Upgrade one unchanged generic pause into a durable recovery lane.

        This transition deliberately leaves the session paused.  It records
        what happened, but cannot authorize another Provider call; resuming is
        still guarded by a completed recovery bound to this failure revision.
        """

        if not _is_stale_scene_evidence_error(error):
            raise JobError("stale scene evidence reconciliation requires structured evidence")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            current_error = _stored_job_error(row)
            current_error_sha256 = _sha256_text(_canonical_json(current_error))
            if (
                row["kind"] != "generation_session"
                or row["state"] != "awaiting_input"
                or row["result_json"] is not None
                or current_error.get("code") != "engine_result_unreadable"
                or current_error_sha256 != expected_legacy_error_sha256
            ):
                raise JobError("job is not the unchanged legacy stale-scene-evidence pause")
            cursor = connection.execute(
                """
                UPDATE jobs SET result_json = ?, error_json = ?,
                    manual_confirmation_required = 1, updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'awaiting_input' AND revision = ?
                    AND result_json IS NULL AND error_json = ?
                """,
                (
                    _canonical_json(_compact_result(result)),
                    _canonical_json(_safe_event_payload(error)),
                    timestamp,
                    str(job_id),
                    int(row["revision"]),
                    str(row["error_json"]),
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("legacy stale-scene-evidence reconciliation CAS failed")
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "chapter.stale_scene_evidence",
                {
                    "chapter": int(row["next_chapter"]),
                    "automatic_retry": False,
                    "reconciled_from": "engine_result_unreadable",
                    "error": _safe_event_payload(error),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def reconcile_review_pause(
        self,
        job_id: str,
        *,
        run_id: str,
        review_status: str,
    ) -> JobRecord:
        """Correct an uncertainty false-positive without authorizing another call."""

        if review_status not in {"needs_revision", "blocked", "error"}:
            raise JobError("review pause reconciliation requires a manual review status")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            if row["kind"] != "generation_session" or row["state"] != "needs_recovery":
                raise JobError("review pause reconciliation requires an uncertain generation session")
            error = json.loads(str(row["error_json"])) if row["error_json"] else {}
            result = json.loads(str(row["result_json"])) if row["result_json"] else {}
            if error.get("code") != "chapter_not_safe_to_continue":
                raise JobError("review pause reconciliation requires a rejected chapter result")
            if result.get("run_id") != run_id or result.get("review_status") != review_status:
                raise JobError("review pause reconciliation evidence changed")
            connection.execute(
                """
                UPDATE jobs SET state = 'awaiting_input', manual_confirmation_required = 1,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'needs_recovery'
                """,
                (timestamp, str(job_id)),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "job.review_pause_reclassified",
                {
                    "chapter": int(row["next_chapter"]),
                    "run_id": run_id,
                    "review_status": review_status,
                    "automatic_retry": False,
                    "reason": "current_run_has_no_provider_uncertainty",
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def claim_next(self) -> JobRecord | None:
        if self.database.restore_scope is not None:
            return None
        self.quarantine_incompatible_jobs()
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM jobs WHERE state IN ('starting','running','pause_requested') LIMIT 1"
            ).fetchone()
            if active is not None:
                return None
            row = connection.execute(
                "SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE jobs SET state = 'starting', started_at = COALESCE(started_at, ?),
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'queued'
                """,
                (timestamp, timestamp, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            self._append_event_in_transaction(
                connection,
                str(row["id"]),
                "job.starting",
                {"chapter": int(row["next_chapter"])},
                timestamp=timestamp,
            )
        return self.get(str(row["id"]))

    def mark_running(self, job_id: str, *, chapter: int, command: Sequence[str]) -> JobRecord:
        # Only the option names are persisted. Paths and credentials are deliberately absent.
        public_command = [Path(command[0]).name, Path(command[1]).name, *[
            item for item in command[2:] if not _looks_like_absolute_path(item)
        ]]
        return self._transition(
            job_id,
            allowed={"starting", "pause_requested"},
            state="running",
            event_type="chapter.started",
            event_payload={"chapter": chapter, "command": public_command},
        )

    def record_progress_event(self, job_id: str, event: Mapping[str, Any]) -> int:
        safe_event = _safe_event_payload(event)
        event_type = str(safe_event.get("event") or safe_event.get("stage") or "engine.progress")
        return self.append_event(job_id, event_type, safe_event)

    def chapter_succeeded(
        self,
        job_id: str,
        *,
        chapter: int,
        result: Mapping[str, Any],
        verification: Mapping[str, Any],
        next_project_state_head: str | None = None,
    ) -> JobRecord:
        timestamp = _utc_now()
        safe_result = _compact_result(result)
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            next_chapter = chapter + 1
            finished = next_chapter > int(row["end_chapter"])
            pause = bool(row["pause_after_chapter"])
            next_config = json.loads(str(row["config_json"]))
            if next_project_state_head is not None:
                next_config["project_state_head"] = str(next_project_state_head)
            if finished:
                state = "completed"
                finished_at = timestamp
            elif pause:
                state = "awaiting_input"
                finished_at = None
            else:
                state = "starting"
                finished_at = None
            connection.execute(
                """
                UPDATE jobs SET state = ?, next_chapter = ?, result_json = ?, config_json = ?,
                    manual_confirmation_required = ?, pause_after_chapter = 0,
                    updated_at = ?, finished_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (
                    state,
                    next_chapter,
                    _canonical_json(safe_result),
                    _canonical_json(next_config),
                    1 if pause and not finished else 0,
                    timestamp,
                    finished_at,
                    str(job_id),
                ),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                "chapter.committed",
                {
                    "chapter": chapter,
                    "run_id": _run_id(result),
                    "review_status": _review_status(result),
                    "verification": _safe_event_payload(verification),
                },
                timestamp=timestamp,
            )
            if state == "completed":
                self._append_event_in_transaction(
                    connection,
                    str(job_id),
                    "job.completed",
                    {"completed_chapters": int(row["end_chapter"]) - int(row["start_chapter"]) + 1},
                    timestamp=timestamp,
                )
            elif state == "awaiting_input":
                self._append_event_in_transaction(
                    connection,
                    str(job_id),
                    "job.paused",
                    {"reason": "author_requested_after_chapter", "next_chapter": next_chapter},
                    timestamp=timestamp,
                )
        record = self.get(job_id)
        self.archive_recovery_draft_for_committed_chapter(record.project_id, chapter)
        return record

    def chapter_failed(
        self,
        job_id: str,
        *,
        chapter: int,
        error: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        uncertain: bool = False,
    ) -> JobRecord:
        timestamp = _utc_now()
        state = "needs_recovery" if uncertain else "awaiting_input"
        with self.database.transaction() as connection:
            _require_job_row(connection, job_id)
            connection.execute(
                """
                UPDATE jobs SET state = ?, result_json = ?, error_json = ?,
                    manual_confirmation_required = 1, updated_at = ?,
                    revision = revision + 1 WHERE id = ?
                """,
                (
                    state,
                    _canonical_json(_compact_result(result)) if result is not None else None,
                    _canonical_json(_safe_event_payload(error)),
                    timestamp,
                    str(job_id),
                ),
            )
            event_type = (
                "chapter.pre_provider_context_budget_exceeded"
                if _is_pre_provider_context_budget_error(error)
                else "chapter.pre_provider_provenance_unavailable"
                if _is_pre_provider_provenance_error(error)
                else "chapter.repair_not_converging"
                if _is_repair_not_converging_error(error)
                else "chapter.stale_scene_evidence"
                if _is_stale_scene_evidence_error(error)
                else "chapter.needs_recovery" if uncertain else "chapter.needs_revision"
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                event_type,
                {
                    "chapter": chapter,
                    "automatic_retry": False,
                    "error": _safe_event_payload(error),
                },
                timestamp=timestamp,
            )
        return self.get(job_id)

    def recovery_succeeded(self, job_id: str, result: Mapping[str, Any]) -> JobRecord:
        return self._transition(
            job_id,
            allowed={"running", "starting"},
            state="completed",
            event_type="recovery.completed",
            event_payload=_compact_result(result),
            result=result,
            finish=True,
        )

    def fail_job(self, job_id: str, error: Mapping[str, Any]) -> JobRecord:
        return self._transition(
            job_id,
            allowed=JOB_STATES - TERMINAL_JOB_STATES,
            state="failed",
            event_type="job.failed",
            event_payload={"error": _safe_event_payload(error)},
            error=error,
            finish=True,
        )

    def mark_cancelled_uncertain(self, job_id: str, *, chapter: int) -> JobRecord:
        return self._transition(
            job_id,
            allowed=ACTIVE_JOB_STATES,
            state="needs_recovery",
            event_type="job.cancelled_provider_uncertain",
            event_payload={
                "chapter": chapter,
                "automatic_retry": False,
                "message": "进程已紧急停止；必须先检查 durable Intent/Receipt。",
            },
            error={
                "code": "provider_state_uncertain_after_emergency_stop",
                "chapter": chapter,
            },
            manual=True,
        )

    def append_event(self, job_id: str, event_type: str, payload: Mapping[str, Any]) -> int:
        with self.database.transaction() as connection:
            _require_job_row(connection, job_id)
            return self._append_event_in_transaction(
                connection, str(job_id), event_type, payload, timestamp=_utc_now()
            )

    def events_after(self, job_id: str, *, after_id: int = 0, limit: int = 200) -> tuple[dict[str, Any], ...]:
        bounded_limit = max(1, min(int(limit), 1000))
        with self.database.connection() as connection:
            _require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT id, sequence, event_type, payload_json, created_at
                FROM job_events WHERE job_id = ? AND id > ?
                ORDER BY id LIMIT ?
                """,
                (str(job_id), max(0, int(after_id)), bounded_limit),
            ).fetchall()
        return tuple(
            {
                "id": int(row["id"]),
                "sequence": int(row["sequence"]),
                "event": str(row["event_type"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        )

    def upsert_draft(
        self,
        *,
        project_id: str,
        chapter: int,
        text: str,
        source_run_id: str | None = None,
        source_path: str | None = None,
    ) -> DraftRecord:
        normalized = _validated_draft_text(text)
        digest = _sha256_text(normalized)
        timestamp = _utc_now()
        draft_id = f"draft_{uuid.uuid4().hex}"
        with self.database.transaction() as connection:
            project_root = self._project_root(str(project_id), connection)
            if _formal_prose_exists(project_root, int(chapter)):
                raise JobError(
                    f"chapter {int(chapter)} already has formal committed prose"
                )
            existing = connection.execute(
                "SELECT * FROM drafts WHERE project_id = ? AND chapter = ?",
                (str(project_id), int(chapter)),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO drafts(
                        id, project_id, chapter, source_run_id, source_path,
                        base_sha256, text, sha256, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'editing', ?, ?)
                    """,
                    (
                        draft_id,
                        str(project_id),
                        int(chapter),
                        source_run_id,
                        source_path,
                        digest,
                        normalized,
                        digest,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                draft_id = str(existing["id"])
                current_sha256 = str(existing["sha256"])
                base_sha256 = str(existing["base_sha256"])
                if current_sha256 != base_sha256 and digest != current_sha256:
                    raise DraftSourceConflict(
                        draft_id=draft_id,
                        current_sha256=current_sha256,
                        base_sha256=base_sha256,
                        incoming_sha256=digest,
                    )
                desired_source_run_id = (
                    source_run_id
                    if source_run_id is not None
                    else existing["source_run_id"]
                )
                desired_source_path = (
                    source_path
                    if source_path is not None
                    else existing["source_path"]
                )
                if (
                    digest == current_sha256
                    and digest == base_sha256
                    and desired_source_run_id == existing["source_run_id"]
                    and desired_source_path == existing["source_path"]
                ):
                    return _draft_from_row(existing)
                connection.execute(
                    """
                    UPDATE drafts SET base_sha256 = ?, text = ?, sha256 = ?,
                        source_run_id = ?, source_path = ?, state = 'editing',
                        validation_json = NULL, updated_at = ?, revision = revision + 1
                    WHERE id = ?
                    """,
                    (
                        digest,
                        normalized,
                        digest,
                        desired_source_run_id,
                        desired_source_path,
                        timestamp,
                        draft_id,
                    ),
                )
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> DraftRecord:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM drafts WHERE id = ?", (str(draft_id),)).fetchone()
        if row is None:
            raise JobError(f"unknown draft: {draft_id}")
        return _draft_from_row(row)

    def get_project_chapter_draft(self, project_id: str, chapter: int) -> DraftRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE project_id = ? AND chapter = ?",
                (str(project_id), int(chapter)),
            ).fetchone()
        return _draft_from_row(row) if row is not None else None

    def update_draft(self, draft_id: str, *, text: str, expected_revision: int) -> DraftRecord:
        normalized = _validated_draft_text(text)
        digest = _sha256_text(normalized)
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM drafts WHERE id = ?", (str(draft_id),)).fetchone()
            if row is None:
                raise JobError(f"unknown draft: {draft_id}")
            self._require_actionable_recovery_draft_row(connection, row)
            if int(row["revision"]) != int(expected_revision):
                raise JobError(
                    f"draft revision conflict: expected {expected_revision}, current {row['revision']}"
                )
            updated = connection.execute(
                """
                UPDATE drafts SET text = ?, sha256 = ?, state = 'editing',
                    validation_json = NULL, revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (normalized, digest, timestamp, str(draft_id), int(expected_revision)),
            )
            if updated.rowcount != 1:
                raise JobError("draft was modified concurrently")
        return self.get_draft(draft_id)

    def mark_draft_validated(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        validation: Mapping[str, Any],
    ) -> DraftRecord:
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM drafts WHERE id = ?", (str(draft_id),)).fetchone()
            if row is None:
                raise JobError(f"unknown draft: {draft_id}")
            self._require_actionable_recovery_draft_row(connection, row)
            if int(row["revision"]) != int(expected_revision):
                raise JobError("draft revision conflict during validation")
            connection.execute(
                """
                UPDATE drafts SET state = 'validated', validation_json = ?,
                    revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?
                """,
                (
                    _canonical_json(_safe_event_payload(validation)),
                    timestamp,
                    str(draft_id),
                    int(expected_revision),
                ),
            )
        return self.get_draft(draft_id)

    def _project_root(self, project_id: str, connection: Any | None = None) -> Path:
        def load(conn: Any) -> Path:
            row = conn.execute(
                "SELECT root FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone()
            if row is None:
                raise JobError(f"unknown workbench project: {project_id}")
            return Path(row["root"])

        if connection is not None:
            return load(connection)
        with self.database.connection() as connection:
            return load(connection)

    def _require_actionable_recovery_draft_row(self, connection: Any, row: Any) -> None:
        chapter = int(row["chapter"])
        if str(row["state"]) == "archived":
            raise JobError(
                f"chapter {chapter} recovery draft has been archived after formal commit"
            )
        project_root = self._project_root(str(row["project_id"]), connection)
        if _formal_prose_exists(project_root, chapter):
            raise JobError(f"chapter {chapter} already has formal committed prose")

    def require_actionable_recovery_draft(self, draft_id: str) -> DraftRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (str(draft_id),)
            ).fetchone()
            if row is None:
                raise JobError(f"unknown draft: {draft_id}")
            self._require_actionable_recovery_draft_row(connection, row)
            return _draft_from_row(row)

    def list_actionable_drafts(self, project_id: str) -> tuple[DraftRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM drafts
                WHERE project_id = ? AND state != 'archived'
                ORDER BY updated_at DESC
                """,
                (str(project_id),),
            ).fetchall()
        return tuple(self.get_draft(str(row["id"])) for row in rows)

    def archive_recovery_draft_for_committed_chapter(
        self,
        project_id: str,
        chapter: int,
        *,
        project_root: str | Path | None = None,
    ) -> DraftRecord | None:
        root = Path(project_root) if project_root is not None else self._project_root(project_id)
        if not _formal_prose_exists(root, int(chapter)):
            return None
        draft = self.get_project_chapter_draft(project_id, int(chapter))
        if draft is None:
            return None
        return self._archive_recovery_draft(draft.id, project_root=root)

    def archive_settled_recovery_drafts(
        self,
        project_id: str,
        *,
        project_root: str | Path | None = None,
    ) -> tuple[DraftRecord, ...]:
        root = Path(project_root) if project_root is not None else self._project_root(project_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, chapter FROM drafts
                WHERE project_id = ? AND state != 'archived'
                ORDER BY chapter, id
                """,
                (str(project_id),),
            ).fetchall()
        archived: list[DraftRecord] = []
        for row in rows:
            record = self.archive_recovery_draft_for_committed_chapter(
                project_id,
                int(row["chapter"]),
                project_root=root,
            )
            if record is not None:
                archived.append(record)
        return tuple(archived)

    def _archive_recovery_draft(
        self,
        draft_id: str,
        *,
        project_root: Path | None = None,
    ) -> DraftRecord | None:
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (str(draft_id),)
            ).fetchone()
            if row is None:
                return None
            if str(row["state"]) == "archived":
                return _draft_from_row(row)
            root = (
                project_root
                if project_root is not None
                else self._project_root(str(row["project_id"]), connection)
            )
            if not _formal_prose_exists(root, int(row["chapter"])):
                return None
            connection.execute(
                """
                UPDATE drafts SET state = 'archived', revision = revision + 1, updated_at = ?
                WHERE id = ? AND state != 'archived'
                """,
                (timestamp, str(draft_id)),
            )
        return self.get_draft(draft_id)

    def _transition(
        self,
        job_id: str,
        *,
        allowed: Iterable[str],
        state: str,
        event_type: str,
        event_payload: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        finish: bool = False,
        manual: bool = False,
    ) -> JobRecord:
        if state not in JOB_STATES:
            raise JobError(f"invalid target state: {state}")
        allowed_set = frozenset(allowed)
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = _require_job_row(connection, job_id)
            if str(row["state"]) not in allowed_set:
                raise JobError(f"unsafe job transition {row['state']} -> {state}")
            connection.execute(
                """
                UPDATE jobs SET state = ?, result_json = COALESCE(?, result_json),
                    error_json = COALESCE(?, error_json), manual_confirmation_required = ?,
                    updated_at = ?, finished_at = ?, revision = revision + 1 WHERE id = ?
                """,
                (
                    state,
                    _canonical_json(_compact_result(result)) if result is not None else None,
                    _canonical_json(_safe_event_payload(error)) if error is not None else None,
                    1 if manual else 0,
                    timestamp,
                    timestamp if finish else None,
                    str(job_id),
                ),
            )
            self._append_event_in_transaction(
                connection,
                str(job_id),
                event_type,
                event_payload,
                timestamp=timestamp,
            )
        return self.get(job_id)

    def _append_event_in_transaction(
        self,
        connection: Any,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        timestamp: str,
    ) -> int:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM job_events WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """
            INSERT INTO job_events(job_id, sequence, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(job_id),
                sequence,
                _safe_event_type(event_type),
                _canonical_json(_safe_event_payload(payload)),
                timestamp,
            ),
        )
        return int(cursor.lastrowid)


class JobSupervisor:
    """Runs one engine subprocess at a time and persists every control decision."""

    def __init__(
        self,
        *,
        store: JobStore,
        project_lookup: Callable[[str], Any],
        repo_root: str | Path,
        runtime_root: str | Path,
        python_executable: str | Path | None = None,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        poll_interval: float = 0.2,
        instance_lease: InstanceLease | None = None,
        startup_recovery: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.project_lookup = project_lookup
        self.repo_root = Path(repo_root).resolve(strict=True)
        self.runtime_root = Path(runtime_root).resolve(strict=False)
        if store.database.restore_scope is None:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.python_executable = str(python_executable or sys.executable)
        self.process_factory = process_factory
        self.poll_interval = max(0.05, float(poll_interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._instance_lease = instance_lease
        self._owns_instance_lease = False
        self._maintenance=maintenance_for(instance_lease) if instance_lease is not None else None
        self._startup_recovery=startup_recovery
        self._startup_recovery_pending=True
        self._uncertain_admissions=[]

    def start(self) -> None:
        if self.store.database.restore_scope is not None:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._require_no_active_process()
        if self._instance_lease is None:
            self._instance_lease = InstanceLease(self.store.database.path.parent).acquire()
            self._owns_instance_lease = True
        self._instance_lease.require_held()
        self._maintenance=maintenance_for(self._instance_lease)
        try:
            self._start_worker()
        except BaseException:
            if self._owns_instance_lease:
                self._instance_lease.close()
                self._instance_lease = None
                self._owns_instance_lease = False
            raise

    def _start_worker(self) -> None:
        self._startup_recovery_pending=True
        try:
            with self._maintenance.admit('worker'):
                self._recover_startup_once()
        except MaintenanceBlocked:
            pass  # Keep the durable request; no restart writes or scheduling.
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="novelagent-workbench-worker",
            daemon=True,
        )
        self._thread.start()
        self._wake.set()

    def _recover_startup_once(self) -> None:
        if not self._startup_recovery_pending:return
        if self._startup_recovery is not None:self._startup_recovery()
        self.store.recover_after_restart()
        self.store.quarantine_incompatible_jobs()
        self._reconcile_receipted_restart_commits()
        self._reconcile_known_review_pauses()
        self._reconcile_pre_provider_context_failures()
        self._reconcile_pre_provider_provenance_failures()
        self._reconcile_repair_not_converging_failures()
        self._reconcile_stale_scene_evidence_failures()
        self._startup_recovery_pending=False

    def notify(self) -> None:
        self._wake.set()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
            if thread.is_alive():
                raise JobError("执行线程尚未结束，保留工作台所有权，禁止接管。")
        self._require_no_active_process()
        if self._owns_instance_lease and self._instance_lease is not None:
            self._instance_lease.close()
            self._instance_lease = None
            self._owns_instance_lease = False

    def _require_no_active_process(self) -> None:
        with self._process_lock:
            if self._active_process is not None:
                if self._active_process.poll() is None:
                    raise JobError("执行子进程尚未确认退出，保留工作台所有权，禁止继续调度。")
                self._active_process = None
            permits,self._uncertain_admissions=self._uncertain_admissions,[]
        for permit in permits:permit.close()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:permit=self._maintenance.admit('worker')
            except MaintenanceBlocked:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            except MaintenanceError:
                self._stop.set()
                return
            try:
                self._recover_startup_once()
                processed=self._run_one_job()
            finally:
                # A monitor exception is not proof that its child exited.
                try:
                    self._require_no_active_process()
                except Exception:
                    with self._process_lock:self._uncertain_admissions.append(permit)
                    self._stop.set()
                else:permit.close()
            if not processed:
                self._wake.wait(timeout=0.5)
                self._wake.clear()

    def _run_one_job(self) -> bool:
        job=self.store.claim_next()
        if job is None:return False
        try:
            if job.kind == "generation_session":self._run_generation_session(job)
            elif job.kind == "locked_chapter_recovery":self._run_recovery(job)
            else:self.store.fail_job(job.id,{"code":"unsupported_job_kind","kind":job.kind})
        except ProcessMonitorError:
            # Preserve the original uncertain pause; never downgrade to failure.
            self._stop.set()
        except Exception as exc:
            try:
                current=self.store.get(job.id)
                if current.state not in TERMINAL_JOB_STATES | PAUSED_JOB_STATES:
                    self.store.fail_job(job.id,{'code':'workbench_worker_error',
                        'type':type(exc).__name__,'message':str(exc)})
            except Exception:pass
        return True

    def _reconcile_receipted_restart_commits(self) -> None:
        for job in self.store.list_jobs(states=("needs_recovery",), limit=200):
            if job.execution_contract != CURRENT_JOB_EXECUTION_CONTRACT:
                continue
            if job.kind != "generation_session":
                continue
            try:
                project = self.project_lookup(job.project_id)
                result = _latest_committed_result(Path(project.root), job.next_chapter)
                if result is None:
                    continue
                verification = verify_committed_chapter(
                    project.root, job.next_chapter, result
                )
                if verification["ok"]:
                    self.store.reconcile_committed_after_restart(
                        job.id,
                        chapter=job.next_chapter,
                        result=result,
                        verification=verification,
                    )
            except Exception:
                # Ambiguous evidence remains needs_recovery and is never retried.
                continue

    def _reconcile_known_review_pauses(self) -> None:
        """Move proven review rejections out of the provider-uncertain lane."""

        for job in self.store.list_jobs(states=("needs_recovery",), limit=200):
            if job.execution_contract != CURRENT_JOB_EXECUTION_CONTRACT:
                continue
            if job.kind != "generation_session" or not isinstance(job.error, Mapping):
                continue
            if job.error.get("code") != "chapter_not_safe_to_continue":
                continue
            compact = job.result if isinstance(job.result, Mapping) else {}
            run_id = compact.get("run_id")
            review_status = compact.get("review_status")
            if not isinstance(run_id, str) or review_status not in {
                "needs_revision",
                "blocked",
                "error",
            }:
                continue
            try:
                project = self.project_lookup(job.project_id)
                result = _result_for_run_id(Path(project.root), job.next_chapter, run_id)
                if result is None or _result_is_uncertain(result):
                    continue
                if _review_status(result) != review_status:
                    continue
                self.store.reconcile_review_pause(
                    job.id,
                    run_id=run_id,
                    review_status=str(review_status),
                )
            except Exception:
                # Any missing, stale, or ambiguous evidence remains fail-closed.
                continue

    def _reconcile_pre_provider_context_failures(self) -> None:
        """Upgrade proven legacy local-budget pauses without replaying the engine."""

        for job in self.store.list_jobs(states=("awaiting_input",), limit=200):
            if job.execution_contract != CURRENT_JOB_EXECUTION_CONTRACT:
                continue
            if job.kind != "generation_session" or not isinstance(job.error, Mapping):
                continue
            if job.error.get("code") != "engine_result_unreadable":
                continue
            try:
                project = self.project_lookup(job.project_id)
                classification = _classify_pre_provider_context_failure(
                    Path(project.root),
                    job.next_chapter,
                    exit_code=job.error.get("exit_code"),
                    stderr_tail=job.error.get("stderr_tail"),
                    started_at=job.started_at,
                    finished_at=job.updated_at,
                )
                if classification is None:
                    continue
                self.store.reconcile_pre_provider_context_failure(
                    job.id,
                    result=classification["result"],
                    error=classification["error"],
                )
            except Exception:
                # Missing or ambiguous zero-call evidence retains the legacy fail-closed pause.
                continue

    def _reconcile_pre_provider_provenance_failures(self) -> None:
        """Upgrade exact legacy Git failures without ever requeueing them."""

        for job in self.store.list_jobs(states=("awaiting_input",), limit=200):
            if job.execution_contract != CURRENT_JOB_EXECUTION_CONTRACT:
                continue
            if job.kind != "generation_session" or not isinstance(job.error, Mapping):
                continue
            if job.error.get("code") != "engine_result_unreadable" or job.result is not None:
                continue
            try:
                project = self.project_lookup(job.project_id)
                project_root = Path(project.root).resolve(strict=True)
                paths = self._job_paths(job.id, job.next_chapter)
                legacy_error_sha256 = _sha256_text(_canonical_json(job.error))
                classification = _classify_pre_provider_provenance_failure(
                    project_root,
                    job.next_chapter,
                    exit_code=job.error.get("exit_code"),
                    stderr_tail=job.error.get("stderr_tail"),
                    paths=paths,
                    legacy_error_sha256=legacy_error_sha256,
                )
                if classification is None:
                    continue
                self.store.reconcile_pre_provider_provenance_failure(
                    job.id,
                    error=classification["error"],
                    expected_legacy_error_sha256=legacy_error_sha256,
                )
            except Exception:
                # Missing, changed, or ambiguous evidence remains fail-closed.
                continue

    def _reconcile_repair_not_converging_failures(self) -> None:
        """Explain proven legacy convergence failures without requeueing them."""

        for job in self.store.list_jobs(states=("awaiting_input",), limit=200):
            if job.execution_contract != CURRENT_JOB_EXECUTION_CONTRACT:
                continue
            if job.kind != "generation_session" or not isinstance(job.error, Mapping):
                continue
            if job.error.get("code") != "engine_result_unreadable" or job.result is not None:
                continue
            try:
                project = self.project_lookup(job.project_id)
                project_root = Path(project.root).resolve(strict=True)
                legacy_error_sha256 = _sha256_text(_canonical_json(job.error))
                classification = _classify_repair_not_converging_failure(
                    project_root,
                    job.next_chapter,
                    exit_code=job.error.get("exit_code"),
                    stderr_tail=job.error.get("stderr_tail"),
                    started_at=job.started_at,
                    finished_at=job.updated_at,
                )
                if classification is None:
                    continue
                self.store.reconcile_repair_not_converging_failure(
                    job.id,
                    result=classification["result"],
                    error=classification["error"],
                    expected_legacy_error_sha256=legacy_error_sha256,
                )
            except Exception:
                # Any tamper, ambiguity, or unsettled call retains the generic pause.
                continue

    def _reconcile_stale_scene_evidence_failures(self) -> None:
        """Explain proven legacy scene-alignment failures without requeueing them."""

        for job in self.store.list_jobs(states=("awaiting_input",), limit=200):
            if job.execution_contract != CURRENT_JOB_EXECUTION_CONTRACT:
                continue
            if job.kind != "generation_session" or not isinstance(job.error, Mapping):
                continue
            if job.error.get("code") != "engine_result_unreadable" or job.result is not None:
                continue
            try:
                project = self.project_lookup(job.project_id)
                project_root = Path(project.root).resolve(strict=True)
                legacy_error_sha256 = _sha256_text(_canonical_json(job.error))
                classification = _classify_stale_scene_evidence_failure(
                    project_root,
                    job.next_chapter,
                    exit_code=job.error.get("exit_code"),
                    stderr_tail=job.error.get("stderr_tail"),
                    started_at=job.started_at,
                    finished_at=job.updated_at,
                )
                if classification is None:
                    continue
                self.store.reconcile_stale_scene_evidence_failure(
                    job.id,
                    result=classification["result"],
                    error=classification["error"],
                    expected_legacy_error_sha256=legacy_error_sha256,
                )
            except Exception:
                # Any tamper, ambiguity, or unsettled call retains the generic pause.
                continue


    def _run_generation_session(self, initial_job: JobRecord) -> None:
        _require_job_execution_contract(initial_job.to_dict())
        job = initial_job
        project = self.project_lookup(job.project_id)
        project_root = Path(project.root).resolve(strict=True)
        while job.next_chapter <= job.end_chapter and not self._stop.is_set():
            if self._maintenance is not None:
                maintenance=self._maintenance.status()
                if maintenance['state']!='idle':
                    self._require_no_active_process()
                    self.store.pause_for_maintenance(job.id,chapter=job.next_chapter,
                        operation_id=maintenance['operation_id'])
                    return
            assert_project_execution_compatible(project_root, expected_book_id=getattr(project, "book_id", None))
            chapter = job.next_chapter
            expected_state_head = job.config.get("project_state_head")
            try:
                actual_state_head = compute_project_state_head(project)
            except Exception as exc:
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    error={
                        "code": "project_state_head_unreadable",
                        "message": str(exc),
                        "provider_calls": 0,
                    },
                    uncertain=False,
                )
                return
            if (
                not isinstance(expected_state_head, str)
                or expected_state_head != actual_state_head
            ):
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    error={
                        "code": "project_state_changed_between_chapters",
                        "expected": expected_state_head,
                        "actual": actual_state_head,
                        "provider_calls": 0,
                    },
                    uncertain=False,
                )
                return
            outline_verification = verify_approved_outline_binding(
                project_root, chapter, job.config
            )
            if not outline_verification["ok"]:
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    error={
                        "code": "approved_outline_drifted",
                        "verification": outline_verification,
                        "provider_calls": 0,
                    },
                    uncertain=False,
                )
                return
            git_preflight = _git_provenance_preflight(project_root)
            if git_preflight.get("ok") is not True:
                error = _pre_provider_provenance_preflight_error(
                    project_root,
                    chapter,
                    preflight=git_preflight,
                )
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    error=error,
                    uncertain=False,
                )
                return
            run_records_before = _strict_chapter_run_artifact_fingerprints(
                project_root, chapter
            )
            model_calls_before = _strict_model_call_artifact_fingerprints(project_root)
            if run_records_before is None or model_calls_before is None:
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    error={
                        "code": PRE_PROVIDER_PROVENANCE_ERROR,
                        "message": "生成前的本地运行证据无法安全判定；任务未启动。",
                        "details": {
                            "dependency": "local_artifact_evidence",
                            "stage": "execution_provenance_preflight",
                            "provider_reached": False,
                            "provider_calls": 0,
                            "unsettled_attempt_count": 0,
                            "accepted": False,
                            "committed": False,
                            "safe_to_retry": False,
                        },
                        "automatic_retry": False,
                        "safe_to_retry": False,
                    },
                    uncertain=False,
                )
                return
            paths = self._job_paths(job.id, chapter)
            command = self._generation_command(project_root, chapter, job.config, paths["progress"])
            self.store.mark_running(job.id, chapter=chapter, command=command)
            execution = self._execute_subprocess(job.id, chapter, command, paths)
            if execution["cancelled"]:
                self.store.mark_cancelled_uncertain(job.id, chapter=chapter)
                return
            result = execution.get("result")
            if not isinstance(result, dict):
                classification = _classify_pre_provider_context_failure(
                    project_root,
                    chapter,
                    exit_code=execution.get("exit_code"),
                    stderr_tail=execution.get("stderr_tail"),
                    prior_fingerprints=run_records_before,
                )
                if classification is not None:
                    self.store.chapter_failed(
                        job.id,
                        chapter=chapter,
                        result=classification["result"],
                        error=classification["error"],
                        uncertain=False,
                    )
                    return
                provenance_classification = _classify_pre_provider_provenance_failure(
                    project_root,
                    chapter,
                    exit_code=execution.get("exit_code"),
                    stderr_tail=execution.get("stderr_tail"),
                    paths=paths,
                    prior_run_fingerprints=run_records_before,
                    prior_model_call_fingerprints=model_calls_before,
                )
                if provenance_classification is not None:
                    self.store.chapter_failed(
                        job.id,
                        chapter=chapter,
                        error=provenance_classification["error"],
                        uncertain=False,
                    )
                    return
                repair_classification = _classify_repair_not_converging_failure(
                    project_root,
                    chapter,
                    exit_code=execution.get("exit_code"),
                    stderr_tail=execution.get("stderr_tail"),
                    prior_fingerprints=run_records_before,
                )
                if repair_classification is not None:
                    self.store.chapter_failed(
                        job.id,
                        chapter=chapter,
                        result=repair_classification["result"],
                        error=repair_classification["error"],
                        uncertain=False,
                    )
                    return
                stale_scene_classification = _classify_stale_scene_evidence_failure(
                    project_root,
                    chapter,
                    exit_code=execution.get("exit_code"),
                    stderr_tail=execution.get("stderr_tail"),
                    prior_fingerprints=run_records_before,
                )
                if stale_scene_classification is not None:
                    self.store.chapter_failed(
                        job.id,
                        chapter=chapter,
                        result=stale_scene_classification["result"],
                        error=stale_scene_classification["error"],
                        uncertain=False,
                    )
                    return
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    error={
                        "code": "engine_result_unreadable",
                        "exit_code": execution.get("exit_code"),
                        "stderr_tail": execution.get("stderr_tail"),
                    },
                    uncertain=_contains_uncertainty(execution.get("stderr_tail")),
                )
                return
            verification = verify_committed_chapter(project_root, chapter, result)
            review_status = _review_status(result)
            accepted_status = review_status in {"pass", "warning"}
            if bool(verification.get("ok")) and accepted_status:
                try:
                    next_state_head = compute_project_state_head(project)
                except Exception as exc:
                    self.store.chapter_failed(
                        job.id,
                        chapter=chapter,
                        result=result,
                        error={
                            "code": "committed_chapter_state_head_unreadable",
                            "message": str(exc),
                            "verification": verification,
                            "automatic_retry": False,
                        },
                        uncertain=True,
                    )
                    return
                job = self.store.chapter_succeeded(
                    job.id,
                    chapter=chapter,
                    result=result,
                    verification=verification,
                    next_project_state_head=next_state_head,
                )
                if job.state != "starting":
                    return
                continue
            stale_scene_classification = _classify_stale_scene_evidence_failure(
                project_root,
                chapter,
                exit_code=execution.get("exit_code"),
                stderr_tail=execution.get("stderr_tail"),
                prior_fingerprints=run_records_before,
            )
            if stale_scene_classification is not None:
                self.store.chapter_failed(
                    job.id,
                    chapter=chapter,
                    result=stale_scene_classification["result"],
                    error=stale_scene_classification["error"],
                    uncertain=False,
                )
                return
            uncertain = _result_is_uncertain(result) or _contains_uncertainty(execution.get("stderr_tail"))
            self.store.chapter_failed(
                job.id,
                chapter=chapter,
                result=result,
                error={
                    "code": "chapter_not_safe_to_continue",
                    "exit_code": execution.get("exit_code"),
                    "review_status": review_status,
                    "verification": verification,
                    "stderr_tail": execution.get("stderr_tail"),
                },
                uncertain=uncertain,
            )
            return

    def _run_recovery(self, job: JobRecord) -> None:
        _require_job_execution_contract(job.to_dict())
        project = self.project_lookup(job.project_id)
        project_root = Path(project.root).resolve(strict=True)
        assert_project_execution_compatible(project_root, expected_book_id=getattr(project, "book_id", None))
        chapter = job.next_chapter
        paths = self._job_paths(job.id, chapter)
        command = self._recovery_command(project_root, job.config)
        self.store.mark_running(job.id, chapter=chapter, command=command)
        execution = self._execute_subprocess(job.id, chapter, command, paths)
        if execution["cancelled"]:
            self.store.mark_cancelled_uncertain(job.id, chapter=chapter)
            return
        result = execution.get("result")
        if execution.get("exit_code") == 0 and isinstance(result, dict) and result.get("ok") is True:
            draft_id = job.config.get("draft_id")
            draft_revision = job.config.get("draft_revision")
            result_payload = dict(result)
            if not (isinstance(draft_id, str) and isinstance(draft_revision, int)):
                conflict = self._capture_recovery_draft(job.id, job.project_id, result)
                if conflict is not None:
                    result_payload["draft_source_conflict"] = conflict
            self.store.recovery_succeeded(job.id, result_payload)
            if isinstance(draft_id, str) and isinstance(draft_revision, int):
                try:
                    self.store.mark_draft_validated(
                        draft_id,
                        expected_revision=draft_revision,
                        validation=result,
                    )
                except JobError:
                    # A concurrent author edit invalidates validation but never
                    # invalidates the append-only recovery checkpoint.
                    self.store.append_event(
                        job.id,
                        "draft.validation_stale",
                        {"draft_id": draft_id, "expected_revision": draft_revision},
                    )
            return
        self.store.fail_job(
            job.id,
            {
                "code": "locked_chapter_recovery_failed",
                "exit_code": execution.get("exit_code"),
                "stderr_tail": execution.get("stderr_tail"),
                "result": _compact_result(result) if isinstance(result, dict) else None,
            },
        )

    def _execute_subprocess(
        self, job_id: str, chapter: int, command: Sequence[str], paths: Mapping[str, Path],
    ) -> dict[str, Any]:
        _require_job_execution_contract(self.store.get(job_id).to_dict())
        lease = self._instance_lease or InstanceLease(self.store.database.path.parent).acquire()
        try:
            return self._execute_owned_subprocess(job_id, chapter, command, paths, lease)
        finally:
            if self._instance_lease is None:
                lease.close()

    def _execute_owned_subprocess(
        self,
        job_id: str,
        chapter: int,
        command: Sequence[str],
        paths: Mapping[str, Path],
        lease: InstanceLease,
    ) -> dict[str, Any]:
        paths["directory"].mkdir(parents=True, exist_ok=True)
        env = _provider_environment()
        try:
            progress_offset = paths["progress"].stat().st_size
        except OSError:
            progress_offset = 0
        with self._process_lock:
            if self._stop.is_set() or self._active_process is not None:
                raise JobError("执行器已停止或仍有子进程，不能启动新的执行任务。")
            process = lease.spawn(self.process_factory,
                list(command),
                cwd=str(self.repo_root),
                env=env,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._active_process = process
        cancelled = False
        stdout = ""
        stderr = ""
        try:
            while True:
                progress_offset = self._ingest_progress(job_id, paths["progress"], progress_offset)
                current = self.store.get(job_id)
                if current.cancel_requested and process.poll() is None:
                    cancelled = True
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    break
                try:
                    stdout, stderr = process.communicate(timeout=self.poll_interval)
                    break
                except subprocess.TimeoutExpired:
                    if self._stop.is_set() and process.poll() is None:
                        # Shutdown has the same uncertainty semantics as emergency stop.
                        cancelled = True
                        process.terminate()
                    continue
            progress_offset = self._ingest_progress(job_id, paths["progress"], progress_offset)
        except BaseException as exc:
            self._stop.set()
            # A lost progress/database monitor must not orphan an engine and
            # then allow this same owner to schedule another one. Termination
            # does not prove that an already sent Provider request was free.
            for stop in (process.terminate, process.kill):
                try:
                    if process.poll() is None:
                        stop()
                    stdout, stderr = process.communicate(timeout=2.0)
                    break
                except Exception:
                    continue
            try:
                self.store.chapter_failed(
                    job_id, chapter=chapter, uncertain=True,
                    error={"code": "execution_monitor_failed", "type": type(exc).__name__,
                           "message": _sanitize_diagnostic(str(exc)),
                           "local_process_exited": process.returncode is not None,
                           "provider_state": "unknown"},
                )
            except Exception:
                # The original running row remains recoverable even if the
                # failing database cannot persist this richer explanation.
                pass
            raise ProcessMonitorError("执行监控中断，已停止调度；请检查任务与调用凭证。") from exc
        finally:
            with self._process_lock:
                if process.returncode is not None:
                    self._active_process = None
        safe_stderr = _sanitize_diagnostic(stderr)
        paths["stdout"].write_text(stdout, encoding="utf-8")
        paths["stderr"].write_text(safe_stderr, encoding="utf-8")
        result = _parse_json_output(stdout)
        return {
            "cancelled": cancelled,
            "exit_code": process.returncode,
            "result": result,
            "stderr_tail": safe_stderr[-4000:],
        }

    def _ingest_progress(self, job_id: str, path: Path, offset: int) -> int:
        if not path.is_file():
            return offset
        try:
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        self.store.record_progress_event(job_id, payload)
                return handle.tell()
        except OSError:
            return offset

    def _generation_command(
        self,
        project_root: Path,
        chapter: int,
        config: Mapping[str, Any],
        progress_path: Path,
    ) -> list[str]:
        command = [
            self.python_executable,
            "-B",
            str(self.repo_root / "main.py"),
            "--story-project",
            str(project_root),
            "--chapter",
            str(chapter),
            "--story-project-writeback",
            "--quality-policy",
            str(config.get("quality_policy") or "strict"),
            "--enable-review-pipeline",
            "--review-gate",
            "needs_revision",
            "--review-auto-repair",
            "--review-repair-max-attempts",
            "1",
            "--output-json",
            "--progress-jsonl",
            str(progress_path),
        ]
        if bool(config.get("llm_validator", True)):
            command.append("--llm-validator")
        if bool(config.get("allow_shadow", True)):
            command.append("--allow-story-state-shadow-downgrade")
        return command

    def _recovery_command(self, project_root: Path, config: Mapping[str, Any]) -> list[str]:
        command = [
            self.python_executable,
            "-B",
            str(self.repo_root / "main.py"),
            "--story-project",
            str(project_root),
            "--recover-locked-chapter",
            "--output-json",
        ]
        draft_path = config.get("manual_draft_path")
        if isinstance(draft_path, str) and draft_path:
            candidate = Path(draft_path).resolve(strict=True)
            candidate.relative_to(project_root)
            command.extend(["--locked-chapter-draft", str(candidate)])
        if bool(config.get("force_reset")):
            command.append("--locked-chapter-reset")
        return command

    def _job_paths(self, job_id: str, chapter: int) -> dict[str, Path]:
        directory = self.runtime_root / job_id / f"chapter_{chapter:04d}"
        return {
            "directory": directory,
            "progress": directory / "progress.jsonl",
            "stdout": directory / "stdout.json",
            "stderr": directory / "stderr.log",
        }

    def _capture_recovery_draft(
        self,
        job_id: str,
        project_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        checkpoint_path = result.get("checkpoint_path")
        if not isinstance(checkpoint_path, str) or not checkpoint_path:
            return None
        try:
            payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        complete = payload.get("complete_draft") if isinstance(payload, dict) else None
        text = complete.get("text") if isinstance(complete, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            self.store.upsert_draft(
                project_id=project_id,
                chapter=int(payload["chapter_index"]),
                text=text,
                source_run_id=str(payload.get("source_run_id") or "") or None,
                source_path=checkpoint_path,
            )
        except DraftSourceConflict as exc:
            # The checkpoint remains append-only evidence, but an author's
            # newer text is never replaced silently.  Hashes are sufficient
            # for the UI to explain the merge requirement without copying
            # either full draft into the event log.
            self.store.append_event(
                job_id,
                "draft.source_upgrade_conflict",
                {
                    "draft_id": exc.draft_id,
                    "chapter": int(payload["chapter_index"]),
                    "current_sha256": exc.current_sha256,
                    "base_sha256": exc.base_sha256,
                    "incoming_sha256": exc.incoming_sha256,
                    "source_run_id": str(payload.get("source_run_id") or "") or None,
                    "source_path": checkpoint_path,
                    "automatic_overwrite": False,
                },
            )
            return {
                "code": "draft_source_upgrade_conflict",
                "draft_id": exc.draft_id,
                "chapter": int(payload["chapter_index"]),
                "current_sha256": exc.current_sha256,
                "base_sha256": exc.base_sha256,
                "incoming_sha256": exc.incoming_sha256,
                "source_run_id": str(payload.get("source_run_id") or "") or None,
                "automatic_overwrite": False,
            }
        return None


def verify_committed_chapter(
    project_root: str | Path,
    chapter: int,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    run = result.get("run") if isinstance(result.get("run"), Mapping) else {}
    story_project = (
        run.get("story_project") if isinstance(run.get("story_project"), Mapping) else {}
    )
    persistence = run.get("persistence") if isinstance(run.get("persistence"), Mapping) else {}
    resolution = resolve_prose(root, int(chapter))
    errors: list[str] = []
    if result.get("committed") is not True or run.get("committed") is not True:
        errors.append("run_not_committed")
    if run.get("status") != "committed":
        errors.append("run_status_not_committed")
    try:
        recorded_chapter = int(run.get("chapter_index"))
    except (TypeError, ValueError):
        recorded_chapter = None
    if recorded_chapter != int(chapter):
        errors.append("run_chapter_mismatch")
    if not isinstance(run.get("id"), str) or not str(run.get("id")).strip():
        errors.append("run_id_missing")
    review_status = _review_status(result)
    if review_status not in {"pass", "warning"}:
        errors.append("review_status_not_accepted")
    if not persistence:
        errors.append("persistence_receipt_missing")
    elif (
        persistence.get("committed") is not True
        or persistence.get("state") not in {"completed", "commit_marked"}
    ):
        errors.append("persistence_receipt_not_committed")
    if len(resolution.candidates) != 1 or resolution.path is None:
        errors.append("formal_prose_readback_not_unique")
        prose_sha = None
    else:
        try:
            prose_path = _safe_project_file(root, resolution.path)
            prose_text = prose_path.read_text(encoding="utf-8")
            prose_sha = _sha256_text(prose_text)
            generated_text = result.get("chapter")
            if not isinstance(generated_text, str) or not generated_text.strip():
                errors.append("generated_chapter_missing")
            elif _sha256_text(generated_text) != prose_sha:
                errors.append("formal_prose_readback_hash_mismatch")
        except (OSError, ValueError):
            prose_sha = None
            errors.append("formal_prose_readback_failed")
    publication = persistence.get("publication") if isinstance(persistence, Mapping) else None
    receipt_path = publication.get("receipt_path") if isinstance(publication, Mapping) else None
    receipt_verified = False
    receipt_kind = None
    if isinstance(receipt_path, str) and receipt_path:
        try:
            receipt = _safe_project_file(root, receipt_path)
            registry = RootRegistryService(
                RuntimePaths.for_story_project(root).persistence_dir
            ).load()
            root_map = {
                str(root_id): str(binding["path"])
                for root_id, binding in registry["roots"].items()
            }
            receipt_result = verify_publication_receipt(
                receipt, root_map=root_map
            )
            receipt_verified = (
                receipt_result.get("valid") is True
                and receipt_result.get("committed") is True
                and receipt_result.get("run_id") == run.get("id")
                and receipt_result.get("book_id")
                == story_project.get("book_id")
            )
            receipt_kind = "publication"
        except Exception:
            receipt_verified = False
        if not receipt_verified:
            errors.append("publication_receipt_readback_failed")
    elif persistence:
        commit_marker = persistence.get("commit_marker")
        try:
            marker_path = _safe_project_file(root, str(commit_marker))
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            receipt_verified = (
                isinstance(marker, dict)
                and marker.get("run_id") == run.get("id")
                and isinstance(marker.get("candidate_sha256"), str)
            )
            candidate_value = persistence.get("candidate_result_path")
            if receipt_verified and isinstance(candidate_value, str) and candidate_value:
                candidate_path = _safe_project_file(root, candidate_value)
                receipt_verified = (
                    hashlib.sha256(candidate_path.read_bytes()).hexdigest()
                    == marker.get("candidate_sha256")
                )
            else:
                receipt_verified = False
            receipt_kind = "transaction_marker"
        except Exception:
            receipt_verified = False
        if not receipt_verified:
            errors.append("transaction_commit_marker_readback_failed")
    return {
        "ok": not errors,
        "errors": errors,
        "prose_path": str(resolution.path) if resolution.path is not None else None,
        "prose_sha256": prose_sha,
        "receipt_verified": receipt_verified,
        "receipt_kind": receipt_kind,
        "review_status": review_status,
    }


def verify_approved_outline_binding(
    project_root: str | Path,
    chapter: int,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the frozen batch and this chapter before any provider call."""

    root = Path(project_root).resolve()
    manifest_value = config.get("outline_manifest_path")
    batch_id = config.get("outline_batch_id")
    proposal_sha = config.get("outline_proposal_sha256")
    receipt_sha = config.get("outline_receipt_sha256")
    if not all(isinstance(value, str) and value for value in (
        manifest_value, batch_id, proposal_sha, receipt_sha
    )):
        return {"ok": False, "errors": ["approved_outline_binding_missing"]}
    try:
        manifest_path = _safe_project_file(root, str(manifest_value))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "errors": ["outline_manifest_unreadable"]}
    if not isinstance(manifest, dict):
        return {"ok": False, "errors": ["outline_manifest_invalid"]}
    errors: list[str] = []
    if manifest.get("batch_id") != batch_id:
        errors.append("outline_batch_id_mismatch")
    if manifest.get("proposal_sha256") != proposal_sha:
        errors.append("outline_proposal_sha_mismatch")
    raw_for_hash = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest_sha = hashlib.sha256(_canonical_json(raw_for_hash).encode("utf-8")).hexdigest()
    if manifest.get("manifest_sha256") != manifest_sha:
        errors.append("outline_manifest_hash_mismatch")
    receipt_path = manifest_path.with_name("approval_receipt.json")
    try:
        receipt = json.loads(
            _safe_project_file(root, receipt_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        receipt = None
    if not isinstance(receipt, dict) or receipt.get("receipt_sha256") != receipt_sha:
        errors.append("outline_approval_receipt_mismatch")
    else:
        receipt_for_hash = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        actual_receipt_sha = hashlib.sha256(
            _canonical_json(receipt_for_hash).encode("utf-8")
        ).hexdigest()
        if actual_receipt_sha != receipt_sha:
            errors.append("outline_approval_receipt_hash_mismatch")
        if receipt.get("manifest_sha256") != manifest_sha:
            errors.append("outline_receipt_manifest_mismatch")
    entry = next(
        (
            item
            for item in manifest.get("files", [])
            if isinstance(item, dict) and item.get("chapter_index") == int(chapter)
        ),
        None,
    )
    if entry is None:
        errors.append("chapter_not_in_approved_outline_batch")
        return {"ok": False, "errors": errors}
    relative = entry.get("relative_path")
    try:
        outline_path = _safe_project_file(root, root / Path(str(relative)))
        outline_sha = hashlib.sha256(outline_path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        outline_path = None
        outline_sha = None
    if outline_sha != entry.get("sha256"):
        errors.append("approved_outline_file_drifted")
    return {
        "ok": not errors,
        "errors": errors,
        "batch_id": batch_id,
        "chapter": chapter,
        "outline_path": str(outline_path) if outline_path is not None else None,
        "outline_sha256": outline_sha,
    }


def _merge_environment_paths(values: Iterable[str]) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for raw_part in str(value or "").split(os.pathsep):
            part = raw_part.strip()
            if not part:
                continue
            normalized = os.path.normcase(os.path.normpath(part.strip('"')))
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(part)
    return os.pathsep.join(merged)


def _trusted_git_executable(source: Mapping[str, str]) -> Path | None:
    candidates: list[Path] = []
    for key, suffix in (
        ("PROGRAMFILES", Path("Git") / "cmd" / "git.exe"),
        ("PROGRAMFILES", Path("Git") / "bin" / "git.exe"),
        ("PROGRAMFILES(X86)", Path("Git") / "cmd" / "git.exe"),
        ("PROGRAMFILES(X86)", Path("Git") / "bin" / "git.exe"),
        ("LOCALAPPDATA", Path("Programs") / "Git" / "cmd" / "git.exe"),
        ("LOCALAPPDATA", Path("Programs") / "Git" / "bin" / "git.exe"),
    ):
        base = next(
            (value for name, value in source.items() if name.upper() == key and value),
            None,
        )
        if base:
            candidates.append(Path(base) / suffix)
    for candidate in candidates:
        try:
            if candidate.is_file():
                resolved = candidate.resolve(strict=True)
                if resolved.is_absolute() and resolved.name.casefold() == "git.exe":
                    return resolved
        except OSError:
            continue
    return None


def _provider_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source_environment = os.environ if source is None else source
    prefixes = (
        "OPENAI_",
        "ANTHROPIC_",
        "CLAUDE_",
        "NOVELAGENT_",
        "PROVIDER_",
        "NOTION_",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    )
    exact = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    }
    path_values: list[str] = []
    pathext_values: list[str] = []
    environment: dict[str, str] = {}
    for key, value in source_environment.items():
        normalized = key.upper()
        if normalized == "PATH":
            path_values.append(value)
            continue
        if normalized == "PATHEXT":
            pathext_values.append(value)
            continue
        if normalized == "NOVELAGENT_GIT_EXECUTABLE":
            # The workbench never forwards a caller-supplied executable path.
            # Only the standard-install resolver below may populate this key.
            continue
        if normalized in exact or any(normalized.startswith(prefix) for prefix in prefixes):
            environment[normalized] = value
    merged_path = _merge_environment_paths(path_values)
    git_executable = _trusted_git_executable(source_environment)
    if git_executable is not None:
        merged_path = _merge_environment_paths((str(git_executable.parent), merged_path))
        environment["NOVELAGENT_GIT_EXECUTABLE"] = str(git_executable)
    if merged_path:
        environment["PATH"] = merged_path
    merged_pathext = _merge_environment_paths(pathext_values)
    if merged_pathext:
        environment["PATHEXT"] = merged_pathext
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _git_subprocess_environment(
    source: Mapping[str, str],
    git_executable: Path,
) -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "PATHEXT",
    }
    environment: dict[str, str] = {}
    pathext_values: list[str] = []
    for key, value in source.items():
        normalized = key.upper()
        if normalized == "PATHEXT":
            pathext_values.append(value)
        elif normalized in allowed:
            environment[normalized] = value
    system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    path_parts = [str(git_executable.parent)]
    if system_root:
        path_parts.append(str(Path(system_root) / "System32"))
    environment["PATH"] = _merge_environment_paths(path_parts)
    merged_pathext = _merge_environment_paths(pathext_values)
    if merged_pathext:
        environment["PATHEXT"] = merged_pathext
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _git_provenance_preflight(repository_root: str | Path) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    git_executable = _trusted_git_executable(os.environ)
    if git_executable is None:
        return {"ok": False, "dependency": "git"}
    environment = _git_subprocess_environment(os.environ, git_executable)
    try:
        commit_process = subprocess.run(
            [str(git_executable), "rev-parse", "--verify", "HEAD"],
            cwd=str(root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
        if commit_process.returncode == 0:
            status_process = subprocess.run(
                [
                    str(git_executable),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=normal",
                ],
                cwd=str(root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
            ok = status_process.returncode == 0
        else:
            # Source archives and unborn repositories are supported by the
            # engine; successful process creation is the relevant preflight.
            ok = True
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return {"ok": ok, "dependency": "git"}


def _safe_job_config(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "quality_policy",
        "llm_validator",
        "allow_shadow",
        "weekly_budget",
        "manual_draft_path",
        "force_reset",
        "parent_job_id",
        "parent_failure_revision",
        "source_run_id",
        "source_draft_sha256",
        "draft_id",
        "draft_revision",
        "outline_batch_id",
        "outline_manifest_path",
        "outline_proposal_sha256",
        "outline_receipt_sha256",
        "cohort_id",
        "project_state_head",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise JobError("unsupported job configuration fields: " + ", ".join(unknown))
    result = {key: value[key] for key in value if key in allowed}
    # Credentials are never accepted as task configuration.
    encoded = _canonical_json(result)
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise JobError("job configuration is too large")
    return result


def _safe_event_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > 8000:
            return value[:8000] + "…"
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if _sensitive_event_key(normalized):
                result[key] = "<redacted>"
            else:
                result[key] = _safe_event_payload(nested, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_event_payload(item, depth=depth + 1) for item in value[:200]]
    return str(value)


def _sensitive_event_key(normalized: str) -> bool:
    if normalized in {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "private_key",
        "credential",
        "credentials",
    }:
        return True
    return normalized.endswith(
        ("_api_key", "_password", "_passwd", "_secret", "_access_token", "_refresh_token")
    )


def _sanitize_diagnostic(value: Any) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 <redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\b("
        r"[A-Za-z][A-Za-z0-9_-]*(?:api[_-]?key|auth[_-]?token|access[_-]?token|"
        r"refresh[_-]?token|id[_-]?token|private[_-]?key|token|secret|password|"
        r"passwd|credentials?)|authorization"
        r")([\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)"
        r"(\s*[:=]\s*)[^\s,;]+",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(https?://)(?:[^/@\s]+@)?([^/?#\s]+)(?:[^\s]*)",
        r"\1\2/<redacted>",
        text,
    )
    return text


def compute_project_state_head(project: Any) -> str:
    """Hash every author-controlled input accepted between chapter calls."""

    from workbench.outlines import compute_source_fingerprint

    root = Path(project.root).resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(b"NovelAgent-workbench-project-head-v1\0")
    digest.update(compute_source_fingerprint(project).encode("ascii"))
    amendments_root = root / ".novelagent" / "amendments"
    if not amendments_root.exists():
        return digest.hexdigest()
    pending = [amendments_root]
    files: list[Path] = []
    while pending:
        current = pending.pop()
        try:
            entries = list(current.iterdir())
        except OSError as exc:
            raise JobError(f"cannot inspect amendment state: {current}") from exc
        for entry in entries:
            try:
                info = entry.lstat()
                entry.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise JobError(f"unsafe amendment state entry: {entry}") from exc
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            attributes = int(getattr(info, "st_file_attributes", 0))
            if stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag):
                raise JobError(f"amendment state cannot contain links: {entry}")
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                files.append(entry)
            else:
                raise JobError(f"unsupported amendment state entry: {entry}")
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        raw = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _safe_project_file(root: Path, value: str | Path) -> Path:
    """Resolve one existing regular file without following a link/reparse hop."""

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    info = candidate.lstat()
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag):
        raise ValueError(f"project evidence cannot be a link: {candidate}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"project evidence is not a regular file: {candidate}")
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
    return resolved


def _job_id(
    *,
    project_id: str,
    kind: str,
    start_chapter: int,
    end_chapter: int,
    config: Mapping[str, Any],
    idempotency_key: str | None,
) -> str:
    if idempotency_key is None:
        return f"job_{uuid.uuid4().hex}"
    key = str(idempotency_key).strip()
    if not key or len(key) > 300:
        raise JobError("Idempotency-Key must contain 1 to 300 characters")
    material = {
        "project_id": project_id,
        "kind": kind,
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "key": key,
    }
    digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()[:40]
    return f"job_{digest}"


def _stored_job_error(row: Any) -> dict[str, Any]:
    if not row["error_json"]:
        return {}
    try:
        value = json.loads(str(row["error_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_pre_provider_context_budget_error(value: Mapping[str, Any] | None) -> bool:
    return isinstance(value, Mapping) and value.get("code") == PRE_PROVIDER_CONTEXT_BUDGET_ERROR


def _is_pre_provider_provenance_error(value: Mapping[str, Any] | None) -> bool:
    return isinstance(value, Mapping) and value.get("code") == PRE_PROVIDER_PROVENANCE_ERROR


def _is_repair_not_converging_error(value: Mapping[str, Any] | None) -> bool:
    return isinstance(value, Mapping) and value.get("code") == REPAIR_NOT_CONVERGING_ERROR


def _is_stale_scene_evidence_error(value: Mapping[str, Any] | None) -> bool:
    return isinstance(value, Mapping) and value.get("code") == STALE_SCENE_EVIDENCE_ERROR


def _chapter_run_record_fingerprints(project_root: Path, chapter: int) -> dict[str, str]:
    root = Path(project_root).resolve(strict=True)
    run_dir = root / ".novelagent" / "runtime" / "runs"
    if not run_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in run_dir.glob("chapter_*.json"):
        try:
            safe_path = _safe_project_file(root, path)
            raw = safe_path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
            run = value.get("run") if isinstance(value, Mapping) else None
            if not isinstance(run, Mapping) or int(run.get("chapter_index") or 0) != int(chapter):
                continue
            relative = safe_path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(raw).hexdigest()
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return result


def _model_call_artifact_fingerprints(project_root: Path) -> dict[str, str]:
    root = Path(project_root).resolve(strict=True)
    executions = root / ".novelagent" / "runtime" / "runs" / "executions"
    if not executions.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in executions.glob("*/model_calls/**/*"):
        if not path.is_file():
            continue
        try:
            safe_path = _safe_project_file(root, path)
            relative = safe_path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(safe_path.read_bytes()).hexdigest()
        except (OSError, RuntimeError, ValueError):
            continue
    return result


def _is_reparse_or_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _strict_chapter_run_artifact_fingerprints(
    project_root: Path,
    chapter: int,
) -> dict[str, str] | None:
    root = Path(project_root).resolve(strict=True)
    run_dir = root / ".novelagent" / "runtime" / "runs"
    if not run_dir.exists():
        return {}
    if not run_dir.is_dir() or _is_reparse_or_link(run_dir):
        return None
    result: dict[str, str] = {}
    try:
        candidates = tuple(run_dir.glob("chapter_*.json"))
    except OSError:
        return None
    for path in candidates:
        try:
            if _is_reparse_or_link(path) or not path.is_file():
                return None
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
            run = value.get("run") if isinstance(value, Mapping) else None
            if not isinstance(run, Mapping):
                return None
            recorded_chapter = int(run.get("chapter_index") or 0)
            relative = path.relative_to(root).as_posix()
            if recorded_chapter == int(chapter):
                result[relative] = hashlib.sha256(raw).hexdigest()
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None
    return result


def _latest_strict_chapter_run_id(
    project_root: Path,
    chapter: int,
) -> tuple[bool, str | None]:
    """Return the newest validated run id, preserving invalid/absent distinction."""

    root = Path(project_root).resolve(strict=True)
    fingerprints = _strict_chapter_run_artifact_fingerprints(root, chapter)
    if fingerprints is None:
        return False, None
    if not fingerprints:
        return True, None
    candidates: list[tuple[int, str, str]] = []
    for relative, expected_sha256 in fingerprints.items():
        try:
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
            if _is_reparse_or_link(path) or not path.is_file():
                return False, None
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                return False, None
            value = json.loads(raw.decode("utf-8"))
            run = value.get("run") if isinstance(value, Mapping) else None
            run_id = str(run.get("id") or "").strip() if isinstance(run, Mapping) else ""
            if not run_id or int(run.get("chapter_index") or 0) != int(chapter):
                return False, None
            candidates.append((path.stat().st_mtime_ns, relative, run_id))
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return False, None
    return True, max(candidates, key=lambda item: (item[0], item[1]))[2]


def _strict_model_call_artifact_fingerprints(
    project_root: Path,
) -> dict[str, str] | None:
    root = Path(project_root).resolve(strict=True)
    executions = root / ".novelagent" / "runtime" / "runs" / "executions"
    if not executions.exists():
        return {}
    if not executions.is_dir() or _is_reparse_or_link(executions):
        return None
    result: dict[str, str] = {}
    try:
        execution_dirs = tuple(executions.iterdir())
    except OSError:
        return None
    for execution in execution_dirs:
        if _is_reparse_or_link(execution):
            return None
        if not execution.is_dir():
            continue
        model_calls = execution / "model_calls"
        if not model_calls.exists():
            continue
        if not model_calls.is_dir() or _is_reparse_or_link(model_calls):
            return None
        try:
            entries = tuple(model_calls.rglob("*"))
        except OSError:
            return None
        for path in entries:
            try:
                if _is_reparse_or_link(path):
                    return None
                if path.is_dir():
                    continue
                if not path.is_file():
                    return None
                raw = path.read_bytes()
                relative = path.relative_to(root).as_posix()
                result[relative] = hashlib.sha256(raw).hexdigest()
            except (OSError, RuntimeError, ValueError):
                return None
    return result


def _fingerprint_manifest_sha256(value: Mapping[str, str]) -> str:
    return _sha256_text(_canonical_json(dict(sorted(value.items()))))


def _artifact_changed_since(paths: Iterable[Path], timestamp: datetime) -> bool:
    threshold = timestamp.timestamp() - 2.0
    for path in paths:
        try:
            if path.is_file() and path.stat().st_mtime >= threshold:
                return True
        except OSError:
            return True
    return False


def _progress_provenance_failure(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    events: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, Mapping):
            return None
        events.append(value)
    if not events:
        return None
    failure = events[-1]
    payload = failure.get("payload")
    if (
        failure.get("event") != "step_failed"
        or not isinstance(payload, Mapping)
        or payload.get("error_type") != "ProvenanceCaptureError"
    ):
        return None
    failure_time = _parse_timestamp(failure.get("timestamp"))
    if failure_time is None:
        return None
    start_event = next(
        (
            item
            for item in reversed(events[:-1])
            if item.get("event") == "step_start"
            and _parse_timestamp(item.get("timestamp")) is not None
        ),
        None,
    )
    if start_event is None:
        return None
    start_time = _parse_timestamp(start_event.get("timestamp"))
    if start_time is None or start_time > failure_time:
        return None
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "started_at": start_time.isoformat(),
        "failed_at": failure_time.isoformat(),
        "failure_sequence": failure.get("sequence"),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _settled_provider_calls_from_trace(run: Mapping[str, Any]) -> int | None:
    """Count physical calls only when every retry report is conclusively settled."""

    trace = run.get("trace")
    if not isinstance(trace, (list, tuple)):
        return None
    settled = 0
    saw_report = False
    for event in trace:
        if not isinstance(event, Mapping):
            return None
        reports = event.get("provider_attempts")
        if reports is None:
            continue
        if not isinstance(reports, (list, tuple)):
            return None
        for report in reports:
            saw_report = True
            if not isinstance(report, Mapping):
                return None
            attempts = report.get("attempts")
            history = report.get("history")
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < 1
                or report.get("completed") is not True
                or report.get("stop_reason") != "succeeded"
                or not isinstance(history, (list, tuple))
                or len(history) != attempts
            ):
                return None
            for expected_attempt, attempt in enumerate(history, start=1):
                if not isinstance(attempt, Mapping):
                    return None
                if attempt.get("attempt") != expected_attempt:
                    return None
                if attempt.get("outcome") not in {"failed", "succeeded"}:
                    return None
                if (
                    attempt.get("failure_category") == "provider_call_uncertain"
                    or attempt.get("partial_content_received") is True
                ):
                    return None
            if history[-1].get("outcome") != "succeeded":
                return None
            settled += attempts
    return settled if saw_report else None


def _provider_call_artifacts_are_settled(
    project_root: Path,
    run: Mapping[str, Any],
    *,
    expected_calls: int,
) -> bool:
    """Validate durable intent/receipt/response evidence when a run references it."""

    execution_evidence = run.get("execution_evidence")
    if not isinstance(execution_evidence, Mapping):
        return False
    reference = execution_evidence.get("model_calls_ref")
    if reference is None:
        # Older RunRecords can still be proven by their complete retry trace.
        return True
    if not isinstance(reference, str) or not reference.strip():
        return False
    runs_root = project_root / ".novelagent" / "runtime" / "runs"
    try:
        model_calls_root = (runs_root / reference).resolve(strict=True)
        model_calls_root.relative_to(runs_root / "executions")
    except (OSError, ValueError):
        return False
    if not model_calls_root.is_dir() or _is_reparse_or_link(model_calls_root):
        return False
    intents_dir = model_calls_root / "intents"
    receipts_dir = model_calls_root / "receipts"
    responses_dir = model_calls_root / "responses"
    for directory in (intents_dir, receipts_dir, responses_dir):
        if not directory.is_dir() or _is_reparse_or_link(directory):
            return False
    try:
        intents = tuple(sorted(intents_dir.glob("*.json")))
        receipts = tuple(sorted(receipts_dir.glob("*.json")))
        responses = tuple(sorted(responses_dir.glob("*.txt")))
    except OSError:
        return False
    if not (
        len(intents) == len(receipts) == len(responses) == expected_calls
        and {path.name for path in intents} == {path.name for path in receipts}
    ):
        return False
    response_names: set[str] = set()
    try:
        for intent_path in intents:
            if _is_reparse_or_link(intent_path):
                return False
            intent = load_model_call_intent(intent_path)
            receipt_path = receipts_dir / intent_path.name
            if _is_reparse_or_link(receipt_path):
                return False
            receipt = load_model_call_receipt(receipt_path)
            if (
                receipt.get("status") != "succeeded"
                or receipt.get("attempt_id") != intent.get("attempt_id")
                or receipt.get("intent_hash") != intent.get("intent_hash")
            ):
                return False
            response_ref = receipt.get("response_artifact_ref")
            if not isinstance(response_ref, str):
                return False
            response_path = (model_calls_root / response_ref).resolve(strict=True)
            response_path.relative_to(responses_dir.resolve(strict=True))
            if _is_reparse_or_link(response_path) or not response_path.is_file():
                return False
            response_raw = response_path.read_bytes()
            if hashlib.sha256(response_raw).hexdigest() != receipt.get(
                "response_artifact_hash"
            ):
                return False
            response_names.add(response_path.name)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return response_names == {path.name for path in responses}


def _repair_not_converging_evidence(
    project_root: Path,
    value: Mapping[str, Any],
    *,
    chapter: int,
    stderr_text: str,
) -> dict[str, Any] | None:
    """Validate one failed run down to its retained candidate and call receipts."""

    run = value.get("run")
    if not isinstance(run, Mapping) or int(run.get("chapter_index") or 0) != int(chapter):
        return None
    run_error = run.get("error")
    if not isinstance(run_error, Mapping) or run_error.get("type") != "RepairNotConvergingError":
        return None
    error_message = run_error.get("message")
    if not isinstance(error_message, str):
        return None
    match = _REPAIR_NOT_CONVERGING_PATTERN.fullmatch(error_message)
    if match is None or f"RepairNotConvergingError: {error_message}" not in stderr_text:
        return None
    if (
        run.get("status") != "failed"
        or run.get("accepted") is not False
        or run.get("committed") is not False
        or value.get("accepted") is not False
        or value.get("committed") is not False
    ):
        return None

    execution_evidence = run.get("execution_evidence")
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence, Mapping)
        else None
    )
    if not isinstance(budget, Mapping):
        return None
    provider_calls = budget.get("provider_calls")
    unsettled = budget.get("unsettled_attempt_count")
    if (
        not isinstance(provider_calls, int)
        or isinstance(provider_calls, bool)
        or provider_calls < 1
        or not isinstance(unsettled, int)
        or isinstance(unsettled, bool)
        or unsettled != 0
        or _result_is_uncertain(value)
    ):
        return None
    settled_provider_calls = _settled_provider_calls_from_trace(run)
    if settled_provider_calls != provider_calls:
        return None
    if not _provider_call_artifacts_are_settled(
        project_root,
        run,
        expected_calls=provider_calls,
    ):
        return None

    persistence = run.get("persistence")
    if persistence not in (None, {}):
        return None
    story_project = run.get("story_project")
    writeback = story_project.get("writeback") if isinstance(story_project, Mapping) else None
    if (
        not isinstance(writeback, Mapping)
        or writeback.get("attempted") is not False
        or writeback.get("applied") is not False
    ):
        return None
    try:
        if resolve_prose(project_root, chapter).candidates:
            return None
    except (OSError, RuntimeError, ValueError):
        return None

    candidate = value.get("chapter")
    run_chapter = run.get("chapter")
    if not isinstance(candidate, str) or not isinstance(run_chapter, Mapping):
        return None
    candidate_chars = run_chapter.get("chars")
    artifact = run_chapter.get("artifact")
    integrity_audit = run_chapter.get("integrity_audit")
    if (
        not isinstance(candidate_chars, int)
        or isinstance(candidate_chars, bool)
        or candidate_chars < 1
        or len(candidate) != candidate_chars
        or not isinstance(artifact, Mapping)
        or artifact.get("chars") != candidate_chars
        or not isinstance(integrity_audit, Mapping)
    ):
        return None
    candidate_sha256 = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    if integrity_audit.get("final_chapter_sha256") != candidate_sha256:
        return None
    artifact_path_value = artifact.get("path")
    artifact_sha256 = artifact.get("sha256")
    if not isinstance(artifact_path_value, str) or not isinstance(artifact_sha256, str):
        return None
    try:
        artifact_path = _safe_project_file(project_root, artifact_path_value)
        artifact_path.relative_to(
            project_root / ".novelagent" / "runtime" / "chapters"
        )
        artifact_raw = artifact_path.read_bytes()
        artifact_text = artifact_raw.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        hashlib.sha256(artifact_raw).hexdigest() != artifact_sha256
        or artifact_text.count(candidate) != 1
        or not artifact_text.endswith((candidate + "\n", candidate + "\r\n"))
    ):
        return None

    trace = run.get("trace")
    if not isinstance(trace, (list, tuple)):
        return None
    failures = [
        event
        for event in trace
        if isinstance(event, Mapping)
        and event.get("action") == "repair_if_needed"
        and event.get("status") == "failed"
        and event.get("error_type") == "RepairNotConvergingError"
        and event.get("error_message") == error_message
    ]
    if len(failures) != 1:
        return None
    failure = failures[0]
    repair_deltas = failure.get("repair_deltas")
    if not isinstance(repair_deltas, (list, tuple)) or not repair_deltas:
        return None
    delta = repair_deltas[-1]
    if not isinstance(delta, Mapping):
        return None
    original_problem_count = int(match.group("before"))
    final_problem_count = int(match.group("after"))
    if (
        delta.get("before_problem_count") != original_problem_count
        or delta.get("after_problem_count") != final_problem_count
        or delta.get("after_ok") is not False
    ):
        return None
    new_problem_codes = delta.get("new_problem_codes")
    after_problem_codes = delta.get("after_problem_codes")
    if (
        not isinstance(new_problem_codes, (list, tuple))
        or not all(isinstance(code, str) and code for code in new_problem_codes)
        or len(set(new_problem_codes)) != len(new_problem_codes)
        or not isinstance(after_problem_codes, (list, tuple))
        or not set(new_problem_codes).issubset(set(after_problem_codes))
    ):
        return None
    convergence_reason = match.group("reason")
    if convergence_reason == "new_more_severe_problem" and not new_problem_codes:
        return None
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    return {
        "failure_origin": "chapter_repair_convergence",
        "run_id": run_id,
        "provider_calls": provider_calls,
        "settled_provider_calls": settled_provider_calls,
        "unsettled_attempt_count": 0,
        "candidate_chars": candidate_chars,
        "candidate_sha256": candidate_sha256,
        "candidate_artifact_path": artifact_path.relative_to(project_root).as_posix(),
        "candidate_artifact_sha256": artifact_sha256,
        "candidate_artifact_bytes": len(artifact_raw),
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_attempted": False,
        "writeback_applied": False,
        "original_problem_count": original_problem_count,
        "final_problem_count": final_problem_count,
        "convergence_reason": convergence_reason,
        "new_problem_codes": list(new_problem_codes),
        "source_error_type": "RepairNotConvergingError",
        "source_error_message": error_message,
        "automatic_retry": False,
        "safe_to_retry": False,
        "recovery_required": True,
    }


def _classify_repair_not_converging_failure(
    project_root: str | Path,
    chapter: int,
    *,
    exit_code: Any,
    stderr_tail: Any,
    prior_fingerprints: Mapping[str, str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any] | None:
    """Classify one uniquely evidenced, fully settled repair-convergence failure."""

    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code == 0
    ):
        return None
    stderr_text = str(stderr_tail or "")
    if "RepairNotConvergingError: chapter_repair_not_converging:" not in stderr_text:
        return None
    root = Path(project_root).resolve(strict=True)
    fingerprints = _strict_chapter_run_artifact_fingerprints(root, chapter)
    if fingerprints is None:
        return None
    window_start = _parse_timestamp(started_at)
    window_end = _parse_timestamp(finished_at)
    matches: list[dict[str, Any]] = []
    for relative, digest in fingerprints.items():
        if prior_fingerprints is not None and prior_fingerprints.get(relative) == digest:
            continue
        try:
            path = _safe_project_file(root, relative)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                continue
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping):
                continue
            run = value.get("run")
            run_finished = _parse_timestamp(
                run.get("finished_at") if isinstance(run, Mapping) else None
            )
            if window_start is not None or window_end is not None:
                if run_finished is None:
                    continue
                finished_epoch = run_finished.timestamp()
                if window_start is not None and finished_epoch < window_start.timestamp() - 2.0:
                    continue
                if window_end is not None and finished_epoch > window_end.timestamp() + 2.0:
                    continue
            evidence = _repair_not_converging_evidence(
                root,
                value,
                chapter=chapter,
                stderr_text=stderr_text,
            )
            if evidence is None:
                continue
            details = {
                **evidence,
                "run_record_path": relative,
                "run_record_sha256": digest,
            }
            matches.append(
                {
                    "result": dict(value),
                    "error": {
                        "code": REPAIR_NOT_CONVERGING_ERROR,
                        "message": (
                            f"第 {chapter} 章已生成候选正文，但自动修订未收敛："
                            f"问题从 {details['original_problem_count']} 项变为 "
                            f"{details['final_problem_count']} 项；正式正文尚未提交。"
                        ),
                        "details": details,
                        "exit_code": exit_code,
                        "automatic_retry": False,
                        "safe_to_retry": False,
                        "recovery_required": True,
                    },
                }
            )
        except (OSError, UnicodeError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return matches[0] if len(matches) == 1 else None


def _stale_scene_evidence_evidence(
    project_root: Path,
    value: Mapping[str, Any],
    *,
    chapter: int,
    stderr_text: str,
) -> dict[str, Any] | None:
    """Validate one failed run down to its retained candidate and settled receipts."""

    run = value.get("run")
    if not isinstance(run, Mapping) or int(run.get("chapter_index") or 0) != int(chapter):
        return None
    run_error = run.get("error")
    if not isinstance(run_error, Mapping) or run_error.get("type") != "SceneEvidenceAlignmentError":
        return None
    error_message = run_error.get("message")
    if not isinstance(error_message, str) or not error_message.startswith("stale_scene_evidence:"):
        return None
    alignment_reason = error_message[len("stale_scene_evidence:"):].strip()
    if not alignment_reason or f"SceneEvidenceAlignmentError: {error_message}" not in stderr_text:
        return None
    if (
        run.get("status") != "failed"
        or run.get("accepted") is not False
        or run.get("committed") is not False
        or value.get("accepted") is not False
        or value.get("committed") is not False
    ):
        return None

    execution_evidence = run.get("execution_evidence")
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence, Mapping)
        else None
    )
    if not isinstance(budget, Mapping):
        return None
    provider_calls = budget.get("provider_calls")
    unsettled = budget.get("unsettled_attempt_count")
    if (
        not isinstance(provider_calls, int)
        or isinstance(provider_calls, bool)
        or provider_calls < 1
        or not isinstance(unsettled, int)
        or isinstance(unsettled, bool)
        or unsettled != 0
        or _result_is_uncertain(value)
    ):
        return None
    settled_provider_calls = _settled_provider_calls_from_trace(run)
    if settled_provider_calls != provider_calls:
        return None
    if not _provider_call_artifacts_are_settled(
        project_root,
        run,
        expected_calls=provider_calls,
    ):
        return None

    persistence = run.get("persistence")
    if persistence not in (None, {}):
        return None
    story_project = run.get("story_project")
    writeback = story_project.get("writeback") if isinstance(story_project, Mapping) else None
    if (
        not isinstance(writeback, Mapping)
        or writeback.get("attempted") is not False
        or writeback.get("applied") is not False
    ):
        return None
    try:
        if resolve_prose(project_root, chapter).candidates:
            return None
    except (OSError, RuntimeError, ValueError):
        return None

    candidate = value.get("chapter")
    run_chapter = run.get("chapter")
    if not isinstance(candidate, str) or not isinstance(run_chapter, Mapping):
        return None
    candidate_chars = run_chapter.get("chars")
    artifact = run_chapter.get("artifact")
    integrity_audit = run_chapter.get("integrity_audit")
    if (
        not isinstance(candidate_chars, int)
        or isinstance(candidate_chars, bool)
        or candidate_chars < 1
        or len(candidate) != candidate_chars
        or not isinstance(artifact, Mapping)
        or artifact.get("chars") != candidate_chars
        or not isinstance(integrity_audit, Mapping)
    ):
        return None
    candidate_sha256 = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    if integrity_audit.get("final_chapter_sha256") != candidate_sha256:
        return None
    artifact_path_value = artifact.get("path")
    artifact_sha256 = artifact.get("sha256")
    if not isinstance(artifact_path_value, str) or not isinstance(artifact_sha256, str):
        return None
    try:
        artifact_path = _safe_project_file(project_root, artifact_path_value)
        artifact_path.relative_to(
            project_root / ".novelagent" / "runtime" / "chapters"
        )
        artifact_raw = artifact_path.read_bytes()
        artifact_text = artifact_raw.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        hashlib.sha256(artifact_raw).hexdigest() != artifact_sha256
        or artifact_text.count(candidate) != 1
        or not artifact_text.endswith((candidate + "\n", candidate + "\r\n"))
    ):
        return None

    trace = run.get("trace")
    if not isinstance(trace, (list, tuple)):
        return None
    failures = [
        event
        for event in trace
        if isinstance(event, Mapping)
        and event.get("action") == "repair_if_needed"
        and event.get("status") == "failed"
        and event.get("error_type") == "SceneEvidenceAlignmentError"
        and event.get("error_message") == error_message
    ]
    if len(failures) != 1:
        return None
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    return {
        "failure_origin": "stale_scene_evidence",
        "run_id": run_id,
        "provider_calls": provider_calls,
        "settled_provider_calls": settled_provider_calls,
        "unsettled_attempt_count": 0,
        "candidate_chars": candidate_chars,
        "candidate_sha256": candidate_sha256,
        "candidate_artifact_path": artifact_path.relative_to(project_root).as_posix(),
        "candidate_artifact_sha256": artifact_sha256,
        "candidate_artifact_bytes": len(artifact_raw),
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_attempted": False,
        "writeback_applied": False,
        "alignment_reason": alignment_reason,
        "source_error_type": "SceneEvidenceAlignmentError",
        "source_error_message": error_message,
        "automatic_retry": False,
        "safe_to_retry": False,
        "recovery_required": True,
    }


def _classify_stale_scene_evidence_failure(
    project_root: str | Path,
    chapter: int,
    *,
    exit_code: Any,
    stderr_tail: Any,
    prior_fingerprints: Mapping[str, str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any] | None:
    """Classify one uniquely evidenced, fully settled scene-alignment failure."""

    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code == 0
    ):
        return None
    stderr_text = str(stderr_tail or "")
    if "SceneEvidenceAlignmentError: stale_scene_evidence:" not in stderr_text:
        return None
    root = Path(project_root).resolve(strict=True)
    fingerprints = _strict_chapter_run_artifact_fingerprints(root, chapter)
    if fingerprints is None:
        return None
    window_start = _parse_timestamp(started_at)
    window_end = _parse_timestamp(finished_at)
    matches: list[dict[str, Any]] = []
    for relative, digest in fingerprints.items():
        if prior_fingerprints is not None and prior_fingerprints.get(relative) == digest:
            continue
        try:
            path = _safe_project_file(root, relative)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                continue
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping):
                continue
            run = value.get("run")
            run_finished = _parse_timestamp(
                run.get("finished_at") if isinstance(run, Mapping) else None
            )
            if window_start is not None or window_end is not None:
                if run_finished is None:
                    continue
                finished_epoch = run_finished.timestamp()
                if window_start is not None and finished_epoch < window_start.timestamp() - 2.0:
                    continue
                if window_end is not None and finished_epoch > window_end.timestamp() + 2.0:
                    continue
            evidence = _stale_scene_evidence_evidence(
                root,
                value,
                chapter=chapter,
                stderr_text=stderr_text,
            )
            if evidence is None:
                continue
            details = {
                **evidence,
                "run_record_path": relative,
                "run_record_sha256": digest,
            }
            matches.append(
                {
                    "result": dict(value),
                    "error": {
                        "code": STALE_SCENE_EVIDENCE_ERROR,
                        "message": (
                            f"第 {chapter} 章已生成 {details['candidate_chars']} 字候选正文，"
                            "但自动修订与场景证据对不齐；正式正文尚未提交。"
                        ),
                        "details": details,
                        "exit_code": exit_code,
                        "automatic_retry": False,
                        "safe_to_retry": False,
                        "recovery_required": True,
                    },
                }
            )
        except (OSError, UnicodeError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return matches[0] if len(matches) == 1 else None


_STRUCTURED_CHARACTER_BUDGET_CODES = (
    "required_json_item_exceeds_budget",
    "required_markdown_context_exceeds_budget",
    "required_structured_entry_exceeds_budget",
    "required_structured_manifest_exceeds_budget",
)
_STRUCTURED_CHARACTER_BUDGET_PATTERN = re.compile(
    r"(?P<code>"
    + "|".join(_STRUCTURED_CHARACTER_BUDGET_CODES)
    + r"): "
    r"(?:required (?:complete Markdown/JSON )?entries|selection and manifest) "
    r"exceed(?:s)? (?P<hard>[0-9]+) characters"
)


def _zero_call_pre_provider_run_guards(
    value: Mapping[str, Any],
    run: Mapping[str, Any],
) -> bool:
    execution_evidence = run.get("execution_evidence")
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence, Mapping)
        else None
    )
    if not isinstance(budget, Mapping):
        return False
    provider_calls = budget.get("provider_calls")
    unsettled = budget.get("unsettled_attempt_count")
    if (
        not isinstance(provider_calls, int)
        or isinstance(provider_calls, bool)
        or provider_calls != 0
        or not isinstance(unsettled, int)
        or isinstance(unsettled, bool)
        or unsettled != 0
    ):
        return False
    run_chapter = run.get("chapter")
    chapter_chars = run_chapter.get("chars") if isinstance(run_chapter, Mapping) else None
    if not isinstance(chapter_chars, int) or isinstance(chapter_chars, bool) or chapter_chars != 0:
        return False
    generated_chapter = value.get("chapter")
    if generated_chapter is not None and generated_chapter != "":
        return False
    if value.get("accepted") is not False or value.get("committed") is not False:
        return False
    if run.get("accepted") is not False or run.get("committed") is not False:
        return False
    persistence = run.get("persistence")
    if isinstance(persistence, Mapping) and (
        persistence.get("committed") is True
        or persistence.get("state") in {"committed", "completed"}
    ):
        return False
    story_project = run.get("story_project")
    writeback = story_project.get("writeback") if isinstance(story_project, Mapping) else None
    if isinstance(writeback, Mapping) and writeback.get("applied") is True:
        return False
    run_id = run.get("id")
    return isinstance(run_id, str) and bool(run_id.strip())


def _pre_provider_structured_character_budget_evidence(
    value: Mapping[str, Any],
    *,
    chapter: int,
) -> dict[str, Any] | None:
    run = value.get("run")
    if not isinstance(run, Mapping) or int(run.get("chapter_index") or 0) != int(chapter):
        return None
    run_error = run.get("error")
    if not isinstance(run_error, Mapping) or run_error.get("type") != "StructuredContextError":
        return None
    error_message = run_error.get("message")
    if not isinstance(error_message, str):
        return None
    match = _STRUCTURED_CHARACTER_BUDGET_PATTERN.fullmatch(error_message)
    if match is None:
        return None
    hard = int(match.group("hard"))
    if hard < 1:
        return None
    if not _zero_call_pre_provider_run_guards(value, run):
        return None
    return {
        "stage": "structured_context",
        "code": match.group("code"),
        "hard": hard,
        "unit": "characters",
        "provider_reached": False,
        "provider_calls": 0,
        "unsettled_attempt_count": 0,
        "chapter_chars": 0,
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_applied": False,
        "run_id": run.get("id"),
        "source_error_type": "StructuredContextError",
        "source_error_message": error_message,
    }


def _pre_provider_context_evidence(
    value: Mapping[str, Any],
    *,
    chapter: int,
) -> dict[str, Any] | None:
    structured = _pre_provider_structured_character_budget_evidence(value, chapter=chapter)
    if structured is not None:
        return structured
    run = value.get("run")
    if not isinstance(run, Mapping) or int(run.get("chapter_index") or 0) != int(chapter):
        return None
    run_error = run.get("error")
    if not isinstance(run_error, Mapping) or run_error.get("type") != "ContextBudgetError":
        return None
    error_message = run_error.get("message")
    if not isinstance(error_message, str) or "story_project_context_budget_exceeded" not in error_message:
        return None
    match = _CONTEXT_BUDGET_PATTERN.search(error_message)
    if match is None:
        return None
    required = int(match.group("required"))
    hard = int(match.group("hard"))
    if hard < 1 or required <= hard:
        return None

    execution_evidence = run.get("execution_evidence")
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence, Mapping)
        else None
    )
    if not isinstance(budget, Mapping):
        return None
    provider_calls = budget.get("provider_calls")
    unsettled = budget.get("unsettled_attempt_count")
    if (
        not isinstance(provider_calls, int)
        or isinstance(provider_calls, bool)
        or provider_calls != 0
        or not isinstance(unsettled, int)
        or isinstance(unsettled, bool)
        or unsettled != 0
    ):
        return None

    run_chapter = run.get("chapter")
    chapter_chars = run_chapter.get("chars") if isinstance(run_chapter, Mapping) else None
    if not isinstance(chapter_chars, int) or isinstance(chapter_chars, bool) or chapter_chars != 0:
        return None
    generated_chapter = value.get("chapter")
    if generated_chapter is not None and generated_chapter != "":
        return None
    if value.get("accepted") is not False or value.get("committed") is not False:
        return None
    if run.get("accepted") is not False or run.get("committed") is not False:
        return None

    persistence = run.get("persistence")
    if isinstance(persistence, Mapping) and (
        persistence.get("committed") is True
        or persistence.get("state") in {"committed", "completed"}
    ):
        return None
    story_project = run.get("story_project")
    writeback = story_project.get("writeback") if isinstance(story_project, Mapping) else None
    if isinstance(writeback, Mapping) and writeback.get("applied") is True:
        return None
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    return {
        "stage": match.group("stage"),
        "required": required,
        "hard": hard,
        "excess": required - hard,
        "unit": "tokens",
        "provider_reached": False,
        "provider_calls": 0,
        "unsettled_attempt_count": 0,
        "chapter_chars": 0,
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_applied": False,
        "run_id": run_id,
        "source_error_type": "ContextBudgetError",
        "source_error_message": error_message,
    }


def _classify_pre_provider_context_failure(
    project_root: str | Path,
    chapter: int,
    *,
    exit_code: Any,
    stderr_tail: Any,
    prior_fingerprints: Mapping[str, str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any] | None:
    """Classify one zero-call local rejection; ambiguous evidence stays generic."""

    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code == 0
    ):
        return None
    stderr_text = str(stderr_tail or "")
    if (
        "ContextBudgetError" not in stderr_text
        and "StructuredContextError" not in stderr_text
    ):
        return None
    root = Path(project_root).resolve(strict=True)
    run_dir = root / ".novelagent" / "runtime" / "runs"
    if not run_dir.is_dir():
        return None
    window_start = _parse_timestamp(started_at)
    window_end = _parse_timestamp(finished_at)
    matches: list[dict[str, Any]] = []
    for path in run_dir.glob("chapter_*.json"):
        try:
            safe_path = _safe_project_file(root, path)
            raw = safe_path.read_bytes()
            relative = safe_path.relative_to(root).as_posix()
            digest = hashlib.sha256(raw).hexdigest()
            if prior_fingerprints is not None and prior_fingerprints.get(relative) == digest:
                continue
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping):
                continue
            evidence = _pre_provider_context_evidence(value, chapter=chapter)
            if evidence is None or evidence["source_error_message"] not in stderr_text:
                continue
            if window_start is not None or window_end is not None:
                run = value.get("run")
                run_finished = _parse_timestamp(
                    run.get("finished_at") if isinstance(run, Mapping) else None
                )
                if run_finished is None:
                    continue
                finished_epoch = run_finished.timestamp()
                if window_start is not None and finished_epoch < window_start.timestamp() - 2.0:
                    continue
                if window_end is not None and finished_epoch > window_end.timestamp() + 2.0:
                    continue
            formal_prose_exists = bool(resolve_prose(root, chapter).candidates)
            if formal_prose_exists:
                continue
            details = {
                **evidence,
                "formal_prose_exists": False,
                "run_record_path": relative,
                "run_record_sha256": digest,
                "safe_to_retry": True,
            }
            if details.get("unit") == "characters":
                message = (
                    f"第 {chapter} 章在调用模型前被本地上下文装配拒绝："
                    f"必填上下文超过了 {details['hard']} 字符的预算；尚未调用模型。"
                )
            else:
                message = (
                    f"第 {chapter} 章在调用 Provider 前被本地上下文预算拒绝："
                    f"需要 {details['required']} tokens，硬上限 {details['hard']}，"
                    f"超出 {details['excess']}；尚未调用模型。"
                )
            matches.append(
                {
                    "result": dict(value),
                    "error": {
                        "code": PRE_PROVIDER_CONTEXT_BUDGET_ERROR,
                        "message": message,
                        "details": details,
                        "exit_code": exit_code,
                        "automatic_retry": False,
                        "safe_to_retry": True,
                    },
                }
            )
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return matches[0] if len(matches) == 1 else None


def _classify_pre_provider_provenance_failure(
    project_root: str | Path,
    chapter: int,
    *,
    exit_code: Any,
    stderr_tail: Any,
    paths: Mapping[str, Path],
    prior_run_fingerprints: Mapping[str, str] | None = None,
    prior_model_call_fingerprints: Mapping[str, str] | None = None,
    legacy_error_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Classify a Git provenance failure that occurred before provider reachability.

    The terminal exception alone is not enough.  The classifier also binds the
    job's empty stdout, final progress event, immutable story artifacts, and the
    absence of chapter prose.  Any ambiguity remains the legacy fail-closed
    error and cannot be resumed.
    """

    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code == 0
    ):
        return None
    stderr_text = str(stderr_tail or "")
    match = _PROVENANCE_GIT_PATTERN.search(stderr_text)
    if match is None or "_capture_git_state" not in stderr_text:
        return None
    if _contains_uncertainty(stderr_text):
        return None
    try:
        root = Path(project_root).resolve(strict=True)
        stdout_path = Path(paths["stdout"]).resolve(strict=True)
        stderr_path = Path(paths["stderr"]).resolve(strict=True)
        progress_path = Path(paths["progress"]).resolve(strict=True)
        stdout_raw = stdout_path.read_bytes()
        stderr_raw = stderr_path.read_bytes()
        stderr_file_text = stderr_raw.decode("utf-8")
    except (KeyError, OSError, UnicodeError, RuntimeError, ValueError):
        return None
    normalized_stderr_file = stderr_file_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_stderr_tail = stderr_text.replace("\r\n", "\n").replace("\r", "\n")
    if stdout_raw or not normalized_stderr_file.endswith(normalized_stderr_tail):
        return None
    progress = _progress_provenance_failure(progress_path)
    if progress is None:
        return None
    attempt_started = _parse_timestamp(progress["started_at"])
    if attempt_started is None:
        return None

    run_fingerprints = _strict_chapter_run_artifact_fingerprints(root, chapter)
    model_call_fingerprints = _strict_model_call_artifact_fingerprints(root)
    if run_fingerprints is None or model_call_fingerprints is None:
        return None
    if prior_run_fingerprints is not None:
        if dict(prior_run_fingerprints) != run_fingerprints:
            return None
    else:
        run_dir = root / ".novelagent" / "runtime" / "runs"
        if run_dir.is_dir() and _artifact_changed_since(
            run_dir.glob("chapter_*.json"), attempt_started
        ):
            return None
    if prior_model_call_fingerprints is not None:
        if dict(prior_model_call_fingerprints) != model_call_fingerprints:
            return None
    else:
        execution_root = root / ".novelagent" / "runtime" / "runs" / "executions"
        model_paths = execution_root.glob("*/model_calls/**/*") if execution_root.is_dir() else ()
        if _artifact_changed_since(model_paths, attempt_started):
            return None
    try:
        if resolve_prose(root, chapter).candidates:
            return None
    except (OSError, RuntimeError, ValueError):
        return None

    artifact_evidence = {
        "stdout": {
            "path": str(stdout_path),
            "sha256": hashlib.sha256(stdout_raw).hexdigest(),
            "bytes": 0,
        },
        "stderr": {
            "path": str(stderr_path),
            "sha256": hashlib.sha256(stderr_raw).hexdigest(),
            "bytes": len(stderr_raw),
        },
        "progress": {
            "path": str(progress_path),
            "sha256": progress["sha256"],
            "failure_sequence": progress["failure_sequence"],
        },
    }
    details = {
        "dependency": "git",
        "stage": "execution_provenance",
        "failure_origin": "engine_provenance_capture",
        "provider_reached": False,
        "provider_calls": 0,
        "unsettled_attempt_count": 0,
        "chapter_chars": 0,
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_applied": False,
        "source_error_type": "ProvenanceCaptureError",
        "source_error_message": match.group(0).split(": ", 1)[1],
        "git_capture_scope": match.group("scope"),
        "attempt_started_at": progress["started_at"],
        "attempt_failed_at": progress["failed_at"],
        "failure_artifacts": artifact_evidence,
        "chapter_run_manifest_sha256": _fingerprint_manifest_sha256(run_fingerprints),
        "model_call_manifest_sha256": _fingerprint_manifest_sha256(model_call_fingerprints),
        "legacy_error_sha256": legacy_error_sha256,
        "retry_precondition": "git_preflight_passed",
        "safe_to_retry": True,
    }
    return {
        "error": {
            "code": PRE_PROVIDER_PROVENANCE_ERROR,
            "message": (
                f"第 {chapter} 章在调用 Provider 前无法采集 Git 执行来源信息；"
                "模型尚未调用，修复本机 Git 后可由作者确认重试。"
            ),
            "details": details,
            "exit_code": exit_code,
            "automatic_retry": False,
            "safe_to_retry": True,
        }
    }


def _pre_provider_provenance_preflight_error(
    project_root: str | Path,
    chapter: int,
    *,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    """Build zero-call evidence when Git is unavailable before Popen."""

    root = Path(project_root).resolve(strict=True)
    try:
        formal_prose_exists = bool(resolve_prose(root, chapter).candidates)
    except (OSError, RuntimeError, ValueError):
        formal_prose_exists = True
    run_fingerprints = _strict_chapter_run_artifact_fingerprints(root, chapter)
    model_call_fingerprints = _strict_model_call_artifact_fingerprints(root)
    evidence_deterministic = run_fingerprints is not None and model_call_fingerprints is not None
    run_fingerprints = run_fingerprints or {}
    model_call_fingerprints = model_call_fingerprints or {}
    safe_to_retry = not formal_prose_exists and evidence_deterministic
    details = {
        "dependency": "git",
        "stage": "execution_provenance_preflight",
        "failure_origin": "git_preflight",
        "provider_reached": False,
        "provider_calls": 0,
        "unsettled_attempt_count": 0,
        "chapter_chars": 0 if not formal_prose_exists else None,
        "formal_prose_exists": formal_prose_exists,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_applied": False,
        "source_error_type": "GitPreflightError",
        "source_error_message": "Git provenance preflight is unavailable",
        "chapter_run_manifest_sha256": _fingerprint_manifest_sha256(run_fingerprints),
        "model_call_manifest_sha256": _fingerprint_manifest_sha256(model_call_fingerprints),
        "preflight_checked_at": _utc_now(),
        "preflight": {
            "ok": False,
            "dependency": "git",
        },
        "retry_precondition": "git_preflight_passed",
        "safe_to_retry": safe_to_retry,
    }
    return {
        "code": PRE_PROVIDER_PROVENANCE_ERROR,
        "message": (
            f"第 {chapter} 章尚未启动生成：本机 Git 执行来源检查不可用；"
            "模型未调用，修复 Git 后可由作者确认重试。"
        ),
        "details": details,
        "automatic_retry": False,
        "safe_to_retry": safe_to_retry,
    }


def _has_pre_provider_retry_evidence(connection: Any, parent: Any) -> bool:
    if parent["state"] != "awaiting_input":
        return False
    error = _stored_job_error(parent)
    if not _is_pre_provider_context_budget_error(error) or error.get("safe_to_retry") is not True:
        return False
    details = error.get("details")
    if not isinstance(details, Mapping) or details.get("safe_to_retry") is not True:
        return False
    if connection.execute(
        "SELECT 1 FROM drafts WHERE project_id = ? AND chapter = ?",
        (str(parent["project_id"]), int(parent["next_chapter"])),
    ).fetchone() is not None:
        return False
    project_row = connection.execute(
        "SELECT root FROM projects WHERE id = ?", (str(parent["project_id"]),)
    ).fetchone()
    if project_row is None:
        return False
    try:
        project_root = Path(str(project_row["root"])).resolve(strict=True)
        if resolve_prose(project_root, int(parent["next_chapter"])).candidates:
            return False
        run_path_value = details.get("run_record_path")
        run_sha = details.get("run_record_sha256")
        if not isinstance(run_path_value, str) or not isinstance(run_sha, str):
            return False
        run_path = _safe_project_file(project_root, run_path_value)
        run_path.relative_to(project_root / ".novelagent" / "runtime" / "runs")
        raw = run_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != run_sha:
            return False
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(value, Mapping):
        return False
    evidence = _pre_provider_context_evidence(
        value,
        chapter=int(parent["next_chapter"]),
    )
    if evidence is None:
        return False
    for key, expected in evidence.items():
        if details.get(key) != expected:
            return False
    compact = None
    if parent["result_json"]:
        try:
            compact = json.loads(str(parent["result_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if not isinstance(compact, Mapping):
        return False
    return (
        compact.get("run_id") == evidence["run_id"]
        and compact.get("accepted") is False
        and compact.get("committed") is False
    )


def _has_pre_provider_provenance_retry_evidence(connection: Any, parent: Any) -> bool:
    if parent["state"] != "awaiting_input" or parent["result_json"] is not None:
        return False
    error = _stored_job_error(parent)
    if not _is_pre_provider_provenance_error(error) or error.get("safe_to_retry") is not True:
        return False
    details = error.get("details")
    required = {
        "dependency": "git",
        "provider_reached": False,
        "provider_calls": 0,
        "unsettled_attempt_count": 0,
        "chapter_chars": 0,
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "persistence_committed": False,
        "writeback_applied": False,
        "retry_precondition": "git_preflight_passed",
        "safe_to_retry": True,
    }
    if not isinstance(details, Mapping) or any(details.get(key) != value for key, value in required.items()):
        return False
    failure_origin = details.get("failure_origin")
    if failure_origin == "engine_provenance_capture":
        if (
            details.get("stage") != "execution_provenance"
            or details.get("source_error_type") != "ProvenanceCaptureError"
        ):
            return False
    elif failure_origin == "git_preflight":
        if (
            details.get("stage") != "execution_provenance_preflight"
            or details.get("source_error_type") != "GitPreflightError"
        ):
            return False
    else:
        return False
    if connection.execute(
        "SELECT 1 FROM drafts WHERE project_id = ? AND chapter = ?",
        (str(parent["project_id"]), int(parent["next_chapter"])),
    ).fetchone() is not None:
        return False
    project_row = connection.execute(
        "SELECT root FROM projects WHERE id = ?", (str(parent["project_id"]),)
    ).fetchone()
    if project_row is None:
        return False
    try:
        project_root = Path(str(project_row["root"])).resolve(strict=True)
        if resolve_prose(project_root, int(parent["next_chapter"])).candidates:
            return False
        if failure_origin == "engine_provenance_capture":
            artifacts = details.get("failure_artifacts")
            if not isinstance(artifacts, Mapping):
                return False
            expected_names = {
                "stdout": "stdout.json",
                "stderr": "stderr.log",
                "progress": "progress.jsonl",
            }
            for key, expected_name in expected_names.items():
                artifact = artifacts.get(key)
                if not isinstance(artifact, Mapping):
                    return False
                path_value = artifact.get("path")
                digest = artifact.get("sha256")
                if not isinstance(path_value, str) or not isinstance(digest, str):
                    return False
                path = Path(path_value).resolve(strict=True)
                if (
                    path.name != expected_name
                    or path.parent.name != f"chapter_{int(parent['next_chapter']):04d}"
                    or path.parent.parent.name != str(parent["id"])
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                ):
                    return False
        current_runs = _strict_chapter_run_artifact_fingerprints(
            project_root, int(parent["next_chapter"])
        )
        current_model_calls = _strict_model_call_artifact_fingerprints(project_root)
        if current_runs is None or current_model_calls is None:
            return False
        if details.get("chapter_run_manifest_sha256") != _fingerprint_manifest_sha256(current_runs):
            return False
        if details.get("model_call_manifest_sha256") != _fingerprint_manifest_sha256(
            current_model_calls
        ):
            return False
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    return _git_provenance_preflight(project_root)["ok"] is True


def _recovery_draft_text_equivalent(left: str, right: str) -> bool:
    """Match checkpoint prose to the author draft after language-validation strip()."""

    return left == right or left.strip() == right.strip()


def _manual_recovery_draft_matches(
    connection: Any,
    *,
    config: Mapping[str, Any],
    checkpoint: Path,
    payload: Mapping[str, Any],
    project_root: Path,
    project_id: str,
    chapter: int,
    latest_run_id: str | None,
) -> bool:
    """Verify the exact validated author draft behind a manual recovery receipt."""

    draft_id = config.get("draft_id")
    expected_revision = config.get("draft_revision")
    manual_path_value = config.get("manual_draft_path")
    complete_draft = payload.get("complete_draft")
    if (
        not isinstance(draft_id, str)
        or not isinstance(expected_revision, int)
        or not isinstance(manual_path_value, str)
        or not isinstance(complete_draft, Mapping)
    ):
        return False
    text = complete_draft.get("text")
    digest = complete_draft.get("sha256")
    if not isinstance(text, str) or not isinstance(digest, str) or _sha256_text(text) != digest:
        return False
    draft = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if draft is None:
        return False
    try:
        revision = int(draft["revision"])
    except (TypeError, ValueError):
        return False
    draft_text = str(draft["text"])
    if (
        str(draft["project_id"]) != project_id
        or int(draft["chapter"]) != chapter
        or str(draft["state"]) != "validated"
        or revision != expected_revision + 1
        or not _recovery_draft_text_equivalent(draft_text, text)
        or str(draft["sha256"]) != _sha256_text(draft_text)
    ):
        return False
    draft_source_run_id = str(draft["source_run_id"] or "").strip() or None
    if not _source_run_covers_empty_pre_provider_latest(
        project_root,
        chapter=chapter,
        latest_run_id=latest_run_id,
        source_run_id=draft_source_run_id,
    ):
        return False
    try:
        archive_path = Path(manual_path_value).resolve(strict=True)
        archive_path.relative_to(project_root)
        if _is_reparse_or_link(archive_path) or not archive_path.is_file():
            return False
        if not _recovery_draft_text_equivalent(archive_path.read_text(encoding="utf-8"), text):
            return False
        validation = json.loads(str(draft["validation_json"]))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(validation, Mapping) or validation.get("ok") is not True:
        return False
    checkpoint_value = validation.get("checkpoint_path")
    if not isinstance(checkpoint_value, str) or not checkpoint_value:
        return False
    try:
        return Path(checkpoint_value).resolve(strict=True) == checkpoint
    except OSError:
        return False



def _empty_pre_provider_context_failure(value: Mapping[str, Any], *, chapter: int) -> bool:
    """Return True when a chapter run failed before any provider call or prose existed."""

    run = value.get("run")
    if not isinstance(run, Mapping) or int(run.get("chapter_index") or 0) != int(chapter):
        return False
    if run.get("status") not in {"failed", "rejected"}:
        return False
    if value.get("accepted") is not False or value.get("committed") is not False:
        return False
    if run.get("accepted") is not False or run.get("committed") is not False:
        return False
    run_error = run.get("error")
    if not isinstance(run_error, Mapping):
        return False
    error_message = run_error.get("message")
    if not isinstance(error_message, str):
        return False
    markers = (
        "required_markdown_context_exceeds_budget",
        "required_json_item_exceeds_budget",
        "story_project_context_budget_exceeded",
    )
    if not any(marker in error_message for marker in markers):
        return False
    error_type = str(run_error.get("type") or "")
    if error_type not in {"StructuredContextError", "ContextBudgetError", "WorkflowExecutionError"}:
        return False
    execution_evidence = run.get("execution_evidence")
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence, Mapping)
        else None
    )
    if not isinstance(budget, Mapping):
        return False
    provider_calls = budget.get("provider_calls")
    unsettled = budget.get("unsettled_attempt_count")
    if (
        not isinstance(provider_calls, int)
        or isinstance(provider_calls, bool)
        or provider_calls != 0
        or not isinstance(unsettled, int)
        or isinstance(unsettled, bool)
        or unsettled != 0
    ):
        return False
    run_chapter = run.get("chapter")
    chapter_chars = run_chapter.get("chars") if isinstance(run_chapter, Mapping) else None
    if not isinstance(chapter_chars, int) or isinstance(chapter_chars, bool) or chapter_chars != 0:
        return False
    generated_chapter = value.get("chapter")
    if isinstance(generated_chapter, str) and generated_chapter.strip():
        return False
    persistence = run.get("persistence")
    if isinstance(persistence, Mapping) and (
        persistence.get("committed") is True
        or persistence.get("state") in {"committed", "completed"}
    ):
        return False
    story_project = run.get("story_project")
    writeback = story_project.get("writeback") if isinstance(story_project, Mapping) else None
    if isinstance(writeback, Mapping) and writeback.get("applied") is True:
        return False
    return True


def _latest_run_is_empty_pre_provider_failure(project_root: Path, chapter: int) -> bool:
    try:
        run_manifest_ok, latest_run_id = _latest_strict_chapter_run_id(
            project_root,
            int(chapter),
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    if not run_manifest_ok or not isinstance(latest_run_id, str) or not latest_run_id:
        return False
    try:
        run_path = _safe_project_file(
            Path(project_root),
            Path(project_root) / ".novelagent" / "runtime" / "runs" / f"{latest_run_id}.json",
        )
        value = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(value, Mapping) and _empty_pre_provider_context_failure(
        value,
        chapter=int(chapter),
    )


def _source_run_covers_empty_pre_provider_latest(
    project_root: Path,
    *,
    chapter: int,
    latest_run_id: str | None,
    source_run_id: str | None,
) -> bool:
    if source_run_id == latest_run_id:
        return True
    if not source_run_id or not latest_run_id:
        return False
    return _latest_run_is_empty_pre_provider_failure(project_root, int(chapter))


def _has_completed_recovery_evidence(connection: Any, parent: Any) -> bool:
    if parent["execution_contract"] != CURRENT_JOB_EXECUTION_CONTRACT:
        return False
    candidates = connection.execute(
        """
        SELECT config_json, result_json, finished_at, execution_contract FROM jobs
        WHERE project_id = ? AND kind = 'locked_chapter_recovery'
          AND start_chapter = ? AND end_chapter = ? AND state = 'completed'
        ORDER BY finished_at DESC, created_at DESC
        """,
        (
            str(parent["project_id"]),
            int(parent["next_chapter"]),
            int(parent["next_chapter"]),
        ),
    ).fetchall()
    project_row = connection.execute(
        "SELECT root FROM projects WHERE id = ?", (str(parent["project_id"]),)
    ).fetchone()
    if project_row is None:
        return False
    try:
        project_root = Path(str(project_row["root"])).resolve(strict=True)
    except OSError:
        return False
    parent_updated_at = _parse_timestamp(parent["updated_at"])
    if parent_updated_at is None:
        return False
    try:
        run_manifest_ok, latest_run_id = _latest_strict_chapter_run_id(
            project_root,
            int(parent["next_chapter"]),
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    if not run_manifest_ok:
        return False
    for candidate in candidates:
        if candidate["execution_contract"] != parent["execution_contract"]:
            continue
        try:
            config = json.loads(str(candidate["config_json"]))
            result = json.loads(str(candidate["result_json"]))
        except (TypeError, ValueError):
            continue
        if config.get("parent_job_id") != str(parent["id"]) or result.get("ok") is not True:
            continue
        candidate_finished_at = _parse_timestamp(candidate["finished_at"])
        if candidate_finished_at is None or candidate_finished_at < parent_updated_at:
            continue
        bound_revision = config.get("parent_failure_revision")
        if bound_revision is not None:
            try:
                if int(bound_revision) != int(parent["revision"]):
                    continue
            except (TypeError, ValueError):
                continue
        checkpoint_value = result.get("checkpoint_path")
        if not isinstance(checkpoint_value, str) or not checkpoint_value:
            continue
        try:
            checkpoint = Path(checkpoint_value).resolve(strict=True)
            checkpoint.relative_to(project_root)
            if _is_reparse_or_link(checkpoint) or not checkpoint.is_file():
                continue
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or int(payload.get("chapter_index") or 0) != int(
            parent["next_chapter"]
        ):
            continue
        checkpoint_run_id = str(payload.get("source_run_id") or "").strip() or None
        configured_run_id = str(config.get("source_run_id") or "").strip() or None
        if latest_run_id is None:
            if checkpoint_run_id is not None or configured_run_id is not None:
                continue
        else:
            if not _source_run_covers_empty_pre_provider_latest(
                project_root,
                chapter=int(parent["next_chapter"]),
                latest_run_id=latest_run_id,
                source_run_id=checkpoint_run_id,
            ):
                continue
            if "source_run_id" in config and not _source_run_covers_empty_pre_provider_latest(
                project_root,
                chapter=int(parent["next_chapter"]),
                latest_run_id=latest_run_id,
                source_run_id=configured_run_id,
            ):
                continue
        complete_draft = payload.get("complete_draft")
        if payload.get("reason") == "manual_repaired_draft_provided":
            if not _manual_recovery_draft_matches(
                connection,
                config=config,
                checkpoint=checkpoint,
                payload=payload,
                project_root=project_root,
                project_id=str(parent["project_id"]),
                chapter=int(parent["next_chapter"]),
                latest_run_id=latest_run_id,
            ):
                continue
        elif isinstance(complete_draft, dict):
            verified_source = verified_final_recovery_draft(
                run_dir=project_root / ".novelagent" / "runtime" / "runs",
                run_id=latest_run_id or "",
            )
            configured_source_sha256 = config.get("source_draft_sha256")
            if (
                not isinstance(verified_source, dict)
                or (
                    configured_source_sha256 is not None
                    and configured_source_sha256 != verified_source.get("sha256")
                )
                or (
                    payload.get("reason") != "manual_repaired_draft_provided"
                    and (
                        complete_draft.get("text") != verified_source.get("text")
                        or complete_draft.get("sha256")
                        != verified_source.get("sha256")
                    )
                )
            ):
                continue
        return True
    return False


def _compact_result(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    run = value.get("run") if isinstance(value.get("run"), Mapping) else {}
    persistence = run.get("persistence") if isinstance(run.get("persistence"), Mapping) else None
    writeback = (
        (run.get("story_project") or {}).get("writeback")
        if isinstance(run.get("story_project"), Mapping)
        else None
    )
    execution_evidence = (
        run.get("execution_evidence")
        if isinstance(run.get("execution_evidence"), Mapping)
        else {}
    )
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence.get("budget"), Mapping)
        else {}
    )
    run_chapter = run.get("chapter") if isinstance(run.get("chapter"), Mapping) else {}
    return _safe_event_payload(
        {
            "ok": value.get("ok"),
            "status": value.get("status"),
            "action": value.get("action"),
            "reason": value.get("reason"),
            "chapter_index": value.get("chapter_index") or run.get("chapter_index"),
            "committed": value.get("committed"),
            "accepted": value.get("accepted"),
            "run_id": run.get("id"),
            "run_status": run.get("status"),
            "review_status": _review_status(value),
            "repair_attempts": value.get("repair_attempts") or run.get("repair_attempts"),
            "persistence": persistence,
            "writeback": writeback,
            "checkpoint_path": value.get("checkpoint_path"),
            "provider_calls": value.get("provider_calls")
            if value.get("provider_calls") is not None
            else budget.get("provider_calls"),
            "unsettled_attempt_count": budget.get("unsettled_attempt_count"),
            "chapter_chars": run_chapter.get("chars"),
            "draft_source_conflict": value.get("draft_source_conflict"),
        }
    )


def _parse_json_output(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip().lstrip("\ufeff")
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        # Defensive fallback for a provider library that logged before the CLI JSON.
        starts = [index for index, character in enumerate(text) if character == "{"]
        for start in reversed(starts):
            try:
                value = json.loads(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _latest_committed_result(project_root: Path, chapter: int) -> dict[str, Any] | None:
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    if not run_dir.is_dir():
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for path in run_dir.glob("chapter_*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            run = value.get("run") if isinstance(value, dict) else None
            if (
                isinstance(run, dict)
                and int(run.get("chapter_index") or 0) == int(chapter)
                and run.get("committed") is True
                and run.get("status") == "committed"
            ):
                candidates.append((path.stat().st_mtime_ns, value))
        except (OSError, ValueError, TypeError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _result_for_run_id(
    project_root: Path,
    chapter: int,
    run_id: str,
) -> dict[str, Any] | None:
    """Load one exact immutable run result; duplicates remain ambiguous."""

    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    if not run_dir.is_dir():
        return None
    matches: list[dict[str, Any]] = []
    for path in run_dir.glob("chapter_*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            run = value.get("run") if isinstance(value, dict) else None
            if (
                isinstance(run, dict)
                and int(run.get("chapter_index") or 0) == int(chapter)
                and run.get("id") == run_id
            ):
                matches.append(value)
        except (OSError, ValueError, TypeError):
            continue
    return matches[0] if len(matches) == 1 else None


def _job_from_row(row: Any) -> JobRecord:
    return JobRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        kind=str(row["kind"]),
        state=str(row["state"]),
        start_chapter=int(row["start_chapter"]),
        end_chapter=int(row["end_chapter"]),
        next_chapter=int(row["next_chapter"]),
        config=json.loads(str(row["config_json"])),
        result=json.loads(str(row["result_json"])) if row["result_json"] else None,
        error=json.loads(str(row["error_json"])) if row["error_json"] else None,
        manual_confirmation_required=bool(row["manual_confirmation_required"]),
        pause_after_chapter=bool(row["pause_after_chapter"]),
        cancel_requested=bool(row["cancel_requested"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=str(row["started_at"]) if row["started_at"] else None,
        finished_at=str(row["finished_at"]) if row["finished_at"] else None,
        execution_contract=str(row["execution_contract"]) if row["execution_contract"] else None,
    )


def _require_job_execution_contract(row: Any) -> None:
    if row["execution_contract"] != CURRENT_JOB_EXECUTION_CONTRACT:
        raise JobError("任务执行契约不兼容；保留记录用于诊断，禁止在当前代码下直接继续。")


def _draft_from_row(row: Any) -> DraftRecord:
    return DraftRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        chapter=int(row["chapter"]),
        source_run_id=str(row["source_run_id"]) if row["source_run_id"] else None,
        source_path=str(row["source_path"]) if row["source_path"] else None,
        base_sha256=str(row["base_sha256"]),
        text=str(row["text"]),
        sha256=str(row["sha256"]),
        revision=int(row["revision"]),
        state=str(row["state"]),
        validation=json.loads(str(row["validation_json"])) if row["validation_json"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _require_job_row(connection: Any, job_id: str) -> Any:
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (str(job_id),)).fetchone()
    if row is None:
        raise JobError(f"unknown job: {job_id}")
    return row


def _review_status(result: Mapping[str, Any]) -> str:
    return resolve_review_status(result)


def _run_id(result: Mapping[str, Any]) -> str | None:
    run = result.get("run")
    value = run.get("id") if isinstance(run, Mapping) else None
    return str(value) if value else None


def _result_is_uncertain(result: Mapping[str, Any]) -> bool:
    """Inspect only current-run structured evidence, never narrative history."""

    run = result.get("run")
    if not isinstance(run, Mapping):
        return False

    execution_evidence = run.get("execution_evidence")
    if isinstance(execution_evidence, Mapping):
        budget = execution_evidence.get("budget")
        if isinstance(budget, Mapping):
            unsettled = budget.get("unsettled_attempt_count")
            if isinstance(unsettled, int) and not isinstance(unsettled, bool) and unsettled > 0:
                return True

    error = run.get("error")
    if isinstance(error, Mapping) and any(
        error.get(key) == "provider_call_uncertain"
        for key in ("failure_category", "status")
    ):
        return True

    trace = run.get("trace")
    if not isinstance(trace, (list, tuple)):
        return False
    for event in trace:
        if not isinstance(event, Mapping):
            continue
        if event.get("status") == "provider_call_uncertain":
            return True
        reports = event.get("provider_attempts")
        if not isinstance(reports, (list, tuple)):
            continue
        for report in reports:
            if not isinstance(report, Mapping):
                continue
            if report.get("failure_category") == "provider_call_uncertain":
                return True
            history = report.get("history")
            if not isinstance(history, (list, tuple)):
                continue
            if any(
                isinstance(attempt, Mapping)
                and attempt.get("failure_category") == "provider_call_uncertain"
                for attempt in history
            ):
                return True
    return False


def _contains_uncertainty(value: Any) -> bool:
    text = str(value or "").casefold()
    return "provider_call_uncertain" in text or "uncertain" in text or "intent" in text and "receipt" in text


def _validated_draft_text(value: Any) -> str:
    if not isinstance(value, str):
        raise JobError("draft text must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise JobError("draft text cannot be empty")
    if len(normalized.encode("utf-8")) > 4 * 1024 * 1024:
        raise JobError("draft text exceeds 4 MiB")
    return normalized + "\n"


def _looks_like_absolute_path(value: str) -> bool:
    try:
        return Path(value).is_absolute()
    except (TypeError, ValueError, OSError):
        return False


def _safe_event_type(value: Any) -> str:
    text = str(value or "event").strip()[:120]
    return text or "event"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _json_copy(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ACTIVE_JOB_STATES",
    "JOB_KINDS",
    "JOB_STATES",
    "PAUSED_JOB_STATES",
    "TERMINAL_JOB_STATES",
    "PRE_PROVIDER_CONTEXT_BUDGET_ERROR",
    "PRE_PROVIDER_PROVENANCE_ERROR",
    "REPAIR_NOT_CONVERGING_ERROR",
    "STALE_SCENE_EVIDENCE_ERROR",
    "DraftRecord",
    "DraftSourceConflict",
    "JobError",
    "JobRecord",
    "JobStore",
    "JobSupervisor",
    "compute_project_state_head",
    "verify_committed_chapter",
    "verify_approved_outline_binding",
]
