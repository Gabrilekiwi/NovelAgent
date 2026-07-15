from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import unittest
import uuid

from core.engine.story_project_context import StoryProjectContextError, StoryProjectContextService
from core.memory_v2 import (
    apply_genesis_event,
    create_genesis_memory_batch,
    CURRENT_REDUCER_VERSION,
    save_canonical_memory,
    write_memory_event_batch,
)
from core.runtime_paths import RuntimePaths
from core.story_project.authority import build_authority_activation_receipt
from core.story_project.identity import ProjectIdentity, validate_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path
from core.story_project.runtime import (
    _load_memory_v2_context,
    build_generation_story_project_context,
)


class EventAuthorityRuntimeTest(unittest.TestCase):
    @staticmethod
    def _event_bytes(root: Path) -> dict[str, bytes]:
        event_store = RuntimePaths.for_story_project(root).memory_dir / "v2" / "events"
        return {
            path.relative_to(event_store).as_posix(): path.read_bytes()
            for path in sorted(event_store.rglob("*"))
            if path.is_file()
        }

    def _case(self, name: str) -> tuple[Path, dict, ProjectIdentity]:
        root = Path.cwd() / ".tmp" / "test_event_authority_runtime" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir()
        canonical_outline_path(root, 1).write_text(
            "# Chapter 1\n\ncore_event: enter the old station\n",
            encoding="utf-8",
        )
        genesis = create_genesis_memory_batch(
            book_id="book-event-runtime",
            title="Event authority",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        projection = apply_genesis_event(genesis["events"][0])
        memory_root = RuntimePaths.for_story_project(root).memory_dir / "v2"
        write_memory_event_batch(memory_root / "events", genesis)
        save_canonical_memory(memory_root / "canonical_memory.json", projection)
        activation = build_authority_activation_receipt(
            book_id=projection["book_id"],
            expected_identity_sha256="c" * 64,
            head_event_hash=projection["head_event_hash"],
            authority_epoch=projection["authority_epoch"],
            minimum_writer_contract=1,
        )
        identity = validate_project_identity(
            {
                "schema_version": "2.0",
                "book_id": projection["book_id"],
                "created_at": "2026-07-14T00:00:00+00:00",
                "root_hint": ".",
                "story_state_mode": "shadow",
                "activation": None,
                "ephemeral": False,
                "authority": {
                    "mode": "event_v1",
                    "authority_epoch": projection["authority_epoch"],
                    "head_event_hash": projection["head_event_hash"],
                    "activation_receipt": activation,
                    "minimum_writer_contract": 1,
                },
            }
        )
        return root, projection, identity

    def test_event_authority_requires_exact_replayed_typed_projection(self) -> None:
        root, projection, identity = self._case("ready")

        runtime_context = build_generation_story_project_context(
            story_project=root,
            chapter=1,
            project_identity=identity,
        )
        context = runtime_context.memory_v2

        self.assertEqual("ready", context["status"])
        self.assertEqual("2.2", context["projection"]["schema_version"])
        self.assertEqual(projection["head_event_hash"], context["head_event_hash"])
        self.assertEqual(context["projection_hash"], context["replay_projection_hash"])

    def test_event_authority_fails_closed_on_pinned_head_mismatch(self) -> None:
        root, projection, identity = self._case("drift")
        wrong_head = copy.deepcopy(identity.to_dict())
        wrong_head["authority"]["head_event_hash"] = "d" * 64
        wrong_head["authority"]["activation_receipt"]["head_event_hash"] = "d" * 64
        wrong_head["authority"]["activation_receipt"]["receipt_sha256"] = "e" * 64
        drifted_identity = ProjectIdentity(
            schema_version="2.0",
            book_id=identity.book_id,
            created_at=identity.created_at,
            root_hint=".",
            story_state_mode="shadow",
            activation=None,
            ephemeral=False,
            authority=wrong_head["authority"],
        )
        with self.assertRaisesRegex(ValueError, "head_mismatch"):
            _load_memory_v2_context(root, drifted_identity)

    def test_main_load_rebuilds_deleted_caches_without_rewriting_events(self) -> None:
        root, projection, identity = self._case("deleted_caches")
        paths = RuntimePaths.for_story_project(root)
        memory_root = paths.memory_dir / "v2"
        _load_memory_v2_context(root, identity)
        expected_cache_bytes = {
            path.relative_to(memory_root).as_posix(): path.read_bytes()
            for path in sorted(memory_root.rglob("*"))
            if path.is_file() and "events" not in path.relative_to(memory_root).parts
        }
        expected_snapshot_bytes = paths.snapshot_path.read_bytes()
        event_bytes_before = self._event_bytes(root)

        (memory_root / "canonical_memory.json").unlink()
        shutil.rmtree(memory_root / "projections")
        paths.snapshot_path.unlink()

        runtime_context = build_generation_story_project_context(
            story_project=root,
            chapter=1,
            project_identity=identity,
        )
        context = runtime_context.memory_v2

        rebuilt_cache_bytes = {
            path.relative_to(memory_root).as_posix(): path.read_bytes()
            for path in sorted(memory_root.rglob("*"))
            if path.is_file() and "events" not in path.relative_to(memory_root).parts
        }
        self.assertEqual(projection, context["projection"])
        self.assertEqual(expected_cache_bytes, rebuilt_cache_bytes)
        self.assertEqual(expected_snapshot_bytes, paths.snapshot_path.read_bytes())
        self.assertEqual(event_bytes_before, self._event_bytes(root))

    def test_main_load_rebuilds_wrong_projection_reducer_metadata(self) -> None:
        root, projection, identity = self._case("wrong_projection_reducer")
        paths = RuntimePaths.for_story_project(root)
        memory_root = paths.memory_dir / "v2"
        _load_memory_v2_context(root, identity)
        receipt_path = memory_root / "projections" / "receipts" / "snapshot.json"
        expected_receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(expected_receipt_bytes.decode("utf-8-sig"))
        receipt["reducer_version"] = "memory-reducer-9.9"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        event_bytes_before = self._event_bytes(root)

        runtime_context = build_generation_story_project_context(
            story_project=root,
            chapter=1,
            project_identity=identity,
        )
        context = runtime_context.memory_v2

        rebuilt_receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(projection, context["projection"])
        self.assertEqual(CURRENT_REDUCER_VERSION, rebuilt_receipt["reducer_version"])
        self.assertEqual(expected_receipt_bytes, receipt_path.read_bytes())
        self.assertEqual(event_bytes_before, self._event_bytes(root))

    def test_corrupt_immutable_event_fails_before_cache_recovery(self) -> None:
        root, _projection, identity = self._case("corrupt_event")
        paths = RuntimePaths.for_story_project(root)
        memory_root = paths.memory_dir / "v2"
        _load_memory_v2_context(root, identity)
        cache_bytes_before = {
            path.relative_to(memory_root).as_posix(): path.read_bytes()
            for path in sorted(memory_root.rglob("*"))
            if path.is_file() and "events" not in path.relative_to(memory_root).parts
        }
        event_path = next(
            path
            for path in sorted((memory_root / "events").rglob("*.json"))
            if "checkpoints" not in path.parts
        )
        event_path.write_bytes(b'{"corrupt":true}\n')
        (memory_root / "canonical_memory.json").unlink()

        with self.assertRaisesRegex(ValueError, "replay_failed"):
            _load_memory_v2_context(root, identity)

        self.assertFalse((memory_root / "canonical_memory.json").exists())
        for relative_path, expected_bytes in cache_bytes_before.items():
            if relative_path == "canonical_memory.json":
                continue
            self.assertEqual(expected_bytes, (memory_root / relative_path).read_bytes())

    def test_event_authority_recovers_valid_but_drifted_canonical_cache(self) -> None:
        root, projection, identity = self._case("canonical_drift")
        _load_memory_v2_context(root, identity)
        tampered = copy.deepcopy(projection)
        tampered["title"] = "tampered cache"
        save_canonical_memory(
            RuntimePaths.for_story_project(root).memory_dir / "v2" / "canonical_memory.json",
            tampered,
        )

        context = _load_memory_v2_context(root, identity)

        self.assertEqual(projection, context["projection"])
        self.assertEqual(
            projection,
            json.loads(
                (
                    RuntimePaths.for_story_project(root).memory_dir
                    / "v2"
                    / "canonical_memory.json"
                ).read_text(encoding="utf-8-sig")
            ),
        )

    def test_markdown_semantics_cannot_override_event_projection(self) -> None:
        _root, projection, identity = self._case("parser_audit_only")
        context = {
            "story_state_mode": "strict",
            "project_identity": identity.to_dict(),
            "memory_v2": {"status": "ready", "projection": projection},
            "semantic_state": {
                "characters": {"forged": {"name": "Markdown forgery"}},
                "story_state": {"last_scene_location": "forged-place"},
            },
        }
        source_snapshot = {
            "chapter_index": 99,
            "book_id": identity.book_id,
            "project_profile": {"language": "zh-CN"},
            "characters": {"stale": {"name": "Stale snapshot"}},
            "story_state": {"last_scene_location": "stale-place"},
        }

        merged = StoryProjectContextService.apply_authority(context, source_snapshot)

        self.assertEqual({}, merged["characters"])
        self.assertEqual("", merged["story_state"]["last_scene_location"])
        self.assertEqual("memory_event_v2_2", merged["semantic_authority"]["source"])

    def test_context_mapper_rechecks_epoch_and_head(self) -> None:
        _root, projection, identity = self._case("mapper_cas")
        context = {
            "story_state_mode": "strict",
            "project_identity": identity.to_dict(),
            "memory_v2": {"status": "ready", "projection": projection},
        }
        context["memory_v2"]["projection"]["authority_epoch"] += 1

        with self.assertRaises(StoryProjectContextError) as raised:
            StoryProjectContextService.apply_authority(context, {})

        self.assertEqual("event_authority_epoch_mismatch", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
