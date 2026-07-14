from __future__ import annotations

import unittest

from core.review.gate import evaluate_review_gate
from core.schema import validate_schema


class ReviewGateTests(unittest.TestCase):
    def test_off_gate_disabled(self) -> None:
        result = evaluate_review_gate(review_pipeline={"status": "blocked"}, threshold="off")

        self.assertFalse(result["enabled"])
        self.assertEqual("disabled", result["status"])
        self.assertFalse(result["matched"])
        self.assertEqual(0, result["exit_code"])
        validate_schema(result, "review_gate_result.schema.json")

    def test_blocked_threshold(self) -> None:
        self.assert_gate("blocked", "pass", 0)
        self.assert_gate("blocked", "warning", 0)
        self.assert_gate("blocked", "needs_revision", 0)
        self.assert_gate("blocked", "blocked", 1)
        self.assert_gate("blocked", "error", 1)

    def test_needs_revision_threshold(self) -> None:
        self.assert_gate("needs_revision", "pass", 0)
        self.assert_gate("needs_revision", "warning", 0)
        self.assert_gate("needs_revision", "needs_revision", 1)
        self.assert_gate("needs_revision", "blocked", 1)
        self.assert_gate("needs_revision", "error", 1)

    def test_warning_threshold(self) -> None:
        self.assert_gate("warning", "pass", 0)
        self.assert_gate("warning", "warning", 0)
        self.assert_gate("warning", "needs_revision", 1)
        self.assert_gate("warning", "blocked", 1)
        self.assert_gate("warning", "error", 1)

    def test_quality_decision_cannot_override_raw_review_gate(self) -> None:
        accepted_review = evaluate_review_gate(
            review_pipeline={"status": "pass"},
            quality_decision={"max_severity": "blocking", "accepted": False},
            threshold="blocked",
        )
        blocked_review = evaluate_review_gate(
            review_pipeline={"status": "blocked"},
            quality_decision={"max_severity": "info", "accepted": True},
            threshold="blocked",
        )

        self.assertEqual("pass", accepted_review["status"])
        self.assertEqual("fail", blocked_review["status"])

    def test_missing_review_pipeline_with_gate_enabled_fails(self) -> None:
        result = evaluate_review_gate(review_pipeline=None, threshold="blocked")

        self.assertEqual("error", result["status"])
        self.assertTrue(result["matched"])
        self.assertEqual(1, result["exit_code"])
        validate_schema(result, "review_gate_result.schema.json")

    def test_invalid_review_status_fails_closed(self) -> None:
        result = evaluate_review_gate(review_pipeline={"status": "unknown"}, threshold="blocked")

        self.assertEqual("error", result["status"])
        self.assertEqual(1, result["exit_code"])
        validate_schema(result, "review_gate_result.schema.json")

    def assert_gate(self, threshold: str, review_status: str, exit_code: int) -> None:
        result = evaluate_review_gate(review_pipeline={"status": review_status}, threshold=threshold)
        self.assertEqual(exit_code, result["exit_code"])
        self.assertEqual(exit_code == 1, result["matched"])
        self.assertEqual("fail" if exit_code else "pass", result["status"])
        self.assertEqual(review_status, result["review_status"])
        validate_schema(result, "review_gate_result.schema.json")


if __name__ == "__main__":
    unittest.main()
