from __future__ import annotations

import re
from typing import Any

from core.schema import validate_schema

_CONFLICT_MARKERS = [
    "conflict",
    "danger",
    "choice",
    "choose",
    "threat",
    "secret",
    "cost",
    "infection",
    "serum",
    "rescue",
    "\u51b2\u7a81",
    "\u5371\u9669",
    "\u9009\u62e9",
    "\u5a01\u80c1",
    "\u79d8\u5bc6",
    "\u4ee3\u4ef7",
]
_EVENT_MARKERS = [
    "had to",
    "must",
    "choose",
    "choice",
    "forced",
    "stood",
    "sounded",
    "entered",
    "found",
    "\u53d1\u73b0",
    "\u5fc5\u987b",
    "\u9009\u62e9",
]
_KNOWN_LOCATION_TERMS = [
    "shelter",
    "safehouse",
    "bridge",
    "sealed gate",
    "corridor",
    "\u907f\u96be\u6240",
    "A\u7ebf\u8f66\u53a2",
    "\u5907\u7528\u901a\u9053",
]
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s*")
_CHARACTER_STATUS_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_-]{1,40})\s+"
    r"(?:is|was|became|becomes|remained|remains)\s+"
    r"(dead|missing|injured|wounded|infected|unavailable|safe|alive)\b",
    re.IGNORECASE,
)
_CHARACTER_LOCATION_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_-]{1,40})\s+"
    r"(?:moved|retreated|returned|arrived|entered|stayed|remained)\s+"
    r"(?:to|at|in|inside)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9 _-]{1,60}?)(?=[.!?,;]|$)",
    re.IGNORECASE,
)
_CN_CHARACTER_LOCATION_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4})"
    r"(?:\u5728|\u8fdb\u5165|\u8fdb\u4e86|\u9000\u5230|\u8fd4\u56de|\u62b5\u8fbe|\u7559\u5728|\u7559\u5230)"
    r"([A-Za-z0-9\u4e00-\u9fff_-]{2,20})"
)
_NAME_STOPWORDS = {"a", "an", "he", "it", "she", "the", "they", "we"}


def analyze_chapter(chapter_text: str, validation: dict[str, Any] | None = None) -> dict[str, Any]:
    text = chapter_text.strip()
    sentences = _split_sentences(text)
    conflicts = _detect_conflicts(text)
    world_changes = _extract_world_changes(text)
    character_changes = _extract_character_changes(sentences)
    new_locations = _extract_candidate_locations(text)

    analysis = {
        "summary": _build_summary(sentences),
        "events": _extract_events(sentences),
        "character_changes": character_changes,
        "world_changes": world_changes,
        "new_locations": new_locations,
        "story_state": _extract_story_state(sentences, character_changes, new_locations),
        "spatial_state": _extract_spatial_state(character_changes, new_locations),
        "conflicts": conflicts,
        "validation_ok": bool((validation or {}).get("ok")),
    }
    return validate_schema(analysis, "analysis_result.schema.json")


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def _build_summary(sentences: list[str]) -> str:
    if not sentences:
        return ""
    return " ".join(sentences[:2])[:500]


def _detect_conflicts(text: str) -> list[str]:
    lowered = text.lower()
    return [marker for marker in _CONFLICT_MARKERS if marker in lowered or marker in text]


def _extract_events(sentences: list[str]) -> list[dict[str, str]]:
    events = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(marker in lowered or marker in sentence for marker in _EVENT_MARKERS):
            events.append({"text": sentence[:300]})
    return events[:5]


def _extract_world_changes(text: str) -> list[dict[str, str]]:
    lowered = text.lower()
    changes = []
    if "infection" in lowered or "\u611f\u67d3" in text:
        changes.append({"type": "infection_pressure", "text": "Infection pressure is active in the chapter."})
    if "serum" in lowered:
        changes.append({"type": "serum_focus", "text": "Serum remains narratively relevant."})
    return changes


def _extract_candidate_locations(text: str) -> list[str]:
    lowered = text.lower()
    return [term for term in _KNOWN_LOCATION_TERMS if term in lowered or term in text]


def _extract_story_state(
    sentences: list[str],
    character_changes: list[dict[str, str]],
    new_locations: list[str],
) -> dict[str, Any]:
    last_sentence = sentences[-1][:500] if sentences else ""
    last_location = _last_known_location(character_changes, new_locations)
    characters = _unique_strings(change.get("name") for change in character_changes)
    return {
        "last_chapter_ending": last_sentence,
        "last_scene_location": last_location,
        "last_scene_characters": characters,
        "open_threads": _open_threads(last_sentence),
        "required_opening_bridge": _required_opening_bridge(last_location, last_sentence),
    }


def _extract_spatial_state(
    character_changes: list[dict[str, str]],
    new_locations: list[str],
) -> dict[str, Any]:
    spaces = {location: {"source": "chapter_analysis"} for location in new_locations}
    character_positions = {
        change["name"]: change["current_location"]
        for change in character_changes
        if change.get("name") and change.get("current_location")
    }
    last_transition = {}
    if new_locations:
        last_transition = {"to": new_locations[-1], "source": "chapter_analysis"}
    return {
        "spaces": spaces,
        "connections": [],
        "character_positions": character_positions,
        "blocked_paths": [],
        "last_transition": last_transition,
    }


def _extract_character_changes(sentences: list[str]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for sentence in sentences:
        for match in _CHARACTER_STATUS_RE.finditer(sentence):
            name = _clean_name(match.group(1))
            status = match.group(2).lower()
            if not name:
                continue
            _append_unique(
                changes,
                seen,
                {
                    "name": name,
                    "status": status,
                    "text": sentence[:300],
                },
            )

        for match in _CHARACTER_LOCATION_RE.finditer(sentence):
            name = _clean_name(match.group(1))
            location = _clean_location(match.group(2))
            if not name or not location:
                continue
            _append_unique(
                changes,
                seen,
                {
                    "name": name,
                    "current_location": location,
                    "text": sentence[:300],
                },
            )

        for match in _CN_CHARACTER_LOCATION_RE.finditer(sentence):
            name = _clean_name(match.group(1))
            location = _clean_location(match.group(2))
            if not name or not location:
                continue
            _append_unique(
                changes,
                seen,
                {
                    "name": name,
                    "current_location": location,
                    "text": sentence[:300],
                },
            )

    return changes[:10]


def _last_known_location(character_changes: list[dict[str, str]], new_locations: list[str]) -> str:
    for change in reversed(character_changes):
        location = change.get("current_location")
        if location:
            return location
    return new_locations[-1] if new_locations else ""


def _required_opening_bridge(last_location: str, last_sentence: str) -> str:
    if not last_location:
        return ""
    return f"Continue from {last_location}: {last_sentence[:240]}" if last_sentence else f"Continue from {last_location}"


def _open_threads(last_sentence: str) -> list[str]:
    lowered = last_sentence.lower()
    threads: list[str] = []
    if any(term in lowered for term in ("choice", "choose", "conflict", "danger", "threat", "cost", "infection", "serum", "rescue")):
        threads.append(last_sentence[:240])
    return threads


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _append_unique(
    changes: list[dict[str, str]],
    seen: set[tuple[str, str, str]],
    change: dict[str, str],
) -> None:
    key = (
        change.get("name", "").lower(),
        change.get("status") or change.get("current_location") or "",
        change.get("text", ""),
    )
    if key in seen:
        return
    seen.add(key)
    changes.append(change)


def _clean_name(value: str) -> str:
    name = value.strip(" ,.;:!?")
    if name.lower() in _NAME_STOPWORDS:
        return ""
    return name


def _clean_location(value: str) -> str:
    raw_location = value.strip(" ,.;:!?，。；：！？")
    location = raw_location.lower()
    if location.startswith("the "):
        location = location[4:]
    for known_location in sorted(_KNOWN_LOCATION_TERMS, key=len, reverse=True):
        if known_location.lower() in location or known_location in raw_location:
            return known_location
    return location
