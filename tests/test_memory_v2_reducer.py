from __future__ import annotations

import copy
import unittest
from pathlib import Path

from core.memory_v2 import (
    MemoryReducerError,
    apply_memory_patch,
    create_empty_canonical_memory,
    create_memory_patch,
    import_v1_memory_file_to_patch,
    validate_canonical_memory,
    validate_memory_event,
)


class MemoryV2ReducerTest(unittest.TestCase):
    def _patch(self, operations: list[dict], *, source_path: str | None = "data/notion_memory.example.json") -> dict:
        return create_memory_patch(
            patch_id="patch_import_v1_default",
            source_path=source_path,
            operations=operations,
        )

    def test_empty_patch_does_not_change_canonical_memory(self) -> None:
        memory = create_empty_canonical_memory()

        updated, events = apply_memory_patch(memory, self._patch([]))

        self.assertEqual(memory, updated)
        self.assertEqual([], events)
        self.assertIsNot(memory, updated)

    def test_upsert_character_writes_character(self) -> None:
        memory = create_empty_canonical_memory()
        patch = self._patch(
            [{"op": "upsert_character", "id": "char_lin_xue", "value": {"name": "Lin Xue", "data": {"role": "lead"}}}]
        )

        updated, events = apply_memory_patch(memory, patch)

        self.assertEqual({"name": "Lin Xue", "data": {"role": "lead"}}, updated["characters"]["char_lin_xue"])
        self.assertEqual("evt_000002", events[0]["event_id"])
        self.assertEqual(2, updated["revision"])

    def test_upsert_character_shallow_merges_existing_record(self) -> None:
        memory = create_empty_canonical_memory()
        memory["characters"]["char_lin_xue"] = {"name": "Lin Xue", "data": {"role": "lead", "status": "ok"}}
        patch = self._patch(
            [{"op": "upsert_character", "id": "char_lin_xue", "value": {"data": {"status": "injured"}}}]
        )

        updated, events = apply_memory_patch(memory, patch)

        self.assertEqual({"role": "lead", "status": "injured"}, updated["characters"]["char_lin_xue"]["data"])
        self.assertEqual({"name": "Lin Xue", "data": {"role": "lead", "status": "ok"}}, events[0]["old_value"])

    def test_upsert_location_writes_location(self) -> None:
        updated, _ = apply_memory_patch(
            create_empty_canonical_memory(),
            self._patch([{"op": "upsert_location", "id": "loc_shelter", "value": {"name": "shelter", "data": {"risk": "rising"}}}]),
        )

        self.assertEqual("rising", updated["locations"]["loc_shelter"]["data"]["risk"])

    def test_upsert_constraint_upserts_by_id(self) -> None:
        memory = create_empty_canonical_memory()
        memory["constraints"].append({"id": "constraint_0001", "text": "Old", "status": "active", "data": {"a": 1}})
        patch = self._patch(
            [
                {
                    "op": "upsert_constraint",
                    "id": "constraint_0001",
                    "value": {"text": "New", "status": "resolved", "data": {"b": 2}},
                }
            ]
        )

        updated, _ = apply_memory_patch(memory, patch)

        self.assertEqual(1, len(updated["constraints"]))
        self.assertEqual({"id": "constraint_0001", "text": "New", "status": "resolved", "data": {"a": 1, "b": 2}}, updated["constraints"][0])

    def test_append_timeline_event_upserts_by_id_without_duplicates(self) -> None:
        memory = create_empty_canonical_memory()
        memory["timeline"].append({"id": "event_001", "summary": "Old", "data": {"a": 1}})
        patch = self._patch(
            [
                {
                    "op": "append_timeline_event",
                    "id": "event_001",
                    "value": {"summary": "New", "characters": ["Lin Xue"], "data": {"b": 2}},
                }
            ]
        )

        updated, _ = apply_memory_patch(memory, patch)

        self.assertEqual(1, len(updated["timeline"]))
        self.assertEqual("New", updated["timeline"][0]["summary"])
        self.assertEqual(["Lin Xue"], updated["timeline"][0]["data"]["characters"])

    def test_update_current_state_updates_current_state(self) -> None:
        updated, events = apply_memory_patch(
            create_empty_canonical_memory(),
            self._patch([{"op": "update_current_state", "value": {"last_scene_location": "shelter"}}]),
        )

        self.assertEqual("shelter", updated["current_state"]["last_scene_location"])
        self.assertEqual({"last_scene_location": None}, events[0]["old_value"])

    def test_upsert_open_thread_upserts_by_id(self) -> None:
        memory = create_empty_canonical_memory()
        memory["open_threads"].append({"id": "thread_001", "title": "Old", "status": "open", "data": {"a": 1}})
        patch = self._patch(
            [{"op": "upsert_open_thread", "id": "thread_001", "value": {"title": "New", "status": "closed", "data": {"b": 2}}}]
        )

        updated, _ = apply_memory_patch(memory, patch)

        self.assertEqual({"id": "thread_001", "title": "New", "status": "closed", "data": {"a": 1, "b": 2}}, updated["open_threads"][0])

    def test_update_character_state_supports_dotted_field_path(self) -> None:
        updated, events = apply_memory_patch(
            create_empty_canonical_memory(),
            self._patch(
                [
                    {
                        "op": "update_character_state",
                        "id": "char_lin_xue",
                        "field": "state.current_location",
                        "value": "shelter",
                        "data": {"source_field": "character_positions"},
                    }
                ]
            ),
        )

        self.assertEqual("shelter", updated["characters"]["char_lin_xue"]["state"]["current_location"])
        self.assertEqual({"source_field": "character_positions"}, events[0]["metadata"]["operation_data"])

    def test_update_character_state_creates_minimal_character(self) -> None:
        updated, _ = apply_memory_patch(
            create_empty_canonical_memory(),
            self._patch([{"op": "update_character_state", "id": "char_new", "field": "state.current_location", "value": "shelter"}]),
        )

        self.assertEqual("char_new", updated["characters"]["char_new"]["name"])
        self.assertEqual({}, updated["characters"]["char_new"]["data"])

    def test_update_world_shallow_merges_world(self) -> None:
        memory = create_empty_canonical_memory()
        memory["world"] = {"infection_level": "low", "weather": "rain"}
        patch = self._patch([{"op": "update_world", "value": {"infection_level": "medium"}}])

        updated, _ = apply_memory_patch(memory, patch)

        self.assertEqual({"infection_level": "medium", "weather": "rain"}, updated["world"])

    def test_update_world_rejects_non_object_value(self) -> None:
        memory = create_empty_canonical_memory()

        with self.assertRaisesRegex(MemoryReducerError, "update_world value must be an object"):
            apply_memory_patch(memory, self._patch([{"op": "update_world", "value": "bad"}]))

        self.assertEqual({}, memory["world"])

    def test_events_and_revision_increment_per_operation(self) -> None:
        patch = self._patch(
            [
                {"op": "update_world", "value": {"infection_level": "medium"}},
                {"op": "update_current_state", "value": {"last_scene_location": "shelter"}},
                {"op": "upsert_location", "id": "loc_shelter", "value": {"name": "shelter", "data": {}}},
            ]
        )

        updated, events = apply_memory_patch(create_empty_canonical_memory(), patch)

        self.assertEqual(4, updated["revision"])
        self.assertEqual(["evt_000002", "evt_000003", "evt_000004"], [event["event_id"] for event in events])
        self.assertEqual([2, 3, 4], [event["revision"] for event in events])

    def test_source_index_records_patch_source(self) -> None:
        updated, events = apply_memory_patch(
            create_empty_canonical_memory(),
            self._patch([{"op": "update_world", "value": {"infection_level": "medium"}}]),
        )

        self.assertEqual(
            {"kind": "local_memory", "path": "data/notion_memory.example.json"},
            updated["source_index"]["patch_import_v1_default"],
        )
        self.assertEqual("patch_import_v1_default", events[0]["source"]["patch_id"])
        self.assertEqual(
            {"chosen_source": "patch_import_v1_default", "reason": "latest_patch_operation"},
            updated["source_resolution"]["world"],
        )

    def test_unknown_op_raises_memory_reducer_error(self) -> None:
        memory = create_empty_canonical_memory()
        original = copy.deepcopy(memory)
        patch = self._patch([{"op": "unknown_op", "value": {}}])

        with self.assertRaises(MemoryReducerError):
            apply_memory_patch(memory, patch)

        self.assertEqual(original, memory)

    def test_reducer_does_not_mutate_inputs(self) -> None:
        memory = create_empty_canonical_memory()
        patch = self._patch([{"op": "update_world", "value": {"infection_level": "medium"}}])
        original_memory = copy.deepcopy(memory)
        original_patch = copy.deepcopy(patch)

        apply_memory_patch(memory, patch)

        self.assertEqual(original_memory, memory)
        self.assertEqual(original_patch, patch)

    def test_output_memory_and_events_validate(self) -> None:
        updated, events = apply_memory_patch(
            create_empty_canonical_memory(),
            self._patch([{"op": "update_world", "value": {"infection_level": "medium"}}]),
        )

        self.assertIs(updated, validate_canonical_memory(updated))
        self.assertTrue(all(validate_memory_event(event) is event for event in events))

    def test_importer_patch_can_reduce_into_canonical_memory(self) -> None:
        patch = import_v1_memory_file_to_patch(Path("data/notion_memory.example.json"))

        updated, events = apply_memory_patch(create_empty_canonical_memory(), patch)

        self.assertEqual("medium", updated["world"]["infection_level"])
        self.assertIn("last_chapter_ending", updated["current_state"])
        self.assertIn("protagonist", [record["name"] for record in updated["characters"].values()])
        self.assertIn("shelter", [record["name"] for record in updated["locations"].values()])
        self.assertGreater(len(updated["open_threads"]), 0)
        self.assertEqual(updated["revision"], events[-1]["revision"])


if __name__ == "__main__":
    unittest.main()
