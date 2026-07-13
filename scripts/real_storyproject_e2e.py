from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.engine.executor import AgentExecutor  # noqa: E402
from core.engine.persistence import atomic_write_json  # noqa: E402
from core.memory_v2 import replay_memory_events  # noqa: E402
from core.runtime_paths import RuntimePaths, init_runtime_state  # noqa: E402
from core.schema import validate_schema  # noqa: E402
from core.story_project.activation import activate_story_state, load_story_state_calibration_report  # noqa: E402
from core.story_project.identity import load_project_identity  # noqa: E402
from core.story_project.runtime import build_generation_story_project_context_loader  # noqa: E402
from core.story_project.writer import StoryProjectWritebackConfig  # noqa: E402


MAX_PROVIDER_ATTEMPTS = 8


class RealStoryProjectE2EError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def run_real_storyproject_e2e(
    *,
    sample_path: str | Path,
    calibration_report_path: str | Path,
    start_chapter: str | int = "auto",
    output_path: str | Path | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    _require_opt_in(confirmed)
    _set_bounded_provider_defaults()
    config = get_config()
    _validate_provider_limits(config)
    if not config.openai_api_key:
        raise RealStoryProjectE2EError("openai_not_configured", "OPENAI_API_KEY is required")

    sample = Path(sample_path).resolve()
    if not sample.is_dir():
        raise RealStoryProjectE2EError("sample_unavailable", "the redacted StoryProject sample is not a directory")
    report = load_story_state_calibration_report(calibration_report_path)

    work_parent = ROOT / ".tmp" / "real_storyproject_e2e"
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run_", dir=work_parent) as temporary:
        book = Path(temporary) / "book"
        shutil.copytree(sample, book)
        runtime_dir = book / ".novelagent" / "runtime"
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        identity = load_project_identity(book)
        if identity is None:
            raise RealStoryProjectE2EError(
                "stable_identity_required",
                "the sample must contain .novelagent/project.json bound to the calibration report",
            )
        activated = activate_story_state(book, report)
        paths = RuntimePaths.for_story_project(book)
        init_runtime_state(
            snapshot_target=paths.snapshot_path,
            memory_target=paths.memory_dir / "notion_memory.json",
        )
        delegate = build_generation_story_project_context_loader(
            story_project=book,
            chapter=start_chapter,
            project_identity=activated,
        )
        contexts: list[dict[str, Any]] = []

        class RecordingLoader:
            story_project_root = delegate.story_project_root
            project_identity = activated

            def __call__(self, snapshot, memory_context, chapter_hint=None):
                context = delegate(snapshot, memory_context, chapter_hint)
                contexts.append(context.to_dict())
                return context

        result = AgentExecutor(
            snapshot_path=paths.snapshot_path,
            memory_path=paths.memory_dir / "notion_memory.json",
            run_dir=paths.run_dir,
            chapter_dir=paths.chapter_dir,
            persistence_dir=paths.persistence_dir,
            dry_run=False,
            polisher=lambda chapter: chapter,
            story_project_context_loader=RecordingLoader(),
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
            scene_limit=2,
        ).run_loop(steps=2, persist=True)

        validated = _build_redacted_report(
            result=result,
            contexts=contexts,
            paths=paths,
            calibration_report=report,
            model=config.openai_model,
            max_output_tokens=config.openai_max_output_tokens,
            request_timeout_seconds=config.openai_timeout_seconds,
            provider_max_attempts=config.provider_max_attempts,
        )

    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, validated)
    return validated


def _build_redacted_report(
    *,
    result: dict[str, Any],
    contexts: list[dict[str, Any]],
    paths: RuntimePaths,
    calibration_report: dict[str, Any],
    model: str,
    max_output_tokens: int,
    request_timeout_seconds: int,
    provider_max_attempts: int,
) -> dict[str, Any]:
    runs = result.get("runs") or []
    if not result.get("succeeded") or len(runs) != 2 or len(contexts) != 2:
        raise RealStoryProjectE2EError("two_chapter_run_failed", "both chapters must commit successfully")
    attempts = _openai_attempt_count(runs)
    if attempts < 1 or attempts > MAX_PROVIDER_ATTEMPTS:
        raise RealStoryProjectE2EError("provider_attempt_limit_exceeded", "provider attempt count is outside the bounded profile")
    second_previous = contexts[1].get("previous_chapter_context") or {}
    path_ref = second_previous.get("path_ref") or {}
    relative_path = str(path_ref.get("relative_path") or "")
    previous_path = (Path(contexts[1]["story_project_root"]) / relative_path).resolve()
    root = Path(contexts[1]["story_project_root"]).resolve()
    if root not in previous_path.parents or not previous_path.is_file():
        raise RealStoryProjectE2EError("previous_prose_unverified", "second chapter previous-prose reference is invalid")
    previous_hash = hashlib.sha256(previous_path.read_bytes()).hexdigest()
    second_read_first = previous_hash == second_previous.get("sha256")
    managed_read = any(
        item.get("source_kind") == "managed_projection"
        for item in (contexts[1].get("semantic_state") or {}).get("provenance") or []
        if isinstance(item, dict)
    )
    strict_both = all(
        context.get("story_state_mode") == "strict"
        and (context.get("semantic_audit") or {}).get("authoritative") is True
        for context in contexts
    )
    replay = replay_memory_events(paths.memory_dir / "v2" / "events")
    final_revision = int((runs[-1]["run"].get("memory") or {}).get("v2", {}).get("revision") or 0)
    memory_verified = replay["revision"] == final_revision and replay["committed_chapter_count"] == 2
    if not all((second_read_first, managed_read, strict_both, memory_verified)):
        raise RealStoryProjectE2EError("semantic_readback_unverified", "strict readback evidence is incomplete")

    payload = {
        "schema_version": "1.0",
        "kind": "real_storyproject_e2e",
        "redacted": True,
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": {"name": "openai", "model": model, "attempt_count": attempts},
        "limits": {
            "steps": 2,
            "scene_limit": 2,
            "max_output_tokens": max_output_tokens,
            "request_timeout_seconds": request_timeout_seconds,
            "provider_max_attempts": provider_max_attempts,
        },
        "calibration_report_sha256": calibration_report["report_sha256"],
        "chapters": [
            {
                "chapter_index": int(item["run"]["chapter_index"]),
                "status": str(item["run"]["status"]),
                "committed": bool(item["run"]["committed"]),
                "run_id_sha256": hashlib.sha256(str(item["run"]["id"]).encode("utf-8")).hexdigest(),
                "memory_revision": int((item["run"].get("memory") or {}).get("v2", {}).get("revision") or 0),
            }
            for item in runs
        ],
        "evidence": {
            "strict_authority_both_chapters": strict_both,
            "second_read_first_prose": second_read_first,
            "second_read_managed_projection": managed_read,
            "memory_hash_chain_verified": memory_verified,
            "committed_chapter_count": int(replay["committed_chapter_count"]),
        },
    }
    return validate_schema(payload, "real_storyproject_e2e_report.schema.json")


def _openai_attempt_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        int(attempt.get("attempts") or 0)
        for item in runs
        for event in item.get("run", {}).get("trace", [])
        for attempt in event.get("provider_attempts", [])
        if isinstance(attempt, dict) and attempt.get("profile") == "model_read_generation"
    )


def _require_opt_in(confirmed: bool) -> None:
    env_confirmed = os.getenv("NOVELAGENT_REAL_STORYPROJECT_E2E", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not confirmed and not env_confirmed:
        raise RealStoryProjectE2EError(
            "real_provider_opt_in_required",
            "set NOVELAGENT_REAL_STORYPROJECT_E2E=1 or pass --confirm-real-provider-calls",
        )


def _set_bounded_provider_defaults() -> None:
    os.environ.setdefault("OPENAI_TIMEOUT_SECONDS", "90")
    os.environ.setdefault("OPENAI_MAX_OUTPUT_TOKENS", "1200")
    os.environ.setdefault("PROVIDER_MAX_ATTEMPTS", "1")
    os.environ.setdefault("PROVIDER_RETRY_DEADLINE_SECONDS", "90")


def _validate_provider_limits(config) -> None:
    if not 1 <= config.openai_timeout_seconds <= 90:
        raise RealStoryProjectE2EError("unsafe_provider_limits", "OPENAI_TIMEOUT_SECONDS must be between 1 and 90")
    if not 1 <= config.openai_max_output_tokens <= 2000:
        raise RealStoryProjectE2EError("unsafe_provider_limits", "OPENAI_MAX_OUTPUT_TOKENS must be at most 2000")
    if not 1 <= config.provider_max_attempts <= 2:
        raise RealStoryProjectE2EError("unsafe_provider_limits", "PROVIDER_MAX_ATTEMPTS must be 1 or 2")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded, redacted two-chapter real OpenAI StoryProject E2E.")
    parser.add_argument("--sample", required=True, help="Path to a redacted real StoryProject sample.")
    parser.add_argument("--calibration-report", required=True, help="Qualified calibration report bound to the sample.")
    parser.add_argument("--start-chapter", default="auto", help="First chapter number, or auto.")
    parser.add_argument("--out", required=True, help="Path for the redacted JSON report.")
    parser.add_argument("--confirm-real-provider-calls", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_real_storyproject_e2e(
            sample_path=args.sample,
            calibration_report_path=args.calibration_report,
            start_chapter=args.start_chapter,
            output_path=args.out,
            confirmed=args.confirm_real_provider_calls,
        )
    except RealStoryProjectE2EError as exc:
        print(json.dumps({"ok": False, "error": exc.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
