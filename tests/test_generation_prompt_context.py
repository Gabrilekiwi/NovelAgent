from __future__ import annotations

import json
import unittest

from core.prompt_compiler import compile_prompt_contexts
from core.state.authoritative import empty_authoritative_state
from core.state.generation_state_view import build_generation_state_view
from core.structured_context import sha256_text


class GenerationPromptContextTest(unittest.TestCase):
    def test_explicit_generation_view_replaces_raw_state_and_timeline(self) -> None:
        authority = empty_authoritative_state()
        authority["characters"] = {
            "陆沉": {
                "character_id": "陆沉",
                "canonical_name": "陆沉",
            }
        }
        authority["locations"] = {
            "陆沉": {
                "entity_id": "陆沉",
                "location_id": "消防站一层车库区",
                "certainty": "confirmed",
                "status": "current",
            }
        }
        read_set = {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 18,
            "required_state_item_ids": [
                "characters/陆沉",
                "locations/陆沉",
            ],
            "required_event_item_ids": [],
            "continuity": {
                "last_scene_location": "消防站一层车库区",
                "last_scene_character_ids": ["陆沉"],
                "required_opening_bridge": "消防站一层车库区",
            },
            "narrative_constraints": [],
            "expected_new_entities": [],
            "source_outline_sha256": "a" * 64,
        }
        read_set["contract_sha256"] = sha256_text(
            json.dumps(
                read_set,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        generation_view = build_generation_state_view(authority, read_set)
        raw_authority_marker = "RAW-AUTHORITY-MUST-STAY-LOCAL"
        raw_timeline_marker = "RAW-TIMELINE-MUST-STAY-LOCAL"
        raw_character_marker = "RAW-CHARACTER-MUST-STAY-LOCAL"
        input_pack = "\n\n".join(
            (
                "# Project Profile\n{}",
                "# Director Decision\n{}",
                "# Story State\n{}",
                "# Spatial State\n{}",
                "# Generation State View\n"
                + json.dumps(generation_view, ensure_ascii=False),
                "# Authoritative State\n"
                + json.dumps(
                    {
                        "schema_version": "1.0",
                        "characters": {
                            "secret": {"audit": raw_authority_marker}
                        },
                        "relationships": {},
                        "roster": {},
                        "numeric_counters": {},
                        "inventory": {},
                        "locations": {},
                        "events": {},
                    }
                ),
                "# Characters\n"
                + json.dumps({"secret": raw_character_marker}),
                "# Timeline\n"
                + json.dumps([{"detail": raw_timeline_marker}]),
                "# StoryProject Chapter Blueprint\n"
                + json.dumps(
                    {
                        "chapter_blueprint": {
                            "chapter_index": 18,
                            "required_beats": [{"index": 1, "text": "继续"}],
                        },
                        "read_set_context_digest": "d" * 64,
                    },
                    ensure_ascii=False,
                ),
                "# Requirements\nPreserve the explicit generation state view.",
            )
        )

        bundle = compile_prompt_contexts(input_pack)

        for stage in (bundle.plan, bundle.scene, bundle.repair):
            with self.subTest(stage=stage.report["stage"]):
                self.assertIn("# Generation State View", stage.text)
                self.assertIn("消防站一层车库区", stage.text)
                self.assertNotIn("chapter_context_read_set", stage.text)
                self.assertNotIn("# Authoritative State", stage.text)
                self.assertNotIn(raw_authority_marker, stage.text)
                self.assertNotIn(raw_timeline_marker, stage.text)
                self.assertNotIn(raw_character_marker, stage.text)
        self.assertIn(
            "# Generation State View Projection",
            bundle.plan.text,
        )
        self.assertIn(
            "# Generation State View Reference",
            bundle.scene.text,
        )


if __name__ == "__main__":
    unittest.main()
