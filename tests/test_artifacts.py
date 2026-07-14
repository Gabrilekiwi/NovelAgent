from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid

from core.engine.artifacts import (
    prepare_chapter_artifact,
    prepare_chapter_pipeline_artifacts,
    prepare_input_pack_artifact,
    prepare_review_repair_artifacts,
    prepare_snapshot_pack_artifact,
    prepare_story_project_writeback_artifacts,
    save_chapter_artifact,
    save_chapter_pipeline_artifacts,
    save_input_pack_artifact,
    save_review_repair_artifacts,
    save_snapshot_pack_artifact,
    save_story_project_writeback_artifacts,
)


class PreparedArtifactsTest(unittest.TestCase):
    def _unused_case_dir(self, name: str) -> Path:
        return Path(".tmp") / "test_artifacts" / f"{name}_{uuid.uuid4().hex}" / "中文目录"

    def _run(self) -> dict:
        return {
            "id": "run-cn",
            "chapter_index": 7,
            "status": "committed",
            "committed": True,
            "repair_attempts": 2,
        }

    def _pipeline(self) -> dict:
        return {
            "plan": {"标题": "潜入", "scene_count": 1},
            "scene_drafts": [{"index": 1, "goal": "进入城门", "text": "雨落长街。"}],
            "scene_spans": [{"index": 1, "start_char": 0, "end_char": 5}],
            "merged_chapter": "雨落长街。",
        }

    def _review_repair(self) -> dict:
        return {
            "repair_plan": {"strategy": "补强因果"},
            "repair_deltas": [{"attempt": 1, "change": "加入线索"}],
            "final_chapter": "修订后的正文。",
            "final_validation": {"status": "pass"},
            "final_review": {"score": 88},
            "status": "repaired",
        }

    def _assert_saved_matches_prepared(self, prepared: dict, saved: dict) -> None:
        self.assertEqual(prepared["metadata"], saved)
        metadata_by_path = {
            item["path"]: item
            for item in self._artifact_metadata(prepared["metadata"])
        }
        self.assertEqual(set(metadata_by_path), {target["path"] for target in prepared["targets"]})
        for target in prepared["targets"]:
            self.assertEqual({"path", "content"}, set(target))
            path = Path(target["path"])
            expected_bytes = target["content"].encode("utf-8")
            self.assertEqual(expected_bytes, path.read_bytes())
            metadata = metadata_by_path[str(path)]
            if "sha256" in metadata:
                self.assertEqual(hashlib.sha256(expected_bytes).hexdigest(), metadata["sha256"])

    def _artifact_metadata(self, value: object) -> list[dict]:
        if isinstance(value, dict):
            if isinstance(value.get("path"), str):
                return [value]
            artifacts: list[dict] = []
            for child in value.values():
                artifacts.extend(self._artifact_metadata(child))
            return artifacts
        if isinstance(value, list):
            artifacts = []
            for child in value:
                artifacts.extend(self._artifact_metadata(child))
            return artifacts
        return []

    def test_prepare_functions_are_pure_and_do_not_mutate_inputs(self) -> None:
        root = self._unused_case_dir("pure")
        run = self._run()
        pipeline = self._pipeline()
        validation = {"status": "pass", "说明": "一致"}
        repair_deltas = [{"attempt": 1, "变化": "收束"}]
        review_repair = self._review_repair()
        plan = {"mode": "apply", "目标": "正文"}
        result = {"diff_summary": {"updated": 1}, "状态": "完成"}
        originals = copy.deepcopy((run, pipeline, validation, repair_deltas, review_repair, plan, result))

        prepared = [
            prepare_chapter_artifact(chapter_text="中文正文", run=run, output_dir=root / "章节"),
            prepare_input_pack_artifact(input_pack="输入包", run=run, output_dir=root / "输入"),
            prepare_snapshot_pack_artifact(snapshot_pack="快照包", run=run, output_dir=root / "快照"),
            prepare_chapter_pipeline_artifacts(
                pipeline=pipeline,
                validation=validation,
                repair_deltas=repair_deltas,
                run=run,
                output_dir=root / "流水线",
            ),
            prepare_review_repair_artifacts(
                review_repair=review_repair,
                run=run,
                output_dir=root / "审阅修复",
            ),
            prepare_story_project_writeback_artifacts(
                plan=plan,
                result=result,
                run=run,
                output_dir=root / "故事回写",
            ),
        ]

        self.assertFalse(root.exists())
        self.assertEqual(originals, (run, pipeline, validation, repair_deltas, review_repair, plan, result))
        for bundle in prepared:
            self.assertTrue(bundle["targets"])
            self.assertTrue(all("中文目录" in target["path"] for target in bundle["targets"]))

    def test_singular_prepare_bytes_and_metadata_match_save_results(self) -> None:
        root = self._unused_case_dir("singular")
        run = self._run()

        chapter = prepare_chapter_artifact(
            chapter_text="第一行\n第二行",
            run=run,
            output_dir=root / "章节",
        )
        self.assertTrue(chapter["targets"][0]["content"].startswith("# Chapter 7\n\n"))
        saved_chapter = save_chapter_artifact(
            chapter_text="第一行\n第二行",
            run=run,
            output_dir=root / "章节",
        )
        self._assert_saved_matches_prepared(chapter, saved_chapter)

        input_pack = prepare_input_pack_artifact(
            input_pack="输入包\n第二行",
            run=run,
            output_dir=root / "输入",
        )
        input_logical = (
            "# Input Pack: Chapter 7\n\n"
            "- Run: `run-cn`\n"
            "- Status: `committed`\n"
            "- Committed: `True`\n\n"
            "---\n\n"
            "输入包\n第二行\n"
        )
        self.assertEqual(input_logical.replace("\n", os.linesep), input_pack["targets"][0]["content"])
        saved_input = save_input_pack_artifact(
            input_pack="输入包\n第二行",
            run=run,
            output_dir=root / "输入",
        )
        self._assert_saved_matches_prepared(input_pack, saved_input)

        snapshot = prepare_snapshot_pack_artifact(
            snapshot_pack="快照包\n第二行",
            run=run,
            output_dir=root / "快照",
        )
        saved_snapshot = save_snapshot_pack_artifact(
            snapshot_pack="快照包\n第二行",
            run=run,
            output_dir=root / "快照",
        )
        self._assert_saved_matches_prepared(snapshot, saved_snapshot)

    def test_bundle_prepare_bytes_sha_and_metadata_match_save_results(self) -> None:
        run = self._run()

        pipeline_root = self._unused_case_dir("pipeline")
        pipeline = self._pipeline()
        prepared_pipeline = prepare_chapter_pipeline_artifacts(
            pipeline=pipeline,
            validation={"status": "pass"},
            repair_deltas=[{"attempt": 1, "change": "收束"}],
            run=run,
            output_dir=pipeline_root,
        )
        logical_plan = json.dumps(pipeline["plan"], ensure_ascii=False, indent=2)
        self.assertEqual(len(logical_plan), prepared_pipeline["metadata"]["plan"]["chars"])
        self.assertEqual(
            logical_plan.replace("\n", os.linesep),
            prepared_pipeline["targets"][0]["content"],
        )
        saved_pipeline = save_chapter_pipeline_artifacts(
            pipeline=pipeline,
            validation={"status": "pass"},
            repair_deltas=[{"attempt": 1, "change": "收束"}],
            run=run,
            output_dir=pipeline_root,
        )
        self._assert_saved_matches_prepared(prepared_pipeline, saved_pipeline)

        review_root = self._unused_case_dir("review")
        review_repair = self._review_repair()
        prepared_review = prepare_review_repair_artifacts(
            review_repair=review_repair,
            run=run,
            output_dir=review_root,
        )
        saved_review = save_review_repair_artifacts(
            review_repair=review_repair,
            run=run,
            output_dir=review_root,
        )
        self._assert_saved_matches_prepared(prepared_review, saved_review)

        writeback_root = self._unused_case_dir("writeback")
        plan = {"mode": "apply", "目标": "正文"}
        result = {"diff_summary": {"updated": 1}, "状态": "完成"}
        prepared_writeback = prepare_story_project_writeback_artifacts(
            plan=plan,
            result=result,
            run=run,
            output_dir=writeback_root,
        )
        saved_writeback = save_story_project_writeback_artifacts(
            plan=plan,
            result=result,
            run=run,
            output_dir=writeback_root,
        )
        self._assert_saved_matches_prepared(prepared_writeback, saved_writeback)


if __name__ == "__main__":
    unittest.main()
