from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from api.contracts import (
    CHAPTER_CONTRACT,
    POLISH_CONTRACT,
    ModelCallError,
    ModelOutputError,
    detect_mojibake,
    validate_text_output,
)
from core.engine.executor import AgentExecutor
from core.schema import validate_schema
import modules.chapter_generator.generator as chapter_module
import modules.claude_polish.polisher as polish_module
import modules.scene_repair.repairer as repair_module


class ModelContractTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_model_contracts" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_rejects_empty_text_output(self) -> None:
        with self.assertRaises(ModelOutputError):
            validate_text_output("", CHAPTER_CONTRACT)

    def test_rejects_assistant_commentary_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
            validate_text_output(
                "Here is the chapter:\nThe shelter faced danger and a costly choice.",
                CHAPTER_CONTRACT,
            )

    def test_rejects_fenced_code_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
            validate_text_output(
                "```markdown\nThe shelter faced danger and a costly choice.\n```",
                CHAPTER_CONTRACT,
            )

    def test_rejects_labeled_chapter_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
            validate_text_output(
                "Chapter:\nThe shelter faced danger and a costly choice.",
                CHAPTER_CONTRACT,
            )

    def test_rejects_markdown_heading_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "Markdown formatting"):
            validate_text_output(
                "# Chapter 4\n\nThe shelter faced danger and a costly choice.",
                CHAPTER_CONTRACT,
            )

    def test_rejects_plain_chapter_heading_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "Markdown formatting"):
            validate_text_output(
                "Chapter 4\n\nThe shelter faced danger and a costly choice.",
                CHAPTER_CONTRACT,
            )

    def test_rejects_markdown_rule_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "Markdown formatting"):
            validate_text_output(
                "---\n\nThe shelter faced danger and a costly choice.",
                CHAPTER_CONTRACT,
            )

    def test_rejects_latin_mojibake_output(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "mojibake"):
            validate_text_output(
                "ç¼å œç°³é—„å—™ç‰ƒæµ ãƒ¤è´Ÿé‘·î„ç¹æµ¼æ°¬æƒ‰",
                CHAPTER_CONTRACT,
            )

    def test_rejects_cjk_mojibake_output(self) -> None:
        text = "榛戞湀闆嗗競鐨勭伅榻愰綈鏆椾笅锛岄檰鐮佺珯鍦ㄨ埞杈广€?"
        self.assertTrue(detect_mojibake(text)["looks_corrupted"])
        self.assertFalse(detect_mojibake(text)["reject"])

    def test_chapter_generator_rejects_empty_model_response(self) -> None:
        original_chat_completion = chapter_module.chat_completion
        chapter_module.chat_completion = lambda messages, **kwargs: ""
        try:
            with self.assertRaises(ModelOutputError):
                chapter_module.generate_chapter("input pack", dry_run=False)
        finally:
            chapter_module.chat_completion = original_chat_completion

    def test_chapter_generator_rejects_meta_model_response(self) -> None:
        original_chat_completion = chapter_module.chat_completion
        chapter_module.chat_completion = lambda messages, **kwargs: "As an AI, I cannot write that chapter."
        try:
            with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
                chapter_module.generate_chapter("input pack", dry_run=False)
        finally:
            chapter_module.chat_completion = original_chat_completion

    def test_chapter_generator_sets_model_call_stage(self) -> None:
        seen_kwargs: list[dict] = []
        original_chat_completion = chapter_module.chat_completion

        def completion(messages, **kwargs):
            seen_kwargs.append(kwargs)
            return "The shelter faced danger as the team had to choose a costly rescue."

        chapter_module.chat_completion = completion
        try:
            chapter_module.generate_chapter("input pack", dry_run=False)
        finally:
            chapter_module.chat_completion = original_chat_completion

        self.assertEqual("chapter_generation", seen_kwargs[0]["stage"])

    def test_polisher_rejects_empty_response(self) -> None:
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: ""
        try:
            with self.assertRaises(ModelOutputError):
                polish_module.polish_chapter("A long enough chapter placeholder.", dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_meta_response(self) -> None:
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: "Note: the chapter is already polished."
        try:
            with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
                polish_module.polish_chapter("A long enough chapter placeholder.", dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_labeled_response(self) -> None:
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: "Polished chapter:\nThe shelter faced danger."
        try:
            with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
                polish_module.polish_chapter("A long enough chapter placeholder.", dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_markdown_heading_response(self) -> None:
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: "## Polished Chapter\nThe shelter faced danger."
        try:
            with self.assertRaisesRegex(ModelOutputError, "Markdown formatting"):
                polish_module.polish_chapter("A long enough chapter placeholder.", dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_english_translation_of_chinese_chapter(self) -> None:
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: (
            "In the Black-Moon Market, Lu Yan and A-Zhao stood beside the ferry while the ledger "
            "opened again. The Ferryman asked for a higher price, and Lu Yan had to choose."
        )
        try:
            with self.assertRaisesRegex(ModelOutputError, "Simplified Chinese"):
                polish_module.polish_chapter("黑月集市里，陆砚和阿照站在船边。", dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_interactive_claude_confirmation(self) -> None:
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: (
            "黑潮在他们脚下合拢。\n\n"
            "如果你希望我对这一章进行润色，请确认这段就是待润色的原稿。"
        )
        try:
            with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
                polish_module.polish_chapter("黑潮在他们脚下合拢。", dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_truncated_shortened_output(self) -> None:
        source = "陆砚站在第七码头，黑水推着无桅窄船向前。" * 120
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: (
            "陆砚站在第七码头，黑水推着无桅窄船向前。" * 20
        )
        try:
            with self.assertRaisesRegex(ModelOutputError, "truncated|over-compressed"):
                polish_module.polish_chapter(source, dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polisher_rejects_missing_terminal_punctuation(self) -> None:
        source = "陆砚站在第七码头，黑水推着无桅窄船向前。" * 20
        original_polisher = polish_module.polish_with_claude
        polish_module.polish_with_claude = lambda chapter_text, dry_run=False: (
            ("陆砚站在第七码头，黑水推着无桅窄船向前。" * 8) +
            "阿照听见铜灯一起震动，船身猛地向下一沉，两侧铜灯同"
        )
        try:
            with self.assertRaisesRegex(ModelOutputError, "truncated"):
                polish_module.polish_chapter(source, dry_run=False)
        finally:
            polish_module.polish_with_claude = original_polisher

    def test_polish_contract_accepts_chinese_prose_with_some_terms(self) -> None:
        text = "黑月集市的灯齐齐暗下，Lu Yan 这个旧译名只在账页边缘闪了一瞬，陆砚没有回头。"

        self.assertEqual(text, validate_text_output(text, POLISH_CONTRACT))

    def test_scene_repair_rejects_meta_model_response(self) -> None:
        original_chat_completion = repair_module.chat_completion
        repair_module.chat_completion = lambda *args, **kwargs: "Error: unable to repair the chapter."
        try:
            with self.assertRaisesRegex(ModelOutputError, "assistant commentary"):
                repair_module.repair_scene(
                    "The serum conflict resolved.",
                    {
                        "ok": False,
                        "problems": [
                            {
                                "code": "forbidden_constraint_term",
                                "message": "Chapter contains forbidden constraint term.",
                                "term": "serum conflict resolved",
                            }
                        ],
                    },
                    "input pack",
                    dry_run=False,
                )
        finally:
            repair_module.chat_completion = original_chat_completion

    def test_scene_repair_rejects_markdown_heading_model_response(self) -> None:
        original_chat_completion = repair_module.chat_completion
        repair_module.chat_completion = lambda *args, **kwargs: "# Repaired Chapter\nThe serum conflict remains unresolved."
        try:
            with self.assertRaisesRegex(ModelOutputError, "Markdown formatting"):
                repair_module.repair_scene(
                    "The serum conflict resolved.",
                    {
                        "ok": False,
                        "problems": [
                            {
                                "code": "forbidden_constraint_term",
                                "message": "Chapter contains forbidden constraint term.",
                                "term": "serum conflict resolved",
                            }
                        ],
                    },
                    "input pack",
                    dry_run=False,
                )
        finally:
            repair_module.chat_completion = original_chat_completion

    def test_scene_repair_uses_repair_prompt_for_model_repair(self) -> None:
        seen_calls: list[tuple[list[dict[str, str]], dict]] = []
        original_chat_completion = repair_module.chat_completion

        def completion(messages: list[dict[str, str]], **kwargs) -> str:
            seen_calls.append((messages, kwargs))
            return "The repaired chapter keeps the serum conflict unresolved while danger escalates."

        repair_module.chat_completion = completion
        try:
            repaired = repair_module.repair_scene(
                "The serum conflict resolved.",
                {
                    "ok": False,
                    "problems": [
                        {
                            "code": "forbidden_constraint_term",
                            "message": "Chapter contains forbidden constraint term.",
                            "term": "serum conflict resolved",
                        }
                    ],
                },
                "input pack",
                dry_run=False,
                recovery_context={
                    "available": True,
                    "source_run_id": "chapter_1_test",
                    "status": "rejected",
                    "problem_codes": ["missing_conflict_marker"],
                    "executed_checks": ["logic"],
                    "skipped_checks": ["continuity", "spatial"],
                    "repair_attempts": 1,
                },
            )
        finally:
            repair_module.chat_completion = original_chat_completion

        self.assertIn("serum conflict unresolved", repaired)
        self.assertEqual(0.2, seen_calls[0][1]["temperature"])
        self.assertEqual("scene_repair", seen_calls[0][1]["stage"])
        self.assertIn("Repair Prompt", seen_calls[0][0][0]["content"])
        self.assertIn("forbidden_constraint_term", seen_calls[0][0][1]["content"])
        self.assertIn("repair_plan", seen_calls[0][0][1]["content"])
        self.assertIn("remove_forbidden_term", seen_calls[0][0][1]["content"])
        payload = json.loads(seen_calls[0][0][1]["content"])
        self.assertEqual("chapter_1_test", payload["recovery_context"]["source_run_id"])
        self.assertEqual(["continuity", "spatial"], payload["recovery_context"]["skipped_checks"])
        self.assertEqual("chapter_1_test", payload["repair_plan"]["recovery"]["source_run_id"])
        self.assertIn("previous_validation_skipped", payload["repair_plan"]["recovery"]["failure_modes"])
        self.assertIn("input pack", seen_calls[0][0][1]["content"])

    def test_executor_persists_failed_run_diagnostics_when_model_contract_fails(self) -> None:
        tmp_path = self._case_dir("executor_failure")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot = {"chapter_index": 4, "world_state": {}, "characters": {}, "timeline": []}
        before = json.dumps(snapshot, ensure_ascii=False, indent=2)
        snapshot_path.write_text(before, encoding="utf-8")

        def failing_generator(input_pack: str) -> str:
            raise ModelOutputError("chapter output is empty")

        with self.assertRaises(ModelOutputError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                generator=failing_generator,
            ).run_once(persist=True)

        self.assertEqual(before, snapshot_path.read_text(encoding="utf-8"))
        run_files = list((tmp_path / "runs").glob("chapter_4_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual("failed", saved["run"]["status"])
        self.assertFalse(saved["run"]["committed"])
        self.assertEqual("rule", saved["run"]["director"]["mode"])
        self.assertEqual("completed", saved["run"]["director"]["status"])
        self.assertEqual("ModelOutputError", saved["run"]["error"]["type"])
        self.assertEqual("generate_chapter", saved["run"]["trace"][-1]["action"])
        self.assertEqual("failed", saved["run"]["trace"][-1]["status"])
        self.assertIs(saved["run"]["trace"][-1], validate_schema(saved["run"]["trace"][-1], "trace_event.schema.json"))
        input_pack_path = Path(saved["run"]["input_pack"]["artifact"]["path"])
        self.assertTrue(input_pack_path.exists())

    def test_executor_trace_preserves_model_call_context_on_provider_failure(self) -> None:
        tmp_path = self._case_dir("executor_model_call_failure")
        snapshot_path = tmp_path / "snapshot.json"
        snapshot = {"chapter_index": 4, "world_state": {}, "characters": {}, "timeline": []}
        before = json.dumps(snapshot, ensure_ascii=False, indent=2)
        snapshot_path.write_text(before, encoding="utf-8")

        def failing_generator(input_pack: str) -> str:
            raise ModelCallError(
                "OpenAI chat completion failed: timeout",
                provider="openai",
                stage="chapter_generation",
                model="gpt-test",
                cause=TimeoutError("timeout"),
            )

        with self.assertRaises(ModelCallError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=tmp_path / "runs",
                generator=failing_generator,
            ).run_once(persist=True)

        run_files = list((tmp_path / "runs").glob("chapter_4_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        trace_event = saved["run"]["trace"][-1]
        self.assertEqual("ModelCallError", trace_event["error_type"])
        self.assertEqual("openai", trace_event["model_call"]["provider"])
        self.assertEqual("chapter_generation", trace_event["model_call"]["stage"])
        self.assertEqual("gpt-test", trace_event["model_call"]["model"])
        self.assertEqual("TimeoutError", trace_event["model_call"]["cause_type"])
        self.assertIs(trace_event, validate_schema(trace_event, "trace_event.schema.json"))


if __name__ == "__main__":
    unittest.main()
