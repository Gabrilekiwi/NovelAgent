from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

from core.validator import validate_chapter
from core.validator.llm import LLM_VALIDATION_AREAS, llm_payload_to_check, validate_llm
from core.validator.spatial import validate_bridge_preconditions


class ValidatorTest(unittest.TestCase):
    def test_llm_validator_retries_once_when_response_is_not_json(self) -> None:
        with patch(
            "core.validator.llm.chat_completion",
            side_effect=["I found no issues.", '{"problems": []}'],
        ) as mocked:
            check = validate_llm({"chapter_index": 2}, "A complete chapter.", model="validator-test")

        self.assertTrue(check["ok"])
        self.assertEqual(2, mocked.call_count)

    def test_llm_validator_accepts_fenced_json(self) -> None:
        with patch("core.validator.llm.chat_completion", return_value='```json\n{"problems": []}\n```'):
            check = validate_llm({"chapter_index": 2}, "A complete chapter.", model="validator-test")

        self.assertTrue(check["ok"])

    def test_llm_validator_uses_stage_specific_output_limit(self) -> None:
        with patch(
            "core.validator.llm.chat_completion",
            return_value='{"problems": []}',
        ) as mocked, patch("core.validator.llm.get_config") as config:
            config.return_value.openai_validation_max_output_tokens = 4321
            check = validate_llm(
                {"chapter_index": 2},
                "A complete chapter.",
                model="validator-test",
            )

        self.assertTrue(check["ok"])
        self.assertEqual(4321, mocked.call_args.kwargs["max_tokens"])

    def test_llm_revalidation_uses_focused_prompt_and_smaller_output_limit(self) -> None:
        previous = "The previous committed chapter established the current mutable state."
        with patch(
            "core.validator.llm.chat_completion",
            return_value='{"problems": []}',
        ) as mocked, patch("core.validator.llm.get_config") as config:
            config.return_value.openai_validation_max_output_tokens = 10000
            config.return_value.openai_revalidation_max_output_tokens = 6000
            check = validate_llm(
                {
                    "chapter_index": 5,
                    "world_state": {"text": "stale source document", "danger": "high"},
                    "timeline": [{"chapter_index": 4, "validation": {"large": "metadata"}}],
                },
                "The repaired chapter keeps the current state consistent.",
                model="validator-test",
                validation_context={
                    "previous_chapter": {"chapter_index": 4, "text": previous},
                    "revalidation": {
                        "prior_problems": [
                            {
                                "code": "llm_timeline_gap",
                                "area": "timeline_causality",
                                "severity": "high",
                            }
                        ]
                    },
                },
            )

        self.assertTrue(check["ok"])
        self.assertEqual("repair_revalidation", check["metadata"]["mode"])
        self.assertEqual(6000, mocked.call_args.kwargs["max_tokens"])
        messages = mocked.call_args.args[0]
        self.assertIn("focused fiction repair verifier", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual("repair_revalidation", payload["validation_mode"])
        self.assertEqual(["timeline_causality"], payload["check_areas"])
        self.assertEqual(previous, payload["previous_chapter"]["text"])
        self.assertNotIn("text", payload["snapshot"]["world_state"])
        self.assertNotIn("validation", payload["snapshot"]["timeline"][0])

    def test_full_llm_validation_includes_previous_chapter_fact_precedence(self) -> None:
        with patch(
            "core.validator.llm.chat_completion",
            return_value='{"problems": []}',
        ) as mocked:
            check = validate_llm(
                {"chapter_index": 5},
                "A complete current chapter.",
                model="validator-test",
                validation_context={
                    "previous_chapter": {
                        "chapter_index": 4,
                        "text": "The protagonist already acquired the ability in chapter four.",
                    }
                },
            )

        self.assertTrue(check["ok"])
        self.assertEqual("full_validation", check["metadata"]["mode"])
        payload = json.loads(mocked.call_args.args[0][1]["content"])
        self.assertEqual(4, payload["previous_chapter"]["chapter_index"])
        self.assertIn("previous_committed_chapter_for_mutable_state", payload["fact_precedence"])

    def test_llm_validator_records_replay_audit_metadata(self) -> None:
        with patch("core.validator.llm.chat_completion", return_value='{"problems": []}'):
            check = validate_llm(
                {"chapter_index": 2},
                "A complete chapter.",
                {"chapter_index": 2},
                model="validator-test",
            )

        self.assertEqual("openai", check["metadata"]["provider"])
        self.assertEqual("validator-test", check["metadata"]["model"])
        self.assertEqual(64, len(check["metadata"]["prompt_hash"]))
        self.assertEqual([{"attempt": 1, "status": "succeeded"}], check["metadata"]["attempt_history"])
        self.assertEqual(
            {"attempted": False, "succeeded": False, "dropped_problem_count": 0},
            check["metadata"]["schema_repair"],
        )

    def test_llm_validator_repairs_schema_invalid_empty_evidence(self) -> None:
        chapter = "Alpha alarm. Beta door."
        fact_id = "chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest()
        invalid = {
            "problems": [
                {
                    "code": "llm_timeline_gap",
                    "message": "The alarm and door transition lacks a causal bridge.",
                    "area": "timeline_causality",
                    "severity": "medium",
                    "fact_id": fact_id,
                    "evidence": [],
                    "repair_hint": "Connect the alarm to the door action.",
                }
            ]
        }
        valid = {
            "problems": [
                {
                    **invalid["problems"][0],
                    "evidence": [
                        {
                            "kind": "chapter_span",
                            "start_char": 0,
                            "end_char": len("Alpha alarm."),
                            "quote": "Alpha alarm.",
                        }
                    ],
                }
            ]
        }

        with patch(
            "core.validator.llm.chat_completion",
            side_effect=[json.dumps(invalid), json.dumps(valid)],
        ) as mocked:
            check = validate_llm({"chapter_index": 1}, chapter, model="validator-test")

        self.assertEqual(2, mocked.call_count)
        self.assertEqual(["llm_timeline_gap"], [problem["code"] for problem in check["problems"]])
        self.assertEqual(
            {"attempted": True, "succeeded": True, "dropped_problem_count": 0},
            check["metadata"]["schema_repair"],
        )

    def test_llm_validator_drops_evidence_free_findings_when_schema_repair_still_fails(self) -> None:
        chapter = "Alpha alarm. Beta door."
        invalid = {
            "problems": [
                {
                    "code": "llm_timeline_gap",
                    "message": "The alarm and door transition lacks a causal bridge.",
                    "area": "timeline_causality",
                    "severity": "medium",
                    "fact_id": "chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest(),
                    "evidence": [],
                    "repair_hint": "Connect the alarm to the door action.",
                }
            ]
        }

        with patch(
            "core.validator.llm.chat_completion",
            side_effect=[json.dumps(invalid), json.dumps(invalid)],
        ) as mocked:
            check = validate_llm({"chapter_index": 1}, chapter, model="validator-test")

        self.assertEqual(2, mocked.call_count)
        self.assertTrue(check["ok"])
        self.assertEqual([], check["problems"])
        self.assertEqual(
            {"attempted": True, "succeeded": False, "dropped_problem_count": 1},
            check["metadata"]["schema_repair"],
        )

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

    def test_named_voice_gender_conflicts_are_blocking_in_both_directions(self) -> None:
        cases = (
            ("male", "女声", "female"),
            ("female", "男声", "male"),
        )
        for snapshot_gender, prose_voice, actual_gender in cases:
            with self.subTest(snapshot_gender=snapshot_gender, prose_voice=prose_voice):
                chapter = f"沈砚按住门闩后开口回答，走廊里响起清晰的{prose_voice}：“先检查回执。”"
                snapshot = {
                    "chapter_index": 12,
                    "world_state": {},
                    "characters": {
                        "沈砚": {
                            "status": "active",
                            "voice_gender": snapshot_gender,
                        }
                    },
                    "timeline": [],
                }

                result = validate_chapter(
                    snapshot,
                    chapter,
                    {"validation_focus": ["continuity"]},
                )

                problem = next(
                    item
                    for item in result["problems"]
                    if item["code"] == "character_voice_gender_conflict"
                )
                self.assertFalse(result["ok"])
                self.assertEqual("critical", problem["severity"])
                self.assertTrue(problem["blocking"])
                self.assertEqual("continuity", problem["validator"])
                self.assertEqual("沈砚", problem["character"])
                self.assertEqual(snapshot_gender, problem["expected"])
                self.assertEqual(actual_gender, problem["actual"])
                self.assertEqual(
                    "snapshot:characters.沈砚.voice_gender",
                    problem["fact_id"],
                )
                evidence = {item["kind"]: item["value"] for item in problem["evidence"]}
                self.assertEqual("characters.沈砚.voice_gender", evidence["snapshot_fact_path"])
                start, end = (int(value) for value in evidence["chapter_span"].split(":"))
                self.assertEqual(chapter[start:end], evidence["chapter_excerpt"])
                self.assertIn("沈砚", evidence["chapter_excerpt"])
                self.assertIn(prose_voice, evidence["chapter_excerpt"])

    def test_voice_gender_requires_named_speaker_binding_not_pronouns_or_nearby_voice(self) -> None:
        snapshot = {
            "chapter_index": 12,
            "world_state": {},
            "characters": {
                "沈砚": {"status": "active", "voice_gender": "male"},
                "林遥": {"status": "active", "voice_gender": "female"},
            },
            "timeline": [],
        }
        clean_cases = (
            "广播员的女声从墙内传来，沈砚只抬手关掉喇叭，没有开口。",
            "沈砚守在门外。她听见广播里传来清晰的女声，却确认那是值班员。沈砚始终没有开口。",
            "沈砚听见林遥开口回答，走廊里响起清晰的女声。",
            "沈砚开口回答，走廊里响起清晰的男声。",
        )
        for chapter in clean_cases:
            with self.subTest(chapter=chapter):
                result = validate_chapter(
                    snapshot,
                    chapter,
                    {"validation_focus": ["continuity"]},
                )
                self.assertNotIn(
                    "character_voice_gender_conflict",
                    {problem["code"] for problem in result["problems"]},
                )

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

    def test_accepts_chinese_opening_bridge_with_punctuation_between_terms(self) -> None:
        snapshot = {
            "chapter_index": 23,
            "world_state": {"locations": {"旧天文馆": {}}},
            "characters": {},
            "timeline": [],
            "story_state": {
                "last_chapter_ending": "他只是安静地站在第一道门前。",
                "last_scene_location": "旧天文馆",
                "last_scene_characters": ["陆敬衡"],
                "open_threads": [],
                "required_opening_bridge": "旧天文馆 陆敬衡",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [],
                "character_positions": {},
                "blocked_paths": [],
                "last_transition": {},
            },
        }

        result = validate_chapter(
            snapshot,
            "旧天文馆，陆敬衡只是安静地站在第一道门前，抬头看向陆砚和阿照手中的黄铜星盘。",
            {"validation_focus": ["spatial"]},
        )

        problem_codes = {p["code"] for p in result["problems"]}
        self.assertNotIn("missing_opening_bridge", problem_codes)
        self.assertNotIn("missing_last_scene_continuity", problem_codes)

    def test_accepts_two_character_chinese_opening_bridge_terms(self) -> None:
        snapshot = {
            "chapter_index": 9,
            "world_state": {"locations": {"冷库": {}}},
            "characters": {},
            "timeline": [],
            "story_state": {
                "last_chapter_ending": "短暂停顿后，她报出了病区坐标。",
                "last_scene_location": "冷库",
                "last_scene_characters": ["陆沉"],
                "open_threads": [],
                "required_opening_bridge": "冷库 陆沉",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [],
                "character_positions": {},
                "blocked_paths": [],
                "last_transition": {},
            },
        }

        result = validate_chapter(
            snapshot,
            "电台里的坐标钉进冷库的墙，陆沉站在控制台前，听见隔离仓再次传来撞击。",
            {"validation_focus": ["spatial"]},
        )

        problem_codes = {problem["code"] for problem in result["problems"]}
        self.assertNotIn("missing_opening_bridge", problem_codes)
        self.assertNotIn("missing_last_scene_continuity", problem_codes)

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

        chapter = "The danger forced a choice that created open conflict in the shelter."
        quote = "forced a choice"
        start = chapter.index(quote)

        def fake_llm_validator(snapshot, chapter_text, decision):
            return llm_payload_to_check(
                {
                    "problems": [
                        {
                            "code": "llm_motivation_inconsistent",
                            "message": "The protagonist changes goals without a cause.",
                            "area": "character_motivation_consistency",
                            "severity": "high",
                            "fact_id": "chapter:sha256:" + hashlib.sha256(chapter_text.encode("utf-8")).hexdigest(),
                            "evidence": [
                                {
                                    "kind": "chapter_span",
                                    "start_char": start,
                                    "end_char": start + len(quote),
                                    "quote": quote,
                                }
                            ],
                            "repair_hint": "Add a causal beat before the goal changes.",
                        }
                    ]
                },
                chapter_text=chapter_text,
            )

        result = validate_chapter(
            snapshot,
            chapter,
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
        self.assertEqual([{"kind": "chapter_span", "value": quote}], problem["evidence"])
        self.assertEqual("chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest(), problem["fact_id"])

    def test_validate_chapter_forwards_optional_llm_context(self) -> None:
        captured: list[dict] = []

        def fake_llm_validator(snapshot, chapter_text, decision, *, validation_context):
            captured.append(validation_context)
            return {"name": "llm", "ok": True, "problems": []}

        context = {"previous_chapter": {"chapter_index": 1, "text": "Committed prose."}}
        result = validate_chapter(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            "The danger forced a difficult choice and created open conflict in the shelter.",
            enable_llm=True,
            llm_validator=fake_llm_validator,
            llm_context=context,
        )

        llm_check = next(check for check in result["checks"] if check["name"] == "llm")
        self.assertTrue(llm_check["ok"])
        self.assertEqual([context], captured)

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
                },
                chapter_text="Missing causal bridge.",
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
                            "fact_id": "chapter:sha256:" + hashlib.sha256(b"Effect appears before cause.").hexdigest(),
                            "evidence": [
                                {
                                    "kind": "chapter_span",
                                    "start_char": 0,
                                    "end_char": len("Effect appears before cause."),
                                    "quote": "Effect appears before cause.",
                                }
                            ],
                            "repair_hint": "Add the missing cause.",
                        }
                    ]
                },
                chapter_text="Effect appears before cause.",
            )

    def test_llm_evidence_with_wrong_span_is_corrected_when_quote_is_unique(self) -> None:
        chapter = "Alpha beta gamma."
        fact_id = "chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest()
        payload = {
            "problems": [
                {
                    "code": "llm_timeline_gap",
                    "message": "The evidence is inconsistent.",
                    "area": "timeline_causality",
                    "severity": "high",
                    "fact_id": fact_id,
                    "evidence": [
                        {
                            "kind": "chapter_span",
                            "start_char": 0,
                            "end_char": 5,
                            "quote": "beta",
                        }
                    ],
                    "repair_hint": "Repair the causal transition.",
                }
            ]
        }

        check = llm_payload_to_check(payload, chapter_text=chapter)

        self.assertFalse(check["ok"])
        self.assertEqual(
            [{"kind": "chapter_span", "start_char": 6, "end_char": 10, "quote": "beta"}],
            check["problems"][0]["evidence_spans"],
        )

    def test_llm_finding_is_dropped_when_no_evidence_quote_exists(self) -> None:
        chapter = "Alpha beta gamma."
        fact_id = "chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest()
        check = llm_payload_to_check(
            {
                "problems": [
                    {
                        "code": "llm_timeline_gap",
                        "message": "The evidence is inconsistent.",
                        "area": "timeline_causality",
                        "severity": "high",
                        "fact_id": fact_id,
                        "evidence": [
                            {
                                "kind": "chapter_span",
                                "start_char": 0,
                                "end_char": 5,
                                "quote": "delta",
                            }
                        ],
                        "repair_hint": "Repair the causal transition.",
                    }
                ]
            },
            chapter_text=chapter,
        )

        self.assertTrue(check["ok"])
        self.assertEqual([], check["problems"])

    def test_llm_finding_is_dropped_when_wrong_span_quote_is_ambiguous(self) -> None:
        chapter = "The door opened, then the door closed."
        fact_id = "chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest()
        check = llm_payload_to_check(
            {
                "problems": [
                    {
                        "code": "llm_timeline_gap",
                        "message": "The evidence is inconsistent.",
                        "area": "timeline_causality",
                        "severity": "medium",
                        "fact_id": fact_id,
                        "evidence": [
                            {
                                "kind": "chapter_span",
                                "start_char": 0,
                                "end_char": 4,
                                "quote": "door",
                            }
                        ],
                        "repair_hint": "Repair the causal transition.",
                    }
                ]
            },
            chapter_text=chapter,
        )

        self.assertTrue(check["ok"])
        self.assertEqual([], check["problems"])

    def test_llm_finding_with_wrong_chapter_digest_is_rejected(self) -> None:
        chapter = "Alpha beta gamma."
        payload = {
            "problems": [
                {
                    "code": "llm_timeline_gap",
                    "message": "The evidence is inconsistent.",
                    "area": "timeline_causality",
                    "severity": "high",
                    "fact_id": "chapter:sha256:" + "0" * 64,
                    "evidence": [
                        {
                            "kind": "chapter_span",
                            "start_char": 0,
                            "end_char": 5,
                            "quote": "Alpha",
                        }
                    ],
                    "repair_hint": "Repair the causal transition.",
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "fact_id does not match"):
            llm_payload_to_check(payload, chapter_text=chapter)

    def test_medium_llm_finding_is_advisory_before_calibration(self) -> None:
        chapter = "The alarm rang before the door opened."
        quote = "alarm rang"
        start = chapter.index(quote)
        check = llm_payload_to_check(
            {
                "problems": [
                    {
                        "code": "llm_possible_timeline_gap",
                        "message": "The causal ordering may be unclear.",
                        "area": "timeline_causality",
                        "severity": "medium",
                        "fact_id": "chapter:sha256:" + hashlib.sha256(chapter.encode("utf-8")).hexdigest(),
                        "evidence": [
                            {
                                "kind": "chapter_span",
                                "start_char": start,
                                "end_char": start + len(quote),
                                "quote": quote,
                            }
                        ],
                        "repair_hint": "Clarify the order if another check confirms it.",
                    }
                ]
            },
            chapter_text=chapter,
        )

        self.assertTrue(check["ok"])
        self.assertFalse(check["problems"][0]["blocking"])

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
