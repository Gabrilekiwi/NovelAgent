from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.preflight import run_preflight
from core.story_project.paths import (
    canonical_outline_path,
    canonical_prose_path,
    infer_next_chapter,
    read_active_book_path,
    resolve_outline,
    resolve_prose,
    resolve_story_project_root,
)
from core.story_project.validator import validate_story_project


class StoryProjectTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_story_project" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _story_project(self, case_dir: Path, name: str = "book") -> Path:
        root = case_dir / name
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        return root

    def _snapshot(self, case_dir: Path) -> Path:
        path = case_dir / "snapshot.json"
        path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        return path

    def _memory(self, case_dir: Path) -> Path:
        path = case_dir / "memory.json"
        path.write_text(json.dumps({"source": "test", "status": "ready", "items": []}), encoding="utf-8")
        return path

    def test_active_book_uses_first_line_only(self) -> None:
        case_dir = self._case_dir("active_book")
        story_project_root = self._story_project(case_dir, "active")
        (case_dir / ".active-book").write_text("active\nignored\n", encoding="utf-8")

        self.assertEqual(story_project_root, read_active_book_path(case_dir))
        resolution = resolve_story_project_root("auto", workspace_root=case_dir)
        self.assertTrue(resolution.ok)
        self.assertEqual(story_project_root, resolution.root)
        self.assertEqual("active_book", resolution.source)

    def test_filename_resolver_uses_canonical_write_paths_and_compatible_reads(self) -> None:
        case_dir = self._case_dir("resolver")
        story_project_root = self._story_project(case_dir)
        outline = story_project_root / "大纲" / "细纲_第3章.md"
        prose = story_project_root / "正文" / "第003章_旧标题.md"
        outline.write_text("# 第三章", encoding="utf-8")
        prose.write_text("正文", encoding="utf-8")

        self.assertEqual(story_project_root / "大纲" / "细纲_第003章.md", canonical_outline_path(story_project_root, 3))
        self.assertEqual(story_project_root / "正文" / "第003章_章名.md", canonical_prose_path(story_project_root, 3, "章名"))
        self.assertEqual(outline, resolve_outline(story_project_root, 3).path)
        self.assertEqual(prose, resolve_prose(story_project_root, 3).path)

    def test_multiple_outlines_for_same_chapter_are_blocking(self) -> None:
        case_dir = self._case_dir("outline_conflict")
        story_project_root = self._story_project(case_dir)
        (story_project_root / "大纲" / "细纲_第3章.md").write_text("a", encoding="utf-8")
        (story_project_root / "大纲" / "细纲_第003章_副本.md").write_text("b", encoding="utf-8")

        result = validate_story_project(story_project=story_project_root, chapter=3, workspace_root=case_dir)

        self.assertFalse(result.ok)
        self.assertIn("outline_chapter_conflict", {problem.code for problem in result.problems})

    def test_multiple_prose_files_for_same_chapter_are_blocking(self) -> None:
        case_dir = self._case_dir("prose_conflict")
        story_project_root = self._story_project(case_dir)
        (story_project_root / "大纲" / "细纲_第004章.md").write_text("outline", encoding="utf-8")
        (story_project_root / "正文" / "第3章.md").write_text("a", encoding="utf-8")
        (story_project_root / "正文" / "第003章_旧标题.md").write_text("b", encoding="utf-8")

        result = validate_story_project(story_project=story_project_root, chapter=4, workspace_root=case_dir)

        self.assertFalse(result.ok)
        self.assertIn("prose_chapter_conflict", {problem.code for problem in result.problems})

    def test_chapter_auto_infers_first_gap_from_existing_prose(self) -> None:
        case_dir = self._case_dir("chapter_auto")
        story_project_root = self._story_project(case_dir)
        (story_project_root / "正文" / "第001章_开端.md").write_text("1", encoding="utf-8")
        (story_project_root / "正文" / "第2章.md").write_text("2", encoding="utf-8")
        (story_project_root / "正文" / "第004章_后续.md").write_text("4", encoding="utf-8")
        (story_project_root / "大纲" / "细纲_第003章.md").write_text("outline", encoding="utf-8")

        self.assertEqual(3, infer_next_chapter(story_project_root))
        result = validate_story_project(story_project=story_project_root, chapter="auto", workspace_root=case_dir)

        self.assertTrue(result.ok)
        self.assertEqual(3, result.chapter_resolution.resolved_chapter)
        self.assertEqual(story_project_root / "大纲" / "细纲_第003章.md", result.outline_resolution.path)

    def test_missing_core_directory_is_blocking_but_oh_story_files_are_not_required(self) -> None:
        case_dir = self._case_dir("missing_core")
        story_project_root = case_dir / "book"
        for directory in ("设定", "大纲", "正文"):
            (story_project_root / directory).mkdir(parents=True)

        result = validate_story_project(story_project=story_project_root, chapter="auto", workspace_root=case_dir)

        self.assertFalse(result.ok)
        self.assertIn("missing_core_directory", {problem.code for problem in result.problems})
        self.assertFalse((story_project_root / ".story-deployed").exists())
        self.assertFalse((story_project_root / ".codex" / "hooks.json").exists())

    def test_preflight_includes_story_project_structure_when_requested(self) -> None:
        case_dir = self._case_dir("preflight")
        story_project_root = self._story_project(case_dir)
        (story_project_root / "大纲" / "细纲_第001章.md").write_text("outline", encoding="utf-8")

        result = run_preflight(
            snapshot_path=self._snapshot(case_dir),
            memory_path=self._memory(case_dir),
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            story_project=story_project_root,
            chapter="auto",
        )

        self.assertTrue(result["ok"])
        check = [item for item in result["checks"] if item["name"] == "story_project_structure"][0]
        self.assertEqual(1, check["details"]["chapter_resolution"]["resolved_chapter"])
        self.assertEqual(str(story_project_root), check["details"]["root"]["root"])

    def test_preflight_keeps_legacy_memory_mode_when_story_project_is_not_requested(self) -> None:
        case_dir = self._case_dir("legacy")

        result = run_preflight(
            snapshot_path=self._snapshot(case_dir),
            memory_path=self._memory(case_dir),
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("story_project_structure", {item["name"] for item in result["checks"]})


if __name__ == "__main__":
    unittest.main()
