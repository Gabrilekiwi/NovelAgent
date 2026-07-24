from __future__ import annotations

import json
import unittest

from core.scene_continuity import (
    SceneBoundaryValidationError,
    empty_scene_state,
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

    def test_second_scene_cannot_repeat_completed_event_id(self) -> None:
        plan = pipeline_module.plan_scenes("input", chapter_index=2, dry_run=True)
        calls = 0
        first_event_id = plan["scenes"][0]["required_event_ids"][0]
        original = pipeline_module.chat_completion

        def completion(messages, **kwargs):
            nonlocal calls
            calls += 1
            request = json.loads(messages[-1]["content"])
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
            with self.assertRaises(SceneBoundaryValidationError) as raised:
                pipeline_module.generate_scenes("input", plan, dry_run=False)
        finally:
            pipeline_module.chat_completion = original

        codes = {item["code"] for item in raised.exception.report["findings"]}
        self.assertEqual(2, calls)
        self.assertIn("repeated_completed_event_id", codes)
        self.assertIn("missing_planned_event", codes)

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


if __name__ == "__main__":
    unittest.main()
