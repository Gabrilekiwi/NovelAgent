from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_STATES = {
    "preparing",
    "prepared",
    "applying",
    "commit_marked",
    "completed",
    "rolling_back",
    "rolled_back",
    "recovery_required",
}
TERMINAL_TRANSACTION_STATES = {"completed", "rolled_back", "recovery_required"}
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FaultInjector = Callable[[str, int | None, Path | None], None]


class PersistenceError(RuntimeError):
    """Base error for local persistence transaction configuration failures."""


class PersistencePreparationError(PersistenceError):
    """Raised when a transaction cannot be safely prepared."""


class PersistenceLockError(PersistenceError):
    """Raised when another process owns the run-directory persistence lock."""


@contextmanager
def persistence_run_lock(
    run_dir: str | Path,
    *,
    state_paths: Iterable[str | Path] = (),
):
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_paths = {root / ".persistence.lock"}
    shared_lock_root = Path(tempfile.gettempdir()) / "novelagent-state-locks"
    for state_path in state_paths:
        identity = Path(state_path).resolve(strict=False)
        canonical = os.path.normcase(str(identity))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
        lock_paths.add(shared_lock_root / f"state-{digest}.lock")
    ordered = sorted(lock_paths, key=lambda item: os.path.normcase(str(item)))
    with ExitStack() as stack:
        for path in ordered:
            stack.enter_context(_exclusive_file_lock(path))
        yield tuple(ordered)


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise PersistenceLockError(f"persistence state is locked: {path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PersistenceLockError(f"persistence state is locked: {path}") from exc
        try:
            yield path
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@dataclass(frozen=True)
class PersistenceTarget:
    kind: str
    path: str | Path
    content: str | bytes
    metadata: Mapping[str, Any] = field(default_factory=dict)
    encoding: str = "utf-8"
    expected_before_exists: bool | None = None
    expected_before_sha256: str | None = None

    def content_bytes(self) -> bytes:
        if isinstance(self.content, bytes):
            return self.content
        if isinstance(self.content, str):
            return self.content.encode(self.encoding)
        raise TypeError(f"persistence target content must be str or bytes, got {type(self.content).__name__}")


@dataclass(frozen=True)
class PersistenceResult:
    run_id: str
    state: str
    committed: bool
    partial: bool
    journal_path: str
    commit_marker: str
    targets: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...] = ()
    candidate_result_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.state in {"completed", "rolled_back"} and not self.partial

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "committed": self.committed,
            "partial": self.partial,
            "journal_path": self.journal_path,
            "commit_marker": self.commit_marker,
            "targets": [dict(target) for target in self.targets],
            "errors": [dict(error) for error in self.errors],
            "candidate_result_path": self.candidate_result_path,
        }


class LocalPersistenceTransaction:
    """A small write-ahead transaction for local files on one or more roots.

    ``prepare`` only writes the journal. ``commit`` performs CAS-guarded target
    replacements and creates ``commit.marker`` after every target is verified.
    The marker is authoritative: a transaction without it is rolled back;
    a transaction with it is completed during reconciliation.
    """

    def __init__(
        self,
        *,
        run_dir: str | Path,
        run_id: str,
        allowed_roots: Iterable[str | Path],
        book_id: str | None = None,
        transactions_dir: str | Path | None = None,
        fault_injector: _FaultInjector | None = None,
        story_project_read_set: Mapping[str, Any] | None = None,
        read_set_declared_writes: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.run_id = _validate_run_id(run_id)
        self.book_id = str(book_id) if book_id is not None else None
        self.transactions_dir = (
            Path(transactions_dir).resolve()
            if transactions_dir is not None
            else self.run_dir / "transactions"
        )
        self.journal_dir = self.transactions_dir / self.run_id
        self.manifest_path = self.journal_dir / "manifest.json"
        self.candidate_path = self.journal_dir / "candidate_result.json"
        self.commit_marker_path = self.journal_dir / "commit.marker"
        self.allowed_roots = tuple(_validated_root(root) for root in allowed_roots)
        if not self.allowed_roots:
            raise PersistencePreparationError("at least one allowed persistence root is required")
        self._fault_injector = fault_injector
        self.story_project_read_set = (
            dict(story_project_read_set) if story_project_read_set is not None else None
        )
        self.read_set_declared_writes = [dict(item) for item in read_set_declared_writes]
        self._manifest: dict[str, Any] | None = None

    def prepare(
        self,
        targets: Iterable[PersistenceTarget],
        *,
        candidate_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.journal_dir.exists():
            raise PersistencePreparationError(f"transaction journal already exists: {self.journal_dir}")

        self._verify_story_project_read_set(phase="prepare")

        prepared_targets = self._preflight_targets(tuple(targets))
        if not prepared_targets:
            raise PersistencePreparationError("at least one persistence target is required")
        candidate_payload = dict(candidate_result) if candidate_result is not None else None
        candidate_bytes = None
        if candidate_payload is not None:
            candidate_bytes = _json_bytes(candidate_payload)

        self.transactions_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(exist_ok=False)
        (self.journal_dir / "staged").mkdir()
        (self.journal_dir / "backups").mkdir()
        now = _utc_now()
        self._manifest = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "run_id": self.run_id,
            "book_id": self.book_id,
            "state": "preparing",
            "created_at": now,
            "updated_at": now,
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "candidate_result_path": "candidate_result.json" if candidate_payload is not None else None,
            "candidate_sha256": _sha256(candidate_bytes) if candidate_bytes is not None else None,
            "commit_marker": "commit.marker",
            "story_project_read_set": self.story_project_read_set,
            "read_set_declared_writes": self.read_set_declared_writes,
            "targets": prepared_targets,
            "errors": [],
        }
        self._write_manifest()

        try:
            if candidate_payload is not None:
                _write_new_durable_file(self.candidate_path, candidate_bytes or b"")
            for target in prepared_targets:
                index = int(target["index"])
                content = target.pop("_content")
                before = target.pop("_before")
                _write_new_durable_file(self.journal_dir / target["staged_path"], content)
                if target["existed"]:
                    _write_new_durable_file(self.journal_dir / target["backup_path"], before)
                target["status"] = "prepared"
            self._manifest["state"] = "prepared"
            self._manifest["updated_at"] = _utc_now()
            self._write_manifest()
            _fsync_directory(self.journal_dir)
            return _public_manifest(self._manifest)
        except Exception as exc:
            self._manifest["state"] = "rolled_back"
            self._manifest["updated_at"] = _utc_now()
            self._manifest["errors"].append(_error_payload("prepare_failed", exc))
            self._best_effort_write_manifest()
            raise PersistencePreparationError(f"failed to prepare transaction {self.run_id}: {exc}") from exc

    def commit(self) -> PersistenceResult:
        manifest = self._load_or_current_manifest()
        if manifest.get("state") != "prepared":
            raise PersistenceError(f"transaction is not prepared: state={manifest.get('state')!r}")

        self._verify_story_project_read_set(phase="pre_apply", manifest=manifest)
        manifest["state"] = "applying"
        manifest["updated_at"] = _utc_now()
        self._write_manifest()
        marker_published = False
        try:
            for target in manifest["targets"]:
                index = int(target["index"])
                path = Path(target["path"])
                self._verify_story_project_read_set(phase="during_apply", manifest=manifest)
                self._assert_current_hash(target, phase="apply")
                if target["before_sha256"] == target["after_sha256"]:
                    target["status"] = "verified"
                    self._write_manifest()
                    continue
                target["status"] = "applying"
                self._write_manifest()
                self._inject("before_target_replace", index, path)
                self._verify_story_project_read_set(phase="during_apply", manifest=manifest)
                self._assert_current_hash(target, phase="replace")
                staged = self.journal_dir / target["staged_path"]
                _atomic_replace_from_bytes(path, staged.read_bytes())
                self._inject("after_target_replace", index, path)
                actual = _path_sha256(path)
                if actual != target["after_sha256"]:
                    raise PersistenceError(
                        f"target verification failed after replace: {path}; expected={target['after_sha256']} actual={actual}"
                    )
                target["status"] = "verified"
                self._write_manifest()

            self._inject("before_commit_marker", None, self.commit_marker_path)
            self._verify_story_project_read_set(phase="pre_marker", manifest=manifest)
            self._assert_all_after_hashes(manifest)
            _atomic_create_from_bytes(
                self.commit_marker_path,
                _json_bytes(
                    {
                        "run_id": self.run_id,
                        "committed_at": _utc_now(),
                        "candidate_sha256": manifest.get("candidate_sha256"),
                    }
                ),
            )
            marker_published = True
            self._inject("after_commit_marker", None, self.commit_marker_path)
            manifest["state"] = "commit_marked"
            manifest["updated_at"] = _utc_now()
            self._write_manifest()
            return self._result()
        except Exception as exc:
            manifest["errors"].append(_error_payload("commit_failed", exc))
            manifest["updated_at"] = _utc_now()
            if marker_published:
                manifest["state"] = "commit_marked"
                self._best_effort_write_manifest()
                return self._result()
            return self._rollback(error=exc)

    def complete_publication(self) -> PersistenceResult:
        manifest = self._load_or_current_manifest()
        marker_error = _validate_commit_marker(
            self.commit_marker_path,
            self.run_id,
            manifest.get("candidate_sha256"),
        )
        if marker_error is not None:
            raise PersistenceError(marker_error["error"])
        if manifest.get("state") == "completed":
            return self._result()
        if manifest.get("state") != "commit_marked":
            raise PersistenceError(f"transaction publication cannot complete from state={manifest.get('state')!r}")
        manifest["state"] = "completed"
        manifest["updated_at"] = _utc_now()
        self._write_manifest()
        return self._result()

    def _preflight_targets(self, targets: tuple[PersistenceTarget, ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for index, target in enumerate(targets):
            if not isinstance(target, PersistenceTarget):
                raise PersistencePreparationError(f"target {index} must be PersistenceTarget")
            if not str(target.kind).strip():
                raise PersistencePreparationError(f"target {index} kind must not be empty")
            path = _validated_target_path(target.path, self.allowed_roots)
            if path in seen:
                raise PersistencePreparationError(f"duplicate persistence target: {path}")
            seen.add(path)
            if not path.parent.exists() or not path.parent.is_dir():
                raise PersistencePreparationError(f"target parent directory does not exist: {path.parent}")
            if path.exists() and not path.is_file():
                raise PersistencePreparationError(f"target is not a regular file: {path}")
            if path.exists() and not os.access(path, os.R_OK | os.W_OK):
                raise PersistencePreparationError(f"target is not readable and writable: {path}")
            if not path.exists() and not os.access(path.parent, os.W_OK):
                raise PersistencePreparationError(f"target parent is not writable: {path.parent}")

            existed = path.exists()
            before = path.read_bytes() if existed else b""
            after = target.content_bytes()
            expected_exists = target.expected_before_exists
            expected_hash = target.expected_before_sha256
            if expected_exists is not None:
                if not isinstance(expected_exists, bool):
                    raise PersistencePreparationError(f"target expected_before_exists must be boolean: {path}")
                if existed != expected_exists:
                    raise PersistencePreparationError(
                        f"target changed before prepare: {path}; expected_exists={expected_exists} actual_exists={existed}"
                    )
                if expected_exists:
                    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                        raise PersistencePreparationError(f"target expected_before_sha256 is invalid: {path}")
                    actual_hash = _sha256(before)
                    if actual_hash != expected_hash:
                        raise PersistencePreparationError(
                            f"target changed before prepare: {path}; expected={expected_hash} actual={actual_hash}"
                        )
                elif expected_hash is not None:
                    raise PersistencePreparationError(
                        f"missing target cannot have expected_before_sha256: {path}"
                    )
            try:
                metadata = dict(target.metadata)
                _json_bytes(metadata)
            except Exception as exc:
                raise PersistencePreparationError(f"target metadata is not JSON serializable: {path}") from exc
            result.append(
                {
                    "index": index,
                    "id": f"target-{index:03d}",
                    "kind": str(target.kind),
                    "path": str(path),
                    "existed": existed,
                    "before_sha256": _sha256(before) if existed else None,
                    "after_sha256": _sha256(after),
                    "before_size": len(before) if existed else None,
                    "after_size": len(after),
                    "backup_path": f"backups/{index:03d}.bin" if existed else None,
                    "staged_path": f"staged/{index:03d}.bin",
                    "status": "planned",
                    "error": None,
                    "metadata": metadata,
                    "_before": before,
                    "_content": after,
                }
            )
        return result

    def _assert_current_hash(self, target: dict[str, Any], *, phase: str) -> None:
        path = _validated_target_path(target["path"], self.allowed_roots)
        if not path.parent.exists() or not path.parent.is_dir():
            raise PersistenceError(f"target parent changed during {phase}: {path.parent}")
        actual = _path_sha256(path)
        expected = target["before_sha256"]
        if actual != expected:
            raise PersistenceError(
                f"CAS mismatch during {phase}: {path}; expected={expected} actual={actual}"
            )

    def _assert_all_after_hashes(self, manifest: dict[str, Any]) -> None:
        for target in manifest["targets"]:
            path = _validated_target_path(target["path"], self.allowed_roots)
            actual = _path_sha256(path)
            if actual != target["after_sha256"]:
                raise PersistenceError(
                    f"after-hash drift before commit marker: {path}; "
                    f"expected={target['after_sha256']} actual={actual}"
                )

    def _verify_story_project_read_set(
        self,
        *,
        phase: str,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        read_set = (
            manifest.get("story_project_read_set")
            if isinstance(manifest, dict)
            else self.story_project_read_set
        )
        if not isinstance(read_set, dict):
            return
        declared = (
            manifest.get("read_set_declared_writes", [])
            if isinstance(manifest, dict)
            else self.read_set_declared_writes
        )
        from core.story_project.read_set import verify_story_project_read_set

        verify_story_project_read_set(
            read_set,
            declared_writes=declared,
            phase=phase,
        )

    def _rollback(self, *, error: Exception | None = None) -> PersistenceResult:
        manifest = self._load_or_current_manifest()
        manifest["state"] = "rolling_back"
        manifest["updated_at"] = _utc_now()
        self._best_effort_write_manifest()
        failures = _rollback_manifest_targets(
            self.journal_dir,
            manifest,
            fault_injector=self._fault_injector,
        )
        if failures:
            manifest["state"] = "recovery_required"
            manifest["errors"].extend(failures)
        else:
            manifest["state"] = "rolled_back"
        manifest["updated_at"] = _utc_now()
        if error is not None and not any(item.get("code") == "commit_failed" for item in manifest["errors"]):
            manifest["errors"].append(_error_payload("commit_failed", error))
        try:
            self._write_manifest()
        except Exception as exc:
            manifest["state"] = "recovery_required"
            manifest["errors"].append(_error_payload("rollback_manifest_write_failed", exc))
            self._best_effort_write_manifest()
        return self._result()

    def _load_or_current_manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            self._manifest = _load_manifest(self.journal_dir)
        return self._manifest

    def _write_manifest(self) -> None:
        if self._manifest is None:
            raise PersistenceError("transaction manifest has not been initialized")
        _atomic_replace_from_bytes(self.manifest_path, _json_bytes(_public_manifest(self._manifest)))

    def _best_effort_write_manifest(self) -> None:
        try:
            self._write_manifest()
        except Exception:
            pass

    def _inject(self, event: str, index: int | None, path: Path | None) -> None:
        if self._fault_injector is not None:
            self._fault_injector(event, index, path)

    def _result(self) -> PersistenceResult:
        manifest = self._load_or_current_manifest()
        return _result_from_manifest(self.journal_dir, manifest)


def reconcile_persistence(
    *,
    run_dir: str | Path,
    run_id: str | None = None,
    expected_book_id: str | None = None,
    transactions_dir: str | Path | None = None,
) -> dict[str, Any]:
    transaction_root = (
        Path(transactions_dir).resolve()
        if transactions_dir is not None
        else Path(run_dir).resolve() / "transactions"
    )
    if run_id is not None:
        journal_dirs = [transaction_root / _validate_run_id(run_id)]
    elif transaction_root.exists():
        journal_dirs = sorted(path for path in transaction_root.iterdir() if path.is_dir())
    else:
        journal_dirs = []

    results: list[dict[str, Any]] = []
    for journal_dir in journal_dirs:
        if not journal_dir.exists():
            continue
        if expected_book_id is not None:
            try:
                manifest = _load_manifest(journal_dir)
            except Exception:
                manifest = None
            if isinstance(manifest, dict) and manifest.get("book_id") != expected_book_id:
                actual = manifest.get("book_id")
                results.append(
                    PersistenceResult(
                        run_id=str(manifest.get("run_id") or journal_dir.name),
                        state="recovery_required",
                        committed=False,
                        partial=False,
                        journal_path=str(journal_dir),
                        commit_marker=str(journal_dir / str(manifest.get("commit_marker") or "commit.marker")),
                        targets=tuple(dict(item) for item in manifest.get("targets", []) if isinstance(item, dict)),
                        errors=(
                            {
                                "code": "story_project_state_identity_mismatch",
                                "error": (
                                    f"journal book_id {actual!r} does not match expected "
                                    f"{expected_book_id!r}"
                                ),
                            },
                        ),
                        candidate_result_path=(
                            str(journal_dir / str(manifest["candidate_result_path"]))
                            if manifest.get("candidate_result_path")
                            else None
                        ),
                    ).to_dict()
                )
                continue
        results.append(reconcile_persistence_transaction(journal_dir).to_dict())
    required = [result for result in results if result["state"] == "recovery_required"]
    return {
        "ok": not required,
        "transaction_count": len(results),
        "recovery_required": [result["run_id"] for result in required],
        "transactions": results,
    }


def reconcile_persistence_transaction(journal_dir: str | Path) -> PersistenceResult:
    journal = Path(journal_dir).resolve()
    try:
        manifest = _load_manifest(journal)
    except Exception as exc:
        return PersistenceResult(
            run_id=journal.name,
            state="recovery_required",
            committed=(journal / "commit.marker").exists(),
            partial=True,
            journal_path=str(journal),
            commit_marker=str(journal / "commit.marker"),
            targets=(),
            errors=(_error_payload("manifest_unreadable", exc),),
            candidate_result_path=str(journal / "candidate_result.json") if (journal / "candidate_result.json").exists() else None,
        )

    try:
        marker = _journal_child(journal, str(manifest.get("commit_marker") or "commit.marker"))
    except Exception as exc:
        manifest["state"] = "recovery_required"
        manifest.setdefault("errors", []).append(_error_payload("commit_marker_path_invalid", exc))
        manifest["updated_at"] = _utc_now()
        _best_effort_manifest_write(journal, manifest)
        return _result_from_manifest(journal, manifest)
    state = manifest.get("state")
    if state == "rolled_back" and not marker.exists():
        return _result_from_manifest(journal, manifest)
    if state == "completed" and marker.exists():
        if _validate_commit_marker(
            marker,
            str(manifest.get("run_id") or journal.name),
            manifest.get("candidate_sha256"),
        ) is None:
            return _result_from_manifest(journal, manifest)
    if marker.exists():
        marker_error = _validate_commit_marker(
            marker,
            str(manifest.get("run_id") or journal.name),
            manifest.get("candidate_sha256"),
        )
        failures = ([marker_error] if marker_error else []) + _verify_committed_targets(manifest)
        if failures:
            manifest["state"] = "recovery_required"
            manifest.setdefault("errors", []).extend(failures)
        else:
            for target in manifest.get("targets", []):
                target["status"] = "verified"
                target["error"] = None
            manifest["state"] = "commit_marked"
        manifest["updated_at"] = _utc_now()
        return _persist_reconciled_manifest(journal, manifest)

    manifest["state"] = "rolling_back"
    manifest["updated_at"] = _utc_now()
    _best_effort_manifest_write(journal, manifest)
    failures = _rollback_manifest_targets(journal, manifest)
    if failures:
        manifest["state"] = "recovery_required"
        manifest.setdefault("errors", []).extend(failures)
    else:
        manifest["state"] = "rolled_back"
    manifest["updated_at"] = _utc_now()
    return _persist_reconciled_manifest(journal, manifest)


def complete_persistence_transaction(journal_dir: str | Path) -> PersistenceResult:
    journal = Path(journal_dir).resolve()
    manifest = _load_manifest(journal)
    marker = _journal_child(journal, str(manifest.get("commit_marker") or "commit.marker"))
    marker_error = _validate_commit_marker(
        marker,
        str(manifest.get("run_id") or journal.name),
        manifest.get("candidate_sha256"),
    )
    if marker_error is not None:
        raise PersistenceError(marker_error["error"])
    if manifest.get("state") == "completed":
        return _result_from_manifest(journal, manifest)
    if manifest.get("state") != "commit_marked":
        raise PersistenceError(f"transaction publication cannot complete from state={manifest.get('state')!r}")
    manifest["state"] = "completed"
    manifest["updated_at"] = _utc_now()
    _atomic_replace_from_bytes(journal / "manifest.json", _json_bytes(_public_manifest(manifest)))
    return _result_from_manifest(journal, manifest)


def load_persistence_candidate(journal_dir: str | Path) -> dict[str, Any] | None:
    journal = Path(journal_dir).resolve()
    manifest = _load_manifest(journal)
    relative = manifest.get("candidate_result_path")
    if not relative:
        return None
    path = _journal_child(journal, str(relative))
    content = path.read_bytes()
    expected_hash = manifest.get("candidate_sha256")
    actual_hash = _sha256(content)
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise PersistenceError(
            f"candidate result hash mismatch: {path}; expected={expected_hash} actual={actual_hash}"
        )
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise PersistenceError(f"candidate result must be a JSON object: {path}")
    run = payload.get("run") if isinstance(payload.get("run"), dict) else None
    if not isinstance(run, dict) or run.get("id") != manifest.get("run_id"):
        raise PersistenceError(
            f"candidate result run id does not match transaction: {path}; expected={manifest.get('run_id')!r}"
        )
    return payload


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    _atomic_replace_from_bytes(Path(path), _json_bytes(dict(payload)))


def atomic_create_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    _atomic_create_from_bytes(Path(path), _json_bytes(dict(payload)))


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    _atomic_replace_from_bytes(Path(path), content.encode(encoding))


def atomic_create_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    """Durably publish text without replacing an existing file."""

    _atomic_create_from_bytes(Path(path), content.encode(encoding))


def _rollback_manifest_targets(
    journal_dir: Path,
    manifest: dict[str, Any],
    *,
    fault_injector: _FaultInjector | None = None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    roots = _manifest_allowed_roots(manifest)
    for target in reversed(manifest.get("targets", [])):
        index = int(target["index"])
        before_hash = target.get("before_sha256")
        after_hash = target.get("after_sha256")
        try:
            path = _validated_target_path(target["path"], roots)
            actual = _path_sha256(path)
            if actual == before_hash:
                target["status"] = "rolled_back"
                target["error"] = None
                continue
            if actual != after_hash:
                if target.get("status") in {"planned", "prepared"}:
                    target["status"] = "rolled_back"
                    target["error"] = "external_change_preserved"
                    continue
                raise PersistenceError(
                    f"rollback CAS mismatch: {path}; before={before_hash} after={after_hash} actual={actual}"
                )
            if fault_injector is not None:
                fault_injector("before_rollback_replace", index, path)
            if target.get("existed"):
                backup_relative = target.get("backup_path")
                if not backup_relative:
                    raise PersistenceError(f"backup path is missing for existing target: {path}")
                backup = _journal_child(journal_dir, str(backup_relative))
                if not backup.exists():
                    raise PersistenceError(f"backup file is missing: {backup}")
                backup_bytes = backup.read_bytes()
                if _sha256(backup_bytes) != before_hash:
                    raise PersistenceError(f"backup hash mismatch: {backup}")
                _atomic_replace_from_bytes(path, backup_bytes)
            else:
                path.unlink()
                _fsync_directory(path.parent)
            if fault_injector is not None:
                fault_injector("after_rollback_replace", index, path)
            restored = _path_sha256(path)
            if restored != before_hash:
                raise PersistenceError(
                    f"rollback verification failed: {path}; expected={before_hash} actual={restored}"
                )
            target["status"] = "rolled_back"
            target["error"] = None
        except Exception as exc:
            path = Path(str(target.get("path") or "<invalid>"))
            target["status"] = "rollback_failed"
            target["error"] = f"{type(exc).__name__}: {exc}"
            failures.append(
                {
                    "code": "rollback_failed",
                    "target": str(path),
                    "kind": target.get("kind"),
                    "error": target["error"],
                }
            )
    return failures


def _verify_committed_targets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    try:
        roots = _manifest_allowed_roots(manifest)
    except Exception as exc:
        return [_error_payload("manifest_roots_invalid", exc)]
    for target in manifest.get("targets", []):
        try:
            path = _validated_target_path(target["path"], roots)
            actual = _path_sha256(path)
            if actual == target.get("after_sha256"):
                continue
            error = f"expected={target.get('after_sha256')} actual={actual}"
        except Exception as exc:
            path = Path(str(target.get("path") or "<invalid>"))
            error = f"{type(exc).__name__}: {exc}"
        failures.append(
            {
                "code": "committed_target_drift",
                "target": str(path),
                "kind": target.get("kind"),
                "error": error,
            }
        )
    return failures


def _load_manifest(journal_dir: Path) -> dict[str, Any]:
    path = journal_dir / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PersistenceError(f"transaction manifest must be a JSON object: {path}")
    if payload.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise PersistenceError(f"unsupported transaction schema version: {payload.get('schema_version')!r}")
    state = payload.get("state")
    if state not in TRANSACTION_STATES:
        raise PersistenceError(f"invalid transaction state: {state!r}")
    if payload.get("run_id") != journal_dir.name:
        raise PersistenceError(f"transaction run_id does not match journal directory: {journal_dir}")
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise PersistenceError("transaction targets must be a list")
    _manifest_allowed_roots(payload)
    return payload


def _result_from_manifest(journal_dir: Path, manifest: dict[str, Any]) -> PersistenceResult:
    state = str(manifest.get("state"))
    targets = tuple(_public_target(target) for target in manifest.get("targets", []))
    # ``partial`` has one narrow meaning: a pre-marker rollback could not be
    # completed. Post-marker drift still requires recovery, but is not a
    # partially applied transaction because the durable commit already won.
    partial = any(target.get("status") == "rollback_failed" for target in targets)
    candidate_relative = manifest.get("candidate_result_path")
    marker = journal_dir / str(manifest.get("commit_marker") or "commit.marker")
    marker_valid = marker.exists() and _validate_commit_marker(
        marker,
        str(manifest.get("run_id") or journal_dir.name),
        manifest.get("candidate_sha256"),
    ) is None
    return PersistenceResult(
        run_id=str(manifest.get("run_id") or journal_dir.name),
        state=state,
        committed=marker_valid,
        partial=partial,
        journal_path=str(journal_dir),
        commit_marker=str(journal_dir / str(manifest.get("commit_marker") or "commit.marker")),
        targets=targets,
        errors=tuple(dict(error) for error in manifest.get("errors", [])),
        candidate_result_path=str(journal_dir / str(candidate_relative)) if candidate_relative else None,
    )


def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: [_public_target(item) for item in value] if key == "targets" else value
        for key, value in manifest.items()
        if not key.startswith("_")
    }


def _public_target(target: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in target.items() if not key.startswith("_")}


def _best_effort_manifest_write(journal_dir: Path, manifest: dict[str, Any]) -> None:
    try:
        _atomic_replace_from_bytes(journal_dir / "manifest.json", _json_bytes(_public_manifest(manifest)))
    except Exception:
        pass


def _persist_reconciled_manifest(journal_dir: Path, manifest: dict[str, Any]) -> PersistenceResult:
    try:
        _atomic_replace_from_bytes(journal_dir / "manifest.json", _json_bytes(_public_manifest(manifest)))
    except Exception as exc:
        manifest["state"] = "recovery_required"
        manifest.setdefault("errors", []).append(_error_payload("manifest_transition_failed", exc))
    return _result_from_manifest(journal_dir, manifest)


def _validated_root(root: str | Path) -> Path:
    path = Path(root).resolve()
    if not path.exists() or not path.is_dir():
        raise PersistencePreparationError(f"allowed persistence root is not a directory: {path}")
    return path


def _validated_target_path(path_value: str | Path, allowed_roots: tuple[Path, ...]) -> Path:
    raw = Path(path_value)
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    raw = Path(os.path.abspath(raw))
    current = raw
    while True:
        if current.is_symlink():
            raise PersistencePreparationError(f"symlink path component is not allowed: {current}")
        if current == current.parent:
            break
        current = current.parent
    path = raw.resolve(strict=False)
    matching_roots = [root for root in allowed_roots if _is_relative_to(path, root)]
    if not matching_roots:
        raise PersistencePreparationError(f"target escapes allowed persistence roots: {path}")
    return path


def _manifest_allowed_roots(manifest: Mapping[str, Any]) -> tuple[Path, ...]:
    values = manifest.get("allowed_roots")
    if not isinstance(values, list) or not values:
        raise PersistenceError("transaction manifest allowed_roots must be a non-empty list")
    roots: list[Path] = []
    for value in values:
        path = Path(str(value))
        if not path.is_absolute():
            raise PersistenceError(f"transaction allowed root must be absolute: {value!r}")
        roots.append(path.resolve(strict=False))
    return tuple(roots)


def _journal_child(journal_dir: Path, relative: str) -> Path:
    path = (journal_dir / relative).resolve(strict=False)
    if not _is_relative_to(path, journal_dir):
        raise PersistenceError(f"journal path escapes transaction directory: {relative!r}")
    return path


def _validate_commit_marker(
    marker: Path,
    run_id: str,
    candidate_sha256: str | None,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("run_id") != run_id
            or not payload.get("committed_at")
            or payload.get("candidate_sha256") != candidate_sha256
        ):
            raise PersistenceError(f"invalid commit marker payload: {marker}")
        return None
    except Exception as exc:
        return _error_payload("commit_marker_invalid", exc)


def _validate_run_id(run_id: str) -> str:
    value = str(run_id)
    if not _RUN_ID_PATTERN.fullmatch(value):
        raise PersistencePreparationError(f"invalid persistence run_id: {run_id!r}")
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        return "not-a-regular-file"
    return _sha256(path.read_bytes())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new_durable_file(path: Path, content: bytes) -> None:
    _atomic_create_from_bytes(path, content)


def _atomic_replace_from_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The target name can itself be a 64-character content hash. Repeating it
    # in the temporary name needlessly crosses classic Windows MAX_PATH limits
    # for otherwise valid StoryProject roots.
    fd, tmp_name = tempfile.mkstemp(prefix=".na-", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _windows_move_file(tmp_path, path, replace_existing=True)
        else:
            os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_create_from_bytes(path: Path, content: bytes) -> None:
    """Durably publish a new file without ever replacing an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"atomic create target already exists: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=".na-", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _windows_move_file(tmp_path, path, replace_existing=False)
        else:
            # Linking a fully durable same-directory file is an atomic,
            # no-clobber publication. Unlike os.replace, it cannot overwrite a
            # marker won by another process between the check and publication.
            os.link(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_move_file(source: Path, destination: Path, *, replace_existing: bool) -> None:
    import ctypes

    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    flags = movefile_write_through | (movefile_replace_existing if replace_existing else 0)
    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    move.restype = ctypes.c_int
    if move(str(source), str(destination), flags):
        return
    error_code = ctypes.get_last_error()
    raise OSError(error_code, f"MoveFileExW failed: {source} -> {destination}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_payload(code: str, exc: Exception) -> dict[str, Any]:
    return {"code": code, "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "atomic_create_json",
    "atomic_write_json",
    "atomic_write_text",
    "complete_persistence_transaction",
    "LocalPersistenceTransaction",
    "PersistenceError",
    "PersistenceLockError",
    "PersistencePreparationError",
    "PersistenceResult",
    "PersistenceTarget",
    "TERMINAL_TRANSACTION_STATES",
    "TRANSACTION_SCHEMA_VERSION",
    "TRANSACTION_STATES",
    "load_persistence_candidate",
    "reconcile_persistence",
    "reconcile_persistence_transaction",
    "persistence_run_lock",
]
