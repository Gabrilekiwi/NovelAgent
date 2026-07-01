from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable

from api.contracts import REPAIR_CONTRACT, validate_text_output
from api.openai_client import chat_completion
from core.schema import validate_schema
from modules.scene_repair.plan import build_repair_plan


_PROMPT_PATH = Path("prompts/repair_prompt.md")
RepairStrategy = Callable[[str, dict[str, Any], list[dict[str, Any]]], str]


def repair_scene(
    chapter_text: str,
    validation: dict[str, Any],
    input_pack: str,
    *,
    dry_run: bool = False,
    repair_plan: dict[str, Any] | None = None,
    recovery_context: dict[str, Any] | None = None,
) -> str:
    effective_plan = validate_schema(
        repair_plan if repair_plan is not None else build_repair_plan(validation, recovery_context=recovery_context),
        "repair_plan.schema.json",
    )
    if not dry_run:
        return _repair_with_model(chapter_text, validation, input_pack, effective_plan, recovery_context)
    return _repair_locally(chapter_text, validation, effective_plan)


def _repair_with_model(
    chapter_text: str,
    validation: dict[str, Any],
    input_pack: str,
    repair_plan: dict[str, Any],
    recovery_context: dict[str, Any] | None,
) -> str:
    output = chat_completion(
        [
            {"role": "system", "content": _load_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "chapter": chapter_text,
                        "validation": validation,
                        "repair_plan": repair_plan,
                        "recovery_context": recovery_context or {"available": False},
                        "input_pack": input_pack,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        temperature=0.2,
        stage="scene_repair",
    )
    return validate_text_output(output, REPAIR_CONTRACT)


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _repair_locally(chapter_text: str, validation: dict[str, Any], repair_plan: dict[str, Any]) -> str:
    repaired = chapter_text.strip()
    return apply_repair_plan(repaired, repair_plan)


def apply_repair_plan(chapter_text: str, repair_plan: dict[str, Any]) -> str:
    repaired = chapter_text.strip()
    steps = [step for step in repair_plan.get("steps", []) if isinstance(step, dict)]
    steps.sort(key=lambda step: (int(step.get("priority") or 0), int(step.get("index") or 0)))
    for step in steps:
        action = str(step.get("action") or "")
        strategy = REPAIR_STRATEGIES.get(action, _manual_review)
        repaired = strategy(repaired, step, steps).strip()
    return repaired


def _seed_conflict_scene(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    if text:
        return text
    return (
        "A local repair begins as the protagonist faces immediate danger, makes a costly choice, "
        "and pulls the team into open conflict."
    )


def _expand_scene(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    return (
        f"{text}\n\nThe decision carries a visible cost: retreat protects the serum, but rescue risks "
        "spreading the infection and splitting the team."
    )


def _add_conflict_signal(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    return f"{text}\n\nA new danger forces a clear choice, and the team conflict becomes impossible to avoid."


def _repair_forbidden_term(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    term = str(_parameters(step).get("term", "")).strip()
    return _remove_case_insensitive(text, term) if term else text


def _repair_required_term(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    term = str(_parameters(step).get("term", "")).strip()
    if not term:
        return text
    return f"{text}\n\nThe scene keeps focus on: {term}."


def _repair_known_location(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    location = str(_parameters(step).get("suggested_term", "")).strip()
    if not location:
        return text
    return f"{text}\n\nThe action remains anchored at {location}."


def _flag_unknown_location(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    character = str(_parameters(step).get("character", "The scene")).strip()
    return f"{text}\n\n{character} avoids relying on an unknown offstage location; the movement stays spatially explicit."


def _repair_character_location(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    character = str(_parameters(step).get("character", "the character")).strip()
    location = str(_parameters(step).get("location", "their current location")).strip()
    return f"{text}\n\n{character} remains at {location}, keeping the scene spatially consistent."


def _repair_inactive_character_action(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    character = str(_parameters(step).get("character", "")).strip()
    return _replace_character_action_sentence(text, character) if character else text


def _replace_character_action_sentence(text: str, character: str) -> str:
    pattern = re.compile(
        rf"[^.!?\n]*\b{re.escape(character)}\b[^.!?\n]*(?:[.!?]|$)",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        original = match.group(0)
        if not _contains_action_marker(original):
            return original
        return (
            f"{character} remains unavailable in this scene; the team reacts to that absence "
            "as the danger and conflict intensify."
        )

    return pattern.sub(replacement, text, count=1)


def _contains_action_marker(text: str) -> bool:
    return bool(re.search(r"\b(said|walked|ran|smiled|looked|speaks|shouted)\b", text, flags=re.IGNORECASE))


def _repair_chapter_index(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    expected = str(_parameters(step).get("expected", "")).strip()
    return _replace_declared_chapter(text, expected) if expected else text


def _manual_review(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    return text


def _parameters(step: dict[str, Any]) -> dict[str, Any]:
    parameters = step.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _remove_case_insensitive(text: str, term: str) -> str:
    replacement = "serum conflict remains unresolved" if "serum" in term.lower() else "the unresolved issue"
    return re.sub(re.escape(term), replacement, text, flags=re.IGNORECASE)


def _replace_declared_chapter(text: str, expected: str) -> str:
    if re.search(r"\bchapter\s+\d+\b", text, flags=re.IGNORECASE):
        return re.sub(r"\bchapter\s+\d+\b", f"Chapter {expected}", text, count=1, flags=re.IGNORECASE)
    return f"Chapter {expected}: {text}"


REPAIR_STRATEGIES: dict[str, RepairStrategy] = {
    "seed_conflict_scene": _seed_conflict_scene,
    "expand_scene": _expand_scene,
    "add_conflict_signal": _add_conflict_signal,
    "remove_forbidden_term": _repair_forbidden_term,
    "add_required_term": _repair_required_term,
    "anchor_known_location": _repair_known_location,
    "flag_unknown_location": _flag_unknown_location,
    "add_character_location": _repair_character_location,
    "rewrite_inactive_character_action": _repair_inactive_character_action,
    "correct_chapter_index": _repair_chapter_index,
    "manual_review": _manual_review,
}
