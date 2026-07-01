from __future__ import annotations

import re
from typing import Any

from core.validator.common import extract_chapter_number


def validate_continuity(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
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

    return {"name": "continuity", "ok": not problems, "problems": problems}
