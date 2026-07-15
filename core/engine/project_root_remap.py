from __future__ import annotations

"""Crash-recoverable, whole-StoryProject root relocation.

The operator moves the complete directory tree.  This module only rebinds the
embedded mutable root registries after proving that the tree was renamed on the
same volume.  A copied tree, a deleted-and-recreated tree, an incomplete
control-plane inventory, or any active writer fails closed.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.engine.persistence import (
    PersistenceLockError,
    _atomic_create_from_bytes,
    _atomic_replace_from_bytes,
    _fsync_directory,
    _journal_child,
    _load_manifest,
    _validate_commit_marker,
    persistence_run_lock,
)
from core.engine.root_registry import (
    RootRegistryCasError,
    RootRegistryError,
    RootRemapBlockedError,
    _active_or_invalid_autonomy_sessions,
    _assert_pending_persistence_idle,
    _canonical_path,
    _json_bytes,
    _path_digest,
    _registry_digest,
    directory_identity,
    load_root_registry,
    validate_directory_identity,
    validate_root_registry,
)
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.identity import load_project_identity


PROJECT_ROOT_REMAP_SCHEMA_VERSION = "1.0"
_MAIN_REGISTRY = Path(".novelagent/runtime/persistence/root_registry.json")
_EA_REGISTRY = Path(".novelagent/runtime/ea/root_registry.json")
_MIGRATION_REGISTRY = Path(".novelagent/migration-v2/tx/root_registry.json")
_HISTORY_REGISTRY = Path(
    ".novelagent/runtime/memory/v2/history_revision_execution/persistence/root_registry.json"
)
_ALLOWED_REGISTRIES = frozenset(
    {_MAIN_REGISTRY, _EA_REGISTRY, _MIGRATION_REGISTRY, _HISTORY_REGISTRY}
)
_ALLOWED_REGISTRY_TEXTS = frozenset(path.as_posix() for path in _ALLOWED_REGISTRIES)
_MAIN_REGISTRY_TEXT = _MAIN_REGISTRY.as_posix()
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_ID_PATTERN = re.compile(r"^root-remap-[0-9a-f]{24}$")
_JOURNAL_RELATIVE = Path(".novelagent/root-remap")
_FaultInjector = Callable[[str, int | None, Path | None], None]


def remap_story_project_roots(
    *,
    new_story_project: str | Path,
    control_plane: str | Path,
    requested: Mapping[str, Mapping[str, Any]],
    expected_revision: int,
    expected_registry_digest: str,
    fault_injector: _FaultInjector | None = None,
) -> dict[str, Any]:
    """Rebind every embedded mutable registry as one forward-only operation."""

    new_story = _existing_directory(new_story_project, "new StoryProject")
    main_root = _existing_directory(control_plane, "main persistence control plane")
    expected_main = new_story / _MAIN_REGISTRY.parent
    if _canonical_path(main_root) != _canonical_path(expected_main):
        raise RootRegistryError(
            "whole-StoryProject relocation requires the canonical main control plane "
            f"{expected_main}; rogue or nested control planes are forbidden"
        )
    normalized_request = _normalize_request(requested)
    story_request = normalized_request.get("story_project")
    if story_request is None or _canonical_path(story_request["path"]) != _canonical_path(
        new_story
    ):
        raise RootRegistryError(
            "whole-StoryProject relocation requires a story_project binding to the new root"
        )

    initial_inventory = _discover_registry_paths(new_story)
    ea_home = new_story / _EA_REGISTRY.parent
    dependency_fence = new_story / ".novelagent" / "runtime" / ".root-remap-fence"
    journal_home = new_story / _JOURNAL_RELATIVE
    legacy_run_root = new_story / ".novelagent" / "runtime" / "runs"
    main_registry = load_root_registry(new_story / _MAIN_REGISTRY)
    initial_story_binding = main_registry.get("roots", {}).get("story_project")
    if isinstance(initial_story_binding, Mapping):
        recorded_old_story = Path(str(initial_story_binding.get("path"))).absolute()
        if (
            _canonical_path(recorded_old_story) != _canonical_path(new_story)
            and os.path.lexists(recorded_old_story)
        ):
            raise RootRegistryError(
                "whole-StoryProject relocation rejected: the old StoryProject path still exists"
            )
    fence_attestation = _project_fence_attestation(
        main_registry, dependency_fence=dependency_fence
    )
    local_root_set = {
        path.parent for path in initial_inventory if path != new_story / _EA_REGISTRY
    }
    if legacy_run_root.is_dir():
        local_root_set.add(legacy_run_root)
    local_roots = sorted(local_root_set, key=_canonical_path)

    try:
        with ExitStack() as locks:
            # Same order as authority/history writers: EA global, dependency
            # fence, then narrower local transaction roots.  The pre-armed
            # dependency fence is the unique project-remap serializer; journal
            # directories are created beneath it without a second lock.
            # Do not materialize a previously absent EA control plane merely
            # to lock it.  The shared dependency fence prevents a new EA
            # writer from passing its normal EA->fence sequence; an EA home
            # that already exists is locked first in writer order.
            ea_identity = None
            if ea_home.exists():
                ea_identity = directory_identity(ea_home)
                ea_lock_identity = _require_armed_lock_file(ea_home)
                locks.enter_context(
                    persistence_run_lock(
                        ea_home,
                        require_existing_root=True,
                        require_existing_lock=True,
                    )
                )
                if (
                    directory_identity(ea_home) != ea_identity
                    or _require_armed_lock_file(ea_home) != ea_lock_identity
                ):
                    raise RootRegistryError(
                        "EA lock root identity changed while acquiring project locks"
                    )
            _assert_project_fence_attestation(
                dependency_fence, fence_attestation
            )
            locks.enter_context(
                persistence_run_lock(
                    dependency_fence,
                    require_existing_root=True,
                    require_existing_lock=True,
                )
            )
            _assert_project_fence_attestation(
                dependency_fence, fence_attestation
            )
            local_lock_attestations: list[
                tuple[Path, dict[str, int], dict[str, int]]
            ] = []
            for root in local_roots:
                root_identity = directory_identity(root)
                root_lock_identity = _require_armed_lock_file(root)
                locks.enter_context(
                    persistence_run_lock(
                        root,
                        require_existing_root=True,
                        require_existing_lock=True,
                    )
                )
                if (
                    directory_identity(root) != root_identity
                    or _require_armed_lock_file(root) != root_lock_identity
                ):
                    raise RootRegistryError(
                        f"local lock root identity changed while acquiring project locks: {root}"
                    )
                local_lock_attestations.append(
                    (root, root_identity, root_lock_identity)
                )

            inventory = _discover_registry_paths(new_story)
            if inventory != initial_inventory:
                raise RootRegistryError(
                    "embedded root-registry inventory changed while acquiring project locks"
                )
            _assert_project_fence_attestation(
                dependency_fence, fence_attestation
            )
            if ea_identity is not None and (
                directory_identity(ea_home) != ea_identity
                or _require_armed_lock_file(ea_home) != ea_lock_identity
            ):
                raise RootRegistryError(
                    "EA lock root identity changed after acquiring project locks"
                )
            for root, root_identity, root_lock_identity in local_lock_attestations:
                if (
                    directory_identity(root) != root_identity
                    or _require_armed_lock_file(root) != root_lock_identity
                ):
                    raise RootRegistryError(
                        f"local lock root identity changed after acquiring project locks: {root}"
                    )
            # This is the first remap write.  It happens only after every
            # no-create lock and the complete project inventory are rechecked.
            journal_identity = _ensure_plain_child_directory(
                journal_home.parent, journal_home
            )
            if directory_identity(journal_home) != journal_identity:
                raise RootRegistryError(
                    "project remap journal root identity changed while acquiring locks"
                )
            _assert_project_idle(new_story, inventory)
            transaction = _load_candidate_transaction(
                journal_home,
                new_story=new_story,
                requested=normalized_request,
                expected_revision=expected_revision,
                expected_registry_digest=expected_registry_digest,
            )
            if transaction is None:
                transaction = _prepare_transaction(
                    new_story=new_story,
                    inventory=inventory,
                    journal_home=journal_home,
                    requested=normalized_request,
                    expected_revision=expected_revision,
                    expected_registry_digest=expected_registry_digest,
                )
                _inject(
                    fault_injector,
                    "after_project_remap_intent_publish",
                    None,
                    transaction["directory"],
                )
            else:
                _assert_resume_request(
                    transaction["intent"],
                    new_story=new_story,
                    requested=normalized_request,
                    expected_revision=expected_revision,
                    expected_registry_digest=expected_registry_digest,
                )

            return _commit_or_recover(
                new_story=new_story,
                inventory=inventory,
                transaction=transaction,
                fault_injector=fault_injector,
            )
    except PersistenceLockError as exc:
        raise RootRemapBlockedError(
            "whole-StoryProject remap is blocked by an active writer"
        ) from exc


def _prepare_transaction(
    *,
    new_story: Path,
    inventory: tuple[Path, ...],
    journal_home: Path,
    requested: Mapping[str, Mapping[str, Any]],
    expected_revision: int,
    expected_registry_digest: str,
) -> dict[str, Any]:
    main_path = new_story / _MAIN_REGISTRY
    main = load_root_registry(main_path)
    if main["revision"] != expected_revision:
        raise RootRegistryCasError(
            "main root registry revision changed: "
            f"expected={expected_revision} actual={main['revision']}"
        )
    if main["registry_digest"] != expected_registry_digest:
        raise RootRegistryCasError("main root registry digest changed")
    story_binding = main["roots"].get("story_project")
    if not isinstance(story_binding, Mapping):
        raise RootRegistryError("main root registry has no story_project binding")
    old_story = Path(str(story_binding["path"])).absolute()
    if _canonical_path(old_story) == _canonical_path(new_story):
        raise RootRegistryError("StoryProject root is already bound to this path")
    if os.path.lexists(old_story):
        raise RootRegistryError(
            "whole-StoryProject relocation rejected: the old StoryProject path still exists"
        )
    _assert_identity_preserved(
        story_binding,
        new_story,
        label="StoryProject",
    )

    identity = load_project_identity(new_story)
    if identity is None or identity.ephemeral:
        raise RootRegistryError(
            "whole-StoryProject relocation requires a stable ProjectIdentity"
        )
    identity_path = new_story / ".novelagent" / "project.json"
    identity_sha256 = _file_sha256(identity_path)

    entries: list[dict[str, Any]] = []
    staged: list[bytes] = []
    registry_ids: set[str] = set()
    for registry_path in _application_order(new_story, inventory):
        before = load_root_registry(registry_path)
        if registry_path == new_story / _EA_REGISTRY and any(
            str(root_id).startswith("external:event-authority-")
            for root_id in before["roots"]
        ):
            raise RootRegistryError(
                "whole-StoryProject relocation cannot claim completion while the EA "
                "registry references external mutable transaction roots"
            )
        if before["registry_id"] in registry_ids:
            raise RootRegistryError(
                "duplicate embedded root-registry identity indicates a copied control plane"
            )
        registry_ids.add(before["registry_id"])
        registry_story = before["roots"].get("story_project")
        if not isinstance(registry_story, Mapping) or _canonical_path(
            registry_story.get("path")
        ) != _canonical_path(old_story):
            raise RootRegistryError(
                f"embedded root registry is not bound to the same old StoryProject: {registry_path}"
            )
        _assert_identity_preserved(
            registry_story,
            new_story,
            label=f"StoryProject binding in {registry_path}",
        )
        after = _remapped_registry(before, old_story=old_story, new_story=new_story)
        relative = registry_path.relative_to(new_story).as_posix()
        before_bytes = registry_path.read_bytes()
        after_bytes = _json_bytes(after)
        root_bindings = {
            root_id: {
                "root_uuid": binding["root_uuid"],
                "before_path": str(Path(str(binding["path"])).absolute()),
                "after_path": str(
                    Path(str(after["roots"][root_id]["path"])).absolute()
                ),
                "directory_identity": validate_directory_identity(
                    binding.get("directory_identity")
                ),
            }
            for root_id, binding in sorted(before["roots"].items())
        }
        entry = {
            "index": len(entries),
            "relative_path": relative,
            "registry_id": before["registry_id"],
            "before_revision": before["revision"],
            "before_registry_digest": before["registry_digest"],
            "before_sha256": _sha256(before_bytes),
            "after_revision": after["revision"],
            "after_registry_digest": after["registry_digest"],
            "after_sha256": _sha256(after_bytes),
            "root_uuids": {
                root_id: binding["root_uuid"]
                for root_id, binding in sorted(before["roots"].items())
            },
            "root_bindings": root_bindings,
            "control_plane_identity": directory_identity(registry_path.parent),
            "control_lock_identity": _require_armed_lock_file(
                registry_path.parent
            ),
        }
        _validate_registry_against_entry(before, entry, state="before")
        _validate_registry_against_entry(after, entry, state="after")
        _assert_entry_physical_targets(entry)
        entries.append(entry)
        staged.append(after_bytes)

    _assert_main_request(
        main,
        requested=requested,
        old_story=old_story,
        new_story=new_story,
    )
    created_at = _utc_now()
    basis = {
        "book_id": identity.book_id,
        "old_story_project": str(old_story),
        "new_story_project": str(new_story),
        "story_directory_identity": directory_identity(new_story),
        "main_registry_id": main["registry_id"],
        "main_expected_revision": expected_revision,
        "main_expected_registry_digest": expected_registry_digest,
        "registry_inventory": [entry["relative_path"] for entry in entries],
    }
    transaction_id = "root-remap-" + canonical_json_hash(basis)[:24]
    intent = {
        "schema_version": PROJECT_ROOT_REMAP_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        **basis,
        "project_identity_sha256": identity_sha256,
        "requested": {
            root_id: {
                "root_uuid": value["root_uuid"],
                "path": str(value["path"]),
            }
            for root_id, value in sorted(requested.items())
        },
        "registries": entries,
        "created_at": created_at,
    }
    intent["intent_hash"] = canonical_json_hash(intent)

    transactions = journal_home / "transactions"
    _ensure_plain_child_directory(journal_home, transactions)
    final = transactions / transaction_id
    if final.exists():
        existing = _load_transaction(final)
        if existing["intent"] != intent:
            raise RootRegistryError("project remap transaction id collision")
        return existing
    # ``tempfile.mkdtemp`` applies a private DACL on recent Windows Python
    # builds that can make the directory inaccessible to child operations in
    # managed runners.  A UUID name under the already-locked private journal
    # root has the same collision property without changing inheritance.
    temporary = transactions / f".prepare-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        (temporary / "after").mkdir()
        (temporary / "progress").mkdir()
        _atomic_create_from_bytes(temporary / "intent.json", _json_bytes(intent))
        for entry, content in zip(entries, staged, strict=True):
            _atomic_create_from_bytes(
                temporary / "after" / f"{int(entry['index']):03d}.json", content
            )
        _fsync_directory(temporary / "after")
        _fsync_directory(temporary / "progress")
        _fsync_directory(temporary)
        os.replace(temporary, final)
        _fsync_directory(transactions)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return _load_transaction(final)


def _commit_or_recover(
    *,
    new_story: Path,
    inventory: tuple[Path, ...],
    transaction: Mapping[str, Any],
    fault_injector: _FaultInjector | None,
) -> dict[str, Any]:
    directory = Path(transaction["directory"])
    intent = _validate_intent(transaction["intent"])
    marker = directory / "commit.marker"
    completed = directory / "completed.json"
    _revalidate_project(intent, new_story=new_story, inventory=inventory)
    if completed.exists():
        if not marker.is_file():
            raise RootRegistryError(
                "project-remap completion exists without its commit marker"
            )
        _validate_marker(_load_json(marker), intent)
        completion = _load_json(completed)
        _validate_completion(completion, intent)
        _verify_all_after(intent, new_story=new_story, directory=directory)
        return _report(intent, completion)

    if not marker.exists():
        _verify_all_before(intent, new_story=new_story)
        _inject(fault_injector, "before_project_remap_commit_marker", None, marker)
        _revalidate_project(intent, new_story=new_story, inventory=inventory)
        _verify_all_before(intent, new_story=new_story)
        marker_payload = {
            "schema_version": PROJECT_ROOT_REMAP_SCHEMA_VERSION,
            "transaction_id": intent["transaction_id"],
            "intent_hash": intent["intent_hash"],
            "committed_at": _utc_now(),
        }
        marker_payload["marker_hash"] = canonical_json_hash(marker_payload)
        _atomic_create_from_bytes(marker, _json_bytes(marker_payload))
        _inject(fault_injector, "after_project_remap_commit_marker", None, marker)
    else:
        _validate_marker(_load_json(marker), intent)

    for entry in intent["registries"]:
        index = int(entry["index"])
        path = new_story / Path(entry["relative_path"])
        staged = directory / "after" / f"{index:03d}.json"
        staged_bytes = staged.read_bytes()
        if _sha256(staged_bytes) != entry["after_sha256"]:
            raise RootRegistryError(f"project remap staged registry was modified: {staged}")
        _revalidate_project(intent, new_story=new_story, inventory=inventory)
        current = _file_sha256(path)
        if current == entry["before_sha256"]:
            _inject(fault_injector, "before_project_registry_replace", index, path)
            # The hook models the final TOCTOU window.  Re-run the complete
            # project proof (including a fresh registry discovery) immediately
            # before the atomic replacement, then repeat the exact-file CAS.
            _revalidate_project(intent, new_story=new_story, inventory=inventory)
            if _file_sha256(path) != entry["before_sha256"]:
                raise RootRegistryCasError(
                    f"embedded root registry changed before replace: {path}"
                )
            _atomic_replace_from_bytes(path, staged_bytes)
        elif current != entry["after_sha256"]:
            raise RootRegistryCasError(
                f"embedded root registry drifted during forward recovery: {path}"
            )
        if _file_sha256(path) != entry["after_sha256"]:
            raise RootRegistryError(f"embedded root registry replace was not durable: {path}")
        _assert_registry_control_identity(entry, path)
        _validate_registry_against_entry(
            load_root_registry(path), entry, state="after"
        )
        _assert_entry_physical_targets(entry)
        progress = directory / "progress" / f"{index:03d}.json"
        if not progress.exists():
            payload = {
                "schema_version": PROJECT_ROOT_REMAP_SCHEMA_VERSION,
                "transaction_id": intent["transaction_id"],
                "intent_hash": intent["intent_hash"],
                "index": index,
                "relative_path": entry["relative_path"],
                "after_sha256": entry["after_sha256"],
                "applied_at": _utc_now(),
            }
            payload["progress_hash"] = canonical_json_hash(payload)
            _atomic_create_from_bytes(progress, _json_bytes(payload))
        _inject(fault_injector, "after_project_registry_replace", index, path)

    _revalidate_project(intent, new_story=new_story, inventory=inventory)
    _verify_all_after(intent, new_story=new_story, directory=directory)
    completion = {
        "schema_version": PROJECT_ROOT_REMAP_SCHEMA_VERSION,
        "transaction_id": intent["transaction_id"],
        "intent_hash": intent["intent_hash"],
        "book_id": intent["book_id"],
        "registry_count": len(intent["registries"]),
        "completed_at": _utc_now(),
    }
    completion["completion_hash"] = canonical_json_hash(completion)
    _atomic_create_from_bytes(completed, _json_bytes(completion))
    return _report(intent, completion)


def _remapped_registry(
    before: Mapping[str, Any], *, old_story: Path, new_story: Path
) -> dict[str, Any]:
    after = copy.deepcopy(dict(before))
    timestamp = _utc_now()
    for root_id, binding in after["roots"].items():
        old_path = Path(str(binding["path"])).absolute()
        target = _target_after_story_move(
            old_path, old_story=old_story, new_story=new_story
        )
        _assert_identity_preserved(binding, target, label=f"logical root {root_id}")
        binding["path"] = str(target.absolute())
        binding["path_identity_sha256"] = _path_digest(target)
        binding["directory_identity"] = directory_identity(target)
        binding["updated_at"] = timestamp
    after["revision"] = int(before["revision"]) + 1
    after["updated_at"] = timestamp
    after["registry_digest"] = _registry_digest(after)
    return validate_root_registry(after)


def _target_after_story_move(
    path: Path, *, old_story: Path, new_story: Path
) -> Path:
    relative = _relative_beneath(path, old_story)
    return path if relative is None else new_story / relative


def _validate_registry_against_entry(
    registry: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    state: str,
) -> None:
    if state not in {"before", "after"}:
        raise RootRegistryError("project-remap registry validation state is invalid")
    expected_revision = entry[f"{state}_revision"]
    expected_digest = entry[f"{state}_registry_digest"]
    if (
        registry.get("registry_id") != entry.get("registry_id")
        or registry.get("revision") != expected_revision
        or registry.get("registry_digest") != expected_digest
    ):
        raise RootRegistryError(
            f"project-remap {state} registry identity or CAS binding changed"
        )
    roots = registry.get("roots")
    specifications = entry.get("root_bindings")
    if not isinstance(roots, Mapping) or not isinstance(specifications, Mapping):
        raise RootRegistryError("project-remap logical root bindings are invalid")
    if set(roots) != set(specifications):
        raise RootRegistryError("project-remap logical root inventory changed")
    for root_id, specification in specifications.items():
        binding = roots[root_id]
        expected_path = specification[f"{state}_path"]
        actual_path = str(Path(str(binding.get("path"))).absolute())
        if (
            binding.get("root_uuid") != specification.get("root_uuid")
            or actual_path != expected_path
            or validate_directory_identity(binding.get("directory_identity"))
            != validate_directory_identity(specification.get("directory_identity"))
        ):
            raise RootRegistryError(
                f"project-remap logical root layout or identity changed: {root_id}"
            )


def _assert_entry_physical_targets(entry: Mapping[str, Any]) -> None:
    specifications = entry.get("root_bindings")
    if not isinstance(specifications, Mapping):
        raise RootRegistryError("project-remap logical root bindings are invalid")
    for root_id, specification in specifications.items():
        target = Path(str(specification.get("after_path"))).absolute()
        expected = validate_directory_identity(specification.get("directory_identity"))
        actual = directory_identity(target)
        if actual != expected:
            raise RootRegistryError(
                f"project-remap logical root physical identity changed: {root_id}"
            )


def _discover_registry_paths(story: Path) -> tuple[Path, ...]:
    novel = _existing_directory(story / ".novelagent", "StoryProject control plane")
    found: list[Path] = []
    for current_text, directories, files in os.walk(novel, followlinks=False):
        current = Path(current_text)
        _assert_plain_directory(current)
        for name in tuple(directories):
            _assert_plain_directory(current / name)
        for name in files:
            path = current / name
            _assert_plain_file(path)
            if name == "root_registry.json":
                relative = path.relative_to(story)
                if relative not in _ALLOWED_REGISTRIES:
                    raise RootRegistryError(
                        "rogue or nested root-registry control plane detected: "
                        + relative.as_posix()
                    )
                found.append(path)
    main = story / _MAIN_REGISTRY
    if main not in found:
        raise RootRegistryError("canonical main root registry is missing")
    return tuple(sorted(found, key=lambda item: item.relative_to(story).as_posix()))


def _assert_project_idle(story: Path, inventory: tuple[Path, ...]) -> None:
    for registry_path in inventory:
        _assert_pending_persistence_idle(registry_path.parent)
    legacy = story / ".novelagent" / "runtime" / "runs" / "transactions"
    if legacy.exists():
        _assert_plain_directory(legacy)
        for journal in sorted(legacy.iterdir(), key=lambda item: item.name):
            _assert_legacy_v1_journal_terminal(journal)
    autonomy = story / ".novelagent" / "runtime" / "autonomy"
    blocked = _active_or_invalid_autonomy_sessions(autonomy)
    if blocked:
        raise RootRemapBlockedError(
            "whole-StoryProject remap is blocked by autonomy operations/sessions/leases: "
            + ", ".join(sorted(blocked))
        )


def _assert_legacy_v1_journal_terminal(journal: Path) -> None:
    try:
        _assert_plain_directory(journal)
        manifest_path = journal / "manifest.json"
        _assert_plain_file(manifest_path)
        manifest = _load_manifest(journal)
        marker_relative = manifest.get("commit_marker") or "commit.marker"
        if not isinstance(marker_relative, str):
            raise RootRegistryError("legacy commit marker path is invalid")
        marker = _journal_child(journal, marker_relative)
        state = manifest.get("state")
        if state == "completed":
            _assert_plain_file(marker)
            if _validate_commit_marker(
                marker,
                str(manifest["run_id"]),
                manifest.get("candidate_sha256"),
            ) is not None:
                raise RootRegistryError("legacy completed marker is invalid")
        elif state == "rolled_back":
            if os.path.lexists(marker):
                raise RootRegistryError("legacy rolled-back journal has a commit marker")
        else:
            raise RootRegistryError("legacy transaction is not terminal")
    except Exception as exc:
        raise RootRemapBlockedError(
            "whole-StoryProject remap is blocked by an invalid or pending legacy journal"
        ) from exc


def _revalidate_project(
    intent: Mapping[str, Any], *, new_story: Path, inventory: tuple[Path, ...]
) -> None:
    if _canonical_path(intent["new_story_project"]) != _canonical_path(new_story):
        raise RootRegistryError("project remap journal belongs to another target root")
    old_story = Path(str(intent["old_story_project"])).absolute()
    if os.path.lexists(old_story):
        raise RootRegistryError(
            "whole-StoryProject relocation rejected: the old StoryProject path reappeared"
        )
    if directory_identity(new_story) != validate_directory_identity(
        intent["story_directory_identity"]
    ):
        raise RootRegistryError("new StoryProject directory identity changed during remap")
    if _file_sha256(new_story / ".novelagent" / "project.json") != intent[
        "project_identity_sha256"
    ]:
        raise RootRegistryError("ProjectIdentity changed during root remap")
    _project_fence_attestation(
        load_root_registry(new_story / _MAIN_REGISTRY),
        dependency_fence=(
            new_story / ".novelagent" / "runtime" / ".root-remap-fence"
        ),
    )
    current_inventory = _discover_registry_paths(new_story)
    if current_inventory != inventory:
        raise RootRegistryError("embedded root-registry inventory changed during remap")
    actual = [
        path.relative_to(new_story).as_posix()
        for path in _application_order(new_story, current_inventory)
    ]
    expected = [entry["relative_path"] for entry in intent["registries"]]
    if actual != expected:
        raise RootRegistryError("embedded root-registry inventory changed during remap")
    for entry in intent["registries"]:
        path = new_story / Path(entry["relative_path"])
        _assert_registry_control_identity(entry, path)
        digest = _file_sha256(path)
        if digest not in {entry["before_sha256"], entry["after_sha256"]}:
            raise RootRegistryCasError(f"embedded root registry drifted: {path}")
        state = "before" if digest == entry["before_sha256"] else "after"
        _validate_registry_against_entry(
            load_root_registry(path), entry, state=state
        )
        _assert_entry_physical_targets(entry)


def _verify_all_before(intent: Mapping[str, Any], *, new_story: Path) -> None:
    for entry in intent["registries"]:
        path = new_story / Path(entry["relative_path"])
        if _file_sha256(path) != entry["before_sha256"]:
            raise RootRegistryCasError(
                f"embedded root registry changed before project commit marker: {path}"
            )
        _validate_registry_against_entry(
            load_root_registry(path), entry, state="before"
        )
        _assert_entry_physical_targets(entry)


def _verify_all_after(
    intent: Mapping[str, Any], *, new_story: Path, directory: Path
) -> None:
    for entry in intent["registries"]:
        index = int(entry["index"])
        path = new_story / Path(entry["relative_path"])
        if _file_sha256(path) != entry["after_sha256"]:
            raise RootRegistryError(f"project root remap is incomplete: {path}")
        after = load_root_registry(path)
        _validate_registry_against_entry(after, entry, state="after")
        _assert_entry_physical_targets(entry)
        progress = directory / "progress" / f"{index:03d}.json"
        if not progress.is_file():
            raise RootRegistryError(f"project root remap progress is missing: {progress}")
        _validate_progress(_load_json(progress), intent=intent, entry=entry)


def _load_candidate_transaction(
    journal_home: Path,
    *,
    new_story: Path,
    requested: Mapping[str, Mapping[str, Any]],
    expected_revision: int,
    expected_registry_digest: str,
) -> dict[str, Any] | None:
    transactions = journal_home / "transactions"
    if not transactions.exists():
        return None
    _assert_plain_directory(transactions)
    incomplete: list[dict[str, Any]] = []
    completed_matches: list[dict[str, Any]] = []
    conflicting_basis: list[Path] = []
    for directory in sorted(transactions.iterdir(), key=lambda item: item.name):
        if directory.name.startswith(".prepare-"):
            if re.fullmatch(r"\.prepare-[0-9a-f]{32}", directory.name) is None:
                raise RootRegistryError("invalid orphan project-remap preparation name")
            _assert_plain_tree(directory)
            abandoned = journal_home / "abandoned-prepares"
            abandoned.mkdir(parents=True, exist_ok=True)
            destination = abandoned / directory.name
            if destination.exists():
                raise RootRegistryError("duplicate orphan project-remap preparation")
            os.replace(directory, destination)
            _fsync_directory(transactions)
            _fsync_directory(abandoned)
            continue
        if not directory.is_dir():
            raise RootRegistryError("project-remap journal contains a non-directory entry")
        loaded = _load_transaction(directory)
        if not (directory / "completed.json").exists():
            incomplete.append(loaded)
            continue
        intent = loaded["intent"]
        same_cas_basis = (
            _canonical_path(intent["new_story_project"])
            == _canonical_path(new_story)
            and intent["main_expected_revision"] == expected_revision
            and intent["main_expected_registry_digest"]
            == expected_registry_digest
        )
        if not same_cas_basis:
            continue
        try:
            _assert_resume_request(
                intent,
                new_story=new_story,
                requested=requested,
                expected_revision=expected_revision,
                expected_registry_digest=expected_registry_digest,
            )
        except RootRegistryCasError:
            conflicting_basis.append(directory)
        else:
            completed_matches.append(loaded)
    if len(incomplete) > 1:
        raise RootRegistryError("multiple incomplete project-remap transactions exist")
    if conflicting_basis:
        raise RootRegistryError(
            "a conflicting completed project-remap intent has the same CAS basis"
        )
    if len(completed_matches) > 1:
        raise RootRegistryError("multiple matching completed project-remap intents exist")
    if incomplete and completed_matches:
        raise RootRegistryError(
            "completed and incomplete project-remap intents conflict for recovery"
        )
    if incomplete:
        return incomplete[0]
    return completed_matches[0] if completed_matches else None


def _load_transaction(directory: Path) -> dict[str, Any]:
    _assert_plain_directory(directory)
    intent_path = directory / "intent.json"
    _assert_plain_file(intent_path)
    intent = _validate_intent(_load_json(intent_path))
    if directory.name != intent["transaction_id"]:
        raise RootRegistryError("project-remap transaction directory identity mismatch")
    _validate_transaction_artifacts(directory, intent)
    return {"directory": directory, "intent": intent}


def _validate_intent(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PROJECT_ROOT_REMAP_SCHEMA_VERSION
        or not isinstance(value.get("transaction_id"), str)
        or _TRANSACTION_ID_PATTERN.fullmatch(value["transaction_id"]) is None
    ):
        raise RootRegistryError("project-remap intent is invalid")
    expected = canonical_json_hash(value, exclude_fields=("intent_hash",))
    if value.get("intent_hash") != expected:
        raise RootRegistryError("project-remap intent hash mismatch")
    for field in (
        "main_registry_id",
        "main_expected_registry_digest",
        "project_identity_sha256",
    ):
        if not isinstance(value.get(field), str):
            raise RootRegistryError(f"project-remap intent field is invalid: {field}")
    _validate_canonical_uuid(value["main_registry_id"], "main registry id")
    _validate_sha256(value["main_expected_registry_digest"], "main registry digest")
    _validate_sha256(value["project_identity_sha256"], "ProjectIdentity digest")
    if (
        not isinstance(value.get("main_expected_revision"), int)
        or isinstance(value["main_expected_revision"], bool)
        or value["main_expected_revision"] < 1
    ):
        raise RootRegistryError("project-remap main expected revision is invalid")
    for field in ("book_id", "old_story_project", "new_story_project", "created_at"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise RootRegistryError(f"project-remap intent field is invalid: {field}")
    for field in ("old_story_project", "new_story_project"):
        if not Path(value[field]).is_absolute():
            raise RootRegistryError(f"project-remap intent path is not absolute: {field}")

    requested = value.get("requested")
    if not isinstance(requested, dict) or not requested:
        raise RootRegistryError("project-remap requested roots are invalid")
    for root_id, item in requested.items():
        if (
            not isinstance(root_id, str)
            or not root_id
            or not isinstance(item, dict)
            or set(item) != {"root_uuid", "path"}
            or not isinstance(item.get("path"), str)
            or not Path(item["path"]).is_absolute()
        ):
            raise RootRegistryError("project-remap requested root is invalid")
        _validate_canonical_uuid(item.get("root_uuid"), f"requested root UUID {root_id}")

    registries = value.get("registries")
    if not isinstance(registries, list) or not registries:
        raise RootRegistryError("project-remap registry inventory is invalid")
    relative_paths: list[str] = []
    registry_ids: set[str] = set()
    for index, entry in enumerate(registries):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("index"), int)
            or isinstance(entry.get("index"), bool)
            or entry.get("index") != index
        ):
            raise RootRegistryError("project-remap registry entry ordering is invalid")
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or relative not in _ALLOWED_REGISTRY_TEXTS:
            raise RootRegistryError(
                "project-remap registry path is not a canonical allowed control plane"
            )
        if relative in relative_paths:
            raise RootRegistryError("project-remap registry path is duplicated")
        relative_paths.append(relative)
        registry_id = entry.get("registry_id")
        _validate_canonical_uuid(registry_id, "embedded registry id")
        if registry_id in registry_ids:
            raise RootRegistryError("project-remap embedded registry id is duplicated")
        registry_ids.add(registry_id)
        for field in (
            "before_registry_digest",
            "before_sha256",
            "after_registry_digest",
            "after_sha256",
        ):
            _validate_sha256(entry.get(field), f"registry {field}")
        before_revision = entry.get("before_revision")
        after_revision = entry.get("after_revision")
        if (
            not isinstance(before_revision, int)
            or isinstance(before_revision, bool)
            or before_revision < 1
            or not isinstance(after_revision, int)
            or isinstance(after_revision, bool)
            or after_revision != before_revision + 1
        ):
            raise RootRegistryError("project-remap registry revision transition is invalid")
        root_uuids = entry.get("root_uuids")
        if not isinstance(root_uuids, dict) or not root_uuids:
            raise RootRegistryError("project-remap logical root inventory is invalid")
        for root_id, root_uuid in root_uuids.items():
            if not isinstance(root_id, str) or not root_id:
                raise RootRegistryError("project-remap logical root id is invalid")
            _validate_canonical_uuid(root_uuid, f"logical root UUID {root_id}")
        root_bindings = entry.get("root_bindings")
        if not isinstance(root_bindings, dict) or set(root_bindings) != set(root_uuids):
            raise RootRegistryError("project-remap logical root binding index is invalid")
        for root_id, specification in root_bindings.items():
            if (
                not isinstance(specification, dict)
                or set(specification)
                != {
                    "root_uuid",
                    "before_path",
                    "after_path",
                    "directory_identity",
                }
                or specification.get("root_uuid") != root_uuids[root_id]
            ):
                raise RootRegistryError(
                    f"project-remap logical root binding is invalid: {root_id}"
                )
            before_path = specification.get("before_path")
            after_path = specification.get("after_path")
            if (
                not isinstance(before_path, str)
                or not isinstance(after_path, str)
                or not Path(before_path).is_absolute()
                or not Path(after_path).is_absolute()
                or before_path != str(Path(before_path).absolute())
                or after_path != str(Path(after_path).absolute())
            ):
                raise RootRegistryError(
                    f"project-remap logical root path is invalid: {root_id}"
                )
            expected_target = _target_after_story_move(
                Path(before_path),
                old_story=Path(value["old_story_project"]),
                new_story=Path(value["new_story_project"]),
            )
            if after_path != str(expected_target.absolute()):
                raise RootRegistryError(
                    f"project-remap logical root layout changed: {root_id}"
                )
            binding_identity = validate_directory_identity(
                specification.get("directory_identity")
            )
            if root_id == "story_project" and (
                _canonical_path(before_path)
                != _canonical_path(value["old_story_project"])
                or _canonical_path(after_path)
                != _canonical_path(value["new_story_project"])
                or binding_identity
                != validate_directory_identity(value.get("story_directory_identity"))
            ):
                raise RootRegistryError(
                    "project-remap StoryProject logical binding is inconsistent"
                )
        validate_directory_identity(entry.get("control_plane_identity"))
        _validate_regular_file_identity(entry.get("control_lock_identity"))

    expected_order = sorted(path for path in relative_paths if path != _MAIN_REGISTRY_TEXT)
    expected_order.append(_MAIN_REGISTRY_TEXT)
    if relative_paths != expected_order:
        raise RootRegistryError(
            "project-remap registry inventory must be canonical, unique, and main-last"
        )
    inventory = value.get("registry_inventory")
    if inventory != relative_paths:
        raise RootRegistryError("project-remap registry inventory index is inconsistent")
    main_entry = registries[-1]
    if (
        main_entry["relative_path"] != _MAIN_REGISTRY_TEXT
        or main_entry["registry_id"] != value["main_registry_id"]
        or main_entry["before_revision"] != value["main_expected_revision"]
        or main_entry["before_registry_digest"]
        != value["main_expected_registry_digest"]
    ):
        raise RootRegistryError("project-remap main registry binding is inconsistent")
    validate_directory_identity(value.get("story_directory_identity"))
    return value


def _validate_transaction_artifacts(
    directory: Path, intent: Mapping[str, Any]
) -> None:
    """Validate journal containment and its exact staged/progress indexes.

    Intent hashes are corruption detectors, not an authorization boundary.  No
    path loaded from a journal is therefore permitted to choose a filesystem
    destination; registry destinations come only from the canonical allowlist.
    """

    allowed_top = {
        "intent.json",
        "after",
        "progress",
        "commit.marker",
        "completed.json",
    }
    for child in directory.iterdir():
        if child.name in allowed_top:
            continue
        if child.name.startswith(".na-") and child.name.endswith(".tmp"):
            _assert_plain_file(child)
            continue
        raise RootRegistryError(
            f"project-remap transaction contains an unexpected artifact: {child.name}"
        )

    after_root = directory / "after"
    progress_root = directory / "progress"
    _assert_plain_directory(after_root)
    _assert_plain_directory(progress_root)
    entries = list(intent["registries"])
    expected_names = {f"{int(entry['index']):03d}.json" for entry in entries}
    actual_after: set[str] = set()
    for path in after_root.iterdir():
        _assert_plain_file(path)
        actual_after.add(path.name)
    if actual_after != expected_names:
        raise RootRegistryError("project-remap staged registry index is inconsistent")
    for entry in entries:
        staged_path = after_root / f"{int(entry['index']):03d}.json"
        staged_bytes = staged_path.read_bytes()
        if _sha256(staged_bytes) != entry["after_sha256"]:
            raise RootRegistryError(
                f"project remap staged registry was modified: {staged_path}"
            )
        try:
            staged_value = json.loads(staged_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RootRegistryError(
                f"project-remap staged registry is invalid: {staged_path}"
            ) from exc
        staged_registry = validate_root_registry(staged_value)
        _validate_registry_against_entry(staged_registry, entry, state="after")

    progress_by_index = {int(entry["index"]): entry for entry in entries}
    for path in progress_root.iterdir():
        if path.name.startswith(".na-") and path.name.endswith(".tmp"):
            _assert_plain_file(path)
            continue
        _assert_plain_file(path)
        match = re.fullmatch(r"([0-9]{3})\.json", path.name)
        if match is None or int(match.group(1)) not in progress_by_index:
            raise RootRegistryError("project-remap progress index is inconsistent")
        entry = progress_by_index[int(match.group(1))]
        _validate_progress(_load_json(path), intent=intent, entry=entry)

    for name in ("commit.marker", "completed.json"):
        path = directory / name
        if path.exists():
            _assert_plain_file(path)


def _validate_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RootRegistryError(f"project-remap {label} is invalid")
    return value


def _validate_canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise RootRegistryError(f"project-remap {label} is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise RootRegistryError(f"project-remap {label} is invalid") from exc
    if str(parsed) != value:
        raise RootRegistryError(f"project-remap {label} is not canonical")
    return value


def _assert_resume_request(
    intent: Mapping[str, Any],
    *,
    new_story: Path,
    requested: Mapping[str, Mapping[str, Any]],
    expected_revision: int,
    expected_registry_digest: str,
) -> None:
    normalized = {
        root_id: {"root_uuid": item["root_uuid"], "path": str(item["path"])}
        for root_id, item in sorted(requested.items())
    }
    if (
        _canonical_path(intent["new_story_project"]) != _canonical_path(new_story)
        or intent["main_expected_revision"] != expected_revision
        or intent["main_expected_registry_digest"] != expected_registry_digest
        or intent["requested"] != normalized
    ):
        raise RootRegistryCasError(
            "project-remap recovery request differs from the durable intent"
        )


def _assert_main_request(
    main: Mapping[str, Any],
    *,
    requested: Mapping[str, Mapping[str, Any]],
    old_story: Path,
    new_story: Path,
) -> None:
    unknown = sorted(set(requested) - set(main["roots"]))
    if unknown:
        raise RootRegistryError("unknown logical roots: " + ", ".join(unknown))
    for root_id, item in requested.items():
        binding = main["roots"][root_id]
        if binding["root_uuid"] != item["root_uuid"]:
            raise RootRegistryError(f"logical root UUID mismatch for {root_id}")
        old_path = Path(str(binding["path"])).absolute()
        relative = _relative_beneath(old_path, old_story)
        expected = old_path if relative is None else new_story / relative
        if _canonical_path(item["path"]) != _canonical_path(expected):
            raise RootRegistryError(
                f"requested root {root_id} does not preserve the moved project layout"
            )


def _normalize_request(
    requested: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    if not isinstance(requested, Mapping) or not requested:
        raise RootRegistryError("at least one root remap is required")
    result: dict[str, dict[str, Any]] = {}
    for root_id, item in requested.items():
        if not isinstance(item, Mapping):
            raise RootRegistryError(f"invalid remap request for {root_id}")
        root_uuid = str(item.get("root_uuid") or "")
        path = _existing_directory(item.get("path"), f"requested root {root_id}")
        result[str(root_id)] = {"root_uuid": root_uuid, "path": path}
    return result


def _assert_identity_preserved(
    binding: Mapping[str, Any], target: Path, *, label: str
) -> None:
    recorded = binding.get("directory_identity")
    if recorded is None:
        raise RootRegistryError(
            f"{label} lacks a pre-move directory identity; rename-only relocation cannot be proven"
        )
    expected = validate_directory_identity(recorded)
    actual = directory_identity(target)
    if actual["device"] != expected["device"]:
        raise RootRegistryError(f"{label} moved across volumes; only same-volume rename is allowed")
    if actual != expected:
        raise RootRegistryError(
            f"{label} directory identity changed; copy-delete relocation is forbidden"
        )


def _assert_registry_control_identity(
    entry: Mapping[str, Any], registry_path: Path
) -> None:
    expected = validate_directory_identity(entry.get("control_plane_identity"))
    if directory_identity(registry_path.parent) != expected:
        raise RootRegistryError(
            f"embedded root-registry control plane identity changed: {registry_path.parent}"
        )
    if _require_armed_lock_file(
        registry_path.parent
    ) != _validate_regular_file_identity(entry.get("control_lock_identity")):
        raise RootRegistryError(
            f"embedded root-registry lock identity changed: {registry_path.parent}"
        )


def _application_order(story: Path, inventory: tuple[Path, ...]) -> tuple[Path, ...]:
    main = story / _MAIN_REGISTRY
    return tuple(path for path in inventory if path != main) + (main,)


def _relative_beneath(path: Path, root: Path) -> Path | None:
    path_text = _canonical_path(path)
    root_text = _canonical_path(root)
    try:
        if os.path.commonpath((path_text, root_text)) != root_text:
            return None
        relative = os.path.relpath(path_text, root_text)
    except ValueError:
        return None
    return Path(".") if relative == "." else Path(relative)


def _existing_directory(value: Any, label: str) -> Path:
    if value is None:
        raise RootRegistryError(f"{label} is required")
    path = assert_safe_local_tree(Path(str(value)).absolute())
    if not path.is_dir():
        raise RootRegistryError(f"{label} is not an existing directory: {path}")
    return path


def _project_fence_attestation(
    main_registry: Mapping[str, Any], *, dependency_fence: Path
) -> dict[str, Any]:
    runtime = main_registry.get("roots", {}).get("runtime")
    attestation = (
        runtime.get("project_remap_fence") if isinstance(runtime, Mapping) else None
    )
    if not isinstance(attestation, Mapping):
        raise RootRegistryError(
            "whole-StoryProject remap requires a pre-armed runtime fence; "
            "open the project at its old path before moving it"
        )
    result = dict(attestation)
    if result.get("relative_path") != ".root-remap-fence":
        raise RootRegistryError("project remap fence attestation is invalid")
    validate_directory_identity(result.get("directory_identity"))
    _validate_regular_file_identity(result.get("lock_identity"))
    _assert_project_fence_attestation(dependency_fence, result)
    return result


def _assert_project_fence_attestation(
    dependency_fence: Path, attestation: Mapping[str, Any]
) -> None:
    if directory_identity(dependency_fence) != validate_directory_identity(
        attestation.get("directory_identity")
    ):
        raise RootRegistryError(
            "pre-armed project remap fence directory identity changed"
        )
    if _require_armed_lock_file(dependency_fence) != _validate_regular_file_identity(
        attestation.get("lock_identity")
    ):
        raise RootRegistryError("pre-armed project remap fence lock identity changed")


def _require_armed_lock_file(root: Path) -> dict[str, int]:
    lock = root / ".persistence.lock"
    try:
        info = os.lstat(lock)
    except OSError as exc:
        raise RootRegistryError(
            f"required pre-armed persistence lock is missing: {lock}"
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        or info.st_size < 1
    ):
        raise RootRegistryError(f"required pre-armed persistence lock is unsafe: {lock}")
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(stat.S_IFMT(info.st_mode)),
    }


def _validate_regular_file_identity(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"device", "inode", "mode"}:
        raise RootRegistryError("pre-armed persistence lock identity is invalid")
    result: dict[str, int] = {}
    for field in ("device", "inode", "mode"):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise RootRegistryError(
                f"pre-armed persistence lock identity {field} is invalid"
            )
        result[field] = item
    if result["inode"] == 0 or not stat.S_ISREG(result["mode"]):
        raise RootRegistryError("pre-armed persistence lock identity is not a file")
    return result


def _ensure_plain_child_directory(parent: Path, child: Path) -> dict[str, int]:
    if child.parent != parent:
        raise RootRegistryError("controlled project directory must be one level below its parent")
    _assert_plain_directory(parent)
    created = False
    if not os.path.lexists(child):
        try:
            os.mkdir(child)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise RootRegistryError(f"cannot create controlled project directory: {child}") from exc
    _assert_plain_directory(parent)
    _assert_plain_directory(child)
    if created:
        _fsync_directory(parent)
    return directory_identity(child)


def _assert_plain_directory(path: Path) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RootRegistryError(f"unsafe directory in StoryProject control plane: {path}")


def _assert_plain_file(path: Path) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RootRegistryError(f"unsafe file in StoryProject control plane: {path}")


def _assert_plain_tree(root: Path) -> None:
    _assert_plain_directory(root)
    for current_text, directories, files in os.walk(root, followlinks=False):
        current = Path(current_text)
        _assert_plain_directory(current)
        for name in directories:
            _assert_plain_directory(current / name)
        for name in files:
            _assert_plain_file(current / name)


def _validate_marker(value: Any, intent: Mapping[str, Any]) -> None:
    if not isinstance(value, dict):
        raise RootRegistryError("project-remap commit marker is invalid")
    expected = canonical_json_hash(value, exclude_fields=("marker_hash",))
    if (
        value.get("marker_hash") != expected
        or value.get("transaction_id") != intent["transaction_id"]
        or value.get("intent_hash") != intent["intent_hash"]
    ):
        raise RootRegistryError("project-remap commit marker mismatch")


def _validate_completion(value: Any, intent: Mapping[str, Any]) -> None:
    if not isinstance(value, dict):
        raise RootRegistryError("project-remap completion is invalid")
    expected = canonical_json_hash(value, exclude_fields=("completion_hash",))
    if (
        value.get("completion_hash") != expected
        or value.get("transaction_id") != intent["transaction_id"]
        or value.get("intent_hash") != intent["intent_hash"]
        or value.get("registry_count") != len(intent["registries"])
    ):
        raise RootRegistryError("project-remap completion mismatch")


def _validate_progress(
    value: Any, *, intent: Mapping[str, Any], entry: Mapping[str, Any]
) -> None:
    if not isinstance(value, dict):
        raise RootRegistryError("project-remap progress is invalid")
    expected = canonical_json_hash(value, exclude_fields=("progress_hash",))
    if (
        value.get("progress_hash") != expected
        or value.get("transaction_id") != intent["transaction_id"]
        or value.get("intent_hash") != intent["intent_hash"]
        or value.get("index") != entry["index"]
        or value.get("relative_path") != entry["relative_path"]
        or value.get("after_sha256") != entry["after_sha256"]
    ):
        raise RootRegistryError("project-remap progress mismatch")


def _report(intent: Mapping[str, Any], completion: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "command": "remap_roots",
        "scope": "complete_story_project_rebind",
        "state": "completed",
        "transaction_id": intent["transaction_id"],
        "book_id": intent["book_id"],
        "old_story_project": intent["old_story_project"],
        "new_story_project": intent["new_story_project"],
        "registry_count": len(intent["registries"]),
        "registries": [
            {
                "relative_path": entry["relative_path"],
                "registry_id": entry["registry_id"],
                "previous_revision": entry["before_revision"],
                "revision": entry["after_revision"],
                "registry_digest": entry["after_registry_digest"],
            }
            for entry in intent["registries"]
        ],
        "all_registries_rebound": True,
        "identity_preserving_same_volume_rename": True,
        "data_moved_or_copied_by_command": False,
        "completed_at": completion["completed_at"],
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RootRegistryError(f"cannot load project-remap artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RootRegistryError(f"project-remap artifact must be an object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    try:
        return _sha256(path.read_bytes())
    except OSError as exc:
        raise RootRegistryError(f"cannot hash project-remap path {path}: {exc}") from exc


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _inject(
    fault_injector: _FaultInjector | None,
    event: str,
    index: int | None,
    path: Path | None,
) -> None:
    if fault_injector is not None:
        fault_injector(event, index, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "PROJECT_ROOT_REMAP_SCHEMA_VERSION",
    "remap_story_project_roots",
]
