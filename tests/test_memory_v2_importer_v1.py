from __future__ import annotations

import unittest
from pathlib import Path

from core.memory_v2 import import_v1_memory_file_to_patch, import_v1_memory_to_patch, validate_memory_patch


class MemoryV2ImporterV1Test(unittest.TestCase):
    def test_imports_notion_memory_example_to_patch(self) -> None:
        patch = import_v1_memory_file_to_patch(Path("data/notion_memory.example.json"))

        self.assertEqual("2.1", patch["schema_version"])
        self.assertEqual("patch_import_v1_default", patch["patch_id"])
        self.assertEqual("local_memory", patch["source"]["kind"])
        self.assertEqual(str(Path("data/notion_memory.example.json")), patch["source"]["path"])
        self.assertGreater(len(patch["operations"]), 0)
        self.assertIs(patch, validate_memory_patch(patch))

    def test_import_result_contains_expected_operation_types(self) -> None:
        patch = import_v1_memory_file_to_patch(Path("data/notion_memory.example.json"))
        ops = [operation["op"] for operation in patch["operations"]]

        self.assertIn("update_world", ops)
        self.assertIn("update_current_state", ops)
        self.assertIn("upsert_open_thread", ops)
        self.assertIn("update_character_state", ops)
        self.assertIn("upsert_location", ops)
        self.assertIn("upsert_character", ops)
        self.assertIn("upsert_constraint", ops)

    def test_imports_character_location_constraint_and_timeline_items(self) -> None:
        patch = import_v1_memory_to_patch(
            {
                "items": [
                    {"type": "character", "name": "Lin Xue", "data": {"role": "lead"}},
                    {"type": "location", "name": "shelter", "data": {"risk": "rising"}},
                    {"type": "constraint", "data": {"rule": "Keep serum in focus."}},
                    {"type": "timeline_event", "data": {"chapter_index": 1, "summary": "Mira arrives."}},
                ]
            }
        )
        operations_by_op = {operation["op"]: operation for operation in patch["operations"]}

        self.assertEqual("char_lin_xue", operations_by_op["upsert_character"]["id"])
        self.assertEqual("loc_shelter", operations_by_op["upsert_location"]["id"])
        self.assertEqual("Keep serum in focus.", operations_by_op["upsert_constraint"]["value"]["text"])
        self.assertEqual("Mira arrives.", operations_by_op["append_timeline_event"]["value"]["summary"])
        self.assertIs(patch, validate_memory_patch(patch))

    def test_story_state_generates_current_state_and_open_threads(self) -> None:
        patch = import_v1_memory_to_patch(
            [
                {
                    "type": "story_state",
                    "data": {
                        "last_chapter_ending": "The door opened.",
                        "last_scene_location": "shelter",
                        "last_scene_characters": ["Lin Xue"],
                        "required_opening_bridge": "Continue from shelter.",
                        "active_conflicts": ["serum choice"],
                        "open_threads": ["Find the serum."],
                    },
                }
            ]
        )
        current_state = next(operation for operation in patch["operations"] if operation["op"] == "update_current_state")
        thread = next(operation for operation in patch["operations"] if operation["op"] == "upsert_open_thread")

        self.assertEqual("The door opened.", current_state["value"]["last_chapter_ending"])
        self.assertEqual(["Lin Xue"], current_state["value"]["last_scene_characters"])
        self.assertEqual("Find the serum.", thread["value"]["title"])

    def test_spatial_state_generates_character_state_and_location_updates(self) -> None:
        patch = import_v1_memory_to_patch(
            [
                {
                    "type": "spatial_state",
                    "data": {
                        "character_positions": {"Lin Xue": "备用通道"},
                        "location_states": {"备用通道": {"risk": "high"}},
                        "connections": [{"from": "shelter", "to": "备用通道"}],
                        "blocked_paths": ["sealed gate"],
                        "last_transition": {"from": "shelter", "to": "备用通道"},
                    },
                }
            ]
        )
        spatial_state = next(
            operation
            for operation in patch["operations"]
            if operation["op"] == "update_current_state" and operation.get("data", {}).get("source_type") == "spatial_state"
        )
        character_state = next(
            operation for operation in patch["operations"] if operation["op"] == "update_character_state"
        )
        location = next(operation for operation in patch["operations"] if operation["op"] == "upsert_location")

        self.assertEqual("char_lin_xue", character_state["id"])
        self.assertEqual("state.current_location", character_state["field"])
        self.assertEqual("备用通道", character_state["value"])
        self.assertEqual("loc_备用通道", location["id"])
        self.assertEqual([{"from": "shelter", "to": "备用通道"}], spatial_state["value"]["spatial_state"]["connections"])
        self.assertEqual(["sealed gate"], spatial_state["value"]["spatial_state"]["blocked_paths"])
        self.assertEqual(
            {"from": "shelter", "to": "备用通道"},
            spatial_state["value"]["spatial_state"]["last_transition"],
        )

    def test_item_type_field_is_supported(self) -> None:
        patch = import_v1_memory_to_patch([{"item_type": "character", "title": "林雪", "data": {}}])

        self.assertEqual("upsert_character", patch["operations"][0]["op"])
        self.assertEqual("char_林雪", patch["operations"][0]["id"])

    def test_existing_item_id_is_preserved(self) -> None:
        patch = import_v1_memory_to_patch([{"type": "character", "id": "custom-char-id", "name": "Lin Xue"}])

        self.assertEqual("custom-char-id", patch["operations"][0]["id"])

    def test_import_is_stable_across_runs(self) -> None:
        memory = {
            "items": [
                {"type": "character", "name": "林雪", "data": {"role": "lead"}},
                {"type": "location", "name": "备用通道", "data": {}},
                {"type": "constraint", "data": {"rule": "Keep serum visible."}},
            ]
        }

        first = import_v1_memory_to_patch(memory)
        second = import_v1_memory_to_patch(memory)

        self.assertEqual(
            [operation.get("id") for operation in first["operations"]],
            [operation.get("id") for operation in second["operations"]],
        )
        self.assertEqual(first["operations"], second["operations"])


if __name__ == "__main__":
    unittest.main()
