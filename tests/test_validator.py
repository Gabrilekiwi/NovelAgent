from __future__ import annotations

import unittest

from core.validator import validate_chapter
from core.validator.llm import LLM_VALIDATION_AREAS, llm_payload_to_check
from core.validator.spatial import validate_bridge_preconditions


class ValidatorTest(unittest.TestCase):
    def test_rejects_short_chapter_without_conflict(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {}},
            "characters": {},
            "timeline": [],
        }

        result = validate_chapter(snapshot, "天气很好。")

        self.assertFalse(result["ok"])
        self.assertIn("chapter_too_short", {p["code"] for p in result["problems"]})
        self.assertIn("missing_conflict_marker", {p["code"] for p in result["problems"]})
        self.assertEqual(len(result["problems"]), result["blocking_problem_count"])
        self.assertEqual(0, result["warning_count"])
        self.assertTrue(all(problem["blocking"] for problem in result["problems"]))
        self.assertTrue(all(problem["category"] == "blocking" for problem in result["problems"]))
        self.assertTrue(all(problem["repair_hint"] for problem in result["problems"]))
        self.assertTrue(all(problem["repair_action"] for problem in result["problems"]))
        self.assertTrue(all(isinstance(problem["repair_parameters"], dict) for problem in result["problems"]))
        self.assertTrue(all(problem["evidence"] for problem in result["problems"]))
        self.assertEqual({"logic"}, {problem["validator"] for problem in result["problems"]})
        self.assertEqual(["continuity", "spatial", "logic"], result["requested_focus"])
        self.assertEqual(["continuity", "spatial", "logic"], result["executed_checks"])
        self.assertEqual([], result["skipped_checks"])
        self.assertEqual(2, result["deterministic_repair_count"])
        self.assertEqual(0, result["manual_review_count"])
        self.assertEqual(
            [
                {"action": "add_conflict_signal", "count": 1},
                {"action": "expand_scene", "count": 1},
            ],
            result["repair_action_counts"],
        )
        severities = {item["severity"]: item["count"] for item in result["severity_counts"]}
        self.assertEqual(1, severities["high"])
        self.assertEqual(1, severities["medium"])

    def test_rejects_declared_chapter_index_mismatch(self) -> None:
        snapshot = {
            "chapter_index": 3,
            "world_state": {"locations": {}},
            "characters": {},
            "timeline": [],
        }

        result = validate_chapter(
            snapshot,
            "Chapter 2: The team entered danger and faced a difficult choice that created open conflict.",
        )

        problems = {p["code"]: p for p in result["problems"]}
        self.assertIn("chapter_index_mismatch", problems)
        self.assertEqual("high", problems["chapter_index_mismatch"]["severity"])
        self.assertTrue(problems["chapter_index_mismatch"]["blocking"])
        self.assertEqual("continuity", problems["chapter_index_mismatch"]["validator"])
        self.assertEqual("correct_chapter_index", problems["chapter_index_mismatch"]["repair_action"])
        self.assertEqual({"expected": "3", "actual": "2"}, problems["chapter_index_mismatch"]["repair_parameters"])
        self.assertIn({"kind": "declared_chapter", "value": "2"}, problems["chapter_index_mismatch"]["evidence"])
        self.assertIn({"kind": "snapshot_chapter_index", "value": "3"}, problems["chapter_index_mismatch"]["evidence"])

    def test_rejects_inactive_character_action(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {}},
            "characters": {"Mira": {"status": "dead"}},
            "timeline": [],
        }

        result = validate_chapter(
            snapshot,
            "Mira said the danger was close, then walked into the conflict as the team faced a costly choice.",
        )

        self.assertIn("inactive_character_action", {p["code"] for p in result["problems"]})

    def test_rejects_unknown_character_location(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {"shelter": {}}},
            "characters": {"Mira": {"current_location": "unknown-room"}},
            "timeline": [],
        }

        result = validate_chapter(
            snapshot,
            "Mira reached the shelter while danger escalated and the team had to choose between rescue and survival.",
        )

        self.assertIn("character_unknown_location", {p["code"] for p in result["problems"]})
        problem = [p for p in result["problems"] if p["code"] == "character_unknown_location"][0]
        self.assertEqual("flag_unknown_location", problem["repair_action"])
        self.assertEqual({"character": "Mira", "location": "unknown-room"}, problem["repair_parameters"])
        self.assertIn({"kind": "unknown_location", "value": "unknown-room"}, problem["evidence"])

    def test_rejects_character_without_current_location_mention(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {"shelter": {}, "bridge": {}}},
            "characters": {"Mira": {"current_location": "shelter"}},
            "timeline": [],
        }

        result = validate_chapter(
            snapshot,
            "Mira reached the bridge while danger escalated and the team had to choose between rescue and survival.",
        )

        self.assertIn("character_location_not_mentioned", {p["code"] for p in result["problems"]})
        problem = [p for p in result["problems"] if p["code"] == "character_location_not_mentioned"][0]
        self.assertEqual("spatial", problem["validator"])
        self.assertEqual("add_character_location", problem["repair_action"])
        self.assertEqual({"character": "Mira", "location": "shelter"}, problem["repair_parameters"])

    def test_rejects_missing_opening_bridge_with_dedicated_code(self) -> None:
        snapshot = {
            "chapter_index": 2,
            "world_state": {"locations": {"train car": {}, "connector passage": {}}},
            "characters": {},
            "timeline": [],
            "story_state": {
                "last_chapter_ending": "The team was trapped in the train car.",
                "last_scene_location": "train car",
                "last_scene_characters": ["Mira"],
                "open_threads": [],
                "required_opening_bridge": "train car to connector passage",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [{"from": "train car", "to": "connector passage"}],
                "character_positions": {},
                "blocked_paths": [],
                "last_transition": {},
            },
        }

        result = validate_chapter(
            snapshot,
            "The connector passage shook as danger forced the team into conflict and a costly choice.",
            {"validation_focus": ["spatial"]},
        )

        problems = {p["code"]: p for p in result["problems"]}
        self.assertIn("missing_opening_bridge", problems)
        self.assertEqual("insert_opening_bridge", problems["missing_opening_bridge"]["repair_action"])
        self.assertEqual("spatial", problems["missing_opening_bridge"]["validator"])
        self.assertIn("missing_last_scene_continuity", problems)
        self.assertEqual("anchor_last_scene_state", problems["missing_last_scene_continuity"]["repair_action"])

    def test_rejects_invalid_spatial_transition_with_dedicated_code(self) -> None:
        snapshot = {
            "chapter_index": 2,
            "world_state": {"locations": {"train car": {}, "platform": {}, "connector passage": {}}},
            "characters": {},
            "timeline": [],
            "story_state": {
                "last_chapter_ending": "",
                "last_scene_location": "train car",
                "last_scene_characters": [],
                "open_threads": [],
                "required_opening_bridge": "",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [{"from": "train car", "to": "platform"}],
                "character_positions": {},
                "blocked_paths": [],
                "last_transition": {},
            },
        }

        result = validate_chapter(
            snapshot,
            "Through a service door, the team entered the connector passage as danger forced open conflict.",
            {"validation_focus": ["spatial"]},
        )

        problem = [p for p in result["problems"] if p["code"] == "invalid_spatial_transition"][0]
        self.assertEqual("add_transition_event", problem["repair_action"])
        self.assertEqual({"expected": "train car", "actual": "connector passage"}, problem["repair_parameters"])

    def test_bridge_preconditions_allow_terminal_last_location(self) -> None:
        snapshot = {
            "chapter_index": 15,
            "world_state": {"locations": {"market": {}, "pier": {}}},
            "characters": {},
            "timeline": [],
            "story_state": {
                "last_chapter_ending": "The ferryman said, now board.",
                "last_scene_location": "pier",
                "last_scene_characters": ["Mira"],
                "open_threads": [],
                "required_opening_bridge": "Continue from pier",
            },
            "spatial_state": {
                "spaces": {"market": {}, "pier": {}},
                "connections": [{"from": "market", "to": "pier"}],
                "character_positions": {"Mira": "pier"},
                "blocked_paths": [],
                "last_transition": {"to": "pier"},
            },
        }

        result = validate_bridge_preconditions(snapshot)

        self.assertTrue(result["ok"])
        self.assertEqual([], result["problem_codes"])

    def test_rejects_character_position_conflict_with_dedicated_code(self) -> None:
        snapshot = {
            "chapter_index": 2,
            "world_state": {"locations": {"train car": {}, "platform": {}, "connector passage": {}}},
            "characters": {},
            "timeline": [],
            "story_state": {
                "last_chapter_ending": "",
                "last_scene_location": "",
                "last_scene_characters": [],
                "open_threads": [],
                "required_opening_bridge": "",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [{"from": "train car", "to": "platform"}],
                "character_positions": {"Mira": "train car"},
                "blocked_paths": [],
                "last_transition": {},
            },
        }

        result = validate_chapter(
            snapshot,
            "Mira waited in the connector passage while danger forced a conflict and a choice.",
            {"validation_focus": ["spatial"]},
        )

        problem = [p for p in result["problems"] if p["code"] == "character_position_conflict"][0]
        self.assertEqual("repair_character_position", problem["repair_action"])
        self.assertEqual(
            {"character": "Mira", "expected": "train car", "actual": "connector passage"},
            problem["repair_parameters"],
        )

    def test_rejects_forbidden_constraint_terms(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {}},
            "characters": {},
            "timeline": [],
            "constraints": [
                {
                    "rule": "Do not resolve the serum conflict.",
                    "forbidden_terms": ["serum conflict resolved"],
                    "required_terms": ["serum"],
                }
            ],
        }

        result = validate_chapter(
            snapshot,
            "The serum conflict resolved too easily, removing danger and choice from the chapter.",
        )

        self.assertIn("forbidden_constraint_term", {p["code"] for p in result["problems"]})
        problem = [p for p in result["problems"] if p["code"] == "forbidden_constraint_term"][0]
        self.assertEqual("remove_forbidden_term", problem["repair_action"])
        self.assertEqual({"term": "serum conflict resolved"}, problem["repair_parameters"])
        self.assertIn({"kind": "matched_forbidden_term", "value": "serum conflict resolved"}, problem["evidence"])

    def test_rejects_missing_required_constraint_terms(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {}},
            "characters": {},
            "timeline": [],
            "constraints": [{"rule": "Keep serum in focus.", "required_terms": ["serum"]}],
        }

        result = validate_chapter(
            snapshot,
            "The danger forced a choice that created conflict, but the scene avoided the required object entirely.",
        )

        self.assertIn("missing_required_constraint_term", {p["code"] for p in result["problems"]})
        problem = [p for p in result["problems"] if p["code"] == "missing_required_constraint_term"][0]
        self.assertEqual("add_required_term", problem["repair_action"])
        self.assertEqual({"term": "serum"}, problem["repair_parameters"])

    def test_validation_focus_can_run_only_logic_checks(self) -> None:
        snapshot = {
            "chapter_index": 3,
            "world_state": {"locations": {"shelter": {}}},
            "characters": {"Mira": {"status": "dead", "current_location": "shelter"}},
            "timeline": [],
        }

        result = validate_chapter(
            snapshot,
            "Chapter 2: Mira said the danger was close, then walked into conflict with a costly choice.",
            {"validation_focus": ["logic"]},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(["logic"], result["requested_focus"])
        self.assertEqual(["logic"], result["executed_checks"])
        self.assertEqual(["continuity", "spatial"], result["skipped_checks"])
        self.assertEqual(["logic"], [check["name"] for check in result["checks"]])

    def test_empty_validation_focus_defaults_to_all_checks(self) -> None:
        snapshot = {
            "chapter_index": 3,
            "world_state": {"locations": {}},
            "characters": {},
            "timeline": [],
        }

        result = validate_chapter(
            snapshot,
            "Chapter 2: The team entered danger and faced a difficult choice that created open conflict.",
            {"validation_focus": []},
        )

        self.assertEqual(["continuity", "spatial", "logic"], [check["name"] for check in result["checks"]])
        self.assertEqual(["continuity", "spatial", "logic"], result["requested_focus"])
        self.assertEqual([], result["skipped_checks"])
        self.assertIn("chapter_index_mismatch", {p["code"] for p in result["problems"]})

    def test_optional_llm_validator_merges_schema_checked_problems(self) -> None:
        snapshot = {
            "chapter_index": 1,
            "world_state": {"locations": {}},
            "characters": {},
            "timeline": [],
        }

        def fake_llm_validator(snapshot, chapter_text, decision):
            return llm_payload_to_check(
                {
                    "problems": [
                        {
                            "code": "llm_motivation_inconsistent",
                            "message": "The protagonist changes goals without a cause.",
                            "area": "character_motivation_consistency",
                            "severity": "high",
                            "evidence": [{"kind": "motivation", "value": "Goal shift has no trigger."}],
                            "repair_hint": "Add a causal beat before the goal changes.",
                        }
                    ]
                }
            )

        result = validate_chapter(
            snapshot,
            "The danger forced a choice that created open conflict in the shelter.",
            enable_llm=True,
            llm_validator=fake_llm_validator,
        )

        problem = [item for item in result["problems"] if item["validator"] == "llm"][0]
        self.assertFalse(result["ok"])
        self.assertIn("llm", result["executed_checks"])
        self.assertEqual("manual_review", problem["repair_action"])
        self.assertEqual("high", problem["severity"])
        self.assertEqual("character_motivation_consistency", problem["area"])
        self.assertEqual("character_motivation_consistency", problem["repair_parameters"]["area"])
        self.assertEqual([{"kind": "motivation", "value": "Goal shift has no trigger."}], problem["evidence"])

    def test_llm_payload_contract_rejects_missing_evidence(self) -> None:
        with self.assertRaisesRegex(Exception, "llm_validation.schema.json"):
            llm_payload_to_check(
                {
                    "problems": [
                        {
                            "code": "llm_timeline_gap",
                            "message": "Missing causal bridge.",
                            "area": "timeline_causality",
                            "severity": "medium",
                            "repair_hint": "Add the missing cause.",
                        }
                    ]
                }
            )

    def test_llm_payload_contract_rejects_missing_area(self) -> None:
        with self.assertRaisesRegex(Exception, "llm_validation.schema.json"):
            llm_payload_to_check(
                {
                    "problems": [
                        {
                            "code": "llm_timeline_gap",
                            "message": "Missing causal bridge.",
                            "severity": "medium",
                            "evidence": [{"kind": "timeline", "value": "Effect appears before cause."}],
                            "repair_hint": "Add the missing cause.",
                        }
                    ]
                }
            )

    def test_llm_validation_areas_match_story_level_contract(self) -> None:
        self.assertEqual(
            (
                "complex_plot_logic",
                "character_motivation_consistency",
                "timeline_causality",
                "setup_and_payoff",
                "emotional_and_theme_drift",
            ),
            LLM_VALIDATION_AREAS,
        )


if __name__ == "__main__":
    unittest.main()
