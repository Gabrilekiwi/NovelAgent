from __future__ import annotations

import argparse
import unittest

import main as cli
import core.story_project as story_project
from core.cli.arguments import apply_notion_shortcuts
from core.cli.config import story_project_writeback_config_from_args
from core.cli.output import format_delivery_command_summary
from core.engine import (
    DeliveryCoordinator,
    PersistenceCoordinator,
    QualityCoordinator,
    StoryProjectContextService,
)
from core.engine.executor import AgentExecutor
from core.engine.story_project_context import StoryProjectContextError
from core.review.repair_loop import ReviewRepairConfig
from core.review.runtime import RuntimeReviewConfig
from core.story_project.writer import StoryProjectWritebackConfig


class CoordinatorStructureTest(unittest.TestCase):
    def test_executor_owns_extracted_coordinators(self) -> None:
        executor = AgentExecutor(dry_run=True)

        self.assertIsInstance(executor.story_project_context_service, StoryProjectContextService)
        self.assertIsInstance(executor.quality_coordinator, QualityCoordinator)
        self.assertIsInstance(executor.persistence_coordinator, PersistenceCoordinator)

    def test_story_project_service_preserves_strict_writeback_guard(self) -> None:
        with self.assertRaisesRegex(StoryProjectContextError, "strict_story_state_requires_apply_writeback"):
            StoryProjectContextService.require_strict_writeback(
                {"story_state_mode": "strict"},
                persist=True,
                writeback_mode="none",
            )

    def test_executor_rejects_story_project_apply_without_persistence(self) -> None:
        executor = AgentExecutor(
            dry_run=True,
            story_project_context={},
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
        )

        with self.assertRaisesRegex(StoryProjectContextError, "story_project_apply_requires_persistence"):
            executor.run_once(persist=False)

    def test_top_level_story_project_exports_preview_not_direct_apply(self) -> None:
        self.assertTrue(hasattr(story_project, "build_story_project_writeback_plan"))
        self.assertFalse(hasattr(story_project, "run_story_project_writeback"))

    def test_quality_coordinator_preserves_standard_apply_default(self) -> None:
        policy = QualityCoordinator.effective_policy(
            configured_policy=None,
            persist=True,
            story_project_apply=True,
            has_story_project_context=True,
            review_config=RuntimeReviewConfig(),
            review_repair_config=ReviewRepairConfig(),
        )

        self.assertEqual("standard", policy.name)

    def test_persistence_coordinator_filters_private_candidate_fields(self) -> None:
        result = {"run": {}}
        public = PersistenceCoordinator.attach(
            result,
            {
                "run_id": "run-1",
                "state": "preview",
                "committed": False,
                "partial": False,
                "targets": [],
                "errors": [],
                "private": "omit",
            },
        )

        self.assertNotIn("private", public)
        self.assertIs(result["persistence"], result["run"]["persistence"])

    def test_delivery_coordinator_routes_inspection(self) -> None:
        class Queue:
            @staticmethod
            def inspect(job_id: str) -> dict:
                return {"job": {"job_id": job_id, "state": "pending"}}

        result = DeliveryCoordinator(Queue(), adapters={}, worker_id="worker-1").inspect("job-1")

        self.assertEqual("inspect_delivery", result["command"])
        self.assertEqual("job-1", result["inspection"]["job"]["job_id"])

    def test_main_reexports_split_cli_surfaces(self) -> None:
        self.assertIs(cli.apply_notion_shortcuts, apply_notion_shortcuts)
        self.assertIs(cli.format_delivery_command_summary, format_delivery_command_summary)
        args = argparse.Namespace(
            story_project_writeback=False,
            story_project_writeback_dry_run=False,
            story_project_overwrite=False,
            dry_run=False,
            persist_dry_run=False,
            story_project=None,
            memory_writeback="none",
        )
        self.assertEqual("none", story_project_writeback_config_from_args(args).mode)


if __name__ == "__main__":
    unittest.main()
