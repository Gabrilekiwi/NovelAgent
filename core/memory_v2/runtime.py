from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from core.memory_v2.event_store import (
    CHECKPOINT_CHAPTER_INTERVAL,
    MemoryIntegrityError,
    MemoryPatchConflictError,
    create_memory_checkpoint,
    create_memory_event_batch,
    load_memory_event_batches,
    memory_patch_content_hash,
    memory_projection_hash,
    replay_memory_events,
)
from core.memory_v2.events import create_memory_event_context
from core.memory_v2.models import create_empty_canonical_memory
from core.memory_v2.patch import create_memory_patch
from core.memory_v2.projection import rebuild_memory_projections
from core.memory_v2.reducer import CURRENT_REDUCER_VERSION, apply_memory_events, apply_memory_patch
from core.memory_v2.storage import load_canonical_memory


def ensure_memory_v2_storage_layout(memory_root: str | Path) -> dict[str, Path]:
    """Create the stable directories required by transactional Memory V2 targets."""
    root = Path(memory_root)
    event_store = root / "events"
    batches = event_store / "batches"
    checkpoints = event_store / "checkpoints"
    batches.mkdir(parents=True, exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "event_store": event_store,
        "batches": batches,
        "checkpoints": checkpoints,
    }


def prepare_chapter_memory_commit(
    *,
    memory_root: str | Path,
    book_id: str,
    run_id: str,
    chapter_index: int,
    analysis: Mapping[str, Any],
    source_project_digest: str,
    context_digest: str,
    quality_state: Mapping[str, Any] | None = None,
    title: str = "Untitled",
    language: str = "zh-CN",
    checkpoint_interval: int = CHECKPOINT_CHAPTER_INTERVAL,
) -> dict[str, Any]:
    root = Path(memory_root)
    event_store = root / "events"
    canonical_path = root / "canonical_memory.json"
    _require_digest("source_project_digest", source_project_digest)
    _require_digest("context_digest", context_digest)
    if not book_id or not run_id:
        raise ValueError("Memory V2 chapter commit requires book_id and run_id")
    if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
        raise ValueError("Memory V2 chapter commit requires a positive chapter_index")

    patch = create_memory_patch(
        patch_id=f"patch_chapter_{chapter_index:06d}_{_safe_id(run_id)}",
        source_kind="committed_chapter",
        source_path=f"chapter:{chapter_index}",
        operations=_chapter_operations(chapter_index, analysis),
        metadata={"source_item_count": _source_item_count(analysis), "run_id": run_id},
    )
    has_batches = any((event_store / "batches").glob("*.json"))
    if has_batches:
        replay = replay_memory_events(event_store)
        base = copy.deepcopy(replay["projection"])
    else:
        base = (
            load_canonical_memory(canonical_path)
            if canonical_path.exists()
            else create_empty_canonical_memory(book_id=book_id, title=title, language=language)
        )
        replay = _empty_replay(base)
    if str(base["book_id"]) != book_id:
        raise ValueError("story_project_memory_v2_identity_mismatch")

    patch_hash = memory_patch_content_hash(patch)
    existing_hash = replay["patch_index"].get(patch["patch_id"])
    if existing_hash is not None:
        if existing_hash != patch_hash:
            raise MemoryPatchConflictError(
                f"patch id {patch['patch_id']} already exists with different content"
            )
        return {
            "status": "no_op",
            "targets": [],
            "audit": _audit(
                root=root,
                previous_revision=int(base["revision"]),
                projection=base,
                patch=patch,
                batch=None,
                checkpoint=None,
            ),
        }

    updated, events = apply_memory_patch(base, patch)
    if not events:
        raise ValueError("Memory V2 chapter patch must produce at least one event")
    batch = create_memory_event_batch(
        book_id=book_id,
        patch=patch,
        events=events,
        expected_revision=int(base["revision"]),
        previous_batch_hash=replay["last_batch_hash"],
        source_project_digest=source_project_digest,
        context_digest=context_digest,
        batch_kind="chapter",
        publication_status="committed",
        base_projection=base if replay["last_batch_hash"] is None else None,
        quality_state=dict(quality_state or {}),
    )
    batch_path = event_store / "batches" / (
        f"batch_{batch['first_revision']:012d}_{batch['last_revision']:012d}_"
        f"{batch['patch_content_hash'][:12]}.json"
    )
    targets = [
        _json_target("memory_event_batch", batch_path, batch),
        _json_target("memory_projection", canonical_path, updated),
    ]

    committed_chapters = int(replay["committed_chapter_count"]) + 1
    checkpoint = None
    if checkpoint_interval > 0 and committed_chapters % checkpoint_interval == 0:
        patch_index = dict(replay["patch_index"])
        patch_index[str(patch["patch_id"])] = patch_hash
        accumulated_quality = copy.deepcopy(replay["quality_state"])
        if quality_state:
            accumulated_quality[str(batch["batch_id"])] = copy.deepcopy(dict(quality_state))
        checkpoint = create_memory_checkpoint(
            projection=updated,
            last_batch=batch,
            committed_chapter_count=committed_chapters,
            patch_index=patch_index,
            quality_state=accumulated_quality,
        )
        checkpoint_path = event_store / "checkpoints" / f"{checkpoint['checkpoint_id']}.json"
        targets.append(_json_target("memory_checkpoint", checkpoint_path, checkpoint))

    return {
        "status": "prepared",
        "targets": targets,
        "audit": _audit(
            root=root,
            previous_revision=int(base["revision"]),
            projection=updated,
            patch=patch,
            batch=batch,
            checkpoint=checkpoint,
        ),
    }


def prepare_event_authority_chapter_commit(
    *,
    memory_root: str | Path,
    book_id: str,
    run_id: str,
    chapter_index: int,
    analysis: Mapping[str, Any],
    chapter_body: str,
    chapter_body_sha256: str,
    evidence_spans: list[dict[str, Any]],
    authority_epoch: int,
    expected_head_event_hash: str,
    source_project_digest: str,
    context_digest: str,
    expected_revision: int | None = None,
    quality_state: Mapping[str, Any] | None = None,
    checkpoint_interval: int = CHECKPOINT_CHAPTER_INTERVAL,
    projection_root: str | Path | None = None,
) -> dict[str, Any]:
    """Purely prepare one committed chapter for an existing Memory 2.2 authority.

    The event store is authoritative and ``canonical_memory.json`` is only a
    verified cache.  This function reads both, fails closed on any drift, and
    returns CAS-aware targets without creating or modifying any files.
    """

    root = Path(memory_root)
    event_store = root / "events"
    canonical_path = root / "canonical_memory.json"
    _require_digest("source_project_digest", source_project_digest)
    _require_digest("context_digest", context_digest)
    _require_digest("chapter_body_sha256", chapter_body_sha256)
    _require_digest("expected_head_event_hash", expected_head_event_hash)
    if not book_id or not run_id:
        raise ValueError("event-authority Memory 2.2 chapter commit requires book_id and run_id")
    if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
        raise ValueError("event-authority Memory 2.2 chapter commit requires a positive chapter_index")
    if expected_revision is not None and (
        isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1
    ):
        raise ValueError("expected_revision must be a positive integer")

    event_context = create_memory_event_context(
        chapter_body=chapter_body,
        evidence_spans=copy.deepcopy(evidence_spans),
        authority_epoch=authority_epoch,
    )
    if event_context["chapter_body_sha256"] != chapter_body_sha256:
        raise ValueError("chapter_body_sha256 does not match the complete chapter_body")

    replay, base = _load_verified_event_authority_base(
        event_store=event_store,
        canonical_path=canonical_path,
        book_id=book_id,
        authority_epoch=authority_epoch,
        expected_head_event_hash=expected_head_event_hash,
        expected_revision=expected_revision,
    )
    patch = create_memory_patch(
        patch_id=f"patch_chapter_{chapter_index:06d}_{_safe_id(run_id)}",
        source_kind="committed_chapter",
        source_path=f"chapter:{chapter_index}",
        operations=_chapter_operations(chapter_index, analysis),
        metadata={
            "source_item_count": _source_item_count(analysis),
            "run_id": run_id,
            "chapter_body_sha256": chapter_body_sha256,
            "authority_epoch": authority_epoch,
        },
    )
    patch_hash = memory_patch_content_hash(patch)
    existing_hash = replay["patch_index"].get(patch["patch_id"])
    if existing_hash is not None:
        if existing_hash != patch_hash:
            raise MemoryPatchConflictError(
                f"patch id {patch['patch_id']} already exists with different content"
            )
        _verify_existing_event_authority_patch(
            event_store=event_store,
            patch_id=str(patch["patch_id"]),
            chapter_body_sha256=chapter_body_sha256,
            authority_epoch=authority_epoch,
        )
        return {
            "status": "no_op",
            "targets": [],
            "batch": None,
            "projection": copy.deepcopy(base),
            "checkpoint": None,
            "projection_receipts": {},
            "audit": _event_authority_audit(
                root=root,
                previous_revision=int(base["revision"]),
                previous_head_event_hash=str(base["head_event_hash"]),
                projection=base,
                patch=patch,
                batch=None,
                checkpoint=None,
                chapter_body_sha256=chapter_body_sha256,
                projection_receipts=None,
            ),
        }

    updated, events = apply_memory_patch(
        base,
        patch,
        reducer_version=CURRENT_REDUCER_VERSION,
        event_context=event_context,
    )
    if not events:
        raise ValueError("Memory 2.2 chapter patch must produce at least one event")
    replayed_update = apply_memory_events(
        base,
        events,
        reducer_version=CURRENT_REDUCER_VERSION,
    )
    if replayed_update != updated:
        raise MemoryIntegrityError("prepared Memory 2.2 events do not reproduce the typed canonical projection")

    batch = create_memory_event_batch(
        book_id=book_id,
        patch=patch,
        events=events,
        expected_revision=int(base["revision"]),
        previous_batch_hash=str(replay["last_batch_hash"]),
        source_project_digest=source_project_digest,
        context_digest=context_digest,
        batch_kind="chapter",
        publication_status="committed",
        quality_state=dict(quality_state or {}),
        schema_version="2.2",
        reducer_version=CURRENT_REDUCER_VERSION,
    )
    batch_path = event_store / "batches" / f"{batch['batch_id']}.json"
    targets = [
        _json_target("memory_event_batch", batch_path, batch),
        _json_target("memory_projection", canonical_path, updated),
    ]

    committed_chapters = int(replay["committed_chapter_count"]) + 1
    checkpoint = None
    if checkpoint_interval > 0 and committed_chapters % checkpoint_interval == 0:
        patch_index = dict(replay["patch_index"])
        patch_index[str(patch["patch_id"])] = patch_hash
        accumulated_quality = copy.deepcopy(replay["quality_state"])
        if quality_state:
            accumulated_quality[str(batch["batch_id"])] = copy.deepcopy(dict(quality_state))
        checkpoint = create_memory_checkpoint(
            projection=updated,
            last_batch=batch,
            committed_chapter_count=committed_chapters,
            patch_index=patch_index,
            quality_state=accumulated_quality,
        )
        checkpoint_path = event_store / "checkpoints" / f"{checkpoint['checkpoint_id']}.json"
        targets.append(_json_target("memory_checkpoint", checkpoint_path, checkpoint))

    projection_artifacts = rebuild_memory_projections(updated)
    projection_targets, projection_receipts = _projection_targets(
        projection_root=Path(projection_root) if projection_root is not None else root / "projections",
        artifacts=projection_artifacts,
    )
    targets.extend(projection_targets)

    return {
        "status": "prepared",
        "targets": targets,
        "batch": copy.deepcopy(batch),
        "projection": copy.deepcopy(updated),
        "checkpoint": copy.deepcopy(checkpoint),
        "projection_receipts": projection_receipts,
        "audit": _event_authority_audit(
            root=root,
            previous_revision=int(base["revision"]),
            previous_head_event_hash=str(base["head_event_hash"]),
            projection=updated,
            patch=patch,
            batch=batch,
            checkpoint=checkpoint,
            chapter_body_sha256=chapter_body_sha256,
            projection_receipts=projection_receipts,
        ),
    }


def _load_verified_event_authority_base(
    *,
    event_store: Path,
    canonical_path: Path,
    book_id: str,
    authority_epoch: int,
    expected_head_event_hash: str,
    expected_revision: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not canonical_path.is_file():
        raise MemoryIntegrityError("event-authority Memory 2.2 requires canonical_memory.json")
    replay = replay_memory_events(event_store)
    canonical = copy.deepcopy(load_canonical_memory(canonical_path))
    if replay.get("schema_version") != "2.2" or canonical.get("schema_version") != "2.2":
        raise MemoryIntegrityError("event-authority chapter commits require Memory 2.2 history and canonical cache")
    if replay.get("reducer_version") != CURRENT_REDUCER_VERSION:
        raise MemoryIntegrityError("Memory 2.2 replay is not bound to CURRENT_REDUCER_VERSION")
    if str(replay.get("book_id")) != book_id or str(canonical.get("book_id")) != book_id:
        raise MemoryIntegrityError("event-authority Memory 2.2 book_id mismatch")
    replay_projection = replay.get("projection")
    if not isinstance(replay_projection, dict):
        raise MemoryIntegrityError("Memory 2.2 replay did not produce a typed canonical projection")
    canonical_hash = memory_projection_hash(canonical)
    if canonical_hash != replay.get("projection_hash") or canonical != replay_projection:
        raise MemoryIntegrityError("canonical Memory 2.2 cache differs from authoritative event replay")
    if int(canonical.get("authority_epoch") or 0) != authority_epoch:
        raise MemoryIntegrityError("event-authority Memory 2.2 authority_epoch mismatch")
    actual_head = canonical.get("head_event_hash")
    if actual_head != expected_head_event_hash:
        raise MemoryIntegrityError("event-authority Memory 2.2 head_event_hash mismatch")
    if expected_revision is not None and int(canonical["revision"]) != expected_revision:
        raise MemoryIntegrityError("event-authority Memory 2.2 revision mismatch")
    return copy.deepcopy(replay), canonical


def _verify_existing_event_authority_patch(
    *,
    event_store: Path,
    patch_id: str,
    chapter_body_sha256: str,
    authority_epoch: int,
) -> None:
    matching = [
        batch
        for batch in load_memory_event_batches(event_store)
        if str(batch.get("patch_id")) == patch_id
    ]
    if len(matching) != 1:
        raise MemoryIntegrityError("Memory 2.2 patch index does not identify exactly one immutable batch")
    batch = matching[0]
    if batch.get("schema_version") != "2.2" or batch.get("reducer_version") != CURRENT_REDUCER_VERSION:
        raise MemoryIntegrityError("existing event-authority patch is not a Memory 2.2 batch")
    if any(
        event.get("chapter_body_sha256") != chapter_body_sha256
        or int(event.get("authority_epoch") or 0) != authority_epoch
        for event in batch["events"]
    ):
        raise MemoryPatchConflictError(
            f"patch id {patch_id} already exists with different chapter evidence"
        )


def _projection_targets(
    *,
    projection_root: Path,
    artifacts: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    snapshot = artifacts.get("snapshot")
    tracking = artifacts.get("tracking")
    snapshot_receipt = artifacts.get("snapshot_receipt")
    tracking_receipt = artifacts.get("tracking_receipt")
    if not isinstance(snapshot, Mapping) or not isinstance(tracking, Mapping):
        raise MemoryIntegrityError("Memory 2.2 projection renderer returned invalid artifacts")
    if not isinstance(snapshot_receipt, Mapping) or not isinstance(tracking_receipt, Mapping):
        raise MemoryIntegrityError("Memory 2.2 projection renderer returned invalid receipts")

    targets = [
        _json_target("memory_snapshot_projection", projection_root / "snapshot.json", snapshot),
    ]
    for relative_name in sorted(str(name) for name in tracking):
        relative_path = Path(relative_name)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise MemoryIntegrityError("Memory 2.2 tracking projection contains an unsafe relative path")
        content = tracking[relative_name]
        if not isinstance(content, str):
            raise MemoryIntegrityError("Memory 2.2 tracking projection content must be text")
        targets.append(
            _text_target(
                "memory_tracking_projection",
                projection_root / relative_path,
                content,
            )
        )
    targets.extend(
        [
            _json_target(
                "memory_snapshot_projection_receipt",
                projection_root / "receipts" / "snapshot.json",
                snapshot_receipt,
            ),
            _json_target(
                "memory_tracking_projection_receipt",
                projection_root / "receipts" / "tracking.json",
                tracking_receipt,
            ),
        ]
    )
    return targets, {
        "snapshot": copy.deepcopy(dict(snapshot_receipt)),
        "tracking": copy.deepcopy(dict(tracking_receipt)),
    }


def _chapter_operations(chapter_index: int, analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    story_state = analysis.get("story_state")
    spatial_state = analysis.get("spatial_state")
    current: dict[str, Any] = {"chapter_index": chapter_index}
    if isinstance(story_state, Mapping):
        current["story_state"] = copy.deepcopy(dict(story_state))
    if isinstance(spatial_state, Mapping):
        current["spatial_state"] = copy.deepcopy(dict(spatial_state))
    operations.append({"op": "update_current_state", "value": current})

    for change in analysis.get("character_changes") or []:
        if not isinstance(change, Mapping) or not str(change.get("name") or "").strip():
            continue
        name = str(change["name"]).strip()
        operations.append(
            {
                "op": "upsert_character",
                "id": _stable_id("character", name),
                "value": {
                    "name": name,
                    "data": {**copy.deepcopy(dict(change)), "chapter_index": chapter_index},
                },
            }
        )
    for location in analysis.get("new_locations") or []:
        if not isinstance(location, str) or not location.strip():
            continue
        name = location.strip()
        operations.append(
            {
                "op": "upsert_location",
                "id": _stable_id("location", name),
                "value": {
                    "name": name,
                    "data": {"first_seen_chapter": chapter_index},
                },
            }
        )
    world_changes = [copy.deepcopy(dict(item)) for item in analysis.get("world_changes") or [] if isinstance(item, Mapping)]
    if world_changes:
        operations.append(
            {
                "op": "update_world",
                "value": {"last_changes": world_changes, "last_changed_chapter": chapter_index},
            }
        )
    if isinstance(story_state, Mapping):
        for thread in story_state.get("open_threads") or []:
            if not str(thread).strip():
                continue
            text = str(thread).strip()
            operations.append(
                {
                    "op": "upsert_open_thread",
                    "id": _stable_id("thread", text),
                    "value": {
                        "title": text,
                        "status": "open",
                        "data": {"chapter_index": chapter_index},
                    },
                }
            )
    operations.append(
        {
            "op": "append_timeline_event",
            "id": f"chapter-{chapter_index:06d}",
            "value": {
                "chapter_index": chapter_index,
                "summary": str(analysis.get("summary") or f"Chapter {chapter_index}"),
                "data": {
                    "events": copy.deepcopy(analysis.get("events") or []),
                    "world_changes": world_changes,
                },
            },
        }
    )
    return operations


def _json_target(kind: str, path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _text_target(
        kind,
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _text_target(kind: str, path: Path, content: str) -> dict[str, Any]:
    before_exists = path.exists()
    before_bytes = path.read_bytes() if before_exists else b""
    return {
        "kind": kind,
        "path": path,
        "content": content,
        "expected_before_exists": before_exists,
        "expected_before_sha256": hashlib.sha256(before_bytes).hexdigest() if before_exists else None,
    }


def _audit(
    *,
    root: Path,
    previous_revision: int,
    projection: Mapping[str, Any],
    patch: Mapping[str, Any],
    batch: Mapping[str, Any] | None,
    checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": "2.1",
        "status": "prepared" if batch is not None else "no_op",
        "memory_root": str(root),
        "previous_revision": previous_revision,
        "revision": int(projection["revision"]),
        "projection_hash": memory_projection_hash(dict(projection)),
        "patch_id": str(patch["patch_id"]),
        "patch_content_hash": memory_patch_content_hash(dict(patch)),
        "batch_id": str(batch["batch_id"]) if batch is not None else None,
        "batch_hash": str(batch["batch_hash"]) if batch is not None else None,
        "event_count": len(batch["events"]) if batch is not None else 0,
        "checkpoint_id": str(checkpoint["checkpoint_id"]) if checkpoint is not None else None,
    }


def _event_authority_audit(
    *,
    root: Path,
    previous_revision: int,
    previous_head_event_hash: str,
    projection: Mapping[str, Any],
    patch: Mapping[str, Any],
    batch: Mapping[str, Any] | None,
    checkpoint: Mapping[str, Any] | None,
    chapter_body_sha256: str,
    projection_receipts: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    receipts = projection_receipts or {}
    snapshot_receipt = receipts.get("snapshot") or {}
    tracking_receipt = receipts.get("tracking") or {}
    return {
        "schema_version": "2.2",
        "reducer_version": CURRENT_REDUCER_VERSION,
        "status": "prepared" if batch is not None else "no_op",
        "memory_root": str(root),
        "book_id": str(projection["book_id"]),
        "authority_epoch": int(projection["authority_epoch"]),
        "previous_revision": previous_revision,
        "revision": int(projection["revision"]),
        "previous_head_event_hash": previous_head_event_hash,
        "head_event_hash": str(projection["head_event_hash"]),
        "projection_hash": memory_projection_hash(dict(projection)),
        "patch_id": str(patch["patch_id"]),
        "patch_content_hash": memory_patch_content_hash(dict(patch)),
        "chapter_body_sha256": chapter_body_sha256,
        "batch_id": str(batch["batch_id"]) if batch is not None else None,
        "batch_hash": str(batch["batch_hash"]) if batch is not None else None,
        "event_count": len(batch["events"]) if batch is not None else 0,
        "checkpoint_id": str(checkpoint["checkpoint_id"]) if checkpoint is not None else None,
        "snapshot_projection_receipt_hash": snapshot_receipt.get("receipt_hash"),
        "tracking_projection_receipt_hash": tracking_receipt.get("receipt_hash"),
    }


def _empty_replay(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "last_batch_hash": None,
        "committed_chapter_count": 0,
        "patch_index": {},
        "quality_state": {},
        "projection": copy.deepcopy(dict(projection)),
    }


def _source_item_count(analysis: Mapping[str, Any]) -> int:
    return sum(
        len(value)
        for value in (
            analysis.get("events") or [],
            analysis.get("character_changes") or [],
            analysis.get("world_changes") or [],
            analysis.get("new_locations") or [],
        )
        if isinstance(value, list)
    ) + 1


def _stable_id(kind: str, value: str) -> str:
    return f"{kind}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _safe_id(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return result or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _require_digest(field: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")


__all__ = [
    "ensure_memory_v2_storage_layout",
    "prepare_chapter_memory_commit",
    "prepare_event_authority_chapter_commit",
]
