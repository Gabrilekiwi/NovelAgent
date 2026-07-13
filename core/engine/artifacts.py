from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.engine.persistence import atomic_write_text
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
    artifact = chapter_artifact_metadata(chapter_text=chapter_text, run=run, output_dir=output_dir)
    path = Path(artifact["path"])
    path.parent.mkdir(parents=True, exist_ok=True)

    content = _format_chapter_markdown(chapter_text, run)
    atomic_write_text(path, content)
    artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return artifact


def chapter_artifact_metadata(
    *,
    chapter_text: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_CHAPTER_DIR,
) -> dict[str, Any]:
    chapter_index = int(run["chapter_index"])
    status = str(run["status"])
    run_id = str(run["id"])
    path = Path(output_dir) / f"chapter_{chapter_index:04d}_{status}_{run_id}.md"
    content = _format_chapter_markdown(chapter_text, run)
    return {
        "path": str(path),
        "chars": len(chapter_text),
        "format": "markdown",
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
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


def save_review_repair_artifacts(
    *,
    review_repair: dict[str, Any],
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "review_repairs",
) -> dict[str, Any]:
    path = Path(output_dir) / str(run["id"])
    path.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, Any] = {}
    repair_plan = review_repair.get("repair_plan")
    if isinstance(repair_plan, dict):
        artifacts["repair_plan"] = _write_artifact(
            path / "review_repair_plan_attempt_01.json",
            json.dumps(repair_plan, ensure_ascii=False, indent=2),
            "json",
        )
    deltas = review_repair.get("repair_deltas") if isinstance(review_repair.get("repair_deltas"), list) else []
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        attempt = int(delta.get("attempt") or len(artifacts) + 1)
        artifacts[f"delta_attempt_{attempt:02d}"] = _write_artifact(
            path / f"review_repair_delta_attempt_{attempt:02d}.json",
            json.dumps(delta, ensure_ascii=False, indent=2),
            "json",
        )
    final_chapter = review_repair.get("final_chapter")
    if isinstance(final_chapter, str) and final_chapter.strip():
        artifacts["final_chapter"] = _write_artifact(
            path / "repaired_chapter_final.md",
            final_chapter.strip() + "\n",
            "markdown",
        )
    final_validation = review_repair.get("final_validation")
    if isinstance(final_validation, dict):
        artifacts["final_validation"] = _write_artifact(
            path / "post_repair_validation_final.json",
            json.dumps(final_validation, ensure_ascii=False, indent=2),
            "json",
        )
    final_review = review_repair.get("final_review")
    if isinstance(final_review, dict):
        artifacts["final_review"] = _write_artifact(
            path / "post_repair_review_final.json",
            json.dumps(final_review, ensure_ascii=False, indent=2),
            "json",
        )
    artifacts["result"] = _write_artifact(
        path / "review_repair_result.json",
        json.dumps(_review_repair_artifact_payload(review_repair), ensure_ascii=False, indent=2),
        "json",
    )
    return artifacts


def _review_repair_artifact_payload(review_repair: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in review_repair.items()
        if key not in {"final_chapter"}
    }


def _write_artifact(path: Path, content: str, artifact_format: str) -> dict[str, Any]:
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "chars": len(content),
        "format": artifact_format,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
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
