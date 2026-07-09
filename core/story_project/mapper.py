from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.project_profile import normalize_project_profile
from core.story_project.loader import load_text
from core.story_project.model import (
    ChapterBlueprint,
    SourcePathSet,
    SourceResolution,
    SourceResolutionEntry,
    StoryProjectRuntimeContext,
)
from core.story_project.paths import resolve_outline, resolve_prose


SETTING_DIR_NAME = "设定"
TRACKING_DIR_NAME = "追踪"
DEFAULT_MAX_FILE_CHARS = 20_000
EXPECTED_TRACKING_FILES = ("上下文.md", "角色状态.md", "伏笔.md", "时间线.md")
SOURCE_PRECEDENCE = (
    "current_outline_or_user_input",
    "tracking_state",
    "previous_prose",
    "settings",
    "snapshot_runtime_cache",
    "memory_sync",
    "model_inference",
)


def build_story_project_runtime_context(
    story_project_root: str | Path,
    chapter_index: int,
    *,
    snapshot: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
) -> StoryProjectRuntimeContext:
    root = Path(story_project_root)
    warnings: list[str] = []
    missing_fields: list[str] = []

    outline_resolution = resolve_outline(root, chapter_index)
    if outline_resolution.path is None:
        raise FileNotFoundError(f"No unique outline file matched chapter {chapter_index}.")
    outline_file = _read_context_file(outline_resolution.path, root=root, max_chars=max_file_chars, warnings=warnings)
    chapter_blueprint = _build_chapter_blueprint(
        chapter_index=chapter_index,
        outline_path=outline_resolution.path,
        outline_text=str(outline_file["text"]),
    )
    missing_fields.extend(chapter_blueprint.missing_fields)

    previous_prose = _read_previous_prose(
        root,
        chapter_index=chapter_index,
        max_file_chars=max_file_chars,
        warnings=warnings,
    )
    tracking_files = _read_markdown_tree(root / TRACKING_DIR_NAME, root=root, max_chars=max_file_chars, warnings=warnings)
    setting_files = _read_markdown_tree(root / SETTING_DIR_NAME, root=root, max_chars=max_file_chars, warnings=warnings)
    _record_missing_tracking_files(tracking_files, warnings=warnings, missing_fields=missing_fields)

    source_paths = SourcePathSet(
        story_project_root=root,
        outline_path=outline_resolution.path,
        previous_prose_path=Path(previous_prose["path"]) if previous_prose and previous_prose.get("path") else None,
        tracking_paths={name: Path(file_data["path"]) for name, file_data in tracking_files.items()},
        setting_paths={name: Path(file_data["path"]) for name, file_data in setting_files.items()},
    )
    snapshot_overlay = _build_snapshot_overlay(
        root=root,
        chapter_index=chapter_index,
        snapshot=snapshot,
        source_paths=source_paths,
    )
    memory_context_overlay = _build_memory_context_overlay(
        outline=outline_file,
        previous_prose=previous_prose,
        tracking_files=tracking_files,
        setting_files=setting_files,
    )
    source_resolution = _build_source_resolution(
        chapter_index=chapter_index,
        snapshot=snapshot,
        memory_context=memory_context,
        tracking_files=tracking_files,
        setting_files=setting_files,
        previous_prose=previous_prose,
    )

    return StoryProjectRuntimeContext(
        story_project_root=root,
        chapter_index=chapter_index,
        outline=outline_file,
        previous_prose=previous_prose,
        tracking_files=tracking_files,
        setting_files=setting_files,
        snapshot_overlay=snapshot_overlay,
        memory_context_overlay=memory_context_overlay,
        chapter_blueprint=chapter_blueprint,
        source_paths=source_paths,
        source_resolution=source_resolution,
        warnings=tuple(warnings),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
    )


def _read_previous_prose(
    root: Path,
    *,
    chapter_index: int,
    max_file_chars: int,
    warnings: list[str],
) -> dict[str, Any] | None:
    if chapter_index <= 1:
        return None
    resolution = resolve_prose(root, chapter_index - 1)
    if resolution.conflict:
        warnings.append(f"previous_prose_conflict: multiple prose files matched chapter {chapter_index - 1}")
        return None
    if resolution.path is None:
        warnings.append(f"previous_prose_missing: no prose file matched chapter {chapter_index - 1}")
        return None
    return _read_context_file(resolution.path, root=root, max_chars=max_file_chars, warnings=warnings)


def _read_markdown_tree(directory: Path, *, root: Path, max_chars: int, warnings: list[str]) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        return {}
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.rglob("*.md"), key=lambda item: _relative_path(item, root)):
        if not path.is_file():
            continue
        files[_relative_path(path, directory)] = _read_context_file(path, root=root, max_chars=max_chars, warnings=warnings)
    return files


def _read_context_file(path: Path, *, root: Path, max_chars: int, warnings: list[str]) -> dict[str, Any]:
    text = load_text(path)
    truncated = len(text) > max_chars
    if truncated:
        warnings.append(f"truncated_file: {_relative_path(path, root)} exceeded {max_chars} chars")
        text = text[:max_chars]
    return {
        "path": str(path),
        "relative_path": _relative_path(path, root),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def _build_chapter_blueprint(*, chapter_index: int, outline_path: Path, outline_text: str) -> ChapterBlueprint:
    title = _extract_title(outline_text, outline_path)
    core_event = _extract_labeled_value(
        outline_text,
        ("core_event", "核心事件", "核心剧情", "本章核心", "主要事件"),
    )
    required_beats = tuple(
        {"index": index, "text": text}
        for index, text in enumerate(
            _extract_list_under_heading(outline_text, ("required_beats", "必写节拍", "剧情节拍", "节拍", "关键情节")),
            start=1,
        )
    )
    ending_pressure = _extract_labeled_value(
        outline_text,
        ("ending_pressure", "结尾压力", "章尾压力", "下一章推动力", "结尾钩子", "章尾钩子"),
    )
    missing_fields: list[str] = []
    if not core_event:
        missing_fields.append("core_event")
    if not required_beats:
        missing_fields.append("required_beats")
    if not ending_pressure:
        missing_fields.append("ending_pressure")
    return ChapterBlueprint(
        chapter_index=chapter_index,
        outline_path=outline_path,
        title=title,
        core_event=core_event,
        required_beats=required_beats,
        ending_pressure=ending_pressure,
        source_path=outline_path,
        missing_fields=tuple(missing_fields),
    )


def _extract_title(text: str, outline_path: Path) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    stem = outline_path.stem
    title = re.sub(r"^细纲_第0*\d+章_?", "", stem).strip()
    return title or stem


def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    lines = text.splitlines()
    label_pattern = "|".join(re.escape(label) for label in labels)
    inline_pattern = re.compile(rf"^\s*(?:[-*]\s*)?(?:\*\*)?(?:{label_pattern})(?:\*\*)?\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
    heading_pattern = re.compile(rf"^\s*#+\s*(?:{label_pattern})\s*$", re.IGNORECASE)
    for index, line in enumerate(lines):
        inline = inline_pattern.match(line)
        if inline and inline.group(1).strip():
            return inline.group(1).strip()
        if heading_pattern.match(line):
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    break
                return _strip_list_marker(stripped)
    return None


def _extract_list_under_heading(text: str, headings: tuple[str, ...]) -> list[str]:
    lines = text.splitlines()
    heading_pattern = re.compile("|".join(re.escape(heading) for heading in headings), re.IGNORECASE)
    in_section = False
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and heading_pattern.search(stripped):
            in_section = True
            continue
        if in_section and stripped.startswith("#"):
            break
        if not in_section:
            continue
        if not stripped:
            continue
        if _is_list_item(stripped):
            items.append(_strip_list_marker(stripped))
    return items


def _is_list_item(text: str) -> bool:
    return bool(re.match(r"^(?:[-*+]|\d+[.)、])\s+", text))


def _strip_list_marker(text: str) -> str:
    return re.sub(r"^(?:[-*+]|\d+[.)、])\s+", "", text).strip()


def _record_missing_tracking_files(
    tracking_files: dict[str, dict[str, Any]],
    *,
    warnings: list[str],
    missing_fields: list[str],
) -> None:
    present_names = {Path(name).name for name in tracking_files}
    for expected in EXPECTED_TRACKING_FILES:
        if expected not in present_names:
            warnings.append(f"tracking_file_missing: {expected}")
            missing_fields.append(f"tracking_files.{expected}")


def _build_snapshot_overlay(
    *,
    root: Path,
    chapter_index: int,
    snapshot: dict[str, Any] | None,
    source_paths: SourcePathSet,
) -> dict[str, Any]:
    profile = normalize_project_profile(snapshot)
    language = profile.get("language") or "zh-CN"
    return {
        "chapter_index": chapter_index,
        "project_profile": {
            **profile,
            "language": language,
        },
        "story_project": {
            "root": str(root),
            "chapter_index": chapter_index,
            "outline_path": str(source_paths.outline_path),
            "previous_prose_path": str(source_paths.previous_prose_path) if source_paths.previous_prose_path else None,
            "tracking_paths": {name: str(path) for name, path in sorted(source_paths.tracking_paths.items())},
            "setting_paths": {name: str(path) for name, path in sorted(source_paths.setting_paths.items())},
        },
    }


def _build_memory_context_overlay(
    *,
    outline: dict[str, Any],
    previous_prose: dict[str, Any] | None,
    tracking_files: dict[str, dict[str, Any]],
    setting_files: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    source_mappings: list[dict[str, Any]] = []
    _append_memory_item(items, source_mappings, "story_state", "current_outline", outline)
    if previous_prose is not None:
        _append_memory_item(items, source_mappings, "timeline_event", "previous_prose", previous_prose)
    for name, file_data in sorted(tracking_files.items()):
        _append_memory_item(items, source_mappings, "story_state", f"tracking:{name}", file_data)
    for name, file_data in sorted(setting_files.items()):
        _append_memory_item(items, source_mappings, "world_state", f"setting:{name}", file_data)
    return {
        "source": "story_project",
        "status": "ready",
        "items": items,
        "source_mappings": source_mappings,
    }


def _append_memory_item(
    items: list[dict[str, Any]],
    source_mappings: list[dict[str, Any]],
    item_type: str,
    name: str,
    file_data: dict[str, Any],
) -> None:
    index = len(items)
    item = {
        "type": item_type,
        "name": name,
        "id": f"story_project:{name}",
        "source": "story_project",
        "path": file_data.get("path"),
        "text": file_data.get("text"),
        "summary": _first_non_empty_line(str(file_data.get("text") or "")),
        "data": {
            "source": "story_project",
            "path": file_data.get("path"),
            "relative_path": file_data.get("relative_path"),
            "text": file_data.get("text"),
            "summary": _first_non_empty_line(str(file_data.get("text") or "")),
            "truncated": bool(file_data.get("truncated")),
        },
    }
    items.append(item)
    source_mappings.append(
        {
            "index": index,
            "source": "story_project",
            "memory_id": item["id"],
            "type": item_type,
            "name": name,
            "path": str(file_data.get("path")),
        }
    )


def _build_source_resolution(
    *,
    chapter_index: int,
    snapshot: dict[str, Any] | None,
    memory_context: dict[str, Any] | None,
    tracking_files: dict[str, dict[str, Any]],
    setting_files: dict[str, dict[str, Any]],
    previous_prose: dict[str, Any] | None,
) -> SourceResolution:
    entries: list[SourceResolutionEntry] = [
        SourceResolutionEntry(
            field="chapter_blueprint",
            chosen_source="current_outline",
            discarded_sources=(),
            reason="current_outline_precedes_runtime_cache",
        )
    ]
    if tracking_files:
        entries.append(
            SourceResolutionEntry(
                field="tracking_context",
                chosen_source="tracking_files",
                discarded_sources=_available_lower_sources(snapshot=snapshot, memory_context=memory_context),
                reason="tracking_state_precedes_snapshot_and_memory",
            )
        )
    if previous_prose is not None:
        entries.append(
            SourceResolutionEntry(
                field="previous_chapter",
                chosen_source="previous_prose",
                discarded_sources=_available_lower_sources(snapshot=snapshot, memory_context=memory_context),
                reason="latest_prose_precedes_runtime_cache",
            )
        )
    if setting_files:
        entries.append(
            SourceResolutionEntry(
                field="setting_context",
                chosen_source="setting_files",
                discarded_sources=_available_lower_sources(snapshot=snapshot, memory_context=memory_context),
                reason="settings_precede_runtime_cache",
            )
        )
    if isinstance(snapshot, dict) and snapshot.get("chapter_index") not in {None, chapter_index}:
        entries.append(
            SourceResolutionEntry(
                field="chapter_index",
                chosen_source="story_project_cli",
                discarded_sources=("snapshot.json",),
                reason="explicit_story_project_chapter_precedes_snapshot",
            )
        )
    return SourceResolution(precedence=SOURCE_PRECEDENCE, entries=tuple(entries))


def _available_lower_sources(*, snapshot: dict[str, Any] | None, memory_context: dict[str, Any] | None) -> tuple[str, ...]:
    sources: list[str] = []
    if isinstance(snapshot, dict):
        sources.append("snapshot.json")
    if isinstance(memory_context, dict):
        sources.append("memory")
    if sources:
        sources.append("model_inference")
    return tuple(sources)


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
