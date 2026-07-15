from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.engine.persistence import (
    PersistenceLockError,
    _atomic_create_from_bytes,
    _atomic_replace_from_bytes,
    _journal_child,
    _load_manifest,
    _validate_commit_marker,
    persistence_run_lock,
)
from core.engine.safe_paths import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    RootBinding,
    SafePathResolver,
    assert_safe_local_tree,
)
from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


ROOT_REGISTRY_SCHEMA_VERSION = "1.0"
_ROOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")


class RootRegistryError(RuntimeError):
    pass


class RootRegistryCasError(RootRegistryError):
    pass


class RootRemapBlockedError(RootRegistryError):
    pass


class RootRemapRequiredError(RootRegistryError):
    pass


class RootRegistryService:
    """Stable logical-root registry.

    A remap changes only this registry. It never moves or copies project data,
    and it is serialized with persistence transactions by the same run lock.
    """

    def __init__(self, transaction_root: str | Path) -> None:
        self.transaction_root = assert_safe_local_tree(transaction_root)
        _assert_root_is_narrow(self.transaction_root)
        self.registry_path = self.transaction_root / "root_registry.json"

    def ensure(
        self,
        root_map: Mapping[str, str | Path],
        *,
        require_runtime: bool = True,
    ) -> dict[str, Any]:
        roots = _validate_physical_roots(
            root_map, require_runtime=require_runtime
        )
        self.transaction_root.mkdir(parents=True, exist_ok=True)
        _arm_persistence_lock(self.transaction_root)
        fence_attestation = _arm_main_project_fence(self.transaction_root, roots)
        if self.registry_path.exists():
            registry = load_root_registry(self.registry_path)
            missing = sorted(set(roots) - set(registry["roots"]))
            mismatched = [
                root_id
                for root_id, path in roots.items()
                if root_id in registry["roots"]
                and _canonical_path(registry["roots"][root_id]["path"]) != _canonical_path(path)
            ]
            if mismatched:
                raise RootRemapRequiredError(
                    "physical roots changed; use explicit remap-roots: " + ", ".join(mismatched)
                )
            unarmed = sorted(
                root_id
                for root_id, path in roots.items()
                if root_id in registry["roots"]
                and "directory_identity" not in registry["roots"][root_id]
                and os.path.lexists(path)
            )
            fence_needs_binding = False
            if fence_attestation is not None:
                current_attestation = registry["roots"]["runtime"].get(
                    "project_remap_fence"
                )
                if current_attestation is None:
                    fence_needs_binding = True
                elif current_attestation != fence_attestation:
                    raise RootRegistryError(
                        "project remap fence identity changed; refusing to re-arm a replaced fence"
                    )
            if not missing and not unarmed and not fence_needs_binding:
                return registry
            _assert_pending_persistence_idle(self.transaction_root)
            updated = copy.deepcopy(registry)
            for root_id in missing:
                updated["roots"][root_id] = _new_binding(root_id, roots[root_id])
            for root_id in unarmed:
                updated["roots"][root_id]["directory_identity"] = directory_identity(
                    roots[root_id]
                )
                updated["roots"][root_id]["updated_at"] = _utc_now()
            if fence_attestation is not None:
                updated["roots"]["runtime"][
                    "project_remap_fence"
                ] = fence_attestation
                updated["roots"]["runtime"]["updated_at"] = _utc_now()
            updated["revision"] += 1
            updated["updated_at"] = _utc_now()
            updated["registry_digest"] = _registry_digest(updated)
            _atomic_replace_from_bytes(self.registry_path, _json_bytes(validate_root_registry(updated)))
            return updated

        _assert_pending_persistence_idle(self.transaction_root)
        now = _utc_now()
        registry = {
            "schema_version": ROOT_REGISTRY_SCHEMA_VERSION,
            "registry_id": str(uuid.uuid4()),
            "revision": 1,
            "roots": {root_id: _new_binding(root_id, path) for root_id, path in sorted(roots.items())},
            "created_at": now,
            "updated_at": now,
            "registry_digest": "",
        }
        if fence_attestation is not None:
            registry["roots"]["runtime"][
                "project_remap_fence"
            ] = fence_attestation
        registry["registry_digest"] = _registry_digest(registry)
        payload = _json_bytes(validate_root_registry(registry))
        try:
            _atomic_create_from_bytes(self.registry_path, payload)
            return registry
        except FileExistsError:
            # A concurrent creator won. Its bindings still have to match; do
            # not silently adopt a different physical root.
            return self.ensure(roots, require_runtime=require_runtime)

    def load(self) -> dict[str, Any]:
        return load_root_registry(self.registry_path)

    def resolver(self, registry: Mapping[str, Any] | None = None) -> SafePathResolver:
        payload = validate_root_registry(dict(registry)) if registry is not None else self.load()
        return SafePathResolver(
            {
                root_id: RootBinding(
                    root_id=root_id,
                    root_uuid=str(binding["root_uuid"]),
                    path=Path(str(binding["path"])),
                )
                for root_id, binding in payload["roots"].items()
            }
        )

    def remap(
        self,
        remaps: Mapping[str, str | Path],
        *,
        expected_revision: int,
        expected_registry_digest: str,
        active_sessions: Iterable[str] = (),
    ) -> dict[str, Any]:
        if not remaps:
            raise RootRegistryError("at least one root remap is required")
        runtime_fence = self.transaction_root.parent / ".root-remap-fence"
        # Fixed cross-subsystem order: runtime remap fence, then persistence
        # root. Autonomy session transitions take the same outer fence before
        # their own state locks, closing the scan->replace race.
        try:
            with ExitStack() as locks:
                if _is_event_authority_home(self.transaction_root):
                    # Event-authority writers own the EA-global lock before the
                    # dependency fence.  Match that order so remap cannot hold
                    # the dependency fence while waiting for EA global.
                    locks.enter_context(persistence_run_lock(self.transaction_root))
                    locks.enter_context(_runtime_remap_fence(runtime_fence))
                else:
                    locks.enter_context(_runtime_remap_fence(runtime_fence))
                    locks.enter_context(persistence_run_lock(self.transaction_root))
                _assert_remap_idle(self.transaction_root, active_sessions=active_sessions)
                registry = self.load()
                story_binding = registry.get("roots", {}).get("story_project")
                if isinstance(story_binding, Mapping) and _is_path_beneath(
                    self.transaction_root,
                    Path(str(story_binding.get("path") or "")).absolute(),
                ):
                    raise RootRegistryError(
                        "an embedded StoryProject root registry may only be changed by "
                        "the project-level remap-roots orchestrator"
                    )
                if registry["revision"] != expected_revision:
                    raise RootRegistryCasError(
                        f"root registry revision changed: expected={expected_revision} actual={registry['revision']}"
                    )
                if registry["registry_digest"] != expected_registry_digest:
                    raise RootRegistryCasError("root registry digest changed")
                unknown = sorted(set(remaps) - set(registry["roots"]))
                if unknown:
                    raise RootRegistryError("unknown logical roots: " + ", ".join(unknown))
                if "story_project" in remaps and _canonical_path(
                    registry["roots"]["story_project"]["path"]
                ) != _canonical_path(remaps["story_project"]):
                    raise RootRegistryError(
                        "StoryProject relocation requires the project-level remap-roots "
                        "orchestrator; a single registry cannot prove completion"
                    )
                if "runtime" in remaps:
                    raise RootRegistryError(
                        "runtime root remap is not supported by a single registry; use "
                        "the project-level remap-roots orchestrator"
                    )
                validated = _validate_physical_roots(remaps, require_runtime=False)
                updated = copy.deepcopy(registry)
                for root_id, path in validated.items():
                    binding = updated["roots"][root_id]
                    binding["path"] = str(path)
                    binding["path_identity_sha256"] = _path_digest(path)
                    binding["directory_identity"] = directory_identity(path)
                    binding["updated_at"] = _utc_now()
                updated["revision"] += 1
                updated["updated_at"] = _utc_now()
                updated["registry_digest"] = _registry_digest(updated)
                _atomic_replace_from_bytes(
                    self.registry_path,
                    _json_bytes(validate_root_registry(updated)),
                )
                return updated
        except PersistenceLockError as exc:
            raise RootRemapBlockedError(
                "remap-roots is blocked by an active persistence writer"
            ) from exc

    def remap_roots(
        self,
        remaps: Mapping[str, str | Path],
        *,
        expected_revision: int,
        expected_registry_digest: str,
        active_sessions: Iterable[str] = (),
    ) -> dict[str, Any]:
        return self.remap(
            remaps,
            expected_revision=expected_revision,
            expected_registry_digest=expected_registry_digest,
            active_sessions=active_sessions,
        )


def remap_roots(
    transaction_root: str | Path,
    remaps: Mapping[str, str | Path],
    *,
    expected_revision: int,
    expected_registry_digest: str,
    active_sessions: Iterable[str] = (),
) -> dict[str, Any]:
    """Pure-service entry point for the explicit ``remap-roots`` operation."""

    return RootRegistryService(transaction_root).remap_roots(
        remaps,
        expected_revision=expected_revision,
        expected_registry_digest=expected_registry_digest,
        active_sessions=active_sessions,
    )


def validate_root_registry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RootRegistryError("root registry must be an object")
    try:
        payload = validate_schema(value, "persistence_root_registry.schema.json")
    except SchemaValidationError as exc:
        raise RootRegistryError(str(exc)) from exc
    try:
        uuid.UUID(str(payload["registry_id"]))
    except ValueError as exc:
        raise RootRegistryError("root registry id is invalid") from exc
    if payload["registry_digest"] != _registry_digest(payload):
        raise RootRegistryError("root registry digest mismatch")
    if not isinstance(payload["revision"], int) or isinstance(payload["revision"], bool):
        raise RootRegistryError("root registry revision is invalid")
    for root_id, binding in payload["roots"].items():
        _validate_root_id(root_id)
        if not isinstance(binding, dict) or binding.get("root_id") != root_id:
            raise RootRegistryError(f"root registry binding is invalid: {root_id}")
        try:
            parsed_uuid = uuid.UUID(str(binding["root_uuid"]))
        except (KeyError, ValueError) as exc:
            raise RootRegistryError(f"logical root UUID is invalid: {root_id}") from exc
        if str(parsed_uuid) != binding["root_uuid"]:
            raise RootRegistryError(f"logical root UUID is not canonical: {root_id}")
        path = Path(str(binding.get("path"))).absolute()
        if binding.get("path_identity_sha256") != _path_digest(path):
            raise RootRegistryError(f"logical root path binding is invalid: {root_id}")
        identity = binding.get("directory_identity")
        if identity is not None:
            validate_directory_identity(identity)
        fence = binding.get("project_remap_fence")
        if fence is not None:
            if (
                root_id != "runtime"
                or not isinstance(fence, Mapping)
                or set(fence)
                != {"relative_path", "directory_identity", "lock_identity"}
                or fence.get("relative_path") != ".root-remap-fence"
            ):
                raise RootRegistryError("project remap fence binding is invalid")
            validate_directory_identity(fence.get("directory_identity"))
            _validate_file_identity(fence.get("lock_identity"))
    return payload


def load_root_registry(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RootRegistryError(f"cannot load root registry: {exc}") from exc
    return validate_root_registry(payload)


def root_registry_manifest_binding(registry: Mapping[str, Any]) -> dict[str, Any]:
    payload = validate_root_registry(dict(registry))
    return {
        "registry_id": payload["registry_id"],
        "revision": payload["revision"],
        "registry_digest": payload["registry_digest"],
        "roots": {
            root_id: binding["root_uuid"] for root_id, binding in sorted(payload["roots"].items())
        },
    }


def validate_registry_manifest_binding(
    binding: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    require_same_revision: bool,
) -> None:
    payload = validate_root_registry(dict(registry))
    if binding.get("registry_id") != payload["registry_id"]:
        raise RootRegistryError("root registry identity changed")
    expected_roots = {
        root_id: item["root_uuid"] for root_id, item in sorted(payload["roots"].items())
    }
    bound_roots = binding.get("roots")
    if not isinstance(bound_roots, dict):
        raise RootRegistryError("manifest root UUID binding is invalid")
    for root_id, root_uuid in bound_roots.items():
        if expected_roots.get(root_id) != root_uuid:
            raise RootRegistryError(f"logical root identity changed: {root_id}")
    if require_same_revision and (
        binding.get("revision") != payload["revision"]
        or binding.get("registry_digest") != payload["registry_digest"]
    ):
        raise RootRegistryError("root registry changed while transaction was pending")


def _validate_physical_roots(
    root_map: Mapping[str, str | Path],
    *,
    require_runtime: bool = True,
) -> dict[str, Path]:
    if not isinstance(root_map, Mapping) or (require_runtime and "runtime" not in root_map):
        raise RootRegistryError("root map must include runtime")
    if not root_map:
        raise RootRegistryError("root map must not be empty")
    roots: dict[str, Path] = {}
    temporary: dict[str, RootBinding] = {}
    for root_id, raw_path in root_map.items():
        root_id = str(root_id)
        _validate_root_id(root_id)
        path = Path(str(raw_path)).absolute()
        _assert_root_is_narrow(path)
        # The UUID is irrelevant for this safety pass.
        temporary[root_id] = RootBinding(root_id, str(uuid.uuid4()), path)
        roots[root_id] = path
    SafePathResolver(temporary)
    return roots


def _assert_root_is_narrow(path: Path) -> None:
    """Reject a volume anchor as a writable registry or logical data root.

    The control plane and every registry binding are capability boundaries.
    Placing either at ``/`` or a Windows drive root would therefore expose an
    entire filesystem.  More specific existing directories (including a
    StoryProject that happens to be the current working directory) remain
    valid.
    """

    normalized = os.path.normcase(os.path.normpath(str(path)))
    anchor = os.path.normcase(os.path.normpath(str(Path(path.anchor))))
    if normalized == anchor:
        raise RootRegistryError(
            f"physical root is too broad; filesystem volume roots are forbidden: {path}"
        )


def _new_binding(root_id: str, path: Path) -> dict[str, Any]:
    now = _utc_now()
    return {
        "root_id": root_id,
        "root_uuid": str(uuid.uuid4()),
        "path": str(path),
        "path_identity_sha256": _path_digest(path),
        "directory_identity": directory_identity(path),
        "created_at": now,
        "updated_at": now,
    }


def _arm_main_project_fence(
    transaction_root: Path, roots: Mapping[str, Path]
) -> dict[str, Any] | None:
    """Pre-arm the no-create relocation fence while the old tree is stable."""

    story = roots.get("story_project")
    runtime = roots.get("runtime")
    if story is None or runtime is None:
        return None
    expected_runtime = story / ".novelagent" / "runtime"
    expected_transaction_root = expected_runtime / "persistence"
    if (
        _canonical_path(runtime) != _canonical_path(expected_runtime)
        or _canonical_path(transaction_root) != _canonical_path(expected_transaction_root)
    ):
        return None
    fence = runtime / ".root-remap-fence"
    if os.path.lexists(fence):
        _assert_plain_directory_for_arm(fence)
    else:
        try:
            os.mkdir(fence)
        except FileExistsError:
            # A no-clobber loser must inspect what won before writing a lock.
            pass
        except OSError as exc:
            raise RootRegistryError(f"cannot arm project remap fence: {fence}: {exc}") from exc
        _assert_plain_directory_for_arm(fence)
    lock_identity = _arm_persistence_lock(fence)
    return {
        "relative_path": ".root-remap-fence",
        "directory_identity": directory_identity(fence),
        "lock_identity": lock_identity,
    }


def _arm_persistence_lock(root: Path) -> dict[str, int]:
    _assert_plain_directory_for_arm(root)
    lock = root / ".persistence.lock"
    try:
        _atomic_create_from_bytes(lock, b"\0")
    except FileExistsError:
        pass
    return _plain_file_identity(lock)


def _assert_plain_directory_for_arm(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RootRegistryError(f"cannot inspect persistence lock root: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RootRegistryError(f"unsafe persistence lock root: {path}")


def _plain_file_identity(path: Path) -> dict[str, int]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RootRegistryError(f"required persistence lock is missing: {path}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        or info.st_size < 1
    ):
        raise RootRegistryError(f"unsafe persistence lock file: {path}")
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(stat.S_IFMT(info.st_mode)),
    }


def _validate_file_identity(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"device", "inode", "mode"}:
        raise RootRegistryError("persistence lock file identity is invalid")
    result: dict[str, int] = {}
    for field in ("device", "inode", "mode"):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise RootRegistryError(f"persistence lock file identity {field} is invalid")
        result[field] = item
    if result["inode"] == 0 or not stat.S_ISREG(result["mode"]):
        raise RootRegistryError("persistence lock identity cannot prove a regular file")
    return result


def _assert_remap_idle(transaction_root: Path, *, active_sessions: Iterable[str]) -> None:
    _assert_pending_persistence_idle(transaction_root)
    explicit_sessions = [str(item) for item in active_sessions if str(item)]
    active_or_invalid_sessions = _active_or_invalid_autonomy_sessions(
        transaction_root.parent / "autonomy"
    )
    if explicit_sessions or active_or_invalid_sessions:
        raise RootRemapBlockedError("remap-roots is blocked by an active session")


def _assert_pending_persistence_idle(transaction_root: Path) -> None:
    pending = list((transaction_root / "registry" / "pending").glob("*.json"))
    recovery_required = list(
        (transaction_root / "registry" / "recovery_required").glob("*.json")
    )
    # StoryProject-global event-authority entries intentionally live outside
    # PersistenceV2's local registry.  They bind transaction roots from several
    # physical locations, so moving one mapping while either state is present
    # could redirect recovery to an empty tree and falsely abandon a marked run.
    authority_pending = list((transaction_root / "r" / "p").glob("*.json"))
    authority_recovery_required = list(
        (transaction_root / "r" / "x").glob("*.json")
    )
    staging = list((transaction_root / "staging").glob("*"))
    orphan_journals = []
    journals = transaction_root / "journals"
    if journals.exists():
        terminal_ids = set()
        for state in ("completed", "rolled_back"):
            terminal_ids.update(path.stem for path in (transaction_root / "registry" / state).glob("*.json"))
        orphan_journals = [path for path in journals.iterdir() if path.is_dir() and path.name not in terminal_ids]
    legacy_pending: list[Path] = []
    # v1 journals are direct children of ``persistence_dir`` (for example
    # ``chapter_10_<timestamp>``), not ``runtime/runs/transactions``.  A v2
    # registry may coexist with those historical journals during upgrade, so
    # every unknown direct child must be classified before a root rebind.
    known_control_directories = {"abandoned", "journals", "r", "registry", "staging"}
    if transaction_root.exists():
        for child in transaction_root.iterdir():
            try:
                child_info = os.lstat(child)
            except OSError:
                legacy_pending.append(child)
                continue
            if (
                stat.S_ISLNK(child_info.st_mode)
                or getattr(child_info, "st_file_attributes", 0)
                & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                legacy_pending.append(child)
                continue
            if stat.S_ISREG(child_info.st_mode):
                continue
            if not stat.S_ISDIR(child_info.st_mode):
                legacy_pending.append(child)
                continue
            if child.name in known_control_directories:
                continue
            try:
                _assert_plain_directory_for_arm(child)
                _plain_file_identity(child / "manifest.json")
                manifest = _load_manifest(child)
                state = str(manifest.get("state") or "")
                marker_relative = manifest.get("commit_marker") or "commit.marker"
                if not isinstance(marker_relative, str):
                    raise RootRegistryError("legacy commit marker path is invalid")
                marker = _journal_child(child, marker_relative)
                if state == "completed":
                    _plain_file_identity(marker)
                    if _validate_commit_marker(
                        marker,
                        str(manifest.get("run_id") or child.name),
                        manifest.get("candidate_sha256"),
                    ) is not None:
                        legacy_pending.append(child)
                elif state == "rolled_back":
                    if os.path.lexists(marker):
                        legacy_pending.append(child)
                else:
                    legacy_pending.append(child)
            except Exception:
                legacy_pending.append(child)
    if (
        pending
        or recovery_required
        or authority_pending
        or authority_recovery_required
        or staging
        or orphan_journals
        or legacy_pending
    ):
        raise RootRemapBlockedError("remap-roots is blocked by a pending persistence transaction")


def _active_or_invalid_autonomy_sessions(autonomy_root: Path) -> list[str]:
    sessions_root = autonomy_root / "sessions"
    leases_root = autonomy_root / "leases"
    operations_root = autonomy_root / "operations"
    if (
        not sessions_root.exists()
        and not leases_root.exists()
        and not operations_root.exists()
    ):
        return []
    blocked: list[str] = []
    try:
        from core.autonomy.session import AutonomySessionStore

        store = AutonomySessionStore(autonomy_root, reconcile_on_open=False)
        if operations_root.exists():
            for entry in sorted(operations_root.iterdir(), key=lambda item: item.name):
                if not entry.is_dir():
                    blocked.append(f"invalid:operation-entry:{entry.name}")
        for operation in store.operations.pending():
            blocked.append(f"operation:{operation['operation_id']}")
        if sessions_root.exists():
            session_directories: list[Path] = []
            for directory in sorted(sessions_root.iterdir(), key=lambda item: item.name):
                if not directory.is_dir():
                    if directory.name != "latest.json" or not directory.is_file():
                        blocked.append(f"invalid:session-entry:{directory.name}")
                    continue
                session_directories.append(directory)
                try:
                    genesis = store._load_genesis(directory.name)
                    events = store._load_events(directory.name)
                except Exception:
                    blocked.append(f"invalid:{directory.name}")
                    continue
                if events[-1].get("event_type") in {"started", "resumed"}:
                    blocked.append(directory.name)
                if genesis.get("session_id") != directory.name:
                    blocked.append(f"invalid:{directory.name}")
            if session_directories:
                latest = sessions_root / "latest.json"
                if not latest.is_file():
                    blocked.append("invalid:latest-session-missing")
                else:
                    store.resolve_session_id("latest")
        if leases_root.exists():
            from core.autonomy.common import load_json_object
            from core.autonomy.lease import validate_book_lease

            for lease_directory in sorted(leases_root.iterdir(), key=lambda item: item.name):
                if not lease_directory.is_dir():
                    blocked.append(f"invalid:lease-entry:{lease_directory.name}")
                    continue
                current = lease_directory / "current.json"
                if not current.is_file():
                    blocked.append(f"invalid:lease-orphan:{lease_directory.name}")
                    continue
                try:
                    lease = validate_book_lease(load_json_object(current))
                    expected_current = store.leases._current_path(lease["book_id"])
                    if current.resolve() != expected_current.resolve():
                        raise ValueError("lease current.json is stored under the wrong book key")
                    durable = store.leases.load(lease["book_id"])
                    historical = store.leases.load_history(
                        lease["book_id"], lease["lease_hash"]
                    )
                    if durable != lease or historical != lease:
                        raise ValueError("lease current.json does not match its durable history")
                    # Expiry alone does not release a durable writer identity.
                    # A stale active lease can still be taken over/resumed, so
                    # project relocation requires an explicit terminal lease.
                    if lease.get("status") == "active":
                        blocked.append(f"lease:{lease['session_id']}")
                except Exception:
                    blocked.append(f"invalid:{current.parent.name}")
    except Exception:
        # A malformed or unreadable autonomy store must fail closed during a
        # root remap; otherwise a later resume could target a different tree.
        blocked.append("invalid:autonomy-store")
    return blocked


@contextmanager
def _runtime_remap_fence(path: Path):
    try:
        with persistence_run_lock(path):
            yield
    except PersistenceLockError as exc:
        raise RootRemapBlockedError(
            "remap-roots is blocked by an autonomy session transition"
        ) from exc


def _is_event_authority_home(path: Path) -> bool:
    return path.name == "ea" and path.parent.name == "runtime" and (path / "r").is_dir()


def _registry_digest(payload: Mapping[str, Any]) -> str:
    content = dict(payload)
    content.pop("registry_digest", None)
    return canonical_json_hash(content)


def directory_identity(path: str | Path) -> dict[str, int]:
    """Return the stable directory identity required for rename-only remaps."""

    candidate = assert_safe_local_tree(path)
    if not candidate.is_dir():
        raise RootRegistryError(f"logical root is not an existing directory: {candidate}")
    info = os.lstat(candidate)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RootRegistryError(
            f"logical root final component is a link or reparse point: {candidate}"
        )
    identity = {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(stat.S_IFMT(info.st_mode)),
    }
    return validate_directory_identity(identity)


def validate_directory_identity(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise RootRegistryError("logical root directory identity is invalid")
    if set(value) != {"device", "inode", "mode"}:
        raise RootRegistryError("logical root directory identity fields are invalid")
    result: dict[str, int] = {}
    for field in ("device", "inode", "mode"):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise RootRegistryError(f"logical root directory identity {field} is invalid")
        result[field] = item
    if result["inode"] == 0 or not stat.S_ISDIR(result["mode"]):
        raise RootRegistryError("logical root directory identity cannot prove a directory rename")
    return result


def _path_digest(path: str | Path) -> str:
    return hashlib.sha256(_canonical_path(path).encode("utf-8")).hexdigest()


def _canonical_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).absolute()))


def _is_path_beneath(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_canonical_path(path), _canonical_path(root))) == _canonical_path(
            root
        )
    except ValueError:
        return False


def _validate_root_id(value: str) -> None:
    if not _ROOT_ID_PATTERN.fullmatch(value):
        raise RootRegistryError(f"logical root id is invalid: {value!r}")


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ROOT_REGISTRY_SCHEMA_VERSION",
    "RootRegistryCasError",
    "RootRegistryError",
    "RootRegistryService",
    "RootRemapBlockedError",
    "RootRemapRequiredError",
    "directory_identity",
    "load_root_registry",
    "remap_roots",
    "root_registry_manifest_binding",
    "validate_registry_manifest_binding",
    "validate_directory_identity",
    "validate_root_registry",
]
