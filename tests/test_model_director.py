from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path

from api.contracts import ModelCallError
from core.director import DirectorDecisionError, ModelDirector, parse_director_output
from core.engine.executor import AgentExecutor
from core.schema import validate_schema


class ModelDirectorTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_model_director" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_parse_director_output_accepts_json_object(self) -> None:
        decision = parse_director_output(
            json.dumps(
                {
                    "chapter_index": 2,
                    "goal": "continue_existing_arc",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 1,
                    "notes": ["Use current snapshot."],
                }
            )
        )

        self.assertEqual(2, decision["chapter_index"])
        self.assertEqual(["generate_chapter", "validate"], decision["actions"])

    def test_parse_director_output_accepts_json_fence(self) -> None:
        decision = parse_director_output(
            """```json
{"chapter_index": 1, "goal": "baseline", "actions": ["generate_chapter", "validate"], "validation_focus": ["logic"], "max_repair_attempts": 0, "notes": []}
```"""
        )

        self.assertEqual("baseline", decision["goal"])

    def test_parse_director_output_rejects_invalid_json(self) -> None:
        with self.assertRaises(DirectorDecisionError):
            parse_director_output("not json")

    def test_parse_director_output_rejects_schema_violation(self) -> None:
        with self.assertRaises(DirectorDecisionError):
            parse_director_output(
                json.dumps(
                    {
                        "chapter_index": 1,
                        "goal": "bad_action",
                        "actions": ["generate_chapter", "publish"],
                        "validation_focus": ["logic"],
                        "max_repair_attempts": 1,
                        "notes": [],
                    }
                )
            )

    def test_model_director_uses_completion_and_validates_decision(self) -> None:
        seen_messages: list[list[dict[str, str]]] = []

        def completion(messages: list[dict[str, str]]) -> str:
            seen_messages.append(messages)
            return json.dumps(
                {
                    "chapter_index": 4,
                    "goal": "model_selected_goal",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["continuity", "logic"],
                    "max_repair_attempts": 0,
                    "notes": ["Model director selected a short path."],
                }
            )

        decision = ModelDirector(completion=completion)(
            {"chapter_index": 4, "world_state": {}, "characters": {}, "timeline": []},
            {"source": "test", "status": "ready", "items": []},
        )

        self.assertEqual("model_selected_goal", decision["goal"])
        self.assertIn("NovelAgent's decision layer", seen_messages[0][0]["content"])
        self.assertIn("snapshot", seen_messages[0][1]["content"])

    def test_model_director_passes_structured_last_run_context(self) -> None:
        seen_messages: list[list[dict[str, str]]] = []

        def completion(messages: list[dict[str, str]]) -> str:
            seen_messages.append(messages)
            return json.dumps(
                {
                    "chapter_index": 4,
                    "goal": "recover_with_severity_context",
                    "actions": ["generate_chapter", "validate", "repair_if_needed"],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 3,
                    "notes": ["Use blocking problem summary."],
                }
            )

        ModelDirector(completion=completion)(
            {"chapter_index": 4, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "problem_codes": ["forbidden_constraint_term"],
                    "blocking_problem_count": 1,
                    "warning_count": 0,
                    "severity_counts": [{"severity": "critical", "count": 1}],
                    "requested_focus": ["logic"],
                    "executed_checks": ["logic"],
                    "skipped_checks": ["continuity", "spatial"],
                    "repair_deltas": [
                        {
                            "attempt": 1,
                            "before_problem_count": 1,
                            "after_problem_count": 1,
                            "resolved_problem_codes": [],
                            "new_problem_codes": ["no_known_location"],
                            "remaining_problem_codes": [],
                        }
                    ],
                    "repair_plan": {
                        "risk_level": "critical",
                        "repair_budget": 2,
                        "attempt": 1,
                        "manual_review_count": 1,
                    },
                    "repair_introduced_new_problems": True,
                },
            },
        )

        payload = json.loads(seen_messages[0][1]["content"])
        last_run = payload["memory_context"]["last_run"]
        self.assertEqual(1, last_run["blocking_problem_count"])
        self.assertEqual([{"severity": "critical", "count": 1}], last_run["severity_counts"])
        self.assertEqual(["logic"], last_run["executed_checks"])
        self.assertEqual(["continuity", "spatial"], last_run["skipped_checks"])
        self.assertEqual(["no_known_location"], last_run["repair_deltas"][0]["new_problem_codes"])
        self.assertEqual("critical", last_run["repair_plan"]["risk_level"])
        self.assertEqual(1, last_run["repair_plan"]["manual_review_count"])
        self.assertTrue(last_run["repair_introduced_new_problems"])

    def test_model_director_passes_compact_snapshot_builder_audit(self) -> None:
        seen_messages: list[list[dict[str, str]]] = []

        def completion(messages: list[dict[str, str]]) -> str:
            seen_messages.append(messages)
            return json.dumps(
                {
                    "chapter_index": 4,
                    "goal": "resolve_memory_quality_risk",
                    "actions": ["generate_chapter", "validate", "repair_if_needed"],
                    "validation_focus": ["continuity", "spatial", "logic"],
                    "max_repair_attempts": 2,
                    "notes": ["Use Snapshot Builder audit."],
                }
            )

        ModelDirector(completion=completion)(
            {"chapter_index": 4, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "snapshot_builder_audit": {
                    "source": "test",
                    "status": "ready",
                    "item_count": 2,
                    "applied_count": 0,
                    "skipped_count": 2,
                    "deduplicated_count": 0,
                    "applied_type_counts": [],
                    "skipped_type_counts": [{"type": "location", "count": 2}],
                    "skipped_reason_counts": [{"reason_code": "missing_name", "count": 2}],
                    "skipped_severity_counts": [{"severity": "medium", "count": 2}],
                    "skipped_blocking_count": 0,
                    "applied_items": [
                        {
                            "source_mapping": {
                                "index": 0,
                                "source": "notion-export",
                                "memory_id": "manual:location:safehouse",
                                "type": "location",
                                "name": "Safehouse",
                                "page_id": "page-safehouse",
                                "page_url": "https://notion.test/page-safehouse",
                                "page_index": 0,
                            }
                        }
                    ],
                    "skipped_items": [
                        {
                            "reason_code": "missing_name",
                            "source_mapping": {
                                "index": 1,
                                "source": "notion-export",
                                "memory_id": "manual:location:missing",
                                "type": "location",
                                "name": None,
                                "page_id": "page-missing",
                                "page_url": "https://notion.test/page-missing",
                                "page_index": 1,
                            },
                        }
                    ],
                },
            },
        )

        payload = json.loads(seen_messages[0][1]["content"])
        audit = payload["memory_context"]["snapshot_builder_audit"]
        self.assertEqual(2, audit["skipped_count"])
        self.assertEqual(1, audit["applied_source_mapping_count"])
        self.assertEqual(1, audit["skipped_source_mapping_count"])
        self.assertEqual("page-safehouse", audit["applied_source_mappings"][0]["page_id"])
        self.assertEqual("page-missing", audit["skipped_source_mappings"][0]["page_id"])
        self.assertEqual("https://notion.test/page-missing", audit["skipped_source_mappings"][0]["page_url"])
        self.assertEqual("missing_name", audit["skipped_source_mappings"][0]["reason_code"])
        self.assertEqual([], audit["applied_type_counts"])
        self.assertEqual([{"type": "location", "count": 2}], audit["skipped_type_counts"])
        self.assertEqual(0, audit["skipped_blocking_count"])
        self.assertEqual([{"reason_code": "missing_name", "count": 2}], audit["skipped_reason_counts"])
        self.assertEqual([{"severity": "medium", "count": 2}], audit["skipped_severity_counts"])

    def test_model_director_accepts_legacy_prompt_path(self) -> None:
        seen_messages: list[list[dict[str, str]]] = []

        def completion(messages: list[dict[str, str]]) -> str:
            seen_messages.append(messages)
            return json.dumps(
                {
                    "chapter_index": 1,
                    "goal": "legacy_prompt_path",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 0,
                    "notes": [],
                }
            )

        decision = ModelDirector(
            completion=completion,
            prompt_path="core/director/prompt.md",
        )(
            {"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []},
            {"source": "test", "status": "ready", "items": []},
        )

        self.assertEqual("legacy_prompt_path", decision["goal"])
        self.assertIn("NovelAgent Director", seen_messages[0][0]["content"])

    def test_model_director_sets_model_call_stage(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        try:
            with self.assertRaises(ModelCallError) as context:
                ModelDirector(model="gpt-stage-test")(
                    {"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []},
                    {"source": "test", "status": "ready", "items": []},
                )
        finally:
            if original_key is not None:
                os.environ["OPENAI_API_KEY"] = original_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)

        self.assertEqual("director_decision", context.exception.stage)
        self.assertEqual("gpt-stage-test", context.exception.model)

    def test_executor_accepts_model_director(self) -> None:
        tmp_path = self._case_dir("executor")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        director = ModelDirector(
            completion=lambda messages: json.dumps(
                {
                    "chapter_index": 2,
                    "goal": "model_directed_step",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 0,
                    "notes": ["Skip polish for test."],
                }
            )
        )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
        ).run_once(persist=False)

        self.assertEqual("model_directed_step", result["decision"]["goal"])
        self.assertEqual(["generate_chapter", "validate"], result["workflow"])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("preview", result["run"]["status"])

    def test_executor_persists_model_director_call_diagnostics_on_failure(self) -> None:
        tmp_path = self._case_dir("executor_model_call_failure")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        director = ModelDirector(
            model="gpt-test",
            completion=lambda messages: (_ for _ in ()).throw(
                ModelCallError(
                    "OpenAI director call failed: timeout",
                    provider="openai",
                    stage="director_decision",
                    model="gpt-test",
                    cause=TimeoutError("timeout"),
                )
            ),
        )

        with self.assertRaises(ModelCallError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                dry_run=True,
                director=director,
            ).run_once(persist=True)

        run_files = list((tmp_path / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual("failed", saved["run"]["status"])
        self.assertEqual("model", saved["run"]["director"]["mode"])
        self.assertEqual("ModelCallError", saved["run"]["director"]["error_type"])
        self.assertEqual("openai", saved["run"]["director"]["model_call"]["provider"])
        self.assertEqual("director_decision", saved["run"]["director"]["model_call"]["stage"])
        self.assertEqual("gpt-test", saved["run"]["director"]["model_call"]["model"])
        self.assertEqual("TimeoutError", saved["run"]["director"]["model_call"]["cause_type"])
        self.assertIs(saved["run"]["director"], validate_schema(saved["run"]["director"], "director_audit.schema.json"))


if __name__ == "__main__":
    unittest.main()
