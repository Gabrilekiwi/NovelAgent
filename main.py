from __future__ import annotations

import argparse
import json

from core.director import ModelDirector
from core.engine.executor import AgentExecutor, LoopExecutionError
from core.engine.preflight import run_preflight
from core.engine.report import build_run_report
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
        default="data/snapshot.json",
        help="Snapshot file path.",
    )
    parser.add_argument(
        "--memory",
        default=None,
        help="Memory context file path. Defaults to NOVELAGENT_MEMORY_PATH or data/memory.json.",
    )
    parser.add_argument(
        "--memory-source",
        choices=["auto", "file", "notion"],
        default="auto",
        help="Select memory input mode. auto uses Notion API when configured and no --memory path is provided.",
    )
    parser.add_argument(
        "--chapter-dir",
        default="data/chapters",
        help="Directory for persisted chapter markdown artifacts.",
    )
    parser.add_argument(
        "--run-dir",
        default="data/runs",
        help="Directory for persisted run JSON records.",
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
        "--director-model",
        default=None,
        help="Use an OpenAI-backed Director with this model name instead of the offline rule Director.",
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
        "--report-runs",
        action="store_true",
        help="Print a JSON report for persisted run records and exit.",
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
    return parser.parse_args()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main() -> None:
    args = parse_args()
    if args.report_runs:
        report = build_run_report(run_dir=args.run_dir, limit=args.report_limit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
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
            memory_writeback=args.memory_writeback,
            memory_outbox=args.memory_outbox,
            notion_readback=args.notion_readback,
            persist=not args.dry_run or args.persist_dry_run,
            steps=args.steps,
            continue_on_rejection=args.continue_on_rejection,
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
        director=ModelDirector(model=args.director_model) if args.director_model else None,
        memory_writer=build_memory_writer(
            mode=args.memory_writeback,
            outbox_path=args.memory_outbox,
            notion_readback=args.notion_readback,
        ),
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
        ("state_builder_audit", "State builder"),
        ("run_history", "Run history"),
        ("planned_workflow", "Workflow"),
        ("v1_structure", "V1 structure"),
        ("schema_assets", "Schemas"),
        ("schema_consistency", "Schema consistency"),
        ("prompt_assets", "Prompts"),
        ("memory_writeback", "Memory writeback"),
        ("artifact_targets", "Artifacts"),
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
    return artifacts


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
            model_call = event.get("model_call")
            if isinstance(model_call, dict):
                label = str(event.get("action") or "workflow")
                model_calls.append((label, model_call))
    return model_calls


def _format_model_call(model_call: dict) -> str:
    parts = [
        str(model_call.get("provider") or "unknown"),
        str(model_call.get("stage") or "unknown"),
    ]
    if model_call.get("model"):
        parts.append(f"model={model_call.get('model')}")
    if model_call.get("cause_type"):
        parts.append(f"cause={model_call.get('cause_type')}")
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
