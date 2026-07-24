from __future__ import annotations

import json
import re
import warnings
from typing import Any

from api.contracts import CHAPTER_CONTRACT, validate_language_output, validate_text_output
from api.openai_client import chat_completion
from core.context_budget import default_context_budget
from core.prompt_compiler import compile_prompt_contexts
from core.quality.final_artifact_integrity import (
    FinalArtifactIntegrityGate,
    build_integrity_stage_record,
)
from core.scene_continuity import (
    empty_scene_state,
    require_scene_transition,
    scene_state_summary,
    validate_scene_transition,
)
from core.schema import validate_schema
from core.state.story_state_context import STORY_STATE_CONTEXT_KEYS, STORY_STATE_SECTION_MAX_CHARS
from core.structured_context import compact_markdown_context, select_text_blocks
from core.story_project.coverage import (
    blueprint_to_dict,
    build_blueprint_coverage,
    build_blueprint_plan,
    validate_generation_blueprint_contract,
)
from modules.chapter_generator.generator import _DRY_RUN_CHAPTER, _load_prompt


PIPELINE_STAGE_NAMES = (
    "plan_chapter",
    "generate_scenes",
    "merge_scenes",
    "validate",
    "repair",
    "commit",
)
_STORY_PROJECT_BLUEPRINT_SECTION_MAX_CHARS = 4_096


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
    prompt_contexts = compile_prompt_contexts(
        input_pack,
        budget=default_context_budget(enable_model_tokenizer=not dry_run),
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

    prompt = (
        "Create a compact chapter plan as JSON only. "
        "Schema: {\"goal\": string, \"scenes\": [{\"index\": int, \"type\": string, \"goal\": string, "
        "\"required_beats\": [string], \"planned_events\": [{\"event_id\": string, \"type\": string, "
        "\"subjects\": [string], \"objects\": [string], \"location\": string, \"status\": \"completed\"}]}]}. "
        "Give every beat a stable event_id scoped to exactly one scene. Keep it to 2-4 scenes. "
        "Scene 1 must be type opening_bridge and continue directly from the last chapter ending."
    )
    payload = _request_chapter_plan(input_pack, prompt)
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
            "Story State": STORY_STATE_CONTEXT_KEYS,
            "StoryProject Chapter Blueprint": {"chapter_blueprint", "read_set_context_digest"},
        },
        allowed_json_keys={"Story State": STORY_STATE_CONTEXT_KEYS},
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
    raw_deltas = value.get("deltas")
    if not isinstance(raw_deltas, dict):
        raise ValueError("Scene response deltas must be an object")
    return {
        "prose": prose,
        "events": [
            _normalize_scene_event(item)
            for item in raw_events
            if isinstance(item, dict)
        ],
        "deltas": {
            key: [
                dict(item)
                for item in raw_deltas.get(key) or []
                if isinstance(item, dict)
            ]
            for key in ("characters", "relationships", "locations", "inventory", "counters")
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
) -> list[dict[str, Any]]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    plan = _validate_plan(plan)
    recovered = _recovered_scene_prefix(recovered_scene_drafts, plan)
    scene_drafts: list[dict[str, Any]] = []
    scene_state = _initial_scene_state(input_pack)
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
            payload = chat_completion(
                [
                    {"role": "system", "content": _load_prompt()},
                    {
                        "role": "user",
                        "content": _scene_request_payload(
                            input_pack=input_pack,
                            plan=plan,
                            scene=scene,
                            scene_required_beats=scene_required_beats,
                            blueprint=blueprint,
                            previous_scene_tail=(
                                str(scene_drafts[-1]["text"])[-600:]
                                if scene_drafts
                                else ""
                            ),
                            prior_scene_summaries=prior_scene_summaries,
                            scene_state=state_before,
                        ),
                    },
                ],
                stage="chapter_generation",
            )
            scene_result = _load_scene_response(payload)
        boundary, state_after = validate_scene_transition(
            scene_index=scene_index,
            state_before=state_before,
            events=scene_result["events"],
            deltas=scene_result["deltas"],
            required_event_ids=scene.get("required_event_ids") or [],
            forbidden_event_ids=scene.get("forbidden_event_ids") or [],
            planned_events=scene.get("planned_events") or [],
        )
        scene_text = validate_language_output(
            scene_result["prose"],
            CHAPTER_CONTRACT,
            language=language,
        )
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
            "scene_state_before": state_before,
            "scene_state_after": scene_state_summary(state_after),
            "boundary_validation": boundary,
        }
        scene_drafts.append(draft)
        require_scene_transition(boundary)
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
) -> str:
    scene_count = max(1, len([item for item in plan.get("scenes", []) if isinstance(item, dict)]))
    target_min_chars = max(600, 3_000 // scene_count)
    target_max_chars = max(target_min_chars, 4_500 // scene_count)
    context_query = json.dumps(
        {
            "chapter_plan": plan,
            "scene": scene,
            "required_beats": scene_required_beats,
            "ending_pressure": (blueprint or {}).get("ending_pressure"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    compact_scene_context = _compact_scene_context(input_pack, query=context_query)
    payload = json.dumps(
        {
            "shared_context": compact_scene_context,
            "chapter_plan": plan,
            "scene": scene,
            "story_project_required_beats": scene_required_beats,
            "story_project_ending_pressure": (blueprint or {}).get("ending_pressure"),
            "previous_scene_tail": str(previous_scene_tail)[-600:],
            "prior_scene_summaries": list(prior_scene_summaries or [])[-8:],
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
                "deltas": {
                    "characters": [],
                    "relationships": [],
                    "locations": [],
                    "inventory": [],
                    "counters": [],
                },
                "continuity_note": "string",
            },
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
                "Deltas must declare before, change, after, and reason "
                "where applicable. Continue directly from previous_scene_tail and current_scene_state."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
    default_context_budget().require_input(
        payload,
        stage="scene",
        protocol_texts=(_load_prompt(),),
    )
    return payload


def _compact_scene_context(
    text: str,
    *,
    max_section_chars: int = 1_500,
    query: str = "",
) -> str:
    """Retrieve complete sections/JSON items relevant to the current scene."""
    selection = compact_markdown_context(
        text,
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
            "StoryProject Chapter Blueprint",
            "Requirements",
            "灏忚鐢熸垚瑙勫垯濂戠害",
        },
        excluded_sections={"Memory Index", "Structured Context Manifest"},
        required_json_keys={
            "Story State": STORY_STATE_CONTEXT_KEYS,
            "StoryProject Chapter Blueprint": {"chapter_blueprint", "read_set_context_digest"},
        },
        allowed_json_keys={"Story State": STORY_STATE_CONTEXT_KEYS},
        section_max_chars={
            "Story State": STORY_STATE_SECTION_MAX_CHARS,
            "StoryProject Chapter Blueprint": _STORY_PROJECT_BLUEPRINT_SECTION_MAX_CHARS,
        },
        prefer_recent=True,
        policy="scene_markdown_json_retrieval_v1",
    )
    return selection.text


def _initial_scene_state(input_pack: str) -> dict[str, Any]:
    state = empty_scene_state()
    match = re.search(
        r"(?ms)^# Story State[ \t]*\r?\n(.*?)(?=^# |\Z)",
        str(input_pack or ""),
    )
    if not match:
        return state
    body = match.group(1).strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return state
    try:
        story_state = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return state
    if not isinstance(story_state, dict):
        return state
    location = str(story_state.get("last_scene_location") or "").strip()
    characters = story_state.get("last_scene_characters")
    if location and isinstance(characters, list):
        for character in characters:
            character_id = str(character).strip()
            if character_id:
                state["locations"][character_id] = location
    for event_id in story_state.get("completed_event_ids") or []:
        normalized = str(event_id).strip()
        if normalized and normalized not in state["completed_event_ids"]:
            state["completed_event_ids"].append(normalized)
    return state


def _recovered_scene_result(
    recovered: dict[str, Any],
    *,
    scene: dict[str, Any],
) -> dict[str, Any]:
    raw_events = recovered.get("events")
    events = (
        [_normalize_scene_event(item) for item in raw_events if isinstance(item, dict)]
        if isinstance(raw_events, list)
        else [dict(item) for item in scene.get("planned_events") or [] if isinstance(item, dict)]
    )
    raw_deltas = recovered.get("deltas")
    return {
        "prose": validate_text_output(recovered.get("text"), CHAPTER_CONTRACT),
        "events": events,
        "deltas": (
            {
                key: [dict(item) for item in raw_deltas.get(key) or [] if isinstance(item, dict)]
                for key in ("characters", "relationships", "locations", "inventory", "counters")
            }
            if isinstance(raw_deltas, dict)
            else _empty_scene_deltas()
        ),
        "continuity_note": str(recovered.get("continuity_note") or "recovered scene"),
    }


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
            f"StoryProject beat {beat_index}: {beat_text}"
            for beat_index, beat_text in beat_texts
        ]
        if scene_index == _last_scene_index(plan):
            ending_pressure = str(chapter_blueprint.get("ending_pressure") or "").strip()
            if ending_pressure:
                text_parts.append(f"Ending pressure: {ending_pressure}")
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


def _empty_scene_deltas() -> dict[str, list[dict[str, Any]]]:
    return {
        "characters": [],
        "relationships": [],
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
