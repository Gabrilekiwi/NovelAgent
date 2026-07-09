from __future__ import annotations

import re
from pathlib import Path

from core.story_project.model import CORE_DIRECTORY_NAMES, PathResolution, StoryProjectRootResolution


ACTIVE_BOOK_FILENAME = ".active-book"
OUTLINE_DIR_NAME = "大纲"
OUTLINE_PREFIX = "细纲"
PROSE_DIR_NAME = "正文"
UNTITLED_CHAPTER = "无题"

_OUTLINE_RE = re.compile(r"^细纲_第(0*[1-9]\d*)章(?:_.+)?\.md$")
_PROSE_RE = re.compile(r"^第(0*[1-9]\d*)章(?:_.+)?\.md$")
_WINDOWS_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def read_active_book_path(workspace_root: str | Path | None = None) -> Path | None:
    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    active_book_path = root / ACTIVE_BOOK_FILENAME
    if not active_book_path.exists():
        return None
    first_line = active_book_path.read_text(encoding="utf-8-sig").splitlines()
    if not first_line:
        return None
    raw_path = first_line[0].strip()
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate


def resolve_story_project_root(
    requested: str | Path | None,
    *,
    workspace_root: str | Path | None = None,
) -> StoryProjectRootResolution:
    root = Path.cwd() if workspace_root is None else Path(workspace_root)
    requested_text = "auto" if requested is None else str(requested)
    active_book_file = root / ACTIVE_BOOK_FILENAME

    if requested_text != "auto":
        candidate = Path(requested_text)
        if not candidate.is_absolute():
            candidate = root / candidate
        return _existing_root_resolution(requested_text, candidate, "explicit", None)

    active_book_target = read_active_book_path(root)
    if active_book_target is not None:
        return _existing_root_resolution(
            requested_text,
            active_book_target,
            "active_book",
            active_book_file,
        )

    discovered = _discover_story_project_root(root)
    if discovered is not None:
        return StoryProjectRootResolution(
            requested=requested_text,
            root=discovered,
            source="auto_discovery",
            active_book_path=None,
        )

    return StoryProjectRootResolution(
        requested=requested_text,
        root=None,
        source="auto_discovery",
        active_book_path=None,
        error="StoryProject root could not be discovered.",
    )


def canonical_outline_path(story_project_root: str | Path, chapter_index: int) -> Path:
    return Path(story_project_root) / OUTLINE_DIR_NAME / f"{OUTLINE_PREFIX}_第{_format_chapter(chapter_index)}章.md"


def canonical_prose_path(story_project_root: str | Path, chapter_index: int, title: str | None = None) -> Path:
    safe_title = _sanitize_title(title)
    return Path(story_project_root) / PROSE_DIR_NAME / f"第{_format_chapter(chapter_index)}章_{safe_title}.md"


def resolve_outline(story_project_root: str | Path, chapter_index: int) -> PathResolution:
    root = Path(story_project_root)
    candidates = tuple(_matching_chapter_paths(root / OUTLINE_DIR_NAME, _OUTLINE_RE, chapter_index))
    path = candidates[0] if len(candidates) == 1 else None
    return PathResolution(
        chapter_index=chapter_index,
        path=path,
        candidates=candidates,
        canonical_path=canonical_outline_path(root, chapter_index),
    )


def resolve_prose(story_project_root: str | Path, chapter_index: int) -> PathResolution:
    root = Path(story_project_root)
    candidates = tuple(_matching_chapter_paths(root / PROSE_DIR_NAME, _PROSE_RE, chapter_index))
    path = candidates[0] if len(candidates) == 1 else None
    return PathResolution(
        chapter_index=chapter_index,
        path=path,
        candidates=candidates,
        canonical_path=canonical_prose_path(root, chapter_index, UNTITLED_CHAPTER),
    )


def scan_prose_chapters(story_project_root: str | Path) -> dict[int, tuple[Path, ...]]:
    prose_dir = Path(story_project_root) / PROSE_DIR_NAME
    chapters: dict[int, list[Path]] = {}
    if not prose_dir.is_dir():
        return {}
    for path in sorted(prose_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        chapter_index = _chapter_index_from_name(path.name, _PROSE_RE)
        if chapter_index is None:
            continue
        chapters.setdefault(chapter_index, []).append(path)
    return {chapter: tuple(paths) for chapter, paths in chapters.items()}


def infer_next_chapter(story_project_root: str | Path) -> int:
    chapters = set(scan_prose_chapters(story_project_root))
    next_chapter = 1
    while next_chapter in chapters:
        next_chapter += 1
    return next_chapter


def outline_chapter_index(path: str | Path) -> int | None:
    return _chapter_index_from_name(Path(path).name, _OUTLINE_RE)


def prose_chapter_index(path: str | Path) -> int | None:
    return _chapter_index_from_name(Path(path).name, _PROSE_RE)


def _existing_root_resolution(
    requested: str,
    candidate: Path,
    source: str,
    active_book_path: Path | None,
) -> StoryProjectRootResolution:
    if not candidate.exists():
        return StoryProjectRootResolution(
            requested=requested,
            root=candidate,
            source=source,
            active_book_path=active_book_path,
            error=f"StoryProject root does not exist: {candidate}",
        )
    if not candidate.is_dir():
        return StoryProjectRootResolution(
            requested=requested,
            root=candidate,
            source=source,
            active_book_path=active_book_path,
            error=f"StoryProject root is not a directory: {candidate}",
        )
    return StoryProjectRootResolution(
        requested=requested,
        root=candidate,
        source=source,
        active_book_path=active_book_path,
    )


def _discover_story_project_root(workspace_root: Path) -> Path | None:
    if _has_core_directories(workspace_root):
        return workspace_root
    try:
        children = sorted(workspace_root.iterdir(), key=lambda item: item.name)
    except OSError:
        return None
    for child in children:
        if child.is_dir() and _has_core_directories(child):
            return child
    return None


def _has_core_directories(candidate: Path) -> bool:
    return all((candidate / name).is_dir() for name in CORE_DIRECTORY_NAMES)


def _matching_chapter_paths(directory: Path, pattern: re.Pattern[str], chapter_index: int) -> list[Path]:
    if not directory.is_dir():
        return []
    matches: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        matched_index = _chapter_index_from_name(path.name, pattern)
        if matched_index == chapter_index:
            matches.append(path)
    return matches


def _chapter_index_from_name(name: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.match(name)
    if match is None:
        return None
    return int(match.group(1))


def _format_chapter(chapter_index: int) -> str:
    if not isinstance(chapter_index, int) or chapter_index < 1:
        raise ValueError("chapter_index must be a positive integer")
    return f"{chapter_index:03d}"


def _sanitize_title(title: str | None) -> str:
    text = (title or UNTITLED_CHAPTER).strip()
    if not text:
        text = UNTITLED_CHAPTER
    text = _WINDOWS_UNSAFE_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or UNTITLED_CHAPTER
