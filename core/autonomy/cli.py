from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any, Mapping

from core.autonomy.common import AutonomyContractError, canonical_hash, load_json_object
from core.autonomy.plans import build_source_snapshot, compile_instruction_plan
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.session import AutonomySessionStore
from core.story_project.identity import load_project_identity
from core.story_project.paths import infer_next_chapter


_INCOMPATIBLE_TOP_LEVEL_COMMANDS = (
    ("check", "--check"),
    ("check_json", "--check-json"),
    ("check_memory_v2", "--check-memory-v2"),
    ("report_runs", "--report-runs"),
    ("recover_latest", "--recover-latest"),
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
    profile_bound = commands[0][0] in {"instruction", "execute_plan", "resume_session"}
    if profile_bound and not getattr(args, "trusted_profiles", None):
        raise ValueError(
            "--instruction, --execute-plan, and --resume-session require --trusted-profiles PATH"
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
        story_profile_id = str(
            (plan.get("selections") or {}).get("story_project", {}).get("profile_id") or ""
        )
        status = sessions.execute_plan(
            plan,
            source_snapshot_loader=lambda: _capture_source_snapshot_from_args(
                args, profiles=profiles, story_profile_id=story_profile_id
            ),
        )
        return {"ok": True, "command": "execute_plan", "session": status}
    if command == "session_status":
        return {
            "ok": True,
            "command": "session_status",
            "session": sessions.status(str(value)),
        }
    if command == "resume_session":
        assert profiles is not None
        plan = sessions.load_instruction_plan(str(value))
        story_profile_id = plan["selections"]["story_project"]["profile_id"]
        return {
            "ok": True,
            "command": "resume_session",
            "session": sessions.resume(
                str(value),
                source_snapshot_loader=lambda: _capture_source_snapshot_from_args(
                    args, profiles=profiles, story_profile_id=story_profile_id
                ),
            ),
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
    roots["persistence"] = Path(story_runtime_paths.persistence_dir).resolve()
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


__all__ = [
    "add_autonomy_arguments",
    "autonomy_command_requested",
    "run_autonomy_command",
]
