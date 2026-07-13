from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import PROSE_DIR_NAME, canonical_prose_path
from core.story_project.writer import (
    TRACKING_DIR_NAME,
    TRACKING_TARGETS,
    StoryProjectWritebackConfig,
    run_story_project_writeback,
)
from core.story_project.managed_block import parse_managed_block


class StoryProjectWriterTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_story_project_writer" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _story_project_root(self, name: str) -> Path:
        root = self._case_dir(name) / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        return root

    def _context(self, root: Path, *, title: str = "稳定标题") -> dict:
        return {
            "story_project_root": str(root),
            "chapter_index": 3,
            "chapter_blueprint": {
                "chapter_index": 3,
                "outline_path": str(root / "大纲" / "细纲_第003章.md"),
                "title": title,
                "core_event": "The route is chosen.",
                "required_beats": [{"index": 1, "text": "route choice"}],
                "ending_pressure": "the countdown starts",
                "source_path": str(root / "大纲" / "细纲_第003章.md"),
                "missing_fields": [],
            },
        }

    def _run(self) -> dict:
        return {
            "id": "chapter_3_test",
            "chapter_index": 3,
            "committed": True,
            "chapter": {
                "pipeline": {
                    "blueprint_coverage": {
                        "missing_beat_indexes": [],
                        "covered_beat_indexes": [1],
                        "ending_pressure_required": True,
                        "ending_pressure_covered": True,
                    }
                }
            },
        }

    def _writeback(self, root: Path, *, mode: str = "apply", overwrite: bool = False, title: str = "稳定标题"):
        return run_story_project_writeback(
            context=self._context(root, title=title),
            run=self._run(),
            chapter_text="# Generated H1\n\nChapter body with the countdown starts.",
            validation={"ok": True},
            analysis={
                "summary": "The team commits to the route.",
                "events": [{"text": "They choose the tunnel."}],
                "conflicts": ["The countdown starts."],
                "world_changes": ["The tunnel is sealed."],
                "character_changes": ["Mira accepts command."],
            },
            config=StoryProjectWritebackConfig(mode=mode, overwrite=overwrite),
        )

    def test_real_writeback_creates_prose_and_tracking_files(self) -> None:
        root = self._story_project_root("create")

        _plan, result = self._writeback(root)
        payload = result.to_dict()

        self.assertTrue(payload["applied"])
        self.assertFalse(payload["partial"])
        prose_path = canonical_prose_path(root, 3, "稳定标题")
        self.assertTrue(prose_path.exists())
        self.assertIn("Chapter body", prose_path.read_text(encoding="utf-8"))
        for filename in TRACKING_TARGETS.values():
            path = root / TRACKING_DIR_NAME / filename
            self.assertTrue(path.exists())
            self.assertIn("NovelAgent:story_project_writeback", path.read_text(encoding="utf-8"))

    def test_existing_prose_blocks_without_overwrite(self) -> None:
        root = self._story_project_root("exists")
        path = canonical_prose_path(root, 3, "稳定标题")
        path.write_text("old text", encoding="utf-8")

        _plan, result = self._writeback(root, overwrite=False)
        payload = result.to_dict()

        self.assertFalse(payload["applied"])
        self.assertIn("target_prose_exists", payload["blocked_reasons"])
        self.assertEqual("old text", path.read_text(encoding="utf-8"))
        for filename in TRACKING_TARGETS.values():
            self.assertFalse((root / TRACKING_DIR_NAME / filename).exists())

    def test_overwrite_replaces_unique_existing_prose(self) -> None:
        root = self._story_project_root("overwrite")
        path = canonical_prose_path(root, 3, "稳定标题")
        path.write_text("old text", encoding="utf-8")

        _plan, result = self._writeback(root, overwrite=True)
        payload = result.to_dict()

        self.assertTrue(payload["applied"])
        self.assertIn("Chapter body", path.read_text(encoding="utf-8"))
        prose_target = [target for target in payload["targets"] if target["kind"] == "prose"][0]
        self.assertEqual("updated", prose_target["status"])
        self.assertGreater(prose_target["chars_before"], 0)

    def test_multiple_prose_files_block_even_with_overwrite(self) -> None:
        root = self._story_project_root("multiple")
        prose_dir = root / PROSE_DIR_NAME
        (prose_dir / "第3章.md").write_text("one", encoding="utf-8")
        (prose_dir / "第003章_旧标题.md").write_text("two", encoding="utf-8")

        _plan, result = self._writeback(root, overwrite=True)
        payload = result.to_dict()

        self.assertFalse(payload["applied"])
        self.assertIn("multiple_prose_targets", payload["blocked_reasons"])
        self.assertEqual("one", (prose_dir / "第3章.md").read_text(encoding="utf-8"))
        self.assertEqual("two", (prose_dir / "第003章_旧标题.md").read_text(encoding="utf-8"))

    def test_writeback_dry_run_does_not_modify_story_project_files(self) -> None:
        root = self._story_project_root("dry_run")

        _plan, result = self._writeback(root, mode="dry_run")
        payload = result.to_dict()

        self.assertFalse(payload["applied"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual([], list((root / PROSE_DIR_NAME).iterdir()))
        self.assertEqual([], list((root / TRACKING_DIR_NAME).iterdir()))
        self.assertTrue(all(target["status"] == "skipped" for target in payload["targets"]))

    def test_tracking_marker_is_not_appended_twice_for_same_run(self) -> None:
        root = self._story_project_root("dedupe")

        self._writeback(root)
        timeline = root / TRACKING_DIR_NAME / TRACKING_TARGETS["timeline"]
        first = timeline.read_text(encoding="utf-8")
        self._writeback(root, overwrite=True)
        second = timeline.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertEqual(1, second.count("run_id=chapter_3_test chapter=003 target=timeline"))

    def test_strict_writeback_uses_one_managed_projection_and_honors_manual_tombstone(self) -> None:
        root = self._story_project_root("strict_managed")
        context_path = root / TRACKING_DIR_NAME / TRACKING_TARGETS["context"]
        manual = (
            "# 人工上下文\n\n"
            "保留这段人工文本。\n"
            "<!-- NovelAgent:tombstone field=story_state.open_threads -->\n"
        )
        context_path.write_text(manual, encoding="utf-8")
        context = self._context(root)
        context.update(
            {
                "story_state_mode": "strict",
                "project_identity": {
                    "schema_version": "1.0",
                    "book_id": "strict-book",
                    "created_at": "2026-07-13T00:00:00+00:00",
                    "root_hint": str(root),
                    "story_state_mode": "strict",
                    "activation": {
                        "parser_version": "shadow-1.0",
                        "semantic_schema_version": "1.0",
                        "layout_profile_version": "canonical-zh-1",
                        "calibration_report_sha256": "a" * 64,
                        "activated_at": "2026-07-13T00:00:00+00:00",
                    },
                    "ephemeral": False,
                },
                "semantic_state": {
                    "parser_version": "shadow-1.0",
                    "source_digest": "b" * 64,
                },
            }
        )
        analysis = {
            "summary": "推进",
            "events": [{"text": "进入控制室"}],
            "world_changes": [],
            "character_changes": [],
            "story_state": {
                "last_scene_location": "控制室",
                "open_threads": ["不得复活"],
            },
        }

        _plan, result = run_story_project_writeback(
            context=context,
            run=self._run(),
            chapter_text="# Chapter\n\nChapter body with the countdown starts.",
            validation={"ok": True},
            analysis=analysis,
            config=StoryProjectWritebackConfig(mode="apply"),
        )

        self.assertTrue(result.applied)
        after = context_path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(manual))
        self.assertEqual(1, after.count("NovelAgent:semantic-state version=1"))
        projection = parse_managed_block(after).projection
        self.assertEqual("控制室", projection["values"]["story_state.last_scene_location"])
        self.assertNotIn("story_state.open_threads", projection["values"])
        self.assertIn(
            "story_state.open_threads",
            {item["field_path"] for item in projection["tombstones"]},
        )

    def test_partial_apply_records_failed_target(self) -> None:
        root = self._story_project_root("partial")
        failed_name = TRACKING_TARGETS["timeline"]

        from core.story_project import writer

        original = writer._atomic_write_text

        def flaky_write(path: Path, content: str) -> None:
            if path.name == failed_name:
                raise OSError("timeline is locked")
            original(path, content)

        with patch("core.story_project.writer._atomic_write_text", side_effect=flaky_write):
            _plan, result = self._writeback(root)

        payload = result.to_dict()
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["partial"])
        self.assertEqual(1, len(payload["failed_targets"]))
        statuses = {Path(target["path"]).name: target["status"] for target in payload["targets"]}
        self.assertEqual("failed", statuses[failed_name])
        self.assertEqual("created", statuses[canonical_prose_path(root, 3, "稳定标题").name])

    def test_missing_required_beat_blocks_writeback(self) -> None:
        root = self._story_project_root("missing_beat")
        run = self._run()
        run["chapter"]["pipeline"]["blueprint_coverage"]["missing_beat_indexes"] = [1]

        _plan, result = run_story_project_writeback(
            context=self._context(root),
            run=run,
            chapter_text="# Chapter\n\ntext",
            validation={"ok": True},
            analysis={},
            config=StoryProjectWritebackConfig(mode="apply"),
        )

        payload = result.to_dict()
        self.assertFalse(payload["applied"])
        self.assertIn("missing_required_beat", payload["blocked_reasons"])
        self.assertEqual([], list((root / PROSE_DIR_NAME).iterdir()))

    def test_missing_ending_pressure_blocks_writeback(self) -> None:
        root = self._story_project_root("missing_ending_pressure")
        run = self._run()
        run["chapter"]["pipeline"]["blueprint_coverage"]["ending_pressure_covered"] = False

        _plan, result = run_story_project_writeback(
            context=self._context(root),
            run=run,
            chapter_text="# Chapter\n\ntext",
            validation={"ok": True},
            analysis={},
            config=StoryProjectWritebackConfig(mode="apply"),
        )

        payload = result.to_dict()
        self.assertFalse(payload["applied"])
        self.assertIn("missing_ending_pressure", payload["blocked_reasons"])
        self.assertEqual([], list((root / PROSE_DIR_NAME).iterdir()))
        self.assertEqual([], list((root / TRACKING_DIR_NAME).iterdir()))

    def test_validation_not_ok_blocks_writeback(self) -> None:
        root = self._story_project_root("validation_not_ok")

        _plan, result = run_story_project_writeback(
            context=self._context(root),
            run=self._run(),
            chapter_text="# Chapter\n\ntext",
            validation={"ok": False, "problems": [{"code": "logic_error"}]},
            analysis={},
            config=StoryProjectWritebackConfig(mode="apply"),
        )

        payload = result.to_dict()
        self.assertFalse(payload["applied"])
        self.assertIn("validation_not_ok", payload["blocked_reasons"])
        self.assertEqual([], list((root / PROSE_DIR_NAME).iterdir()))
        self.assertEqual([], list((root / TRACKING_DIR_NAME).iterdir()))

    def test_run_not_committed_blocks_writeback(self) -> None:
        root = self._story_project_root("run_not_committed")
        run = self._run()
        run["committed"] = False

        _plan, result = run_story_project_writeback(
            context=self._context(root),
            run=run,
            chapter_text="# Chapter\n\ntext",
            validation={"ok": True},
            analysis={},
            config=StoryProjectWritebackConfig(mode="apply"),
        )

        payload = result.to_dict()
        self.assertFalse(payload["applied"])
        self.assertIn("run_not_committed", payload["blocked_reasons"])
        self.assertEqual([], list((root / PROSE_DIR_NAME).iterdir()))
        self.assertEqual([], list((root / TRACKING_DIR_NAME).iterdir()))


if __name__ == "__main__":
    unittest.main()
