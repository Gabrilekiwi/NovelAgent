from __future__ import annotations

import argparse
import json
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


def _run(command: list[str], *, echo_stdout: bool = True) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(_quote(part) for part in command))
    completed = subprocess.run(
        command,
        cwd=ROOT,
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
    for name in ("snapshot_pack", "input_pack", "chapter"):
        artifact = artifacts.get(name) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict) or not artifact.get("exists"):
            raise RuntimeError(f"latest run missing existing {name} artifact")
    return latest


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


def _quote(value: str) -> str:
    return f'"{value}"' if any(char.isspace() for char in value) else value


if __name__ == "__main__":
    main()
