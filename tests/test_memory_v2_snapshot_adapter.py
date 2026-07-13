from __future__ import annotations

import copy
import json
import unittest
import uuid
from pathlib import Path

from core.memory_v2 import (
    apply_memory_patch,
    canonical_memory_to_snapshot,
    create_empty_canonical_memory,
    import_v1_memory_file_to_patch,
    load_canonical_memory_snapshot,
    rebuild_semantic_snapshot,
)
from core.state.input_pack import build_input_pack
from core.state.snapshot import validate_snapshot


class MemoryV2SnapshotAdapterTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_v2_snapshot_adapter" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _canonical(self) -> dict:
        memory = create_empty_canonical_memory(book_id="book-1", title="Metro", language="zh-CN")
        memory["revision"] = 7
        memory["world"] = {"infection_level": "medium"}
        memory["characters"]["char_lin_xue"] = {
            "name": "Lin Xue",
            "data": {"role": "crew", "current_goal": "protect serum"},
            "state": {"current_location": "backup tunnel"},
        }
        memory["locations"]["loc_backup_tunnel"] = {
            "name": "backup tunnel",
            "data": {"risk": "high", "status": "unstable"},
        }
        memory["timeline"].append({"id": "event_001", "chapter_index": 1, "summary": "Alarm sounded.", "data": {}})
        memory["constraints"].append(
            {"id": "constraint_001", "text": "Keep serum unresolved.", "status": "active", "data": {}}
        )
        memory["constraints"].append(
            {"id": "constraint_002", "text": "Resolved constraint.", "status": "resolved", "data": {}}
        )
        memory["open_threads"].append(
            {"id": "thread_serum", "title": "The serum choice remains unresolved.", "status": "open", "data": {}}
        )
        memory["style_rules"].append(
            {"id": "style_001", "rule": "Keep prose restrained.", "status": "active", "data": {}}
        )
        memory["style_rules"].append(
            {"id": "style_002", "rule": "Inactive style.", "status": "inactive", "data": {}}
        )
        memory["current_state"] = {
            "last_chapter_ending": "The door opened.",
            "last_scene_location": "shelter",
            "last_scene_characters": ["Lin Xue"],
            "required_opening_bridge": "Continue from shelter.",
            "active_conflicts": ["serum choice"],
            "spatial_state": {
                "spaces": {"shelter": {"risk": "rising"}},
                "connections": [{"from": "shelter", "to": "backup tunnel"}],
                "blocked_paths": ["sealed gate"],
                "last_transition": {"from": "shelter", "to": "backup tunnel"},
            },
        }
        memory["source_index"] = {"patch_import_v1_default": {"kind": "local_memory"}}
        memory["source_resolution"] = {"world": {"chosen_source": "patch_import_v1_default"}}
        return memory

    def test_empty_canonical_memory_converts_to_snapshot(self) -> None:
        snapshot = canonical_memory_to_snapshot(create_empty_canonical_memory())

        self.assertEqual("zh-CN", snapshot["project_profile"]["language"])
        self.assertIn("world_state", snapshot)
        self.assertIn("characters", snapshot)
        self.assertIn("story_state", snapshot)
        self.assertIn("spatial_state", snapshot)
        self.assertEqual(1, snapshot["memory_v2"]["revision"])
        self.assertIs(snapshot, validate_snapshot(snapshot))

    def test_world_maps_to_world_state(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())

        self.assertEqual("medium", snapshot["world_state"]["infection_level"])

    def test_character_maps_to_snapshot_and_character_positions(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())
        character = snapshot["characters"]["char_lin_xue"]

        self.assertEqual("char_lin_xue", character["id"])
        self.assertEqual("Lin Xue", character["name"])
        self.assertEqual("crew", character["role"])
        self.assertEqual("crew", character["data"]["role"])
        self.assertEqual("backup tunnel", snapshot["spatial_state"]["character_positions"]["Lin Xue"])

    def test_location_maps_to_locations_and_spatial_spaces(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())

        self.assertEqual("backup tunnel", snapshot["locations"]["loc_backup_tunnel"]["name"])
        self.assertEqual("high", snapshot["spatial_state"]["spaces"]["backup tunnel"]["risk"])
        self.assertEqual("rising", snapshot["spatial_state"]["spaces"]["shelter"]["risk"])

    def test_story_state_and_open_threads_map_to_story_state(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())

        self.assertEqual("The door opened.", snapshot["story_state"]["last_chapter_ending"])
        self.assertEqual("Continue from shelter.", snapshot["story_state"]["required_opening_bridge"])
        self.assertEqual(["The serum choice remains unresolved."], snapshot["story_state"]["open_threads"])
        self.assertEqual("thread_serum", snapshot["open_threads"][0]["id"])

    def test_spatial_state_maps_from_current_state(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())

        self.assertEqual([{"from": "shelter", "to": "backup tunnel"}], snapshot["spatial_state"]["connections"])
        self.assertEqual(["sealed gate"], snapshot["spatial_state"]["blocked_paths"])
        self.assertEqual({"from": "shelter", "to": "backup tunnel"}, snapshot["spatial_state"]["last_transition"])

    def test_constraints_and_style_rules_map_to_snapshot(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())

        self.assertEqual(2, len(snapshot["constraints"]))
        self.assertEqual(["constraint_001"], [item["id"] for item in snapshot["active_constraints"]])
        self.assertEqual("style_001", snapshot["project_profile"]["style_rules"][0]["id"])
        self.assertEqual(2, len(snapshot["style_rules"]))

    def test_memory_v2_metadata_maps_to_snapshot(self) -> None:
        canonical = self._canonical()
        snapshot = canonical_memory_to_snapshot(canonical)

        self.assertEqual(canonical["revision"], snapshot["memory_v2"]["revision"])
        self.assertEqual(canonical["source_index"], snapshot["memory_v2"]["source_index"])
        self.assertEqual(canonical["source_resolution"], snapshot["memory_v2"]["source_resolution"])

    def test_adapter_does_not_mutate_input(self) -> None:
        canonical = self._canonical()
        original = copy.deepcopy(canonical)

        canonical_memory_to_snapshot(canonical)

        self.assertEqual(original, canonical)

    def test_snapshot_can_be_consumed_by_input_pack(self) -> None:
        snapshot = canonical_memory_to_snapshot(self._canonical())

        input_pack = build_input_pack(snapshot)

        self.assertIn("# World State", input_pack)
        self.assertIn("backup tunnel", input_pack)

    def test_load_canonical_memory_snapshot(self) -> None:
        path = self._case_dir("load") / "canonical_memory.json"
        path.write_text(json.dumps(self._canonical(), ensure_ascii=False), encoding="utf-8")

        snapshot = load_canonical_memory_snapshot(path)

        self.assertEqual("medium", snapshot["world_state"]["infection_level"])

    def test_importer_reducer_adapter_chain(self) -> None:
        patch = import_v1_memory_file_to_patch(Path("data/notion_memory.example.json"))
        canonical, _ = apply_memory_patch(create_empty_canonical_memory(), patch)

        snapshot = canonical_memory_to_snapshot(canonical)

        self.assertEqual("medium", snapshot["world_state"]["infection_level"])
        self.assertTrue(snapshot["story_state"]["last_chapter_ending"])
        self.assertTrue(snapshot["story_state"]["open_threads"])
        self.assertTrue(snapshot["spatial_state"]["character_positions"])
        self.assertTrue(snapshot["spatial_state"]["spaces"])
        self.assertTrue(snapshot["characters"])
        self.assertTrue(snapshot["locations"])
        self.assertIs(snapshot, validate_snapshot(snapshot))

    def test_semantic_rebuild_keeps_story_project_facts_authoritative(self) -> None:
        canonical = self._canonical()
        canonical["world"]["infection_level"] = "memory-low"
        canonical["characters"]["char_lin_xue"]["data"]["role"] = "memory-role"
        story = {
            "schema_version": "1.0",
            "book_id": "book-1",
            "chapter_index": 8,
            "story_state": {"last_scene_location": "manual shelter"},
            "world_state": {"infection_level": "manual-high"},
            "spatial_state": {"character_positions": {"Lin Xue": "manual shelter"}},
            "characters": {"char_lin_xue": {"name": "Lin Xue", "data": {"role": "manual-role"}}},
            "timeline": [{"id": "event_001", "summary": "Manual timeline", "data": {}}],
            "constraints": [{"id": "constraint_001", "text": "Manual constraint", "status": "active", "data": {}}],
            "foreshadowing": [],
            "provenance": [],
            "conflicts": [],
            "parse_warnings": [],
            "unsupported_excerpts": [],
            "parser_version": "test-parser",
            "layout_profile_version": "test-layout",
            "source_digest": "a" * 64,
        }

        snapshot = rebuild_semantic_snapshot(story, canonical)

        self.assertEqual(8, snapshot["chapter_index"])
        self.assertEqual("manual-high", snapshot["world_state"]["infection_level"])
        self.assertEqual("manual shelter", snapshot["story_state"]["last_scene_location"])
        self.assertEqual("manual-role", snapshot["characters"]["char_lin_xue"]["data"]["role"])
        self.assertEqual("Manual timeline", snapshot["timeline"][0]["summary"])
        self.assertEqual("Manual constraint", snapshot["constraints"][0]["text"])

    def test_semantic_rebuild_rejects_cross_book_projection(self) -> None:
        canonical = self._canonical()
        story = {
            "schema_version": "1.0",
            "book_id": "other-book",
            "chapter_index": 1,
            "story_state": {},
            "world_state": {},
            "spatial_state": {},
            "characters": {},
            "timeline": [],
            "constraints": [],
            "foreshadowing": [],
            "provenance": [],
            "conflicts": [],
            "parse_warnings": [],
            "unsupported_excerpts": [],
            "parser_version": "test-parser",
            "layout_profile_version": "test-layout",
            "source_digest": "b" * 64,
        }

        with self.assertRaisesRegex(ValueError, "book_id"):
            rebuild_semantic_snapshot(story, canonical)


if __name__ == "__main__":
    unittest.main()
