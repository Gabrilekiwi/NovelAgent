from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.engine.executor import AgentExecutor
from core.engine.persistence_v2 import verify_publication_receipt
from core.memory_v2 import (
    apply_genesis_event,
    canonical_memory_to_snapshot,
    create_genesis_memory_batch,
    replay_memory_events,
    save_canonical_memory,
    write_memory_event_batch,
)
from core.runtime_paths import RuntimePaths
from core.schema import validate_schema
from core.story_project.authority import (
    activate_event_authority,
    project_identity_sha256,
)
from core.story_project.identity import ensure_project_identity, load_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.writer import StoryProjectWritebackConfig


class EventAuthorityExecutorV2E2ETest(unittest.TestCase):
    def _case_dir(self) -> Path:
        root = (
            Path.cwd()
            / ".tmp"
            / "test_executor_event_v2"
            / uuid.uuid4().hex
        )
        root.mkdir(parents=True)
        return root

    def _story_book(self, parent: Path) -> Path:
        root = parent / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        outline = canonical_outline_path(root, 1)
        outline.write_text(
            "\n".join(
                [
                    "# The sealed passage",
                    "",
                    "core_event: danger forces a costly route choice",
                    "",
                    "## required_beats",
                    "- danger forces the route choice",
                    "- open conflict over the serum",
                    "",
                    "ending_pressure: the locked door starts a countdown",
                ]
            ),
            encoding="utf-8",
        )
        return root

    @staticmethod
    def _validation(_snapshot: dict, _chapter: str, _decision: dict) -> dict:
        return validate_schema(
            {
                "ok": True,
                "requested_focus": ["logic"],
                "executed_checks": ["logic"],
                "skipped_checks": [],
                "checks": [{"name": "logic", "ok": True, "problems": []}],
                "problems": [],
                "blocking_problem_count": 0,
                "warning_count": 0,
                "severity_counts": [],
                "deterministic_repair_count": 0,
                "manual_review_count": 0,
                "repair_action_counts": [],
            },
            "validation_result.schema.json",
        )

    @staticmethod
    def _analysis(chapter: str, validation: dict) -> dict:
        return validate_schema(
            {
                "events": [{"text": "The sealed passage opens under pressure."}],
                "character_changes": [],
                "world_changes": [
                    {
                        "type": "countdown_started",
                        "text": "The locked door begins its countdown.",
                    }
                ],
                "new_locations": [],
                "story_state": {
                    "last_chapter_ending": chapter[-80:],
                    "last_scene_location": "sealed passage",
                    "last_scene_characters": [],
                    "open_threads": ["Reach the serum before the countdown ends."],
                    "required_opening_bridge": "",
                },
                "spatial_state": {
                    "spaces": {},
                    "connections": [],
                    "character_positions": {},
                    "blocked_paths": [],
                    "last_transition": {},
                },
                "conflicts": ["The survivors disagree over the serum."],
                "validation_ok": bool(validation.get("ok")),
                "summary": chapter[:80],
            },
            "analysis_result.schema.json",
        )

    def test_event_authority_commit_is_receipt_backed_v2_without_v1_fallback(self) -> None:
        case = self._case_dir()
        book = self._story_book(case)
        identity = ensure_project_identity(book, book_id="book-event-v2-e2e")
        paths = RuntimePaths.for_story_project(book)
        memory_root = paths.memory_dir / "v2"
        genesis = create_genesis_memory_batch(
            book_id=identity.book_id,
            title="Event authority e2e",
            source_project_digest="1" * 64,
            context_digest="2" * 64,
            language="en",
            authority_epoch=1,
        )
        genesis_projection = apply_genesis_event(genesis["events"][0])
        write_memory_event_batch(memory_root / "events", genesis)
        save_canonical_memory(memory_root / "canonical_memory.json", genesis_projection)
        activated = activate_event_authority(
            book,
            expected_identity_sha256=project_identity_sha256(book),
            head_event_hash=genesis_projection["head_event_hash"],
        )
        initial_snapshot = canonical_memory_to_snapshot(genesis_projection)
        paths.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        paths.snapshot_path.write_text(
            json.dumps(initial_snapshot, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        loader = build_generation_story_project_context_loader(
            story_project=book,
            chapter=1,
            project_identity=activated,
        )
        executor = AgentExecutor(
            snapshot_path=paths.snapshot_path,
            memory_path=paths.memory_dir / "unused_legacy_memory.json",
            run_dir=paths.run_dir,
            chapter_dir=paths.chapter_dir,
            persistence_dir=paths.persistence_dir,
            dry_run=True,
            memory_loader=lambda: {},
            polisher=lambda value: value,
            validator=self._validation,
            analyzer=self._analysis,
            story_project_context_loader=loader,
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
            enable_execution_provenance=False,
        )

        with (
            patch(
                "core.engine.executor.LocalPersistenceTransaction",
                side_effect=AssertionError("v1 persistence fallback was invoked"),
            ),
            patch(
                "core.engine.executor.prepare_chapter_memory_commit",
                side_effect=AssertionError("v1 Memory commit fallback was invoked"),
            ),
        ):
            result = executor.run_once(persist=True)

        self.assertTrue(result["committed"])
        self.assertTrue(result["run"]["committed"])
        self.assertEqual("committed", result["run"]["status"])
        self.assertEqual("v2", executor.persistence_coordinator.backend_id)

        replay = replay_memory_events(memory_root / "events")
        canonical = json.loads(
            (memory_root / "canonical_memory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, replay["batch_count"])
        self.assertEqual(1, replay["committed_chapter_count"])
        self.assertEqual(replay["projection"], canonical)
        self.assertNotEqual(
            genesis_projection["head_event_hash"], canonical["head_event_hash"]
        )

        persisted_identity = load_project_identity(book)
        self.assertIsNotNone(persisted_identity)
        self.assertEqual(
            canonical["head_event_hash"],
            persisted_identity.authority["head_event_hash"],
        )
        saved_snapshot = json.loads(paths.snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(2, saved_snapshot["chapter_index"])
        self.assertEqual(canonical["revision"], saved_snapshot["memory_v2"]["revision"])

        writeback_targets = result["run"]["story_project"]["writeback"]["targets"]
        prose_targets = [target for target in writeback_targets if target["kind"] == "prose"]
        self.assertEqual(1, len(prose_targets))
        prose_path = Path(prose_targets[0]["path"])
        self.assertTrue(prose_path.is_file())
        self.assertIn(result["chapter"], prose_path.read_text(encoding="utf-8"))
        self.assertEqual([], list((book / CORE_DIRECTORY_NAMES[3]).iterdir()))

        receipt_pointer = result["run"]["publication_receipt"]
        self.assertEqual(f"receipt-{result['run']['id']}", receipt_pointer["id"])
        self.assertNotIn("publication_receipt", result)
        receipt_path = paths.runtime_dir / Path(
            receipt_pointer["path_ref"]["relative_path"]
        )
        verification = verify_publication_receipt(
            receipt_path,
            root_map=paths.root_map(book),
        )
        self.assertTrue(verification["valid"], verification)
        self.assertTrue(verification["committed"], verification)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(receipt["apply_targets"]), 5)
        self.assertGreaterEqual(len(receipt["artifacts"]), 4)

        final_run_ref = receipt["final_run"]["path_ref"]
        final_run_path = paths.runtime_dir / Path(final_run_ref["relative_path"])
        final_result = json.loads(final_run_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt_pointer, final_result["run"]["publication_receipt"])
        for artifact in receipt["artifacts"]:
            binding = artifact["path_ref"]
            artifact_path = paths.root_map(book)[binding["root_id"]] / Path(
                binding["relative_path"]
            )
            self.assertTrue(artifact_path.is_file(), artifact_path)


if __name__ == "__main__":
    unittest.main()
