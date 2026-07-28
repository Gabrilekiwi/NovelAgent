from __future__ import annotations

import copy
import unittest

from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate
from core.quality.repair_patch import (
    RepairPatchError,
    apply_repair_patch,
    build_repair_patch_from_texts,
    coerce_repair_patch,
    validate_repair_patch,
)
from core.quality.scene_evidence import (
    SceneEvidenceAlignmentError,
    realign_scene_evidence,
)


def _pipeline() -> dict:
    first = "Alpha enters the corridor."
    second = "Beta closes the damaged gate."
    merged = f"{first}\n\n{second}"
    return {
        "merged_chapter": merged,
        "scene_drafts": [
            {"index": 1, "goal": "enter", "text": first},
            {"index": 2, "goal": "close gate", "text": second},
        ],
        "scene_spans": [
            {"index": 1, "start_char": 0, "end_char": len(first), "chars": len(first)},
            {
                "index": 2,
                "start_char": len(first) + 2,
                "end_char": len(merged),
                "chars": len(second),
            },
        ],
    }


class RepairPatchTests(unittest.TestCase):
    def test_patch_binds_base_ranges_and_applied_output_hash(self) -> None:
        base = "The team waits. The gate is open."
        repaired = "The team advances. The gate is closed."
        patch = build_repair_patch_from_texts(
            base,
            repaired,
            problem_codes=["missing_conflict_marker"],
        )

        output, audit = apply_repair_patch(base, patch)

        self.assertEqual(repaired, output)
        self.assertEqual(patch["patch_sha256"], audit["patch_sha256"])
        self.assertGreater(audit["operation_count"], 0)
        self.assertEqual(len(base), audit["base_chars"])
        self.assertEqual(len(repaired), audit["output_chars"])

    def test_patch_rejects_wrong_base_and_tampered_range_hash(self) -> None:
        patch = build_repair_patch_from_texts("alpha", "omega")

        with self.assertRaisesRegex(RepairPatchError, "repair_patch_base_hash_mismatch"):
            validate_repair_patch(patch, base_chapter="other")

        tampered = copy.deepcopy(patch)
        tampered["operations"][0]["expected_text_sha256"] = "0" * 64
        with self.assertRaisesRegex(RepairPatchError, "repair_patch_hash_mismatch"):
            validate_repair_patch(tampered, base_chapter="alpha")

    def test_model_patch_proposal_gets_deterministic_output_and_patch_hashes(self) -> None:
        generated = build_repair_patch_from_texts("alpha", "omega")
        proposal = copy.deepcopy(generated)
        proposal.pop("output_chapter_sha256")
        proposal.pop("patch_sha256")

        finalized = coerce_repair_patch("alpha", proposal)
        output, _audit = apply_repair_patch("alpha", finalized)

        self.assertEqual("omega", output)
        self.assertEqual(64, len(finalized["output_chapter_sha256"]))
        self.assertEqual(64, len(finalized["patch_sha256"]))


class SceneEvidenceAlignmentTests(unittest.TestCase):
    def test_scene_local_patch_rebuilds_drafts_spans_and_hash_history(self) -> None:
        pipeline = _pipeline()
        before = pipeline["merged_chapter"]
        after = before.replace("corridor", "service corridor").replace("damaged", "sealed")
        patch = build_repair_patch_from_texts(before, after, mode="stage_text_diff")

        updated = realign_scene_evidence(
            pipeline,
            before_chapter=before,
            after_chapter=after,
            patch=patch,
            stage="repair",
        )

        self.assertEqual(after, updated["merged_chapter"])
        self.assertIn("service corridor", updated["scene_drafts"][0]["text"])
        self.assertIn("sealed", updated["scene_drafts"][1]["text"])
        self.assertGreater(
            updated["scene_spans"][1]["start_char"],
            pipeline["scene_spans"][1]["start_char"],
        )
        self.assertEqual([1, 2], updated["scene_evidence_history"][0]["modified_scene_indexes"])
        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=after,
            stage="final_gate",
            scene_drafts=updated["scene_drafts"],
            scene_spans=updated["scene_spans"],
        )
        self.assertTrue(report["accepted"])

    def test_cross_scene_rewrite_is_rejected_instead_of_guessing_alignment(self) -> None:
        pipeline = _pipeline()
        before = pipeline["merged_chapter"]
        after = "A completely different single-scene chapter."
        patch = build_repair_patch_from_texts(before, after, mode="stage_text_diff")

        with self.assertRaisesRegex(SceneEvidenceAlignmentError, "stale_scene_evidence"):
            realign_scene_evidence(
                pipeline,
                before_chapter=before,
                after_chapter=after,
                patch=patch,
                stage="repair",
            )

    def test_old_scene_evidence_remains_blocking_after_text_changes(self) -> None:
        pipeline = _pipeline()
        changed = pipeline["merged_chapter"].replace("damaged", "sealed")

        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=changed,
            stage="final_gate",
            scene_drafts=pipeline["scene_drafts"],
            scene_spans=pipeline["scene_spans"],
        )

        self.assertFalse(report["accepted"])
        self.assertIn(
            "stale_scene_evidence",
            {item["code"] for item in report["findings"]},
        )

    def test_scene_evidence_must_cover_the_complete_artifact(self) -> None:
        pipeline = _pipeline()
        changed = pipeline["merged_chapter"] + " Fixed."

        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=changed,
            stage="final_gate",
            scene_drafts=pipeline["scene_drafts"],
            scene_spans=pipeline["scene_spans"],
        )

        self.assertFalse(report["accepted"])
        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "stale_scene_evidence"
        )
        reasons = {
            item["reason"]
            for item in finding["evidence"]["problems"]
        }
        self.assertIn("scene_coverage_does_not_reach_artifact_end", reasons)
        self.assertIn("scene_reconstruction_mismatch", reasons)

    def test_scene_evidence_rejects_untracked_prefix_and_separator_gap(self) -> None:
        pipeline = _pipeline()
        changed = "Prefix.\n\n" + pipeline["merged_chapter"]
        shifted = copy.deepcopy(pipeline["scene_spans"])
        for span in shifted:
            span["start_char"] += len("Prefix.\n\n")
            span["end_char"] += len("Prefix.\n\n")

        report = FinalArtifactIntegrityGate().evaluate(
            artifact_text=changed,
            stage="final_gate",
            scene_drafts=pipeline["scene_drafts"],
            scene_spans=shifted,
        )

        self.assertFalse(report["accepted"])
        self.assertIn(
            "stale_scene_evidence",
            {item["code"] for item in report["findings"]},
        )
