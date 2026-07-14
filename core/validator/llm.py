from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from api.openai_client import chat_completion
from api.retry import retry_telemetry_snapshot
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
    telemetry_offset = len(retry_telemetry_snapshot())
    payload = _call_llm_validator(messages, model=selected_model)
    retry_reports = retry_telemetry_snapshot()[telemetry_offset:]
    check = llm_payload_to_check(payload, chapter_text=chapter_text)
    check["metadata"] = {
        "provider": "openai",
        "model": selected_model,
        "prompt_hash": hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "policy_version": QUALITY_POLICY_VERSION,
        "attempt_history": [
            {
                "profile": report["profile"],
                "stop_reason": report["stop_reason"],
                **attempt,
            }
            for report in retry_reports
            for attempt in report["history"]
        ]
        or [{"attempt": 1, "status": "succeeded"}],
    }
    return check


def llm_payload_to_check(
    payload: dict[str, Any],
    *,
    chapter_text: str,
) -> dict[str, Any]:
    checked = validate_schema(payload, "llm_validation.schema.json")
    chapter_fact_id = _chapter_fact_id(chapter_text)
    problems = [
        _normalize_llm_problem(
            _require_verifiable_problem(problem, chapter_text=chapter_text, chapter_fact_id=chapter_fact_id)
        )
        for problem in checked.get("problems", [])
    ]
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
        payload = _parse_json_object(response)
    except ValueError:
        repaired = chat_completion(
            messages
            + [
                {"role": "assistant", "content": response},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON. Return only one JSON object matching the "
                        "requested validation schema. Do not add markdown fences or commentary."
                    ),
                },
            ],
            model=model,
            temperature=0.0,
            stage="llm_validation",
        )
        payload = _parse_json_object(repaired)
    if not isinstance(payload, dict):
        raise ValueError("LLM validator response must be a JSON object")
    return payload


def _parse_json_object(response: str) -> dict[str, Any]:
    text = str(response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        if start < 0:
            raise ValueError("LLM validator response was not valid JSON") from exc
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as nested:
            raise ValueError("LLM validator response was not valid JSON") from nested
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
                    "chapter_fact_id": _chapter_fact_id(chapter_text),
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
    # Until the independent calibration/holdout gate is complete, medium LLM
    # findings are advisory.  Only high and critical findings can block.
    blocking = severity in {"critical", "high"}
    area = str(problem.get("area") or "complex_plot_logic")
    spans = [dict(item) for item in problem.get("evidence") or []]
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
        "fact_id": str(problem["fact_id"]),
        "evidence_spans": spans,
        "repair_parameters": {
            "area": area,
            "fact_id": str(problem["fact_id"]),
            "evidence_spans": spans,
        },
        "evidence": [
            {"kind": "chapter_span", "value": str(item["quote"])}
            for item in spans
        ],
    }


def _require_verifiable_problem(
    problem: dict[str, Any],
    *,
    chapter_text: str,
    chapter_fact_id: str,
) -> dict[str, Any]:
    if problem.get("fact_id") != chapter_fact_id:
        raise ValueError("LLM finding fact_id does not match the current chapter digest")
    for evidence in problem.get("evidence") or []:
        start = int(evidence["start_char"])
        end = int(evidence["end_char"])
        quote = str(evidence["quote"])
        if start < 0 or end <= start or end > len(chapter_text):
            raise ValueError("LLM finding evidence span is outside the chapter")
        if chapter_text[start:end] != quote:
            raise ValueError("LLM finding evidence quote does not match its chapter span")
    return problem


def _chapter_fact_id(chapter_text: str) -> str:
    digest = hashlib.sha256(str(chapter_text).encode("utf-8")).hexdigest()
    return f"chapter:sha256:{digest}"


def _llm_output_schema_hint() -> dict[str, Any]:
    return {
        "problems": [
            {
                "code": "llm_character_motivation_inconsistent",
                "message": "Short human-readable problem.",
                "area": "character_motivation_consistency",
                "severity": "high|medium|low|critical",
                "fact_id": "Copy chapter_fact_id exactly.",
                "evidence": [
                    {
                        "kind": "chapter_span",
                        "start_char": 0,
                        "end_char": 4,
                        "quote": "Exact chapter_text[start_char:end_char].",
                    }
                ],
                "repair_hint": "Specific scene-level repair guidance.",
            }
        ]
    }


__all__ = ["LLM_VALIDATION_AREAS", "llm_payload_to_check", "validate_llm"]
