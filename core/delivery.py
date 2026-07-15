from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from api.notion_client import create_database_page, query_database_pages
from api.retry import retry_telemetry_snapshot
from core.engine.persistence import _atomic_create_from_bytes, _atomic_replace_from_bytes, persistence_run_lock
from core.engine.safe_paths import RootBinding, SafePathResolver
from core.memory_v2.canonical import canonical_json_hash
from core.path_refs import resolve_path_ref, validate_path_ref
from core.reliable_semantic_contracts import DELIVERY_STATES
from core.schema import SchemaValidationError, validate_schema


DELIVERY_SCHEMA_VERSION = "1.0"
DELIVERY_POLICIES = frozenset({"not_required", "required", "best_effort"})
DELIVERY_TERMINAL_STATES = frozenset(
    {"not_required", "succeeded", "permanent_failed", "conflict", "cancelled"}
)
_ATTEMPT_OUTCOME_STATES = frozenset(
    {"succeeded", "retryable_failed", "permanent_failed", "uncertain", "conflict", "cancelled"}
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_Clock = Callable[[], datetime]
_FaultInjector = Callable[[str, str | None, Path | None], None]


class DeliveryError(RuntimeError):
    pass


class DeliveryConflictError(DeliveryError):
    pass


class DeliveryLeaseError(DeliveryError):
    pass


class DeliveryAdapter(Protocol):
    def deliver(self, job: Mapping[str, Any], context: "DeliveryAttemptContext") -> Mapping[str, Any]: ...


@dataclass
class DeliveryAttemptContext:
    queue: "DeliveryQueue"
    job_id: str
    attempt_id: str
    query_only: bool
    remote_mutation_started: bool = False

    def mark_remote_mutation_started(self) -> None:
        self.queue._mark_remote_mutation_started(self.job_id, self.attempt_id)
        self.remote_mutation_started = True


def delivery_payload_hash(payload: Mapping[str, Any]) -> str:
    return canonical_json_hash(dict(payload), exclude_environment_fields=False)


def delivery_operation_id(*, book_id: str, run_id: str, job_id: str, payload_hash: str) -> str:
    _require_sha256("payload_hash", payload_hash)
    digest = canonical_json_hash(
        {
            "book_id": str(book_id),
            "run_id": str(run_id),
            "job_id": str(job_id),
            "payload_hash": payload_hash,
        },
        exclude_environment_fields=False,
    )
    return f"novelagent:{digest}"


def delivery_outcome(
    state: str,
    *,
    code: str,
    message: str,
    remote_refs: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in _ATTEMPT_OUTCOME_STATES:
        raise DeliveryError(f"invalid delivery attempt outcome state: {state}")
    return {
        "state": state,
        "code": str(code),
        "message": str(message)[:1000],
        "remote_refs": _sanitize_mapping(remote_refs or {}),
        "observation": _sanitize_mapping(observation or {}),
    }


def delivery_outcome_from_legacy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Map all v1 writer status/verification forms through one compatibility function."""

    payload = dict(value)
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else payload
    status = str(verification.get("status") or payload.get("status") or "").lower()
    target = str(verification.get("target") or payload.get("target") or "").lower()
    written = int(payload.get("written") or 0)
    failures = verification.get("failures") if isinstance(verification.get("failures"), list) else []
    failure_text = json.dumps(failures, ensure_ascii=False).lower()
    if "duplicate" in failure_text or "conflict" in status:
        return delivery_outcome("conflict", code="legacy_payload_conflict", message="Legacy delivery reported a payload conflict")
    if status in {"verified", "succeeded", "delivered", "response_recorded"}:
        return delivery_outcome("succeeded", code="legacy_verified", message="Legacy delivery evidence was successful")
    if status in {"not_applicable", "skipped_no_writer"}:
        return delivery_outcome("permanent_failed", code="legacy_not_delivered", message="Legacy evidence does not prove a required delivery")
    if target == "notion" and written > 0 and status in {"readback_failed", "response_incomplete", "failed", "error"}:
        return delivery_outcome("uncertain", code="legacy_remote_write_uncertain", message="A remote write may have occurred without verified readback")
    if status in {"auth_failed", "schema_invalid", "permanent_failed"}:
        return delivery_outcome("permanent_failed", code="legacy_permanent_failure", message="Legacy delivery cannot be retried without configuration changes")
    return delivery_outcome("retryable_failed", code="legacy_retryable_failure", message="Legacy delivery did not succeed")


class DeliveryQueue:
    def __init__(
        self,
        root: str | Path,
        *,
        lease_seconds: int = 60,
        clock: _Clock | None = None,
        fault_injector: _FaultInjector | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.root = Path(root).resolve()
        self.jobs_dir = self.root / "jobs"
        self.attempts_dir = self.root / "attempts"
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_injector = fault_injector

    def enqueue(
        self,
        *,
        job_id: str,
        book_id: str,
        run_id: str,
        publication_receipt_hash: str,
        target_type: str,
        target: Mapping[str, Any],
        payload: Mapping[str, Any],
        policy: str | None = None,
    ) -> dict[str, Any]:
        job_id = _validate_id("job_id", job_id)
        book_id = _validate_id("book_id", book_id)
        run_id = _validate_id("run_id", run_id)
        _require_sha256("publication_receipt_hash", publication_receipt_hash)
        if target_type not in {"none", "file", "notion"}:
            raise DeliveryError(f"unsupported delivery target type: {target_type}")
        resolved_policy = policy or ("not_required" if target_type == "none" else "required")
        if resolved_policy not in DELIVERY_POLICIES:
            raise DeliveryError(f"unsupported delivery policy: {resolved_policy}")
        if target_type == "none" and resolved_policy != "not_required":
            raise DeliveryError("target_type=none requires policy=not_required")
        if target_type != "none" and resolved_policy == "not_required":
            raise DeliveryError("not_required is only valid when no delivery target is configured")
        target_payload = copy.deepcopy(dict(target))
        payload_copy = copy.deepcopy(dict(payload))
        forbidden_target_keys = {"api_key", "authorization", "token", "secret"}
        if forbidden_target_keys.intersection(_all_mapping_keys(target_payload)):
            raise DeliveryError("DeliveryJob target must not persist credentials or secrets")
        payload_hash = delivery_payload_hash(payload_copy)
        operation_id = delivery_operation_id(
            book_id=book_id,
            run_id=run_id,
            job_id=job_id,
            payload_hash=payload_hash,
        )
        if target_type == "file":
            validate_path_ref(target_payload.get("path_ref"))
            if not isinstance(payload_copy.get("content"), str):
                raise DeliveryError("file delivery payload.content must be a string")
        if target_type == "notion":
            if not isinstance(payload_copy.get("id"), str) or not payload_copy["id"]:
                raise DeliveryError("Notion delivery payload requires a stable Memory ID in payload.id")
            target_payload.setdefault("property_map", default_notion_property_map())
        now = _iso(self.clock())
        job = {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "job_id": job_id,
            "book_id": book_id,
            "run_id": run_id,
            "publication_receipt_hash": publication_receipt_hash,
            "operation_id": operation_id,
            "target_type": target_type,
            "target": target_payload,
            "payload": payload_copy,
            "payload_hash": payload_hash,
            "policy": resolved_policy,
            "state": "not_required" if resolved_policy == "not_required" else "pending",
            "lease": None,
            "attempt_count": 0,
            "last_attempt_receipt": None,
            "uncertain_since": None,
            "confirmed_absent_at": None,
            "created_at": now,
            "updated_at": now,
        }
        validated = validate_delivery_job(job)
        path = self._job_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with persistence_run_lock(self.root, state_paths=[path]):
            if path.exists():
                existing = load_delivery_job(path)
                immutable_fields = (
                    "job_id",
                    "book_id",
                    "run_id",
                    "publication_receipt_hash",
                    "operation_id",
                    "target_type",
                    "target",
                    "payload_hash",
                    "policy",
                )
                if all(existing[field] == validated[field] for field in immutable_fields):
                    return existing
                existing["state"] = "conflict"
                existing["lease"] = None
                existing["updated_at"] = now
                _write_job(path, existing)
                raise DeliveryConflictError(f"DeliveryJob id already exists with different immutable content: {job_id}")
            _write_new_json(path, validated)
        return validated

    def load(self, job_id: str) -> dict[str, Any]:
        return load_delivery_job(self._job_path(_validate_id("job_id", job_id)))

    def inspect(self, job_id: str) -> dict[str, Any]:
        job = self.load(job_id)
        receipts = []
        attempt_dir = self.attempts_dir / job["job_id"]
        if attempt_dir.exists():
            for path in sorted(attempt_dir.glob("*.json")):
                receipt = load_delivery_attempt_receipt(path)
                receipts.append(
                    {
                        "attempt_id": receipt["attempt_id"],
                        "outcome": receipt["outcome"],
                        "started_at": receipt["started_at"],
                        "finished_at": receipt["finished_at"],
                        "attempt_receipt_hash": receipt["attempt_receipt_hash"],
                    }
                )
        return {"job": job, "attempts": receipts}

    def attempt(self, job_id: str, *, worker_id: str, adapter: DeliveryAdapter) -> dict[str, Any]:
        worker_id = _validate_id("worker_id", worker_id)
        claim = self._claim(job_id, worker_id)
        if claim is None:
            return self.load(job_id)
        if claim.get("recovered_receipt"):
            return self.load(job_id)
        job = claim["job"]
        context = DeliveryAttemptContext(
            queue=self,
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            query_only=bool(claim["query_only"]),
        )
        retry_telemetry_offset = len(retry_telemetry_snapshot())
        try:
            raw_outcome = adapter.deliver(copy.deepcopy(job), context)
            outcome = _validate_delivery_outcome(raw_outcome)
        except Exception as exc:
            state = "uncertain" if context.remote_mutation_started else "retryable_failed"
            outcome = delivery_outcome(
                state,
                code="adapter_exception_after_remote_mutation" if context.remote_mutation_started else "adapter_exception",
                message=f"{type(exc).__name__}: {exc}",
            )
        finished_at = _iso(self.clock())
        provider_attempts = retry_telemetry_snapshot()[retry_telemetry_offset:]
        receipt = {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "attempt_id": claim["attempt_id"],
            "job_id": job["job_id"],
            "book_id": job["book_id"],
            "run_id": job["run_id"],
            "publication_receipt_hash": job["publication_receipt_hash"],
            "payload_hash": job["payload_hash"],
            "target_type": job["target_type"],
            "worker_id": worker_id,
            "previous_state": claim["previous_state"],
            "outcome": outcome,
            "query_only": bool(claim["query_only"]),
            "remote_mutation_started": context.remote_mutation_started,
            "started_at": claim["started_at"],
            "finished_at": finished_at,
            "diagnostics": {
                "lease_seconds": self.lease_seconds,
                "attempt_number": job["attempt_count"],
                "provider_attempts": provider_attempts,
            },
        }
        receipt["attempt_receipt_hash"] = canonical_json_hash(
            receipt,
            exclude_fields=("attempt_receipt_hash",),
            exclude_environment_fields=False,
        )
        receipt = validate_delivery_attempt_receipt(receipt)
        receipt_path = self._attempt_path(job["job_id"], claim["attempt_id"])
        self._inject("before_attempt_receipt", claim["attempt_id"], receipt_path)
        _write_new_json(receipt_path, receipt)
        self._inject("after_attempt_receipt", claim["attempt_id"], receipt_path)
        self._finalize_attempt(receipt)
        return self.load(job_id)

    def reconcile(
        self,
        *,
        adapters: Mapping[str, DeliveryAdapter],
        worker_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.jobs_dir.glob("*.json")) if self.jobs_dir.exists() else []:
            job = self._load_job_path(path)
            if run_id is not None and job["run_id"] != run_id:
                continue
            if job["state"] in DELIVERY_TERMINAL_STATES:
                continue
            adapter = adapters.get(str(job["target_type"]))
            if adapter is None:
                outcome = delivery_outcome(
                    "retryable_failed",
                    code="delivery_adapter_missing",
                    message=f"No adapter is currently configured for {job['target_type']}",
                )
                adapter = _FixedOutcomeAdapter(outcome)
            results.append(self.attempt(job["job_id"], worker_id=worker_id, adapter=adapter))
        considered_jobs = (
            self.jobs_for_run(run_id)
            if run_id is not None
            else [
                self._load_job_path(path)
                for path in sorted(self.jobs_dir.glob("*.json"))
            ]
            if self.jobs_dir.exists()
            else []
        )
        return {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "run_id": run_id,
            "attempted": len(results),
            "jobs": results,
            "required_succeeded": all(
                job["state"] == "succeeded"
                for job in considered_jobs
                if job.get("policy") == "required"
            ),
        }

    def resolve_confirmed_absent(
        self,
        job_id: str,
        *,
        worker_id: str,
        adapter: DeliveryAdapter,
    ) -> dict[str, Any]:
        job = self.load(job_id)
        if job["state"] != "uncertain":
            raise DeliveryError("--confirmed-absent is only valid for an uncertain DeliveryJob")
        uncertain_since = _parse_time(job.get("uncertain_since"))
        quarantine_seconds = int(job["target"].get("quarantine_seconds") or 60)
        if uncertain_since is None or self.clock() < uncertain_since + timedelta(seconds=quarantine_seconds):
            raise DeliveryError("delivery quarantine/read-after window has not elapsed")
        queried = self.attempt(job_id, worker_id=worker_id, adapter=adapter)
        if queried["state"] != "uncertain":
            return queried
        last_attempt = queried.get("last_attempt_receipt")
        if not isinstance(last_attempt, Mapping):
            raise DeliveryError("confirmed absence was not proven by an attempt receipt")
        attempt_receipt = load_delivery_attempt_receipt(
            self._attempt_path(job_id, str(last_attempt.get("attempt_id") or ""))
        )
        if attempt_receipt["outcome"]["code"] != "notion_absent_during_uncertain_reconcile":
            raise DeliveryError("confirmed absence requires a successful, fully paginated query")
        path = self._job_path(job_id)
        with persistence_run_lock(self.root, state_paths=[path]):
            current = load_delivery_job(path)
            if current["state"] != "uncertain":
                return current
            current["state"] = "pending"
            current["confirmed_absent_at"] = _iso(self.clock())
            current["lease"] = None
            current["updated_at"] = _iso(self.clock())
            _write_job(path, current)
            return current

    def required_states(self, *, run_id: str) -> tuple[str, ...]:
        states: list[str] = []
        for path in sorted(self.jobs_dir.glob("*.json")) if self.jobs_dir.exists() else []:
            job = self._load_job_path(path)
            if job["run_id"] == run_id and job["policy"] == "required":
                states.append(str(job["state"]))
        return tuple(states)

    def jobs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in sorted(self.jobs_dir.glob("*.json")) if self.jobs_dir.exists() else []:
            job = self._load_job_path(path)
            if job["run_id"] == run_id:
                jobs.append(job)
        return jobs

    def _claim(self, job_id: str, worker_id: str) -> dict[str, Any] | None:
        path = self._job_path(_validate_id("job_id", job_id))
        with persistence_run_lock(self.root, state_paths=[path]):
            job = load_delivery_job(path)
            now = self.clock()
            if job["state"] == "delivering":
                lease = job["lease"]
                receipt_path = self._attempt_path(job["job_id"], str(lease["attempt_id"]))
                if receipt_path.exists():
                    receipt = load_delivery_attempt_receipt(receipt_path)
                    _apply_attempt_receipt_to_job(job, receipt, now=now)
                    _write_job(path, job)
                    return {"recovered_receipt": True}
                expires_at = _parse_time(lease.get("expires_at"))
                if expires_at is not None and expires_at > now:
                    return None
                if lease.get("phase") == "remote_mutation_started":
                    job["state"] = "uncertain"
                    job["uncertain_since"] = job.get("uncertain_since") or _iso(now)
                else:
                    job["state"] = "retryable_failed"
                job["lease"] = None
                _write_job(path, job)
            if job["state"] in DELIVERY_TERMINAL_STATES:
                return None
            if job["state"] not in {"pending", "retryable_failed", "uncertain"}:
                return None
            previous_state = str(job["state"])
            query_only = previous_state == "uncertain"
            attempt_number = int(job["attempt_count"]) + 1
            attempt_id = f"{job['job_id']}:{attempt_number:04d}:{canonical_json_hash({'worker': worker_id, 'at': _iso(now)})[:8]}"
            started_at = _iso(now)
            job["state"] = "delivering"
            job["attempt_count"] = attempt_number
            job["lease"] = {
                "worker_id": worker_id,
                "attempt_id": attempt_id,
                "acquired_at": started_at,
                "expires_at": _iso(now + timedelta(seconds=self.lease_seconds)),
                "phase": "query_only" if query_only else "claimed",
            }
            job["updated_at"] = started_at
            _write_job(path, job)
            self._inject("after_lease_acquired", attempt_id, path)
            return {
                "job": job,
                "attempt_id": attempt_id,
                "previous_state": previous_state,
                "query_only": query_only,
                "started_at": started_at,
            }

    def _mark_remote_mutation_started(self, job_id: str, attempt_id: str) -> None:
        path = self._job_path(job_id)
        with persistence_run_lock(self.root, state_paths=[path]):
            job = load_delivery_job(path)
            lease = job.get("lease")
            if job["state"] != "delivering" or not isinstance(lease, dict) or lease.get("attempt_id") != attempt_id:
                raise DeliveryLeaseError("delivery lease no longer belongs to this attempt")
            if lease.get("phase") == "query_only":
                raise DeliveryLeaseError("query-only reconciliation cannot start a remote mutation")
            lease["phase"] = "remote_mutation_started"
            job["updated_at"] = _iso(self.clock())
            _write_job(path, job)
        self._inject("remote_mutation_started", attempt_id, path)

    def _finalize_attempt(self, receipt: Mapping[str, Any]) -> None:
        path = self._job_path(str(receipt["job_id"]))
        with persistence_run_lock(self.root, state_paths=[path]):
            job = load_delivery_job(path)
            lease = job.get("lease")
            if job["state"] != "delivering" or not isinstance(lease, dict) or lease.get("attempt_id") != receipt["attempt_id"]:
                if job["last_attempt_receipt"] and job["last_attempt_receipt"].get("attempt_receipt_hash") == receipt["attempt_receipt_hash"]:
                    return
                raise DeliveryLeaseError("cannot finalize an attempt whose lease is no longer current")
            _apply_attempt_receipt_to_job(job, receipt, now=self.clock())
            _write_job(path, job)

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _attempt_path(self, job_id: str, attempt_id: str) -> Path:
        if not _SAFE_ATTEMPT_ID.fullmatch(str(attempt_id)):
            raise DeliveryError(f"attempt_id is invalid: {attempt_id!r}")
        safe_attempt = attempt_id.replace(":", "_")
        return self.attempts_dir / job_id / f"{safe_attempt}.json"

    def _load_job_path(self, path: Path) -> dict[str, Any]:
        job = load_delivery_job(path)
        if path.resolve() != self._job_path(job["job_id"]).resolve():
            raise DeliveryError(f"DeliveryJob filename does not match job_id: {path}")
        return job

    def _inject(self, event: str, attempt_id: str | None, path: Path | None) -> None:
        if self.fault_injector is not None:
            self.fault_injector(event, attempt_id, path)


class _FixedOutcomeAdapter:
    def __init__(self, outcome: Mapping[str, Any]) -> None:
        self.outcome = dict(outcome)

    def deliver(self, job: Mapping[str, Any], context: DeliveryAttemptContext) -> Mapping[str, Any]:
        del job, context
        return self.outcome


class FileDeliveryAdapter:
    def __init__(self, *, root_map: Mapping[str, str | Path]) -> None:
        self.root_map = dict(root_map)

    def deliver(self, job: Mapping[str, Any], context: DeliveryAttemptContext) -> Mapping[str, Any]:
        del context
        path = resolve_path_ref(job["target"]["path_ref"], self.root_map)
        encoding = str(job["payload"].get("encoding") or "utf-8")
        content = str(job["payload"]["content"]).encode(encoding)
        expected_hash = hashlib.sha256(content).hexdigest()
        if path.exists():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                return delivery_outcome(
                    "conflict",
                    code="file_payload_conflict",
                    message="File export path already exists with different content",
                    remote_refs={"path_ref": job["target"]["path_ref"]},
                )
            return delivery_outcome(
                "succeeded",
                code="file_already_present",
                message="File export already contains the expected bytes",
                remote_refs={"path_ref": job["target"]["path_ref"], "sha256": expected_hash},
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_create_from_bytes(path, content)
        except Exception as exc:
            return delivery_outcome(
                "retryable_failed",
                code="file_export_failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            return delivery_outcome("conflict", code="file_readback_mismatch", message="File readback hash differs")
        return delivery_outcome(
            "succeeded",
            code="file_exported",
            message="File export created and verified",
            remote_refs={"path_ref": job["target"]["path_ref"], "sha256": expected_hash},
        )


class SafeFileDeliveryAdapter:
    """File Delivery adapter with UUID binding and reparse/TOCTOU guards."""

    def __init__(self, *, binding: RootBinding) -> None:
        self.resolver = SafePathResolver({binding.root_id: binding})

    def deliver(self, job: Mapping[str, Any], context: DeliveryAttemptContext) -> Mapping[str, Any]:
        del context
        path_ref = job["target"]["path_ref"]
        resolved = self.resolver.resolve(path_ref)
        path = resolved.path
        encoding = str(job["payload"].get("encoding") or "utf-8")
        content = str(job["payload"]["content"]).encode(encoding)
        expected_hash = hashlib.sha256(content).hexdigest()
        if path.exists():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                return delivery_outcome(
                    "conflict",
                    code="file_payload_conflict",
                    message="File export path already exists with different content",
                    remote_refs={"path_ref": resolved.path_ref.to_dict()},
                )
            return delivery_outcome(
                "succeeded",
                code="file_already_present",
                message="File export already contains the expected bytes",
                remote_refs={"path_ref": resolved.path_ref.to_dict(), "sha256": expected_hash},
            )
        try:
            prepared = self.resolver.ensure_parent(
                path_ref, expected_guard=resolved.guard
            )
            # Revalidate every parent identity immediately before the create.
            final = self.resolver.resolve(
                path_ref, expected_guard=prepared.guard
            )
            _atomic_create_from_bytes(final.path, content)
            readback = self.resolver.resolve(
                path_ref, expected_guard=final.guard
            )
        except Exception as exc:
            return delivery_outcome(
                "retryable_failed",
                code="file_export_failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        if hashlib.sha256(readback.path.read_bytes()).hexdigest() != expected_hash:
            return delivery_outcome("conflict", code="file_readback_mismatch", message="File readback hash differs")
        return delivery_outcome(
            "succeeded",
            code="file_exported",
            message="File export created and verified",
            remote_refs={"path_ref": readback.path_ref.to_dict(), "sha256": expected_hash},
        )


class NotionDeliveryAdapter:
    def __init__(
        self,
        *,
        database_id: str,
        api_key: str,
        database_schema: Mapping[str, Any] | None,
        transport=None,
    ) -> None:
        self.database_id = database_id
        self.api_key = api_key
        self.database_schema = copy.deepcopy(dict(database_schema)) if database_schema is not None else None
        self.transport = transport

    def deliver(self, job: Mapping[str, Any], context: DeliveryAttemptContext) -> Mapping[str, Any]:
        property_map = job["target"].get("property_map") or default_notion_property_map()
        target_database_id = str(job["target"].get("database_id") or self.database_id)
        if target_database_id != self.database_id:
            return delivery_outcome(
                "retryable_failed",
                code="notion_database_configuration_mismatch",
                message="Configured Notion database does not match the durable DeliveryJob target",
            )
        if job["policy"] == "required":
            if self.database_schema is None:
                return delivery_outcome(
                    "retryable_failed",
                    code="notion_schema_preflight_unavailable",
                    message="Required Notion delivery needs a captured database schema preflight",
                )
            try:
                validate_notion_delivery_schema(self.database_schema, property_map=property_map)
            except Exception as exc:
                return delivery_outcome(
                    "permanent_failed",
                    code="notion_schema_invalid",
                    message=f"{type(exc).__name__}: {exc}",
                )
        try:
            properties = notion_delivery_properties(job, property_map=property_map)
        except Exception as exc:
            return delivery_outcome(
                "permanent_failed",
                code="notion_payload_invalid",
                message=f"{type(exc).__name__}: {exc}",
            )
        try:
            pages = query_database_pages(
                database_id=self.database_id,
                api_key=self.api_key,
                transport=self.transport,
            )
        except Exception as exc:
            state = "uncertain" if context.query_only else "retryable_failed"
            return delivery_outcome(state, code="notion_query_failed", message=f"{type(exc).__name__}: {exc}")
        observed = _classify_notion_pages(job, pages, property_map)
        if observed["state"] != "absent":
            return observed["outcome"]
        if context.query_only:
            return delivery_outcome(
                "uncertain",
                code="notion_absent_during_uncertain_reconcile",
                message="No matching page is visible; automatic POST remains disabled",
                observation={"matched_pages": 0, "fully_paginated": True},
            )

        context.mark_remote_mutation_started()
        try:
            page = create_database_page(
                database_id=self.database_id,
                api_key=self.api_key,
                transport=self.transport,
                properties=properties,
            )
        except Exception as exc:
            return delivery_outcome(
                "uncertain",
                code="notion_post_uncertain",
                message=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(page, dict) or not page.get("id"):
            return delivery_outcome(
                "uncertain",
                code="notion_post_missing_page_id",
                message="Notion create response did not include a page id",
            )
        try:
            readback_pages = query_database_pages(
                database_id=self.database_id,
                api_key=self.api_key,
                transport=self.transport,
            )
        except Exception as exc:
            return delivery_outcome(
                "uncertain",
                code="notion_readback_failed",
                message=f"{type(exc).__name__}: {exc}",
                remote_refs={"page_id": str(page["id"])},
            )
        readback = _classify_notion_pages(job, readback_pages, property_map)
        if readback["state"] == "absent":
            return delivery_outcome(
                "uncertain",
                code="notion_readback_missing",
                message="Created page is not visible in paginated readback",
                remote_refs={"page_id": str(page["id"])},
            )
        return readback["outcome"]


def default_notion_property_map() -> dict[str, str]:
    return {
        "operation_id": "Operation ID",
        "memory_id": "Memory ID",
        "payload_hash": "Payload Hash",
        "type": "Type",
        "name": "Name",
        "data": "Data",
    }


def validate_notion_delivery_schema(
    database_schema: Mapping[str, Any] | None,
    *,
    property_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(database_schema, Mapping):
        raise DeliveryError("Notion required delivery needs a database schema preflight response")
    properties = database_schema.get("properties")
    if not isinstance(properties, Mapping):
        raise DeliveryError("Notion database schema is missing properties")
    mapping = dict(property_map or default_notion_property_map())
    required_types = {
        "operation_id": "rich_text",
        "memory_id": "rich_text",
        "payload_hash": "rich_text",
        "type": "select",
        "name": "title",
        "data": "rich_text",
    }
    checked: dict[str, Any] = {}
    for logical, expected_type in required_types.items():
        remote_name = mapping.get(logical)
        if not isinstance(remote_name, str) or not remote_name:
            raise DeliveryError(f"Notion property mapping is missing {logical}")
        remote = properties.get(remote_name)
        if not isinstance(remote, Mapping) or remote.get("type") != expected_type:
            raise DeliveryError(
                f"Notion property {remote_name!r} must exist with type {expected_type}"
            )
        checked[logical] = {"name": remote_name, "type": expected_type}
    return {"ok": True, "properties": checked}


def notion_delivery_properties(
    job: Mapping[str, Any],
    *,
    property_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    mapping = dict(property_map or default_notion_property_map())
    payload = job["payload"]
    values = {
        "operation_id": str(job["operation_id"]),
        "memory_id": str(payload.get("id") or ""),
        "payload_hash": str(job["payload_hash"]),
        "type": str(payload.get("type") or "memory"),
        "name": str(payload.get("name") or "untitled"),
        "data": json.dumps(payload.get("data", {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    }
    for logical in ("operation_id", "memory_id", "payload_hash", "name", "data"):
        if len(values[logical]) > 2000:
            raise DeliveryError(f"Notion {logical} exceeds the 2000-character property limit")
    return {
        mapping["operation_id"]: _notion_rich_text(values["operation_id"]),
        mapping["memory_id"]: _notion_rich_text(values["memory_id"]),
        mapping["payload_hash"]: _notion_rich_text(values["payload_hash"]),
        mapping["type"]: {"select": {"name": values["type"][:100]}},
        mapping["name"]: {"title": [{"text": {"content": values["name"]}}]},
        mapping["data"]: _notion_rich_text(values["data"]),
    }


def validate_delivery_job(job: Any) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise DeliveryError("DeliveryJob must be an object")
    try:
        validated = validate_schema(job, "delivery_job.schema.json")
    except SchemaValidationError as exc:
        raise DeliveryError(str(exc)) from exc
    for field in ("job_id", "book_id", "run_id"):
        _validate_id(field, validated[field])
    _require_sha256("publication_receipt_hash", validated["publication_receipt_hash"])
    _require_sha256("payload_hash", validated["payload_hash"])
    if delivery_payload_hash(validated["payload"]) != validated["payload_hash"]:
        raise DeliveryError("DeliveryJob payload hash mismatch")
    expected_operation_id = delivery_operation_id(
        book_id=validated["book_id"],
        run_id=validated["run_id"],
        job_id=validated["job_id"],
        payload_hash=validated["payload_hash"],
    )
    if validated["operation_id"] != expected_operation_id:
        raise DeliveryError("DeliveryJob operation id mismatch")
    if {"api_key", "authorization", "token", "secret"}.intersection(
        _all_mapping_keys(validated["target"])
    ):
        raise DeliveryError("DeliveryJob target must not persist credentials or secrets")
    target_type = validated["target_type"]
    if target_type == "none":
        if validated["policy"] != "not_required":
            raise DeliveryError("target_type=none requires not_required policy")
    elif target_type == "file":
        validate_path_ref(validated["target"].get("path_ref"))
        if not isinstance(validated["payload"].get("content"), str):
            raise DeliveryError("file delivery payload.content must be a string")
    elif target_type == "notion":
        if not isinstance(validated["payload"].get("id"), str) or not validated["payload"]["id"]:
            raise DeliveryError("Notion delivery payload requires a stable Memory ID")
        quarantine_seconds = validated["target"].get("quarantine_seconds", 60)
        if not isinstance(quarantine_seconds, int) or isinstance(quarantine_seconds, bool) or quarantine_seconds < 1:
            raise DeliveryError("Notion quarantine_seconds must be a positive integer")
    if validated["state"] == "delivering":
        lease = validated.get("lease")
        if not isinstance(lease, dict):
            raise DeliveryError("delivering job must have a lease")
        for field in ("worker_id", "attempt_id", "acquired_at", "expires_at", "phase"):
            if not isinstance(lease.get(field), str) or not lease[field]:
                raise DeliveryError(f"delivery lease.{field} is required")
        _validate_id("delivery lease.worker_id", lease["worker_id"])
        if not _SAFE_ATTEMPT_ID.fullmatch(lease["attempt_id"]):
            raise DeliveryError("delivery lease.attempt_id is invalid")
        acquired_at = _require_time("delivery lease.acquired_at", lease["acquired_at"])
        expires_at = _require_time("delivery lease.expires_at", lease["expires_at"])
        if expires_at <= acquired_at:
            raise DeliveryError("delivery lease expiry must be after acquisition")
        if lease["phase"] not in {"claimed", "query_only", "remote_mutation_started"}:
            raise DeliveryError("delivery lease.phase is invalid")
    elif validated.get("lease") is not None:
        raise DeliveryError("only a delivering job may retain a lease")
    if validated["state"] not in DELIVERY_STATES:
        raise DeliveryError(f"unknown DeliveryJob state: {validated['state']}")
    if validated["state"] == "not_required" and validated["policy"] != "not_required":
        raise DeliveryError("not_required state requires not_required policy")
    if validated["policy"] == "not_required" and validated["target_type"] != "none":
        raise DeliveryError("not_required policy requires target_type=none")
    last_attempt = validated.get("last_attempt_receipt")
    if last_attempt is not None:
        if not isinstance(last_attempt, dict):
            raise DeliveryError("last_attempt_receipt must be an object")
        attempt_id = last_attempt.get("attempt_id")
        if not isinstance(attempt_id, str) or not _SAFE_ATTEMPT_ID.fullmatch(attempt_id):
            raise DeliveryError("last_attempt_receipt.attempt_id is invalid")
        _require_sha256("last_attempt_receipt.attempt_receipt_hash", last_attempt.get("attempt_receipt_hash"))
        expected_relative = f"attempts/{validated['job_id']}/{attempt_id.replace(':', '_')}.json"
        if last_attempt.get("relative_path") != expected_relative:
            raise DeliveryError("last_attempt_receipt.relative_path is invalid")
    for field in ("created_at", "updated_at"):
        _require_time(f"DeliveryJob.{field}", validated[field])
    if validated.get("uncertain_since") is not None:
        _require_time("DeliveryJob.uncertain_since", validated["uncertain_since"])
    if validated.get("confirmed_absent_at") is not None:
        _require_time("DeliveryJob.confirmed_absent_at", validated["confirmed_absent_at"])
    return validated


def load_delivery_job(path: str | Path) -> dict[str, Any]:
    return validate_delivery_job(_load_json(Path(path)))


def validate_delivery_attempt_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise DeliveryError("DeliveryAttemptReceipt must be an object")
    try:
        validated = validate_schema(receipt, "delivery_attempt_receipt.schema.json")
    except SchemaValidationError as exc:
        raise DeliveryError(str(exc)) from exc
    for field in ("publication_receipt_hash", "payload_hash", "attempt_receipt_hash"):
        _require_sha256(field, validated[field])
    for field in ("job_id", "book_id", "run_id", "worker_id"):
        _validate_id(field, validated[field])
    if not _SAFE_ATTEMPT_ID.fullmatch(validated["attempt_id"]):
        raise DeliveryError("DeliveryAttemptReceipt attempt_id is invalid")
    if validated["previous_state"] not in {"pending", "retryable_failed", "uncertain"}:
        raise DeliveryError("DeliveryAttemptReceipt previous_state is invalid")
    if validated["target_type"] not in {"file", "notion"}:
        raise DeliveryError("DeliveryAttemptReceipt target_type is invalid")
    started_at = _require_time("DeliveryAttemptReceipt.started_at", validated["started_at"])
    finished_at = _require_time("DeliveryAttemptReceipt.finished_at", validated["finished_at"])
    if finished_at < started_at:
        raise DeliveryError("DeliveryAttemptReceipt finished_at precedes started_at")
    if validated["query_only"] != (validated["previous_state"] == "uncertain"):
        raise DeliveryError("DeliveryAttemptReceipt query_only does not match previous_state")
    if validated["query_only"] and validated["remote_mutation_started"]:
        raise DeliveryError("query-only delivery cannot start a remote mutation")
    _validate_delivery_outcome(validated["outcome"])
    expected = canonical_json_hash(
        validated,
        exclude_fields=("attempt_receipt_hash",),
        exclude_environment_fields=False,
    )
    if expected != validated["attempt_receipt_hash"]:
        raise DeliveryError("DeliveryAttemptReceipt hash mismatch")
    forbidden = {"api_key", "authorization", "token", "payload", "content", "body"}
    if forbidden.intersection(_all_mapping_keys(validated)):
        raise DeliveryError("DeliveryAttemptReceipt contains sensitive or full-payload fields")
    return validated


def load_delivery_attempt_receipt(path: str | Path) -> dict[str, Any]:
    return validate_delivery_attempt_receipt(_load_json(Path(path)))


def _classify_notion_pages(
    job: Mapping[str, Any],
    pages: Iterable[Mapping[str, Any]],
    property_map: Mapping[str, str],
) -> dict[str, Any]:
    operation_id = str(job["operation_id"])
    memory_id = str(job["payload"].get("id") or "")
    matches: list[dict[str, Any]] = []
    for raw_page in pages:
        page = dict(raw_page)
        properties = page.get("properties") if isinstance(page.get("properties"), dict) else {}
        remote_operation = _notion_property_text(properties.get(property_map["operation_id"]))
        remote_memory = _notion_property_text(properties.get(property_map["memory_id"]))
        if remote_operation == operation_id or remote_memory == memory_id:
            matches.append(
                {
                    "page": page,
                    "operation_id": remote_operation,
                    "memory_id": remote_memory,
                    "payload_hash": _notion_property_text(properties.get(property_map["payload_hash"])),
                }
            )
    if not matches:
        return {"state": "absent", "outcome": None}
    if len(matches) > 1:
        return {
            "state": "conflict",
            "outcome": delivery_outcome(
                "conflict",
                code="notion_duplicate_pages",
                message="Multiple Notion pages match the stable operation or Memory ID",
                observation={"matched_pages": len(matches), "page_ids": [item["page"].get("id") for item in matches]},
            ),
        }
    match = matches[0]
    if (
        match["operation_id"] != operation_id
        or match["memory_id"] != memory_id
        or match["payload_hash"] != job["payload_hash"]
    ):
        return {
            "state": "conflict",
            "outcome": delivery_outcome(
                "conflict",
                code="notion_payload_conflict",
                message="Stable Notion identity exists with a different payload hash or mapping",
                remote_refs={"page_id": match["page"].get("id"), "page_url": match["page"].get("url")},
            ),
        }
    return {
        "state": "succeeded",
        "outcome": delivery_outcome(
            "succeeded",
            code="notion_page_verified",
            message="A unique Notion page matches the operation id, Memory ID, and payload hash",
            remote_refs={"page_id": match["page"].get("id"), "page_url": match["page"].get("url")},
            observation={"matched_pages": 1, "fully_paginated": True},
        ),
    }


def _apply_attempt_receipt_to_job(job: dict[str, Any], receipt: Mapping[str, Any], *, now: datetime) -> None:
    bound_fields = ("job_id", "book_id", "run_id", "publication_receipt_hash", "payload_hash", "target_type")
    if any(receipt[field] != job[field] for field in bound_fields):
        raise DeliveryConflictError("attempt receipt does not belong to DeliveryJob payload")
    state = str(receipt["outcome"]["state"])
    job["state"] = state
    job["lease"] = None
    job["last_attempt_receipt"] = {
        "attempt_id": receipt["attempt_id"],
        "relative_path": f"attempts/{job['job_id']}/{str(receipt['attempt_id']).replace(':', '_')}.json",
        "attempt_receipt_hash": receipt["attempt_receipt_hash"],
    }
    if state == "uncertain":
        job["uncertain_since"] = job.get("uncertain_since") or _iso(now)
    elif state == "succeeded":
        job["uncertain_since"] = None
    job["updated_at"] = _iso(now)


def _validate_delivery_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(outcome, Mapping):
        raise DeliveryError("DeliveryOutcome must be an object")
    payload = dict(outcome)
    if payload.get("state") not in _ATTEMPT_OUTCOME_STATES:
        raise DeliveryError(f"invalid DeliveryOutcome state: {payload.get('state')}")
    for field in ("code", "message"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise DeliveryError(f"DeliveryOutcome.{field} is required")
    payload.setdefault("remote_refs", {})
    payload.setdefault("observation", {})
    try:
        payload = validate_schema(payload, "delivery_outcome.schema.json")
    except SchemaValidationError as exc:
        raise DeliveryError(str(exc)) from exc
    return delivery_outcome(
        str(payload["state"]),
        code=str(payload["code"]),
        message=str(payload["message"]),
        remote_refs=payload["remote_refs"],
        observation=payload["observation"],
    )


def _write_job(path: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = job.get("updated_at") or _iso(datetime.now(timezone.utc))
    _atomic_replace_from_bytes(path, _json_bytes(validate_delivery_job(job)))


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_create_from_bytes(path, _json_bytes(dict(payload)))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"cannot read delivery JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeliveryError(f"delivery JSON must contain an object: {path}")
    return payload


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _notion_rich_text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": value}}]}


def _notion_property_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("rich_text", "title"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("plain_text"), str):
                parts.append(item["plain_text"])
            else:
                text = item.get("text")
                if isinstance(text, dict) and isinstance(text.get("content"), str):
                    parts.append(text["content"])
        return "".join(parts)
    select = value.get("select")
    if isinstance(select, dict) and isinstance(select.get("name"), str):
        return select["name"]
    return ""


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = {"api_key", "authorization", "token", "payload", "content", "body", "secret"}
    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key.lower() in forbidden:
            sanitized[key] = "<redacted>"
        elif isinstance(raw_value, Mapping):
            sanitized[key] = _sanitize_mapping(raw_value)
        elif isinstance(raw_value, list):
            sanitized[key] = [_sanitize_value(item) for item in raw_value[:50]]
        elif isinstance(raw_value, str):
            sanitized[key] = raw_value[:1000]
        else:
            sanitized[key] = raw_value
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:1000]
    return value


def _all_mapping_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(_all_mapping_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_all_mapping_keys(child))
    return keys


def _validate_id(field: str, value: Any) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise DeliveryError(f"{field} is invalid: {value!r}")
    return text


def _require_sha256(field: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DeliveryError(f"{field} must be a lowercase SHA-256 digest")


def _require_time(field: str, value: Any) -> datetime:
    try:
        parsed = _parse_time(value)
    except (TypeError, ValueError) as exc:
        raise DeliveryError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed is None:
        raise DeliveryError(f"{field} must be an ISO-8601 timestamp")
    return parsed


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


__all__ = [
    "DELIVERY_POLICIES",
    "DELIVERY_SCHEMA_VERSION",
    "DELIVERY_TERMINAL_STATES",
    "DeliveryAdapter",
    "DeliveryAttemptContext",
    "DeliveryConflictError",
    "DeliveryError",
    "DeliveryLeaseError",
    "DeliveryQueue",
    "FileDeliveryAdapter",
    "SafeFileDeliveryAdapter",
    "NotionDeliveryAdapter",
    "default_notion_property_map",
    "delivery_operation_id",
    "delivery_outcome",
    "delivery_outcome_from_legacy",
    "delivery_payload_hash",
    "load_delivery_attempt_receipt",
    "load_delivery_job",
    "notion_delivery_properties",
    "validate_delivery_attempt_receipt",
    "validate_delivery_job",
    "validate_notion_delivery_schema",
]
