from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.report import build_run_report
from core.engine.workflow import build_workflow_plan


class RunReportTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_run_report" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_build_run_report_summarizes_recent_runs_and_artifacts(self) -> None:
        tmp_path = self._case_dir("summary")
        run_dir = tmp_path / "runs"
        chapter_path = tmp_path / "chapters" / "chapter.md"
        chapter_path.parent.mkdir()
        chapter_path.write_text("# Chapter", encoding="utf-8")
        run_dir.mkdir()
        self._write_run(
            run_dir / "chapter_1_20260101T000000000000Z.json",
            {
                "id": "chapter_1_20260101T000000000000Z",
                "status": "committed",
                "committed": True,
                "chapter_index": 1,
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
                "decision": {"goal": "continue_existing_arc"},
                "workflow": ["generate_chapter", "validate"],
                "workflow_plan": {
                    "recovery": False,
                    "steps": [
                        {"action": "generate_chapter", "mode": "required"},
                        {"action": "validate", "mode": "required"},
                    ],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 1,
                },
                "director": {"mode": "rule", "model": None, "status": "completed", "duration_ms": 2},
                "validation": {"ok": True, "problem_codes": [], "problem_count": 0},
                "analysis": {
                    "summary": "A chapter was committed.",
                    "conflict_count": 1,
                    "event_count": 2,
                    "world_change_count": 1,
                },
                "memory": {
                    "source": "test",
                    "status": "ready",
                    "item_count": 0,
                    "source_mapping_count": 2,
                    "source_mapping_sources": [{"source": "notion-export", "count": 2}],
                    "file_mapping_count": 1,
                    "line_mapping_count": 0,
                    "notion_page_mapping_count": 2,
                    "notion_page_url_count": 2,
                    "writeback": {
                        "target": "notion",
                        "written": 1,
                        "skipped": 1,
                        "item_mappings": [
                            {
                                "memory_id": "chapter_1:timeline_event:summary",
                                "type": "timeline_event",
                                "name": "chapter_1_summary",
                                "target": "notion",
                                "status": "written",
                                "page_id": "page-1",
                                "page_url": "https://notion.test/page-1",
                                "database_id": "db",
                                "property_names": ["Data", "Memory ID", "Name", "Type"],
                            },
                            {
                                "memory_id": "chapter_1:location:shelter",
                                "type": "location",
                                "name": "shelter",
                                "target": "notion",
                                "status": "skipped_duplicate",
                                "page_id": "page-existing",
                                "page_url": "https://notion.test/page-existing",
                                "database_id": "db",
                            }
                        ],
                        "verification": {
                            "status": "response_recorded",
                            "target": "notion",
                            "checked": 1,
                            "passed": 1,
                            "failed": 0,
                            "reason": "remote_readback_not_configured",
                        },
                    },
                },
                "state_update": {
                    "applied": True,
                    "timeline_added": 1,
                    "memory_update_count": 3,
                    "next_chapter_index": 2,
                },
                "snapshot_builder": {
                    "audit": {
                        "item_count": 2,
                        "applied_count": 2,
                        "skipped_count": 0,
                        "deduplicated_count": 0,
                        "applied_type_counts": [{"type": "location", "count": 1}, {"type": "timeline_event", "count": 1}],
                        "skipped_type_counts": [],
                        "skipped_blocking_count": 0,
                        "applied_items": [
                            {
                                "index": 0,
                                "type": "location",
                                "name": "Safehouse",
                                "memory_id": "manual:location:safehouse",
                                "operation": "upsert_location",
                                "target": "world_state.locations.Safehouse",
                                "source_mapping": {
                                    "index": 0,
                                    "source": "notion-export",
                                    "memory_id": "manual:location:safehouse",
                                    "type": "location",
                                    "name": "Safehouse",
                                    "page_id": "page-safehouse",
                                    "page_index": 0,
                                },
                            },
                            {
                                "index": 1,
                                "type": "timeline_event",
                                "name": "chapter_1_summary",
                                "memory_id": "chapter_1:timeline_event:summary",
                                "operation": "append_timeline_event",
                                "target": "timeline",
                            },
                        ],
                        "skipped_items": [],
                        "skipped_reason_counts": [],
                        "skipped_severity_counts": [],
                    }
                },
                "repair_attempts": 0,
                "trace": [{"action": "validate", "status": "completed", "validation_ok": True}],
                "chapter": {"artifact": {"path": str(chapter_path)}},
            },
        )
        self._write_run(
            run_dir / "chapter_2_20260101T000001000000Z.json",
            {
                "id": "chapter_2_20260101T000001000000Z",
                "status": "rejected",
                "committed": False,
                "chapter_index": 2,
                "started_at": "2026-01-01T00:00:01+00:00",
                "finished_at": "2026-01-01T00:00:02+00:00",
                "decision": {"goal": "recover_from_rejected_run"},
                "workflow": ["generate_chapter", "validate", "repair_if_needed"],
                "workflow_plan": {
                    "recovery": True,
                    "steps": [
                        {"action": "generate_chapter", "mode": "required"},
                        {"action": "validate", "mode": "required"},
                        {"action": "repair_if_needed", "mode": "conditional"},
                    ],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 2,
                },
                "director": {
                    "mode": "model",
                    "model": "gpt-test",
                    "status": "completed",
                    "duration_ms": 5,
                    "model_call": {
                        "provider": "openai",
                        "stage": "director_decision",
                        "model": "gpt-test",
                        "cause_type": "TimeoutError",
                        "message": "timeout",
                    },
                },
                "validation": {
                    "ok": False,
                    "problem_codes": ["missing_conflict_marker"],
                    "problem_count": 1,
                    "blocking_problem_count": 1,
                    "warning_count": 0,
                    "severity_counts": [{"severity": "high", "count": 1}],
                    "deterministic_repair_count": 1,
                    "manual_review_count": 0,
                    "repair_action_counts": [{"action": "add_conflict_signal", "count": 1}],
                    "problem_evidence": [
                        {
                            "code": "missing_conflict_marker",
                            "validator": "logic",
                            "severity": "high",
                            "blocking": True,
                            "repair_action": "add_conflict_signal",
                            "evidence": [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
                        }
                    ],
                },
                "analysis": {"summary": "", "conflict_count": 0, "event_count": 0, "world_change_count": 0},
                "repair_attempts": 1,
                "trace": [
                    {
                        "action": "repair_if_needed",
                        "status": "completed",
                        "plan_step_index": 4,
                        "plan_step_mode": "conditional",
                        "plan_failure_policy": "fail_run",
                        "model_stage": "scene_repair",
                        "model_provider": "openai",
                        "model_name": "gpt-test",
                        "model_invocation": "model",
                        "validation_ok": False,
                        "problem_count": 1,
                        "skipped": False,
                        "repair_plan": {
                            "actions": ["add_conflict_signal"],
                            "risk_level": "high",
                            "repair_budget": 2,
                            "manual_review_count": 0,
                            "recovery": {
                                "available": True,
                                "source_run_id": "chapter_1_20260101T000000000000Z",
                                "source_status": "rejected",
                                "source_problem_codes": ["missing_conflict_marker"],
                                "repeated_problem_codes": ["missing_conflict_marker"],
                                "unresolved_problem_codes": ["missing_conflict_marker"],
                                "new_problem_codes": ["no_known_location"],
                                "skipped_checks": ["continuity", "spatial"],
                                "previous_repair_attempts": 1,
                                "previous_repair_risk_level": "high",
                                "previous_manual_review_count": 0,
                                "repair_stalled": True,
                                "repair_introduced_new_problems": True,
                                "repair_budget_exhausted": True,
                                "failure_modes": [
                                    "previous_problem_repeated",
                                    "previous_repair_stalled",
                                    "previous_repair_introduced_new_problems",
                                    "previous_validation_skipped",
                                    "previous_repair_budget_exhausted",
                                ],
                            },
                            "steps": [
                                {
                                    "validator": "logic",
                                    "evidence": [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
                                }
                            ],
                        },
                        "repair_deltas": [
                            {
                                "attempt": 1,
                                "before_problem_count": 1,
                                "after_problem_count": 0,
                                "resolved_problem_codes": ["missing_conflict_marker"],
                                "new_problem_codes": [],
                                "remaining_problem_codes": [],
                            }
                        ],
                        "model_call": {
                            "provider": "openai",
                            "stage": "repair",
                            "model": "gpt-test",
                            "cause_type": "TimeoutError",
                            "message": "timeout",
                        },
                    }
                ],
                "error": {"type": "ValidationError", "message": "not enough conflict"},
            },
        )
        (run_dir / "chapter_3_bad.json").write_text("{bad json", encoding="utf-8")
        session_dir = run_dir / "loop_sessions"
        session_dir.mkdir()
        self._write_loop_session(
            session_dir / "loop_20260101T000002000000Z.json",
            {
                "id": "loop_20260101T000002000000Z",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:02+00:00",
                "requested_steps": 2,
                "completed_steps": 2,
                "stopped_reason": "max_steps",
                "persist": True,
                "stop_on_rejection": True,
                "committed_count": 1,
                "rejected_count": 1,
                "failed_count": 0,
                "first_chapter_index": 1,
                "last_chapter_index": 2,
                "last_run_id": "chapter_2_20260101T000001000000Z",
                "recovery_links": [
                    {
                        "run_id": "chapter_2_20260101T000001000000Z",
                        "run_status": "rejected",
                        "director_goal": "recover_from_rejected_run",
                        "source_run_id": "chapter_1_20260101T000000000000Z",
                        "source_status": "committed",
                        "source_chapter_index": 1,
                        "source_problem_codes": [],
                        "repair_stalled": False,
                        "repair_introduced_new_problems": False,
                        "repair_risk_level": None,
                        "repair_budget_exhausted": False,
                    }
                ],
                "runs": [
                    {
                        "id": "chapter_1_20260101T000000000000Z",
                        "status": "committed",
                        "committed": True,
                        "chapter_index": 1,
                        "problem_codes": [],
                        "requested_focus": ["continuity", "spatial", "logic"],
                        "executed_checks": ["continuity", "spatial", "logic"],
                        "skipped_checks": [],
                        "workflow_actions": ["generate_chapter", "polish", "validate", "repair_if_needed"],
                        "trace_actions": ["generate_chapter", "polish", "validate", "repair_if_needed"],
                        "trace_plan_aligned": True,
                        "repair_attempts": 0,
                    },
                    {
                        "id": "chapter_2_20260101T000001000000Z",
                        "status": "rejected",
                        "committed": False,
                        "chapter_index": 2,
                        "problem_codes": ["missing_conflict_marker"],
                        "problem_evidence": [
                            {
                                "code": "missing_conflict_marker",
                                "validator": "logic",
                                "severity": "high",
                                "blocking": True,
                                "repair_action": "add_conflict_signal",
                                "evidence": [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
                            }
                        ],
                        "requested_focus": ["logic"],
                        "executed_checks": ["logic"],
                        "skipped_checks": ["continuity", "spatial"],
                        "workflow_actions": ["generate_chapter", "validate", "repair_if_needed"],
                        "trace_actions": ["generate_chapter", "validate", "repair_if_needed"],
                        "trace_plan_aligned": True,
                        "repair_attempts": 1,
                        "repair_evidence": [
                            {
                                "code": "missing_conflict_marker",
                                "validator": "logic",
                                "action": "add_conflict_signal",
                                "evidence": [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
                            }
                        ],
                    },
                ],
            },
        )

        report = build_run_report(run_dir, limit=2)

        self.assertEqual(3, report["total"])
        self.assertEqual(2, report["loaded"])
        self.assertEqual(1, report["loop_session_total"])
        self.assertEqual(1, report["loop_session_loaded"])
        self.assertEqual(1, len(report["skipped"]))
        self.assertEqual([], report["skipped_loop_sessions"])
        self.assertEqual({"committed": 1, "rejected": 1}, report["status_counts"])
        self.assertEqual({"missing_conflict_marker": 1}, report["problem_counts"])
        self.assertEqual(2, len(report["runs"]))
        self.assertEqual("chapter_2_20260101T000001000000Z", report["latest"]["id"])
        self.assertTrue(report["latest"]["workflow_plan"]["recovery"])
        self.assertEqual(3, report["latest"]["workflow_plan"]["step_count"])
        self.assertEqual(2, report["latest"]["workflow_plan"]["required_step_count"])
        self.assertEqual(0, report["latest"]["workflow_plan"]["optional_step_count"])
        self.assertEqual(1, report["latest"]["workflow_plan"]["conditional_step_count"])
        self.assertEqual(["logic"], report["latest"]["workflow_plan"]["validation_focus"])
        self.assertEqual("model", report["latest"]["director"]["mode"])
        self.assertEqual("director_decision", report["latest"]["director"]["model_call"]["stage"])
        self.assertEqual("ValidationError", report["latest"]["error"]["type"])
        self.assertEqual(1, report["latest"]["validation"]["blocking_problem_count"])
        self.assertEqual([{"severity": "high", "count": 1}], report["latest"]["validation"]["severity_counts"])
        self.assertEqual(1, report["latest"]["validation"]["deterministic_repair_count"])
        self.assertEqual(0, report["latest"]["validation"]["manual_review_count"])
        self.assertEqual(
            [{"action": "add_conflict_signal", "count": 1}],
            report["latest"]["validation"]["repair_action_counts"],
        )
        self.assertEqual(["logic"], report["latest"]["validation"]["requested_focus"])
        self.assertEqual(["logic"], report["latest"]["validation"]["executed_checks"])
        self.assertEqual(["continuity", "spatial"], report["latest"]["validation"]["skipped_checks"])
        self.assertEqual(
            [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
            report["latest"]["validation"]["problem_evidence"][0]["evidence"],
        )
        self.assertEqual(4, report["latest"]["trace"][0]["plan_step_index"])
        self.assertEqual("conditional", report["latest"]["trace"][0]["plan_step_mode"])
        self.assertEqual("fail_run", report["latest"]["trace"][0]["plan_failure_policy"])
        self.assertEqual("scene_repair", report["latest"]["trace"][0]["model_stage"])
        self.assertEqual("openai", report["latest"]["trace"][0]["model_provider"])
        self.assertEqual("gpt-test", report["latest"]["trace"][0]["model_name"])
        self.assertEqual("model", report["latest"]["trace"][0]["model_invocation"])
        self.assertEqual(["add_conflict_signal"], report["latest"]["trace"][0]["repair_actions"])
        self.assertEqual(["logic"], report["latest"]["trace"][0]["repair_validators"])
        self.assertEqual(
            [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
            report["latest"]["trace"][0]["repair_evidence"][0]["evidence"],
        )
        self.assertEqual("high", report["latest"]["trace"][0]["repair_risk_level"])
        self.assertEqual(2, report["latest"]["trace"][0]["repair_budget"])
        self.assertEqual(0, report["latest"]["trace"][0]["repair_manual_review_count"])
        self.assertIn("previous_repair_stalled", report["latest"]["trace"][0]["repair_failure_modes"])
        self.assertEqual(["missing_conflict_marker"], report["latest"]["trace"][0]["repair_repeated_problem_codes"])
        self.assertEqual(["missing_conflict_marker"], report["latest"]["trace"][0]["repair_unresolved_problem_codes"])
        self.assertEqual(["no_known_location"], report["latest"]["trace"][0]["repair_new_problem_codes"])
        self.assertFalse(report["latest"]["trace"][0]["skipped"])
        self.assertIsNone(report["latest"]["trace"][0]["skip_reason"])
        self.assertEqual(["missing_conflict_marker"], report["latest"]["trace"][0]["repair_deltas"][0]["resolved_problem_codes"])
        self.assertEqual("openai", report["latest"]["trace"][0]["model_call"]["provider"])
        committed = [run for run in report["runs"] if run["status"] == "committed"][0]
        self.assertEqual(2, committed["memory"]["source_mapping_count"])
        self.assertEqual([{"source": "notion-export", "count": 2}], committed["memory"]["source_mapping_sources"])
        self.assertEqual(1, committed["memory"]["file_mapping_count"])
        self.assertEqual(0, committed["memory"]["line_mapping_count"])
        self.assertEqual(2, committed["memory"]["notion_page_mapping_count"])
        self.assertEqual(2, committed["memory"]["notion_page_url_count"])
        self.assertEqual(1, committed["memory"]["writeback"]["written"])
        self.assertEqual(1, committed["memory"]["writeback"]["skipped"])
        self.assertEqual(
            [{"status": "skipped_duplicate", "count": 1}, {"status": "written", "count": 1}],
            committed["memory"]["writeback"]["status_counts"],
        )
        self.assertEqual(
            [{"type": "location", "count": 1}, {"type": "timeline_event", "count": 1}],
            committed["memory"]["writeback"]["type_counts"],
        )
        self.assertTrue(committed["artifacts"]["chapter"]["exists"])
        self.assertEqual("page-1", committed["memory"]["writeback"]["item_mappings"][0]["page_id"])
        self.assertEqual("https://notion.test/page-1", committed["memory"]["writeback"]["item_mappings"][0]["page_url"])
        self.assertEqual(["Data", "Memory ID", "Name", "Type"], committed["memory"]["writeback"]["item_mappings"][0]["property_names"])
        self.assertEqual("response_recorded", committed["memory"]["writeback"]["verification"]["status"])
        self.assertEqual("remote_readback_not_configured", committed["memory"]["writeback"]["verification"]["reason"])
        self.assertEqual(
            "chapter_1:timeline_event:summary",
            committed["memory"]["writeback"]["item_mappings"][0]["memory_id"],
        )
        self.assertEqual(2, committed["state_builder"]["applied_count"])
        self.assertEqual(
            [{"type": "location", "count": 1}, {"type": "timeline_event", "count": 1}],
            committed["state_builder"]["applied_type_counts"],
        )
        self.assertEqual([], committed["state_builder"]["skipped_type_counts"])
        self.assertEqual(0, committed["state_builder"]["skipped_blocking_count"])
        self.assertEqual(1, committed["state_builder"]["applied_source_mapping_count"])
        self.assertEqual(0, committed["state_builder"]["skipped_source_mapping_count"])
        self.assertEqual([], committed["state_builder"]["skipped_reason_counts"])
        self.assertTrue(committed["state_update"]["applied"])
        self.assertEqual(3, committed["state_update"]["memory_update_count"])
        self.assertEqual(1, len(report["loop_sessions"]))
        self.assertEqual("loop_20260101T000002000000Z", report["latest_loop_session"]["id"])
        self.assertEqual(2, report["latest_loop_session"]["completed_steps"])
        self.assertEqual(1, report["latest_loop_session"]["recovery_link_count"])
        self.assertEqual("chapter_1_20260101T000000000000Z", report["latest_loop_session"]["recovery_links"][0]["source_run_id"])
        self.assertEqual(["chapter_1_20260101T000000000000Z", "chapter_2_20260101T000001000000Z"], report["latest_loop_session"]["run_ids"])
        self.assertEqual(["logic"], report["latest_loop_session"]["run_summaries"][1]["executed_checks"])
        self.assertEqual(["continuity", "spatial"], report["latest_loop_session"]["run_summaries"][1]["skipped_checks"])
        self.assertEqual(
            [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
            report["latest_loop_session"]["run_summaries"][1]["problem_evidence"][0]["evidence"],
        )
        self.assertEqual(
            [{"kind": "missing_any_marker", "value": "conflict, danger, choice"}],
            report["latest_loop_session"]["run_summaries"][1]["repair_evidence"][0]["evidence"],
        )
        self.assertEqual(
            ["generate_chapter", "validate", "repair_if_needed"],
            report["latest_loop_session"]["run_summaries"][1]["workflow_actions"],
        )
        self.assertEqual(
            ["generate_chapter", "validate", "repair_if_needed"],
            report["latest_loop_session"]["run_summaries"][1]["trace_actions"],
        )
        self.assertTrue(report["latest_loop_session"]["run_summaries"][1]["trace_plan_aligned"])
        self.assertTrue(report["latest_loop_session"]["artifact"]["exists"])
        self.assertEqual(str(session_dir / "loop_20260101T000002000000Z.json"), report["latest_loop_session"]["artifact"]["path"])
        self.assertEqual("json", report["latest_loop_session"]["artifact"]["format"])

    def test_build_run_report_limit_zero_returns_counts_only(self) -> None:
        tmp_path = self._case_dir("limit_zero")
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        self._write_run(
            run_dir / "chapter_1_20260101T000000000000Z.json",
            {
                "id": "chapter_1_20260101T000000000000Z",
                "status": "committed",
                "committed": True,
                "chapter_index": 1,
                "decision": {},
                "validation": {"problem_codes": []},
            },
        )

        report = build_run_report(run_dir, limit=0)

        self.assertEqual(1, report["loaded"])
        self.assertEqual(0, report["loop_session_loaded"])
        self.assertIsNone(report["latest"])
        self.assertIsNone(report["latest_loop_session"])
        self.assertEqual([], report["runs"])
        self.assertEqual([], report["loop_sessions"])
        self.assertEqual({"committed": 1}, report["status_counts"])

    def test_build_run_report_skips_unreportable_run_record(self) -> None:
        tmp_path = self._case_dir("bad_run_contract")
        run_dir = tmp_path / "runs"
        run_dir.mkdir()
        (run_dir / "chapter_1_missing_status.json").write_text(
            json.dumps(
                {
                    "run": {
                        "id": "chapter_1_missing_status",
                        "committed": True,
                        "chapter_index": 1,
                    }
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "chapter_2_bad_envelope.json").write_text(
            json.dumps(
                {
                    "run": {
                        "id": "chapter_2_bad_envelope",
                        "status": "committed",
                        "committed": True,
                        "chapter_index": 2,
                    },
                    "unexpected": True,
                }
            ),
            encoding="utf-8",
        )

        report = build_run_report(run_dir)

        self.assertEqual(2, report["total"])
        self.assertEqual(0, report["loaded"])
        self.assertEqual(2, len(report["skipped"]))
        errors = " ".join(item["error"] for item in report["skipped"])
        self.assertIn("run_record.schema.json", errors)
        self.assertIn("run_result.schema.json", errors)

    def test_build_run_report_summarizes_failed_loop_session_error(self) -> None:
        tmp_path = self._case_dir("failed_loop_session")
        run_dir = tmp_path / "runs"
        session_dir = run_dir / "loop_sessions"
        session_dir.mkdir(parents=True)
        self._write_loop_session(
            session_dir / "loop_20260101T000000000000Z.json",
            {
                "id": "loop_20260101T000000000000Z",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
                "requested_steps": 2,
                "completed_steps": 1,
                "stopped_reason": "failed",
                "persist": True,
                "stop_on_rejection": True,
                "committed_count": 0,
                "rejected_count": 0,
                "failed_count": 1,
                "first_chapter_index": 2,
                "last_chapter_index": 2,
                "last_run_id": "chapter_2_failed",
                "recovery_links": [],
                "runs": [
                    {
                        "id": "chapter_2_failed",
                        "status": "failed",
                        "committed": False,
                        "chapter_index": 2,
                        "problem_codes": ["execution_error"],
                        "repair_attempts": 0,
                    }
                ],
                "error": {
                    "type": "ValueError",
                    "message": "generation failed",
                },
            },
        )

        report = build_run_report(run_dir)

        self.assertEqual(1, report["loop_session_loaded"])
        self.assertEqual("failed", report["latest_loop_session"]["stopped_reason"])
        self.assertEqual("ValueError", report["latest_loop_session"]["error"]["type"])
        self.assertTrue(report["latest_loop_session"]["artifact"]["exists"])
        self.assertEqual(str(session_dir / "loop_20260101T000000000000Z.json"), report["latest_loop_session"]["artifact"]["path"])

    def test_build_run_report_rejects_file_run_dir(self) -> None:
        tmp_path = self._case_dir("file_run_dir")
        run_dir = tmp_path / "runs"
        run_dir.write_text("not a directory", encoding="utf-8")

        report = build_run_report(run_dir)

        self.assertEqual(0, report["loaded"])
        self.assertEqual(0, report["loop_session_loaded"])
        self.assertEqual(1, len(report["skipped"]))
        self.assertIn("not a directory", report["skipped"][0]["error"])

    def _write_run(self, path: Path, run: dict) -> None:
        run = self._complete_run(run)
        path.write_text(json.dumps({"run": run}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_loop_session(self, path: Path, session: dict) -> None:
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    def _complete_run(self, override: dict) -> dict:
        chapter_index = override.get("chapter_index", 1)
        run_id = override.get("id", f"chapter_{chapter_index}_test")
        default = {
            "id": run_id,
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "status": "committed",
            "committed": True,
            "chapter_index": chapter_index,
            "snapshot": {
                "base_chapter_index": chapter_index,
                "runtime_chapter_index": chapter_index,
                "next_chapter_index": chapter_index + 1,
            },
            "memory": {"source": "test", "status": "ready", "item_count": 0},
            "recovery_context": {
                "available": False,
                "source_run_id": None,
                "source_status": None,
                "source_committed": None,
                "source_chapter_index": None,
                "source_goal": None,
                "problem_codes": [],
                "problem_count": 0,
                "blocking_problem_count": None,
                "severity_counts": [],
                "repair_attempts": 0,
                "repair_effective": None,
                "repair_stalled": False,
                "repair_introduced_new_problems": False,
                "repair_risk_level": None,
                "repair_budget": None,
                "repair_manual_review_count": 0,
                "repair_budget_exhausted": False,
            },
            "snapshot_builder": {
                "chars": 0,
                "preview": "",
                "audit": {
                    "source": "test",
                    "status": "ready",
                    "item_count": 0,
                    "applied_count": 0,
                    "skipped_count": 0,
                    "deduplicated_count": 0,
                    "applied_type_counts": [],
                    "skipped_type_counts": [],
                    "skipped_reason_counts": [],
                    "skipped_severity_counts": [],
                    "skipped_blocking_count": 0,
                    "applied_items": [],
                    "skipped_items": [],
                },
            },
            "director": {
                "mode": "rule",
                "source": "core.director.director.decide_next_step",
                "model": None,
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
                "duration_ms": 0,
            },
            "decision": {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
            },
            "workflow": ["generate_chapter", "validate"],
            "workflow_plan": None,
            "input_pack": {"chars": 0, "preview": ""},
            "chapter": {"chars": 0},
            "validation": {
                "ok": True,
                "problem_codes": [],
                "problem_count": 0,
                "blocking_problem_count": 0,
                "warning_count": 0,
                "severity_counts": [],
                "requested_focus": ["logic"],
                "executed_checks": ["logic"],
                "skipped_checks": ["continuity", "spatial"],
            },
            "analysis": {
                "validation_ok": True,
                "conflict_count": 0,
                "event_count": 0,
                "world_change_count": 0,
                "summary": "",
            },
            "state_update": {
                "applied": True,
                "chapter_index": chapter_index,
                "next_chapter_index": chapter_index + 1,
                "timeline_added": 0,
                "character_update_count": 0,
                "location_update_count": 0,
                "world_change_count": 0,
                "memory_update_count": 0,
                "memory_update_types": [],
                "analysis_validation_ok": True,
            },
            "repair_attempts": 0,
            "trace": [],
        }
        run = self._deep_merge(default, override)
        run["snapshot_builder"]["audit"]["applied_items"] = [
            self._normalize_audit_item(item) for item in run["snapshot_builder"]["audit"].get("applied_items", [])
        ]
        run["snapshot_builder"]["audit"]["skipped_items"] = [
            self._normalize_audit_item(item) for item in run["snapshot_builder"]["audit"].get("skipped_items", [])
        ]
        if isinstance(run.get("workflow_plan"), dict):
            run["workflow_plan"] = self._complete_workflow_plan(run)
        run["trace"] = [self._complete_trace_event(event) for event in run.get("trace", [])]
        return run

    def _complete_workflow_plan(self, run: dict) -> dict:
        plan = run["workflow_plan"]
        actions = list(run.get("workflow") or plan.get("actions") or ["generate_chapter", "validate"])
        return build_workflow_plan(
            {
                "goal": run["decision"]["goal"],
                "actions": actions,
                "validation_focus": run["decision"]["validation_focus"],
                "max_repair_attempts": run["decision"]["max_repair_attempts"],
            }
        )

    def _complete_trace_event(self, event: dict) -> dict:
        completed = self._deep_merge(
            {
                "action": "validate",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
                "chapter_chars": 0,
                "repair_attempts": 0,
            },
            event,
        )
        if isinstance(completed.get("repair_plan"), dict):
            completed["repair_plan"] = self._complete_repair_plan(completed["repair_plan"])
        if isinstance(completed.get("repair_deltas"), list):
            completed["repair_deltas"] = [
                self._deep_merge(
                    {
                        "attempt": 1,
                        "before_ok": False,
                        "after_ok": True,
                        "before_problem_count": 1,
                        "after_problem_count": 0,
                        "before_problem_codes": ["missing_conflict_marker"],
                        "after_problem_codes": [],
                        "resolved_problem_codes": ["missing_conflict_marker"],
                        "new_problem_codes": [],
                        "remaining_problem_codes": [],
                    },
                    delta,
                )
                for delta in completed["repair_deltas"]
                if isinstance(delta, dict)
            ]
        return completed

    def _complete_repair_plan(self, plan: dict) -> dict:
        completed = self._deep_merge(
            {
                "problem_count": 1,
                "blocking_problem_count": 1,
                "warning_count": 0,
                "severity_counts": [{"severity": "high", "count": 1}],
                "risk_level": "high",
                "repair_budget": 1,
                "attempt": 1,
                "deterministic_step_count": 1,
                "manual_review_count": 0,
                "actions": ["add_conflict_signal"],
                "recovery": _empty_repair_recovery(),
                "steps": [],
            },
            plan,
        )
        completed["steps"] = [
            self._deep_merge(
                {
                    "index": index,
                    "code": "missing_conflict_marker",
                    "message": "Missing conflict signal.",
                    "validator": "logic",
                    "severity": "high",
                    "blocking": True,
                    "repair_hint": "Add explicit danger, choice, threat, secret, cost, or conflict.",
                    "action": "add_conflict_signal",
                    "priority": 30,
                    "strategy": "Add explicit danger, choice, threat, secret, cost, or conflict.",
                    "parameters": {},
                },
                step,
            )
            for index, step in enumerate(completed.get("steps", []), start=1)
            if isinstance(step, dict)
        ]
        return completed

    def _normalize_audit_item(self, item: dict) -> dict:
        item = dict(item)
        if "id" not in item and "memory_id" in item:
            item["id"] = item["memory_id"]
        item.pop("memory_id", None)
        return item

    def _deep_merge(self, left: dict, right: dict) -> dict:
        result = dict(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result


def _empty_repair_recovery() -> dict:
    return {
        "available": False,
        "source_run_id": None,
        "source_status": None,
        "source_problem_codes": [],
        "repeated_problem_codes": [],
        "unresolved_problem_codes": [],
        "new_problem_codes": [],
        "skipped_checks": [],
        "previous_repair_attempts": 0,
        "previous_repair_risk_level": None,
        "previous_manual_review_count": 0,
        "repair_stalled": False,
        "repair_introduced_new_problems": False,
        "repair_budget_exhausted": False,
        "failure_modes": [],
    }


if __name__ == "__main__":
    unittest.main()
