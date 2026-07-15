from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.memory_v2 import (
    capture_historical_revision_dependency_inventory,
    load_memory_event_batches,
    prepare_event_authority_chapter_commit,
    replay_memory_events,
)
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.storage import load_canonical_memory
from core.story_project.history_revision_execution import execute_amend_transaction
from core.story_project.identity import ensure_project_identity, load_project_identity, project_identity_path
from core.story_project.authority import AuthorityError, activate_event_authority, project_identity_sha256
from core.story_project.mapper import SETTING_DIR_NAME, TRACKING_DIR_NAME
from core.story_project.migration_execution import (
    MigrationExecutionError,
    execute_event_authority_migration,
)
from core.story_project.migration_v2 import (
    MigrationPlanStaleError,
    MigrationV2Error,
    build_migration_approval,
    build_migration_plan,
)
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.read_set import capture_story_project_read_set


NOW = "2026-07-14T00:00:00+00:00"


class StoryProjectMigrationExecutionTest(unittest.TestCase):
    def _book(self, name: str) -> Path:
        root = (
            Path.cwd()
            / ".tmp"
            / "test_story_project_migration_execution"
            / f"{name}_{uuid.uuid4().hex}"
            / "book"
        )
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        ensure_project_identity(root, book_id=f"book-{name}")
        canonical_prose_path(root, 1).write_text("Chapter one happened.\n", encoding="utf-8")
        for chapter in range(2, 10):
            canonical_prose_path(root, chapter).write_text(
                f"Chapter {chapter} happened.\n", encoding="utf-8"
            )
        canonical_prose_path(root, 10).write_text("Chapter ten opened the gate.\n", encoding="utf-8")
        canonical_outline_path(root, 10).write_text(
            "# 第十章\n\n- 核心事件：OUTLINE_ONLY_SENTINEL\n", encoding="utf-8"
        )
        (root / SETTING_DIR_NAME / "world.md").write_text(
            "# 世界\n\n| 字段 | 值 |\n|---|---|\n| 城门 | 只能由持钥匙者开启 |\n\n"
            "- 重力必须保持恒定\n",
            encoding="utf-8",
        )
        location = root / SETTING_DIR_NAME / "地点" / "城门.md"
        location.parent.mkdir(parents=True)
        location.write_text(
            "# 城门\n\n| 字段 | 值 |\n|---|---|\n| 状态 | 可通行 |\n",
            encoding="utf-8",
        )
        (root / TRACKING_DIR_NAME / "上下文.md").write_text(
            "# 当前上下文\n\n- 当前位置：追踪专用地点\n", encoding="utf-8"
        )
        (root / TRACKING_DIR_NAME / "角色状态.md").write_text(
            "# 角色状态\n\n## 追踪英雄\n\n- 位置：追踪专用地点\n- 状态：追踪状态\n",
            encoding="utf-8",
        )
        (root / TRACKING_DIR_NAME / "伏笔.md").write_text(
            "# 伏笔\n\n| ID | 内容 | 状态 |\n|---|---|---|\n"
            "| tracker-thread | 追踪投影中的未决线索 | open |\n",
            encoding="utf-8",
        )
        (root / TRACKING_DIR_NAME / "时间线.md").write_text(
            "# 时间线\n\n| ID | 章节 | 地点 | 事件 |\n|---|---:|---|---|\n"
            "| tracker-gate | 10 | 追踪专用地点 | 追踪投影声称门被打开 |\n",
            encoding="utf-8",
        )
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'{"legacy":true}\r\n')
        return root

    @staticmethod
    def _decisions() -> dict:
        return {
            "timeline_elapsed_minutes": 155,
            "chapter_10_character_state": {
                "hero": {"location": "gate", "condition": "injured"}
            },
            "open_foreshadowing": [
                {"id": "thread-door", "status": "open", "description": "door remains open"}
            ],
            "inventory": {"hero": {"key": 1, "water": 0}},
            "lexicon": {
                "black_tide": {"definition": "A dangerous black tide", "known_by": ["hero"]}
            },
            "corruption": {"hero": 3},
        }

    def _approved(self, root: Path) -> tuple[dict, dict]:
        plan = build_migration_plan(root, created_at=NOW)
        approval = build_migration_approval(
            plan,
            decisions=self._decisions(),
            approver_id="operator-1",
            approved_at=NOW,
        )
        return plan, approval

    def test_approved_bootstrap_is_one_receipted_source_sync_and_is_idempotent(self) -> None:
        root = self._book("happy")
        plan, approval = self._approved(root)
        preserved = {
            item["relative_path"]: (root / item["relative_path"]).read_bytes()
            for item in plan["sources"]
            if item["role"] != "project_identity"
        }

        result = execute_event_authority_migration(root, plan=plan, approval=approval)

        self.assertEqual("completed", result["status"])
        self.assertFalse(result["idempotent"])
        self.assertTrue(result["verification"]["valid"])
        identity = load_project_identity(root)
        self.assertEqual("2.0", identity.schema_version)
        self.assertEqual("event_v1", identity.authority["mode"])
        self.assertEqual(result["head_event_hash"], identity.authority["head_event_hash"])
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        batches = load_memory_event_batches(memory_root / "events")
        self.assertEqual(["genesis", "source_sync"], [item["batch_kind"] for item in batches])
        self.assertEqual(["genesis", "source_sync"], [item["publication_status"] for item in batches])
        replay = replay_memory_events(memory_root / "events")
        self.assertEqual(result["head_event_hash"], replay["projection"]["head_event_hash"])
        projection = replay["projection"]
        self.assertEqual([], projection["timeline"])
        self.assertIn("世界", projection["world"]["settings"])
        self.assertIn("城门", {item["name"] for item in projection["locations"].values()})
        self.assertTrue(projection["constraints"])
        self.assertTrue(all(item["status"] == "active" for item in projection["constraints"]))
        hero = projection["characters"]["character_hero"]
        self.assertEqual("gate", hero["state"]["current_location"])
        self.assertEqual("injured", hero["data"]["approved_fields"]["condition"])
        self.assertEqual(
            {"hero": "gate"},
            projection["current_state"]["spatial_state"]["character_positions"],
        )
        self.assertIn("thread_thread-door", projection["foreshadowing"])
        self.assertEqual("door remains open", projection["open_threads"][0]["title"])
        serialized_projection = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("追踪投影声称门被打开", serialized_projection)
        self.assertNotIn("追踪投影中的未决线索", serialized_projection)
        self.assertNotIn("追踪专用地点", serialized_projection)
        self.assertNotIn("追踪英雄", serialized_projection)
        self.assertNotIn("追踪状态", serialized_projection)
        self.assertNotIn("OUTLINE_ONLY_SENTINEL", serialized_projection)
        self.assertEqual(11, replay["projection"]["current_state"]["chapter_index"])
        checkpoint = next((memory_root / "events" / "checkpoints").glob("*.json"))
        checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(0, checkpoint_payload["committed_chapter_count"])
        baseline_path = next(
            (root / ".novelagent" / "migration-v2" / "artifacts" / "baselines").glob(
                "*.json"
            )
        )
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        source_sync = batches[1]
        operations = source_sync["patch"]["operations"]
        operations_hash = canonical_json_hash(operations)
        self.assertEqual(
            operations_hash, source_sync["patch"]["metadata"]["operations_hash"]
        )
        self.assertEqual(operations_hash, baseline["baseline_audit"]["operations_hash"])
        self.assertEqual(
            canonical_json_hash(
                baseline["baseline_audit"], exclude_fields=("audit_hash",)
            ),
            baseline["baseline_audit"]["audit_hash"],
        )
        self.assertEqual(
            canonical_json_hash(baseline, exclude_fields=("manifest_hash",)),
            baseline["manifest_hash"],
        )
        serialized_operations = json.dumps(operations, ensure_ascii=False, sort_keys=True)
        for sentinel in (
            "追踪投影声称门被打开",
            "追踪投影中的未决线索",
            "追踪专用地点",
            "追踪英雄",
            "追踪状态",
            "OUTLINE_ONLY_SENTINEL",
        ):
            self.assertNotIn(sentinel, serialized_operations)
        self.assertEqual(
            {"static_constraint", "user_approved_decision"},
            {item["data"]["evidence_class"] for item in operations},
        )
        self.assertFalse(baseline["history_policy"]["tracking_projection_imported_as_fact"])
        self.assertEqual(10, len(baseline["baseline_audit"]["published_prose_import"]))
        self.assertEqual(1, baseline["baseline_audit"]["excluded_unknown"]["timeline_count"])
        self.assertEqual(
            result["record"]["semantic_baseline_hash"],
            baseline["baseline_audit"]["semantic_baseline_hash"],
        )
        for relative, content in preserved.items():
            self.assertEqual(content, (root / relative).read_bytes(), relative)

        repeated = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(result["publication_receipt"], repeated["publication_receipt"])
        with self.assertRaisesRegex(MigrationV2Error, "migration_event_authority_already_active"):
            build_migration_plan(root, created_at=NOW)

        completed_entries = list(
            (root / ".novelagent" / "migration-v2" / "tx" / "registry" / "completed").glob(
                "*.json"
            )
        )
        self.assertEqual(1, len(completed_entries))

    def test_missing_approval_is_non_mutating(self) -> None:
        root = self._book("unconfirmed")
        plan = build_migration_plan(root, created_at=NOW)
        identity_before = project_identity_path(root).read_bytes()

        with self.assertRaisesRegex(MigrationExecutionError, "migration_approval_required"):
            execute_event_authority_migration(root, plan=plan, approval=None)

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertFalse((root / ".novelagent" / "runtime" / "memory" / "v2").exists())
        self.assertFalse((root / ".novelagent" / "migration-v2").exists())

    def test_completed_migration_remains_idempotent_after_legal_chapter(self) -> None:
        root = self._book("idempotent_after_chapter")
        plan, approval = self._approved(root)
        migrated = execute_event_authority_migration(
            root,
            plan=plan,
            approval=approval,
        )
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        body = "Chapter eleven advances the canonical story."
        prepared = prepare_event_authority_chapter_commit(
            memory_root=memory_root,
            book_id=plan["book_id"],
            run_id="legal-chapter-11",
            chapter_index=11,
            analysis={
                "summary": "Chapter eleven advances.",
                "events": [{"text": "The story advances."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "story_state": {},
                "spatial_state": {},
            },
            chapter_body=body,
            chapter_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            evidence_spans=[{"start": 0, "end": 7, "quote": "Chapter"}],
            authority_epoch=1,
            expected_head_event_hash=migrated["head_event_hash"],
            source_project_digest="a" * 64,
            context_digest="b" * 64,
            checkpoint_interval=1,
        )
        for target in prepared["targets"]:
            target["path"].parent.mkdir(parents=True, exist_ok=True)
            target["path"].write_text(target["content"], encoding="utf-8")
        identity = load_project_identity(root)
        identity_payload = identity.to_dict()
        identity_payload["authority"]["head_event_hash"] = prepared["projection"][
            "head_event_hash"
        ]
        project_identity_path(root).write_text(
            json.dumps(identity_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        repeated = execute_event_authority_migration(
            root,
            plan=plan,
            approval=approval,
        )

        self.assertTrue(repeated["idempotent"])
        self.assertEqual(migrated["head_event_hash"], repeated["baseline_head_event_hash"])
        self.assertEqual(
            prepared["projection"]["head_event_hash"],
            repeated["head_event_hash"],
        )
        self.assertEqual(
            ["genesis", "source_sync", "chapter"],
            [
                item["batch_kind"]
                for item in load_memory_event_batches(memory_root / "events")
            ],
        )

        latest_checkpoint = sorted(
            (memory_root / "events" / "checkpoints").glob("checkpoint_*.json")
        )[-1]
        checkpoint_payload = json.loads(
            latest_checkpoint.read_text(encoding="utf-8-sig")
        )
        checkpoint_payload["projection"]["world"]["absolute_path"] = "forged"
        latest_checkpoint.write_text(
            json.dumps(checkpoint_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        canonical_path = memory_root / "canonical_memory.json"
        canonical_payload = json.loads(canonical_path.read_text(encoding="utf-8-sig"))
        canonical_payload["world"]["absolute_path"] = "forged"
        canonical_path.write_text(
            json.dumps(canonical_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MigrationExecutionError, "migration_baseline_drift"):
            execute_event_authority_migration(root, plan=plan, approval=approval)

    def test_completed_migration_rechecks_raw_baseline_artifact_bytes(self) -> None:
        root = self._book("raw_baseline_receipt_binding")
        plan, approval = self._approved(root)
        execute_event_authority_migration(root, plan=plan, approval=approval)
        event_store = root / ".novelagent" / "runtime" / "memory" / "v2" / "events"
        source_sync = load_memory_event_batches(event_store)[1]
        source_sync_path = event_store / "batches" / f"{source_sync['batch_id']}.json"
        payload = json.loads(source_sync_path.read_text(encoding="utf-8-sig"))
        payload["patch"]["metadata"]["absolute_path"] = "forged"
        source_sync_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MigrationExecutionError, "migration_baseline_artifact_drift"
        ):
            execute_event_authority_migration(root, plan=plan, approval=approval)

    def test_unbound_legacy_plan_is_rejected_before_any_migration_write(self) -> None:
        root = self._book("unbound-plan")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()
        unbound = copy.deepcopy(plan)
        unbound.pop("baseline_contract")
        unbound["plan_hash"] = canonical_json_hash(
            unbound, exclude_fields=("plan_hash",)
        )

        with self.assertRaisesRegex(MigrationV2Error, "migration_baseline_contract_unbound"):
            execute_event_authority_migration(root, plan=unbound, approval=approval)

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertFalse((root / ".novelagent" / "migration-v2").exists())
        self.assertFalse((root / ".novelagent" / "runtime" / "memory" / "v2").exists())

    def test_source_drift_after_approval_expires_plan_before_bootstrap(self) -> None:
        root = self._book("source-drift")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()
        canonical_prose_path(root, 10).write_text("changed after approval\n", encoding="utf-8")

        with self.assertRaises(MigrationPlanStaleError):
            execute_event_authority_migration(root, plan=plan, approval=approval)

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertFalse(
            (root / ".novelagent" / "runtime" / "memory" / "v2" / "canonical_memory.json").exists()
        )

    def test_failure_after_activation_receipt_before_identity_rolls_back_and_retries(self) -> None:
        root = self._book("pre-identity-fault")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()

        def fail_before_identity(point: str, index: int | None, _path: Path | None) -> None:
            if point == "before_apply_target" and index == 9:
                raise OSError("injected before identity")

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root, plan=plan, approval=approval, fault_injector=fail_before_identity
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        receipt_dir = root / ".novelagent" / "authority" / "receipts"
        self.assertEqual([], list(receipt_dir.glob("*.json")) if receipt_dir.exists() else [])
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        self.assertEqual([], list(memory_root.rglob("*.json")) if memory_root.exists() else [])

        recovered = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertEqual("completed", recovered["status"])
        self.assertFalse(recovered["idempotent"])

    def test_failure_after_marker_recovers_same_transaction_without_downgrade(self) -> None:
        root = self._book("post-marker-fault")
        plan, approval = self._approved(root)

        def fail_after_marker(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise OSError("injected after marker")

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root, plan=plan, approval=approval, fault_injector=fail_after_marker
            )

        interim = load_project_identity(root)
        self.assertEqual("event_v1", interim.authority["mode"])
        self.assertEqual(approval["source_digest"], plan["source_digest"])

        recovered = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertTrue(recovered["idempotent"])
        self.assertEqual(interim.authority["head_event_hash"], recovered["head_event_hash"])

    def test_real_history_entry_recovers_marked_migration_then_replays(self) -> None:
        root = Path.cwd() / ".tmp" / "mh" / uuid.uuid4().hex[:8] / "b"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        ensure_project_identity(root, book_id=f"book-mh-{uuid.uuid4().hex[:8]}")
        canonical_prose_path(root, 1).write_text(
            "Chapter one happened.\n", encoding="utf-8"
        )
        for chapter in range(2, 10):
            canonical_prose_path(root, chapter).write_text(
                f"Chapter {chapter} happened.\n", encoding="utf-8"
            )
        canonical_prose_path(root, 10).write_text(
            "Chapter ten opened the gate.\n", encoding="utf-8"
        )
        canonical_outline_path(root, 10).write_text("# 第十章\n", encoding="utf-8")
        (root / SETTING_DIR_NAME / "world.md").write_text(
            "Gravity is constant.\n", encoding="utf-8"
        )
        (root / TRACKING_DIR_NAME / "notes.md").write_text(
            "Legacy tracking projection.\n", encoding="utf-8"
        )
        plan, approval = self._approved(root)

        def fail_after_marker(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise OSError("injected after migration marker")

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root,
                plan=plan,
                approval=approval,
                fault_injector=fail_after_marker,
            )

        global_pending = (
            root / ".novelagent" / "runtime" / "ea" / "r" / "p"
        )
        self.assertEqual(1, len(list(global_pending.glob("*.json"))))
        identity = load_project_identity(root)
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        projection = load_canonical_memory(memory_root / "canonical_memory.json")
        next_chapter = int(projection["current_state"]["chapter_index"])
        read_set = capture_story_project_read_set(
            root,
            next_chapter,
            project_identity=identity,
        )
        inventory = capture_historical_revision_dependency_inventory(
            story_project_root=root,
            book_id=identity.book_id,
            authority_epoch=int(identity.authority["authority_epoch"]),
            head_event_hash=projection["head_event_hash"],
            historical_chapter_index=1,
            canonical_next_chapter_index=next_chapter,
        )
        historical = canonical_prose_path(root, 1)
        revision_source = root.parent / "r.txt"
        revision_text = "Official correction: chapter one happened at dusk."
        revision_source.write_text(revision_text, encoding="utf-8")
        evidence_start = revision_text.index("chapter one")
        kwargs = {
            "memory_root": memory_root,
            "story_project_root": root,
            "transaction_id": "mh-001",
            "historical_chapter_index": 1,
            "historical_chapter_path": historical,
            "expected_historical_chapter_sha256": hashlib.sha256(
                historical.read_bytes()
            ).hexdigest(),
            "revision_source_path": revision_source,
            "expected_revision_source_sha256": hashlib.sha256(
                revision_source.read_bytes()
            ).hexdigest(),
            "evidence_spans": [
                {
                    "start_char": evidence_start,
                    "end_char": evidence_start + len("chapter one"),
                    "quote": "chapter one",
                }
            ],
            "operations": [
                {"op": "update_world", "value": {"chapter_1_time": "dusk"}}
            ],
            "authority_epoch": int(identity.authority["authority_epoch"]),
            "expected_head_event_hash": projection["head_event_hash"],
            "expected_revision": projection["revision"],
            "source_project_digest": read_set["membership_fingerprint"],
            "context_digest": read_set["context_digest"],
            "dependency_inventory": inventory,
        }

        revised = execute_amend_transaction(**kwargs)
        self.assertEqual("completed", revised["status"])
        self.assertEqual([], list(global_pending.glob("*.json")))
        completed_entries = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (
                root / ".novelagent" / "runtime" / "ea" / "r" / "c"
            ).glob("*.json")
        ]
        self.assertEqual(
            {"migration", "history_revision"},
            {entry["writer_kind"] for entry in completed_entries},
        )

        replayed = execute_amend_transaction(**kwargs)
        self.assertEqual("already_committed", replayed["status"])
        self.assertEqual(revised["head_event_hash"], replayed["head_event_hash"])

    def test_non_markdown_source_drift_at_marker_rolls_back_identity(self) -> None:
        root = self._book("marker-drift")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy.json"

        def mutate_legacy(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "before_commit_marker":
                legacy.write_bytes(b'{"legacy":"changed"}\n')

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root, plan=plan, approval=approval, fault_injector=mutate_legacy
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertEqual("legacy_markdown_v1", load_project_identity(root).authority["mode"])

    def test_preexisting_event_store_is_never_merged_into_baseline(self) -> None:
        root = self._book("foreign-event-store")
        foreign = root / ".novelagent" / "runtime" / "memory" / "v2" / "foreign.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_text('{"foreign":true}\n', encoding="utf-8")
        plan, approval = self._approved(root)

        with self.assertRaisesRegex(MigrationExecutionError, "migration_event_store_not_empty"):
            execute_event_authority_migration(root, plan=plan, approval=approval)

        self.assertEqual("legacy_markdown_v1", load_project_identity(root).authority["mode"])
        self.assertEqual('{"foreign":true}\n', foreign.read_text(encoding="utf-8"))

    def test_old_book_cannot_bypass_approval_through_direct_activation(self) -> None:
        root = self._book("direct-bypass")
        identity_before = project_identity_path(root).read_bytes()

        with self.assertRaisesRegex(
            AuthorityError, "migration_approval_required_for_existing_book"
        ):
            activate_event_authority(
                root,
                expected_identity_sha256=project_identity_sha256(root),
                head_event_hash="a" * 64,
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        plan, approval = self._approved(root)
        migrated = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertEqual("completed", migrated["status"])


if __name__ == "__main__":
    unittest.main()
