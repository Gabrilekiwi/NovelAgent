from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from core.quality.calibration import (
    ACCEPTANCE_CRITERIA,
    FIXTURE_SOURCE,
    RAW_FIXTURE_SOURCE,
    RAW_LABEL_SOURCE,
    FixtureIntegrityError,
    QualityCalibrationError,
    build_quality_calibration_report,
    build_raw_quality_calibration_report,
    calibrate_blocking_policy,
    calibrate_raw_blocking_policy,
    detect_quality_sample,
    detect_raw_quality_sample,
    evaluate_holdout,
    evaluate_raw_holdout,
    fixture_sha256,
    load_quality_calibration_fixture,
    load_raw_quality_calibration_fixture,
    split_quality_calibration_samples,
    validate_quality_calibration_fixture,
    validate_raw_quality_calibration_fixture,
)
from core.schema import validate_schema, validate_schema_keywords


FIXTURE_PATH = Path("tests/fixtures/quality_calibration/synthetic_acceptance_v1.json")
RAW_FIXTURE_PATH = Path("tests/fixtures/quality_calibration/synthetic_raw_production_v1.json")
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


class RawProductionQualityCalibrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_raw_quality_calibration_fixture(RAW_FIXTURE_PATH)
        self.calibration, self.holdout = split_quality_calibration_samples(
            self.fixture["samples"]
        )

    def test_frozen_recipe_expands_to_raw_inputs_with_isolated_40_24_split(self) -> None:
        self.assertEqual(RAW_FIXTURE_SOURCE, self.fixture["source"])
        self.assertEqual(64, len(self.fixture["samples"]))
        self.assertEqual(40, len(self.calibration))
        self.assertEqual(24, len(self.holdout))
        self.assertTrue(
            all(sample["label_source"] == RAW_LABEL_SOURCE for sample in self.fixture["samples"])
        )
        self.assertFalse(
            {sample["sample_id"] for sample in self.calibration}
            & {sample["sample_id"] for sample in self.holdout}
        )
        for sample in self.fixture["samples"]:
            self.assertEqual(
                {"snapshot", "chapter_text", "decision"},
                set(sample["input"]),
            )
            serialized_input = json.dumps(sample["input"], ensure_ascii=False)
            self.assertNotIn('"expected"', serialized_input)
            self.assertNotIn('"severity"', serialized_input)
            self.assertNotIn('"blocking"', serialized_input)
            self.assertNotIn('"signals"', serialized_input)

    def test_calibration_and_holdout_use_independent_frozen_template_prose(self) -> None:
        raw = json.loads(RAW_FIXTURE_PATH.read_text(encoding="utf-8"))
        templates = raw["recipe"]["templates"]
        groups = raw["recipe"]["groups"]
        calibration_template_ids = {
            group["template_id"]
            for group in groups
            if group["split"] == "calibration_set"
        }
        holdout_template_ids = {
            group["template_id"]
            for group in groups
            if group["split"] == "holdout_set"
        }

        self.assertFalse(calibration_template_ids & holdout_template_ids)
        self.assertFalse(
            {templates[template_id] for template_id in calibration_template_ids}
            & {templates[template_id] for template_id in holdout_template_ids}
        )

    def test_raw_report_uses_production_path_and_passes_original_holdout_metrics(self) -> None:
        report = build_raw_quality_calibration_report(self.fixture)

        self.assertTrue(report["passed"])
        self.assertEqual("high", report["policy"]["blocking_threshold"])
        self.assertEqual("calibration_set", report["policy"]["threshold_source"])
        self.assertEqual(1.0, report["holdout_metrics"]["blocking_precision"])
        self.assertEqual(1.0, report["holdout_metrics"]["critical_high_recall"])
        self.assertEqual(0.0, report["holdout_metrics"]["clean_false_block_rate"])
        self.assertTrue(report["gender_contradiction"]["passed"])
        self.assertEqual(8, len(report["gender_contradiction"]["case_ids"]))

        conflict = next(
            sample
            for sample in self.holdout
            if sample["expected"]["issue_type"] == "character_voice_gender_conflict"
        )
        prediction = detect_raw_quality_sample(conflict, blocking_threshold="high")
        self.assertTrue(prediction["predicted_blocking"])
        self.assertEqual("validate_chapter", prediction["production_path"]["validator"])
        self.assertEqual(
            "build_quality_decision",
            prediction["production_path"]["quality_decision"],
        )
        self.assertFalse(prediction["production_path"]["validation_ok"])
        self.assertFalse(prediction["production_path"]["quality_decision_accepted"])

    def test_holdout_labels_cannot_tune_raw_threshold(self) -> None:
        baseline = calibrate_raw_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )
        changed_holdout = copy.deepcopy(self.holdout)
        for sample in changed_holdout:
            sample["expected"]["issue_type"] = "holdout_label_changed_after_freeze"
            sample["expected"]["direction"] = "none"
        after_holdout_change = calibrate_raw_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )

        self.assertEqual(baseline, after_holdout_change)
        with self.assertRaisesRegex(QualityCalibrationError, "accepts only calibration_set"):
            calibrate_raw_blocking_policy(
                self.calibration + tuple(changed_holdout[:1]),
                fixture_id=self.fixture["fixture_id"],
            )

    def test_detector_does_not_read_expected_label(self) -> None:
        sample = copy.deepcopy(self.holdout[0])
        baseline = detect_raw_quality_sample(sample, blocking_threshold="high")
        sample["expected"]["issue_type"] = "independent_label_changed"
        sample["expected"]["direction"] = "none"
        after_label_change = detect_raw_quality_sample(sample, blocking_threshold="high")

        self.assertEqual(baseline, after_label_change)

    def test_quality_decision_acceptance_is_a_required_blocking_gate(self) -> None:
        conflict = next(
            sample
            for sample in self.holdout
            if sample["expected"]["issue_type"] == "character_voice_gender_conflict"
        )
        baseline = detect_raw_quality_sample(conflict, blocking_threshold="high")
        self.assertTrue(baseline["predicted_blocking"])

        forced_acceptance = {
            "accepted": True,
            "decision_digest": "0" * 64,
        }
        with patch(
            "core.quality.calibration.build_quality_decision",
            return_value=forced_acceptance,
        ):
            gated = detect_raw_quality_sample(conflict, blocking_threshold="high")

        self.assertEqual("critical", gated["predicted_severity"])
        self.assertFalse(gated["predicted_blocking"])
        self.assertTrue(gated["production_path"]["quality_decision_accepted"])

    def test_raw_holdout_rejects_calibration_contamination(self) -> None:
        policy = calibrate_raw_blocking_policy(
            self.calibration,
            fixture_id=self.fixture["fixture_id"],
        )
        with self.assertRaisesRegex(QualityCalibrationError, "accepts only holdout_set"):
            evaluate_raw_holdout(
                policy=policy,
                calibration_samples=self.calibration,
                holdout_samples=self.holdout + (self.calibration[0],),
            )

    def test_raw_recipe_tampering_is_rejected(self) -> None:
        raw = json.loads(RAW_FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["recipe"]["templates"]["female_bound"] += "被篡改"
        with self.assertRaisesRegex(FixtureIntegrityError, "fixture_sha256 mismatch"):
            validate_raw_quality_calibration_fixture(raw)


def _unsigned_sha256(value: dict, digest_field: str) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop(digest_field, None)
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
