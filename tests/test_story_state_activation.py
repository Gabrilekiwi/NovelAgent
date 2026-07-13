from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest
import uuid

from core.story_project.activation import (
    StoryStateActivationError,
    activate_story_state,
    build_story_state_calibration_report,
    evaluate_story_state_activation,
    validate_story_state_calibration_report,
)
from core.story_project.identity import ensure_project_identity, load_project_identity
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION


class StoryStateActivationTest(unittest.TestCase):
    def _root(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_story_state_activation" / f"{name}_{uuid.uuid4().hex}"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        return root

    @staticmethod
    def _now() -> datetime:
        return datetime(2026, 7, 13, tzinfo=timezone.utc)

    @staticmethod
    def _qualified_evidence() -> dict:
        return {
            "target_sample_count": 1,
            "format_variant_count": 2,
            "managed_round_trip_rate": 1.0,
            "required_field_exact_match_rate": 1.0,
            "authoritative_precision": 1.0,
            "supported_optional_recall": 0.95,
            "unsupported_structure_count": 3,
            "unsupported_structure_captured_count": 3,
            "consecutive_shadow_chapters": 10,
            "blocking_conflict_count": 0,
            "missing_provenance_fields": [],
        }

    def _report(self, book_id: str, **evidence_overrides) -> dict:
        evidence = {**self._qualified_evidence(), **evidence_overrides}
        return build_story_state_calibration_report(
            book_id=book_id,
            parser_version=SEMANTIC_PARSER_VERSION,
            semantic_schema_version=STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
            target_layout_profile_version="canonical-zh-1",
            evidence=evidence,
            generated_at=self._now().isoformat(),
        )

    @staticmethod
    def _semantic_state(**overrides) -> dict:
        state = {
            "schema_version": STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
            "parser_version": SEMANTIC_PARSER_VERSION,
            "layout_profile_version": "canonical-zh-1",
            "conflicts": [],
        }
        state.update(overrides)
        return state

    def test_calibration_thresholds_are_derived_and_target_sample_is_mandatory(self) -> None:
        report = self._report("book", target_sample_count=0, consecutive_shadow_chapters=3)

        self.assertFalse(report["strict_eligible"])
        self.assertEqual(
            ["target_book_redacted_sample_missing", "consecutive_shadow_chapters_below_10"],
            report["strict_blockers"],
        )

    def test_report_hash_and_derived_decision_are_tamper_evident(self) -> None:
        report = self._report("book")
        report["evidence"]["authoritative_precision"] = 0.5

        with self.assertRaisesRegex(StoryStateActivationError, "hash"):
            validate_story_state_calibration_report(report)

    def test_explicit_activation_pins_profiles_and_report_hash(self) -> None:
        root = self._root("activate")
        identity = ensure_project_identity(root, now=self._now)

        activated = activate_story_state(root, self._report(identity.book_id), now=self._now)

        self.assertEqual("strict", activated.story_state_mode)
        self.assertEqual(SEMANTIC_PARSER_VERSION, activated.activation["parser_version"])
        self.assertEqual("canonical-zh-1", activated.activation["layout_profile_version"])
        self.assertEqual(64, len(activated.activation["calibration_report_sha256"]))
        self.assertEqual(activated, load_project_identity(root))

    def test_unqualified_or_wrong_book_report_cannot_activate(self) -> None:
        root = self._root("blocked")
        identity = ensure_project_identity(root, now=self._now)

        with self.assertRaisesRegex(StoryStateActivationError, "strict_calibration_not_qualified"):
            activate_story_state(
                root,
                self._report(identity.book_id, supported_optional_recall=0.94),
                now=self._now,
            )
        with self.assertRaisesRegex(StoryStateActivationError, "identity_mismatch"):
            activate_story_state(root, self._report("another-book"), now=self._now)

    def test_strict_profile_drift_fails_closed_unless_explicitly_downgraded(self) -> None:
        root = self._root("drift")
        identity = ensure_project_identity(root, now=self._now)
        activated = activate_story_state(root, self._report(identity.book_id), now=self._now)
        drifted = self._semantic_state(parser_version="shadow-2.0")

        with self.assertRaisesRegex(StoryStateActivationError, "strict_profile_version_mismatch"):
            evaluate_story_state_activation(activated, drifted)

        status = evaluate_story_state_activation(
            activated,
            drifted,
            allow_shadow_downgrade=True,
        )
        self.assertEqual("shadow", status["effective_mode"])
        self.assertTrue(status["downgraded"])
        self.assertFalse(status["ready_for_next_step"])
        self.assertFalse(status["authoritative"])


if __name__ == "__main__":
    unittest.main()
