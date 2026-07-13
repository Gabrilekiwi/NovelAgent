from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from core.schema import validate_schema
from core.story_project.identity import ProjectIdentity, project_identity_for_operation
from core.story_project.loader import load_text
from core.story_project.managed_block import ManagedBlockError, parse_managed_block
from core.story_project.paths import resolve_outline, resolve_prose
from core.story_project.semantic_contracts import validate_story_project_semantic_state


SEMANTIC_PARSER_VERSION = "shadow-1.0"
SHADOW_REPORT_SCHEMA_VERSION = "1.0"
TRACKING_FILES = ("上下文.md", "角色状态.md", "伏笔.md", "时间线.md")
MANAGED_START = "<!-- NovelAgent:semantic-state"
MANAGED_END = "<!-- /NovelAgent:semantic-state -->"


class StoryProjectSemanticParseError(ValueError):
    pass


def parse_story_project_semantic_state(
    story_project_root: str | Path,
    chapter_index: int,
    *,
    project_identity: ProjectIdentity | None = None,
) -> dict[str, Any]:
    root = Path(story_project_root).resolve()
    if not root.is_dir():
        raise StoryProjectSemanticParseError(f"StoryProject root is not a directory: {root}")
    if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
        raise StoryProjectSemanticParseError("chapter_index must be a positive integer")
    identity = project_identity or project_identity_for_operation(root, persist=False)
    sources = _load_sources(root, chapter_index)
    builder = _SemanticStateBuilder(
        root=root,
        chapter_index=chapter_index,
        book_id=identity.book_id,
        layout_profile_version=_detect_layout_profile(sources),
        sources=sources,
    )
    builder.parse()
    return validate_story_project_semantic_state(builder.state())


def build_story_project_shadow_report(
    state: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validated = validate_story_project_semantic_state(state)
    differences = _semantic_snapshot_differences(validated, snapshot or {})
    blockers = ["shadow_mode_not_activated", "target_book_calibration_not_proven"]
    if any(conflict.get("blocking") for conflict in validated["conflicts"]):
        blockers.append("blocking_semantic_conflict")
    report = {
        "schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "mode": "shadow",
        "authoritative": False,
        "affects_generation": False,
        "affects_snapshot": False,
        "book_id": validated["book_id"],
        "chapter_index": validated["chapter_index"],
        "state": validated,
        "differences": differences,
        "blocking_conflict_count": sum(1 for item in validated["conflicts"] if item.get("blocking")),
        "warning_count": len(validated["parse_warnings"]),
        "strict_eligible": False,
        "strict_blockers": blockers,
    }
    return validate_schema(report, "story_project_shadow_report.schema.json")


class _SemanticStateBuilder:
    def __init__(
        self,
        *,
        root: Path,
        chapter_index: int,
        book_id: str,
        layout_profile_version: str,
        sources: dict[str, dict[str, Any]],
    ) -> None:
        self.root = root
        self.chapter_index = chapter_index
        self.book_id = book_id
        self.layout_profile_version = layout_profile_version
        self.sources = sources
        self.story_state: dict[str, Any] = {}
        self.world_state: dict[str, Any] = {}
        self.spatial_state: dict[str, Any] = {"character_positions": {}}
        self.characters: dict[str, Any] = {}
        self.timeline: list[dict[str, Any]] = []
        self.constraints: list[dict[str, Any]] = []
        self.foreshadowing: list[dict[str, Any]] = []
        self.provenance: list[dict[str, Any]] = []
        self.conflicts: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.unsupported: list[dict[str, Any]] = []

    def parse(self) -> None:
        self._parse_outline_identity()
        self._parse_context()
        self._parse_characters()
        self._parse_foreshadowing()
        self._parse_timeline()
        self._parse_settings()
        self._parse_managed_projections()
        for name in TRACKING_FILES:
            key = f"追踪/{name}"
            if key not in self.sources:
                self._warn("tracking_file_missing", key, f"Tracking file is missing: {name}")

    def state(self) -> dict[str, Any]:
        if not self.spatial_state["character_positions"]:
            self.spatial_state = {}
        return {
            "schema_version": "1.0",
            "book_id": self.book_id,
            "chapter_index": self.chapter_index,
            "story_state": self.story_state,
            "world_state": self.world_state,
            "spatial_state": self.spatial_state,
            "characters": self.characters,
            "timeline": self.timeline,
            "constraints": self.constraints,
            "foreshadowing": self.foreshadowing,
            "provenance": self.provenance,
            "conflicts": self.conflicts,
            "parse_warnings": self.warnings,
            "unsupported_excerpts": self.unsupported,
            "parser_version": SEMANTIC_PARSER_VERSION,
            "layout_profile_version": self.layout_profile_version,
            "source_digest": _source_set_digest(self.sources),
        }

    def _parse_outline_identity(self) -> None:
        source = self.sources.get("outline")
        if source is None:
            return
        self._provenance("chapter_index", source, 0, min(len(source["text"]), 120), "outline", "authoritative")

    def _parse_context(self) -> None:
        source = self.sources.get("追踪/上下文.md")
        if source is None:
            return
        text = source["text"]
        starts = _line_starts(text)
        if "NovelAgent:story_project_writeback" in text:
            self._warn(
                "legacy_append_block_evidence_only",
                source["relative_path"],
                "Legacy append blocks are retained as historical evidence and are not authoritative semantic state",
            )

        label_map = {
            "当前位置": "current_location",
            "地点": "current_location",
            "最近决定": "recent_decision",
            "上一章结尾": "previous_ending",
            "上一章收束": "previous_ending",
            "opening bridge": "opening_bridge",
            "openingbridge": "opening_bridge",
        }
        candidates: dict[str, list[tuple[str, int, int]]] = {}
        lines = text.splitlines()
        in_pending = False
        pending: list[str] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
            if heading:
                in_pending = "待处理" in heading.group(1) or "未结" in heading.group(1)
                continue
            if in_pending:
                item = _list_item(stripped)
                if item:
                    pending.append(item)
                    self._provenance(
                        f"story_state.pending_threads[{len(pending) - 1}]",
                        source,
                        starts[index],
                        starts[index] + len(line),
                        "tracking_manual",
                        "authoritative",
                    )
                continue
            match = re.match(r"^(?:[-*+]\s*)?([^:=：]+?)\s*(?:[:：=])\s*(.+?)\s*$", stripped, re.IGNORECASE)
            if not match:
                continue
            raw_label = re.sub(r"\s+", "", match.group(1)).lower()
            field = label_map.get(raw_label)
            if field is None:
                continue
            value = match.group(2).strip()
            candidates.setdefault(field, []).append((value, starts[index], starts[index] + len(line)))

        for field, values in candidates.items():
            unique = list(dict.fromkeys(value for value, _, _ in values))
            if len(unique) > 1:
                self._conflict(
                    f"story_state.{field}",
                    "same_authority_conflict",
                    [f"追踪/上下文.md:{value}" for value in unique],
                    f"Multiple manual values exist for {field}",
                )
                continue
            value, start, end = values[0]
            self.story_state[field] = value
            self._provenance(
                f"story_state.{field}", source, start, end, "tracking_manual", "authoritative"
            )
        if pending:
            self.story_state["pending_threads"] = pending

    def _parse_characters(self) -> None:
        source = self.sources.get("追踪/角色状态.md")
        if source is None:
            return
        text = source["text"]
        lines = text.splitlines()
        starts = _line_starts(text)
        current_name: str | None = None
        field_map = {"身份": "identity", "状态": "status", "位置": "location", "所在": "location", "能力": "abilities", "关系": "relationships", "最近变更": "recent_changes"}
        for index, line in enumerate(lines):
            stripped = line.strip()
            heading = re.match(r"^#{2,6}\s+(.+?)\s*$", stripped)
            if heading:
                current_name = heading.group(1).strip()
                continue
            if current_name is None:
                continue
            pair = _table_pair(stripped) or _labeled_list_pair(stripped)
            if pair is None:
                continue
            raw_field, value = pair
            field = field_map.get(raw_field.strip())
            if field is None or not value:
                continue
            character_id = _stable_id("character", current_name)
            is_new_character = character_id not in self.characters
            character = self.characters.setdefault(character_id, {"name": current_name})
            existing = character.get(field)
            if existing is not None and existing != value:
                self._conflict(
                    f"characters.{character_id}.{field}",
                    "same_authority_conflict",
                    [f"{source['relative_path']}:{existing}", f"{source['relative_path']}:{value}"],
                    f"Multiple manual values exist for {current_name}.{field}",
                )
                character.pop(field, None)
                continue
            character[field] = value
            if is_new_character:
                self._provenance(
                    f"characters.{character_id}",
                    source,
                    starts[index],
                    starts[index] + len(line),
                    "tracking_manual",
                    "authoritative",
                )
            if field == "location":
                self.spatial_state["character_positions"][current_name] = value
                self._provenance(
                    f"spatial_state.character_positions.{current_name}",
                    source,
                    starts[index],
                    starts[index] + len(line),
                    "tracking_manual",
                    "authoritative",
                )
            self._provenance(
                f"characters.{character_id}.{field}",
                source,
                starts[index],
                starts[index] + len(line),
                "tracking_manual",
                "authoritative",
            )

    def _parse_foreshadowing(self) -> None:
        source = self.sources.get("追踪/伏笔.md")
        if source is None:
            return
        tables = _markdown_tables(source["text"])
        parsed = False
        for table in tables:
            headers = {_normalize_header(name): index for index, name in enumerate(table["headers"])}
            id_index = _first_header(headers, "id", "编号")
            content_index = _first_header(headers, "内容", "伏笔")
            status_index = _first_header(headers, "状态")
            if content_index is None or status_index is None:
                continue
            for row in table["rows"]:
                values = row["values"]
                item_id = _cell(values, id_index)
                content = _cell(values, content_index)
                status = _normalize_foreshadow_status(_cell(values, status_index))
                if not item_id:
                    self._warn("foreshadowing_missing_stable_id", source["relative_path"], "Foreshadowing row lacks a stable ID")
                    continue
                if not content:
                    self._warn("foreshadowing_missing_content", source["relative_path"], f"Foreshadowing {item_id} lacks content")
                    continue
                existing = next((entry for entry in self.foreshadowing if entry["id"] == item_id), None)
                if existing is not None:
                    if existing["content"] != content or existing["status"] != status:
                        self._conflict(
                            f"foreshadowing.{item_id}",
                            "same_authority_conflict",
                            [source["relative_path"]],
                            f"Foreshadowing ID {item_id} has conflicting manual rows",
                        )
                    continue
                item = {
                    "id": item_id,
                    "content": content,
                    "status": status,
                    "introduced_chapter": _chapter_number(_cell(values, _first_header(headers, "引入章节", "提出章节"))),
                    "target_chapter": _chapter_number(_cell(values, _first_header(headers, "目标章节", "计划章节"))),
                    "resolved_chapter": _chapter_number(_cell(values, _first_header(headers, "解决章节"))),
                }
                self.foreshadowing.append(item)
                self._provenance(
                    f"foreshadowing.{item_id}",
                    source,
                    row["start"],
                    row["end"],
                    "tracking_manual",
                    "authoritative",
                )
                parsed = True
        if parsed:
            return
        for start, end, line in _lines_with_ranges(source["text"]):
            match = re.match(
                r"^\s*[-*+]\s*\[(.+?)\]\s*`([^`]+)`\s*(.+?)(?:（第\s*(\d+)\s*章提出(?:，计划第\s*(\d+)\s*章处理)?）)?\s*$",
                line,
            )
            if not match:
                continue
            content = match.group(3).strip()
            content = re.sub(r"（第\s*\d+\s*章提出.*?）$", "", content).strip()
            item = {
                "id": match.group(2),
                "content": content,
                "status": _normalize_foreshadow_status(match.group(1)),
                "introduced_chapter": int(match.group(4)) if match.group(4) else None,
                "target_chapter": int(match.group(5)) if match.group(5) else None,
                "resolved_chapter": None,
            }
            if any(entry["id"] == item["id"] for entry in self.foreshadowing):
                self._conflict(
                    f"foreshadowing.{item['id']}",
                    "same_authority_conflict",
                    [source["relative_path"]],
                    f"Foreshadowing ID {item['id']} has duplicate manual entries",
                )
                continue
            self.foreshadowing.append(item)
            self._provenance(
                f"foreshadowing.{item['id']}", source, start, end, "tracking_manual", "authoritative"
            )

    def _parse_timeline(self) -> None:
        source = self.sources.get("追踪/时间线.md")
        if source is None:
            return
        parsed = False
        for table in _markdown_tables(source["text"]):
            headers = {_normalize_header(name): index for index, name in enumerate(table["headers"])}
            event_index = _first_header(headers, "事件", "内容")
            chapter_index = _first_header(headers, "章节")
            if event_index is None or chapter_index is None:
                continue
            for row in table["rows"]:
                values = row["values"]
                chapter = _chapter_number(_cell(values, chapter_index))
                text = _cell(values, event_index)
                if chapter is None or not text:
                    self._warn("timeline_chapter_unknown", source["relative_path"], "Timeline row lacks a deterministic chapter")
                    continue
                event_id = _cell(values, _first_header(headers, "id", "编号")) or _stable_id("event", f"{chapter}:{text}")
                if any(entry["id"] == event_id for entry in self.timeline):
                    self._conflict(
                        f"timeline.{event_id}",
                        "same_authority_conflict",
                        [source["relative_path"]],
                        f"Timeline ID {event_id} has duplicate manual rows",
                    )
                    continue
                item = {
                    "id": event_id,
                    "chapter": chapter,
                    "location": _cell(values, _first_header(headers, "地点")) or None,
                    "text": text,
                }
                self.timeline.append(item)
                self._provenance(
                    f"timeline.{event_id}", source, row["start"], row["end"], "tracking_manual", "authoritative"
                )
                parsed = True
        if parsed:
            return
        for start, end, line in _lines_with_ranges(source["text"]):
            item = _list_item(line.strip())
            if not item:
                continue
            parts = [part.strip() for part in re.split(r"[｜|]", item)]
            chapter = _chapter_number(parts[0]) if parts else None
            if chapter is None or len(parts) < 2:
                self._warn("timeline_chapter_unknown", source["relative_path"], "Timeline list item lacks a deterministic chapter")
                continue
            location = parts[1] if len(parts) >= 3 else None
            text = parts[-1]
            event_id = _stable_id("event", f"{chapter}:{text}")
            self.timeline.append({"id": event_id, "chapter": chapter, "location": location, "text": text})
            self._provenance(
                f"timeline.{event_id}", source, start, end, "tracking_manual", "authoritative"
            )

    def _parse_settings(self) -> None:
        settings: dict[str, Any] = {}
        locations: dict[str, Any] = {}
        for relative_path, source in sorted(self.sources.items()):
            if not relative_path.startswith("设定/"):
                continue
            text = source["text"]
            subject = _first_heading(text) or Path(relative_path).stem
            fields: dict[str, str] = {}
            facts: list[str] = []
            structured_ranges: set[tuple[int, int]] = set()
            for table in _markdown_tables(text):
                if len(table["headers"]) < 2:
                    continue
                for row in table["rows"]:
                    if len(row["values"]) < 2:
                        continue
                    key, value = row["values"][0].strip(), row["values"][1].strip()
                    if not key or not value:
                        continue
                    if key in fields and fields[key] != value:
                        self._conflict(
                            f"world_state.settings.{subject}.fields.{key}",
                            "same_authority_conflict",
                            [source["relative_path"]],
                            f"Setting {subject}.{key} has conflicting structured values",
                        )
                        fields.pop(key, None)
                        continue
                    fields[key] = value
                    structured_ranges.add((row["start"], row["end"]))
                    field_path = (
                        f"world_state.locations.{subject}.{key}"
                        if relative_path.startswith("设定/地点/")
                        else f"world_state.settings.{subject}.fields.{key}"
                    )
                    self._provenance(
                        field_path,
                        source,
                        row["start"],
                        row["end"],
                        "setting",
                        "authoritative",
                    )
                    self._add_constraint_if_normative(value, source, row["start"], row["end"])
            for start, end, line in _lines_with_ranges(text):
                item = _list_item(line.strip())
                if item:
                    facts.append(item)
                    structured_ranges.add((start, end))
                    self._provenance(
                        f"world_state.settings.{subject}.facts[{len(facts) - 1}]",
                        source,
                        start,
                        end,
                        "setting",
                        "authoritative",
                    )
                    self._add_constraint_if_normative(item, source, start, end)
            if fields or facts:
                if relative_path.startswith("设定/地点/") and fields:
                    locations[subject] = fields
                    if facts:
                        settings[subject] = {"fields": {}, "facts": facts}
                else:
                    settings[subject] = {"fields": fields, "facts": facts}
            for start, end, line in _lines_with_ranges(text):
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("|") or _list_item(stripped):
                    continue
                if (start, end) in structured_ranges:
                    continue
                self._unsupported(source, start, end, stripped)
        if settings:
            self.world_state["settings"] = settings
        if locations:
            self.world_state["locations"] = locations

    def _parse_managed_projections(self) -> None:
        scopes = {
            "追踪/上下文.md": "context",
            "追踪/角色状态.md": "character_state",
            "追踪/伏笔.md": "foreshadowing",
            "追踪/时间线.md": "timeline",
        }
        for relative_path, expected_scope in scopes.items():
            source = self.sources.get(relative_path)
            if source is None:
                continue
            try:
                parsed = parse_managed_block(source["text"])
            except ManagedBlockError as exc:
                self._conflict(
                    "managed_projection",
                    exc.code,
                    [relative_path],
                    str(exc),
                )
                continue
            if parsed is None:
                continue
            projection = parsed.projection
            if projection["book_id"] != self.book_id:
                self._conflict(
                    "managed_projection.book_id",
                    "managed_projection_identity_mismatch",
                    [relative_path],
                    "Managed projection belongs to another StoryProject",
                )
                continue
            if projection["scope"] != expected_scope:
                self._conflict(
                    "managed_projection.scope",
                    "managed_projection_scope_mismatch",
                    [relative_path],
                    f"Managed projection scope {projection['scope']} does not match {expected_scope}",
                )
                continue
            tombstones = {item["field_path"] for item in projection["tombstones"]}
            for field_path, value in projection["values"].items():
                if field_path in tombstones or self._has_manual_authority(field_path):
                    continue
                if not self._apply_managed_value(expected_scope, field_path, value):
                    self._warn(
                        "managed_projection_field_unsupported",
                        relative_path,
                        f"Managed projection field is outside its scope: {field_path}",
                    )
                    continue
                self._provenance(
                    field_path,
                    source,
                    parsed.start_char,
                    parsed.end_char,
                    "managed_projection",
                    "supporting",
                )

    def _has_manual_authority(self, field_path: str) -> bool:
        for item in self.provenance:
            if item["authority_class"] != "authoritative":
                continue
            existing = item["field_path"]
            if existing == field_path or existing.startswith(field_path + ".") or field_path.startswith(existing + "."):
                return True
        return False

    def _apply_managed_value(self, scope: str, field_path: str, value: Any) -> bool:
        parts = field_path.split(".")
        allowed = {
            "context": {"story_state"},
            "character_state": {"characters", "spatial_state"},
            "foreshadowing": {"foreshadowing"},
            "timeline": {"timeline"},
        }[scope]
        if len(parts) < 2 or parts[0] not in allowed:
            return False
        if parts[0] == "story_state":
            return _set_missing_nested(self.story_state, parts[1:], value)
        if parts[0] == "spatial_state":
            return _set_missing_nested(self.spatial_state, parts[1:], value)
        if parts[0] == "characters":
            return _set_missing_nested(self.characters, parts[1:], value)
        if parts[0] == "foreshadowing" and len(parts) == 2 and isinstance(value, dict):
            if any(item.get("id") == parts[1] for item in self.foreshadowing):
                return True
            item = dict(value)
            item.setdefault("id", parts[1])
            self.foreshadowing.append(item)
            return True
        if parts[0] == "timeline" and len(parts) == 2 and isinstance(value, dict):
            if any(item.get("id") == parts[1] for item in self.timeline):
                return True
            item = dict(value)
            item.setdefault("id", parts[1])
            self.timeline.append(item)
            return True
        return False

    def _add_constraint_if_normative(
        self,
        content: str,
        source: dict[str, Any],
        start: int,
        end: int,
    ) -> None:
        if not any(marker in content for marker in ("必须", "只能", "不得", "不能", "禁止")):
            return
        constraint_id = _stable_id("constraint", content)
        if any(entry["id"] == constraint_id for entry in self.constraints):
            return
        self.constraints.append({"id": constraint_id, "content": content, "status": "active"})
        self._provenance(
            f"constraints.{constraint_id}", source, start, end, "setting", "authoritative"
        )

    def _provenance(
        self,
        field_path: str,
        source: dict[str, Any],
        start: int,
        end: int,
        source_kind: str,
        authority_class: str,
    ) -> None:
        self.provenance.append(
            {
                "field_path": field_path,
                "source_path": source["relative_path"],
                "source_kind": source_kind,
                "authority_class": authority_class,
                "parser_version": SEMANTIC_PARSER_VERSION,
                "source_sha256": source["sha256"],
                "start_char": max(0, start),
                "end_char": max(start, end),
            }
        )

    def _conflict(self, field_path: str, code: str, sources: list[str], message: str) -> None:
        conflict_id = _stable_id("conflict", json.dumps([field_path, code, sources], ensure_ascii=False))
        self.conflicts.append(
            {
                "id": conflict_id,
                "field_path": field_path,
                "code": code,
                "blocking": True,
                "sources": sources,
                "message": message,
            }
        )

    def _warn(self, code: str, source_path: str, message: str) -> None:
        self.warnings.append({"code": code, "source_path": source_path, "message": message})

    def _unsupported(self, source: dict[str, Any], start: int, end: int, excerpt: str) -> None:
        raw = source["text"][start:end]
        self.unsupported.append(
            {
                "source_path": source["relative_path"],
                "start_char": start,
                "end_char": end,
                "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "excerpt": excerpt[:500],
                "authoritative": False,
            }
        )


def _load_sources(root: Path, chapter_index: int) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    outline = resolve_outline(root, chapter_index)
    if outline.conflict or outline.path is None:
        raise StoryProjectSemanticParseError(f"No unique outline matched chapter {chapter_index}")
    _add_source(sources, "outline", outline.path, root)
    if chapter_index > 1:
        previous = resolve_prose(root, chapter_index - 1)
        if previous.conflict:
            raise StoryProjectSemanticParseError(f"Multiple prose files matched chapter {chapter_index - 1}")
        if previous.path is not None:
            _add_source(sources, "previous_prose", previous.path, root)
    for directory_name in ("追踪", "设定"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md"), key=lambda item: item.relative_to(root).as_posix()):
            _add_source(sources, path.relative_to(root).as_posix(), path, root)
    return sources


def _add_source(sources: dict[str, dict[str, Any]], key: str, path: Path, root: Path) -> None:
    text = load_text(path)
    sources[key] = {
        "path": str(path),
        "relative_path": path.relative_to(root).as_posix(),
        "text": text,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _detect_layout_profile(sources: dict[str, dict[str, Any]]) -> str:
    context = str((sources.get("追踪/上下文.md") or {}).get("text") or "")
    if context.count(MANAGED_START) > 1:
        return "malformed-zh-1"
    if "NovelAgent:story_project_writeback" in context or "剧情运行状态" in context:
        return "legacy-zh-1"
    return "canonical-zh-1"


def _source_set_digest(sources: dict[str, dict[str, Any]]) -> str:
    payload = [
        {"role": role, "relative_path": source["relative_path"], "sha256": source["sha256"]}
        for role, source in sorted(sources.items())
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_snapshot_differences(state: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for field in ("story_state", "world_state", "spatial_state", "characters", "timeline", "constraints"):
        semantic_value = state.get(field)
        snapshot_value = snapshot.get(field)
        if semantic_value != snapshot_value:
            differences.append(
                {
                    "field_path": field,
                    "semantic_value": semantic_value,
                    "snapshot_value": snapshot_value,
                }
            )
    return differences


def _stable_id(kind: str, value: str) -> str:
    return f"{kind}-{hashlib.sha256(value.strip().encode('utf-8')).hexdigest()[:16]}"


def _line_starts(text: str) -> list[int]:
    starts: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    if not starts and text == "":
        return []
    if len(starts) < len(text.splitlines()):
        starts.append(offset)
    return starts


def _lines_with_ranges(text: str) -> Iterable[tuple[int, int, str]]:
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        yield offset, offset + len(content), content
        offset += len(line)


def _list_item(text: str) -> str | None:
    match = re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def _table_pair(line: str) -> tuple[str, str] | None:
    if not line.startswith("|"):
        return None
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    if len(cells) < 2 or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
        return None
    if cells[0] in {"字段", "Field"} and cells[1] in {"值", "Value"}:
        return None
    return cells[0], cells[1]


def _labeled_list_pair(line: str) -> tuple[str, str] | None:
    item = _list_item(line)
    if item is None:
        return None
    match = re.match(r"^([^:：=]+)\s*[:：=]\s*(.+)$", item)
    return (match.group(1).strip(), match.group(2).strip()) if match else None


def _markdown_tables(text: str) -> list[dict[str, Any]]:
    lines = list(_lines_with_ranges(text))
    tables: list[dict[str, Any]] = []
    index = 0
    while index + 1 < len(lines):
        _, _, header_line = lines[index]
        _, _, separator = lines[index + 1]
        if not header_line.strip().startswith("|") or not separator.strip().startswith("|"):
            index += 1
            continue
        separator_cells = [cell.strip() for cell in separator.strip().strip("|").split("|")]
        if not separator_cells or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator_cells):
            index += 1
            continue
        headers = [cell.strip() for cell in header_line.strip().strip("|").split("|")]
        rows: list[dict[str, Any]] = []
        cursor = index + 2
        while cursor < len(lines) and lines[cursor][2].strip().startswith("|"):
            start, end, row_line = lines[cursor]
            rows.append(
                {
                    "values": [cell.strip() for cell in row_line.strip().strip("|").split("|")],
                    "start": start,
                    "end": end,
                }
            )
            cursor += 1
        tables.append({"headers": headers, "rows": rows})
        index = cursor
    return tables


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _first_header(headers: dict[str, int], *names: str) -> int | None:
    for name in names:
        normalized = _normalize_header(name)
        if normalized in headers:
            return headers[normalized]
    return None


def _cell(values: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(values):
        return ""
    return values[index].strip()


def _chapter_number(value: str) -> int | None:
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else None


def _normalize_foreshadow_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"open", "未解", "未解决", "开放"}:
        return "open"
    if normalized in {"developing", "进行中", "推进中"}:
        return "developing"
    if normalized in {"resolved", "已解决", "完成"}:
        return "resolved"
    if normalized in {"cancelled", "取消", "废弃"}:
        return "cancelled"
    return "unknown"


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if match:
            return match.group(1).strip()
    return None


def _set_missing_nested(target: dict[str, Any], parts: list[str], value: Any) -> bool:
    if not parts:
        return False
    cursor = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if existing is None:
            existing = {}
            cursor[part] = existing
        if not isinstance(existing, dict):
            return True
        cursor = existing
    cursor.setdefault(parts[-1], value)
    return True


__all__ = [
    "SEMANTIC_PARSER_VERSION",
    "SHADOW_REPORT_SCHEMA_VERSION",
    "StoryProjectSemanticParseError",
    "build_story_project_shadow_report",
    "parse_story_project_semantic_state",
]
