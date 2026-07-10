from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR


def save_loop_session_artifact(
    *,
    session: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "loop_sessions",
) -> dict[str, Any]:
    path = Path(output_dir) / f"{session['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    artifact = {
        "path": str(path),
        "format": "json",
    }
    payload = dict(session)
    payload["artifact"] = artifact
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


def save_chapter_artifact(
    *,
    chapter_text: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_CHAPTER_DIR,
) -> dict[str, Any]:
    chapter_index = int(run["chapter_index"])
    status = str(run["status"])
    run_id = str(run["id"])
    path = Path(output_dir) / f"chapter_{chapter_index:04d}_{status}_{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    content = _format_chapter_markdown(chapter_text, run)
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "chars": len(chapter_text),
        "format": "markdown",
    }


def save_input_pack_artifact(
    *,
    input_pack: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "input_packs",
) -> dict[str, Any]:
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    path = Path(output_dir) / f"input_pack_{chapter_index:04d}_{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(_format_input_pack_markdown(input_pack, run), encoding="utf-8")
    return {
        "path": str(path),
        "chars": len(input_pack),
        "format": "markdown",
    }


def save_snapshot_pack_artifact(
    *,
    snapshot_pack: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "snapshot_packs",
) -> dict[str, Any]:
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    path = Path(output_dir) / f"snapshot_pack_{chapter_index:04d}_{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(_format_snapshot_pack_markdown(snapshot_pack, run), encoding="utf-8")
    return {
        "path": str(path),
        "chars": len(snapshot_pack),
        "format": "markdown",
    }


def save_chapter_pipeline_artifacts(
    *,
    pipeline: dict[str, Any],
    validation: dict[str, Any] | None,
    repair_deltas: list[dict[str, Any]] | None,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "chapter_pipeline",
) -> dict[str, Any]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])

    plan_artifact = _write_artifact(
        path / f"chapter_plan_{chapter_index:04d}_{run_id}.json",
        json.dumps(pipeline.get("plan") or {}, ensure_ascii=False, indent=2),
        "json",
    )
    scene_artifacts = []
    scene_spans = {
        int(span.get("index")): span
        for span in pipeline.get("scene_spans", [])
        if isinstance(span, dict) and span.get("index") is not None
    }
    for scene in pipeline.get("scene_drafts", []):
        if not isinstance(scene, dict):
            continue
        index = int(scene.get("index") or len(scene_artifacts) + 1)
        if index in scene_spans:
            scene = {**scene, "span": scene_spans[index]}
        scene_artifacts.append(
            _write_artifact(
                path / f"scene_{chapter_index:04d}_{index:02d}_{run_id}.md",
                _format_scene_markdown(scene, run),
                "markdown",
            )
        )
    merged_artifact = _write_artifact(
        path / f"merged_chapter_{chapter_index:04d}_{run_id}.md",
        _format_merged_chapter_markdown(str(pipeline.get("merged_chapter") or ""), run),
        "markdown",
    )
    validation_artifact = _write_artifact(
        path / f"validation_report_{chapter_index:04d}_{run_id}.json",
        json.dumps(validation or {}, ensure_ascii=False, indent=2),
        "json",
    )
    repair_artifact = _write_artifact(
        path / f"repair_deltas_{chapter_index:04d}_{run_id}.json",
        json.dumps(repair_deltas or [], ensure_ascii=False, indent=2),
        "json",
    )
    return {
        "plan": plan_artifact,
        "scene_drafts": scene_artifacts,
        "merged_chapter": merged_artifact,
        "validation_report": validation_artifact,
        "repair_deltas": repair_artifact,
    }


def save_story_project_writeback_artifacts(
    *,
    plan: dict[str, Any],
    result: dict[str, Any],
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "story_project_writebacks",
) -> dict[str, Any]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    diff = result.get("diff_summary") if isinstance(result.get("diff_summary"), dict) else {}
    return {
        "plan": _write_artifact(
            path / f"writeback_plan_{chapter_index:04d}_{run_id}.json",
            json.dumps(plan, ensure_ascii=False, indent=2),
            "json",
        ),
        "diff": _write_artifact(
            path / f"writeback_diff_{chapter_index:04d}_{run_id}.json",
            json.dumps(diff, ensure_ascii=False, indent=2),
            "json",
        ),
        "result": _write_artifact(
            path / f"writeback_result_{chapter_index:04d}_{run_id}.json",
            json.dumps(result, ensure_ascii=False, indent=2),
            "json",
        ),
    }


def _write_artifact(path: Path, content: str, artifact_format: str) -> dict[str, Any]:
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "chars": len(content),
        "format": artifact_format,
    }


def _format_chapter_markdown(chapter_text: str, run: dict[str, Any]) -> str:
    return (
        f"# Chapter {run['chapter_index']}\n\n"
        f"- Run: `{run['id']}`\n"
        f"- Status: `{run['status']}`\n"
        f"- Committed: `{run['committed']}`\n"
        f"- Repair Attempts: `{run.get('repair_attempts', 0)}`\n\n"
        "---\n\n"
        f"{chapter_text.strip()}\n"
    )


def _format_input_pack_markdown(input_pack: str, run: dict[str, Any]) -> str:
    return (
        f"# Input Pack: Chapter {run['chapter_index']}\n\n"
        f"- Run: `{run['id']}`\n"
        f"- Status: `{run['status']}`\n"
        f"- Committed: `{run['committed']}`\n\n"
        "---\n\n"
        f"{input_pack.strip()}\n"
    )


def _format_snapshot_pack_markdown(snapshot_pack: str, run: dict[str, Any]) -> str:
    return (
        f"# Snapshot Input Pack: Chapter {run['chapter_index']}\n\n"
        f"- Run: `{run['id']}`\n"
        f"- Status: `{run['status']}`\n"
        f"- Committed: `{run['committed']}`\n\n"
        "---\n\n"
        f"{snapshot_pack.strip()}\n"
    )


def _format_scene_markdown(scene: dict[str, Any], run: dict[str, Any]) -> str:
    span = scene.get("span") if isinstance(scene.get("span"), dict) else {}
    span_line = ""
    if span:
        span_line = f"- Merged Span: `{span.get('start_char')}-{span.get('end_char')}`\n"
    return (
        f"# Scene {scene.get('index')}\n\n"
        f"- Run: `{run['id']}`\n"
        f"- Chapter: `{run['chapter_index']}`\n"
        f"- Goal: {scene.get('goal')}\n"
        f"{span_line}"
        "\n"
        "---\n\n"
        f"{str(scene.get('text') or '').strip()}\n"
    )


def _format_merged_chapter_markdown(chapter_text: str, run: dict[str, Any]) -> str:
    return (
        f"# Merged Chapter {run['chapter_index']}\n\n"
        f"- Run: `{run['id']}`\n\n"
        "---\n\n"
        f"{chapter_text.strip()}\n"
    )
