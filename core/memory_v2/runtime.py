from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from core.memory_v2.event_store import (
    CHECKPOINT_CHAPTER_INTERVAL,
    MemoryPatchConflictError,
    create_memory_checkpoint,
    create_memory_event_batch,
    memory_patch_content_hash,
    memory_projection_hash,
    replay_memory_events,
)
from core.memory_v2.models import create_empty_canonical_memory
from core.memory_v2.patch import create_memory_patch
from core.memory_v2.reducer import apply_memory_patch
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
    before_exists = path.exists()
    before_bytes = path.read_bytes() if before_exists else b""
    return {
        "kind": kind,
        "path": path,
        "content": json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
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


__all__ = ["ensure_memory_v2_storage_layout", "prepare_chapter_memory_commit"]
