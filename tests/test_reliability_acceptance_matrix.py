from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock
import uuid

from core.delivery import DeliveryQueue
from core.delivery_intents import (
    build_file_delivery_intent,
    delivery_intent_receipt_binding,
)
from core.engine.delivery_intent_recovery import recover_completed_delivery_jobs
from core.engine.persistence_v2 import (
    PersistenceV2IntegrityError,
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
    reconcile_pending_persistence_v2,
    verify_publication_receipt,
)
from core.engine.root_registry import RootRegistryService
from core.engine.safe_paths import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    RootBinding,
    SafePathError,
    SafePathResolver,
)
from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    apply_genesis_event,
    apply_memory_patch,
    create_genesis_memory_batch,
    create_memory_event_batch,
    create_memory_patch,
    prepare_event_authority_chapter_commit,
    replay_memory_events,
    save_canonical_memory,
    write_memory_event_batch,
)
from core.memory_v2.recovery import (
    MemoryCacheRecoveryError,
    rebuild_event_authority_caches,
)
from core.path_refs import PathRef, PathRefError, path_ref_for, resolve_path_ref
from core.story_project.identity import (
    LEGACY_AUTHORITY_PROJECTION,
    ensure_project_identity,
    load_project_identity,
    project_identity_path,
)
from core.story_project.mapper import SETTING_DIR_NAME, TRACKING_DIR_NAME
from core.story_project.migration_execution import (
    MigrationExecutionError,
    execute_event_authority_migration,
)
from core.story_project.migration_v2 import (
    MigrationPlanStaleError,
    build_migration_approval,
    build_migration_plan,
)
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.read_set import (
    StoryProjectSourceDriftError,
    capture_story_project_read_set,
    declared_read_set_writes,
)


NOW = "2026-07-14T00:00:00+00:00"


class SimulatedPowerLoss(BaseException):
    pass


class ReliabilityAcceptanceMatrixTest(unittest.TestCase):
    def _base(self, name: str) -> Path:
        base = (
            Path.cwd()
            / ".tmp"
            / "test_reliability_acceptance_matrix"
            / f"{name}_{uuid.uuid4().hex}"
            / "中文项目"
        )
        base.mkdir(parents=True)
        return base

    @staticmethod
    def _sha_bytes(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @classmethod
    def _sha_text(cls, value: str) -> str:
        return cls._sha_bytes(value.encode("utf-8"))

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    def _persistence_case(self, name: str) -> dict:
        base = self._base(name)
        story = base / "故事"
        runtime = base / "运行时"
        chapters = base / "章节产物"
        delivery = base / "交付队列"
        external = base / "外部导出"
        for directory in (story, runtime, chapters, delivery, external):
            directory.mkdir()
        snapshot = runtime / "snapshot.json"
        snapshot.write_bytes(b'{"chapter":1}\n')
        return {
            "base": base,
            "story": story,
            "runtime": runtime,
            "chapters": chapters,
            "delivery": delivery,
            "external": external,
            "snapshot": snapshot,
            "transaction_root": runtime / "persistence-v2",
            "root_map": {
                "story_project": story,
                "runtime": runtime,
                "snapshot": runtime,
                "chapter_artifacts": chapters,
                "delivery_store": delivery,
                "external:canonical-export": external,
            },
        }

    def _prepare_persistence(
        self,
        case: dict,
        *,
        run_id: str,
        fault_injector=None,
        delivery_jobs: list[dict] | None = None,
        extra_artifacts: list[PersistenceV2Target] | None = None,
    ) -> PersistenceV2Transaction:
        runtime = case["runtime"]
        receipt_path = runtime / "receipts" / f"{run_id}.json"
        receipt_ref = path_ref_for(
            receipt_path, root_id="runtime", root=runtime
        )
        final_ref = path_ref_for(
            runtime / "runs" / f"{run_id}.json",
            root_id="runtime",
            root=runtime,
        )
        final_record = bind_final_run_record_receipt(
            {
                "id": run_id,
                "status": "committed",
                "committed": True,
                "chapter_index": 2,
            },
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
        )
        transaction = PersistenceV2Transaction(
            transaction_root=case["transaction_root"],
            run_id=run_id,
            book_id="book-reliability",
            root_map=case["root_map"],
            fault_injector=fault_injector,
        )
        artifacts = [
            PersistenceV2Target(
                target_id=f"chapter-{run_id}",
                kind="chapter_artifact",
                path_ref=path_ref_for(
                    case["chapters"] / f"{run_id}.md",
                    root_id="chapter_artifacts",
                    root=case["chapters"],
                ),
                content="deterministic chapter\n",
                phase="publication",
            )
        ]
        artifacts.extend(extra_artifacts or [])
        transaction.prepare(
            apply_targets=[
                PersistenceV2Target(
                    target_id=f"snapshot-{run_id}",
                    kind="snapshot",
                    path_ref=path_ref_for(
                        case["snapshot"], root_id="snapshot", root=runtime
                    ),
                    content='{"chapter":2}\n',
                    expected_before_exists=True,
                    expected_before_sha256=self._sha_text('{"chapter":1}\n'),
                )
            ],
            artifacts=artifacts,
            final_run_record=final_record,
            final_run_path_ref=final_ref,
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
            context_digest="a" * 64,
            generation_input_context_digest="b" * 64,
            story_project_source_revision_after={"revision": 2},
            candidate_result={"run_id": run_id, "accepted": True},
            delivery_jobs=delivery_jobs or [],
        )
        return transaction

    def test_persistence_prepare_fault_hooks_leave_no_unrecoverable_write(self) -> None:
        cases = {
            "after_temporary_journal_created": "abandoned_prepare",
            "after_prepare_target_staged": "abandoned_prepare",
            "before_journal_publish": "rolled_back",
            "after_journal_publish": "rolled_back",
            "before_pending_registry": "rolled_back",
            "after_pending_registry": "rolled_back",
        }
        for hook, expected_state in cases.items():
            with self.subTest(hook=hook):
                case = self._persistence_case(f"prepare-{hook}")

                def crash(
                    event: str,
                    _index: int | None,
                    _path: Path | None,
                    *,
                    expected: str = hook,
                ) -> None:
                    if event == expected:
                        raise SimulatedPowerLoss(event)

                with self.assertRaises(SimulatedPowerLoss):
                    self._prepare_persistence(
                        case, run_id="run-prepare", fault_injector=crash
                    )
                report = reconcile_pending_persistence_v2(case["transaction_root"])

                self.assertTrue(report["ok"], report)
                self.assertEqual(
                    b'{"chapter":1}\n', case["snapshot"].read_bytes()
                )
                states = [item["state"] for item in report["transactions"]]
                if expected_state == "abandoned_prepare":
                    error_codes = [
                        error["code"]
                        for item in report["transactions"]
                        for error in item.get("errors", [])
                    ]
                    self.assertIn("abandoned_prepare", error_codes)
                else:
                    self.assertIn(expected_state, states)

    def test_persistence_commit_fault_hooks_obey_marker_boundary(self) -> None:
        matrix = {
            "before_apply_target": "rolled_back",
            "after_apply_target": "rolled_back",
            "before_commit_marker": "rolled_back",
            "after_commit_marker": "completed",
            "before_publication_target": "completed",
            "after_publication_target": "completed",
            "before_publication_receipt": "completed",
            "after_publication_receipt": "completed",
        }
        for hook, expected_state in matrix.items():
            with self.subTest(hook=hook):
                case = self._persistence_case(f"commit-{hook}")

                def crash(
                    event: str,
                    _index: int | None,
                    _path: Path | None,
                    *,
                    expected: str = hook,
                ) -> None:
                    if event == expected:
                        raise SimulatedPowerLoss(event)

                transaction = self._prepare_persistence(
                    case, run_id="run-commit", fault_injector=crash
                )
                with self.assertRaises(SimulatedPowerLoss):
                    transaction.commit()

                report = reconcile_pending_persistence_v2(case["transaction_root"])
                self.assertTrue(report["ok"], report)
                self.assertEqual(expected_state, report["transactions"][0]["state"])
                if expected_state == "rolled_back":
                    self.assertEqual(
                        b'{"chapter":1}\n', case["snapshot"].read_bytes()
                    )
                    self.assertFalse(
                        (case["runtime"] / "receipts" / "run-commit.json").exists()
                    )
                else:
                    self.assertEqual(
                        b'{"chapter":2}\n', case["snapshot"].read_bytes()
                    )
                    verification = verify_publication_receipt(
                        case["runtime"] / "receipts" / "run-commit.json",
                        root_map=case["root_map"],
                    )
                    self.assertTrue(verification["valid"], verification)
                    self.assertTrue(verification["committed"], verification)

    def _delivery_intent(self, run_id: str) -> dict:
        body = "Lin reached the old station."
        genesis = create_genesis_memory_batch(
            book_id="book-reliability",
            title="Reliability",
            source_project_digest="1" * 64,
            context_digest="2" * 64,
        )
        projection = apply_genesis_event(genesis["events"][0])
        patch = create_memory_patch(
            patch_id="chapter-2",
            source_kind="chapter",
            operations=[
                {
                    "op": "update_story_time",
                    "value": {
                        "label": "chapter 2",
                        "elapsed_minutes": 5,
                        "chapter_index": 2,
                    },
                }
            ],
        )
        _updated, events = apply_memory_patch(
            projection,
            patch,
            reducer_version=CURRENT_REDUCER_VERSION,
            event_context={
                "chapter_body": body,
                "evidence_spans": [
                    {
                        "start_char": 0,
                        "end_char": len(body),
                        "quote": body,
                    }
                ],
                "authority_epoch": 1,
            },
        )
        batch = create_memory_event_batch(
            book_id="book-reliability",
            patch=patch,
            events=events,
            expected_revision=projection["revision"],
            previous_batch_hash=genesis["batch_hash"],
            source_project_digest="3" * 64,
            context_digest="4" * 64,
            batch_kind="chapter",
            publication_status="committed",
            schema_version="2.2",
            reducer_version=CURRENT_REDUCER_VERSION,
        )
        return build_file_delivery_intent(
            profile={
                "schema_version": "1.0",
                "profile_id": "canonical-export",
                "root_id": "external:canonical-export",
                "root_uuid": None,
                "relative_directory": "canonical",
                "filename_template": "chapter-{chapter_index}-{run_id}.json",
            },
            book_id="book-reliability",
            run_id=run_id,
            chapter_index=2,
            event_batch=batch,
            chapter_body_sha256=self._sha_text(body),
            policy="required",
            created_at=NOW,
        )

    def test_receipt_to_delivery_job_crash_window_recovers_exactly_once(self) -> None:
        case = self._persistence_case("delivery-recovery")
        run_id = "run-delivery"
        intent = self._delivery_intent(run_id)
        intent_path = case["runtime"] / "delivery-intents" / f"{run_id}.json"
        artifact = PersistenceV2Target(
            target_id="delivery-intent",
            kind="delivery_intent",
            path_ref=path_ref_for(
                intent_path, root_id="runtime", root=case["runtime"]
            ),
            content=self._json_bytes(intent),
            phase="publication",
        )

        def crash_after_receipt(
            event: str, _index: int | None, _path: Path | None
        ) -> None:
            if event == "after_publication_receipt":
                raise SimulatedPowerLoss(event)

        transaction = self._prepare_persistence(
            case,
            run_id=run_id,
            fault_injector=crash_after_receipt,
            delivery_jobs=[delivery_intent_receipt_binding(intent)],
            extra_artifacts=[artifact],
        )
        with self.assertRaises(SimulatedPowerLoss):
            transaction.commit()
        self.assertFalse((case["delivery"] / "jobs").exists())

        persistence = reconcile_pending_persistence_v2(case["transaction_root"])
        self.assertTrue(persistence["ok"], persistence)
        queue = DeliveryQueue(
            case["delivery"],
            clock=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )
        first = recover_completed_delivery_jobs(
            case["transaction_root"], root_map=case["root_map"], queue=queue
        )
        second = recover_completed_delivery_jobs(
            case["transaction_root"], root_map=case["root_map"], queue=queue
        )

        self.assertEqual(1, first["job_count"])
        self.assertEqual(first["jobs"], second["jobs"])
        job = queue.load(intent["intent_id"])
        self.assertEqual("pending", job["state"])
        self.assertEqual(0, job["attempt_count"])
        self.assertEqual([], list(case["external"].rglob("*.json")))

    @staticmethod
    def _materialize_memory_targets(prepared: dict) -> None:
        for target in prepared["targets"]:
            target["path"].parent.mkdir(parents=True, exist_ok=True)
            # PersistenceV2 publishes the exact UTF-8 payload bytes; using a
            # text writer here would translate LF to CRLF on Windows and turn
            # this into a newline-policy test instead of a cache rebuild test.
            target["path"].write_bytes(target["content"].encode("utf-8"))

    @staticmethod
    def _tree_hashes(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_deleted_memory_caches_rebuild_to_identical_unicode_path_bytes(self) -> None:
        memory_root = self._base("cache-rebuild") / "运行时" / "记忆" / "v2"
        runtime_root = memory_root.parents[1]
        runtime_snapshot = runtime_root / "snapshot.json"
        event_store = memory_root / "events"
        genesis = create_genesis_memory_batch(
            book_id="book-cache",
            title="旧站",
            source_project_digest="1" * 64,
            context_digest="2" * 64,
            authority_epoch=3,
        )
        write_memory_event_batch(event_store, genesis)
        base = replay_memory_events(event_store)["projection"]
        save_canonical_memory(memory_root / "canonical_memory.json", base)
        body = "林雪进入旧站控制室，确认闸门已经关闭。"
        prepared = prepare_event_authority_chapter_commit(
            memory_root=memory_root,
            book_id="book-cache",
            run_id="run-cache-1",
            chapter_index=1,
            analysis={
                "summary": "林雪进入控制室。",
                "events": [{"text": "闸门关闭"}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": ["控制室"],
                "story_state": {"last_scene_location": "控制室"},
                "spatial_state": {"character_positions": {"林雪": "控制室"}},
            },
            chapter_body=body,
            chapter_body_sha256=self._sha_text(body),
            evidence_spans=[{"start": 0, "end": 2, "quote": "林雪"}],
            authority_epoch=3,
            expected_head_event_hash=base["head_event_hash"],
            expected_revision=base["revision"],
            source_project_digest="3" * 64,
            context_digest="4" * 64,
            checkpoint_interval=1,
        )
        self._materialize_memory_targets(prepared)
        runtime_snapshot.write_bytes(
            (memory_root / "projections" / "snapshot.json").read_bytes()
        )
        expected_runtime_snapshot_hash = hashlib.sha256(
            runtime_snapshot.read_bytes()
        ).hexdigest()
        event_hashes_before = self._tree_hashes(event_store)
        expected_cache_hashes = {
            path.relative_to(memory_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in [
                memory_root / "canonical_memory.json",
                *sorted((memory_root / "projections").rglob("*")),
            ]
            if path.is_file()
        }

        with self.assertRaisesRegex(
            MemoryCacheRecoveryError, "outside or unsafe"
        ):
            rebuild_event_authority_caches(
                memory_root,
                runtime_root=runtime_root,
                runtime_snapshot_target=runtime_root.parent / "越界快照.json",
                expected_book_id="book-cache",
                expected_authority_epoch=3,
                expected_head_event_hash=prepared["projection"]["head_event_hash"],
            )

        (memory_root / "canonical_memory.json").unlink()
        shutil.rmtree(memory_root / "projections")
        runtime_snapshot.unlink()
        with self.assertRaisesRegex(
            MemoryCacheRecoveryError, "head_event_hash mismatch"
        ):
            rebuild_event_authority_caches(
                memory_root,
                runtime_root=runtime_root,
                runtime_snapshot_target=runtime_snapshot,
                expected_book_id="book-cache",
                expected_authority_epoch=3,
                expected_head_event_hash="f" * 64,
            )
        self.assertFalse((memory_root / "canonical_memory.json").exists())
        self.assertFalse((memory_root / "projections").exists())
        self.assertFalse(runtime_snapshot.exists())
        first = rebuild_event_authority_caches(
            memory_root,
            runtime_root=runtime_root,
            runtime_snapshot_target=runtime_snapshot,
            expected_book_id="book-cache",
            expected_authority_epoch=3,
            expected_head_event_hash=prepared["projection"]["head_event_hash"],
        )
        rebuilt_hashes = {
            item["relative_path"]: item["sha256"] for item in first["cache_files"]
        }
        second = rebuild_event_authority_caches(
            memory_root,
            runtime_root=runtime_root,
            runtime_snapshot_target=runtime_snapshot,
            expected_book_id="book-cache",
            expected_authority_epoch=3,
            expected_head_event_hash=prepared["projection"]["head_event_hash"],
        )

        self.assertEqual(expected_cache_hashes, rebuilt_hashes)
        self.assertEqual(
            expected_runtime_snapshot_hash,
            hashlib.sha256(runtime_snapshot.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            expected_runtime_snapshot_hash, first["runtime_snapshot"]["sha256"]
        )
        self.assertEqual(first, second)
        self.assertEqual(event_hashes_before, self._tree_hashes(event_store))
        self.assertEqual(
            first["projection_hash"], replay_memory_events(event_store)["projection_hash"]
        )

    def _story_read_set_case(self, name: str) -> tuple[dict, dict, Path, Path]:
        case = self._persistence_case(name)
        story = case["story"]
        for directory in CORE_DIRECTORY_NAMES:
            (story / directory).mkdir(exist_ok=True)
        canonical_outline_path(story, 2).write_text("# 第二章\n", encoding="utf-8")
        canonical_prose_path(story, 1).write_text("第一章\n", encoding="utf-8")
        tracking = story / TRACKING_DIR_NAME / "上下文.md"
        settings = story / SETTING_DIR_NAME / "地点.md"
        tracking.write_text("# 上下文\n旧\n", encoding="utf-8")
        settings.write_text("# 地点\n旧站\n", encoding="utf-8")
        identity = ensure_project_identity(story, book_id=f"book-{name}")
        read_set = capture_story_project_read_set(
            story, 2, project_identity=identity
        )
        return case, read_set, tracking, settings

    def _prepare_read_set_transaction(
        self,
        case: dict,
        read_set: dict,
        tracking: Path,
        *,
        run_id: str,
        fault_injector=None,
    ) -> PersistenceV2Transaction:
        content = "# 上下文\n新\n"
        raw = content.encode("utf-8")
        target = PersistenceV2Target(
            target_id="tracking",
            kind="tracking",
            path_ref=path_ref_for(
                tracking, root_id="story_project", root=case["story"]
            ),
            content=raw,
            expected_before_exists=True,
            expected_before_sha256=self._sha_bytes(tracking.read_bytes()),
        )
        declared = declared_read_set_writes(
            read_set, [(tracking, self._sha_bytes(raw), len(raw))]
        )
        receipt_ref = path_ref_for(
            case["runtime"] / "receipts" / f"{run_id}.json",
            root_id="runtime",
            root=case["runtime"],
        )
        final_ref = path_ref_for(
            case["runtime"] / "runs" / f"{run_id}.json",
            root_id="runtime",
            root=case["runtime"],
        )
        final = bind_final_run_record_receipt(
            {"id": run_id, "committed": True, "status": "committed"},
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
        )
        transaction = PersistenceV2Transaction(
            transaction_root=case["transaction_root"],
            run_id=run_id,
            book_id=read_set["book_id"],
            root_map=case["root_map"],
            fault_injector=fault_injector,
            story_project_read_set=read_set,
            read_set_declared_writes=declared,
        )
        transaction.prepare(
            apply_targets=[target],
            artifacts=[],
            final_run_record=final,
            final_run_path_ref=final_ref,
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
            context_digest="5" * 64,
            generation_input_context_digest="6" * 64,
            story_project_source_revision_after={"revision": 2},
            candidate_result={"run_id": run_id},
            delivery_jobs=[],
        )
        return transaction

    def test_story_project_read_set_rejects_prepare_preapply_and_premarker_drift(
        self,
    ) -> None:
        case, read_set, tracking, settings = self._story_read_set_case(
            "readset-prepare"
        )
        before = tracking.read_bytes()
        settings.write_text("concurrent prepare edit\n", encoding="utf-8")
        with self.assertRaises(StoryProjectSourceDriftError):
            self._prepare_read_set_transaction(
                case, read_set, tracking, run_id="prepare-drift"
            )
        self.assertEqual(before, tracking.read_bytes())

        case, read_set, tracking, settings = self._story_read_set_case(
            "readset-preapply"
        )
        before = tracking.read_bytes()
        transaction = self._prepare_read_set_transaction(
            case, read_set, tracking, run_id="preapply-drift"
        )
        settings.write_text("concurrent preapply edit\n", encoding="utf-8")
        result = transaction.commit()
        self.assertEqual("rolled_back", result["state"])
        self.assertEqual(before, tracking.read_bytes())

        case, read_set, tracking, settings = self._story_read_set_case(
            "readset-premarker"
        )
        before = tracking.read_bytes()

        def mutate_at_marker(
            event: str, _index: int | None, _path: Path | None
        ) -> None:
            if event == "before_commit_marker":
                settings.write_text("concurrent marker edit\n", encoding="utf-8")

        transaction = self._prepare_read_set_transaction(
            case,
            read_set,
            tracking,
            run_id="premarker-drift",
            fault_injector=mutate_at_marker,
        )
        result = transaction.commit()
        self.assertEqual("rolled_back", result["state"])
        self.assertEqual(before, tracking.read_bytes())

    def _migration_book(self, name: str) -> Path:
        root = self._base(name) / "长篇小说"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        ensure_project_identity(root, book_id=f"book-{name}")
        canonical_prose_path(root, 1).write_bytes(
            "第一章发生过。\r\n".encode("utf-8")
        )
        canonical_prose_path(root, 10).write_bytes(
            "第十章打开了门。\r\n".encode("utf-8")
        )
        (root / SETTING_DIR_NAME / "世界.md").write_bytes(
            "重力恒定。\r\n".encode("utf-8")
        )
        (root / TRACKING_DIR_NAME / "旧追踪.md").write_bytes(
            "旧版追踪投影。\r\n".encode("utf-8")
        )
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'{"schema_version":"1.0","legacy":true}\r\n')
        return root

    @staticmethod
    def _migration_decisions() -> dict:
        return {
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
            "inventory": {"hero": {"key": 1}},
            "lexicon": {"black_tide": {"known_by": ["hero"]}},
            "corruption": {"hero": 3},
        }

    def _approved_migration(self, root: Path) -> tuple[dict, dict]:
        plan = build_migration_plan(root, created_at=NOW)
        approval = build_migration_approval(
            plan,
            decisions=self._migration_decisions(),
            approver_id="operator-reliability",
            approved_at=NOW,
        )
        return plan, approval

    def test_stale_migration_plan_rejects_historical_prose_edit_without_activation(
        self,
    ) -> None:
        root = self._migration_book("stale-history")
        plan, approval = self._approved_migration(root)
        identity_before = project_identity_path(root).read_bytes()
        canonical_prose_path(root, 1).write_text(
            "历史正文被手工修改。\n", encoding="utf-8"
        )

        with self.assertRaises(MigrationPlanStaleError):
            execute_event_authority_migration(
                root, plan=plan, approval=approval
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertEqual(
            "legacy_markdown_v1", load_project_identity(root).authority["mode"]
        )

    def test_post_marker_migration_recovery_preserves_v1_bytes_and_blocks_downgrade(
        self,
    ) -> None:
        root = self._migration_book("legacy-bytes")
        plan, approval = self._approved_migration(root)
        legacy_paths = [
            canonical_prose_path(root, 1),
            canonical_prose_path(root, 10),
            root / SETTING_DIR_NAME / "世界.md",
            root / TRACKING_DIR_NAME / "旧追踪.md",
            root / ".novelagent" / "runtime" / "runs" / "legacy.json",
        ]
        before = {path: path.read_bytes() for path in legacy_paths}

        def fail_after_marker(
            event: str, _index: int | None, _path: Path | None
        ) -> None:
            if event == "after_commit_marker":
                raise OSError("injected post-marker crash")

        with self.assertRaises(MigrationExecutionError):
            execute_event_authority_migration(
                root,
                plan=plan,
                approval=approval,
                fault_injector=fail_after_marker,
            )
        for path, content in before.items():
            self.assertEqual(content, path.read_bytes(), path)

        recovered = execute_event_authority_migration(
            root, plan=plan, approval=approval
        )
        self.assertTrue(recovered["idempotent"])
        for path, content in before.items():
            self.assertEqual(content, path.read_bytes(), path)
        identity = load_project_identity(root)
        self.assertEqual("event_v1", identity.authority["mode"])

        identity_path = project_identity_path(root)
        identity_before = identity_path.read_bytes()
        downgraded = identity.to_dict()
        downgraded["authority"] = copy.deepcopy(LEGACY_AUTHORITY_PROJECTION)
        downgraded_bytes = self._json_bytes(downgraded)
        read_set = capture_story_project_read_set(
            root, 11, project_identity=identity
        )
        runtime = root / ".novelagent"
        transaction_root = runtime / "reliability-downgrade" / "tx"
        root_map = {"story_project": root, "runtime": runtime}
        registry = RootRegistryService(transaction_root).ensure(root_map)
        declaration = {
            "relative_path": ".novelagent/project.json",
            "role": "project_identity",
            "action": "replace",
            "after_sha256": self._sha_bytes(downgraded_bytes),
            "after_size": len(downgraded_bytes),
            "book_id": identity.book_id,
            "expected_authority_epoch": identity.authority["authority_epoch"],
            "expected_head_event_hash": identity.authority["head_event_hash"],
            "after_authority_epoch": 0,
            "after_head_event_hash": None,
        }
        receipt_ref = path_ref_for(
            runtime / "reliability-downgrade" / "receipt.json",
            root_id="runtime",
            root=runtime,
        )
        final_ref = path_ref_for(
            runtime / "reliability-downgrade" / "run.json",
            root_id="runtime",
            root=runtime,
        )
        final = bind_final_run_record_receipt(
            {"id": "downgrade", "committed": True, "status": "committed"},
            receipt_id="receipt-downgrade",
            receipt_path_ref=receipt_ref,
        )
        transaction = PersistenceV2Transaction(
            transaction_root=transaction_root,
            run_id="downgrade",
            book_id=identity.book_id,
            root_map=root_map,
            story_project_read_set=read_set,
            read_set_declared_writes=[declaration],
        )
        with self.assertRaisesRegex(
            PersistenceV2IntegrityError, "cannot be downgraded"
        ):
            transaction.prepare(
                apply_targets=[
                    PersistenceV2Target(
                        target_id="project-identity",
                        kind="project_identity",
                        path_ref=path_ref_for(
                            identity_path,
                            root_id="story_project",
                            root=root,
                        ),
                        content=downgraded_bytes,
                        expected_before_exists=True,
                        expected_before_sha256=self._sha_bytes(identity_before),
                    )
                ],
                artifacts=[],
                final_run_record=final,
                final_run_path_ref=final_ref,
                receipt_id="receipt-downgrade",
                receipt_path_ref=receipt_ref,
                context_digest="7" * 64,
                generation_input_context_digest="8" * 64,
                story_project_source_revision_after={
                    "schema_version": "1.0",
                    "book_id": identity.book_id,
                    "root_uuid": registry["roots"]["story_project"]["root_uuid"],
                    "identity_sha256": declaration["after_sha256"],
                    "authority_epoch": 0,
                    "head_event_hash": None,
                },
                candidate_result={"run_id": "downgrade"},
                delivery_jobs=[],
            )
        self.assertEqual(identity_before, identity_path.read_bytes())

    def test_unicode_path_ref_out_of_root_and_reparse_guards(self) -> None:
        root = self._base("path-safety") / "小说根目录"
        parent = root / "追踪" / "角色"
        parent.mkdir(parents=True)
        target = parent / "林雪状态.md"
        target.write_text("安全\n", encoding="utf-8")
        ref = path_ref_for(target, root_id="story_project", root=root)

        self.assertEqual("追踪/角色/林雪状态.md", ref.relative_path)
        self.assertEqual(target.resolve(), resolve_path_ref(ref, {"story_project": root}))
        with self.assertRaises(PathRefError):
            path_ref_for(root.parent / "越界.md", root_id="story_project", root=root)
        with self.assertRaises(PathRefError):
            resolve_path_ref(
                PathRef(root_id="story_project", relative_path="../越界.md"),
                {"story_project": root},
            )

        resolver = SafePathResolver(
            {
                "story_project": RootBinding(
                    root_id="story_project",
                    root_uuid=str(uuid.uuid4()),
                    path=root,
                )
            }
        )
        real_lstat = os.lstat

        class ReparseStat:
            def __init__(self, wrapped) -> None:
                self._wrapped = wrapped
                self.st_file_attributes = getattr(
                    wrapped, "st_file_attributes", 0
                ) | FILE_ATTRIBUTE_REPARSE_POINT

            def __getattr__(self, name: str):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            result = real_lstat(path)
            if Path(path).absolute() == parent.absolute():
                return ReparseStat(result)
            return result

        with mock.patch("core.engine.safe_paths.os.lstat", side_effect=fake_lstat):
            with self.assertRaises(SafePathError):
                resolver.resolve(ref)

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics")
    def test_real_windows_junction_is_rejected_without_touching_its_target(self) -> None:
        base = self._base("real-junction")
        root = base / "受控根"
        outside = base / "根外目标"
        root.mkdir()
        outside.mkdir()
        secret = outside / "不得写入.md"
        secret.write_text("outside remains untouched\n", encoding="utf-8")
        junction = root / "联接目录"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            check=False,
            capture_output=True,
        )
        if created.returncode != 0:
            self.skipTest("junction creation is not permitted in this environment")
        try:
            resolver = SafePathResolver(
                {
                    "story_project": RootBinding(
                        root_id="story_project",
                        root_uuid=str(uuid.uuid4()),
                        path=root,
                    )
                }
            )
            with self.assertRaises(SafePathError):
                resolver.resolve(
                    PathRef(
                        root_id="story_project",
                        relative_path="联接目录/不得写入.md",
                    )
                )
        finally:
            # os.rmdir removes the junction entry itself and never traverses
            # into or removes the external target directory.
            os.rmdir(junction)
        self.assertEqual("outside remains untouched\n", secret.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
