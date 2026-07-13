from __future__ import annotations

import argparse
from pathlib import Path

from core.review.repair_loop import ReviewRepairConfig, validate_review_repair_config
from core.review.runtime import RuntimeReviewConfig, validate_runtime_review_config
from core.runtime_paths import RuntimePaths
from core.story_project.paths import resolve_story_project_root
from core.story_project.writer import StoryProjectWritebackConfig


def apply_story_project_runtime_defaults(args: argparse.Namespace) -> RuntimePaths | None:
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
    for key, value in (
        ("snapshot", paths.snapshot_path),
        ("run_dir", paths.run_dir),
        ("chapter_dir", paths.chapter_dir),
        ("review_output_dir", paths.review_dir),
        ("persistence_dir", paths.persistence_dir),
        ("delivery_dir", paths.delivery_dir),
        ("memory_v2_out", paths.memory_dir / "v2"),
    ):
        if not explicit.get(key):
            setattr(args, key, str(value))
    if args.memory_writeback == "file" and not explicit.get("memory_outbox"):
        args.memory_outbox = str(paths.memory_dir / "memory_outbox.jsonl")
    args._resolved_story_project_root = root
    args._story_project_runtime_paths = paths
    return paths


def runtime_review_config_from_args(args: argparse.Namespace) -> RuntimeReviewConfig:
    return validate_runtime_review_config(
        RuntimeReviewConfig(
            enabled=bool(args.enable_review_pipeline),
            output_dir=Path(args.review_output_dir) if args.review_output_dir else None,
            rules_path=Path(args.review_rules) if args.review_rules else None,
            use_default_rules=not bool(args.review_no_default_rules),
            build_repair_prompt=not bool(args.review_no_repair_prompt),
            build_human_report=not bool(args.review_no_human_report),
            gate_threshold=str(args.review_gate),
        )
    )


def review_repair_config_from_args(args: argparse.Namespace) -> ReviewRepairConfig:
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


def story_project_writeback_config_from_args(args: argparse.Namespace) -> StoryProjectWritebackConfig:
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
    mode = "apply" if real_writeback else "dry_run" if dry_run_writeback else "none"
    return StoryProjectWritebackConfig(mode=mode, overwrite=bool(getattr(args, "story_project_overwrite", False)))


def validate_story_project_multistep_args(
    args: argparse.Namespace,
    writeback: StoryProjectWritebackConfig,
) -> None:
    if getattr(args, "story_project", None) is None or int(getattr(args, "steps", 1)) <= 1:
        return
    if writeback.mode != "apply":
        raise ValueError("StoryProject --steps > 1 requires --story-project-writeback")
    if bool(getattr(args, "dry_run", False)) or bool(getattr(args, "persist_dry_run", False)):
        raise ValueError("StoryProject multi-step writeback cannot use global dry-run or --persist-dry-run")


__all__ = [
    "apply_story_project_runtime_defaults",
    "review_repair_config_from_args",
    "runtime_review_config_from_args",
    "story_project_writeback_config_from_args",
    "validate_story_project_multistep_args",
]
