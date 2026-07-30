from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from core.schema import validate_schema
from core.state.generation_state_view import (
    build_plan_generation_state_projection,
    validate_plan_generation_state_projection,
)


REPAIR_ENVELOPE_SCHEMA_VERSION = "1.0"
REPAIR_ENVELOPE_KIND = "repair_envelope"

_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_CATEGORIES = frozenset({"blocking", "warning"})
_RISK_LEVELS = frozenset({*_SEVERITIES, "none"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROBLEM_ID = re.compile(r"^p[0-9]{3,}$")


class RepairEnvelopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def repair_validation_sha256(validation: Mapping[str, Any]) -> str:
    return _canonical_sha256(_mapping(validation, "validation"))


def generation_state_view_sha256(
    generation_state_view: Mapping[str, Any],
) -> str:
    return _canonical_sha256(
        _mapping(generation_state_view, "generation_state_view")
    )


def build_repair_envelope(
    chapter_text: str,
    validation: Mapping[str, Any],
    repair_plan: Mapping[str, Any],
    *,
    chapter_index: int,
    recovery_context: Mapping[str, Any] | None,
    generation_state_view: Mapping[str, Any],
    chapter_contract: Mapping[str, Any],
    expected_validation_sha256: str,
    expected_generation_state_view_sha256: str,
) -> dict[str, Any]:
    """Build a compact, hash-bound model input for one repair attempt."""

    if not isinstance(chapter_text, str):
        raise _error("chapter_invalid", "chapter_text must be a string")
    normalized_chapter_index = _positive_integer(
        chapter_index,
        "chapter_index",
    )
    raw_validation = _mapping(validation, "validation")
    raw_plan = _mapping(repair_plan, "repair_plan")
    validation_sha256 = repair_validation_sha256(raw_validation)
    if validation_sha256 != _digest(
        expected_validation_sha256,
        "expected_validation_sha256",
    ):
        raise _error(
            "validation_digest_mismatch",
            "validation no longer matches the caller-bound digest",
        )
    declared_plan_validation_sha = raw_plan.get("validation_sha256")
    if (
        declared_plan_validation_sha is not None
        and _digest(
            declared_plan_validation_sha,
            "repair_plan.validation_sha256",
        )
        != validation_sha256
    ):
        raise _error(
            "repair_plan_validation_digest_mismatch",
            "repair plan is bound to a different validation result",
        )

    view = _validated_generation_state_view(
        generation_state_view,
        chapter_index=normalized_chapter_index,
    )
    view_sha256 = generation_state_view_sha256(view)
    if view_sha256 != _digest(
        expected_generation_state_view_sha256,
        "expected_generation_state_view_sha256",
    ):
        raise _error(
            "generation_state_view_digest_mismatch",
            "Generation State View no longer matches the caller-bound digest",
        )
    state_projection = build_plan_generation_state_projection(view)
    contract = _validated_chapter_contract(
        chapter_contract,
        chapter_index=normalized_chapter_index,
    )

    problems, compact_plan = _canonical_problem_contract(
        raw_validation,
        raw_plan,
    )
    recovery = (
        {"available": False}
        if recovery_context is None
        else _mapping(recovery_context, "recovery_context")
    )
    if (
        bool(recovery.get("available"))
        and isinstance(recovery.get("chapter_index"), int)
        and not isinstance(recovery.get("chapter_index"), bool)
        and recovery["chapter_index"] != normalized_chapter_index
    ):
        raise _error(
            "recovery_context_chapter_mismatch",
            "recovery context belongs to a different chapter",
        )

    envelope: dict[str, Any] = {
        "schema_version": REPAIR_ENVELOPE_SCHEMA_VERSION,
        "envelope_kind": REPAIR_ENVELOPE_KIND,
        "chapter_index": normalized_chapter_index,
        "chapter": chapter_text,
        "base_chapter_sha256": _text_sha256(chapter_text),
        "chapter_contract": contract,
        "chapter_contract_sha256": _canonical_sha256(contract),
        "validation_sha256": validation_sha256,
        "generation_state_view_sha256": view_sha256,
        "generation_state_projection_sha256": state_projection[
            "projection_sha256"
        ],
        "source_generation_state_view_projection_sha256": view[
            "projection_sha256"
        ],
        "generation_state_read_set_digest": view["read_set_digest"],
        "source_authority_sha256": view["source_authority_sha256"],
        "problem_set_sha256": _canonical_sha256(problems),
        "repair_plan_sha256": _canonical_sha256(compact_plan),
        "recovery_context_sha256": _canonical_sha256(recovery),
        "problems": problems,
        "repair_plan": compact_plan,
        "recovery_context": recovery,
        "generation_state_projection": state_projection,
    }
    envelope["envelope_sha256"] = _canonical_sha256(envelope)
    return validate_repair_envelope(envelope)


def validate_repair_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _mapping(value, "repair envelope")
    try:
        validate_schema(envelope, "repair_envelope.schema.json")
    except (TypeError, ValueError) as exc:
        raise _error("repair_envelope_schema_invalid", str(exc)) from exc
    if envelope["schema_version"] != REPAIR_ENVELOPE_SCHEMA_VERSION:
        raise _error(
            "repair_envelope_schema_invalid",
            "unsupported schema_version",
        )
    if envelope["envelope_kind"] != REPAIR_ENVELOPE_KIND:
        raise _error(
            "repair_envelope_schema_invalid",
            "unsupported envelope_kind",
        )
    if envelope["base_chapter_sha256"] != _text_sha256(envelope["chapter"]):
        raise _error(
            "base_chapter_digest_mismatch",
            "base chapter digest does not match chapter text",
        )
    contract = _validated_chapter_contract(
        envelope["chapter_contract"],
        chapter_index=envelope["chapter_index"],
    )
    if envelope["chapter_contract_sha256"] != _canonical_sha256(contract):
        raise _error(
            "chapter_contract_digest_mismatch",
            "chapter contract digest does not match",
        )

    problems = envelope["problems"]
    expected_problem_ids = [
        f"p{index:03d}" for index in range(1, len(problems) + 1)
    ]
    actual_problem_ids = [problem["problem_id"] for problem in problems]
    if actual_problem_ids != expected_problem_ids:
        raise _error(
            "problem_id_sequence_invalid",
            "canonical problem IDs must be contiguous p001... in list order",
        )
    if envelope["problem_set_sha256"] != _canonical_sha256(problems):
        raise _error(
            "problem_set_digest_mismatch",
            "problem set digest does not match canonical problems",
        )

    compact_plan = envelope["repair_plan"]
    references = [
        str(step.get("problem_id") or "") for step in compact_plan["steps"]
    ]
    if sorted(references) != expected_problem_ids:
        raise _error(
            "repair_plan_problem_reference_invalid",
            "repair plan must reference every canonical problem exactly once",
        )
    if envelope["repair_plan_sha256"] != _canonical_sha256(compact_plan):
        raise _error(
            "repair_plan_digest_mismatch",
            "repair plan digest does not match compact plan",
        )

    recovery = envelope["recovery_context"]
    if envelope["recovery_context_sha256"] != _canonical_sha256(recovery):
        raise _error(
            "recovery_context_digest_mismatch",
            "recovery context digest does not match",
        )
    if _key_occurrences(envelope, "recovery_context") != 1:
        raise _error(
            "recovery_context_duplicated",
            "repair envelope must contain exactly one recovery context",
        )

    try:
        state_projection = validate_plan_generation_state_projection(
            envelope["generation_state_projection"]
        )
    except (TypeError, ValueError) as exc:
        raise _error(
            "generation_state_projection_invalid",
            str(exc),
        ) from exc
    for envelope_field, projection_field in (
        ("generation_state_projection_sha256", "projection_sha256"),
        (
            "source_generation_state_view_projection_sha256",
            "source_generation_state_view_sha256",
        ),
        ("generation_state_read_set_digest", "read_set_digest"),
        ("source_authority_sha256", "source_authority_sha256"),
    ):
        if envelope[envelope_field] != state_projection[projection_field]:
            raise _error(
                "generation_state_projection_binding_mismatch",
                f"{envelope_field} does not match generation state projection",
            )

    chapter_occurrences = _exact_string_occurrences(
        envelope,
        envelope["chapter"],
    )
    if chapter_occurrences != 1:
        raise _error(
            "full_chapter_duplicated",
            "full chapter text must appear exactly once in the envelope",
        )

    hash_input = copy.deepcopy(envelope)
    declared_envelope_sha256 = hash_input.pop("envelope_sha256")
    if declared_envelope_sha256 != _canonical_sha256(hash_input):
        raise _error(
            "repair_envelope_digest_mismatch",
            "envelope digest does not match canonical payload",
        )
    return envelope


def _canonical_problem_contract(
    validation: dict[str, Any],
    repair_plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_problems = validation.get("problems")
    if not isinstance(raw_problems, list):
        raise _error(
            "validation_problems_invalid",
            "validation.problems must be an array",
        )
    if any(not isinstance(problem, Mapping) for problem in raw_problems):
        raise _error(
            "validation_problems_invalid",
            "validation.problems must contain only objects",
        )
    raw_steps = repair_plan.get("steps")
    if not isinstance(raw_steps, list) or any(
        not isinstance(step, Mapping) for step in raw_steps
    ):
        raise _error(
            "repair_plan_steps_invalid",
            "repair_plan.steps must contain only objects",
        )
    declared_problem_count = repair_plan.get("problem_count")
    if (
        declared_problem_count is not None
        and declared_problem_count != len(raw_problems)
    ):
        raise _error(
            "repair_plan_problem_count_mismatch",
            "repair plan problem_count is stale",
        )

    steps_by_index: dict[int, dict[str, Any]] = {}
    for raw_step in raw_steps:
        step = _mapping(raw_step, "repair plan step")
        index = _positive_integer(
            step.get("index"),
            "repair plan step index",
        )
        if index > len(raw_problems):
            raise _error(
                "repair_plan_problem_reference_invalid",
                f"repair plan step index {index} has no validation problem",
            )
        if index in steps_by_index:
            raise _error(
                "repair_plan_problem_reference_invalid",
                f"validation problem index {index} is referenced more than once",
            )
        steps_by_index[index] = step
    expected_indexes = set(range(1, len(raw_problems) + 1))
    if set(steps_by_index) != expected_indexes:
        missing = sorted(expected_indexes - set(steps_by_index))
        raise _error(
            "repair_plan_problem_reference_invalid",
            f"repair plan does not reference validation problem indexes {missing}",
        )

    pairs_by_fingerprint: dict[
        str,
        tuple[dict[str, Any], dict[str, Any]],
    ] = {}
    for index, raw_problem in enumerate(raw_problems, start=1):
        problem, step = _normalize_problem_and_step(
            _mapping(raw_problem, f"validation.problems[{index - 1}]"),
            steps_by_index[index],
        )
        pair = {"problem": problem, "step": step}
        fingerprint = _canonical_json(pair)
        pairs_by_fingerprint.setdefault(fingerprint, (problem, step))

    problems: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for ordinal, fingerprint in enumerate(sorted(pairs_by_fingerprint), start=1):
        problem, step = pairs_by_fingerprint[fingerprint]
        problem_id = f"p{ordinal:03d}"
        problems.append({"problem_id": problem_id, **problem})
        steps.append({"problem_id": problem_id, **step})
    steps.sort(
        key=lambda item: (
            item["priority"],
            item["problem_id"],
            item["action"],
        )
    )

    risk_level = str(repair_plan.get("risk_level") or "none").strip()
    if risk_level not in _RISK_LEVELS:
        raise _error(
            "repair_plan_risk_level_invalid",
            f"unsupported repair plan risk level {risk_level!r}",
        )
    repair_budget = repair_plan.get("repair_budget")
    if repair_budget is not None:
        repair_budget = _bounded_integer(
            repair_budget,
            "repair_plan.repair_budget",
            minimum=0,
            maximum=5,
        )
    attempt = repair_plan.get("attempt")
    if attempt is not None:
        attempt = _positive_integer(attempt, "repair_plan.attempt")
    compact_plan = {
        "risk_level": risk_level,
        "repair_budget": repair_budget,
        "attempt": attempt,
        "steps": steps,
    }
    return problems, compact_plan


def _normalize_problem_and_step(
    raw_problem: dict[str, Any],
    raw_step: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    code = _nonempty_text(raw_problem.get("code") or "unknown", "problem.code")
    message = _text(raw_problem.get("message") or "", "problem.message")
    validator = _text(raw_problem.get("validator") or "", "problem.validator")
    severity = str(raw_problem.get("severity") or "medium").strip()
    if severity not in _SEVERITIES:
        raise _error(
            "validation_problem_invalid",
            f"unsupported problem severity {severity!r}",
        )
    blocking = bool(raw_problem.get("blocking", True))
    category = str(
        raw_problem.get("category")
        or ("blocking" if blocking else "warning")
    ).strip()
    if category not in _CATEGORIES:
        raise _error(
            "validation_problem_invalid",
            f"unsupported problem category {category!r}",
        )
    if (category == "blocking") != blocking:
        raise _error(
            "validation_problem_invalid",
            "problem category and blocking flag disagree",
        )
    evidence = _normalize_evidence(raw_problem.get("evidence"))

    for field, expected in (
        ("code", code),
        ("message", message),
        ("validator", validator),
        ("severity", severity),
        ("blocking", blocking),
    ):
        actual = raw_step.get(field)
        if field == "blocking":
            normalized_actual = bool(actual)
        else:
            normalized_actual = str(actual or "").strip()
        if normalized_actual != expected:
            raise _error(
                "repair_plan_validation_mismatch",
                f"repair plan step {field} does not match validation problem",
            )
    step_evidence = _normalize_evidence(raw_step.get("evidence"))
    if step_evidence != evidence:
        raise _error(
            "repair_plan_validation_mismatch",
            "repair plan step evidence does not match validation problem",
        )

    problem_repair_hint = raw_problem.get("repair_hint")
    step_repair_hint = _text(
        raw_step.get("repair_hint") or "",
        "repair plan step repair_hint",
    )
    if (
        problem_repair_hint is not None
        and _text(problem_repair_hint, "problem.repair_hint")
        != step_repair_hint
    ):
        raise _error(
            "repair_plan_validation_mismatch",
            "repair plan repair_hint does not match validation problem",
        )
    problem = {
        "code": code,
        "message": message,
        "validator": validator,
        "severity": severity,
        "blocking": blocking,
        "category": category,
        "repair_hint": step_repair_hint,
        "evidence": evidence,
    }

    action = _nonempty_text(
        raw_step.get("action"),
        "repair plan step action",
    )
    declared_action = raw_problem.get("repair_action")
    if (
        declared_action is not None
        and _nonempty_text(declared_action, "problem.repair_action") != action
    ):
        raise _error(
            "repair_plan_validation_mismatch",
            "repair plan action does not match validation problem",
        )
    priority = _bounded_integer(
        raw_step.get("priority"),
        "repair plan step priority",
        minimum=0,
    )
    strategy = _nonempty_text(
        raw_step.get("strategy"),
        "repair plan step strategy",
    )
    parameters = raw_step.get("parameters")
    if not isinstance(parameters, Mapping):
        raise _error(
            "repair_plan_step_invalid",
            "repair plan step parameters must be an object",
        )
    step = {
        "action": action,
        "priority": priority,
        "strategy": strategy,
        "parameters": copy.deepcopy(dict(parameters)),
    }
    _canonical_json(step)
    return problem, step


def _normalize_evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _error(
            "validation_problem_invalid",
            "problem evidence must be an array",
        )
    normalized: dict[tuple[str, str], dict[str, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise _error(
                "validation_problem_invalid",
                "problem evidence must contain only objects",
            )
        kind = _nonempty_text(item.get("kind"), "evidence.kind")
        evidence_value = _nonempty_text(item.get("value"), "evidence.value")
        normalized[(kind, evidence_value)] = {
            "kind": kind,
            "value": evidence_value,
        }
    return [normalized[key] for key in sorted(normalized)]


def _validated_chapter_contract(
    value: Mapping[str, Any],
    *,
    chapter_index: int,
) -> dict[str, Any]:
    contract = _mapping(value, "chapter_contract")
    allowed = {
        "chapter_index",
        "title",
        "chapter_goal",
        "core_event",
        "human_conflict",
        "required_beats",
        "ending_pressure",
    }
    unknown = set(contract) - allowed
    if unknown:
        raise _error(
            "chapter_contract_invalid",
            "chapter contract contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown)),
        )
    if contract.get("chapter_index") != chapter_index:
        raise _error(
            "chapter_contract_chapter_mismatch",
            "chapter contract belongs to a different chapter",
        )
    for field in (
        "title",
        "chapter_goal",
        "core_event",
        "human_conflict",
        "ending_pressure",
    ):
        if field in contract and contract[field] is not None:
            if not isinstance(contract[field], str):
                raise _error(
                    "chapter_contract_invalid",
                    f"chapter_contract.{field} must be a string or null",
                )
            contract[field] = contract[field].strip()
    beats = contract.get("required_beats")
    if beats is not None:
        if not isinstance(beats, list) or any(
            not isinstance(item, (str, Mapping)) for item in beats
        ):
            raise _error(
                "chapter_contract_invalid",
                "chapter_contract.required_beats must be an array of strings or objects",
            )
        contract["required_beats"] = [
            copy.deepcopy(dict(item)) if isinstance(item, Mapping) else item
            for item in beats
        ]
    _canonical_json(contract)
    return contract


def _validated_generation_state_view(
    value: Mapping[str, Any],
    *,
    chapter_index: int,
) -> dict[str, Any]:
    view = _mapping(value, "generation_state_view")
    if view.get("schema_version") != "1.0":
        raise _error(
            "generation_state_view_invalid",
            "unsupported Generation State View schema_version",
        )
    if view.get("view_kind") != "chapter_generation_state":
        raise _error(
            "generation_state_view_invalid",
            "unsupported Generation State View kind",
        )
    source_authority_sha256 = _digest(
        view.get("source_authority_sha256"),
        "generation_state_view.source_authority_sha256",
    )
    projection_sha256 = _digest(
        view.get("projection_sha256"),
        "generation_state_view.projection_sha256",
    )
    projection_input = copy.deepcopy(view)
    projection_input.pop("projection_sha256", None)
    if projection_sha256 != _canonical_sha256(projection_input):
        raise _error(
            "generation_state_view_projection_mismatch",
            "Generation State View projection digest does not match",
        )

    read_set = view.get("chapter_context_read_set")
    if not isinstance(read_set, Mapping):
        raise _error(
            "generation_state_view_invalid",
            "Generation State View read set must be an object",
        )
    normalized_read_set = copy.deepcopy(dict(read_set))
    read_set_contract_sha256 = _digest(
        normalized_read_set.pop("contract_sha256", None),
        "chapter_context_read_set.contract_sha256",
    )
    if read_set_contract_sha256 != _canonical_sha256(normalized_read_set):
        raise _error(
            "generation_state_view_read_set_mismatch",
            "Generation State View read-set contract digest does not match",
        )
    if view.get("read_set_digest") != read_set_contract_sha256:
        raise _error(
            "generation_state_view_read_set_mismatch",
            "Generation State View read_set_digest does not match its contract",
        )
    if normalized_read_set.get("chapter_index") != chapter_index:
        raise _error(
            "generation_state_view_chapter_mismatch",
            "Generation State View belongs to a different chapter",
        )

    state_ids = _string_array(
        view.get("selected_state_item_ids"),
        "generation_state_view.selected_state_item_ids",
    )
    event_ids = _string_array(
        view.get("selected_event_item_ids"),
        "generation_state_view.selected_event_item_ids",
    )
    if view.get("selected_item_ids_sha256") != _canonical_sha256(
        [*state_ids, *event_ids]
    ):
        raise _error(
            "generation_state_view_selected_items_mismatch",
            "Generation State View selected item digest does not match",
        )
    view["source_authority_sha256"] = source_authority_sha256
    view["projection_sha256"] = projection_sha256
    return view


def _string_array(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise _error(
            "generation_state_view_invalid",
            f"{field} must contain only non-empty strings",
        )
    normalized = [item.strip() for item in value]
    if normalized != sorted(set(normalized)):
        raise _error(
            "generation_state_view_invalid",
            f"{field} must be sorted and unique",
        )
    return normalized


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("repair_envelope_input_invalid", f"{field} must be an object")
    result = copy.deepcopy(dict(value))
    _canonical_json(result)
    return result


def _positive_integer(value: Any, field: str) -> int:
    return _bounded_integer(value, field, minimum=1)


def _bounded_integer(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(
            "repair_envelope_input_invalid",
            f"{field} must be an integer",
        )
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}"
        if maximum is not None:
            bounds += f" and <= {maximum}"
        raise _error(
            "repair_envelope_input_invalid",
            f"{field} must be {bounds}",
        )
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _error(
            "repair_envelope_input_invalid",
            f"{field} must be a string",
        )
    return value.strip()


def _nonempty_text(value: Any, field: str) -> str:
    normalized = _text(value, field)
    if not normalized:
        raise _error(
            "repair_envelope_input_invalid",
            f"{field} must not be empty",
        )
    return normalized


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _error(
            "repair_envelope_input_invalid",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise _error(
            "repair_envelope_input_invalid",
            f"value is not canonical JSON data: {exc}",
        ) from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key_occurrences(value: Any, key: str) -> int:
    if isinstance(value, dict):
        return sum(
            (1 if item_key == key else 0) + _key_occurrences(item, key)
            for item_key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_key_occurrences(item, key) for item in value)
    return 0


def _exact_string_occurrences(value: Any, expected: str) -> int:
    if isinstance(value, str):
        return 1 if value == expected else 0
    if isinstance(value, dict):
        return sum(
            _exact_string_occurrences(item, expected)
            for item in value.values()
        )
    if isinstance(value, list):
        return sum(
            _exact_string_occurrences(item, expected) for item in value
        )
    return 0


def _error(code: str, message: str) -> RepairEnvelopeError:
    return RepairEnvelopeError(code, message)


__all__ = [
    "REPAIR_ENVELOPE_KIND",
    "REPAIR_ENVELOPE_SCHEMA_VERSION",
    "RepairEnvelopeError",
    "build_repair_envelope",
    "generation_state_view_sha256",
    "repair_validation_sha256",
    "validate_repair_envelope",
]
