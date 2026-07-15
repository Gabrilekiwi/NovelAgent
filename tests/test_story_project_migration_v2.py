from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

import main as cli
from core.delivery import DeliveryQueue, delivery_outcome
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.identity import ensure_project_identity, project_identity_path
from core.story_project.migration_v2 import (
    MigrationPlanStaleError,
    MigrationV2Error,
    assert_migration_plan_current,
    assert_migration_source_snapshot_current,
    build_migration_approval,
    build_migration_plan,
    build_migration_preview,
    validate_migration_approval,
    validate_migration_plan,
)


NOW = "2026-07-14T00:00:00+00:00"


class _LegacySuccessAdapter:
    def deliver(self, _job, _context):
        return delivery_outcome(
            "succeeded", code="legacy_delivery_verified", message="legacy fixture"
        )


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
        (root / "正文" / ".gitkeep").write_bytes(b"\n")
        (root / "设定" / "世界.md").write_text("重力恒定。", encoding="utf-8")
        (root / "大纲" / "总纲.md").write_text("未知的后续方向。", encoding="utf-8")
        (root / "大纲" / "细纲_第010章.md").write_text("第十章细纲。", encoding="utf-8")
        (root / "追踪" / "伏笔.md").write_text("尚未裁决。", encoding="utf-8")
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy-run.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'{"schema_version":"1.0","legacy":true}\r\n')
        (root / ".novelagent" / "runtime" / "snapshot.json").write_text(
            '{"chapter_index":11,"legacy":true}\n', encoding="utf-8"
        )
        review = root / ".novelagent" / "runtime" / "reviews" / "legacy-review.json"
        review.parent.mkdir(parents=True)
        review.write_text('{"status":"legacy"}\n', encoding="utf-8")
        return root

    @staticmethod
    def _legacy_delivery_history(root: Path) -> tuple[Path, Path]:
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        queue = DeliveryQueue(
            root / ".novelagent" / "runtime" / "deliveries",
            clock=lambda: fixed_now,
        )
        queue.enqueue(
            job_id="legacy-job-1",
            book_id="legacy-book-1",
            run_id="legacy-run-1",
            publication_receipt_hash="a" * 64,
            target_type="file",
            target={
                "path_ref": {
                    "root_id": "delivery_store",
                    "relative_path": "legacy-export.md",
                }
            },
            payload={"content": "legacy chapter\n", "encoding": "utf-8"},
        )
        queue.attempt(
            "legacy-job-1",
            worker_id="legacy-worker-1",
            adapter=_LegacySuccessAdapter(),
        )
        job = queue.jobs_dir / "legacy-job-1.json"
        attempt = next((queue.attempts_dir / "legacy-job-1").glob("*.json"))
        return job, attempt

    def test_clean_interpreter_can_import_migration_preview_api(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from core.story_project.migration_v2 import build_migration_plan; print(build_migration_plan.__name__)",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual("build_migration_plan", completed.stdout.strip())

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
        self.assertEqual(2, plan["evidence_summary"]["published_prose_count"])
        self.assertNotIn("正文/.gitkeep", {item["relative_path"] for item in plan["sources"]})
        self.assertTrue(all(item["evidence_class"] == "static_constraint" for item in by_role["explicit_setting"]))
        self.assertTrue(all(item["evidence_class"] == "unknown" for item in by_role["master_outline"]))
        self.assertTrue(all(item["evidence_class"] == "unknown" for item in by_role["tracking_projection"]))
        self.assertEqual("legacy_artifact", by_role["legacy_runs"][0]["evidence_class"])
        self.assertEqual("legacy_artifact", by_role["legacy_reviews"][0]["evidence_class"])
        self.assertEqual("legacy_artifact", by_role["legacy_runtime"][0]["evidence_class"])
        self.assertEqual(2, plan["evidence_summary"]["occurred_event_evidence_count"])
        self.assertEqual(1, plan["evidence_summary"]["static_constraint_evidence_count"])
        self.assertEqual(plan, assert_migration_plan_current(plan, root))

    def test_plan_binds_deterministic_shadow_candidate_and_user_decision_topics(self) -> None:
        root = self._populated_book("shadow_candidate")

        first = build_migration_plan(root, created_at=NOW)
        second = build_migration_plan(root, created_at=NOW)

        self.assertEqual(first, second)
        candidate = first["shadow_candidate"]
        self.assertEqual("shadow", candidate["mode"])
        self.assertFalse(candidate["authoritative"])
        self.assertTrue(candidate["read_only"])
        self.assertIsNotNone(candidate["state"])
        self.assertEqual(10, candidate["chapter_index"])
        self.assertEqual("latest_published_chapter_fallback", candidate["target_basis"])
        self.assertEqual(canonical_json_hash(candidate), first["shadow_candidate_hash"])
        self.assertEqual(
            [
                "timeline_elapsed_minutes",
                "chapter_10_character_state",
                "open_foreshadowing",
                "inventory",
                "lexicon",
                "corruption",
            ],
            [item["topic"] for item in candidate["required_user_decisions"]],
        )
        self.assertTrue(
            all(item["status"] == "user_decision_required" for item in candidate["required_user_decisions"])
        )

        tampered = copy.deepcopy(first)
        tampered["shadow_candidate"]["warnings"].append({"code": "invented"})
        with self.assertRaises(MigrationV2Error):
            validate_migration_plan(tampered)

        legacy_v2 = copy.deepcopy(first)
        legacy_v2.pop("shadow_candidate")
        legacy_v2.pop("shadow_candidate_hash")
        legacy_v2["plan_hash"] = canonical_json_hash(legacy_v2, exclude_fields=("plan_hash",))
        self.assertEqual(legacy_v2, validate_migration_plan(legacy_v2))

    def test_candidate_unavailability_is_reported_instead_of_inventing_state(self) -> None:
        root = self._populated_book("candidate_unavailable")
        (root / "大纲" / "细纲_第010章.md").unlink()

        plan = build_migration_plan(root, created_at=NOW)
        candidate = plan["shadow_candidate"]

        self.assertIsNone(candidate["state"])
        self.assertEqual("no_unique_chapter_outline", candidate["target_basis"])
        self.assertEqual("semantic_candidate_unavailable", candidate["conflicts"][0]["code"])
        self.assertTrue(candidate["warnings"])
        self.assertTrue(candidate["unsupported"])
        self.assertTrue(
            all(not item["candidate_evidence_available"] for item in candidate["required_user_decisions"])
        )

    def test_preview_is_explicit_and_source_tree_is_byte_identical(self) -> None:
        root = self._populated_book("read_only_preview")
        before = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

        preview = build_migration_preview(root, created_at=NOW)

        after = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["authoritative"])
        self.assertFalse(preview["approval_created"])
        self.assertFalse(preview["execution_performed"])
        self.assertFalse(preview["activation_performed"])
        self.assertIsInstance(preview["source_conflicts"], list)
        self.assertIsInstance(preview["semantic_conflicts"], list)
        self.assertIsInstance(preview["warnings"], list)
        self.assertIsInstance(preview["unsupported"], list)
        self.assertEqual(6, len(preview["required_user_decisions"]))
        self.assertEqual(
            canonical_json_hash(preview, exclude_fields=("preview_hash",)),
            preview["preview_hash"],
        )

    def test_normal_cli_preview_is_generation_free_and_read_only(self) -> None:
        root = self._populated_book("cli_preview")
        before = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

        class FailingExecutor:
            def __init__(self, **kwargs):
                raise AssertionError("migration preview must not construct AgentExecutor")

        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--story-project",
                str(root),
                "--preview-event-authority-migration",
                "--migration-preview-created-at",
                NOW,
                "--output-json",
            ],
        ), patch.object(cli, "AgentExecutor", FailingExecutor), contextlib.redirect_stdout(output), self.assertRaises(
            SystemExit
        ) as raised:
            cli.main()

        self.assertEqual(0, raised.exception.code)
        payload = json.loads(output.getvalue())
        self.assertEqual("read_only_shadow", payload["mode"])
        self.assertEqual(NOW, payload["plan"]["created_at"])
        after = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_any_source_membership_or_byte_change_expires_plan(self) -> None:
        root = self._populated_book("stale")
        plan = build_migration_plan(root, created_at=NOW)

        (root / "追踪" / "伏笔.md").write_text("人工修改。", encoding="utf-8")
        with self.assertRaisesRegex(MigrationPlanStaleError, "source bytes"):
            assert_migration_plan_current(plan, root)

        root = self._populated_book("runtime_snapshot")
        plan = build_migration_plan(root, created_at=NOW)
        (root / ".novelagent" / "runtime" / "snapshot.json").write_text(
            '{"chapter_index":12,"legacy":true}\n', encoding="utf-8"
        )
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

    def test_legacy_delivery_job_and_attempt_receipt_are_frozen_and_cas_bound(self) -> None:
        root = self._populated_book("delivery_history")
        job, attempt = self._legacy_delivery_history(root)
        before = {path: path.read_bytes() for path in (job, attempt)}

        plan = build_migration_plan(root, created_at=NOW)

        sources = {item["relative_path"]: item for item in plan["sources"]}
        for path, content in before.items():
            relative = path.relative_to(root).as_posix()
            self.assertEqual("legacy_deliveries", sources[relative]["role"])
            self.assertEqual("legacy_artifact", sources[relative]["evidence_class"])
            self.assertEqual(canonical_json_hash(plan["sources"]), plan["source_digest"])
            self.assertEqual(content, path.read_bytes())

    def test_delivery_history_add_edit_delete_each_expires_plan_without_rewrite(self) -> None:
        mutations = ("add", "edit", "delete")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                root = self._populated_book(f"delivery_{mutation}")
                job, attempt = self._legacy_delivery_history(root)
                plan = build_migration_plan(root, created_at=NOW)
                job_before = job.read_bytes()
                attempt_before = attempt.read_bytes()

                if mutation == "add":
                    added = attempt.parent / "legacy-added.json"
                    added.write_bytes(b'{"schema_version":"1.0","legacy":true}\r\n')
                elif mutation == "edit":
                    job.write_bytes(job_before + b" ")
                else:
                    attempt.unlink()

                with self.assertRaises(MigrationPlanStaleError):
                    assert_migration_plan_current(plan, root)
                with self.assertRaisesRegex(
                    MigrationPlanStaleError, "during migration bootstrap"
                ):
                    assert_migration_source_snapshot_current(plan, root)

                if mutation == "add":
                    self.assertEqual(job_before, job.read_bytes())
                    self.assertEqual(attempt_before, attempt.read_bytes())
                    self.assertEqual(
                        b'{"schema_version":"1.0","legacy":true}\r\n',
                        added.read_bytes(),
                    )
                elif mutation == "edit":
                    self.assertEqual(job_before + b" ", job.read_bytes())
                    self.assertEqual(attempt_before, attempt.read_bytes())
                else:
                    self.assertEqual(job_before, job.read_bytes())
                    self.assertFalse(attempt.exists())

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

    def test_unclassified_prose_requires_explicit_exclusion_resolution(self) -> None:
        root = self._populated_book("unclassified")
        unknown = root / "正文" / "旧稿.md"
        unknown.write_text("无法从文件名判定章节。", encoding="utf-8")
        plan = build_migration_plan(root, created_at=NOW)
        conflict = next(
            item for item in plan["conflicts"]
            if item["code"] == "unclassified_chapter_source"
        )
        self.assertEqual(["正文/旧稿.md"], conflict["paths"])
        self.assertEqual(2, plan["evidence_summary"]["published_prose_count"])

        with self.assertRaisesRegex(
            MigrationV2Error, "migration_conflict_resolution_missing"
        ):
            build_migration_approval(
                plan,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )

        decisions = self._decisions()
        decisions["conflict_resolutions"] = {
            conflict["conflict_id"]: "正文/旧稿.md"
        }
        approval = build_migration_approval(
            plan,
            decisions=decisions,
            approver_id="operator",
            approved_at=NOW,
        )
        self.assertEqual(decisions["conflict_resolutions"], approval["decisions"]["conflict_resolutions"])

        invented = self._decisions()
        invented["conflict_resolutions"] = {
            "duplicate_chapter_source:published_prose:1": "正文/not-a-reported-source.md"
        }
        with self.assertRaisesRegex(MigrationV2Error, "migration_conflict_resolution_invalid"):
            build_migration_approval(
                plan,
                decisions=invented,
                approver_id="operator",
                approved_at=NOW,
            )

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
