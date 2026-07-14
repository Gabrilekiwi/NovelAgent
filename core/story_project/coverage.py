from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.schema import validate_schema


def blueprint_to_dict(chapter_blueprint: Any) -> dict[str, Any] | None:
    if chapter_blueprint is None:
        return None
    if hasattr(chapter_blueprint, "to_dict"):
        value = chapter_blueprint.to_dict()
    elif isinstance(chapter_blueprint, dict):
        value = dict(chapter_blueprint)
    else:
        return None
    return validate_schema(_json_path_strings(value), "chapter_blueprint.schema.json")


def validate_generation_blueprint_contract(chapter_blueprint: dict[str, Any]) -> None:
    missing = set(str(item) for item in chapter_blueprint.get("missing_fields") or [])
    required_beats = chapter_blueprint.get("required_beats")
    if not str(chapter_blueprint.get("core_event") or "").strip() or "core_event" in missing:
        raise ValueError("StoryProject generation requires chapter_blueprint.core_event.")
    if not isinstance(required_beats, list) or not required_beats or "required_beats" in missing:
        raise ValueError("StoryProject generation requires at least one chapter_blueprint.required_beats item.")
    if not str(chapter_blueprint.get("ending_pressure") or "").strip() or "ending_pressure" in missing:
        raise ValueError("StoryProject generation requires chapter_blueprint.ending_pressure.")


def build_blueprint_plan(
    chapter_blueprint: dict[str, Any],
    *,
    scene_limit: int | None = None,
) -> dict[str, Any]:
    validate_generation_blueprint_contract(chapter_blueprint)
    beats = _normalized_beats(chapter_blueprint)
    scene_count = len(beats)
    if scene_limit is not None:
        scene_count = min(scene_count, max(1, int(scene_limit)))
    groups = _group_beats(beats, scene_count)
    scenes: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        beat_indexes = [int(beat["index"]) for beat in group]
        beat_texts = [str(beat["text"]) for beat in group]
        scenes.append(
            {
                "index": index,
                "type": "story_project_blueprint",
                "goal": _scene_goal(chapter_blueprint, index, beat_texts),
                "required_beats": beat_texts,
                "required_beat_indexes": beat_indexes,
            }
        )
    return {
        "goal": str(chapter_blueprint.get("core_event") or chapter_blueprint.get("title") or "Follow StoryProject blueprint."),
        "scenes": scenes,
    }


def build_blueprint_coverage(
    chapter_blueprint: dict[str, Any],
    scene_drafts: list[dict[str, Any]],
    merged_chapter: str,
) -> dict[str, Any]:
    beats = _normalized_beats(chapter_blueprint)
    declared: set[int] = set()
    for scene in scene_drafts:
        for value in scene.get("covered_beat_indexes") or []:
            if isinstance(value, int) and not isinstance(value, bool):
                declared.add(value)
    covered: list[int] = []
    missing: list[int] = []
    for beat in beats:
        index = int(beat["index"])
        if index in declared and _text_covers(merged_chapter, str(beat["text"])):
            covered.append(index)
        else:
            missing.append(index)
    ending_pressure = str(chapter_blueprint.get("ending_pressure") or "").strip()
    ending_required = bool(ending_pressure)
    ending_covered = ending_required and _text_covers(_ending_window(merged_chapter), ending_pressure)
    return {
        "required_beat_count": len(beats),
        "covered_beat_indexes": covered,
        "missing_beat_indexes": missing,
        "ending_pressure_required": ending_required,
        "ending_pressure_covered": bool(ending_covered),
    }


def validate_blueprint_coverage(
    chapter_blueprint: dict[str, Any],
    blueprint_coverage: dict[str, Any],
) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    beats = {int(beat["index"]): str(beat["text"]) for beat in _normalized_beats(chapter_blueprint)}
    for index in blueprint_coverage.get("missing_beat_indexes") or []:
        beat_text = beats.get(int(index), "")
        problems.append(
            {
                "code": "missing_required_beat",
                "message": f"Generated chapter did not prove coverage of required beat {index}.",
                "beat_index": str(index),
                "beat_text": beat_text,
                "evidence": [
                    {"kind": "missing_required_beat", "value": f"{index}: {beat_text}" or str(index)},
                ],
            }
        )
    if blueprint_coverage.get("ending_pressure_required") and not blueprint_coverage.get("ending_pressure_covered"):
        ending_pressure = str(chapter_blueprint.get("ending_pressure") or "")
        problems.append(
            {
                "code": "missing_ending_pressure",
                "message": "Generated chapter ending did not prove coverage of ending_pressure.",
                "ending_pressure": ending_pressure,
                "evidence": [{"kind": "ending_pressure", "value": ending_pressure or "required"}],
            }
        )
    return {"name": "story_project", "ok": not problems, "problems": problems}


def _normalized_beats(chapter_blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    beats: list[dict[str, Any]] = []
    for position, raw in enumerate(chapter_blueprint.get("required_beats") or [], start=1):
        if not isinstance(raw, dict):
            text = str(raw).strip()
            index = position
        else:
            text = str(raw.get("text") or "").strip()
            raw_index = raw.get("index")
            index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else position
        if text:
            beats.append({"index": int(index), "text": text})
    return beats


def _group_beats(beats: list[dict[str, Any]], scene_count: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = [[] for _ in range(max(1, scene_count))]
    for offset, beat in enumerate(beats):
        groups[offset % len(groups)].append(beat)
    return [group for group in groups if group]


def _scene_goal(chapter_blueprint: dict[str, Any], index: int, beat_texts: list[str]) -> str:
    title = str(chapter_blueprint.get("title") or "").strip()
    prefix = f"{title}: " if title else ""
    return f"{prefix}Cover StoryProject required beat group {index}: " + "; ".join(beat_texts)


def _text_covers(text: str, required_text: str) -> bool:
    normalized_text = _normalize_text(text)
    normalized_required = _normalize_text(required_text)
    if not normalized_required:
        return False
    if normalized_required in normalized_text:
        return True
    terms = _coverage_terms(required_text)
    if not terms:
        return False
    matched = sum(1 for term in terms if _normalize_text(term) in normalized_text)
    required_matches = min(len(terms), 2) if len(terms) <= 3 else max(2, len(terms) // 2)
    if matched >= required_matches:
        return True
    return _cjk_bigram_coverage(text, required_text) >= 0.22


def _cjk_bigram_coverage(text: str, required_text: str) -> float:
    required = "".join(re.findall(r"[\u4e00-\u9fff]", required_text))
    actual = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    if len(required) < 8 or len(actual) < 2:
        return 0.0
    required_bigrams = {required[index:index + 2] for index in range(len(required) - 1)}
    actual_bigrams = {actual[index:index + 2] for index in range(len(actual) - 1)}
    matched = required_bigrams & actual_bigrams
    if len(matched) < 4:
        return 0.0
    return len(matched) / max(1, len(required_bigrams))


def _coverage_terms(value: str) -> list[str]:
    cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    latin_terms = re.findall(r"[A-Za-z0-9_]{4,}", value.lower())
    return [term for term in [*cjk_terms, *latin_terms] if term]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _ending_window(text: str) -> str:
    normalized = str(text or "").strip()
    return normalized[-1200:]


def _json_path_strings(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_path_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_path_strings(item) for item in value]
    if isinstance(value, tuple):
        return [_json_path_strings(item) for item in value]
    return value


__all__ = [
    "blueprint_to_dict",
    "build_blueprint_coverage",
    "build_blueprint_plan",
    "validate_blueprint_coverage",
    "validate_generation_blueprint_contract",
]
