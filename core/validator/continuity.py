from __future__ import annotations

import re
from typing import Any

from core.validator.common import extract_chapter_number


_VOICE_GENDER_ALIASES = {
    "male": "male",
    "man": "male",
    "masculine": "male",
    "男": "male",
    "男性": "male",
    "男声": "male",
    "female": "female",
    "woman": "female",
    "feminine": "female",
    "女": "female",
    "女性": "female",
    "女声": "female",
}
_VOICE_GENDER_PATTERNS = (
    (
        "male",
        re.compile(r"男声|男性(?:的)?(?:声音|嗓音)|男人(?:的)?(?:声音|嗓音)"),
    ),
    (
        "female",
        re.compile(r"女声|女性(?:的)?(?:声音|嗓音)|女人(?:的)?(?:声音|嗓音)"),
    ),
)
_SPEECH_CUE_RE = re.compile(
    r"开口|说话|说道|说着|低声说|回答|答道|喊道|问道|声音|嗓音"
)
_NEGATED_SPEECH_RE = re.compile(
    r"(?:没有|并未|未曾|从未|不曾).{0,6}(?:开口|说话|回答|喊|发声)"
)
_REVERSE_ATTRIBUTION_RE = re.compile(
    r"(?:来自|属于|正是|发自|说话者是|开口的是|声音的主人是)"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?\n\r]")
_MAX_VOICE_BINDING_DISTANCE = 56


def validate_continuity(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    expected_index = snapshot.get("chapter_index")

    if expected_index is None:
        problems.append(
            {
                "code": "missing_chapter_index",
                "message": "Snapshot lacks chapter_index.",
                "evidence": [{"kind": "snapshot_field", "value": "chapter_index"}],
            }
        )

    if not chapter_text.strip():
        problems.append(
            {
                "code": "empty_chapter",
                "message": "Generated chapter is empty.",
                "actual_length": "0",
            }
        )

    chapter_number = extract_chapter_number(chapter_text)
    if chapter_number is not None and expected_index is not None and chapter_number != expected_index:
        problems.append(
            {
                "code": "chapter_index_mismatch",
                "message": f"Chapter text declares chapter {chapter_number}, expected {expected_index}.",
                "expected": str(expected_index),
                "actual": str(chapter_number),
                "evidence": [
                    {"kind": "declared_chapter", "value": str(chapter_number)},
                    {"kind": "snapshot_chapter_index", "value": str(expected_index)},
                ],
            }
        )

    characters = snapshot.get("characters") or {}
    action_markers = [" said", " walked", " ran", " smiled", " looked", " speaks", " shouted"]
    for name, data in characters.items():
        if not isinstance(data, dict):
            continue
        status = str(data.get("status", "")).lower()
        if status not in {"dead", "missing", "unavailable"}:
            continue
        pattern = re.compile(re.escape(str(name)) + r".{0,40}(" + "|".join(re.escape(marker.strip()) for marker in action_markers) + r")", re.IGNORECASE)
        if pattern.search(chapter_text):
            problems.append(
                {
                    "code": "inactive_character_action",
                    "message": f"Inactive character appears to take action: {name}.",
                    "character": str(name),
                    "evidence": [
                        {"kind": "character", "value": str(name)},
                        {"kind": "status", "value": status},
                    ],
                }
            )

    problems.extend(_voice_gender_conflicts(characters, chapter_text))

    return {"name": "continuity", "ok": not problems, "problems": problems}


def _voice_gender_conflicts(
    characters: Any,
    chapter_text: str,
) -> list[dict[str, Any]]:
    if not isinstance(characters, dict) or not chapter_text:
        return []

    mentions = _voice_gender_mentions(chapter_text)
    if not mentions:
        return []

    character_names = [str(name) for name in characters if str(name)]
    problems: list[dict[str, Any]] = []
    for raw_name, raw_data in characters.items():
        name = str(raw_name)
        if not name or not isinstance(raw_data, dict):
            continue
        expected = _snapshot_voice_gender(raw_data)
        if expected is None:
            continue
        expected_gender, fact_field = expected
        conflict = _bound_conflicting_voice(
            chapter_text,
            name=name,
            expected_gender=expected_gender,
            mentions=mentions,
            character_names=character_names,
        )
        if conflict is None:
            continue
        actual_gender, start, end = conflict
        fact_path = f"characters.{name}.{fact_field}"
        excerpt = chapter_text[start:end]
        problems.append(
            {
                "code": "character_voice_gender_conflict",
                "message": (
                    f"Voice gender attributed to {name} conflicts with the canonical "
                    f"snapshot fact {fact_path}."
                ),
                "character": name,
                "subject": name,
                "predicate": "voice_gender_consistency",
                "expected": expected_gender,
                "actual": actual_gender,
                "fact_id": f"snapshot:{fact_path}",
                "evidence": [
                    {"kind": "character", "value": name},
                    {"kind": "snapshot_fact_path", "value": fact_path},
                    {"kind": "snapshot_fact_value", "value": expected_gender},
                    {"kind": "chapter_span", "value": f"{start}:{end}"},
                    {"kind": "chapter_excerpt", "value": excerpt},
                ],
            }
        )
    return problems


def _snapshot_voice_gender(data: dict[str, Any]) -> tuple[str, str] | None:
    for field in ("voice_gender", "gender"):
        value = str(data.get(field) or "").strip().lower()
        normalized = _VOICE_GENDER_ALIASES.get(value)
        if normalized is not None:
            return normalized, field
    return None


def _voice_gender_mentions(text: str) -> list[tuple[str, int, int]]:
    mentions: list[tuple[str, int, int]] = []
    for gender, pattern in _VOICE_GENDER_PATTERNS:
        mentions.extend((gender, match.start(), match.end()) for match in pattern.finditer(text))
    return sorted(mentions, key=lambda item: (item[1], item[2], item[0]))


def _bound_conflicting_voice(
    text: str,
    *,
    name: str,
    expected_gender: str,
    mentions: list[tuple[str, int, int]],
    character_names: list[str],
) -> tuple[str, int, int] | None:
    name_matches = list(re.finditer(re.escape(name), text))
    for actual_gender, voice_start, voice_end in mentions:
        if actual_gender == expected_gender:
            continue
        for name_match in name_matches:
            binding = _voice_binding_span(
                text,
                name_start=name_match.start(),
                name_end=name_match.end(),
                voice_start=voice_start,
                voice_end=voice_end,
            )
            if binding is None:
                continue
            start, end, between = binding
            if _contains_other_character(
                between,
                current=name,
                character_names=character_names,
            ):
                continue
            return actual_gender, start, end
    return None


def _voice_binding_span(
    text: str,
    *,
    name_start: int,
    name_end: int,
    voice_start: int,
    voice_end: int,
) -> tuple[int, int, str] | None:
    if name_end <= voice_start:
        between = text[name_end:voice_start]
        if len(between) > _MAX_VOICE_BINDING_DISTANCE:
            return None
        if _SENTENCE_BOUNDARY_RE.search(between):
            return None
        if _NEGATED_SPEECH_RE.search(between):
            return None
        if not _SPEECH_CUE_RE.search(between):
            return None
        return name_start, voice_end, between

    if voice_end <= name_start:
        between = text[voice_end:name_start]
        if len(between) > _MAX_VOICE_BINDING_DISTANCE:
            return None
        if _SENTENCE_BOUNDARY_RE.search(between):
            return None
        if not _REVERSE_ATTRIBUTION_RE.search(between):
            return None
        return voice_start, name_end, between
    return None


def _contains_other_character(
    text: str,
    *,
    current: str,
    character_names: list[str],
) -> bool:
    return any(name != current and name and name in text for name in character_names)
