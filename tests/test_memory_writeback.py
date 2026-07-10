from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.executor import AgentExecutor
from core.schema import SchemaValidationError
from core.state.memory_updates import build_memory_updates
from core.state.memory_writer import (
    DEFAULT_MEMORY_OUTBOX,
    FileMemoryWriter,
    NotionMemoryWriter,
    build_memory_writer,
    memory_item_to_notion_properties,
    resolve_memory_writeback_mode,
    validate_memory_writeback_result,
    write_memory_updates,
)


class MemoryWritebackTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_writeback" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _write_snapshot(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "chapter_index": 2,
                    "world_state": {"locations": {}},
                    "characters": {},
                    "timeline": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_build_memory_updates_from_committed_analysis(self) -> None:
        updates = build_memory_updates(
            {"id": "run-1", "chapter_index": 3},
            {
                "summary": "The shelter changed.",
                "events": [{"text": "The team chose rescue."}],
                "world_changes": [{"type": "infection_pressure", "text": "Infection rose."}],
                "new_locations": ["shelter"],
            },
        )

        self.assertEqual(
            ["timeline_event", "timeline_event", "world_state", "location"],
            [item["type"] for item in updates],
        )
        self.assertTrue(all(item["id"].startswith("chapter_3:") for item in updates))
        self.assertTrue(all(item["data"]["source_run_id"] == "run-1" for item in updates))

    def test_build_memory_updates_includes_character_changes(self) -> None:
        updates = build_memory_updates(
            {"id": "run-1", "chapter_index": 3},
            {
                "character_changes": [
                    {
                        "name": "Mira",
                        "status": "injured",
                        "current_location": "shelter",
                        "text": "Mira was injured at the shelter.",
                    }
                ],
            },
        )

        self.assertEqual(1, len(updates))
        self.assertEqual("character", updates[0]["type"])
        self.assertEqual("Mira", updates[0]["name"])
        self.assertEqual("chapter_3:character:mira", updates[0]["id"])
        self.assertEqual("injured", updates[0]["data"]["status"])
        self.assertEqual("shelter", updates[0]["data"]["current_location"])
        self.assertEqual("run-1", updates[0]["data"]["source_run_id"])

    def test_memory_updates_includes_story_and_spatial_state(self) -> None:
        updates = build_memory_updates(
            {"id": "run-1", "chapter_index": 3},
            {
                "story_state": {
                    "last_chapter_ending": "Mira reached the connector passage.",
                    "last_scene_location": "connector passage",
                    "last_scene_characters": ["Mira"],
                    "open_threads": ["The sealed gate remains blocked."],
                    "required_opening_bridge": "Continue from connector passage.",
                },
                "spatial_state": {
                    "spaces": {"connector passage": {"source": "chapter_analysis"}},
                    "connections": [{"from": "train car", "to": "connector passage"}],
                    "character_positions": {"Mira": "connector passage"},
                    "blocked_paths": [],
                    "last_transition": {"from": "train car", "to": "connector passage"},
                },
            },
        )

        self.assertEqual(["story_state", "spatial_state"], [item["type"] for item in updates])
        self.assertEqual("chapter_3:story_state:chapter_3_story_state", updates[0]["id"])
        self.assertEqual("connector passage", updates[0]["data"]["last_scene_location"])
        self.assertEqual(["Mira"], updates[0]["data"]["last_scene_characters"])
        self.assertEqual("chapter_3:spatial_state:chapter_3_spatial_state", updates[1]["id"])
        self.assertEqual("connector passage", updates[1]["data"]["character_positions"]["Mira"])
        self.assertEqual("run-1", updates[0]["data"]["source_run_id"])
        self.assertEqual("run-1", updates[1]["data"]["source_run_id"])

    def test_file_memory_writer_appends_jsonl(self) -> None:
        tmp_path = self._case_dir("file_writer")
        outbox = tmp_path / "memory_outbox.jsonl"

        result = FileMemoryWriter(outbox)([{"type": "location", "name": "shelter", "data": {}}])

        self.assertEqual(1, result["written"])
        self.assertEqual("written", result["item_mappings"][0]["status"])
        self.assertEqual(1, result["item_mappings"][0]["line_number"])
        self.assertEqual(str(outbox), result["item_mappings"][0]["path"])
        self.assertEqual("verified", result["verification"]["status"])
        self.assertEqual(1, result["verification"]["checked"])
        self.assertEqual(1, result["verification"]["passed"])
        self.assertEqual(0, result["verification"]["failed"])
        lines = outbox.read_text(encoding="utf-8").splitlines()
        self.assertEqual("location", json.loads(lines[0])["type"])

    def test_file_memory_writer_skips_duplicate_ids(self) -> None:
        tmp_path = self._case_dir("file_writer_dedupe")
        outbox = tmp_path / "memory_outbox.jsonl"
        update = {"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {}}
        writer = FileMemoryWriter(outbox)

        first = writer([update])
        second = writer([update])

        self.assertEqual(1, first["written"])
        self.assertEqual(0, second["written"])
        self.assertEqual(1, second["skipped"])
        self.assertEqual("skipped_duplicate", second["item_mappings"][0]["status"])
        self.assertEqual("run-1:location:shelter", second["item_mappings"][0]["memory_id"])
        self.assertEqual("not_applicable", second["verification"]["status"])
        self.assertEqual("no_written_items", second["verification"]["reason"])
        self.assertEqual(1, len(outbox.read_text(encoding="utf-8").splitlines()))

    def test_file_memory_writer_fails_closed_on_duplicate_payload_conflict(self) -> None:
        tmp_path = self._case_dir("file_writer_duplicate_conflict")
        outbox = tmp_path / "memory_outbox.jsonl"
        writer = FileMemoryWriter(outbox)
        writer([{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {"safe": True}}])

        result = writer(
            [{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {"safe": False}}]
        )

        self.assertEqual(0, result["written"])
        self.assertEqual("duplicate_conflict", result["item_mappings"][0]["status"])
        self.assertEqual("failed", result["verification"]["status"])
        self.assertEqual("duplicate_payload_conflict", result["verification"]["failures"][0]["reason"])
        self.assertEqual(1, len(outbox.read_text(encoding="utf-8").splitlines()))

    def test_memory_outbox_implies_file_writeback_mode(self) -> None:
        self.assertEqual("file", resolve_memory_writeback_mode(mode="none", outbox_path="outbox.jsonl"))

    def test_build_memory_writer_defaults_file_outbox_path(self) -> None:
        writer = build_memory_writer(mode="file")

        self.assertIsInstance(writer, FileMemoryWriter)
        self.assertEqual(Path(DEFAULT_MEMORY_OUTBOX), writer.path)

    def test_build_memory_writer_can_create_notion_writer(self) -> None:
        writer = build_memory_writer(mode="notion")

        self.assertIsInstance(writer, NotionMemoryWriter)
        self.assertTrue(writer.dedupe_existing)

    def test_build_memory_writer_can_enable_notion_readback(self) -> None:
        writer = build_memory_writer(mode="notion", notion_readback=True)

        self.assertIsInstance(writer, NotionMemoryWriter)
        self.assertTrue(writer.verify_remote_readback)

    def test_file_memory_writer_rejects_invalid_memory_update(self) -> None:
        tmp_path = self._case_dir("file_writer_invalid")
        outbox = tmp_path / "memory_outbox.jsonl"

        with self.assertRaises(SchemaValidationError):
            FileMemoryWriter(outbox)([{"type": "location", "name": "shelter", "data": "bad"}])

        self.assertFalse(outbox.exists())

    def test_write_memory_updates_validates_even_without_writer(self) -> None:
        with self.assertRaises(SchemaValidationError):
            write_memory_updates([{"type": "location", "name": "shelter", "data": "bad"}], None)

    def test_write_memory_updates_without_writer_returns_item_mappings(self) -> None:
        result = write_memory_updates(
            [{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {}}],
            None,
        )

        self.assertEqual(0, result["written"])
        self.assertTrue(result["skipped"])
        self.assertEqual("skipped_no_writer", result["item_mappings"][0]["status"])
        self.assertEqual("run-1:location:shelter", result["item_mappings"][0]["memory_id"])
        self.assertEqual("not_applicable", result["verification"]["status"])
        self.assertEqual("no_writer", result["verification"]["reason"])

    def test_memory_writeback_result_rejects_unknown_fields(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_memory_writeback_result(
                {
                    "target": "file",
                    "written": 0,
                    "item_mappings": [],
                    "verification": {"status": "verified", "target": "file"},
                    "unexpected": True,
                }
            )

    def test_notion_memory_writer_maps_updates_to_pages(self) -> None:
        calls: list[dict] = []

        def transport(url, headers, body):
            calls.append(body)
            return {"id": f"page-{len(calls)}", "url": f"https://notion.test/page-{len(calls)}"}

        writer = NotionMemoryWriter(database_id="db", api_key="secret", transport=transport)
        result = writer([{"type": "timeline_event", "name": "chapter_2_summary", "data": {"summary": "A"}}])

        self.assertEqual(1, result["written"])
        self.assertEqual(["page-1"], result["page_ids"])
        self.assertEqual("page-1", result["item_mappings"][0]["page_id"])
        self.assertEqual("https://notion.test/page-1", result["item_mappings"][0]["page_url"])
        self.assertEqual("db", result["item_mappings"][0]["database_id"])
        self.assertEqual(["Data", "Memory ID", "Name", "Type"], result["item_mappings"][0]["property_names"])
        self.assertEqual("chapter_2_summary", result["item_mappings"][0]["name"])
        self.assertEqual("written", result["item_mappings"][0]["status"])
        self.assertEqual("response_recorded", result["verification"]["status"])
        self.assertEqual("remote_readback_not_configured", result["verification"]["reason"])
        self.assertEqual(1, result["verification"]["checked"])
        self.assertEqual(1, result["verification"]["passed"])
        self.assertEqual("timeline_event", calls[0]["properties"]["Type"]["select"]["name"])

    def test_notion_memory_writer_skips_existing_remote_memory_id_when_dedupe_enabled(self) -> None:
        calls: list[str] = []

        def transport(url, headers, body):
            calls.append(url)
            if url.endswith("/query"):
                return {
                    "results": [
                        {
                            "id": "page-existing",
                            "url": "https://notion.test/page-existing",
                            "properties": {
                                "Memory ID": {"rich_text": [{"plain_text": "run-1:location:shelter"}]},
                                "Type": {"select": {"name": "location"}},
                                "Name": {"title": [{"plain_text": "shelter"}]},
                                "Data": {"rich_text": [{"plain_text": "{}"}]},
                            },
                        }
                    ],
                    "has_more": False,
                }
            return {"id": "page-created", "url": "https://notion.test/page-created"}

        result = NotionMemoryWriter(
            database_id="db",
            api_key="secret",
            transport=transport,
            dedupe_existing=True,
        )([{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {}}])

        self.assertEqual(0, result["written"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual("skipped_duplicate", result["item_mappings"][0]["status"])
        self.assertEqual("page-existing", result["item_mappings"][0]["page_id"])
        self.assertEqual("https://notion.test/page-existing", result["item_mappings"][0]["page_url"])
        self.assertEqual("not_applicable", result["verification"]["status"])
        self.assertEqual("no_written_items", result["verification"]["reason"])
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0].endswith("/query"))

    def test_notion_memory_writer_writes_new_items_after_remote_dedupe(self) -> None:
        calls: list[str] = []

        def transport(url, headers, body):
            calls.append(url)
            if url.endswith("/query"):
                return {"results": [], "has_more": False}
            return {"id": "page-created", "url": "https://notion.test/page-created"}

        result = NotionMemoryWriter(
            database_id="db",
            api_key="secret",
            transport=transport,
            dedupe_existing=True,
        )([{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {}}])

        self.assertEqual(1, result["written"])
        self.assertEqual(0, result["skipped"])
        self.assertEqual("written", result["item_mappings"][0]["status"])
        self.assertEqual("page-created", result["item_mappings"][0]["page_id"])
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0].endswith("/query"))
        self.assertTrue(calls[1].endswith("/pages"))

    def test_notion_memory_writer_marks_response_incomplete_without_page_id(self) -> None:
        def transport(url, headers, body):
            return {}

        result = NotionMemoryWriter(database_id="db", api_key="secret", transport=transport)(
            [{"type": "location", "name": "shelter", "data": {}}]
        )

        self.assertEqual("response_incomplete", result["verification"]["status"])
        self.assertEqual(1, result["verification"]["failed"])
        self.assertEqual("missing_page_id", result["verification"]["failures"][0]["reason"])

    def test_notion_memory_writer_can_verify_remote_readback(self) -> None:
        def transport(url, headers, body):
            if url.endswith("/pages"):
                return {"id": "page-1", "url": "https://notion.test/page-1"}
            return {
                "results": [
                    {
                        "id": "page-1",
                        "url": "https://notion.test/page-1",
                        "properties": {
                            "Memory ID": {"rich_text": [{"plain_text": "run-1:location:shelter"}]},
                            "Type": {"select": {"name": "location"}},
                            "Name": {"title": [{"plain_text": "shelter"}]},
                            "Data": {"rich_text": [{"plain_text": "{}"}]},
                        },
                    }
                ],
                "has_more": False,
            }

        result = NotionMemoryWriter(
            database_id="db",
            api_key="secret",
            transport=transport,
            verify_remote_readback=True,
        )([{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {}}])

        self.assertEqual("verified", result["verification"]["status"])
        self.assertEqual("remote_readback", result["verification"]["reason"])
        self.assertEqual(1, result["verification"]["checked"])
        self.assertEqual(1, result["verification"]["passed"])
        self.assertEqual(0, result["verification"]["failed"])

    def test_notion_memory_writer_reports_remote_readback_mismatch(self) -> None:
        def transport(url, headers, body):
            if url.endswith("/pages"):
                return {"id": "page-1", "url": "https://notion.test/page-1"}
            return {
                "results": [
                    {
                        "id": "page-1",
                        "properties": {
                            "Memory ID": {"rich_text": [{"plain_text": "run-1:location:shelter"}]},
                            "Type": {"select": {"name": "character"}},
                            "Name": {"title": [{"plain_text": "shelter"}]},
                            "Data": {"rich_text": [{"plain_text": "{}"}]},
                        },
                    }
                ],
                "has_more": False,
            }

        result = NotionMemoryWriter(
            database_id="db",
            api_key="secret",
            transport=transport,
            verify_remote_readback=True,
        )([{"id": "run-1:location:shelter", "type": "location", "name": "shelter", "data": {}}])

        self.assertEqual("failed", result["verification"]["status"])
        self.assertEqual(1, result["verification"]["failed"])
        self.assertEqual("field_mismatch", result["verification"]["failures"][0]["reason"])
        self.assertEqual(["type"], result["verification"]["failures"][0]["fields"])

    def test_notion_property_mapping_uses_type_name_data(self) -> None:
        properties = memory_item_to_notion_properties(
            {
                "id": "run-1:world_state:infection_pressure",
                "type": "world_state",
                "name": "infection_pressure",
                "data": {"level": "high"},
            }
        )

        self.assertEqual("run-1:world_state:infection_pressure", properties["Memory ID"]["rich_text"][0]["text"]["content"])
        self.assertEqual("world_state", properties["Type"]["select"]["name"])
        self.assertEqual("infection_pressure", properties["Name"]["title"][0]["text"]["content"])
        self.assertIn("\"level\": \"high\"", properties["Data"]["rich_text"][0]["text"]["content"])

    def test_executor_writes_memory_updates_only_for_committed_persisted_runs(self) -> None:
        tmp_path = self._case_dir("executor_writer")
        snapshot_path = tmp_path / "snapshot.json"
        outbox = tmp_path / "memory_outbox.jsonl"
        self._write_snapshot(snapshot_path)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            memory_writer=FileMemoryWriter(outbox),
        ).run_once(persist=True)

        self.assertTrue(result["committed"])
        self.assertGreater(result["memory_write"]["written"], 0)
        self.assertTrue(result["memory_write"]["gate"]["allowed"])
        self.assertGreater(result["memory_write"]["gate"]["pending_update_count"], 0)
        self.assertTrue(result["memory_write"]["gate"]["pending_update_types"])
        self.assertEqual(
            result["run"]["state_update"]["memory_update_count"],
            result["memory_write"]["gate"]["pending_update_count"],
        )
        self.assertEqual("verified", result["memory_write"]["verification"]["status"])
        self.assertTrue(result["memory_write"]["item_mappings"])
        self.assertTrue(all(mapping["status"] == "written" for mapping in result["memory_write"]["item_mappings"]))
        self.assertEqual(result["memory_write"], result["run"]["memory"]["writeback"])
        self.assertTrue(outbox.exists())

    def test_executor_blocks_memory_writeback_when_repair_delta_has_new_problem(self) -> None:
        tmp_path = self._case_dir("executor_writer_gate")
        snapshot_path = tmp_path / "snapshot.json"
        outbox = tmp_path / "memory_outbox.jsonl"
        self._write_snapshot(snapshot_path)
        calls = {"count": 0}

        def validator(snapshot, chapter, decision):
            calls["count"] += 1
            if calls["count"] == 1:
                return {
                    "ok": False,
                    "problems": [{"code": "missing_conflict_marker", "message": "Missing conflict."}],
                }
            return {
                "ok": True,
                "problems": [{"code": "no_known_location", "message": "New spatial problem."}],
            }

        def analyzer(chapter, validation):
            return {
                "events": [{"text": "A risky rescue moved forward."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "conflicts": ["risk"],
                "validation_ok": True,
                "summary": "A risky rescue moved forward.",
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            chapter_dir=tmp_path / "chapters",
            dry_run=True,
            validator=validator,
            analyzer=analyzer,
            memory_writer=FileMemoryWriter(outbox),
        ).run_once(persist=True)

        self.assertTrue(result["committed"])
        self.assertEqual(0, result["memory_write"]["written"])
        self.assertTrue(result["memory_write"]["skipped"])
        self.assertFalse(result["memory_write"]["gate"]["allowed"])
        self.assertGreater(result["memory_write"]["gate"]["pending_update_count"], 0)
        self.assertTrue(result["memory_write"]["gate"]["pending_update_types"])
        self.assertEqual(
            result["run"]["state_update"]["memory_update_count"],
            result["memory_write"]["gate"]["pending_update_count"],
        )
        self.assertEqual("not_applicable", result["memory_write"]["verification"]["status"])
        self.assertEqual("gate_blocked", result["memory_write"]["verification"]["reason"])
        self.assertIn("repair_after_problems_remaining", result["memory_write"]["gate"]["reasons"])
        self.assertIn("repair_introduced_new_problem_codes", result["memory_write"]["gate"]["reasons"])
        self.assertFalse(outbox.exists())

    def test_outbox_can_be_reused_as_next_memory_input(self) -> None:
        tmp_path = self._case_dir("outbox_loop")
        first_snapshot_path = tmp_path / "snapshot_1.json"
        second_snapshot_path = tmp_path / "snapshot_2.json"
        outbox = tmp_path / "memory_outbox.jsonl"
        self._write_snapshot(first_snapshot_path)
        self._write_snapshot(second_snapshot_path)

        AgentExecutor(
            snapshot_path=first_snapshot_path,
            run_dir=tmp_path / "runs_1",
            dry_run=True,
            memory_writer=FileMemoryWriter(outbox),
        ).run_once(persist=True)

        captured_input: list[str] = []

        def generator(input_pack: str) -> str:
            captured_input.append(input_pack)
            return "At the shelter, danger forced a serum choice and created conflict with a visible cost."

        result = AgentExecutor(
            snapshot_path=second_snapshot_path,
            memory_path=outbox,
            run_dir=tmp_path / "runs_2",
            dry_run=True,
            generator=generator,
        ).run_once(persist=False)

        self.assertTrue(result["committed"])
        self.assertEqual("jsonl-outbox", result["run"]["memory"]["source"])
        self.assertIn("chapter_2_summary", captured_input[0])

    def test_executor_does_not_write_memory_for_rejected_runs(self) -> None:
        tmp_path = self._case_dir("executor_rejected")
        snapshot_path = tmp_path / "snapshot.json"
        outbox = tmp_path / "memory_outbox.jsonl"
        self._write_snapshot(snapshot_path)

        def director(snapshot, memory_context):
            return {
                "chapter_index": snapshot["chapter_index"],
                "goal": "reject_memory_write",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            }

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            director=director,
            generator=lambda input_pack: "A quiet inventory scene without pressure.",
            memory_writer=FileMemoryWriter(outbox),
        ).run_once(persist=True)

        self.assertFalse(result["committed"])
        self.assertNotIn("memory_write", result)
        self.assertFalse(outbox.exists())


if __name__ == "__main__":
    unittest.main()
