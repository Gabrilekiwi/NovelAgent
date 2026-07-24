from __future__ import annotations

import unittest

from core.engine.repair_convergence import (
    build_validation_checkpoint,
    compare_validation_checkpoints,
    validation_quality_score,
)


def _validation(*problems: dict, ok: bool = False) -> dict:
    return {"ok": ok, "problems": list(problems)}


def _problem(code: str, *, severity: str = "high", message: str | None = None) -> dict:
    return {
        "code": code,
        "message": message or code,
        "validator": "llm",
        "area": "complex_plot_logic",
        "severity": severity,
        "blocking": severity in {"critical", "high"},
    }


class RepairConvergenceTest(unittest.TestCase):
    def _checkpoint(self, *problems: dict, chapter: str = "draft", ok: bool = False) -> dict:
        return build_validation_checkpoint(
            _validation(*problems, ok=ok),
            chapter_text=chapter,
        )

    def test_problem_count_reduction_is_eligible_for_elastic_budget(self) -> None:
        before = self._checkpoint(_problem("a"), _problem("b"), _problem("c"), _problem("d"))
        after = self._checkpoint(_problem("a"), _problem("b"), _problem("c"), chapter="repaired")

        transition = compare_validation_checkpoints(before, after)

        self.assertEqual("improved", transition["status"])
        self.assertEqual("problem_count_reduced", transition["reason"])
        self.assertTrue(transition["eligible_for_elastic_budget"])

    def test_unchanged_problem_count_is_stalled_and_records_repeated_problem(self) -> None:
        before = self._checkpoint(_problem("a"), _problem("b"))
        after = self._checkpoint(
            _problem("a", message="The model reworded problem A."),
            _problem("b", message="The model reworded problem B."),
            chapter="rewritten",
        )

        transition = compare_validation_checkpoints(before, after)

        self.assertEqual("stalled", transition["status"])
        self.assertFalse(transition["eligible_for_elastic_budget"])
        self.assertEqual(2, len(transition["repeated_problem_fingerprints"]))

    def test_increased_problem_count_is_regression(self) -> None:
        before = self._checkpoint(_problem("a"), _problem("b"))
        after = self._checkpoint(_problem("a"), _problem("b"), _problem("c"), chapter="worse")

        transition = compare_validation_checkpoints(before, after)

        self.assertEqual("regressed", transition["status"])
        self.assertEqual("problem_count_increased", transition["reason"])

    def test_new_critical_problem_blocks_false_count_based_progress(self) -> None:
        before = self._checkpoint(_problem("a"), _problem("b"), _problem("c"))
        after = self._checkpoint(_problem("fatal", severity="critical"), chapter="dangerous")

        transition = compare_validation_checkpoints(before, after)

        self.assertEqual("regressed", transition["status"])
        self.assertEqual("new_critical_problem", transition["reason"])
        self.assertFalse(transition["eligible_for_elastic_budget"])
        self.assertLess(validation_quality_score(before), validation_quality_score(after))

    def test_passing_validation_finishes_without_requesting_more_budget(self) -> None:
        before = self._checkpoint(_problem("a"))
        after = self._checkpoint(chapter="fixed", ok=True)

        transition = compare_validation_checkpoints(before, after)

        self.assertEqual("passed", transition["status"])
        self.assertFalse(transition["eligible_for_elastic_budget"])


if __name__ == "__main__":
    unittest.main()
