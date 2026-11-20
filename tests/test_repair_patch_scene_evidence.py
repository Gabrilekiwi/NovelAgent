from __future__ import annotations

import copy
import hashlib
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
    canonicalize_scene_local_repair_patch,
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


def _duplicate_tail_pipeline() -> dict:
    first = "Alpha enters the corridor."
    second = "Beta spots the marker.\n\nThe damaged gate is open."
    third = "Gamma seals the route."
    merged = "\n\n".join((first, second, third))
    drafts = [
        {"index": 1, "text": first},
        {"index": 2, "text": second},
        {"index": 3, "text": third},
    ]
    cursor = 0
    spans = []
    for draft in drafts:
        text = draft["text"]
        spans.append(
            {"index": draft["index"], "start_char": cursor, "end_char": cursor + len(text), "chars": len(text)}
        )
        cursor += len(text) + 2
    return {"merged_chapter": merged, "scene_drafts": drafts, "scene_spans": spans}


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

    def test_separator_only_crossing_deletion_is_canonicalized_to_scene_local_patch(self) -> None:
        pipeline = _duplicate_tail_pipeline()
        before = pipeline["merged_chapter"]
        second = pipeline["scene_drafts"][1]["text"]
        third = pipeline["scene_drafts"][2]["text"]
        duplicate_tail = "\n\nThe damaged gate is open."
        start = pipeline["scene_spans"][1]["start_char"] + second.rfind(duplicate_tail)
        end = pipeline["scene_spans"][2]["start_char"]
        raw_patch = coerce_repair_patch(
            before,
            {
                "base_chapter_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
                "mode": "stage_text_diff",
                "operations": [
                    {
                        "start_char": start,
                        "end_char": end,
                        "expected_text_sha256": hashlib.sha256(
                            before[start:end].encode("utf-8")
                        ).hexdigest(),
                        "replacement": "",
                        "problem_codes": ["duplicate_scene_text"],
                    }
                ],
            },
        )
        raw_after, _ = apply_repair_patch(before, raw_patch)

        canonical_patch, audit = canonicalize_scene_local_repair_patch(
            pipeline,
            before_chapter=before,
            patch=raw_patch,
        )
        canonical_after, _ = apply_repair_patch(before, canonical_patch)
        updated = realign_scene_evidence(
            pipeline,
            before_chapter=before,
            after_chapter=canonical_after,
            patch=canonical_patch,
            stage="repair",
        )

        self.assertIsNotNone(audit)
        self.assertEqual("scene_separator_preservation_v1", audit["policy"])
        self.assertEqual("\n\n".join((pipeline["scene_drafts"][0]["text"], second.removesuffix(duplicate_tail), third)), canonical_after)
        self.assertEqual(canonical_after, updated["merged_chapter"])
        self.assertNotEqual(raw_after, canonical_after)

    def test_actual_cross_scene_rewrite_is_not_canonicalized(self) -> None:
        pipeline = _pipeline()
        before = pipeline["merged_chapter"]
        raw_patch = build_repair_patch_from_texts(
            before,
            "A completely different single-scene chapter.",
            mode="stage_text_diff",
        )

        unchanged_patch, audit = canonicalize_scene_local_repair_patch(
            pipeline,
            before_chapter=before,
            patch=raw_patch,
        )

        self.assertIsNone(audit)
        self.assertEqual(raw_patch["patch_sha256"], unchanged_patch["patch_sha256"])

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
