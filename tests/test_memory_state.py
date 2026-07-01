from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.executor import AgentExecutor
from core.schema import validate_schema
from core.state.builder import build_snapshot_state, build_snapshot_state_with_audit
from core.state.memory import MemoryError, load_memory_context, normalize_memory_context
from core.state.notion_export import normalize_notion_export


class MemoryStateTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_state" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_normalizes_file_memory_context(self) -> None:
        memory = normalize_memory_context(
            [
                {
                    "id": "manual:location:safehouse",
                    "type": "location",
                    "name": "Safehouse",
                    "source_run_id": "run-1",
                    "data": {"risk": "rising"},
                }
            ]
        )

        self.assertEqual("file", memory["source"])
        self.assertEqual("ready", memory["status"])
        self.assertEqual("location", memory["items"][0]["type"])
        self.assertEqual("manual:location:safehouse", memory["items"][0]["id"])
        self.assertEqual("run-1", memory["items"][0]["source_run_id"])

    def test_normalizes_memory_type_case(self) -> None:
        memory = normalize_memory_context(
            [
                {
                    "type": "Location",
                    "name": "Safehouse",
                    "data": {"risk": "rising"},
                }
            ]
        )

        self.assertEqual("location", memory["items"][0]["type"])

    def test_normalizes_partial_source_mappings(self) -> None:
        memory = normalize_memory_context(
            {
                "source": "notion-export",
                "status": "ready",
                "items": [
                    {"type": "location", "name": "Safehouse", "data": {"risk": "rising"}},
                    {"type": "character", "name": "Mira", "data": {"role": "lead"}},
                ],
                "source_mappings": [
                    {
                        "index": 1,
                        "page_id": "page-mira",
                        "page_url": "https://notion.test/page-mira",
                    }
                ],
            }
        )

        self.assertEqual(2, len(memory["source_mappings"]))
        self.assertEqual(0, memory["source_mappings"][0]["index"])
        self.assertEqual("notion-export", memory["source_mappings"][0]["source"])
        self.assertEqual("Safehouse", memory["source_mappings"][0]["name"])
        self.assertEqual(1, memory["source_mappings"][1]["index"])
        self.assertEqual("Mira", memory["source_mappings"][1]["name"])
        self.assertEqual("page-mira", memory["source_mappings"][1]["page_id"])

    def test_rejects_duplicate_source_mapping_index(self) -> None:
        with self.assertRaisesRegex(MemoryError, "duplicated"):
            normalize_memory_context(
                {
                    "source": "test",
                    "status": "ready",
                    "items": [{"type": "location", "name": "Safehouse", "data": {}}],
                    "source_mappings": [
                        {"index": 0, "source": "test", "type": "location", "name": "Safehouse"},
                        {"index": 0, "source": "test", "type": "location", "name": "Safehouse"},
                    ],
                }
            )

    def test_rejects_out_of_range_source_mapping_index(self) -> None:
        with self.assertRaisesRegex(MemoryError, "out of range"):
            normalize_memory_context(
                {
                    "source": "test",
                    "status": "ready",
                    "items": [{"type": "location", "name": "Safehouse", "data": {}}],
                    "source_mappings": [
                        {"index": 1, "source": "test", "type": "location", "name": "Safehouse"},
                    ],
                }
            )

    def test_rejects_unsupported_memory_item_type(self) -> None:
        with self.assertRaisesRegex(MemoryError, "unsupported type"):
            normalize_memory_context([{"type": "plot_seed", "data": {"text": "unused"}}])

    def test_rejects_memory_item_without_type(self) -> None:
        with self.assertRaises(MemoryError):
            normalize_memory_context([{"data": {}}])

    def test_rejects_memory_item_with_invalid_name_type(self) -> None:
        with self.assertRaises(MemoryError):
            normalize_memory_context([{"type": "location", "name": 123, "data": {}}])

    def test_normalize_missing_memory_context_marks_no_source(self) -> None:
        memory = normalize_memory_context(None)

        self.assertEqual("none", memory["source"])
        self.assertEqual("adapter_pending", memory["status"])

    def test_load_missing_memory_file_marks_file_source(self) -> None:
        tmp_path = self._case_dir("missing_file")
        memory = load_memory_context(tmp_path / "missing_memory.json")

        self.assertEqual("file", memory["source"])
        self.assertEqual("adapter_pending", memory["status"])
        self.assertIn("missing_memory.json", memory["note"])

    def test_load_memory_context_rejects_unknown_source_mode(self) -> None:
        with self.assertRaisesRegex(MemoryError, "memory source"):
            load_memory_context(source="database")

    def test_build_snapshot_state_applies_memory_items(self) -> None:
        snapshot = build_snapshot_state(
            {"chapter_index": 1},
            {
                "source": "test",
                "status": "ready",
                "items": [
                    {"type": "world_state", "data": {"infection_level": "high"}},
                    {"type": "location", "name": "Safehouse", "data": {"risk": "rising"}},
                    {"type": "character", "name": "Mira", "data": {"role": "lead"}},
                    {"type": "constraint", "data": {"rule": "Keep the serum unresolved."}},
                ],
            },
        )

        self.assertEqual("high", snapshot["world_state"]["infection_level"])
        self.assertEqual("rising", snapshot["world_state"]["locations"]["Safehouse"]["risk"])
        self.assertEqual("lead", snapshot["characters"]["Mira"]["role"])
        self.assertEqual(1, len(snapshot["constraints"]))
        self.assertEqual(4, snapshot["memory"]["item_count"])

    def test_build_snapshot_state_deduplicates_list_memory_items(self) -> None:
        snapshot = build_snapshot_state(
            {
                "chapter_index": 2,
                "world_state": {},
                "characters": {},
                "timeline": [{"memory_id": "chapter_1:timeline_event:summary", "summary": "Existing"}],
            },
            {
                "source": "test",
                "status": "ready",
                "items": [
                    {
                        "id": "chapter_1:timeline_event:summary",
                        "type": "timeline_event",
                        "name": "chapter_1_summary",
                        "source_run_id": "run-1",
                        "data": {"chapter_index": 1, "summary": "Existing"},
                    },
                    {
                        "id": "chapter_2:timeline_event:summary",
                        "type": "timeline_event",
                        "name": "chapter_2_summary",
                        "source_run_id": "run-2",
                        "data": {"chapter_index": 2, "summary": "New"},
                    },
                    {
                        "type": "constraint",
                        "data": {"rule": "Keep serum visible."},
                    },
                    {
                        "type": "constraint",
                        "data": {"rule": "Keep serum visible."},
                    },
                ],
            },
        )

        self.assertEqual(2, len(snapshot["timeline"]))
        self.assertEqual("chapter_2:timeline_event:summary", snapshot["timeline"][1]["memory_id"])
        self.assertEqual("chapter_2_summary", snapshot["timeline"][1]["name"])
        self.assertEqual("run-2", snapshot["timeline"][1]["source_run_id"])
        self.assertEqual([{"rule": "Keep serum visible."}], snapshot["constraints"])

    def test_build_snapshot_state_deduplicates_timeline_memory_ids(self) -> None:
        snapshot = build_snapshot_state(
            {
                "chapter_index": 3,
                "world_state": {},
                "characters": {},
                "timeline": [
                    {
                        "chapter_index": 2,
                        "memory_id": "chapter_2:timeline_event:chapter_2_summary",
                        "memory_ids": [
                            "chapter_2:timeline_event:chapter_2_summary",
                            "chapter_2:timeline_event:chapter_2_event_1",
                        ],
                        "summary": "Existing summary.",
                    }
                ],
            },
            {
                "source": "jsonl-outbox",
                "status": "ready",
                "items": [
                    {
                        "id": "chapter_2:timeline_event:chapter_2_summary",
                        "type": "timeline_event",
                        "name": "chapter_2_summary",
                        "data": {"chapter_index": 2, "summary": "Existing summary."},
                    },
                    {
                        "id": "chapter_2:timeline_event:chapter_2_event_1",
                        "type": "timeline_event",
                        "name": "chapter_2_event_1",
                        "data": {"chapter_index": 2, "text": "Existing event."},
                    },
                ],
            },
        )

        self.assertEqual(1, len(snapshot["timeline"]))
        self.assertEqual("Existing summary.", snapshot["timeline"][0]["summary"])

    def test_build_snapshot_state_with_audit_tracks_applied_and_skipped_memory(self) -> None:
        result = build_snapshot_state_with_audit(
            {
                "chapter_index": 2,
                "world_state": {},
                "characters": {},
                "timeline": [{"memory_id": "chapter_1:timeline_event:summary", "summary": "Existing"}],
            },
            {
                "source": "test",
                "status": "ready",
                "items": [
                    {"type": "location", "name": "Safehouse", "data": {"risk": "rising"}},
                    {"type": "character", "data": {"role": "unnamed"}},
                    {
                        "id": "chapter_1:timeline_event:summary",
                        "type": "timeline_event",
                        "data": {"chapter_index": 1, "summary": "Existing"},
                    },
                    {"type": "constraint", "data": {"rule": "Keep serum visible."}},
                ],
            },
        )

        snapshot = result["snapshot"]
        audit = result["audit"]
        self.assertEqual("rising", snapshot["world_state"]["locations"]["Safehouse"]["risk"])
        self.assertEqual(4, audit["item_count"])
        self.assertEqual(2, audit["applied_count"])
        self.assertEqual(2, audit["skipped_count"])
        self.assertEqual(1, audit["deduplicated_count"])
        self.assertEqual(
            [{"type": "constraint", "count": 1}, {"type": "location", "count": 1}],
            audit["applied_type_counts"],
        )
        self.assertEqual(
            [{"type": "character", "count": 1}, {"type": "timeline_event", "count": 1}],
            audit["skipped_type_counts"],
        )
        self.assertEqual(0, audit["skipped_blocking_count"])
        self.assertEqual("upsert_location", audit["applied_items"][0]["operation"])
        self.assertEqual("test", audit["applied_items"][0]["source_mapping"]["source"])
        self.assertEqual(0, audit["applied_items"][0]["source_mapping"]["index"])
        self.assertEqual("Safehouse", audit["applied_items"][0]["source_mapping"]["name"])
        self.assertEqual("missing name", audit["skipped_items"][0]["reason"])
        self.assertEqual("missing_name", audit["skipped_items"][0]["reason_code"])
        self.assertEqual(1, audit["skipped_items"][0]["source_mapping"]["index"])
        self.assertEqual("medium", audit["skipped_items"][0]["severity"])
        self.assertFalse(audit["skipped_items"][0]["blocking"])
        self.assertEqual("memory_quality", audit["skipped_items"][0]["category"])
        self.assertEqual("duplicate", audit["skipped_items"][1]["reason"])
        self.assertEqual("duplicate_memory", audit["skipped_items"][1]["reason_code"])
        self.assertEqual("chapter_1:timeline_event:summary", audit["skipped_items"][1]["source_mapping"]["memory_id"])
        self.assertEqual("low", audit["skipped_items"][1]["severity"])
        self.assertEqual(
            [{"reason_code": "duplicate_memory", "count": 1}, {"reason_code": "missing_name", "count": 1}],
            audit["skipped_reason_counts"],
        )
        self.assertEqual(
            [{"severity": "low", "count": 1}, {"severity": "medium", "count": 1}],
            audit["skipped_severity_counts"],
        )
        self.assertIs(audit, validate_schema(audit, "snapshot_builder_audit.schema.json"))

    def test_executor_includes_memory_in_input_pack(self) -> None:
        tmp_path = self._case_dir("executor_memory")
        snapshot_path = tmp_path / "snapshot.json"
        memory_path = tmp_path / "memory.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        memory_path.write_text(
            json.dumps(
                {
                    "source": "test",
                    "status": "ready",
                    "items": [
                        {"type": "location", "name": "Safehouse", "data": {"risk": "rising"}},
                        {"type": "constraint", "data": {"rule": "Keep the serum unresolved."}},
                    ],
                }
            ),
            encoding="utf-8",
        )

        captured_input: list[str] = []

        def generator(input_pack: str) -> str:
            captured_input.append(input_pack)
            return (
                "Safehouse 的警报响起后，队伍发现撤离路线已经被封死。主角必须在保护样本和救回同伴之间做出选择，"
                "这个决定立刻引发公开冲突，并让所有人意识到危险正在逼近。"
            )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=memory_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            generator=generator,
        ).run_once(persist=False)

        self.assertTrue(result["validation"]["ok"])
        self.assertIn("Safehouse", captured_input[0])
        self.assertIn("Keep the serum unresolved.", captured_input[0])

    def test_load_memory_context_from_file(self) -> None:
        tmp_path = self._case_dir("file")
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(
            json.dumps({"source": "test", "status": "ready", "items": [{"type": "world_state", "data": {}}]}),
            encoding="utf-8",
        )

        memory = load_memory_context(memory_path)

        self.assertEqual("test", memory["source"])
        self.assertEqual(1, len(memory["items"]))
        self.assertEqual(str(memory_path), memory["source_mappings"][0]["path"])
        self.assertEqual(0, memory["source_mappings"][0]["index"])

    def test_load_memory_context_accepts_explicit_file_source(self) -> None:
        tmp_path = self._case_dir("explicit_file")
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(
            json.dumps({"source": "manual-file", "status": "ready", "items": []}),
            encoding="utf-8",
        )

        memory = load_memory_context(memory_path, source="file")

        self.assertEqual("manual-file", memory["source"])
        self.assertEqual("ready", memory["status"])

    def test_load_memory_context_from_jsonl_outbox(self) -> None:
        tmp_path = self._case_dir("jsonl")
        memory_path = tmp_path / "memory_outbox.jsonl"
        memory_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "location", "name": "shelter", "data": {"risk": "rising"}}),
                    "",
                    json.dumps({"type": "timeline_event", "name": "chapter_1_summary", "data": {"summary": "A"}}),
                ]
            ),
            encoding="utf-8",
        )

        memory = load_memory_context(memory_path)

        self.assertEqual("jsonl-outbox", memory["source"])
        self.assertEqual(2, len(memory["items"]))
        self.assertEqual("shelter", memory["items"][0]["name"])
        self.assertEqual(str(memory_path), memory["source_mappings"][0]["path"])
        self.assertEqual(1, memory["source_mappings"][0]["line_number"])
        self.assertEqual(3, memory["source_mappings"][1]["line_number"])

    def test_load_memory_context_rejects_invalid_jsonl_line(self) -> None:
        tmp_path = self._case_dir("bad_jsonl")
        memory_path = tmp_path / "memory_outbox.jsonl"
        memory_path.write_text('{"type": "location", "data": {}}\nnot json\n', encoding="utf-8")

        with self.assertRaises(MemoryError):
            load_memory_context(memory_path)

    def test_load_memory_context_rejects_invalid_jsonl_item_schema(self) -> None:
        tmp_path = self._case_dir("bad_jsonl_schema")
        memory_path = tmp_path / "memory_outbox.jsonl"
        memory_path.write_text(json.dumps({"type": "location", "name": 42, "data": {}}), encoding="utf-8")

        with self.assertRaises(MemoryError):
            load_memory_context(memory_path)

    def test_load_memory_context_rejects_unknown_jsonl_item_type(self) -> None:
        tmp_path = self._case_dir("bad_jsonl_type")
        memory_path = tmp_path / "memory_outbox.jsonl"
        memory_path.write_text(json.dumps({"type": "plot_seed", "data": {}}), encoding="utf-8")

        with self.assertRaisesRegex(MemoryError, "unsupported type"):
            load_memory_context(memory_path)

    def test_normalizes_notion_export_pages(self) -> None:
        memory = normalize_notion_export(
            {
                "pages": [
                    {
                        "properties": {
                            "Type": "location",
                            "Name": "shelter",
                            "Memory ID": "manual:location:shelter",
                            "Risk": "rising",
                        }
                    },
                    {
                        "properties": {
                            "Type": "character",
                            "Name": "Mira",
                            "Current Location": "shelter",
                        }
                    },
                ]
            }
        )

        self.assertEqual("notion-export", memory["source"])
        self.assertEqual("location", memory["items"][0]["type"])
        self.assertEqual("shelter", memory["items"][0]["name"])
        self.assertEqual("manual:location:shelter", memory["items"][0]["id"])
        self.assertEqual("rising", memory["items"][0]["data"]["risk"])
        self.assertEqual("shelter", memory["items"][1]["data"]["current_location"])
        self.assertEqual("notion-export", memory["source_mappings"][0]["source"])
        self.assertEqual(0, memory["source_mappings"][0]["page_index"])

    def test_normalizes_real_notion_api_property_shapes(self) -> None:
        memory = normalize_notion_export(
            {
                "pages": [
                    {
                        "id": "page-1",
                        "url": "https://notion.test/page-1",
                        "properties": {
                            "Type": {"type": "select", "select": {"name": "location"}},
                            "Name": {
                                "type": "title",
                                "title": [
                                    {
                                        "type": "text",
                                        "text": {"content": "shelter"},
                                        "plain_text": "shelter",
                                    }
                                ],
                            },
                            "Memory ID": {
                                "type": "rich_text",
                                "rich_text": [
                                    {
                                        "type": "text",
                                        "text": {"content": "manual:location:shelter"},
                                        "plain_text": "manual:location:shelter",
                                    }
                                ],
                            },
                            "Data": {
                                "type": "rich_text",
                                "rich_text": [
                                    {
                                        "type": "text",
                                        "text": {"content": '{"risk": "rising", "capacity": 12}'},
                                        "plain_text": '{"risk": "rising", "capacity": 12}',
                                    }
                                ],
                            },
                            "Tags": {
                                "type": "multi_select",
                                "multi_select": [{"name": "sealed"}, {"name": "medical"}],
                            },
                        },
                    },
                    {
                        "id": "page-2",
                        "properties": {
                            "Type": {"type": "select", "select": {"name": "character"}},
                            "Name": {
                                "type": "title",
                                "title": [
                                    {
                                        "type": "text",
                                        "text": {"content": "Mira"},
                                        "plain_text": "Mira",
                                    }
                                ],
                            },
                            "Current Location": {
                                "type": "rich_text",
                                "rich_text": [
                                    {
                                        "type": "text",
                                        "text": {"content": "shelter"},
                                        "plain_text": "shelter",
                                    }
                                ],
                            },
                            "Traits": {
                                "type": "multi_select",
                                "multi_select": [{"name": "medic"}, {"name": "guarded"}],
                            },
                        },
                    }
                ]
            }
        )

        item = memory["items"][0]
        self.assertEqual("location", item["type"])
        self.assertEqual("shelter", item["name"])
        self.assertEqual("manual:location:shelter", item["id"])
        self.assertEqual("rising", item["data"]["risk"])
        self.assertEqual(12, item["data"]["capacity"])
        self.assertEqual("page-1", memory["source_mappings"][0]["page_id"])
        self.assertEqual("https://notion.test/page-1", memory["source_mappings"][0]["page_url"])
        self.assertEqual("shelter", memory["items"][1]["data"]["current_location"])
        self.assertEqual(["medic", "guarded"], memory["items"][1]["data"]["traits"])

    def test_normalizes_common_notion_api_property_shapes(self) -> None:
        memory = normalize_notion_export(
            {
                "pages": [
                    {
                        "id": "page-1",
                        "properties": {
                            "Type": {"type": "select", "select": {"name": "timeline_event"}},
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Raid window"}],
                            },
                            "Status": {"type": "status", "status": {"name": "Active"}},
                            "Event Date": {
                                "type": "date",
                                "date": {"start": "2026-06-30", "end": "2026-07-01", "time_zone": None},
                            },
                            "Reference URL": {"type": "url", "url": "https://notion.test/ref"},
                            "Contact Email": {"type": "email", "email": "mira@example.test"},
                            "Contact Phone": {"type": "phone_number", "phone_number": "+1-555-0100"},
                            "Owner": {"type": "people", "people": [{"name": "Mira"}, {"id": "user-2"}]},
                            "Related": {"type": "relation", "relation": [{"id": "page-2"}, {"id": "page-3"}]},
                            "Attachment": {
                                "type": "files",
                                "files": [
                                    {
                                        "name": "map.png",
                                        "type": "external",
                                        "external": {"url": "https://files.test/map.png"},
                                    }
                                ],
                            },
                            "Created": {"type": "created_time", "created_time": "2026-06-30T00:00:00.000Z"},
                            "Edited By": {"type": "last_edited_by", "last_edited_by": {"name": "Editor"}},
                        },
                    }
                ]
            }
        )

        data = memory["items"][0]["data"]
        self.assertEqual("Active", data["status"])
        self.assertEqual({"start": "2026-06-30", "end": "2026-07-01"}, data["event_date"])
        self.assertEqual("https://notion.test/ref", data["reference_url"])
        self.assertEqual("mira@example.test", data["contact_email"])
        self.assertEqual("+1-555-0100", data["contact_phone"])
        self.assertEqual(["Mira", "user-2"], data["owner"])
        self.assertEqual(["page-2", "page-3"], data["related"])
        self.assertEqual([{"name": "map.png", "url": "https://files.test/map.png"}], data["attachment"])
        self.assertEqual("2026-06-30T00:00:00.000Z", data["created"])
        self.assertEqual("Editor", data["edited_by"])

    def test_load_memory_context_detects_notion_export_file(self) -> None:
        tmp_path = self._case_dir("notion_file")
        memory_path = tmp_path / "notion.json"
        memory_path.write_text(
            json.dumps(
                {
                    "pages": [
                        {
                            "properties": {
                                "Type": "location",
                                "Name": "shelter",
                                "Risk": "rising",
                            }
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        memory = load_memory_context(memory_path)

        self.assertEqual("notion-export", memory["source"])
        self.assertEqual("shelter", memory["items"][0]["name"])
        self.assertEqual(str(memory_path), memory["source_mappings"][0]["path"])

    def test_executor_accepts_notion_export_memory_file(self) -> None:
        tmp_path = self._case_dir("executor_notion")
        snapshot_path = tmp_path / "snapshot.json"
        memory_path = tmp_path / "notion.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        memory_path.write_text(
            json.dumps(
                {
                    "pages": [
                        {"properties": {"Type": "location", "Name": "shelter"}},
                        {"properties": {"Type": "constraint", "Required Terms": ["serum"]}},
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=memory_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            generator=lambda input_pack: (
                "At the shelter, danger forced a choice over the serum and created conflict with a visible cost."
            ),
        ).run_once(persist=False)

        self.assertTrue(result["validation"]["ok"])
        self.assertEqual("notion-export", result["run"]["memory"]["source"])

    def test_executor_accepts_jsonl_outbox_memory_file(self) -> None:
        tmp_path = self._case_dir("executor_jsonl")
        snapshot_path = tmp_path / "snapshot.json"
        memory_path = tmp_path / "memory_outbox.jsonl"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        memory_path.write_text(
            json.dumps({"type": "location", "name": "shelter", "data": {"risk": "rising"}}),
            encoding="utf-8",
        )

        captured_input: list[str] = []

        def generator(input_pack: str) -> str:
            captured_input.append(input_pack)
            return "At the shelter, danger forced a serum choice and created conflict with a visible cost."

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=memory_path,
            run_dir=tmp_path / "runs",
            dry_run=True,
            generator=generator,
        ).run_once(persist=False)

        self.assertTrue(result["validation"]["ok"])
        self.assertEqual("jsonl-outbox", result["run"]["memory"]["source"])
        self.assertIn("shelter", captured_input[0])


if __name__ == "__main__":
    unittest.main()
