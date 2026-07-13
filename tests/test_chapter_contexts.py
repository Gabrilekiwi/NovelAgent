from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.chapter_contexts import (
    ChapterContextError,
    build_attempt_context,
    build_recovery_context,
    resolve_committed_previous_chapter_artifact,
    resolve_story_project_previous_chapter,
)
from core.engine.artifacts import save_chapter_artifact
from core.engine.executor import _previous_chapter_text
from core.story_project.mapper import build_story_project_runtime_context


class ChapterContextsTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        path = Path(".tmp") / "test_chapter_contexts" / f"{name}_{uuid.uuid4().hex}"
        path.mkdir(parents=True)
        return path

    def _story_project(self, name: str) -> Path:
        root = self._case_dir(name) / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        return root

    def test_story_project_resolves_only_unique_n_minus_one_with_full_hash(self) -> None:
        root = self._story_project("unique_previous")
        previous = root / "正文" / "第001章_开端.md"
        raw = b"\xef\xbb\xbf" + ("开头。" + "中段。" * 50 + "最新尾声。").encode("utf-8")
        previous.write_bytes(raw)

        context = resolve_story_project_previous_chapter(
            root,
            2,
            generation_max_chars=40,
            review_tail_chars=12,
        )

        self.assertEqual(1, context.chapter_index)
        self.assertEqual("story_project_prose", context.source_kind)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), context.sha256)
        self.assertEqual("正文/第001章_开端.md", context.path_ref.relative_path)
        self.assertTrue(context.generation_excerpt["truncated"])
        self.assertEqual(2, len(context.generation_excerpt["ranges"]))
        self.assertTrue(context.review_tail["text"].endswith("最新尾声。"))
        self.assertTrue(context.to_dict()["committed_verified"])

    def test_first_chapter_allows_no_previous_but_later_chapter_fails_closed(self) -> None:
        root = self._story_project("missing_previous")

        self.assertIsNone(resolve_story_project_previous_chapter(root, 1))
        with self.assertRaises(ChapterContextError) as raised:
            resolve_story_project_previous_chapter(root, 2)
        self.assertEqual("previous_chapter_missing", raised.exception.code)
        self.assertEqual("high", raised.exception.risk)

    def test_duplicate_n_minus_one_is_always_blocking(self) -> None:
        root = self._story_project("duplicate_previous")
        (root / "正文" / "第001章_A.md").write_text("A", encoding="utf-8")
        (root / "正文" / "第1章_B.md").write_text("B", encoding="utf-8")

        with self.assertRaises(ChapterContextError) as raised:
            resolve_story_project_previous_chapter(root, 2, fail_closed=False)

        self.assertEqual("previous_chapter_conflict", raised.exception.code)

    def test_mapper_can_fail_closed_and_does_not_project_previous_into_memory(self) -> None:
        root = self._story_project("mapper")
        (root / "大纲" / "细纲_第002章.md").write_text("# 二\n核心事件：继续\n- 节拍\n结尾压力：门开", encoding="utf-8")

        with self.assertRaises(ChapterContextError):
            build_story_project_runtime_context(root, 2, previous_chapter_fail_closed=True)

        (root / "正文" / "第001章_一.md").write_text("唯一上一章", encoding="utf-8")
        context = build_story_project_runtime_context(root, 2, previous_chapter_fail_closed=True)
        self.assertIsNotNone(context.previous_chapter_context)
        self.assertEqual("previous_chapter", context.previous_prose["context_kind"])
        self.assertFalse(any(item["name"] == "previous_prose" for item in context.memory_context_overlay["items"]))

    def test_attempt_and_recovery_contexts_remain_same_chapter_draft_context(self) -> None:
        attempt = build_attempt_context(
            chapter_index=8,
            run_id="run-rejected",
            status="rejected",
            draft_text="本轮被拒稿件",
        )
        recovery = build_recovery_context(
            chapter_index=8,
            source_run_id="run-failed",
            source_status="failed",
            draft_text="崩溃前草稿",
        )

        self.assertEqual(8, attempt.chapter_index)
        self.assertEqual("rejected", attempt.status)
        self.assertEqual("本轮被拒稿件", attempt.excerpt["text"])
        self.assertEqual(8, recovery.chapter_index)
        self.assertEqual("failed", recovery.source_status)
        self.assertFalse(recovery.artifact_hash_verified)
        self.assertNotIn("path_ref", attempt.to_dict())
        self.assertIn("artifact_path_ref", recovery.to_dict())

    def test_review_previous_text_uses_transaction_tail_and_rejects_same_chapter_retry(self) -> None:
        rejected_memory = {
            "last_run": {
                "chapter_index": 8,
                "status": "rejected",
                "committed": False,
                "chapter_text": "不得冒充上一章的被拒稿",
            }
        }
        story_context = {
            "previous_chapter_context": {
                "chapter_index": 7,
                "review_tail": {"text": "本次 read transaction 的第七章尾部"},
            }
        }

        self.assertEqual(
            "本次 read transaction 的第七章尾部",
            _previous_chapter_text(
                rejected_memory,
                story_project_context=story_context,
                chapter_index=8,
            ),
        )
        self.assertIsNone(_previous_chapter_text(rejected_memory, chapter_index=8))

        committed_text = "已验证的第七章"
        committed_memory = {
            "last_run": {
                "chapter_index": 7,
                "status": "committed",
                "committed": True,
                "artifact_hash_verified": True,
                "chapter_text": committed_text,
                "chapter_text_sha256": hashlib.sha256(committed_text.encode("utf-8")).hexdigest(),
            }
        }
        self.assertEqual(committed_text, _previous_chapter_text(committed_memory, chapter_index=8))

    def test_non_story_fallback_accepts_only_hash_verified_committed_n_minus_one(self) -> None:
        case = self._case_dir("fallback")
        run_dir = case / "runs"
        chapter_dir = case / "chapters"
        run_dir.mkdir()
        chapter_dir.mkdir()
        committed_run = {
            "id": "chapter_3_committed",
            "chapter_index": 3,
            "status": "committed",
            "committed": True,
            "repair_attempts": 0,
        }
        artifact = save_chapter_artifact(
            chapter_text="经过提交的第三章正文",
            run=committed_run,
            output_dir=chapter_dir,
        )
        committed_run["chapter"] = {"artifact": artifact}
        (run_dir / "chapter_3_committed.json").write_text(
            json.dumps({"run": committed_run}, ensure_ascii=False),
            encoding="utf-8",
        )
        rejected = dict(committed_run)
        rejected.update({"id": "chapter_3_rejected", "status": "rejected", "committed": False})
        (run_dir / "chapter_3_rejected.json").write_text(
            json.dumps({"run": rejected}, ensure_ascii=False),
            encoding="utf-8",
        )
        wrong_chapter = dict(committed_run)
        wrong_chapter.update({"id": "chapter_4_committed", "chapter_index": 4})
        (run_dir / "chapter_4_committed.json").write_text(
            json.dumps({"run": wrong_chapter}, ensure_ascii=False),
            encoding="utf-8",
        )

        context = resolve_committed_previous_chapter_artifact(
            chapter_index=4,
            run_dir=run_dir,
            chapter_artifact_root=chapter_dir,
        )

        self.assertEqual("committed_artifact", context.source_kind)
        self.assertEqual("经过提交的第三章正文", context.generation_excerpt["text"])
        self.assertEqual(artifact["sha256"], context.sha256)

        Path(artifact["path"]).write_text("被篡改", encoding="utf-8")
        with self.assertRaises(ChapterContextError) as raised:
            resolve_committed_previous_chapter_artifact(
                chapter_index=4,
                run_dir=run_dir,
                chapter_artifact_root=chapter_dir,
            )
        self.assertEqual("committed_previous_chapter_artifact_missing", raised.exception.code)

    def test_recovery_artifact_requires_matching_hash_and_stays_recovery_only(self) -> None:
        root = self._case_dir("recovery_artifact")
        artifact = root / "draft.md"
        artifact.write_text("失败草稿", encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

        context = build_recovery_context(
            chapter_index=5,
            source_run_id="run-5-failed",
            source_status="failed",
            draft_text="失败草稿",
            artifact_path=artifact,
            artifact_root=root,
            expected_artifact_sha256=digest,
        )

        self.assertTrue(context.artifact_hash_verified)
        self.assertEqual("draft.md", context.artifact_path_ref.relative_path)
        with self.assertRaises(ChapterContextError) as raised:
            build_recovery_context(
                chapter_index=5,
                source_run_id="run-5-failed",
                source_status="failed",
                draft_text="失败草稿",
                artifact_path=artifact,
                artifact_root=root,
                expected_artifact_sha256="0" * 64,
            )
        self.assertEqual("recovery_artifact_hash_mismatch", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
