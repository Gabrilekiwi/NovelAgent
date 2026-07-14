from __future__ import annotations

import json
import re
from typing import Any

from api.contracts import CHAPTER_CONTRACT, validate_language_output, validate_text_output
from api.openai_client import chat_completion
from core.context_budget import default_context_budget
from core.prompt_compiler import compile_prompt_contexts
from core.schema import validate_schema
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


def run_chapter_pipeline(
    input_pack: str,
    *,
    chapter_index: int,
    dry_run: bool = False,
    scene_limit: int | None = None,
    language: str | None = None,
    chapter_blueprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    prompt_contexts = compile_prompt_contexts(input_pack)
    plan_input = prompt_contexts.plan.text
    scene_input = prompt_contexts.scene.text
    if blueprint is None:
        plan = plan_chapter(plan_input, chapter_index=chapter_index, dry_run=dry_run)
        plan = _limit_plan_scenes(plan, scene_limit)
    else:
        validate_generation_blueprint_contract(blueprint)
        plan = plan_chapter(
            plan_input,
            chapter_index=chapter_index,
            dry_run=dry_run,
            chapter_blueprint=blueprint,
            scene_limit=scene_limit,
        )
    scenes = generate_scenes(scene_input, plan, dry_run=dry_run, language=language, chapter_blueprint=blueprint)
    merged, scene_spans = _merge_scene_texts(scenes)
    merged = validate_language_output(merged, CHAPTER_CONTRACT, language=language)
    blueprint_coverage = build_blueprint_coverage(blueprint, scenes, merged) if blueprint is not None else None
    return validate_schema(
        {
            "chapter_index": int(chapter_index),
            "story_project": {"enabled": True} if blueprint is not None else None,
            "chapter_blueprint": blueprint,
            "plan": plan,
            "scene_drafts": scenes,
            "merged_chapter": merged,
            "scene_spans": scene_spans,
            "blueprint_coverage": blueprint_coverage,
            "context_budget": {
                "context_digest": prompt_contexts.context_digest,
                "plan": prompt_contexts.plan.report,
                "scene": prompt_contexts.scene.report,
                "repair": prompt_contexts.repair.report,
                "plan_sections": list(prompt_contexts.plan.selected_sections),
                "scene_sections": list(prompt_contexts.scene.selected_sections),
                "repair_sections": list(prompt_contexts.repair.selected_sections),
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


def plan_chapter(
    input_pack: str,
    *,
    chapter_index: int,
    dry_run: bool = False,
    chapter_blueprint: dict[str, Any] | None = None,
    scene_limit: int | None = None,
) -> dict[str, Any]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    if blueprint is not None:
        return _validate_plan(build_blueprint_plan(blueprint, scene_limit=scene_limit))

    if dry_run:
        return _dry_run_plan(chapter_index)

    prompt = (
        "Create a compact chapter plan as JSON only. "
        "Schema: {\"goal\": string, \"scenes\": [{\"index\": int, \"type\": string, \"goal\": string, "
        "\"required_beats\": [string]}]}. Keep it to 2-4 scenes. "
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
    return _validate_plan(plan)


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
    default_context_budget().require_input(
        input_pack[:6000] + invalid_payload,
        stage="plan_json_repair",
    )
    return chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Repair the chapter plan response into JSON only. "
                    "Return exactly one object with shape "
                    "{\"goal\": string, \"scenes\": [{\"index\": int, \"type\": string, "
                    "\"goal\": string, \"required_beats\": [string]}]}. "
                    "No prose, no Markdown, no explanation."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "chapter_index": chapter_index,
                        "json_error": str(error),
                        "invalid_response": invalid_payload,
                        "input_pack_excerpt": input_pack[:6000],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
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


def generate_scenes(
    input_pack: str,
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
    language: str | None = None,
    chapter_blueprint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    blueprint = blueprint_to_dict(chapter_blueprint)
    if dry_run:
        return _dry_run_scene_drafts(plan, chapter_blueprint=blueprint)

    scene_drafts: list[dict[str, Any]] = []
    for scene in plan.get("scenes", []):
        required_beat_indexes = _scene_beat_indexes(scene)
        scene_required_beats = [
            beat
            for beat in (blueprint or {}).get("required_beats", [])
            if isinstance(beat, dict) and int(beat.get("index") or 0) in required_beat_indexes
        ]
        scene_text = chat_completion(
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
                    ),
                },
            ],
            stage="chapter_generation",
        )
        scene_drafts.append(
            {
                "index": int(scene["index"]),
                "goal": str(scene["goal"]),
                **({"covered_beat_indexes": required_beat_indexes} if required_beat_indexes else {}),
                **(
                    {"ending_pressure_covered": True}
                    if blueprint is not None and int(scene["index"]) == _last_scene_index(plan)
                    else {}
                ),
                "text": validate_language_output(scene_text, CHAPTER_CONTRACT, language=language),
            }
        )
    return _validate_scene_drafts(scene_drafts)


def _scene_request_payload(
    *,
    input_pack: str,
    plan: dict[str, Any],
    scene: dict[str, Any],
    scene_required_beats: list[dict[str, Any]],
    blueprint: dict[str, Any] | None,
) -> str:
    scene_count = max(1, len([item for item in plan.get("scenes", []) if isinstance(item, dict)]))
    target_min_chars = max(600, 3_000 // scene_count)
    target_max_chars = max(target_min_chars, 4_500 // scene_count)
    compact_scene_context = _compact_scene_context(input_pack)
    payload = json.dumps(
        {
            "shared_context": compact_scene_context,
            "chapter_plan": plan,
            "scene": scene,
            "story_project_required_beats": scene_required_beats,
            "story_project_ending_pressure": (blueprint or {}).get("ending_pressure"),
            "instruction": (
                "Draft only this scene as continuous prose. No heading. "
                "If story_project_required_beats are provided, cover each listed beat in the prose and preserve "
                "its essential factual phrases closely enough for deterministic coverage checks. "
                f"Target {target_min_chars}-{target_max_chars} Chinese characters for this scene when the project "
                "language is zh-CN, so the merged chapter remains 3000-4500 Chinese characters; treat the upper "
                "bound as a hard limit and stop the scene before exceeding it. "
                "Do not restart, duplicate, or retell an event already completed earlier in the scene."
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


def _compact_scene_context(text: str, *, max_section_chars: int = 1_500) -> str:
    """Bound cumulative StoryProject writeback while preserving every current context section."""
    if len(text) <= max_section_chars * 7:
        return text
    matches = list(re.finditer(r"(?m)^# ([^\r\n]+)\r?$", text))
    if not matches:
        return _head_tail_context(text, max_section_chars * 7)
    compact: list[str] = []
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        if name == "Memory Index":
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        compact.append(_head_tail_context(text[match.start():end].rstrip(), max_section_chars))
    return "\n\n".join(compact)


def _head_tail_context(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(1, int(limit * 0.7))
    tail = max(1, limit - head)
    return f"{text[:head].rstrip()}\n\n[...scene context excerpted...]\n\n{text[-tail:].lstrip()}"


def merge_scenes(scene_drafts: list[dict[str, Any]]) -> str:
    merged, _scene_spans = _merge_scene_texts(scene_drafts)
    return validate_text_output(merged, CHAPTER_CONTRACT)


def _validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "goal": str(plan.get("goal") or "Advance the chapter with clear conflict."),
        "scenes": [],
    }
    raw_scenes = plan.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raw_scenes = _dry_run_plan(1)["scenes"]
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
        normalized["scenes"].append(
            {
                "index": int(scene.get("index") or index),
                "type": scene_type,
                "goal": goal,
                "required_beats": [str(beat) for beat in beats if str(beat).strip()],
                **({"required_beat_indexes": story_project_beat_indexes} if story_project_beat_indexes else {}),
            }
        )
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
    blueprint = blueprint_to_dict(chapter_blueprint)
    sentences = [sentence.strip() + "." for sentence in _DRY_RUN_CHAPTER.split(".") if sentence.strip()]
    scenes = plan.get("scenes", [])
    drafts: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes, start=1):
        beat_indexes = _scene_beat_indexes(scene)
        if blueprint is not None:
            beat_texts = _beat_texts_for_indexes(blueprint, beat_indexes)
            text_parts = [f"StoryProject beat {beat_index}: {beat_text}" for beat_index, beat_text in beat_texts]
            if int(scene.get("index") or index) == _last_scene_index(plan):
                ending_pressure = str(blueprint.get("ending_pressure") or "").strip()
                if ending_pressure:
                    text_parts.append(f"Ending pressure: {ending_pressure}")
            text = " ".join(text_parts) or str(scene.get("goal") or f"Scene {index}")
        else:
            text = sentences[index - 1] if index - 1 < len(sentences) else sentences[-1]
        drafts.append(
            {
                "index": int(scene.get("index") or index),
                "goal": str(scene.get("goal") or f"Scene {index}"),
                **({"covered_beat_indexes": beat_indexes} if beat_indexes else {}),
                **(
                    {"ending_pressure_covered": True}
                    if blueprint is not None and int(scene.get("index") or index) == _last_scene_index(plan)
                    else {}
                ),
                "text": validate_text_output(text, CHAPTER_CONTRACT),
            }
        )
    return _validate_scene_drafts(drafts)


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


__all__ = ["PIPELINE_STAGE_NAMES", "generate_scenes", "merge_scenes", "plan_chapter", "run_chapter_pipeline"]
