from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.run_record import load_latest_run_summary


class RunRecordTest(unittest.TestCase):
    def test_load_latest_run_summary_accepts_utf8_bom(self) -> None:
        run_dir = Path.cwd() / ".tmp" / "test_run_record" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        payload = {
            "run": {
                "id": "chapter_1_20260101T000000000000Z",
                "status": "rejected",
                "committed": False,
                "chapter_index": 1,
                "decision": {"goal": "continue_existing_arc"},
                "workflow": ["generate_chapter", "validate"],
                "validation": {"problem_codes": ["missing_conflict_marker"]},
                "repair_attempts": 0,
                "director": {"mode": "rule", "status": "completed"},
            }
        }
        (run_dir / "chapter_1_20260101T000000000000Z.json").write_text(
            "\ufeff" + json.dumps({"run": self._complete_run(payload["run"])}),
            encoding="utf-8",
        )

        summary = load_latest_run_summary(run_dir)

        self.assertIsNotNone(summary)
        self.assertEqual("rejected", summary["status"])
        self.assertEqual(1, summary["chapter_index"])
        self.assertEqual("continue_existing_arc", summary["goal"])
        self.assertEqual(["generate_chapter", "validate"], summary["workflow"])
        self.assertEqual(["missing_conflict_marker"], summary["problem_codes"])
        self.assertEqual(["logic"], summary["requested_focus"])
        self.assertEqual(["logic"], summary["executed_checks"])
        self.assertEqual(["continuity", "spatial"], summary["skipped_checks"])
        self.assertEqual("rule", summary["director_mode"])

    def test_load_latest_run_summary_includes_failure_error_context(self) -> None:
        run_dir = Path.cwd() / ".tmp" / "test_run_record" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        payload = {
            "run": {
                "id": "chapter_2_20260101T000000000000Z",
                "status": "failed",
                "committed": False,
                "chapter_index": 2,
                "decision": {
                    "goal": "bad_workflow",
                    "actions": ["generate_chapter", "repair_if_needed", "validate"],
                },
                "workflow": [],
                "validation": {
                    "problem_codes": ["workflow_error"],
                    "problem_count": 1,
                    "blocking_problem_count": 1,
                    "warning_count": 0,
                    "severity_counts": [{"severity": "critical", "count": 1}],
                },
                "repair_attempts": 0,
                "director": {"mode": "model", "status": "completed"},
                "error": {
                    "type": "WorkflowError",
                    "message": "repair_if_needed requires validate before it",
                },
            }
        }
        (run_dir / "chapter_2_20260101T000000000000Z.json").write_text(
            json.dumps({"run": self._complete_run(payload["run"])}),
            encoding="utf-8",
        )

        summary = load_latest_run_summary(run_dir)

        self.assertIsNotNone(summary)
        self.assertEqual("failed", summary["status"])
        self.assertEqual(["workflow_error"], summary["problem_codes"])
        self.assertEqual(1, summary["problem_count"])
        self.assertEqual(1, summary["blocking_problem_count"])
        self.assertEqual(0, summary["warning_count"])
        self.assertEqual([{"severity": "critical", "count": 1}], summary["severity_counts"])
        self.assertEqual("WorkflowError", summary["error_type"])
        self.assertEqual("repair_if_needed requires validate before it", summary["error_message"])
        self.assertEqual("model", summary["director_mode"])

    def test_load_latest_run_summary_includes_director_model_call_context(self) -> None:
        run_dir = Path.cwd() / ".tmp" / "test_run_record" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        payload = {
            "run": {
                "id": "chapter_3_20260101T000000000000Z",
                "status": "failed",
                "committed": False,
                "chapter_index": 3,
                "decision": {"goal": None},
                "workflow": [],
                "validation": {"problem_codes": ["director_error"], "problem_count": 1},
                "repair_attempts": 0,
                "director": {
                    "mode": "model",
                    "status": "failed",
                    "model_call": {
                        "provider": "openai",
                        "stage": "director_decision",
                        "model": "gpt-test",
                        "cause_type": "TimeoutError",
                        "message": "timeout",
                    },
                },
                "error": {
                    "type": "ModelCallError",
                    "message": "OpenAI director call failed: timeout",
                },
            }
        }
        (run_dir / "chapter_3_20260101T000000000000Z.json").write_text(
            json.dumps({"run": self._complete_run(payload["run"])}),
            encoding="utf-8",
        )

        summary = load_latest_run_summary(run_dir)

        self.assertIsNotNone(summary)
        self.assertEqual("failed", summary["status"])
        self.assertEqual("model", summary["director_mode"])
        self.assertEqual("director_decision", summary["director_model_call"]["stage"])
        self.assertEqual("TimeoutError", summary["director_model_call"]["cause_type"])

    def test_load_latest_run_summary_includes_repair_delta_context(self) -> None:
        run_dir = Path.cwd() / ".tmp" / "test_run_record" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        payload = {
            "run": {
                "id": "chapter_4_20260101T000000000000Z",
                "status": "rejected",
                "committed": False,
                "chapter_index": 4,
                "decision": {"goal": "recover"},
                "workflow": ["generate_chapter", "validate", "repair_if_needed"],
                "validation": {
                    "problem_codes": ["no_known_location"],
                    "problem_count": 1,
                    "problem_evidence": [
                        {
                            "code": "no_known_location",
                            "validator": "spatial",
                            "severity": "critical",
                            "blocking": True,
                            "repair_action": "flag_unknown_location",
                            "evidence": [{"kind": "unknown_location", "value": "empty corridor"}],
                        }
                    ],
                },
                "repair_attempts": 1,
                "trace": [
                    {
                        "action": "repair_if_needed",
                        "repair_plan": {
                            "risk_level": "critical",
                            "repair_budget": 1,
                            "attempt": 1,
                            "deterministic_step_count": 0,
                            "manual_review_count": 1,
                            "steps": [
                                {
                                    "index": 1,
                                    "code": "no_known_location",
                                    "message": "Character location is unknown.",
                                    "validator": "spatial",
                                    "severity": "critical",
                                    "blocking": True,
                                    "repair_hint": "Anchor the scene to a known location.",
                                    "evidence": [{"kind": "unknown_location", "value": "empty corridor"}],
                                    "action": "flag_unknown_location",
                                    "priority": 90,
                                    "strategy": "Flag unknown location for manual review.",
                                    "parameters": {"location": "empty corridor"},
                                }
                            ],
                        },
                        "repair_deltas": [
                            {
                                "attempt": 1,
                                "before_problem_count": 1,
                                "after_problem_count": 1,
                                "resolved_problem_codes": ["missing_conflict_marker"],
                                "new_problem_codes": ["no_known_location"],
                                "remaining_problem_codes": [],
                            }
                        ],
                    }
                ],
                "director": {"mode": "rule", "status": "completed"},
            }
        }
        (run_dir / "chapter_4_20260101T000000000000Z.json").write_text(
            json.dumps({"run": self._complete_run(payload["run"])}),
            encoding="utf-8",
        )

        summary = load_latest_run_summary(run_dir)

        self.assertIsNotNone(summary)
        self.assertFalse(summary["repair_effective"])
        self.assertTrue(summary["repair_stalled"])
        self.assertTrue(summary["repair_introduced_new_problems"])
        self.assertEqual("critical", summary["repair_plan"]["risk_level"])
        self.assertEqual(1, summary["repair_plan"]["repair_budget"])
        self.assertEqual(1, summary["repair_plan"]["manual_review_count"])
        self.assertEqual("no_known_location", summary["problem_evidence"][0]["code"])
        self.assertEqual("no_known_location", summary["repair_evidence"][0]["code"])
        self.assertEqual([{"kind": "unknown_location", "value": "empty corridor"}], summary["repair_evidence"][0]["evidence"])
        self.assertEqual(["no_known_location"], summary["repair_deltas"][0]["new_problem_codes"])

    def test_load_latest_run_summary_skips_invalid_run_result(self) -> None:
        run_dir = Path.cwd() / ".tmp" / "test_run_record" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        (run_dir / "chapter_1_invalid.json").write_text(
            json.dumps({"run": {"id": "chapter_1_invalid", "status": "rejected"}}),
            encoding="utf-8",
        )

        self.assertIsNone(load_latest_run_summary(run_dir))

    def test_load_latest_run_summary_skips_semantically_invalid_workflow_plan(self) -> None:
        run_dir = Path.cwd() / ".tmp" / "test_run_record" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        run = self._complete_run(
            {
                "workflow": ["generate_chapter", "validate"],
                "workflow_plan": {
                    "goal": "continue_existing_arc",
                    "actions": ["generate_chapter", "validate"],
                    "steps": [
                        {
                            "index": 1,
                            "action": "generate_chapter",
                            "requires": [],
                            "produces": ["chapter"],
                            "purpose": "Generate draft chapter prose from the input pack.",
                            "mode": "required",
                            "skippable": False,
                            "skip_condition": None,
                            "failure_policy": "fail_run",
                        },
                        {
                            "index": 4,
                            "action": "validate",
                            "requires": ["chapter"],
                            "produces": ["validation"],
                            "purpose": "Check continuity, spatial, and logic constraints selected by the Director.",
                            "mode": "required",
                            "skippable": False,
                            "skip_condition": None,
                            "failure_policy": "fail_run",
                        },
                    ],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 0,
                    "recovery": False,
                },
            }
        )
        (run_dir / "chapter_1_invalid_plan.json").write_text(json.dumps({"run": run}), encoding="utf-8")

        self.assertIsNone(load_latest_run_summary(run_dir))

    def _complete_run(self, override: dict) -> dict:
        chapter_index = override.get("chapter_index", 1)
        status = override.get("status", "committed")
        committed = bool(override.get("committed", status == "committed"))
        default = {
            "id": override.get("id", f"chapter_{chapter_index}_test"),
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "status": status,
            "committed": committed,
            "chapter_index": chapter_index,
            "snapshot": {
                "base_chapter_index": chapter_index,
                "runtime_chapter_index": chapter_index,
                "next_chapter_index": chapter_index + 1 if committed else chapter_index,
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
            "snapshot_builder": {"chars": 0, "preview": ""},
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
                "ok": status == "committed",
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
                "validation_ok": status == "committed",
                "conflict_count": 0,
                "event_count": 0,
                "world_change_count": 0,
                "summary": "",
            },
            "state_update": {
                "applied": committed,
                "chapter_index": chapter_index,
                "next_chapter_index": chapter_index + 1 if committed else chapter_index,
                "timeline_added": 0,
                "character_update_count": 0,
                "location_update_count": 0,
                "world_change_count": 0,
                "memory_update_count": 0,
                "memory_update_types": [],
                "analysis_validation_ok": status == "committed",
            },
            "repair_attempts": 0,
            "trace": [],
        }
        run = self._deep_merge(default, override)
        run["trace"] = [self._complete_trace_event(event) for event in run.get("trace", [])]
        return run

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
                        "after_ok": False,
                        "before_problem_count": 1,
                        "after_problem_count": 1,
                        "before_problem_codes": ["missing_conflict_marker"],
                        "after_problem_codes": ["no_known_location"],
                        "resolved_problem_codes": [],
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
        return self._deep_merge(
            {
                "problem_count": 1,
                "blocking_problem_count": 1,
                "warning_count": 0,
                "severity_counts": [{"severity": "critical", "count": 1}],
                "risk_level": "critical",
                "repair_budget": 1,
                "attempt": 1,
                "deterministic_step_count": 0,
                "manual_review_count": 0,
                "actions": ["manual_review"],
                "recovery": _empty_repair_recovery(),
                "steps": [],
            },
            plan,
        )

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
