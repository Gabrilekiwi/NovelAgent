from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.config import clear_proxy_env, proxy_disabled_by_env
from core.director import ModelDirector
from core.engine.artifacts import chapter_artifact_metadata, save_chapter_artifact
from core.engine.executor import AgentExecutor, LoopExecutionError
from core.engine.persistence import (
    atomic_create_json,
    atomic_write_json,
    complete_persistence_transaction,
    load_persistence_candidate,
    persistence_run_lock,
    reconcile_persistence,
)
from core.engine.preflight import run_preflight
from core.engine.recovery import RecoveryError, recover_latest_chapter_draft
from core.engine.report import build_run_report
from core.engine.run_record import validate_run_result
from core.runtime_paths import (
    DEFAULT_CHAPTER_DIR,
    DEFAULT_RUN_DIR,
    DEFAULT_SNAPSHOT_PATH,
    RuntimePaths,
    init_runtime_state,
)
from core.review.dashboard import build_review_dashboard_from_index
from core.review.index import get_latest_review, list_recent_reviews
from core.review.repair_loop import ReviewRepairConfig, validate_review_repair_config
from core.review.runtime import RuntimeReviewConfig, validate_runtime_review_config
from core.state.memory_writer import build_memory_writer, write_memory_updates
from core.state.snapshot import load_snapshot
from core.story_project.oh_story_detection import (
    detect_oh_story_compatibility,
    failed_oh_story_compatibility_report,
)
from core.story_project.paths import resolve_story_project_root
from core.story_project.identity import (
    ensure_project_identity_for_runtime,
    load_project_identity,
    project_identity_for_operation,
)
from core.story_project.migration import (
    StoryProjectRuntimeMigrationError,
    inspect_story_project_runtime_migration,
    migrate_story_project_runtime,
)
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.semantic_parser import (
    build_story_project_shadow_report,
    parse_story_project_semantic_state,
)
from core.story_project.validator import validate_story_project
from core.story_project.writer import StoryProjectWritebackConfig


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
        "--story-project",
        default=None,
        help="StoryProject root path, or auto to use .active-book / directory discovery.",
    )
    parser.add_argument(
        "--chapter",
        default="auto",
        help="StoryProject chapter number, or auto to infer the next chapter from 正文/.",
    )
    parser.add_argument(
        "--story-project-writeback",
        action="store_true",
        help="After a committed StoryProject run, write generated prose and tracking updates back to StoryProject.",
    )
    parser.add_argument(
        "--story-project-writeback-dry-run",
        action="store_true",
        help="Build StoryProject writeback plan and artifacts without modifying StoryProject files.",
    )
    parser.add_argument(
        "--story-project-overwrite",
        action="store_true",
        help="Allow StoryProject writeback to overwrite a uniquely resolved existing prose file.",
    )
    parser.add_argument(
        "--story-project-compat-report",
        action="store_true",
        help="Print a read-only oh-story compatibility report for the StoryProject root and exit.",
    )
    parser.add_argument(
        "--story-state-shadow-report",
        action="store_true",
        help="Parse StoryProject semantic state, print a read-only shadow diff, and exit without generation.",
    )
    parser.add_argument(
        "--inspect-story-project-runtime-from",
        default=None,
        metavar="PATH",
        help="Inspect an old runtime for safe StoryProject migration without writing files.",
    )
    parser.add_argument(
        "--migrate-story-project-runtime-from",
        default=None,
        metavar="PATH",
        help="Copy a proven matching old runtime into <StoryProject>/.novelagent/runtime and write a manifest.",
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
        "--persistence-dir",
        default=None,
        help="Directory for local persistence journals. Defaults to <run-dir>/transactions or StoryProject runtime/persistence.",
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
        "--quality-policy",
        choices=["auto", "minimal", "standard", "strict"],
        default="auto",
        help="Quality policy. auto uses standard for real StoryProject writeback and minimal otherwise.",
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
        "--review-auto-repair",
        action="store_true",
        help="Enable explicit review-driven repair when runtime review requests revision.",
    )
    parser.add_argument(
        "--review-repair-max-attempts",
        type=_review_repair_attempts,
        default=1,
        help="Maximum review repair attempts, from 1 to 3.",
    )
    parser.add_argument(
        "--review-repair-dry-run",
        action="store_true",
        help="Build review repair plan artifacts without changing runtime chapter text.",
    )
    parser.add_argument(
        "--review-latest",
        action="store_true",
        help="Print the latest runtime review entry from review_index.json and exit.",
    )
    parser.add_argument(
        "--review-list",
        action="store_true",
        help="Print recent runtime review entries from review_index.json and exit.",
    )
    parser.add_argument(
        "--review-list-limit",
        type=_positive_int,
        default=10,
        help="Maximum recent review entries to print with --review-list.",
    )
    parser.add_argument(
        "--review-status",
        choices=["pass", "warning", "needs_revision", "blocked", "error", "unknown"],
        default=None,
        help="Filter --review-list by review status.",
    )
    parser.add_argument(
        "--review-gate-status",
        choices=["disabled", "pass", "fail", "error"],
        default=None,
        help="Filter --review-list by review gate status.",
    )
    parser.add_argument(
        "--review-dashboard",
        action="store_true",
        help="Build a static HTML dashboard from review_index.json and exit.",
    )
    parser.add_argument(
        "--review-dashboard-out",
        default=None,
        help="Optional output path for --review-dashboard. Defaults to <review-output-dir>/dashboard.html.",
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
        "--reconcile-persistence",
        action="store_true",
        help="Reconcile incomplete local persistence transactions and publish durable candidates without generating.",
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
    argv = list(sys.argv[1:])
    args = parser.parse_args(argv)
    args._runtime_path_explicit = {
        "snapshot": _option_was_provided(argv, "--snapshot"),
        "run_dir": _option_was_provided(argv, "--run-dir"),
        "persistence_dir": _option_was_provided(argv, "--persistence-dir"),
        "chapter_dir": _option_was_provided(argv, "--chapter-dir"),
        "review_output_dir": _option_was_provided(argv, "--review-output-dir"),
        "memory_outbox": _option_was_provided(argv, "--memory-outbox"),
        "memory_v2_out": _option_was_provided(argv, "--memory-v2-out"),
    }
    return args


def _option_was_provided(argv: list[str], option: str) -> bool:
    return any(token == option or token.startswith(f"{option}=") for token in argv)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _review_repair_attempts(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 3:
        raise argparse.ArgumentTypeError("must be between 1 and 3")
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


def _apply_story_project_runtime_defaults(args: argparse.Namespace) -> RuntimePaths | None:
    if getattr(args, "story_project", None) is None:
        args._resolved_story_project_root = None
        args._story_project_runtime_paths = None
        return None

    resolution = resolve_story_project_root(args.story_project)
    if not resolution.ok or resolution.root is None:
        raise ValueError(resolution.error or "StoryProject root could not be resolved.")
    root = resolution.root.resolve()
    paths = RuntimePaths.for_story_project(root)
    explicit = getattr(args, "_runtime_path_explicit", {})
    if not explicit.get("snapshot"):
        args.snapshot = str(paths.snapshot_path)
    if not explicit.get("run_dir"):
        args.run_dir = str(paths.run_dir)
    if not explicit.get("chapter_dir"):
        args.chapter_dir = str(paths.chapter_dir)
    if not explicit.get("review_output_dir"):
        args.review_output_dir = str(paths.review_dir)
    if not explicit.get("persistence_dir"):
        args.persistence_dir = str(paths.persistence_dir)
    if not explicit.get("memory_v2_out"):
        args.memory_v2_out = str(paths.memory_dir / "v2")
    if args.memory_writeback == "file" and not explicit.get("memory_outbox"):
        args.memory_outbox = str(paths.memory_dir / "memory_outbox.jsonl")
    args._resolved_story_project_root = root
    args._story_project_runtime_paths = paths
    return paths


def _load_story_project_identity_for_read_command(args: argparse.Namespace):
    if getattr(args, "story_project", None) is None:
        return None
    identity = load_project_identity(args._resolved_story_project_root)
    if identity is None:
        raise ValueError(
            "StoryProject read command requires an existing .novelagent/project.json; "
            "run --init-runtime or a persistent StoryProject operation first"
        )
    return identity


def _validate_story_project_read_command_identity(args: argparse.Namespace) -> None:
    if bool(getattr(args, "report_runs", False)) or bool(getattr(args, "recover_latest", False)):
        _load_story_project_identity_for_read_command(args)


def main() -> None:
    args = parse_args()
    apply_notion_shortcuts(args)
    if args.no_proxy or proxy_disabled_by_env():
        clear_proxy_env()
    try:
        story_runtime_paths = _apply_story_project_runtime_defaults(args)
    except ValueError as exc:
        print(f"Configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.review_latest:
        latest = get_latest_review(review_output_dir=args.review_output_dir)
        payload = {"ok": True, "latest": latest}
        if args.output_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(format_review_latest_summary(latest))
        raise SystemExit(0)

    if args.review_list:
        entries = list_recent_reviews(
            review_output_dir=args.review_output_dir,
            limit=args.review_list_limit,
            status=args.review_status,
            gate_status=args.review_gate_status,
        )
        payload = {"ok": True, "count": len(entries), "entries": entries}
        if args.output_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(format_review_list_summary(entries))
        raise SystemExit(0)

    if args.review_dashboard:
        result = build_review_dashboard_from_index(
            review_output_dir=args.review_output_dir,
            output_path=args.review_dashboard_out,
        )
        payload = {"ok": True, "dashboard": result["metadata"]}
        if args.output_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(format_review_dashboard_summary(result["metadata"]))
        raise SystemExit(0)

    try:
        review_config = _runtime_review_config_from_args(args)
        review_repair_config = _review_repair_config_from_args(args)
        story_project_writeback = _story_project_writeback_config_from_args(args)
        _validate_story_project_multistep_args(args, story_project_writeback)
        _validate_story_project_compat_report_args(args)
        _validate_story_project_shadow_report_args(args)
        _validate_story_project_runtime_migration_args(args)
        _validate_story_project_read_command_identity(args)
    except ValueError as exc:
        print(f"Configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.init_runtime:
        if story_runtime_paths is not None:
            ensure_project_identity_for_runtime(
                args._resolved_story_project_root,
                persistence_dir=args.persistence_dir,
            )
            result = init_runtime_state(
                snapshot_target=args.snapshot,
                memory_target=story_runtime_paths.memory_dir / "notion_memory.json",
                overwrite=args.force_init_runtime,
            )
        else:
            result = init_runtime_state(overwrite=args.force_init_runtime)
        print(format_init_runtime_summary(result))
        raise SystemExit(0)

    if args.reconcile_persistence:
        reconcile_identity = (
            ensure_project_identity_for_runtime(
                args._resolved_story_project_root,
                persistence_dir=args.persistence_dir,
            )
            if args.story_project is not None
            else None
        )
        reconcile_kwargs = {
            "chapter_dir": args.chapter_dir,
            "state_paths": _persistence_state_paths_from_args(args),
        }
        if args.persistence_dir is not None:
            reconcile_kwargs["persistence_dir"] = args.persistence_dir
        if reconcile_identity is not None:
            reconcile_kwargs["expected_book_id"] = reconcile_identity.book_id
        result = _reconcile_and_publish_persistence(args.run_dir, **reconcile_kwargs)
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_persistence_reconcile_summary(result))
        raise SystemExit(0 if result.get("ok") else 1)

    if args.report_runs:
        report_identity = _load_story_project_identity_for_read_command(args)
        report = build_run_report(
            run_dir=args.run_dir,
            limit=args.report_limit,
            expected_book_id=report_identity.book_id if report_identity is not None else None,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    if args.recover_latest:
        recovery_identity = _load_story_project_identity_for_read_command(args)
        try:
            result = recover_latest_chapter_draft(
                run_dir=args.run_dir,
                chapter_dir=args.chapter_dir,
                expected_book_id=recovery_identity.book_id if recovery_identity is not None else None,
            )
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

    if args.story_project_compat_report:
        report = _build_story_project_compat_report(args)
        if args.output_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_story_project_compat_report(report))
        raise SystemExit(0)

    if args.story_state_shadow_report:
        try:
            report = _build_story_state_shadow_report(args)
        except ValueError as exc:
            print(f"StoryProject semantic shadow report failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    if args.inspect_story_project_runtime_from:
        inspection = inspect_story_project_runtime_migration(
            source_runtime=args.inspect_story_project_runtime_from,
            story_project_root=args._resolved_story_project_root,
        )
        if args.output_json:
            print(json.dumps(inspection, ensure_ascii=False, indent=2))
        else:
            print(format_story_project_runtime_migration_summary(inspection))
        raise SystemExit(0 if inspection["ok"] else 1)

    if args.migrate_story_project_runtime_from:
        try:
            migration = migrate_story_project_runtime(
                source_runtime=args.migrate_story_project_runtime_from,
                story_project_root=args._resolved_story_project_root,
            )
        except StoryProjectRuntimeMigrationError as exc:
            print(f"StoryProject runtime migration failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        if args.output_json:
            print(json.dumps(migration, ensure_ascii=False, indent=2))
        else:
            print(format_story_project_runtime_migration_summary(migration["inspection"], migrated=migration))
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
            story_project=args.story_project,
            chapter=args.chapter,
        )
        if args.check_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_preflight_summary(result))
        raise SystemExit(0 if result["ok"] else 1)

    will_commit_state = (not args.dry_run or args.persist_dry_run) and not args.story_project_writeback_dry_run
    project_identity = (
        project_identity_for_operation(
            args._resolved_story_project_root,
            persist=True,
            persistence_dir=args.persistence_dir,
        )
        if will_commit_state and args.story_project is not None
        else None
    )
    if will_commit_state:
        reconcile_kwargs = {
            "chapter_dir": args.chapter_dir,
            "state_paths": _persistence_state_paths_from_args(args),
        }
        if args.persistence_dir is not None:
            reconcile_kwargs["persistence_dir"] = args.persistence_dir
        if project_identity is not None:
            reconcile_kwargs["expected_book_id"] = project_identity.book_id
        persistence_health = _reconcile_and_publish_persistence(args.run_dir, **reconcile_kwargs)
        if not persistence_health.get("ok"):
            if args.output_json:
                print(json.dumps(persistence_health, ensure_ascii=False, indent=2))
            else:
                print(format_persistence_reconcile_summary(persistence_health), file=sys.stderr)
            raise SystemExit(1)

    story_project_context_loader = None
    story_project_oh_story_report = None
    if args.story_project is not None:
        project_identity = project_identity or project_identity_for_operation(
            args._resolved_story_project_root, persist=False
        )
        story_project_context_loader = build_generation_story_project_context_loader(
            story_project=args._resolved_story_project_root,
            chapter=args.chapter,
            overwrite=args.story_project_overwrite,
            project_identity=project_identity,
        )
        story_project_oh_story_report = _detect_story_project_context_compatibility(story_project_context_loader)

    executor = AgentExecutor(
        snapshot_path=args.snapshot,
        memory_path=args.memory,
        memory_source=args.memory_source,
        run_dir=args.run_dir,
        chapter_dir=args.chapter_dir,
        persistence_dir=args.persistence_dir,
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
        review_repair_config=review_repair_config,
        story_project_context_loader=story_project_context_loader,
        story_project_oh_story_report=story_project_oh_story_report,
        story_project_writeback=story_project_writeback,
        quality_policy=None if args.quality_policy == "auto" else args.quality_policy,
    )
    persist = (
        not args.dry_run
        or args.persist_dry_run
        or args.story_project_writeback_dry_run
        or args.review_repair_dry_run
    )
    loop_exit_code = 0
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
            loop_exit_code = int(loop_result.get("exit_code") or 0)
            if loop_exit_code:
                raise SystemExit(loop_exit_code)
            return
        result = loop_result["last_result"]
        loop_exit_code = int(loop_result.get("exit_code") or 0)
        result["loop"] = {
            "session_id": loop_result["session"]["id"],
            "completed_steps": loop_result["completed_steps"],
            "stopped_reason": loop_result["stopped_reason"],
            "run_ids": [item["run"]["id"] for item in loop_result["runs"]],
            "committed": [item["committed"] for item in loop_result["runs"]],
            "succeeded": bool(loop_result.get("succeeded")),
            "exit_code": loop_exit_code,
            "failure_reasons": list(loop_result.get("failure_reasons") or []),
        }
        if loop_result["session"].get("artifact"):
            result["loop"]["artifact"] = loop_result["session"]["artifact"]

    if args.output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.output_run_json:
        print(json.dumps(result["run"], ensure_ascii=False, indent=2))
    else:
        print(format_run_summary(result))
    if loop_exit_code:
        raise SystemExit(loop_exit_code)
    gate_exit_code = _review_gate_exit_code(result)
    if gate_exit_code:
        raise SystemExit(gate_exit_code)
    writeback_exit_code = _story_project_writeback_exit_code(result)
    if writeback_exit_code:
        raise SystemExit(writeback_exit_code)
    memory_exit_code = _memory_writeback_exit_code(result)
    if memory_exit_code:
        raise SystemExit(memory_exit_code)


def _reconcile_and_publish_persistence(
    run_dir: str | Path,
    *,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    state_paths: tuple[Path, ...] = (),
    expected_book_id: str | None = None,
    persistence_dir: str | Path | None = None,
) -> dict:
    with persistence_run_lock(run_dir, state_paths=state_paths):
        return _reconcile_and_publish_persistence_locked(
            run_dir,
            chapter_dir=chapter_dir,
            expected_book_id=expected_book_id,
            persistence_dir=persistence_dir,
        )


def _reconcile_and_publish_persistence_locked(
    run_dir: str | Path,
    *,
    chapter_dir: str | Path,
    expected_book_id: str | None = None,
    persistence_dir: str | Path | None = None,
) -> dict:
    report = reconcile_persistence(
        run_dir=run_dir,
        expected_book_id=expected_book_id,
        transactions_dir=persistence_dir,
    )
    published: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    root = Path(run_dir)
    reconciled_transactions: list[dict] = []
    for transaction in report.get("transactions") or []:
        reconciled_transactions.append(transaction)
        if not isinstance(transaction, dict) or not transaction.get("committed"):
            continue
        if transaction.get("state") not in {"commit_marked", "completed"}:
            continue
        run_id = str(transaction.get("run_id") or "")
        journal = transaction.get("journal_path")
        if not run_id or not journal:
            continue
        destination = root / f"{run_id}.json"
        try:
            candidate = load_persistence_candidate(journal)
            if not isinstance(candidate, dict):
                raise ValueError("committed transaction has no candidate result")
            validate_run_result(candidate)
            _validate_persistence_publication_identity(candidate, transaction)
            if destination.exists():
                existing = json.loads(destination.read_text(encoding="utf-8"))
                validate_run_result(existing)
                _validate_persistence_publication_identity(existing, transaction)
                _validate_existing_publication(existing, candidate)
                if transaction.get("state") == "commit_marked":
                    planned = ((candidate.get("run") or {}).get("chapter") or {}).get("artifact") or {}
                    if not planned.get("path") or not Path(str(planned["path"])).exists():
                        _ensure_candidate_chapter_artifact(candidate, fallback_chapter_dir=chapter_dir)
                skipped.append(run_id)
            else:
                _deliver_candidate_postcommit_outbox(candidate)
                _ensure_candidate_chapter_artifact(candidate, fallback_chapter_dir=chapter_dir)
                atomic_create_json(destination, candidate)
                published.append(run_id)
            if transaction.get("state") == "commit_marked":
                completed = complete_persistence_transaction(journal).to_dict()
                reconciled_transactions[-1] = completed
        except Exception as exc:  # noqa: BLE001 - reconciliation reports ambiguity without overwriting.
            errors.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
    result = dict(report)
    result["transactions"] = reconciled_transactions
    result["published_run_ids"] = published
    result["existing_run_ids"] = skipped
    result["publish_errors"] = errors
    pending = [
        item.get("run_id")
        for item in reconciled_transactions
        if isinstance(item, dict) and item.get("state") == "commit_marked"
    ]
    result["pending_publication"] = pending
    result["ok"] = bool(report.get("ok")) and not errors and not pending
    return result


def _validate_persistence_publication_identity(candidate: dict, transaction: dict) -> None:
    run = candidate.get("run") if isinstance(candidate.get("run"), dict) else None
    if not isinstance(run, dict) or run.get("id") != transaction.get("run_id"):
        raise ValueError("published run id does not match persistence transaction")
    persistence = run.get("persistence") if isinstance(run.get("persistence"), dict) else None
    if not isinstance(persistence, dict) or persistence.get("run_id") != transaction.get("run_id"):
        raise ValueError("published persistence identity does not match transaction")
    expected_journal = Path(str(transaction.get("journal_path") or "")).resolve()
    actual_journal = Path(str(persistence.get("journal_path") or "")).resolve()
    if expected_journal != actual_journal:
        raise ValueError("published persistence journal does not match transaction")
    if not persistence.get("committed"):
        raise ValueError("published persistence candidate is not committed")


def _ensure_candidate_chapter_artifact(candidate: dict, *, fallback_chapter_dir: str | Path) -> None:
    run = candidate.get("run") if isinstance(candidate.get("run"), dict) else None
    chapter = candidate.get("chapter")
    if not isinstance(run, dict) or not isinstance(chapter, str):
        raise ValueError("committed candidate is missing run or chapter content")
    run_chapter = run.get("chapter") if isinstance(run.get("chapter"), dict) else None
    if not isinstance(run_chapter, dict):
        raise ValueError("committed candidate is missing run chapter metadata")
    planned = run_chapter.get("artifact") if isinstance(run_chapter.get("artifact"), dict) else None
    output_dir = Path(str(planned.get("path"))).parent if planned and planned.get("path") else Path(fallback_chapter_dir)
    expected = chapter_artifact_metadata(chapter_text=chapter, run=run, output_dir=output_dir)
    if planned and Path(str(planned.get("path"))).resolve() != Path(expected["path"]).resolve():
        raise ValueError("candidate chapter artifact path does not match run identity")
    run_chapter["artifact"] = save_chapter_artifact(chapter_text=chapter, run=run, output_dir=output_dir)


def _validate_existing_publication(existing: dict, candidate: dict) -> None:
    existing_run = existing["run"]
    candidate_run = candidate["run"]
    if existing.get("chapter") != candidate.get("chapter"):
        raise ValueError("existing run chapter does not match hash-bound candidate")
    for key in ("id", "chapter_index", "status", "accepted", "committed"):
        if existing_run.get(key) != candidate_run.get(key):
            raise ValueError(f"existing run {key} does not match hash-bound candidate")
    existing_chapter = existing_run.get("chapter") if isinstance(existing_run.get("chapter"), dict) else {}
    candidate_chapter = candidate_run.get("chapter") if isinstance(candidate_run.get("chapter"), dict) else {}
    existing_artifact = existing_chapter.get("artifact") if isinstance(existing_chapter.get("artifact"), dict) else {}
    candidate_artifact = candidate_chapter.get("artifact") if isinstance(candidate_chapter.get("artifact"), dict) else {}
    if existing_artifact.get("path") != candidate_artifact.get("path"):
        raise ValueError("existing chapter artifact path does not match hash-bound candidate")


def _deliver_candidate_postcommit_outbox(candidate: dict) -> None:
    run = candidate["run"]
    persistence = run.get("persistence") if isinstance(run.get("persistence"), dict) else {}
    publication = persistence.get("publication") if isinstance(persistence.get("publication"), dict) else {}
    memory = publication.get("memory_outbox") if isinstance(publication.get("memory_outbox"), dict) else {}
    status = str(memory.get("status") or "not_applicable")
    if status == "external_pending":
        raise RuntimeError("pending external memory delivery cannot be reconciled deterministically")
    if status not in {"pending", "failed"}:
        return
    if memory.get("target") != "file" or not memory.get("path"):
        raise ValueError("pending memory outbox is missing a deterministic file target")
    updates = memory.get("updates")
    if not isinstance(updates, list):
        raise ValueError("pending memory outbox is missing updates")
    writer = build_memory_writer(mode="file", outbox_path=str(memory["path"]))
    writeback = write_memory_updates(updates, writer)
    gate = memory.get("gate")
    if isinstance(gate, dict):
        writeback["gate"] = gate
    verification = writeback.get("verification") if isinstance(writeback.get("verification"), dict) else {}
    if verification.get("status") in {"failed", "error", "readback_failed"}:
        raise RuntimeError("pending memory outbox verification failed")
    candidate["memory_write"] = writeback
    run.setdefault("memory", {})["writeback"] = writeback
    updated_memory = dict(memory)
    updated_memory["status"] = "delivered"
    updated_publication = dict(publication)
    updated_publication["memory_outbox"] = updated_memory
    persistence["publication"] = updated_publication
    if isinstance(candidate.get("persistence"), dict):
        candidate["persistence"]["publication"] = updated_publication


def _persistence_state_paths_from_args(args: argparse.Namespace) -> tuple[Path, ...]:
    paths = [Path(str(args.snapshot))]
    story_project = getattr(args, "story_project", None)
    if story_project is not None:
        resolution = resolve_story_project_root(story_project)
        if resolution.root is not None:
            paths.append(Path(resolution.root))
    writer = build_memory_writer(
        mode=str(getattr(args, "memory_writeback", "none")),
        outbox_path=getattr(args, "memory_outbox", None),
        notion_readback=bool(getattr(args, "notion_readback", False)),
    )
    if hasattr(writer, "path"):
        paths.append(Path(getattr(writer, "path")))
    return tuple(paths)


def format_persistence_reconcile_summary(result: dict) -> str:
    return "\n".join(
        [
            f"Persistence reconcile: {'OK' if result.get('ok') else 'FAILED'}",
            f"Transactions: {result.get('transaction_count', 0)}",
            f"Published runs: {len(result.get('published_run_ids') or [])}",
            f"Recovery required: {len(result.get('recovery_required') or [])}",
            f"Publish errors: {len(result.get('publish_errors') or [])}",
        ]
    )


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
        ("story_project_structure", "StoryProject"),
        ("story_project_runtime_context", "StoryProject runtime"),
        ("oh_story_detection", "oh-story"),
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

    review_index = run.get("review_index") if isinstance(run.get("review_index"), dict) else None
    if review_index and review_index.get("enabled"):
        lines.extend(
            [
                "",
                "Review index:",
                f"- status: {review_index.get('status')}",
                f"- index: {review_index.get('index_path')}",
                f"- latest_run_id: {review_index.get('latest_run_id')}",
                f"- entry_count: {review_index.get('entry_count')}",
            ]
        )
        if review_index.get("error"):
            lines.append(f"- error: {review_index.get('error')}")

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


def format_review_latest_summary(entry: dict | None) -> str:
    if not entry:
        return "No review entries found."
    lines = [
        "Latest review:",
        f"- run_id: {entry.get('run_id')}",
        f"- chapter_index: {entry.get('chapter_index')}",
        f"- review_status: {entry.get('review_status')}",
        f"- decision: {entry.get('review_decision')}",
        f"- quality_score: {entry.get('quality_score')}",
        f"- rule_score: {entry.get('rule_score')}",
        f"- repair_tasks: {entry.get('repair_task_count')} total, {entry.get('blocking_task_count')} blocking",
        (
            f"- gate: {entry.get('gate_status')} threshold={entry.get('gate_threshold')} "
            f"exit_code={entry.get('gate_exit_code')}"
        ),
    ]
    if entry.get("human_report_path"):
        lines.append(f"- human_report: {entry.get('human_report_path')}")
    if entry.get("repair_prompt_path"):
        lines.append(f"- repair_prompt: {entry.get('repair_prompt_path')}")
    if entry.get("summary_path"):
        lines.append(f"- summary: {entry.get('summary_path')}")
    return "\n".join(lines)


def format_story_project_compat_report(report: dict) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "oh-story compatibility:",
        f"- root: {report.get('root')}",
        f"- detected: {report.get('detected')}",
        f"- confidence: {report.get('confidence')}",
        f"- markers: {summary.get('present_count', 0)} present, {summary.get('optional_missing_count', 0)} missing optional",
        f"- unsupported: {summary.get('unsupported_count', 0)}",
    ]
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    if warnings:
        lines.append(f"- warnings: {len(warnings)}")
    return "\n".join(lines)


def format_story_project_runtime_migration_summary(
    inspection: dict,
    *,
    migrated: dict | None = None,
) -> str:
    lines = [
        "StoryProject runtime migration:",
        f"- source: {inspection.get('source_runtime')}",
        f"- target: {inspection.get('target_runtime')}",
        f"- copy_allowed: {inspection.get('copy_allowed')}",
        f"- records: {len(inspection.get('records') or [])}",
        f"- problems: {len(inspection.get('problems') or [])}",
    ]
    if migrated is not None:
        lines.append(f"- migrated: {migrated.get('ok')}")
        lines.append(f"- manifest: {migrated.get('manifest_path')}")
    return "\n".join(lines)


def format_review_list_summary(entries: list[dict]) -> str:
    if not entries:
        return "No review entries found."
    lines = ["Recent reviews:"]
    for index, entry in enumerate(entries, start=1):
        lines.append(
            f"{index}. {entry.get('run_id')} - {entry.get('review_status')} - "
            f"gate={entry.get('gate_status')} - quality={entry.get('quality_score')} - rule={entry.get('rule_score')}"
        )
    return "\n".join(lines)


def format_review_dashboard_summary(metadata: dict) -> str:
    return "\n".join(
        [
            "Review dashboard generated:",
            f"- output: {metadata.get('output_path')}",
            f"- entries: {metadata.get('entry_count')}",
            f"- latest_run_id: {metadata.get('latest_run_id')}",
        ]
    )


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


def _validate_story_project_compat_report_args(args: argparse.Namespace) -> None:
    if bool(getattr(args, "story_project_compat_report", False)) and getattr(args, "story_project", None) is None:
        raise ValueError("--story-project-compat-report requires --story-project")


def _validate_story_project_shadow_report_args(args: argparse.Namespace) -> None:
    enabled = bool(getattr(args, "story_state_shadow_report", False))
    if enabled and getattr(args, "story_project", None) is None:
        raise ValueError("--story-state-shadow-report requires --story-project")
    if enabled and bool(getattr(args, "story_project_compat_report", False)):
        raise ValueError("--story-state-shadow-report cannot be combined with --story-project-compat-report")
    if enabled and (
        getattr(args, "inspect_story_project_runtime_from", None)
        or getattr(args, "migrate_story_project_runtime_from", None)
    ):
        raise ValueError("--story-state-shadow-report cannot be combined with StoryProject runtime migration")


def _build_story_state_shadow_report(args: argparse.Namespace) -> dict:
    root = Path(args._resolved_story_project_root)
    validation = validate_story_project(
        story_project=root,
        chapter=args.chapter,
        workspace_root=Path.cwd(),
        allow_existing_prose=True,
    )
    if validation.problems:
        details = "; ".join(f"{problem.code}: {problem.message}" for problem in validation.problems)
        raise ValueError(details)
    chapter_resolution = validation.chapter_resolution
    if chapter_resolution is None or chapter_resolution.resolved_chapter is None:
        raise ValueError("StoryProject chapter could not be resolved")
    identity = project_identity_for_operation(root, persist=False)
    state = parse_story_project_semantic_state(
        root,
        chapter_resolution.resolved_chapter,
        project_identity=identity,
    )
    snapshot_path = Path(args.snapshot)
    snapshot = load_snapshot(snapshot_path) if snapshot_path.is_file() else None
    return build_story_project_shadow_report(state, snapshot=snapshot)


def _validate_story_project_runtime_migration_args(args: argparse.Namespace) -> None:
    inspect_source = getattr(args, "inspect_story_project_runtime_from", None)
    migrate_source = getattr(args, "migrate_story_project_runtime_from", None)
    if inspect_source and migrate_source:
        raise ValueError(
            "--inspect-story-project-runtime-from and --migrate-story-project-runtime-from are mutually exclusive"
        )
    if (inspect_source or migrate_source) and getattr(args, "story_project", None) is None:
        raise ValueError("StoryProject runtime migration requires --story-project")
    if (inspect_source or migrate_source) and bool(getattr(args, "init_runtime", False)):
        raise ValueError("StoryProject runtime migration cannot be combined with --init-runtime")
    if (inspect_source or migrate_source) and bool(getattr(args, "story_project_compat_report", False)):
        raise ValueError("StoryProject runtime migration cannot be combined with --story-project-compat-report")


def _build_story_project_compat_report(args: argparse.Namespace) -> dict:
    resolution = resolve_story_project_root(args.story_project)
    root = resolution.root if resolution.root is not None else None
    try:
        report = detect_oh_story_compatibility(root, workspace_root=Path.cwd())
    except Exception as exc:  # noqa: BLE001 - report mode is read-only and diagnostic.
        report = failed_oh_story_compatibility_report(root, exc, workspace_root=Path.cwd())
    if resolution.error:
        report = _report_with_warning(report, resolution.error)
    return report


def _detect_story_project_context_compatibility(story_project_context) -> dict | None:
    root = getattr(story_project_context, "story_project_root", None)
    try:
        return detect_oh_story_compatibility(root, workspace_root=Path.cwd())
    except Exception as exc:  # noqa: BLE001 - oh-story detection never blocks generation.
        return failed_oh_story_compatibility_report(root, exc, workspace_root=Path.cwd())


def _report_with_warning(report: dict, warning: str) -> dict:
    updated = dict(report)
    warnings = list(updated.get("warnings") or [])
    warnings.append(str(warning))
    updated["warnings"] = list(dict.fromkeys(warnings))
    return updated


def _review_repair_config_from_args(args: argparse.Namespace) -> ReviewRepairConfig:
    if bool(getattr(args, "review_auto_repair", False)) and not bool(getattr(args, "enable_review_pipeline", False)):
        raise ValueError("--review-auto-repair requires --enable-review-pipeline")
    if bool(getattr(args, "review_repair_dry_run", False)) and not bool(getattr(args, "review_auto_repair", False)):
        raise ValueError("--review-repair-dry-run requires --review-auto-repair")
    return validate_review_repair_config(
        ReviewRepairConfig(
            enabled=bool(getattr(args, "review_auto_repair", False)),
            max_attempts=int(getattr(args, "review_repair_max_attempts", 1)),
            dry_run=bool(getattr(args, "review_repair_dry_run", False)),
            gate_threshold=str(getattr(args, "review_gate", "off")),
        )
    )


def _story_project_writeback_config_from_args(args: argparse.Namespace) -> StoryProjectWritebackConfig:
    real_writeback = bool(getattr(args, "story_project_writeback", False))
    dry_run_writeback = bool(getattr(args, "story_project_writeback_dry_run", False))
    if real_writeback and dry_run_writeback:
        raise ValueError("--story-project-writeback and --story-project-writeback-dry-run are mutually exclusive")
    if real_writeback and bool(getattr(args, "dry_run", False)):
        raise ValueError("--dry-run cannot be combined with --story-project-writeback; use --story-project-writeback-dry-run")
    if dry_run_writeback and bool(getattr(args, "persist_dry_run", False)):
        raise ValueError("--story-project-writeback-dry-run cannot be combined with --persist-dry-run")
    if (real_writeback or dry_run_writeback) and getattr(args, "story_project", None) is None:
        raise ValueError("--story-project-writeback requires --story-project")
    if real_writeback and str(getattr(args, "memory_writeback", "none")) == "notion":
        raise ValueError("--story-project-writeback cannot be combined with direct Notion writeback; use file outbox")
    mode = "none"
    if real_writeback:
        mode = "apply"
    elif dry_run_writeback:
        mode = "dry_run"
    return StoryProjectWritebackConfig(mode=mode, overwrite=bool(getattr(args, "story_project_overwrite", False)))


def _validate_story_project_multistep_args(
    args: argparse.Namespace,
    writeback: StoryProjectWritebackConfig,
) -> None:
    if getattr(args, "story_project", None) is None or int(getattr(args, "steps", 1)) <= 1:
        return
    if writeback.mode != "apply":
        raise ValueError("StoryProject --steps > 1 requires --story-project-writeback")
    if bool(getattr(args, "dry_run", False)) or bool(getattr(args, "persist_dry_run", False)):
        raise ValueError("StoryProject multi-step writeback cannot use global dry-run or --persist-dry-run")


def _review_gate_exit_code(result: dict | None) -> int:
    if not isinstance(result, dict):
        return 0
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    gate = run.get("review_gate") if isinstance(run.get("review_gate"), dict) else result.get("review_gate")
    if not isinstance(gate, dict):
        return 0
    return 1 if gate.get("exit_code") == 1 else 0


def _story_project_writeback_exit_code(result: dict | None) -> int:
    if not isinstance(result, dict):
        return 0
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    story_project = run.get("story_project") if isinstance(run.get("story_project"), dict) else {}
    writeback = story_project.get("writeback") if isinstance(story_project.get("writeback"), dict) else {}
    if not writeback.get("attempted") or writeback.get("dry_run"):
        return 0
    return 0 if writeback.get("applied") else 1


def _memory_writeback_exit_code(result: dict | None) -> int:
    if not isinstance(result, dict):
        return 0
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    memory = run.get("memory") if isinstance(run.get("memory"), dict) else {}
    writeback = memory.get("writeback") if isinstance(memory.get("writeback"), dict) else {}
    verification = writeback.get("verification") if isinstance(writeback.get("verification"), dict) else {}
    return 1 if verification.get("status") in {"failed", "error"} else 0


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
    if name == "story_project_structure" and isinstance(details, dict):
        root = details.get("root") if isinstance(details.get("root"), dict) else {}
        chapter = details.get("chapter_resolution") if isinstance(details.get("chapter_resolution"), dict) else {}
        problems = details.get("problems") if isinstance(details.get("problems"), list) else []
        return (
            f"root={root.get('root')} source={root.get('source')} "
            f"chapter={chapter.get('resolved_chapter')} problems={len(problems)}"
        )
    if name == "story_project_runtime_context" and isinstance(details, dict):
        source_paths = details.get("source_paths") if isinstance(details.get("source_paths"), dict) else {}
        memory_overlay = details.get("memory_context_overlay") if isinstance(details.get("memory_context_overlay"), dict) else {}
        return (
            f"chapter={details.get('chapter_index')} "
            f"outline={source_paths.get('outline_path')} "
            f"items={len(memory_overlay.get('items') or [])} "
            f"warnings={len(details.get('warnings') or [])} "
            f"missing={len(details.get('missing_fields') or [])}"
        )
    if name == "oh_story_detection" and isinstance(details, dict):
        summary = details.get("summary") if isinstance(details.get("summary"), dict) else {}
        return (
            f"detected={details.get('detected')} "
            f"confidence={details.get('confidence')} "
            f"markers={summary.get('present_count', 0)} "
            f"unsupported={summary.get('unsupported_count', 0)}"
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
