from __future__ import annotations

import json
import re
import warnings
from typing import Any

from api.contracts import (
    CHAPTER_CONTRACT,
    ModelOutputError,
    validate_language_output,
    validate_text_output,
)
from api.openai_client import chat_completion
from core.context_budget import ContextBudgetError, default_context_budget
from core.model_calls import build_scene_generation_call_id
from core.prompt_compiler import PROMPT_CONTEXT_SELECTION_KEYS, compile_prompt_contexts
from core.quality.final_artifact_integrity import (
    FinalArtifactIntegrityGate,
    build_integrity_stage_record,
)
from core.scene_continuity import (
    empty_scene_state,
    require_scene_transition,
    scene_delta_response_schema,
    scene_state_summary,
    validate_scene_transition,
)
from core.schema import validate_schema
from core.state.authoritative_context import (
    AUTHORITATIVE_SCENE_SECTION_MAX_CHARS,
    authoritative_event_is_open,
    authoritative_state_from_markdown,
    compact_authoritative_state_in_markdown,
)
from core.state.story_state_context import STORY_STATE_CONTEXT_KEYS, STORY_STATE_SECTION_MAX_CHARS
from core.structured_context import compact_markdown_context, select_text_blocks
from core.story_project.coverage import (
    blueprint_to_dict,
    build_blueprint_coverage,
    build_blueprint_plan,
    validate_generation_blueprint_contract,
)
from modules.chapter_generator.generator import _DRY_RUN_CHAPTER, _load_scene_prompt


PIPELINE_STAGE_NAMES = (
    "plan_chapter",
    "generate_scenes",
    "merge_scenes",
    "validate",
    "repair",
    "commit",
)
_STORY_PROJECT_BLUEPRINT_SECTION_MAX_CHARS = 4_096
_SCENE_INPUT_HEADROOM_TOKENS = 2_000
_CHAPTER_PLAN_PROMPT = (
    "Create a compact chapter plan as JSON only. "
    "Schema: {\"goal\": string, \"scenes\": [{\"index\": int, \"type\": string, "
    "\"goal\": string, \"required_beats\": [string], \"planned_events\": "
    "[{\"event_id\": string, \"type\": string, \"subjects\": [string], "
    "\"objects\": [string], \"location\": string, \"status\": \"completed\"}]}]}. "
    "Give every beat a stable event_id scoped to exactly one scene. Keep it to "
    "2-4 scenes. Scene 1 must be type opening_bridge and continue directly from "
    "the last chapter ending."
)
_SCENE_BOUNDARY_REGENERATION_LIMIT = 1
_SCENE_BOUNDARY_FEEDBACK_FINDING_LIMIT = 4
_SCENE_BOUNDARY_FEEDBACK_EVIDENCE_MAX_CHARS = 480
_EVENT_LOCATION_CONTRACT = (
    "Use one exact, non-compound location for each event.location. Every known entity "
    "listed in event.subjects or event.objects is asserted to be physically present "
    "there. Omit remote or differently located entities from that event, and make each "
    "matching location delta.after exactly equal event.location."
)
_SCENE_CONTEXT_PAYLOAD_POLICIES = (
    {
        "previous_tail_chars": 600,
        "summary_limit": 8,
        "summary_tail_chars": 280,
        "include_summary_goal": True,
    },
    {
        "previous_tail_chars": 500,
        "summary_limit": 8,
        "summary_tail_chars": 200,
        "include_summary_goal": False,
    },
    {
        "previous_tail_chars": 400,
        "summary_limit": 8,
        "summary_tail_chars": 120,
        "include_summary_goal": False,
    },
    {
        "previous_tail_chars": 360,
        "summary_limit": 6,
        "summary_tail_chars": 120,
        "include_summary_goal": False,
    },
)


def run_chapter_pipeline(
    input_pack: str,
    *,
    chapter_index: int,
    dry_run: bool = False,
    scene_limit: int | None = None,
    language: str | None = None,
    chapter_blueprint: dict[str, Any] | None = None,
    recovered_scene_drafts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    initial_scene_state = _initial_scene_state(input_pack)
    authoritative_state_source = authoritative_state_from_markdown(input_pack)
    prompt_contexts = compile_prompt_contexts(
        input_pack,
        budget=default_context_budget(enable_model_tokenizer=not dry_run),
        stage_protocol_texts=(
            {"plan": (_CHAPTER_PLAN_PROMPT,)}
            if blueprint is None
            else None
        ),
    )
    plan_input = prompt_contexts.plan.text
    scene_input = prompt_contexts.scene.text
    if blueprint is None:
        plan = plan_scenes(plan_input, chapter_index=chapter_index, dry_run=dry_run)
        plan = _limit_plan_scenes(plan, scene_limit)
    else:
        validate_generation_blueprint_contract(blueprint)
        plan = plan_scenes(
            plan_input,
            chapter_index=chapter_index,
            dry_run=dry_run,
            chapter_blueprint=blueprint,
            scene_limit=scene_limit,
        )
    scenes = generate_scenes(
        scene_input,
        plan,
        dry_run=dry_run,
        language=language,
        chapter_blueprint=blueprint,
        recovered_scene_drafts=recovered_scene_drafts,
        initial_scene_state=initial_scene_state,
        authoritative_state_source=authoritative_state_source,
    )
    merged, scene_spans = _merge_scene_texts(scenes)
    merged = validate_language_output(merged, CHAPTER_CONTRACT, language=language)
    scene_events = [
        event
        for scene in scenes
        for event in (scene.get("events") or [])
        if isinstance(event, dict)
    ]
    merge_integrity = FinalArtifactIntegrityGate().evaluate(
        artifact_text=merged,
        stage="merge",
        scene_events=scene_events,
        scene_drafts=scenes,
        scene_spans=scene_spans,
    )
    blueprint_coverage = build_blueprint_coverage(blueprint, scenes, merged) if blueprint is not None else None
    boundary_validations = [
        dict(scene["boundary_validation"])
        for scene in scenes
        if isinstance(scene.get("boundary_validation"), dict)
    ]
    final_scene_state = (
        dict(scenes[-1]["scene_state_after"])
        if scenes and isinstance(scenes[-1].get("scene_state_after"), dict)
        else empty_scene_state()
    )
    return validate_schema(
        {
            "chapter_index": int(chapter_index),
            "story_project": {"enabled": True} if blueprint is not None else None,
            "chapter_blueprint": blueprint,
            "plan": plan,
            "scene_drafts": scenes,
            "merged_chapter": merged,
            "scene_spans": scene_spans,
            "scene_boundary_validations": boundary_validations,
            "scene_state_final": final_scene_state,
            "integrity": {"merge": merge_integrity},
            "integrity_records": [
                build_integrity_stage_record(
                    stage="merge",
                    input_text=None,
                    output_text=merged,
                    report=merge_integrity,
                )
            ],
            "blueprint_coverage": blueprint_coverage,
            "context_budget": {
                "context_digest": prompt_contexts.context_digest,
                "plan": prompt_contexts.plan.report,
                "scene": prompt_contexts.scene.report,
                "repair": prompt_contexts.repair.report,
                "plan_sections": list(prompt_contexts.plan.selected_sections),
                "scene_sections": list(prompt_contexts.scene.selected_sections),
                "repair_sections": list(prompt_contexts.repair.selected_sections),
                "plan_selection": dict(prompt_contexts.plan.selection_manifest),
                "scene_selection": dict(prompt_contexts.scene.selection_manifest),
                "repair_selection": dict(prompt_contexts.repair.selection_manifest),
            },
            "stages": _pipeline_stages(
                {
                    "plan_chapter": {
                        "status": "completed",
                        "artifact_key": "plan",
                        "summary": {"scene_count": len(plan.get("scenes", []))},
                    },
                    "generate_scenes": {
                        "status": "completed",
                        "artifact_key": "scene_drafts",
                        "summary": {"scene_count": len(scenes)},
                    },
                    "merge_scenes": {
                        "status": "completed",
                        "artifact_key": "merged_chapter",
                        "summary": {"chars": len(merged)},
                    },
                    **(
                        {
                            "validate": {
                                "status": "pending",
                                "artifact_key": "blueprint_coverage",
                                "summary": blueprint_coverage,
                            }
                        }
                        if blueprint_coverage is not None
                        else {}
                    ),
                }
            ),
        },
        "chapter_pipeline.schema.json",
    )


def plan_scenes(
    input_pack: str,
    *,
    chapter_index: int,
    dry_run: bool = False,
    chapter_blueprint: dict[str, Any] | None = None,
    scene_limit: int | None = None,
) -> dict[str, Any]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    if blueprint is not None:
        return _validate_plan(
            build_blueprint_plan(blueprint, scene_limit=scene_limit),
            chapter_index=chapter_index,
        )

    if dry_run:
        return _validate_plan(_dry_run_plan(chapter_index), chapter_index=chapter_index)

    payload = _request_chapter_plan(input_pack, _CHAPTER_PLAN_PROMPT)
    try:
        plan = _load_plan_json(payload)
    except json.JSONDecodeError as first_exc:
        repair_payload = _request_chapter_plan_json_repair(input_pack, chapter_index, payload, first_exc)
        try:
            plan = _load_plan_json(repair_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Chapter plan response was not valid JSON") from exc
    if not isinstance(plan, dict):
        raise ValueError("Chapter plan response must be a JSON object")
    return _validate_plan(plan, chapter_index=chapter_index)


def plan_chapter(
    input_pack: str,
    *,
    chapter_index: int,
    dry_run: bool = False,
    chapter_blueprint: dict[str, Any] | None = None,
    scene_limit: int | None = None,
) -> dict[str, Any]:
    """Deprecated compatibility alias for :func:`plan_scenes`."""

    warnings.warn(
        "plan_chapter() is deprecated; use plan_scenes() instead",
        FutureWarning,
        stacklevel=2,
    )
    return plan_scenes(
        input_pack,
        chapter_index=chapter_index,
        dry_run=dry_run,
        chapter_blueprint=chapter_blueprint,
        scene_limit=scene_limit,
    )


def _request_chapter_plan(input_pack: str, prompt: str) -> str:
    default_context_budget().require_input(
        input_pack,
        stage="plan",
        protocol_texts=(prompt,),
    )
    return chat_completion(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": input_pack},
        ],
        temperature=0.2,
        stage="chapter_generation",
    )


def _request_chapter_plan_json_repair(
    input_pack: str,
    chapter_index: int,
    invalid_payload: str,
    error: json.JSONDecodeError,
) -> str:
    query = f"chapter plan JSON repair\n{error}"
    input_selection = compact_markdown_context(
        input_pack,
        max_chars=6_000,
        per_section_max_chars=1_200,
        query=query,
        required_sections={
            "Context Digest",
            "Prompt Context Selection",
            "Director Decision",
            "Story State",
            "StoryProject Chapter Blueprint",
            "Requirements",
        },
        excluded_sections={"Memory Index", "Structured Context Manifest"},
        required_json_keys={
            "Prompt Context Selection": PROMPT_CONTEXT_SELECTION_KEYS,
            "Story State": STORY_STATE_CONTEXT_KEYS,
            "StoryProject Chapter Blueprint": {"chapter_blueprint", "read_set_context_digest"},
        },
        allowed_json_keys={
            "Prompt Context Selection": PROMPT_CONTEXT_SELECTION_KEYS,
            "Story State": STORY_STATE_CONTEXT_KEYS,
        },
        section_max_chars={
            "Story State": STORY_STATE_SECTION_MAX_CHARS,
            "StoryProject Chapter Blueprint": _STORY_PROJECT_BLUEPRINT_SECTION_MAX_CHARS,
        },
        policy="plan_json_repair_input_v1",
    )
    invalid_selection = select_text_blocks(
        invalid_payload,
        max_chars=4_000,
        query=str(error),
        required="edges",
        prefer_recent=False,
        policy="invalid_plan_response_blocks_v1",
    )
    request_payload = json.dumps(
        {
            "chapter_index": chapter_index,
            "json_error": str(error),
            "invalid_response": invalid_selection.text,
            "invalid_response_selection": invalid_selection.manifest(),
            "input_pack_excerpt": input_selection.text,
            "input_pack_selection": input_selection.manifest(),
        },
        ensure_ascii=False,
        indent=2,
    )
    default_context_budget().require_input(request_payload, stage="plan_json_repair")
    return chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Repair the chapter plan response into JSON only. "
                    "Return exactly one object with shape "
                    "{\"goal\": string, \"scenes\": [{\"index\": int, \"type\": string, "
                    "\"goal\": string, \"required_beats\": [string], \"planned_events\": "
                    "[{\"event_id\": string, \"type\": string, \"subjects\": [string], "
                    "\"objects\": [string], \"location\": string, \"status\": \"completed\"}]}]}. "
                    "No prose, no Markdown, no explanation."
                ),
            },
            {
                "role": "user",
                "content": request_payload,
            },
        ],
        temperature=0.0,
        stage="chapter_generation",
    )


def _load_plan_json(payload: str) -> Any:
    text = str(payload or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _load_scene_response(payload: str) -> dict[str, Any]:
    try:
        value = _load_plan_json(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Scene response was not valid structured JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Scene response must be a JSON object")
    prose = validate_text_output(value.get("prose"), CHAPTER_CONTRACT)
    raw_events = value.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("Scene response events must be an array")
    if any(not isinstance(item, dict) for item in raw_events):
        raise ValueError("Scene response events must contain objects only")
    raw_deltas = value.get("deltas")
    if not isinstance(raw_deltas, dict):
        raise ValueError("Scene response deltas must be an object")
    delta_keys = (
        "characters",
        "relationships",
        "rosters",
        "locations",
        "inventory",
        "counters",
    )
    for key in delta_keys:
        items = raw_deltas.get(key)
        if not isinstance(items, list):
            raise ValueError(f"Scene response deltas.{key} must be an array")
        if any(not isinstance(item, dict) for item in items):
            raise ValueError(
                f"Scene response deltas.{key} must contain objects only"
            )
    return {
        "prose": prose,
        "events": [_normalize_scene_event(item) for item in raw_events],
        "deltas": {
            key: [dict(item) for item in raw_deltas[key]]
            for key in delta_keys
        },
        "continuity_note": str(value.get("continuity_note") or ""),
    }


def generate_scenes(
    input_pack: str,
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
    language: str | None = None,
    chapter_blueprint: dict[str, Any] | None = None,
    recovered_scene_drafts: list[dict[str, Any]] | None = None,
    initial_scene_state: dict[str, Any] | None = None,
    authoritative_state_source: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    plan = _validate_plan(plan)
    recovered = _recovered_scene_prefix(recovered_scene_drafts, plan)
    scene_drafts: list[dict[str, Any]] = []
    scene_state = (
        scene_state_summary(initial_scene_state)
        if initial_scene_state is not None
        else _initial_scene_state(input_pack)
    )
    prior_scene_summaries: list[dict[str, Any]] = []
    for scene in plan.get("scenes", []):
        required_beat_indexes = _scene_beat_indexes(scene)
        scene_required_beats = [
            beat
            for beat in (blueprint or {}).get("required_beats", [])
            if isinstance(beat, dict) and int(beat.get("index") or 0) in required_beat_indexes
        ]
        scene_index = int(scene["index"])
        state_before = scene_state_summary(scene_state)
        previous_scene_tail = (
            str(scene_drafts[-1]["text"])[-600:]
            if scene_drafts
            else ""
        )
        if scene_index in recovered:
            scene_result = _recovered_scene_result(
                recovered[scene_index],
                scene=scene,
            )
        elif dry_run:
            scene_result = _dry_run_scene_result(
                plan,
                scene,
                chapter_blueprint=blueprint,
            )
        else:
            scene_result = _request_scene_candidate(
                input_pack=input_pack,
                plan=plan,
                scene=scene,
                scene_required_beats=scene_required_beats,
                blueprint=blueprint,
                previous_scene_tail=previous_scene_tail,
                prior_scene_summaries=prior_scene_summaries,
                state_before=state_before,
                language=language,
                authoritative_state_source=authoritative_state_source,
            )
        local_regeneration_attempts = 0
        rejected_boundaries: list[dict[str, Any]] = []
        while True:
            scene_text = validate_language_output(
                scene_result["prose"],
                CHAPTER_CONTRACT,
                language=language,
            )
            boundary, state_after = validate_scene_transition(
                scene_index=scene_index,
                state_before=state_before,
                events=scene_result["events"],
                deltas=scene_result["deltas"],
                required_event_ids=scene.get("required_event_ids") or [],
                forbidden_event_ids=scene.get("forbidden_event_ids") or [],
                planned_events=scene.get("planned_events") or [],
            )
            if boundary["accepted"]:
                break
            feedback = _scene_boundary_retry_feedback(
                boundary,
                attempt=local_regeneration_attempts + 1,
            )
            rejected_boundaries.append(feedback)
            if (
                dry_run
                or local_regeneration_attempts >= _SCENE_BOUNDARY_REGENERATION_LIMIT
            ):
                boundary = {
                    **boundary,
                    "local_regeneration_attempts": local_regeneration_attempts,
                    "local_regeneration_rejections": rejected_boundaries,
                }
                require_scene_transition(boundary)
            local_regeneration_attempts += 1
            scene_result = _request_scene_candidate(
                input_pack=input_pack,
                plan=plan,
                scene=scene,
                scene_required_beats=scene_required_beats,
                blueprint=blueprint,
                previous_scene_tail=previous_scene_tail,
                prior_scene_summaries=prior_scene_summaries,
                state_before=state_before,
                language=language,
                boundary_retry=feedback,
                boundary_retry_attempt=local_regeneration_attempts,
                authoritative_state_source=authoritative_state_source,
            )
        if local_regeneration_attempts:
            boundary = {
                **boundary,
                "local_regeneration_attempts": local_regeneration_attempts,
                "local_regeneration_rejections": rejected_boundaries,
            }
        draft = {
            "index": scene_index,
            "goal": str(scene["goal"]),
            **({"covered_beat_indexes": required_beat_indexes} if required_beat_indexes else {}),
            **(
                {"ending_pressure_covered": True}
                if blueprint is not None and int(scene["index"]) == _last_scene_index(plan)
                else {}
            ),
            "text": scene_text,
            "events": scene_result["events"],
            "deltas": scene_result["deltas"],
            "continuity_note": str(scene_result.get("continuity_note") or ""),
            **(
                {"source_call_id": str(scene_result["source_call_id"])}
                if scene_result.get("source_call_id")
                else {}
            ),
            **(
                {"source_attempt_id": str(scene_result["source_attempt_id"])}
                if scene_result.get("source_attempt_id")
                else {}
            ),
            "scene_state_before": state_before,
            "scene_state_after": scene_state_summary(state_after),
            "boundary_validation": boundary,
        }
        require_scene_transition(boundary)
        scene_drafts.append(draft)
        scene_state = state_after
        prior_scene_summaries.append(
            {
                "index": scene_index,
                "goal": str(scene["goal"]),
                "tail": scene_text[-280:],
                "event_ids": [str(event["event_id"]) for event in scene_result["events"]],
            }
        )
    return _validate_scene_drafts(scene_drafts)


def _request_scene_candidate(
    *,
    input_pack: str,
    plan: dict[str, Any],
    scene: dict[str, Any],
    scene_required_beats: list[dict[str, Any]],
    blueprint: dict[str, Any] | None,
    previous_scene_tail: str,
    prior_scene_summaries: list[dict[str, Any]],
    state_before: dict[str, Any],
    language: str | None,
    boundary_retry: dict[str, Any] | None = None,
    boundary_retry_attempt: int = 0,
    authoritative_state_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scene_index = int(scene["index"])
    call_kwargs: dict[str, Any] = {
        "stage": "chapter_generation",
        "call_id": build_scene_generation_call_id(
            scene_index,
            boundary_retry=boundary_retry_attempt,
        ),
    }
    if boundary_retry is not None:
        call_kwargs["temperature"] = 0.0
    payload = chat_completion(
        [
            {"role": "system", "content": _load_scene_prompt()},
            {
                "role": "user",
                "content": _scene_request_payload(
                    input_pack=input_pack,
                    plan=plan,
                    scene=scene,
                    scene_required_beats=scene_required_beats,
                    blueprint=blueprint,
                    previous_scene_tail=previous_scene_tail,
                    prior_scene_summaries=prior_scene_summaries,
                    scene_state=state_before,
                    boundary_retry=boundary_retry,
                    authoritative_state_source=authoritative_state_source,
                ),
            },
        ],
        **call_kwargs,
    )
    result = _load_scene_response(payload)
    result["source_call_id"] = str(call_kwargs["call_id"])
    return result


def _scene_boundary_retry_feedback(
    report: dict[str, Any],
    *,
    attempt: int,
) -> dict[str, Any]:
    raw_findings = [
        item
        for item in report.get("findings") or []
        if isinstance(item, dict)
    ]
    findings: list[dict[str, Any]] = []
    for item in raw_findings[:_SCENE_BOUNDARY_FEEDBACK_FINDING_LIMIT]:
        findings.append(
            {
                "code": str(item.get("code") or "unknown"),
                "message": str(item.get("message") or "")[:320],
                "evidence": _bounded_boundary_evidence(item.get("evidence")),
            }
        )
    return {
        "attempt": int(attempt),
        "scene_index": int(report.get("scene_index") or 0),
        "state_before_sha256": str(report.get("state_before_sha256") or ""),
        "finding_count": len(raw_findings),
        "omitted_finding_count": max(0, len(raw_findings) - len(findings)),
        "findings": findings,
        "instruction": (
            "Regenerate this same scene only. Treat current_scene_state as authoritative. "
            "Correct or remove every rejected delta; each before value must exactly equal "
            "the current value. Do not repeat any earlier scene or event. "
            + _EVENT_LOCATION_CONTRACT
        ),
    }


def _bounded_boundary_evidence(value: Any) -> Any:
    if value is None:
        return {}
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        serialized = str(value)
    if len(serialized) <= _SCENE_BOUNDARY_FEEDBACK_EVIDENCE_MAX_CHARS:
        try:
            return json.loads(serialized)
        except json.JSONDecodeError:
            return serialized
    return {
        "truncated_json": (
            serialized[: _SCENE_BOUNDARY_FEEDBACK_EVIDENCE_MAX_CHARS - 3]
            + "..."
        )
    }


def _recovered_scene_prefix(
    scene_drafts: list[dict[str, Any]] | None,
    plan: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    if not scene_drafts:
        return {}
    plan_indexes = [
        int(scene["index"])
        for scene in plan.get("scenes", [])
        if isinstance(scene, dict) and isinstance(scene.get("index"), int)
    ]
    recovered: dict[int, dict[str, Any]] = {}
    for position, draft in enumerate(scene_drafts, start=1):
        if not isinstance(draft, dict) or int(draft.get("index") or 0) != position:
            raise ValueError("recovered scenes must form a contiguous prefix starting at scene 1")
        if position not in plan_indexes:
            raise ValueError("recovered scene count exceeds the current chapter plan")
        if not _has_complete_structured_scene_payload(draft):
            # Legacy prose remains immutable evidence, but it cannot be reused
            # as an accepted Scene because it has no model-declared boundary
            # facts. Regenerate this scene and the remaining suffix instead of
            # synthesizing completed events from the plan.
            break
        recovered[position] = {
            **dict(draft),
            "text": validate_text_output(draft.get("text"), CHAPTER_CONTRACT),
        }
    return recovered


def _scene_request_payload(
    *,
    input_pack: str,
    plan: dict[str, Any],
    scene: dict[str, Any],
    scene_required_beats: list[dict[str, Any]],
    blueprint: dict[str, Any] | None,
    previous_scene_tail: str = "",
    prior_scene_summaries: list[dict[str, Any]] | None = None,
    scene_state: dict[str, Any] | None = None,
    boundary_retry: dict[str, Any] | None = None,
    authoritative_state_source: dict[str, Any] | None = None,
) -> str:
    target_min_chars, target_max_chars = _scene_target_char_range(plan)
    chapter_plan_context = _compact_chapter_plan_context(plan)
    context_query = json.dumps(
        {
            "chapter_plan": chapter_plan_context,
            "scene": scene,
            "required_beats": scene_required_beats,
            "ending_pressure": (blueprint or {}).get("ending_pressure"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    payload_body = {
        "chapter_plan": chapter_plan_context,
        "scene": scene,
        "story_project_required_beats": scene_required_beats,
        "story_project_ending_pressure": (blueprint or {}).get("ending_pressure"),
        "current_scene_state": scene_state_summary(scene_state or empty_scene_state()),
        "required_event_ids": list(scene.get("required_event_ids") or []),
        "forbidden_event_ids": list(scene.get("forbidden_event_ids") or []),
        "response_schema": {
            "prose": "string",
            "events": [
                {
                    "event_id": "one required_event_id",
                    "type": "string",
                    "subjects": ["stable character ids"],
                    "objects": ["stable entity ids"],
                    "location": "stable location id",
                    "status": "completed|started|ongoing",
                }
            ],
            "deltas": scene_delta_response_schema(),
            "continuity_note": "string",
        },
        "delta_rules": [
            "Use an empty list for every state kind that does not actually change in this scene.",
            "Do not encode confirmations, observations, scene presence, role descriptions, rules, thresholds, "
            "or prose facts as deltas.",
            "Use exactly the type-specific id fields shown in response_schema.deltas; never use "
            "stable_entity_id as a generic substitute.",
            "A genuinely new character uses separate character deltas for canonical_name and aliases. Never "
            "put a nested character record in one after value or re-introduce a character already present in "
            "the project context.",
            "Every before value must exactly match current_scene_state. Inventory and counter before, delta, "
            "and after values must be numbers with after equal to before plus delta.",
            "The numeric values shown in response_schema are type examples, not values to copy.",
            "For roster join or replace, member_ids and members must describe the same stable member ids. "
            "Do not invent missing roster members just to satisfy a declared count.",
        ],
        **({"boundary_retry": boundary_retry} if boundary_retry is not None else {}),
        "instruction": (
            "Return JSON only and exactly one object matching response_schema. "
            "prose must draft only this scene as continuous prose with no heading. "
            "If story_project_required_beats are provided, cover each listed beat in the prose and preserve "
            "its essential factual phrases closely enough for deterministic coverage checks. "
            f"Target {target_min_chars}-{target_max_chars} Chinese characters for this scene when the project "
            "language is zh-CN, so the merged chapter remains 3000-4500 Chinese characters; treat the upper "
            "bound as a hard limit and stop the scene before exceeding it. "
            "Every required_event_id must appear exactly once in events. Do not restart, duplicate, or retell "
            "a completed event; never use a forbidden_event_id or roll state back. "
            f"{_EVENT_LOCATION_CONTRACT} "
            "Deltas must follow the exact type-specific fields and delta_rules in this request. Do not emit a "
            "delta merely to restate an unchanged fact. Continue directly from previous_scene_tail and "
            "current_scene_state."
        ),
    }
    budget = default_context_budget()
    safe_input_limit = _scene_safe_input_limit(budget)
    protocol_texts = (_load_scene_prompt(),)
    compact_scene_context = _compact_scene_context(
        input_pack,
        query=context_query,
        authoritative_state_source=authoritative_state_source,
    )
    payload = ""
    for policy in _SCENE_CONTEXT_PAYLOAD_POLICIES:
        compact_summaries = _compact_prior_scene_summaries(
            prior_scene_summaries,
            limit=int(policy["summary_limit"]),
            tail_chars=int(policy["summary_tail_chars"]),
            include_goal=bool(policy["include_summary_goal"]),
        )
        payload = json.dumps(
            {
                "shared_context": compact_scene_context,
                **payload_body,
                "previous_scene_tail": str(previous_scene_tail)[
                    -int(policy["previous_tail_chars"]) :
                ],
                "prior_scene_summaries": compact_summaries,
            },
            ensure_ascii=False,
            indent=2,
        )
        report = budget.measure(payload, stage="scene", protocol_texts=protocol_texts)
        if report["within_budget"] and _scene_report_within_safe_limit(
            report,
            safe_input_limit=safe_input_limit,
        ):
            return payload
    report = budget.require_input(payload, stage="scene", protocol_texts=protocol_texts)
    if not _scene_report_within_safe_limit(
        report,
        safe_input_limit=safe_input_limit,
    ):
        raise ContextBudgetError(
            "story_project_context_headroom_exceeded",
            "scene input requires "
            f"{report['budgeted_input_tokens']} tokens; safe target is "
            f"{safe_input_limit}; hard limit is {report['hard_input_limit']}",
        )
    return payload


def _compact_chapter_plan_context(plan: dict[str, Any]) -> dict[str, Any]:
    """Keep the chapter arc while avoiding per-scene duplication of full beat records."""

    scenes: list[dict[str, Any]] = []
    for raw in plan.get("scenes") or []:
        if not isinstance(raw, dict):
            continue
        scene = {
            key: raw.get(key)
            for key in ("index", "type", "goal")
            if raw.get(key) is not None
        }
        for key in ("required_beat_indexes", "required_event_ids"):
            values = raw.get(key)
            if isinstance(values, list):
                scene[key] = list(values)
        scenes.append(scene)
    return {
        "goal": str(plan.get("goal") or ""),
        "scenes": scenes,
    }


def _scene_safe_input_limit(budget: Any) -> int | None:
    hard_limit = getattr(budget, "hard_input_limit", None)
    if isinstance(hard_limit, bool) or not isinstance(hard_limit, int) or hard_limit < 1:
        return None
    headroom = min(
        _SCENE_INPUT_HEADROOM_TOKENS,
        max(256, hard_limit // 16),
    )
    return max(1, hard_limit - headroom)


def _scene_report_within_safe_limit(
    report: dict[str, Any],
    *,
    safe_input_limit: int | None,
) -> bool:
    if safe_input_limit is None:
        return True
    tokens = report.get("budgeted_input_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        return True
    return tokens <= safe_input_limit


def _compact_prior_scene_summaries(
    summaries: list[dict[str, Any]] | None,
    *,
    limit: int,
    tail_chars: int,
    include_goal: bool,
) -> list[dict[str, Any]]:
    """Bound accumulated scene evidence without discarding completed event ids."""

    bounded_limit = max(0, limit)
    if bounded_limit == 0:
        return []
    compacted: list[dict[str, Any]] = []
    for raw in list(summaries or [])[-bounded_limit:]:
        if not isinstance(raw, dict):
            continue
        item = {
            "index": int(raw.get("index") or 0),
            "tail": str(raw.get("tail") or "")[-max(0, tail_chars) :],
            "event_ids": [str(value) for value in raw.get("event_ids") or [] if str(value)],
        }
        if include_goal:
            item["goal"] = str(raw.get("goal") or "")
        compacted.append(item)
    return compacted


def _scene_target_char_range(plan: dict[str, Any]) -> tuple[int, int]:
    scene_count = max(
        1,
        len([item for item in plan.get("scenes", []) if isinstance(item, dict)]),
    )
    target_min_chars = max(600, 3_000 // scene_count)
    return target_min_chars, max(target_min_chars, 4_500 // scene_count)


def _compact_scene_context(
    text: str,
    *,
    max_section_chars: int = 1_500,
    query: str = "",
    authoritative_state_source: dict[str, Any] | None = None,
) -> str:
    """Retrieve complete sections/JSON items relevant to the current scene."""
    authority_section_limit = min(
        AUTHORITATIVE_SCENE_SECTION_MAX_CHARS,
        max(1, max_section_chars * 7),
    )
    projected_text = compact_authoritative_state_in_markdown(
        text,
        max_section_chars=authority_section_limit,
        query=query,
        require_query_references=False,
        require_open_events=False,
        authoritative_state_source=authoritative_state_source,
    )
    selection = compact_markdown_context(
        projected_text,
        max_chars=max_section_chars * 7,
        per_section_max_chars=max_section_chars,
        query=query,
        required_sections={
            "Context Digest",
            "Prompt Context Selection",
            "Project Profile",
            "Director Decision",
            "Story State",
            "Spatial State",
            "Authoritative State",
            "StoryProject Chapter Blueprint",
            "Requirements",
            "灏忚鐢熸垚瑙勫垯濂戠害",
        },
        excluded_sections={"Memory Index", "Structured Context Manifest"},
        required_json_keys={
            "Prompt Context Selection": PROMPT_CONTEXT_SELECTION_KEYS,
            "Story State": STORY_STATE_CONTEXT_KEYS,
            "StoryProject Chapter Blueprint": {"chapter_blueprint", "read_set_context_digest"},
        },
        allowed_json_keys={
            "Prompt Context Selection": PROMPT_CONTEXT_SELECTION_KEYS,
            "Story State": STORY_STATE_CONTEXT_KEYS,
        },
        section_max_chars={
            "Story State": STORY_STATE_SECTION_MAX_CHARS,
            "Authoritative State": authority_section_limit,
            "StoryProject Chapter Blueprint": _STORY_PROJECT_BLUEPRINT_SECTION_MAX_CHARS,
        },
        prefer_recent=True,
        policy="scene_markdown_json_retrieval_v1",
    )
    return selection.text


def _initial_scene_state(input_pack: str) -> dict[str, Any]:
    state = empty_scene_state()
    authority = authoritative_state_from_markdown(input_pack)
    if isinstance(authority, dict):
        for character_id, record in (authority.get("characters") or {}).items():
            if isinstance(record, dict):
                state["characters"][str(character_id)] = dict(record)
        for relationship_id, record in (authority.get("relationships") or {}).items():
            if isinstance(record, dict):
                source_id = str(record.get("source_character_id") or "")
                target_id = str(record.get("target_character_id") or "")
                key = f"{source_id}->{target_id}" if source_id and target_id else str(relationship_id)
                state["relationships"][key] = dict(record)
        for roster_id, record in (authority.get("roster") or {}).items():
            if isinstance(record, dict):
                state["rosters"][str(roster_id)] = dict(record)
        for counter_id, record in (authority.get("numeric_counters") or {}).items():
            if isinstance(record, dict) and isinstance(record.get("current_value"), (int, float)):
                state["counters"][str(counter_id)] = record["current_value"]
        for inventory_id, record in (authority.get("inventory") or {}).items():
            if isinstance(record, dict) and isinstance(record.get("quantity"), (int, float)):
                state["inventories"][str(inventory_id)] = record["quantity"]
        for entity_id, record in (authority.get("locations") or {}).items():
            if isinstance(record, dict) and record.get("location_id") not in (None, ""):
                state["locations"][str(entity_id)] = record["location_id"]
        for event_id, record in (authority.get("events") or {}).items():
            if not isinstance(record, dict):
                continue
            normalized = str(event_id).strip()
            if not authoritative_event_is_open(record):
                if normalized and normalized not in state["completed_event_ids"]:
                    state["completed_event_ids"].append(normalized)
                state["completed_events"].append(dict(record))
            else:
                state["open_actions"].append(dict(record))
        state["open_action"] = (
            str(state["open_actions"][0].get("event_id") or "")
            if state["open_actions"]
            else ""
        )
    story_state = _input_pack_json_section(input_pack, "Story State") or {}
    _apply_scene_bridge(
        state,
        authority=authority if isinstance(authority, dict) else {},
        story_state=story_state,
    )
    for event_id in story_state.get("completed_event_ids") or []:
        normalized = str(event_id).strip()
        if normalized and normalized not in state["completed_event_ids"]:
            state["completed_event_ids"].append(normalized)
    return state


def _input_pack_json_section(input_pack: str, section: str) -> dict[str, Any] | None:
    match = re.search(
        rf"(?ms)^# {re.escape(section)}[ \t]*\r?\n(.*?)(?=^# |\Z)",
        str(input_pack or ""),
    )
    if not match:
        return None
    body = match.group(1).strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _apply_scene_bridge(
    state: dict[str, Any],
    *,
    authority: dict[str, Any],
    story_state: dict[str, Any],
) -> None:
    """Apply Story State only where the authoritative ledger has no answer."""

    story_location = str(
        story_state.get("last_scene_location") or ""
    ).strip()
    raw_story_characters = story_state.get("last_scene_characters")
    story_characters = (
        [
            normalized
            for character in raw_story_characters
            if (normalized := str(character).strip())
        ]
        if isinstance(raw_story_characters, list)
        else []
    )
    aliases = _authoritative_character_aliases(authority)
    resolved_story_characters = [
        aliases.get(character_id, character_id)
        for character_id in story_characters
    ]
    known_characters = _authoritative_character_ids(authority)
    locations = {
        str(entity_id): str(location_id).strip()
        for entity_id, location_id in (state.get("locations") or {}).items()
        if str(entity_id).strip() and str(location_id).strip()
    }
    authoritative_location = _authoritative_bridge_location(
        authority,
        locations=locations,
        known_characters=known_characters,
        story_location=story_location,
        story_characters=resolved_story_characters,
    )
    if authoritative_location:
        state["current_location"] = authoritative_location
        present = {
            character_id
            for character_id in known_characters
            if locations.get(character_id) == authoritative_location
        }
        if story_location == authoritative_location:
            present.update(
                character_id
                for character_id in resolved_story_characters
                if not locations.get(character_id)
                or locations.get(character_id) == authoritative_location
            )
        state["characters_present"] = sorted(present)
        return
    if not story_location:
        return
    state["current_location"] = story_location
    state["characters_present"] = story_characters
    for character_id in story_characters:
        state["locations"].setdefault(character_id, story_location)


def _authoritative_bridge_location(
    authority: dict[str, Any],
    *,
    locations: dict[str, str],
    known_characters: set[str],
    story_location: str,
    story_characters: list[str],
) -> str:
    if story_characters:
        story_authority_locations = {
            locations[character_id]
            for character_id in story_characters
            if locations.get(character_id)
        }
        if story_location in story_authority_locations:
            return story_location
        if len(story_authority_locations) == 1:
            return next(iter(story_authority_locations))
        protagonist_locations = _protagonist_locations(
            authority,
            locations=locations,
            allowed_characters=set(story_characters),
        )
        return (
            next(iter(protagonist_locations))
            if len(protagonist_locations) == 1
            else ""
        )
    protagonist_locations = _protagonist_locations(
        authority,
        locations=locations,
        allowed_characters=known_characters,
    )
    if len(protagonist_locations) == 1:
        return next(iter(protagonist_locations))
    known_locations = {
        locations[character_id]
        for character_id in known_characters
        if locations.get(character_id)
    }
    return next(iter(known_locations)) if len(known_locations) == 1 else ""


def _protagonist_locations(
    authority: dict[str, Any],
    *,
    locations: dict[str, str],
    allowed_characters: set[str],
) -> set[str]:
    result: set[str] = set()
    for record_id, record in (authority.get("characters") or {}).items():
        if not isinstance(record, dict):
            continue
        character_id = str(record.get("character_id") or record_id).strip()
        if character_id not in allowed_characters:
            continue
        role_values = {
            str(record.get(field) or "").strip().lower()
            for field in ("role", "identity")
        }
        if not any(
            value == "protagonist"
            or value == "lead"
            or "主角" in value
            for value in role_values
        ):
            continue
        location = locations.get(character_id)
        if location:
            result.add(location)
    return result


def _authoritative_character_aliases(
    authority: dict[str, Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for record_id, record in (authority.get("characters") or {}).items():
        if not isinstance(record, dict):
            continue
        stable_id = str(record.get("character_id") or record_id).strip()
        if not stable_id:
            continue
        references = [
            record_id,
            record.get("character_id"),
            record.get("canonical_name"),
            *(record.get("aliases") or []),
        ]
        for reference in references:
            normalized = str(reference or "").strip()
            if normalized:
                result.setdefault(normalized, stable_id)
    return result


def _authoritative_character_ids(authority: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for record_id, record in (authority.get("characters") or {}).items():
        normalized_id = str(record_id).strip()
        if normalized_id:
            result.add(normalized_id)
        if isinstance(record, dict):
            character_id = str(record.get("character_id") or "").strip()
            if character_id:
                result.add(character_id)
    for record in (authority.get("relationships") or {}).values():
        if not isinstance(record, dict):
            continue
        for field in (
            "source_character_id",
            "target_character_id",
            "source_id",
            "target_id",
        ):
            character_id = str(record.get(field) or "").strip()
            if character_id:
                result.add(character_id)
    for record in (authority.get("roster") or {}).values():
        if not isinstance(record, dict):
            continue
        for character_id in record.get("character_ids") or []:
            normalized = str(character_id).strip()
            if normalized:
                result.add(normalized)
        for member in record.get("members") or []:
            if isinstance(member, str):
                normalized = member.strip()
            elif isinstance(member, dict):
                normalized = str(
                    member.get("character_id")
                    or member.get("member_id")
                    or ""
                ).strip()
            else:
                normalized = ""
            if normalized:
                result.add(normalized)
    return result


def _recovered_scene_result(
    recovered: dict[str, Any],
    *,
    scene: dict[str, Any],
) -> dict[str, Any]:
    if not _has_complete_structured_scene_payload(recovered):
        raise ValueError("recovered scene lacks a complete structured boundary")
    raw_events = recovered.get("events")
    assert isinstance(raw_events, list)
    events = [
        _normalize_scene_event(item)
        for item in raw_events
    ]
    raw_deltas = recovered.get("deltas")
    assert isinstance(raw_deltas, dict)
    return {
        "prose": validate_text_output(recovered.get("text"), CHAPTER_CONTRACT),
        "events": events,
        "deltas": {
            key: [
                dict(item)
                for item in raw_deltas[key]
            ]
            for key in (
                "characters",
                "relationships",
                "rosters",
                "locations",
                "inventory",
                "counters",
            )
        },
        "continuity_note": str(recovered.get("continuity_note") or "recovered scene"),
        **(
            {"source_attempt_id": str(recovered["source_attempt_id"])}
            if recovered.get("source_attempt_id")
            else {}
        ),
    }


def _has_complete_structured_scene_payload(value: dict[str, Any]) -> bool:
    raw_events = value.get("events")
    if not isinstance(raw_events, list) or any(
        not isinstance(item, dict) for item in raw_events
    ):
        return False
    raw_deltas = value.get("deltas")
    if not isinstance(raw_deltas, dict):
        return False
    for key in (
        "characters",
        "relationships",
        "rosters",
        "locations",
        "inventory",
        "counters",
    ):
        items = raw_deltas.get(key)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            return False
    return True


def _dry_run_scene_result(
    plan: dict[str, Any],
    scene: dict[str, Any],
    *,
    chapter_blueprint: dict[str, Any] | None,
) -> dict[str, Any]:
    scene_index = int(scene.get("index") or 1)
    if chapter_blueprint is not None:
        beat_indexes = _scene_beat_indexes(scene)
        beat_texts = _beat_texts_for_indexes(chapter_blueprint, beat_indexes)
        text_parts = [
            (
                f"StoryProject beat {beat_index}: {beat_text}"
                if scene_index == 1 and position == 0
                else f"StoryProject beat {beat_index}: {_dry_run_unique_requirement(beat_text)}"
            )
            for position, (beat_index, beat_text) in enumerate(beat_texts)
        ]
        if scene_index == _last_scene_index(plan):
            ending_pressure = str(chapter_blueprint.get("ending_pressure") or "").strip()
            if ending_pressure:
                text_parts.append(
                    f"Ending pressure: {_dry_run_unique_requirement(ending_pressure)}"
                )
        prose = " ".join(text_parts) or str(scene.get("goal") or f"Scene {scene_index}")
    else:
        sentences = [
            sentence.strip() + "."
            for sentence in _DRY_RUN_CHAPTER.split(".")
            if sentence.strip()
        ]
        prose = sentences[min(scene_index - 1, len(sentences) - 1)]
    return {
        "prose": validate_text_output(prose, CHAPTER_CONTRACT),
        "events": [
            dict(item)
            for item in scene.get("planned_events") or []
            if isinstance(item, dict)
        ],
        "deltas": _empty_scene_deltas(),
        "continuity_note": "deterministic dry-run continuation",
    }


def _dry_run_unique_requirement(value: str) -> str:
    text = str(value or "").strip()
    for separator in ("：", ":"):
        if separator in text:
            suffix = text.rsplit(separator, 1)[-1].strip()
            if suffix:
                return suffix
    return text


def _empty_scene_deltas() -> dict[str, list[dict[str, Any]]]:
    return {
        "characters": [],
        "relationships": [],
        "rosters": [],
        "locations": [],
        "inventory": [],
        "counters": [],
    }


def _normalize_scene_event(value: dict[str, Any]) -> dict[str, Any]:
    event_id = str(value.get("event_id") or "").strip()
    event_type = str(value.get("type") or "").strip()
    if not event_id or not event_type:
        raise ValueError("scene response event requires event_id and type")
    return {
        "event_id": event_id,
        "type": event_type,
        "subjects": [
            str(item) for item in value.get("subjects") or [] if str(item)
        ],
        "objects": [
            str(item) for item in value.get("objects") or [] if str(item)
        ],
        "location": str(value.get("location") or ""),
        "status": str(value.get("status") or "completed"),
    }


def _normalize_planned_events(
    value: Any,
    *,
    chapter_index: int,
    scene_index: int,
    scene_type: str,
    required_beats: list[Any],
    required_beat_indexes: list[int],
) -> list[dict[str, Any]]:
    raw = [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []
    target_count = max(1, len(required_beats))
    events: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for offset in range(target_count):
        source = raw[offset] if offset < len(raw) else {}
        beat_index = (
            required_beat_indexes[offset]
            if offset < len(required_beat_indexes)
            else None
        )
        default_id = (
            f"chapter-{chapter_index:04d}-beat-{beat_index:03d}"
            if beat_index is not None
            else f"chapter-{chapter_index:04d}-scene-{scene_index:03d}-event-{offset + 1:03d}"
        )
        event_id = str(source.get("event_id") or default_id).strip()
        if event_id in used_ids:
            event_id = default_id
        used_ids.add(event_id)
        events.append(
            {
                "event_id": event_id,
                "type": str(
                    source.get("type")
                    or (
                        f"required_beat_{beat_index}_completed"
                        if beat_index is not None
                        else f"{scene_type}_advance_{scene_index}_{offset + 1}"
                    )
                ),
                "subjects": [
                    str(item)
                    for item in source.get("subjects") or []
                    if str(item)
                ],
                "objects": [
                    str(item)
                    for item in source.get("objects") or []
                    if str(item)
                ],
                "location": str(source.get("location") or ""),
                "status": str(source.get("status") or "completed"),
            }
        )
    return events


def merge_scenes(scene_drafts: list[dict[str, Any]]) -> str:
    merged, _scene_spans = _merge_scene_texts(scene_drafts)
    return validate_text_output(merged, CHAPTER_CONTRACT)


def _validate_plan(plan: dict[str, Any], *, chapter_index: int = 1) -> dict[str, Any]:
    normalized = {
        "goal": str(plan.get("goal") or "Advance the chapter with clear conflict."),
        "scenes": [],
    }
    raw_scenes = plan.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raw_scenes = _dry_run_plan(1)["scenes"]
    prior_event_ids: list[str] = []
    for index, raw_scene in enumerate(raw_scenes, start=1):
        scene = raw_scene if isinstance(raw_scene, dict) else {}
        beats = scene.get("required_beats")
        if not isinstance(beats, list) or not beats:
            beats = [str(scene.get("goal") or "Move the scene forward.")]
        scene_type = str(scene.get("type") or "development")
        goal = str(scene.get("goal") or f"Scene {index}")
        story_project_beat_indexes = _scene_beat_indexes(scene)
        if index == 1 and not story_project_beat_indexes:
            scene_type = "opening_bridge"
            goal = "Continue directly from last_chapter_ending"
            beats = [
                "repeat last known location",
                "show immediate consequence",
                "explain transition before new scene",
            ]
        planned_events = _normalize_planned_events(
            scene.get("planned_events"),
            chapter_index=chapter_index,
            scene_index=index,
            scene_type=scene_type,
            required_beats=beats,
            required_beat_indexes=story_project_beat_indexes,
        )
        required_event_ids = [str(item["event_id"]) for item in planned_events]
        forbidden_event_ids = list(dict.fromkeys([
            *prior_event_ids,
            *[
                str(item)
                for item in scene.get("forbidden_event_ids") or []
                if str(item)
            ],
        ]))
        normalized["scenes"].append(
            {
                "index": int(scene.get("index") or index),
                "type": scene_type,
                "goal": goal,
                "required_beats": [str(beat) for beat in beats if str(beat).strip()],
                **({"required_beat_indexes": story_project_beat_indexes} if story_project_beat_indexes else {}),
                "planned_events": planned_events,
                "required_event_ids": required_event_ids,
                "forbidden_event_ids": forbidden_event_ids,
            }
        )
        prior_event_ids.extend(required_event_ids)
    pipeline = validate_schema(
        {
            "chapter_index": 1,
            "plan": normalized,
            "scene_drafts": [{"index": 1, "goal": "placeholder", "text": "placeholder"}],
            "merged_chapter": "placeholder",
            "scene_spans": [{"index": 1, "start_char": 0, "end_char": 11, "chars": 11}],
            "stages": _pipeline_stages(),
        },
        "chapter_pipeline.schema.json",
    )
    return pipeline["plan"]


def _limit_plan_scenes(plan: dict[str, Any], scene_limit: int | None) -> dict[str, Any]:
    if scene_limit is None:
        return plan
    limit = max(1, int(scene_limit))
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or len(scenes) <= limit:
        return plan
    limited = dict(plan)
    limited["scenes"] = scenes[:limit]
    return _validate_plan(limited)


def _validate_scene_drafts(scene_drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pipeline = validate_schema(
        {
            "chapter_index": 1,
        "plan": {
            "goal": "placeholder",
            "scenes": [
                {
                    "index": 1,
                    "type": "opening_bridge",
                    "goal": "placeholder",
                    "required_beats": ["placeholder"],
                }
            ],
        },
        "scene_drafts": scene_drafts,
            "merged_chapter": "placeholder",
            "scene_spans": [{"index": 1, "start_char": 0, "end_char": 11, "chars": 11}],
            "stages": _pipeline_stages(),
        },
        "chapter_pipeline.schema.json",
    )
    return pipeline["scene_drafts"]


def _dry_run_plan(chapter_index: int) -> dict[str, Any]:
    return {
        "goal": f"Advance chapter {chapter_index} through alarm, blocked route, and serum conflict.",
        "scenes": [
            {
                "index": 1,
                "type": "opening_bridge",
                "goal": "Continue directly from last_chapter_ending",
                "required_beats": [
                    "repeat last known location",
                    "show immediate consequence",
                    "explain transition before new scene",
                ],
            },
            {
                "index": 2,
                "type": "development",
                "goal": "Reveal the sealed gate and new infection zone.",
                "required_beats": ["sealed gate", "safe route cut off", "infection zone"],
            },
            {
                "index": 3,
                "type": "development",
                "goal": "Force the protagonist into a serum-centered choice.",
                "required_beats": ["rescue teammate", "protect serum sample", "open conflict"],
            },
        ],
    }


def _dry_run_scene_drafts(
    plan: dict[str, Any],
    *,
    chapter_blueprint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return generate_scenes(
        "",
        plan,
        dry_run=True,
        chapter_blueprint=chapter_blueprint,
    )


def _scene_beat_indexes(scene: dict[str, Any]) -> list[int]:
    raw = scene.get("required_beat_indexes")
    if not isinstance(raw, list):
        return []
    indexes: list[int] = []
    for value in raw:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1 and value not in indexes:
            indexes.append(value)
    return indexes


def _last_scene_index(plan: dict[str, Any]) -> int:
    indexes = [
        int(scene.get("index"))
        for scene in plan.get("scenes", [])
        if isinstance(scene, dict) and isinstance(scene.get("index"), int)
    ]
    return max(indexes) if indexes else 1


def _beat_texts_for_indexes(chapter_blueprint: dict[str, Any], indexes: list[int]) -> list[tuple[int, str]]:
    by_index: dict[int, str] = {}
    for beat in chapter_blueprint.get("required_beats") or []:
        if not isinstance(beat, dict):
            continue
        index = beat.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            by_index[index] = str(beat.get("text") or "")
    return [(index, by_index.get(index, "")) for index in indexes]


def _merge_scene_texts(scene_drafts: list[dict[str, Any]]) -> tuple[str, list[dict[str, int]]]:
    parts: list[str] = []
    spans: list[dict[str, int]] = []
    cursor = 0
    for scene in scene_drafts:
        text = str(scene.get("text") or "").strip()
        if not text:
            continue
        if parts:
            cursor += 2
        start = cursor
        end = start + len(text)
        spans.append(
            {
                "index": int(scene.get("index") or len(spans) + 1),
                "start_char": start,
                "end_char": end,
                "chars": len(text),
            }
        )
        parts.append(text)
        cursor = end
    return "\n\n".join(parts), spans


def _pipeline_stages(overrides: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    overrides = overrides or {}
    stages: list[dict[str, Any]] = []
    for name in PIPELINE_STAGE_NAMES:
        override = overrides.get(name, {})
        stage: dict[str, Any] = {
            "name": name,
            "status": str(override.get("status") or "pending"),
        }
        artifact_key = override.get("artifact_key")
        if artifact_key:
            stage["artifact_key"] = str(artifact_key)
        summary = override.get("summary")
        if isinstance(summary, dict):
            stage["summary"] = summary
        stages.append(stage)
    return stages


__all__ = [
    "PIPELINE_STAGE_NAMES",
    "generate_scenes",
    "merge_scenes",
    "plan_chapter",
    "plan_scenes",
    "run_chapter_pipeline",
]
