from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MEMORY_EXAMPLE = ROOT / "data" / "notion_memory.example.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local NovelAgent v1.0 smoke gate.")
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip the unittest discovery step and only exercise the runtime CLI flow.",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Optional smoke artifact directory. Defaults to .tmp/smoke_v1/<timestamp>.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    work_dir = Path(args.work_dir) if args.work_dir else _default_work_dir()
    if not work_dir.is_absolute():
        work_dir = ROOT / work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = work_dir / "snapshot.json"
    run_dir = work_dir / "runs"
    chapter_dir = work_dir / "chapters"
    outbox_path = work_dir / "memory_outbox.jsonl"
    provider_smoke_dir = work_dir / "provider_smoke_missing_config"
    _write_smoke_snapshot(snapshot_path)

    commands: list[list[str]] = []
    if not args.skip_tests:
        commands.append([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])

    commands.extend(
        [
            [
                sys.executable,
                "-B",
                "main.py",
                "--check",
                "--dry-run",
                "--snapshot",
                str(snapshot_path),
                "--memory",
                str(MEMORY_EXAMPLE),
                "--run-dir",
                str(run_dir),
                "--chapter-dir",
                str(chapter_dir),
                "--memory-writeback",
                "file",
                "--memory-outbox",
                str(outbox_path),
            ],
            [
                sys.executable,
                "-B",
                "main.py",
                "--dry-run",
                "--persist-dry-run",
                "--snapshot",
                str(snapshot_path),
                "--memory",
                str(MEMORY_EXAMPLE),
                "--run-dir",
                str(run_dir),
                "--chapter-dir",
                str(chapter_dir),
                "--memory-writeback",
                "file",
                "--memory-outbox",
                str(outbox_path),
            ],
            [
                sys.executable,
                "-B",
                "main.py",
                "--report-runs",
                "--run-dir",
                str(run_dir),
                "--report-limit",
                "1",
            ],
        ]
    )

    report_stdout = ""
    for command in commands:
        completed = _run(command, echo_stdout="--report-runs" not in command)
        if "--report-runs" in command:
            report_stdout = completed.stdout

    provider_smoke = _run(
        [
            sys.executable,
            "-B",
            "scripts/provider_smoke.py",
            "--providers",
            "openai",
            "claude",
            "notion",
            "--allow-missing",
            "--ignore-dotenv",
            "--no-proxy",
            "--work-dir",
            str(provider_smoke_dir),
        ],
        echo_stdout=False,
        env_overrides=_missing_provider_env(),
    )
    provider_report = _parse_report(provider_smoke.stdout)
    _assert_provider_smoke_missing_config(provider_report, provider_smoke_dir)

    report = _parse_report(report_stdout)
    latest = _assert_report(report, run_dir)
    run_path = Path(str(latest["path"]))
    run_result = _load_json(run_path)
    _assert_run_result(run_result, chapter_dir=chapter_dir, outbox_path=outbox_path)

    print("Smoke v1: OK")
    print(f"- work_dir: {work_dir}")
    print(f"- run: {run_path}")
    print(f"- chapter_artifact: {run_result['run']['chapter']['artifact']['path']}")
    print(f"- memory_outbox: {outbox_path}")
    print(f"- provider_smoke_report: {provider_smoke_dir / 'provider_smoke_report.json'}")


def _default_work_dir() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return ROOT / ".tmp" / "smoke_v1" / timestamp


def _write_smoke_snapshot(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "chapter_index": 2,
                "world_state": {
                    "infection_level": "medium",
                    "locations": {},
                },
                "characters": {},
                "timeline": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _run(
    command: list[str],
    *,
    echo_stdout: bool = True,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(_quote(part) for part in command))
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if echo_stdout and completed.stdout.strip():
        print(completed.stdout.rstrip())
    if completed.stderr.strip():
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    return completed


def _missing_provider_env() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "",
        "ANTHROPIC_MODEL": "",
        "CLAUDE_MODEL": "",
        "NOTION_API_KEY": "",
        "NOTION_DATABASE_ID": "",
        "NOVELAGENT_NOTION_DATABASE_ID": "",
    }


def _parse_report(stdout: str) -> dict[str, Any]:
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("report output is not valid JSON") from exc
    if not isinstance(report, dict):
        raise RuntimeError("report output must be a JSON object")
    return report


def _assert_report(report: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    if report.get("run_dir") != str(run_dir):
        raise RuntimeError(f"report run_dir mismatch: {report.get('run_dir')!r}")
    if report.get("loaded") != 1:
        raise RuntimeError(f"expected exactly one loaded run, got {report.get('loaded')!r}")
    if report.get("skipped"):
        raise RuntimeError(f"report skipped run artifacts: {report['skipped']!r}")
    latest = report.get("latest")
    if not isinstance(latest, dict):
        raise RuntimeError("report latest run is missing")
    if latest.get("status") != "committed":
        raise RuntimeError(f"expected committed latest run, got {latest.get('status')!r}")
    if not latest.get("committed"):
        raise RuntimeError("latest run is not committed")
    artifacts = latest.get("artifacts") if isinstance(latest.get("artifacts"), dict) else {}
    for name in ("snapshot_pack", "input_pack", "chapter", "chapter_pipeline"):
        artifact = artifacts.get(name) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict) or not artifact.get("exists"):
            raise RuntimeError(f"latest run missing existing {name} artifact")
    return latest


def _assert_provider_smoke_missing_config(report: dict[str, Any], work_dir: Path) -> None:
    if report.get("work_dir") != str(work_dir):
        raise RuntimeError(f"provider smoke work_dir mismatch: {report.get('work_dir')!r}")
    if not report.get("ok"):
        raise RuntimeError("provider smoke missing-config report should be ok with --allow-missing")
    if report.get("required_checks_ok"):
        raise RuntimeError("provider smoke missing-config report unexpectedly passed required checks")
    if (report.get("diagnostics") or {}).get("status") != "incomplete":
        raise RuntimeError("provider smoke missing-config diagnostics should be incomplete")
    missing = set((report.get("diagnostics") or {}).get("missing_config") or [])
    expected = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "CLAUDE_MODEL",
        "NOTION_API_KEY",
        "NOTION_DATABASE_ID",
        "NOVELAGENT_NOTION_DATABASE_ID",
    }
    if missing != expected:
        raise RuntimeError(f"provider smoke missing-config mismatch: {sorted(missing)!r}")
    config_status = report.get("config_status")
    if not isinstance(config_status, dict):
        raise RuntimeError("provider smoke report missing config_status")
    if (config_status.get("openai") or {}).get("api_key") != "missing" or (config_status.get("openai") or {}).get("configured"):
        raise RuntimeError("provider smoke OpenAI config_status should be missing")
    if (
        (config_status.get("claude") or {}).get("api_key") != "missing"
        or (config_status.get("claude") or {}).get("model_status") != "missing"
        or (config_status.get("claude") or {}).get("configured")
    ):
        raise RuntimeError("provider smoke Claude config_status should be missing")
    if (
        (config_status.get("notion") or {}).get("api_key") != "missing"
        or (config_status.get("notion") or {}).get("database_id") != "missing"
        or (config_status.get("notion") or {}).get("configured")
    ):
        raise RuntimeError("provider smoke Notion config_status should be missing")
    required_checks = report.get("required_checks")
    if not isinstance(required_checks, list) or len(required_checks) != 6:
        raise RuntimeError("provider smoke required check summary is incomplete")
    if any(item.get("status") != "skipped" for item in required_checks if isinstance(item, dict)):
        raise RuntimeError("provider smoke missing-config checks should all be skipped")
    report_path = Path(str(report.get("report_path")))
    if not report_path.exists() or report_path.parent != work_dir:
        raise RuntimeError(f"provider smoke report was not written under work dir: {report_path}")


def _assert_run_result(run_result: dict[str, Any], *, chapter_dir: Path, outbox_path: Path) -> None:
    run = run_result.get("run")
    if not isinstance(run, dict):
        raise RuntimeError("run artifact is missing run object")
    if run.get("status") != "committed" or not run.get("committed"):
        raise RuntimeError("run artifact is not committed")
    chapter_artifact = (run.get("chapter") or {}).get("artifact")
    if not isinstance(chapter_artifact, dict):
        raise RuntimeError("run artifact is missing chapter artifact metadata")
    chapter_path = Path(str(chapter_artifact.get("path")))
    if not chapter_path.exists() or chapter_path.parent != chapter_dir:
        raise RuntimeError(f"chapter artifact is missing or outside expected directory: {chapter_path}")
    pipeline = (run.get("chapter") or {}).get("pipeline")
    if not isinstance(pipeline, dict):
        raise RuntimeError("run artifact is missing chapter pipeline metadata")
    if int(pipeline.get("scene_count") or 0) < 1:
        raise RuntimeError("chapter pipeline did not record scene drafts")
    scene_spans = pipeline.get("scene_spans")
    if not isinstance(scene_spans, list) or len(scene_spans) != int(pipeline.get("scene_count") or 0):
        raise RuntimeError("chapter pipeline scene span count does not match scene count")
    pipeline_artifacts = pipeline.get("artifacts")
    if not isinstance(pipeline_artifacts, dict):
        raise RuntimeError("chapter pipeline is missing artifacts")
    for name in ("plan", "merged_chapter", "validation_report", "repair_deltas"):
        artifact = pipeline_artifacts.get(name)
        if not isinstance(artifact, dict) or not Path(str(artifact.get("path"))).exists():
            raise RuntimeError(f"chapter pipeline missing {name} artifact")
    merged_text = _artifact_body(Path(str(pipeline_artifacts["merged_chapter"]["path"])))
    scene_artifacts = pipeline_artifacts.get("scene_drafts")
    if not isinstance(scene_artifacts, list) or not scene_artifacts:
        raise RuntimeError("chapter pipeline missing scene draft artifacts")
    for index, artifact in enumerate(scene_artifacts):
        if not isinstance(artifact, dict) or not Path(str(artifact.get("path"))).exists():
            raise RuntimeError("chapter pipeline scene draft artifact is missing")
        scene_text = Path(str(artifact.get("path"))).read_text(encoding="utf-8")
        span = scene_spans[index] if index < len(scene_spans) else None
        if not isinstance(span, dict):
            raise RuntimeError("chapter pipeline scene span is missing")
        expected_span = f"Merged Span: `{span.get('start_char')}-{span.get('end_char')}`"
        if expected_span not in scene_text:
            raise RuntimeError(f"scene draft artifact missing span metadata: {expected_span}")
        if int(span.get("end_char") or 0) <= int(span.get("start_char") or 0):
            raise RuntimeError(f"scene span is invalid: {span!r}")
        scene_body = _artifact_body(Path(str(artifact.get("path"))))
        start = int(span["start_char"])
        end = int(span["end_char"])
        if merged_text[start:end] != scene_body:
            raise RuntimeError("scene span does not match merged chapter text")
    writeback = (run.get("memory") or {}).get("writeback")
    if not isinstance(writeback, dict):
        raise RuntimeError("run artifact is missing memory writeback result")
    if writeback.get("target") != "file":
        raise RuntimeError(f"expected file memory writeback, got {writeback.get('target')!r}")
    if int(writeback.get("written") or 0) < 1:
        raise RuntimeError("expected at least one memory writeback item")
    if not outbox_path.exists() or not outbox_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"memory outbox was not written: {outbox_path}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _artifact_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    marker = "---\n\n"
    if marker not in text:
        raise RuntimeError(f"{path} missing artifact body marker")
    return text.split(marker, 1)[1].strip()


def _quote(value: str) -> str:
    return f'"{value}"' if any(char.isspace() for char in value) else value


if __name__ == "__main__":
    main()
