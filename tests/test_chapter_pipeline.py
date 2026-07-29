from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

from api.contracts import ModelOutputError
from core.schema import validate_schema
from core.story_project.coverage import validate_blueprint_coverage
from modules.chapter_generator import run_chapter_pipeline
import modules.chapter_generator.pipeline as pipeline_module


class ChapterPipelineTest(unittest.TestCase):
    def test_initial_scene_state_restores_location_presence_and_open_action(
        self,
    ) -> None:
        input_pack = (
            "# Authoritative State\n"
            + json.dumps(
                {
                    "events": {
                        "door-opening": {
                            "event_id": "door-opening",
                            "type": "door_opening",
                            "subjects": ["Mira"],
                            "objects": ["blast-door"],
                            "location": "control room",
                            "status": "ongoing",
                        },
                        "radio-scan": {
                            "event_id": "radio-scan",
                            "type": "signal_search",
                            "subjects": ["Mira"],
                            "objects": ["radio"],
                            "location": "control room",
                            "status": "active",
                        },
                        "aborted-scan": {
                            "event_id": "aborted-scan",
                            "type": "signal_search",
                            "subjects": ["Mira"],
                            "objects": ["radio"],
                            "location": "control room",
                            "status": "cancelled",
                        }
                    }
                }
            )
            + "\n\n# Story State\n"
            + json.dumps(
                {
                    "last_scene_location": "control room",
                    "last_scene_characters": ["Mira"],
                    "completed_event_ids": [],
                }
            )
        )

        state = pipeline_module._initial_scene_state(input_pack)

        self.assertEqual("control room", state["current_location"])
        self.assertEqual(["Mira"], state["characters_present"])
        self.assertEqual("door-opening", state["open_action"])
        self.assertEqual(
            {"door-opening", "radio-scan"},
            {
                event["event_id"]
                for event in state["open_actions"]
            },
        )
        self.assertEqual(["aborted-scan"], state["completed_event_ids"])

    def test_authority_location_overrides_stale_story_bridge(self) -> None:
        input_pack = (
            "# Authoritative State\n"
            + json.dumps(
                {
                    "characters": {
                        "hero": {
                            "character_id": "hero",
                            "canonical_name": "Captain",
                            "aliases": ["Captain"],
                            "role": "protagonist",
                        },
                        "ally": {
                            "character_id": "ally",
                        },
                    },
                    "locations": {
                        "hero": {
                            "entity_id": "hero",
                            "location_id": "garage",
                        },
                        "ally": {
                            "entity_id": "ally",
                            "location_id": "garage",
                        },
                    },
                }
            )
            + "\n\n# Story State\n"
            + json.dumps(
                {
                    "last_scene_location": "stale-cold-storage",
                    "last_scene_characters": ["Captain"],
                }
            )
        )

        state = pipeline_module._initial_scene_state(input_pack)

        self.assertEqual("garage", state["current_location"])
        self.assertEqual(["ally", "hero"], state["characters_present"])
        self.assertEqual("garage", state["locations"]["hero"])

    def test_scene_context_keeps_only_bounded_story_state_semantics(self) -> None:
        story_state = {
            "last_chapter_ending": "The generator failed.",
            "last_scene_location": "control room",
            "last_scene_characters": ["Mira"],
            "open_threads": ["Restore power."],
            "required_opening_bridge": "Continue from the control room.",
            "source": "tracking/character-state.md",
            "text": "cumulative audit text " * 200,
        }
        blueprint = {
            "chapter_blueprint": {
                "chapter_index": 3,
                "core_event": "restore the generator " + ("under pressure " * 120),
            },
            "read_set_context_digest": "a" * 64,
            "tracking_excerpts": "unneeded source material " * 200,
        }
        input_pack = (
            "# Story State\n"
            + json.dumps(story_state, ensure_ascii=False, indent=2)
            + "\n\n# StoryProject Chapter Blueprint\n"
            + json.dumps(blueprint, ensure_ascii=False, indent=2)
        )

        compact = pipeline_module._compact_scene_context(input_pack)

        body = compact.split("# Story State\n", 1)[1].split(
            "\n\n# StoryProject Chapter Blueprint\n",
            1,
        )[0]
        retained = json.loads(body)
        self.assertEqual(
            {
                "last_chapter_ending",
                "last_scene_location",
                "last_scene_characters",
                "open_threads",
                "required_opening_bridge",
            },
            set(retained),
        )
        self.assertNotIn("cumulative audit text", compact)
        blueprint_body = compact.split("# StoryProject Chapter Blueprint\n", 1)[1].split(
            "\n\n# Structured Context Manifest\n",
            1,
        )[0]
        retained_blueprint = json.loads(blueprint_body)
        self.assertEqual(
            {"chapter_blueprint", "read_set_context_digest"},
            set(retained_blueprint),
        )
        self.assertNotIn("tracking_excerpts", retained_blueprint)

    def test_scene_context_selects_complete_authority_records(self) -> None:
        events = {
            f"chapter-0017-event-{index:03d}": {
                "event_id": f"chapter-0017-event-{index:03d}",
                "type": "checkpoint_completed",
                "subjects": [f"character-{index % 3}"],
                "objects": [f"object-{index}"],
                "location": "fire-station",
                "status": "completed",
                "detail": f"complete event detail {index} " + ("evidence " * 12),
            }
            for index in range(40)
        }
        events["open-transmission"] = {
            "event_id": "open-transmission",
            "type": "transmission_started",
            "subjects": ["Mira"],
            "objects": ["radio"],
            "location": "fire-station",
            "status": "ongoing",
            "detail": "The transmission remains open.",
        }
        authority = {
            "schema_version": "1.0",
            "source_precedence": ["chapter_event", "model_inference"],
            "characters": {
                "Mira": {
                    "character_id": "Mira",
                    "canonical_name": "Mira",
                    "role": "leader",
                }
            },
            "relationships": {},
            "roster": {},
            "numeric_counters": {
                "erosion": {
                    "counter_id": "erosion",
                    "owner_id": "Mira",
                    "current_value": 6,
                }
            },
            "inventory": {},
            "locations": {
                "Mira": {
                    "entity_id": "Mira",
                    "location_id": "fire-station",
                }
            },
            "events": events,
        }
        context = "# Authoritative State\n" + json.dumps(
            authority,
            ensure_ascii=False,
            indent=2,
        )

        compact = pipeline_module._compact_scene_context(
            context,
            query="chapter-0017-event-039 radio transmission",
        )

        body = compact.split("# Authoritative State\n", 1)[1].split(
            "\n\n# Structured Context Manifest\n",
            1,
        )[0]
        projected = json.loads(body)
        selection = projected["context_selection"]
        self.assertLessEqual(len(compact), 1_500 * 7)
        self.assertGreater(selection["omitted_count"], 0)
        self.assertIn("events/open-transmission", selection["selected_items"])
        self.assertEqual(
            authority["events"]["open-transmission"],
            projected["events"]["open-transmission"],
        )
        self.assertIn("chapter-0017-event-039", projected["events"])
        for collection in (
            "characters",
            "relationships",
            "roster",
            "numeric_counters",
            "inventory",
            "locations",
            "events",
        ):
            for record_id, record in projected[collection].items():
                self.assertEqual(authority[collection][record_id], record)

    def test_scene_request_uses_full_state_before_authority_prompt_projection(self) -> None:
        authority = {
            "schema_version": "1.0",
            "characters": {
                "Mira": {
                    "character_id": "Mira",
                    "canonical_name": "Mira",
                    "role": "leader",
                }
            },
            "relationships": {
                "Mira->Ivo": {
                    "relationship_id": "Mira->Ivo",
                    "source_character_id": "Mira",
                    "target_character_id": "Ivo",
                    "type": "allies",
                }
            },
            "roster": {},
            "numeric_counters": {
                "erosion": {
                    "counter_id": "erosion",
                    "current_value": 6,
                }
            },
            "inventory": {},
            "locations": {
                f"entity-{index}": {
                    "entity_id": f"entity-{index}",
                    "location_id": "fire-station",
                }
                for index in range(12)
            },
            "events": {
                f"chapter-0017-event-{index:03d}": {
                    "event_id": f"chapter-0017-event-{index:03d}",
                    "type": "checkpoint_completed",
                    "subjects": ["Mira"],
                    "objects": [f"object-{index}"],
                    "location": "fire-station",
                    "status": "completed",
                    "detail": "bound event evidence " * 12,
                }
                for index in range(40)
            },
        }
        raw_input = "# Authoritative State\n" + json.dumps(
            authority,
            ensure_ascii=False,
            indent=2,
        )
        initial_state = pipeline_module._initial_scene_state(raw_input)
        compiled = pipeline_module.compile_prompt_contexts(
            raw_input,
            budget=pipeline_module.default_context_budget(
                enable_model_tokenizer=False,
            ),
        )
        plan = {
            "goal": "Continue the transmission.",
            "scenes": [
                {
                    "index": 1,
                    "goal": "Decode the signal.",
                    "required_beat_indexes": [],
                    "required_event_ids": ["chapter-0018-event-001"],
                }
            ],
        }

        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack=compiled.scene.text,
                plan=plan,
                scene=plan["scenes"][0],
                scene_required_beats=[],
                blueprint=None,
                scene_state=initial_state,
                authoritative_state_source=authority,
            )
        )

        current = payload["current_scene_state"]
        self.assertEqual(12, len(current["locations"]))
        self.assertEqual(24, len(current["completed_event_ids"]))
        self.assertEqual(40, current["completed_event_ids_count"])
        self.assertTrue(current["completed_event_ids_truncated"])
        self.assertEqual(64, len(current["completed_event_ids_sha256"]))
        self.assertEqual(24, len(current["completed_events"]))
        self.assertEqual(6, current["counters"]["erosion"])
        self.assertIn("context_selection", payload["shared_context"])

    def test_scene_payload_reprojects_from_raw_authority_not_compiled_subset(
        self,
    ) -> None:
        target_event_id = "zz-target-beta"
        events = {
            f"event-{index:03d}": {
                "event_id": f"event-{index:03d}",
                "type": "checkpoint_completed",
                "subjects": ["Mira"],
                "objects": [f"object-{index}"],
                "location": "fire-station",
                "status": "completed",
                "detail": "distractor history " * 24,
            }
            for index in range(50)
        }
        events[target_event_id] = {
            "event_id": target_event_id,
            "type": "signal_recovered",
            "subjects": ["Mira"],
            "objects": ["beta-key"],
            "location": "fire-station",
            "status": "completed",
            "detail": "Only this record contains beta recovery evidence. " * 30,
        }
        authority = {
            "schema_version": "1.0",
            "characters": {
                "Mira": {
                    "character_id": "Mira",
                    "canonical_name": "Mira",
                }
            },
            "relationships": {},
            "roster": {},
            "numeric_counters": {},
            "inventory": {},
            "locations": {},
            "events": events,
        }
        raw_input = "# Authoritative State\n" + json.dumps(
            authority,
            ensure_ascii=False,
            indent=2,
        )
        compiled = pipeline_module.compile_prompt_contexts(
            raw_input,
            budget=pipeline_module.default_context_budget(
                enable_model_tokenizer=False,
            ),
        )
        compiled_authority = pipeline_module.authoritative_state_from_markdown(
            compiled.scene.text
        )
        self.assertIsNotNone(compiled_authority)
        self.assertNotIn(target_event_id, compiled_authority["events"])
        plan = {
            "goal": f"Recover {target_event_id} beta-key evidence.",
            "scenes": [
                {
                    "index": 1,
                    "goal": f"Use {target_event_id} and beta-key.",
                    "required_beat_indexes": [],
                    "required_event_ids": ["chapter-0018-event-001"],
                }
            ],
        }

        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack=compiled.scene.text,
                plan=plan,
                scene=plan["scenes"][0],
                scene_required_beats=[],
                blueprint=None,
                scene_state=pipeline_module._initial_scene_state(raw_input),
                authoritative_state_source=authority,
            )
        )
        scene_authority = pipeline_module.authoritative_state_from_markdown(
            payload["shared_context"]
        )

        self.assertIsNotNone(scene_authority)
        self.assertIn(target_event_id, scene_authority["events"])
        self.assertNotIn(
            "parent_projection_sha256",
            scene_authority["context_selection"],
        )
        self.assertNotIn(
            "input_sha256",
            scene_authority["context_selection"],
        )

    def test_plan_chapter_is_compatibility_alias_for_plan_scenes(self) -> None:
        expected = pipeline_module.plan_scenes("input pack", chapter_index=7, dry_run=True)

        with self.assertWarnsRegex(
            FutureWarning,
            r"plan_chapter\(\) is deprecated; use plan_scenes\(\) instead",
        ) as warning:
            actual = pipeline_module.plan_chapter("input pack", chapter_index=7, dry_run=True)

        self.assertEqual(expected, actual)
        self.assertEqual(__file__, warning.filename)
        self.assertIn("plan_scenes", pipeline_module.__all__)

    def _blueprint(self) -> dict:
        return {
            "chapter_index": 3,
            "outline_path": "book/大纲/细纲_第003章.md",
            "title": "Pressure Test",
            "core_event": "The crew enters the sealed station.",
            "required_beats": [
                {"index": 1, "text": "open the sealed station"},
                {"index": 2, "text": "discover the missing signal"},
                {"index": 3, "text": "choose who carries the serum"},
            ],
            "ending_pressure": "the signal starts counting down",
            "source_path": "book/大纲/细纲_第003章.md",
            "missing_fields": [],
        }

    def test_dry_run_scene_limit_bounds_scene_drafts(self) -> None:
        pipeline = run_chapter_pipeline(
            "Input pack for a smoke-sized chapter generation check.",
            chapter_index=2,
            dry_run=True,
            scene_limit=1,
        )

        self.assertIs(pipeline, validate_schema(pipeline, "chapter_pipeline.schema.json"))
        self.assertEqual(1, len(pipeline["plan"]["scenes"]))
        self.assertEqual(1, len(pipeline["scene_drafts"]))
        self.assertEqual(1, len(pipeline["scene_spans"]))
        self.assertEqual("opening_bridge", pipeline["plan"]["scenes"][0]["type"])
        self.assertEqual("Continue directly from last_chapter_ending", pipeline["plan"]["scenes"][0]["goal"])
        self.assertEqual(
            [
                "repeat last known location",
                "show immediate consequence",
                "explain transition before new scene",
            ],
            pipeline["plan"]["scenes"][0]["required_beats"],
        )
        span = pipeline["scene_spans"][0]
        scene_text = pipeline["scene_drafts"][0]["text"]
        self.assertEqual(0, span["start_char"])
        self.assertEqual(len(scene_text), span["end_char"])
        self.assertEqual(scene_text, pipeline["merged_chapter"][span["start_char"]:span["end_char"]])
        self.assertEqual(1, pipeline["stages"][0]["summary"]["scene_count"])
        self.assertEqual(1, pipeline["stages"][1]["summary"]["scene_count"])
        self.assertIsNone(pipeline.get("story_project"))
        self.assertIsNone(pipeline.get("chapter_blueprint"))
        self.assertIsNone(pipeline.get("blueprint_coverage"))

    def test_story_project_scene_limit_one_keeps_all_required_beats(self) -> None:
        pipeline = run_chapter_pipeline(
            "StoryProject input pack.",
            chapter_index=3,
            dry_run=True,
            scene_limit=1,
            chapter_blueprint=self._blueprint(),
        )

        self.assertEqual(1, len(pipeline["plan"]["scenes"]))
        self.assertEqual([1, 2, 3], pipeline["plan"]["scenes"][0]["required_beat_indexes"])
        self.assertEqual([1, 2, 3], pipeline["scene_drafts"][0]["covered_beat_indexes"])
        self.assertEqual([], pipeline["blueprint_coverage"]["missing_beat_indexes"])
        self.assertEqual([1, 2, 3], pipeline["blueprint_coverage"]["covered_beat_indexes"])
        self.assertTrue(pipeline["blueprint_coverage"]["ending_pressure_covered"])

    def test_scene_request_bounds_zh_chapter_length_and_warns_against_restarts(self) -> None:
        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack="context",
                plan={"scenes": [{"index": 1}, {"index": 2}, {"index": 3}]},
                scene={"index": 1},
                scene_required_beats=[],
                blueprint=None,
            )
        )

        self.assertIn("1000-1500 Chinese characters", payload["instruction"])
        self.assertIn("Do not restart, duplicate, or retell", payload["instruction"])
        delta_schema = payload["response_schema"]["deltas"]
        self.assertEqual(
            {"character_id", "field", "before", "after", "reason"},
            set(delta_schema["characters"][0]),
        )
        self.assertEqual(
            {"owner_id", "item_id", "before", "delta", "after", "reason", "source_event_id"},
            set(delta_schema["inventory"][0]),
        )
        self.assertEqual(
            {"counter_id", "before", "delta", "after", "reason", "source_event_id"},
            set(delta_schema["counters"][0]),
        )
        self.assertNotIn("stable_entity_id", json.dumps(delta_schema))
        self.assertTrue(any("does not actually change" in rule for rule in payload["delta_rules"]))

    def test_scene_request_compacts_global_plan_but_keeps_current_scene_complete(self) -> None:
        scenes = [
            {
                "index": index,
                "type": "story_project_blueprint",
                "goal": f"Scene {index} goal",
                "required_beat_indexes": [index],
                "required_beats": [f"Full required beat {index}"],
                "planned_events": [
                    {
                        "event_id": f"event-{index}",
                        "type": f"beat_{index}_completed",
                        "subjects": [],
                        "objects": [],
                        "location": "",
                        "status": "completed",
                    }
                ],
                "required_event_ids": [f"event-{index}"],
                "forbidden_event_ids": [
                    f"event-{previous}"
                    for previous in range(1, index)
                ],
            }
            for index in range(1, 10)
        ]

        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack="# Requirements\nPreserve the chapter arc.",
                plan={"goal": "Nine-scene chapter", "scenes": scenes},
                scene=scenes[7],
                scene_required_beats=[],
                blueprint=None,
            )
        )

        self.assertEqual(9, len(payload["chapter_plan"]["scenes"]))
        self.assertEqual([8], payload["chapter_plan"]["scenes"][7]["required_beat_indexes"])
        self.assertEqual(["event-8"], payload["chapter_plan"]["scenes"][7]["required_event_ids"])
        self.assertNotIn("planned_events", payload["chapter_plan"]["scenes"][7])
        self.assertNotIn("required_beats", payload["chapter_plan"]["scenes"][7])
        self.assertNotIn("forbidden_event_ids", payload["chapter_plan"]["scenes"][7])
        self.assertEqual(scenes[7], payload["scene"])

    def test_scene_request_compacts_below_safe_limit_not_just_hard_limit(self) -> None:
        context = "# Requirements\n" + "\n\n".join(
            f"Requirement {index}: preserve continuity."
            for index in range(100)
        )

        class HeadroomBudget:
            hard_input_limit = 32_000

            def __init__(self) -> None:
                self.measured_payloads: list[dict] = []

            def measure(self, text: str, *, stage: str, **_: object) -> dict:
                if stage != "scene":
                    raise AssertionError(f"unexpected budget stage: {stage}")
                self.measured_payloads.append(json.loads(text))
                tokens = 31_989 if len(self.measured_payloads) == 1 else 28_027
                return {
                    "within_budget": True,
                    "budgeted_input_tokens": tokens,
                    "hard_input_limit": self.hard_input_limit,
                }

            def require_input(self, *_: object, **__: object) -> dict:
                raise AssertionError("the second candidate should meet the safe input target")

        budget = HeadroomBudget()
        with patch.object(pipeline_module, "default_context_budget", return_value=budget):
            payload = json.loads(
                pipeline_module._scene_request_payload(
                    input_pack=context,
                    plan={
                        "goal": "Keep nine scenes ordered.",
                        "scenes": [{"index": index} for index in range(1, 10)],
                    },
                    scene={"index": 8},
                    scene_required_beats=[],
                    blueprint=None,
                    previous_scene_tail="latest scene tail " * 50,
                    prior_scene_summaries=[
                        {
                            "index": index,
                            "goal": f"Scene {index}",
                            "tail": "prior scene evidence " * 20,
                            "event_ids": [f"event-{index}"],
                        }
                        for index in range(1, 8)
                    ],
                )
            )

        self.assertEqual(2, len(budget.measured_payloads))
        self.assertEqual(500, len(payload["previous_scene_tail"]))
        self.assertTrue(
            all("goal" not in item for item in payload["prior_scene_summaries"])
        )

    def test_second_scene_uses_lossless_transport_after_state_advances(
        self,
    ) -> None:
        class TwoSceneBudget:
            hard_input_limit = 10_500

            def measure(self, text: str, *, stage: str, **_: object) -> dict:
                if stage != "scene":
                    raise AssertionError(f"unexpected budget stage: {stage}")
                tokens = len(text)
                return {
                    "within_budget": tokens <= self.hard_input_limit,
                    "budgeted_input_tokens": tokens,
                    "hard_input_limit": self.hard_input_limit,
                }

            def require_input(self, text: str, *, stage: str, **_: object) -> dict:
                return self.measure(text, stage=stage)

        plan = {
            "goal": "Complete the two-stage transmission.",
            "scenes": [],
        }
        for index, (event_type, event_object) in enumerate(
            [
                ("signal_alignment_completed", "relay"),
                ("transmission_completed", "receiver"),
            ],
            start=1,
        ):
            event = {
                "event_id": f"chapter-0018-event-{index:03d}",
                "type": event_type,
                "subjects": ["hero"],
                "objects": [event_object],
                "location": "station",
                "status": "completed",
            }
            plan["scenes"].append(
                {
                    "index": index,
                    "type": "story_project_blueprint",
                    "goal": f"Scene {index} " + ("preserve ordered continuity " * 2),
                    "required_beat_indexes": [],
                    "required_event_ids": [event["event_id"]],
                    "forbidden_event_ids": [
                        f"chapter-0018-event-{prior:03d}"
                        for prior in range(1, index)
                    ],
                    "planned_events": [event],
                }
            )
        initial_state = pipeline_module.empty_scene_state()
        initial_state["characters"] = {
            "hero": {
                "character_id": "hero",
                "canonical_name": "Mira",
                "role": "leader",
                "identity": "station commander",
                "notes": "stable authority " * 2,
            }
        }
        initial_state["locations"] = {"hero": "station"}
        initial_state["counters"] = {"erosion": 6}
        initial_state["current_location"] = "station"
        initial_state["characters_present"] = ["hero"]
        historical_event = {
            "event_id": "chapter-0017-event-001",
            "type": "checkpoint_completed",
            "subjects": ["hero"],
            "objects": ["north gate"],
            "location": "station",
            "status": "completed",
            "detail": "historical audit evidence " * 5,
        }
        initial_state["completed_event_ids"] = [historical_event["event_id"]]
        initial_state["completed_events"] = [historical_event]
        requests: list[str] = []

        def completion(messages, **_kwargs):
            raw_request = messages[-1]["content"]
            requests.append(raw_request)
            request = json.loads(raw_request)
            scene_index = request["scene"]["index"]
            return json.dumps(
                {
                    "prose": (
                        f"Scene {scene_index} unique marker. "
                        + ("continuous scene prose " * 30)
                    ),
                    "events": request["scene"]["planned_events"],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                    "continuity_note": f"Scene {scene_index} continues.",
                }
            )

        budget = TwoSceneBudget()
        with (
            patch.object(pipeline_module, "default_context_budget", return_value=budget),
            patch.object(pipeline_module, "chat_completion", side_effect=completion),
        ):
            drafts = pipeline_module.generate_scenes(
                "# Requirements\nPreserve authority and continuity.",
                plan,
                dry_run=False,
                initial_scene_state=initial_state,
            )

        self.assertEqual(2, len(requests))
        self.assertEqual(2, len(drafts))
        scene_two_payload = json.loads(requests[1])
        self.assertEqual(
            drafts[0]["scene_state_after"],
            scene_two_payload["current_scene_state"],
        )
        self.assertEqual(
            6,
            scene_two_payload["current_scene_state"]["counters"]["erosion"],
        )
        self.assertEqual(
            "station",
            scene_two_payload["current_scene_state"]["locations"]["hero"],
        )
        self.assertIn(
            plan["scenes"][0]["required_event_ids"][0],
            scene_two_payload["current_scene_state"]["completed_event_ids"],
        )
        pretty_payload = json.dumps(
            scene_two_payload,
            ensure_ascii=False,
            indent=2,
        )
        safe_input_limit = pipeline_module._scene_safe_input_limit(budget)
        self.assertGreater(len(pretty_payload), safe_input_limit)
        self.assertLessEqual(len(pretty_payload), budget.hard_input_limit)
        self.assertLessEqual(len(requests[1]), safe_input_limit)
        self.assertEqual(
            requests[1],
            json.dumps(
                scene_two_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def test_full_local_event_ids_are_validated_with_bounded_prompt_summary(self) -> None:
        oldest = {
            "event_id": "historical-event-000",
            "type": "rescue_completed",
            "subjects": ["hero"],
            "objects": ["survivor-group"],
            "location": "service-tunnel",
            "status": "completed",
        }
        historical_events = [
            oldest,
            *[
                {
                    "event_id": f"historical-event-{index:03d}",
                    "type": f"checkpoint_{index}_completed",
                    "subjects": ["hero"],
                    "objects": [f"checkpoint-{index}"],
                    "location": "service-tunnel",
                    "status": "completed",
                }
                for index in range(1, 25)
            ],
        ]
        initial_state = pipeline_module.empty_scene_state()
        initial_state["completed_event_ids"] = [
            event["event_id"]
            for event in historical_events
        ]
        initial_state["completed_events"] = historical_events
        repeated = dict(oldest)
        plan = {
            "goal": "Do not repeat a historical rescue.",
            "scenes": [
                {
                    "index": 1,
                    "type": "story_project_blueprint",
                    "goal": "Advance without replaying the rescue.",
                    "required_beats": ["advance"],
                    "required_event_ids": [repeated["event_id"]],
                    "forbidden_event_ids": [],
                    "planned_events": [repeated],
                }
            ],
        }
        requests: list[dict] = []

        def repeated_completion(messages, **_kwargs):
            request = json.loads(messages[-1]["content"])
            requests.append(request)
            return json.dumps(
                {
                    "prose": "The same completed rescue reuses its old event id.",
                    "events": [repeated],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                    "continuity_note": "invalid historical replay",
                }
            )

        with patch.object(
            pipeline_module,
            "chat_completion",
            side_effect=repeated_completion,
        ):
            with self.assertRaisesRegex(ValueError, "repeated_completed_event_id"):
                pipeline_module.generate_scenes(
                    "input pack",
                    plan,
                    dry_run=False,
                    initial_scene_state=initial_state,
                )

        self.assertEqual(2, len(requests))
        self.assertEqual(
            24,
            len(requests[0]["current_scene_state"]["completed_events"]),
        )
        self.assertEqual(
            24,
            len(requests[0]["current_scene_state"]["completed_event_ids"]),
        )
        self.assertNotIn(
            oldest["event_id"],
            {
                event["event_id"]
                for event in requests[0]["current_scene_state"]["completed_events"]
            },
        )
        self.assertNotIn(
            oldest["event_id"],
            requests[0]["current_scene_state"]["completed_event_ids"],
        )
        self.assertEqual(
            len(historical_events),
            requests[0]["current_scene_state"]["completed_event_ids_count"],
        )
        self.assertTrue(
            requests[0]["current_scene_state"]["completed_event_ids_truncated"],
        )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    initial_state["completed_event_ids"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            requests[0]["current_scene_state"]["completed_event_ids_sha256"],
        )

    def test_scene_request_compacts_large_sections_and_drops_memory_index(self) -> None:
        context = "\n\n".join(
            [f"# Section {index}\nHEAD-{index}\n" + (str(index) * 5_000) + f"\nTAIL-{index}" for index in range(8)]
            + ["# Memory Index\n" + ("memory" * 1_000)]
        )

        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack=context,
                plan={"scenes": [{"index": 1}]},
                scene={"index": 1},
                scene_required_beats=[],
                blueprint=None,
            )
        )

        self.assertNotIn("Memory Index", payload["shared_context"])
        self.assertIn("完整条目已省略", payload["shared_context"])
        self.assertIn("TAIL-7", payload["shared_context"])
        self.assertLessEqual(len(payload["shared_context"]), 1_500 * 7)
        manifest = json.loads(payload["shared_context"].split("# Structured Context Manifest\n", 1)[1])
        self.assertEqual(len(context), manifest["original_chars"])
        self.assertEqual(64, len(manifest["source_sha256"]))
        self.assertTrue(manifest["selected_items"])

    def test_scene_context_compacts_prompt_selection_audit_list(self) -> None:
        selection = {
            "schema_version": "1.0",
            "policy": "prompt_scene_section_relevance_v1",
            "source_sha256": "a" * 64,
            "original_chars": 127_984,
            "selected_items": [
                {
                    "id": f"section:{index}:long-audit-name",
                    "name": f"Long Audit Section {index}",
                    "sha256": f"{index:064x}",
                    "original_chars": 10_000 + index,
                }
                for index in range(12)
            ],
            "omitted_count": 4,
        }
        context = (
            "# Prompt Context Selection\n"
            + json.dumps(selection, ensure_ascii=False, indent=2)
            + "\n\n# Requirements\nPreserve every authoritative story fact."
        )

        compact = pipeline_module._compact_scene_context(context)

        body = compact.split("# Prompt Context Selection\n", 1)[1].split(
            "\n\n# Requirements\n",
            1,
        )[0]
        retained = json.loads(body)
        self.assertEqual(
            {
                "schema_version",
                "policy",
                "source_sha256",
                "original_chars",
                "omitted_count",
            },
            set(retained),
        )
        self.assertNotIn("selected_items", retained)
        self.assertEqual("a" * 64, retained["source_sha256"])

    def test_scene_request_adaptively_compacts_accumulated_scene_evidence(self) -> None:
        context = "# Requirements\n\n" + "\n\n".join(
            f"Requirement {index}: " + ("bounded context " * 10)
            for index in range(40)
        )

        class AdaptiveBudget:
            def __init__(self) -> None:
                self.measured_payloads: list[dict] = []

            def measure(self, text: str, *, stage: str, **_: object) -> dict:
                self.assert_stage(stage)
                self.measured_payloads.append(json.loads(text))
                return {"within_budget": len(self.measured_payloads) >= 2}

            def require_input(self, *_: object, **__: object) -> dict:
                raise AssertionError("the second adaptive context candidate should fit")

            @staticmethod
            def assert_stage(stage: str) -> None:
                if stage != "scene":
                    raise AssertionError(f"unexpected budget stage: {stage}")

        budget = AdaptiveBudget()
        with patch.object(pipeline_module, "default_context_budget", return_value=budget):
            payload = json.loads(
                pipeline_module._scene_request_payload(
                    input_pack=context,
                    plan={"scenes": [{"index": index} for index in range(1, 10)]},
                    scene={"index": 9},
                    scene_required_beats=[],
                    blueprint=None,
                    previous_scene_tail="latest scene tail " * 50,
                    prior_scene_summaries=[
                        {
                            "index": index,
                            "goal": "planned scene goal " * 10,
                            "tail": "previous scene tail " * 20,
                            "event_ids": [f"event-{index}"],
                        }
                        for index in range(1, 9)
                    ],
                )
            )

        self.assertEqual(2, len(budget.measured_payloads))
        first, second = budget.measured_payloads
        self.assertEqual(first["shared_context"], second["shared_context"])
        self.assertLess(
            len(json.dumps(second["prior_scene_summaries"])),
            len(json.dumps(first["prior_scene_summaries"])),
        )
        self.assertLess(
            len(second["previous_scene_tail"]),
            len(first["previous_scene_tail"]),
        )
        self.assertTrue(all("goal" in item for item in first["prior_scene_summaries"]))
        self.assertTrue(all("goal" not in item for item in second["prior_scene_summaries"]))
        self.assertEqual(
            [f"event-{index}" for index in range(1, 9)],
            [item["event_ids"][0] for item in payload["prior_scene_summaries"]],
        )

    def test_story_project_plan_does_not_call_model_planner(self) -> None:
        original_chat_completion = pipeline_module.chat_completion

        def fail_if_called(*args, **kwargs):
            raise AssertionError("StoryProject planning must not call OpenAI")

        pipeline_module.chat_completion = fail_if_called
        try:
            plan = pipeline_module.plan_scenes(
                "input pack",
                chapter_index=3,
                dry_run=False,
                chapter_blueprint=self._blueprint(),
            )
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("The crew enters the sealed station.", plan["goal"])
        self.assertEqual([1], plan["scenes"][0]["required_beat_indexes"])

    def test_legacy_recovered_scene_prefix_is_regenerated_from_first_unstructured_scene(
        self,
    ) -> None:
        calls: list[str] = []
        recovered = [
            {"index": 1, "text": "The sealed door opened onto the abandoned station."},
            {"index": 2, "text": "The crew found the missing signal pulsing below the platform."},
        ]
        original_chat_completion = pipeline_module.chat_completion

        def completion(messages, **kwargs):
            calls.append(kwargs.get("stage"))
            request = json.loads(messages[-1]["content"])
            planned_events = request["scene"]["planned_events"]
            scene_index = request["scene"]["index"]
            return json.dumps(
                {
                    "prose": (
                        f"Scene {scene_index} advances the station mission "
                        f"with unique marker {scene_index}."
                    ),
                    "events": planned_events,
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                    "continuity_note": f"Structured scene {scene_index}.",
                }
            )

        pipeline_module.chat_completion = completion
        try:
            pipeline = run_chapter_pipeline(
                "StoryProject input pack.",
                chapter_index=3,
                dry_run=False,
                chapter_blueprint=self._blueprint(),
                recovered_scene_drafts=recovered,
            )
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual(["chapter_generation"] * 3, calls)
        self.assertEqual(3, len(pipeline["scene_drafts"]))
        self.assertNotEqual(recovered[0]["text"], pipeline["scene_drafts"][0]["text"])
        self.assertTrue(
            all(draft.get("source_call_id") for draft in pipeline["scene_drafts"])
        )
        self.assertIn("unique marker 3", pipeline["merged_chapter"])

    def test_recovered_scene_prefix_stops_before_incomplete_delta_schema(self) -> None:
        complete = {
            "index": 1,
            "text": "The first scene has a complete structured boundary.",
            "events": [],
            "deltas": {
                "characters": [],
                "relationships": [],
                "rosters": [],
                "locations": [],
                "inventory": [],
                "counters": [],
            },
        }
        incomplete = {
            **complete,
            "index": 2,
            "text": "The second scene omits one required delta collection.",
            "deltas": {
                key: value
                for key, value in complete["deltas"].items()
                if key != "counters"
            },
        }

        recovered = pipeline_module._recovered_scene_prefix(
            [complete, incomplete],
            {"scenes": [{"index": 1}, {"index": 2}]},
        )

        self.assertEqual([1], list(recovered))

    def test_story_project_generation_blocks_missing_ending_pressure(self) -> None:
        blueprint = self._blueprint()
        blueprint["ending_pressure"] = None
        blueprint["missing_fields"] = ["ending_pressure"]

        with self.assertRaisesRegex(ValueError, "ending_pressure"):
            run_chapter_pipeline(
                "StoryProject input pack.",
                chapter_index=3,
                dry_run=True,
                chapter_blueprint=blueprint,
            )

    def test_story_project_missing_coverage_can_be_validated(self) -> None:
        blueprint = self._blueprint()
        validation = validate_blueprint_coverage(
            blueprint,
            {
                "required_beat_count": 3,
                "covered_beat_indexes": [1, 2],
                "missing_beat_indexes": [3],
                "ending_pressure_required": True,
                "ending_pressure_covered": False,
            },
        )

        codes = [problem["code"] for problem in validation["problems"]]
        self.assertIn("missing_required_beat", codes)
        self.assertIn("missing_ending_pressure", codes)

    def test_model_plan_accepts_fenced_json_response(self) -> None:
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: """```json
{"goal": "Open the first rift.", "scenes": [{"index": 1, "type": "opening_bridge", "goal": "Begin at the observatory.", "required_beats": ["old observatory", "danger"]}]}
```"""
        try:
            plan = pipeline_module.plan_scenes("input pack", chapter_index=1, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("Open the first rift.", plan["goal"])
        self.assertEqual("opening_bridge", plan["scenes"][0]["type"])

    def test_model_plan_accepts_json_embedded_in_text(self) -> None:
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: (
            "Here is the plan:\n"
            '{"goal": "Enter the mirror waste.", "scenes": [{"index": 1, "goal": "Start from the last state.", "required_beats": ["bridge"]}]}'
        )
        try:
            plan = pipeline_module.plan_scenes("input pack", chapter_index=1, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("Enter the mirror waste.", plan["goal"])

    def test_model_plan_repairs_invalid_json_once(self) -> None:
        calls: list[tuple[list[dict[str, str]], dict]] = []
        outputs = [
            "goal: Open the first rift\nscenes: opening bridge",
            '{"goal": "Open the first rift.", "scenes": [{"index": 1, "type": "opening_bridge", "goal": "Begin at the observatory.", "required_beats": ["old observatory"]}]}',
        ]
        original_chat_completion = pipeline_module.chat_completion

        def completion(messages, **kwargs):
            calls.append((messages, kwargs))
            return outputs.pop(0)

        pipeline_module.chat_completion = completion
        try:
            plan = pipeline_module.plan_scenes("# Chapter Index\n3\n\ninput pack", chapter_index=3, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("Open the first rift.", plan["goal"])
        self.assertEqual(2, len(calls))
        self.assertEqual(0.0, calls[1][1]["temperature"])
        self.assertIn("invalid_response", calls[1][0][1]["content"])

    def test_model_plan_still_fails_when_json_repair_fails(self) -> None:
        outputs = ["not json", "still not json"]
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: outputs.pop(0)
        try:
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                pipeline_module.plan_scenes("input pack", chapter_index=1, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

    def test_scene_generation_respects_configured_chinese_language(self) -> None:
        original_chat_completion = pipeline_module.chat_completion

        def english_scene(messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            return json.dumps(
                {
                    "prose": "The ferry crossed the black water.",
                    "events": [
                        {
                            "event_id": request["required_event_ids"][0],
                            "type": "crossing",
                            "subjects": ["crew"],
                            "objects": ["ferry"],
                            "location": "black_water",
                            "status": "completed",
                        }
                    ],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                }
            )

        pipeline_module.chat_completion = english_scene
        try:
            with self.assertRaisesRegex(ModelOutputError, "Simplified Chinese"):
                pipeline_module.generate_scenes(
                    "input pack",
                    {
                        "goal": "continue",
                        "scenes": [{"index": 1, "goal": "continue", "required_beats": ["bridge"]}],
                    },
                    dry_run=False,
                    language="zh-CN",
                )
        finally:
            pipeline_module.chat_completion = original_chat_completion

    def test_scene_generation_uses_structured_scene_system_prompt(self) -> None:
        captured_system_prompts: list[str] = []
        original_chat_completion = pipeline_module.chat_completion

        def structured_scene(messages, **kwargs):
            captured_system_prompts.append(str(messages[0]["content"]))
            request = json.loads(messages[-1]["content"])
            return json.dumps(
                {
                    "prose": "陆沉沿着前一场留下的脚印继续推进。",
                    "events": request["scene"].get("planned_events") or [],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                },
                ensure_ascii=False,
            )

        pipeline_module.chat_completion = structured_scene
        try:
            drafts = pipeline_module.generate_scenes(
                "input pack",
                {
                    "goal": "continue",
                    "scenes": [{"index": 1, "goal": "continue", "required_beats": ["bridge"]}],
                },
                dry_run=False,
                language="zh-CN",
            )
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual(1, len(captured_system_prompts))
        self.assertIn("exactly one JSON object", captured_system_prompts[0])
        self.assertIn("never the whole chapter", captured_system_prompts[0])
        self.assertNotIn("Returns only chapter prose", captured_system_prompts[0])
        self.assertEqual(
            "openai-chapter_generation-scene-0001-primary",
            drafts[0]["source_call_id"],
        )

    def test_scene_boundary_relationship_rollback_regenerates_only_current_scene_once(
        self,
    ) -> None:
        calls: list[tuple[dict, dict]] = []
        established_boundary = (
            "双方完成战后双向验伤并共享临时避险区，但消防站保留水池和枪械控制权，"
            "组织未合并"
        )
        plan = {
            "goal": "完成临时合作并隔离危险核心",
            "scenes": [
                {
                    "index": 1,
                    "goal": "完成双向验伤",
                    "required_beats": ["完成双向验伤"],
                    "required_beat_indexes": [1],
                    "planned_events": [
                        {
                            "event_id": "chapter-0017-beat-008",
                            "type": "required_beat_8_completed",
                            "subjects": ["陆沉", "韩野"],
                            "objects": ["临时避险区"],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                },
                {
                    "index": 2,
                    "goal": "把危险核心转入隔离柜",
                    "required_beats": ["隔离危险核心"],
                    "required_beat_indexes": [2],
                    "planned_events": [
                        {
                            "event_id": "chapter-0017-beat-010",
                            "type": "required_beat_10_completed",
                            "subjects": ["陆沉", "韩野"],
                            "objects": ["R-17铅盒"],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                },
            ],
        }

        def completion(messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            calls.append((request, kwargs))
            event = request["scene"]["planned_events"][0]
            if len(calls) == 1:
                relationships = [
                    {
                        "source_id": "陆沉",
                        "target_id": "韩野",
                        "field": "合作边界",
                        "before": None,
                        "after": established_boundary,
                        "reason": "完成双向验伤",
                        "source_event_id": event["event_id"],
                    }
                ]
                prose = "陆沉与韩野完成双向验伤，划定临时避险区的合作边界。"
            elif len(calls) == 2:
                relationships = [
                    {
                        "source_id": "陆沉",
                        "target_id": "韩野",
                        "field": "合作边界",
                        "before": None,
                        "after": "危险核心进入独立隔离柜",
                        "reason": "隔离危险核心",
                        "source_event_id": event["event_id"],
                    }
                ]
                prose = "双方把危险核心送进隔离柜，却错误声明此前没有合作边界。"
            else:
                relationships = [
                    {
                        "source_id": "陆沉",
                        "target_id": "韩野",
                        "field": "合作边界",
                        "before": established_boundary,
                        "after": "危险核心进入独立隔离柜，双方仍不合并组织",
                        "reason": "隔离危险核心",
                        "source_event_id": event["event_id"],
                    }
                ]
                prose = "双方按既有合作边界把危险核心转入隔离柜，组织仍不合并。"
            return json.dumps(
                {
                    "prose": prose,
                    "events": [event],
                    "deltas": {
                        "characters": [],
                        "relationships": relationships,
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                    "continuity_note": "沿用上一场的合作边界。",
                },
                ensure_ascii=False,
            )

        with patch.object(pipeline_module, "chat_completion", side_effect=completion):
            drafts = pipeline_module.generate_scenes(
                "input pack",
                plan,
                dry_run=False,
                language="zh-CN",
            )

        self.assertEqual(3, len(calls))
        retry_request, retry_kwargs = calls[2]
        self.assertEqual(0.0, retry_kwargs["temperature"])
        self.assertEqual(
            "openai-chapter_generation-scene-0002-boundary-retry-01",
            retry_kwargs["call_id"],
        )
        self.assertEqual(
            established_boundary,
            retry_request["current_scene_state"]["relationships"]["陆沉->韩野"]["合作边界"],
        )
        self.assertEqual(
            established_boundary,
            retry_request["boundary_retry"]["findings"][0]["evidence"]["expected_before"],
        )
        self.assertEqual(
            "危险核心进入独立隔离柜，双方仍不合并组织",
            drafts[1]["deltas"]["relationships"][0]["after"],
        )
        self.assertEqual(1, drafts[1]["boundary_validation"]["local_regeneration_attempts"])

    def test_roster_prose_mismatch_regenerates_only_current_scene_once(self) -> None:
        plan = {
            "goal": "接纳五名幸存者并完成账本对账",
            "scenes": [
                {
                    "index": 1,
                    "goal": "接纳五名幸存者",
                    "required_beats": ["五名幸存者加入主队"],
                    "required_beat_indexes": [1],
                    "required_event_ids": ["chapter-0018-join-001"],
                    "forbidden_event_ids": [],
                    "planned_events": [
                        {
                            "event_id": "chapter-0018-join-001",
                            "type": "survivors_joined",
                            "subjects": ["hero"],
                            "objects": ["incoming-group"],
                            "location": "shelter",
                            "status": "completed",
                        }
                    ],
                }
            ],
        }
        initial_state = pipeline_module.empty_scene_state()
        initial_state["rosters"]["main"] = {
            "roster_id": "main",
            "members": [
                {"member_id": f"member-{index:02d}"}
                for index in range(1, 8)
            ],
            "computed_count": 7,
            "declared_count": 7,
        }
        joined = [
            {"member_id": f"member-{index:02d}"}
            for index in range(8, 13)
        ]
        calls: list[dict] = []

        def completion(messages, **_kwargs):
            request = json.loads(messages[-1]["content"])
            calls.append(request)
            event = request["scene"]["planned_events"][0]
            declared_in_prose = 11 if len(calls) == 1 else 12
            return json.dumps(
                {
                    "prose": (
                        "五名幸存者完成登记后加入主队，"
                        f"队伍现在共有{declared_in_prose}人。"
                    ),
                    "events": [event],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [
                            {
                                "roster_id": "main",
                                "operation": "join",
                                "member_ids": [
                                    member["member_id"]
                                    for member in joined
                                ],
                                "members": joined,
                                "delta": 5,
                                "declared_count": 12,
                                "reason_event_id": event["event_id"],
                            }
                        ],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                    "continuity_note": "账本与正文人数必须一致。",
                },
                ensure_ascii=False,
            )

        with patch.object(
            pipeline_module,
            "chat_completion",
            side_effect=completion,
        ):
            drafts = pipeline_module.generate_scenes(
                "input pack",
                plan,
                dry_run=False,
                language="zh-CN",
                initial_scene_state=initial_state,
            )

        self.assertEqual(2, len(calls))
        self.assertIn(
            "roster_count_mismatch",
            {
                finding["code"]
                for finding in calls[1]["boundary_retry"]["findings"]
            },
        )
        self.assertEqual(
            1,
            drafts[0]["boundary_validation"]["local_regeneration_attempts"],
        )
        self.assertIn("12人", drafts[0]["text"])

    def test_location_retry_contract_excludes_remote_participant_from_event_scope(
        self,
    ) -> None:
        authority = {
            "locations": {
                "陆沉": {"entity_id": "陆沉", "location_id": "二层通信室"},
                "韩野": {"entity_id": "韩野", "location_id": "二层通信室"},
            }
        }
        input_pack = "# Authoritative State\n" + json.dumps(
            authority,
            ensure_ascii=False,
        )
        plan = {
            "goal": "完成入站安排",
            "scenes": [
                {
                    "index": 1,
                    "goal": "陆沉下楼，韩野留守通信室",
                    "required_beats": ["完成入站安排"],
                    "required_beat_indexes": [1],
                    "planned_events": [
                        {
                            "event_id": "chapter-0017-beat-001",
                            "type": "required_beat_1_completed",
                            "subjects": [],
                            "objects": [],
                            "location": "",
                            "status": "completed",
                        }
                    ],
                }
            ],
        }
        calls: list[tuple[dict, dict]] = []

        def completion(messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            calls.append((request, kwargs))
            planned = request["scene"]["planned_events"][0]
            if len(calls) == 1:
                event = {
                    **planned,
                    "subjects": ["陆沉", "韩野"],
                    "location": "一层车库区与二层通信室",
                }
            else:
                self.assertIn(
                    "one exact, non-compound location",
                    request["boundary_retry"]["instruction"],
                )
                event = {
                    **planned,
                    "subjects": ["陆沉"],
                    "location": "一层车库区",
                }
            return json.dumps(
                {
                    "prose": "陆沉下到一层车库区安排入站，韩野仍在二层通信室远程确认。",
                    "events": [event],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [
                            {
                                "entity_id": "陆沉",
                                "before": "二层通信室",
                                "after": "一层车库区",
                                "reason": "下楼安排入站",
                            }
                        ],
                        "inventory": [],
                        "counters": [],
                    },
                    "continuity_note": "韩野远程参与但没有离开通信室。",
                },
                ensure_ascii=False,
            )

        with patch.object(pipeline_module, "chat_completion", side_effect=completion):
            drafts = pipeline_module.generate_scenes(
                input_pack,
                plan,
                dry_run=False,
                language="zh-CN",
            )

        self.assertEqual(2, len(calls))
        self.assertEqual(
            "openai-chapter_generation-scene-0001-boundary-retry-01",
            calls[1][1]["call_id"],
        )
        self.assertEqual(["陆沉"], drafts[0]["events"][0]["subjects"])
        self.assertEqual(
            "一层车库区",
            drafts[0]["scene_state_after"]["locations"]["陆沉"],
        )
        self.assertEqual(
            "二层通信室",
            drafts[0]["scene_state_after"]["locations"]["韩野"],
        )
        self.assertTrue(drafts[0]["boundary_validation"]["accepted"])

    def test_scene_boundary_failure_raises_after_one_local_regeneration(self) -> None:
        calls = 0
        authority = {
            "relationships": {
                "rel_lu_han": {
                    "source_character_id": "陆沉",
                    "target_character_id": "韩野",
                    "合作边界": "现有合作边界",
                }
            }
        }
        input_pack = "# Authoritative State\n" + json.dumps(authority, ensure_ascii=False)
        plan = {
            "goal": "继续合作",
            "scenes": [
                {
                    "index": 1,
                    "goal": "更新合作边界",
                    "required_beats": ["更新合作边界"],
                    "required_beat_indexes": [1],
                    "planned_events": [
                        {
                            "event_id": "chapter-0017-beat-010",
                            "type": "required_beat_10_completed",
                            "subjects": ["陆沉", "韩野"],
                            "objects": [],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                }
            ],
        }

        def stale_completion(messages, **kwargs):
            nonlocal calls
            calls += 1
            request = json.loads(messages[-1]["content"])
            return json.dumps(
                {
                    "prose": "双方尝试更新合作边界，但仍提交了过期的起始状态。",
                    "events": [request["scene"]["planned_events"][0]],
                    "deltas": {
                        "characters": [],
                        "relationships": [
                            {
                                "source_id": "陆沉",
                                "target_id": "韩野",
                                "field": "合作边界",
                                "before": None,
                                "after": "新的合作边界",
                                "reason": "继续合作",
                                "source_event_id": request["scene"]["planned_events"][0][
                                    "event_id"
                                ],
                            }
                        ],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                },
                ensure_ascii=False,
            )

        with patch.object(pipeline_module, "chat_completion", side_effect=stale_completion):
            with self.assertRaisesRegex(
                ValueError,
                "relationship_state_rollback",
            ):
                pipeline_module.generate_scenes(
                    input_pack,
                    plan,
                    dry_run=False,
                    language="zh-CN",
                )

        self.assertEqual(2, calls)

    def test_recovered_invalid_scene_regenerates_without_recalling_valid_prefix(
        self,
    ) -> None:
        established_boundary = "既有合作边界"
        plan = {
            "goal": "恢复后继续隔离危险核心",
            "scenes": [
                {
                    "index": 1,
                    "goal": "建立合作边界",
                    "required_beats": ["建立合作边界"],
                    "required_beat_indexes": [1],
                    "planned_events": [
                        {
                            "event_id": "chapter-0017-beat-008",
                            "type": "required_beat_8_completed",
                            "subjects": ["陆沉", "韩野"],
                            "objects": [],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                },
                {
                    "index": 2,
                    "goal": "隔离危险核心",
                    "required_beats": ["隔离危险核心"],
                    "required_beat_indexes": [2],
                    "planned_events": [
                        {
                            "event_id": "chapter-0017-beat-010",
                            "type": "required_beat_10_completed",
                            "subjects": ["陆沉", "韩野"],
                            "objects": ["R-17铅盒"],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                },
            ],
        }
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        recovered = [
            {
                "index": 1,
                "text": "陆沉与韩野完成验伤，建立了清晰的临时合作边界。",
                "events": [plan["scenes"][0]["planned_events"][0]],
                "deltas": {
                    **empty_deltas,
                    "relationships": [
                        {
                            "source_id": "陆沉",
                            "target_id": "韩野",
                            "field": "合作边界",
                            "before": None,
                            "after": established_boundary,
                            "reason": "完成验伤",
                            "source_event_id": plan["scenes"][0]["planned_events"][0][
                                "event_id"
                            ],
                        }
                    ],
                },
            },
            {
                "index": 2,
                "text": "双方准备隔离危险核心，但候选状态仍从空值开始。",
                "events": [plan["scenes"][1]["planned_events"][0]],
                "deltas": {
                    **empty_deltas,
                    "relationships": [
                        {
                            "source_id": "陆沉",
                            "target_id": "韩野",
                            "field": "合作边界",
                            "before": None,
                            "after": "核心已经隔离",
                            "reason": "隔离危险核心",
                            "source_event_id": plan["scenes"][1]["planned_events"][0][
                                "event_id"
                            ],
                        }
                    ],
                },
            },
        ]
        calls: list[tuple[dict, dict]] = []

        def repair_recovered_scene(messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            calls.append((request, kwargs))
            return json.dumps(
                {
                    "prose": "双方沿用既有合作边界，把危险核心转入独立隔离柜。",
                    "events": [request["scene"]["planned_events"][0]],
                    "deltas": {
                        **empty_deltas,
                        "relationships": [
                            {
                                "source_id": "陆沉",
                                "target_id": "韩野",
                                "field": "合作边界",
                                "before": established_boundary,
                                "after": "核心已经隔离",
                                "reason": "隔离危险核心",
                                "source_event_id": request["scene"]["planned_events"][0][
                                    "event_id"
                                ],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )

        with patch.object(
            pipeline_module,
            "chat_completion",
            side_effect=repair_recovered_scene,
        ):
            drafts = pipeline_module.generate_scenes(
                "input pack",
                plan,
                dry_run=False,
                language="zh-CN",
                recovered_scene_drafts=recovered,
            )

        self.assertEqual(1, len(calls))
        self.assertEqual(
            "openai-chapter_generation-scene-0002-boundary-retry-01",
            calls[0][1]["call_id"],
        )
        self.assertEqual(recovered[0]["text"], drafts[0]["text"])
        self.assertEqual("核心已经隔离", drafts[1]["deltas"]["relationships"][0]["after"])

    def test_live_plain_prose_scene_is_rejected_instead_of_synthesizing_events(
        self,
    ) -> None:
        calls = 0
        original_chat_completion = pipeline_module.chat_completion

        def plain_prose(messages, **kwargs):
            nonlocal calls
            calls += 1
            return "The hero waits outside and does not perform the rescue."

        pipeline_module.chat_completion = plain_prose
        try:
            with self.assertRaisesRegex(
                ValueError,
                "structured JSON",
            ):
                pipeline_module.generate_scenes(
                    "input pack",
                    pipeline_module.plan_scenes("input pack", chapter_index=1, dry_run=True),
                    dry_run=False,
                    language="zh-CN",
                )
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
