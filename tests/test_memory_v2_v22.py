from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from pathlib import Path

from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    LEGACY_REDUCER_VERSION,
    REDUCER_REGISTRY,
    MemoryEventStoreError,
    MemoryEventValidationError,
    MemoryIntegrityError,
    MemoryProjectionError,
    MemoryReducerError,
    MemoryV2ValidationError,
    append_memory_event,
    apply_memory_patch,
    canonical_json_hash,
    commit_memory_patch,
    create_empty_canonical_memory,
    create_empty_typed_canonical_memory,
    create_genesis_memory_batch,
    create_memory_checkpoint,
    create_memory_event,
    create_memory_event_batch,
    create_memory_patch,
    load_memory_event_batches,
    load_memory_events,
    memory_event_hash,
    memory_patch_content_hash,
    memory_projection_hash,
    rebuild_canonical_memory,
    rebuild_memory_projections,
    replay_memory_events,
    reducer_version_for_batch,
    reducer_version_for_event,
    resolve_memory_reducer,
    validate_canonical_memory,
    validate_memory_event,
    validate_memory_event_batch,
    validate_memory_projection_receipt,
    verify_memory_event_evidence,
    write_memory_event_batch,
)


class MemoryV22Test(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_memory_v2_v22" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _context(body: str = "林雪在旧站握紧钥匙。") -> dict:
        return {
            "chapter_body": body,
            "evidence_spans": [{"start_char": 0, "end_char": 2, "quote": "林雪"}],
            "authority_epoch": 1,
        }

    def test_legacy_event_jsonl_golden_bytes_are_unchanged(self) -> None:
        event = create_memory_event(
            event_id="evt_000002",
            revision=2,
            op="update_world",
            field="world",
            old_value={},
            new_value={"level": 1},
            source={"kind": "test", "patch_id": "p1"},
        )
        path = self._case_dir("legacy_event_golden") / "events.jsonl"
        append_memory_event(path, event)

        expected = (
            '{"event_hash": "aa8f6f8fec9b6b0e7fe0f2fc1fa847d1bc4e6baf264ea41c6c2dd5449c95bf82", '
            '"event_id": "evt_000002", "field": "world", "metadata": {"created_by": '
            '"NovelAgent Memory System V2"}, "new_value": {"level": 1}, "old_value": {}, '
            '"op": "update_world", "revision": 2, "schema_version": "2.1", "source": '
            '{"kind": "test", "patch_id": "p1"}}'
            + os.linesep
        ).encode("utf-8")
        self.assertEqual(expected, path.read_bytes())

        legacy20 = create_memory_event(
            event_id="evt_legacy",
            revision=2,
            op="update_world",
            field="world",
            old_value={},
            new_value={"level": 1},
            source={"kind": "test"},
            schema_version="2.0",
        )
        legacy_path = self._case_dir("legacy20_event_golden") / "events.jsonl"
        append_memory_event(legacy_path, legacy20)
        expected20 = (
            '{"event_id": "evt_legacy", "field": "world", "metadata": {"created_by": '
            '"NovelAgent Memory System V2"}, "new_value": {"level": 1}, "old_value": {}, '
            '"op": "update_world", "revision": 2, "schema_version": "2.0", "source": '
            '{"kind": "test"}}'
            + os.linesep
        ).encode("utf-8")
        self.assertEqual(expected20, legacy_path.read_bytes())
        before_read = legacy_path.read_bytes()
        loaded = load_memory_events(legacy_path)
        self.assertEqual(before_read, legacy_path.read_bytes())
        self.assertEqual("2.1", loaded[0]["schema_version"])
        self.assertEqual(memory_event_hash(loaded[0]), loaded[0]["event_hash"])
        self.assertEqual(
            "9171ce099a6408e06153242811148d805f8aa1b6d31e40044fec22d9edfdf611",
            loaded[0]["event_hash"],
        )
        self.assertNotEqual(legacy20, loaded[0])

    def test_legacy_batch_checkpoint_and_projection_hashes_are_frozen(self) -> None:
        memory = create_empty_canonical_memory(book_id="b", title="B")
        self.assertEqual("aae36f6c0b872f9545b26b95d5907cde7573f3469a13870b7eec806b46bbefcb", canonical_json_hash(memory))
        patch = create_memory_patch(
            patch_id="p1",
            source_kind="test",
            operations=[{"op": "update_world", "value": {"level": 1}}],
        )
        updated, events = apply_memory_patch(memory, patch, reducer_version=LEGACY_REDUCER_VERSION)
        batch = create_memory_event_batch(
            book_id="b",
            patch=patch,
            events=events,
            expected_revision=1,
            previous_batch_hash=None,
            source_project_digest="a" * 64,
            context_digest="b" * 64,
            base_projection=memory,
        )
        checkpoint = create_memory_checkpoint(
            projection=updated,
            last_batch=batch,
            committed_chapter_count=0,
            patch_index={"p1": memory_patch_content_hash(patch)},
            quality_state={},
        )

        self.assertNotIn("reducer_version", batch)
        self.assertNotIn("reducer_version", checkpoint)
        self.assertEqual(LEGACY_REDUCER_VERSION, reducer_version_for_event(events[0]))
        self.assertEqual(LEGACY_REDUCER_VERSION, reducer_version_for_batch(batch))
        self.assertEqual("b86576e5a3d4857b428ffcf6af2b02f97b30d4ca9da0ab3e4a336ec2634365fb", events[0]["event_hash"])
        self.assertEqual("d89c0815707bc7413d3573b7b13c9fe3e8b10341f5963a04b548c7eafd6ef329", batch["batch_hash"])
        self.assertEqual("746577559da7bbe32da95ce69186745e75be9239ecaee2875e6b90eac205a53a", checkpoint["checkpoint_hash"])
        self.assertEqual("7e2ac850b673b918de72ac6f04d79e2167e4b77bbf86518f2c5167f1a03019dc", memory_projection_hash(updated))
        store = self._case_dir("legacy_replay_golden") / "events"
        write_memory_event_batch(store, batch)
        replay = replay_memory_events(store, use_checkpoint=False)
        self.assertEqual("2.1", replay["schema_version"])
        self.assertNotIn("reducer_version", replay)
        self.assertEqual("7e2ac850b673b918de72ac6f04d79e2167e4b77bbf86518f2c5167f1a03019dc", replay["projection_hash"])

    def test_reducer_registry_is_frozen_and_unknown_versions_fail_closed(self) -> None:
        self.assertEqual({LEGACY_REDUCER_VERSION, CURRENT_REDUCER_VERSION}, set(REDUCER_REGISTRY))
        with self.assertRaises(TypeError):
            REDUCER_REGISTRY["memory-reducer-9.9"] = lambda *_args, **_kwargs: None  # type: ignore[index]
        with self.assertRaisesRegex(MemoryReducerError, "unsupported"):
            resolve_memory_reducer("memory-reducer-9.9")

        batch = create_genesis_memory_batch(
            book_id="b",
            title="B",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        batch["reducer_version"] = "memory-reducer-9.9"
        with self.assertRaisesRegex(MemoryEventStoreError, "unsupported"):
            validate_memory_event_batch(batch)

    def test_typed_canonical_memory_covers_required_story_domains(self) -> None:
        memory = create_empty_typed_canonical_memory(book_id="b", title="B")
        memory["revision"] = 9
        memory["characters"] = {
            "lin": {"id": "lin", "name": "林雪", "status": "active", "data": {}}
        }
        memory["locations"] = {
            "station": {"id": "station", "name": "旧站", "status": "active", "data": {}}
        }
        memory["relationships"] = {
            "lin-self-no": {
                "id": "lin-self-no",
                "source_character_id": "lin",
                "target_character_id": "other",
                "kind": "ally",
                "status": "active",
                "data": {},
            }
        }
        memory["characters"]["other"] = {
            "id": "other", "name": "周野", "status": "active", "data": {}
        }
        memory["injuries"] = {
            "inj-1": {
                "id": "inj-1", "character_id": "lin", "description": "左臂擦伤",
                "severity": "minor", "status": "healing", "data": {},
            }
        }
        memory["inventories"] = {
            "bag": {
                "id": "bag", "owner_id": "lin", "items": {
                    "key": {"name": "钥匙", "quantity": 1, "status": "held"}
                }, "data": {},
            }
        }
        memory["resources"] = {
            "water": {"id": "water", "name": "净水", "quantity": 2, "unit": "瓶", "status": "available", "data": {}}
        }
        memory["glossary"] = {
            "erosion": {"id": "erosion", "term": "侵蚀", "definition": "异常污染", "status": "active", "data": {}}
        }
        memory["corruption"] = {
            "lin-corruption": {"id": "lin-corruption", "subject_id": "lin", "level": 12, "status": "stable", "data": {}}
        }
        memory["story_time"] = {"label": "灾变后155分钟", "elapsed_minutes": 155, "chapter_index": 10, "scene_index": 2}
        memory["foreshadowing"] = {
            "signal": {
                "id": "signal", "description": "三短一长敲击", "status": "resolved",
                "introduced_revision": 2, "resolved_revision": 8, "data": {},
            }
        }

        self.assertIs(memory, validate_canonical_memory(memory))
        snapshot = rebuild_memory_projections(memory)["snapshot"]
        self.assertEqual(10, snapshot["chapter_index"])
        self.assertEqual(memory["story_time"], snapshot["story_time"])
        self.assertEqual(memory["relationships"], snapshot["relationships"])
        self.assertEqual(memory["injuries"], snapshot["injuries"])
        self.assertEqual(memory["inventories"], snapshot["inventories"])
        self.assertEqual(memory["resources"], snapshot["resources"])
        self.assertEqual(memory["glossary"], snapshot["glossary"])
        self.assertEqual(memory["corruption"], snapshot["corruption"])
        self.assertEqual(memory["foreshadowing"], snapshot["foreshadowing"])
        self.assertEqual(1, snapshot["memory_v2"]["authority_epoch"])

        broken = copy.deepcopy(memory)
        broken["foreshadowing"]["signal"]["resolved_revision"] = 1
        with self.assertRaisesRegex(MemoryV2ValidationError, "cannot precede"):
            validate_canonical_memory(broken)

    def test_event_requires_exact_evidence_and_detects_field_tampering(self) -> None:
        body = "林雪在旧站握紧钥匙。"
        event = create_memory_event(
            event_id="evt_000002",
            revision=2,
            op="upsert_character",
            subject_id="lin",
            field="characters.lin",
            source={"kind": "chapter", "patch_id": "p1"},
            schema_version="2.2",
            before=None,
            after={"id": "lin", "name": "林雪"},
            precondition={
                "expected_revision": 1,
                "expected_head_event_hash": None,
                "expected_field_hash": canonical_json_hash(None),
            },
            chapter_body=body,
            evidence_spans=[{"start_char": 0, "end_char": 2, "quote": "林雪"}],
            authority_epoch=1,
            reducer_version=CURRENT_REDUCER_VERSION,
        )
        self.assertTrue(verify_memory_event_evidence(event, body))
        self.assertEqual({"start": 0, "end": 2, "quote": "林雪"}, event["evidence_spans"][0])

        with self.assertRaisesRegex(MemoryEventValidationError, "does not match chapter_body"):
            create_memory_event(
                event_id="evt_000002",
                revision=2,
                op="upsert_character",
                field="characters.lin",
                source={"kind": "chapter"},
                schema_version="2.2",
                before=None,
                after={},
                precondition={
                    "expected_revision": 1,
                    "expected_head_event_hash": None,
                    "expected_field_hash": canonical_json_hash(None),
                },
                chapter_body=body,
                evidence_spans=[{"start_char": 1, "end_char": 3, "quote": "林雪"}],
                authority_epoch=1,
            )

        tampered = copy.deepcopy(event)
        tampered["after"]["name"] = "周野"
        with self.assertRaisesRegex(MemoryEventValidationError, "event_hash mismatch"):
            validate_memory_event(tampered)

        forged_precondition = copy.deepcopy(event)
        forged_precondition["precondition"]["expected_field_hash"] = "f" * 64
        forged_precondition["event_hash"] = canonical_json_hash(forged_precondition, exclude_fields=("event_hash",))
        with self.assertRaisesRegex(MemoryEventValidationError, "expected_field_hash mismatch"):
            validate_memory_event(forged_precondition)

    def test_genesis_delete_replay_and_projection_rebuild_are_deterministic(self) -> None:
        root = self._case_dir("genesis_delete_rebuild")
        store = root / "events"
        canonical_path = root / "canonical_memory.json"
        genesis = create_genesis_memory_batch(
            book_id="book-1",
            title="旧站",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        self.assertEqual(CURRENT_REDUCER_VERSION, reducer_version_for_batch(genesis))
        write_memory_event_batch(store, genesis)

        patch = create_memory_patch(
            patch_id="chapter-1",
            source_kind="chapter",
            operations=[
                {"op": "upsert_character", "id": "lin", "value": {"name": "林雪", "status": "active"}},
                {"op": "upsert_location", "id": "station", "value": {"name": "旧站", "status": "active"}},
                {"op": "upsert_glossary_entry", "id": "signal", "value": {"term": "三短一长", "definition": "旧站暗号"}},
                {"op": "upsert_foreshadowing", "id": "door", "value": {"description": "封闭门后的脚步"}},
                {"op": "update_story_time", "value": {"label": "灾变后15分钟", "elapsed_minutes": 15, "chapter_index": 1}},
                {"op": "append_timeline_event", "id": "arrival", "value": {"summary": "林雪抵达旧站", "chapter_index": 1}},
            ],
        )
        committed = commit_memory_patch(
            store_dir=store,
            canonical_path=canonical_path,
            patch=patch,
            source_project_digest="c" * 64,
            context_digest="d" * 64,
            batch_kind="chapter",
            checkpoint_interval=1,
            event_context=self._context(),
        )
        self.assertEqual("2.2", committed["batch"]["schema_version"])
        self.assertEqual(CURRENT_REDUCER_VERSION, committed["checkpoint"]["reducer_version"])

        delete_patch = create_memory_patch(
            patch_id="delete-signal",
            source_kind="retcon",
            operations=[{"op": "delete_record", "field": "glossary", "id": "signal"}],
        )
        deleted = commit_memory_patch(
            store_dir=store,
            canonical_path=canonical_path,
            patch=delete_patch,
            source_project_digest="e" * 64,
            context_digest="f" * 64,
            event_context=self._context(),
        )
        self.assertIsNone(deleted["events"][0]["after"])
        self.assertNotIn("signal", deleted["projection"]["glossary"])

        replay_with_checkpoint = replay_memory_events(store)
        replay_from_root = replay_memory_events(store, use_checkpoint=False)
        self.assertEqual(replay_with_checkpoint["projection"], replay_from_root["projection"])
        self.assertEqual(deleted["projection"], replay_from_root["projection"])
        self.assertEqual(3, len(load_memory_event_batches(store)))

        canonical_path.unlink()
        rebuilt = rebuild_canonical_memory(store, canonical_path)
        self.assertEqual(deleted["projection"], rebuilt)

        first = rebuild_memory_projections(rebuilt)
        second = rebuild_memory_projections(rebuilt)
        self.assertEqual(first, second)
        self.assertEqual(
            {"追踪/上下文.md", "追踪/角色状态.md", "追踪/伏笔.md", "追踪/时间线.md"},
            set(first["tracking"]),
        )
        self.assertIs(
            first["tracking_receipt"],
            validate_memory_projection_receipt(
                first["tracking_receipt"],
                canonical_memory=rebuilt,
                artifact=first["tracking"],
            ),
        )

        forged_receipt = copy.deepcopy(first["tracking_receipt"])
        forged_receipt["artifact_hash"] = "0" * 64
        with self.assertRaisesRegex(MemoryProjectionError, "receipt hash mismatch"):
            validate_memory_projection_receipt(forged_receipt)

    def test_batch_event_tampering_and_unknown_schema_fail_closed(self) -> None:
        genesis = create_genesis_memory_batch(
            book_id="b",
            title="B",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        tampered = copy.deepcopy(genesis)
        tampered["events"][0]["after"]["title"] = "Changed"
        with self.assertRaises(MemoryEventValidationError):
            validate_memory_event_batch(tampered)

        unknown = copy.deepcopy(genesis)
        unknown["schema_version"] = "9.9"
        with self.assertRaisesRegex(MemoryEventStoreError, "unsupported"):
            validate_memory_event_batch(unknown)

        with self.assertRaisesRegex(MemoryEventValidationError, "unsupported"):
            validate_memory_event({"schema_version": "9.9"})

    def test_legacy_and_typed_reducers_cannot_cross_schema_boundaries(self) -> None:
        patch = create_memory_patch(
            patch_id="p",
            operations=[{"op": "update_world", "value": {"level": 1}}],
        )
        with self.assertRaisesRegex(MemoryReducerError, "requires CanonicalMemory 2.0"):
            apply_memory_patch(
                create_empty_typed_canonical_memory(),
                patch,
                reducer_version=LEGACY_REDUCER_VERSION,
            )
        with self.assertRaisesRegex(MemoryReducerError, "requires CanonicalMemory 2.2"):
            apply_memory_patch(
                create_empty_canonical_memory(),
                patch,
                reducer_version=CURRENT_REDUCER_VERSION,
                event_context=self._context(),
            )


if __name__ == "__main__":
    unittest.main()
