from __future__ import annotations

import unittest
from pathlib import Path
import json
import uuid

import core.analyzer as legacy_analyzer
import core.generator as legacy_generator
import core.input_pack as legacy_input_pack
import core.orchestrator as legacy_orchestrator
import core.snapshot as legacy_snapshot
import core.updater as legacy_updater
import modules as feature_modules
import workflows
import api
import core
from core.state import input_pack as state_input_pack
from core.state.builder import build_snapshot_state_with_audit
from core.state import snapshot as state_snapshot
from core.engine.executor import AgentExecutor
from api.openai_client import chat_completion
from api.claude_client import polish_chapter as api_polish_chapter
from api.notion_client import create_database_page, query_database_pages
from workflows.dynamic_flow import build_dynamic_flow_plan
from modules.chapter_generator import generate_chapter
from modules.claude_polish import polish_chapter
from modules.conflict_engine import analyze_chapter
from modules.scene_repair import build_repair_plan, repair_scene


class LegacyWrapperTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_legacy_wrappers" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _write_snapshot(self, path: Path) -> None:
        path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

    def test_legacy_core_imports_delegate_to_v1_modules(self) -> None:
        self.assertIs(generate_chapter, legacy_generator.generate_chapter)
        self.assertIs(state_input_pack.build_input_pack, legacy_input_pack.build_input_pack)
        self.assertIs(state_input_pack.build_input_pack_metadata, legacy_input_pack.build_input_pack_metadata)
        self.assertIs(state_input_pack.build_recovery_context, legacy_input_pack.build_recovery_context)
        self.assertIs(state_input_pack.build_recovery_context_metadata, legacy_input_pack.build_recovery_context_metadata)
        self.assertIs(state_input_pack.build_snapshot_input_pack, legacy_input_pack.build_snapshot_input_pack)
        self.assertIs(state_snapshot.load_snapshot, legacy_snapshot.load_snapshot)
        self.assertIs(state_snapshot.save_snapshot, legacy_snapshot.save_snapshot)
        self.assertIs(state_snapshot.update_snapshot, legacy_snapshot.update_snapshot)
        self.assertIs(state_snapshot.build_state_update_audit, legacy_snapshot.build_state_update_audit)
        self.assertIs(state_snapshot.update_snapshot, legacy_updater.update_snapshot)
        self.assertIs(state_snapshot.build_state_update_audit, legacy_updater.build_state_update_audit)
        self.assertIs(analyze_chapter, legacy_analyzer.analyze_chapter)

    def test_package_exports_expose_v1_surfaces(self) -> None:
        import core.engine as engine_package
        import core.state as state_package

        self.assertIs(AgentExecutor, core.AgentExecutor)
        self.assertIs(legacy_orchestrator.run_agent_once, core.run_agent_once)
        self.assertIs(legacy_orchestrator.run_agent_loop, core.run_agent_loop)
        self.assertIs(legacy_orchestrator.check_runtime, core.check_runtime)
        self.assertIs(legacy_orchestrator.report_runs, core.report_runs)
        self.assertTrue(callable(engine_package.run_once))
        self.assertTrue(callable(engine_package.run_preflight))
        self.assertTrue(callable(engine_package.build_run_report))
        self.assertIs(build_snapshot_state_with_audit, state_package.build_snapshot_state_with_audit)
        self.assertIs(build_dynamic_flow_plan, workflows.build_dynamic_flow_plan)
        self.assertIs(generate_chapter, feature_modules.generate_chapter)
        self.assertIs(polish_chapter, feature_modules.polish_chapter)
        self.assertIs(analyze_chapter, feature_modules.analyze_chapter)
        self.assertIs(build_repair_plan, feature_modules.build_repair_plan)
        self.assertIs(repair_scene, feature_modules.repair_scene)
        self.assertIs(chat_completion, api.chat_completion)
        self.assertIs(api_polish_chapter, api.polish_chapter)
        self.assertIs(create_database_page, api.create_database_page)
        self.assertIs(query_database_pages, api.query_database_pages)

    def test_legacy_orchestrator_runs_through_agent_executor(self) -> None:
        result = legacy_orchestrator.run_agent_once(dry_run=True, persist=False)

        self.assertIn("run", result)
        self.assertIn("validation", result)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("preview", result["run"]["status"])

    def test_legacy_orchestrator_run_once_returns_chapter_text(self) -> None:
        chapter = legacy_orchestrator.run_once(dry_run=True, persist=False)

        self.assertIsInstance(chapter, str)
        self.assertIn("conflict", chapter.lower())

    def test_legacy_orchestrator_accepts_v1_runtime_paths(self) -> None:
        tmp_path = self._case_dir("paths")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        result = legacy_orchestrator.run_agent_once(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            persist=True,
        )

        self.assertTrue(result["committed"])
        self.assertEqual(2, json.loads(snapshot_path.read_text(encoding="utf-8"))["chapter_index"])
        self.assertEqual(1, len(list((tmp_path / "runs").glob("chapter_1_*.json"))))
        self.assertEqual(1, len(list((tmp_path / "chapters").glob("chapter_0001_*.md"))))
        run_payload = json.loads(next((tmp_path / "runs").glob("chapter_1_*.json")).read_text(encoding="utf-8"))
        self.assertTrue(run_payload["run"]["state_update"]["applied"])
        self.assertEqual(2, run_payload["run"]["state_update"]["next_chapter_index"])

    def test_legacy_orchestrator_exposes_v1_loop_preflight_and_report(self) -> None:
        tmp_path = self._case_dir("loop")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        preflight = legacy_orchestrator.check_runtime(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
        )
        loop = legacy_orchestrator.run_agent_loop(
            steps=2,
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            persist=True,
        )
        report = legacy_orchestrator.report_runs(run_dir=tmp_path / "runs", limit=2)

        self.assertTrue(preflight["ok"])
        self.assertEqual(2, loop["completed_steps"])
        self.assertEqual("max_steps", loop["stopped_reason"])
        self.assertEqual(2, report["loaded"])
        self.assertEqual({"committed": 2}, report["status_counts"])

    def test_legacy_orchestrator_check_runtime_accepts_loop_mode(self) -> None:
        tmp_path = self._case_dir("check_loop_mode")
        snapshot_path = tmp_path / "snapshot.json"
        self._write_snapshot(snapshot_path)

        preflight = legacy_orchestrator.check_runtime(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            persist=True,
            steps=3,
            continue_on_rejection=True,
        )

        execution = [check for check in preflight["checks"] if check["name"] == "execution_mode"][0]
        self.assertTrue(execution["details"]["persist"])
        self.assertEqual(3, execution["details"]["steps"])
        self.assertFalse(execution["details"]["stop_on_rejection"])


if __name__ == "__main__":
    unittest.main()
