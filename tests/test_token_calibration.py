from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from core.context_budget import (
    DEFAULT_CALIBRATED_ESTIMATOR,
    ESTIMATOR_CALIBRATION_MANIFEST_HASH,
    ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE,
    ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
    ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE,
    ESTIMATOR_HOLDOUT_MANIFEST_HASH,
    conservative_calibrated_token_estimate,
)
from core.schema import validate_schema_keywords
from core.token_calibration import (
    TOKEN_CALIBRATION_SOURCE_SYNTHETIC,
    TokenCalibrationError,
    build_token_calibration_report,
    fit_token_estimator,
    load_token_calibration_fixture,
)


FIXTURE = Path("tests/fixtures/token_calibration/synthetic_acceptance_v1.json")


class TokenCalibrationTest(unittest.TestCase):
    def _fixture(self):
        return load_token_calibration_fixture(str(FIXTURE))

    def test_fit_uses_calibration_and_error_uses_holdout_only(self) -> None:
        calibration, holdout, source = self._fixture()
        estimator = fit_token_estimator(calibration, version="synthetic-token-calibration-v1")
        report = build_token_calibration_report(
            estimator=estimator,
            calibration_samples=calibration,
            holdout_samples=holdout,
            dataset_source=source,
        )

        self.assertEqual(TOKEN_CALIBRATION_SOURCE_SYNTHETIC, report["dataset_source"])
        self.assertEqual("holdout", report["split"]["error_evaluated_on"])
        self.assertTrue(report["split"]["sample_ids_disjoint"])
        self.assertTrue(report["split"]["content_fingerprints_disjoint"])
        self.assertEqual(
            {sample["id"] for sample in holdout},
            {sample["id"] for sample in report["holdout_results"]},
        )
        self.assertFalse({sample["id"] for sample in calibration} & {sample["id"] for sample in report["holdout_results"]})
        self.assertEqual(["official", "openai_compatible", "unknown"], report["coverage"]["endpoint_types"])
        self.assertEqual(["en", "mixed", "zh"], report["coverage"]["language_profiles"])

    def test_calibration_only_fit_is_the_installed_production_estimator(self) -> None:
        calibration, holdout, source = self._fixture()
        fitted = fit_token_estimator(
            calibration,
            version=DEFAULT_CALIBRATED_ESTIMATOR.version,
        )
        report = build_token_calibration_report(
            estimator=fitted,
            calibration_samples=calibration,
            holdout_samples=holdout,
            dataset_source=source,
        )

        self.assertEqual(DEFAULT_CALIBRATED_ESTIMATOR, fitted)
        self.assertEqual(0.0, report["metrics"]["maximum_underestimate_ratio"])
        self.assertEqual("holdout", report["split"]["error_evaluated_on"])
        self.assertEqual(
            "synthetic_acceptance_v1",
            report["dataset_source"],
        )
        self.assertEqual(
            ESTIMATOR_CALIBRATION_MANIFEST_HASH,
            report["split"]["calibration_manifest_hash"],
        )
        self.assertEqual(
            ESTIMATOR_HOLDOUT_MANIFEST_HASH,
            report["split"]["holdout_manifest_hash"],
        )

    def test_holdout_changes_cannot_tune_installed_enforcement_profile(self) -> None:
        calibration, holdout, source = self._fixture()
        baseline = build_token_calibration_report(
            estimator=fit_token_estimator(
                calibration,
                version=DEFAULT_CALIBRATED_ESTIMATOR.version,
            ),
            calibration_samples=calibration,
            holdout_samples=holdout,
            dataset_source=source,
        )
        changed_holdout = copy.deepcopy(holdout)
        for sample in changed_holdout:
            sample["actual_tokens"] *= 10
        changed = build_token_calibration_report(
            estimator=fit_token_estimator(
                calibration,
                version=DEFAULT_CALIBRATED_ESTIMATOR.version,
            ),
            calibration_samples=calibration,
            holdout_samples=changed_holdout,
            dataset_source=source,
        )

        self.assertEqual(
            baseline["split"]["calibration_manifest_hash"],
            changed["split"]["calibration_manifest_hash"],
        )
        self.assertNotEqual(
            baseline["split"]["holdout_manifest_hash"],
            changed["split"]["holdout_manifest_hash"],
        )
        self.assertEqual(0.0, ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE)
        self.assertEqual(
            1.0,
            ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE,
        )
        self.assertEqual(64, ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS)
        payload = "字" * 3_000
        self.assertEqual(
            4_089,
            conservative_calibrated_token_estimate(payload),
        )
        self.assertLess(
            conservative_calibrated_token_estimate(payload),
            len(payload.encode("utf-8")) + 64,
        )

    def test_compatible_and_unknown_references_are_never_labelled_exact(self) -> None:
        calibration, holdout, source = self._fixture()
        report = build_token_calibration_report(
            estimator=fit_token_estimator(calibration, version="synthetic-token-calibration-v1"),
            calibration_samples=calibration,
            holdout_samples=holdout,
            dataset_source=source,
        )

        for result in report["holdout_results"]:
            if result["endpoint_type"] != "official":
                self.assertEqual("calibration_reference", result["reference_count_mode"])
        official = [item for item in report["holdout_results"] if item["endpoint_type"] == "official"]
        self.assertTrue(official)
        self.assertTrue(all(item["reference_count_mode"] == "provider_exact" for item in official))

    def test_fit_rejects_holdout_samples(self) -> None:
        _, holdout, _ = self._fixture()
        with self.assertRaisesRegex(TokenCalibrationError, "holdout"):
            fit_token_estimator(holdout, version="bad")

    def test_report_rejects_id_or_content_leakage_between_splits(self) -> None:
        calibration, holdout, source = self._fixture()
        estimator = fit_token_estimator(calibration, version="synthetic-token-calibration-v1")

        same_id = copy.deepcopy(holdout)
        same_id[0]["id"] = calibration[0]["id"]
        with self.assertRaisesRegex(TokenCalibrationError, "ids overlap"):
            build_token_calibration_report(
                estimator=estimator,
                calibration_samples=calibration,
                holdout_samples=same_id,
                dataset_source=source,
            )

        same_content = copy.deepcopy(holdout)
        for field in ("text", "actual_tokens", "provider", "model", "endpoint_type"):
            same_content[0][field] = calibration[0][field]
        with self.assertRaisesRegex(TokenCalibrationError, "fingerprints overlap"):
            build_token_calibration_report(
                estimator=estimator,
                calibration_samples=calibration,
                holdout_samples=same_content,
                dataset_source=source,
            )

    def test_report_requires_endpoint_and_language_holdout_coverage(self) -> None:
        calibration, holdout, source = self._fixture()
        estimator = fit_token_estimator(calibration, version="synthetic-token-calibration-v1")
        incomplete = [sample for sample in holdout if sample["endpoint_type"] == "official"]

        with self.assertRaisesRegex(TokenCalibrationError, "coverage incomplete"):
            build_token_calibration_report(
                estimator=estimator,
                calibration_samples=calibration,
                holdout_samples=incomplete,
                dataset_source=source,
            )

    def test_schema_uses_supported_runtime_keywords(self) -> None:
        schema_path = Path("schemas/token_calibration_report.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIs(schema, validate_schema_keywords(schema, schema_path.name))


if __name__ == "__main__":
    unittest.main()
