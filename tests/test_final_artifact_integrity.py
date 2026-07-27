from __future__ import annotations

import unittest

from core.quality.final_artifact_integrity import (
    FinalArtifactIntegrityConfig,
    FinalArtifactIntegrityError,
    FinalArtifactIntegrityGate,
    build_integrity_stage_record,
    merge_integrity_report_into_validation,
    merge_integrity_reports_for_canonicalized_artifact,
    merge_integrity_reports_for_artifact,
)
from core.quality_decision import build_quality_decision
from core.state.snapshot import normalize_snapshot
from core.validator import validate_chapter


def _codes(report: dict) -> set[str]:
    return {str(item["code"]) for item in report["findings"]}


class FinalArtifactIntegrityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = FinalArtifactIntegrityGate()

    def test_exact_substantial_paragraph_duplicate_is_blocking_with_positions(self) -> None:
        repeated = (
            "顾晨推开检修门，先确认通道没有感染者，再逐一登记十二名幸存者，"
            "最后让钱明带队穿过缓冲走廊，所有人都在安全区重新集合。"
        )
        report = self.gate.evaluate(
            artifact_text=f"{repeated}\n\n队伍短暂休整后继续前进。\n\n{repeated}",
            stage="merge",
        )
        self.assertFalse(report["accepted"])
        finding = next(item for item in report["findings"] if item["code"] == "duplicate_scene_text")
        self.assertTrue(finding["blocking"])
        self.assertEqual(2, len(finding["locations"]))
        self.assertLess(finding["locations"][0]["start_char"], finding["locations"][1]["start_char"])

    def test_near_duplicate_substantial_paragraph_is_blocking(self) -> None:
        first = (
            "顾晨推开员工通道的检修门，发现十二名幸存者围在配电柜旁。"
            "他逐一登记身份，确认两名伤员的情况，随后宣布所有人接入主队，"
            "沿缓冲走廊向安全区转移。"
        )
        second = (
            "顾晨再次推开员工通道检修门，发现十二名幸存者仍围在配电柜旁。"
            "他重新登记每个人的身份，确认两名伤员情况，然后又宣布所有人接入主队，"
            "沿着缓冲走廊朝安全区转移。"
        )
        report = self.gate.evaluate(
            artifact_text=f"{first}\n\n{second}",
            stage="merge",
        )
        self.assertFalse(report["accepted"])
        finding = next(item for item in report["findings"] if item["code"] == "near_duplicate_scene_text")
        self.assertGreaterEqual(finding["similarity"]["combined"], 0.68)

    def test_short_dialogue_and_explicit_memory_repetition_are_not_blocking(self) -> None:
        text = (
            "“别开门。”钱明说。\n\n顾晨没有回答，只把手按在门锁上。\n\n"
            "“别开门。”钱明又说了一次。\n\n"
            "旧广播里也曾循环播放“别开门”，但那是顾晨明确记得的过去录音。"
        )
        report = self.gate.evaluate(artifact_text=text, stage="final_gate")
        self.assertTrue(report["accepted"])
        self.assertNotIn("duplicate_scene_text", _codes(report))

    def test_append_instead_of_replace_reports_transition_metrics(self) -> None:
        source = (
            "顾晨确认检修门已经关闭，十二名幸存者全部进入缓冲区。"
            "钱明守在队尾，远处撞击声越来越近。"
        )
        revision = (
            "检修门在顾晨身后闭合，十二名幸存者终于挤进缓冲区。"
            "钱明压住队尾，黑暗中的撞击正步步逼近。"
        )
        report = self.gate.evaluate(
            artifact_text=f"{source}\n\n{revision}",
            source_text=source,
            stage="repair",
        )
        self.assertFalse(report["accepted"])
        self.assertIn("repair_append_instead_of_replace", _codes(report))
        self.assertGreaterEqual(report["metrics"]["source_prefix_retained_ratio"], 0.99)
        self.assertTrue(report["metrics"]["suspected_append_instead_of_replace"])

    def test_scoped_prefix_and_suffix_insertions_do_not_look_like_two_drafts(self) -> None:
        source = (
            "The crew crossed the sealed gate while the alarm kept rising. "
            "They protected the serum and held their formation under pressure. "
            "No one restarted the evacuation or rediscovered the same survivors."
        )
        output = (
            "At the service corridor, Mira signaled the team forward. "
            + source
            + " The gate locked behind them, leaving one unresolved choice."
        )

        report = self.gate.evaluate(
            artifact_text=output,
            source_text=source,
            stage="repair",
        )

        self.assertTrue(report["accepted"])
        self.assertFalse(report["metrics"]["source_at_append_boundary"])
        self.assertFalse(report["metrics"]["suspected_append_instead_of_replace"])

    def test_hash_binding_mismatch_is_blocking(self) -> None:
        accepted = self.gate.evaluate(artifact_text="唯一的最终正文。", stage="final_gate")
        self.assertTrue(accepted["accepted"])
        writeback = self.gate.evaluate(
            artifact_text="写回时被改动的正文。\n",
            stage="writeback",
            expected_artifact_sha256=accepted["artifact_sha256"],
        )
        self.assertFalse(writeback["accepted"])
        self.assertIn("final_artifact_hash_mismatch", _codes(writeback))

    def test_require_accepted_raises_with_report(self) -> None:
        repeated = "这一段足够长，用于证明同一个完整正文片段不能在正式章节里被原样写入两次。" * 3
        report = self.gate.evaluate(
            artifact_text=f"{repeated}\n\n{repeated}",
            stage="merge",
        )
        with self.assertRaises(FinalArtifactIntegrityError) as raised:
            self.gate.require_accepted(report)
        self.assertEqual(report["artifact_sha256"], raised.exception.report["artifact_sha256"])

    def test_stage_record_binds_input_and_output_hashes(self) -> None:
        report = self.gate.evaluate(
            artifact_text="修订后的唯一正文。",
            source_text="原正文。",
            stage="polish",
        )
        record = build_integrity_stage_record(
            stage="polish",
            input_text="原正文。",
            output_text="修订后的唯一正文。",
            report=report,
        )
        self.assertEqual(report["artifact_sha256"], record["output_sha256"])
        self.assertEqual(report["accepted"], record["accepted"])
        self.assertEqual("polish", record["stage"])

    def test_final_gate_retains_unresolved_stage_finding_for_same_bytes(self) -> None:
        source = "The crew crossed the gate under pressure. " * 4
        output = source + "\n\n" + ("A rewritten version follows with different wording. " * 4)
        polish = self.gate.evaluate(
            artifact_text=output,
            source_text=source,
            stage="polish",
        )
        final = self.gate.evaluate(artifact_text=output, stage="final_gate")

        combined = merge_integrity_reports_for_artifact(final, [polish])

        self.assertTrue(final["accepted"])
        self.assertFalse(combined["accepted"])
        self.assertIn("polish_append_instead_of_replace", _codes(combined))
        self.assertEqual(["polish"], combined["metrics"]["matching_prior_stages"])

    def test_final_gate_drops_stage_finding_after_bytes_are_replaced(self) -> None:
        source = "The crew crossed the gate under pressure. " * 4
        appended = source + "\n\n" + ("A rewritten version follows with different wording. " * 4)
        polish = self.gate.evaluate(
            artifact_text=appended,
            source_text=source,
            stage="polish",
        )
        replacement = "Only the corrected scene remains after the transition. " * 4
        final = self.gate.evaluate(artifact_text=replacement, stage="final_gate")

        combined = merge_integrity_reports_for_artifact(final, [polish])

        self.assertTrue(combined["accepted"])
        self.assertNotIn("polish_append_instead_of_replace", _codes(combined))

    def test_safe_canonicalization_retains_blocking_prior_finding(self) -> None:
        source = "The crew crossed the gate under pressure. " * 4
        before = source + "\n\n" + (
            "A rewritten version follows with different wording. " * 4
        )
        polish = self.gate.evaluate(
            artifact_text=before,
            source_text=source,
            stage="polish",
        )
        canonicalized = before.rstrip() + "\n"
        final = self.gate.evaluate(
            artifact_text=canonicalized,
            stage="final_gate",
        )

        combined = merge_integrity_reports_for_canonicalized_artifact(
            final,
            [polish],
            before_artifact_text=before,
            before_artifact_sha256=polish["artifact_sha256"],
            canonicalized_artifact_text=canonicalized,
        )

        self.assertTrue(final["accepted"])
        self.assertFalse(combined["accepted"])
        self.assertIn("polish_append_instead_of_replace", _codes(combined))
        self.assertEqual(
            ["polish"],
            combined["metrics"]["equivalent_prior_stages"],
        )
        self.assertEqual(
            [polish["artifact_sha256"]],
            combined["metrics"]["equivalent_prior_artifact_sha256s"],
        )
        self.assertEqual(
            polish["artifact_sha256"],
            combined["metrics"]["canonicalization_transition"][
                "before_artifact_sha256"
            ],
        )
        self.assertEqual(
            final["artifact_sha256"],
            combined["metrics"]["canonicalization_transition"][
                "after_artifact_sha256"
            ],
        )

    def test_different_sha_does_not_inherit_without_explicit_canonicalization(self) -> None:
        source = "The crew crossed the gate under pressure. " * 4
        before = source + "\n\n" + (
            "A rewritten version follows with different wording. " * 4
        )
        polish = self.gate.evaluate(
            artifact_text=before,
            source_text=source,
            stage="polish",
        )
        canonicalized = before.rstrip() + "\n"
        final = self.gate.evaluate(
            artifact_text=canonicalized,
            stage="final_gate",
        )

        combined = merge_integrity_reports_for_artifact(final, [polish])

        self.assertNotEqual(
            polish["artifact_sha256"],
            final["artifact_sha256"],
        )
        self.assertTrue(combined["accepted"])
        self.assertNotIn("polish_append_instead_of_replace", _codes(combined))
        self.assertNotIn("equivalent_prior_stages", combined["metrics"])

    def test_canonicalization_merge_rejects_unproved_transition(self) -> None:
        before = "Only one artifact version remains."
        canonicalized = before + "\n"
        final = self.gate.evaluate(
            artifact_text=canonicalized,
            stage="final_gate",
        )

        with self.assertRaisesRegex(ValueError, "before_artifact_sha256"):
            merge_integrity_reports_for_canonicalized_artifact(
                final,
                [],
                before_artifact_text=before,
                before_artifact_sha256="0" * 64,
                canonicalized_artifact_text=canonicalized,
            )

    def test_validation_and_quality_decision_include_integrity_problem_code(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 1})
        base_validation = validate_chapter(
            snapshot,
            "danger conflict " + ("scene prose " * 80),
            {"validation_focus": ["logic"]},
        )
        source = "source chapter " * 30
        report = self.gate.evaluate(
            artifact_text=source + "\n\n" + ("revised chapter " * 30),
            source_text=source,
            stage="polish",
        )
        validation = merge_integrity_report_into_validation(base_validation, report)
        decision = build_quality_decision(policy="minimal", validation=validation)
        codes = {
            code
            for finding in decision["findings"]
            for code in finding["codes"]
        }
        self.assertFalse(validation["ok"])
        self.assertFalse(decision["accepted"])
        self.assertIn("polish_append_instead_of_replace", codes)
        self.assertIn("final_artifact_integrity", decision["validation_coverage"]["executed_checks"])

    def test_config_rejects_unsafe_values(self) -> None:
        with self.assertRaises(ValueError):
            FinalArtifactIntegrityConfig(near_sequence_threshold=1.5)
        with self.assertRaises(ValueError):
            FinalArtifactIntegrityConfig(append_length_ratio_threshold=1.0)


if __name__ == "__main__":
    unittest.main()
