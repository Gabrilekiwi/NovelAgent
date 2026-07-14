from __future__ import annotations

import copy
from pathlib import Path
import unittest
import uuid

from core.engine.story_project_context import StoryProjectContextError, StoryProjectContextService
from core.memory_v2 import (
    apply_genesis_event,
    create_genesis_memory_batch,
    save_canonical_memory,
    write_memory_event_batch,
)
from core.runtime_paths import RuntimePaths
from core.story_project.authority import build_authority_activation_receipt
from core.story_project.identity import ProjectIdentity, validate_project_identity
from core.story_project.runtime import _load_memory_v2_context


class EventAuthorityRuntimeTest(unittest.TestCase):
    def _case(self, name: str) -> tuple[Path, dict, ProjectIdentity]:
        root = Path.cwd() / ".tmp" / "test_event_authority_runtime" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
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

        context = _load_memory_v2_context(root, identity)

        self.assertEqual("ready", context["status"])
        self.assertEqual("2.2", context["projection"]["schema_version"])
        self.assertEqual(projection["head_event_hash"], context["head_event_hash"])
        self.assertEqual(context["projection_hash"], context["replay_projection_hash"])

    def test_event_authority_fails_closed_on_identity_or_cache_drift(self) -> None:
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

        tampered = copy.deepcopy(projection)
        tampered["title"] = "tampered cache"
        save_canonical_memory(
            RuntimePaths.for_story_project(root).memory_dir / "v2" / "canonical_memory.json",
            tampered,
        )
        with self.assertRaisesRegex(ValueError, "projection_drift"):
            _load_memory_v2_context(root, identity)

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
