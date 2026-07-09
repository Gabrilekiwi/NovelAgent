from __future__ import annotations

from pathlib import Path
from typing import Any

from core.story_project.paths import resolve_outline, resolve_prose


def load_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def load_outline(story_project_root: str | Path, chapter_index: int) -> dict[str, Any]:
    resolution = resolve_outline(story_project_root, chapter_index)
    return {
        "resolution": resolution.to_dict(),
        "text": load_text(resolution.path) if resolution.path else None,
    }


def load_prose(story_project_root: str | Path, chapter_index: int) -> dict[str, Any]:
    resolution = resolve_prose(story_project_root, chapter_index)
    return {
        "resolution": resolution.to_dict(),
        "text": load_text(resolution.path) if resolution.path else None,
    }


def load_tracking_files(story_project_root: str | Path) -> dict[str, str]:
    tracking_dir = Path(story_project_root) / "追踪"
    if not tracking_dir.is_dir():
        return {}
    files: dict[str, str] = {}
    for path in sorted(tracking_dir.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.suffix.lower() == ".md":
            files[path.name] = load_text(path)
    return files
