from __future__ import annotations

import unittest
from pathlib import Path

from core.schema import validate_schema
from core.state.input_pack import build_input_pack, build_input_pack_metadata, build_snapshot_input_pack


class PromptAssetTest(unittest.TestCase):
    def test_snapshot_input_pack_uses_snapshot_prompt_and_memory_data(self) -> None:
        snapshot_pack = build_snapshot_input_pack(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "id": "memory-1",
                        "type": "location",
                        "name": "shelter",
                        "data": {"risk": "rising"},
                    }
                ],
            },
        )

        self.assertIn("You are NovelAgent's state builder.", snapshot_pack)
        self.assertIn("# Base Snapshot", snapshot_pack)
        self.assertIn("# Memory Context", snapshot_pack)
        self.assertIn('"risk": "rising"', snapshot_pack)

    def test_input_pack_uses_clean_generation_instructions(self) -> None:
        input_pack = build_input_pack(
            {
                "chapter_index": 2,
                "world_state": {},
                "story_state": {"last_scene_location": "shelter", "required_opening_bridge": "Continue from shelter"},
                "spatial_state": {"spaces": {"shelter": {}}, "connections": []},
                "characters": {},
                "timeline": [],
            },
            {"goal": "continue", "validation_focus": ["logic"]},
            {"source": "test", "status": "ready", "items": []},
        )

        self.assertIn("You are NovelAgent's chapter generation module.", input_pack)
        self.assertIn("# Story State", input_pack)
        self.assertIn("# Spatial State", input_pack)
        self.assertIn('"last_scene_location": "shelter"', input_pack)
        self.assertIn('"spaces"', input_pack)
        self.assertIn("# Requirements", input_pack)
        self.assertIn("Return only chapter prose", input_pack)
        self.assertNotIn("\ufffd", input_pack)

    def test_input_pack_uses_memory_index_not_full_memory_payload(self) -> None:
        snapshot = {
                "chapter_index": 2,
                "world_state": {"locations": {"shelter": {"risk": "rising"}}},
                "story_state": {
                    "last_chapter_ending": "The alarm rose.",
                    "last_scene_location": "shelter",
                    "last_scene_characters": ["Mira"],
                    "open_threads": ["Keep serum visible."],
                    "required_opening_bridge": "Continue from shelter",
                },
                "spatial_state": {
                    "spaces": {"shelter": {"risk": "rising"}},
                    "connections": [{"from": "shelter", "to": "sealed gate"}],
                    "character_positions": {"Mira": "shelter"},
                    "blocked_paths": [],
                    "last_transition": {},
                },
                "characters": {},
                "timeline": [],
                "constraints": [{"rule": "Keep serum visible."}],
                "memory": {"source": "test", "status": "ready", "item_count": 1},
            }
        decision = {
            "goal": "continue",
            "actions": ["generate_chapter", "validate"],
            "validation_focus": ["logic"],
            "max_repair_attempts": 1,
        }
        memory = {
                "source": "test",
                "status": "ready",
                "last_run": {
                    "id": "chapter_1_test",
                    "status": "rejected",
                    "committed": False,
                    "chapter_index": 1,
                    "goal": "recover_from_validation_failure",
                    "workflow": ["generate_chapter", "validate", "repair_if_needed"],
                    "problem_codes": ["missing_conflict_marker"],
                    "problem_count": 1,
                    "blocking_problem_count": 1,
                    "warning_count": 0,
                    "severity_counts": {"blocking": 1, "warning": 0},
                    "requested_focus": ["logic"],
                    "executed_checks": ["logic", "llm"],
                    "skipped_checks": ["continuity", "spatial"],
                    "repair_attempts": 1,
                    "repair_plan": {
                        "risk_level": "high",
                        "repair_budget": 2,
                        "attempt": 1,
                        "deterministic_step_count": 1,
                        "manual_review_count": 0,
                    },
                    "repair_deltas": [
                        {
                            "attempt": 1,
                            "before_problem_count": 1,
                            "after_problem_count": 1,
                            "resolved_problem_codes": [],
                            "new_problem_codes": [],
                            "remaining_problem_codes": ["missing_conflict_marker"],
                        }
                    ],
                },
                "source_mappings": [
                    {
                        "index": 0,
                        "source": "test",
                        "memory_id": "memory-1",
                        "type": "location",
                        "name": "shelter",
                        "path": "memory.json",
                    }
                ],
                "items": [
                    {
                        "id": "memory-1",
                        "type": "location",
                        "name": "shelter",
                        "data": {"large_note": "do not duplicate this payload"},
                    }
                ],
            }
        input_pack = build_input_pack(snapshot, decision, memory)

        self.assertIn("# Memory Index", input_pack)
        self.assertIn("# Recovery Context", input_pack)
        self.assertIn('"id": "memory-1"', input_pack)
        self.assertIn('"source_mappings"', input_pack)
        self.assertIn('"path": "memory.json"', input_pack)
        self.assertIn('"item_count": 1', input_pack)
        self.assertIn('"available": true', input_pack)
        self.assertIn('"source_run_id": "chapter_1_test"', input_pack)
        self.assertIn('"missing_conflict_marker"', input_pack)
        self.assertIn('"skipped_checks"', input_pack)
        self.assertIn('"continuity"', input_pack)
        self.assertIn("Keep serum visible.", input_pack)
        self.assertIn("# Story State", input_pack)
        self.assertIn("# Spatial State", input_pack)
        self.assertNotIn("do not duplicate this payload", input_pack)

        metadata = build_input_pack_metadata(input_pack, snapshot, decision, memory)
        self.assertIs(metadata, validate_schema(metadata, "input_pack_metadata.schema.json"))
        self.assertEqual("chapter_input_pack", metadata["kind"])
        self.assertEqual(len(input_pack), metadata["chars"])
        self.assertEqual(1, metadata["memory_index"]["indexed_item_count"])
        self.assertEqual(1, metadata["memory_index"]["source_mapping_count"])
        self.assertEqual(["last_chapter_ending", "last_scene_characters", "last_scene_location", "open_threads", "required_opening_bridge"], metadata["snapshot"]["story_state_keys"])
        self.assertEqual(1, metadata["snapshot"]["open_thread_count"])
        self.assertEqual(1, metadata["snapshot"]["space_count"])
        self.assertEqual(1, metadata["snapshot"]["connection_count"])
        self.assertEqual(1, metadata["snapshot"]["character_position_count"])
        self.assertTrue(metadata["memory_index"]["last_run_present"])
        self.assertIn("recovery_context", metadata["sections"])
        self.assertTrue(metadata["recovery_context"]["available"])
        self.assertEqual("chapter_1_test", metadata["recovery_context"]["source_run_id"])
        self.assertEqual(1, metadata["recovery_context"]["problem_count"])
        self.assertEqual(["logic", "llm"], metadata["recovery_context"]["executed_checks"])
        self.assertEqual(["continuity", "spatial"], metadata["recovery_context"]["skipped_checks"])
        self.assertEqual(1, metadata["recovery_context"]["repair_attempts"])

    def test_story_project_input_pack_exposes_read_set_digest_only(self) -> None:
        input_pack = build_input_pack(
            {
                "chapter_index": 2,
                "world_state": {},
                "story_state": {},
                "spatial_state": {},
                "characters": {},
                "timeline": [],
            },
            story_project_context={
                "chapter_index": 2,
                "chapter_blueprint": {"required_beats": ["advance"]},
                "read_set": {
                    "context_digest": "read-set-digest",
                    "membership": [{"relative_path": "tracking/secret.md"}],
                },
            },
        )

        self.assertIn('"read_set_context_digest": "read-set-digest"', input_pack)
        self.assertNotIn('"membership"', input_pack)
        self.assertNotIn("tracking/secret.md", input_pack)

    def test_story_project_input_pack_compacts_semantic_provenance(self) -> None:
        input_pack = build_input_pack(
            {
                "chapter_index": 2,
                "world_state": {},
                "story_state": {},
                "spatial_state": {},
                "characters": {},
                "timeline": [],
            },
            story_project_context={
                "chapter_index": 2,
                "chapter_blueprint": {"required_beats": ["advance"]},
                "semantic_state": {
                    "schema_version": "1.0",
                    "book_id": "book-1",
                    "chapter_index": 2,
                    "parser_version": "shadow-1.0",
                    "layout_profile_version": "canonical-zh-1",
                    "source_digest": "a" * 64,
                    "provenance": [{"field_path": "story_state.open_threads[0]", "secret": "omit"}],
                    "conflicts": [],
                    "parse_warnings": [],
                    "unsupported_excerpts": [],
                },
            },
        )

        self.assertIn('"provenance_count": 1', input_pack)
        self.assertNotIn('"field_path"', input_pack)
        self.assertNotIn('"secret"', input_pack)

    def test_prompt_assets_are_ascii_text(self) -> None:
        for path in sorted(Path("prompts").glob("*.md")):
            content = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=str(path)):
                self.assertTrue(content.strip())
                self.assertTrue(all(ord(char) < 128 for char in content))
                self.assertNotIn("\ufffd", content)

    def test_model_prompts_explicitly_forbid_non_prose_wrappers(self) -> None:
        for path in (Path("prompts/chapter_prompt.md"), Path("prompts/polish_prompt.md"), Path("prompts/repair_prompt.md")):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertIn("prose", content)
                self.assertIn("Markdown", content)
                self.assertIn("JSON", content)
                self.assertIn("labels", content)
                self.assertIn("commentary", content)

    def test_director_prompts_include_recovery_coverage_contract(self) -> None:
        for path in (Path("prompts/director_prompt.md"), Path("core/director/prompt.md")):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertIn("executed_checks", content)
                self.assertIn("skipped_checks", content)
                self.assertIn("validation coverage", content)
                self.assertIn("Prioritize any skipped continuity, spatial, or logic checks", content)


if __name__ == "__main__":
    unittest.main()
