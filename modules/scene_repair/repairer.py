from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable

from api.contracts import REPAIR_CONTRACT, validate_text_output
from api.openai_client import chat_completion
from core.context_budget import default_context_budget
from core.prompt_compiler import compile_prompt_contexts
from core.schema import validate_schema
from core.structured_context import compact_markdown_context, select_json_items, sha256_text
from modules.scene_repair.plan import build_repair_plan


_PROMPT_PATH = Path("prompts/repair_prompt.md")
RepairStrategy = Callable[[str, dict[str, Any], list[dict[str, Any]]], str]


@dataclass(frozen=True)
class RepairContext:
    language: str = "en"
    allow_new_facts: bool = False
    known_conflict_hint: str | None = None


def repair_scene(
    chapter_text: str,
    validation: dict[str, Any],
    input_pack: str,
    *,
    dry_run: bool = False,
    repair_plan: dict[str, Any] | None = None,
    recovery_context: dict[str, Any] | None = None,
    language: str = "en",
    repair_context: RepairContext | None = None,
) -> str:
    context = repair_context or RepairContext(language=language or "en")
    effective_plan = validate_schema(
        repair_plan if repair_plan is not None else build_repair_plan(validation, recovery_context=recovery_context),
        "repair_plan.schema.json",
    )
    if not dry_run:
        return _repair_with_model(chapter_text, validation, input_pack, effective_plan, recovery_context, context)
    return _repair_locally(chapter_text, validation, effective_plan, context)


def _repair_with_model(
    chapter_text: str,
    validation: dict[str, Any],
    input_pack: str,
    repair_plan: dict[str, Any],
    recovery_context: dict[str, Any] | None,
    repair_context: RepairContext,
) -> str:
    repair_query = json.dumps(
        {
            "problem_codes": [str(item.get("code") or "") for item in validation.get("problems") or [] if isinstance(item, dict)],
            "repair_plan": repair_plan,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    compact_context = _compact_repair_context(
        compile_prompt_contexts(input_pack).repair.text,
        query=repair_query,
    )
    payload = json.dumps(
        {
            "chapter": chapter_text,
            "validation": _compact_validation(validation),
            "repair_plan": repair_plan,
            "recovery_context": recovery_context or {"available": False},
            "repair_context": {
                "language": _normalized_language(repair_context.language),
                "allow_new_facts": repair_context.allow_new_facts,
                "known_conflict_hint": repair_context.known_conflict_hint,
            },
            "context_digest_and_excerpts": compact_context,
        },
        ensure_ascii=False,
        indent=2,
    )
    default_context_budget().require_input(
        payload,
        stage="repair",
        protocol_texts=(_load_prompt(),),
    )
    output = chat_completion(
        [
            {"role": "system", "content": _load_prompt()},
            {
                "role": "user",
                "content": payload,
            },
        ],
        temperature=0.2,
        stage="scene_repair",
    )
    return validate_text_output(output, REPAIR_CONTRACT)


def _compact_validation(validation: dict[str, Any]) -> dict[str, Any]:
    problems = validation.get("problems")
    if not isinstance(problems, list):
        problems = []
        for check in validation.get("checks") or []:
            if isinstance(check, dict) and isinstance(check.get("problems"), list):
                problems.extend(check["problems"])
    compact_problems: list[dict[str, Any]] = []
    for raw in problems:
        if not isinstance(raw, dict):
            continue
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
        normalized_evidence = [
            {
                "kind": str(item.get("kind") or "evidence"),
                "value": str(item.get("value") or ""),
            }
            for item in evidence
            if isinstance(item, dict)
        ]
        evidence_selection = select_json_items(
            normalized_evidence,
            max_chars=2_400,
            query=f"{raw.get('code') or ''} {raw.get('message') or ''}",
            max_items=4,
            policy="repair_evidence_json_items_v1",
        )
        compact_problems.append(
            {
                key: raw.get(key)
                for key in ("code", "message", "validator", "severity", "blocking", "repair_action")
                if raw.get(key) is not None
            }
            | {
                "evidence": list(evidence_selection.items),
                "evidence_selection": dict(evidence_selection.manifest),
            }
        )
    required_problem_indexes = {
        index
        for index, problem in enumerate(compact_problems)
        if problem.get("blocking") is True
    }
    problem_selection = select_json_items(
        compact_problems,
        max_chars=16_000,
        query=" ".join(str(item.get("code") or "") for item in compact_problems),
        required_indexes=required_problem_indexes,
        prefer_recent=True,
        policy="repair_problem_json_items_v1",
    )
    selected_problems = list(problem_selection.items)
    serialized_validation = json.dumps(validation, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "ok": bool(validation.get("ok")),
        "requested_focus": list(validation.get("requested_focus") or []),
        "executed_checks": list(validation.get("executed_checks") or []),
        "skipped_checks": list(validation.get("skipped_checks") or []),
        "problem_codes": [str(item.get("code") or "") for item in selected_problems],
        "problems": selected_problems,
        "selection": {
            **dict(problem_selection.manifest),
            "source_sha256": sha256_text(serialized_validation),
            "original_chars": len(serialized_validation),
        },
    }


def _compact_repair_context(
    text: str,
    *,
    max_section_chars: int = 4_000,
    query: str = "",
) -> str:
    """Retrieve complete repair-relevant sections, paragraphs, and JSON items."""
    selection = compact_markdown_context(
        text,
        max_chars=max_section_chars * 5,
        per_section_max_chars=max_section_chars,
        query=query,
        required_sections={
            "Context Digest",
            "Prompt Context Selection",
            "Project Profile",
            "Story State",
            "Spatial State",
            "StoryProject Chapter Blueprint",
            "Requirements",
            "灏忚鐢熸垚瑙勫垯濂戠害",
        },
        excluded_sections={"Memory Index", "Structured Context Manifest"},
        required_json_keys={
            "StoryProject Chapter Blueprint": {"chapter_blueprint", "read_set_context_digest"},
        },
        prefer_recent=True,
        policy="repair_markdown_json_retrieval_v1",
    )
    return selection.text


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _repair_locally(
    chapter_text: str,
    validation: dict[str, Any],
    repair_plan: dict[str, Any],
    repair_context: RepairContext,
) -> str:
    repaired = chapter_text.strip()
    return apply_repair_plan(repaired, repair_plan, repair_context=repair_context)


def apply_repair_plan(
    chapter_text: str,
    repair_plan: dict[str, Any],
    *,
    language: str = "en",
    repair_context: RepairContext | None = None,
) -> str:
    context = repair_context or RepairContext(language=language or "en")
    strategies = REPAIR_STRATEGY_REGISTRY[_normalized_language(context.language)]
    repaired = chapter_text.strip()
    steps = [step for step in repair_plan.get("steps", []) if isinstance(step, dict)]
    steps.sort(key=lambda step: (int(step.get("priority") or 0), int(step.get("index") or 0)))
    for step in steps:
        action = str(step.get("action") or "")
        if (
            action == "add_conflict_signal"
            and _normalized_language(context.language) == "zh-CN"
            and context.known_conflict_hint
        ):
            step = dict(step)
            step["parameters"] = {
                **_parameters(step),
                "conflict_hint": context.known_conflict_hint,
            }
        strategy = strategies.get(action, _manual_review)
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
        f"{text}\n\nThe decision carries a visible cost: retreat protects what the team carries, but rescue risks "
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


def _insert_opening_bridge(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    bridge = str(parameters.get("bridge", "")).strip()
    location = str(parameters.get("location", "")).strip()
    bridge_text = _bridge_sentence(bridge, location)
    return _prepend_sentence(text, bridge_text)


def _rewrite_spatial_transition(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    expected = str(_parameters(step).get("expected", "")).strip()
    actual = str(_parameters(step).get("actual", "")).strip()
    if not expected or not actual:
        return text
    return _prepend_sentence(
        text,
        f"From {expected}, the movement into {actual} happens in view, with the last scene's pressure still driving every step.",
    )


def _anchor_last_scene_state(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    location = str(_parameters(step).get("location", "")).strip()
    character = str(_parameters(step).get("character", "")).strip()
    parts = [part for part in (location, character) if part]
    if not parts:
        return text
    if location:
        return _prepend_sentence(text, f"At {location}, {character or 'the group'} is still dealing with the last scene's immediate fallout.")
    return _prepend_sentence(text, f"{character} is still dealing with the last scene's immediate fallout.")


def _repair_character_position(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    character = str(parameters.get("character", "The character")).strip()
    expected = str(parameters.get("expected", "")).strip()
    actual = str(parameters.get("actual", "")).strip()
    if not expected:
        return text
    suffix = f" before any move toward {actual}" if actual else ""
    return f"{text}\n\n{character} starts at {expected}{suffix}, so the next movement follows from a clear position."


def _add_transition_event(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    expected = str(_parameters(step).get("expected", "")).strip()
    actual = str(_parameters(step).get("actual", "")).strip()
    if not expected or not actual:
        return text
    return f"{text}\n\nThe route from {expected} to {actual} becomes explicit before the scene commits to the new space."


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


def _zh_no_safe_repair(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    return text


def _zh_add_known_conflict_signal(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    hint = str(_parameters(step).get("conflict_hint", "")).strip()
    return f"{text}\n\n冲突焦点仍是：{hint}。" if hint else text


def _zh_repair_forbidden_term(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    term = str(_parameters(step).get("term", "")).strip()
    return re.sub(re.escape(term), "该事项", text, flags=re.IGNORECASE) if term else text


def _zh_repair_required_term(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    term = str(_parameters(step).get("term", "")).strip()
    return f"{text}\n\n场景仍围绕“{term}”推进。" if term else text


def _zh_repair_known_location(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    location = str(_parameters(step).get("suggested_term", "")).strip()
    return f"{text}\n\n行动始终发生在{location}。" if location else text


def _zh_insert_opening_bridge(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    bridge = str(_parameters(step).get("bridge", "")).strip()
    return _prepend_sentence(text, bridge) if bridge else text


def _zh_rewrite_spatial_transition(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    expected = str(parameters.get("expected", "")).strip()
    actual = str(parameters.get("actual", "")).strip()
    if not expected or not actual:
        return text
    return _prepend_sentence(text, f"从{expected}到{actual}的移动过程清晰发生在场景中")


def _zh_anchor_last_scene_state(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    location = str(parameters.get("location", "")).strip()
    character = str(parameters.get("character", "")).strip()
    if location and character:
        return _prepend_sentence(text, f"在{location}，{character}仍在承受上一场景的直接后果")
    if location:
        return _prepend_sentence(text, f"在{location}，上一场景的直接后果仍在延续")
    if character:
        return _prepend_sentence(text, f"{character}仍在承受上一场景的直接后果")
    return text


def _zh_repair_character_position(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    character = str(parameters.get("character", "")).strip()
    expected = str(parameters.get("expected", "")).strip()
    actual = str(parameters.get("actual", "")).strip()
    if not character or not expected:
        return text
    suffix = f"，随后才向{actual}移动" if actual else ""
    return f"{text}\n\n{character}起初位于{expected}{suffix}。"


def _zh_add_transition_event(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    expected = str(parameters.get("expected", "")).strip()
    actual = str(parameters.get("actual", "")).strip()
    if not expected or not actual:
        return text
    return f"{text}\n\n场景明确呈现了从{expected}前往{actual}的过程。"


def _zh_repair_character_location(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    parameters = _parameters(step)
    character = str(parameters.get("character", "")).strip()
    location = str(parameters.get("location", "")).strip()
    return f"{text}\n\n{character}仍在{location}。" if character and location else text


def _zh_repair_inactive_character_action(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    character = str(_parameters(step).get("character", "")).strip()
    if not character:
        return text
    pattern = re.compile(rf"[^。！？!?\n]*{re.escape(character)}[^。！？!?\n]*(?:[。！？!?]|$)")

    def replacement(match: re.Match[str]) -> str:
        sentence = match.group(0)
        if not re.search(r"说|走|跑|笑|看|喊|冲|抓|推|打开|关闭|拿起|放下", sentence):
            return sentence
        return ""

    return pattern.sub(replacement, text, count=1).strip()


def _zh_repair_chapter_index(text: str, step: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    expected = str(_parameters(step).get("expected", "")).strip()
    if not expected:
        return text
    rendered = _zh_integer(int(expected)) if expected.isdigit() else expected
    if re.search(r"第[零〇一二三四五六七八九十百千0-9]+章", text):
        return re.sub(r"第[零〇一二三四五六七八九十百千0-9]+章", f"第{rendered}章", text, count=1)
    if re.search(r"\bchapter\s+\d+\b", text, flags=re.IGNORECASE):
        return re.sub(r"\bchapter\s+\d+\b", f"第{rendered}章", text, count=1, flags=re.IGNORECASE)
    return f"第{rendered}章\n\n{text}"


def _zh_integer(value: int) -> str:
    if value == 0:
        return "零"
    if value < 0 or value > 9999:
        return str(value)
    digits = "零一二三四五六七八九"
    units = ("", "十", "百", "千")
    result: list[str] = []
    zero_pending = False
    text = str(value)
    for index, raw in enumerate(text):
        digit = int(raw)
        position = len(text) - index - 1
        if digit == 0:
            zero_pending = bool(result) and any(char != "0" for char in text[index + 1 :])
            continue
        if zero_pending:
            result.append("零")
            zero_pending = False
        if not (digit == 1 and position == 1 and not result):
            result.append(digits[digit])
        result.append(units[position])
    return "".join(result)


def _prepend_sentence(text: str, sentence: str) -> str:
    sentence = sentence.strip()
    if not sentence:
        return text
    if sentence[-1] not in ".!?。！？":
        sentence = f"{sentence}。" if re.search(r"[\u4e00-\u9fff]", sentence) else f"{sentence}."
    return f"{sentence}\n\n{text}" if text else sentence


def _bridge_sentence(bridge: str, location: str) -> str:
    if bridge:
        cleaned = bridge.strip()
        if ":" in cleaned:
            prefix, detail = cleaned.split(":", 1)
            detail = detail.strip()
            if detail:
                return f"From {location or prefix.strip()}, {detail[0].lower()}{detail[1:]}"
        return cleaned
    return f"From {location}, the next moment carries the last scene's consequence forward" if location else ""


def _parameters(step: dict[str, Any]) -> dict[str, Any]:
    parameters = step.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _remove_case_insensitive(text: str, term: str) -> str:
    replacement = re.sub(r"\bresolved\b", "remains unresolved", term, count=1, flags=re.IGNORECASE)
    if replacement == term:
        replacement = "the unresolved issue"
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
    "insert_opening_bridge": _insert_opening_bridge,
    "rewrite_spatial_transition": _rewrite_spatial_transition,
    "anchor_last_scene_state": _anchor_last_scene_state,
    "repair_character_position": _repair_character_position,
    "add_transition_event": _add_transition_event,
    "flag_unknown_location": _flag_unknown_location,
    "add_character_location": _repair_character_location,
    "rewrite_inactive_character_action": _repair_inactive_character_action,
    "correct_chapter_index": _repair_chapter_index,
    "manual_review": _manual_review,
}

ZH_CN_REPAIR_STRATEGIES: dict[str, RepairStrategy] = {
    "seed_conflict_scene": _zh_no_safe_repair,
    "expand_scene": _zh_no_safe_repair,
    "add_conflict_signal": _zh_add_known_conflict_signal,
    "remove_forbidden_term": _zh_repair_forbidden_term,
    "add_required_term": _zh_repair_required_term,
    "anchor_known_location": _zh_repair_known_location,
    "insert_opening_bridge": _zh_insert_opening_bridge,
    "rewrite_spatial_transition": _zh_rewrite_spatial_transition,
    "anchor_last_scene_state": _zh_anchor_last_scene_state,
    "repair_character_position": _zh_repair_character_position,
    "add_transition_event": _zh_add_transition_event,
    "flag_unknown_location": _zh_no_safe_repair,
    "add_character_location": _zh_repair_character_location,
    "rewrite_inactive_character_action": _zh_repair_inactive_character_action,
    "correct_chapter_index": _zh_repair_chapter_index,
    "manual_review": _manual_review,
}

REPAIR_STRATEGY_REGISTRY: dict[str, dict[str, RepairStrategy]] = {
    "en": REPAIR_STRATEGIES,
    "zh-CN": ZH_CN_REPAIR_STRATEGIES,
}


def _normalized_language(value: str) -> str:
    normalized = str(value or "en").strip().lower().replace("_", "-")
    if normalized in {"zh", "zh-cn", "zh-hans", "chinese"}:
        return "zh-CN"
    if normalized in {"", "en", "en-us", "en-gb", "english"}:
        return "en"
    raise ValueError(f"unsupported local repair language: {value}")
