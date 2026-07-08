from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any

from core.schema import validate_schema
from core.state.snapshot import SnapshotError, validate_snapshot


OPENING_WINDOW_CHARS = 1000
SHORT_CHAPTER_WARNING_CHARS = 800
SHORT_CHAPTER_FAIL_CHARS = 300
LONG_CHAPTER_WARNING_CHARS = 12000

CHECK_ORDER = (
    "snapshot_compatibility",
    "continues_previous_ending",
    "preserves_last_scene_location",
    "preserves_last_scene_characters",
    "advances_open_threads_or_conflicts",
    "avoids_premature_resolution",
    "no_meta_output",
    "language_consistency",
    "repetition_or_stalling",
    "chapter_length_reasonable",
)

FAIL_DEDUCTIONS = {
    "critical": 35,
    "high": 25,
    "medium": 15,
    "low": 5,
}
WARNING_DEDUCTIONS = {
    "critical": 20,
    "high": 12,
    "medium": 8,
    "low": 3,
}

META_MARKERS = (
    "作为AI",
    "作为 AI",
    "以下是",
    "本章分析",
    "章节分析",
    "分析：",
    "总结：",
    "```json",
    "# Chapter",
    "## ",
)

PREMATURE_THREAD_MARKERS = (
    "未解决",
    "仍未解决",
    "不能解决",
    "不要回收",
    "不要提前",
)
RESOLUTION_MARKERS = (
    "终于解决",
    "彻底解决",
    "谜底揭晓",
    "真相大白",
    "一切结束",
)

STALLING_MARKERS = (
    "他不知道",
    "她不知道",
    "他们不知道",
    "沉默",
    "空气凝固",
    "一时无言",
)


def evaluate_chapter_quality(
    *,
    chapter_text: str,
    snapshot: dict,
    previous_chapter_text: str | None = None,
    language: str | None = None,
) -> dict:
    snapshot_copy = copy.deepcopy(snapshot)
    text = str(chapter_text or "")
    target_language = language or _snapshot_language(snapshot_copy)

    checks = [
        _check_snapshot_compatibility(snapshot_copy),
        _check_continues_previous_ending(text, snapshot_copy, previous_chapter_text),
        _check_preserves_last_scene_location(text, snapshot_copy),
        _check_preserves_last_scene_characters(text, snapshot_copy),
        _check_advances_open_threads_or_conflicts(text, snapshot_copy),
        _check_avoids_premature_resolution(text, snapshot_copy),
        _check_no_meta_output(text),
        _check_language_consistency(text, target_language),
        _check_repetition_or_stalling(text),
        _check_chapter_length_reasonable(text, target_language),
    ]
    checks = sorted(checks, key=lambda check: CHECK_ORDER.index(check["code"]))
    score = _score_checks(checks)
    report = {
        "schema_version": "1.0",
        "status": _overall_status(checks, score),
        "score": score,
        "summary": _summary(checks),
        "checks": checks,
        "metrics": _metrics(text),
        "snapshot_refs": _snapshot_refs(snapshot_copy),
    }
    return validate_schema(report, "chapter_quality_report.schema.json")


def _check_snapshot_compatibility(snapshot: Any) -> dict[str, Any]:
    try:
        if not isinstance(snapshot, dict):
            raise SnapshotError("snapshot must be an object")
        validate_snapshot(snapshot)
    except Exception as exc:  # noqa: BLE001 - quality reports validation failures as checks.
        return _check(
            "snapshot_compatibility",
            "fail",
            "critical",
            "Snapshot cannot be validated for chapter quality evaluation.",
            {"error": str(exc)},
        )
    return _check(
        "snapshot_compatibility",
        "pass",
        "critical",
        "Snapshot is compatible with the runtime snapshot schema.",
        {},
    )


def _check_continues_previous_ending(text: str, snapshot: dict, previous_chapter_text: str | None) -> dict[str, Any]:
    story_state = _dict(snapshot.get("story_state"))
    opening = _opening(text)
    bridge = _clean_text(story_state.get("required_opening_bridge"))
    last_ending = _clean_text(story_state.get("last_chapter_ending"))
    previous_tail = _previous_tail(previous_chapter_text)
    terms = _terms([bridge, last_ending, previous_tail])
    if not terms:
        return _check(
            "continues_previous_ending",
            "skip",
            "high",
            "No previous-ending context is available.",
            {},
        )

    matched = _matched_terms(opening, terms)
    required_count = 1 if bridge else min(2, len(terms))
    if len(matched) >= required_count:
        return _check(
            "continues_previous_ending",
            "pass",
            "high",
            "Chapter opening appears to continue from the previous ending.",
            {"matched_terms": matched[:8], "candidate_terms": terms[:12]},
        )
    return _check(
        "continues_previous_ending",
        "warning",
        "high",
        "Chapter opening does not clearly connect to the previous ending.",
        {"matched_terms": matched, "candidate_terms": terms[:12]},
    )


def _check_preserves_last_scene_location(text: str, snapshot: dict) -> dict[str, Any]:
    story_state = _dict(snapshot.get("story_state"))
    location = _clean_text(story_state.get("last_scene_location"))
    if not location:
        return _check(
            "preserves_last_scene_location",
            "skip",
            "high",
            "No last-scene location is available.",
            {},
        )

    opening = _opening(text)
    if location in opening:
        return _check(
            "preserves_last_scene_location",
            "pass",
            "high",
            "Chapter opening preserves the last-scene location.",
            {"last_scene_location": location},
        )

    other_locations = [name for name in _known_locations(snapshot) if name != location and name in opening]
    return _check(
        "preserves_last_scene_location",
        "warning",
        "high",
        "Chapter opening may have shifted away from the last-scene location.",
        {"last_scene_location": location, "new_locations_in_opening": other_locations[:8]},
    )


def _check_preserves_last_scene_characters(text: str, snapshot: dict) -> dict[str, Any]:
    story_state = _dict(snapshot.get("story_state"))
    characters = [item for item in _string_items(story_state.get("last_scene_characters")) if item]
    if not characters:
        return _check(
            "preserves_last_scene_characters",
            "skip",
            "high",
            "No last-scene characters are available.",
            {},
        )

    early_text = text[: max(OPENING_WINDOW_CHARS, len(text) // 2)]
    matched = [name for name in characters if name in early_text]
    if matched:
        status = "pass" if len(matched) == len(characters) else "warning"
        message = (
            "Chapter keeps last-scene characters in the early chapter."
            if status == "pass"
            else "Chapter keeps some, but not all, last-scene characters in the early chapter."
        )
        return _check(
            "preserves_last_scene_characters",
            status,
            "high",
            message,
            {"expected_characters": characters, "matched_characters": matched},
        )

    return _check(
        "preserves_last_scene_characters",
        "fail",
        "high",
        "Chapter does not mention last-scene characters early enough.",
        {"expected_characters": characters, "matched_characters": []},
    )


def _check_advances_open_threads_or_conflicts(text: str, snapshot: dict) -> dict[str, Any]:
    thread_sources = _thread_sources(snapshot)
    terms = _terms(thread_sources)
    if not terms:
        return _check(
            "advances_open_threads_or_conflicts",
            "skip",
            "medium",
            "No open threads or active conflicts are available.",
            {},
        )

    matched = _matched_terms(text, terms)
    if matched:
        return _check(
            "advances_open_threads_or_conflicts",
            "pass",
            "medium",
            "Chapter touches current threads or conflicts.",
            {"matched_terms": matched[:12], "candidate_terms": terms[:16]},
        )
    return _check(
        "advances_open_threads_or_conflicts",
        "warning",
        "medium",
        "Chapter does not clearly touch current threads or conflicts.",
        {"candidate_terms": terms[:16]},
    )


def _check_avoids_premature_resolution(text: str, snapshot: dict) -> dict[str, Any]:
    thread_sources = _thread_sources(snapshot)
    if not thread_sources:
        return _check(
            "avoids_premature_resolution",
            "skip",
            "medium",
            "No open threads are available for premature-resolution checks.",
            {},
        )

    guarded_threads = [
        source
        for source in thread_sources
        if any(marker in source for marker in PREMATURE_THREAD_MARKERS)
    ]
    matched_resolution = [marker for marker in RESOLUTION_MARKERS if marker in text]
    if guarded_threads and matched_resolution:
        return _check(
            "avoids_premature_resolution",
            "warning",
            "high",
            "Chapter may resolve a guarded open thread too early.",
            {"guarded_threads": guarded_threads[:6], "resolution_markers": matched_resolution},
        )
    return _check(
        "avoids_premature_resolution",
        "pass",
        "medium",
        "No premature resolution marker was detected.",
        {"guarded_thread_count": len(guarded_threads)},
    )


def _check_no_meta_output(text: str) -> dict[str, Any]:
    matched = [marker for marker in META_MARKERS if marker in text]
    if matched:
        return _check(
            "no_meta_output",
            "fail",
            "critical",
            "Chapter includes meta, analysis, JSON, or Markdown-like non-prose output.",
            {"matched_markers": matched},
        )
    return _check(
        "no_meta_output",
        "pass",
        "critical",
        "Chapter appears to contain prose only.",
        {},
    )


def _check_language_consistency(text: str, language: str | None) -> dict[str, Any]:
    if language != "zh-CN":
        return _check(
            "language_consistency",
            "skip",
            "high",
            "No zh-CN language requirement is active.",
            {"language": language},
        )

    cjk_count = _cjk_count(text)
    english_count = len(re.findall(r"[A-Za-z]", text))
    total_language_chars = cjk_count + english_count
    ratio = cjk_count / total_language_chars if total_language_chars else 0.0
    evidence = {"language": language, "cjk_count": cjk_count, "english_count": english_count, "cjk_ratio": round(ratio, 3)}
    if cjk_count < 50 or ratio < 0.45:
        return _check(
            "language_consistency",
            "fail",
            "high",
            "Chapter does not appear to satisfy the zh-CN language requirement.",
            evidence,
        )
    if ratio < 0.7:
        return _check(
            "language_consistency",
            "warning",
            "high",
            "Chapter contains substantial non-Chinese text for a zh-CN target.",
            evidence,
        )
    return _check(
        "language_consistency",
        "pass",
        "high",
        "Chapter satisfies the zh-CN language requirement.",
        evidence,
    )


def _check_repetition_or_stalling(text: str) -> dict[str, Any]:
    paragraphs = _paragraphs(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    repeated_paragraphs = _repeated_items(paragraphs, threshold=2)
    repeated_lines = _repeated_items(lines, threshold=2)
    stalling_counts = {marker: text.count(marker) for marker in STALLING_MARKERS if text.count(marker) > 0}
    stalling_total = sum(stalling_counts.values())
    evidence = {
        "repeated_paragraphs": repeated_paragraphs[:5],
        "repeated_lines": repeated_lines[:5],
        "stalling_markers": stalling_counts,
    }
    if repeated_paragraphs or repeated_lines or stalling_total > 5:
        return _check(
            "repetition_or_stalling",
            "warning",
            "medium",
            "Chapter may contain repeated or stalling prose.",
            evidence,
        )
    return _check(
        "repetition_or_stalling",
        "pass",
        "medium",
        "No obvious repeated or stalling prose was detected.",
        evidence,
    )


def _check_chapter_length_reasonable(text: str, language: str | None) -> dict[str, Any]:
    measured_length = _cjk_count(text) if language == "zh-CN" else len(text.strip())
    evidence = {"measured_length": measured_length, "language": language}
    if measured_length < SHORT_CHAPTER_FAIL_CHARS:
        return _check(
            "chapter_length_reasonable",
            "fail",
            "medium",
            "Chapter is too short to be a full next chapter.",
            evidence,
        )
    if measured_length < SHORT_CHAPTER_WARNING_CHARS:
        return _check(
            "chapter_length_reasonable",
            "warning",
            "medium",
            "Chapter is shorter than expected.",
            evidence,
        )
    if measured_length > LONG_CHAPTER_WARNING_CHARS:
        return _check(
            "chapter_length_reasonable",
            "warning",
            "low",
            "Chapter is longer than the current heuristic range.",
            evidence,
        )
    return _check(
        "chapter_length_reasonable",
        "pass",
        "medium",
        "Chapter length is within the current heuristic range.",
        evidence,
    )


def _check(code: str, status: str, severity: str, message: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": code,
        "status": status,
        "severity": severity,
        "message": message,
        "evidence": evidence,
    }


def _score_checks(checks: list[dict[str, Any]]) -> int:
    score = 100
    for check in checks:
        severity = str(check.get("severity") or "low")
        if check.get("status") == "fail":
            score -= FAIL_DEDUCTIONS.get(severity, 5)
        elif check.get("status") == "warning":
            score -= WARNING_DEDUCTIONS.get(severity, 3)
    return max(0, min(100, score))


def _overall_status(checks: list[dict[str, Any]], score: int) -> str:
    if any(check["status"] == "fail" and check["severity"] in {"critical", "high"} for check in checks):
        return "fail"
    if score < 60:
        return "fail"
    if any(check["status"] == "warning" for check in checks) or score < 85:
        return "warning"
    return "pass"


def _summary(checks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for check in checks if check["status"] == "pass"),
        "warnings": sum(1 for check in checks if check["status"] == "warning"),
        "failed": sum(1 for check in checks if check["status"] == "fail"),
        "skipped": sum(1 for check in checks if check["status"] == "skip"),
    }


def _metrics(text: str) -> dict[str, Any]:
    paragraphs = _paragraphs(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "chapter_length": len(text.strip()),
        "cjk_character_count": _cjk_count(text),
        "paragraph_count": len(paragraphs),
        "dialogue_line_count": sum(1 for line in lines if line.startswith(("“", "\"", "「", "『", "- "))),
        "repeated_line_count": len(_repeated_items(lines, threshold=2)),
    }


def _snapshot_refs(snapshot: dict) -> dict[str, Any]:
    story_state = _dict(snapshot.get("story_state"))
    return {
        "last_scene_location": _clean_text(story_state.get("last_scene_location")),
        "last_scene_characters": _string_items(story_state.get("last_scene_characters")),
        "open_thread_count": len(_string_items(story_state.get("open_threads"))) + len(_string_items(snapshot.get("open_threads"))),
        "active_conflict_count": len(_string_items(story_state.get("active_conflicts"))) + len(_string_items(snapshot.get("active_conflicts"))),
    }


def _snapshot_language(snapshot: dict) -> str | None:
    profile = _dict(snapshot.get("project_profile"))
    value = profile.get("language")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _thread_sources(snapshot: dict) -> list[str]:
    story_state = _dict(snapshot.get("story_state"))
    sources: list[str] = []
    for value in (
        story_state.get("open_threads"),
        story_state.get("active_conflicts"),
        snapshot.get("open_threads"),
        snapshot.get("active_conflicts"),
        snapshot.get("active_constraints"),
    ):
        sources.extend(_string_items(value))
    return sources


def _known_locations(snapshot: dict) -> list[str]:
    names: list[str] = []
    world_state = _dict(snapshot.get("world_state"))
    locations = _dict(world_state.get("locations"))
    names.extend(str(name) for name in locations if str(name).strip())
    spatial_state = _dict(snapshot.get("spatial_state"))
    spaces = _dict(spatial_state.get("spaces"))
    names.extend(str(name) for name in spaces if str(name).strip())
    return sorted(set(names), key=len, reverse=True)


def _opening(text: str) -> str:
    return text[:OPENING_WINDOW_CHARS]


def _previous_tail(previous_chapter_text: str | None) -> str:
    if not previous_chapter_text:
        return ""
    paragraphs = _paragraphs(previous_chapter_text)
    if paragraphs:
        return paragraphs[-1][-500:]
    return previous_chapter_text[-500:]


def _terms(values: Any) -> list[str]:
    raw_text = " ".join(_flatten_strings(values))
    terms: list[str] = []
    terms.extend(match.group(0) for match in re.finditer(r"[\u4e00-\u9fff]{2,}", raw_text))
    terms.extend(match.group(0) for match in re.finditer(r"[A-Za-z0-9_]{3,}", raw_text))
    cleaned = []
    for term in terms:
        item = term.strip(" ，。！？；：、,.!?;:\"'`[]()（）")
        if len(item) >= 2 and item not in cleaned:
            cleaned.append(item)
    return cleaned


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term and term in text]


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_flatten_strings(item))
        return strings
    if isinstance(value, list) or isinstance(value, tuple):
        strings = []
        for item in value:
            strings.extend(_flatten_strings(item))
        return strings
    return []


def _string_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return _flatten_strings(value)
    if isinstance(value, dict):
        return _flatten_strings(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _repeated_items(items: list[str], *, threshold: int) -> list[str]:
    counts = Counter(items)
    return [item for item, count in counts.items() if item and count > threshold]


def _cjk_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


__all__ = [
    "LONG_CHAPTER_WARNING_CHARS",
    "SHORT_CHAPTER_FAIL_CHARS",
    "SHORT_CHAPTER_WARNING_CHARS",
    "evaluate_chapter_quality",
]
