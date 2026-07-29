from __future__ import annotations

import json
import unittest

from core.scene_continuity import (
    SceneBoundaryValidationError,
    empty_scene_state,
    require_scene_transition,
    validate_scene_transition,
)
import modules.chapter_generator.pipeline as pipeline_module


def _empty_deltas() -> dict[str, list[dict]]:
    return {
        "characters": [],
        "relationships": [],
        "rosters": [],
        "locations": [],
        "inventory": [],
        "counters": [],
    }


class SceneContinuityTests(unittest.TestCase):
    def test_plan_assigns_unique_event_scope_and_forbids_prior_ids(self) -> None:
        plan = pipeline_module.plan_scenes(
            "input",
            chapter_index=7,
            dry_run=True,
        )

        seen: list[str] = []
        for scene in plan["scenes"]:
            required = scene["required_event_ids"]
            self.assertTrue(required)
            self.assertEqual(seen, scene["forbidden_event_ids"])
            self.assertEqual(
                required,
                [event["event_id"] for event in scene["planned_events"]],
            )
            seen.extend(required)
        self.assertEqual(len(seen), len(set(seen)))

    def test_scene_generation_receives_previous_tail_events_and_current_state(self) -> None:
        plan = pipeline_module.plan_scenes("input", chapter_index=2, dry_run=True)
        requests: list[dict] = []
        original = pipeline_module.chat_completion

        def completion(messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            requests.append(request)
            scene_index = request["scene"]["index"]
            return json.dumps(
                {
                    "prose": f"Scene {scene_index} advances once and ends at marker-{scene_index}.",
                    "events": [
                        {
                            **planned,
                            "subjects": ["crew"],
                            "objects": [f"objective-{scene_index}-{offset}"],
                            "location": f"zone-{scene_index}",
                        }
                        for offset, planned in enumerate(
                            request["scene"]["planned_events"],
                            start=1,
                        )
                    ],
                    "deltas": _empty_deltas(),
                    "continuity_note": f"continued from scene {scene_index - 1}",
                }
            )

        pipeline_module.chat_completion = completion
        try:
            drafts = pipeline_module.generate_scenes(
                "input",
                plan,
                dry_run=False,
            )
        finally:
            pipeline_module.chat_completion = original

        self.assertEqual(3, len(drafts))
        self.assertEqual("", requests[0]["previous_scene_tail"])
        self.assertIn("marker-1", requests[1]["previous_scene_tail"])
        first_event = drafts[0]["events"][0]["event_id"]
        self.assertIn(
            first_event,
            requests[1]["current_scene_state"]["completed_event_ids"],
        )
        self.assertEqual([1], [item["index"] for item in requests[1]["prior_scene_summaries"]])
        self.assertTrue(all(item["boundary_validation"]["accepted"] for item in drafts))

    def test_second_scene_repeating_completed_event_id_is_regenerated_once(self) -> None:
        plan = pipeline_module.plan_scenes("input", chapter_index=2, dry_run=True)
        calls = 0
        requests: list[dict] = []
        first_event_id = plan["scenes"][0]["required_event_ids"][0]
        original = pipeline_module.chat_completion

        def completion(messages, **kwargs):
            nonlocal calls
            calls += 1
            request = json.loads(messages[-1]["content"])
            requests.append(request)
            planned_events = request["scene"]["planned_events"]
            return json.dumps(
                {
                    "prose": f"Scene {calls} prose remains distinct.",
                    "events": [
                        {
                            **planned,
                            "event_id": (
                                first_event_id
                                if calls == 2 and offset == 1
                                else planned["event_id"]
                            ),
                            "subjects": ["crew"],
                            "objects": [f"objective-{calls}-{offset}"],
                            "location": f"zone-{calls}",
                        }
                        for offset, planned in enumerate(planned_events, start=1)
                    ],
                    "deltas": _empty_deltas(),
                }
            )

        pipeline_module.chat_completion = completion
        try:
            drafts = pipeline_module.generate_scenes("input", plan, dry_run=False)
        finally:
            pipeline_module.chat_completion = original

        retry_findings = requests[2]["boundary_retry"]["findings"]
        codes = {item["code"] for item in retry_findings}
        self.assertEqual(4, calls)
        self.assertEqual(3, len(drafts))
        self.assertEqual(2, requests[2]["scene"]["index"])
        self.assertIn("repeated_completed_event_id", codes)
        self.assertIn("missing_planned_event", codes)
        self.assertEqual(
            1,
            drafts[1]["boundary_validation"]["local_regeneration_attempts"],
        )

    def test_new_event_id_cannot_hide_semantic_event_repetition(self) -> None:
        state = empty_scene_state()
        first = {
            "event_id": "scene-1-rescue",
            "type": "rescue_completed",
            "subjects": ["hero"],
            "objects": ["survivor_group"],
            "location": "service_tunnel",
            "status": "completed",
        }
        first_report, after_first = validate_scene_transition(
            scene_index=1,
            state_before=state,
            events=[first],
            deltas=_empty_deltas(),
            required_event_ids=["scene-1-rescue"],
        )
        second = {**first, "event_id": "scene-2-rescue-again"}

        second_report, _after_second = validate_scene_transition(
            scene_index=2,
            state_before=after_first,
            events=[second],
            deltas=_empty_deltas(),
            required_event_ids=["scene-2-rescue-again"],
        )

        self.assertTrue(first_report["accepted"])
        self.assertFalse(second_report["accepted"])
        self.assertIn(
            "duplicate_scene_event",
            {item["code"] for item in second_report["findings"]},
        )

    def test_location_and_counter_transitions_reject_stale_or_impossible_before_state(self) -> None:
        state = empty_scene_state()
        state["locations"]["hero"] = "gate"
        state["counters"]["survivors"] = 12
        report, _after = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[],
            deltas={
                **_empty_deltas(),
                "locations": [
                    {
                        "entity_id": "hero",
                        "before": "tunnel",
                        "after": "shelter",
                        "reason": "walked",
                    }
                ],
                "counters": [
                    {
                        "counter_id": "survivors",
                        "before": 10,
                        "delta": 1,
                        "after": 15,
                        "reason": "rescued one",
                    }
                ],
            },
        )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["accepted"])
        self.assertIn("location_state_rollback", codes)
        self.assertIn("counter_state_rollback", codes)
        self.assertIn("counters_delta_arithmetic_mismatch", codes)

    def test_valid_state_transition_advances_location_inventory_and_counter(self) -> None:
        state = empty_scene_state()
        state["locations"]["hero"] = "gate"
        state["inventories"]["hero:serum"] = 2
        state["counters"]["survivors"] = 12
        report, after = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[],
            deltas={
                **_empty_deltas(),
                "locations": [
                    {
                        "entity_id": "hero",
                        "before": "gate",
                        "after": "shelter",
                        "reason": "walked",
                    }
                ],
                "inventory": [
                    {
                        "owner_id": "hero",
                        "item_id": "serum",
                        "before": 2,
                        "delta": -1,
                        "after": 1,
                        "reason": "used one dose",
                    }
                ],
                "counters": [
                    {
                        "counter_id": "survivors",
                        "before": 12,
                        "delta": 1,
                        "after": 13,
                        "reason": "rescued one",
                    }
                ],
            },
        )

        self.assertTrue(report["accepted"])
        self.assertEqual("shelter", after["locations"]["hero"])
        self.assertEqual(1, after["inventories"]["hero:serum"])
        self.assertEqual(13, after["counters"]["survivors"])

    def test_event_location_requires_matching_entity_transition(self) -> None:
        state = empty_scene_state()
        state["locations"]["hero"] = "gate"
        event = {
            "event_id": "scene-2-arrival",
            "type": "arrival_completed",
            "subjects": ["hero"],
            "objects": [],
            "location": "shelter",
            "status": "completed",
        }

        rejected, _ = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[event],
            deltas=_empty_deltas(),
            required_event_ids=["scene-2-arrival"],
            planned_events=[event],
        )
        accepted, after = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[event],
            deltas={
                **_empty_deltas(),
                "locations": [
                    {
                        "entity_id": "hero",
                        "before": "gate",
                        "after": "shelter",
                        "reason": "crossed the gate",
                    }
                ],
            },
            required_event_ids=["scene-2-arrival"],
            planned_events=[event],
        )

        self.assertFalse(rejected["accepted"])
        self.assertIn(
            "scene_boundary_state_mismatch",
            {item["code"] for item in rejected["findings"]},
        )
        self.assertTrue(accepted["accepted"])
        self.assertEqual("shelter", after["locations"]["hero"])
        self.assertEqual("shelter", after["current_location"])
        self.assertEqual(["hero"], after["characters_present"])

    def test_old_semantic_signature_may_recur_but_old_event_id_may_not(self) -> None:
        state = empty_scene_state()
        oldest = {
            "event_id": "chapter-0001-patrol",
            "type": "patrol_completed",
            "subjects": ["hero"],
            "objects": ["north-gate"],
            "location": "station",
            "status": "completed",
        }
        state["completed_events"] = [
            oldest,
            *[
                {
                    "event_id": f"chapter-0002-checkpoint-{index:02d}",
                    "type": "checkpoint_completed",
                    "subjects": ["hero"],
                    "objects": [f"checkpoint-{index:02d}"],
                    "location": "station",
                    "status": "completed",
                }
                for index in range(24)
            ],
        ]
        state["completed_event_ids"] = [
            event["event_id"]
            for event in state["completed_events"]
        ]
        new_instance = {
            **oldest,
            "event_id": "chapter-0018-patrol",
        }

        accepted, _ = validate_scene_transition(
            scene_index=1,
            state_before=state,
            events=[new_instance],
            deltas=_empty_deltas(),
            required_event_ids=[new_instance["event_id"]],
            planned_events=[new_instance],
        )
        rejected, _ = validate_scene_transition(
            scene_index=1,
            state_before=state,
            events=[oldest],
            deltas=_empty_deltas(),
            required_event_ids=[oldest["event_id"]],
            planned_events=[oldest],
        )

        self.assertTrue(accepted["accepted"], accepted["findings"])
        self.assertFalse(rejected["accepted"])
        self.assertIn(
            "repeated_completed_event_id",
            {item["code"] for item in rejected["findings"]},
        )

    def test_open_action_cannot_restart_under_a_new_event_id(self) -> None:
        state = empty_scene_state()
        first = {
            "event_id": "door-opening-1",
            "type": "door_opening",
            "subjects": ["hero"],
            "objects": ["blast-door"],
            "location": "gate",
            "status": "started",
        }
        first_report, after_first = validate_scene_transition(
            scene_index=1,
            state_before=state,
            events=[first],
            deltas=_empty_deltas(),
            required_event_ids=["door-opening-1"],
        )
        repeated = {
            **first,
            "event_id": "door-opening-2",
            "status": "ongoing",
        }
        repeated_report, _ = validate_scene_transition(
            scene_index=2,
            state_before=after_first,
            events=[repeated],
            deltas=_empty_deltas(),
            required_event_ids=["door-opening-2"],
        )

        self.assertTrue(first_report["accepted"])
        self.assertEqual("door-opening-1", after_first["open_action"])
        self.assertFalse(repeated_report["accepted"])
        self.assertIn(
            "open_action_restarted",
            {item["code"] for item in repeated_report["findings"]},
        )

    def test_relationship_delta_requires_current_scene_event_reference(self) -> None:
        event = {
            "event_id": "scene-1-alliance",
            "type": "alliance_updated",
            "subjects": ["hero", "ally"],
            "objects": [],
            "location": "gate",
            "status": "completed",
        }
        missing, _ = validate_scene_transition(
            scene_index=1,
            state_before=empty_scene_state(),
            events=[event],
            deltas={
                **_empty_deltas(),
                "relationships": [
                    {
                        "source_id": "hero",
                        "target_id": "ally",
                        "field": "status",
                        "before": None,
                        "after": "active",
                    }
                ],
            },
            required_event_ids=[event["event_id"]],
        )
        valid, after = validate_scene_transition(
            scene_index=1,
            state_before=empty_scene_state(),
            events=[event],
            deltas={
                **_empty_deltas(),
                "relationships": [
                    {
                        "source_id": "hero",
                        "target_id": "ally",
                        "field": "status",
                        "before": None,
                        "after": "active",
                        "source_event_id": event["event_id"],
                    }
                ],
            },
            required_event_ids=[event["event_id"]],
        )

        self.assertFalse(missing["accepted"])
        self.assertIn(
            "missing_authority_event_reference",
            {item["code"] for item in missing["findings"]},
        )
        self.assertTrue(valid["accepted"], valid["findings"])
        self.assertEqual(
            "active",
            after["relationships"]["hero->ally"]["status"],
        )

    def test_relationship_delta_cannot_reuse_prior_scene_event(self) -> None:
        state = empty_scene_state()
        state["completed_event_ids"] = ["scene-1-alliance"]
        state["completed_events"] = [
            {
                "event_id": "scene-1-alliance",
                "type": "alliance_updated",
                "subjects": ["hero", "ally"],
                "objects": [],
                "location": "gate",
                "status": "completed",
            }
        ]
        current = {
            "event_id": "scene-2-move",
            "type": "movement_completed",
            "subjects": ["hero", "ally"],
            "objects": [],
            "location": "shelter",
            "status": "completed",
        }

        report, _ = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[current],
            deltas={
                **_empty_deltas(),
                "relationships": [
                    {
                        "source_id": "hero",
                        "target_id": "ally",
                        "field": "status",
                        "before": None,
                        "after": "strained",
                        "source_event_id": "scene-1-alliance",
                    }
                ],
            },
            required_event_ids=[current["event_id"]],
        )

        self.assertFalse(report["accepted"])
        self.assertIn(
            "invalid_authority_event_reference",
            {item["code"] for item in report["findings"]},
        )

    def test_explicit_roster_prose_count_must_match_scene_ledger(self) -> None:
        state = empty_scene_state()
        state["rosters"]["main"] = {
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
        event = {
            "event_id": "scene-2-join",
            "type": "survivors_joined",
            "subjects": ["hero"],
            "objects": [member["member_id"] for member in joined],
            "location": "shelter",
            "status": "completed",
        }
        deltas = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": [member["member_id"] for member in joined],
                    "members": joined,
                    "delta": 5,
                    "declared_count": 12,
                    "reason_event_id": event["event_id"],
                }
            ],
        }

        rejected, _ = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[event],
            deltas=deltas,
            prose="五名幸存者加入后，队伍现在共有十一人。",
            required_event_ids=[event["event_id"]],
        )
        accepted, after = validate_scene_transition(
            scene_index=2,
            state_before=state,
            events=[event],
            deltas=deltas,
            prose="原先队伍共有七人，五名幸存者加入后，队伍现在共有十二人。",
            required_event_ids=[event["event_id"]],
        )

        self.assertFalse(rejected["accepted"])
        self.assertIn(
            "roster_count_mismatch",
            {item["code"] for item in rejected["findings"]},
        )
        self.assertTrue(accepted["accepted"], accepted["findings"])
        self.assertEqual(12, after["rosters"]["main"]["computed_count"])

    def test_cross_chapter_required_beat_bookkeeping_is_not_a_duplicate_event(
        self,
    ) -> None:
        state = empty_scene_state()
        prior = {
            "event_id": "chapter-0001-beat-001",
            "type": "required_beat_1_completed",
            "subjects": ["hero"],
            "objects": ["gate"],
            "location": "shelter",
            "status": "completed",
        }
        state["completed_event_ids"] = [prior["event_id"]]
        state["completed_events"] = [prior]
        current = {
            **prior,
            "event_id": "chapter-0002-beat-001",
        }

        report, _ = validate_scene_transition(
            scene_index=1,
            state_before=state,
            events=[current],
            deltas=_empty_deltas(),
            required_event_ids=[current["event_id"]],
            planned_events=[current],
        )

        self.assertTrue(report["accepted"], report["findings"])

    def test_invalid_generic_delta_error_aggregates_codes_and_includes_evidence(self) -> None:
        report, _after = validate_scene_transition(
            scene_index=1,
            state_before=empty_scene_state(),
            events=[],
            deltas={
                **_empty_deltas(),
                "characters": [
                    {
                        "stable_entity_id": "陆沉",
                        "before": None,
                        "after": {"status": "active"},
                    },
                    {
                        "stable_entity_id": "苏晴",
                        "before": None,
                        "after": {"status": "active"},
                    },
                ],
                "rosters": [
                    {
                        "stable_entity_id": "火种一号",
                        "before": None,
                        "after": {"total_count": 17},
                    }
                ],
                "locations": [
                    {
                        "stable_entity_id": "陆沉",
                        "before": "冷库",
                        "after": "人防医院地下病区",
                    }
                ],
                "inventory": [
                    {
                        "stable_entity_id": "R-17核心碎片",
                        "before": None,
                        "after": {"container": "铅板药械柜"},
                    }
                ],
                "counters": [
                    {
                        "stable_entity_id": "陆沉_侵蚀值",
                        "before": None,
                        "after": "6/100",
                    }
                ],
            },
        )

        with self.assertRaises(SceneBoundaryValidationError) as raised:
            require_scene_transition(report)

        message = str(raised.exception)
        self.assertIn("invalid_character_delta x2", message)
        self.assertIn("invalid_roster_delta", message)
        self.assertIn("invalid_location_delta", message)
        self.assertIn("invalid_inventories_delta", message)
        self.assertIn("invalid_counters_delta", message)
        self.assertIn("Character delta is incomplete", message)
        self.assertIn('"stable_entity_id":"陆沉"', message)


if __name__ == "__main__":
    unittest.main()
