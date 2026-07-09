from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import main as cli
from core.engine.preflight import run_preflight
from core.story_project.mapper import build_story_project_runtime_context


class StoryProjectMapperTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_story_project_mapper" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _story_project(self, case_dir: Path) -> Path:
        root = case_dir / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        return root

    def _snapshot(self, case_dir: Path, chapter_index: int = 2) -> Path:
        path = case_dir / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "chapter_index": chapter_index,
                    "project_profile": {"language": "zh-CN"},
                    "world_state": {},
                    "characters": {},
                    "timeline": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def _memory(self, case_dir: Path) -> Path:
        path = case_dir / "memory.json"
        path.write_text(json.dumps({"source": "test", "status": "ready", "items": []}), encoding="utf-8")
        return path

    def _write_outline(self, root: Path, chapter_index: int = 2) -> Path:
        path = root / "大纲" / f"细纲_第{chapter_index:03d}章.md"
        path.write_text(
            "\n".join(
                [
                    "# 风暴前夜",
                    "",
                    "核心事件：主角发现地铁门后有人求救",
                    "",
                    "## 剧情节拍",
                    "- 发现求救声",
                    "- 队友出现分歧",
                    "",
                    "结尾压力：门内的人知道主角秘密",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def test_build_runtime_context_reads_current_outline(self) -> None:
        case_dir = self._case_dir("outline")
        root = self._story_project(case_dir)
        outline = self._write_outline(root)

        context = build_story_project_runtime_context(root, 2)

        self.assertEqual(str(outline), context.outline["path"])
        self.assertIn("主角发现地铁门后有人求救", context.outline["text"])
        self.assertEqual("风暴前夜", context.chapter_blueprint.title)

    def test_runtime_context_reads_tracking_context_and_character_state(self) -> None:
        case_dir = self._case_dir("tracking")
        root = self._story_project(case_dir)
        self._write_outline(root)
        (root / "追踪" / "上下文.md").write_text("# 上下文\n当前在地铁口。", encoding="utf-8")
        (root / "追踪" / "角色状态.md").write_text("# 角色状态\n主角受伤。", encoding="utf-8")

        context = build_story_project_runtime_context(root, 2)

        self.assertIn("上下文.md", context.tracking_files)
        self.assertIn("角色状态.md", context.tracking_files)
        self.assertIn("当前在地铁口", context.tracking_files["上下文.md"]["text"])
        self.assertIn("主角受伤", context.tracking_files["角色状态.md"]["text"])

    def test_runtime_context_recursively_reads_setting_markdown_files(self) -> None:
        case_dir = self._case_dir("settings")
        root = self._story_project(case_dir)
        self._write_outline(root)
        setting_path = root / "设定" / "角色" / "主角.md"
        setting_path.parent.mkdir()
        setting_path.write_text("# 主角\n机械师。", encoding="utf-8")

        context = build_story_project_runtime_context(root, 2)

        self.assertIn("角色\\主角.md", context.setting_files)
        self.assertEqual(str(setting_path), context.setting_files["角色\\主角.md"]["path"])

    def test_runtime_context_resolves_previous_prose_for_later_chapters(self) -> None:
        case_dir = self._case_dir("previous")
        root = self._story_project(case_dir)
        self._write_outline(root, 2)
        previous = root / "正文" / "第001章_开端.md"
        previous.write_text("上一章正文。", encoding="utf-8")

        context = build_story_project_runtime_context(root, 2)

        self.assertIsNotNone(context.previous_prose)
        self.assertEqual(str(previous), context.previous_prose["path"])

    def test_runtime_context_uses_none_previous_prose_for_first_chapter(self) -> None:
        case_dir = self._case_dir("first")
        root = self._story_project(case_dir)
        self._write_outline(root, 1)

        context = build_story_project_runtime_context(root, 1)

        self.assertIsNone(context.previous_prose)
        self.assertIsNone(context.source_paths.previous_prose_path)

    def test_missing_previous_prose_warns_without_blocking(self) -> None:
        case_dir = self._case_dir("missing_previous")
        root = self._story_project(case_dir)
        self._write_outline(root, 2)

        context = build_story_project_runtime_context(root, 2)

        self.assertIsNone(context.previous_prose)
        self.assertIn("previous_prose_missing", " ".join(context.warnings))

    def test_chapter_blueprint_extracts_title_from_markdown_heading(self) -> None:
        case_dir = self._case_dir("title")
        root = self._story_project(case_dir)
        self._write_outline(root, 2)

        context = build_story_project_runtime_context(root, 2)

        self.assertEqual("风暴前夜", context.chapter_blueprint.title)

    def test_chapter_blueprint_records_missing_fields(self) -> None:
        case_dir = self._case_dir("missing_fields")
        root = self._story_project(case_dir)
        (root / "大纲" / "细纲_第001章.md").write_text("# 只有标题", encoding="utf-8")

        context = build_story_project_runtime_context(root, 1)

        self.assertIn("core_event", context.chapter_blueprint.missing_fields)
        self.assertIn("required_beats", context.chapter_blueprint.missing_fields)
        self.assertIn("ending_pressure", context.chapter_blueprint.missing_fields)
        self.assertIn("core_event", context.missing_fields)

    def test_runtime_context_builds_snapshot_and_memory_overlays(self) -> None:
        case_dir = self._case_dir("overlays")
        root = self._story_project(case_dir)
        outline = self._write_outline(root, 2)
        previous = root / "正文" / "第001章_开端.md"
        previous.write_text("上一章正文。", encoding="utf-8")
        (root / "追踪" / "上下文.md").write_text("# 上下文", encoding="utf-8")

        context = build_story_project_runtime_context(
            root,
            2,
            snapshot={"chapter_index": 9, "project_profile": {"language": "zh-CN"}},
            memory_context={"source": "test", "status": "ready", "items": []},
        )

        self.assertEqual(2, context.snapshot_overlay["chapter_index"])
        self.assertEqual("zh-CN", context.snapshot_overlay["project_profile"]["language"])
        self.assertEqual(str(root), context.snapshot_overlay["story_project"]["root"])
        self.assertEqual(str(outline), str(context.source_paths.outline_path))
        self.assertEqual(str(previous), str(context.source_paths.previous_prose_path))
        self.assertEqual("story_project", context.memory_context_overlay["source"])
        self.assertGreaterEqual(len(context.memory_context_overlay["items"]), 3)
        first_item = context.memory_context_overlay["items"][0]
        self.assertEqual("story_project", first_item["source"])
        self.assertIn("path", first_item)
        self.assertIn("text", first_item)
        self.assertIn("summary", first_item)
        self.assertTrue(any(entry.field == "chapter_index" for entry in context.source_resolution.entries))

    def test_preflight_includes_runtime_context_when_story_project_requested(self) -> None:
        case_dir = self._case_dir("preflight_story")
        root = self._story_project(case_dir)
        self._write_outline(root, 1)

        result = run_preflight(
            snapshot_path=self._snapshot(case_dir, chapter_index=1),
            memory_path=self._memory(case_dir),
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            story_project=root,
            chapter=1,
        )

        self.assertTrue(result["ok"])
        runtime = [check for check in result["checks"] if check["name"] == "story_project_runtime_context"][0]
        self.assertEqual(1, runtime["details"]["chapter_blueprint"]["chapter_index"])
        self.assertIn("source_paths", runtime["details"])
        self.assertIn("source_resolution", runtime["details"])
        self.assertIn("missing_fields", runtime["details"])
        self.assertIn("warnings", runtime["details"])

    def test_cli_check_json_includes_runtime_context_when_story_project_requested(self) -> None:
        case_dir = self._case_dir("cli_check_json")
        root = self._story_project(case_dir)
        self._write_outline(root, 1)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--check",
                "--check-json",
                "--dry-run",
                "--snapshot",
                str(self._snapshot(case_dir, chapter_index=1)),
                "--memory",
                str(self._memory(case_dir)),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
                "--story-project",
                str(root),
                "--chapter",
                "1",
            ],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        payload = json.loads(output.getvalue())
        runtime = [check for check in payload["checks"] if check["name"] == "story_project_runtime_context"][0]
        self.assertIn("chapter_blueprint", runtime["details"])
        self.assertIn("source_paths", runtime["details"])
        self.assertIn("source_resolution", runtime["details"])
        self.assertIn("missing_fields", runtime["details"])
        self.assertIn("warnings", runtime["details"])

    def test_preflight_omits_runtime_context_without_story_project(self) -> None:
        case_dir = self._case_dir("preflight_legacy")

        result = run_preflight(
            snapshot_path=self._snapshot(case_dir, chapter_index=1),
            memory_path=self._memory(case_dir),
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("story_project_runtime_context", {check["name"] for check in result["checks"]})


if __name__ == "__main__":
    unittest.main()
