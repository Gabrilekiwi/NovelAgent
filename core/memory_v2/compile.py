from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import (
    MemoryEventStoreError,
    MemoryPatchConflictError,
    commit_memory_patch,
    memory_patch_content_hash,
    replay_memory_events,
)
from core.memory_v2.importer_v1 import import_v1_memory_file_to_patch
from core.memory_v2.models import create_empty_canonical_memory
from core.memory_v2.reducer import apply_memory_patch
from core.memory_v2.snapshot_adapter import canonical_memory_to_snapshot
from core.memory_v2.storage import load_canonical_memory
from core.schema import SchemaValidationError, validate_schema
from core.state.snapshot import validate_snapshot


class MemoryCompileError(ValueError):
    pass


def compile_memory_v2(
    *,
    memory_path: str | Path,
    output_dir: str | Path,
    book_id: str = "default",
    title: str = "Untitled",
    language: str = "zh-CN",
    reset: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    source_path = Path(memory_path)
    if not source_path.exists():
        raise MemoryCompileError(f"memory file not found: {source_path}")

    out_dir = Path(output_dir)
    paths = _output_paths(out_dir)
    source_project_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    patch = import_v1_memory_file_to_patch(
        source_path,
        patch_id=f"patch_source_sync_{source_project_digest}",
    )
    has_batches = any((paths["memory_events"] / "batches").glob("*.json"))
    if reset and has_batches:
        raise MemoryCompileError(
            "reset cannot replace an immutable Memory 2.1 event history; use a new output directory"
        )

    context_digest = canonical_json_hash(
        {
            "book_id": book_id,
            "title": title,
            "language": language,
            "source_project_digest": source_project_digest,
        }
    )
    initial_memory = _initial_memory(
        paths=paths,
        book_id=book_id,
        title=title,
        language=language,
        reset=reset,
        has_batches=has_batches,
    )

    try:
        if dry_run:
            canonical_memory, previous_revision, updated_memory, events, apply_status = _preview_patch(
                paths=paths,
                patch=patch,
                initial_memory=initial_memory,
                has_batches=has_batches,
            )
            batch = None
        else:
            result = commit_memory_patch(
                store_dir=paths["memory_events"],
                canonical_path=paths["canonical_memory"],
                patch=patch,
                source_project_digest=source_project_digest,
                context_digest=context_digest,
                initial_memory=initial_memory,
                batch_kind="source_sync",
            )
            canonical_memory = initial_memory
            previous_revision = int(result["previous_revision"])
            updated_memory = result["projection"]
            events = result["events"]
            apply_status = str(result["status"])
            batch = result["batch"]
    except MemoryEventStoreError as exc:
        raise MemoryCompileError(str(exc)) from exc

    snapshot_preview = validate_snapshot(canonical_memory_to_snapshot(updated_memory))
    report = _build_report(
        memory_path=source_path,
        output_dir=out_dir,
        paths=paths,
        reset=reset,
        dry_run=dry_run,
        patch=patch,
        previous_revision=previous_revision,
        canonical_memory=updated_memory,
        events=events,
        apply_status=apply_status,
        batch=batch,
        snapshot_preview=snapshot_preview,
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(paths["memory_patch"], patch)
        _write_json(paths["snapshot_preview"], snapshot_preview)
        _write_json(paths["memory_compile_report"], report)

    return report


def validate_memory_compile_report(report: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_schema(report, "memory_compile_report.schema.json")
    except SchemaValidationError as exc:
        raise MemoryCompileError(str(exc)) from exc


def _build_report(
    *,
    memory_path: Path,
    output_dir: Path,
    paths: dict[str, Path],
    reset: bool,
    dry_run: bool,
    patch: dict[str, Any],
    previous_revision: int,
    canonical_memory: dict[str, Any],
    events: list[dict[str, Any]],
    apply_status: str,
    batch: dict[str, Any] | None,
    snapshot_preview: dict[str, Any],
) -> dict[str, Any]:
    operation_types = _count_by_key(patch.get("operations", []), "op")
    event_revisions = [event["revision"] for event in events if isinstance(event.get("revision"), int)]
    report = {
        "schema_version": "2.1",
        "status": "ok",
        "memory_path": str(memory_path),
        "output_dir": str(output_dir),
        "reset": bool(reset),
        "dry_run": bool(dry_run),
        "input": {
            "source_kind": patch.get("source", {}).get("kind", "local_memory"),
            "source_path": str(memory_path),
        },
        "patch": {
            "patch_id": patch.get("patch_id"),
            "apply_status": apply_status,
            "operation_count": len(patch.get("operations", [])),
            "operation_types": operation_types,
        },
        "canonical_memory": {
            "path": str(paths["canonical_memory"]),
            "previous_revision": previous_revision,
            "revision": int(canonical_memory.get("revision") or previous_revision),
            "character_count": len(canonical_memory.get("characters") or {}),
            "location_count": len(canonical_memory.get("locations") or {}),
            "timeline_count": len(canonical_memory.get("timeline") or []),
            "constraint_count": len(canonical_memory.get("constraints") or []),
            "open_thread_count": len(canonical_memory.get("open_threads") or []),
        },
        "events": {
            "path": str(paths["memory_events"]),
            "event_count": len(events),
            "first_revision": min(event_revisions) if event_revisions else 0,
            "last_revision": max(event_revisions) if event_revisions else previous_revision,
            "batch_id": batch.get("batch_id") if isinstance(batch, dict) else None,
            "batch_hash": batch.get("batch_hash") if isinstance(batch, dict) else None,
        },
        "snapshot_preview": {
            "path": str(paths["snapshot_preview"]),
            "chapter_index": int(snapshot_preview.get("chapter_index") or 1),
            "character_count": len(snapshot_preview.get("characters") or {}),
            "space_count": len((snapshot_preview.get("spatial_state") or {}).get("spaces") or {}),
            "open_thread_count": len((snapshot_preview.get("story_state") or {}).get("open_threads") or []),
        },
        "outputs": {key: str(path) for key, path in paths.items()},
    }
    return validate_memory_compile_report(report)


def _output_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "canonical_memory": output_dir / "canonical_memory.json",
        "memory_events": output_dir / "memory_events",
        "memory_patch": output_dir / "memory_patch.json",
        "snapshot_preview": output_dir / "snapshot_preview.json",
        "memory_compile_report": output_dir / "memory_compile_report.json",
    }


def _count_by_key(items: Any, key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if isinstance(value, str) and value:
            counts[value] = counts.get(value, 0) + 1
    return {name: counts[name] for name in sorted(counts)}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _initial_memory(
    *,
    paths: dict[str, Path],
    book_id: str,
    title: str,
    language: str,
    reset: bool,
    has_batches: bool,
) -> dict[str, Any]:
    if has_batches:
        return replay_memory_events(paths["memory_events"])["projection"]
    if not reset and paths["canonical_memory"].exists():
        return load_canonical_memory(paths["canonical_memory"])
    return create_empty_canonical_memory(book_id=book_id, title=title, language=language)


def _preview_patch(
    *,
    paths: dict[str, Path],
    patch: dict[str, Any],
    initial_memory: dict[str, Any],
    has_batches: bool,
) -> tuple[dict[str, Any], int, dict[str, Any], list[dict[str, Any]], str]:
    base = initial_memory
    previous_revision = int(base["revision"])
    if has_batches:
        replay = replay_memory_events(paths["memory_events"])
        existing_hash = replay["patch_index"].get(str(patch["patch_id"]))
        if existing_hash is not None:
            if existing_hash != memory_patch_content_hash(patch):
                raise MemoryPatchConflictError(
                    f"patch id {patch['patch_id']} already exists with different content"
                )
            return base, previous_revision, base, [], "no_op"
    updated, events = apply_memory_patch(base, patch)
    return base, previous_revision, updated, events, "applied"


__all__ = [
    "MemoryCompileError",
    "compile_memory_v2",
    "validate_memory_compile_report",
]
