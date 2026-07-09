from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import core.engine.preflight as preflight_module
from core.engine.preflight import run_preflight
from core.engine.run_record import build_run_record
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_MEMORY_OUTBOX, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self._claude_alias_env = {
            "ANTHROPIC_AUTH_TOKEN": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL"),
        }
        os.environ["ANTHROPIC_AUTH_TOKEN"] = ""
        os.environ["ANTHROPIC_MODEL"] = ""

    def tearDown(self) -> None:
        for name, value in self._claude_alias_env.items():
            _restore_env(name, value)

    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_preflight" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_dry_run_preflight_accepts_valid_inputs_without_api_keys(self) -> None:
        tmp_path = self._case_dir("dry_run")
        snapshot_path = tmp_path / "snapshot.json"
        memory_path = tmp_path / "memory.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        memory_path.write_text(
            json.dumps({"source": "test", "status": "ready", "items": []}),
            encoding="utf-8",
        )

        result = run_preflight(snapshot_path=snapshot_path, memory_path=memory_path, dry_run=True)

        self.assertTrue(result["ok"])
        check_names = {check["name"] for check in result["checks"]}
        self.assertIn("prompt_assets", check_names)
        self.assertIn("schema_assets", check_names)
        self.assertIn("v1_structure", check_names)
        self.assertIn("schema_consistency", check_names)
        self.assertNotIn("memory_v2_compile", check_names)
        self.assertIn("artifact_targets", check_names)
        self.assertIn("planned_flow", check_names)
        self.assertIn("state_builder_audit", check_names)
        self.assertIn("runtime_state_summary", check_names)
        structure = [check for check in result["checks"] if check["name"] == "v1_structure"][0]
        self.assertEqual(len(preflight_module.V1_STRUCTURE_PATHS), structure["details"]["count"])
        memory_input = [check for check in result["checks"] if check["name"] == "memory_input"][0]
        self.assertEqual("auto", memory_input["details"]["normalized_source"])
        self.assertEqual("file", memory_input["details"]["resolved_source"])
        self.assertEqual("explicit_memory_path", memory_input["details"]["resolution_reason"])
        self.assertEqual(str(memory_path), memory_input["details"]["resolved_path"])
        self.assertTrue(memory_input["details"]["path_exists"])
        memory = [check for check in result["checks"] if check["name"] == "memory"][0]
        self.assertEqual("auto", memory["details"]["requested_source"])
        self.assertEqual("test", memory["details"]["source"])
        self.assertEqual("ready", memory["details"]["status"])
        self.assertEqual(0, memory["details"]["item_count"])
        self.assertEqual(0, memory["details"]["source_mapping_count"])
        flow = [check for check in result["checks"] if check["name"] == "planned_flow"][0]
        self.assertEqual(["generate_chapter", "polish", "validate", "repair_if_needed"], flow["details"]["actions"])
        self.assertEqual("generate_chapter", flow["details"]["steps"][0]["action"])
        audit = [check for check in result["checks"] if check["name"] == "state_builder_audit"][0]
        self.assertEqual(0, audit["details"]["applied_count"])
        consistency = [check for check in result["checks"] if check["name"] == "schema_consistency"][0]
        self.assertEqual(9, consistency["details"]["count"])
        execution = [check for check in result["checks"] if check["name"] == "execution_mode"][0]
        self.assertFalse(execution["details"]["persist"])
        self.assertTrue(execution["details"]["dry_run"])
        self.assertEqual([], execution["details"]["model_calls"])
        self.assertEqual(str(memory_path), execution["details"]["memory_path"])

    def test_default_preflight_paths_use_local_runtime_dir(self) -> None:
        result = run_preflight(dry_run=True)

        self.assertTrue(result["ok"])
        execution = [check for check in result["checks"] if check["name"] == "execution_mode"][0]
        self.assertEqual(str(DEFAULT_SNAPSHOT_PATH), execution["details"]["snapshot_path"])
        self.assertEqual(str(DEFAULT_RUN_DIR), execution["details"]["run_dir"])
        self.assertEqual(str(DEFAULT_CHAPTER_DIR), execution["details"]["chapter_dir"])

    def test_memory_v2_compile_check_is_opt_in_dry_run(self) -> None:
        tmp_path = self._case_dir("memory_v2")
        snapshot_path = tmp_path / "snapshot.json"
        output_dir = tmp_path / "memory_v2_out"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            memory_path=Path("data/notion_memory.example.json"),
            memory_source="file",
            dry_run=True,
            check_memory_v2=True,
            memory_v2_output_dir=output_dir,
        )

        self.assertTrue(result["ok"])
        memory_v2 = [check for check in result["checks"] if check["name"] == "memory_v2_compile"][0]
        self.assertTrue(memory_v2["ok"])
        self.assertTrue(memory_v2["details"]["dry_run"])
        self.assertTrue(memory_v2["details"]["reset"])
        self.assertGreater(memory_v2["details"]["operation_count"], 0)
        self.assertGreater(memory_v2["details"]["event_count"], 0)
        for name in (
            "canonical_memory.json",
            "memory_events.jsonl",
            "memory_patch.json",
            "snapshot_preview.json",
            "memory_compile_report.json",
        ):
            self.assertFalse((output_dir / name).exists(), name)

    def test_preflight_reports_memory_source_mapping_summary(self) -> None:
        tmp_path = self._case_dir("memory_mappings")
        snapshot_path = tmp_path / "snapshot.json"
        memory_path = tmp_path / "notion_memory.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        memory_path.write_text(
            json.dumps(
                {
                    "pages": [
                        {
                            "id": "page-1",
                            "url": "https://notion.test/page-1",
                            "properties": {
                                "Type": "location",
                                "Name": "shelter",
                                "Risk": "rising",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_preflight(snapshot_path=snapshot_path, memory_path=memory_path, dry_run=True)

        self.assertTrue(result["ok"])
        memory = [check for check in result["checks"] if check["name"] == "memory"][0]
        self.assertEqual("notion-export", memory["details"]["source"])
        self.assertEqual(1, memory["details"]["source_mapping_count"])
        self.assertEqual([{"source": "notion-export", "count": 1}], memory["details"]["source_mapping_sources"])
        self.assertEqual(1, memory["details"]["file_mapping_count"])
        self.assertEqual(0, memory["details"]["line_mapping_count"])
        self.assertEqual(1, memory["details"]["notion_page_mapping_count"])
        self.assertEqual(1, memory["details"]["notion_page_url_count"])

    def test_memory_input_details_resolves_auto_to_notion_without_loading_network(self) -> None:
        original_key = os.environ.get("NOTION_API_KEY")
        original_database = os.environ.get("NOTION_DATABASE_ID")
        original_novelagent_database = os.environ.get("NOVELAGENT_NOTION_DATABASE_ID")
        os.environ["NOTION_API_KEY"] = "secret"
        os.environ["NOTION_DATABASE_ID"] = "database"
        os.environ["NOVELAGENT_NOTION_DATABASE_ID"] = ""

        try:
            details = preflight_module._memory_input_details(memory_path=None, memory_source="auto")
        finally:
            _restore_env("NOTION_API_KEY", original_key)
            _restore_env("NOTION_DATABASE_ID", original_database)
            _restore_env("NOVELAGENT_NOTION_DATABASE_ID", original_novelagent_database)

        self.assertTrue(details["valid"])
        self.assertTrue(details["notion_api_configured"])
        self.assertEqual("notion-api", details["resolved_source"])
        self.assertEqual("auto_notion_configured", details["resolution_reason"])
        self.assertIsNone(details["resolved_path"])

    def test_forced_notion_memory_source_requires_notion_config(self) -> None:
        original_key = os.environ.get("NOTION_API_KEY")
        original_database = os.environ.get("NOTION_DATABASE_ID")
        original_novelagent_database = os.environ.get("NOVELAGENT_NOTION_DATABASE_ID")
        os.environ["NOTION_API_KEY"] = ""
        os.environ["NOTION_DATABASE_ID"] = ""
        os.environ["NOVELAGENT_NOTION_DATABASE_ID"] = ""
        tmp_path = self._case_dir("forced_notion_missing_config")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(
                snapshot_path=snapshot_path,
                memory_source="notion",
                dry_run=True,
            )
        finally:
            _restore_env("NOTION_API_KEY", original_key)
            _restore_env("NOTION_DATABASE_ID", original_database)
            _restore_env("NOVELAGENT_NOTION_DATABASE_ID", original_novelagent_database)

        self.assertFalse(result["ok"])
        memory_input = [check for check in result["checks"] if check["name"] == "memory_input"][0]
        self.assertTrue(memory_input["ok"])
        self.assertEqual("notion-api", memory_input["details"]["resolved_source"])
        self.assertEqual("forced_notion", memory_input["details"]["resolution_reason"])
        memory = [check for check in result["checks"] if check["name"] == "memory"][0]
        self.assertFalse(memory["ok"])
        self.assertIn("NOTION_DATABASE_ID", memory["error"])

    def test_preflight_reports_execution_mode_for_persisted_loop(self) -> None:
        tmp_path = self._case_dir("execution_mode")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            persist=True,
            steps=3,
            continue_on_rejection=True,
        )

        self.assertTrue(result["ok"])
        execution = [check for check in result["checks"] if check["name"] == "execution_mode"][0]
        self.assertTrue(execution["details"]["persist"])
        self.assertEqual(3, execution["details"]["steps"])
        self.assertFalse(execution["details"]["stop_on_rejection"])
        self.assertEqual(str(tmp_path / "runs"), execution["details"]["run_dir"])

    def test_preflight_reports_valid_run_history(self) -> None:
        tmp_path = self._case_dir("run_history")
        snapshot_path = tmp_path / "snapshot.json"
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        run = build_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 1},
            runtime_snapshot={"chapter_index": 1},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 1,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="The shelter faced danger as the team had to choose a costly rescue.",
            validation={"ok": True, "problems": []},
            analysis={
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team had to choose."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team had to choose.",
            },
            repair_attempts=0,
            committed=True,
        )
        (run_dir / f"{run['id']}.json").write_text(
            json.dumps({"run": run}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = run_preflight(snapshot_path=snapshot_path, run_dir=run_dir, dry_run=True)

        self.assertTrue(result["ok"])
        history = [check for check in result["checks"] if check["name"] == "run_history"][0]
        self.assertTrue(history["ok"])
        self.assertEqual(1, history["details"]["total"])
        self.assertEqual(1, history["details"]["loaded"])
        self.assertEqual(run["id"], history["details"]["latest_run_id"])
        self.assertEqual("committed", history["details"]["latest_run_status"])
        self.assertEqual(0, history["details"]["latest_run_problem_count"])
        self.assertEqual(["logic"], history["details"]["latest_run_requested_focus"])
        self.assertEqual(["logic"], history["details"]["latest_run_executed_checks"])
        self.assertEqual(["continuity", "spatial"], history["details"]["latest_run_skipped_checks"])

    def test_preflight_reports_latest_loop_session_validation_coverage(self) -> None:
        tmp_path = self._case_dir("loop_history")
        snapshot_path = tmp_path / "snapshot.json"
        run_dir = tmp_path / "runs"
        session_dir = run_dir / "loop_sessions"
        session_dir.mkdir(parents=True)
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        session = {
            "id": "loop_20260101T000000000000Z",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "requested_steps": 2,
            "completed_steps": 2,
            "stopped_reason": "rejected",
            "persist": True,
            "stop_on_rejection": True,
            "committed_count": 1,
            "rejected_count": 1,
            "failed_count": 0,
            "first_chapter_index": 1,
            "last_chapter_index": 2,
            "last_run_id": "chapter_2_test",
            "recovery_links": [],
            "runs": [
                {
                    "id": "chapter_1_test",
                    "status": "committed",
                    "committed": True,
                    "chapter_index": 1,
                    "problem_codes": [],
                    "requested_focus": ["continuity", "spatial", "logic"],
                    "executed_checks": ["continuity", "spatial", "logic"],
                    "skipped_checks": [],
                    "repair_attempts": 0,
                },
                {
                    "id": "chapter_2_test",
                    "status": "rejected",
                    "committed": False,
                    "chapter_index": 2,
                    "problem_codes": ["missing_conflict_marker"],
                    "requested_focus": ["logic"],
                    "executed_checks": ["logic"],
                    "skipped_checks": ["continuity", "spatial"],
                    "repair_attempts": 1,
                },
            ],
        }
        (session_dir / f"{session['id']}.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = run_preflight(snapshot_path=snapshot_path, run_dir=run_dir, dry_run=True)

        self.assertTrue(result["ok"])
        history = [check for check in result["checks"] if check["name"] == "run_history"][0]
        self.assertEqual(session["id"], history["details"]["latest_loop_session_id"])
        self.assertEqual("rejected", history["details"]["latest_loop_session_stopped_reason"])
        self.assertEqual("chapter_2_test", history["details"]["latest_loop_session_last_run_id"])
        self.assertEqual("rejected", history["details"]["latest_loop_session_last_run_status"])
        self.assertEqual(["logic"], history["details"]["latest_loop_session_last_run_executed_checks"])
        self.assertEqual(
            ["continuity", "spatial"],
            history["details"]["latest_loop_session_last_run_skipped_checks"],
        )

    def test_preflight_rejects_invalid_run_history_artifact(self) -> None:
        tmp_path = self._case_dir("bad_run_history")
        snapshot_path = tmp_path / "snapshot.json"
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        (run_dir / "chapter_1_bad.json").write_text(json.dumps({"run": {"id": "chapter_1_bad"}}), encoding="utf-8")

        result = run_preflight(snapshot_path=snapshot_path, run_dir=run_dir, dry_run=True)

        self.assertFalse(result["ok"])
        history = [check for check in result["checks"] if check["name"] == "run_history"][0]
        self.assertFalse(history["ok"])
        self.assertEqual(1, history["details"]["total"])
        self.assertEqual(0, history["details"]["loaded"])
        self.assertEqual(1, history["details"]["skipped"])
        self.assertIn("run_record.schema.json", history["error"])

    def test_preflight_rejects_invalid_loop_steps(self) -> None:
        tmp_path = self._case_dir("bad_loop_steps")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            steps=0,
        )

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "loop_parameters"][0]
        self.assertFalse(failed["ok"])
        self.assertEqual(0, failed["details"]["steps"])
        self.assertIn("steps must be at least 1", failed["error"])

    def test_preflight_rejects_artifact_target_that_is_file(self) -> None:
        tmp_path = self._case_dir("bad_artifact_target")
        snapshot_path = tmp_path / "snapshot.json"
        run_dir = tmp_path / "runs"
        run_dir.write_text("not a directory", encoding="utf-8")
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            run_dir=run_dir,
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
        )

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "artifact_targets"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("not a directory", failed["error"])

    def test_preflight_rejects_chapter_artifact_target_that_is_file(self) -> None:
        tmp_path = self._case_dir("bad_chapter_artifact_target")
        snapshot_path = tmp_path / "snapshot.json"
        chapter_dir = tmp_path / "chapters"
        chapter_dir.write_text("not a directory", encoding="utf-8")
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=chapter_dir,
            dry_run=True,
        )

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "artifact_targets"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("chapter_dir", failed["error"])
        self.assertIn("not a directory", failed["error"])

    def test_preflight_rejects_missing_prompt_asset(self) -> None:
        tmp_path = self._case_dir("missing_prompt_asset")
        snapshot_path = tmp_path / "snapshot.json"
        missing_prompt = tmp_path / "missing_prompt.md"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        original_assets = preflight_module.PROMPT_ASSETS
        preflight_module.PROMPT_ASSETS = (missing_prompt,)

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True)
        finally:
            preflight_module.PROMPT_ASSETS = original_assets

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "prompt_assets"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("missing", failed["error"])

    def test_preflight_rejects_invalid_schema_asset(self) -> None:
        tmp_path = self._case_dir("bad_schema_asset")
        snapshot_path = tmp_path / "snapshot.json"
        schema_path = tmp_path / "bad.schema.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        schema_path.write_text("{bad json", encoding="utf-8")
        original_assets = preflight_module.SCHEMA_ASSETS
        preflight_module.SCHEMA_ASSETS = (schema_path,)

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True)
        finally:
            preflight_module.SCHEMA_ASSETS = original_assets

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "schema_assets"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("invalid JSON", failed["error"])

    def test_preflight_rejects_unsupported_schema_asset_keyword(self) -> None:
        tmp_path = self._case_dir("unsupported_schema_asset")
        snapshot_path = tmp_path / "snapshot.json"
        schema_path = tmp_path / "unsupported.schema.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        schema_path.write_text(
            json.dumps({"type": "object", "properties": {"name": {"type": "string", "pattern": "^[A-Z]"}}}),
            encoding="utf-8",
        )
        original_assets = preflight_module.SCHEMA_ASSETS
        preflight_module.SCHEMA_ASSETS = (schema_path,)

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True)
        finally:
            preflight_module.SCHEMA_ASSETS = original_assets

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "schema_assets"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("unsupported.schema.json.properties.name.pattern is unsupported", failed["error"])

    def test_preflight_rejects_missing_v1_structure_path(self) -> None:
        tmp_path = self._case_dir("missing_v1_structure")
        snapshot_path = tmp_path / "snapshot.json"
        missing_path = tmp_path / "missing.py"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        original_paths = preflight_module.V1_STRUCTURE_PATHS
        preflight_module.V1_STRUCTURE_PATHS = (missing_path,)
        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True)
        finally:
            preflight_module.V1_STRUCTURE_PATHS = original_paths

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if check["name"] == "v1_structure"][0]
        self.assertFalse(failed["ok"])
        self.assertEqual([str(missing_path)], failed["details"]["paths"])
        self.assertIn("missing", failed["error"])

    def test_preflight_rejects_invalid_snapshot(self) -> None:
        tmp_path = self._case_dir("bad_snapshot")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 0, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(snapshot_path=snapshot_path, dry_run=True)

        self.assertFalse(result["ok"])
        failed = [check for check in result["checks"] if not check["ok"]]
        self.assertEqual("snapshot", failed[0]["name"])

    def test_preflight_reports_invalid_memory_source_mode(self) -> None:
        tmp_path = self._case_dir("bad_memory_source")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(snapshot_path=snapshot_path, memory_source="database", dry_run=True)

        self.assertFalse(result["ok"])
        memory_input = [check for check in result["checks"] if check["name"] == "memory_input"][0]
        self.assertFalse(memory_input["ok"])
        self.assertEqual("invalid_source", memory_input["details"]["resolution_reason"])
        failed = [check for check in result["checks"] if check["name"] == "memory"][0]
        self.assertFalse(failed["ok"])
        self.assertEqual("database", failed["details"]["requested_source"])
        self.assertIn("memory source", failed["error"])

    def test_non_dry_run_preflight_requires_openai_key(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        tmp_path = self._case_dir("missing_openai")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=False)
        finally:
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key

        self.assertFalse(result["ok"])
        self.assertIn("env:OPENAI_API_KEY", {check["name"] for check in result["checks"] if not check["ok"]})

    def test_non_dry_run_preflight_checks_openai_dependency(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        original_find_spec = preflight_module.find_spec
        os.environ["OPENAI_API_KEY"] = "test-openai"
        tmp_path = self._case_dir("missing_openai_dependency")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        preflight_module.find_spec = lambda name: None if name == "openai" else object()

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=False)
        finally:
            preflight_module.find_spec = original_find_spec
            _restore_env("OPENAI_API_KEY", original_key)

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("dependency:openai", failed_names)

    def test_dry_run_with_model_director_requires_openai_key(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        tmp_path = self._case_dir("missing_director_openai")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(
                snapshot_path=snapshot_path,
                dry_run=True,
                director_model="gpt-4.1-mini",
            )
        finally:
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("env:OPENAI_API_KEY", failed_names)
        director = [check for check in result["checks"] if check["name"] == "director"][0]
        self.assertEqual("model", director["details"]["mode"])

    def test_dry_run_with_rule_director_does_not_require_openai_key(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        tmp_path = self._case_dir("rule_director_no_openai")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True)
        finally:
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key

        self.assertTrue(result["ok"])
        director = [check for check in result["checks"] if check["name"] == "director"][0]
        self.assertEqual("rule", director["details"]["mode"])

    def test_dry_run_with_llm_validator_requires_openai_key(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        tmp_path = self._case_dir("missing_llm_validator_openai")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(
                snapshot_path=snapshot_path,
                dry_run=True,
                enable_llm_validator=True,
            )
        finally:
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("env:OPENAI_API_KEY", failed_names)
        execution = [check for check in result["checks"] if check["name"] == "execution_mode"][0]
        self.assertIn("llm_validation_openai", execution["details"]["model_calls"])
        llm_check = [check for check in result["checks"] if check["name"] == "llm_validator"][0]
        self.assertTrue(llm_check["details"]["enabled"])

    def test_require_claude_checks_even_in_dry_run(self) -> None:
        original_key = os.environ.get("ANTHROPIC_API_KEY")
        original_model = os.environ.get("CLAUDE_MODEL")
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["CLAUDE_MODEL"] = ""
        tmp_path = self._case_dir("require_claude")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True, require_claude=True)
        finally:
            if original_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = original_key
            if original_model is None:
                os.environ.pop("CLAUDE_MODEL", None)
            else:
                os.environ["CLAUDE_MODEL"] = original_model

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("env:ANTHROPIC_API_KEY", failed_names)
        self.assertIn("env:CLAUDE_MODEL", failed_names)

    def test_require_claude_checks_anthropic_dependency(self) -> None:
        original_key = os.environ.get("ANTHROPIC_API_KEY")
        original_model = os.environ.get("CLAUDE_MODEL")
        original_find_spec = preflight_module.find_spec
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["CLAUDE_MODEL"] = "claude-test"
        tmp_path = self._case_dir("missing_anthropic_dependency")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        preflight_module.find_spec = lambda name: None if name == "anthropic" else object()

        try:
            result = run_preflight(snapshot_path=snapshot_path, dry_run=True, require_claude=True)
        finally:
            preflight_module.find_spec = original_find_spec
            _restore_env("ANTHROPIC_API_KEY", original_key)
            _restore_env("CLAUDE_MODEL", original_model)

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("dependency:anthropic", failed_names)

    def test_non_dry_run_rule_workflow_requires_claude_when_polish_planned(self) -> None:
        original_openai = os.environ.get("OPENAI_API_KEY")
        original_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        original_claude_model = os.environ.get("CLAUDE_MODEL")
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["CLAUDE_MODEL"] = ""
        tmp_path = self._case_dir("rule_requires_claude")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(snapshot_path=snapshot_path, run_dir=tmp_path / "runs", dry_run=False)
        finally:
            _restore_env("OPENAI_API_KEY", original_openai)
            _restore_env("ANTHROPIC_API_KEY", original_anthropic)
            _restore_env("CLAUDE_MODEL", original_claude_model)

        self.assertFalse(result["ok"])
        planned = [check for check in result["checks"] if check["name"] == "planned_workflow"][0]
        self.assertIn("polish", planned["ok"] and planned.get("details", planned))
        execution = [check for check in result["checks"] if check["name"] == "execution_mode"][0]
        self.assertEqual(["chapter_generation_openai", "claude_polish"], execution["details"]["model_calls"])
        self.assertTrue(execution["details"]["persist"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("env:ANTHROPIC_API_KEY", failed_names)
        self.assertIn("env:CLAUDE_MODEL", failed_names)

    def test_recovery_rule_workflow_skips_claude_check_when_polish_not_planned(self) -> None:
        original_openai = os.environ.get("OPENAI_API_KEY")
        original_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        original_claude_model = os.environ.get("CLAUDE_MODEL")
        original_find_spec = preflight_module.find_spec
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["CLAUDE_MODEL"] = ""
        tmp_path = self._case_dir("recovery_skips_claude")
        snapshot_path = tmp_path / "snapshot.json"
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rejected_run = build_run_record(
            started_at=now,
            finished_at=now,
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
            chapter="The scene did not provide a clear conflict.",
            validation={
                "ok": False,
                "problems": [
                    {
                        "code": "missing_conflict_marker",
                        "message": "Missing conflict signal.",
                        "severity": "high",
                        "blocking": True,
                        "category": "logic",
                        "repair_hint": "Add explicit danger, choice, threat, secret, cost, or conflict.",
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
        (run_dir / f"{rejected_run['id']}.json").write_text(
            json.dumps({"run": rejected_run}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        preflight_module.find_spec = lambda name: object()

        try:
            result = run_preflight(snapshot_path=snapshot_path, run_dir=run_dir, dry_run=False)
        finally:
            preflight_module.find_spec = original_find_spec
            _restore_env("OPENAI_API_KEY", original_openai)
            _restore_env("ANTHROPIC_API_KEY", original_anthropic)
            _restore_env("CLAUDE_MODEL", original_claude_model)

        self.assertTrue(result["ok"])
        planned = [check for check in result["checks"] if check["name"] == "planned_workflow"][0]
        self.assertEqual(["generate_chapter", "validate", "repair_if_needed"], planned["details"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertNotIn("env:ANTHROPIC_API_KEY", failed_names)
        self.assertNotIn("env:CLAUDE_MODEL", failed_names)

    def test_non_dry_run_model_director_conservatively_requires_claude(self) -> None:
        original_openai = os.environ.get("OPENAI_API_KEY")
        original_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        original_claude_model = os.environ.get("CLAUDE_MODEL")
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["CLAUDE_MODEL"] = ""
        tmp_path = self._case_dir("model_requires_claude")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                dry_run=False,
                director_model="gpt-4.1-mini",
            )
        finally:
            _restore_env("OPENAI_API_KEY", original_openai)
            _restore_env("ANTHROPIC_API_KEY", original_anthropic)
            _restore_env("CLAUDE_MODEL", original_claude_model)

        self.assertFalse(result["ok"])
        planned = [check for check in result["checks"] if check["name"] == "planned_workflow"][0]
        self.assertEqual("model", planned["details"]["source"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("env:ANTHROPIC_API_KEY", failed_names)
        self.assertIn("env:CLAUDE_MODEL", failed_names)

    def test_memory_outbox_implies_file_writeback_preflight(self) -> None:
        tmp_path = self._case_dir("outbox_preflight")
        snapshot_path = tmp_path / "snapshot.json"
        outbox_path = tmp_path / "memory_outbox.jsonl"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            dry_run=True,
            memory_outbox=outbox_path,
        )

        self.assertTrue(result["ok"])
        writeback = [check for check in result["checks"] if check["name"] == "memory_writeback"][0]
        self.assertEqual("file", writeback["details"]["mode"])
        self.assertEqual(str(outbox_path), writeback["details"]["path"])

    def test_file_writeback_preflight_uses_default_outbox(self) -> None:
        tmp_path = self._case_dir("file_writeback_default")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path=snapshot_path,
            dry_run=True,
            memory_writeback="file",
        )

        self.assertTrue(result["ok"])
        writeback = [check for check in result["checks"] if check["name"] == "memory_writeback"][0]
        self.assertEqual("file", writeback["details"]["mode"])
        self.assertEqual(str(DEFAULT_MEMORY_OUTBOX), writeback["details"]["path"])

    def test_notion_writeback_preflight_requires_notion_config(self) -> None:
        original_key = os.environ.get("NOTION_API_KEY")
        original_database = os.environ.get("NOTION_DATABASE_ID")
        original_novelagent_database = os.environ.get("NOVELAGENT_NOTION_DATABASE_ID")
        os.environ["NOTION_API_KEY"] = ""
        os.environ["NOTION_DATABASE_ID"] = ""
        os.environ["NOVELAGENT_NOTION_DATABASE_ID"] = ""
        tmp_path = self._case_dir("notion_writeback")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(
                snapshot_path=snapshot_path,
                dry_run=True,
                memory_writeback="notion",
            )
        finally:
            _restore_env("NOTION_API_KEY", original_key)
            _restore_env("NOTION_DATABASE_ID", original_database)
            _restore_env("NOVELAGENT_NOTION_DATABASE_ID", original_novelagent_database)

        self.assertFalse(result["ok"])
        writeback = [check for check in result["checks"] if check["name"] == "memory_writeback"][0]
        self.assertEqual("notion", writeback["details"]["mode"])
        self.assertTrue(writeback["details"]["notion_dedupe_existing"])
        failed_names = {check["name"] for check in result["checks"] if not check["ok"]}
        self.assertIn("env:NOTION_API_KEY", failed_names)
        self.assertIn("env:NOTION_DATABASE_ID", failed_names)

    def test_notion_readback_flag_is_reported_in_preflight(self) -> None:
        original_key = os.environ.get("NOTION_API_KEY")
        original_database = os.environ.get("NOTION_DATABASE_ID")
        os.environ["NOTION_API_KEY"] = "secret"
        os.environ["NOTION_DATABASE_ID"] = "db"
        tmp_path = self._case_dir("notion_readback")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        try:
            result = run_preflight(
                snapshot_path=snapshot_path,
                memory_source="file",
                dry_run=True,
                memory_writeback="notion",
                notion_readback=True,
            )
        finally:
            _restore_env("NOTION_API_KEY", original_key)
            _restore_env("NOTION_DATABASE_ID", original_database)

        self.assertTrue(result["ok"])
        writeback = [check for check in result["checks"] if check["name"] == "memory_writeback"][0]
        self.assertEqual("notion", writeback["details"]["mode"])
        self.assertTrue(writeback["details"]["notion_readback"])
        self.assertTrue(writeback["details"]["notion_dedupe_existing"])


if __name__ == "__main__":
    unittest.main()
