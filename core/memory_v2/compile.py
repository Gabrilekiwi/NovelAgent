from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.memory_v2.events import append_memory_events
from core.memory_v2.importer_v1 import import_v1_memory_file_to_patch
from core.memory_v2.models import create_empty_canonical_memory
from core.memory_v2.reducer import apply_memory_patch
from core.memory_v2.snapshot_adapter import canonical_memory_to_snapshot
from core.memory_v2.storage import load_canonical_memory, save_canonical_memory
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
    patch = import_v1_memory_file_to_patch(source_path)

    if not reset and paths["canonical_memory"].exists():
        canonical_memory = load_canonical_memory(paths["canonical_memory"])
    else:
        canonical_memory = create_empty_canonical_memory(book_id=book_id, title=title, language=language)

    previous_revision = int(canonical_memory.get("revision") or 1)
    updated_memory, events = apply_memory_patch(canonical_memory, patch)
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
        snapshot_preview=snapshot_preview,
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        save_canonical_memory(paths["canonical_memory"], updated_memory)
        append_memory_events(paths["memory_events"], events)
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
    snapshot_preview: dict[str, Any],
) -> dict[str, Any]:
    operation_types = _count_by_key(patch.get("operations", []), "op")
    event_revisions = [event["revision"] for event in events if isinstance(event.get("revision"), int)]
    report = {
        "schema_version": "2.0",
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
        "memory_events": output_dir / "memory_events.jsonl",
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


__all__ = [
    "MemoryCompileError",
    "compile_memory_v2",
    "validate_memory_compile_report",
]
