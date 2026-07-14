from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.delivery import DeliveryQueue, FileDeliveryAdapter
from core.delivery_intents import delivery_intent_receipt_binding
from core.engine.delivery_coordinator import DeliveryCoordinator
from core.engine.delivery_intent_recovery import recover_completed_delivery_jobs
from core.engine.executor import AgentExecutor
from core.engine.persistence import PersistenceError
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

    def _event_case(self) -> dict:
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

        external = case / "external-export"
        external.mkdir()
        profile = {
            "schema_version": "1.0",
            "profile_id": "canonical-json-export",
            "root_id": "external:canonical-json-export",
            "root_uuid": str(uuid.uuid4()),
            "relative_directory": "canonical-chapters",
            "filename_template": "chapter-{chapter_index}-{run_id}.json",
        }
        queue = DeliveryQueue(paths.delivery_dir)
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
            file_delivery_profile=profile,
            delivery_queue=queue,
        )
        return {
            "case": case,
            "book": book,
            "paths": paths,
            "memory_root": memory_root,
            "genesis_projection": genesis_projection,
            "external": external,
            "profile": profile,
            "queue": queue,
            "executor": executor,
        }

    def test_event_authority_commit_is_receipt_backed_v2_without_v1_fallback(self) -> None:
        fixture = self._event_case()
        book = fixture["book"]
        paths = fixture["paths"]
        memory_root = fixture["memory_root"]
        genesis_projection = fixture["genesis_projection"]
        executor = fixture["executor"]

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
        self.assertGreaterEqual(len(receipt["artifacts"]), 5)
        intent_artifacts = [
            artifact
            for artifact in receipt["artifacts"]
            if artifact["kind"] == "delivery_intent"
        ]
        self.assertEqual(1, len(intent_artifacts))
        intent_binding = intent_artifacts[0]["path_ref"]
        intent_path = paths.root_map(book)[intent_binding["root_id"]] / Path(
            intent_binding["relative_path"]
        )
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(
            fixture["profile"]["root_uuid"],
            intent["target"]["path_ref"]["root_uuid"],
        )
        self.assertEqual(
            [delivery_intent_receipt_binding(intent)], receipt["delivery_jobs"]
        )

        queue = fixture["queue"]
        job = queue.load(intent["intent_id"])
        self.assertEqual("pending", job["state"])
        self.assertEqual(0, job["attempt_count"])
        self.assertEqual([], list(fixture["external"].rglob("*.json")))

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

        delivery = DeliveryCoordinator(
            queue,
            adapters={
                "file": FileDeliveryAdapter(
                    root_map={fixture["profile"]["root_id"]: fixture["external"]}
                )
            },
            worker_id="event-v2-e2e-worker",
        ).reconcile(run_id=result["run"]["id"])
        self.assertTrue(delivery["ok"], delivery)
        self.assertEqual("succeeded", queue.load(intent["intent_id"])["state"])
        exported_path = fixture["external"] / Path(
            intent["target"]["path_ref"]["relative_path"]
        )
        self.assertTrue(exported_path.is_file())
        exported = json.loads(exported_path.read_text(encoding="utf-8"))
        self.assertEqual(intent["canonical_payload"], exported)
        self.assertEqual(result["run"]["id"], exported["run_id"])
        self.assertEqual(
            result["run"]["memory"]["v2"]["chapter_body_sha256"],
            exported["chapter_body_sha256"],
        )

    def test_completed_receipt_recovers_crash_before_delivery_enqueue(self) -> None:
        fixture = self._event_case()
        executor = fixture["executor"]
        with (
            patch(
                "core.engine.executor.LocalPersistenceTransaction",
                side_effect=AssertionError("v1 persistence fallback was invoked"),
            ),
            patch(
                "core.engine.executor.prepare_chapter_memory_commit",
                side_effect=AssertionError("v1 Memory commit fallback was invoked"),
            ),
            patch(
                "core.engine.executor.recover_delivery_jobs_for_receipt",
                side_effect=RuntimeError("simulated crash after receipt before enqueue"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "after receipt before enqueue"):
                executor.run_once(persist=True)

        queue = fixture["queue"]
        self.assertFalse(queue.jobs_dir.exists())
        restarted = AgentExecutor(
            snapshot_path=fixture["paths"].snapshot_path,
            run_dir=fixture["paths"].run_dir,
            chapter_dir=fixture["paths"].chapter_dir,
            persistence_dir=fixture["paths"].persistence_dir,
            story_project_context={"story_project_root": str(fixture["book"])},
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            enable_execution_provenance=False,
            file_delivery_profile=fixture["profile"],
            delivery_queue=queue,
        )
        for _ in range(2):
            with patch.object(
                restarted,
                "_run_once_impl",
                side_effect=RuntimeError("provider boundary reached"),
            ):
                with self.assertRaisesRegex(RuntimeError, "provider boundary"):
                    restarted.run_once(persist=True)
            jobs = list(queue.jobs_dir.glob("*.json"))
            self.assertEqual(1, len(jobs))
            self.assertEqual("pending", queue.load(jobs[0].stem)["state"])

        root_map = fixture["paths"].root_map(fixture["book"])
        first = recover_completed_delivery_jobs(
            fixture["paths"].persistence_dir,
            root_map=root_map,
            queue=queue,
        )
        second = recover_completed_delivery_jobs(
            fixture["paths"].persistence_dir,
            root_map=root_map,
            queue=queue,
        )

        self.assertEqual(1, first["receipt_count"])
        self.assertEqual(1, first["job_count"])
        self.assertEqual(first["jobs"], second["jobs"])
        job = queue.load(first["jobs"][0]["job_id"])
        self.assertEqual("pending", job["state"])
        self.assertEqual(0, job["attempt_count"])
        self.assertEqual([], list(fixture["external"].rglob("*.json")))

    def test_required_file_delivery_rejects_unpaired_untrusted_or_legacy_config(self) -> None:
        case = self._case_dir()
        queue = DeliveryQueue(case / "queue")
        profile = {
            "schema_version": "1.0",
            "profile_id": "canonical-json-export",
            "root_id": "external:canonical-json-export",
            "root_uuid": str(uuid.uuid4()),
            "relative_directory": "canonical-chapters",
            "filename_template": "chapter-{chapter_index}-{run_id}.json",
        }
        with self.assertRaisesRegex(ValueError, "configured together"):
            AgentExecutor(file_delivery_profile=profile)

        untrusted = dict(profile)
        untrusted["root_uuid"] = None
        with self.assertRaisesRegex(ValueError, "trusted external root_uuid"):
            AgentExecutor(file_delivery_profile=untrusted, delivery_queue=queue)

        book = self._story_book(case / "legacy")
        ensure_project_identity(book, book_id="book-legacy-delivery")
        paths = RuntimePaths.for_story_project(book)
        executor = AgentExecutor(
            snapshot_path=paths.snapshot_path,
            run_dir=paths.run_dir,
            chapter_dir=paths.chapter_dir,
            persistence_dir=paths.persistence_dir,
            story_project_context={"story_project_root": str(book)},
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            enable_execution_provenance=False,
            file_delivery_profile=profile,
            delivery_queue=DeliveryQueue(paths.delivery_dir),
        )
        with (
            patch.object(
                executor,
                "_run_once_impl",
                side_effect=AssertionError("provider must not start"),
            ),
            self.assertRaisesRegex(PersistenceError, "requires event-authority"),
        ):
            executor.run_once(persist=True)


if __name__ == "__main__":
    unittest.main()
