from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_DIR = Path(".tmp/runtime")
DEFAULT_SNAPSHOT_PATH = DEFAULT_RUNTIME_DIR / "snapshot.json"
DEFAULT_MEMORY_PATH = DEFAULT_RUNTIME_DIR / "notion_memory.json"
DEFAULT_RUN_DIR = DEFAULT_RUNTIME_DIR / "runs"
DEFAULT_CHAPTER_DIR = DEFAULT_RUNTIME_DIR / "chapters"
DEFAULT_MEMORY_OUTBOX = DEFAULT_RUNTIME_DIR / "memory_outbox.jsonl"

SNAPSHOT_EXAMPLE_PATH = Path("data/snapshot.example.json")
NOTION_MEMORY_EXAMPLE_PATH = Path("data/notion_memory.example.json")


def init_runtime_state(
    *,
    snapshot_source: str | Path = SNAPSHOT_EXAMPLE_PATH,
    memory_source: str | Path = NOTION_MEMORY_EXAMPLE_PATH,
    snapshot_target: str | Path = DEFAULT_SNAPSHOT_PATH,
    memory_target: str | Path = DEFAULT_MEMORY_PATH,
    overwrite: bool = False,
) -> dict[str, Any]:
    runtime_dir = Path(snapshot_target).parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "runs").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "chapters").mkdir(parents=True, exist_ok=True)

    copied = []
    skipped = []
    for source, target, label in (
        (Path(snapshot_source), Path(snapshot_target), "snapshot"),
        (Path(memory_source), Path(memory_target), "memory"),
    ):
        if not source.exists():
            raise FileNotFoundError(f"{label} example not found: {source}")
        if target.exists() and not overwrite:
            skipped.append({"name": label, "path": str(target), "reason": "exists"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append({"name": label, "source": str(source), "path": str(target)})

    return {
        "runtime_dir": str(runtime_dir),
        "snapshot_path": str(snapshot_target),
        "memory_path": str(memory_target),
        "run_dir": str(runtime_dir / "runs"),
        "chapter_dir": str(runtime_dir / "chapters"),
        "copied": copied,
        "skipped": skipped,
    }
