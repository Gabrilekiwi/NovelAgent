from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from api.openai_client import chat_completion
from api.retry import retry_telemetry_snapshot
from core.config import get_config
from core.quality_decision import QUALITY_POLICY_VERSION
from core.schema import SchemaValidationError, validate_schema


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
    validation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_model = model or get_config().openai_model
    context = validation_context if isinstance(validation_context, dict) else {}
    revalidation = context.get("revalidation")
    revalidation_mode = isinstance(revalidation, dict)
    messages = _llm_messages(
        snapshot,
        chapter_text,
        decision,
        validation_context=context,
    )
    telemetry_offset = len(retry_telemetry_snapshot())
    max_tokens = _validation_max_tokens(revalidation=revalidation_mode)
    payload = _call_llm_validator(
        messages,
        model=selected_model,
        max_tokens=max_tokens,
    )
    schema_repair = {
        "attempted": False,
        "succeeded": False,
        "dropped_problem_count": 0,
    }
    try:
        check = llm_payload_to_check(payload, chapter_text=chapter_text)
    except SchemaValidationError as initial_error:
        schema_repair["attempted"] = True
        fallback_payload = payload
        try:
            repaired_payload = _repair_llm_validation_payload(
                messages,
                payload,
                schema_error=initial_error,
                model=selected_model,
                max_tokens=max_tokens,
            )
            fallback_payload = repaired_payload
            check = llm_payload_to_check(repaired_payload, chapter_text=chapter_text)
            schema_repair["succeeded"] = True
        except (SchemaValidationError, ValueError):
            safe_payload, dropped_count = _retain_schema_valid_problems(fallback_payload)
            check = llm_payload_to_check(safe_payload, chapter_text=chapter_text)
            schema_repair["dropped_problem_count"] = dropped_count
    retry_reports = retry_telemetry_snapshot()[telemetry_offset:]
    check["metadata"] = {
        "provider": "openai",
        "model": selected_model,
        "prompt_hash": hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "policy_version": QUALITY_POLICY_VERSION,
        "mode": "repair_revalidation" if revalidation_mode else "full_validation",
        "previous_chapter_context": isinstance(context.get("previous_chapter"), dict),
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
        "schema_repair": schema_repair,
    }
    return check


def llm_payload_to_check(
    payload: dict[str, Any],
    *,
    chapter_text: str,
) -> dict[str, Any]:
    checked = validate_schema(payload, "llm_validation.schema.json")
    chapter_fact_id = _chapter_fact_id(chapter_text)
    problems = []
    for problem in checked.get("problems", []):
        verified = _require_verifiable_problem(
            problem,
            chapter_text=chapter_text,
            chapter_fact_id=chapter_fact_id,
        )
        if verified is not None:
            problems.append(_normalize_llm_problem(verified))
    return {
        "name": "llm",
        "ok": not any(problem["blocking"] for problem in problems),
        "problems": problems,
    }


def _call_llm_validator(
    messages: list[dict[str, str]],
    *,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    response = chat_completion(
        messages,
        model=model,
        temperature=0.0,
        stage="llm_validation",
        max_tokens=max_tokens,
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
            max_tokens=max_tokens,
        )
        payload = _parse_json_object(repaired)
    if not isinstance(payload, dict):
        raise ValueError("LLM validator response must be a JSON object")
    return payload


def _repair_llm_validation_payload(
    messages: list[dict[str, str]],
    payload: dict[str, Any],
    *,
    schema_error: SchemaValidationError,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    response = chat_completion(
        messages
        + [
            {
                "role": "assistant",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
            {
                "role": "user",
                "content": (
                    "Your JSON was syntactically valid but violated the required validation schema: "
                    f"{schema_error}. Return one corrected JSON object only. Preserve a finding only when "
                    "you can support it with 1-3 non-empty evidence entries. Every evidence.quote must be "
                    "copied exactly from the supplied chapter, and start_char/end_char must identify that "
                    "exact occurrence. Remove any finding that cannot be supported by an exact chapter quote. "
                    "Copy chapter_fact_id exactly and do not add commentary or markdown fences."
                ),
            },
        ],
        model=model,
        temperature=0.0,
        stage="llm_validation",
        max_tokens=max_tokens,
    )
    return _parse_json_object(response)


def _retain_schema_valid_problems(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    raw_problems = payload.get("problems") if isinstance(payload, dict) else None
    if not isinstance(raw_problems, list):
        return {"problems": []}, 0
    retained: list[dict[str, Any]] = []
    for problem in raw_problems:
        candidate = {"problems": [problem]}
        try:
            validate_schema(candidate, "llm_validation.schema.json")
        except SchemaValidationError:
            continue
        retained.append(dict(problem))
    return {"problems": retained}, len(raw_problems) - len(retained)


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
    *,
    validation_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    context = validation_context if isinstance(validation_context, dict) else {}
    revalidation = context.get("revalidation")
    focused = isinstance(revalidation, dict)
    if focused:
        system = (
            "You are a focused fiction repair verifier. Return only JSON matching the supplied schema. "
            "Do not repeat a broad chapter audit. Verify whether the listed prior problems remain after "
            "repair and whether the repaired causal neighborhood introduced a new critical or high-severity "
            "contradiction. Report at most three unresolved or newly introduced critical/high problems. "
            "Do not report a mutable-state conflict merely because an initial setting differs from the most "
            "recent committed chapter; the previous committed chapter has precedence. Every finding must "
            "contain an exact, non-empty quote copied from the current chapter with correct character offsets. "
            "Omit unsupported findings and keep the analysis concise."
        )
        prior_areas = [
            str(item.get("area"))
            for item in revalidation.get("prior_problems") or []
            if isinstance(item, dict) and str(item.get("area") or "").strip()
        ]
        check_areas = list(dict.fromkeys(prior_areas)) or list(LLM_VALIDATION_AREAS)
    else:
        system = (
            "You are a strict fiction continuity validator. Return only JSON matching "
            "the supplied schema. Report high-signal story problems; do not rewrite prose. "
            "Treat mutable facts in the most recent committed chapter as newer and more authoritative "
            "than baseline values in initial character or setting files. Every reported problem must "
            "contain at least one exact, non-empty quote copied from the supplied chapter with correct "
            "character offsets. Omit any problem that cannot be supported by exact chapter evidence. "
            "Return at most five high-signal problems and keep the analysis concise."
        )
        check_areas = list(LLM_VALIDATION_AREAS)
    return [
        {
            "role": "system",
            "content": system,
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "schema": _llm_output_schema_hint(),
                    "chapter_fact_id": _chapter_fact_id(chapter_text),
                    "validation_mode": "repair_revalidation" if focused else "full_validation",
                    "fact_precedence": [
                        "current_chapter_for_current_events",
                        "previous_committed_chapter_for_mutable_state",
                        "runtime_snapshot_for_unsuperseded_facts",
                        "initial_setting_as_baseline_only",
                    ],
                    "check_areas": check_areas,
                    "snapshot": _project_snapshot_for_llm(snapshot),
                    "previous_chapter": context.get("previous_chapter"),
                    "revalidation": revalidation if focused else None,
                    "decision": decision or {},
                    "chapter": chapter_text,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def _validation_max_tokens(*, revalidation: bool) -> int:
    config = get_config()
    if revalidation:
        return int(config.openai_revalidation_max_output_tokens)
    return int(config.openai_validation_max_output_tokens)


def _project_snapshot_for_llm(snapshot: dict[str, Any]) -> dict[str, Any]:
    if isinstance(snapshot.get("generation_state_view"), dict):
        return _project_explicit_generation_snapshot(snapshot)
    story_state = snapshot.get("story_state") if isinstance(snapshot.get("story_state"), dict) else {}
    world_state = snapshot.get("world_state") if isinstance(snapshot.get("world_state"), dict) else {}
    timeline = snapshot.get("timeline") if isinstance(snapshot.get("timeline"), list) else []
    return {
        "book_id": snapshot.get("book_id"),
        "chapter_index": snapshot.get("chapter_index"),
        "project_profile": snapshot.get("project_profile") or {},
        "characters": snapshot.get("characters") or {},
        "story_state": _without_source_document(story_state),
        "world_state": _without_source_document(world_state),
        "spatial_state": snapshot.get("spatial_state") or {},
        "timeline": [_compact_timeline_entry(item) for item in timeline[-8:] if isinstance(item, dict)],
        "constraints": snapshot.get("constraints") or [],
        "story_project": {
            "chapter_index": (snapshot.get("story_project") or {}).get("chapter_index")
            if isinstance(snapshot.get("story_project"), dict)
            else None,
        },
    }


def _project_explicit_generation_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Preserve one shared working-set snapshot without reopening raw state."""

    allowed_top_level = (
        "book_id",
        "chapter_index",
        "project_profile",
        "world_state",
        "story_state",
        "spatial_state",
        "characters",
        "relationships",
        "roster",
        "numeric_counters",
        "inventory",
        "locations",
        "required_events",
        "constraints",
        "expected_new_entities",
        "authority_summary",
        "timeline_summary",
        "generation_state_view",
        "story_project",
    )
    projected = {
        key: copy.deepcopy(snapshot[key])
        for key in allowed_top_level
        if key in snapshot
    }
    authority_summary = (
        projected.get("authority_summary")
        if isinstance(projected.get("authority_summary"), dict)
        else {}
    )
    authority_summary = {
        key: copy.deepcopy(authority_summary.get(key))
        for key in (
            "source_authority_sha256",
            "selected_state_item_ids",
            "selected_event_item_ids",
        )
        if key in authority_summary
    }
    projected["authority_summary"] = authority_summary
    timeline_summary = (
        projected.get("timeline_summary")
        if isinstance(projected.get("timeline_summary"), dict)
        else {}
    )
    projected["timeline_summary"] = {
        key: copy.deepcopy(timeline_summary.get(key))
        for key in ("entry_count", "source_sha256")
        if key in timeline_summary
    }
    view_summary = (
        projected.get("generation_state_view")
        if isinstance(projected.get("generation_state_view"), dict)
        else {}
    )
    projected["generation_state_view"] = {
        key: copy.deepcopy(view_summary.get(key))
        for key in (
            "schema_version",
            "chapter_index",
            "read_set_digest",
            "source_authority_sha256",
            "projection_sha256",
        )
        if key in view_summary
    }
    if isinstance(projected.get("story_project"), dict):
        projected["story_project"] = {
            "chapter_index": projected["story_project"].get("chapter_index")
        }
    selected = _selected_generation_ids(authority_summary)
    for collection in (
        "characters",
        "relationships",
        "roster",
        "numeric_counters",
        "inventory",
        "locations",
    ):
        records = projected.get(collection)
        projected[collection] = (
            {
                record_id: record
                for record_id, record in records.items()
                if str(record_id) in selected[collection]
            }
            if isinstance(records, dict)
            else {}
        )
    events = projected.get("required_events")
    projected["required_events"] = (
        {
            event_id: event
            for event_id, event in events.items()
            if str(event_id) in selected["events"]
        }
        if isinstance(events, dict)
        else {}
    )

    story_state = (
        projected.get("story_state")
        if isinstance(projected.get("story_state"), dict)
        else {}
    )
    projected["story_state"] = {
        key: copy.deepcopy(story_state.get(key))
        for key in (
            "last_scene_location",
            "last_scene_characters",
            "required_opening_bridge",
        )
        if key in story_state
    }
    relevant_location_ids = {
        str(record.get("location_id") or "").strip()
        for record in projected["locations"].values()
        if isinstance(record, dict)
        and str(record.get("location_id") or "").strip()
    }
    story_location = str(
        projected["story_state"].get("last_scene_location") or ""
    ).strip()
    if story_location:
        relevant_location_ids.add(story_location)
    projected["world_state"] = _project_explicit_world_state(
        projected.get("world_state"),
        relevant_location_ids=relevant_location_ids,
    )
    projected["spatial_state"] = _project_explicit_spatial_state(
        projected.get("spatial_state"),
        selected_character_ids=selected["characters"],
        selected_locations=projected["locations"],
        relevant_location_ids=relevant_location_ids,
    )
    projected["project_profile"] = _project_explicit_profile(
        projected.get("project_profile"),
        characters=projected["characters"],
        story_state=projected["story_state"],
        relevant_location_ids=relevant_location_ids,
    )
    return projected


def _selected_generation_ids(
    authority_summary: dict[str, Any],
) -> dict[str, set[str]]:
    selected = {
        collection: set()
        for collection in (
            "characters",
            "relationships",
            "roster",
            "numeric_counters",
            "inventory",
            "locations",
            "events",
        )
    }
    item_ids = [
        *(authority_summary.get("selected_state_item_ids") or []),
        *(authority_summary.get("selected_event_item_ids") or []),
    ]
    for raw in item_ids:
        item_id = str(raw or "").strip()
        collection, separator, record_id = item_id.partition("/")
        if separator and collection in selected and record_id:
            selected[collection].add(record_id)
    return selected


def _project_explicit_world_state(
    value: Any,
    *,
    relevant_location_ids: set[str],
) -> dict[str, Any]:
    source = _without_source_document(value) if isinstance(value, dict) else {}
    locations = source.get("locations")
    return {
        **{
            key: copy.deepcopy(item)
            for key, item in source.items()
            if key != "locations"
            and (
                isinstance(item, (bool, int, float))
                or item is None
                or isinstance(item, str)
            )
        },
        "locations": {
            location_id: copy.deepcopy(locations[location_id])
            for location_id in sorted(relevant_location_ids)
            if isinstance(locations, dict) and location_id in locations
        },
    }


def _project_explicit_spatial_state(
    value: Any,
    *,
    selected_character_ids: set[str],
    selected_locations: dict[str, Any],
    relevant_location_ids: set[str],
) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    spaces = source.get("spaces") if isinstance(source.get("spaces"), dict) else {}
    positions = (
        source.get("character_positions")
        if isinstance(source.get("character_positions"), dict)
        else {}
    )
    confirmed_location_entities = {
        entity_id
        for entity_id, record in selected_locations.items()
        if isinstance(record, dict) and _generation_location_confirmed(record)
    }
    allowed_position_ids = selected_character_ids | confirmed_location_entities
    return {
        "spaces": {
            location_id: copy.deepcopy(spaces[location_id])
            for location_id in sorted(relevant_location_ids)
            if location_id in spaces
        },
        "connections": [
            copy.deepcopy(item)
            for item in source.get("connections") or []
            if _generation_connection_relevant(
                item,
                relevant_location_ids=relevant_location_ids,
            )
        ],
        "character_positions": {
            entity_id: copy.deepcopy(positions[entity_id])
            for entity_id in sorted(allowed_position_ids)
            if entity_id in positions
        },
        "blocked_paths": [
            copy.deepcopy(item)
            for item in source.get("blocked_paths") or []
            if _generation_connection_relevant(
                item,
                relevant_location_ids=relevant_location_ids,
            )
        ],
        "last_transition": (
            copy.deepcopy(source.get("last_transition"))
            if _generation_connection_relevant(
                source.get("last_transition"),
                relevant_location_ids=relevant_location_ids,
            )
            else {}
        ),
    }


def _project_explicit_profile(
    value: Any,
    *,
    characters: dict[str, Any],
    story_state: dict[str, Any],
    relevant_location_ids: set[str],
) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    allowed_character_names = {
        str(item).strip()
        for item in story_state.get("last_scene_characters") or []
        if str(item).strip()
    }
    for character_id, record in characters.items():
        allowed_character_names.add(str(character_id))
        if not isinstance(record, dict):
            continue
        values = [
            record.get("canonical_name"),
            record.get("name"),
            *(record.get("aliases") or []),
        ]
        allowed_character_names.update(
            str(item).strip() for item in values if str(item or "").strip()
        )
    declared_characters = source.get("known_characters")
    declared_locations = source.get("known_locations")
    return {
        "language": str(
            source.get("language") or source.get("language_code") or ""
        ).strip(),
        "known_characters": [
            str(item)
            for item in declared_characters or []
            if str(item) in allowed_character_names
        ],
        "known_locations": [
            str(item)
            for item in declared_locations or []
            if str(item) in relevant_location_ids
        ],
    }


def _generation_location_confirmed(record: dict[str, Any]) -> bool:
    certainty = record.get("certainty")
    if certainty in (None, ""):
        return True
    return str(certainty).strip().casefold() in {"confirmed", "current"}


def _generation_connection_relevant(
    value: Any,
    *,
    relevant_location_ids: set[str],
) -> bool:
    if not isinstance(value, dict):
        return False
    endpoints = {
        str(value.get(key) or "").strip()
        for key in ("from", "to", "source", "target")
        if str(value.get(key) or "").strip()
    }
    return bool(endpoints) and endpoints.issubset(relevant_location_ids)


def _without_source_document(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"path", "relative_path", "source", "summary", "text", "truncated"}
    }


def _compact_timeline_entry(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "chapter_index",
            "summary",
            "events",
            "character_changes",
            "world_changes",
            "story_state",
            "spatial_state",
            "conflicts",
            "source_run_id",
        )
        if value.get(key) not in (None, [], {})
    }


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
) -> dict[str, Any] | None:
    if problem.get("fact_id") != chapter_fact_id:
        raise ValueError("LLM finding fact_id does not match the current chapter digest")

    verified_evidence: list[dict[str, Any]] = []
    for evidence in problem.get("evidence") or []:
        start = int(evidence["start_char"])
        end = int(evidence["end_char"])
        quote = str(evidence["quote"])
        if 0 <= start < end <= len(chapter_text) and chapter_text[start:end] == quote:
            verified_evidence.append(dict(evidence))
            continue

        actual_start = chapter_text.find(quote)
        if actual_start < 0 or chapter_text.find(quote, actual_start + 1) >= 0:
            continue

        corrected = dict(evidence)
        corrected["start_char"] = actual_start
        corrected["end_char"] = actual_start + len(quote)
        verified_evidence.append(corrected)

    if not verified_evidence:
        return None

    verified_problem = dict(problem)
    verified_problem["evidence"] = verified_evidence
    return verified_problem


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
