from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.context_budget import ContextBudgetError, default_context_budget
from core.prompt_compiler import compile_prompt_contexts
from core.state.authoritative_context import authoritative_state_from_markdown
from modules.scene_repair.repairer import (
    RepairContext,
    _LEGACY_REPAIR_CONTEXT_POLICIES,
    _REPAIR_INPUT_HEADROOM_TOKENS,
    _compact_repair_context,
    _legacy_repair_payload,
    _legacy_validation_summary,
    _load_prompt,
    build_repair_messages,
)


FIXTURES = Path("tests/fixtures/repair_budget")
# Official chapter_26_20260902T050842557412Z failed at 32761 budgeted tokens
# against the same 30000/32000 budget. Current unprojected packing of the
# same artifacts measures 33178; after ledger projection it must fit.
HISTORICAL_OFFICIAL_OVERFLOW_TOKENS = 32_761
CURRENT_UNPROJECTED_OFFICIAL_TOKENS = 33_178
SAFE_TARGET = 30_000
HARD_LIMIT = 32_000


def _official_ch26_artifacts() -> tuple[str, dict, str, dict, dict]:
    chapter = (FIXTURES / "official_ch26_chapter.txt").read_text(encoding="utf-8")
    validation = json.loads(
        (FIXTURES / "official_ch26_validation.json").read_text(encoding="utf-8")
    )
    input_pack = (FIXTURES / "official_ch26_input_pack.md").read_text(encoding="utf-8")
    repair_plan = json.loads(
        (FIXTURES / "official_ch26_repair_plan.json").read_text(encoding="utf-8")
    )
    recovery = json.loads(
        (FIXTURES / "official_ch26_recovery.json").read_text(encoding="utf-8")
    )
    return chapter, validation, input_pack, repair_plan, recovery


def _measure(messages: list[dict[str, str]]) -> int:
    budget = default_context_budget()
    report = budget.measure(
        messages[-1]["content"],
        stage="repair",
        protocol_texts=(messages[0]["content"],),
    )
    return int(report["budgeted_input_tokens"])


def _unprojected_official_legacy_tokens() -> int:
    chapter, validation, input_pack, repair_plan, recovery = _official_ch26_artifacts()
    repair_query = json.dumps(
        {
            "problem_codes": [
                str(item.get("code") or "")
                for item in validation.get("problems") or []
                if isinstance(item, dict)
            ],
            "repair_plan": repair_plan,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    compiled = compile_prompt_contexts(input_pack, query_hint=repair_query).repair.text
    authority = authoritative_state_from_markdown(input_pack)
    policy = _LEGACY_REPAIR_CONTEXT_POLICIES[0]
    compact = _compact_repair_context(
        compiled,
        query=repair_query,
        authoritative_state_source=authority,
        authority_section_chars=policy["authority_section_chars"],
        context_chars=policy["context_chars"],
        per_section_chars=policy["per_section_chars"],
        authority_selection_mode="required_only",
        policy="repair_markdown_json_retrieval_required_only_v2",
    )
    payload = _legacy_repair_payload(
        chapter,
        validation,
        repair_plan,
        recovery,
        RepairContext(language="zh-CN"),
        compact,
        validation_projection=_legacy_validation_summary(validation),
        compact_json=True,
    )
    budget = default_context_budget()
    report = budget.measure(
        payload,
        stage="repair",
        protocol_texts=(_load_prompt(),),
    )
    return int(report["budgeted_input_tokens"])


def _projected_evidence(payload: dict) -> list[dict[str, str]]:
    types = {item["type_id"]: item for item in payload.get("problem_types") or []}
    evidence: list[dict[str, str]] = []
    for problem in payload.get("problems") or []:
        typ = types.get(problem.get("type_id")) or {}
        evidence.extend(list(typ.get("shared_evidence") or []))
        evidence.extend(list(problem.get("evidence") or []))
    return evidence


def test_unprojected_official_ch26_repair_input_still_overflows_current_budget() -> None:
    tokens = _unprojected_official_legacy_tokens()
    assert tokens == CURRENT_UNPROJECTED_OFFICIAL_TOKENS
    assert tokens > HARD_LIMIT
    assert HISTORICAL_OFFICIAL_OVERFLOW_TOKENS > SAFE_TARGET


def test_official_ch26_repair_input_fits_safe_target_after_projection() -> None:
    chapter, validation, input_pack, repair_plan, recovery = _official_ch26_artifacts()

    messages = build_repair_messages(
        chapter,
        validation,
        input_pack,
        repair_plan,
        recovery,
        RepairContext(language="zh-CN"),
    )

    tokens = _measure(messages)
    payload = json.loads(messages[-1]["content"])
    evidence = _projected_evidence(payload)
    summaries = [
        item["value"] for item in evidence if item.get("kind") == "summary"
    ]

    assert tokens <= SAFE_TARGET
    assert tokens < CURRENT_UNPROJECTED_OFFICIAL_TOKENS
    assert payload["chapter"] == chapter
    assert len(payload["problems"]) == 20
    assert len(payload["problem_types"]) == 4
    assert len(summaries) == 18
    assert "下一瞬，回声囊腹部的脉冲骤然收缩" in " ".join(summaries)
    assert any(
        item.get("kind") == "missing_last_scene_state"
        for item in evidence
    )
    assert any(item.get("kind") == "chapter_span" for item in evidence)
    assert "problem_types" in messages[0]["content"]
    assert default_context_budget().hard_input_limit == HARD_LIMIT
    assert (
        default_context_budget().hard_input_limit - _REPAIR_INPUT_HEADROOM_TOKENS
        == SAFE_TARGET
    )


def test_official_like_envelope_repair_input_fits_safe_target() -> None:
    chapter, validation, _legacy_pack, repair_plan, recovery = _official_ch26_artifacts()
    input_pack = (FIXTURES / "official_ch63_gsv_input_pack.md").read_text(
        encoding="utf-8"
    )

    messages = build_repair_messages(
        chapter,
        validation,
        input_pack,
        repair_plan,
        recovery,
        RepairContext(language="zh-CN"),
    )

    tokens = _measure(messages)
    payload = json.loads(messages[-1]["content"])
    evidence = _projected_evidence(payload)

    assert payload["envelope_kind"] == "repair_envelope"
    assert "problem_types" in payload
    assert tokens <= SAFE_TARGET
    assert len(payload["problems"]) == 20
    assert len([item for item in evidence if item.get("kind") == "summary"]) == 18
    assert payload["chapter"] == chapter
    assert "generation_state_projection" in payload


def test_required_repair_input_too_large_fails_closed_as_budget_error() -> None:
    chapter = "字" * 80_000
    validation = {
        "ok": False,
        "problems": [
            {
                "code": "chapter_too_long_to_pack",
                "message": "unique evidence " + ("证" * 8_000),
                "validator": "logic",
                "severity": "critical",
                "blocking": True,
                "category": "blocking",
                "repair_hint": "Keep the unique evidence.",
                "repair_action": "manual_review",
                "evidence": [
                    {"kind": "excerpt", "value": "独证" * 8_000}
                ],
            }
        ],
    }
    from modules.scene_repair.plan import build_repair_plan

    plan = build_repair_plan(validation)
    input_pack = (
        "# Story State\n"
        + json.dumps(
            {
                "last_chapter_ending": "end",
                "last_scene_location": "here",
                "last_scene_characters": ["hero"],
                "open_threads": [],
                "required_opening_bridge": "continue",
            },
            ensure_ascii=False,
        )
        + "\n"
    )

    with pytest.raises(ContextBudgetError) as raised:
        build_repair_messages(
            chapter,
            validation,
            input_pack,
            plan,
            {"available": False},
            RepairContext(language="zh-CN"),
        )

    assert raised.value.code == "story_project_repair_context_headroom_exceeded"
    assert "safe target is 30000" in str(raised.value)
    assert "hard limit is 32000" in str(raised.value)
