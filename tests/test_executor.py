from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from api.contracts import ModelCallError, ModelOutputError, ModelResponse
from core.director import DirectorDecisionError
from core.engine.executor import AgentExecutor, LoopExecutionError
from core.execution_provenance import validate_execution_provenance
from core.model_call_runtime import ProviderCallUncertainError, current_model_call_runtime
from core.model_calls import ModelCallStore
from core.engine.persistence import LocalPersistenceTransaction, PersistenceError, PersistenceTarget
from core.engine.run_record import build_run_record
from core.engine.workflow import WorkflowError
from core.review.repair_loop import ReviewRepairConfig
from core.review.runtime import RuntimeReviewConfig
from core.schema import SchemaValidationError, validate_schema
from core.state.memory_writer import FileMemoryWriter
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.identity import ensure_project_identity
from core.story_project.paths import PROSE_DIR_NAME, canonical_outline_path, canonical_prose_path
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.writer import StoryProjectWritebackConfig


class AgentExecutorTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_executor" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _write_snapshot(self, path: Path) -> str:
        snapshot = {
            "chapter_index": 2,
            "world_state": {
                "infection_level": "medium",
                "locations": {},
            },
            "characters": {},
            "timeline": [],
        }
        content = json.dumps(snapshot, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        return content

    def _story_book(self, parent: Path) -> Path:
        root = parent / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        return root

    def _set_snapshot_language(self, path: Path, language: str = "en") -> None:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["project_profile"] = {"language": language}
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    def _story_outline(self, root: Path, chapter: int, title: str) -> Path:
        path = canonical_outline_path(root, chapter)
        path.write_text(
            "\n".join(
                [
                    f"# {title}",
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
        return path

    def _ok_validation(self, snapshot: dict, chapter: str, decision: dict) -> dict:
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

    def _analysis(self, chapter: str, validation: dict) -> dict:
        return validate_schema(
            {
                "events": [{"text": chapter[:40]}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "story_state": {
                    "last_chapter_ending": chapter[-80:],
                    "last_scene_location": "",
                    "last_scene_characters": [],
                    "open_threads": [],
                    "required_opening_bridge": "",
                },
                "spatial_state": {
                    "spaces": {},
                    "connections": [],
                    "character_positions": {},
                    "blocked_paths": [],
                    "last_transition": {},
                },
                "conflicts": [],
                "validation_ok": bool(validation.get("ok")),
                "summary": chapter[:80],
            },
            "analysis_result.schema.json",
        )

    def test_dry_run_without_persist_leaves_snapshot_unchanged(self) -> None:
        tmp_path = self._case_dir("dry_run_no_persist")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
        ).run_once(persist=False)

        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        self.assertFalse((tmp_path / "runs").exists())
        self.assertIsNone(result["run"]["execution_evidence"]["provenance_artifact_ref"])
        self.assertIsNone(result["run"]["execution_evidence"]["model_calls_ref"])

    def test_event_authority_selects_v2_backend_without_fallback(self) -> None:
        tmp_path = self._case_dir("event_backend")
        book = self._story_book(tmp_path)
        executor = AgentExecutor(
            snapshot_path=tmp_path / "runtime" / "snapshot.json",
            run_dir=tmp_path / "runtime" / "runs",
            persistence_dir=tmp_path / "runtime" / "persistence",
            chapter_dir=tmp_path / "runtime" / "chapters",
            story_project_context={"story_project_root": str(book)},
            enable_execution_provenance=False,
        )
        executor._last_project_identity = {
            "book_id": "book-event",
            "authority": {"mode": "event_v1"},
        }

        executor._configure_authority_persistence_backend()

        self.assertEqual("v2", executor.persistence_coordinator.backend_id)
        self.assertEqual(
            book.resolve(), executor._event_authority_root_map["story_project"]
        )
        self.assertTrue((tmp_path / "runtime" / "deliveries").is_dir())

    def test_prior_event_activation_permanently_blocks_legacy_backend(self) -> None:
        tmp_path = self._case_dir("event_downgrade")
        book = self._story_book(tmp_path)
        receipts = book / ".novelagent" / "authority" / "receipts"
        receipts.mkdir(parents=True)
        (receipts / "activation.json").write_text(
            json.dumps({"receipt_type": "authority_activation"}), encoding="utf-8"
        )
        executor = AgentExecutor(
            run_dir=tmp_path / "runtime" / "runs",
            persistence_dir=tmp_path / "runtime" / "persistence",
            story_project_context={"story_project_root": str(book)},
            enable_execution_provenance=False,
        )
        executor._last_project_identity = {
            "book_id": "book-event",
            "authority": {"mode": "legacy_markdown_v1"},
        }

        with self.assertRaisesRegex(PersistenceError, "downgrade"):
            executor._configure_authority_persistence_backend()

    def test_persist_updates_snapshot_and_writes_run_record(self) -> None:
        tmp_path = self._case_dir("persist")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
        ).run_once(persist=True)

        saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(3, saved["chapter_index"])
        self.assertEqual(1, len(saved["timeline"]))
        self.assertTrue(saved["timeline"][0]["summary"])
        self.assertEqual(saved["timeline"][0]["source_run_id"], result["run"]["id"])
        self.assertEqual("chapter_2:timeline_event:chapter_2_summary", saved["timeline"][0]["memory_id"])
        self.assertIn("chapter_2:timeline_event:chapter_2_event_1", saved["timeline"][0]["memory_ids"])
        self.assertGreater(len(saved["timeline"][0]["events"]), 0)
        self.assertGreater(len(saved["timeline"][0]["world_changes"]), 0)
        self.assertGreater(len(saved["timeline"][0]["conflicts"]), 0)
        self.assertIn("shelter", saved["world_state"]["locations"])
        self.assertGreater(len(saved["world_state"]["last_world_changes"]), 0)
        self.assertTrue(result["validation"]["ok"])
        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved_run = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertIs(saved_run, validate_schema(saved_run, "run_result.schema.json"))
        evidence = saved_run["run"]["execution_evidence"]
        provenance_path = tmp_path / "runs" / Path(evidence["provenance_artifact_ref"])
        self.assertTrue(provenance_path.is_file())
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["provenance_hash"], provenance["provenance_hash"])
        self.assertEqual(provenance, validate_execution_provenance(provenance))
        self.assertFalse(Path(evidence["provenance_artifact_ref"]).is_absolute())
        self.assertFalse(Path(evidence["model_calls_ref"]).is_absolute())
        self.assertEqual("committed", saved_run["run"]["status"])
        self.assertTrue(saved_run["run"]["committed"])
        self.assertEqual("rule", saved_run["run"]["director"]["mode"])
        self.assertEqual("core.director.director.decide_next_step", saved_run["run"]["director"]["source"])
        self.assertIsNone(saved_run["run"]["director"]["model"])
        self.assertEqual("completed", saved_run["run"]["director"]["status"])
        self.assertGreaterEqual(saved_run["run"]["director"]["duration_ms"], 0)
        self.assertIs(
            saved_run["run"]["director"],
            validate_schema(saved_run["run"]["director"], "director_audit.schema.json"),
        )

        self.assertEqual(
            ["generate_chapter", "polish", "validate", "repair_if_needed"],
            [event["action"] for event in saved_run["run"]["trace"]],
        )
        self.assertEqual(
            ["generate_chapter", "polish", "validate", "repair_if_needed"],
            saved_run["run"]["workflow_plan"]["actions"],
        )
        self.assertEqual("generate_chapter", saved_run["run"]["workflow_plan"]["steps"][0]["action"])
        self.assertFalse(saved_run["run"]["workflow_plan"]["recovery"])
        self.assertIs(
            saved_run["run"]["workflow_plan"],
            validate_schema(saved_run["run"]["workflow_plan"], "workflow_plan.schema.json"),
        )
        planned_steps = {
            step["action"]: step
            for step in saved_run["run"]["workflow_plan"]["steps"]
        }
        for event in saved_run["run"]["trace"]:
            self.assertIs(event, validate_schema(event, "trace_event.schema.json"))
            planned_step = planned_steps[event["action"]]
            self.assertEqual(planned_step["index"], event["plan_step_index"])
            self.assertEqual(planned_step["mode"], event["plan_step_mode"])
            self.assertEqual(planned_step["failure_policy"], event["plan_failure_policy"])
        model_trace = {event["action"]: event for event in saved_run["run"]["trace"]}
        self.assertEqual("chapter_generation", model_trace["generate_chapter"]["model_stage"])
        self.assertEqual("local", model_trace["generate_chapter"]["model_provider"])
        self.assertIsNone(model_trace["generate_chapter"]["model_name"])

        self.assertEqual("dry_run", model_trace["generate_chapter"]["model_invocation"])
        self.assertEqual("claude_polish", model_trace["polish"]["model_stage"])
        self.assertEqual("local", model_trace["polish"]["model_provider"])
        self.assertEqual("dry_run", model_trace["polish"]["model_invocation"])
        self.assertNotIn("model_stage", model_trace["validate"])
        self.assertEqual("scene_repair", model_trace["repair_if_needed"]["model_stage"])
        self.assertIsNone(model_trace["repair_if_needed"]["model_provider"])
        self.assertEqual("none", model_trace["repair_if_needed"]["model_invocation"])
        self.assertTrue(all(event["status"] == "completed" for event in saved_run["run"]["trace"]))
        self.assertGreater(saved_run["run"]["trace"][0]["chapter_chars"], 0)
        self.assertTrue(saved_run["run"]["trace"][-1]["validation_ok"])
        self.assertGreater(saved_run["run"]["snapshot_builder"]["chars"], 0)
        self.assertEqual(0, saved_run["run"]["snapshot_builder"]["audit"]["item_count"])
        self.assertEqual(0, saved_run["run"]["snapshot_builder"]["audit"]["applied_count"])
        snapshot_pack_artifact_path = Path(saved_run["run"]["snapshot_builder"]["artifact"]["path"])
        self.assertTrue(snapshot_pack_artifact_path.exists())
        self.assertIn("# Snapshot Input Pack: Chapter 2", snapshot_pack_artifact_path.read_text(encoding="utf-8"))
        self.assertIn("Snapshot Prompt", snapshot_pack_artifact_path.read_text(encoding="utf-8"))
        self.assertGreater(saved_run["run"]["input_pack"]["chars"], 0)
        self.assertIs(
            saved_run["run"]["input_pack"]["metadata"],
            validate_schema(saved_run["run"]["input_pack"]["metadata"], "input_pack_metadata.schema.json"),
        )
        self.assertEqual(2, saved_run["run"]["input_pack"]["metadata"]["chapter_index"])
        self.assertIn("memory_index", saved_run["run"]["input_pack"]["metadata"]["sections"])
        input_pack_artifact_path = Path(saved_run["run"]["input_pack"]["artifact"]["path"])
        self.assertTrue(input_pack_artifact_path.exists())
        self.assertIn("# Input Pack: Chapter 2", input_pack_artifact_path.read_text(encoding="utf-8"))
        self.assertIn("# Memory Index", input_pack_artifact_path.read_text(encoding="utf-8"))
        self.assertEqual([], saved_run["run"]["validation"]["problem_codes"])
        self.assertGreater(saved_run["run"]["analysis"]["conflict_count"], 0)
        self.assertGreater(saved_run["run"]["analysis"]["event_count"], 0)
        self.assertGreater(saved_run["run"]["analysis"]["world_change_count"], 0)
        self.assertTrue(saved_run["run"]["analysis"]["summary"])
        self.assertTrue(saved_run["run"]["state_update"]["applied"])
        self.assertEqual(2, saved_run["run"]["state_update"]["chapter_index"])
        self.assertEqual(3, saved_run["run"]["state_update"]["next_chapter_index"])
        self.assertEqual(1, saved_run["run"]["state_update"]["timeline_added"])
        self.assertGreater(saved_run["run"]["state_update"]["memory_update_count"], 0)
        self.assertIs(
            saved_run["run"]["state_update"],
            validate_schema(saved_run["run"]["state_update"], "state_update_audit.schema.json"),
        )
        artifact_path = Path(saved_run["run"]["chapter"]["artifact"]["path"])
        self.assertTrue(artifact_path.exists())
        self.assertIn("# Chapter 2", artifact_path.read_text(encoding="utf-8"))
        pipeline = saved_run["run"]["chapter"]["pipeline"]
        self.assertEqual(2, pipeline["chapter_index"])
        self.assertEqual(3, pipeline["scene_count"])
        self.assertGreater(pipeline["merged_chars"], 0)
        self.assertEqual(3, len(pipeline["scene_spans"]))
        self.assertEqual(0, pipeline["scene_spans"][0]["start_char"])
        self.assertGreater(pipeline["scene_spans"][0]["end_char"], pipeline["scene_spans"][0]["start_char"])
        self.assertEqual(
            ["plan_chapter", "generate_scenes", "merge_scenes", "validate", "repair", "commit"],
            [stage["name"] for stage in pipeline["stages"]],
        )
        stage_statuses = {stage["name"]: stage["status"] for stage in pipeline["stages"]}
        self.assertEqual("completed", stage_statuses["plan_chapter"])
        self.assertEqual("completed", stage_statuses["generate_scenes"])
        self.assertEqual("completed", stage_statuses["merge_scenes"])
        self.assertEqual("completed", stage_statuses["validate"])
        self.assertEqual("skipped", stage_statuses["repair"])
        self.assertEqual("completed", stage_statuses["commit"])
        pipeline_artifacts = pipeline["artifacts"]
        for name in ("plan", "merged_chapter", "validation_report", "repair_deltas"):
            with self.subTest(pipeline_artifact=name):
                self.assertTrue(Path(pipeline_artifacts[name]["path"]).exists())
        self.assertEqual(3, len(pipeline_artifacts["scene_drafts"]))
        for scene_artifact in pipeline_artifacts["scene_drafts"]:
            self.assertTrue(Path(scene_artifact["path"]).exists())
            self.assertIn("Merged Span", Path(scene_artifact["path"]).read_text(encoding="utf-8"))

    def test_executor_injects_shared_model_call_store_and_budget_before_provider(self) -> None:
        tmp_path = self._case_dir("model_call_evidence")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        private_prompt = "private prompt must only be hashed"

        def generator(_: str) -> str:
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            assert runtime is not None
            return str(
                runtime.execute_attempt(
                    call_id="executor-generation",
                    attempt_number=1,
                    provider="openai",
                    model="gpt-test",
                    stage="chapter_generation",
                    endpoint_type="official",
                    request={"messages": [{"role": "user", "content": private_prompt}]},
                    max_output_tokens=20,
                    input_tokens=4,
                    operation=lambda: ModelResponse(
                        "The shelter alarm failed, so the group crossed the flooded service tunnel.",
                        usage={"input_tokens": 4, "output_tokens": 8},
                        finish_reason="stop",
                        request_id="req-executor",
                        actual_model="gpt-test-actual",
                        endpoint_type="official",
                    ),
                )
            )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            generator=generator,
            polisher=lambda chapter: chapter,
            validator=self._ok_validation,
            analyzer=self._analysis,
        ).run_once(persist=True)

        evidence = result["run"]["execution_evidence"]
        model_root = tmp_path / "runs" / Path(evidence["model_calls_ref"])
        provenance = json.loads(
            (tmp_path / "runs" / Path(evidence["provenance_artifact_ref"])).read_text(
                encoding="utf-8"
            )
        )
        public_config = {
            item["name"]: item["value"] for item in provenance["config"]
        }
        self.assertEqual(
            {
                "openai": provenance["model"]["model"],
                "anthropic": public_config["configured_models"]["anthropic"],
            },
            public_config["configured_models"],
        )
        intent_path = model_root / "intents" / "executor-generation-a1.json"
        receipt_path = model_root / "receipts" / "executor-generation-a1.json"
        response_path = model_root / "responses" / "executor-generation-a1.txt"
        self.assertTrue(intent_path.is_file())
        self.assertTrue(receipt_path.is_file())
        self.assertTrue(response_path.is_file())
        self.assertNotIn(private_prompt, intent_path.read_text(encoding="utf-8"))
        self.assertEqual("req-executor", json.loads(receipt_path.read_text(encoding="utf-8"))["request_id"])
        self.assertEqual(1, evidence["budget"]["provider_calls"])
        self.assertEqual(8, evidence["budget"]["total_output_tokens"])
        self.assertEqual(0, evidence["budget"]["reserved_output_tokens"])

    def test_executor_refuses_new_provider_work_when_prior_intent_is_uncertain(self) -> None:
        tmp_path = self._case_dir("prior_uncertain_model_call")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        run_dir = tmp_path / "runs"
        store = ModelCallStore(run_dir / "executions" / "execution_old" / "model_calls")
        store.create_intent(
            call_id="old-call",
            attempt_id="old-call-a1",
            provider="openai",
            model="gpt-test",
            stage="chapter_generation",
            budget_reservation={
                "reserved_input_tokens": 1,
                "reserved_output_tokens": 5,
                "reserved_total_tokens": 6,
            },
            request={"messages": [{"role": "user", "content": "private"}]},
        )

        with self.assertRaises(ProviderCallUncertainError) as caught:
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=run_dir,
                dry_run=True,
            ).run_once(persist=True)

        self.assertEqual("old-call-a1", caught.exception.attempt_id)
        self.assertEqual(
            ["execution_old"],
            [path.name for path in (run_dir / "executions").iterdir()],
        )

    def test_review_auto_repair_accepts_repaired_chapter_before_commit(self) -> None:
        tmp_path = self._case_dir("review_repair_accept")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        reviews = [
            {
                "enabled": True,
                "status": "blocked",
                "decision": "blocked",
                "quality_score": 40,
                "rule_score": 20,
                "repair_task_count": 1,
                "blocking_task_count": 1,
                "artifacts_dir": str(tmp_path / "reviews" / "original"),
                "summary_path": None,
            },
            {
                "enabled": True,
                "status": "pass",
                "decision": "accept",
                "quality_score": 90,
                "rule_score": 90,
                "repair_task_count": 0,
                "blocking_task_count": 0,
                "artifacts_dir": str(tmp_path / "reviews" / "repair_attempt_01"),
                "summary_path": None,
            },
        ]

        with patch("core.engine.executor.run_runtime_review", side_effect=reviews):
            result = AgentExecutor(
                snapshot_path=snapshot_path,
                memory_path=tmp_path / "missing_memory.json",
                run_dir=tmp_path / "runs",
                chapter_dir=tmp_path / "chapters",
                dry_run=True,
                generator=lambda _input_pack: "original chapter",
                polisher=lambda chapter: chapter,
                validator=self._ok_validation,
                repairer=lambda chapter, _validation, _input_pack, _plan, _recovery: chapter + " fixed",
                analyzer=self._analysis,
                review_config=RuntimeReviewConfig(enabled=True, output_dir=tmp_path / "reviews"),
                review_repair_config=ReviewRepairConfig(enabled=True),
            ).run_once(persist=True)

        self.assertTrue(result["committed"])
        self.assertEqual("original chapter fixed", result["chapter"])
        self.assertTrue(result["run"]["review_repair"]["accepted"])
        self.assertEqual("pass", result["run"]["review_pipeline"]["status"])
        self.assertTrue((tmp_path / "runs" / "review_repairs" / result["run"]["id"] / "repaired_chapter_final.md").exists())

    def test_review_auto_repair_rejects_when_post_repair_review_still_blocked(self) -> None:
        tmp_path = self._case_dir("review_repair_reject")
        snapshot_path = tmp_path / "snapshot.json"
        before_snapshot = self._write_snapshot(snapshot_path)
        blocked_review = {
            "enabled": True,
            "status": "blocked",
            "decision": "blocked",
            "quality_score": 20,
            "rule_score": 20,
            "repair_task_count": 1,
            "blocking_task_count": 1,
            "artifacts_dir": str(tmp_path / "reviews"),
            "summary_path": None,
        }

        with patch("core.engine.executor.run_runtime_review", side_effect=[blocked_review, blocked_review]):
            result = AgentExecutor(
                snapshot_path=snapshot_path,
                memory_path=tmp_path / "missing_memory.json",
                run_dir=tmp_path / "runs",
                chapter_dir=tmp_path / "chapters",
                dry_run=True,
                generator=lambda _input_pack: "original chapter",
                polisher=lambda chapter: chapter,
                validator=self._ok_validation,
                repairer=lambda chapter, _validation, _input_pack, _plan, _recovery: chapter + " fixed",
                analyzer=self._analysis,
                review_config=RuntimeReviewConfig(enabled=True, output_dir=tmp_path / "reviews"),
                review_repair_config=ReviewRepairConfig(enabled=True),
            ).run_once(persist=True)

        self.assertFalse(result["committed"])
        self.assertEqual("original chapter fixed", result["chapter"])
        self.assertEqual(result["validation"], result["run"]["review_repair"]["final_validation"])
        self.assertFalse(result["run"]["review_repair"]["accepted"])
        self.assertEqual("post_repair_review_blocked", result["run"]["review_repair"]["rejected_reason"])
        self.assertEqual("rejected", result["run"]["status"])
        self.assertEqual(before_snapshot, snapshot_path.read_text(encoding="utf-8"))

    def test_story_project_context_records_blueprint_coverage_without_writeback(self) -> None:
        tmp_path = self._case_dir("story_project_audit")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        (tmp_path / "book").mkdir()
        story_project_context = {
            "story_project_root": str(tmp_path / "book"),
            "chapter_index": 2,
            "snapshot_overlay": {"chapter_index": 2},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": {
                "chapter_index": 2,
                "outline_path": "book/大纲/细纲_第002章.md",
                "title": "Audit",
                "core_event": "The team chooses a dangerous route.",
                "required_beats": [
                    {"index": 1, "text": "danger forces the route choice"},
                    {"index": 2, "text": "open conflict over the serum"},
                ],
                "ending_pressure": "the locked door starts a countdown",
                "source_path": "book/大纲/细纲_第002章.md",
                "missing_fields": [],
            },
            "source_paths": {"outline_path": "book/大纲/细纲_第002章.md"},
            "source_resolution": {"entries": []},
        }
        oh_story_report = validate_schema(
            {
                "enabled": True,
                "detected": False,
                "confidence": "none",
                "root": str(tmp_path / "book"),
                "markers": [],
                "summary": {"present_count": 0, "optional_missing_count": 0, "unsupported_count": 2},
                "capabilities": {
                    "story_project_core_dirs": True,
                    "active_book": True,
                    "chapter_blueprint": True,
                    "story_project_writeback": True,
                    "review_repair_loop": True,
                    "oh_story_js_execution": False,
                    "oh_story_provider": False,
                },
                "warnings": [],
                "unsupported": ["oh_story_js_execution", "oh_story_api_provider"],
                "recommendations": [],
            },
            "oh_story_compatibility.schema.json",
        )

        AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            story_project_context=story_project_context,
            story_project_oh_story_report=oh_story_report,
        ).run_once(persist=True)

        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved_run = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertIs(saved_run, validate_schema(saved_run, "run_result.schema.json"))
        story_project = saved_run["run"]["story_project"]
        self.assertIsNotNone(story_project["book_id"])
        self.assertEqual(story_project["book_id"], story_project["project_identity"]["book_id"])
        self.assertFalse(story_project["project_identity"]["ephemeral"])
        committed_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(story_project["book_id"], committed_snapshot["book_id"])
        self.assertEqual(oh_story_report, story_project["oh_story"])
        self.assertFalse(story_project["writeback"]["attempted"])
        self.assertEqual([], story_project["blueprint_coverage"]["missing_beat_indexes"])
        self.assertTrue(story_project["blueprint_coverage"]["ending_pressure_covered"])
        self.assertIn("story_project", saved_run["run"]["validation"]["executed_checks"])
        self.assertFalse((tmp_path / "runs" / "story_project_writebacks").exists())

    def test_story_project_snapshot_identity_mismatch_fails_before_provider(self) -> None:
        tmp_path = self._case_dir("story_project_identity_mismatch")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot = json.loads(self._write_snapshot(snapshot_path))
        snapshot["book_id"] = "different-book"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        calls: list[str] = []
        context = {
            "story_project_root": str(book),
            "chapter_index": 2,
            "snapshot_overlay": {"chapter_index": 2},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": None,
            "source_paths": {},
            "source_resolution": {},
        }

        with self.assertRaisesRegex(ValueError, "story_project_state_identity_mismatch"):
            AgentExecutor(
                snapshot_path=snapshot_path,
                memory_path=tmp_path / "missing-memory.json",
                run_dir=tmp_path / "runs",
                chapter_dir=tmp_path / "chapters",
                dry_run=True,
                story_project_context=context,
                generator=lambda _input: calls.append("called") or "正文",
            ).run_once(persist=True)

        self.assertEqual([], calls)

    def test_stable_story_project_identity_rejects_unbound_explicit_snapshot(self) -> None:
        tmp_path = self._case_dir("story_project_identity_missing")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        identity = ensure_project_identity(book)
        context = {
            "story_project_root": str(book),
            "project_identity": identity.to_dict(),
            "chapter_index": 2,
            "snapshot_overlay": {"chapter_index": 2},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": None,
            "source_paths": {},
            "source_resolution": {},
        }

        with self.assertRaisesRegex(ValueError, "story_project_state_identity_mismatch"):
            AgentExecutor(
                snapshot_path=snapshot_path,
                memory_path=tmp_path / "missing-memory.json",
                run_dir=tmp_path / "runs",
                chapter_dir=tmp_path / "chapters",
                dry_run=True,
                story_project_context=context,
            ).run_once(persist=True)

    def test_story_project_real_writeback_blocked_is_recorded(self) -> None:
        tmp_path = self._case_dir("story_project_writeback_blocked")
        snapshot_path = tmp_path / "snapshot.json"
        before_snapshot = self._write_snapshot(snapshot_path)
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        canonical_prose_path(book, 2, "Audit").write_text("existing", encoding="utf-8")
        story_project_context = {
            "story_project_root": str(book),
            "chapter_index": 2,
            "snapshot_overlay": {"chapter_index": 2},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": {
                "chapter_index": 2,
                "outline_path": str(book / "大纲" / "细纲_第002章.md"),
                "title": "Audit",
                "core_event": "danger forces the route choice",
                "required_beats": [
                    {"index": 1, "text": "danger forces the route choice"},
                    {"index": 2, "text": "open conflict over the serum"},
                ],
                "ending_pressure": "the locked door starts a countdown",
                "source_path": str(book / "大纲" / "细纲_第002章.md"),
                "missing_fields": [],
            },
            "source_paths": {"outline_path": str(book / "大纲" / "细纲_第002章.md")},
            "source_resolution": {"entries": []},
        }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            story_project_context=story_project_context,
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
        ).run_once(persist=True)

        saved_run = json.loads(next((tmp_path / "runs").glob("chapter_2_*.json")).read_text(encoding="utf-8"))
        writeback = saved_run["run"]["story_project"]["writeback"]
        self.assertTrue(writeback["attempted"])
        self.assertFalse(writeback["applied"])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("failed", result["run"]["status"])
        self.assertEqual("preparation_failed", result["run"]["persistence"]["state"])
        self.assertIn("target_prose_exists", writeback["blocked_reasons"])
        self.assertTrue((tmp_path / "runs" / "story_project_writebacks").exists())
        self.assertEqual("existing", canonical_prose_path(book, 2, "Audit").read_text(encoding="utf-8"))
        self.assertEqual([], list((book / "追踪").iterdir()))
        self.assertEqual(before_snapshot, snapshot_path.read_text(encoding="utf-8"))

    def test_story_project_writeback_and_snapshot_commit_in_one_transaction(self) -> None:
        tmp_path = self._case_dir("story_project_transaction_success")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        outline = book / "大纲" / "细纲_第002章.md"
        outline.write_text("# Audit", encoding="utf-8")
        story_project_context = {
            "story_project_root": str(book),
            "chapter_index": 2,
            "snapshot_overlay": {"chapter_index": 2},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": {
                "chapter_index": 2,
                "outline_path": str(outline),
                "title": "Audit",
                "core_event": "danger forces the route choice",
                "required_beats": [
                    {"index": 1, "text": "danger forces the route choice"},
                    {"index": 2, "text": "open conflict over the serum"},
                ],
                "ending_pressure": "the locked door starts a countdown",
                "source_path": str(outline),
                "missing_fields": [],
            },
            "source_paths": {"outline_path": str(outline)},
            "source_resolution": {"entries": []},
        }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            story_project_context=story_project_context,
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
        ).run_once(persist=True)

        self.assertTrue(result["accepted"])
        self.assertTrue(result["committed"])
        self.assertEqual("completed", result["run"]["persistence"]["state"])
        self.assertTrue(result["run"]["story_project"]["writeback"]["applied"])
        self.assertTrue(canonical_prose_path(book, 2, "Audit").exists())
        self.assertEqual(4, len(list((book / "追踪").glob("*.md"))))
        self.assertEqual(3, json.loads(snapshot_path.read_text(encoding="utf-8"))["chapter_index"])
        journal = Path(result["run"]["persistence"]["journal_path"])
        self.assertTrue((journal / "commit.marker").exists())
        self.assertTrue((journal / "candidate_result.json").exists())

    def test_story_project_real_writeback_defaults_to_standard_quality_policy(self) -> None:
        tmp_path = self._case_dir("story_project_standard_quality")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        outline = book / "大纲" / "细纲_第002章.md"
        outline.write_text("# Audit", encoding="utf-8")
        context = {
            "story_project_root": str(book),
            "chapter_index": 2,
            "snapshot_overlay": {"chapter_index": 2},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": {
                "chapter_index": 2,
                "outline_path": str(outline),
                "title": "Audit",
                "core_event": "danger forces the route choice",
                "required_beats": [
                    {"index": 1, "text": "danger forces the route choice"},
                    {"index": 2, "text": "open conflict over the serum"},
                ],
                "ending_pressure": "the locked door starts a countdown",
                "source_path": str(outline),
                "missing_fields": [],
            },
            "source_paths": {"outline_path": str(outline)},
            "source_resolution": {"entries": []},
        }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            story_project_context=context,
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
        ).run_once(persist=True)

        self.assertFalse(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("standard", result["quality_decision"]["policy"]["name"])
        self.assertIn("chapter_length", {item["code_family"] for item in result["quality_decision"]["findings"]})
        self.assertEqual(result["accepted"], result["run"]["quality_decision"]["accepted"])
        self.assertFalse(canonical_prose_path(book, 2, "Audit").exists())

    def test_reconcile_republishes_chapter_and_idempotent_file_outbox_after_marker_crash(self) -> None:
        import main as cli

        tmp_path = self._case_dir("story_project_transaction_reconcile")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        outline = book / "大纲" / "细纲_第002章.md"
        outline.write_text("# Recover", encoding="utf-8")
        outbox = tmp_path / "memory-outbox.jsonl"
        chapter_dir = tmp_path / "chapters"
        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=chapter_dir,
            dry_run=True,
            analyzer=self._analysis,
            memory_writer=FileMemoryWriter(outbox),
            story_project_context={
                "story_project_root": str(book),
                "chapter_index": 2,
                "snapshot_overlay": {"chapter_index": 2},
                "memory_context_overlay": {"items": [], "source_mappings": []},
                "chapter_blueprint": {
                    "chapter_index": 2,
                    "outline_path": str(outline),
                    "title": "Recover",
                    "core_event": "danger forces the route choice",
                    "required_beats": [
                        {"index": 1, "text": "danger forces the route choice"},
                        {"index": 2, "text": "open conflict over the serum"},
                    ],
                    "ending_pressure": "the locked door starts a countdown",
                    "source_path": str(outline),
                    "missing_fields": [],
                },
                "source_paths": {"outline_path": str(outline)},
                "source_resolution": {"entries": []},
            },
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
        ).run_once(persist=True)

        run_id = result["run"]["id"]
        run_path = tmp_path / "runs" / f"{run_id}.json"
        artifact_path = Path(result["run"]["chapter"]["artifact"]["path"])
        journal = Path(result["run"]["persistence"]["journal_path"])
        manifest_path = journal / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"] = "commit_marked"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        run_path.unlink()
        artifact_path.unlink()
        outbox.unlink()

        report = cli._reconcile_and_publish_persistence(tmp_path / "runs", chapter_dir=chapter_dir)

        self.assertTrue(report["ok"])
        self.assertEqual([run_id], report["published_run_ids"])
        self.assertTrue(run_path.exists())
        self.assertTrue(artifact_path.exists())
        self.assertTrue(outbox.exists())
        recovered = json.loads(run_path.read_text(encoding="utf-8"))
        self.assertEqual("verified", recovered["run"]["memory"]["writeback"]["verification"]["status"])
        self.assertEqual("completed", json.loads(manifest_path.read_text(encoding="utf-8"))["state"])

    def test_direct_executor_blocks_unpublished_transaction_before_provider(self) -> None:
        tmp_path = self._case_dir("unpublished_transaction_block")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        target = tmp_path / "state.txt"
        transaction = LocalPersistenceTransaction(
            run_dir=run_dir,
            run_id="pending-run",
            allowed_roots=[tmp_path],
        )
        transaction.prepare(
            [PersistenceTarget("state", target, "committed")],
            candidate_result={"run": {"id": "pending-run"}},
        )
        self.assertEqual("commit_marked", transaction.commit().state)
        provider_calls: list[str] = []

        with self.assertRaisesRegex(PersistenceError, "persistence_reconciliation_required"):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=run_dir,
                dry_run=True,
                generator=lambda prompt: provider_calls.append(prompt) or "chapter",
            ).run_once(persist=True)

        self.assertEqual([], provider_calls)

    def test_snapshot_cas_preserves_external_edit_made_during_generation(self) -> None:
        tmp_path = self._case_dir("snapshot_cas_external_edit")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        external = json.dumps(
            {"chapter_index": 99, "world_state": {}, "characters": {}, "timeline": []},
            ensure_ascii=False,
            indent=2,
        )

        def analyzer(chapter: str, validation: dict) -> dict:
            snapshot_path.write_text(external, encoding="utf-8")
            return self._analysis(chapter, validation)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing-memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            analyzer=analyzer,
        ).run_once(persist=True)

        self.assertTrue(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("failed", result["run"]["status"])
        self.assertEqual("preparation_failed", result["run"]["persistence"]["state"])
        self.assertEqual(external, snapshot_path.read_text(encoding="utf-8"))

    def test_story_project_writeback_preview_changes_no_primary_state(self) -> None:
        tmp_path = self._case_dir("story_project_transaction_preview")
        snapshot_path = tmp_path / "snapshot.json"
        before_snapshot = self._write_snapshot(snapshot_path)
        book = tmp_path / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        outline = book / "大纲" / "细纲_第002章.md"
        outline.write_text("# Preview", encoding="utf-8")
        memory_calls: list[list[dict]] = []

        def memory_writer(updates):
            memory_calls.append(updates)
            raise AssertionError("preview must not deliver memory updates")

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing_memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            memory_writer=memory_writer,
            story_project_context={
                "story_project_root": str(book),
                "chapter_index": 2,
                "snapshot_overlay": {"chapter_index": 2},
                "memory_context_overlay": {"items": [], "source_mappings": []},
                "chapter_blueprint": {
                    "chapter_index": 2,
                    "outline_path": str(outline),
                    "title": "Preview",
                    "core_event": "danger forces the route choice",
                    "required_beats": [
                        {"index": 1, "text": "danger forces the route choice"},
                        {"index": 2, "text": "open conflict over the serum"},
                    ],
                    "ending_pressure": "the locked door starts a countdown",
                    "source_path": str(outline),
                    "missing_fields": [],
                },
                "source_paths": {"outline_path": str(outline)},
                "source_resolution": {"entries": []},
            },
            story_project_writeback=StoryProjectWritebackConfig(mode="dry_run"),
        ).run_once(persist=True)

        self.assertTrue(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("preview", result["run"]["status"])
        self.assertEqual("preview", result["run"]["persistence"]["state"])
        self.assertEqual(before_snapshot, snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual([], memory_calls)
        self.assertFalse(canonical_prose_path(book, 2, "Preview").exists())
        self.assertEqual([], list((book / "追踪").iterdir()))


    def test_pre_validate_bridge_records_real_precheck(self) -> None:
        tmp_path = self._case_dir("pre_validate_bridge")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "chapter_index": 2,
                    "world_state": {"locations": {}},
                    "characters": {},
                    "timeline": [],
                    "story_state": {
                        "last_chapter_ending": "Mira was trapped in the train car.",
                        "last_scene_location": "train car",
                        "last_scene_characters": ["Mira"],
                        "open_threads": [],
                        "required_opening_bridge": "Continue from train car",
                    },
                    "spatial_state": {
                        "spaces": {"train car": {}, "connector passage": {}},
                        "connections": [{"from": "train car", "to": "connector passage"}],
                        "character_positions": {"Mira": "train car"},
                        "blocked_paths": [],
                        "last_transition": {},
                    },
                }
            ),
            encoding="utf-8",
        )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=lambda snapshot, memory: {
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["build_snapshot", "pre_validate_bridge", "generate_chapter", "validate"],
                "validation_focus": ["spatial", "logic"],
                "max_repair_attempts": 0,
                "notes": [],
            },
            generator=lambda input_pack: (
                "Continue from train car through the connector passage, Mira faced danger and conflict over the serum choice."
            ),
        ).run_once(persist=False)

        precheck_event = [event for event in result["run"]["trace"] if event["action"] == "pre_validate_bridge"][0]
        self.assertTrue(precheck_event["bridge_precheck"]["ok"])
        self.assertEqual([], precheck_event["bridge_precheck"]["problem_codes"])

    def test_pre_validate_bridge_fails_before_generation_when_bridge_missing(self) -> None:
        tmp_path = self._case_dir("pre_validate_bridge_missing")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "chapter_index": 2,
                    "world_state": {"locations": {"train car": {}}},
                    "characters": {},
                    "timeline": [],
                    "story_state": {
                        "last_chapter_ending": "Mira was trapped in the train car.",
                        "last_scene_location": "train car",
                        "last_scene_characters": ["Mira"],
                        "open_threads": [],
                        "required_opening_bridge": "",
                    },
                    "spatial_state": {
                        "spaces": {"train car": {}},
                        "connections": [],
                        "character_positions": {"Mira": "train car"},
                        "blocked_paths": [],
                        "last_transition": {},
                    },
                }
            ),
            encoding="utf-8",
        )
        calls: list[str] = []

        with self.assertRaisesRegex(ValueError, "missing_opening_bridge"):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                dry_run=True,
                director=lambda snapshot, memory: {
                    "chapter_index": 2,
                    "goal": "continue_existing_arc",
                    "actions": ["build_snapshot", "pre_validate_bridge", "generate_chapter", "validate"],
                    "validation_focus": ["spatial"],
                    "max_repair_attempts": 0,
                    "notes": [],
                },
                generator=lambda input_pack: calls.append(input_pack) or "unreached",
            ).run_once(persist=False)

        self.assertEqual([], calls)

    def test_analyzer_failure_persists_failed_run_diagnostics(self) -> None:
        tmp_path = self._case_dir("analyzer_failure")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        def bad_analyzer(chapter: str, validation: dict) -> dict:
            return {"summary": "missing required analysis fields"}

        with self.assertRaises(SchemaValidationError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                chapter_dir=tmp_path / "chapters",
                dry_run=True,
                generator=lambda input_pack: (
                    "The shelter faced danger as the team had to choose between a costly rescue "
                    "and protecting the serum, creating open conflict."
                ),
                analyzer=bad_analyzer,
            ).run_once(persist=True)

        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual("failed", saved["run"]["status"])
        self.assertFalse(saved["run"]["committed"])
        self.assertEqual("SchemaValidationError", saved["run"]["error"]["type"])
        self.assertEqual(["execution_error"], saved["run"]["validation"]["problem_codes"])
        validate_trace = [event for event in saved["run"]["trace"] if event["action"] == "validate"][0]
        self.assertTrue(validate_trace["validation_ok"])
        self.assertEqual("repair_if_needed", saved["run"]["trace"][-1]["action"])
        self.assertEqual("validation_ok", saved["run"]["trace"][-1]["skip_reason"])
        chapter_artifact_path = Path(saved["run"]["chapter"]["artifact"]["path"])
        self.assertTrue(chapter_artifact_path.exists())
        chapter_artifact = chapter_artifact_path.read_text(encoding="utf-8")
        self.assertIn("Status: `failed`", chapter_artifact)
        self.assertIn("The shelter faced danger", chapter_artifact)
        self.assertIs(saved, validate_schema(saved, "run_result.schema.json"))

    def test_committed_run_persists_character_changes_to_snapshot(self) -> None:
        tmp_path = self._case_dir("persist_character_changes")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            generator=lambda input_pack: (
                "Mira was injured at the shelter as danger closed in. "
                "Jon returned to the shelter with the serum, forcing the team to choose rescue "
                "despite the conflict and visible cost."
            ),
        ).run_once(persist=True)

        saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertTrue(result["committed"])
        self.assertEqual("injured", saved["characters"]["Mira"]["status"])
        self.assertEqual("shelter", saved["characters"]["Jon"]["current_location"])
        self.assertEqual(2, saved["characters"]["Mira"]["last_seen_chapter"])

    def test_repair_loop_can_fix_short_chapter(self) -> None:
        tmp_path = self._case_dir("repair_loop")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            generator=lambda input_pack: "天气很好。",
        ).run_once(persist=False)

        self.assertEqual(1, result["repair_attempts"])
        self.assertTrue(result["validation"]["ok"])
        self.assertIn("conflict", result["chapter"].lower())
        self.assertEqual(["generate_chapter", "polish", "validate", "repair_if_needed"], [event["action"] for event in result["run"]["trace"]])
        self.assertGreaterEqual(result["run"]["trace"][-1]["repair_attempts"], 1)
        self.assertIs(
            result["run"]["trace"][-1]["repair_plan"],
            validate_schema(result["run"]["trace"][-1]["repair_plan"], "repair_plan.schema.json"),
        )
        self.assertIn("expand_scene", result["run"]["trace"][-1]["repair_plan"]["actions"])
        self.assertIn("add_conflict_signal", result["run"]["trace"][-1]["repair_plan"]["actions"])
        self.assertEqual(1, result["run"]["trace"][-1]["repair_plan"]["attempt"])
        self.assertEqual(1, result["run"]["trace"][-1]["repair_plan"]["repair_budget"])
        self.assertEqual("high", result["run"]["trace"][-1]["repair_plan"]["risk_level"])
        repair_deltas = result["run"]["trace"][-1]["repair_deltas"]
        self.assertEqual(1, len(repair_deltas))
        self.assertEqual(1, repair_deltas[0]["attempt"])
        self.assertFalse(repair_deltas[0]["before_ok"])
        self.assertTrue(repair_deltas[0]["after_ok"])
        self.assertIn("chapter_too_short", repair_deltas[0]["resolved_problem_codes"])
        self.assertIn("missing_conflict_marker", repair_deltas[0]["resolved_problem_codes"])
        self.assertEqual([], repair_deltas[0]["new_problem_codes"])
        self.assertFalse(result["run"]["trace"][-1]["skipped"])

    def test_executor_passes_trace_repair_plan_to_repairer(self) -> None:
        tmp_path = self._case_dir("repair_plan_passthrough")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        captured_plans: list[dict] = []

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "repair_with_budget",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 2,
                "notes": [],
            }

        def repairer(chapter: str, validation: dict, input_pack: str, repair_plan: dict) -> str:
            captured_plans.append(repair_plan)
            return (
                "At the shelter, danger forced the team to choose between protecting the serum "
                "and rescuing a teammate, creating open conflict with a visible cost."
            )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
            generator=lambda input_pack: "Too short.",
            repairer=repairer,
        ).run_once(persist=False)

        repair_trace = result["run"]["trace"][-1]
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(1, len(captured_plans))
        self.assertEqual(repair_trace["repair_plan"], captured_plans[0])
        self.assertEqual(2, captured_plans[0]["repair_budget"])
        self.assertEqual(1, captured_plans[0]["attempt"])
        self.assertFalse(captured_plans[0]["recovery"]["available"])

    def test_executor_passes_recovery_context_to_repairer_when_supported(self) -> None:
        tmp_path = self._case_dir("repair_recovery_context_passthrough")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        run_dir = tmp_path / "runs"
        run_dir.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        previous_run = build_run_record(
            started_at=started_at,
            finished_at=started_at,
            base_snapshot={"chapter_index": 1},
            runtime_snapshot={"chapter_index": 1},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 1,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate", "repair_if_needed"],
            input_pack="input",
            chapter="Too short.",
            validation={
                "ok": False,
                "requested_focus": ["logic"],
                "executed_checks": ["logic"],
                "skipped_checks": ["continuity", "spatial"],
                "problems": [
                    {
                        "code": "missing_conflict_marker",
                        "message": "Missing conflict marker.",
                        "validator": "logic",
                        "severity": "high",
                        "blocking": True,
                    }
                ],
            },
            analysis={
                "validation_ok": False,
                "conflicts": [],
                "events": [],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "",
            },
            repair_attempts=1,
            committed=False,
        )
        (run_dir / f"{previous_run['id']}.json").write_text(
            json.dumps({"run": previous_run}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        captured_plans: list[dict] = []
        captured_recovery: list[dict] = []

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "repair_with_recovery",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            }

        def repairer(
            chapter: str,
            validation: dict,
            input_pack: str,
            repair_plan: dict,
            recovery_context: dict,
        ) -> str:
            captured_plans.append(repair_plan)
            captured_recovery.append(recovery_context)
            return (
                "At the shelter, danger forced the team to choose between protecting the serum "
                "and rescuing a teammate, creating open conflict with a visible cost."
            )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=run_dir,
            dry_run=True,
            director=director,
            generator=lambda input_pack: "Too short.",
            repairer=repairer,
        ).run_once(persist=False)

        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(1, len(captured_recovery))
        self.assertTrue(captured_recovery[0]["available"])
        self.assertEqual(previous_run["id"], captured_recovery[0]["source_run_id"])
        self.assertEqual(["logic"], captured_recovery[0]["executed_checks"])
        self.assertEqual(["continuity", "spatial"], captured_recovery[0]["skipped_checks"])
        self.assertTrue(captured_plans[0]["recovery"]["available"])
        self.assertEqual(previous_run["id"], captured_plans[0]["recovery"]["source_run_id"])
        self.assertEqual(["missing_conflict_marker"], captured_plans[0]["recovery"]["repeated_problem_codes"])
        self.assertIn("previous_validation_skipped", captured_plans[0]["recovery"]["failure_modes"])

    def test_repair_if_needed_trace_marks_validation_ok_skip(self) -> None:
        tmp_path = self._case_dir("repair_skipped_validation_ok")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            generator=lambda input_pack: (
                "The shelter faced danger as the team had to choose between a costly rescue "
                "and protecting the serum, creating open conflict."
            ),
        ).run_once(persist=False)

        repair_trace = result["run"]["trace"][-1]
        self.assertEqual("repair_if_needed", repair_trace["action"])
        self.assertTrue(repair_trace["skipped"])
        self.assertEqual("validation_ok", repair_trace["skip_reason"])
        self.assertNotIn("repair_plan", repair_trace)

    def test_repair_if_needed_trace_marks_exhausted_budget_skip(self) -> None:
        tmp_path = self._case_dir("repair_skipped_budget")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "validate_without_repair_budget",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
            generator=lambda input_pack: "Too short.",
        ).run_once(persist=False)

        repair_trace = result["run"]["trace"][-1]
        self.assertEqual("repair_if_needed", repair_trace["action"])
        self.assertTrue(repair_trace["skipped"])
        self.assertEqual("max_repair_attempts_exhausted", repair_trace["skip_reason"])
        self.assertFalse(result["committed"])

    def test_director_actions_skip_polish_handler(self) -> None:
        tmp_path = self._case_dir("skip_polish")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "skip_polish",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        def fail_if_polished(chapter: str) -> str:
            raise AssertionError("polisher should not run")

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
            generator=lambda input_pack: (
                "The shelter faced danger as the team had to choose between a costly rescue "
                "and protecting the serum, creating open conflict."
            ),
            polisher=fail_if_polished,
        ).run_once(persist=False)

        self.assertEqual(["generate_chapter", "validate"], result["workflow"])
        self.assertTrue(result["validation"]["ok"])

    def test_claude_polish_api_error_continues_with_generated_chapter(self) -> None:
        tmp_path = self._case_dir("provider_polish")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        generated = (
            "The shelter faced danger as the team had to choose between a costly rescue "
            "and protecting the serum, creating open conflict. The choice changed the "
            "route forward, but the chapter remained complete enough for validation."
        )

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "continue_after_provider_polish_error",
                "actions": ["generate_chapter", "polish", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        def fail_polish(chapter: str) -> str:
            raise ModelCallError(
                "Claude polish failed: invalid provider response",
                provider="anthropic",
                stage="claude_polish",
                model="claude-test",
                failure_category="provider_error",
                retryable=False,
                attempts=1,
                elapsed_ms=50,
            )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            director=director,
            generator=lambda input_pack: generated,
            polisher=fail_polish,
        ).run_once(persist=True)

        self.assertTrue(result["committed"])
        self.assertEqual(generated, result["chapter"])
        trace = result["run"]["trace"]
        self.assertEqual(["generate_chapter", "polish", "validate"], [event["action"] for event in trace])
        self.assertEqual("failed", trace[1]["status"])
        self.assertEqual("continue_unpolished", trace[1]["plan_failure_policy"])
        self.assertEqual("provider_error", trace[1]["model_call"]["failure_category"])
        self.assertFalse(trace[1]["model_call"]["retryable"])
        self.assertEqual("completed", trace[2]["status"])
        self.assertTrue(result["validation"]["ok"])

    def test_claude_polish_output_error_continues_with_generated_chapter(self) -> None:
        tmp_path = self._case_dir("polish_output_error")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        generated = (
            "The shelter faced danger as the team had to choose between a costly rescue "
            "and protecting the serum, creating open conflict. The generated chapter "
            "remained complete even though the polish response was unusable."
        )

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "continue_after_bad_polish_output",
                "actions": ["generate_chapter", "polish", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        def fail_polish(chapter: str) -> str:
            raise ModelOutputError("polished_chapter output is empty")

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            director=director,
            generator=lambda input_pack: generated,
            polisher=fail_polish,
        ).run_once(persist=True)

        self.assertTrue(result["committed"])
        self.assertEqual(generated, result["chapter"])
        trace = result["run"]["trace"]
        self.assertEqual(["generate_chapter", "polish", "validate"], [event["action"] for event in trace])
        self.assertEqual("failed", trace[1]["status"])
        self.assertEqual("continue_unpolished", trace[1]["plan_failure_policy"])
        self.assertEqual("ModelOutputError", trace[1]["error_type"])
        self.assertEqual("completed", trace[2]["status"])
        self.assertTrue(result["validation"]["ok"])

    def test_executor_passes_snapshot_builder_audit_to_director(self) -> None:
        tmp_path = self._case_dir("director_snapshot_audit")
        snapshot_path = tmp_path / "snapshot.json"
        memory_path = tmp_path / "memory.json"
        self._write_snapshot(snapshot_path)
        memory_path.write_text(
            json.dumps(
                {
                    "source": "test",
                    "status": "ready",
                    "items": [{"type": "character", "data": {"role": "unnamed"}}],
                }
            ),
            encoding="utf-8",
        )
        captured_memory: list[dict[str, object]] = []

        def director(snapshot, memory_context):
            captured_memory.append(memory_context)
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "use_current_snapshot_audit",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["continuity", "spatial", "logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=memory_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
        ).run_once(persist=False)

        audit = captured_memory[0]["snapshot_builder_audit"]
        self.assertEqual(1, audit["skipped_count"])
        self.assertEqual([{"reason_code": "missing_name", "count": 1}], audit["skipped_reason_counts"])
        self.assertEqual([{"severity": "medium", "count": 1}], audit["skipped_severity_counts"])

    def test_invalid_director_decision_persists_failed_run_diagnostics(self) -> None:
        tmp_path = self._case_dir("director_failure")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        def bad_director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "bad_director",
                "actions": ["generate_chapter"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        with self.assertRaises(DirectorDecisionError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                director=bad_director,
            ).run_once(persist=True)

        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual("failed", saved["run"]["status"])
        self.assertFalse(saved["run"]["committed"])
        self.assertEqual("injected", saved["run"]["director"]["mode"])
        self.assertEqual("failed", saved["run"]["director"]["status"])
        self.assertEqual("DirectorDecisionError", saved["run"]["director"]["error_type"])
        self.assertEqual("DirectorDecisionError", saved["run"]["error"]["type"])
        self.assertEqual([], saved["run"]["workflow"])
        self.assertIsNone(saved["run"]["workflow_plan"])
        self.assertEqual([], saved["run"]["trace"])
        self.assertEqual(["director_error"], saved["run"]["validation"]["problem_codes"])

    def test_invalid_workflow_persists_failed_run_diagnostics(self) -> None:
        tmp_path = self._case_dir("workflow_failure")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        def bad_workflow_director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "bad_workflow",
                "actions": ["generate_chapter", "repair_if_needed", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        with self.assertRaises(WorkflowError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                director=bad_workflow_director,
            ).run_once(persist=True)

        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual("failed", saved["run"]["status"])
        self.assertFalse(saved["run"]["committed"])
        self.assertEqual("injected", saved["run"]["director"]["mode"])
        self.assertEqual("completed", saved["run"]["director"]["status"])
        self.assertEqual("WorkflowError", saved["run"]["error"]["type"])
        self.assertEqual([], saved["run"]["workflow"])
        self.assertIsNone(saved["run"]["workflow_plan"])
        self.assertEqual([], saved["run"]["trace"])
        self.assertEqual(["workflow_error"], saved["run"]["validation"]["problem_codes"])
        self.assertEqual(["generate_chapter", "repair_if_needed", "validate"], saved["run"]["decision"]["actions"])

    def test_invalid_final_chapter_does_not_update_snapshot(self) -> None:
        tmp_path = self._case_dir("invalid_no_commit")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "test_invalid_no_commit",
                "actions": ["generate_chapter", "polish", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            director=director,
            generator=lambda input_pack: (
                "The corridor stayed quiet while everyone reviewed supplies and repeated old procedures. "
                "Nothing changed, no pressure rose, and the scene avoided any decisive story movement."
            ),
        ).run_once(persist=True)

        self.assertFalse(result["validation"]["ok"])
        self.assertFalse(result["committed"])
        self.assertEqual("rejected", result["run"]["status"])
        self.assertIn("missing_conflict_marker", result["run"]["validation"]["problem_codes"])
        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved_run = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual("rejected", saved_run["run"]["status"])
        self.assertFalse(saved_run["run"]["committed"])
        self.assertFalse(saved_run["run"]["state_update"]["applied"])
        self.assertEqual(0, saved_run["run"]["state_update"]["memory_update_count"])
        self.assertIn("missing_conflict_marker", saved_run["run"]["validation"]["problem_codes"])
        input_pack_artifact_path = Path(saved_run["run"]["input_pack"]["artifact"]["path"])
        self.assertTrue(input_pack_artifact_path.exists())
        artifact_path = Path(saved_run["run"]["chapter"]["artifact"]["path"])
        self.assertTrue(artifact_path.exists())
        self.assertIn("Status: `rejected`", artifact_path.read_text(encoding="utf-8"))

    def test_memory_constraint_rejection_does_not_update_snapshot(self) -> None:
        tmp_path = self._case_dir("memory_constraint_reject")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        def memory_loader():
            return {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "type": "constraint",
                        "data": {
                            "rule": "Do not resolve the serum conflict.",
                            "forbidden_terms": ["serum conflict resolved"],
                            "required_terms": ["serum"],
                        },
                    }
                ],
            }

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "test_memory_constraint_rejection",
                "actions": ["generate_chapter", "polish", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            memory_loader=memory_loader,
            director=director,
            generator=lambda input_pack: (
                "The serum conflict resolved in a sudden choice, ending the danger before the team had to pay a cost."
            ),
        ).run_once(persist=True)

        self.assertFalse(result["committed"])
        self.assertIn("forbidden_constraint_term", result["run"]["validation"]["problem_codes"])
        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))

    def test_repair_loop_can_remove_forbidden_constraint_term(self) -> None:
        tmp_path = self._case_dir("repair_forbidden_term")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        def memory_loader():
            return {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "type": "constraint",
                        "data": {
                            "rule": "Do not resolve the serum conflict.",
                            "forbidden_terms": ["serum conflict resolved"],
                            "required_terms": ["serum"],
                        },
                    }
                ],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            memory_loader=memory_loader,
            generator=lambda input_pack: (
                "The serum conflict resolved in a sudden choice, ending the danger before the team had to pay a cost."
            ),
        ).run_once(persist=False)

        self.assertEqual(1, result["repair_attempts"])
        self.assertTrue(result["validation"]["ok"])
        self.assertNotIn("serum conflict resolved", result["chapter"].lower())

    def test_repair_loop_can_add_missing_required_constraint_term(self) -> None:
        tmp_path = self._case_dir("repair_required_term")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        def memory_loader():
            return {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "type": "constraint",
                        "data": {
                            "rule": "Keep serum in focus.",
                            "required_terms": ["serum"],
                        },
                    }
                ],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            memory_loader=memory_loader,
            generator=lambda input_pack: (
                "The team entered danger and had to choose between retreat and rescue, creating conflict "
                "that carried a visible cost for everyone involved."
            ),
        ).run_once(persist=False)

        self.assertEqual(1, result["repair_attempts"])
        self.assertTrue(result["validation"]["ok"])
        self.assertIn("serum", result["chapter"].lower())

    def test_repair_loop_can_add_missing_known_location(self) -> None:
        tmp_path = self._case_dir("repair_location")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        def memory_loader():
            return {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "type": "location",
                        "name": "shelter",
                        "data": {"aliases": ["sealed gate"]},
                    }
                ],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            memory_loader=memory_loader,
            generator=lambda input_pack: (
                "The team entered danger and had to choose between retreat and rescue, creating conflict "
                "that carried a visible cost for everyone involved."
            ),
        ).run_once(persist=False)

        self.assertEqual(1, result["repair_attempts"])
        self.assertTrue(result["validation"]["ok"])
        self.assertIn("shelter", result["chapter"].lower())

    def test_repair_loop_can_rewrite_inactive_character_action(self) -> None:
        tmp_path = self._case_dir("repair_inactive_character")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        def memory_loader():
            return {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "type": "character",
                        "name": "Mira",
                        "data": {"status": "dead"},
                    }
                ],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            memory_loader=memory_loader,
            generator=lambda input_pack: (
                "Mira said the danger was close, then walked into the conflict as the team faced "
                "a costly choice over the serum."
            ),
        ).run_once(persist=False)

        self.assertEqual(1, result["repair_attempts"])
        self.assertTrue(result["validation"]["ok"])
        self.assertIn("mira remains unavailable", result["chapter"].lower())
        self.assertNotIn("inactive_character_action", result["run"]["validation"]["problem_codes"])

    def test_executor_uses_latest_rejected_run_history_for_director(self) -> None:
        tmp_path = self._case_dir("history_context")
        snapshot_path = tmp_path / "snapshot.json"
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        self._write_snapshot(snapshot_path)
        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history_run = build_run_record(
            started_at=started_at,
            finished_at=started_at,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate", "repair_if_needed"],
            input_pack="input",
            chapter="The team failed to mention the required constraint term.",
            validation={
                "ok": False,
                "problems": [
                    {
                        "code": "missing_required_constraint_term",
                        "message": "Missing required term.",
                        "severity": "high",
                        "blocking": True,
                        "category": "constraint",
                        "repair_hint": "Mention the required term.",
                    }
                ],
            },
            analysis={
                "validation_ok": False,
                "conflicts": [],
                "events": [],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "",
            },
            repair_attempts=1,
            committed=False,
        )
        (run_dir / f"{history_run['id']}.json").write_text(
            json.dumps({"run": history_run}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=run_dir,
            dry_run=True,
        ).run_once(persist=False)

        self.assertEqual("recover_from_rejected_run", result["decision"]["goal"])
        self.assertEqual(2, result["decision"]["max_repair_attempts"])
        self.assertEqual(["generate_chapter", "validate", "repair_if_needed"], result["workflow"])

    def test_non_story_project_dry_run_loop_advances_only_loop_local_snapshot(self) -> None:
        tmp_path = self._case_dir("loop_local_preview")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)
        seen_memory: list[dict] = []
        generated = [
            "First preview faced danger and conflict over a costly serum choice.",
            "Second preview faced danger and conflict over another costly serum choice.",
        ]

        def director(snapshot, memory_context):
            seen_memory.append(dict(memory_context))
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "loop_local_preview",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
            generator=lambda _input_pack: generated.pop(0),
            validator=self._ok_validation,
            analyzer=self._analysis,
        ).run_loop(steps=2, persist=False)

        self.assertEqual([2, 3], [item["run"]["chapter_index"] for item in loop_result["runs"]])
        self.assertEqual(["preview", "preview"], [item["run"]["status"] for item in loop_result["runs"]])
        self.assertEqual([False, False], [item["committed"] for item in loop_result["runs"]])
        self.assertTrue(all(item["accepted"] for item in loop_result["runs"]))
        self.assertEqual(4, loop_result["last_result"]["snapshot"]["chapter_index"])
        self.assertEqual(loop_result["runs"][0]["chapter"], seen_memory[1]["last_run"]["chapter_text"])
        self.assertTrue(loop_result["succeeded"])
        self.assertEqual(0, loop_result["exit_code"])
        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        self.assertFalse((tmp_path / "runs").exists())

    def test_story_project_dynamic_loader_commits_two_chapters_and_reloads_primary_state(self) -> None:
        tmp_path = self._case_dir("story_project_dynamic_two_steps")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        self._set_snapshot_language(snapshot_path)
        book = self._story_book(tmp_path)
        self._story_outline(book, 2, "Second")
        self._story_outline(book, 3, "Third")
        contexts: list[dict] = []
        delegate = build_generation_story_project_context_loader(story_project=book, chapter=2)

        class RecordingLoader:
            story_project_root = delegate.story_project_root

            def __call__(self, snapshot, memory_context, chapter_hint=None):
                context = delegate(snapshot, memory_context, chapter_hint)
                contexts.append(context.to_dict())
                return context

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing-memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            story_project_context_loader=RecordingLoader(),
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
        ).run_loop(steps=2, persist=True)

        self.assertEqual([2, 3], [item["run"]["chapter_index"] for item in loop_result["runs"]])
        self.assertEqual([True, True], [item["committed"] for item in loop_result["runs"]])
        self.assertTrue(loop_result["succeeded"])
        self.assertEqual(
            loop_result["runs"][1]["run"]["story_project"]["book_id"],
            loop_result["session"]["book_id"],
        )
        for item in loop_result["runs"]:
            manifest_path = tmp_path / "runs" / "transactions" / item["run"]["id"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(loop_result["session"]["book_id"], manifest["book_id"])
        self.assertTrue(canonical_prose_path(book, 2, "Second").exists())
        self.assertTrue(canonical_prose_path(book, 3, "Third").exists())
        self.assertEqual(4, json.loads(snapshot_path.read_text(encoding="utf-8"))["chapter_index"])
        self.assertEqual(2, len(contexts))
        self.assertEqual(
            canonical_prose_path(book, 2, "Second").read_text(encoding="utf-8").strip(),
            contexts[1]["previous_prose"]["text"].strip(),
        )
        self.assertEqual(4, len(contexts[1]["tracking_files"]))
        first_run_id = loop_result["runs"][0]["run"]["id"]
        self.assertTrue(any(first_run_id in item["text"] for item in contexts[1]["tracking_files"].values()))
        self.assertEqual("3", loop_result["runs"][1]["run"]["story_project"]["chapter_resolution"]["requested"])

    def test_story_project_rejection_retry_keeps_same_chapter(self) -> None:
        from core.validator import validate_chapter as real_validate_chapter

        tmp_path = self._case_dir("story_project_rejection_retry")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        self._set_snapshot_language(snapshot_path)
        book = self._story_book(tmp_path)
        self._story_outline(book, 2, "Second")
        hints: list[int | None] = []
        delegate = build_generation_story_project_context_loader(story_project=book, chapter=2)

        class RecordingLoader:
            story_project_root = delegate.story_project_root

            def __call__(self, snapshot, memory_context, chapter_hint=None):
                hints.append(chapter_hint)
                return delegate(snapshot, memory_context, chapter_hint)

        validation_calls = 0

        def validator(snapshot, chapter, decision):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 1:
                return real_validate_chapter(snapshot, "Nothing changed.", decision)
            return self._ok_validation(snapshot, chapter, decision)

        def director(snapshot, _memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "retry_same_story_chapter",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=tmp_path / "missing-memory.json",
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            director=director,
            validator=validator,
            story_project_context_loader=RecordingLoader(),
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
        ).run_loop(steps=2, persist=True, stop_on_rejection=False)

        self.assertEqual([2, 2], [item["run"]["chapter_index"] for item in loop_result["runs"]])
        self.assertEqual([False, True], [item["committed"] for item in loop_result["runs"]])
        self.assertEqual([None, 2], hints)
        self.assertEqual(3, json.loads(snapshot_path.read_text(encoding="utf-8"))["chapter_index"])
        self.assertEqual(1, len(list((book / CORE_DIRECTORY_NAMES[2]).glob("*.md"))))
        self.assertFalse(loop_result["succeeded"])
        self.assertEqual(["run_rejected"], loop_result["failure_reasons"])

    def test_story_project_missing_next_outline_stops_before_second_provider_call(self) -> None:
        from modules.chapter_generator import run_chapter_pipeline as real_run_chapter_pipeline

        tmp_path = self._case_dir("story_project_missing_next_outline")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        self._set_snapshot_language(snapshot_path)
        book = self._story_book(tmp_path)
        self._story_outline(book, 2, "Second")
        loader = build_generation_story_project_context_loader(story_project=book, chapter=2)

        with patch("core.engine.executor.run_chapter_pipeline", wraps=real_run_chapter_pipeline) as provider:
            with self.assertRaises(LoopExecutionError) as raised:
                AgentExecutor(
                    snapshot_path=snapshot_path,
                    memory_path=tmp_path / "missing-memory.json",
                    run_dir=tmp_path / "runs",
                    chapter_dir=tmp_path / "chapters",
                    dry_run=True,
                    story_project_context_loader=loader,
                    story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
                    quality_policy="minimal",
                ).run_loop(steps=2, persist=True)

        self.assertEqual(1, provider.call_count)
        self.assertIn("outline", str(raised.exception.original).lower())
        self.assertEqual(3, json.loads(snapshot_path.read_text(encoding="utf-8"))["chapter_index"])
        self.assertTrue(canonical_prose_path(book, 2, "Second").exists())

    def test_story_project_auto_sequence_drift_stops_before_second_provider_call(self) -> None:
        from modules.chapter_generator import run_chapter_pipeline as real_run_chapter_pipeline

        tmp_path = self._case_dir("story_project_auto_drift")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        self._set_snapshot_language(snapshot_path)
        book = self._story_book(tmp_path)
        self._story_outline(book, 2, "Second")
        self._story_outline(book, 4, "Fourth")
        canonical_prose_path(book, 1, "First").write_text("chapter one", encoding="utf-8")
        canonical_prose_path(book, 3, "Third").write_text("chapter three", encoding="utf-8")
        loader = build_generation_story_project_context_loader(story_project=book, chapter="auto")

        with patch("core.engine.executor.run_chapter_pipeline", wraps=real_run_chapter_pipeline) as provider:
            with self.assertRaises(LoopExecutionError) as raised:
                AgentExecutor(
                    snapshot_path=snapshot_path,
                    memory_path=tmp_path / "missing-memory.json",
                    run_dir=tmp_path / "runs",
                    chapter_dir=tmp_path / "chapters",
                    dry_run=True,
                    story_project_context_loader=loader,
                    story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
                    quality_policy="minimal",
                ).run_loop(steps=2, persist=True)

        self.assertEqual(1, provider.call_count)
        self.assertEqual("story_project_sequence_drift", raised.exception.original.code)
        self.assertIn("story_project_sequence_drift", raised.exception.session["failure_reasons"])
        self.assertFalse(canonical_prose_path(book, 4, "Fourth").exists())

    def test_story_project_loader_configuration_and_director_chapter_mismatch_fail_closed(self) -> None:
        tmp_path = self._case_dir("story_project_loader_guards")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        self._set_snapshot_language(snapshot_path)
        book = self._story_book(tmp_path)
        self._story_outline(book, 2, "Second")
        loader = build_generation_story_project_context_loader(story_project=book, chapter=2)

        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            AgentExecutor(story_project_context={"chapter_index": 2}, story_project_context_loader=loader)
        with self.assertRaisesRegex(ValueError, "context_loader"):
            AgentExecutor(
                snapshot_path=snapshot_path,
                story_project_context={"chapter_index": 2},
                story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            ).run_loop(steps=2, persist=True)
        provider_calls: list[str] = []
        with self.assertRaisesRegex(ValueError, "story_project_chapter_mismatch"):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs-mismatch",
                dry_run=True,
                director=lambda _snapshot, _memory: {
                    "chapter_index": 3,
                    "goal": "wrong_chapter",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 0,
                    "notes": [],
                },
                generator=lambda prompt: provider_calls.append(prompt) or "unreached",
                story_project_context_loader=loader,
            ).run_once(persist=False)
        self.assertEqual([], provider_calls)

    def test_run_loop_commits_multiple_steps(self) -> None:
        tmp_path = self._case_dir("loop_commits")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
        ).run_loop(steps=2, persist=True)

        saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(2, loop_result["completed_steps"])
        self.assertEqual(2, loop_result["session"]["completed_steps"])
        self.assertEqual("max_steps", loop_result["stopped_reason"])
        self.assertEqual("max_steps", loop_result["session"]["stopped_reason"])
        self.assertEqual(2, loop_result["session"]["committed_count"])
        self.assertTrue(loop_result["succeeded"])
        self.assertEqual(0, loop_result["exit_code"])
        self.assertEqual([], loop_result["failure_reasons"])
        self.assertEqual(0, loop_result["session"]["rejected_count"])
        self.assertEqual(2, len(loop_result["session"]["runs"]))
        self.assertEqual(
            ["continuity", "spatial", "logic"],
            loop_result["session"]["runs"][0]["executed_checks"],
        )
        self.assertEqual([], loop_result["session"]["runs"][0]["skipped_checks"])
        self.assertEqual(
            ["generate_chapter", "polish", "validate", "repair_if_needed"],
            loop_result["session"]["runs"][0]["workflow_actions"],
        )
        self.assertEqual(
            ["generate_chapter", "polish", "validate", "repair_if_needed"],
            loop_result["session"]["runs"][0]["trace_actions"],
        )
        self.assertTrue(loop_result["session"]["runs"][0]["trace_plan_aligned"])
        self.assertIs(loop_result["session"], validate_schema(loop_result["session"], "loop_session.schema.json"))
        self.assertEqual([True, True], [run["committed"] for run in loop_result["runs"]])
        execution_ids = {
            run["run"]["execution_evidence"]["execution_id"]
            for run in loop_result["runs"]
        }
        self.assertEqual(1, len(execution_ids))
        self.assertEqual(
            1,
            len(list((tmp_path / "runs" / "executions").glob("*/provenance.json"))),
        )
        self.assertEqual(4, saved["chapter_index"])
        self.assertEqual(2, len(list((tmp_path / "runs").glob("chapter_*.json"))))
        session_files = list((tmp_path / "runs" / "loop_sessions").glob("loop_*.json"))
        self.assertEqual(1, len(session_files))
        saved_session = json.loads(session_files[0].read_text(encoding="utf-8"))
        self.assertIs(saved_session, validate_schema(saved_session, "loop_session.schema.json"))
        self.assertEqual(loop_result["session"]["id"], saved_session["id"])
        self.assertEqual(str(session_files[0]), saved_session["artifact"]["path"])
        self.assertEqual(str(session_files[0]), loop_result["session"]["artifact"]["path"])
        self.assertEqual(
            ["continuity", "spatial", "logic"],
            saved_session["runs"][0]["requested_focus"],
        )
        self.assertTrue(saved_session["runs"][0]["trace_plan_aligned"])
        self.assertEqual(2, len(saved_session["step_timings"]))
        self.assertEqual([1, 2], [item["step"] for item in saved_session["step_timings"]])
        self.assertEqual(["committed", "committed"], [item["status"] for item in saved_session["step_timings"]])
        self.assertTrue(all("duration_ms" in item for item in saved_session["step_timings"]))

    def test_run_loop_notifies_observer_for_progress(self) -> None:
        tmp_path = self._case_dir("loop_observer")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        events: list[dict] = []

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
        ).run_loop(steps=2, persist=True, observer=events.append)

        self.assertEqual("loop_start", events[0]["event"])
        self.assertEqual(["step_start", "step_end", "step_start", "step_end"], [event["event"] for event in events[1:5]])
        self.assertEqual("loop_end", events[-1]["event"])
        self.assertEqual(loop_result["runs"][0]["run"]["id"], events[2]["run_id"])
        self.assertIn("duration_ms", events[2])

    def test_run_loop_stops_on_rejection_by_default(self) -> None:
        tmp_path = self._case_dir("loop_stop_rejection")
        snapshot_path = tmp_path / "snapshot.json"
        before = self._write_snapshot(snapshot_path)

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "reject_once",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
            generator=lambda input_pack: (
                "The corridor stayed quiet while everyone reviewed supplies and repeated old procedures."
            ),
        ).run_loop(steps=3, persist=True)

        self.assertEqual(1, loop_result["completed_steps"])
        self.assertEqual(1, loop_result["session"]["completed_steps"])
        self.assertEqual("rejected", loop_result["stopped_reason"])
        self.assertEqual("rejected", loop_result["session"]["stopped_reason"])
        self.assertEqual(0, loop_result["session"]["committed_count"])
        self.assertEqual(1, loop_result["session"]["rejected_count"])
        self.assertFalse(loop_result["last_result"]["committed"])
        self.assertFalse(loop_result["succeeded"])
        self.assertEqual(1, loop_result["exit_code"])
        self.assertIn("run_rejected", loop_result["failure_reasons"])
        self.assertEqual("missing_conflict_marker", loop_result["session"]["runs"][0]["problem_evidence"][0]["code"])
        self.assertEqual(
            [{"kind": "missing_any_marker", "value": "conflict, danger, choice, choose, threat, secret"}],
            loop_result["session"]["runs"][0]["problem_evidence"][0]["evidence"],
        )
        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))

    def test_run_loop_can_continue_after_rejection_with_history(self) -> None:
        tmp_path = self._case_dir("loop_continue_rejection")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        outputs = [
            "Mira said the danger was close, then walked into the conflict as the team faced a costly choice.",
            (
                "The shelter filled with danger as the team had to choose whether to protect the serum "
                "or rescue a teammate, creating open conflict with a visible cost."
            ),
        ]

        def memory_loader():
            return {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "type": "character",
                        "name": "Mira",
                        "data": {"status": "dead"},
                    }
                ],
            }

        loop_result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            memory_loader=memory_loader,
            generator=lambda input_pack: outputs.pop(0),
            repairer=lambda chapter, validation, input_pack: chapter,
        ).run_loop(steps=2, persist=True, stop_on_rejection=False)

        saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(2, loop_result["completed_steps"])
        self.assertEqual(2, loop_result["session"]["completed_steps"])
        self.assertEqual([False, True], [run["committed"] for run in loop_result["runs"]])
        self.assertEqual(1, loop_result["session"]["committed_count"])
        self.assertEqual(1, loop_result["session"]["rejected_count"])
        self.assertFalse(loop_result["succeeded"])
        self.assertEqual(1, loop_result["exit_code"])
        self.assertEqual(["run_rejected"], loop_result["failure_reasons"])
        self.assertEqual("recover_from_rejected_run", loop_result["runs"][1]["decision"]["goal"])
        self.assertTrue(loop_result["runs"][1]["run"]["recovery_context"]["available"])
        self.assertEqual(loop_result["runs"][0]["run"]["id"], loop_result["runs"][1]["run"]["recovery_context"]["source_run_id"])
        self.assertEqual(1, len(loop_result["session"]["recovery_links"]))
        self.assertEqual(loop_result["runs"][0]["run"]["id"], loop_result["session"]["recovery_links"][0]["source_run_id"])
        self.assertEqual(loop_result["runs"][1]["run"]["id"], loop_result["session"]["recovery_links"][0]["run_id"])
        self.assertTrue(loop_result["session"]["runs"][1]["trace_plan_aligned"])
        self.assertEqual(3, saved["chapter_index"])

    def test_run_loop_persists_failed_session_when_step_raises(self) -> None:
        tmp_path = self._case_dir("loop_failed_session")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)
        outputs = [
            "The shelter faced danger as the team had to choose between a costly rescue and the serum, creating conflict.",
        ]

        def generator(input_pack: str) -> str:
            if outputs:
                return outputs.pop(0)
            raise ValueError("generation failed")

        with self.assertRaises(LoopExecutionError) as raised:
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                chapter_dir=tmp_path / "chapters",
                dry_run=True,
                generator=generator,
            ).run_loop(steps=2, persist=True)

        session = raised.exception.session
        self.assertEqual("failed", session["stopped_reason"])
        self.assertEqual(2, session["completed_steps"])
        self.assertEqual(1, session["committed_count"])
        self.assertEqual(1, session["failed_count"])
        self.assertEqual("ValueError", session["error"]["type"])
        self.assertIs(session, validate_schema(session, "loop_session.schema.json"))
        self.assertEqual(2, len(raised.exception.runs))
        self.assertEqual("failed", raised.exception.runs[-1]["run"]["status"])
        self.assertEqual("ValueError", raised.exception.original.__class__.__name__)
        session_files = list((tmp_path / "runs" / "loop_sessions").glob("loop_*.json"))
        self.assertEqual(1, len(session_files))
        saved_session = json.loads(session_files[0].read_text(encoding="utf-8"))
        self.assertIs(saved_session, validate_schema(saved_session, "loop_session.schema.json"))
        self.assertEqual("failed", saved_session["stopped_reason"])
        self.assertEqual(str(session_files[0]), saved_session["artifact"]["path"])
        self.assertEqual(str(session_files[0]), session["artifact"]["path"])
        self.assertEqual(["committed", "failed"], [run["status"] for run in saved_session["runs"]])
        self.assertEqual(2, len(list((tmp_path / "runs").glob("chapter_*.json"))))


if __name__ == "__main__":
    unittest.main()
