from __future__ import annotations

import shutil
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path
    snapshot_path: Path
    run_dir: Path
    chapter_dir: Path
    review_dir: Path
    persistence_dir: Path
    delivery_dir: Path
    memory_dir: Path

    @classmethod
    def for_story_project(cls, story_project_root: str | Path) -> "RuntimePaths":
        runtime_dir = Path(story_project_root).resolve() / ".novelagent" / "runtime"
        return cls(
            runtime_dir=runtime_dir,
            snapshot_path=runtime_dir / "snapshot.json",
            run_dir=runtime_dir / "runs",
            chapter_dir=runtime_dir / "chapters",
            review_dir=runtime_dir / "reviews",
            persistence_dir=runtime_dir / "persistence",
            delivery_dir=runtime_dir / "deliveries",
            memory_dir=runtime_dir / "memory",
        )

    @classmethod
    def legacy_default(cls) -> "RuntimePaths":
        return cls(
            runtime_dir=DEFAULT_RUNTIME_DIR,
            snapshot_path=DEFAULT_SNAPSHOT_PATH,
            run_dir=DEFAULT_RUN_DIR,
            chapter_dir=DEFAULT_CHAPTER_DIR,
            review_dir=DEFAULT_RUNTIME_DIR / "reviews",
            persistence_dir=DEFAULT_RUN_DIR / "transactions",
            delivery_dir=DEFAULT_RUNTIME_DIR / "deliveries",
            memory_dir=DEFAULT_RUNTIME_DIR,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "runtime_dir": str(self.runtime_dir),
            "snapshot_path": str(self.snapshot_path),
            "run_dir": str(self.run_dir),
            "chapter_dir": str(self.chapter_dir),
            "review_dir": str(self.review_dir),
            "persistence_dir": str(self.persistence_dir),
            "delivery_dir": str(self.delivery_dir),
            "memory_dir": str(self.memory_dir),
        }

    def root_map(self, story_project_root: str | Path) -> dict[str, Path]:
        return {
            "story_project": Path(story_project_root).resolve(),
            "runtime": self.runtime_dir.resolve(),
            "snapshot": self.snapshot_path.parent.resolve(),
            "chapter_artifacts": self.chapter_dir.resolve(),
            "delivery_store": self.delivery_dir.resolve(),
        }


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
