from __future__ import annotations

from typing import Any


def build_memory_updates(run: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    chapter_index = int(run.get("chapter_index", 0) or 0)
    run_id = str(run.get("id") or "")
    updates: list[dict[str, Any]] = []

    summary = analysis.get("summary")
    if isinstance(summary, str) and summary.strip():
        updates.append(
            _memory_item(
                "timeline_event",
                f"chapter_{chapter_index}_summary",
                {
                    "chapter_index": chapter_index,
                    "summary": summary.strip(),
                },
                run_id,
            )
        )

    for index, event in enumerate(_objects(analysis.get("events"))):
        text = event.get("text")
        if isinstance(text, str) and text.strip():
            updates.append(
                _memory_item(
                    "timeline_event",
                    f"chapter_{chapter_index}_event_{index + 1}",
                    {
                        "chapter_index": chapter_index,
                        "text": text.strip(),
                    },
                    run_id,
                )
            )

    for index, change in enumerate(_objects(analysis.get("world_changes"))):
        updates.append(
            _memory_item(
                "world_state",
                str(change.get("type") or f"chapter_{chapter_index}_world_change_{index + 1}"),
                {
                    "chapter_index": chapter_index,
                    **change,
                },
                run_id,
            )
        )

    for change in _objects(analysis.get("character_changes")):
        name = change.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        data: dict[str, Any] = {"chapter_index": chapter_index}
        for field in ("status", "current_location", "current_goal"):
            value = change.get(field)
            if isinstance(value, str) and value.strip():
                data[field] = value.strip()
        text = change.get("text")
        if isinstance(text, str) and text.strip():
            data["last_observation"] = text.strip()
        updates.append(_memory_item("character", name.strip(), data, run_id))

    for location in analysis.get("new_locations", []) or []:
        if isinstance(location, str) and location.strip():
            updates.append(
                _memory_item(
                    "location",
                    location.strip(),
                    {
                        "chapter_index": chapter_index,
                        "first_seen_chapter": chapter_index,
                        "source": "chapter_analysis",
                    },
                    run_id,
                )
            )

    story_state = analysis.get("story_state")
    if isinstance(story_state, dict) and _has_content(story_state):
        updates.append(
            _memory_item(
                "story_state",
                f"chapter_{chapter_index}_story_state",
                {
                    "chapter_index": chapter_index,
                    **story_state,
                },
                run_id,
            )
        )

    spatial_state = analysis.get("spatial_state")
    if isinstance(spatial_state, dict) and _has_content(spatial_state):
        updates.append(
            _memory_item(
                "spatial_state",
                f"chapter_{chapter_index}_spatial_state",
                {
                    "chapter_index": chapter_index,
                    **spatial_state,
                },
                run_id,
            )
        )

    return updates


def _memory_item(
    item_type: str,
    name: str,
    data: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    item_id = _memory_item_id(data, item_type, name, run_id)
    payload = {
        "id": item_id,
        "type": item_type,
        "name": name,
        "data": data,
    }
    if run_id:
        payload["source_run_id"] = run_id
        payload["data"] = {**data, "source_run_id": run_id}
    return payload


def _memory_item_id(data: dict[str, Any], item_type: str, name: str, run_id: str) -> str:
    chapter_index = data.get("chapter_index")
    prefix = f"chapter_{chapter_index}" if chapter_index else (run_id or "manual")
    return f"{prefix}:{item_type}:{_slug(name)}"


def _slug(value: str) -> str:
    clean = "".join(char.lower() if char.isalnum() else "_" for char in str(value).strip())
    return "_".join(part for part in clean.split("_") if part)


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _has_content(value: dict[str, Any]) -> bool:
    for item in value.values():
        if isinstance(item, str) and item.strip():
            return True
        if isinstance(item, (list, dict)) and item:
            return True
        if item not in (None, "", [], {}):
            return True
    return False


__all__ = ["build_memory_updates"]
