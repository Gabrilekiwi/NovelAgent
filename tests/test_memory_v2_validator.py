from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from core.memory_v2 import MemoryV2ValidationError, create_empty_canonical_memory, validate_canonical_memory
from core.schema import validate_schema_keywords


class MemoryV2ValidatorTest(unittest.TestCase):
    def test_empty_canonical_memory_matches_schema(self) -> None:
        memory = create_empty_canonical_memory()

        self.assertIs(memory, validate_canonical_memory(memory))
        self.assertEqual("2.0", memory["schema_version"])
        self.assertEqual(1, memory["revision"])
        self.assertEqual("default", memory["book_id"])
        self.assertEqual("Untitled", memory["title"])
        self.assertEqual("zh-CN", memory["language"])
        self.assertEqual({}, memory["current_state"])
        self.assertEqual({}, memory["characters"])
        self.assertEqual({}, memory["locations"])
        self.assertEqual([], memory["open_threads"])
        self.assertEqual({}, memory["chapter_states"])
        self.assertEqual([], memory["style_rules"])
        self.assertEqual({}, memory["source_index"])
        self.assertEqual({}, memory["source_resolution"])

    def test_schema_asset_uses_supported_keywords(self) -> None:
        schema = json.loads(Path("schemas/canonical_memory.schema.json").read_text(encoding="utf-8"))

        self.assertIs(schema, validate_schema_keywords(schema, "canonical_memory.schema.json"))

    def test_rejects_missing_required_field(self) -> None:
        memory = create_empty_canonical_memory()
        memory.pop("book_id")

        with self.assertRaisesRegex(MemoryV2ValidationError, "book_id is required"):
            validate_canonical_memory(memory)

    def test_rejects_unknown_top_level_field(self) -> None:
        memory = create_empty_canonical_memory()
        memory["unexpected"] = True

        with self.assertRaisesRegex(MemoryV2ValidationError, "unexpected is not allowed"):
            validate_canonical_memory(memory)

    def test_accepts_minimal_typed_records(self) -> None:
        memory = copy.deepcopy(create_empty_canonical_memory())
        memory["characters"]["char:mira"] = {"name": "Mira", "data": {"role": "lead"}}
        memory["locations"]["loc:shelter"] = {"name": "shelter", "data": {"risk": "rising"}}
        memory["timeline"].append({"id": "event:1", "chapter_index": 1, "summary": "Mira arrives.", "data": {}})
        memory["open_threads"].append({"id": "thread:1", "title": "Find serum.", "status": "open", "data": {}})
        memory["constraints"].append(
            {"id": "constraint:1", "text": "Keep serum unresolved.", "status": "active", "data": {}}
        )
        memory["style_rules"].append({"id": "style:1", "rule": "Keep prose direct.", "status": "active", "data": {}})

        self.assertIs(memory, validate_canonical_memory(memory))

    def test_rejects_invalid_nested_record(self) -> None:
        memory = copy.deepcopy(create_empty_canonical_memory())
        memory["constraints"].append({"id": "constraint:1", "text": "Keep serum unresolved.", "status": "open", "data": {}})

        with self.assertRaisesRegex(MemoryV2ValidationError, "status must be one of"):
            validate_canonical_memory(memory)


if __name__ == "__main__":
    unittest.main()
