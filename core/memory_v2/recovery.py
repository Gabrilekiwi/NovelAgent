from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from core.engine.persistence import atomic_write_json, atomic_write_text
from core.engine.safe_paths import RootBinding, SafePathError, SafePathResolver
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import memory_projection_hash, replay_memory_events
from core.memory_v2.projection import (
    rebuild_memory_projections,
    validate_memory_projection_receipt,
)
from core.memory_v2.storage import load_canonical_memory
from core.memory_v2.versions import CURRENT_REDUCER_VERSION
from core.path_refs import PathRef, PathRefError, path_ref_for


class MemoryCacheRecoveryError(ValueError):
    """Raised when disposable Memory 2.2 caches cannot be safely rebuilt."""


class MemoryAuthorityMismatchError(MemoryCacheRecoveryError):
    """Raised when the immutable event authority differs from its pinned identity."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"memory cache recovery {field} mismatch")


def ensure_event_authority_caches(
    memory_root: str | Path,
    *,
    runtime_root: str | Path | None = None,
    runtime_snapshot_target: str | Path | None = None,
    expected_book_id: str | None = None,
    expected_authority_epoch: int | None = None,
    expected_head_event_hash: str | None = None,
) -> dict[str, Any]:
    """Validate event authority and rebuild disposable caches when necessary.

    Immutable batches and checkpoints are validated before any cache is
    inspected or replaced.  Identity mismatches therefore remain hard errors;
    only canonical/projection/runtime-snapshot cache failures are recoverable.
    """

    if (runtime_root is None) != (runtime_snapshot_target is None):
        raise MemoryCacheRecoveryError(
            "runtime_root and runtime_snapshot_target must be provided together"
        )

    root = Path(memory_root).absolute()
    authoritative, projection = _validated_event_authority_projection(
        root,
        expected_book_id=expected_book_id,
        expected_authority_epoch=expected_authority_epoch,
        expected_head_event_hash=expected_head_event_hash,
    )
    artifacts = rebuild_memory_projections(projection)
    recovery_report: dict[str, Any] | None = None
    try:
        _verify_cache_bundle(
            root,
            projection=projection,
            artifacts=artifacts,
            runtime_root=runtime_root,
            runtime_snapshot_target=runtime_snapshot_target,
        )
    except (OSError, ValueError, TypeError, KeyError):
        # These files are explicitly disposable.  The rebuild performs the
        # immutable-chain and pinned-identity checks again before publishing,
        # so a cache failure can never turn authority drift into recovery.
        recovery_report = rebuild_event_authority_caches(
            root,
            runtime_root=runtime_root,
            runtime_snapshot_target=runtime_snapshot_target,
            expected_book_id=expected_book_id,
            expected_authority_epoch=expected_authority_epoch,
            expected_head_event_hash=expected_head_event_hash,
        )

    return {
        "projection": projection,
        "replay_report": authoritative,
        "cache_status": "rebuilt" if recovery_report is not None else "current",
        "recovery_report": recovery_report,
    }


def rebuild_event_authority_caches(
    memory_root: str | Path,
    *,
    runtime_root: str | Path | None = None,
    runtime_snapshot_target: str | Path | None = None,
    expected_book_id: str | None = None,
    expected_authority_epoch: int | None = None,
    expected_head_event_hash: str | None = None,
) -> dict[str, Any]:
    """Rebuild all disposable Memory 2.2 caches from the immutable event chain.

    The event store and checkpoints are treated as read-only evidence.  The
    canonical cache, semantic snapshot, tracking Markdown, and their projection
    receipts are rendered deterministically and atomically replaced.  A crash
    between cache writes is safe: invoking this function again produces the
    same bytes.
    """

    if (runtime_root is None) != (runtime_snapshot_target is None):
        raise MemoryCacheRecoveryError(
            "runtime_root and runtime_snapshot_target must be provided together"
        )

    root = Path(memory_root).absolute()
    authoritative, projection = _validated_event_authority_projection(
        root,
        expected_book_id=expected_book_id,
        expected_authority_epoch=expected_authority_epoch,
        expected_head_event_hash=expected_head_event_hash,
    )
    artifacts = rebuild_memory_projections(projection)
    payloads = _cache_payloads(projection=projection, artifacts=artifacts)
    resolver = _cache_resolver(root)
    runtime_destination = (
        _runtime_snapshot_destination(
            runtime_root=runtime_root,
            runtime_snapshot_target=runtime_snapshot_target,
        )
        if runtime_root is not None and runtime_snapshot_target is not None
        else None
    )

    resolved: list[tuple[str, PathRef, dict[str, Any], str, Any]] = []
    for relative_path, kind, payload in payloads:
        path_ref = PathRef(root_id="runtime", relative_path=relative_path)
        safe = resolver.ensure_parent(path_ref)
        resolved.append((relative_path, safe.path_ref, safe.guard, kind, payload))

    published: list[tuple[str, Path]] = []
    for relative_path, path_ref, guard, kind, payload in resolved:
        # Parent identities are checked again immediately before every atomic
        # replace so a directory swap cannot redirect a recovery write.
        path = resolver.resolve(path_ref, expected_guard=guard).path
        if kind == "json":
            atomic_write_json(path, payload)
        else:
            atomic_write_text(path, payload)
        published.append((relative_path, path))

    runtime_snapshot_report = None
    if runtime_destination is not None:
        runtime_resolver, runtime_ref, runtime_guard = runtime_destination
        runtime_path = runtime_resolver.resolve(
            runtime_ref, expected_guard=runtime_guard
        ).path
        # A different disposable cache may have triggered this rebuild while
        # the runtime snapshot is already semantically current.  Preserve its
        # bytes in that case so readers holding a valid CAS version do not see
        # a spurious concurrent mutation caused only by JSON formatting.
        runtime_snapshot_before = None
        if runtime_path.is_file():
            try:
                runtime_snapshot_before = json.loads(
                    runtime_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                runtime_snapshot_before = None
        if runtime_snapshot_before != artifacts["snapshot"]:
            atomic_write_json(runtime_path, artifacts["snapshot"])
        runtime_snapshot = json.loads(
            runtime_path.read_text(encoding="utf-8-sig")
        )
        if runtime_snapshot != artifacts["snapshot"]:
            raise MemoryCacheRecoveryError(
                "rebuilt runtime snapshot differs from canonical renderer"
            )
        runtime_snapshot_report = {
            "root_id": "snapshot",
            "relative_path": runtime_ref.relative_path,
            "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            "size": runtime_path.stat().st_size,
            "artifact_hash": artifacts["snapshot_receipt"]["artifact_hash"],
        }

    loaded = load_canonical_memory(root / "canonical_memory.json")
    if loaded != projection or memory_projection_hash(loaded) != authoritative.get(
        "projection_hash"
    ):
        raise MemoryCacheRecoveryError("rebuilt canonical cache does not match event replay")
    _verify_projection_files(root, projection=projection, artifacts=artifacts)

    cache_files = [
        {
            "relative_path": relative_path,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for relative_path, path in published
    ]
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "rebuilt",
        "book_id": projection["book_id"],
        "authority_epoch": projection["authority_epoch"],
        "revision": projection["revision"],
        "head_event_hash": projection["head_event_hash"],
        "reducer_version": authoritative["reducer_version"],
        "projection_hash": authoritative["projection_hash"],
        "snapshot_artifact_hash": artifacts["snapshot_receipt"]["artifact_hash"],
        "tracking_artifact_hash": artifacts["tracking_receipt"]["artifact_hash"],
        "runtime_snapshot": runtime_snapshot_report,
        "cache_files": cache_files,
    }
    report["recovery_hash"] = canonical_json_hash(
        report, exclude_fields=("recovery_hash",)
    )
    return report


def _cache_payloads(
    *,
    projection: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> list[tuple[str, str, Any]]:
    snapshot = artifacts.get("snapshot")
    tracking = artifacts.get("tracking")
    snapshot_receipt = artifacts.get("snapshot_receipt")
    tracking_receipt = artifacts.get("tracking_receipt")
    if not isinstance(snapshot, Mapping) or not isinstance(tracking, Mapping):
        raise MemoryCacheRecoveryError("projection renderer returned invalid cache artifacts")
    if not isinstance(snapshot_receipt, Mapping) or not isinstance(
        tracking_receipt, Mapping
    ):
        raise MemoryCacheRecoveryError("projection renderer returned invalid receipts")

    payloads: list[tuple[str, str, Any]] = [
        ("canonical_memory.json", "json", dict(projection)),
        ("projections/snapshot.json", "json", dict(snapshot)),
    ]
    for relative_name in sorted(str(item) for item in tracking):
        relative = Path(relative_name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise MemoryCacheRecoveryError("tracking renderer returned an unsafe path")
        content = tracking[relative_name]
        if not isinstance(content, str):
            raise MemoryCacheRecoveryError("tracking renderer returned non-text content")
        payloads.append(
            (f"projections/{relative.as_posix()}", "text", content)
        )
    payloads.extend(
        [
            (
                "projections/receipts/snapshot.json",
                "json",
                dict(snapshot_receipt),
            ),
            (
                "projections/receipts/tracking.json",
                "json",
                dict(tracking_receipt),
            ),
        ]
    )
    return payloads


def _cache_resolver(root: Path) -> SafePathResolver:
    root_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"novelagent:memory-cache-root:{root}")
    )
    return SafePathResolver(
        {"runtime": RootBinding(root_id="runtime", root_uuid=root_uuid, path=root)}
    )


def _runtime_snapshot_destination(
    *,
    runtime_root: str | Path,
    runtime_snapshot_target: str | Path,
) -> tuple[SafePathResolver, PathRef, dict[str, Any]]:
    root = Path(runtime_root).absolute()
    target = Path(runtime_snapshot_target).absolute()
    root_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"novelagent:runtime-snapshot-root:{root}")
    )
    try:
        path_ref = path_ref_for(
            target,
            root_id="snapshot",
            root=root,
            root_uuid=root_uuid,
        )
        resolver = SafePathResolver(
            {
                "snapshot": RootBinding(
                    root_id="snapshot", root_uuid=root_uuid, path=root
                )
            }
        )
        resolved = resolver.ensure_parent(path_ref)
    except (PathRefError, SafePathError) as exc:
        raise MemoryCacheRecoveryError(
            f"runtime snapshot target is outside or unsafe for its explicit root: {exc}"
        ) from exc
    return resolver, resolved.path_ref, resolved.guard


def _verify_projection_files(
    root: Path,
    *,
    projection: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> None:
    snapshot = json.loads(
        (root / "projections" / "snapshot.json").read_text(encoding="utf-8-sig")
    )
    if snapshot != artifacts["snapshot"]:
        raise MemoryCacheRecoveryError("rebuilt snapshot projection differs from renderer")
    for relative_name, content in artifacts["tracking"].items():
        if (
            root / "projections" / Path(relative_name)
        ).read_text(encoding="utf-8") != content:
            raise MemoryCacheRecoveryError("rebuilt tracking projection differs from renderer")

    snapshot_receipt = json.loads(
        (root / "projections" / "receipts" / "snapshot.json").read_text(
            encoding="utf-8-sig"
        )
    )
    tracking_receipt = json.loads(
        (root / "projections" / "receipts" / "tracking.json").read_text(
            encoding="utf-8-sig"
        )
    )
    validate_memory_projection_receipt(
        snapshot_receipt,
        canonical_memory=dict(projection),
        artifact=snapshot,
    )
    validate_memory_projection_receipt(
        tracking_receipt,
        canonical_memory=dict(projection),
        artifact=dict(artifacts["tracking"]),
    )


def _verify_cache_bundle(
    root: Path,
    *,
    projection: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    runtime_root: str | Path | None,
    runtime_snapshot_target: str | Path | None,
) -> None:
    loaded = load_canonical_memory(root / "canonical_memory.json")
    if loaded != projection:
        raise MemoryCacheRecoveryError(
            "canonical cache differs from immutable event replay"
        )
    _verify_projection_files(root, projection=projection, artifacts=artifacts)

    if runtime_root is None or runtime_snapshot_target is None:
        return
    runtime_resolver, runtime_ref, runtime_guard = _runtime_snapshot_destination(
        runtime_root=runtime_root,
        runtime_snapshot_target=runtime_snapshot_target,
    )
    runtime_path = runtime_resolver.resolve(
        runtime_ref, expected_guard=runtime_guard
    ).path
    runtime_snapshot = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
    if runtime_snapshot != artifacts["snapshot"]:
        raise MemoryCacheRecoveryError(
            "runtime snapshot cache differs from canonical renderer"
        )


def _validated_event_authority_projection(
    root: Path,
    *,
    expected_book_id: str | None,
    expected_authority_epoch: int | None,
    expected_head_event_hash: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_store = root / "events"
    if not event_store.is_dir():
        raise MemoryCacheRecoveryError("memory event store is missing")

    # Replay from genesis rather than trusting a checkpoint cache.  A normal
    # checkpoint-assisted replay is also required to agree when checkpoints
    # are present, so corruption in either immutable history or acceleration
    # evidence fails closed even when all disposable caches appear healthy.
    authoritative = replay_memory_events(event_store, use_checkpoint=False)
    accelerated = replay_memory_events(event_store)
    projection = authoritative.get("projection")
    if not isinstance(projection, dict):
        raise MemoryCacheRecoveryError(
            "memory event replay produced no canonical projection"
        )
    if accelerated.get("projection") != projection:
        raise MemoryCacheRecoveryError(
            "checkpoint replay differs from the immutable event chain"
        )
    if (
        authoritative.get("schema_version") != "2.2"
        or projection.get("schema_version") != "2.2"
    ):
        raise MemoryCacheRecoveryError(
            "event-authority cache recovery requires Memory 2.2"
        )
    if authoritative.get("reducer_version") != CURRENT_REDUCER_VERSION:
        raise MemoryCacheRecoveryError(
            "memory event chain uses an unsupported reducer"
        )
    _assert_expected_identity(
        projection,
        expected_book_id=expected_book_id,
        expected_authority_epoch=expected_authority_epoch,
        expected_head_event_hash=expected_head_event_hash,
    )
    return authoritative, projection


def _assert_expected_identity(
    projection: Mapping[str, Any],
    *,
    expected_book_id: str | None,
    expected_authority_epoch: int | None,
    expected_head_event_hash: str | None,
) -> None:
    if expected_book_id is not None and projection.get("book_id") != expected_book_id:
        raise MemoryAuthorityMismatchError("book_id")
    if expected_authority_epoch is not None and projection.get(
        "authority_epoch"
    ) != expected_authority_epoch:
        raise MemoryAuthorityMismatchError("authority_epoch")
    if expected_head_event_hash is not None and projection.get(
        "head_event_hash"
    ) != expected_head_event_hash:
        raise MemoryAuthorityMismatchError("head_event_hash")


__all__ = [
    "MemoryAuthorityMismatchError",
    "MemoryCacheRecoveryError",
    "ensure_event_authority_caches",
    "rebuild_event_authority_caches",
]
