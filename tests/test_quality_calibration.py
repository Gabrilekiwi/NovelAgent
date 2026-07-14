from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from core.quality.calibration import (
    ACCEPTANCE_CRITERIA,
    FIXTURE_SOURCE,
    FixtureIntegrityError,
    QualityCalibrationError,
    build_quality_calibration_report,
    calibrate_blocking_policy,
    detect_quality_sample,
    evaluate_holdout,
    fixture_sha256,
    load_quality_calibration_fixture,
    split_quality_calibration_samples,
    validate_quality_calibration_fixture,
)
from core.schema import validate_schema, validate_schema_keywords


FIXTURE_PATH = Path("tests/fixtures/quality_calibration/synthetic_acceptance_v1.json")
SCHEMA_PATH = Path("schemas/quality_calibration_report.schema.json")


class QualityCalibrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_quality_calibration_fixture(FIXTURE_PATH)
        self.calibration, self.holdout = split_quality_calibration_samples(self.fixture["samples"])

    def test_fixture_is_disclosed_synthetic_and_has_isolated_splits(self) -> None:
        self.assertEqual(FIXTURE_SOURCE, self.fixture["source"])
        self.assertGreaterEqual(len(self.fixture["samples"]), 60)
        self.assertEqual(40, len(self.calibration))
        self.assertEqual(24, len(self.holdout))
        self.assertTrue(all(sample["label_source"] == FIXTURE_SOURCE for sample in self.fixture["samples"]))

        calibration_ids = {sample["sample_id"] for sample in self.calibration}
        holdout_ids = {sample["sample_id"] for sample in self.holdout}
        self.assertFalse(calibration_ids & holdout_ids)

    def test_report_passes_schema_and_acceptance_thresholds(self) -> None:
        report = build_quality_calibration_report(self.fixture)

        self.assertIs(report, validate_schema(report, "quality_calibration_report.schema.json"))
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(
            report["holdout_metrics"]["blocking_precision"],
            ACCEPTANCE_CRITERIA["blocking_precision_min"],
        )
        self.assertGreaterEqual(
            report["holdout_metrics"]["critical_high_recall"],
            ACCEPTANCE_CRITERIA["critical_high_recall_min"],
        )
        self.assertLessEqual(
            report["holdout_metrics"]["clean_false_block_rate"],
            ACCEPTANCE_CRITERIA["clean_false_block_rate_max"],
        )

    def test_schema_uses_supported_runtime_keywords(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertIs(schema, validate_schema_keywords(schema, SCHEMA_PATH.name))

    def test_threshold_is_derived_only_from_calibration_set(self) -> None:
        policy = calibrate_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )

        self.assertEqual("calibration_set", policy["threshold_source"])
        self.assertEqual("high", policy["blocking_threshold"])
        self.assertEqual(
            sorted(sample["sample_id"] for sample in self.calibration),
            policy["calibration_sample_ids"],
        )
        self.assertFalse(
            {sample["sample_id"] for sample in self.holdout} & set(policy["calibration_sample_ids"])
        )

    def test_holdout_changes_cannot_change_threshold_generation(self) -> None:
        baseline = calibrate_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )
        changed = copy.deepcopy(self.fixture)
        for sample in changed["samples"]:
            if sample["split"] == "holdout_set":
                sample["input"]["signals"] = [
                    {
                        "rule_id": "adversarial_holdout_only",
                        "issue_type": "holdout_only",
                        "severity": "critical",
                        "evidence_verified": True,
                    }
                ]
        changed_calibration, _ = split_quality_calibration_samples(changed["samples"])

        after_holdout_change = calibrate_blocking_policy(
            changed_calibration,
            fixture_id=changed["fixture_id"],
        )

        self.assertEqual(baseline, after_holdout_change)

    def test_holdout_sample_is_rejected_during_threshold_calibration(self) -> None:
        contaminated = self.calibration + (self.holdout[0],)

        with self.assertRaisesRegex(QualityCalibrationError, "accepts only calibration_set"):
            calibrate_blocking_policy(contaminated, fixture_id=self.fixture["fixture_id"])

    def test_calibration_sample_is_rejected_during_holdout_evaluation(self) -> None:
        policy = calibrate_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )

        with self.assertRaisesRegex(QualityCalibrationError, "accepts only holdout_set"):
            evaluate_holdout(
                policy=policy,
                calibration_samples=self.calibration,
                holdout_samples=self.holdout + (self.calibration[0],),
            )

    def test_fixture_tampering_is_rejected(self) -> None:
        tampered = copy.deepcopy(self.fixture)
        tampered["samples"][0]["expected"]["issue_type"] = "silently_changed_label"

        with self.assertRaisesRegex(FixtureIntegrityError, "fixture_sha256 mismatch"):
            validate_quality_calibration_fixture(tampered)

        self.assertNotEqual(tampered["fixture_sha256"], fixture_sha256(tampered))

    def test_policy_tampering_is_rejected_even_with_a_recomputed_digest(self) -> None:
        policy = calibrate_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )
        forged = copy.deepcopy(policy)
        forged["blocking_threshold"] = "critical"
        forged["policy_sha256"] = _unsigned_sha256(forged, "policy_sha256")

        with self.assertRaisesRegex(QualityCalibrationError, "calibration-set-only derivation"):
            evaluate_holdout(
                policy=forged,
                calibration_samples=self.calibration,
                holdout_samples=self.holdout,
            )

    def test_self_reported_confidence_is_forbidden(self) -> None:
        poisoned = copy.deepcopy(self.calibration)
        poisoned[0]["input"]["signals"][0]["confidence"] = 0.999

        with self.assertRaisesRegex(QualityCalibrationError, "confidence is forbidden"):
            calibrate_blocking_policy(poisoned, fixture_id=self.fixture["fixture_id"])

    def test_male_and_female_reference_contradictions_are_both_detected(self) -> None:
        cases = [
            sample
            for sample in self.holdout
            if sample["expected"]["issue_type"] == "gender_contradiction"
        ]
        directions = {
            (sample["input"]["declared_gender"], sample["input"]["reference_gender"])
            for sample in cases
        }

        self.assertEqual({("male", "female"), ("female", "male")}, directions)
        for sample in cases:
            detected = detect_quality_sample(sample, blocking_threshold="high")
            self.assertTrue(detected["predicted_blocking"])
            self.assertIn("gender_contradiction", {issue["issue_type"] for issue in detected["issues"]})

    def test_report_is_fully_deterministic(self) -> None:
        first = build_quality_calibration_report(self.fixture)
        second = build_quality_calibration_report(copy.deepcopy(self.fixture))

        self.assertEqual(first, second)
        self.assertEqual(first["report_sha256"], _unsigned_sha256(first, "report_sha256"))


def _unsigned_sha256(value: dict, digest_field: str) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop(digest_field, None)
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
