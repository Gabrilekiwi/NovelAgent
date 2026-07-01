from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def save_loop_session_artifact(
    *,
    session: dict[str, Any],
    output_dir: str | Path = "data/runs/loop_sessions",
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
    output_dir: str | Path = "data/chapters",
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
    output_dir: str | Path = "data/runs/input_packs",
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
    output_dir: str | Path = "data/runs/snapshot_packs",
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
