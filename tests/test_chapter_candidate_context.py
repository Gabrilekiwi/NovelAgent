from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from core.engine.chapter_candidate_context import (
    CHAPTER_CANDIDATE_CONTEXT_VERSION,
    ChapterCandidateContextBuilder,
    ChapterCandidateContextError,
)
from core.engine.chapter_candidate import ChapterCandidateExecutor, ChapterCandidateStore
from core.engine.chapter_fact_context import (
    CHAPTER_FACT_CONTEXT_MAX_CHARS,
    ChapterFactContextError,
    build_chapter_fact_context,
)
from core.execution_provenance import build_execution_provenance
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2 import (
    apply_genesis_event,
    create_genesis_memory_batch,
    memory_projection_hash,
    write_memory_event_batch,
)
from core.runtime_paths import RuntimePaths
from core.story_project.activation import (
    activate_story_state,
    build_story_state_calibration_report,
)
from core.story_project.authority import (
    activate_event_authority,
    project_identity_sha256,
)
from core.story_project.identity import ensure_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path
from core.story_project.semantic_contracts import (
    STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
)
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from core.story_project.read_set import StoryProjectSourceDriftError
from core.story_project.runtime import build_generation_story_project_context


def _confirmation(root: Path, chapter_index: int, chapter_intent: dict) -> dict:
    trusted = ChapterCandidateContextBuilder(root).build(chapter_index)
    payload = {
        "schema_version": "1.0",
        "record_type": "chapter_intent_confirmation",
        "book_id": trusted.story_project_context["project_identity"]["book_id"],
        "chapter_index": chapter_index,
        "confirmed": True,
        "card_sha256": "4" * 64,
        "context_sha256": trusted.context_sha256,
        "chapter_intent": chapter_intent,
    }
    payload["confirmation_sha256"] = canonical_json_hash(
        payload,
        exclude_environment_fields=False,
    )
    return payload


class ChapterCandidateContextTest(unittest.TestCase):
    def _legacy_case(self, name: str) -> Path:
        source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
        root = Path.cwd() / ".tmp" / "ccc" / f"{name}_{uuid.uuid4().hex[:8]}" / "book"
        shutil.copytree(source, root)
        chapter_two = root / "大纲" / "细纲_第002章.md"
        chapter_two.write_text(
            chapter_two.read_text(encoding="utf-8").replace(
                "结尾压力：",
                "结尾压力：危险迫使两人立刻做出选择；",
                1,
            ),
            encoding="utf-8",
        )
        identity = ensure_project_identity(root)
        activate_story_state(root, self._qualified_report(identity.book_id))
        return root

    def _event_case(self, name: str) -> tuple[Path, dict]:
        root = Path.cwd() / ".tmp" / "ccc" / f"{name}_{uuid.uuid4().hex[:8]}" / "book"
        root.mkdir(parents=True)
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir()
        canonical_outline_path(root, 1).write_text(
            "# Chapter 1\n\ncore_event: enter the old station\n",
            encoding="utf-8",
        )
        identity = ensure_project_identity(root, book_id=f"book-{name}")
        genesis = create_genesis_memory_batch(
            book_id=identity.book_id,
            title="Immutable event title",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
            language="en",
            authority_epoch=1,
        )
        projection = apply_genesis_event(genesis["events"][0])
        memory_root = RuntimePaths.for_story_project(root).memory_dir / "v2"
        write_memory_event_batch(memory_root / "events", genesis)
        activated = activate_event_authority(
            root,
            expected_identity_sha256=project_identity_sha256(root),
            head_event_hash=projection["head_event_hash"],
        )
        self.assertEqual("shadow", activated.story_state_mode)
        return root, projection

    @staticmethod
    def _qualified_report(book_id: str) -> dict:
        return build_story_state_calibration_report(
            book_id=book_id,
            parser_version=SEMANTIC_PARSER_VERSION,
            semantic_schema_version=STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
            target_layout_profile_version="canonical-zh-1",
            evidence={
                "target_sample_count": 1,
                "format_variant_count": 2,
                "managed_round_trip_rate": 1.0,
                "required_field_exact_match_rate": 1.0,
                "authoritative_precision": 1.0,
                "supported_optional_recall": 0.95,
                "unsupported_structure_count": 1,
                "unsupported_structure_captured_count": 1,
                "consecutive_shadow_chapters": 10,
                "blocking_conflict_count": 0,
                "missing_provenance_fields": [],
            },
            generated_at="2026-09-03T00:00:00+00:00",
        )

    @staticmethod
    def _provenance():
        return build_execution_provenance(
            code_bundle_hash="1" * 64,
            code_file_count=1,
            git_commit=None,
            git_dirty=True,
            prompt_hashes={"prompts/chapter_prompt.md": "2" * 64},
            schema_hashes={
                "schemas/chapter_candidate_record.schema.json": "3" * 64
            },
            dependency_versions={},
            provider="test",
            model="test-model",
            config={"test_mode": True},
            feature_flags={"chapter_candidate": True},
        )

    @staticmethod
    def _tree_inventory(root: Path) -> dict[str, tuple[str, str | None]]:
        inventory: dict[str, tuple[str, str | None]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                inventory[relative] = ("directory", None)
            elif path.is_file():
                inventory[relative] = (
                    "file",
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
        return inventory

    def test_missing_identity_fails_without_bootstrapping_project(self) -> None:
        source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
        root = Path.cwd() / ".tmp" / "ccc" / f"missing_{uuid.uuid4().hex[:8]}" / "book"
        shutil.copytree(source, root)
        before = self._tree_inventory(root)

        with self.assertRaisesRegex(
            ChapterCandidateContextError,
            "chapter_candidate_identity_missing",
        ):
            ChapterCandidateContextBuilder(root).build(2)

        self.assertEqual(before, self._tree_inventory(root))
        self.assertFalse((root / ".novelagent").exists())

    def test_legacy_context_ignores_runtime_memory_cache_and_is_zero_write(self) -> None:
        root = self._legacy_case("legacy_cache")
        memory_root = RuntimePaths.for_story_project(root).memory_dir / "v2"
        memory_root.mkdir(parents=True, exist_ok=True)
        canonical_path = memory_root / "canonical_memory.json"
        canonical_path.write_text("{not valid canonical memory}\n", encoding="utf-8")
        before = self._tree_inventory(root)

        trusted = ChapterCandidateContextBuilder(root).build(2)

        self.assertEqual(before, self._tree_inventory(root))
        self.assertEqual(CHAPTER_CANDIDATE_CONTEXT_VERSION, trusted.to_dict()["context_builder_version"])
        self.assertEqual("strict", trusted.story_project_context["story_state_mode"])
        self.assertEqual("absent", trusted.story_project_context["memory_v2"]["status"])
        self.assertEqual("bypass", trusted.story_project_context["memory_v2"]["cache_status"])
        self.assertIsNone(trusted.authority_projection_sha256)
        self.assertEqual("{not valid canonical memory}\n", canonical_path.read_text(encoding="utf-8"))

    def test_source_change_inside_parse_window_is_rejected(self) -> None:
        root = self._legacy_case("parse_window")
        outline = root / "大纲" / "细纲_第002章.md"

        def build_then_change_source(**kwargs):
            context = build_generation_story_project_context(**kwargs)
            outline.write_text(
                outline.read_text(encoding="utf-8") + "\n解析窗口内发生了修改。\n",
                encoding="utf-8",
            )
            return context

        with (
            patch(
                "core.engine.chapter_candidate_context.build_generation_story_project_context",
                side_effect=build_then_change_source,
            ),
            self.assertRaises(StoryProjectSourceDriftError),
        ):
            ChapterCandidateContextBuilder(root).build(2)

    def test_event_context_replays_without_creating_any_disposable_cache(self) -> None:
        root, projection = self._event_case("event_no_cache")
        paths = RuntimePaths.for_story_project(root)
        memory_root = paths.memory_dir / "v2"
        self.assertFalse((memory_root / "canonical_memory.json").exists())
        self.assertFalse((memory_root / "projections").exists())
        self.assertFalse(paths.snapshot_path.exists())
        before = self._tree_inventory(root)

        trusted = ChapterCandidateContextBuilder(root).build(1)

        self.assertEqual(before, self._tree_inventory(root))
        context = trusted.story_project_context
        self.assertEqual("strict", context["story_state_mode"])
        self.assertEqual("bypass", context["memory_v2"]["cache_status"])
        self.assertEqual(projection, context["memory_v2"]["projection"])
        self.assertEqual(memory_projection_hash(projection), trusted.authority_projection_sha256)
        self.assertFalse((memory_root / "canonical_memory.json").exists())
        self.assertFalse((memory_root / "projections").exists())
        self.assertFalse(paths.snapshot_path.exists())

    def test_event_context_ignores_valid_but_forged_canonical_cache(self) -> None:
        root, projection = self._event_case("event_forged_cache")
        canonical_path = RuntimePaths.for_story_project(root).memory_dir / "v2" / "canonical_memory.json"
        forged = dict(projection)
        forged["title"] = "forged disposable title"
        canonical_path.write_text(
            json.dumps(forged, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        before = self._tree_inventory(root)

        trusted = ChapterCandidateContextBuilder(root).build(1)

        self.assertEqual(before, self._tree_inventory(root))
        replayed = trusted.story_project_context["memory_v2"]["projection"]
        self.assertEqual("Immutable event title", replayed["title"])
        self.assertEqual(
            "forged disposable title",
            json.loads(canonical_path.read_text(encoding="utf-8"))["title"],
        )

    def test_event_authority_change_during_generation_preserves_stale_draft(self) -> None:
        root, _projection = self._event_case("event_drift")
        memory_root = RuntimePaths.for_story_project(root).memory_dir / "v2"
        event_path = next(
            path
            for path in sorted((memory_root / "events").rglob("*.json"))
            if "checkpoints" not in path.parts
        )
        prose = "Mara enters the old station and confirms that the signal is still moving."

        def generate_while_authority_changes(_input: str) -> str:
            event_path.write_bytes(b'{"corrupt":true}\n')
            return prose

        executor = ChapterCandidateExecutor(
            story_project_root=root,
            repository_root=Path.cwd(),
            generator=generate_while_authority_changes,
            execution_kind="deterministic",
            provenance_factory=self._provenance,
        )

        result = executor.execute(
            chapter_index=1,
            confirmation=_confirmation(
                root,
                1,
                {
                    "goal": "Enter the old station and identify the signal source",
                    "validation_focus": ["continuity", "spatial", "logic"],
                    "notes": [],
                },
            ),
            idempotency_key="event-authority-drift",
        )

        self.assertEqual("stale", result["status"])
        self.assertEqual("story_project_source_drift", result["error"]["code"])
        self.assertEqual(
            prose,
            ChapterCandidateStore(root).load(result["candidate_id"])["prose"],
        )
        self.assertEqual(
            "chapter_candidate_authority_replay_failed",
            result["source_verification"]["after"]["differences"][0]["code"],
        )

    def test_completed_event_candidate_does_not_rebuild_disposable_caches(self) -> None:
        root, projection = self._event_case("event_completed")
        paths = RuntimePaths.for_story_project(root)
        memory_root = paths.memory_dir / "v2"
        event_bytes_before = {
            path.relative_to(memory_root / "events").as_posix(): path.read_bytes()
            for path in sorted((memory_root / "events").rglob("*"))
            if path.is_file()
        }
        prose = "Mara enters the old station and traces the moving signal to its source."

        result = ChapterCandidateExecutor(
            story_project_root=root,
            repository_root=Path.cwd(),
            generator=lambda _input: prose,
            execution_kind="deterministic",
            provenance_factory=self._provenance,
        ).execute(
            chapter_index=1,
            confirmation=_confirmation(
                root,
                1,
                {
                    "goal": "Enter the old station and identify the signal source",
                    "validation_focus": ["continuity", "spatial", "logic"],
                    "notes": [],
                },
            ),
            idempotency_key="event-authority-completed",
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual(
            memory_projection_hash(projection),
            result["source_verification"]["after"][
                "authority_projection_sha256"
            ],
        )
        self.assertFalse((memory_root / "canonical_memory.json").exists())
        self.assertFalse((memory_root / "projections").exists())
        self.assertFalse(paths.snapshot_path.exists())
        self.assertEqual(
            event_bytes_before,
            {
                path.relative_to(memory_root / "events").as_posix(): path.read_bytes()
                for path in sorted((memory_root / "events").rglob("*"))
                if path.is_file()
            },
        )

    def test_fact_context_uses_global_directory_but_details_only_for_prose_matches(self) -> None:
        root = self._legacy_case("fact_view_legacy")
        trusted = ChapterCandidateContextBuilder(root).build(2)
        before = self._tree_inventory(root)

        first = build_chapter_fact_context(
            trusted,
            "林澈抵达旧城站，随后检查闸门。",
        )
        second = build_chapter_fact_context(
            trusted,
            "林澈抵达旧城站，随后检查闸门。",
        )

        self.assertEqual(before, self._tree_inventory(root))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.sha256, second.sha256)
        self.assertLessEqual(first.char_count, CHAPTER_FACT_CONTEXT_MAX_CHARS)
        payload = first.to_dict()
        self.assertFalse(payload["selection"]["prose_is_authority"])
        self.assertEqual(
            payload["selection"]["directory_count"],
            len(payload["entity_directory"]),
        )
        self.assertEqual(
            payload["selection"]["matched_count"],
            len(payload["current_state_details"]),
        )
        directory_ids = {item["entity_id"] for item in payload["entity_directory"]}
        detail_ids = {item["entity_id"] for item in payload["current_state_details"]}
        self.assertTrue(detail_ids)
        self.assertTrue(detail_ids.issubset(directory_ids))
        self.assertTrue(
            any(
                item["name"] == "林澈" or "林澈" in item["aliases"]
                for item in payload["entity_directory"]
            )
        )

    def test_fact_context_reads_replayed_event_projection_without_writes(self) -> None:
        root, projection = self._event_case("fact_view_event")
        trusted = ChapterCandidateContextBuilder(root).build(1)
        before = self._tree_inventory(root)

        view = build_chapter_fact_context(
            trusted,
            "Mara enters the old station.",
        )

        self.assertEqual(before, self._tree_inventory(root))
        binding = view.to_dict()["authority_binding"]
        self.assertEqual("event_v1", binding["mode"])
        self.assertEqual(projection["head_event_hash"], binding["head_event_hash"])
        self.assertEqual(
            memory_projection_hash(projection),
            binding["authority_projection_sha256"],
        )

    def test_fact_context_rejects_mutated_trusted_payload(self) -> None:
        root = self._legacy_case("fact_view_tamper")
        trusted = ChapterCandidateContextBuilder(root).build(2)
        changed = json.loads(json.dumps(trusted.story_project_context))
        changed["chapter_index"] = 99

        with self.assertRaisesRegex(
            ChapterFactContextError,
            "chapter_fact_context_binding_mismatch",
        ):
            build_chapter_fact_context(
                replace(trusted, story_project_context=changed),
                "林澈检查闸门。",
            )

    def test_fact_context_budget_is_a_hard_limit(self) -> None:
        root = self._legacy_case("fact_view_budget")
        trusted = ChapterCandidateContextBuilder(root).build(2)

        with self.assertRaisesRegex(
            ChapterFactContextError,
            "chapter_fact_context_budget_exceeded",
        ):
            build_chapter_fact_context(
                trusted,
                "林澈检查闸门。",
                max_chars=50,
            )


if __name__ == "__main__":
    unittest.main()
