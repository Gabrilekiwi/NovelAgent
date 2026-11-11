from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from core.state.generation_state_view import (
    validate_plan_generation_state_projection,
)
from core.structured_context import sha256_text


_IDENTITY_KEYS = frozenset(
    {
        "event_id",
        "entity_id",
        "character_id",
        "item_id",
        "record_id",
        "location_id",
        "counter_id",
    }
)
_EMPTY_VALUES = (None, "", [], {})
_DROPABLE_PARAMETERS = ({}, {"raw_problem": {}})


def project_canonical_repair_ledger(
    problems: list[dict[str, Any]],
    repair_plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Project a canonical problem ledger onto shared types plus instances.

    Every distinct problem and its unique evidence is kept. Fields that are
    identical across a type (message, hint, strategy, and truly shared
    evidence) are stored once in ``problem_types`` instead of being repeated
    on every instance.
    """

    if not isinstance(problems, list) or not isinstance(repair_plan, dict):
        raise ValueError("canonical repair ledger requires problems and a plan")
    steps = repair_plan.get("steps")
    if not isinstance(steps, list):
        raise ValueError("repair_plan.steps must be an array")

    steps_by_id: dict[str, dict[str, Any]] = {}
    for raw_step in steps:
        if not isinstance(raw_step, Mapping):
            raise ValueError("repair_plan.steps must contain only objects")
        step = copy.deepcopy(dict(raw_step))
        problem_id = str(step.get("problem_id") or "")
        if not problem_id:
            raise ValueError("repair plan step is missing problem_id")
        if problem_id in steps_by_id:
            raise ValueError(f"repair plan step {problem_id} is duplicated")
        steps_by_id[problem_id] = step

    groups: dict[tuple[Any, ...], list[str]] = {}
    problem_by_id: dict[str, dict[str, Any]] = {}
    for raw_problem in problems:
        if not isinstance(raw_problem, Mapping):
            raise ValueError("problems must contain only objects")
        problem = copy.deepcopy(dict(raw_problem))
        problem_id = str(problem.get("problem_id") or "")
        if not problem_id:
            raise ValueError("canonical problem is missing problem_id")
        if problem_id not in steps_by_id:
            raise ValueError(f"problem {problem_id} has no repair plan step")
        problem_by_id[problem_id] = problem
        key = _type_key(problem, steps_by_id[problem_id])
        groups.setdefault(key, []).append(problem_id)

    catalog: list[dict[str, Any]] = []
    type_id_by_key: dict[tuple[Any, ...], str] = {}
    # Assign type ids in the order the first problem of each type appears.
    for ordinal, (key, problem_ids) in enumerate(groups.items(), start=1):
        type_id = f"t{ordinal:03d}"
        type_id_by_key[key] = type_id
        catalog.append(_type_record(type_id, key, problem_ids, problem_by_id))

    projected_problems: list[dict[str, Any]] = []
    for raw_problem in problems:
        problem_id = str(raw_problem["problem_id"])
        problem = problem_by_id[problem_id]
        step = steps_by_id[problem_id]
        type_id = type_id_by_key[_type_key(problem, step)]
        type_record = next(item for item in catalog if item["type_id"] == type_id)
        shared = list(type_record.get("shared_evidence") or [])
        evidence = [
            item
            for item in _evidence_items(problem.get("evidence"))
            if item not in shared
        ]
        projected_problems.append(
            {
                "problem_id": problem_id,
                "type_id": type_id,
                "evidence": evidence,
            }
        )

    projected_steps: list[dict[str, Any]] = []
    for raw_step in steps:
        step = steps_by_id[str(raw_step["problem_id"])]
        packed_step: dict[str, Any] = {
            "problem_id": step["problem_id"],
            "priority": step["priority"],
        }
        type_record = next(
            item
            for item in catalog
            if item["type_id"]
            == type_id_by_key[_type_key(problem_by_id[step["problem_id"]], step)]
        )
        parameters = step.get("parameters")
        type_parameters = type_record.get("parameters")
        if (
            isinstance(parameters, Mapping)
            and dict(parameters) not in _DROPABLE_PARAMETERS
            and dict(parameters) != dict(type_parameters or {})
        ):
            packed_step["parameters"] = copy.deepcopy(dict(parameters))
        projected_steps.append(packed_step)

    projected_plan = {
        key: copy.deepcopy(repair_plan[key])
        for key in ("risk_level", "repair_budget", "attempt")
        if key in repair_plan
    }
    projected_plan["steps"] = projected_steps
    return catalog, projected_problems, projected_plan


def pack_generation_state_projection(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Elide identity keys and empty optional fields from a state projection.

    Record identities stay in the map keys. Required records and events are
    not dropped; only duplicated identifiers and empty optional fields are
    removed, then the projection digest is rebuilt.
    """

    packed = copy.deepcopy(dict(projection))
    packed.pop("projection_sha256", None)
    events = packed.get("required_events")
    if isinstance(events, dict):
        packed["required_events"] = {
            event_id: _pack_record(event, record_id=event_id)
            for event_id, event in events.items()
            if isinstance(event, dict)
        }
    current = packed.get("current_state")
    if isinstance(current, dict):
        packed_current: dict[str, Any] = {}
        for collection, records in current.items():
            if not isinstance(records, dict):
                packed_current[collection] = copy.deepcopy(records)
                continue
            packed_current[collection] = {
                record_id: _pack_record(record, record_id=record_id)
                for record_id, record in records.items()
                if isinstance(record, dict)
            }
        packed["current_state"] = packed_current
    packed["projection_sha256"] = sha256_text(_canonical_json(packed))
    return validate_plan_generation_state_projection(packed)


def resolve_problem_type(
    problem: Mapping[str, Any],
    problem_types: list[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    type_id = problem.get("type_id")
    if type_id is None:
        return None
    for item in problem_types or []:
        if item.get("type_id") == type_id:
            return dict(item)
    raise ValueError(f"problem type {type_id!r} is not defined")


def _type_key(problem: Mapping[str, Any], step: Mapping[str, Any]) -> tuple[Any, ...]:
    parameters = step.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}
    return (
        str(problem.get("code") or ""),
        str(problem.get("message") or ""),
        str(problem.get("validator") or ""),
        str(problem.get("severity") or ""),
        bool(problem.get("blocking")),
        str(problem.get("category") or ""),
        str(problem.get("repair_hint") or ""),
        str(step.get("action") or ""),
        str(step.get("strategy") or ""),
        _canonical_json(dict(parameters)),
    )


def _type_record(
    type_id: str,
    key: tuple[Any, ...],
    problem_ids: list[str],
    problem_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    (
        code,
        message,
        validator,
        severity,
        blocking,
        category,
        repair_hint,
        action,
        strategy,
        parameters_json,
    ) = key
    record: dict[str, Any] = {
        "type_id": type_id,
        "code": code,
        "message": message,
        "validator": validator,
        "severity": severity,
        "blocking": blocking,
        "category": category,
        "repair_hint": repair_hint,
        "action": action,
        "strategy": strategy,
    }
    parameters = json.loads(parameters_json)
    if isinstance(parameters, dict) and parameters not in _DROPABLE_PARAMETERS:
        record["parameters"] = parameters
    evidence_lists = [
        _evidence_items(problem_by_id[problem_id].get("evidence"))
        for problem_id in problem_ids
    ]
    shared = _shared_evidence(evidence_lists)
    if shared:
        record["shared_evidence"] = shared
    return record


def _shared_evidence(
    evidence_lists: list[list[dict[str, str]]],
) -> list[dict[str, str]]:
    if not evidence_lists:
        return []
    shared = evidence_lists[0][:]
    for items in evidence_lists[1:]:
        shared = [item for item in shared if item in items]
    return shared


def _evidence_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "").strip()
        evidence_value = str(raw.get("value") or "").strip()
        if kind and evidence_value:
            items.append({"kind": kind, "value": evidence_value})
    return items


def _pack_record(record: Mapping[str, Any], *, record_id: str) -> dict[str, Any]:
    packed: dict[str, Any] = {}
    for key, value in record.items():
        if key in _IDENTITY_KEYS and value == record_id:
            continue
        if value in _EMPTY_VALUES:
            continue
        packed[key] = copy.deepcopy(value)
    return packed


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "pack_generation_state_projection",
    "project_canonical_repair_ledger",
    "resolve_problem_type",
]
