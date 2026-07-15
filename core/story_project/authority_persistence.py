from __future__ import annotations

"""StoryProject-global event-authority persistence recovery.

Durable EA entries never store absolute transaction-root paths.  The sole
mutable EA-global physical-path control-plane is ``ea/root_registry.json``;
immutable local PersistenceV2 manifests retain their own root snapshots.
Every pending entry
binds its registry identity, exact revision/digest, StoryProject root UUID, and
a logical ``PathRef``.  Adding or remapping a logical root is forbidden while
``ea/r/p`` or ``ea/r/x`` contains an entry, and a binding mismatch fails closed
as recovery-required rather than treating the original transaction as absent.

Root relocation moves no data.  It is an explicit, idle-only control-plane
operation; the runtime root remains non-remappable without a separate
persistence control-plane relocation.
"""

import copy
import hashlib
import json
import os
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from core.engine.persistence import (
    PersistenceLockError,
    _fsync_directory,
    atomic_create_json,
    persistence_run_lock,
)
from core.engine.persistence_v2 import (
    PersistenceV2Transaction,
    load_persistence_manifest_v2,
    reconcile_pending_persistence_v2,
)
from core.engine.root_registry import RootRegistryService
from core.engine.safe_paths import RootBinding, SafePathResolver, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.path_refs import PathRef, path_ref_for, validate_path_ref
from core.story_project.identity import load_project_identity


EVENT_AUTHORITY_BARRIER_SCHEMA_VERSION = "1.1"
EVENT_AUTHORITY_WRITER_KINDS = frozenset(
    {"chapter", "migration", "history_revision"}
)
_TERMINAL_STATES = frozenset({"completed", "rolled_back", "abandoned"})
_GLOBAL_HOME_RELATIVE = ".novelagent/runtime/ea"
_DEPENDENCY_FENCE_RELATIVE = ".novelagent/runtime/.root-remap-fence"
_STATE_DIRECTORIES = {
    "pending": "p",
    "recovery_required": "x",
    "completed": "c",
    "rolled_back": "b",
    "abandoned": "a",
}


class EventAuthorityPersistenceBarrierError(RuntimeError):
    """The event authority cannot advance until global recovery is resolved."""


@dataclass
class EventAuthorityWriteOperation:
    story_project_root: Path
    book_id: str | None
    writer_kind: str
    home: Path
    resolver: SafePathResolver
    recovery: dict[str, Any]
    dependency_fence_root: Path
    dependency_fence_held: bool = False

    def bind_book_id(self, expected_book_id: str) -> None:
        """Bind a just-created legacy identity while retaining the same fence."""

        identity = load_project_identity(self.story_project_root)
        if identity is None or identity.ephemeral or identity.book_id != expected_book_id:
            raise EventAuthorityPersistenceBarrierError(
                "event_authority_barrier_book_mismatch: stable ProjectIdentity binding failed"
            )
        if self.book_id is not None and self.book_id != expected_book_id:
            raise EventAuthorityPersistenceBarrierError(
                "event_authority_barrier_book_mismatch: recovered authority belongs to another book"
            )
        self.book_id = expected_book_id

    def prepare_transaction(
        self,
        transaction: PersistenceV2Transaction,
        **prepare_kwargs: Any,
    ) -> dict[str, Any]:
        """Register the global barrier before a local journal can be published."""

        entry = self._register(transaction)
        try:
            return transaction.prepare(**prepare_kwargs)
        except Exception:
            # Preparation failures are marker-less.  Reconcile while this
            # writer still owns the StoryProject operation lock so partial
            # staging cannot be mistaken for another live writer.
            _reconcile_entry_locked(
                entry_path=self._pending_path(str(entry["entry_id"])),
                entry=entry,
                story_project_root=self.story_project_root,
                home=self.home,
                resolver=self.resolver,
            )
            raise

    def commit_transaction(
        self, transaction: PersistenceV2Transaction
    ) -> dict[str, Any]:
        if self.dependency_fence_held:
            result = transaction.commit()
        else:
            try:
                dependency_manager = persistence_run_lock(
                    self.dependency_fence_root
                )
                dependency_manager.__enter__()
            except PersistenceLockError as exc:
                raise EventAuthorityPersistenceBarrierError(
                    "event_authority_dependency_busy: an outline/session writer owns the dependency fence"
                ) from exc
            try:
                result = transaction.commit()
            finally:
                dependency_manager.__exit__(None, None, None)
        state = str(result.get("state") or "")
        transaction_ref, _registry_binding = _transaction_root_ref(
            home=self.home,
            story_project_root=self.story_project_root,
            transaction_root=transaction.transaction_root,
            writer_kind=self.writer_kind,
        )
        entry_id = _entry_id(transaction_ref, transaction.run_id)
        pending = self._pending_path(entry_id)
        if state in _TERMINAL_STATES:
            if pending.exists():
                entry = _load_entry(
                    pending,
                    expected_entry_id=entry_id,
                    story_project_root=self.story_project_root,
                    expected_book_id=self.book_id,
                )
                _settle_entry(
                    entry_path=pending,
                    entry=entry,
                    state=state,
                    home=self.home,
                    resolver=self.resolver,
                )
        elif state == "recovery_required":
            if pending.exists():
                entry = _load_entry(
                    pending,
                    expected_entry_id=entry_id,
                    story_project_root=self.story_project_root,
                    expected_book_id=self.book_id,
                )
                _mark_entry_recovery_required(
                    entry_path=pending,
                    entry=entry,
                    error="local PersistenceV2 transaction requires recovery",
                    home=self.home,
                    resolver=self.resolver,
                )
        # commit_marked/applying/prepared remains globally pending.  In
        # particular, a crash after the marker must be completed forward by
        # the next entrypoint before any descendant authority write starts.
        return result

    def _register(self, transaction: PersistenceV2Transaction) -> dict[str, Any]:
        if self.book_id is None or transaction.book_id != self.book_id:
            raise EventAuthorityPersistenceBarrierError(
                "event_authority_barrier_book_mismatch: transaction belongs to another book"
            )
        transaction_story = transaction.root_map.get("story_project")
        if transaction_story is None or not _same_path(
            Path(transaction_story), self.story_project_root
        ):
            raise EventAuthorityPersistenceBarrierError(
                "event_authority_barrier_story_root_mismatch: transaction is bound to another StoryProject"
            )
        transaction_root = assert_safe_local_tree(transaction.transaction_root)
        transaction_ref, registry_binding = _transaction_root_ref(
            home=self.home,
            story_project_root=self.story_project_root,
            transaction_root=transaction_root,
            writer_kind=self.writer_kind,
        )
        entry_id = _entry_id(transaction_ref, transaction.run_id)
        pending_path = self._pending_path(entry_id)
        entry = {
            "schema_version": EVENT_AUTHORITY_BARRIER_SCHEMA_VERSION,
            "entry_id": entry_id,
            "book_id": self.book_id,
            "writer_kind": self.writer_kind,
            "story_project_root_sha256": _path_identity_sha256(
                self.story_project_root
            ),
            "run_id": transaction.run_id,
            "root_registry_id": registry_binding["registry_id"],
            "root_registry_revision": registry_binding["revision"],
            "root_registry_digest": registry_binding["registry_digest"],
            "story_project_root_uuid": registry_binding["story_project_root_uuid"],
            "transaction_root_ref": transaction_ref.to_dict(),
            "state": "pending",
            "registered_at": _utc_now(),
        }
        entry["entry_hash"] = canonical_json_hash(entry)

        if pending_path.exists():
            existing = _load_entry(
                pending_path,
                expected_entry_id=entry_id,
                story_project_root=self.story_project_root,
                expected_book_id=self.book_id,
            )
            invariant_fields = (
                "entry_id",
                "book_id",
                "writer_kind",
                "story_project_root_sha256",
                "run_id",
                "root_registry_id",
                "root_registry_revision",
                "root_registry_digest",
                "story_project_root_uuid",
                "transaction_root_ref",
                "state",
            )
            if any(existing.get(field) != entry.get(field) for field in invariant_fields):
                raise EventAuthorityPersistenceBarrierError(
                    f"event_authority_barrier_entry_collision: {entry_id}"
                )
            return existing

        for state in (*sorted(_TERMINAL_STATES), "recovery_required"):
            terminal = _entry_path(
                self.home, self.resolver, state=state, entry_id=entry_id
            )
            if terminal.exists():
                raise EventAuthorityPersistenceBarrierError(
                    f"event_authority_barrier_run_reuse: {transaction.run_id}"
                )
        atomic_create_json(pending_path, entry)
        _fsync_directory(pending_path.parent)
        return copy.deepcopy(entry)

    def _pending_path(self, entry_id: str) -> Path:
        return _entry_path(
            self.home, self.resolver, state="pending", entry_id=entry_id
        )


@contextmanager
def event_authority_write_operation(
    story_project_root: str | Path,
    *,
    expected_book_id: str | None,
    writer_kind: str,
    allow_identity_missing: bool = False,
) -> Iterator[EventAuthorityWriteOperation]:
    """Own the one writer lock and reconcile every known authority root.

    The registry is stored below the StoryProject rather than any caller's
    transaction root.  A process crash releases the OS lock but cannot erase
    the pending entry already durably published there.
    """

    if writer_kind not in EVENT_AUTHORITY_WRITER_KINDS:
        raise EventAuthorityPersistenceBarrierError(
            f"event_authority_writer_kind_invalid: {writer_kind!r}"
        )
    story_root, home, resolver = _barrier_layout(story_project_root)
    stack = ExitStack()
    try:
        # PersistenceV2.commit acquires its exact apply-target locks below this
        # fence; pre-acquiring those same non-reentrant Windows byte locks would
        # self-deadlock.
        stack.enter_context(persistence_run_lock(home))
    except PersistenceLockError as exc:
        stack.close()
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_writer_busy: another StoryProject authority writer owns the lock"
        ) from exc
    with stack:
        # Outline/session writers use this durable fence before their own
        # narrower locks.  Recovery always takes it because a pending entry may
        # be a history transaction.  History writers retain it through their
        # PersistenceV2 marker; other authority writers release it after global
        # recovery so AgentExecutor autonomy callbacks cannot self-lock.
        dependency_stack = ExitStack()
        try:
            dependency_stack.enter_context(
                persistence_run_lock(_dependency_fence_root(resolver))
            )
        except PersistenceLockError as exc:
            dependency_stack.close()
            raise EventAuthorityPersistenceBarrierError(
                "event_authority_dependency_busy: an outline/session writer owns the dependency fence"
            ) from exc
        try:
            recovery = _reconcile_event_authority_persistence_locked(
                story_project_root=story_root,
                expected_book_id=expected_book_id,
                home=home,
                resolver=resolver,
            )
            if not recovery["ok"]:
                raise EventAuthorityPersistenceBarrierError(
                    "event_authority_global_recovery_required: "
                    + ", ".join(recovery["recovery_required"])
                )
            identity = load_project_identity(story_root)
            if identity is None or identity.ephemeral:
                if not allow_identity_missing:
                    raise EventAuthorityPersistenceBarrierError(
                        "event_authority_barrier_identity_missing: a stable ProjectIdentity is required"
                    )
                book_id = recovery.get("book_id")
            else:
                book_id = identity.book_id
            recovered_book_id = recovery.get("book_id")
            if (
                expected_book_id is not None
                and book_id is not None
                and book_id != expected_book_id
            ) or (
                recovered_book_id is not None
                and book_id is not None
                and recovered_book_id != book_id
            ):
                raise EventAuthorityPersistenceBarrierError(
                    "event_authority_barrier_book_mismatch: ProjectIdentity belongs to another book"
                )
            operation = EventAuthorityWriteOperation(
                story_project_root=story_root,
                book_id=book_id,
                writer_kind=writer_kind,
                home=home,
                resolver=resolver,
                recovery=recovery,
                dependency_fence_root=_dependency_fence_root(resolver),
                dependency_fence_held=(writer_kind == "history_revision"),
            )
            if writer_kind != "history_revision":
                dependency_stack.close()
            yield operation
        finally:
            dependency_stack.close()


def reconcile_event_authority_persistence(
    story_project_root: str | Path,
    *,
    expected_book_id: str | None = None,
) -> dict[str, Any]:
    """Recover all transaction roots registered for one StoryProject."""

    story_root, home, resolver = _barrier_layout(story_project_root)
    stack = ExitStack()
    try:
        stack.enter_context(persistence_run_lock(home))
    except PersistenceLockError as exc:
        stack.close()
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_writer_busy: another StoryProject authority writer owns the lock"
        ) from exc
    with stack:
        dependency_stack = ExitStack()
        try:
            dependency_stack.enter_context(
                persistence_run_lock(_dependency_fence_root(resolver))
            )
        except PersistenceLockError as exc:
            dependency_stack.close()
            raise EventAuthorityPersistenceBarrierError(
                "event_authority_dependency_busy: an outline/session writer owns the dependency fence"
            ) from exc
        with dependency_stack:
            recovery = _reconcile_event_authority_persistence_locked(
                story_project_root=story_root,
                expected_book_id=expected_book_id,
                home=home,
                resolver=resolver,
            )
            if not recovery["ok"]:
                return recovery
            identity = load_project_identity(story_root)
            if identity is None or identity.ephemeral:
                raise EventAuthorityPersistenceBarrierError(
                    "event_authority_barrier_identity_missing: a stable ProjectIdentity is required"
                )
            if (
                expected_book_id is not None
                and identity.book_id != expected_book_id
            ) or (
                recovery.get("book_id") is not None
                and recovery["book_id"] != identity.book_id
            ):
                raise EventAuthorityPersistenceBarrierError(
                    "event_authority_barrier_book_mismatch: ProjectIdentity belongs to another book"
                )
            recovery["book_id"] = identity.book_id
            return recovery


def _reconcile_event_authority_persistence_locked(
    *,
    story_project_root: Path,
    expected_book_id: str | None,
    home: Path,
    resolver: SafePathResolver,
) -> dict[str, Any]:
    transactions: list[dict[str, Any]] = []
    recovery_required: list[str] = []
    paths: list[Path] = []
    observed_book_id = expected_book_id
    for state in ("pending", "recovery_required"):
        directory = _registry_dir(home, resolver, state)
        paths.extend(sorted(directory.glob("*.json")))

    for entry_path in paths:
        entry_id = entry_path.stem
        try:
            entry = _load_entry(
                entry_path,
                expected_entry_id=entry_id,
                story_project_root=story_project_root,
                expected_book_id=expected_book_id,
            )
            entry_book_id = str(entry["book_id"])
            if observed_book_id is None:
                observed_book_id = entry_book_id
            elif observed_book_id != entry_book_id:
                raise EventAuthorityPersistenceBarrierError(
                    "event_authority_barrier_book_mismatch: global entries span multiple books"
                )
            result = _reconcile_entry_locked(
                entry_path=entry_path,
                entry=entry,
                story_project_root=story_project_root,
                home=home,
                resolver=resolver,
            )
        except Exception as exc:
            recovery_required.append(entry_id)
            result = {
                "entry_id": entry_id,
                "run_id": None,
                "writer_kind": None,
                "transaction_root_ref": None,
                "state": "recovery_required",
                "error": f"{type(exc).__name__}: {exc}",
            }
        transactions.append(result)
        if result.get("state") == "recovery_required":
            value = str(result.get("entry_id") or entry_id)
            if value not in recovery_required:
                recovery_required.append(value)

    return {
        "schema_version": EVENT_AUTHORITY_BARRIER_SCHEMA_VERSION,
        "ok": not recovery_required,
        "book_id": observed_book_id,
        "registry_root": str(home / "r"),
        "transaction_count": len(transactions),
        "transactions": transactions,
        "recovery_required": sorted(recovery_required),
    }


def _reconcile_entry_locked(
    *,
    entry_path: Path,
    entry: Mapping[str, Any],
    story_project_root: Path,
    home: Path,
    resolver: SafePathResolver,
) -> dict[str, Any]:
    transaction_root = _resolve_transaction_root_ref(
        home=home,
        entry=entry,
    )
    run_id = str(entry["run_id"])
    journal = transaction_root / "journals" / run_id
    staging = transaction_root / "staging" / run_id

    manifest_path: Path | None = None
    if (journal / "manifest.json").is_file():
        manifest_path = journal / "manifest.json"
    elif (staging / "manifest.json").is_file():
        manifest_path = staging / "manifest.json"
    if manifest_path is not None:
        manifest = load_persistence_manifest_v2(manifest_path)
        _assert_manifest_entry_binding(
            manifest,
            entry=entry,
            story_project_root=story_project_root,
        )
    elif not journal.exists() and not staging.exists():
        _settle_entry(
            entry_path=entry_path,
            entry=entry,
            state="abandoned",
            home=home,
            resolver=resolver,
        )
        return _entry_result(entry, state="abandoned", local_recovery=None)

    local = reconcile_pending_persistence_v2(
        transaction_root, expected_book_id=str(entry["book_id"])
    )
    match = next(
        (
            item
            for item in local.get("transactions", [])
            if isinstance(item, Mapping) and item.get("run_id") == run_id
        ),
        None,
    )
    state = str(match.get("state")) if isinstance(match, Mapping) else ""
    if not state:
        state = _verified_local_terminal_state(
            transaction_root=transaction_root,
            run_id=run_id,
            entry=entry,
            story_project_root=story_project_root,
        )
    if state in _TERMINAL_STATES:
        _settle_entry(
            entry_path=entry_path,
            entry=entry,
            state=state,
            home=home,
            resolver=resolver,
        )
        return _entry_result(entry, state=state, local_recovery=local)

    error = (
        f"local PersistenceV2 recovery is unresolved: state={state or 'missing'}; "
        f"recovery_required={local.get('recovery_required')}"
    )
    _mark_entry_recovery_required(
        entry_path=entry_path,
        entry=entry,
        error=error,
        home=home,
        resolver=resolver,
    )
    return {
        **_entry_result(entry, state="recovery_required", local_recovery=local),
        "error": error,
    }


def _verified_local_terminal_state(
    *,
    transaction_root: Path,
    run_id: str,
    entry: Mapping[str, Any],
    story_project_root: Path,
) -> str:
    journal = transaction_root / "journals" / run_id
    manifest_path = journal / "manifest.json"
    if not manifest_path.is_file():
        return ""
    manifest = load_persistence_manifest_v2(manifest_path)
    _assert_manifest_entry_binding(
        manifest, entry=entry, story_project_root=story_project_root
    )
    state = str(manifest.get("state") or "")
    local_registry_entry = transaction_root / "registry" / state / f"{run_id}.json"
    if state not in {"completed", "rolled_back"} or not local_registry_entry.is_file():
        return state
    if state == "completed":
        root_map = {
            str(root_id): Path(str(binding["path"]))
            for root_id, binding in manifest["immutable"]["root_map"].items()
        }
        transaction = PersistenceV2Transaction(
            transaction_root=transaction_root,
            run_id=run_id,
            book_id=str(entry["book_id"]),
            root_map=root_map,
        )
        verified = transaction.complete_publication()
        if not verified.get("committed") or verified.get("state") != "completed":
            return "recovery_required"
    return state


def _assert_manifest_entry_binding(
    manifest: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
    story_project_root: Path,
) -> None:
    immutable = manifest.get("immutable")
    if not isinstance(immutable, Mapping):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_manifest_invalid: immutable payload missing"
        )
    story_binding = (immutable.get("root_map") or {}).get("story_project")
    if (
        immutable.get("run_id") != entry.get("run_id")
        or immutable.get("book_id") != entry.get("book_id")
        or not isinstance(story_binding, Mapping)
        or not _same_path(Path(str(story_binding.get("path"))), story_project_root)
    ):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_manifest_mismatch: registered journal belongs to another authority"
        )


def _load_entry(
    path: Path,
    *,
    expected_entry_id: str,
    story_project_root: Path,
    expected_book_id: str | None,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventAuthorityPersistenceBarrierError(
            f"event_authority_barrier_entry_unreadable: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: entry must be an object"
        )
    required = {
        "schema_version",
        "entry_id",
        "entry_hash",
        "book_id",
        "writer_kind",
        "story_project_root_sha256",
        "run_id",
        "root_registry_id",
        "root_registry_revision",
        "root_registry_digest",
        "story_project_root_uuid",
        "transaction_root_ref",
        "state",
        "registered_at",
    }
    if set(value) != required:
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: exact fields are required"
        )
    if (
        value["schema_version"] != EVENT_AUTHORITY_BARRIER_SCHEMA_VERSION
        or value["entry_id"] != expected_entry_id
        or (
            expected_book_id is not None
            and value["book_id"] != expected_book_id
        )
        or value["writer_kind"] not in EVENT_AUTHORITY_WRITER_KINDS
        or value["state"] not in {"pending", "recovery_required"}
        or value["story_project_root_sha256"]
        != _path_identity_sha256(story_project_root)
    ):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: identity fields differ"
        )
    try:
        registry_id = str(uuid.UUID(str(value["root_registry_id"])))
    except ValueError as exc:
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: root registry id is invalid"
        ) from exc
    if (
        registry_id != value["root_registry_id"]
        or not isinstance(value["root_registry_revision"], int)
        or isinstance(value["root_registry_revision"], bool)
        or value["root_registry_revision"] < 1
        or not isinstance(value["root_registry_digest"], str)
        or len(value["root_registry_digest"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["root_registry_digest"]
        )
        or not isinstance(value["book_id"], str)
        or not value["book_id"]
        or not isinstance(value["run_id"], str)
        or not value["run_id"]
        or not isinstance(value["registered_at"], str)
        or len(value["entry_id"]) != 32
        or any(character not in "0123456789abcdef" for character in value["entry_id"])
    ):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: scalar fields are invalid"
        )
    try:
        story_root_uuid = str(uuid.UUID(str(value["story_project_root_uuid"])))
    except ValueError as exc:
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: StoryProject root UUID is invalid"
        ) from exc
    if story_root_uuid != value["story_project_root_uuid"]:
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: StoryProject root UUID is not canonical"
        )
    transaction_ref = validate_path_ref(value["transaction_root_ref"])
    if (
        value["entry_id"] != _entry_id(transaction_ref, str(value["run_id"]))
        or value["entry_hash"]
        != canonical_json_hash(value, exclude_fields=("entry_hash",))
    ):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_entry_invalid: hash binding failed"
        )
    _resolve_transaction_root_ref(home=path.parents[2], entry=value)
    return value


def _settle_entry(
    *,
    entry_path: Path,
    entry: Mapping[str, Any],
    state: str,
    home: Path,
    resolver: SafePathResolver,
) -> None:
    if state not in _TERMINAL_STATES:
        raise EventAuthorityPersistenceBarrierError(
            f"event_authority_barrier_terminal_state_invalid: {state}"
        )
    destination = _entry_path(
        home, resolver, state=state, entry_id=str(entry["entry_id"])
    )
    terminal = dict(entry)
    terminal["state"] = state
    terminal["entry_hash"] = canonical_json_hash(
        terminal, exclude_fields=("entry_hash",)
    )
    _write_terminal_idempotent(destination, terminal)
    entry_path.unlink(missing_ok=True)
    _fsync_directory(entry_path.parent)


def _mark_entry_recovery_required(
    *,
    entry_path: Path,
    entry: Mapping[str, Any],
    error: str,
    home: Path,
    resolver: SafePathResolver,
) -> None:
    destination = _entry_path(
        home,
        resolver,
        state="recovery_required",
        entry_id=str(entry["entry_id"]),
    )
    recovery = dict(entry)
    recovery["state"] = "recovery_required"
    # Keep the registry entry's exact schema small; the detailed local error is
    # already durable in the PersistenceV2 manifest and returned in the report.
    recovery["entry_hash"] = canonical_json_hash(
        recovery, exclude_fields=("entry_hash",)
    )
    _write_terminal_idempotent(destination, recovery)
    if entry_path != destination:
        entry_path.unlink(missing_ok=True)
        _fsync_directory(entry_path.parent)


def _write_terminal_idempotent(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EventAuthorityPersistenceBarrierError(
                f"event_authority_barrier_terminal_unreadable: {path}: {exc}"
            ) from exc
        if existing != dict(value):
            raise EventAuthorityPersistenceBarrierError(
                f"event_authority_barrier_terminal_collision: {path}"
            )
        return
    atomic_create_json(path, dict(value))
    _fsync_directory(path.parent)


def _entry_result(
    entry: Mapping[str, Any],
    *,
    state: str,
    local_recovery: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "entry_id": str(entry["entry_id"]),
        "run_id": str(entry["run_id"]),
        "writer_kind": str(entry["writer_kind"]),
        "transaction_root_ref": copy.deepcopy(entry["transaction_root_ref"]),
        "state": state,
        "local_recovery": copy.deepcopy(dict(local_recovery))
        if local_recovery is not None
        else None,
    }


def _barrier_layout(
    story_project_root: str | Path,
) -> tuple[Path, Path, SafePathResolver]:
    story_root = assert_safe_local_tree(story_project_root)
    if not story_root.is_dir():
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_story_root_invalid: StoryProject root is not a directory"
        )
    story_root = story_root.resolve()
    root_uuid = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"novelagent:event-authority-barrier:{_normalized_path(story_root)}",
        )
    )
    resolver = SafePathResolver(
        {"story_project": RootBinding("story_project", root_uuid, story_root)}
    )
    sentinel = PathRef(
        root_id="story_project",
        root_uuid=root_uuid,
        relative_path=f"{_GLOBAL_HOME_RELATIVE}/.lock-sentinel",
    )
    resolver.ensure_parent(sentinel)
    home = resolver.resolve(sentinel).path.parent
    assert_safe_local_tree(home)
    for state in (
        "pending",
        "recovery_required",
        "completed",
        "rolled_back",
        "abandoned",
    ):
        _registry_dir(home, resolver, state)
    return story_root, home, resolver


def _dependency_fence_root(resolver: SafePathResolver) -> Path:
    binding = resolver.bindings["story_project"]
    sentinel = PathRef(
        root_id="story_project",
        root_uuid=binding.root_uuid,
        relative_path=f"{_DEPENDENCY_FENCE_RELATIVE}/.lock-sentinel",
    )
    resolver.ensure_parent(sentinel)
    root = resolver.resolve(sentinel).path.parent
    return assert_safe_local_tree(root)


def _registry_dir(home: Path, resolver: SafePathResolver, state: str) -> Path:
    probe = _entry_path(home, resolver, state=state, entry_id="0" * 32)
    return probe.parent


def _entry_path(
    home: Path,
    resolver: SafePathResolver,
    *,
    state: str,
    entry_id: str,
) -> Path:
    if state not in _STATE_DIRECTORIES:
        raise EventAuthorityPersistenceBarrierError(
            f"event_authority_barrier_registry_state_invalid: {state}"
        )
    if not entry_id or any(character not in "0123456789abcdef-" for character in entry_id):
        raise EventAuthorityPersistenceBarrierError(
            f"event_authority_barrier_entry_id_invalid: {entry_id!r}"
        )
    relative = home.relative_to(resolver.bindings["story_project"].path).as_posix()
    ref = PathRef(
        root_id="story_project",
        root_uuid=resolver.bindings["story_project"].root_uuid,
        relative_path=f"{relative}/r/{_STATE_DIRECTORIES[state]}/{entry_id}.json",
    )
    resolver.ensure_parent(ref)
    return resolver.resolve(ref).path


def _transaction_root_ref(
    *,
    home: Path,
    story_project_root: Path,
    transaction_root: str | Path,
    writer_kind: str,
) -> tuple[PathRef, dict[str, Any]]:
    transaction_path = assert_safe_local_tree(transaction_root)
    registry_service = RootRegistryService(home)
    if writer_kind not in EVENT_AUTHORITY_WRITER_KINDS:
        raise EventAuthorityPersistenceBarrierError(
            f"event_authority_writer_kind_invalid: {writer_kind!r}"
        )
    root_map: dict[str, Path] = {"story_project": story_project_root}
    try:
        relative = transaction_path.relative_to(story_project_root)
        if not relative.parts:
            raise ValueError("transaction root cannot equal its logical root")
        root_id = "story_project"
        physical_root = story_project_root
    except ValueError:
        physical_root = transaction_path.parent
        root_id = f"external:event-authority-{writer_kind}"
        root_map[root_id] = physical_root
    registry = registry_service.ensure(root_map, require_runtime=False)
    ref = path_ref_for(
        transaction_path,
        root_id=root_id,
        root=physical_root,
        root_uuid=str(registry["roots"][root_id]["root_uuid"]),
    )
    return ref, {
        "registry_id": str(registry["registry_id"]),
        "revision": int(registry["revision"]),
        "registry_digest": str(registry["registry_digest"]),
        "story_project_root_uuid": str(
            registry["roots"]["story_project"]["root_uuid"]
        ),
    }


def _resolve_transaction_root_ref(
    *,
    home: Path,
    entry: Mapping[str, Any],
) -> Path:
    registry_service = RootRegistryService(home)
    registry = registry_service.load()
    story_binding = registry.get("roots", {}).get("story_project")
    if (
        entry.get("root_registry_id") != registry.get("registry_id")
        or entry.get("root_registry_revision") != registry.get("revision")
        or entry.get("root_registry_digest") != registry.get("registry_digest")
        or not isinstance(story_binding, Mapping)
        or entry.get("story_project_root_uuid") != story_binding.get("root_uuid")
    ):
        raise EventAuthorityPersistenceBarrierError(
            "event_authority_barrier_root_registry_mismatch: pending entry registry binding changed"
        )
    ref = validate_path_ref(entry.get("transaction_root_ref"))
    # SafePathResolver.bind enforces exact root_uuid equality.  A plain lexical
    # PathRef resolution is intentionally insufficient for recovery pointers.
    resolved = registry_service.resolver(registry).resolve(ref)
    return assert_safe_local_tree(resolved.path)


def _entry_id(transaction_root_ref: PathRef | Mapping[str, Any], run_id: str) -> str:
    ref = validate_path_ref(transaction_root_ref)
    basis = canonical_json_hash(ref.to_dict()) + "\0" + run_id
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _path_identity_sha256(path: str | Path) -> str:
    return hashlib.sha256(_normalized_path(Path(path)).encode("utf-8")).hexdigest()


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def _same_path(left: Path, right: Path) -> bool:
    return _normalized_path(left) == _normalized_path(right)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "EVENT_AUTHORITY_BARRIER_SCHEMA_VERSION",
    "EVENT_AUTHORITY_WRITER_KINDS",
    "EventAuthorityPersistenceBarrierError",
    "EventAuthorityWriteOperation",
    "event_authority_write_operation",
    "reconcile_event_authority_persistence",
]
