from __future__ import annotations

import re
from typing import Any


PROBLEM_METADATA = {
    "missing_chapter_index": {
        "severity": "critical",
        "blocking": True,
        "repair_action": "manual_review",
        "repair_hint": "Restore snapshot.chapter_index before generation can continue.",
    },
    "empty_chapter": {
        "severity": "critical",
        "blocking": True,
        "repair_action": "seed_conflict_scene",
        "repair_hint": "Regenerate a complete scene with danger, choice, and conflict.",
    },
    "chapter_index_mismatch": {
        "severity": "high",
        "blocking": True,
        "repair_action": "correct_chapter_index",
        "repair_hint": "Correct the declared chapter number to match the runtime snapshot.",
    },
    "inactive_character_action": {
        "severity": "high",
        "blocking": True,
        "repair_action": "rewrite_inactive_character_action",
        "repair_hint": "Rewrite the action as absence, memory, consequence, or another character's reaction.",
    },
    "no_known_location": {
        "severity": "medium",
        "blocking": True,
        "repair_action": "anchor_known_location",
        "repair_hint": "Anchor the scene to a known location or location alias from the snapshot.",
    },
    "character_unknown_location": {
        "severity": "high",
        "blocking": True,
        "repair_action": "flag_unknown_location",
        "repair_hint": "Use a known location for the character or keep the scene spatially explicit.",
    },
    "character_location_not_mentioned": {
        "severity": "medium",
        "blocking": True,
        "repair_action": "add_character_location",
        "repair_hint": "Mention the character together with their current known location.",
    },
    "chapter_too_short": {
        "severity": "medium",
        "blocking": True,
        "repair_action": "expand_scene",
        "repair_hint": "Expand the scene with concrete plot movement, consequence, and cost.",
    },
    "missing_conflict_marker": {
        "severity": "high",
        "blocking": True,
        "repair_action": "add_conflict_signal",
        "repair_hint": "Add explicit danger, choice, threat, secret, cost, or conflict.",
    },
    "forbidden_constraint_term": {
        "severity": "critical",
        "blocking": True,
        "repair_action": "remove_forbidden_term",
        "repair_hint": "Remove or replace the forbidden term while preserving unresolved tension.",
    },
    "missing_required_constraint_term": {
        "severity": "high",
        "blocking": True,
        "repair_action": "add_required_term",
        "repair_hint": "Mention the required term without resolving the constraint.",
    },
}

PROBLEM_PARAMETER_FIELDS = {
    "chapter_index_mismatch": ("expected", "actual"),
    "inactive_character_action": ("character",),
    "no_known_location": ("suggested_term",),
    "character_unknown_location": ("character", "location"),
    "character_location_not_mentioned": ("character", "location"),
    "forbidden_constraint_term": ("term",),
    "missing_required_constraint_term": ("term",),
}

STANDARD_PROBLEM_FIELDS = {
    "code",
    "message",
    "validator",
    "severity",
    "blocking",
    "category",
    "repair_hint",
    "repair_action",
    "repair_parameters",
    "evidence",
}


def contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term and (term.lower() in lowered) for term in terms)


def find_present_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term and term.lower() in lowered]


def get_constraints(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    constraints = snapshot.get("constraints") or []
    return [item for item in constraints if isinstance(item, dict)]


def get_locations(snapshot: dict[str, Any]) -> dict[str, Any]:
    world_state = snapshot.get("world_state") or {}
    locations = world_state.get("locations") or {}
    return locations if isinstance(locations, dict) else {}


def get_location_terms(name: str, data: Any) -> list[str]:
    terms = [name]
    if isinstance(data, dict):
        aliases = data.get("aliases") or []
        if isinstance(aliases, list):
            terms.extend(str(alias) for alias in aliases if alias)
    return terms


def extract_chapter_number(chapter_text: str) -> int | None:
    match = re.search(r"\bchapter\s+(\d+)\b", chapter_text, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def enrich_problem(problem: dict[str, Any], *, validator: str | None = None) -> dict[str, Any]:
    code = str(problem.get("code") or "unknown")
    metadata = PROBLEM_METADATA.get(
        code,
        {
            "severity": "medium",
            "blocking": True,
            "repair_action": "manual_review",
            "repair_hint": "Review this validation problem manually.",
        },
    )
    enriched = dict(problem)
    enriched["code"] = code
    enriched["message"] = str(enriched.get("message") or "")
    enriched.setdefault("severity", metadata["severity"])
    enriched.setdefault("blocking", bool(metadata["blocking"]))
    enriched.setdefault("category", "blocking" if enriched["blocking"] else "warning")
    enriched.setdefault("repair_action", str(metadata["repair_action"]))
    enriched.setdefault("repair_hint", metadata["repair_hint"])
    if validator and "validator" not in enriched:
        enriched["validator"] = validator
    enriched["repair_parameters"] = _repair_parameters(enriched, code)
    enriched["evidence"] = _problem_evidence(enriched)
    return enriched


def enrich_check(check: dict[str, Any]) -> dict[str, Any]:
    validator = str(check.get("name") or "") or None
    problems = [
        enrich_problem(problem, validator=validator)
        for problem in check.get("problems", [])
        if isinstance(problem, dict)
    ]
    enriched = dict(check)
    enriched["problems"] = problems
    enriched["ok"] = not any(problem["blocking"] for problem in problems)
    return enriched


def _repair_parameters(problem: dict[str, Any], code: str) -> dict[str, Any]:
    fields = PROBLEM_PARAMETER_FIELDS.get(code)
    if fields is None:
        return {
            "raw_problem": {
                str(key): value
                for key, value in problem.items()
                if key not in STANDARD_PROBLEM_FIELDS
            }
        }
    return {field: str(problem.get(field) or "") for field in fields}


def _problem_evidence(problem: dict[str, Any]) -> list[dict[str, str]]:
    raw_evidence = problem.get("evidence")
    if isinstance(raw_evidence, list):
        evidence = [_normalize_evidence_item(item) for item in raw_evidence]
        return [item for item in evidence if item is not None]

    evidence: list[dict[str, str]] = []
    message = str(problem.get("message") or "")
    if message:
        evidence.append({"kind": "message", "value": message})
    for field in ("expected", "actual", "character", "location", "term", "suggested_term", "actual_length", "minimum_length"):
        value = problem.get(field)
        if value not in (None, ""):
            evidence.append({"kind": field, "value": str(value)})
    return evidence


def _normalize_evidence_item(item: Any) -> dict[str, str] | None:
    if isinstance(item, dict):
        kind = str(item.get("kind") or "").strip()
        value = str(item.get("value") or "").strip()
        if kind and value:
            return {"kind": kind, "value": value}
    elif item not in (None, ""):
        return {"kind": "note", "value": str(item)}
    return None
