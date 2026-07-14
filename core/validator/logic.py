from __future__ import annotations

from typing import Any

from core.validator.common import find_present_terms, get_constraints

_CONFLICT_MARKERS = [
    "conflict",
    "danger",
    "choice",
    "choose",
    "threat",
    "secret",
    "cost",
    "\u51b2\u7a81",
    "\u5371\u9669",
    "\u9009\u62e9",
    "\u5a01\u80c1",
    "\u79d8\u5bc6",
    "\u4ee3\u4ef7",
    "\u88ad\u51fb",
    "\u54ac",
    "\u649e\u51fb",
    "\u5c16\u53eb",
    "\u53cd\u6740",
    "\u640f\u6597",
]


def validate_logic(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
    text = chapter_text.strip()
    lowered = text.lower()

    if len(text) < 80:
        problems.append(
            {
                "code": "chapter_too_short",
                "message": "Chapter is too short to prove plot progression.",
                "actual_length": str(len(text)),
                "minimum_length": "80",
            }
        )

    if not any(marker in lowered or marker in text for marker in _CONFLICT_MARKERS):
        problems.append(
            {
                "code": "missing_conflict_marker",
                "message": "Chapter does not show an obvious conflict signal.",
                "evidence": [
                    {"kind": "missing_any_marker", "value": ", ".join(_CONFLICT_MARKERS[:6])},
                ],
            }
        )

    for constraint in get_constraints(snapshot):
        _validate_constraint_terms(problems, text, lowered, constraint)

    return {"name": "logic", "ok": not problems, "problems": problems}


def _validate_constraint_terms(
    problems: list[dict[str, str]],
    text: str,
    lowered: str,
    constraint: dict[str, Any],
) -> None:
    forbidden_terms = constraint.get("forbidden_terms") or constraint.get("must_not_contain") or []
    required_terms = constraint.get("required_terms") or constraint.get("must_contain") or []

    if isinstance(forbidden_terms, list):
        for term in find_present_terms(text, [str(term) for term in forbidden_terms]):
            problems.append(
                {
                    "code": "forbidden_constraint_term",
                    "message": f"Chapter contains forbidden constraint term: {term}.",
                    "term": term,
                    "evidence": [{"kind": "matched_forbidden_term", "value": term}],
                }
            )

    if isinstance(required_terms, list):
        for term in required_terms:
            clean_term = str(term)
            if clean_term and clean_term.lower() not in lowered:
                problems.append(
                    {
                        "code": "missing_required_constraint_term",
                        "message": f"Chapter misses required constraint term: {clean_term}.",
                        "term": clean_term,
                        "evidence": [{"kind": "missing_required_term", "value": clean_term}],
                    }
                )
