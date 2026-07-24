from __future__ import annotations

import argparse
import hashlib
import uuid
from pathlib import Path
from typing import Any, Mapping

from core.autonomy.common import AutonomyContractError, canonical_hash, load_json_object
from core.autonomy.plans import build_source_snapshot, compile_instruction_plan
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.runner import AutonomyRunner
from core.autonomy.session import AutonomySessionStore
from core.config import get_config
from core.delivery import DeliveryQueue
from core.engine.executor import AgentExecutor
from core.review.runtime import RuntimeReviewConfig
from core.story_project.writer import StoryProjectWritebackConfig
from core.story_project.identity import load_project_identity
from core.story_project.paths import infer_next_chapter
from core.story_project.runtime import build_generation_story_project_context_loader


_INCOMPATIBLE_TOP_LEVEL_COMMANDS = (
    ("check", "--check"),
    ("check_json", "--check-json"),
    ("check_memory_v2", "--check-memory-v2"),
    ("report_runs", "--report-runs"),
    ("recover_latest", "--recover-latest"),
    ("recover_locked_chapter", "--recover-locked-chapter"),
    ("reconcile_persistence", "--reconcile-persistence"),
    ("reconcile_deliveries", "--reconcile-deliveries"),
    ("inspect_delivery", "--inspect-delivery"),
    ("resolve_delivery", "--resolve-delivery"),
    ("init_runtime", "--init-runtime"),
    ("force_init_runtime", "--force-init-runtime"),
    ("review_latest", "--review-latest"),
    ("review_list", "--review-list"),
    ("review_dashboard", "--review-dashboard"),
    ("story_project_compat_report", "--story-project-compat-report"),
    ("story_state_shadow_report", "--story-state-shadow-report"),
    ("activate_story_state", "--activate-story-state"),
    ("inspect_story_project_runtime_from", "--inspect-story-project-runtime-from"),
    ("migrate_story_project_runtime_from", "--migrate-story-project-runtime-from"),
)


def add_autonomy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--instruction",
        default=None,
        metavar="TEXT",
        help="Compile natural language into a read-only trusted InstructionPlan preview.",
    )
    parser.add_argument(
        "--execute-plan",
        default=None,
        metavar="PATH",
        help="Validate and start or recover the durable session for an immutable InstructionPlan.",
    )
    parser.add_argument(
        "--trusted-profiles",
        default=None,
        metavar="PATH",
        help="Trusted StoryProject, provider/model, File Delivery, budget, and quality profiles.",
    )
    parser.add_argument(
        "--autonomy-root-map",
        default=None,
        metavar="PATH",
        help=(
            "Operator-owned JSON mapping trusted File Delivery root UUIDs to physical "
            "directories. Required only for execution/resume; never enters the plan."
        ),
    )
    for option, help_text in (
        ("session-status", "Inspect a durable autonomy session without generating."),
        ("resume-session", "Resume a cancelled durable autonomy session."),
        ("cancel-session", "Cancel and release the book lease for a durable session."),
        ("abandon-session", "Permanently abandon a durable session and release its lease."),
    ):
        parser.add_argument(
            f"--{option}",
            nargs="?",
            const="latest",
            default=None,
            metavar="SESSION_ID",
            help=f"{help_text} Omit SESSION_ID to use the latest session.",
        )


def autonomy_command_requested(args: argparse.Namespace) -> bool:
    commands = _commands(args)
    if len(commands) > 1:
        raise ValueError("choose only one autonomy instruction/session command")
    if not commands:
        return False
    conflicts = [
        option
        for attribute, option in _INCOMPATIBLE_TOP_LEVEL_COMMANDS
        if bool(getattr(args, attribute, False))
    ]
    if conflicts:
        raise ValueError(
            "autonomy commands cannot be combined with existing top-level commands: "
            + ", ".join(conflicts)
        )
    if getattr(args, "story_project", None) is None:
        raise ValueError("autonomy commands require an explicit --story-project locator")
    if bool(getattr(args, "_steps_explicit", False)) or int(
        getattr(args, "steps", 1)
    ) != 1:
        raise ValueError(
            "autonomy commands reject deprecated --steps; chapter count is bound "
            "only by the immutable InstructionPlan"
        )
    profile_bound = commands[0][0] in {"instruction", "execute_plan", "resume_session"}
    if profile_bound and not getattr(args, "trusted_profiles", None):
        raise ValueError(
            "--instruction, --execute-plan, and --resume-session require --trusted-profiles PATH"
        )
    if commands[0][0] in {"execute_plan", "resume_session"} and not getattr(
        args, "autonomy_root_map", None
    ):
        raise ValueError(
            "--execute-plan and --resume-session require --autonomy-root-map PATH"
        )
    if (
        commands[0][0] in {"execute_plan", "resume_session"}
        and bool(getattr(args, "dry_run", False))
        and not bool(getattr(args, "persist_dry_run", False))
    ):
        raise ValueError(
            "autonomy --dry-run uses synthetic prose and requires explicit --persist-dry-run"
        )
    forbidden = (
        bool(getattr(args, "notion_sync", False)),
        bool(getattr(args, "notion_memory", False)),
        str(getattr(args, "memory_writeback", "none")) == "notion",
    )
    if any(forbidden):
        raise ValueError("autonomy commands cannot be combined with Notion execution")
    return True


def run_autonomy_command(
    args: argparse.Namespace,
    *,
    story_runtime_paths: Any | None,
) -> dict[str, Any]:
    commands = _commands(args)
    if len(commands) != 1:
        raise ValueError("exactly one autonomy command is required")
    command, value = commands[0]
    profiles = (
        TrustedProfiles.load(args.trusted_profiles)
        if getattr(args, "trusted_profiles", None)
        else None
    )
    runtime_root = _autonomy_root(args, story_runtime_paths)
    sessions = AutonomySessionStore(
        runtime_root,
        trusted_profiles=profiles,
        publication_root_map=_publication_root_map(args, story_runtime_paths),
    )

    if command == "instruction":
        assert profiles is not None
        source = _capture_source_snapshot_from_args(args, profiles=profiles)
        plan = compile_instruction_plan(
            str(value), trusted_profiles=profiles, source_snapshot=source
        )
        _assert_supported_cli_provider(plan)
        artifact = sessions.save_preview(plan)
        return {
            "ok": True,
            "command": "instruction_preview",
            "plan": plan,
            "artifact": {"path": str(artifact)},
            "executed": False,
        }
    if command == "execute_plan":
        assert profiles is not None
        plan = load_json_object(value)
        _assert_cli_execution_environment(args, plan)
        story_profile_id = str(
            (plan.get("selections") or {}).get("story_project", {}).get("profile_id") or ""
        )
        runner = _build_autonomy_runner(
            args,
            story_runtime_paths=story_runtime_paths,
            sessions=sessions,
            profiles=profiles,
            story_profile_id=story_profile_id,
        )
        execution = runner.execute_plan(plan)
        return {
            "ok": execution["stopped_reason"] == "completed",
            "command": "execute_plan",
            "session": execution["session"],
            "execution": execution,
        }
    if command == "session_status":
        return {
            "ok": True,
            "command": "session_status",
            "session": sessions.status(str(value)),
        }
    if command == "resume_session":
        assert profiles is not None
        plan = sessions.load_instruction_plan(str(value))
        _assert_cli_execution_environment(args, plan)
        story_profile_id = plan["selections"]["story_project"]["profile_id"]
        runner = _build_autonomy_runner(
            args,
            story_runtime_paths=story_runtime_paths,
            sessions=sessions,
            profiles=profiles,
            story_profile_id=story_profile_id,
        )
        execution = runner.resume(str(value))
        return {
            "ok": execution["stopped_reason"] == "completed",
            "command": "resume_session",
            "session": execution["session"],
            "execution": execution,
        }
    if command == "cancel_session":
        return {
            "ok": True,
            "command": "cancel_session",
            "session": sessions.cancel(str(value), reason="cli_cancel"),
        }
    return {
        "ok": True,
        "command": "abandon_session",
        "session": sessions.abandon(str(value), reason="cli_abandon"),
    }


def _commands(args: argparse.Namespace) -> list[tuple[str, Any]]:
    return [
        (name, value)
        for name, value in (
            ("instruction", getattr(args, "instruction", None)),
            ("execute_plan", getattr(args, "execute_plan", None)),
            ("session_status", getattr(args, "session_status", None)),
            ("resume_session", getattr(args, "resume_session", None)),
            ("cancel_session", getattr(args, "cancel_session", None)),
            ("abandon_session", getattr(args, "abandon_session", None)),
        )
        if value is not None
    ]


def _autonomy_root(args: argparse.Namespace, story_runtime_paths: Any | None) -> Path:
    if story_runtime_paths is not None:
        return Path(story_runtime_paths.runtime_dir) / "autonomy"
    run_dir = Path(getattr(args, "run_dir", ".tmp/runtime/runs"))
    return run_dir.parent / "autonomy"


def _publication_root_map(
    args: argparse.Namespace, story_runtime_paths: Any | None
) -> dict[str, Path] | None:
    story_root = getattr(args, "_resolved_story_project_root", None)
    if (
        story_runtime_paths is None
        or story_root is None
        or not hasattr(story_runtime_paths, "root_map")
    ):
        return None
    roots = story_runtime_paths.root_map(story_root)
    roots["runtime"] = Path(story_runtime_paths.runtime_dir).resolve()
    return roots


def _capture_source_snapshot_from_args(
    args: argparse.Namespace,
    *,
    profiles: TrustedProfiles,
    story_profile_id: str | None = None,
) -> dict[str, Any]:
    root_value = getattr(args, "_resolved_story_project_root", None)
    if root_value is None:
        raise AutonomyContractError(
            "autonomy_story_project_missing", "resolved StoryProject root is required"
        )
    root = Path(root_value).resolve()
    identity = load_project_identity(root)
    if identity is None or identity.ephemeral:
        raise AutonomyContractError(
            "autonomy_story_project_identity_missing",
            "durable autonomy requires an existing non-ephemeral ProjectIdentity",
        )
    profile = profiles.get("story_projects", story_profile_id or None)
    if profile["book_id"] != identity.book_id:
        raise AutonomyContractError(
            "autonomy_story_project_identity_mismatch",
            "trusted StoryProject profile does not match ProjectIdentity",
        )
    next_chapter = infer_next_chapter(root)
    authority = identity.authority if isinstance(identity.authority, Mapping) else {}
    return build_source_snapshot(
        book_id=identity.book_id,
        root_uuid=profile["root_uuid"],
        authority_epoch=int(authority.get("authority_epoch") or 0),
        authority_head_event_hash=authority.get("head_event_hash"),
        canonical_next_chapter=next_chapter,
        source_digest=_story_source_digest(root, identity.to_dict()),
    )


def _story_source_digest(root: Path, identity: Mapping[str, Any]) -> str:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] in {".novelagent", ".git"}:
            continue
        if path.is_symlink() or not path.is_file():
            raise AutonomyContractError(
                "autonomy_story_source_unsafe", "StoryProject Markdown source must be a regular file"
            )
        raw = path.read_bytes()
        files.append(
            {
                "relative_path": relative.as_posix(),
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return canonical_hash({"project_identity": dict(identity), "markdown_files": files})


def _build_autonomy_runner(
    args: argparse.Namespace,
    *,
    story_runtime_paths: Any | None,
    sessions: AutonomySessionStore,
    profiles: TrustedProfiles,
    story_profile_id: str,
) -> AutonomyRunner:
    if story_runtime_paths is None:
        raise AutonomyContractError(
            "autonomy_runtime_missing", "StoryProject runtime paths are required"
        )
    story_root = Path(args._resolved_story_project_root).resolve()
    publication_roots = _publication_root_map(args, story_runtime_paths)
    if publication_roots is None:
        raise AutonomyContractError(
            "autonomy_publication_roots_missing",
            "event publication roots are required for autonomy execution",
        )
    operator_roots = _load_operator_root_map(args.autonomy_root_map)
    queue = DeliveryQueue(story_runtime_paths.delivery_dir)

    def source_loader() -> dict[str, Any]:
        return _capture_source_snapshot_from_args(
            args, profiles=profiles, story_profile_id=story_profile_id
        )

    def executor_factory(request):
        identity = load_project_identity(story_root)
        if identity is None or identity.ephemeral:
            raise AutonomyContractError(
                "autonomy_story_project_identity_missing",
                "durable autonomy requires an existing ProjectIdentity",
            )
        provider = request.provider_profile
        quality = request.quality_profile
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run:
            config = get_config()
            if config.openai_model != provider["model"]:
                raise AutonomyContractError(
                    "autonomy_provider_model_mismatch",
                    "configured OpenAI model differs from the trusted profile",
                )
            if int(config.openai_max_output_tokens) > int(
                provider["max_output_tokens"]
            ):
                raise AutonomyContractError(
                    "autonomy_provider_budget_mismatch",
                    "configured provider output limit exceeds the trusted profile",
                )
            actual_endpoint = (
                "openai_compatible" if config.openai_base_url else "official"
            )
            if actual_endpoint != provider["endpoint_type"]:
                raise AutonomyContractError(
                    "autonomy_provider_endpoint_mismatch",
                    "configured endpoint type differs from the trusted profile",
                )
        enable_llm_validator = not dry_run and quality["policy"] == "strict"
        strict_review_config = (
            RuntimeReviewConfig(
                enabled=True,
                output_dir=story_runtime_paths.review_dir,
                gate_threshold="warning",
            )
            if quality["policy"] == "strict"
            else None
        )
        loader = build_generation_story_project_context_loader(
            story_project=story_root,
            chapter=request.chapter_index,
            project_identity=identity,
            outline_override=request.outline_checkpoint,
        )
        return AgentExecutor(
            snapshot_path=story_runtime_paths.snapshot_path,
            memory_path=story_runtime_paths.memory_dir / "unused_legacy_memory.json",
            memory_source="file",
            run_dir=story_runtime_paths.run_dir,
            chapter_dir=story_runtime_paths.chapter_dir,
            persistence_dir=story_runtime_paths.persistence_dir,
            dry_run=dry_run,
            enable_llm_validator=enable_llm_validator,
            review_config=strict_review_config,
            use_run_history=False,
            memory_loader=lambda: {},
            polisher=lambda chapter: chapter,
            story_project_context_loader=loader,
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy=str(quality["policy"]),
            enable_execution_provenance=True,
            run_budget_limits=request.runtime_context.expected_run_budget_limits(),
            file_delivery_profile=request.file_delivery_profile,
            delivery_queue=queue,
            autonomy_run_context=request.runtime_context,
        )

    return AutonomyRunner(
        sessions=sessions,
        source_snapshot_loader=source_loader,
        executor_factory=executor_factory,
        story_project_root=story_root,
        run_dir=story_runtime_paths.run_dir,
        persistence_dir=story_runtime_paths.persistence_dir,
        publication_root_map=publication_roots,
        delivery_queue=queue,
        operator_delivery_roots=operator_roots,
        deterministic_stages=("polish",),
    )


def _load_operator_root_map(path: str | Path) -> dict[str, Path]:
    payload = load_json_object(path)
    if set(payload) != {"schema_version", "roots"} or payload.get(
        "schema_version"
    ) != "1.0" or not isinstance(payload.get("roots"), Mapping):
        raise AutonomyContractError(
            "autonomy_operator_root_map_invalid",
            "operator root map must be a v1.0 object containing roots",
        )
    result: dict[str, Path] = {}
    for raw_uuid, raw_path in payload["roots"].items():
        root_uuid = str(raw_uuid)
        try:
            parsed = uuid.UUID(root_uuid)
        except ValueError as exc:
            raise AutonomyContractError(
                "autonomy_operator_root_map_invalid",
                "operator root map keys must be canonical UUIDs",
            ) from exc
        if str(parsed) != root_uuid or not isinstance(raw_path, str) or not raw_path:
            raise AutonomyContractError(
                "autonomy_operator_root_map_invalid",
                "operator root binding is malformed",
            )
        result[root_uuid] = Path(raw_path).resolve()
    if not result:
        raise AutonomyContractError(
            "autonomy_operator_root_map_invalid", "operator root map is empty"
        )
    return result


def _assert_supported_cli_provider(plan: Mapping[str, Any]) -> None:
    provider = (plan.get("selections") or {}).get("provider_model") or {}
    if provider.get("provider") != "openai":
        raise AutonomyContractError(
            "autonomy_provider_unsupported",
            "current chapter generation runtime supports trusted OpenAI profiles only",
        )


def _assert_cli_execution_environment(
    args: argparse.Namespace, plan: Mapping[str, Any]
) -> None:
    _assert_supported_cli_provider(plan)
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run and not bool(getattr(args, "persist_dry_run", False)):
        raise AutonomyContractError(
            "autonomy_dry_run_persistence_unapproved",
            "synthetic autonomy prose requires explicit --persist-dry-run",
        )
    if dry_run:
        return
    provider = plan["selections"]["provider_model"]
    config = get_config()
    if config.openai_model != provider["model"]:
        raise AutonomyContractError(
            "autonomy_provider_model_mismatch",
            "configured OpenAI model differs from the trusted profile",
        )
    if int(config.openai_max_output_tokens) > int(provider["max_output_tokens"]):
        raise AutonomyContractError(
            "autonomy_provider_budget_mismatch",
            "configured provider output limit exceeds the trusted profile",
        )
    actual_endpoint = "openai_compatible" if config.openai_base_url else "official"
    if actual_endpoint != provider["endpoint_type"]:
        raise AutonomyContractError(
            "autonomy_provider_endpoint_mismatch",
            "configured endpoint type differs from the trusted profile",
        )


__all__ = [
    "add_autonomy_arguments",
    "autonomy_command_requested",
    "run_autonomy_command",
]
