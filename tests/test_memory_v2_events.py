from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from core.memory_v2 import (
    MemoryEventValidationError,
    append_memory_event,
    append_memory_events,
    create_memory_event,
    load_memory_events,
    validate_memory_event,
)


class MemoryV2EventsTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_v2_events" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _event(self, event_id: str = "evt_000002", revision: int = 2) -> dict:
        return create_memory_event(
            event_id=event_id,
            revision=revision,
            op="upsert_character",
            subject_id="char_lin_xue",
            field="characters.char_lin_xue",
            old_value=None,
            new_value={"name": "Lin Xue", "data": {}},
            source={"kind": "local_memory", "patch_id": "patch_import_v1_default"},
        )

    def test_create_and_validate_memory_event(self) -> None:
        event = self._event()

        self.assertEqual("2.1", event["schema_version"])
        self.assertEqual("evt_000002", event["event_id"])
        self.assertEqual(64, len(event["event_hash"]))
        self.assertIs(event, validate_memory_event(event))

    def test_reads_legacy_20_event_without_hash(self) -> None:
        event = self._event()
        event["schema_version"] = "2.0"
        event.pop("event_hash")

        self.assertIs(event, validate_memory_event(event))

    def test_rejects_tampered_21_event(self) -> None:
        event = self._event()
        event["new_value"]["name"] = "Changed"

        with self.assertRaisesRegex(MemoryEventValidationError, "hash mismatch"):
            validate_memory_event(event)

    def test_rejects_missing_required_fields(self) -> None:
        required_fields = ("event_id", "revision", "op", "source", "metadata")
        for field in required_fields:
            with self.subTest(field=field):
                event = self._event()
                event.pop(field)
                with self.assertRaises(MemoryEventValidationError):
                    validate_memory_event(event)

    def test_append_single_event_to_jsonl(self) -> None:
        path = self._case_dir("single") / "memory_events.jsonl"
        event = self._event()

        returned = append_memory_event(path, event)

        self.assertEqual(event, returned)
        self.assertEqual([event], load_memory_events(path))

    def test_append_multiple_events_to_jsonl(self) -> None:
        path = self._case_dir("multiple") / "memory_events.jsonl"
        events = [self._event("evt_000002", 2), self._event("evt_000003", 3)]

        returned = append_memory_events(path, events)

        self.assertEqual(events, returned)
        self.assertEqual(events, load_memory_events(path))

    def test_load_missing_or_empty_event_file(self) -> None:
        case_dir = self._case_dir("missing")
        missing_path = case_dir / "missing.jsonl"
        empty_path = case_dir / "empty.jsonl"
        empty_path.write_text("\n\n", encoding="utf-8")

        self.assertEqual([], load_memory_events(missing_path))
        self.assertEqual([], load_memory_events(empty_path))

    def test_load_rejects_invalid_json_line(self) -> None:
        path = self._case_dir("bad_json") / "memory_events.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")

        with self.assertRaisesRegex(MemoryEventValidationError, "not valid JSON"):
            load_memory_events(path)

    def test_append_memory_events_validates_all_before_writing(self) -> None:
        path = self._case_dir("all_or_none") / "memory_events.jsonl"
        valid = self._event()
        invalid = dict(valid)
        invalid.pop("op")

        with self.assertRaises(MemoryEventValidationError):
            append_memory_events(path, [valid, invalid])

        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
