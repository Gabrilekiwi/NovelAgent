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
from core.story_project.mapper import SETTING_DIR_NAME
from core.story_project.paths import canonical_prose_path
from core.story_project.migration_v2 import (
    MIGRATION_BASELINE_CONTRACT,
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
        for chapter in range(2, 10):
            canonical_prose_path(root, chapter).write_text(
                f"Chapter {chapter} happened.\n", encoding="utf-8"
            )
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
                {
                    "id": "thread-door",
                    "status": "open",
                    "description": "The door remains open",
                    "evidence": "第十章门被打开",
                }
            ],
            "inventory": {"hero": {"key": 1, "water": 0}},
            "lexicon": {
                "black_tide": {"definition": "A dangerous black tide", "known_by": ["hero"]}
            },
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
        self.assertEqual(10, plan["evidence_summary"]["published_prose_count"])
        self.assertNotIn("正文/.gitkeep", {item["relative_path"] for item in plan["sources"]})
        self.assertTrue(all(item["evidence_class"] == "static_constraint" for item in by_role["explicit_setting"]))
        self.assertTrue(all(item["evidence_class"] == "unknown" for item in by_role["master_outline"]))
        self.assertTrue(all(item["evidence_class"] == "unknown" for item in by_role["tracking_projection"]))
        self.assertEqual("legacy_artifact", by_role["legacy_runs"][0]["evidence_class"])
        self.assertEqual("legacy_artifact", by_role["legacy_reviews"][0]["evidence_class"])
        self.assertEqual("legacy_artifact", by_role["legacy_runtime"][0]["evidence_class"])
        self.assertEqual(10, plan["evidence_summary"]["occurred_event_evidence_count"])
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
        self.assertEqual(MIGRATION_BASELINE_CONTRACT, first["baseline_contract"])
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
        with self.assertRaisesRegex(MigrationV2Error, "migration_semantic_candidate_unavailable"):
            build_migration_approval(
                legacy_v2,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )

        unbound = copy.deepcopy(first)
        unbound.pop("baseline_contract")
        unbound["plan_hash"] = canonical_json_hash(unbound, exclude_fields=("plan_hash",))
        self.assertEqual(unbound, validate_migration_plan(unbound))
        with self.assertRaisesRegex(MigrationV2Error, "migration_baseline_contract_unbound"):
            build_migration_approval(
                unbound,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )

        unsupported = copy.deepcopy(first)
        unsupported["baseline_contract"]["mapper_version"] = "migration-baseline-999.0"
        unsupported["plan_hash"] = canonical_json_hash(
            unsupported, exclude_fields=("plan_hash",)
        )
        with self.assertRaisesRegex(MigrationV2Error, "migration_baseline_contract_unsupported"):
            validate_migration_plan(unsupported)

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
        with self.assertRaisesRegex(MigrationV2Error, "migration_semantic_candidate_unavailable"):
            build_migration_approval(
                plan,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )

    def test_blocking_semantic_conflict_cannot_create_or_validate_approval(self) -> None:
        clean_root = self._populated_book("clean_approval")
        clean_plan = build_migration_plan(clean_root, created_at=NOW)
        clean_approval = build_migration_approval(
            clean_plan,
            decisions=self._decisions(),
            approver_id="operator",
            approved_at=NOW,
        )

        root = self._populated_book("blocking_candidate")
        (root / "追踪" / "上下文.md").write_text(
            "# 当前上下文\n\n当前位置：东门\n当前位置：西门\n",
            encoding="utf-8",
        )
        plan = build_migration_plan(root, created_at=NOW)
        self.assertTrue(
            any(item.get("blocking") is True for item in plan["shadow_candidate"]["conflicts"])
        )

        with self.assertRaisesRegex(MigrationV2Error, "migration_blocking_semantic_conflict"):
            build_migration_approval(
                plan,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )
        with self.assertRaisesRegex(MigrationV2Error, "migration_blocking_semantic_conflict"):
            validate_migration_approval(clean_approval, plan=plan)

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
            {
                **self._decisions(),
                "chapter_10_character_state": {"hero": {"storage_path": "relative.json"}},
            },
            {**self._decisions(), "chapter_10_character_state": {"hero": "injured"}},
            {
                **self._decisions(),
                "chapter_10_character_state": {"hero": {"name": None}},
            },
            {
                **self._decisions(),
                "chapter_10_character_state": {"hero": {"name": 123}},
            },
            {
                **self._decisions(),
                "chapter_10_character_state": {" hero ": {"name": "hero"}},
            },
            {
                **self._decisions(),
                "chapter_10_character_state": {1: {"name": "hero"}},
            },
            {
                **self._decisions(),
                "chapter_10_character_state": {
                    "hero-a": {"name": "hero", "location": "east"},
                    "hero-b": {"name": "hero", "location": "west"},
                },
            },
            {**self._decisions(), "inventory": {"hero": {"water": -1}}},
            {
                **self._decisions(),
                "inventory": {"hero": {"water": {"name": None, "quantity": 1}}},
            },
            {
                **self._decisions(),
                "inventory": {"hero": {"water": {"name": ["water"], "quantity": 1}}},
            },
            {**self._decisions(), "inventory": {" hero ": {"water": 1}}},
            {**self._decisions(), "inventory": {"hero": {1: 1, "1": 2}}},
            {**self._decisions(), "lexicon": {"black_tide": {"definition": ""}}},
            {**self._decisions(), "lexicon": {"black_tide": {"known_by": ["hero"]}}},
            {**self._decisions(), "lexicon": {"black_tide": 123}},
            {**self._decisions(), "lexicon": {"black_tide": ["definition"]}},
            {**self._decisions(), "lexicon": {" black_tide ": "definition"}},
            {
                **self._decisions(),
                "lexicon": {
                    "black_tide": {
                        "definition": "潮汐",
                        "external_id": "forged",
                    }
                },
            },
            {
                **self._decisions(),
                "lexicon": {
                    "black_tide": {
                        "definition": "潮汐",
                        "decision_digest": "0" * 64,
                    }
                },
            },
            {**self._decisions(), "corruption": {"hero": {}}},
            {**self._decisions(), "corruption": {"hero": 101}},
            {**self._decisions(), "corruption": {" hero ": 3}},
            {
                **self._decisions(),
                "open_foreshadowing": [
                    {"id": "same", "description": "one"},
                    {"id": "same", "description": "two"},
                ],
            },
            {
                **self._decisions(),
                "open_foreshadowing": [
                    {"id": "done", "status": "resolved", "description": "done"}
                ],
            },
            {
                **self._decisions(),
                "open_foreshadowing": [{"id": "missing-description"}],
            },
            {
                **self._decisions(),
                "open_foreshadowing": [{"id": {"value": "bad"}, "description": "text"}],
            },
            {
                **self._decisions(),
                "open_foreshadowing": [{"id": " bad ", "description": "text"}],
            },
            {
                **self._decisions(),
                "open_foreshadowing": [
                    {"id": "shadowed", "description": "   ", "content": "real"}
                ],
            },
            {
                **self._decisions(),
                "open_foreshadowing": [
                    {"id": "wrong-type", "description": 123, "content": "real"}
                ],
            },
        ):
            with self.subTest(decisions=decisions):
                with self.assertRaises(MigrationV2Error):
                    build_migration_approval(
                        plan,
                        decisions=decisions,
                        approver_id="operator",
                        approved_at=NOW,
                    )

    def test_approval_validation_rechecks_nested_decisions_and_timestamp(self) -> None:
        root = self._populated_book("approval_boundary")
        plan = build_migration_plan(root, created_at=NOW)
        approval = build_migration_approval(
            plan,
            decisions=self._decisions(),
            approver_id="operator",
            approved_at=NOW,
        )

        invalid_decisions = []
        invalid_character = copy.deepcopy(approval["decisions"])
        invalid_character["chapter_10_character_state"]["hero"] = "injured"
        invalid_decisions.append(invalid_character)
        invalid_character_name = copy.deepcopy(approval["decisions"])
        invalid_character_name["chapter_10_character_state"]["hero"]["name"] = {"value": "hero"}
        invalid_decisions.append(invalid_character_name)
        invalid_character_id = copy.deepcopy(approval["decisions"])
        invalid_character_id["chapter_10_character_state"] = {
            " hero ": {"name": "hero"}
        }
        invalid_decisions.append(invalid_character_id)
        invalid_inventory_name = copy.deepcopy(approval["decisions"])
        invalid_inventory_name["inventory"]["hero"]["key"] = {
            "name": 123,
            "quantity": 1,
        }
        invalid_decisions.append(invalid_inventory_name)
        invalid_foreshadow_id = copy.deepcopy(approval["decisions"])
        invalid_foreshadow_id["open_foreshadowing"][0]["id"] = " thread-door "
        invalid_decisions.append(invalid_foreshadow_id)
        conflicting_character_locations = copy.deepcopy(approval["decisions"])
        conflicting_character_locations["chapter_10_character_state"] = {
            "hero-a": {"name": "hero", "location": "east"},
            "hero-b": {"name": "hero", "location": "west"},
        }
        invalid_decisions.append(conflicting_character_locations)
        invalid_lexicon = copy.deepcopy(approval["decisions"])
        invalid_lexicon["lexicon"]["black_tide"]["external_id"] = "forged"
        invalid_decisions.append(invalid_lexicon)
        missing_lexicon_definition = copy.deepcopy(approval["decisions"])
        missing_lexicon_definition["lexicon"]["black_tide"].pop("definition")
        invalid_decisions.append(missing_lexicon_definition)
        invalid_lexicon_type = copy.deepcopy(approval["decisions"])
        invalid_lexicon_type["lexicon"]["black_tide"] = {"definition": ["wrong"]}
        invalid_decisions.append(invalid_lexicon_type)
        missing_foreshadow_description = copy.deepcopy(approval["decisions"])
        missing_foreshadow_description["open_foreshadowing"] = [{"id": "empty"}]
        invalid_decisions.append(missing_foreshadow_description)
        shadowed_foreshadow_description = copy.deepcopy(approval["decisions"])
        shadowed_foreshadow_description["open_foreshadowing"] = [
            {"id": "shadowed", "description": "   ", "content": "real"}
        ]
        invalid_decisions.append(shadowed_foreshadow_description)
        for decisions in invalid_decisions:
            tampered = copy.deepcopy(approval)
            tampered["decisions"] = decisions
            tampered["decision_digest"] = canonical_json_hash(decisions)
            tampered["approval_id"] = (
                "approval-"
                + canonical_json_hash(
                    {
                        "plan_hash": plan["plan_hash"],
                        "decision_digest": tampered["decision_digest"],
                    }
                )[:20]
            )
            tampered["approval_hash"] = canonical_json_hash(
                tampered, exclude_fields=("approval_hash",)
            )
            with self.subTest(decisions=decisions, path="validate"):
                with self.assertRaisesRegex(
                    MigrationV2Error, "migration_decisions_invalid"
                ):
                    validate_migration_approval(tampered, plan=plan)

        for approved_at in ("not-a-time", "2026-07-14T00:00:00"):
            with self.subTest(approved_at=approved_at, path="build"):
                with self.assertRaisesRegex(
                    MigrationV2Error, "migration_approval_time_invalid"
                ):
                    build_migration_approval(
                        plan,
                        decisions=self._decisions(),
                        approver_id="operator",
                        approved_at=approved_at,
                    )

            invalid_time = copy.deepcopy(approval)
            invalid_time["approved_at"] = approved_at
            invalid_time["approval_hash"] = canonical_json_hash(
                invalid_time, exclude_fields=("approval_hash",)
            )
            with self.subTest(approved_at=approved_at, path="validate"):
                with self.assertRaisesRegex(
                    MigrationV2Error, "migration_approval_time_invalid"
                ):
                    validate_migration_approval(invalid_time, plan=plan)

    def test_hash_excluded_static_setting_key_cannot_be_approved(self) -> None:
        root = self._populated_book("hash_excluded_static_key")
        (root / SETTING_DIR_NAME / "hash-bound.md").write_text(
            "# Hash bound setting\n\n"
            "| field | value |\n"
            "|---|---|\n"
            "| file_path | vault/door |\n",
            encoding="utf-8",
        )
        plan = build_migration_plan(root, created_at=NOW)
        self.assertIn(
            "file_path",
            plan["shadow_candidate"]["state"]["world_state"]["settings"][
                "Hash bound setting"
            ]["fields"],
        )

        with self.assertRaisesRegex(
            MigrationV2Error, "migration_static_field_hash_unbound"
        ):
            build_migration_approval(
                plan,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )
        self.assertFalse((root / ".novelagent" / "migration-v2").exists())

    def test_duplicate_published_chapter_source_blocks_approval(self) -> None:
        root = self._populated_book("conflict")
        (root / "正文" / "第001章_副本.md").write_text("冲突正文。", encoding="utf-8")
        plan = build_migration_plan(root, created_at=NOW)
        self.assertEqual("duplicate_chapter_source", plan["conflicts"][0]["code"])

        with self.assertRaisesRegex(
            MigrationV2Error, "migration_duplicate_chapter_source_blocking"
        ):
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
        with self.assertRaisesRegex(
            MigrationV2Error, "migration_duplicate_chapter_source_blocking"
        ):
            build_migration_approval(
                plan,
                decisions=decisions,
                approver_id="operator",
                approved_at=NOW,
            )
        clean_root = self._populated_book("clean_duplicate_validation")
        clean_plan = build_migration_plan(clean_root, created_at=NOW)
        clean_approval = build_migration_approval(
            clean_plan,
            decisions=self._decisions(),
            approver_id="operator",
            approved_at=NOW,
        )
        with self.assertRaisesRegex(
            MigrationV2Error, "migration_duplicate_chapter_source_blocking"
        ):
            validate_migration_approval(clean_approval, plan=plan)

    def test_duplicate_next_outline_blocks_approval(self) -> None:
        root = self._populated_book("duplicate_next_outline")
        (root / "大纲" / "细纲_第011章_A.md").write_text("# 第十一章 A\n", encoding="utf-8")
        (root / "大纲" / "细纲_第011章_B.md").write_text("# 第十一章 B\n", encoding="utf-8")
        plan = build_migration_plan(root, created_at=NOW)
        conflict = next(
            item
            for item in plan["conflicts"]
            if item["code"] == "duplicate_chapter_source"
            and item["role"] == "chapter_outline"
        )
        decisions = self._decisions()
        decisions["conflict_resolutions"] = {
            "duplicate_chapter_source:chapter_outline:11": conflict["paths"][0]
        }
        with self.assertRaisesRegex(
            MigrationV2Error, "migration_duplicate_chapter_source_blocking"
        ):
            build_migration_approval(
                plan,
                decisions=decisions,
                approver_id="operator",
                approved_at=NOW,
            )

    def test_published_chapter_gap_blocks_build_and_validate_approval(self) -> None:
        root = self._populated_book("published_gap")
        canonical_prose_path(root, 5).unlink()
        plan = build_migration_plan(root, created_at=NOW)
        with self.assertRaisesRegex(MigrationV2Error, "migration_published_chapter_gap"):
            build_migration_approval(
                plan,
                decisions=self._decisions(),
                approver_id="operator",
                approved_at=NOW,
            )

        clean_root = self._populated_book("clean_gap_validation")
        clean_plan = build_migration_plan(clean_root, created_at=NOW)
        clean_approval = build_migration_approval(
            clean_plan,
            decisions=self._decisions(),
            approver_id="operator",
            approved_at=NOW,
        )
        with self.assertRaisesRegex(MigrationV2Error, "migration_published_chapter_gap"):
            validate_migration_approval(clean_approval, plan=plan)

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
        self.assertEqual(10, plan["evidence_summary"]["published_prose_count"])

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

        windows_decisions = self._decisions()
        windows_decisions["conflict_resolutions"] = {
            conflict["conflict_id"]: conflict["paths"][0].replace("/", "\\")
        }
        normalized_approval = build_migration_approval(
            plan,
            decisions=windows_decisions,
            approver_id="operator",
            approved_at=NOW,
        )
        self.assertEqual(
            conflict["paths"][0],
            normalized_approval["decisions"]["conflict_resolutions"][
                conflict["conflict_id"]
            ],
        )

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
