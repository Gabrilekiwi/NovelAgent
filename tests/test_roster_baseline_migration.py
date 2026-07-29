from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import uuid

import pytest

from core.memory_v2.canonical import canonical_json_hash
from core.state.authoritative import empty_authoritative_state
from core.state.roster_baseline_migration import (
    RosterBaselineMigrationError,
    run_roster_baseline_migration,
)

BOOK_ID = "book-roster-baseline-fixture"


@pytest.fixture
def short_tmp_path() -> Path:
    root = Path(".tmp") / f"rbm-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, value: dict) -> bytes:
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(
    root: Path,
    *,
    station_evidence: str = "消防站原有八人。",
) -> tuple[Path, bytes]:
    root.mkdir(parents=True)
    prose_relative = "正文/chapter17.md"
    prose_path = root / "正文" / "chapter17.md"
    prose_path.parent.mkdir(parents=True)
    prose_lines = [
        "火种一号十七人全部在门外。",
        station_evidence,
        "四名获救者被单独引进观察间。",
    ]
    prose_bytes = ("\n".join(prose_lines) + "\n").encode("utf-8")
    prose_path.write_bytes(prose_bytes)
    prose_sha256 = _sha256(prose_bytes)

    events = {
        event_id: {
            "event_id": event_id,
            "type": "roster_observed",
            "subjects": [],
            "objects": [],
            "location": "消防站",
            "status": "completed",
        }
        for event_id in ("chapter-0017-beat-001", "chapter-0017-beat-002", "chapter-0017-beat-003")
    }
    authority = empty_authoritative_state()
    authority["events"] = events
    snapshot_relative = ".novelagent/runtime/snapshot.json"
    snapshot_path = root / Path(snapshot_relative)
    snapshot_bytes = _write_json(
        snapshot_path,
        {
            "schema_version": "1.0",
            "book_id": BOOK_ID,
            "chapter_index": 18,
            "authoritative_state": authority,
        },
    )

    run_id = "chapter_17_fixture"
    run_relative = ".novelagent/runtime/runs/chapter17.json"
    run_path = root / Path(run_relative)
    run_payload = {
        "committed": True,
        "run": {
            "id": run_id,
            "status": "committed",
            "committed": True,
            "chapter_index": 17,
            "story_project": {
                "book_id": BOOK_ID,
                "project_identity": {"book_id": BOOK_ID},
                "writeback": {
                    "applied": True,
                    "final_artifact_sha256": prose_sha256,
                    "writeback_artifact_sha256": prose_sha256,
                    "transaction": {"committed": True},
                }
            },
        },
        "analysis": {
            "authoritative_state_delta": {
                "events": list(events.values()),
            }
        },
        "snapshot": {"authoritative_state": {"events": events}},
        "persistence": {
            "targets": [
                {
                    "kind": "prose",
                    "path": prose_relative,
                    "status": "verified",
                    "after_sha256": prose_sha256,
                }
            ]
        },
        "validation": {
            "checks": [
                {
                    "name": "final_artifact_integrity",
                    "stage": "final_gate",
                    "ok": True,
                    "artifact_sha256": prose_sha256,
                }
            ]
        },
    }
    run_bytes = _write_json(run_path, run_payload)

    evidence = [
        {"evidence_id": "fireseed-17", "line_number": 1, "text": prose_lines[0]},
        {"evidence_id": "station-8", "line_number": 2, "text": prose_lines[1]},
        {"evidence_id": "rescued-4", "line_number": 3, "text": prose_lines[2]},
    ]
    manifest = {
        "schema_version": "1.0",
        "migration_id": "chapter17-roster-baseline-fixture",
        "story_project_name": root.name,
        "book_id": BOOK_ID,
        "chapter_index": 17,
        "source_tier": "story_project_standard",
        "snapshot": {
            "path": snapshot_relative,
            "sha256_before": _sha256(snapshot_bytes),
        },
        "committed_run": {
            "path": run_relative,
            "sha256": _sha256(run_bytes),
            "run_id": run_id,
        },
        "committed_prose": {
            "path": prose_relative,
            "sha256": prose_sha256,
            "required_exact_evidence": evidence,
        },
        "expected_roster_count": 3,
        "rosters": [
            {
                "roster_id": "roster_fireseed_one",
                "name": "火种一号",
                "aliases": ["火种队"],
                "unresolved_count": 17,
                "introduced_event_id": "chapter-0017-beat-001",
                "evidence_ids": ["fireseed-17"],
            },
            {
                "roster_id": "roster_fire_station",
                "name": "消防站幸存者",
                "aliases": ["消防站", "站内"],
                "unresolved_count": 8,
                "introduced_event_id": "chapter-0017-beat-002",
                "evidence_ids": ["station-8"],
            },
            {
                "roster_id": "roster_rescued_observation",
                "name": "获救者观察组",
                "aliases": ["获救者", "待检者"],
                "unresolved_count": 4,
                "introduced_event_id": "chapter-0017-beat-003",
                "evidence_ids": ["rescued-4"],
            },
        ],
    }
    manifest_path = root.parent / f"{root.name}.manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path, snapshot_bytes


def test_preview_is_read_only_and_apply_is_backed_up_receipted_and_idempotent(
    short_tmp_path: Path,
) -> None:
    root = short_tmp_path / "story"
    manifest_path, snapshot_before = _fixture(root)
    snapshot_path = root / ".novelagent" / "runtime" / "snapshot.json"

    preview = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
    )

    assert preview["mode"] == "preview"
    assert preview["status"] == "ready"
    assert preview["writes_performed"] is False
    assert snapshot_path.read_bytes() == snapshot_before
    assert not (root / ".novelagent" / "runtime" / "migrations").exists()

    applied = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
        apply=True,
    )
    assert applied["status"] == "applied"
    assert applied["writes_performed"] is True
    after_bytes = snapshot_path.read_bytes()
    after = json.loads(after_bytes)
    assert {
        roster_id: record["computed_count"]
        for roster_id, record in after["authoritative_state"]["roster"].items()
    } == {
        "roster_fireseed_one": 17,
        "roster_fire_station": 8,
        "roster_rescued_observation": 4,
    }
    backup_path = root / Path(applied["snapshot"]["backup_path"])
    receipt_path = root / Path(applied["receipt_path"])
    assert backup_path.read_bytes() == snapshot_before
    assert receipt_path.is_file()

    second = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
        apply=True,
    )
    assert second["status"] == "already_applied"
    assert second["writes_performed"] is False
    assert snapshot_path.read_bytes() == after_bytes


def test_manifest_requires_stable_book_id(short_tmp_path: Path) -> None:
    root = short_tmp_path / "story"
    manifest_path, _ = _fixture(root)
    manifest = _read_json(manifest_path)
    manifest.pop("book_id")
    _write_json(manifest_path, manifest)

    with pytest.raises(RosterBaselineMigrationError) as captured:
        run_roster_baseline_migration(
            story_project=root,
            manifest_path=manifest_path,
        )

    assert captured.value.code == "manifest_book_id_invalid"


@pytest.mark.parametrize(
    ("identity_source", "expected_code"),
    [
        ("snapshot", "snapshot_book_id_mismatch"),
        ("run_story_project", "committed_run_book_id_mismatch"),
        ("run_project_identity", "committed_run_book_id_mismatch"),
    ],
)
def test_cross_book_snapshot_or_run_identity_is_rejected(
    short_tmp_path: Path,
    identity_source: str,
    expected_code: str,
) -> None:
    root = short_tmp_path / "story"
    manifest_path, _ = _fixture(root)
    manifest = _read_json(manifest_path)

    if identity_source == "snapshot":
        snapshot_path = root / Path(manifest["snapshot"]["path"])
        snapshot = _read_json(snapshot_path)
        snapshot["book_id"] = "book-from-another-story"
        snapshot_bytes = _write_json(snapshot_path, snapshot)
        manifest["snapshot"]["sha256_before"] = _sha256(snapshot_bytes)
    else:
        run_path = root / Path(manifest["committed_run"]["path"])
        run_payload = _read_json(run_path)
        story_project = run_payload["run"]["story_project"]
        if identity_source == "run_story_project":
            story_project["book_id"] = "book-from-another-story"
        else:
            story_project["project_identity"]["book_id"] = "book-from-another-story"
        run_bytes = _write_json(run_path, run_payload)
        manifest["committed_run"]["sha256"] = _sha256(run_bytes)
    _write_json(manifest_path, manifest)

    with pytest.raises(RosterBaselineMigrationError) as captured:
        run_roster_baseline_migration(
            story_project=root,
            manifest_path=manifest_path,
        )

    assert captured.value.code == expected_code


def test_explicit_identity_upgrade_preserves_legacy_receipt_and_adds_sidecar(
    short_tmp_path: Path,
) -> None:
    root = short_tmp_path / "story"
    manifest_path, _ = _fixture(root)
    applied = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
        apply=True,
    )
    receipt_path = root / Path(applied["receipt_path"])

    legacy_manifest = _read_json(manifest_path)
    legacy_manifest.pop("book_id")
    legacy_manifest_path = root.parent / "legacy.manifest.json"
    legacy_manifest_bytes = _write_json(legacy_manifest_path, legacy_manifest)

    legacy_receipt = _read_json(receipt_path)
    legacy_receipt.pop("book_id")
    for key in (
        "book_id",
        "run_story_project_book_id",
        "run_project_identity_book_id",
    ):
        legacy_receipt["evidence"].pop(key)
    legacy_receipt["manifest"] = {
        "path": str(legacy_manifest_path.resolve()),
        "sha256": _sha256(legacy_manifest_bytes),
    }
    legacy_receipt.pop("receipt_hash")
    legacy_receipt["receipt_hash"] = canonical_json_hash(legacy_receipt)
    legacy_receipt_bytes = _write_json(receipt_path, legacy_receipt)
    identity_receipt_path = root / Path(applied["identity_receipt_path"])

    preview = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
        legacy_manifest_path=legacy_manifest_path,
    )
    assert preview["status"] == "identity_binding_upgrade_ready"
    assert preview["writes_performed"] is False
    assert not identity_receipt_path.exists()

    upgraded = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
        legacy_manifest_path=legacy_manifest_path,
        apply=True,
    )
    assert upgraded["status"] == "identity_binding_upgraded"
    assert upgraded["writes_performed"] is True
    assert receipt_path.read_bytes() == legacy_receipt_bytes
    identity_receipt = _read_json(identity_receipt_path)
    assert identity_receipt["book_id"] == BOOK_ID
    assert identity_receipt["manifest"]["sha256"] == _sha256(
        manifest_path.read_bytes()
    )
    assert identity_receipt["legacy_manifest"]["sha256"] == _sha256(
        legacy_manifest_bytes
    )

    rerun = run_roster_baseline_migration(
        story_project=root,
        manifest_path=manifest_path,
        apply=True,
    )
    assert rerun["status"] == "already_applied"
    assert rerun["writes_performed"] is False
    assert receipt_path.read_bytes() == legacy_receipt_bytes


def test_pinned_prose_hash_mismatch_is_rejected(short_tmp_path: Path) -> None:
    root = short_tmp_path / "story"
    manifest_path, _ = _fixture(root)
    (root / "正文" / "chapter17.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RosterBaselineMigrationError) as captured:
        run_roster_baseline_migration(
            story_project=root,
            manifest_path=manifest_path,
        )

    assert captured.value.code == "committed_prose_hash_mismatch"


def test_exact_evidence_must_attest_each_17_8_4_person_count(
    short_tmp_path: Path,
) -> None:
    root = short_tmp_path / "story"
    manifest_path, _ = _fixture(root, station_evidence="消防站原有九人。")

    with pytest.raises(RosterBaselineMigrationError) as captured:
        run_roster_baseline_migration(
            story_project=root,
            manifest_path=manifest_path,
        )

    assert captured.value.code == "roster_evidence_count_mismatch"
