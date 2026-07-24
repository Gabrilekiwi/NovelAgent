from __future__ import annotations

import hashlib
import json
import re
import unittest

from core.context_budget import ContextBudget
from core.prompt_compiler import compile_prompt_contexts
from core.structured_context import (
    StructuredContextError,
    compact_markdown_context,
    select_json_items,
    select_text_blocks,
)


def character_counter(text: str) -> int:
    return len(text)


character_counter.version = "structured-context-character-counter-v1"
character_counter.tokenizer = "character-counter"
character_counter.model_is_known = True


class StructuredContextTest(unittest.TestCase):
    def test_paragraph_selection_ranges_are_complete_and_hash_verified(self) -> None:
        source = "OPENING\n\nordinary history\n\ncritical lantern fact\n\nLATEST END"

        selection = select_text_blocks(
            source,
            max_chars=55,
            query="critical lantern",
            required="edges",
            policy="test_paragraphs_v1",
        )

        self.assertLessEqual(len(selection.text), 55)
        self.assertIn("OPENING", selection.text)
        self.assertIn("critical lantern fact", selection.text)
        self.assertIn("LATEST END", selection.text)
        self.assertEqual(hashlib.sha256(source.encode("utf-8")).hexdigest(), selection.source_sha256)
        for item in selection.selected_items:
            retained = source[item["start_char"]:item["end_char"]]
            self.assertEqual(item["original_chars"], len(retained))
            self.assertEqual(item["sha256"], hashlib.sha256(retained.encode("utf-8")).hexdigest())

    def test_oversized_required_json_item_fails_closed(self) -> None:
        with self.assertRaises(StructuredContextError) as raised:
            select_json_items(
                [{"id": "required", "value": "x" * 500}],
                max_chars=80,
                required_indexes={0},
            )

        self.assertEqual("required_json_item_exceeds_budget", raised.exception.code)

    def test_json_selection_drops_whole_items_and_remains_parseable(self) -> None:
        values = [
            {"chapter": index, "summary": f"chapter-{index}-" + ("event " * 20)}
            for index in range(100)
        ]

        selection = select_json_items(
            values,
            max_chars=800,
            query="chapter-99",
            prefer_recent=True,
        )
        rendered = json.dumps(list(selection.items), ensure_ascii=False)

        self.assertEqual(list(selection.items), json.loads(rendered))
        self.assertTrue(all(item in values for item in selection.items))
        self.assertGreater(selection.manifest["omitted_count"], 0)
        self.assertEqual(64, len(selection.manifest["source_sha256"]))

    def test_thousand_chapter_markdown_history_is_bounded_and_json_is_parseable(self) -> None:
        history = [
            {
                "chapter": index,
                "event_id": f"event-{index:04d}",
                "summary": f"complete chapter {index} event",
            }
            for index in range(1_000)
        ]
        source = "# Chapter History\n" + json.dumps(history, ensure_ascii=False, indent=2)

        selection = compact_markdown_context(
            source,
            max_chars=5_000,
            per_section_max_chars=4_000,
            query="event-0999",
            required_sections={"Chapter History"},
            policy="thousand_chapter_history_v1",
        )

        self.assertLessEqual(len(selection.text), 5_000)
        match = re.search(
            r"# Chapter History\n(.*?)\n\n# Structured Context Manifest\n",
            selection.text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        retained_history = json.loads(match.group(1))
        self.assertTrue(retained_history)
        self.assertTrue(all(item in history for item in retained_history))
        manifest = json.loads(selection.text.split("# Structured Context Manifest\n", 1)[1])
        self.assertEqual(len(source), manifest["original_chars"])
        self.assertEqual(64, len(manifest["source_sha256"]))

    def test_section_override_and_json_allowlist_remove_prompt_only_audit_fields(self) -> None:
        story_state = {
            "last_chapter_ending": "The alarm started.",
            "last_scene_location": "control room",
            "last_scene_characters": ["Mira"],
            "open_threads": ["Reach the generator."],
            "required_opening_bridge": "Continue from the control room.",
            "source": "tracking/character-state.md",
            "path": "D:/book/tracking/character-state.md",
            "text": "historical audit excerpt " * 80,
        }
        source = "# Story State\n" + json.dumps(story_state, ensure_ascii=False, indent=2)
        semantic_keys = {
            "last_chapter_ending",
            "last_scene_location",
            "last_scene_characters",
            "open_threads",
            "required_opening_bridge",
        }

        selection = compact_markdown_context(
            source,
            max_chars=5_000,
            per_section_max_chars=100,
            required_sections={"Story State"},
            required_json_keys={"Story State": semantic_keys},
            allowed_json_keys={"Story State": semantic_keys},
            section_max_chars={"Story State": 4_096},
        )

        body = selection.text.split("# Story State\n", 1)[1].split(
            "\n\n# Structured Context Manifest\n",
            1,
        )[0]
        retained = json.loads(body)
        self.assertEqual(semantic_keys, set(retained))
        self.assertEqual("The alarm started.", retained["last_chapter_ending"])
        self.assertNotIn("historical audit excerpt", selection.text)
        self.assertLessEqual(len("# Story State\n" + body), 4_096)

    def test_prompt_compiler_keeps_required_blueprint_json_parseable_with_thousand_chapter_history(self) -> None:
        history = [
            {"chapter": index, "summary": f"chapter {index} completed event " + ("detail " * 8)}
            for index in range(1_000)
        ]
        blueprint = {
            "chapter_blueprint": {
                "chapter_index": 1_001,
                "core_event": "resolve the current signal",
                "required_beats": [{"index": 1, "text": "decode the signal"}],
                "ending_pressure": "the timer reaches ten",
            },
            "read_set_context_digest": "a" * 64,
            "chapter_history": history,
        }
        input_pack = (
            "# Project Profile\n{}\n\n"
            "# Director Decision\n{\"goal\":\"decode the signal\"}\n\n"
            "# Story State\n{\"location\":\"station\"}\n\n"
            "# Spatial State\n{}\n\n"
            "# StoryProject Chapter Blueprint\n"
            + json.dumps(blueprint, ensure_ascii=False, indent=2)
            + "\n\n# Requirements\nReturn prose."
        )
        budget = ContextBudget(
            provider="test",
            model="structured",
            model_context_window=30_000,
            output_reserve_tokens=1_000,
            protocol_overhead_tokens=500,
            safety_margin_tokens=500,
            max_input_tokens=20_000,
        )

        bundle = compile_prompt_contexts(input_pack, budget=budget, exact_counter=character_counter)

        self.assertLessEqual(len(bundle.plan.text), budget.hard_input_limit)
        match = re.search(
            r"# StoryProject Chapter Blueprint\n(.*?)\n\n# Structured Context Manifest\n",
            bundle.plan.text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        retained_blueprint = json.loads(match.group(1))
        self.assertEqual(1_001, retained_blueprint["chapter_blueprint"]["chapter_index"])
        self.assertEqual("a" * 64, retained_blueprint["read_set_context_digest"])
        self.assertNotIn("chapter_history", retained_blueprint)
        self.assertEqual(hashlib.sha256(input_pack.encode("utf-8")).hexdigest(), bundle.context_digest)
        self.assertEqual(len(input_pack), bundle.plan.selection_manifest["original_chars"])


if __name__ == "__main__":
    unittest.main()
