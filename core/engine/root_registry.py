from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.engine.persistence import (
    PersistenceLockError,
    _atomic_create_from_bytes,
    _atomic_replace_from_bytes,
    persistence_run_lock,
)
from core.engine.safe_paths import RootBinding, SafePathResolver, assert_safe_local_tree
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
        self.registry_path = self.transaction_root / "root_registry.json"

    def ensure(self, root_map: Mapping[str, str | Path]) -> dict[str, Any]:
        roots = _validate_physical_roots(root_map)
        self.transaction_root.mkdir(parents=True, exist_ok=True)
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
            if not missing:
                return registry
            _assert_remap_idle(self.transaction_root, active_sessions=())
            updated = copy.deepcopy(registry)
            for root_id in missing:
                updated["roots"][root_id] = _new_binding(root_id, roots[root_id])
            updated["revision"] += 1
            updated["updated_at"] = _utc_now()
            updated["registry_digest"] = _registry_digest(updated)
            _atomic_replace_from_bytes(self.registry_path, _json_bytes(validate_root_registry(updated)))
            return updated

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
        registry["registry_digest"] = _registry_digest(registry)
        payload = _json_bytes(validate_root_registry(registry))
        try:
            _atomic_create_from_bytes(self.registry_path, payload)
            return registry
        except FileExistsError:
            # A concurrent creator won. Its bindings still have to match; do
            # not silently adopt a different physical root.
            return self.ensure(roots)

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
        with _runtime_remap_fence(runtime_fence):
            with persistence_run_lock(self.transaction_root):
                _assert_remap_idle(self.transaction_root, active_sessions=active_sessions)
                registry = self.load()
                if registry["revision"] != expected_revision:
                    raise RootRegistryCasError(
                        f"root registry revision changed: expected={expected_revision} actual={registry['revision']}"
                    )
                if registry["registry_digest"] != expected_registry_digest:
                    raise RootRegistryCasError("root registry digest changed")
                unknown = sorted(set(remaps) - set(registry["roots"]))
                if unknown:
                    raise RootRegistryError("unknown logical roots: " + ", ".join(unknown))
                validated = _validate_physical_roots(remaps, require_runtime=False)
                updated = copy.deepcopy(registry)
                for root_id, path in validated.items():
                    binding = updated["roots"][root_id]
                    binding["path"] = str(path)
                    binding["path_identity_sha256"] = _path_digest(path)
                    binding["updated_at"] = _utc_now()
                updated["revision"] += 1
                updated["updated_at"] = _utc_now()
                updated["registry_digest"] = _registry_digest(updated)
                _atomic_replace_from_bytes(
                    self.registry_path,
                    _json_bytes(validate_root_registry(updated)),
                )
                return updated

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
        # The UUID is irrelevant for this safety pass.
        temporary[root_id] = RootBinding(root_id, str(uuid.uuid4()), path)
        roots[root_id] = path
    SafePathResolver(temporary)
    return roots


def _new_binding(root_id: str, path: Path) -> dict[str, Any]:
    now = _utc_now()
    return {
        "root_id": root_id,
        "root_uuid": str(uuid.uuid4()),
        "path": str(path),
        "path_identity_sha256": _path_digest(path),
        "created_at": now,
        "updated_at": now,
    }


def _assert_remap_idle(transaction_root: Path, *, active_sessions: Iterable[str]) -> None:
    explicit_sessions = [str(item) for item in active_sessions if str(item)]
    pending = list((transaction_root / "registry" / "pending").glob("*.json"))
    recovery_required = list(
        (transaction_root / "registry" / "recovery_required").glob("*.json")
    )
    staging = list((transaction_root / "staging").glob("*"))
    orphan_journals = []
    journals = transaction_root / "journals"
    if journals.exists():
        terminal_ids = set()
        for state in ("completed", "rolled_back"):
            terminal_ids.update(path.stem for path in (transaction_root / "registry" / state).glob("*.json"))
        orphan_journals = [path for path in journals.iterdir() if path.is_dir() and path.name not in terminal_ids]
    if pending or recovery_required or staging or orphan_journals:
        raise RootRemapBlockedError("remap-roots is blocked by a pending persistence transaction")
    active_or_invalid_sessions = _active_or_invalid_autonomy_sessions(
        transaction_root.parent / "autonomy"
    )
    if explicit_sessions or active_or_invalid_sessions:
        raise RootRemapBlockedError("remap-roots is blocked by an active session")


def _active_or_invalid_autonomy_sessions(autonomy_root: Path) -> list[str]:
    sessions_root = autonomy_root / "sessions"
    blocked: list[str] = []
    try:
        from core.autonomy.session import AutonomySessionStore

        store = AutonomySessionStore(autonomy_root)
        if sessions_root.exists():
            for directory in sorted(sessions_root.iterdir(), key=lambda item: item.name):
                if not directory.is_dir():
                    continue
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
        leases_root = autonomy_root / "leases"
        if leases_root.exists():
            from core.autonomy.common import load_json_object, now_utc, parse_utc
            from core.autonomy.lease import validate_book_lease

            now = parse_utc(now_utc())
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
                    if lease.get("status") == "active" and parse_utc(
                        lease["expires_at"]
                    ) > now:
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


def _registry_digest(payload: Mapping[str, Any]) -> str:
    content = dict(payload)
    content.pop("registry_digest", None)
    return canonical_json_hash(content)


def _path_digest(path: str | Path) -> str:
    return hashlib.sha256(_canonical_path(path).encode("utf-8")).hexdigest()


def _canonical_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).absolute()))


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
    "load_root_registry",
    "remap_roots",
    "root_registry_manifest_binding",
    "validate_registry_manifest_binding",
    "validate_root_registry",
]
