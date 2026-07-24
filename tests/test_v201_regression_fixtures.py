from __future__ import annotations

import json
from pathlib import Path
import unittest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "v2_0_1_stabilization"


def _load_case(case_id: str) -> dict:
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["cases"] if item["id"] == case_id)
    return json.loads((FIXTURE_ROOT / entry["path"]).read_text(encoding="utf-8"))


def _problem_codes(report: dict) -> set[str]:
    return {
        str(item.get("code"))
        for item in report.get("findings", [])
        if isinstance(item, dict) and item.get("code")
    }


class V201FixtureContractTests(unittest.TestCase):
    def test_manifest_and_case_expectations_are_complete(self) -> None:
        manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("2.0", manifest["baseline_version"])
        self.assertEqual(8, len(manifest["cases"]))
        self.assertEqual(8, len({item["id"] for item in manifest["cases"]}))
        for entry in manifest["cases"]:
            with self.subTest(case=entry["id"]):
                case = json.loads((FIXTURE_ROOT / entry["path"]).read_text(encoding="utf-8"))
                self.assertEqual(entry["id"], case["id"])
                self.assertEqual(entry["expected_accepted"], case["expected"]["accepted"])
                self.assertEqual(
                    set(entry["expected_problem_codes"]),
                    set(case["expected"]["problem_codes"]),
                )


class V20KnownGapRegressionTests(unittest.TestCase):
    def test_duplicate_rescue_scene_is_blocked(self) -> None:
        from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate

        case = _load_case("duplicate_rescue_scene")
        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text="\n\n".join(case["scene_texts"]),
            stage=case["stage"],
            scene_events=case["scene_events"],
        )
        self.assertFalse(report["accepted"])
        self.assertTrue(set(case["expected"]["problem_codes"]) <= _problem_codes(report))

    @unittest.expectedFailure
    def test_character_identity_and_relationship_drift_are_blocked(self) -> None:
        from core.state.authoritative import validate_authoritative_state_delta

        case = _load_case("character_identity_role_drift")
        report = validate_authoritative_state_delta(
            base_state=case["base_state"],
            state_delta=case["state_delta"],
            chapter_text=case["chapter_text"],
        )
        self.assertFalse(report["accepted"])
        self.assertTrue(set(case["expected"]["problem_codes"]) <= _problem_codes(report))

    @unittest.expectedFailure
    def test_numeric_counter_rollback_is_blocked(self) -> None:
        from core.state.authoritative import validate_authoritative_state_delta

        case = _load_case("numeric_counter_rollback")
        report = validate_authoritative_state_delta(
            base_state=case["base_state"],
            state_delta=case["state_delta"],
            chapter_text=case["chapter_text"],
        )
        self.assertFalse(report["accepted"])
        self.assertIn("numeric_counter_rollback", _problem_codes(report))

    @unittest.expectedFailure
    def test_roster_arithmetic_mismatch_is_blocked(self) -> None:
        from core.state.authoritative import validate_authoritative_state_delta

        case = _load_case("roster_count_mismatch")
        report = validate_authoritative_state_delta(
            base_state=case["base_state"],
            state_delta=case["state_delta"],
            chapter_text=case["chapter_text"],
        )
        self.assertFalse(report["accepted"])
        self.assertIn("roster_count_mismatch", _problem_codes(report))

    def test_repair_append_original_and_revision_is_blocked(self) -> None:
        from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate

        case = _load_case("repair_append_original_and_revision")
        output = case["output_template"].format(**case)
        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=output,
            stage=case["stage"],
            source_text=case["source_text"],
        )
        self.assertFalse(report["accepted"])
        self.assertIn("repair_append_instead_of_replace", _problem_codes(report))

    def test_polish_append_original_and_revision_is_blocked(self) -> None:
        from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate

        case = _load_case("polish_append_original_and_revision")
        output = case["output_template"].format(**case)
        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=output,
            stage=case["stage"],
            source_text=case["source_text"],
        )
        self.assertFalse(report["accepted"])
        self.assertIn("polish_append_instead_of_replace", _problem_codes(report))

    def test_stale_scene_evidence_is_blocked(self) -> None:
        from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate

        case = _load_case("stale_scene_evidence")
        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=case["final_text"],
            stage="final_gate",
            scene_drafts=case["scene_drafts"],
            scene_spans=case["scene_spans"],
        )
        self.assertFalse(report["accepted"])
        self.assertIn("stale_scene_evidence", _problem_codes(report))

    def test_legitimate_short_repetition_is_not_blocked(self) -> None:
        from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate

        case = _load_case("legitimate_repetition_not_blocked")
        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=case["artifact_text"],
            stage=case["stage"],
        )
        self.assertTrue(report["accepted"])
        self.assertFalse(any(item.get("blocking") for item in report["findings"]))


if __name__ == "__main__":
    unittest.main()
