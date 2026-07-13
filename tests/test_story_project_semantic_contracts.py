from __future__ import annotations

import json
from pathlib import Path
import unittest

from core.schema import SchemaValidationError, validate_schema_keywords
from core.reliable_semantic_contracts import (
    HASH_DEPENDENCIES,
    is_valid_local_commit_transition,
    readiness_failure_reasons,
    ready_for_next_step,
    validate_hash_dependency_dag,
)
from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.semantic_contracts import (
    validate_story_project_semantic_fixture_manifest,
    validate_story_project_semantic_state,
)


FIXTURE_ROOT = Path("tests/fixtures/story_project_semantics")


class StoryProjectSemanticContractTest(unittest.TestCase):
    def test_local_commit_state_machine_never_rolls_back_after_marker(self) -> None:
        self.assertTrue(is_valid_local_commit_transition("prepared", "rolling_back"))
        self.assertTrue(is_valid_local_commit_transition("applying", "commit_marked"))
        self.assertTrue(is_valid_local_commit_transition("commit_marked", "publishing"))
        self.assertFalse(is_valid_local_commit_transition("commit_marked", "rolling_back"))
        self.assertFalse(is_valid_local_commit_transition("completed", "rolling_back"))
        self.assertTrue(
            is_valid_local_commit_transition(
                "recovery_required",
                "rolling_back",
                commit_marker_valid=False,
            )
        )
        self.assertTrue(
            is_valid_local_commit_transition(
                "recovery_required",
                "publishing",
                commit_marker_valid=True,
            )
        )
        self.assertFalse(
            is_valid_local_commit_transition(
                "recovery_required",
                "rolling_back",
                commit_marker_valid=True,
            )
        )

    def test_hash_contract_is_acyclic_and_receipt_does_not_feed_final_run_or_marker(self) -> None:
        order = validate_hash_dependency_dag()

        self.assertLess(order.index("commit_marker_hash"), order.index("publication_receipt_hash"))
        self.assertNotIn("publication_receipt_hash", HASH_DEPENDENCIES["final_run_hash"])
        self.assertNotIn("publication_receipt_hash", HASH_DEPENDENCIES["commit_marker_hash"])

    def test_hash_contract_rejects_a_cycle(self) -> None:
        with self.assertRaisesRegex(ValueError, "hash dependency cycle"):
            validate_hash_dependency_dag({"a": {"b"}, "b": {"a"}})

    def test_readiness_requires_verified_commit_delivery_and_stable_next_context(self) -> None:
        complete_evidence = {
            "accepted": True,
            "committed": True,
            "project_identity_matches": True,
            "required_delivery_states": ["succeeded"],
            "next_context_valid": True,
            "read_set_unchanged": True,
        }

        self.assertTrue(ready_for_next_step(**complete_evidence))
        self.assertEqual(
            (
                "required_delivery_not_succeeded:pending",
                "next_step_context_drift",
            ),
            readiness_failure_reasons(
                **{
                    **complete_evidence,
                    "required_delivery_states": ["pending"],
                    "read_set_unchanged": False,
                }
            ),
        )

    def test_semantic_schemas_use_supported_keywords(self) -> None:
        for schema_name in (
            "story_project_semantic_state.schema.json",
            "story_project_semantic_fixture_manifest.schema.json",
        ):
            schema = _load_json(Path("schemas") / schema_name)
            self.assertIs(schema, validate_schema_keywords(schema, schema_name))

    def test_fixture_manifest_and_all_golden_states_validate(self) -> None:
        manifest = validate_story_project_semantic_fixture_manifest(
            _load_json(FIXTURE_ROOT / "manifest.json")
        )

        self.assertEqual(3, len(manifest["cases"]))
        for case in manifest["cases"]:
            project_path = FIXTURE_ROOT / case["project_path"]
            expected_path = FIXTURE_ROOT / case["expected_path"]
            self.assertTrue(project_path.is_dir(), case["id"])
            self.assertTrue(expected_path.is_file(), case["id"])
            state = validate_story_project_semantic_state(_load_json(expected_path))
            self.assertEqual(case["chapter_index"], state["chapter_index"])

    def test_synthetic_fixtures_cannot_claim_strict_qualification(self) -> None:
        manifest = _load_json(FIXTURE_ROOT / "manifest.json")

        self.assertFalse(manifest["qualification"]["target_sample_present"])
        self.assertFalse(manifest["qualification"]["strict_eligible"])
        self.assertFalse(any(case["source_class"] == "target_book_redacted" for case in manifest["cases"]))

        manifest["qualification"]["strict_eligible"] = True
        with self.assertRaisesRegex(ValueError, "requires a target_book_redacted case"):
            validate_story_project_semantic_fixture_manifest(manifest)

    def test_semantic_state_rejects_unknown_top_level_fields(self) -> None:
        state = _load_json(
            FIXTURE_ROOT / "cases" / "synthetic_standard" / "expected.json"
        )
        state["untracked_state"] = {"must_not": "pass"}

        with self.assertRaises(SchemaValidationError):
            validate_story_project_semantic_state(state)

    def test_malformed_fixture_keeps_unknown_text_non_authoritative(self) -> None:
        state = validate_story_project_semantic_state(
            _load_json(FIXTURE_ROOT / "cases" / "malformed_variant" / "expected.json")
        )

        self.assertTrue(any(conflict["blocking"] for conflict in state["conflicts"]))
        self.assertEqual(
            {"duplicate_managed_block", "same_authority_conflict"},
            {conflict["code"] for conflict in state["conflicts"]},
        )
        self.assertTrue(state["unsupported_excerpts"])
        self.assertTrue(all(not excerpt["authoritative"] for excerpt in state["unsupported_excerpts"]))

    def test_soak_spec_deterministically_describes_one_hundred_chapters(self) -> None:
        spec = _load_json(FIXTURE_ROOT / "soak_spec.json")
        chapters = list(range(1, spec["chapter_count"] + 1))
        schedule = spec["fact_schedule"]
        introduced = [
            chapter
            for chapter in chapters
            if chapter % schedule["foreshadowing_open_every"] == 0
        ]
        resolved = [
            chapter
            for chapter in introduced
            if chapter + schedule["foreshadowing_resolve_after"] <= spec["chapter_count"]
        ]

        self.assertEqual(100, len(chapters))
        self.assertEqual(spec["expected"]["introduced_foreshadowing"], len(introduced))
        self.assertEqual(spec["expected"]["resolved_foreshadowing"], len(resolved))
        self.assertEqual(
            spec["expected"]["open_foreshadowing"],
            len(introduced) - len(resolved),
        )
        self.assertTrue(spec["long_file"]["latest_fact_at_tail"])
        self.assertGreaterEqual(spec["long_file"]["tracking_prefix_chars"], 100000)

    def test_current_mapper_baseline_still_truncates_context_and_projects_previous_prose(self) -> None:
        project = FIXTURE_ROOT / "cases" / "synthetic_standard" / "book"

        context = build_story_project_runtime_context(project, 2, max_file_chars=80)

        self.assertTrue(context.previous_prose["truncated"])
        self.assertEqual(80, context.previous_prose["chars"])
        previous_items = [
            item
            for item in context.memory_context_overlay["items"]
            if item["name"] == "previous_prose"
        ]
        self.assertEqual(1, len(previous_items))
        self.assertEqual("timeline_event", previous_items[0]["type"])


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
