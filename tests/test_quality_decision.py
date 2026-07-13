from __future__ import annotations

import copy
import unittest

from core.quality_decision import (
    build_quality_decision,
    quality_decision_review_status,
    resolve_quality_policy,
)
from core.schema import validate_schema


def _validation(*problems: dict, executed: list[str] | None = None) -> dict:
    return {
        "ok": not any(problem.get("blocking") for problem in problems),
        "requested_focus": ["continuity", "logic"],
        "executed_checks": executed or ["continuity", "logic"],
        "skipped_checks": ["spatial"],
        "problems": list(problems),
    }


def _problem(
    code: str,
    *,
    message: str = "problem wording",
    blocking: bool = True,
) -> dict:
    return {
        "code": code,
        "message": message,
        "validator": "continuity",
        "blocking": blocking,
        "category": "blocking" if blocking else "warning",
        "repair_action": "insert_opening_bridge" if "opening" in code else "manual_review",
        "repair_parameters": {"bridge": "已知承接句"} if "opening" in code else {},
        "evidence": [{"kind": "fixture", "value": "same fact"}],
    }


class QualityDecisionTest(unittest.TestCase):
    def test_finding_identity_ignores_message_and_merges_all_producer_evidence(self) -> None:
        quality_report = {
            "checks": [
                {
                    "code": "opening_continuity",
                    "status": "warning",
                    "severity": "medium",
                    "message": "review wording",
                    "evidence": {"opening": "same fact"},
                }
            ]
        }
        first = build_quality_decision(
            policy="standard",
            validation=_validation(_problem("missing_opening_bridge", message="first wording")),
            chapter_quality_report=quality_report,
            rule_validation_report={"violations": []},
            chapter_index=4,
        )
        changed_validation = _validation(_problem("missing_opening_bridge", message="completely different wording"))
        changed_report = copy.deepcopy(quality_report)
        changed_report["checks"][0]["message"] = "another review wording"
        second = build_quality_decision(
            policy="standard",
            validation=changed_validation,
            chapter_quality_report=changed_report,
            rule_validation_report={"violations": []},
            chapter_index=4,
        )

        self.assertEqual(first["finding_ids"], second["finding_ids"])
        self.assertEqual(1, len(first["findings"]))
        self.assertEqual(
            {"base_validation", "deterministic_review"},
            {item["producer"] for item in first["findings"][0]["producer_evidence"]},
        )
        self.assertFalse(first["accepted"])

    def test_policy_thresholds_and_strict_llm_fail_closed(self) -> None:
        warning = _validation(_problem("soft_warning", blocking=False))
        self.assertTrue(build_quality_decision(policy="minimal", validation=warning)["accepted"])
        self.assertTrue(
            build_quality_decision(
                policy="standard",
                validation=warning,
                chapter_quality_report={"checks": []},
                rule_validation_report={"violations": []},
            )["accepted"]
        )

        strict = build_quality_decision(policy="strict", validation=warning)
        self.assertFalse(strict["accepted"])
        self.assertIn("llm_validator", strict["producers"])
        self.assertFalse(strict["llm_validator"]["available"])

        llm_validation = _validation(executed=["continuity", "logic", "llm"])
        llm_validation["checks"] = [
            {
                "name": "llm",
                "metadata": {
                    "provider": "openai",
                    "model": "test-model",
                    "prompt_hash": "a" * 64,
                    "attempt_history": [{"attempt": 1, "status": "succeeded"}],
                },
            }
        ]
        strict_with_llm = build_quality_decision(
            policy="strict",
            validation=llm_validation,
            chapter_quality_report={"checks": []},
            rule_validation_report={"violations": []},
        )
        self.assertTrue(strict_with_llm["accepted"])
        self.assertEqual("test-model", strict_with_llm["llm_validator"]["model"])

    def test_minimal_ignores_review_while_standard_rejects_needs_revision(self) -> None:
        review_report = {
            "checks": [
                {
                    "code": "chapter_length_reasonable",
                    "status": "fail",
                    "severity": "high",
                    "message": "too short",
                    "evidence": {"measured": 120},
                }
            ]
        }
        minimal = build_quality_decision(
            policy="minimal",
            validation=_validation(),
            chapter_quality_report=review_report,
        )
        standard = build_quality_decision(
            policy="standard",
            validation=_validation(),
            chapter_quality_report=review_report,
            rule_validation_report={"violations": []},
        )

        self.assertTrue(minimal["accepted"])
        self.assertFalse(standard["accepted"])
        self.assertEqual("needs_revision", standard["max_severity"])
        self.assertEqual("needs_revision", quality_decision_review_status(standard))

    def test_saved_evidence_replays_to_identical_decision(self) -> None:
        inputs = {
            "policy": resolve_quality_policy("standard"),
            "validation": _validation(_problem("missing_opening_bridge")),
            "chapter_index": 7,
        }
        first = build_quality_decision(**inputs)
        second = build_quality_decision(**copy.deepcopy(inputs))

        self.assertEqual(first, second)
        self.assertIs(first, validate_schema(first, "quality_decision.schema.json"))

    def test_zero_finding_review_still_proves_required_producers_executed(self) -> None:
        review_decision = build_quality_decision(
            policy="standard",
            chapter_quality_report={"checks": []},
            rule_validation_report={"violations": []},
        )
        final = build_quality_decision(
            policy="standard",
            validation=_validation(),
            upstream_decisions=[review_decision],
        )

        self.assertTrue(review_decision["accepted"])
        self.assertEqual(
            {"deterministic_review", "narrative_rules"},
            set(review_decision["producers"]),
        )
        self.assertTrue(final["accepted"])
        self.assertEqual([], final["findings"])


if __name__ == "__main__":
    unittest.main()
