from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from core.engine.executor import AgentExecutor, StoryProjectContextError
from core.memory_v2 import replay_memory_events
from core.runtime_paths import RuntimePaths
from core.story_project.activation import activate_story_state, build_story_state_calibration_report
from core.story_project.identity import ensure_project_identity
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from core.story_project.writer import StoryProjectWritebackConfig


class StrictStoryProjectE2ETest(unittest.TestCase):
    def _book(self) -> Path:
        source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
        target = (
            Path.cwd()
            / ".tmp"
            / "test_strict_storyproject_e2e"
            / uuid.uuid4().hex
            / "book"
        )
        shutil.copytree(source, target)
        chapter_two = target / "大纲" / "细纲_第002章.md"
        chapter_two.write_text(
            chapter_two.read_text(encoding="utf-8").replace(
                "结尾压力：",
                "结尾压力：危险迫使两人立刻做出选择；",
                1,
            ),
            encoding="utf-8",
        )
        (target / "大纲" / "细纲_第003章.md").write_text(
            "# 第三章：控制室回声\n\n"
            "- 核心事件：林澈与周岚追踪闸门信号。\n\n"
            "## 剧情节拍\n\n"
            "1. 林澈确认控制室信号来源。\n"
            "2. 周岚发现新的门禁风险。\n\n"
            "- 结尾压力：危险迫使两人做出选择，备用闸门开始倒计时。\n",
            encoding="utf-8",
        )
        return target

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
            generated_at="2026-07-13T00:00:00+00:00",
        )

    def test_second_chapter_reads_first_prose_managed_state_and_memory_revision(self) -> None:
        book = self._book()
        identity = ensure_project_identity(book)
        activated = activate_story_state(book, self._qualified_report(identity.book_id))
        paths = RuntimePaths.for_story_project(book)
        delegate = build_generation_story_project_context_loader(
            story_project=book,
            chapter=2,
            project_identity=activated,
        )
        contexts: list[dict] = []

        class RecordingLoader:
            story_project_root = delegate.story_project_root
            project_identity = activated

            def __call__(self, snapshot, memory_context, chapter_hint=None):
                context = delegate(snapshot, memory_context, chapter_hint)
                contexts.append(context.to_dict())
                return context

        result = AgentExecutor(
            snapshot_path=paths.snapshot_path,
            memory_path=paths.memory_dir / "notion_memory.json",
            run_dir=paths.run_dir,
            chapter_dir=paths.chapter_dir,
            persistence_dir=paths.persistence_dir,
            dry_run=True,
            story_project_context_loader=RecordingLoader(),
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
        ).run_loop(steps=2, persist=True)

        self.assertTrue(
            result["succeeded"],
            {
                "stopped_reason": result.get("stopped_reason"),
                "failure_reasons": result.get("failure_reasons"),
                "runs": [
                    {
                        "status": item["run"].get("status"),
                        "accepted": item["run"].get("quality", {}).get("accepted"),
                        "committed": item["run"].get("persistence", {}).get("committed"),
                        "story_project": item["run"].get("story_project"),
                        "memory": item["run"].get("memory"),
                    }
                    for item in result.get("runs", [])
                ],
            },
        )
        self.assertEqual(2, len(contexts))
        second = contexts[1]
        self.assertEqual("strict", second["story_state_mode"])
        self.assertTrue(second["semantic_audit"]["authoritative"])
        self.assertIn("第002章", second["previous_chapter_context"]["path_ref"]["relative_path"])
        self.assertGreater(second["memory_v2"]["revision"], 1)
        self.assertTrue(second["semantic_state"]["timeline"])
        self.assertTrue(
            any(
                source["source_kind"] == "managed_projection"
                for source in second["semantic_state"]["provenance"]
            )
        )
        replay = replay_memory_events(paths.memory_dir / "v2" / "events")
        self.assertEqual(2, replay["committed_chapter_count"])
        self.assertEqual(
            result["runs"][-1]["run"]["memory"]["v2"]["revision"],
            replay["revision"],
        )

    def test_persistent_strict_run_requires_apply_writeback(self) -> None:
        book = self._book()
        identity = ensure_project_identity(book)
        activated = activate_story_state(book, self._qualified_report(identity.book_id))
        paths = RuntimePaths.for_story_project(book)
        loader = build_generation_story_project_context_loader(
            story_project=book,
            chapter=2,
            project_identity=activated,
        )

        executor = AgentExecutor(
            snapshot_path=paths.snapshot_path,
            memory_path=paths.memory_dir / "notion_memory.json",
            run_dir=paths.run_dir,
            chapter_dir=paths.chapter_dir,
            persistence_dir=paths.persistence_dir,
            dry_run=True,
            story_project_context_loader=loader,
            quality_policy="minimal",
        )

        with self.assertRaisesRegex(StoryProjectContextError, "strict_story_state_requires_apply_writeback"):
            executor.run_once(persist=True)


if __name__ == "__main__":
    unittest.main()
