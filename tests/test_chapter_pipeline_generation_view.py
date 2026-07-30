from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from core.state.authoritative import empty_authoritative_state
from core.state.generation_state_view import (
    build_generation_state_view,
    generation_state_view_from_markdown,
    scene_generation_state_reference_from_markdown,
)
import modules.chapter_generator.pipeline as pipeline_module


_RAW_AUTHORITY_MARKER = "RAW-AUTHORITY-MUST-REMAIN-LOCAL"
_RAW_CHARACTER_MARKER = "RAW-CHARACTERS-MUST-REMAIN-LOCAL"
_RAW_TIMELINE_MARKER = "RAW-TIMELINE-MUST-REMAIN-LOCAL"
_RAW_WORLD_MARKER = "RAW-WORLD-MUST-REMAIN-LOCAL"
_RAW_MEMORY_MARKER = "RAW-MEMORY-MUST-REMAIN-LOCAL"
_STALE_CHARACTER_MARKER = "STALE-COLD-ROOM-GOAL-MUST-REMAIN-LOCAL"
_OLD_EVENT_ID = "chapter-0001-beat-001"


class _AlwaysFitsBudget:
    hard_input_limit = 32_000

    @staticmethod
    def measure(_text: str, *, stage: str, **_kwargs: object) -> dict:
        if stage != "scene":
            raise AssertionError(f"unexpected stage: {stage}")
        return {
            "within_budget": True,
            "budgeted_input_tokens": 1_000,
            "hard_input_limit": 32_000,
        }

    @staticmethod
    def require_input(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("small focused payload should fit")


def _authority() -> dict:
    state = empty_authoritative_state()
    state["characters"] = {
        "hero": {
            "character_id": "hero",
            "canonical_name": "Lu Chen",
            "current_goal": _STALE_CHARACTER_MARKER,
            "current_location": "cold-room",
            "last_observation": "legacy append log",
        },
        "noise-character": {
            "character_id": "noise-character",
            "canonical_name": "Unrelated",
            "private_note": _RAW_AUTHORITY_MARKER,
        },
    }
    state["locations"] = {
        "hero": {
            "entity_id": "hero",
            "location_id": "second-floor-radio-room",
            "certainty": "confirmed",
            "status": "current",
        },
        "noise-character": {
            "entity_id": "noise-character",
            "location_id": "unrelated-location",
            "certainty": "confirmed",
            "status": "current",
        },
    }
    state["events"] = {
        _OLD_EVENT_ID: {
            "event_id": _OLD_EVENT_ID,
            "type": "old_checkpoint",
            "subjects": ["hero"],
            "objects": ["red-record"],
            "location": "archive-room",
            "status": "completed",
            "detail": "The old record was recovered.",
        },
        **{
            f"chapter-0017-beat-{index:03d}": {
                "event_id": f"chapter-0017-beat-{index:03d}",
                "type": "recent_checkpoint",
                "subjects": ["noise-character"],
                "objects": [],
                "location": "unrelated-location",
                "status": "completed",
            }
            for index in range(1, 31)
        },
    }
    return state


def _generation_view(authority: dict) -> dict:
    return build_generation_state_view(
        authority,
        {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 18,
            "required_state_item_ids": [
                "characters/hero",
                "locations/hero",
            ],
            "required_event_item_ids": [f"events/{_OLD_EVENT_ID}"],
            "continuity": {
                "last_scene_location": "second-floor-radio-room",
                "last_scene_character_ids": ["hero"],
                "required_opening_bridge": "Continue the radio-room countdown.",
            },
            "narrative_constraints": [],
            "expected_new_entities": [
                {
                    "kind": "scene_local",
                    "entity_id": "seeker-18",
                    "display_name": "Signal Seeker",
                }
            ],
            "source_outline_sha256": "a" * 64,
        },
    )


def _input_pack(authority: dict, view: dict) -> str:
    return "\n\n".join(
        (
            "# Project Profile\n{}",
            "# Director Decision\n{}",
            "# Story State\n"
            + json.dumps(
                {
                    "last_scene_location": "second-floor-radio-room",
                    "last_scene_characters": ["hero"],
                    "required_opening_bridge": (
                        "Continue the radio-room countdown."
                    ),
                },
                ensure_ascii=False,
            ),
            "# Spatial State\n{}",
            "# Generation State View\n"
            + json.dumps(view, ensure_ascii=False, indent=2),
            "# Authoritative State\n"
            + json.dumps(authority, ensure_ascii=False, indent=2),
            "# Characters\n"
            + json.dumps({"private": _RAW_CHARACTER_MARKER}),
            "# Timeline\n"
            + json.dumps([{"private": _RAW_TIMELINE_MARKER}]),
            "# World State\n"
            + json.dumps({"private": _RAW_WORLD_MARKER}),
            "# Memory Index\n" + _RAW_MEMORY_MARKER,
            "# StoryProject Chapter Blueprint\n"
            + json.dumps(
                {
                    "chapter_blueprint": {
                        "chapter_index": 18,
                        "required_beats": [
                            {"index": 1, "text": "Continue the countdown."}
                        ],
                    },
                    "read_set_context_digest": view["read_set_digest"],
                },
                ensure_ascii=False,
            ),
            "# Requirements\nContinue without replaying completed events.",
        )
    )


def _plan() -> dict:
    scene = {
        "index": 1,
        "goal": "Continue the countdown.",
        "required_event_ids": ["chapter-0018-beat-001"],
        "forbidden_event_ids": [],
    }
    return {"goal": "Continue the countdown.", "scenes": [scene]}


class ChapterPipelineGenerationViewTests(unittest.TestCase):
    def test_explicit_view_filters_model_payload_and_keeps_full_local_state(
        self,
    ) -> None:
        authority = _authority()
        view = _generation_view(authority)
        input_pack = _input_pack(authority, view)
        local_state = pipeline_module._initial_scene_state(input_pack)
        original_local_state = copy.deepcopy(local_state)
        plan = _plan()
        blueprint = {
            "chapter_index": 18,
            "title": "Signal",
            "core_event": "Continue the countdown.",
            "human_conflict": "Transmit or remain silent.",
            "required_beats": [],
            "ending_pressure": "A second receiver wakes.",
            "chapter_context_read_set": view[
                "chapter_context_read_set"
            ],
            "source_path": "outline.md",
        }

        with (
            patch.object(
                pipeline_module,
                "compact_authoritative_state_in_markdown",
                side_effect=AssertionError(
                    "explicit view must not compact or render raw authority"
                ),
            ),
            patch.object(
                pipeline_module,
                "default_context_budget",
                return_value=_AlwaysFitsBudget(),
            ),
        ):
            first = pipeline_module._scene_request_payload(
                input_pack=input_pack,
                plan=plan,
                scene=plan["scenes"][0],
                scene_required_beats=[],
                blueprint=blueprint,
                scene_state=local_state,
                authoritative_state_source=authority,
            )
            second = pipeline_module._scene_request_payload(
                input_pack=input_pack,
                plan=plan,
                scene=plan["scenes"][0],
                scene_required_beats=[],
                blueprint=blueprint,
                scene_state=local_state,
                authoritative_state_source=authority,
            )

        self.assertEqual(first, second)
        self.assertEqual(original_local_state, local_state)
        self.assertIn("noise-character", local_state["characters"])
        self.assertEqual(
            _STALE_CHARACTER_MARKER,
            local_state["characters"]["hero"]["current_goal"],
        )
        self.assertEqual(31, len(local_state["completed_event_ids"]))

        payload = json.loads(first)
        current = payload["current_scene_state"]
        self.assertEqual({"hero"}, set(current["characters"]))
        self.assertNotIn("noise-character", current["locations"])
        self.assertEqual([_OLD_EVENT_ID], current["completed_event_ids"])
        self.assertEqual([], current["completed_events"])
        self.assertFalse(current["completed_event_ids_truncated"])
        self.assertEqual(
            view["expected_new_entities"],
            payload["expected_new_entities"],
        )
        self.assertEqual(
            view["projection_sha256"],
            payload["generation_state_projection_sha256"],
        )
        self.assertEqual(
            view["read_set_digest"],
            payload["generation_state_read_set_digest"],
        )
        self.assertEqual(
            {
                "chapter_index": 18,
                "core_event": "Continue the countdown.",
                "human_conflict": "Transmit or remain silent.",
                "title": "Signal",
            },
            payload["story_project_chapter_contract"],
        )

        shared_context = payload["shared_context"]
        self.assertIsNone(
            generation_state_view_from_markdown(shared_context)
        )
        reference = scene_generation_state_reference_from_markdown(
            shared_context
        )
        self.assertIsNotNone(reference)
        self.assertEqual(
            view["projection_sha256"],
            reference["source_generation_state_view_sha256"],
        )
        self.assertEqual(
            _OLD_EVENT_ID,
            next(iter(reference["required_events"])),
        )
        self.assertNotIn(
            "# StoryProject Chapter Blueprint",
            shared_context,
        )
        for duplicated_heading in (
            "Director Decision",
            "Story State",
            "Requirements",
        ):
            self.assertNotIn(
                f"# {duplicated_heading}",
                shared_context,
            )
        self.assertEqual(
            "second-floor-radio-room",
            reference["continuity"]["last_scene_location"],
        )
        for heading in (
            "Authoritative State",
            "Characters",
            "Timeline",
            "World State",
            "Memory Index",
        ):
            self.assertNotIn(f"# {heading}", shared_context)
        for marker in (
            _RAW_AUTHORITY_MARKER,
            _RAW_CHARACTER_MARKER,
            _RAW_TIMELINE_MARKER,
            _RAW_WORLD_MARKER,
            _RAW_MEMORY_MARKER,
            _STALE_CHARACTER_MARKER,
        ):
            self.assertNotIn(marker, first)

    def test_initial_state_can_restore_required_old_event_from_view_only(
        self,
    ) -> None:
        view = _generation_view(_authority())
        view_only = (
            "# Generation State View\n"
            + json.dumps(view, ensure_ascii=False, indent=2)
        )

        state = pipeline_module._initial_scene_state(view_only)

        self.assertEqual([_OLD_EVENT_ID], state["completed_event_ids"])
        self.assertEqual(
            [_OLD_EVENT_ID],
            [event["event_id"] for event in state["completed_events"]],
        )

    def test_legacy_scene_context_still_uses_authority_projection(self) -> None:
        authority = _authority()
        input_pack = (
            "# Authoritative State\n"
            + json.dumps(authority, ensure_ascii=False)
            + "\n\n# Requirements\nContinue."
        )
        original_compactor = (
            pipeline_module.compact_authoritative_state_in_markdown
        )

        with patch.object(
            pipeline_module,
            "compact_authoritative_state_in_markdown",
            wraps=original_compactor,
        ) as compact_authority:
            compact = pipeline_module._compact_scene_context(input_pack)

        compact_authority.assert_called_once()
        self.assertIn("# Authoritative State", compact)
        self.assertIsNotNone(
            pipeline_module.authoritative_state_from_markdown(compact)
        )


if __name__ == "__main__":
    unittest.main()
