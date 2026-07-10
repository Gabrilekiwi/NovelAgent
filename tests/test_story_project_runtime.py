from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.runtime import (
    StoryProjectSequenceDriftError,
    build_generation_story_project_context,
    build_generation_story_project_context_loader,
)


class StoryProjectRuntimeTests(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_story_project_runtime" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        return root

    def _book(self, parent: Path, name: str = "book") -> Path:
        root = parent / name
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        return root

    def _outline(self, root: Path, chapter: int, title: str | None = None) -> Path:
        path = canonical_outline_path(root, chapter)
        path.write_text(
            "\n".join(
                [
                    f"# {title or f'Chapter {chapter}'}",
                    "",
                    "core_event: the team makes a costly choice",
                    "",
                    "## required_beats",
                    "- danger forces a choice",
                    "- conflict exposes a secret",
                    "",
                    "ending_pressure: the locked door starts a countdown",
                ]
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _snapshot(chapter: int = 1) -> dict:
        return {
            "chapter_index": chapter,
            "project_profile": {"language": "zh-CN"},
            "world_state": {},
            "characters": {},
            "timeline": [],
        }

    @staticmethod
    def _memory() -> dict:
        return {"source": "test", "status": "ready", "items": []}

    def test_loader_pins_auto_resolved_root(self) -> None:
        case = self._case_dir("pinned_root")
        first = self._book(case, "first")
        second = self._book(case, "second")
        self._outline(first, 1, "First")
        self._outline(second, 1, "Second")
        active_book = case / ".active-book"
        active_book.write_text(str(first), encoding="utf-8")

        loader = build_generation_story_project_context_loader(
            story_project="auto",
            workspace_root=case,
        )
        active_book.write_text(str(second), encoding="utf-8")
        context = loader(self._snapshot(), self._memory())

        self.assertEqual(first.resolve(), loader.story_project_root)
        self.assertEqual(first.resolve(), context.story_project_root.resolve())
        self.assertEqual("First", context.chapter_blueprint.title)

    def test_explicit_loader_uses_hint_for_subsequent_chapter_and_reloads_files(self) -> None:
        case = self._case_dir("explicit")
        root = self._book(case)
        self._outline(root, 2, "Second")
        self._outline(root, 3, "Third")
        tracking_path = root / CORE_DIRECTORY_NAMES[3] / "state.md"
        tracking_path.write_text("initial tracking", encoding="utf-8")
        loader = build_generation_story_project_context_loader(story_project=root, chapter=2)

        first = loader(self._snapshot(2), self._memory())
        prose = canonical_prose_path(root, 2, "Second")
        prose.write_text("freshly generated chapter two", encoding="utf-8")
        tracking_path.write_text("tracking updated after chapter two", encoding="utf-8")
        second = loader(self._snapshot(3), self._memory(), 3)

        self.assertEqual(2, first.chapter_index)
        self.assertEqual("2", first.chapter_resolution.requested)
        self.assertEqual(3, second.chapter_index)
        self.assertEqual("3", second.chapter_resolution.requested)
        self.assertEqual("freshly generated chapter two", second.previous_prose["text"])
        self.assertEqual("tracking updated after chapter two", second.tracking_files["state.md"]["text"])

    def test_auto_loader_rescans_and_preserves_auto_resolution_audit(self) -> None:
        case = self._case_dir("auto")
        root = self._book(case)
        self._outline(root, 1, "First")
        self._outline(root, 2, "Second")
        canonical_prose_path(root, 1, "First").write_text("chapter one", encoding="utf-8")
        loader = build_generation_story_project_context_loader(story_project=root, chapter="auto")

        context = loader(self._snapshot(2), self._memory(), 2)

        self.assertEqual(2, context.chapter_index)
        self.assertEqual("auto", context.chapter_resolution.requested)
        self.assertEqual([CORE_DIRECTORY_NAMES[2] + "/"], list(context.chapter_resolution.basis))

    def test_auto_loader_fails_on_sequence_drift(self) -> None:
        case = self._case_dir("drift")
        root = self._book(case)
        self._outline(root, 2, "Second")
        self._outline(root, 4, "Fourth")
        canonical_prose_path(root, 1, "First").write_text("chapter one", encoding="utf-8")
        canonical_prose_path(root, 3, "Third").write_text("chapter three", encoding="utf-8")
        loader = build_generation_story_project_context_loader(story_project=root, chapter="auto")
        first = loader(self._snapshot(2), self._memory())
        canonical_prose_path(root, 2, "Second").write_text("chapter two", encoding="utf-8")

        with self.assertRaises(StoryProjectSequenceDriftError) as raised:
            loader(self._snapshot(3), self._memory(), 3)

        self.assertEqual(2, first.chapter_index)
        self.assertEqual("story_project_sequence_drift", raised.exception.code)
        self.assertEqual(3, raised.exception.expected_chapter)
        self.assertEqual(4, raised.exception.resolved_chapter)

    def test_overwrite_allows_one_existing_target_but_not_duplicates(self) -> None:
        case = self._case_dir("overwrite")
        root = self._book(case)
        self._outline(root, 2, "Second")
        canonical_prose_path(root, 2, "Second").write_text("existing", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Target prose file already exists"):
            build_generation_story_project_context(
                story_project=root,
                chapter=2,
                snapshot=self._snapshot(2),
                memory_context=self._memory(),
            )

        context = build_generation_story_project_context(
            story_project=root,
            chapter=2,
            snapshot=self._snapshot(2),
            memory_context=self._memory(),
            overwrite=True,
        )
        self.assertEqual(2, context.chapter_index)

        (root / CORE_DIRECTORY_NAMES[2] / "第002章_duplicate.md").write_text("duplicate", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Multiple prose files matched"):
            build_generation_story_project_context(
                story_project=root,
                chapter=2,
                snapshot=self._snapshot(2),
                memory_context=self._memory(),
                overwrite=True,
            )

    def test_loader_validates_chapter_hint(self) -> None:
        case = self._case_dir("invalid_hint")
        root = self._book(case)
        self._outline(root, 1)
        loader = build_generation_story_project_context_loader(story_project=root, chapter=1)

        for invalid in (0, -1, True, "2"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "chapter_hint must be a positive integer"):
                    loader(self._snapshot(), self._memory(), invalid)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
