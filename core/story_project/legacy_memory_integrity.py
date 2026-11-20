"""Evidence checks for legacy Markdown memory; never infer replacement facts."""
from __future__ import annotations

import hashlib
from pathlib import Path

from core.location_names import is_usable_location_name
from core.story_project.loader import load_text
from core.story_project.paths import resolve_prose


MEMORY_INTEGRITY_CODES = frozenset({
    "memory_source_changed", "memory_source_missing", "memory_source_unbound",
    "memory_character_identity_conflict", "memory_invalid_scene_location",
    "memory_observation_not_in_prose",
})


def prose_digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").rstrip().encode("utf-8")).hexdigest()


def projection_source_issues(root: Path, projection: dict, previous_text: str) -> list[tuple[str, str]]:
    issues = []
    checked: dict[int, str | None] = {}
    source_texts: dict[int, str] = {}
    for field, value in projection["values"].items():
        source = projection.get("field_sources", {}).get(field)
        if source is None:
            # Old projections label the whole accumulated object with the newest
            # run. An inherited observation is not evidence of the current scene.
            quote = value.get("text") if field.startswith("characters.") and isinstance(value, dict) else None
            if quote and previous_text and str(quote).strip() not in previous_text:
                issues.append(("memory_source_unbound", f"{field}: inherited observation has no chapter/prose binding; review its current state"))
            continue
        chapter = source["chapter"]
        if chapter not in checked:
            resolved = resolve_prose(root, chapter)
            if resolved.path is not None and not resolved.conflict:
                source_texts[chapter] = load_text(resolved.path)
                checked[chapter] = prose_digest(source_texts[chapter])
            else:
                checked[chapter] = None
        if checked[chapter] is None:
            issues.append(("memory_source_missing", f"{field}: source chapter {chapter} is missing or ambiguous"))
        elif checked[chapter] != source["prose_sha256"]:
            issues.append(("memory_source_changed", f"{field}: source chapter {chapter} changed; review and rebuild affected memory"))
        elif field.startswith("characters.") and isinstance(value, dict):
            quote = str(value.get("text") or "").strip()
            if quote and quote not in source_texts[chapter]:
                issues.append(("memory_observation_not_in_prose", f"{field}: observation is absent from its declared source chapter {chapter}"))
    return issues


def current_state_issues(story: dict, characters: dict) -> list[tuple[str, str]]:
    issues = []
    location = str(story.get("last_scene_location") or "").strip()
    if location and not is_usable_location_name(location):
        issues.append(("memory_invalid_scene_location", f"last_scene_location={location!r} is not a usable place; review the scene boundary"))
    by_name: dict[str, list[tuple[str, dict]]] = {}
    for entity_id, record in characters.items():
        if isinstance(record, dict):
            name = str(record.get("name") or entity_id).strip()
            by_name.setdefault(name, []).append((entity_id, record))
    for name, records in by_name.items():
        if len(records) < 2:
            continue
        locations = {str(record.get("current_location") or record.get("location") or "").strip()
                     for _, record in records} - {""}
        statuses = {str(record.get("status") or "").strip() for _, record in records} - {""}
        if len(locations) > 1 or len(statuses) > 1:
            issues.append(("memory_character_identity_conflict", f"{name}: multiple identities disagree on current state; reconcile before generation"))
    return issues


def require_legacy_memory_integrity(semantic: dict) -> None:
    issues = [item for item in semantic.get("parse_warnings", []) if item.get("code") in MEMORY_INTEGRITY_CODES]
    if issues:
        details = "; ".join(f"{item['code']}: {item['message']}" for item in issues[:8])
        raise ValueError("小说记忆与正文存在未核对的冲突。请先核对当前状态并确认已审核事实，再继续生成。 " + details)
