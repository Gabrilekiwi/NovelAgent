from __future__ import annotations

from typing import Any


def normalize_project_profile(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = snapshot if isinstance(snapshot, dict) else {}
    raw_profile = data.get("project_profile")
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    language = _text(profile.get("language") or profile.get("language_code"))
    return {
        "language": language,
        "known_characters": _unique_strings(
            [
                *_list_strings(profile.get("known_characters")),
                *_snapshot_character_names(data),
            ]
        ),
        "known_locations": _unique_strings(
            [
                *_list_strings(profile.get("known_locations")),
                *_snapshot_location_names(data),
            ]
        ),
    }


def project_language(snapshot: dict[str, Any] | None) -> str:
    return str(normalize_project_profile(snapshot).get("language") or "")


def _snapshot_character_names(snapshot: dict[str, Any]) -> list[str]:
    names: list[str] = []
    characters = snapshot.get("characters")
    if isinstance(characters, dict):
        names.extend(str(name) for name in characters if str(name).strip())
    story_state = snapshot.get("story_state")
    if isinstance(story_state, dict):
        names.extend(_list_strings(story_state.get("last_scene_characters")))
    spatial_state = snapshot.get("spatial_state")
    if isinstance(spatial_state, dict):
        positions = spatial_state.get("character_positions")
        if isinstance(positions, dict):
            names.extend(str(name) for name in positions if str(name).strip())
    return names


def _snapshot_location_names(snapshot: dict[str, Any]) -> list[str]:
    names: list[str] = []
    world_state = snapshot.get("world_state")
    if isinstance(world_state, dict):
        locations = world_state.get("locations")
        if isinstance(locations, dict):
            names.extend(str(name) for name in locations if str(name).strip())
    spatial_state = snapshot.get("spatial_state")
    if isinstance(spatial_state, dict):
        spaces = spatial_state.get("spaces")
        if isinstance(spaces, dict):
            names.extend(str(name) for name in spaces if str(name).strip())
    story_state = snapshot.get("story_state")
    if isinstance(story_state, dict):
        names.append(str(story_state.get("last_scene_location") or ""))
    return names


def _list_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _text(value: Any) -> str:
    return str(value or "").strip()
