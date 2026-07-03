from __future__ import annotations

import json
import re
from typing import Any

from api.contracts import CHAPTER_CONTRACT, validate_language_output, validate_text_output
from api.openai_client import chat_completion
from core.schema import validate_schema
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
) -> dict[str, Any]:
    plan = plan_chapter(input_pack, chapter_index=chapter_index, dry_run=dry_run)
    plan = _limit_plan_scenes(plan, scene_limit)
    scenes = generate_scenes(input_pack, plan, dry_run=dry_run, language=language)
    merged, scene_spans = _merge_scene_texts(scenes)
    merged = validate_language_output(merged, CHAPTER_CONTRACT, language=language)
    return validate_schema(
        {
            "chapter_index": int(chapter_index),
            "plan": plan,
            "scene_drafts": scenes,
            "merged_chapter": merged,
            "scene_spans": scene_spans,
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
                }
            ),
        },
        "chapter_pipeline.schema.json",
    )


def plan_chapter(input_pack: str, *, chapter_index: int, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return _dry_run_plan(chapter_index)

    prompt = (
        "Create a compact chapter plan as JSON only. "
        "Schema: {\"goal\": string, \"scenes\": [{\"index\": int, \"type\": string, \"goal\": string, "
        "\"required_beats\": [string]}]}. Keep it to 2-4 scenes. "
        "Scene 1 must be type opening_bridge and continue directly from the last chapter ending."
    )
    payload = chat_completion(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": input_pack},
        ],
        temperature=0.2,
        stage="chapter_generation",
    )
    try:
        plan = _load_plan_json(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Chapter plan response was not valid JSON") from exc
    if not isinstance(plan, dict):
        raise ValueError("Chapter plan response must be a JSON object")
    return _validate_plan(plan)


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
) -> list[dict[str, Any]]:
    if dry_run:
        return _dry_run_scene_drafts(plan)

    scene_drafts: list[dict[str, Any]] = []
    for scene in plan.get("scenes", []):
        scene_text = chat_completion(
            [
                {"role": "system", "content": _load_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "input_pack": input_pack,
                            "chapter_plan": plan,
                            "scene": scene,
                            "instruction": "Draft only this scene as continuous prose. No heading.",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],
            stage="chapter_generation",
        )
        scene_drafts.append(
            {
                "index": int(scene["index"]),
                "goal": str(scene["goal"]),
                "text": validate_language_output(scene_text, CHAPTER_CONTRACT, language=language),
            }
        )
    return _validate_scene_drafts(scene_drafts)


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
        if index == 1:
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
                "scenes": [{"index": 1, "type": "opening_bridge", "goal": "placeholder", "required_beats": ["placeholder"]}],
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


def _dry_run_scene_drafts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    sentences = [sentence.strip() + "." for sentence in _DRY_RUN_CHAPTER.split(".") if sentence.strip()]
    scenes = plan.get("scenes", [])
    drafts: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes, start=1):
        text = sentences[index - 1] if index - 1 < len(sentences) else sentences[-1]
        drafts.append(
            {
                "index": int(scene.get("index") or index),
                "goal": str(scene.get("goal") or f"Scene {index}"),
                "text": validate_text_output(text, CHAPTER_CONTRACT),
            }
        )
    return _validate_scene_drafts(drafts)


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
