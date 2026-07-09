from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.config import clear_proxy_env, proxy_disabled_by_env
from core.director import ModelDirector
from core.engine.executor import AgentExecutor, LoopExecutionError
from core.engine.preflight import run_preflight
from core.engine.recovery import RecoveryError, recover_latest_chapter_draft
from core.engine.report import build_run_report
from core.runtime_paths import (
    DEFAULT_CHAPTER_DIR,
    DEFAULT_RUN_DIR,
    DEFAULT_SNAPSHOT_PATH,
    init_runtime_state,
)
from core.review.runtime import RuntimeReviewConfig, validate_runtime_review_config
from core.state.memory_writer import build_memory_writer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NovelAgent v1.0 agent loop")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run with deterministic local output instead of API calls.",
    )
    parser.add_argument(
        "--persist-dry-run",
        action="store_true",
        help="Persist snapshot and run records even when --dry-run is enabled.",
    )
    parser.add_argument(
        "--snapshot",
        default=str(DEFAULT_SNAPSHOT_PATH),
        help="Snapshot file path.",
    )
    parser.add_argument(
        "--memory",
        default=None,
        help="Memory context file path. Defaults to NOVELAGENT_MEMORY_PATH or .tmp/runtime/notion_memory.json.",
    )
    parser.add_argument(
        "--memory-source",
        choices=["auto", "file", "notion"],
        default="auto",
        help="Select memory input mode. auto uses Notion API when configured and no --memory path is provided.",
    )
    parser.add_argument(
        "--notion-memory",
        action="store_true",
        help="Shortcut for --memory-source notion.",
    )
    parser.add_argument(
        "--chapter-dir",
        default=str(DEFAULT_CHAPTER_DIR),
        help="Directory for persisted chapter markdown artifacts.",
    )
    parser.add_argument(
        "--run-dir",
        default=str(DEFAULT_RUN_DIR),
        help="Directory for persisted run JSON records.",
    )
    parser.add_argument(
        "--init-runtime",
        action="store_true",
        help="Initialize .tmp/runtime from committed example state and exit.",
    )
    parser.add_argument(
        "--force-init-runtime",
        action="store_true",
        help="With --init-runtime, overwrite existing .tmp/runtime snapshot and memory files.",
    )
    parser.add_argument(
        "--memory-outbox",
        default=None,
        help="Append committed memory updates to this JSONL file.",
    )
    parser.add_argument(
        "--memory-writeback",
        choices=["none", "file", "notion"],
        default="none",
        help="Persist committed memory updates to no target, a JSONL file, or Notion.",
    )
    parser.add_argument(
        "--notion-readback",
        action="store_true",
        help="After Notion memory writeback, query the database and verify written Memory IDs.",
    )
    parser.add_argument(
        "--notion-sync",
        action="store_true",
        help="Shortcut for live Notion memory input plus Notion writeback and readback.",
    )
    parser.add_argument(
        "--director-model",
        default=None,
        help="Use an OpenAI-backed Director with this model name instead of the offline rule Director.",
    )
    parser.add_argument(
        "--llm-validator",
        action="store_true",
        help="Run the optional OpenAI-backed story-level validator after rule validation.",
    )
    parser.add_argument(
        "--enable-review-pipeline",
        action="store_true",
        help="After chapter generation, run the optional deterministic Review Pipeline.",
    )
    parser.add_argument(
        "--review-output-dir",
        default=".tmp/runtime/reviews",
        help="Root directory for optional runtime review artifacts.",
    )
    parser.add_argument(
        "--review-rules",
        default=None,
        help="Optional custom Narrative Rule Pack JSON path for runtime review.",
    )
    parser.add_argument(
        "--review-no-default-rules",
        action="store_true",
        help="Disable default review rules. Requires --review-rules.",
    )
    parser.add_argument(
        "--review-no-repair-prompt",
        action="store_true",
        help="Run runtime review without generating a repair prompt artifact.",
    )
    parser.add_argument(
        "--review-no-human-report",
        action="store_true",
        help="Run runtime review without generating a human-readable review report.",
    )
    parser.add_argument(
        "--review-gate",
        choices=["off", "blocked", "needs_revision", "warning"],
        default="off",
        help="Optional review status threshold that can make the CLI exit non-zero.",
    )
    parser.add_argument(
        "--scene-limit",
        type=_positive_int,
        default=None,
        help="Limit generated chapter scenes for bounded provider runs. Defaults to the full model plan.",
    )
    parser.add_argument(
        "--steps",
        type=_positive_int,
        default=1,
        help="Number of agent loop steps to run.",
    )
    parser.add_argument(
        "--continue-on-rejection",
        action="store_true",
        help="Continue loop after a rejected run so the next step can use recovery context.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run preflight checks and exit without generating a chapter.",
    )
    parser.add_argument(
        "--check-json",
        action="store_true",
        help="With --check, print the full preflight JSON instead of the concise summary.",
    )
    parser.add_argument(
        "--check-memory-v2",
        action="store_true",
        help="With --check, validate the Memory V2 compile chain in dry-run mode.",
    )
    parser.add_argument(
        "--memory-v2-out",
        default="data/memory_v2/default",
        help="Output directory path used for --check-memory-v2 dry-run diagnostics.",
    )
    parser.add_argument(
        "--report-runs",
        action="store_true",
        help="Print a JSON report for persisted run records and exit.",
    )
    parser.add_argument(
        "--recover-latest",
        action="store_true",
        help="Recover the latest failed or rejected pre-polish chapter draft into chapter-dir and exit.",
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        help="After generation, print the full result JSON instead of the concise summary.",
    )
    parser.add_argument(
        "--output-run-json",
        action="store_true",
        help="After generation, print only the full run record JSON instead of the concise summary.",
    )
    parser.add_argument(
        "--report-limit",
        type=int,
        default=5,
        help="Maximum number of recent runs to include in --report-runs output. Use 0 for counts only.",
    )
    parser.add_argument(
        "--require-claude",
        action="store_true",
        help="During --check, require Claude polish environment variables.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Clear HTTP(S)/ALL proxy environment variables before provider calls.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable multi-step progress lines printed to stderr.",
    )
    return parser.parse_args()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def apply_notion_shortcuts(args: argparse.Namespace) -> argparse.Namespace:
    if getattr(args, "notion_memory", False) or getattr(args, "notion_sync", False):
        args.memory_source = "notion"
    if getattr(args, "notion_sync", False):
        args.memory_writeback = "notion"
        args.notion_readback = True
    return args


def _loop_progress_observer(args: argparse.Namespace):
    if args.no_progress or args.output_json or args.output_run_json or args.steps <= 1:
        return None

    def observe(event: dict) -> None:
        line = format_loop_progress_event(event)
        if line:
            print(line, file=sys.stderr, flush=True)

    return observe


def format_loop_progress_event(event: dict) -> str:
    name = event.get("event")
    if name == "loop_start":
        return f"Loop progress: starting {event.get('requested_steps')} steps"
    if name == "step_start":
        return f"Loop progress: step {event.get('step')}/{event.get('requested_steps')} started"
    if name == "step_end":
        status = event.get("status")
        run_id = event.get("run_id")
        duration = event.get("duration_ms")
        committed = str(bool(event.get("committed"))).lower()
        return (
            f"Loop progress: step {event.get('step')}/{event.get('requested_steps')} "
            f"{status} committed={committed} duration_ms={duration} run={run_id}"
        )
    if name == "step_failed":
        return (
            f"Loop progress: step {event.get('step')}/{event.get('requested_steps')} failed "
            f"duration_ms={event.get('duration_ms')} error={event.get('error_type')}: {event.get('message')}"
        )
    if name == "loop_end":
        return (
            f"Loop progress: finished {event.get('completed_steps')}/{event.get('requested_steps')} "
            f"reason={event.get('stopped_reason')}"
        )
    return ""


def main() -> None:
    args = parse_args()
    apply_notion_shortcuts(args)
    if args.no_proxy or proxy_disabled_by_env():
        clear_proxy_env()
    try:
        review_config = _runtime_review_config_from_args(args)
    except ValueError as exc:
        print(f"Review pipeline configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.init_runtime:
        result = init_runtime_state(overwrite=args.force_init_runtime)
        print(format_init_runtime_summary(result))
        raise SystemExit(0)

    if args.report_runs:
        report = build_run_report(run_dir=args.run_dir, limit=args.report_limit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    if args.recover_latest:
        try:
            result = recover_latest_chapter_draft(run_dir=args.run_dir, chapter_dir=args.chapter_dir)
        except RecoveryError as exc:
            payload = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
            if args.output_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(format_recovery_summary(payload))
            raise SystemExit(1) from exc
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_recovery_summary(result))
        raise SystemExit(0)

    if args.check:
        result = run_preflight(
            snapshot_path=args.snapshot,
            memory_path=args.memory,
            memory_source=args.memory_source,
            run_dir=args.run_dir,
            chapter_dir=args.chapter_dir,
            dry_run=args.dry_run,
            require_claude=args.require_claude,
            director_model=args.director_model,
            enable_llm_validator=args.llm_validator,
            memory_writeback=args.memory_writeback,
            memory_outbox=args.memory_outbox,
            notion_readback=args.notion_readback,
            persist=not args.dry_run or args.persist_dry_run,
            steps=args.steps,
            continue_on_rejection=args.continue_on_rejection,
            check_memory_v2=args.check_memory_v2,
            memory_v2_output_dir=args.memory_v2_out,
        )
        if args.check_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_preflight_summary(result))
        raise SystemExit(0 if result["ok"] else 1)

    executor = AgentExecutor(
        snapshot_path=args.snapshot,
        memory_path=args.memory,
        memory_source=args.memory_source,
        run_dir=args.run_dir,
        chapter_dir=args.chapter_dir,
        dry_run=args.dry_run,
        scene_limit=args.scene_limit,
        director=ModelDirector(model=args.director_model) if args.director_model else None,
        enable_llm_validator=args.llm_validator,
        memory_writer=build_memory_writer(
            mode=args.memory_writeback,
            outbox_path=args.memory_outbox,
            notion_readback=args.notion_readback,
        ),
        review_config=review_config,
    )
    persist = not args.dry_run or args.persist_dry_run
    if args.steps == 1:
        result = executor.run_once(persist=persist)
    else:
        try:
            loop_result = executor.run_loop(
                steps=args.steps,
                persist=persist,
                stop_on_rejection=not args.continue_on_rejection,
                observer=_loop_progress_observer(args),
            )
        except LoopExecutionError as exc:
            payload = {
                "session": exc.session,
                "runs": exc.runs,
                "error": {
                    "type": type(exc.original).__name__,
                    "message": str(exc.original),
                },
            }
            if args.output_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.output_run_json and exc.runs:
                print(json.dumps(exc.runs[-1]["run"], ensure_ascii=False, indent=2))
            else:
                print(format_loop_failure_summary(payload))
            raise SystemExit(1) from exc
        if args.output_json:
            print(json.dumps(loop_result, ensure_ascii=False, indent=2))
            gate_exit_code = _review_gate_exit_code(loop_result.get("last_result"))
            if gate_exit_code:
                raise SystemExit(gate_exit_code)
            return
        result = loop_result["last_result"]
        result["loop"] = {
            "session_id": loop_result["session"]["id"],
            "completed_steps": loop_result["completed_steps"],
            "stopped_reason": loop_result["stopped_reason"],
            "run_ids": [item["run"]["id"] for item in loop_result["runs"]],
            "committed": [item["committed"] for item in loop_result["runs"]],
        }
        if loop_result["session"].get("artifact"):
            result["loop"]["artifact"] = loop_result["session"]["artifact"]

    if args.output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.output_run_json:
        print(json.dumps(result["run"], ensure_ascii=False, indent=2))
    else:
        print(format_run_summary(result))
    gate_exit_code = _review_gate_exit_code(result)
    if gate_exit_code:
        raise SystemExit(gate_exit_code)


def format_preflight_summary(result: dict) -> str:
    checks = result.get("checks", [])
    failed = [check for check in checks if not check.get("ok")]
    passed_count = len(checks) - len(failed)
    lines = [
        f"Preflight: {'OK' if result.get('ok') else 'FAILED'}",
        f"Checks: {passed_count} passed, {len(failed)} failed",
    ]

    for name, label in (
        ("runtime_state_summary", "Runtime state"),
        ("execution_mode", "Execution"),
        ("memory_input", "Memory input"),
        ("memory", "Memory"),
        ("memory_v2_compile", "Memory V2 compile"),
        ("state_builder_audit", "State builder"),
        ("run_history", "Run history"),
        ("planned_workflow", "Workflow"),
        ("v1_structure", "V1 structure"),
        ("schema_assets", "Schemas"),
        ("schema_consistency", "Schema consistency"),
        ("prompt_assets", "Prompts"),
        ("memory_writeback", "Memory writeback"),
        ("artifact_targets", "Artifacts"),
        ("llm_validator", "LLM validator"),
        ("director", "Director"),
    ):
        check = _check_by_name(checks, name)
        if check is not None and check.get("ok"):
            lines.append(f"{label}: {_summarize_check(name, check.get('details'))}")

    if failed:
        lines.append("Failures:")
        for check in failed:
            message = check.get("error") or "failed"
            lines.append(f"- {check.get('name')}: {message}")
        lines.append("Use --check-json for full diagnostics.")

    return "\n".join(lines)


def format_init_runtime_summary(result: dict) -> str:
    lines = [
        "Runtime initialized:",
        f"- runtime_dir: {result.get('runtime_dir')}",
        f"- snapshot: {result.get('snapshot_path')}",
        f"- memory: {result.get('memory_path')}",
        f"- run_dir: {result.get('run_dir')}",
        f"- chapter_dir: {result.get('chapter_dir')}",
    ]
    copied = result.get("copied") if isinstance(result.get("copied"), list) else []
    skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    if copied:
        lines.append(f"- copied: {', '.join(str(item.get('name')) for item in copied if isinstance(item, dict))}")
    if skipped:
        lines.append(f"- skipped_existing: {', '.join(str(item.get('name')) for item in skipped if isinstance(item, dict))}")
    return "\n".join(lines)


def format_recovery_summary(result: dict) -> str:
    if not result.get("ok"):
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        return f"Recovery failed: {error.get('type')}: {error.get('message')}"
    artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else {}
    return "\n".join(
        [
            "Recovered chapter draft:",
            f"- source_run: {result.get('source_run_id')} ({result.get('source_status')})",
            f"- chapter_index: {result.get('chapter_index')}",
            f"- chars: {result.get('chars')}",
            f"- artifact: {artifact.get('path')}",
            "- snapshot_updated: False",
        ]
    )


def format_run_summary(result: dict) -> str:
    run = result.get("run") or {}
    validation = result.get("validation") or {}
    analysis = result.get("analysis") or {}
    loop = result.get("loop")
    lines = [
        "Chapter:",
        str(result.get("chapter") or "").strip(),
        "",
        "Run:",
        f"- id: {run.get('id')}",
        f"- status: {run.get('status')}",
        f"- committed: {run.get('committed')}",
        f"- chapter_index: {run.get('chapter_index')}",
        f"- workflow: {' -> '.join(str(action) for action in run.get('workflow', []))}",
        f"- repair_attempts: {run.get('repair_attempts', 0)}",
    ]
    if isinstance(loop, dict):
        lines.extend(
            [
                f"- loop_completed_steps: {loop.get('completed_steps')}",
                f"- loop_stopped_reason: {loop.get('stopped_reason')}",
            ]
        )
        if loop.get("session_id"):
            lines.append(f"- loop_session: {loop.get('session_id')}")
        if isinstance(loop.get("artifact"), dict) and loop["artifact"].get("path"):
            lines.append(f"- loop_artifact: {loop['artifact']['path']}")

    polish_failure = _recoverable_polish_failure(run)
    if polish_failure:
        lines.extend(
            [
                "",
                "Polish:",
                "- status: failed",
                "- result: using unpolished generated chapter",
                "- diagnostics: recorded in run trace",
            ]
        )

    error = run.get("error") if isinstance(run.get("error"), dict) else None
    if error:
        lines.extend(
            [
                "",
                "Error:",
                f"- type: {error.get('type')}",
                f"- message: {error.get('message')}",
            ]
        )

    model_calls = _run_model_calls(run)
    if model_calls:
        lines.extend(["", "Model Calls:"])
        for label, model_call in model_calls:
            lines.append(f"- {label}: {_format_model_call(model_call)}")

    artifacts = _run_artifacts(run)
    if artifacts:
        lines.append("- artifacts:")
        for label, path in artifacts:
            lines.append(f"  - {label}: {path}")

    review = run.get("review_pipeline") if isinstance(run.get("review_pipeline"), dict) else None
    if review and review.get("enabled"):
        lines.extend(
            [
                "",
                "Review pipeline:",
                f"- status: {review.get('status')}",
                f"- decision: {review.get('decision')}",
                f"- quality_score: {review.get('quality_score')}",
                f"- rule_score: {review.get('rule_score')}",
                f"- repair_tasks: {review.get('repair_task_count')} total, {review.get('blocking_task_count')} blocking",
            ]
        )
        if review.get("artifacts_dir"):
            lines.append(f"- artifacts: {review.get('artifacts_dir')}")
        if review.get("summary_path"):
            lines.append(f"- summary: {review.get('summary_path')}")
        if review.get("error"):
            lines.append(f"- error: {review.get('error')}")

    gate = run.get("review_gate") if isinstance(run.get("review_gate"), dict) else None
    if gate and gate.get("enabled"):
        lines.extend(
            [
                "",
                "Review gate:",
                f"- threshold: {gate.get('threshold')}",
                f"- status: {gate.get('status')}",
                f"- matched: {gate.get('matched')}",
                f"- exit_code: {gate.get('exit_code')}",
                f"- reason: {gate.get('reason')}",
            ]
        )

    lines.extend(
        [
            "",
            "Validation:",
            f"- ok: {validation.get('ok')}",
            f"- problems: {len(validation.get('problems') or [])}",
        ]
    )
    lines.extend(_validation_coverage_summary(run.get("validation"), validation))
    problem_codes = (run.get("validation") or {}).get("problem_codes")
    if problem_codes:
        lines.append(f"- problem_codes: {', '.join(str(code) for code in problem_codes)}")

    if analysis:
        lines.extend(
            [
                "",
                "Analysis:",
                f"- summary: {analysis.get('summary', '')}",
                f"- events: {len(analysis.get('events') or [])}",
                f"- conflicts: {len(analysis.get('conflicts') or [])}",
                f"- world_changes: {len(analysis.get('world_changes') or [])}",
            ]
        )

    lines.append("")
    lines.append("Use --output-json for the full result or --output-run-json for the run record.")
    return "\n".join(lines)


def format_loop_failure_summary(payload: dict) -> str:
    session = payload.get("session") or {}
    error = payload.get("error") or session.get("error") or {}
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    last_run = runs[-1].get("run") if runs and isinstance(runs[-1], dict) else None
    lines = [
        "Loop failed:",
        f"- session: {session.get('id')}",
        f"- completed_steps: {session.get('completed_steps')}",
        f"- stopped_reason: {session.get('stopped_reason')}",
        f"- committed: {session.get('committed_count')}",
        f"- rejected: {session.get('rejected_count')}",
        f"- failed: {session.get('failed_count')}",
        f"- error: {error.get('type')}: {error.get('message')}",
    ]
    artifact = session.get("artifact") if isinstance(session, dict) else None
    if isinstance(artifact, dict) and artifact.get("path"):
        lines.append(f"- loop_artifact: {artifact['path']}")
    if isinstance(last_run, dict):
        lines.append(f"- last_run: {last_run.get('id')} ({last_run.get('status')})")
        run_error = last_run.get("error") if isinstance(last_run.get("error"), dict) else None
        if run_error:
            lines.append(f"- last_run_error: {run_error.get('type')}: {run_error.get('message')}")
        for label, model_call in _run_model_calls(last_run):
            lines.append(f"- model_call_{label}: {_format_model_call(model_call)}")
    return "\n".join(lines)


def _run_artifacts(run: dict) -> list[tuple[str, str]]:
    artifacts: list[tuple[str, str]] = []
    for label, section in (
        ("chapter", run.get("chapter")),
        ("input_pack", run.get("input_pack")),
        ("snapshot_pack", run.get("snapshot_builder")),
    ):
        if isinstance(section, dict):
            artifact = section.get("artifact")
            if isinstance(artifact, dict) and artifact.get("path"):
                artifacts.append((label, str(artifact["path"])))
    chapter = run.get("chapter")
    pipeline = chapter.get("pipeline") if isinstance(chapter, dict) else None
    pipeline_artifacts = pipeline.get("artifacts") if isinstance(pipeline, dict) else None
    if isinstance(pipeline_artifacts, dict):
        for label in ("plan", "merged_chapter", "validation_report", "repair_deltas"):
            artifact = pipeline_artifacts.get(label)
            if isinstance(artifact, dict) and artifact.get("path"):
                artifacts.append((f"pipeline_{label}", str(artifact["path"])))
    return artifacts


def _runtime_review_config_from_args(args: argparse.Namespace) -> RuntimeReviewConfig:
    config = RuntimeReviewConfig(
        enabled=bool(args.enable_review_pipeline),
        output_dir=Path(args.review_output_dir) if args.review_output_dir else None,
        rules_path=Path(args.review_rules) if args.review_rules else None,
        use_default_rules=not bool(args.review_no_default_rules),
        build_repair_prompt=not bool(args.review_no_repair_prompt),
        build_human_report=not bool(args.review_no_human_report),
        gate_threshold=str(args.review_gate),
    )
    return validate_runtime_review_config(config)


def _review_gate_exit_code(result: dict | None) -> int:
    if not isinstance(result, dict):
        return 0
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    gate = run.get("review_gate") if isinstance(run.get("review_gate"), dict) else result.get("review_gate")
    if not isinstance(gate, dict):
        return 0
    return 1 if gate.get("exit_code") == 1 else 0


def _run_model_calls(run: dict) -> list[tuple[str, dict]]:
    model_calls: list[tuple[str, dict]] = []
    director = run.get("director") if isinstance(run.get("director"), dict) else {}
    director_call = director.get("model_call") if isinstance(director, dict) else None
    if isinstance(director_call, dict):
        model_calls.append(("director", director_call))

    trace = run.get("trace")
    if isinstance(trace, list):
        for event in trace:
            if not isinstance(event, dict):
                continue
            if _is_recoverable_polish_failure_event(event):
                continue
            model_call = event.get("model_call")
            if isinstance(model_call, dict):
                label = str(event.get("action") or "workflow")
                model_calls.append((label, model_call))
    return model_calls


def _recoverable_polish_failure(run: dict) -> dict | None:
    trace = run.get("trace")
    if not isinstance(trace, list):
        return None
    for event in trace:
        if isinstance(event, dict) and _is_recoverable_polish_failure_event(event):
            return event
    return None


def _is_recoverable_polish_failure_event(event: dict) -> bool:
    return (
        event.get("action") == "polish"
        and event.get("status") == "failed"
        and event.get("plan_failure_policy") == "continue_unpolished"
    )


def _format_model_call(model_call: dict) -> str:
    parts = [
        str(model_call.get("provider") or "unknown"),
        str(model_call.get("stage") or "unknown"),
    ]
    if model_call.get("model"):
        parts.append(f"model={model_call.get('model')}")
    if model_call.get("cause_type"):
        parts.append(f"cause={model_call.get('cause_type')}")
    if model_call.get("failure_category"):
        parts.append(f"category={model_call.get('failure_category')}")
    if model_call.get("retryable") is not None:
        parts.append(f"retryable={str(bool(model_call.get('retryable'))).lower()}")
    if model_call.get("attempts"):
        parts.append(f"attempts={model_call.get('attempts')}")
    if model_call.get("elapsed_ms") is not None:
        parts.append(f"elapsed_ms={model_call.get('elapsed_ms')}")
    if model_call.get("message"):
        parts.append(f"message={model_call.get('message')}")
    return " ".join(parts)


def _validation_coverage_summary(run_validation, full_validation) -> list[str]:
    compact = run_validation if isinstance(run_validation, dict) else {}
    full = full_validation if isinstance(full_validation, dict) else {}
    requested = _list_value(compact.get("requested_focus") or full.get("requested_focus"))
    executed = _list_value(compact.get("executed_checks") or full.get("executed_checks"))
    skipped = _list_value(compact.get("skipped_checks") or full.get("skipped_checks"))
    lines: list[str] = []
    if requested:
        lines.append(f"- requested_focus: {', '.join(requested)}")
    if executed:
        lines.append(f"- executed_checks: {', '.join(executed)}")
    if skipped:
        lines.append(f"- skipped_checks: {', '.join(skipped)}")
    return lines


def _list_value(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _check_by_name(checks: list[dict], name: str) -> dict | None:
    for check in checks:
        if check.get("name") == name:
            return check
    return None


def _summarize_check(name: str, details) -> str:
    if name == "runtime_state_summary" and isinstance(details, dict):
        return (
            f"chapter={details.get('chapter_index')}, "
            f"memory_items={details.get('memory_items')}, "
            f"constraints={details.get('constraints')}"
        )
    if name == "planned_workflow":
        if isinstance(details, list):
            return " -> ".join(str(action) for action in details)
        if isinstance(details, dict):
            return str(details.get("note") or details.get("source") or details)
    if name in {"schema_assets", "prompt_assets", "v1_structure"} and isinstance(details, dict):
        return f"{details.get('count')} files"
    if name == "execution_mode" and isinstance(details, dict):
        calls = details.get("model_calls") or []
        call_text = ",".join(str(call) for call in calls) if calls else "none"
        return (
            f"dry_run={details.get('dry_run')}, persist={details.get('persist')}, "
            f"steps={details.get('steps')}, model_calls={call_text}"
        )
    if name == "memory" and isinstance(details, dict):
        summary = (
            f"{details.get('source')} status={details.get('status')} "
            f"items={details.get('item_count')}"
        )
        if "source_mapping_count" in details:
            summary += f" mappings={details.get('source_mapping_count')}"
        return summary
    if name == "memory_v2_compile" and isinstance(details, dict):
        return (
            f"dry_run={details.get('dry_run')} reset={details.get('reset')} "
            f"ops={details.get('operation_count')} events={details.get('event_count')} "
            f"revision={details.get('canonical_revision')}"
        )
    if name == "state_builder_audit" and isinstance(details, dict):
        return (
            f"items={details.get('item_count')} "
            f"applied={details.get('applied_count')} "
            f"skipped={details.get('skipped_count')} "
            f"deduplicated={details.get('deduplicated_count')} "
            f"applied_types={_format_count_entries(details.get('applied_type_counts'), 'type')} "
            f"skipped_types={_format_count_entries(details.get('skipped_type_counts'), 'type')} "
            f"skipped_blocking={details.get('skipped_blocking_count', 0)} "
            f"reasons={_format_count_entries(details.get('skipped_reason_counts'), 'reason_code')} "
            f"severity={_format_count_entries(details.get('skipped_severity_counts'), 'severity')}"
        )
    if name == "memory_input" and isinstance(details, dict):
        resolved = details.get("resolved_source")
        reason = details.get("resolution_reason")
        path = details.get("resolved_path")
        if path:
            return f"{resolved} ({path}) reason={reason}"
        return f"{resolved} reason={reason}"
    if name == "run_history" and isinstance(details, dict):
        latest = details.get("latest_run_id")
        if latest:
            summary = (
                f"runs={details.get('loaded')}/{details.get('total')} "
                f"latest={latest} status={details.get('latest_run_status')}"
            )
            executed_checks = details.get("latest_run_executed_checks")
            if isinstance(executed_checks, list) and executed_checks:
                summary += f" checks={','.join(str(item) for item in executed_checks)}"
            skipped_checks = details.get("latest_run_skipped_checks")
            if isinstance(skipped_checks, list) and skipped_checks:
                summary += f" skipped={','.join(str(item) for item in skipped_checks)}"
            return summary
        return f"runs={details.get('loaded')}/{details.get('total')} latest=none"
    if name == "schema_consistency" and isinstance(details, dict):
        return f"{details.get('count')} contracts"
    if name == "memory_writeback" and isinstance(details, dict):
        mode = details.get("mode")
        path = details.get("path")
        suffix = ""
        if mode == "notion" and details.get("notion_readback"):
            suffix = " readback=True"
        return f"{mode} ({path}){suffix}" if path else f"{mode}{suffix}"
    if name == "artifact_targets" and isinstance(details, dict):
        return f"run_dir={details.get('run_dir')}, chapter_dir={details.get('chapter_dir')}"
    if name == "director" and isinstance(details, dict):
        model = details.get("model")
        return f"{details.get('mode')} ({model})" if model else str(details.get("mode"))
    if name == "llm_validator" and isinstance(details, dict):
        return f"enabled={details.get('enabled')}"
    return str(details)


def _format_count_entries(entries, key: str) -> str:
    if not isinstance(entries, list) or not entries:
        return "none"
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get(key)
        count = entry.get("count")
        if name is None or count is None:
            continue
        parts.append(f"{name}={count}")
    return ",".join(parts) if parts else "none"


if __name__ == "__main__":
    main()
