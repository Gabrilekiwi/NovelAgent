from __future__ import annotations

import re
import unicodedata
from typing import Any

from core.validator.common import get_location_terms, get_locations


TRANSITION_TERMS = (
    "through",
    "across",
    "into",
    "from",
    "toward",
    "via",
    "corridor",
    "passage",
    "bridge",
    "door",
    "gate",
    "moved",
    "crossed",
    "entered",
    "left",
    "returned",
    "走",
    "穿过",
    "进入",
    "离开",
    "通道",
    "车厢",
)


def validate_spatial(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    locations = get_locations(snapshot)

    location_terms = [
        term
        for location, data in locations.items()
        for term in get_location_terms(str(location), data)
    ]
    lowered = chapter_text.lower()

    if location_terms and not any(term.lower() in lowered for term in location_terms):
        problems.append(
            {
                "code": "no_known_location",
                "message": "Chapter does not mention any known location from snapshot.",
                "suggested_term": location_terms[0],
                "evidence": [{"kind": "known_location_terms", "value": ", ".join(location_terms[:10])}],
            }
        )

    problems.extend(_validate_opening_bridge(snapshot, chapter_text, locations))
    problems.extend(_validate_location_transition(snapshot, chapter_text, locations))

    characters = snapshot.get("characters") or {}
    for name, data in characters.items():
        if not isinstance(data, dict):
            continue
        current_location, location_source = _resolved_character_location(
            snapshot,
            str(name),
            data,
        )
        if not current_location:
            continue
        if location_source == "legacy" and not _character_location_is_fresh(snapshot, data):
            # ``current_location`` is a last observation, not an eternal
            # constraint.  Legacy/imported projects often retain a location
            # for many chapters when later analyses contain no explicit
            # character-position delta.  Treating that stale observation as
            # current makes every later mention look spatially invalid and
            # sends the repairer backwards in time.
            continue
        if current_location not in locations:
            problems.append(
                {
                    "code": "character_unknown_location",
                    "message": f"Character {name} references unknown location {current_location}.",
                    "character": str(name),
                    "location": str(current_location),
                    "evidence": [
                        {"kind": "character", "value": str(name)},
                        {"kind": "unknown_location", "value": str(current_location)},
                    ],
                }
            )
            continue
        character_mentioned = str(name).lower() in lowered
        if character_mentioned:
            terms = get_location_terms(str(current_location), locations[current_location])
            if not _contains_location_signal(chapter_text, terms):
                problems.append(
                    {
                        "code": "character_location_not_mentioned",
                        "message": f"Character {name} appears without current location {current_location}.",
                        "character": str(name),
                        "location": str(current_location),
                        "evidence": [
                            {"kind": "character", "value": str(name)},
                            {"kind": "current_location", "value": str(current_location)},
                        ],
                    }
                )

    problems.extend(_validate_character_positions(snapshot, chapter_text, locations))

    return {"name": "spatial", "ok": not problems, "problems": problems}


def validate_bridge_preconditions(snapshot: dict[str, Any]) -> dict[str, Any]:
    story_state = snapshot.get("story_state") if isinstance(snapshot.get("story_state"), dict) else {}
    spatial_state = snapshot.get("spatial_state") if isinstance(snapshot.get("spatial_state"), dict) else {}
    locations = get_locations(snapshot)
    last_location = str(story_state.get("last_scene_location") or "").strip()
    required_bridge = str(story_state.get("required_opening_bridge") or "").strip()
    problems: list[dict[str, Any]] = []

    if not last_location and required_bridge:
        problems.append(
            {
                "code": "missing_last_scene_continuity",
                "message": "required_opening_bridge is set but story_state.last_scene_location is missing.",
                "location": "",
                "character": "",
                "evidence": [{"kind": "required_opening_bridge", "value": required_bridge}],
            }
        )
    if last_location and last_location not in locations:
        problems.append(
            {
                "code": "missing_last_scene_continuity",
                "message": f"story_state.last_scene_location {last_location} is not a known space or location.",
                "location": last_location,
                "character": "",
                "evidence": [{"kind": "last_scene_location", "value": last_location}],
            }
        )
    if last_location and not required_bridge:
        problems.append(
            {
                "code": "missing_opening_bridge",
                "message": "story_state.last_scene_location is set but required_opening_bridge is missing.",
                "bridge": "",
                "location": last_location,
                "evidence": [{"kind": "last_scene_location", "value": last_location}],
            }
        )
    return {
        "name": "pre_validate_bridge",
        "ok": not problems,
        "problems": problems,
        "problem_codes": [problem["code"] for problem in problems],
    }


def _validate_opening_bridge(
    snapshot: dict[str, Any],
    chapter_text: str,
    locations: dict[str, Any],
) -> list[dict[str, Any]]:
    story_state = snapshot.get("story_state") if isinstance(snapshot.get("story_state"), dict) else {}
    bridge = str(story_state.get("required_opening_bridge") or "").strip()
    last_location = str(story_state.get("last_scene_location") or "").strip()
    last_characters = [str(item).strip() for item in story_state.get("last_scene_characters", []) if str(item).strip()]
    opening = _opening_text(chapter_text)
    lowered_opening = opening.lower()
    problems: list[dict[str, Any]] = []

    if bridge and not _contains_bridge_signal(opening, bridge):
        problems.append(
            {
                "code": "missing_opening_bridge",
                "message": "Chapter opening does not include the required bridge from the previous ending.",
                "bridge": bridge,
                "location": last_location,
                "evidence": [
                    {"kind": "required_opening_bridge", "value": bridge},
                    {"kind": "opening_excerpt", "value": opening[:160]},
                ],
            }
        )

    missing_parts: list[str] = []
    if last_location:
        terms = get_location_terms(last_location, locations.get(last_location, {}))
        if not any(term.lower() in lowered_opening for term in terms):
            missing_parts.append(f"location:{last_location}")
    missing_characters = [name for name in last_characters if name.lower() not in lowered_opening]
    missing_parts.extend(f"character:{name}" for name in missing_characters)
    if missing_parts:
        # Pass the full last-scene cast (not only currently missing names) so
        # repair re-anchors everyone. Model surgical edits that add the reported
        # missing names can otherwise drop other cast members still required in
        # the opening window and stall repair convergence.
        problems.append(
            {
                "code": "missing_last_scene_continuity",
                "message": "Chapter opening does not anchor the last scene state before continuing.",
                "location": last_location,
                "character": ", ".join(last_characters),
                "evidence": [
                    {"kind": "missing_last_scene_state", "value": ", ".join(missing_parts)},
                    {"kind": "opening_excerpt", "value": opening[:160]},
                ],
            }
        )
    return problems


def _validate_location_transition(
    snapshot: dict[str, Any],
    chapter_text: str,
    locations: dict[str, Any],
) -> list[dict[str, Any]]:
    story_state = snapshot.get("story_state") if isinstance(snapshot.get("story_state"), dict) else {}
    spatial_state = snapshot.get("spatial_state") if isinstance(snapshot.get("spatial_state"), dict) else {}
    last_location = str(story_state.get("last_scene_location") or "").strip()
    if not last_location or not locations:
        return []

    first_location = _first_mentioned_location(chapter_text, locations)
    if not first_location or first_location == last_location:
        return []

    opening = _opening_text(chapter_text)
    has_transition = _has_transition_language(opening) or bool(str(story_state.get("required_opening_bridge") or "").strip())
    problems: list[dict[str, Any]] = []
    if not has_transition:
        problems.append(
            {
                "code": "unexplained_location_shift",
                "message": f"Chapter shifts from {last_location} to {first_location} without an opening transition.",
                "expected": last_location,
                "actual": first_location,
                "evidence": [
                    {"kind": "last_scene_location", "value": last_location},
                    {"kind": "first_mentioned_location", "value": first_location},
                ],
            }
        )

    if not _transition_allowed(spatial_state, last_location, first_location):
        problems.append(
            {
                "code": "invalid_spatial_transition",
                "message": f"Snapshot spatial graph does not allow movement from {last_location} to {first_location}.",
                "expected": last_location,
                "actual": first_location,
                "evidence": [
                    {"kind": "blocked_or_missing_connection", "value": f"{last_location}->{first_location}"},
                ],
            }
        )
    return problems


def _validate_character_positions(
    snapshot: dict[str, Any],
    chapter_text: str,
    locations: dict[str, Any],
) -> list[dict[str, Any]]:
    spatial_state = snapshot.get("spatial_state") if isinstance(snapshot.get("spatial_state"), dict) else {}
    positions = spatial_state.get("character_positions") if isinstance(spatial_state, dict) else {}
    if not isinstance(positions, dict):
        return []

    lowered = chapter_text.lower()
    problems: list[dict[str, Any]] = []
    for character, expected_location in positions.items():
        character_name = str(character).strip()
        expected = str(expected_location).strip()
        if not character_name or not expected or character_name.lower() not in lowered:
            continue
        character_data = snapshot.get("characters", {}).get(character_name)
        resolved, source = _resolved_character_location(
            snapshot,
            character_name,
            character_data if isinstance(character_data, dict) else {},
        )
        if resolved:
            expected = resolved
        if source == "unverified_authority":
            # A self-reported or otherwise unverified authority observation
            # must suppress the stale compatibility cache, not silently fall
            # back to it as a deterministic position constraint.
            continue
        if (
            source == "legacy"
            and isinstance(character_data, dict)
            and not _character_location_is_fresh(snapshot, character_data)
        ):
            continue
        actual = _first_mentioned_location_near_character(chapter_text, character_name, locations)
        if actual and actual != expected and not _transition_allowed(spatial_state, expected, actual):
            problems.append(
                {
                    "code": "character_position_conflict",
                    "message": f"{character_name} appears at {actual} but spatial state places them at {expected}.",
                    "character": character_name,
                    "expected": expected,
                    "actual": actual,
                    "evidence": [
                        {"kind": "character", "value": character_name},
                        {"kind": "expected_position", "value": expected},
                        {"kind": "actual_position", "value": actual},
                    ],
                }
            )
    return problems


def _character_location_is_fresh(snapshot: dict[str, Any], character: dict[str, Any]) -> bool:
    """Return whether a stored character location can constrain this chapter.

    ``snapshot.chapter_index`` is the chapter about to be written, while
    ``last_seen_chapter`` records the chapter that actually established the
    character position.  Only the immediately preceding chapter is fresh
    enough to serve as a strict spatial precondition.  Chapter-one snapshots
    predate this metadata, so their explicit locations retain the legacy
    behavior.
    """

    chapter_index = snapshot.get("chapter_index")
    if isinstance(chapter_index, bool) or not isinstance(chapter_index, int):
        return True
    last_seen = character.get("last_seen_chapter")
    if chapter_index <= 1 and last_seen is None:
        return True
    if isinstance(last_seen, bool) or not isinstance(last_seen, int):
        return False
    return last_seen >= chapter_index - 1


def _resolved_character_location(
    snapshot: dict[str, Any],
    character_id: str,
    character: dict[str, Any],
) -> tuple[str, str]:
    """Resolve one position with authoritative state before legacy caches.

    An explicit unverified authority record suppresses the legacy fallback:
    evidence may be shown to a model, but it must not become a deterministic
    spatial constraint.
    """

    authority = snapshot.get("authoritative_state")
    locations = authority.get("locations") if isinstance(authority, dict) else {}
    aliases = {
        _entity_key(character_id),
        _entity_key(character.get("id")),
        _entity_key(character.get("name")),
        _entity_key(character.get("canonical_name")),
    }
    for value in character.get("aliases") or []:
        aliases.add(_entity_key(value))
    aliases.discard("")
    if isinstance(locations, dict):
        for record_id, raw in locations.items():
            if not isinstance(raw, dict):
                continue
            entity_id = _entity_key(raw.get("entity_id") or record_id)
            if entity_id not in aliases:
                continue
            if not _authority_location_is_confirmed(raw):
                return "", "unverified_authority"
            return str(raw.get("location_id") or "").strip(), "authority"
    return str(character.get("current_location") or "").strip(), "legacy"


def _authority_location_is_confirmed(record: dict[str, Any]) -> bool:
    raw = record.get("certainty")
    if raw in (None, ""):
        return True
    return str(raw).strip().casefold() in {"confirmed", "current"}


def _entity_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _contains_location_signal(text: str, terms: list[str]) -> bool:
    normalized_text = _normalize_location_text(text)
    for term in terms:
        normalized = _normalize_location_text(term)
        if normalized and normalized in normalized_text:
            return True
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        # Natural Chinese commonly inserts/removes qualifiers such as “的” or
        # expands a known compound (消防站 -> 消防站一层车库区).  A stable
        # three-character anchor is sufficient for this advisory check only;
        # explicit conflicts still use the stricter near-character rule.
        if len(cjk) >= 3 and cjk[:3] in normalized_text:
            return True
    return False


def _normalize_location_text(value: str) -> str:
    return re.sub(r"[\s\W_的]+", "", str(value).casefold(), flags=re.UNICODE)


def _opening_text(chapter_text: str) -> str:
    return chapter_text.strip()[:700]


def _contains_bridge_signal(opening: str, bridge: str) -> bool:
    lowered = opening.lower()
    bridge_lower = bridge.lower()
    if bridge_lower and bridge_lower in lowered:
        return True
    opening_terms = set(_word_terms(lowered))
    all_bridge_terms = _word_terms(bridge_lower)
    bridge_terms = [
        term
        for term in all_bridge_terms
        if len(term) >= 3 or (_contains_cjk(term) and len(term) >= 2)
    ]
    if not bridge_terms:
        return False
    source_terms = _bridge_source_terms(all_bridge_terms)
    if source_terms and not all(_term_in_opening(term, lowered, opening_terms) for term in source_terms):
        return False
    checked_terms = bridge_terms[:4]
    matched = sum(1 for term in checked_terms if _term_in_opening(term, lowered, opening_terms))
    if matched >= min(2, len(checked_terms)):
        return True
    return _cjk_bigram_overlap(opening, bridge) >= 0.22


def _cjk_bigram_overlap(actual: str, required: str) -> float:
    required_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", required))
    actual_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", actual))
    if len(required_cjk) < 8 or len(actual_cjk) < 2:
        return 0.0
    required_bigrams = {required_cjk[index:index + 2] for index in range(len(required_cjk) - 1)}
    actual_bigrams = {actual_cjk[index:index + 2] for index in range(len(actual_cjk) - 1)}
    matched = required_bigrams & actual_bigrams
    if len(matched) < 4:
        return 0.0
    return len(matched) / max(1, len(required_bigrams))


def _word_terms(value: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff]+", value.lower())


def _term_in_opening(term: str, lowered_opening: str, opening_terms: set[str]) -> bool:
    if term in opening_terms:
        return True
    if _contains_cjk(term):
        return term in lowered_opening
    return False


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _bridge_source_terms(bridge_terms: list[str]) -> list[str]:
    if "from" in bridge_terms:
        index = bridge_terms.index("from")
        candidates = [term for term in bridge_terms[index + 1:index + 3] if term not in {"the", "a", "an"}]
        if len(candidates) > 1 and candidates[1] in {"show", "resolve", "continue", "explain", "move", "moving", "before"}:
            return candidates[:1]
        return candidates
    if "to" in bridge_terms:
        index = bridge_terms.index("to")
        return [term for term in bridge_terms[max(0, index - 2):index] if term not in {"the", "a", "an"}]
    return []


def _has_transition_language(opening: str) -> bool:
    lowered = opening.lower()
    return any(term in lowered for term in TRANSITION_TERMS)


def _first_mentioned_location(chapter_text: str, locations: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, str]] = []
    lowered = chapter_text.lower()
    for location, data in locations.items():
        location_name = str(location)
        for term in get_location_terms(location_name, data):
            position = lowered.find(term.lower())
            if position >= 0:
                candidates.append((position, location_name))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _first_mentioned_location_near_character(
    chapter_text: str,
    character: str,
    locations: dict[str, Any],
) -> str | None:
    lowered = chapter_text.lower()
    character_position = lowered.find(character.lower())
    if character_position < 0:
        return None
    window = chapter_text[max(0, character_position - 180): character_position + 260]
    return _first_mentioned_location(window, locations)


def _transition_allowed(spatial_state: dict[str, Any], source: str, target: str) -> bool:
    if source == target:
        return True
    if _path_blocked(spatial_state.get("blocked_paths"), source, target):
        return False
    connections = spatial_state.get("connections")
    if not isinstance(connections, list) or not connections:
        return True
    for connection in connections:
        parsed = _parse_connection(connection)
        if parsed is None:
            continue
        left, right, bidirectional = parsed
        if left == source and right == target:
            return True
        if bidirectional and left == target and right == source:
            return True
    return False


def _path_blocked(blocked_paths: Any, source: str, target: str) -> bool:
    if not isinstance(blocked_paths, list):
        return False
    for path in blocked_paths:
        parsed = _parse_connection(path)
        if parsed is None:
            continue
        left, right, bidirectional = parsed
        if left == source and right == target:
            return True
        if bidirectional and left == target and right == source:
            return True
    return False


def _parse_connection(value: Any) -> tuple[str, str, bool] | None:
    if isinstance(value, dict):
        left = str(value.get("from") or value.get("source") or value.get("a") or "").strip()
        right = str(value.get("to") or value.get("target") or value.get("b") or "").strip()
        bidirectional = bool(value.get("bidirectional", True))
    elif isinstance(value, list) and len(value) >= 2:
        left = str(value[0]).strip()
        right = str(value[1]).strip()
        bidirectional = True
    elif isinstance(value, str) and ("->" in value or "→" in value):
        separator = "->" if "->" in value else "→"
        left, right = [part.strip() for part in value.split(separator, 1)]
        bidirectional = False
    elif isinstance(value, str) and ("--" in value or "↔" in value):
        separator = "--" if "--" in value else "↔"
        left, right = [part.strip() for part in value.split(separator, 1)]
        bidirectional = True
    else:
        return None
    if not left or not right:
        return None
    return left, right, bidirectional


def _connection_endpoints(value: Any) -> tuple[str, str] | None:
    parsed = _parse_connection(value)
    if parsed is None:
        return None
    return parsed[0], parsed[1]


def _connection_starts_at(value: Any, source: str) -> bool:
    parsed = _parse_connection(value)
    if parsed is None:
        return False
    left, right, bidirectional = parsed
    return left == source or (bidirectional and right == source)


__all__ = ["validate_bridge_preconditions", "validate_spatial"]
