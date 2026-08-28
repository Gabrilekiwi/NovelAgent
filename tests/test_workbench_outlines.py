from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.paths import canonical_outline_path
from workbench.database import WorkbenchDatabase
from workbench.outlines import (
    OutlineBatchConflict,
    OutlineBatchError,
    OutlineBatchService,
)
from workbench.projects import ProjectRegistry


class WorkbenchOutlineBatchTest(unittest.TestCase):
    def _case(self, name: str):
        root = Path.cwd() / ".tmp" / "test_workbench_outlines" / f"{name}_{uuid.uuid4().hex}"
        workspace = root / "books"
        workspace.mkdir(parents=True)
        database = WorkbenchDatabase(root / "workbench.db")
        projects = ProjectRegistry(database, allowed_workspaces=[workspace])
        project = projects.create_project(workspace / "book", name="测试书")
        service = OutlineBatchService(database, projects)
        return project, service

    def _proposal(self, start: int = 1, count: int = 3):
        return [
            {
                "chapter_index": chapter,
                "title": f"第{chapter}次选择",
                "core_event": f"队伍完成第{chapter}次关键选择",
                "required_beats": [f"确认线索{chapter}", f"付出代价{chapter}"],
                "ending_pressure": f"新的倒计时{chapter}启动",
            }
            for chapter in range(start, start + count)
        ]

    def test_create_and_update_use_revision_and_sha_cas(self) -> None:
        project, service = self._case("cas")
        batch = service.create_batch(project.id, self._proposal())
        self.assertEqual(1, batch.revision)
        self.assertEqual(batch.proposal_sha256, batch.source_state_sha)

        changed = self._proposal()
        changed[0]["title"] = "新的标题"
        updated = service.update_batch(
            batch.id,
            changed,
            expected_revision=batch.revision,
            expected_sha256=batch.proposal_sha256,
        )
        self.assertEqual(2, updated.revision)
        self.assertNotEqual(batch.proposal_sha256, updated.proposal_sha256)

        with self.assertRaisesRegex(OutlineBatchConflict, "changed"):
            service.update_batch(
                batch.id,
                changed,
                expected_revision=batch.revision,
                expected_sha256=batch.proposal_sha256,
            )

    def test_proposal_requires_contiguous_complete_chapters(self) -> None:
        project, service = self._case("validation")
        missing = self._proposal(count=1)
        missing[0]["required_beats"] = []
        with self.assertRaisesRegex(OutlineBatchError, "required beat"):
            service.create_batch(project.id, missing)

        discontinuous = self._proposal(count=2)
        discontinuous[1]["chapter_index"] = 3
        with self.assertRaisesRegex(OutlineBatchError, "contiguous"):
            service.create_batch(project.id, discontinuous)

        with self.assertRaisesRegex(OutlineBatchError, "canonical next chapter"):
            service.create_batch(project.id, self._proposal(start=2, count=1))

    def test_approve_publishes_all_outlines_and_durable_evidence(self) -> None:
        project, service = self._case("approve")
        batch = service.create_batch(project.id, self._proposal())

        approved = service.approve_batch(
            batch.id,
            expected_revision=batch.revision,
            expected_sha256=batch.proposal_sha256,
        )

        self.assertEqual("approved", approved.status)
        for item in approved.items:
            path = canonical_outline_path(project.root, item.chapter_index)
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"# 第{item.chapter_index}章 {item.title}", text)
            self.assertIn(f"核心事件：{item.core_event}", text)
            self.assertIn("## 必写节拍", text)
            self.assertIn(f"结尾压力：{item.ending_pressure}", text)
        manifest_path = Path(approved.approval_manifest_path)
        receipt_path = manifest_path.with_name("approval_receipt.json")
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(receipt_path.is_file())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(approved.approval_receipt_sha256, receipt["receipt_sha256"])
        runtime_context = build_story_project_runtime_context(project.root, 1)
        self.assertEqual(approved.items[0].core_event, runtime_context.chapter_blueprint.core_event)
        self.assertEqual(
            list(approved.items[0].required_beats),
            [beat["text"] for beat in runtime_context.chapter_blueprint.required_beats],
        )
        self.assertEqual(
            approved.items[0].ending_pressure,
            runtime_context.chapter_blueprint.ending_pressure,
        )
        self.assertEqual((), runtime_context.chapter_blueprint.missing_fields)

        repeated = service.approve_batch(
            batch.id,
            expected_revision=batch.revision,
            expected_sha256=batch.proposal_sha256,
        )
        self.assertEqual(approved, repeated)

        # A crash after durable file publication but before the SQLite commit is
        # reconciled from the receipt instead of publishing the outlines again.
        with service.database.transaction() as connection:
            connection.execute(
                """
                UPDATE outline_batches
                SET status = 'draft', approved_at = NULL,
                    approval_manifest_path = NULL, approval_receipt_sha256 = NULL
                WHERE id = ?
                """,
                (batch.id,),
            )
        reconciled = service.approve_batch(
            batch.id,
            expected_revision=batch.revision,
            expected_sha256=batch.proposal_sha256,
        )
        self.assertEqual("approved", reconciled.status)
        self.assertEqual(approved.approval_receipt_sha256, reconciled.approval_receipt_sha256)

    def test_source_drift_blocks_approval_without_partial_publication(self) -> None:
        project, service = self._case("drift")
        batch = service.create_batch(project.id, self._proposal())
        (project.root / "设定" / "世界.md").write_text("世界发生了变化", encoding="utf-8")

        with self.assertRaisesRegex(OutlineBatchConflict, "sources changed"):
            service.approve_batch(
                batch.id,
                expected_revision=batch.revision,
                expected_sha256=batch.proposal_sha256,
            )
        self.assertFalse(any(canonical_outline_path(project.root, n).exists() for n in range(1, 4)))
        self.assertEqual("draft", service.get_batch(batch.id).status)

    def test_existing_outline_collision_refuses_entire_batch(self) -> None:
        project, service = self._case("collision")
        batch = service.create_batch(project.id, self._proposal())
        collision = canonical_outline_path(project.root, 2)
        collision.write_text("existing", encoding="utf-8")

        with self.assertRaisesRegex(OutlineBatchConflict, "already exist"):
            service.approve_batch(
                batch.id,
                expected_revision=batch.revision,
                expected_sha256=batch.proposal_sha256,
            )
        self.assertFalse(canonical_outline_path(project.root, 1).exists())
        self.assertEqual("existing", collision.read_text(encoding="utf-8"))
        self.assertFalse(canonical_outline_path(project.root, 3).exists())


if __name__ == "__main__":
    unittest.main()
