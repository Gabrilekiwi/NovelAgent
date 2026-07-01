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
_NAME_STOPWORDS = {"a", "an", "he", "it", "she", "the", "they", "we"}


def analyze_chapter(chapter_text: str, validation: dict[str, Any] | None = None) -> dict[str, Any]:
    text = chapter_text.strip()
    sentences = _split_sentences(text)
    conflicts = _detect_conflicts(text)
    world_changes = _extract_world_changes(text)

    analysis = {
        "summary": _build_summary(sentences),
        "events": _extract_events(sentences),
        "character_changes": _extract_character_changes(sentences),
        "world_changes": world_changes,
        "new_locations": _extract_candidate_locations(text),
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

    return changes[:10]


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
    location = value.strip(" ,.;:!?").lower()
    if location.startswith("the "):
        location = location[4:]
    for known_location in sorted(_KNOWN_LOCATION_TERMS, key=len, reverse=True):
        if known_location in location:
            return known_location
    return location
