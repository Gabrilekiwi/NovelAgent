from __future__ import annotations

import hashlib
import json
import os
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
    prepared = prepare_chapter_artifact(chapter_text=chapter_text, run=run, output_dir=output_dir)
    _write_prepared_targets(prepared["targets"])
    return prepared["metadata"]


def prepare_chapter_artifact(
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
    metadata, target = _prepare_artifact(
        path,
        content,
        "markdown",
        metadata_chars=len(chapter_text),
    )
    return {"metadata": metadata, "targets": [target]}


def chapter_artifact_metadata(
    *,
    chapter_text: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_CHAPTER_DIR,
) -> dict[str, Any]:
    return prepare_chapter_artifact(
        chapter_text=chapter_text,
        run=run,
        output_dir=output_dir,
    )["metadata"]


def save_input_pack_artifact(
    *,
    input_pack: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "input_packs",
) -> dict[str, Any]:
    prepared = prepare_input_pack_artifact(input_pack=input_pack, run=run, output_dir=output_dir)
    _write_prepared_targets(prepared["targets"])
    return prepared["metadata"]


def prepare_input_pack_artifact(
    *,
    input_pack: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "input_packs",
) -> dict[str, Any]:
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    path = Path(output_dir) / f"input_pack_{chapter_index:04d}_{run_id}.md"
    content = _format_input_pack_markdown(input_pack, run)
    metadata, target = _prepare_artifact(
        path,
        content,
        "markdown",
        metadata_chars=len(input_pack),
        include_sha256=False,
        native_newlines=True,
    )
    return {"metadata": metadata, "targets": [target]}


def save_snapshot_pack_artifact(
    *,
    snapshot_pack: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "snapshot_packs",
) -> dict[str, Any]:
    prepared = prepare_snapshot_pack_artifact(snapshot_pack=snapshot_pack, run=run, output_dir=output_dir)
    _write_prepared_targets(prepared["targets"])
    return prepared["metadata"]


def prepare_snapshot_pack_artifact(
    *,
    snapshot_pack: str,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "snapshot_packs",
) -> dict[str, Any]:
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    path = Path(output_dir) / f"snapshot_pack_{chapter_index:04d}_{run_id}.md"
    content = _format_snapshot_pack_markdown(snapshot_pack, run)
    metadata, target = _prepare_artifact(
        path,
        content,
        "markdown",
        metadata_chars=len(snapshot_pack),
        include_sha256=False,
        native_newlines=True,
    )
    return {"metadata": metadata, "targets": [target]}


def save_chapter_pipeline_artifacts(
    *,
    pipeline: dict[str, Any],
    validation: dict[str, Any] | None,
    repair_deltas: list[dict[str, Any]] | None,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "chapter_pipeline",
) -> dict[str, Any]:
    prepared = prepare_chapter_pipeline_artifacts(
        pipeline=pipeline,
        validation=validation,
        repair_deltas=repair_deltas,
        run=run,
        output_dir=output_dir,
    )
    _write_prepared_targets(prepared["targets"])
    return prepared["metadata"]


def prepare_chapter_pipeline_artifacts(
    *,
    pipeline: dict[str, Any],
    validation: dict[str, Any] | None,
    repair_deltas: list[dict[str, Any]] | None,
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "chapter_pipeline",
) -> dict[str, Any]:
    path = Path(output_dir)
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    targets: list[dict[str, str]] = []

    plan_artifact = _append_prepared_artifact(
        targets,
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
            _append_prepared_artifact(
                targets,
                path / f"scene_{chapter_index:04d}_{index:02d}_{run_id}.md",
                _format_scene_markdown(scene, run),
                "markdown",
            )
        )
    merged_artifact = _append_prepared_artifact(
        targets,
        path / f"merged_chapter_{chapter_index:04d}_{run_id}.md",
        _format_merged_chapter_markdown(str(pipeline.get("merged_chapter") or ""), run),
        "markdown",
    )
    validation_artifact = _append_prepared_artifact(
        targets,
        path / f"validation_report_{chapter_index:04d}_{run_id}.json",
        json.dumps(validation or {}, ensure_ascii=False, indent=2),
        "json",
    )
    repair_artifact = _append_prepared_artifact(
        targets,
        path / f"repair_deltas_{chapter_index:04d}_{run_id}.json",
        json.dumps(repair_deltas or [], ensure_ascii=False, indent=2),
        "json",
    )
    return {
        "metadata": {
            "plan": plan_artifact,
            "scene_drafts": scene_artifacts,
            "merged_chapter": merged_artifact,
            "validation_report": validation_artifact,
            "repair_deltas": repair_artifact,
        },
        "targets": targets,
    }


def save_story_project_writeback_artifacts(
    *,
    plan: dict[str, Any],
    result: dict[str, Any],
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "story_project_writebacks",
) -> dict[str, Any]:
    prepared = prepare_story_project_writeback_artifacts(
        plan=plan,
        result=result,
        run=run,
        output_dir=output_dir,
    )
    _write_prepared_targets(prepared["targets"])
    return prepared["metadata"]


def prepare_story_project_writeback_artifacts(
    *,
    plan: dict[str, Any],
    result: dict[str, Any],
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "story_project_writebacks",
) -> dict[str, Any]:
    path = Path(output_dir)
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    diff = result.get("diff_summary") if isinstance(result.get("diff_summary"), dict) else {}
    targets: list[dict[str, str]] = []
    return {
        "metadata": {
            "plan": _append_prepared_artifact(
                targets,
                path / f"writeback_plan_{chapter_index:04d}_{run_id}.json",
                json.dumps(plan, ensure_ascii=False, indent=2),
                "json",
            ),
            "diff": _append_prepared_artifact(
                targets,
                path / f"writeback_diff_{chapter_index:04d}_{run_id}.json",
                json.dumps(diff, ensure_ascii=False, indent=2),
                "json",
            ),
            "result": _append_prepared_artifact(
                targets,
                path / f"writeback_result_{chapter_index:04d}_{run_id}.json",
                json.dumps(result, ensure_ascii=False, indent=2),
                "json",
            ),
        },
        "targets": targets,
    }


def save_review_repair_artifacts(
    *,
    review_repair: dict[str, Any],
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "review_repairs",
) -> dict[str, Any]:
    prepared = prepare_review_repair_artifacts(
        review_repair=review_repair,
        run=run,
        output_dir=output_dir,
    )
    _write_prepared_targets(prepared["targets"])
    return prepared["metadata"]


def prepare_review_repair_artifacts(
    *,
    review_repair: dict[str, Any],
    run: dict[str, Any],
    output_dir: str | Path = DEFAULT_RUN_DIR / "review_repairs",
) -> dict[str, Any]:
    path = Path(output_dir) / str(run["id"])
    artifacts: dict[str, Any] = {}
    targets: list[dict[str, str]] = []
    repair_plan = review_repair.get("repair_plan")
    if isinstance(repair_plan, dict):
        artifacts["repair_plan"] = _append_prepared_artifact(
            targets,
            path / "review_repair_plan_attempt_01.json",
            json.dumps(repair_plan, ensure_ascii=False, indent=2),
            "json",
        )
    deltas = review_repair.get("repair_deltas") if isinstance(review_repair.get("repair_deltas"), list) else []
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        attempt = int(delta.get("attempt") or len(artifacts) + 1)
        artifacts[f"delta_attempt_{attempt:02d}"] = _append_prepared_artifact(
            targets,
            path / f"review_repair_delta_attempt_{attempt:02d}.json",
            json.dumps(delta, ensure_ascii=False, indent=2),
            "json",
        )
    final_chapter = review_repair.get("final_chapter")
    if isinstance(final_chapter, str) and final_chapter.strip():
        artifacts["final_chapter"] = _append_prepared_artifact(
            targets,
            path / "repaired_chapter_final.md",
            final_chapter.strip() + "\n",
            "markdown",
        )
    final_validation = review_repair.get("final_validation")
    if isinstance(final_validation, dict):
        artifacts["final_validation"] = _append_prepared_artifact(
            targets,
            path / "post_repair_validation_final.json",
            json.dumps(final_validation, ensure_ascii=False, indent=2),
            "json",
        )
    final_review = review_repair.get("final_review")
    if isinstance(final_review, dict):
        artifacts["final_review"] = _append_prepared_artifact(
            targets,
            path / "post_repair_review_final.json",
            json.dumps(final_review, ensure_ascii=False, indent=2),
            "json",
        )
    artifacts["result"] = _append_prepared_artifact(
        targets,
        path / "review_repair_result.json",
        json.dumps(_review_repair_artifact_payload(review_repair), ensure_ascii=False, indent=2),
        "json",
    )
    return {"metadata": artifacts, "targets": targets}


def _review_repair_artifact_payload(review_repair: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in review_repair.items()
        if key not in {"final_chapter"}
    }


def _prepare_artifact(
    path: Path,
    content: str,
    artifact_format: str,
    *,
    metadata_chars: int | None = None,
    include_sha256: bool = True,
    native_newlines: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    target_content = content.replace("\n", os.linesep) if native_newlines and os.linesep != "\n" else content
    metadata: dict[str, Any] = {
        "path": str(path),
        "chars": len(content) if metadata_chars is None else metadata_chars,
        "format": artifact_format,
    }
    if include_sha256:
        metadata["sha256"] = hashlib.sha256(target_content.encode("utf-8")).hexdigest()
    return metadata, {"path": str(path), "content": target_content}


def _append_prepared_artifact(
    targets: list[dict[str, str]],
    path: Path,
    content: str,
    artifact_format: str,
) -> dict[str, Any]:
    metadata, target = _prepare_artifact(path, content, artifact_format, native_newlines=True)
    targets.append(target)
    return metadata


def _write_prepared_targets(targets: list[dict[str, str]]) -> None:
    for target in targets:
        atomic_write_text(target["path"], target["content"])


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
