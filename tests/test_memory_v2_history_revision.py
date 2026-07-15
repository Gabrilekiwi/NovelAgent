from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    LEGACY_REDUCER_VERSION,
    HistoricalRevisionError,
    MemoryIntegrityError,
    apply_genesis_event,
    apply_memory_patch,
    capture_historical_revision_dependency_inventory,
    create_empty_canonical_memory,
    create_genesis_memory_batch,
    create_memory_event_batch,
    create_memory_patch,
    prepare_amend_transaction,
    prepare_historical_revision_transaction,
    prepare_import_transaction,
    prepare_retcon_transaction,
    replay_memory_events,
    validate_historical_revision_bundle,
    validate_historical_revision_evidence,
    validate_historical_revision_impact_report,
    validate_historical_revision_invalidation_manifest,
    validate_historical_revision_transaction,
    validate_memory_event_batch,
    write_memory_event_batch,
)
from core.engine.persistence_v2 import PersistenceV2Target
from core.engine.preflight import _check_story_project_runtime_context
from core.engine.root_registry import RootRegistryService
from core.engine.story_project_context import (
    StoryProjectContextError,
    StoryProjectContextService,
)
from core.path_refs import path_ref_for
from core.schema import validate_schema
from core.memory_v2.storage import save_canonical_memory
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.authority import (
    activate_event_authority,
    prepare_event_authority_advance,
    project_identity_sha256,
)
from core.story_project.identity import (
    ensure_project_identity,
    load_project_identity,
    project_identity_path,
)
from core.story_project.mapper import SETTING_DIR_NAME, TRACKING_DIR_NAME
from core.story_project.migration_execution import execute_event_authority_migration
from core.story_project.migration_v2 import build_migration_approval, build_migration_plan
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.read_set import SOURCE_DIRECTORIES, capture_story_project_read_set
from core.runtime_paths import RuntimePaths
from core.autonomy.outline import (
    OutlineCheckpointStore,
    build_outline_checkpoint,
)
from core.autonomy.plans import build_source_snapshot, compile_instruction_plan
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.session import AutonomySessionStore


class HistoricalRevisionTransactionTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "hr" / f"{name[:12]}_{uuid.uuid4().hex[:12]}"
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _sha(value: bytes | str) -> str:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _write_identity(story: Path, identity) -> None:
        project_identity_path(story).write_text(
            json.dumps(identity.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _fixture(self, name: str = "case") -> dict:
        root = self._case_dir(name)
        story = root / "book"
        story.mkdir()
        for directory in SOURCE_DIRECTORIES:
            (story / directory).mkdir()
        prose_dir = story / SOURCE_DIRECTORIES[1]
        identity = ensure_project_identity(story, book_id=f"book-{uuid.uuid4().hex[:12]}")
        memory_root = root / "memory-v2"
        event_store = memory_root / "events"
        genesis = create_genesis_memory_batch(
            book_id=identity.book_id,
            title="History revision fixture",
            source_project_digest="1" * 64,
            context_digest="2" * 64,
            authority_epoch=1,
        )
        projection = apply_genesis_event(genesis["events"][0])
        write_memory_event_batch(event_store, genesis)
        save_canonical_memory(memory_root / "canonical_memory.json", projection)
        identity = activate_event_authority(
            story,
            expected_identity_sha256=project_identity_sha256(story),
            head_event_hash=str(projection["head_event_hash"]),
        )

        prose_paths: dict[int, Path] = {}
        for chapter in range(1, 4):
            path = canonical_prose_path(story, chapter, f"Chapter {chapter}")
            path.write_text(
                f"Chapter {chapter}: Alice carried the red key through gate {chapter}.\n",
                encoding="utf-8",
            )
            prose_paths[chapter] = path

        previous_batch_hash = str(genesis["batch_hash"])
        for chapter in range(1, 4):
            body = prose_paths[chapter].read_text(encoding="utf-8")
            patch = create_memory_patch(
                patch_id=f"chapter-{chapter}",
                source_kind="committed_chapter",
                source_path=f"chapter:{chapter}",
                operations=[
                    {
                        "op": "update_current_state",
                        "value": {
                            "chapter_index": chapter + 1,
                            "last_committed_chapter_index": chapter,
                        },
                    },
                    {
                        "op": "update_world",
                        "value": {f"chapter_{chapter}_fact": f"red-key-{chapter}"},
                    }
                ],
            )
            updated, events = apply_memory_patch(
                projection,
                patch,
                reducer_version=CURRENT_REDUCER_VERSION,
                event_context={
                    "chapter_body": body,
                    "evidence_spans": [
                        {"start_char": 0, "end_char": len(body), "quote": body}
                    ],
                    "authority_epoch": 1,
                },
            )
            batch = create_memory_event_batch(
                book_id=identity.book_id,
                patch=patch,
                events=events,
                expected_revision=int(projection["revision"]),
                previous_batch_hash=previous_batch_hash,
                source_project_digest=str(chapter) * 64,
                context_digest=str(chapter + 3) * 64,
                batch_kind="chapter",
                publication_status="committed",
                schema_version="2.2",
                reducer_version=CURRENT_REDUCER_VERSION,
            )
            write_memory_event_batch(event_store, batch)
            save_canonical_memory(memory_root / "canonical_memory.json", updated)
            identity = prepare_event_authority_advance(
                identity,
                expected_authority_epoch=1,
                expected_head_event_hash=str(projection["head_event_hash"]),
                new_head_event_hash=str(updated["head_event_hash"]),
            )
            self._write_identity(story, identity)
            projection = updated
            previous_batch_hash = str(batch["batch_hash"])

        revision_source = root / "revision-evidence.txt"
        revision_text = "Official correction: Alice carried the blue key through gate one."
        revision_source.write_text(revision_text, encoding="utf-8")
        alice_start = revision_text.index("Alice")
        return {
            "root": root,
            "story": story,
            "memory_root": memory_root,
            "event_store": event_store,
            "identity": identity,
            "projection": projection,
            "prose": prose_paths,
            "revision_source": revision_source,
            "evidence_spans": [
                {"start_char": alice_start, "end_char": alice_start + 5, "quote": "Alice"}
            ],
            "root_uuid": str(uuid.uuid4()),
        }

    def _kwargs(self, fixture: dict, *, transaction_id: str = "revision-001") -> dict:
        historical = fixture["prose"][1]
        revision_source = fixture["revision_source"]
        projection = fixture["projection"]
        identity = load_project_identity(fixture["story"])
        read_set = capture_story_project_read_set(
            fixture["story"],
            int(projection["current_state"]["chapter_index"]),
            project_identity=identity,
        )
        dependency_inventory = capture_historical_revision_dependency_inventory(
            story_project_root=fixture["story"],
            book_id=identity.book_id,
            authority_epoch=1,
            head_event_hash=projection["head_event_hash"],
            historical_chapter_index=1,
            canonical_next_chapter_index=int(
                projection["current_state"]["chapter_index"]
            ),
        )
        return {
            "memory_root": fixture["memory_root"],
            "story_project_root": fixture["story"],
            "story_project_root_uuid": fixture["root_uuid"],
            "transaction_id": transaction_id,
            "historical_chapter_index": 1,
            "historical_chapter_path": historical,
            "expected_historical_chapter_sha256": self._sha(historical.read_bytes()),
            "revision_source_path": revision_source,
            "expected_revision_source_sha256": self._sha(revision_source.read_bytes()),
            "evidence_spans": fixture["evidence_spans"],
            "operations": [
                {"op": "update_world", "value": {"chapter_1_fact": "blue-key-1"}}
            ],
            "authority_epoch": 1,
            "expected_head_event_hash": projection["head_event_hash"],
            "expected_revision": projection["revision"],
            "source_project_digest": read_set["membership_fingerprint"],
            "context_digest": read_set["context_digest"],
            "dependency_inventory": dependency_inventory,
        }

    def _install_autonomy_dependencies(self, fixture: dict) -> dict[str, str]:
        identity = load_project_identity(fixture["story"])
        projection = fixture["projection"]
        profiles = TrustedProfiles.from_dict(
            {
                "schema_version": "1.0",
                "profile_set_id": "history-revision-profiles",
                "story_projects": [
                    {
                        "profile_id": "active-book",
                        "book_id": identity.book_id,
                        "root_uuid": fixture["root_uuid"],
                    }
                ],
                "provider_models": [
                    {
                        "profile_id": "deterministic",
                        "provider": "openai",
                        "model": "test-model",
                        "max_output_tokens": 16000,
                    }
                ],
                "file_deliveries": [
                    {
                        "profile_id": "local-export",
                        "target_kind": "file",
                        "root_uuid": "11111111-1111-4111-8111-111111111111",
                        "path_template": "exports/chapter-{chapter_index}-{run_id}.json",
                        "requires_run_id": True,
                        "requires_chapter_id": True,
                    }
                ],
                "budgets": [
                    {
                        "profile_id": "bounded",
                        "max_chapters": 3,
                        "max_model_calls": 48,
                        "max_input_tokens": 500000,
                        "max_output_tokens": 200000,
                        "max_wall_seconds": 3600,
                    }
                ],
                "quality_policies": [
                    {
                        "profile_id": "strict-local",
                        "policy": "strict",
                        "minimum_score": 0,
                    }
                ],
                "defaults": {
                    "story_project": "active-book",
                    "provider_model": "deterministic",
                    "file_delivery": "local-export",
                    "budget": "bounded",
                    "quality_policy": "strict-local",
                },
            }
        )
        source = build_source_snapshot(
            book_id=identity.book_id,
            root_uuid=fixture["root_uuid"],
            authority_epoch=1,
            authority_head_event_hash=projection["head_event_hash"],
            canonical_next_chapter=int(projection["current_state"]["chapter_index"]),
            source_digest="9" * 64,
            captured_at="2026-07-14T00:00:00+00:00",
        )
        plan = compile_instruction_plan(
            "write 3 chapters",
            trusted_profiles=profiles,
            source_snapshot=source,
            created_at="2026-07-14T00:00:00+00:00",
        )
        autonomy_root = RuntimePaths.for_story_project(fixture["story"]).runtime_dir / "autonomy"
        store = AutonomySessionStore(autonomy_root, trusted_profiles=profiles)
        status = store.execute_plan(
            plan,
            source_snapshot_loader=lambda: source,
            at="2026-07-14T00:00:00+00:00",
        )
        genesis = json.loads(
            (
                autonomy_root
                / "sessions"
                / status["session_id"]
                / "genesis.json"
            ).read_text(encoding="utf-8")
        )
        chapter = int(plan["chapter_start"])
        checkpoint = build_outline_checkpoint(
            book_id=identity.book_id,
            session_id=status["session_id"],
            plan_id=plan["plan_id"],
            arc_plan_id=genesis["arc_plan_id"],
            chapter_index=chapter,
            planned_target_hash="7" * 64,
            source_snapshot_hash=source["snapshot_hash"],
            authority_epoch=1,
            authority_head_event_hash=projection["head_event_hash"],
            outline_input_digest="8" * 64,
            provider_profile="deterministic",
            execution_kind="deterministic",
            outline_text="A durable downstream outline checkpoint.",
            canonical_relative_path=canonical_outline_path(
                fixture["story"], chapter
            ).relative_to(fixture["story"]).as_posix(),
            canonical_before_sha256=None,
            created_at="2026-07-14T00:01:00+00:00",
        )
        OutlineCheckpointStore(autonomy_root).create(
            checkpoint, invalidated_at="2026-07-14T00:01:00+00:00"
        )
        result = {
            "session_id": status["session_id"],
            "outline_id": checkpoint["checkpoint_id"],
        }
        fixture["autonomy_dependencies"] = result
        return result

    @staticmethod
    def _apply_targets(prepared: dict) -> None:
        for target in prepared["targets"]:
            path = Path(target["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(target["content"].encode("utf-8"))

    def test_all_revision_kinds_prepare_immutable_non_prose_transactions(self) -> None:
        cases = (
            ("amend", prepare_amend_transaction, "amended"),
            ("import", prepare_import_transaction, "imported"),
            ("retcon", prepare_retcon_transaction, "retconned"),
        )
        for kind, prepare, status in cases:
            with self.subTest(kind=kind):
                fixture = self._fixture(kind)
                prepared = prepare(**self._kwargs(fixture, transaction_id=f"{kind}-001"))

                self.assertEqual("prepared", prepared["status"])
                self.assertEqual(kind, prepared["batch"]["batch_kind"])
                self.assertEqual(status, prepared["batch"]["publication_status"])
                self.assertTrue(all(event["source"]["kind"] == kind for event in prepared["batch"]["events"]))
                for event in prepared["batch"]["events"]:
                    self.assertIn("before", event)
                    self.assertIn("after", event)
                    self.assertEqual(
                        event["precondition"]["expected_field_hash"],
                        hashlib.sha256(
                            json.dumps(
                                event["before"],
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        prepared["evidence"]["revision_evidence_text_sha256"],
                        event["chapter_body_sha256"],
                    )
                    self.assertTrue(event["evidence_spans"])
                self.assertTrue(
                    all(
                        event["metadata"]["operation_data"]["historical_revision"]["revision_kind"]
                        == kind
                        for event in prepared["batch"]["events"]
                    )
                )
                self.assertNotIn("prose", {target["kind"] for target in prepared["targets"]})
                self.assertNotIn(
                    fixture["prose"][1].resolve(),
                    {Path(target["path"]).resolve() for target in prepared["targets"]},
                )
                self.assertIs(
                    prepared["evidence"],
                    validate_historical_revision_evidence(prepared["evidence"]),
                )
                self.assertIs(
                    prepared["invalidation_manifest"],
                    validate_historical_revision_invalidation_manifest(
                        prepared["invalidation_manifest"]
                    ),
                )
                self.assertIs(
                    prepared["impact_report"],
                    validate_historical_revision_impact_report(prepared["impact_report"]),
                )
                self.assertIs(
                    prepared["transaction"],
                    validate_historical_revision_transaction(prepared["transaction"]),
                )
                self.assertIs(prepared, validate_historical_revision_bundle(prepared))
                declaration = prepared["read_set_declared_writes"]
                self.assertEqual([".novelagent/project.json"], [item["relative_path"] for item in declaration])
                self.assertEqual(
                    prepared["projection"]["head_event_hash"],
                    prepared["story_project_source_revision_after"]["head_event_hash"],
                )

    def test_downstream_event_outline_and_active_session_impacts_are_persisted(self) -> None:
        fixture = self._fixture("impact")
        dependencies = self._install_autonomy_dependencies(fixture)
        prepared = prepare_retcon_transaction(**self._kwargs(fixture))
        manifest = prepared["invalidation_manifest"]
        report = prepared["impact_report"]

        self.assertEqual(2, len(manifest["event_invalidations"]))
        self.assertEqual(
            ["chapter", "chapter"],
            [item["batch_kind"] for item in manifest["event_invalidations"]],
        )
        self.assertEqual([dependencies["outline_id"]], [item["outline_id"] for item in manifest["outline_invalidations"]])
        self.assertEqual([dependencies["session_id"]], [item["session_id"] for item in manifest["session_invalidations"]])
        canonical_marker = prepared["projection"]["current_state"]["historical_revision"]
        self.assertTrue(canonical_marker["requires_downstream_reconciliation"])
        self.assertEqual(
            [item["batch_id"] for item in manifest["event_invalidations"]],
            canonical_marker["invalidated_event_batch_ids"],
        )
        self.assertEqual([dependencies["outline_id"]], canonical_marker["invalidated_outline_ids"])
        self.assertEqual([dependencies["session_id"]], canonical_marker["invalidated_session_ids"])
        self.assertEqual(
            {"event_count": 2, "outline_count": 1, "session_count": 1},
            {key: report["summary"][key] for key in ("event_count", "outline_count", "session_count")},
        )
        target_kinds = {target["kind"] for target in prepared["targets"]}
        self.assertIn("historical_revision_invalidation_manifest", target_kinds)
        self.assertIn("historical_revision_impact_report", target_kinds)

    def test_dependency_inventory_is_recaptured_and_cannot_omit_or_forge_artifacts(self) -> None:
        fixture = self._fixture("inventory")
        self._install_autonomy_dependencies(fixture)
        kwargs = self._kwargs(fixture)

        missing = copy.deepcopy(kwargs)
        missing_inventory = missing["dependency_inventory"]
        missing_inventory["outline_dependencies"] = []
        missing_inventory["inventory_hash"] = canonical_json_hash(
            missing_inventory, exclude_fields=("inventory_hash",)
        )
        with self.assertRaisesRegex(
            HistoricalRevisionError, "historical_revision_dependency_inventory_mismatch"
        ):
            prepare_retcon_transaction(**missing)

        forged = copy.deepcopy(kwargs)
        forged_inventory = forged["dependency_inventory"]
        forged_inventory["session_dependencies"][0]["artifact_sha256"] = "0" * 64
        forged_inventory["inventory_hash"] = canonical_json_hash(
            forged_inventory, exclude_fields=("inventory_hash",)
        )
        with self.assertRaisesRegex(
            HistoricalRevisionError, "historical_revision_dependency_inventory_mismatch"
        ):
            prepare_retcon_transaction(**forged)

        autonomy_root = RuntimePaths.for_story_project(fixture["story"]).runtime_dir / "autonomy"
        events_root = (
            autonomy_root
            / "sessions"
            / fixture["autonomy_dependencies"]["session_id"]
            / "events"
        )
        event_path = next(events_root.glob("*.json"))
        event_path.rename(events_root / "000001-forged-filename.json")
        with self.assertRaisesRegex(
            HistoricalRevisionError,
            "session event filename does not bind its immutable event hash",
        ):
            self._kwargs(fixture)

    def test_dependency_inventory_rejects_linked_or_reparse_autonomy_root(self) -> None:
        fixture = self._fixture("inventory-link")
        outside = fixture["root"] / "outside-autonomy"
        outside.mkdir()
        autonomy_root = RuntimePaths.for_story_project(fixture["story"]).runtime_dir / "autonomy"
        autonomy_root.parent.mkdir(parents=True, exist_ok=True)
        try:
            autonomy_root.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are not available in this Windows environment")
        with self.assertRaisesRegex(
            HistoricalRevisionError,
            "historical_revision_dependency_inventory_invalid",
        ):
            self._kwargs(fixture)

    def test_runtime_and_preflight_block_until_complete_reconciliation(self) -> None:
        fixture = self._fixture("reconcile")
        dependencies = self._install_autonomy_dependencies(fixture)
        first = prepare_retcon_transaction(**self._kwargs(fixture, transaction_id="revision-001"))
        blocked_context = {
            "story_state_mode": "strict",
            "project_identity": first["project_identity_after"],
            "memory_v2": {"projection": first["projection"]},
        }
        with self.assertRaisesRegex(
            StoryProjectContextError,
            "event_authority_downstream_reconciliation_required",
        ):
            StoryProjectContextService.apply_authority(blocked_context, {})

        validation = SimpleNamespace(
            root_resolution=SimpleNamespace(root=fixture["story"]),
            chapter_resolution=SimpleNamespace(resolved_chapter=4),
        )
        blocked_wrapper = SimpleNamespace(to_dict=lambda: blocked_context)
        checks: list[dict] = []
        with patch(
            "core.engine.preflight.build_generation_story_project_context",
            return_value=blocked_wrapper,
        ):
            _check_story_project_runtime_context(
                checks,
                story_project_validation=validation,
                snapshot={},
                memory={},
                identity=load_project_identity(fixture["story"]),
                allow_story_state_shadow_downgrade=False,
            )
        self.assertFalse(checks[-1]["ok"])
        self.assertIn(
            "event_authority_downstream_reconciliation_required",
            checks[-1]["error"],
        )

        self._apply_targets(first)
        fixture["projection"] = first["projection"]
        revision_text = "Audited reconciliation: Alice and every downstream artifact were repaired."
        fixture["revision_source"].write_bytes(revision_text.encode("utf-8"))
        alice_start = revision_text.index("Alice")
        fixture["evidence_spans"] = [
            {"start_char": alice_start, "end_char": alice_start + 5, "quote": "Alice"}
        ]
        second_kwargs = self._kwargs(fixture, transaction_id="revision-002")
        second_kwargs["reconciliation"] = {
            "blocked_transaction_id": first["transaction_id"],
            "blocked_impact_basis_hash": first["impact_report"]["impact_basis_hash"],
            "resolved_event_batch_ids": [
                *[
                    item["batch_id"]
                    for item in first["invalidation_manifest"]["event_invalidations"]
                ],
                first["batch"]["batch_id"],
            ],
            "resolved_outline_ids": [dependencies["outline_id"]],
            "resolved_session_ids": [dependencies["session_id"]],
            "resolved_dependency_inventory_hash": second_kwargs[
                "dependency_inventory"
            ]["inventory_hash"],
        }
        second = prepare_retcon_transaction(**second_kwargs)
        marker = second["projection"]["current_state"]["historical_revision"]
        self.assertFalse(marker["requires_downstream_reconciliation"])
        self.assertEqual(
            second_kwargs["dependency_inventory"]["inventory_hash"],
            marker["reconciliation"]["resolved_dependency_inventory_hash"],
        )
        self._apply_targets(second)

        ready_context = {
            "story_state_mode": "strict",
            "project_identity": second["project_identity_after"],
            "memory_v2": {"projection": second["projection"]},
        }
        ready_snapshot = StoryProjectContextService.apply_authority(ready_context, {})
        self.assertEqual(second["projection"]["head_event_hash"], ready_snapshot["semantic_authority"]["head_event_hash"])
        ready_wrapper = SimpleNamespace(to_dict=lambda: ready_context)
        checks = []
        with patch(
            "core.engine.preflight.build_generation_story_project_context",
            return_value=ready_wrapper,
        ):
            _check_story_project_runtime_context(
                checks,
                story_project_validation=validation,
                snapshot={},
                memory={},
                identity=load_project_identity(fixture["story"]),
                allow_story_state_shadow_downgrade=False,
            )
        self.assertTrue(checks[-1]["ok"])

    def test_empty_revision_parent_is_retry_safe_but_artifacts_collide(self) -> None:
        fixture = self._fixture("empty-retry")
        revision_root = fixture["memory_root"] / "history_revisions" / "revision-001"
        revision_root.mkdir(parents=True)
        prepared = prepare_amend_transaction(**self._kwargs(fixture))
        self.assertEqual("prepared", prepared["status"])

        (revision_root / "evidence.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(
            HistoricalRevisionError, "historical_revision_artifact_collision"
        ):
            prepare_amend_transaction(**self._kwargs(fixture))

    def test_anchor_and_target_cross_binding_rejects_recomputed_tamper(self) -> None:
        fixture = self._fixture("cross-bind")
        fixture["prose"][1].write_bytes(
            b"Chapter 1: Alice carried a forged green key through gate 1.\r\n"
        )
        with self.assertRaisesRegex(
            HistoricalRevisionError, "historical_chapter_published_evidence_drift"
        ):
            prepare_amend_transaction(**self._kwargs(fixture))

        fixture = self._fixture("target-bind")
        prepared = prepare_amend_transaction(**self._kwargs(fixture))
        tampered = copy.deepcopy(prepared)
        target = next(
            item
            for item in tampered["targets"]
            if item["kind"] == "historical_revision_evidence"
        )
        target["content"] = target["content"].replace("Alice", "AlicE", 1)
        with self.assertRaisesRegex(
            HistoricalRevisionError, "historical_revision_bundle_mismatch"
        ):
            validate_historical_revision_bundle(tampered)

    def test_source_sync_migration_snapshot_is_a_valid_tamper_evident_anchor(self) -> None:
        root = self._case_dir("source-sync")
        story = root / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (story / directory).mkdir(parents=True)
        ensure_project_identity(story, book_id=f"book-{uuid.uuid4().hex[:12]}")
        chapter_one = canonical_prose_path(story, 1)
        chapter_ten = canonical_prose_path(story, 10)
        chapter_one.write_text("Chapter one happened.\n", encoding="utf-8")
        for chapter in range(2, 10):
            canonical_prose_path(story, chapter).write_text(
                f"Chapter {chapter} happened.\n", encoding="utf-8"
            )
        chapter_ten.write_text("Chapter ten opened the gate.\n", encoding="utf-8")
        (story / "大纲" / "细纲_第010章.md").write_text("# 第十章\n", encoding="utf-8")
        (story / SETTING_DIR_NAME / "world.md").write_text(
            "Gravity is constant.\n", encoding="utf-8"
        )
        (story / TRACKING_DIR_NAME / "notes.md").write_text(
            "Legacy tracking projection.\n", encoding="utf-8"
        )
        plan = build_migration_plan(
            story, created_at="2026-07-14T00:00:00+00:00"
        )
        approval = build_migration_approval(
            plan,
            decisions={
                "timeline_elapsed_minutes": 155,
                "chapter_10_character_state": {
                    "hero": {"location": "gate", "condition": "injured"}
                },
                "open_foreshadowing": [
                    {
                        "id": "thread-door",
                        "status": "open",
                        "description": "door remains open",
                    }
                ],
                "inventory": {"hero": {"key": 1, "water": 0}},
                "lexicon": {
                    "black_tide": {"definition": "A dangerous black tide", "known_by": ["hero"]}
                },
                "corruption": {"hero": 3},
            },
            approver_id="operator-1",
            approved_at="2026-07-14T00:00:00+00:00",
        )
        execute_event_authority_migration(story, plan=plan, approval=approval)
        memory_root = RuntimePaths.for_story_project(story).memory_dir / "v2"
        projection = replay_memory_events(memory_root / "events")["projection"]
        revision_source = root / "revision-evidence.txt"
        revision_text = "Official correction: Alice documents the migrated chapter-one fact."
        revision_source.write_bytes(revision_text.encode("utf-8"))
        alice_start = revision_text.index("Alice")
        fixture = {
            "root": root,
            "story": story,
            "memory_root": memory_root,
            "event_store": memory_root / "events",
            "identity": load_project_identity(story),
            "projection": projection,
            "prose": {1: chapter_one, 10: chapter_ten},
            "revision_source": revision_source,
            "evidence_spans": [
                {"start_char": alice_start, "end_char": alice_start + 5, "quote": "Alice"}
            ],
            "root_uuid": str(uuid.uuid4()),
        }
        prepared = prepare_import_transaction(**self._kwargs(fixture))
        anchor = prepared["impact_report"]["anchor_batch"]
        self.assertEqual("migration_source_snapshot", anchor["evidence_kind"])
        self.assertEqual(self._sha(chapter_one.read_bytes()), anchor["historical_chapter_sha256"])

        chapter_one.write_bytes(b"Chapter one was silently rewritten.\r\n")
        with self.assertRaisesRegex(
            HistoricalRevisionError, "historical_chapter_published_evidence_drift"
        ):
            prepare_import_transaction(**self._kwargs(fixture, transaction_id="revision-002"))

    def test_historical_source_drift_fails_before_event_creation(self) -> None:
        fixture = self._fixture("drift")
        kwargs = self._kwargs(fixture)
        fixture["prose"][1].write_text("manually rewritten published prose", encoding="utf-8")
        with self.assertRaisesRegex(HistoricalRevisionError, "historical_chapter_source_drift"):
            prepare_amend_transaction(**kwargs)

    def test_completed_book_without_next_outline_uses_canonical_next_read_set(self) -> None:
        fixture = self._fixture("no-next-outline")
        self.assertEqual([], list((fixture["story"] / SOURCE_DIRECTORIES[0]).iterdir()))
        prepared = prepare_amend_transaction(**self._kwargs(fixture))
        self.assertEqual(4, fixture["projection"]["current_state"]["chapter_index"])
        self.assertEqual(4, prepared["story_project_read_set"]["chapter_index"])
        self.assertFalse(
            any(
                item["role"] == "outline"
                for item in prepared["story_project_read_set"]["entries"]
            )
        )

    def test_stale_head_epoch_and_revision_fail_closed(self) -> None:
        for field, value, code in (
            ("expected_head_event_hash", "0" * 64, "stale_authority_head"),
            ("authority_epoch", 2, "stale_authority_epoch"),
            ("expected_revision", 999, "stale_memory_revision"),
        ):
            with self.subTest(field=field):
                fixture = self._fixture(f"stale-{field}")
                kwargs = self._kwargs(fixture)
                kwargs[field] = value
                with self.assertRaisesRegex(HistoricalRevisionError, code):
                    prepare_retcon_transaction(**kwargs)

    def test_revision_evidence_cannot_be_the_published_chapter(self) -> None:
        fixture = self._fixture("in-place")
        kwargs = self._kwargs(fixture)
        kwargs["revision_source_path"] = fixture["prose"][1]
        kwargs["expected_revision_source_sha256"] = kwargs["expected_historical_chapter_sha256"]
        with self.assertRaisesRegex(
            HistoricalRevisionError, "published_prose_in_place_edit_forbidden"
        ):
            prepare_import_transaction(**kwargs)

        kwargs = self._kwargs(fixture)
        kwargs["operations"] = [{"op": "replace_chapter", "value": {"text": "forbidden"}}]
        with self.assertRaisesRegex(
            HistoricalRevisionError, "published_prose_in_place_edit_forbidden"
        ):
            prepare_import_transaction(**kwargs)

    def test_prepare_and_replay_are_deterministic_after_atomic_target_apply(self) -> None:
        fixture = self._fixture("deterministic")
        kwargs = self._kwargs(fixture)
        first = prepare_retcon_transaction(**kwargs)
        second = prepare_retcon_transaction(**kwargs)
        self.assertEqual(first["batch"], second["batch"])
        self.assertEqual(first["projection"], second["projection"])
        self.assertEqual(first["invalidation_manifest"], second["invalidation_manifest"])
        self.assertEqual(first["impact_report"], second["impact_report"])
        self.assertEqual(first["transaction"], second["transaction"])
        self.assertEqual(
            [(item["kind"], item["content"]) for item in first["targets"]],
            [(item["kind"], item["content"]) for item in second["targets"]],
        )

        self._apply_targets(first)
        replay_one = replay_memory_events(fixture["event_store"], use_checkpoint=False)
        replay_two = replay_memory_events(fixture["event_store"], use_checkpoint=False)
        self.assertEqual(first["projection"], replay_one["projection"])
        self.assertEqual(replay_one, replay_two)
        persisted_identity = load_project_identity(fixture["story"])
        self.assertEqual(first["projection"]["head_event_hash"], persisted_identity.authority["head_event_hash"])

    def test_unknown_revision_kind_batch_kind_and_reducer_fail_closed(self) -> None:
        fixture = self._fixture("unknown")
        with self.assertRaisesRegex(HistoricalRevisionError, "unknown_historical_revision_kind"):
            prepare_historical_revision_transaction(
                revision_kind="rewrite",
                **self._kwargs(fixture),
            )

        prepared = prepare_amend_transaction(**self._kwargs(fixture))
        unknown_batch = copy.deepcopy(prepared["batch"])
        unknown_batch["batch_kind"] = "rewrite"
        with self.assertRaises(Exception):
            validate_memory_event_batch(unknown_batch)

        unknown_reducer = copy.deepcopy(prepared["transaction"])
        unknown_reducer["reducer_version"] = "memory-reducer-9.9"
        unknown_reducer["transaction_hash"] = self._sha(
            json.dumps(
                {key: value for key, value in unknown_reducer.items() if key != "transaction_hash"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        with self.assertRaisesRegex(HistoricalRevisionError, "schema_invalid|unknown_reducer"):
            validate_historical_revision_transaction(unknown_reducer)

        generic_patch = create_memory_patch(
            patch_id="untrusted-retcon-writer",
            source_kind="retcon",
            source_path="history-revision:untrusted",
            operations=[{"op": "update_world", "value": {"forged": True}}],
        )
        revision_text = fixture["revision_source"].read_text(encoding="utf-8")
        _, generic_events = apply_memory_patch(
            fixture["projection"],
            generic_patch,
            reducer_version=CURRENT_REDUCER_VERSION,
            event_context={
                "chapter_body": revision_text,
                "evidence_spans": fixture["evidence_spans"],
                "authority_epoch": 1,
            },
        )
        replay = replay_memory_events(fixture["event_store"])
        with self.assertRaisesRegex(MemoryIntegrityError, "writer contract"):
            create_memory_event_batch(
                book_id=fixture["identity"].book_id,
                patch=generic_patch,
                events=generic_events,
                expected_revision=fixture["projection"]["revision"],
                previous_batch_hash=replay["last_batch_hash"],
                source_project_digest="1" * 64,
                context_digest="2" * 64,
                batch_kind="retcon",
                publication_status="retconned",
                schema_version="2.2",
                reducer_version=CURRENT_REDUCER_VERSION,
            )

    def test_prepare_output_constructs_persistence_v2_targets_and_exact_identity_declaration(self) -> None:
        fixture = self._fixture("persistence-targets")
        registry = RootRegistryService(fixture["root"] / "persistence").ensure(
            {"runtime": fixture["root"], "story_project": fixture["story"]}
        )
        kwargs = self._kwargs(fixture)
        kwargs["story_project_root_uuid"] = registry["roots"]["story_project"]["root_uuid"]
        prepared = prepare_amend_transaction(**kwargs)
        validate_schema(prepared["story_project_read_set"], "story_project_read_set.schema.json")

        constructed: list[PersistenceV2Target] = []
        for index, target in enumerate(prepared["targets"], start=1):
            path = Path(target["path"]).resolve()
            try:
                path.relative_to(fixture["story"].resolve())
                root_id = "story_project"
                root_path = fixture["story"]
            except ValueError:
                root_id = "runtime"
                root_path = fixture["root"]
            ref = path_ref_for(
                path,
                root_id=root_id,
                root=root_path,
                root_uuid=registry["roots"][root_id]["root_uuid"],
            )
            constructed.append(
                PersistenceV2Target(
                    target_id=f"history-revision-{index:03d}",
                    kind=target["kind"],
                    path_ref=ref,
                    content=target["content"],
                    expected_before_exists=target["expected_before_exists"],
                    expected_before_sha256=target["expected_before_sha256"],
                )
            )
        self.assertTrue(all(target.content_bytes() for target in constructed))
        self.assertNotIn("prose", {target.kind for target in constructed})
        self.assertEqual(
            [".novelagent/project.json"],
            [item["relative_path"] for item in prepared["read_set_declared_writes"]],
        )
        declaration = prepared["read_set_declared_writes"][0]
        self.assertEqual(
            prepared["story_project_read_set"]["identity_revision"],
            next(
                target.expected_before_sha256
                for target in constructed
                if target.kind == "project_identity"
            ),
        )
        self.assertEqual(
            declaration["after_sha256"],
            prepared["story_project_source_revision_after"]["identity_sha256"],
        )

    def test_legacy_batch_bytes_and_hash_remain_frozen(self) -> None:
        memory = create_empty_canonical_memory(book_id="b", title="B")
        patch = create_memory_patch(
            patch_id="p1",
            source_kind="test",
            operations=[{"op": "update_world", "value": {"level": 1}}],
        )
        _, events = apply_memory_patch(memory, patch, reducer_version=LEGACY_REDUCER_VERSION)
        batch = create_memory_event_batch(
            book_id="b",
            patch=patch,
            events=events,
            expected_revision=1,
            previous_batch_hash=None,
            source_project_digest="a" * 64,
            context_digest="b" * 64,
            base_projection=memory,
        )
        path = write_memory_event_batch(self._case_dir("legacy-golden") / "events", batch)
        self.assertEqual(
            "d89c0815707bc7413d3573b7b13c9fe3e8b10341f5963a04b548c7eafd6ef329",
            batch["batch_hash"],
        )
        self.assertEqual(
            "784c589893f4335d456def0db145edf2c15ef7d7092d47e9d16e2c89a6c6b646",
            self._sha(path.read_bytes()),
        )
        self.assertEqual(batch, validate_memory_event_batch(json.loads(path.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main()
