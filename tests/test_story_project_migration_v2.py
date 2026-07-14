from __future__ import annotations

import copy
from pathlib import Path
import unittest
import uuid

from core.story_project.identity import ensure_project_identity, project_identity_path
from core.story_project.migration_v2 import (
    MigrationPlanStaleError,
    MigrationV2Error,
    assert_migration_plan_current,
    build_migration_approval,
    build_migration_plan,
    validate_migration_approval,
    validate_migration_plan,
)


NOW = "2026-07-14T00:00:00+00:00"


class StoryProjectMigrationV2Test(unittest.TestCase):
    def _book(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_story_project_migration_v2" / f"{name}_{uuid.uuid4().hex}" / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        ensure_project_identity(root, book_id=f"book-{name}")
        return root

    def _populated_book(self, name: str = "populated") -> Path:
        root = self._book(name)
        (root / "正文" / "第001章_开始.md").write_text("第一章发生的事件。", encoding="utf-8")
        (root / "正文" / "第010章_门.md").write_text("第十章门被打开。", encoding="utf-8")
        (root / "设定" / "世界.md").write_text("重力恒定。", encoding="utf-8")
        (root / "大纲" / "总纲.md").write_text("未知的后续方向。", encoding="utf-8")
        (root / "大纲" / "细纲_第010章.md").write_text("第十章细纲。", encoding="utf-8")
        (root / "追踪" / "伏笔.md").write_text("尚未裁决。", encoding="utf-8")
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy-run.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'{"schema_version":"1.0","legacy":true}\r\n')
        return root

    @staticmethod
    def _decisions() -> dict:
        return {
            "timeline_elapsed_minutes": 155,
            "chapter_10_character_state": {
                "hero": {"location": "gate", "condition": "injured"}
            },
            "open_foreshadowing": [
                {"id": "thread-door", "status": "open", "evidence": "第十章门被打开"}
            ],
            "inventory": {"hero": {"key": 1, "water": 0}},
            "lexicon": {"black_tide": {"known_by": ["hero"]}},
            "corruption": {"hero": 3},
        }

    def test_plan_freezes_bytes_and_classifies_only_proven_sources(self) -> None:
        root = self._populated_book()
        identity_before = project_identity_path(root).read_bytes()
        legacy_path = root / ".novelagent" / "runtime" / "runs" / "legacy-run.json"
        legacy_before = legacy_path.read_bytes()

        plan = build_migration_plan(root, created_at=NOW)

        self.assertEqual(plan, validate_migration_plan(plan))
        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertEqual(legacy_before, legacy_path.read_bytes())
        by_role = {}
        for source in plan["sources"]:
            by_role.setdefault(source["role"], []).append(source)
            self.assertFalse(Path(source["relative_path"]).is_absolute())
            self.assertNotIn(str(root), source["relative_path"])
        self.assertTrue(all(item["evidence_class"] == "occurred_event" for item in by_role["published_prose"]))
        self.assertTrue(all(item["evidence_class"] == "static_constraint" for item in by_role["explicit_setting"]))
        self.assertTrue(all(item["evidence_class"] == "unknown" for item in by_role["master_outline"]))
        self.assertTrue(all(item["evidence_class"] == "unknown" for item in by_role["tracking_projection"]))
        self.assertEqual("legacy_artifact", by_role["legacy_runs"][0]["evidence_class"])
        self.assertEqual(2, plan["evidence_summary"]["occurred_event_evidence_count"])
        self.assertEqual(1, plan["evidence_summary"]["static_constraint_evidence_count"])
        self.assertEqual(plan, assert_migration_plan_current(plan, root))

    def test_any_source_membership_or_byte_change_expires_plan(self) -> None:
        root = self._populated_book("stale")
        plan = build_migration_plan(root, created_at=NOW)

        (root / "追踪" / "伏笔.md").write_text("人工修改。", encoding="utf-8")
        with self.assertRaisesRegex(MigrationPlanStaleError, "source bytes"):
            assert_migration_plan_current(plan, root)

        root = self._populated_book("new_member")
        plan = build_migration_plan(root, created_at=NOW)
        (root / "设定" / "新增.md").write_text("新增约束。", encoding="utf-8")
        with self.assertRaises(MigrationPlanStaleError):
            assert_migration_plan_current(plan, root)

        root = self._populated_book("identity_bytes")
        plan = build_migration_plan(root, created_at=NOW)
        project_identity_path(root).write_bytes(b"not-json-anymore")
        with self.assertRaisesRegex(MigrationPlanStaleError, "ProjectIdentity bytes changed"):
            assert_migration_plan_current(plan, root)

    def test_plan_and_approval_are_tamper_evident(self) -> None:
        root = self._populated_book("approval")
        plan = build_migration_plan(root, created_at=NOW)
        approval = build_migration_approval(
            plan,
            decisions=self._decisions(),
            approver_id="operator-1",
            approved_at=NOW,
        )

        self.assertEqual(155, approval["decisions"]["timeline_elapsed_minutes"])
        self.assertEqual(approval, validate_migration_approval(approval, plan=plan))
        tampered = copy.deepcopy(approval)
        tampered["decisions"]["timeline_elapsed_minutes"] = 156
        with self.assertRaises(MigrationV2Error):
            validate_migration_approval(tampered, plan=plan)

        tampered_plan = copy.deepcopy(plan)
        tampered_plan["sources"][0]["size"] += 1
        with self.assertRaises(MigrationV2Error):
            validate_migration_plan(tampered_plan)

        tampered_plan = copy.deepcopy(plan)
        tampered_plan["conflicts"] = [{"code": "invented"}]
        tampered_plan["evidence_summary"]["conflict_count"] = 1
        with self.assertRaisesRegex(MigrationV2Error, "migration_conflicts_not_derived"):
            validate_migration_plan(tampered_plan)

    def test_approval_requires_every_human_decision_and_rejects_secrets_paths(self) -> None:
        root = self._populated_book("decisions")
        plan = build_migration_plan(root, created_at=NOW)

        missing = self._decisions()
        missing.pop("inventory")
        with self.assertRaisesRegex(MigrationV2Error, "migration_decisions_invalid"):
            build_migration_approval(plan, decisions=missing, approver_id="operator", approved_at=NOW)

        for decisions in (
            {**self._decisions(), "inventory": {"api_key": "hidden"}},
            {**self._decisions(), "chapter_10_character_state": {"source": "C:/private/state.json"}},
        ):
            with self.subTest(decisions=decisions):
                with self.assertRaises(MigrationV2Error):
                    build_migration_approval(
                        plan,
                        decisions=decisions,
                        approver_id="operator",
                        approved_at=NOW,
                    )

    def test_duplicate_chapter_sources_require_exact_conflict_resolution(self) -> None:
        root = self._populated_book("conflict")
        (root / "正文" / "第001章_副本.md").write_text("冲突正文。", encoding="utf-8")
        plan = build_migration_plan(root, created_at=NOW)
        self.assertEqual("duplicate_chapter_source", plan["conflicts"][0]["code"])

        with self.assertRaisesRegex(MigrationV2Error, "migration_conflict_resolution_missing"):
            build_migration_approval(
                plan,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )
        decisions = self._decisions()
        decisions["conflict_resolutions"] = {
            "duplicate_chapter_source:published_prose:1": "正文/第001章_开始.md"
        }
        approval = build_migration_approval(
            plan,
            decisions=decisions,
            approver_id="operator",
            approved_at=NOW,
        )
        self.assertEqual(decisions["conflict_resolutions"], approval["decisions"]["conflict_resolutions"])
        self.assertEqual(approval, validate_migration_approval(approval, plan=plan))
        self.assertEqual(approval, validate_migration_approval(approval))

    def test_linked_sources_are_rejected_when_supported(self) -> None:
        root = self._populated_book("link")
        target = root / "outside.md"
        target.write_text("outside", encoding="utf-8")
        link = root / "设定" / "linked.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted")

        with self.assertRaisesRegex(MigrationV2Error, "migration_source_link_forbidden"):
            build_migration_plan(root, created_at=NOW)


if __name__ == "__main__":
    unittest.main()
