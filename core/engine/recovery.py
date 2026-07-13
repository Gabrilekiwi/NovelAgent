from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.engine.run_record import validate_run_result
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR


class RecoveryError(ValueError):
    pass


def recover_latest_chapter_draft(
    *,
    run_dir: str | Path = DEFAULT_RUN_DIR,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    expected_book_id: str | None = None,
) -> dict[str, Any]:
    result = _latest_recoverable_run(Path(run_dir), expected_book_id=expected_book_id)
    run = result["run"]
    draft_text = _recoverable_draft_text(run)
    if not draft_text.strip():
        raise RecoveryError(f"run {run.get('id')} does not contain recoverable chapter text")

    artifact = _write_recovered_chapter(draft_text, run, Path(chapter_dir))
    return {
        "ok": True,
        "source_run_id": run["id"],
        "source_status": run["status"],
        "chapter_index": run["chapter_index"],
        "artifact": artifact,
        "chars": len(draft_text),
    }


def _latest_recoverable_run(run_dir: Path, *, expected_book_id: str | None = None) -> dict[str, Any]:
    if not run_dir.exists():
        raise RecoveryError(f"run_dir does not exist: {run_dir}")
    candidates = sorted(run_dir.glob("chapter_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            result = validate_run_result(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
        run = result.get("run")
        if not isinstance(run, dict):
            continue
        if expected_book_id is not None:
            story = run.get("story_project") if isinstance(run.get("story_project"), dict) else {}
            if story.get("book_id") != expected_book_id:
                continue
        if run.get("status") == "committed":
            continue
        if _recoverable_draft_text(run).strip():
            return result
    raise RecoveryError(f"no recoverable failed or rejected run was found in {run_dir}")


def _recoverable_draft_text(run: dict[str, Any]) -> str:
    pipeline = ((run.get("chapter") or {}).get("pipeline") or {}) if isinstance(run.get("chapter"), dict) else {}
    artifacts = pipeline.get("artifacts") if isinstance(pipeline, dict) else None
    merged_artifact = artifacts.get("merged_chapter") if isinstance(artifacts, dict) else None
    path = merged_artifact.get("path") if isinstance(merged_artifact, dict) else None
    if path:
        draft = _markdown_body(Path(path))
        if draft.strip():
            return draft
    chapter_artifact = (run.get("chapter") or {}).get("artifact") if isinstance(run.get("chapter"), dict) else None
    artifact_path = chapter_artifact.get("path") if isinstance(chapter_artifact, dict) else None
    if artifact_path:
        return _markdown_body(Path(artifact_path))
    return ""


def _markdown_body(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    marker = "\n---\n\n"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text.strip()


def _write_recovered_chapter(chapter_text: str, run: dict[str, Any], chapter_dir: Path) -> dict[str, Any]:
    chapter_dir.mkdir(parents=True, exist_ok=True)
    chapter_index = int(run["chapter_index"])
    run_id = str(run["id"])
    path = chapter_dir / f"chapter_{chapter_index:04d}_recovered_{run_id}.md"
    content = (
        f"# Recovered Chapter {chapter_index}\n\n"
        f"- Source Run: `{run_id}`\n"
        f"- Source Status: `{run.get('status')}`\n"
        "- Snapshot Updated: `False`\n\n"
        "---\n\n"
        f"{chapter_text.strip()}\n"
    )
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "format": "markdown",
        "chars": len(chapter_text),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
