from __future__ import annotations

import argparse
import re
import uuid
from pathlib import Path
from typing import Any

from core.engine.project_root_remap import remap_story_project_roots
from core.engine.root_registry import RootRegistryError, RootRegistryService


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def root_remap_command_requested(
    args: argparse.Namespace,
    *,
    delivery_command_requested: bool = False,
    autonomy_command_requested: bool = False,
) -> bool:
    """Validate the dedicated, mutating ``remap-roots`` command surface."""

    enabled = bool(getattr(args, "remap_roots", False))
    entries = list(getattr(args, "remap_root", None) or [])
    expected_revision = getattr(args, "expected_root_registry_revision", None)
    expected_digest = getattr(args, "expected_root_registry_digest", None)

    dependent_options = bool(entries or expected_revision is not None or expected_digest)
    if not enabled:
        if dependent_options:
            raise ValueError(
                "--remap-root and expected registry CAS options require --remap-roots"
            )
        return False

    if not entries:
        raise ValueError(
            "--remap-roots requires at least one "
            "--remap-root ROOT_ID ROOT_UUID NEW_PATH"
        )
    if expected_revision is None or expected_digest is None:
        raise ValueError(
            "--remap-roots requires --expected-root-registry-revision and "
            "--expected-root-registry-digest"
        )
    if not _SHA256_PATTERN.fullmatch(str(expected_digest)):
        raise ValueError(
            "--expected-root-registry-digest must be 64 lowercase hex characters"
        )

    explicit_paths = getattr(args, "_runtime_path_explicit", {})
    if not getattr(args, "persistence_dir", None) or not explicit_paths.get(
        "persistence_dir", False
    ):
        raise ValueError(
            "--remap-roots requires an explicit --persistence-dir pointing to the "
            "existing root-registry control plane"
        )

    if delivery_command_requested or autonomy_command_requested:
        raise ValueError(
            "--remap-roots cannot be combined with delivery or autonomy commands"
        )

    incompatible = {
        "activate_story_state": "--activate-story-state",
        "check": "--check",
        "dry_run": "--dry-run",
        "init_runtime": "--init-runtime",
        "inspect_story_project_runtime_from": "--inspect-story-project-runtime-from",
        "migrate_story_project_runtime_from": "--migrate-story-project-runtime-from",
        "persist_dry_run": "--persist-dry-run",
        "preview_event_authority_migration": "--preview-event-authority-migration",
        "reconcile_persistence": "--reconcile-persistence",
        "recover_latest": "--recover-latest",
        "recover_locked_chapter": "--recover-locked-chapter",
        "report_runs": "--report-runs",
        "review_dashboard": "--review-dashboard",
        "review_latest": "--review-latest",
        "review_list": "--review-list",
        "story_project_compat_report": "--story-project-compat-report",
        "story_project_writeback": "--story-project-writeback",
        "story_project_writeback_dry_run": "--story-project-writeback-dry-run",
        "story_state_shadow_report": "--story-state-shadow-report",
    }
    combined = [
        label for field, label in incompatible.items() if getattr(args, field, False)
    ]
    if combined:
        raise ValueError("--remap-roots cannot be combined with " + ", ".join(combined))
    if bool(getattr(args, "_steps_explicit", False)):
        raise ValueError("--remap-roots cannot be combined with --steps")

    _parse_remap_entries(entries)
    return True


def run_root_remap_command(args: argparse.Namespace) -> dict[str, Any]:
    """Rebind paths after an operator-controlled move; never move data here."""

    requested = _parse_remap_entries(list(args.remap_root or []))
    if "story_project" in requested:
        return remap_story_project_roots(
            new_story_project=requested["story_project"]["path"],
            control_plane=args.persistence_dir,
            requested=requested,
            expected_revision=int(args.expected_root_registry_revision),
            expected_registry_digest=str(args.expected_root_registry_digest),
        )

    service = RootRegistryService(args.persistence_dir)
    before = service.load()

    remaps: dict[str, Path] = {}
    for root_id, request in requested.items():
        binding = before["roots"].get(root_id)
        if not isinstance(binding, dict):
            raise RootRegistryError(f"unknown logical root: {root_id}")
        if binding.get("root_uuid") != request["root_uuid"]:
            raise RootRegistryError(
                f"logical root UUID mismatch for {root_id}: the command must bind "
                "the UUID currently recorded in the registry"
            )
        remaps[root_id] = request["path"]

    after = service.remap_roots(
        remaps,
        expected_revision=int(args.expected_root_registry_revision),
        expected_registry_digest=str(args.expected_root_registry_digest),
    )
    return {
        "ok": True,
        "command": "remap_roots",
        "scope": "registered_data_roots_only",
        "control_plane": str(service.transaction_root),
        "data_moved_or_copied": False,
        "runtime_control_plane_relocation_supported": False,
        "runtime_control_plane_relocated": False,
        "registry": {
            "registry_id": after["registry_id"],
            "previous_revision": before["revision"],
            "revision": after["revision"],
            "registry_digest": after["registry_digest"],
        },
        "remapped_roots": [
            {
                "root_id": root_id,
                "root_uuid": after["roots"][root_id]["root_uuid"],
                "path": after["roots"][root_id]["path"],
            }
            for root_id in sorted(requested)
        ],
        "notice": (
            "Logical bindings were changed after validating the existing control plane. "
            "No files were moved or copied by this command."
        ),
    }


def _parse_remap_entries(entries: list[Any]) -> dict[str, dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError(
                "each --remap-root requires ROOT_ID ROOT_UUID NEW_PATH"
            )
        root_id, raw_uuid, raw_path = (str(item) for item in entry)
        if root_id in parsed:
            raise ValueError(f"duplicate --remap-root logical id: {root_id}")
        if not raw_path.strip():
            raise ValueError(f"new physical path must not be empty for {root_id}")
        try:
            canonical_uuid = str(uuid.UUID(raw_uuid))
        except ValueError as exc:
            raise ValueError(f"invalid logical root UUID for {root_id}") from exc
        if canonical_uuid != raw_uuid:
            raise ValueError(f"logical root UUID must be canonical for {root_id}")
        parsed[root_id] = {
            "root_uuid": canonical_uuid,
            "path": Path(raw_path),
        }
    return parsed


__all__ = ["root_remap_command_requested", "run_root_remap_command"]
