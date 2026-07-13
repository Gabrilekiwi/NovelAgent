from __future__ import annotations

import hashlib
import json
from typing import Any

from api.openai_client import chat_completion
from core.config import get_config
from core.quality_decision import QUALITY_POLICY_VERSION
from core.schema import validate_schema


LLM_VALIDATION_AREAS = (
    "complex_plot_logic",
    "character_motivation_consistency",
    "timeline_causality",
    "setup_and_payoff",
    "emotional_and_theme_drift",
)


def validate_llm(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    selected_model = model or get_config().openai_model
    messages = _llm_messages(snapshot, chapter_text, decision)
    payload = _call_llm_validator(messages, model=selected_model)
    check = llm_payload_to_check(payload)
    check["metadata"] = {
        "provider": "openai",
        "model": selected_model,
        "prompt_hash": hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "policy_version": QUALITY_POLICY_VERSION,
        "attempt_history": [{"attempt": 1, "status": "succeeded"}],
    }
    return check


def llm_payload_to_check(payload: dict[str, Any]) -> dict[str, Any]:
    checked = validate_schema(payload, "llm_validation.schema.json")
    problems = [_normalize_llm_problem(problem) for problem in checked.get("problems", [])]
    return {
        "name": "llm",
        "ok": not any(problem["blocking"] for problem in problems),
        "problems": problems,
    }


def _call_llm_validator(
    messages: list[dict[str, str]],
    *,
    model: str,
) -> dict[str, Any]:
    response = chat_completion(
        messages,
        model=model,
        temperature=0.0,
        stage="llm_validation",
    )
    try:
        payload = json.loads(response)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM validator response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("LLM validator response must be a JSON object")
    return payload


def _llm_messages(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a strict fiction continuity validator. Return only JSON matching "
                "the supplied schema. Report high-signal story problems; do not rewrite prose."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "schema": _llm_output_schema_hint(),
                    "check_areas": list(LLM_VALIDATION_AREAS),
                    "snapshot": snapshot,
                    "decision": decision or {},
                    "chapter": chapter_text,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def _normalize_llm_problem(problem: dict[str, Any]) -> dict[str, Any]:
    severity = str(problem.get("severity") or "medium")
    blocking = severity in {"critical", "high", "medium"}
    area = str(problem.get("area") or "complex_plot_logic")
    return {
        "code": str(problem.get("code") or "llm_story_problem"),
        "message": str(problem.get("message") or ""),
        "validator": "llm",
        "area": area,
        "severity": severity,
        "blocking": blocking,
        "category": "blocking" if blocking else "warning",
        "repair_hint": str(problem.get("repair_hint") or "Review and repair this story-level issue manually."),
        "repair_action": "manual_review",
        "repair_parameters": {"area": area, "raw_problem": dict(problem)},
        "evidence": list(problem.get("evidence") or []),
    }


def _llm_output_schema_hint() -> dict[str, Any]:
    return {
        "problems": [
            {
                "code": "llm_character_motivation_inconsistent",
                "message": "Short human-readable problem.",
                "area": "character_motivation_consistency",
                "severity": "high|medium|low|critical",
                "evidence": [{"kind": "chapter_excerpt_or_fact", "value": "Concrete evidence."}],
                "repair_hint": "Specific scene-level repair guidance.",
            }
        ]
    }


__all__ = ["LLM_VALIDATION_AREAS", "llm_payload_to_check", "validate_llm"]
