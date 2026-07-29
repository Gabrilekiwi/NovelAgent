from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from core.engine.persistence import (
    atomic_create_json,
    atomic_create_text,
    atomic_write_text,
    persistence_run_lock,
)
from core.memory_v2.canonical import canonical_json_hash
from core.state.authoritative import validate_authoritative_state_delta
from core.state.roster import normalize_roster_alias


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MIGRATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_TIER = "story_project_standard"
_BASELINE_SOURCE_KIND = "committed_chapter_prose"


class RosterBaselineMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def run_roster_baseline_migration(
    *,
    story_project: str | Path,
    manifest_path: str | Path,
    apply: bool = False,
    legacy_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Preview or apply one hash-pinned aggregate-roster baseline migration."""

    root = Path(story_project).resolve(strict=True)
    if not root.is_dir():
        _fail("story_project_invalid", f"StoryProject root is not a directory: {root}")
    manifest_file = Path(manifest_path).resolve(strict=True)
    manifest_bytes = manifest_file.read_bytes()
    manifest = _json_object(manifest_bytes, label="migration manifest")
    _validate_manifest(manifest, root=root)
    manifest_sha256 = _sha256(manifest_bytes)
    runtime_root = root / ".novelagent" / "runtime"
    migration_root = runtime_root / "migrations" / str(manifest["migration_id"])
    snapshot_path = _project_file(
        root,
        manifest["snapshot"]["path"],
        label="snapshot.path",
    )
    legacy_manifest_file = (
        Path(legacy_manifest_path).resolve(strict=True)
        if legacy_manifest_path is not None
        else None
    )

    if not apply:
        if legacy_manifest_file is not None:
            plan = _prepare_identity_upgrade_plan(
                root=root,
                manifest_file=manifest_file,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                legacy_manifest_file=legacy_manifest_file,
                migration_root=migration_root,
                snapshot_path=snapshot_path,
            )
            return _public_result(plan, mode="preview")
        plan = _prepare_plan(
            root=root,
            manifest_file=manifest_file,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            migration_root=migration_root,
            snapshot_path=snapshot_path,
        )
        return _public_result(plan, mode="preview")

    with persistence_run_lock(runtime_root, state_paths=(snapshot_path,)):
        if legacy_manifest_file is not None:
            plan = _prepare_identity_upgrade_plan(
                root=root,
                manifest_file=manifest_file,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                legacy_manifest_file=legacy_manifest_file,
                migration_root=migration_root,
                snapshot_path=snapshot_path,
            )
            identity_receipt_path = plan["identity_receipt_path"]
            identity_receipt = _build_identity_receipt(plan)
            if identity_receipt_path.exists():
                plan["identity_receipt"] = _validate_identity_receipt(
                    identity_receipt_path,
                    expected=plan,
                    current_snapshot_sha256=plan["current_snapshot_sha256"],
                )
                plan["status"] = "identity_binding_already_upgraded"
            else:
                atomic_create_json(identity_receipt_path, identity_receipt)
                plan["identity_receipt"] = _validate_identity_receipt(
                    identity_receipt_path,
                    expected=plan,
                    current_snapshot_sha256=plan["current_snapshot_sha256"],
                )
                plan["status"] = "identity_binding_upgraded"
            return _public_result(plan, mode="apply")
        plan = _prepare_plan(
            root=root,
            manifest_file=manifest_file,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            migration_root=migration_root,
            snapshot_path=snapshot_path,
        )
        if plan["status"] == "already_applied":
            return _public_result(plan, mode="apply")

        migration_root.mkdir(parents=True, exist_ok=True)
        backup_path = plan["backup_path"]
        before_bytes = plan["before_bytes"]
        if backup_path.exists():
            if _sha256(backup_path.read_bytes()) != plan["before_sha256"]:
                _fail(
                    "migration_backup_conflict",
                    f"Existing migration backup does not match the pinned snapshot: {backup_path}",
                )
        else:
            atomic_create_text(
                backup_path,
                before_bytes.decode("utf-8"),
                encoding="utf-8",
            )
        if _sha256(backup_path.read_bytes()) != plan["before_sha256"]:
            _fail(
                "migration_backup_verification_failed",
                f"Migration backup failed byte-for-byte verification: {backup_path}",
            )

        if plan["status"] == "ready":
            atomic_write_text(
                snapshot_path,
                plan["after_bytes"].decode("utf-8"),
                encoding="utf-8",
            )
        actual_after = _sha256(snapshot_path.read_bytes())
        if actual_after != plan["after_sha256"]:
            _fail(
                "snapshot_atomic_write_verification_failed",
                "Snapshot hash differs after atomic publication.",
            )

        receipt = _build_receipt(
            plan,
            recovered_after_snapshot_write=plan["status"] == "recovery_required",
        )
        receipt_path = plan["receipt_path"]
        if receipt_path.exists():
            _validate_receipt(
                receipt_path,
                expected=plan,
                current_snapshot_sha256=actual_after,
            )
        else:
            atomic_create_json(receipt_path, receipt)
        plan["receipt"] = _validate_receipt(
            receipt_path,
            expected=plan,
            current_snapshot_sha256=actual_after,
        )
        plan["status"] = (
            "receipt_recovered"
            if plan["status"] == "recovery_required"
            else "applied"
        )
        return _public_result(plan, mode="apply")


def _prepare_plan(
    *,
    root: Path,
    manifest_file: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    migration_root: Path,
    snapshot_path: Path,
) -> dict[str, Any]:
    evidence = _verify_evidence(root=root, manifest=manifest)
    before_sha256 = str(manifest["snapshot"]["sha256_before"])
    backup_path = migration_root / f"snapshot.before.{before_sha256}.json"
    receipt_path = migration_root / "receipt.json"
    identity_receipt_path = migration_root / "identity_receipt.json"
    current_bytes = snapshot_path.read_bytes()
    current_sha256 = _sha256(current_bytes)
    common = {
        "root": root,
        "manifest_file": manifest_file,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "migration_root": migration_root,
        "snapshot_path": snapshot_path,
        "backup_path": backup_path,
        "receipt_path": receipt_path,
        "identity_receipt_path": identity_receipt_path,
        "before_sha256": before_sha256,
        "current_snapshot_sha256": current_sha256,
        "evidence": evidence,
    }

    if current_sha256 == before_sha256:
        if receipt_path.exists():
            _fail(
                "migration_state_receipt_conflict",
                "Receipt exists but the snapshot is still at the pre-migration hash.",
            )
        candidate = _build_candidate(
            snapshot_bytes=current_bytes,
            manifest=manifest,
        )
        return {
            **common,
            **candidate,
            "status": "ready",
            "before_bytes": current_bytes,
        }

    if receipt_path.exists():
        if identity_receipt_path.exists():
            identity_receipt = _validate_identity_receipt(
                identity_receipt_path,
                expected=common,
                current_snapshot_sha256=current_sha256,
            )
            if not backup_path.is_file():
                _fail(
                    "migration_backup_missing",
                    "Applied migration identity receipt has no project-scoped snapshot backup.",
                )
            before_bytes = backup_path.read_bytes()
            if _sha256(before_bytes) != before_sha256:
                _fail(
                    "migration_backup_hash_mismatch",
                    "Applied migration backup no longer matches snapshot_sha256_before.",
                )
            candidate = _build_candidate(
                snapshot_bytes=before_bytes,
                manifest=manifest,
            )
            if candidate["after_sha256"] != current_sha256:
                _fail(
                    "migration_receipt_snapshot_mismatch",
                    (
                        "Current snapshot does not equal the identity-bound "
                        "deterministic migration result."
                    ),
                )
            return {
                **common,
                **candidate,
                "status": "already_applied",
                "before_bytes": before_bytes,
                "identity_receipt": identity_receipt,
            }
        receipt = _validate_receipt(
            receipt_path,
            expected=common,
            current_snapshot_sha256=current_sha256,
        )
        if not backup_path.is_file():
            _fail(
                "migration_backup_missing",
                "Applied migration receipt has no project-scoped snapshot backup.",
            )
        before_bytes = backup_path.read_bytes()
        if _sha256(before_bytes) != before_sha256:
            _fail(
                "migration_backup_hash_mismatch",
                "Applied migration backup no longer matches snapshot_sha256_before.",
            )
        candidate = _build_candidate(
            snapshot_bytes=before_bytes,
            manifest=manifest,
        )
        if candidate["after_sha256"] != current_sha256:
            _fail(
                "migration_receipt_snapshot_mismatch",
                "Current snapshot does not equal the deterministic migration result.",
            )
        return {
            **common,
            **candidate,
            "status": "already_applied",
            "before_bytes": before_bytes,
            "receipt": receipt,
        }

    if backup_path.is_file():
        before_bytes = backup_path.read_bytes()
        if _sha256(before_bytes) != before_sha256:
            _fail(
                "migration_backup_hash_mismatch",
                "Recovery backup does not match snapshot_sha256_before.",
            )
        candidate = _build_candidate(
            snapshot_bytes=before_bytes,
            manifest=manifest,
        )
        if candidate["after_sha256"] == current_sha256:
            return {
                **common,
                **candidate,
                "status": "recovery_required",
                "before_bytes": before_bytes,
            }

    _fail(
        "snapshot_precondition_failed",
        (
            f"Snapshot SHA256 is {current_sha256}; expected pre-migration "
            f"{before_sha256}, or a receipted deterministic after hash."
        ),
    )


def _prepare_identity_upgrade_plan(
    *,
    root: Path,
    manifest_file: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    legacy_manifest_file: Path,
    migration_root: Path,
    snapshot_path: Path,
) -> dict[str, Any]:
    """Bind one already-applied legacy migration to the stable project identity.

    The legacy receipt and manifest remain immutable.  The only permitted
    manifest change is adding ``book_id``; a separate, hash-bound identity
    receipt records the upgrade.
    """

    legacy_manifest_bytes = legacy_manifest_file.read_bytes()
    legacy_manifest = _json_object(
        legacy_manifest_bytes,
        label="legacy migration manifest",
    )
    _validate_manifest(
        legacy_manifest,
        root=root,
        allow_legacy_without_book_id=True,
    )
    if legacy_manifest.get("book_id") is not None:
        _fail(
            "identity_upgrade_legacy_manifest_not_legacy",
            "Identity upgrade requires a legacy manifest with no book_id.",
        )
    comparable_manifest = copy.deepcopy(manifest)
    comparable_manifest.pop("book_id", None)
    if comparable_manifest != legacy_manifest:
        _fail(
            "identity_upgrade_manifest_delta_forbidden",
            (
                "Identity upgrade may add book_id only; all legacy manifest "
                "fields must remain identical."
            ),
        )

    evidence = _verify_evidence(root=root, manifest=manifest)
    before_sha256 = str(manifest["snapshot"]["sha256_before"])
    backup_path = migration_root / f"snapshot.before.{before_sha256}.json"
    receipt_path = migration_root / "receipt.json"
    identity_receipt_path = migration_root / "identity_receipt.json"
    if not receipt_path.is_file():
        _fail(
            "identity_upgrade_legacy_receipt_missing",
            "Identity upgrade requires the immutable legacy migration receipt.",
        )
    if not backup_path.is_file():
        _fail(
            "migration_backup_missing",
            "Identity upgrade requires the project-scoped pre-migration snapshot backup.",
        )

    current_bytes = snapshot_path.read_bytes()
    current_sha256 = _sha256(current_bytes)
    if current_sha256 == before_sha256:
        _fail(
            "identity_upgrade_not_applied",
            (
                "The snapshot is still at the pre-migration hash; apply the "
                "identity-bound manifest normally."
            ),
        )
    before_bytes = backup_path.read_bytes()
    if _sha256(before_bytes) != before_sha256:
        _fail(
            "migration_backup_hash_mismatch",
            "Identity upgrade backup no longer matches snapshot_sha256_before.",
        )
    candidate = _build_candidate(
        snapshot_bytes=before_bytes,
        manifest=manifest,
    )
    if candidate["after_sha256"] != current_sha256:
        _fail(
            "identity_upgrade_snapshot_mismatch",
            "Current snapshot is not the deterministic result of the identity-bound manifest.",
        )

    legacy_manifest_sha256 = _sha256(legacy_manifest_bytes)
    legacy_expected = {
        "manifest": legacy_manifest,
        "manifest_sha256": legacy_manifest_sha256,
        "before_sha256": before_sha256,
        "evidence": evidence,
    }
    legacy_receipt = _validate_receipt(
        receipt_path,
        expected=legacy_expected,
        current_snapshot_sha256=current_sha256,
        require_identity=False,
    )
    common = {
        "root": root,
        "manifest_file": manifest_file,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "legacy_manifest_file": legacy_manifest_file,
        "legacy_manifest_sha256": legacy_manifest_sha256,
        "legacy_receipt": legacy_receipt,
        "legacy_receipt_sha256": _sha256(receipt_path.read_bytes()),
        "migration_root": migration_root,
        "snapshot_path": snapshot_path,
        "backup_path": backup_path,
        "receipt_path": receipt_path,
        "identity_receipt_path": identity_receipt_path,
        "before_sha256": before_sha256,
        "current_snapshot_sha256": current_sha256,
        "evidence": evidence,
        "before_bytes": before_bytes,
        **candidate,
    }
    if identity_receipt_path.exists():
        common["identity_receipt"] = _validate_identity_receipt(
            identity_receipt_path,
            expected=common,
            current_snapshot_sha256=current_sha256,
        )
        common["status"] = "identity_binding_already_upgraded"
    else:
        common["status"] = "identity_binding_upgrade_ready"
    return common


def _build_candidate(
    *,
    snapshot_bytes: bytes,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    snapshot = _json_object(snapshot_bytes, label="snapshot")
    manifest_book_id = str(manifest["book_id"])
    snapshot_book_id = snapshot.get("book_id")
    if (
        not isinstance(snapshot_book_id, str)
        or not snapshot_book_id.strip()
        or snapshot_book_id != manifest_book_id
    ):
        _fail(
            "snapshot_book_id_mismatch",
            (
                f"Snapshot book_id {snapshot_book_id!r} does not exactly match "
                f"manifest book_id {manifest_book_id!r}."
            ),
        )
    authoritative = snapshot.get("authoritative_state")
    if not isinstance(authoritative, dict):
        _fail(
            "snapshot_authoritative_state_missing",
            "Snapshot has no authoritative_state object.",
        )
    existing_roster = authoritative.get("roster")
    if not isinstance(existing_roster, dict):
        _fail("snapshot_roster_invalid", "authoritative_state.roster must be an object.")
    if existing_roster:
        _fail(
            "snapshot_roster_not_empty",
            "Baseline migration requires the pinned Chapter 17 roster ledger to be empty.",
        )

    evidence_by_id = {
        str(item["evidence_id"]): copy.deepcopy(item)
        for item in manifest["committed_prose"]["required_exact_evidence"]
    }
    roster_changes: list[dict[str, Any]] = []
    for item in manifest["rosters"]:
        selected_evidence = [
            evidence_by_id[str(evidence_id)]
            for evidence_id in item["evidence_ids"]
        ]
        roster_changes.append(
            {
                "roster_id": item["roster_id"],
                "name": item["name"],
                "aliases": copy.deepcopy(item.get("aliases") or []),
                "operation": "replace",
                "member_ids": [],
                "members": [],
                "unresolved_before": 0,
                "unresolved_count": int(item["unresolved_count"]),
                "delta": int(item["unresolved_count"]),
                "declared_count": int(item["unresolved_count"]),
                "baseline_evidence": {
                    "source_kind": _BASELINE_SOURCE_KIND,
                    "source_path": manifest["committed_prose"]["path"],
                    "sha256": manifest["committed_prose"]["sha256"],
                    "exact_evidence": selected_evidence,
                },
                "introduced_chapter": int(manifest["chapter_index"]),
                "introduced_event_id": item["introduced_event_id"],
                "baseline_source": manifest["committed_run"]["path"],
                "migration_id": manifest["migration_id"],
            }
        )
    state_delta = {
        "source_tier": _SOURCE_TIER,
        "roster_changes": roster_changes,
        "events": [],
    }
    report = validate_authoritative_state_delta(
        base_state=authoritative,
        state_delta=state_delta,
        chapter_text="",
    )
    if not report.get("accepted"):
        codes = ", ".join(
            str(item.get("code") or "unknown")
            for item in report.get("findings") or []
        )
        _fail(
            "authoritative_roster_delta_rejected",
            f"validate_authoritative_state_delta rejected the migration: {codes}",
        )
    validated_rosters = report["state_after"]["roster"]
    _verify_result_rosters(validated_rosters, manifest=manifest)

    candidate = copy.deepcopy(snapshot)
    candidate["authoritative_state"]["roster"] = copy.deepcopy(validated_rosters)
    after_bytes = (
        json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    return {
        "after_bytes": after_bytes,
        "after_sha256": _sha256(after_bytes),
        "state_delta": state_delta,
        "validation": {
            "accepted": True,
            "finding_count": 0,
            "source_tier": _SOURCE_TIER,
        },
        "rosters_after": copy.deepcopy(validated_rosters),
    }


def _verify_evidence(
    *,
    root: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    run_spec = manifest["committed_run"]
    prose_spec = manifest["committed_prose"]
    run_path = _project_file(root, run_spec["path"], label="committed_run.path")
    prose_path = _project_file(
        root,
        prose_spec["path"],
        label="committed_prose.path",
    )
    run_bytes = run_path.read_bytes()
    prose_bytes = prose_path.read_bytes()
    run_sha256 = _sha256(run_bytes)
    prose_sha256 = _sha256(prose_bytes)
    if run_sha256 != run_spec["sha256"]:
        _fail(
            "committed_run_hash_mismatch",
            f"Committed run SHA256 is {run_sha256}, expected {run_spec['sha256']}.",
        )
    if prose_sha256 != prose_spec["sha256"]:
        _fail(
            "committed_prose_hash_mismatch",
            f"Committed prose SHA256 is {prose_sha256}, expected {prose_spec['sha256']}.",
        )

    prose_lines = prose_bytes.decode("utf-8-sig").splitlines()
    evidence_results: list[dict[str, Any]] = []
    for item in prose_spec["required_exact_evidence"]:
        line_number = int(item["line_number"])
        actual = (
            prose_lines[line_number - 1]
            if 0 < line_number <= len(prose_lines)
            else None
        )
        if actual != item["text"]:
            _fail(
                "committed_prose_evidence_mismatch",
                (
                    f"Exact evidence {item['evidence_id']} does not match "
                    f"{prose_spec['path']}:{line_number}."
                ),
            )
        evidence_results.append(
            {
                "evidence_id": item["evidence_id"],
                "line_number": line_number,
                "matched": True,
            }
        )

    run_payload = _json_object(run_bytes, label="committed run")
    run = run_payload.get("run")
    if not isinstance(run, dict):
        _fail("committed_run_invalid", "Committed run has no nested run object.")
    story_project = run.get("story_project")
    project_identity = (
        story_project.get("project_identity")
        if isinstance(story_project, dict)
        else None
    )
    manifest_book_id = str(manifest["book_id"])
    run_book_id = (
        story_project.get("book_id")
        if isinstance(story_project, dict)
        else None
    )
    project_identity_book_id = (
        project_identity.get("book_id")
        if isinstance(project_identity, dict)
        else None
    )
    if (
        not isinstance(run_book_id, str)
        or not run_book_id.strip()
        or not isinstance(project_identity_book_id, str)
        or not project_identity_book_id.strip()
        or run_book_id != manifest_book_id
        or project_identity_book_id != manifest_book_id
    ):
        _fail(
            "committed_run_book_id_mismatch",
            (
                "Committed run story_project.book_id and "
                "story_project.project_identity.book_id must both be non-empty "
                f"and exactly equal manifest book_id {manifest_book_id!r}; got "
                f"{run_book_id!r} and {project_identity_book_id!r}."
            ),
        )
    if (
        run.get("id") != run_spec["run_id"]
        or run.get("status") != "committed"
        or run.get("committed") is not True
        or run.get("chapter_index") != manifest["chapter_index"]
        or run_payload.get("committed") is not True
    ):
        _fail(
            "committed_run_state_mismatch",
            "Pinned run is not the committed Chapter 17 run declared by the manifest.",
        )

    committed_hash = _committed_prose_target_hash(
        run_payload,
        expected_path=prose_spec["path"],
    )
    final_gate_hash = _final_gate_hash(run_payload)
    writeback_hash = _writeback_hash(run)
    hashes = {
        "committed_prose_target_sha256": committed_hash,
        "final_gate_sha256": final_gate_hash,
        "writeback_sha256": writeback_hash,
    }
    if set(hashes.values()) != {prose_sha256}:
        _fail(
            "committed_artifact_integrity_mismatch",
            f"Committed/final-gate/writeback hashes do not all equal {prose_sha256}.",
        )

    event_ids = _committed_event_ids(run_payload)
    for roster in manifest["rosters"]:
        event_id = str(roster["introduced_event_id"])
        if event_id not in event_ids:
            _fail(
                "introduced_event_missing",
                f"Roster {roster['roster_id']} references absent event {event_id}.",
            )
        _verify_roster_evidence_binding(
            roster,
            evidence_by_id={
                str(item["evidence_id"]): item
                for item in prose_spec["required_exact_evidence"]
            },
        )
    return {
        "book_id": manifest_book_id,
        "run_story_project_book_id": run_book_id,
        "run_project_identity_book_id": project_identity_book_id,
        "run_path": run_spec["path"],
        "run_sha256": run_sha256,
        "prose_path": prose_spec["path"],
        "prose_sha256": prose_sha256,
        "integrity_hashes": hashes,
        "exact_evidence": evidence_results,
        "introduced_event_ids": sorted(
            {str(item["introduced_event_id"]) for item in manifest["rosters"]}
        ),
    }


def _committed_prose_target_hash(
    run_payload: dict[str, Any],
    *,
    expected_path: str,
) -> str:
    persistence = run_payload.get("persistence")
    targets = persistence.get("targets") if isinstance(persistence, dict) else None
    matches = [
        item
        for item in targets or []
        if isinstance(item, dict)
        and item.get("kind") == "prose"
        and _path_suffix_matches(item.get("path"), expected_path)
    ]
    if (
        len(matches) != 1
        or matches[0].get("status") != "verified"
        or not _digest(matches[0].get("after_sha256"))
    ):
        _fail(
            "committed_prose_target_missing",
            "Committed persistence record has no unique verified prose target.",
        )
    return str(matches[0]["after_sha256"])


def _final_gate_hash(run_payload: dict[str, Any]) -> str:
    validation = run_payload.get("validation")
    checks = validation.get("checks") if isinstance(validation, dict) else None
    matches = [
        item
        for item in checks or []
        if isinstance(item, dict)
        and item.get("name") == "final_artifact_integrity"
        and item.get("stage") == "final_gate"
    ]
    if (
        len(matches) != 1
        or matches[0].get("ok") is not True
        or not _digest(matches[0].get("artifact_sha256"))
    ):
        _fail(
            "final_gate_evidence_missing",
            "Committed run has no passing final_artifact_integrity gate.",
        )
    return str(matches[0]["artifact_sha256"])


def _writeback_hash(run: dict[str, Any]) -> str:
    story_project = run.get("story_project")
    writeback = (
        story_project.get("writeback")
        if isinstance(story_project, dict)
        else None
    )
    if (
        not isinstance(writeback, dict)
        or writeback.get("applied") is not True
        or not _digest(writeback.get("writeback_artifact_sha256"))
        or writeback.get("final_artifact_sha256")
        != writeback.get("writeback_artifact_sha256")
    ):
        _fail(
            "writeback_evidence_missing",
            "Committed run has no matching applied StoryProject writeback hash.",
        )
    transaction = writeback.get("transaction")
    if not isinstance(transaction, dict) or transaction.get("committed") is not True:
        _fail(
            "writeback_transaction_not_committed",
            "StoryProject writeback transaction is not committed.",
        )
    return str(writeback["writeback_artifact_sha256"])


def _committed_event_ids(run_payload: dict[str, Any]) -> set[str]:
    run = run_payload.get("run")
    analysis = run_payload.get("analysis")
    if not isinstance(analysis, dict) and isinstance(run, dict):
        analysis = run.get("analysis")
    delta = (
        analysis.get("authoritative_state_delta")
        if isinstance(analysis, dict)
        else None
    )
    events = delta.get("events") if isinstance(delta, dict) else None
    ids = {
        str(item.get("event_id"))
        for item in events or []
        if isinstance(item, dict) and str(item.get("event_id") or "").strip()
    }
    snapshot = run_payload.get("snapshot")
    authority = (
        snapshot.get("authoritative_state")
        if isinstance(snapshot, dict)
        else None
    )
    snapshot_events = (
        authority.get("events")
        if isinstance(authority, dict)
        else None
    )
    if isinstance(snapshot_events, dict):
        ids.intersection_update(str(key) for key in snapshot_events)
    return ids


def _verify_result_rosters(
    rosters: Any,
    *,
    manifest: dict[str, Any],
) -> None:
    if not isinstance(rosters, dict) or len(rosters) != len(manifest["rosters"]):
        _fail(
            "roster_result_count_mismatch",
            "Migration did not produce exactly the manifest-declared rosters.",
        )
    for expected in manifest["rosters"]:
        roster_id = expected["roster_id"]
        actual = rosters.get(roster_id)
        if not isinstance(actual, dict):
            _fail("roster_result_missing", f"Missing migrated roster {roster_id}.")
        required = {
            "roster_id": roster_id,
            "name": expected["name"],
            "aliases": expected.get("aliases") or [],
            "members": [],
            "unresolved_count": expected["unresolved_count"],
            "declared_count": expected["unresolved_count"],
            "computed_count": expected["unresolved_count"],
            "introduced_chapter": manifest["chapter_index"],
            "introduced_event_id": expected["introduced_event_id"],
            "baseline_source": manifest["committed_run"]["path"],
            "migration_id": manifest["migration_id"],
            "source_tier": _SOURCE_TIER,
        }
        for field, value in required.items():
            if actual.get(field) != value:
                _fail(
                    "roster_result_invalid",
                    f"Roster {roster_id} has invalid {field}.",
                )
        evidence = actual.get("baseline_evidence")
        evidence_by_id = {
            item["evidence_id"]: item
            for item in manifest["committed_prose"]["required_exact_evidence"]
        }
        expected_exact_evidence = [
            evidence_by_id[evidence_id]
            for evidence_id in expected["evidence_ids"]
        ]
        if (
            not isinstance(evidence, dict)
            or evidence.get("source_kind") != _BASELINE_SOURCE_KIND
            or evidence.get("source_path") != manifest["committed_prose"]["path"]
            or evidence.get("sha256") != manifest["committed_prose"]["sha256"]
            or evidence.get("exact_evidence") != expected_exact_evidence
        ):
            _fail(
                "roster_baseline_evidence_invalid",
                f"Roster {roster_id} is not bound to the committed prose artifact.",
            )


def _verify_roster_evidence_binding(
    roster: Mapping[str, Any],
    *,
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    selected = [
        evidence_by_id[str(evidence_id)]
        for evidence_id in roster["evidence_ids"]
    ]
    combined = "\n".join(str(item["text"]) for item in selected)
    normalized_combined = normalize_roster_alias(combined)
    identity_terms = [
        str(roster["name"]),
        *[str(alias) for alias in roster.get("aliases") or []],
    ]
    if not any(
        (normalized := normalize_roster_alias(term))
        and normalized in normalized_combined
        for term in identity_terms
    ):
        _fail(
            "roster_evidence_identity_mismatch",
            (
                f"Exact evidence for roster {roster['roster_id']} does not name "
                "its canonical name or any declared alias."
            ),
        )
    count = int(roster["unresolved_count"])
    count_tokens = {str(count), _chinese_count_token(count)}
    count_pattern = re.compile(
        rf"(?:{'|'.join(re.escape(token) for token in sorted(count_tokens) if token)})"
        r"\s*(?:名|位|个|人)"
    )
    if count_pattern.search(combined) is None:
        _fail(
            "roster_evidence_count_mismatch",
            (
                f"Exact evidence for roster {roster['roster_id']} does not attest "
                f"the manifest count {count} as a person count."
            ),
        )


def _chinese_count_token(value: int) -> str:
    if value < 0 or value > 9999:
        return ""
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    units = ((1000, "千"), (100, "百"), (10, "十"))
    remainder = value
    parts: list[str] = []
    pending_zero = False
    for unit_value, unit_name in units:
        digit, remainder = divmod(remainder, unit_value)
        if digit:
            if pending_zero and parts:
                parts.append("零")
            if not (unit_value == 10 and digit == 1 and not parts):
                parts.append(digits[digit])
            parts.append(unit_name)
            pending_zero = False
        elif parts and remainder:
            pending_zero = True
    if remainder:
        if pending_zero and parts:
            parts.append("零")
        parts.append(digits[remainder])
    return "".join(parts)


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    root: Path,
    allow_legacy_without_book_id: bool = False,
) -> None:
    if manifest.get("schema_version") != "1.0":
        _fail("manifest_schema_unsupported", "Manifest schema_version must be 1.0.")
    migration_id = manifest.get("migration_id")
    if not isinstance(migration_id, str) or _SAFE_MIGRATION_ID.fullmatch(migration_id) is None:
        _fail("manifest_migration_id_invalid", "migration_id is invalid.")
    if manifest.get("story_project_name") != root.name:
        _fail(
            "manifest_story_project_mismatch",
            f"Manifest targets {manifest.get('story_project_name')!r}, not {root.name!r}.",
        )
    book_id = manifest.get("book_id")
    if allow_legacy_without_book_id and book_id is None:
        pass
    elif (
        not isinstance(book_id, str)
        or not book_id.strip()
        or book_id != book_id.strip()
    ):
        _fail(
            "manifest_book_id_invalid",
            (
                "Manifest book_id must be a non-empty stable identifier "
                "without surrounding whitespace."
            ),
        )
    chapter_index = manifest.get("chapter_index")
    if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
        _fail("manifest_chapter_invalid", "chapter_index must be a positive integer.")
    if manifest.get("source_tier") != _SOURCE_TIER:
        _fail(
            "manifest_source_tier_invalid",
            f"source_tier must be {_SOURCE_TIER}.",
        )

    snapshot = _mapping(manifest.get("snapshot"), "snapshot")
    _relative_path(snapshot.get("path"), "snapshot.path")
    _require_digest(snapshot.get("sha256_before"), "snapshot.sha256_before")
    run = _mapping(manifest.get("committed_run"), "committed_run")
    _relative_path(run.get("path"), "committed_run.path")
    _require_digest(run.get("sha256"), "committed_run.sha256")
    if not isinstance(run.get("run_id"), str) or not run["run_id"].strip():
        _fail("manifest_run_id_invalid", "committed_run.run_id is required.")
    prose = _mapping(manifest.get("committed_prose"), "committed_prose")
    _relative_path(prose.get("path"), "committed_prose.path")
    _require_digest(prose.get("sha256"), "committed_prose.sha256")

    evidence = prose.get("required_exact_evidence")
    if not isinstance(evidence, list) or not evidence:
        _fail(
            "manifest_evidence_invalid",
            "committed_prose.required_exact_evidence must be a non-empty array.",
        )
    evidence_ids: set[str] = set()
    for item in evidence:
        record = _mapping(item, "required_exact_evidence item")
        evidence_id = record.get("evidence_id")
        if (
            not isinstance(evidence_id, str)
            or not evidence_id.strip()
            or evidence_id in evidence_ids
        ):
            _fail("manifest_evidence_invalid", "Evidence IDs must be unique strings.")
        evidence_ids.add(evidence_id)
        line_number = record.get("line_number")
        if (
            isinstance(line_number, bool)
            or not isinstance(line_number, int)
            or line_number < 1
            or not isinstance(record.get("text"), str)
            or not record["text"]
        ):
            _fail(
                "manifest_evidence_invalid",
                f"Evidence {evidence_id} requires a line_number and exact text.",
            )

    rosters = manifest.get("rosters")
    expected_count = manifest.get("expected_roster_count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
        or not isinstance(rosters, list)
        or len(rosters) != expected_count
    ):
        _fail(
            "manifest_roster_count_invalid",
            "rosters must contain exactly expected_roster_count entries.",
        )
    roster_ids: set[str] = set()
    for item in rosters:
        roster = _mapping(item, "roster")
        if "members" in roster or "member_ids" in roster:
            _fail(
                "manifest_member_ids_forbidden",
                "Aggregate baseline manifest entries must not declare members or member_ids.",
            )
        roster_id = roster.get("roster_id")
        name = roster.get("name")
        count = roster.get("unresolved_count")
        if (
            not isinstance(roster_id, str)
            or not roster_id.strip()
            or roster_id in roster_ids
            or not isinstance(name, str)
            or not name.strip()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            _fail("manifest_roster_invalid", "Each roster requires a unique ID, name, and positive count.")
        roster_ids.add(roster_id)
        aliases = roster.get("aliases", [])
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip()
            for alias in aliases
        ):
            _fail("manifest_roster_invalid", f"Roster {roster_id} aliases are invalid.")
        normalized_aliases = [
            normalize_roster_alias(alias)
            for alias in aliases
        ]
        if (
            len(aliases) != len(set(aliases))
            or len(normalized_aliases) != len(set(normalized_aliases))
        ):
            _fail(
                "manifest_roster_invalid",
                f"Roster {roster_id} aliases must be semantically unique.",
            )
        if not isinstance(roster.get("introduced_event_id"), str) or not roster[
            "introduced_event_id"
        ].strip():
            _fail(
                "manifest_roster_invalid",
                f"Roster {roster_id} requires introduced_event_id.",
            )
        selected = roster.get("evidence_ids")
        if (
            not isinstance(selected, list)
            or not selected
            or any(
                not isinstance(value, str) or value not in evidence_ids
                for value in selected
            )
            or len(selected) != len(set(selected))
        ):
            _fail(
                "manifest_roster_invalid",
                f"Roster {roster_id} references invalid exact evidence.",
            )


def _build_receipt(
    plan: dict[str, Any],
    *,
    recovered_after_snapshot_write: bool,
) -> dict[str, Any]:
    manifest = plan["manifest"]
    receipt = {
        "schema_version": "1.0",
        "migration_id": manifest["migration_id"],
        "status": "applied",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "recovered_after_snapshot_write": recovered_after_snapshot_write,
        "story_project_name": manifest["story_project_name"],
        "book_id": manifest["book_id"],
        "manifest": {
            "path": str(plan["manifest_file"]),
            "sha256": plan["manifest_sha256"],
        },
        "snapshot": {
            "path": manifest["snapshot"]["path"],
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "backup_path": _relative_to_root(
                plan["root"],
                plan["backup_path"],
            ),
            "backup_sha256": plan["before_sha256"],
        },
        "evidence": copy.deepcopy(plan["evidence"]),
        "roster_baseline": {
            "source_tier": _SOURCE_TIER,
            "introduced_chapter": manifest["chapter_index"],
            "roster_count": len(manifest["rosters"]),
            "rosters": [
                {
                    "roster_id": item["roster_id"],
                    "name": item["name"],
                    "unresolved_count": item["unresolved_count"],
                    "member_ids": [],
                    "introduced_event_id": item["introduced_event_id"],
                }
                for item in manifest["rosters"]
            ],
        },
    }
    receipt["receipt_hash"] = canonical_json_hash(receipt)
    return receipt


def _build_identity_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    manifest = plan["manifest"]
    legacy_receipt = plan["legacy_receipt"]
    receipt = {
        "schema_version": "1.0",
        "receipt_kind": "roster_baseline_identity_binding",
        "migration_id": manifest["migration_id"],
        "status": "applied",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "story_project_name": manifest["story_project_name"],
        "book_id": manifest["book_id"],
        "manifest": {
            "path": str(plan["manifest_file"]),
            "sha256": plan["manifest_sha256"],
        },
        "legacy_manifest": {
            "path": str(plan["legacy_manifest_file"]),
            "sha256": plan["legacy_manifest_sha256"],
        },
        "legacy_receipt": {
            "path": _relative_to_root(plan["root"], plan["receipt_path"]),
            "sha256": plan["legacy_receipt_sha256"],
            "receipt_hash": legacy_receipt["receipt_hash"],
        },
        "snapshot": {
            "path": manifest["snapshot"]["path"],
            "book_id": manifest["book_id"],
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "backup_sha256": plan["before_sha256"],
        },
        "evidence": {
            key: plan["evidence"][key]
            for key in (
                "book_id",
                "run_story_project_book_id",
                "run_project_identity_book_id",
                "run_sha256",
                "prose_sha256",
            )
        },
    }
    receipt["receipt_hash"] = canonical_json_hash(receipt)
    return receipt


def _validate_identity_receipt(
    path: Path,
    *,
    expected: dict[str, Any],
    current_snapshot_sha256: str,
) -> dict[str, Any]:
    receipt = _json_object(path.read_bytes(), label="migration identity receipt")
    receipt_hash = receipt.get("receipt_hash")
    if (
        not _digest(receipt_hash)
        or canonical_json_hash(
            {key: value for key, value in receipt.items() if key != "receipt_hash"}
        )
        != receipt_hash
    ):
        _fail(
            "migration_identity_receipt_hash_mismatch",
            "Migration identity receipt hash is invalid.",
        )

    legacy_receipt_path = expected["receipt_path"]
    legacy_receipt_bytes = legacy_receipt_path.read_bytes()
    legacy_receipt = _json_object(
        legacy_receipt_bytes,
        label="legacy migration receipt",
    )
    legacy_receipt_hash = legacy_receipt.get("receipt_hash")
    if (
        not _digest(legacy_receipt_hash)
        or canonical_json_hash(
            {
                key: value
                for key, value in legacy_receipt.items()
                if key != "receipt_hash"
            }
        )
        != legacy_receipt_hash
    ):
        _fail(
            "migration_receipt_hash_mismatch",
            "Legacy migration receipt hash is invalid.",
        )

    manifest = expected["manifest"]
    manifest_record = receipt.get("manifest")
    legacy_manifest_record = receipt.get("legacy_manifest")
    legacy_receipt_record = receipt.get("legacy_receipt")
    snapshot_record = receipt.get("snapshot")
    evidence_record = receipt.get("evidence")
    legacy_manifest_from_receipt = legacy_receipt.get("manifest")
    legacy_manifest_sha256 = (
        legacy_manifest_record.get("sha256")
        if isinstance(legacy_manifest_record, dict)
        else None
    )
    if (
        receipt.get("receipt_kind") != "roster_baseline_identity_binding"
        or receipt.get("migration_id") != manifest["migration_id"]
        or receipt.get("status") != "applied"
        or receipt.get("story_project_name") != manifest["story_project_name"]
        or receipt.get("book_id") != manifest["book_id"]
        or not isinstance(manifest_record, dict)
        or manifest_record.get("sha256") != expected["manifest_sha256"]
        or not isinstance(legacy_manifest_record, dict)
        or not _digest(legacy_manifest_sha256)
        or not isinstance(legacy_manifest_from_receipt, dict)
        or legacy_manifest_from_receipt.get("sha256") != legacy_manifest_sha256
        or (
            expected.get("legacy_manifest_sha256") is not None
            and legacy_manifest_sha256 != expected["legacy_manifest_sha256"]
        )
        or not isinstance(legacy_receipt_record, dict)
        or legacy_receipt_record.get("sha256") != _sha256(legacy_receipt_bytes)
        or legacy_receipt_record.get("receipt_hash") != legacy_receipt_hash
        or not isinstance(snapshot_record, dict)
        or snapshot_record.get("path") != manifest["snapshot"]["path"]
        or snapshot_record.get("book_id") != manifest["book_id"]
        or snapshot_record.get("before_sha256") != expected["before_sha256"]
        or snapshot_record.get("after_sha256") != current_snapshot_sha256
        or snapshot_record.get("backup_sha256") != expected["before_sha256"]
        or not isinstance(evidence_record, dict)
        or any(
            evidence_record.get(key) != expected["evidence"].get(key)
            for key in (
                "book_id",
                "run_story_project_book_id",
                "run_project_identity_book_id",
                "run_sha256",
                "prose_sha256",
            )
        )
    ):
        _fail(
            "migration_identity_receipt_binding_mismatch",
            (
                "Migration identity receipt does not bind the stable book, "
                "manifests, legacy receipt, evidence, and snapshot."
            ),
        )
    return receipt


def _validate_receipt(
    path: Path,
    *,
    expected: dict[str, Any],
    current_snapshot_sha256: str,
    require_identity: bool = True,
) -> dict[str, Any]:
    receipt = _json_object(path.read_bytes(), label="migration receipt")
    receipt_hash = receipt.get("receipt_hash")
    if (
        not _digest(receipt_hash)
        or canonical_json_hash(
            {key: value for key, value in receipt.items() if key != "receipt_hash"}
        )
        != receipt_hash
    ):
        _fail("migration_receipt_hash_mismatch", "Migration receipt hash is invalid.")
    manifest = expected["manifest"]
    snapshot = receipt.get("snapshot")
    manifest_receipt = receipt.get("manifest")
    evidence = receipt.get("evidence")
    identity_mismatch = require_identity and (
        receipt.get("book_id") != manifest.get("book_id")
        or not isinstance(evidence, dict)
        or evidence.get("book_id") != manifest.get("book_id")
        or evidence.get("run_story_project_book_id") != manifest.get("book_id")
        or evidence.get("run_project_identity_book_id") != manifest.get("book_id")
    )
    if (
        receipt.get("migration_id") != manifest["migration_id"]
        or receipt.get("status") != "applied"
        or identity_mismatch
        or not isinstance(manifest_receipt, dict)
        or manifest_receipt.get("sha256") != expected["manifest_sha256"]
        or not isinstance(snapshot, dict)
        or snapshot.get("before_sha256") != expected["before_sha256"]
        or snapshot.get("after_sha256") != current_snapshot_sha256
        or snapshot.get("backup_sha256") != expected["before_sha256"]
        or not isinstance(evidence, dict)
        or evidence.get("run_sha256") != expected["evidence"]["run_sha256"]
        or evidence.get("prose_sha256") != expected["evidence"]["prose_sha256"]
    ):
        _fail(
            "migration_receipt_binding_mismatch",
            "Migration receipt does not bind the current manifest, evidence, and snapshot.",
        )
    return receipt


def _public_result(plan: dict[str, Any], *, mode: str) -> dict[str, Any]:
    manifest = plan["manifest"]
    return {
        "schema_version": "1.0",
        "mode": mode,
        "status": plan["status"],
        "migration_id": manifest["migration_id"],
        "book_id": manifest["book_id"],
        "story_project": str(plan["root"]),
        "manifest": {
            "path": str(plan["manifest_file"]),
            "sha256": plan["manifest_sha256"],
        },
        "snapshot": {
            "path": manifest["snapshot"]["path"],
            "current_sha256": plan["current_snapshot_sha256"],
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "backup_path": _relative_to_root(plan["root"], plan["backup_path"]),
        },
        "evidence": copy.deepcopy(plan["evidence"]),
        "validation": copy.deepcopy(plan["validation"]),
        "rosters": [
            {
                "roster_id": item["roster_id"],
                "name": item["name"],
                "unresolved_count": item["unresolved_count"],
                "member_ids": [],
                "introduced_event_id": item["introduced_event_id"],
            }
            for item in manifest["rosters"]
        ],
        "receipt_path": _relative_to_root(plan["root"], plan["receipt_path"]),
        "identity_receipt_path": _relative_to_root(
            plan["root"],
            plan["identity_receipt_path"],
        ),
        "writes_performed": mode == "apply"
        and plan["status"]
        in {"applied", "receipt_recovered", "identity_binding_upgraded"},
    }


def _project_file(root: Path, value: Any, *, label: str) -> Path:
    relative = _relative_path(value, label)
    candidate = (root / Path(*PurePosixPath(relative).parts)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError:
        _fail("manifest_path_escape", f"{label} escapes the StoryProject root.")
    if not candidate.is_file():
        _fail("manifest_path_invalid", f"{label} is not a file: {candidate}")
    return candidate


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("manifest_path_invalid", f"{label} must be a relative path.")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        _fail("manifest_path_invalid", f"{label} must stay within the StoryProject.")
    return pure.as_posix()


def _relative_to_root(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root).as_posix()


def _path_suffix_matches(value: Any, expected_relative: str) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.replace("\\", "/")
    expected = expected_relative.replace("\\", "/")
    return normalized == expected or normalized.endswith("/" + expected)


def _json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        _fail("migration_json_invalid", f"{label} is not valid UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        _fail("migration_json_invalid", f"{label} must contain a JSON object.")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("manifest_invalid", f"{label} must be an object.")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not _digest(value):
        _fail("manifest_digest_invalid", f"{label} must be a lowercase SHA256.")
    return str(value)


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fail(code: str, message: str) -> None:
    raise RosterBaselineMigrationError(code, message)


__all__ = [
    "RosterBaselineMigrationError",
    "run_roster_baseline_migration",
]
