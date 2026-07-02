from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.director import DirectorDecisionError
from core.engine.executor import AgentExecutor, LoopExecutionError
from core.engine.run_record import build_run_record
from core.engine.workflow import WorkflowError
from core.schema import SchemaValidationError, validate_schema


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
