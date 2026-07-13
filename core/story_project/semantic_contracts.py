from __future__ import annotations

from typing import Any

from core.schema import validate_schema


STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION = "1.0"
STORY_PROJECT_SEMANTIC_FIXTURE_SCHEMA_VERSION = "1.0"


def validate_story_project_semantic_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("StoryProject semantic state must be a JSON object")
    return validate_schema(state, "story_project_semantic_state.schema.json")


def validate_story_project_semantic_fixture_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("StoryProject semantic fixture manifest must be a JSON object")
    validated = validate_schema(manifest, "story_project_semantic_fixture_manifest.schema.json")
    qualification = validated["qualification"]
    cases = validated["cases"]
    target_cases = [case for case in cases if case["source_class"] == "target_book_redacted"]

    if qualification["target_sample_present"] != bool(target_cases):
        raise ValueError("target_sample_present must match target_book_redacted fixture cases")
    if qualification["strict_eligible"] and not target_cases:
        raise ValueError("strict fixture qualification requires a target_book_redacted case")
    if qualification["strict_eligible"] and any(
        not case["strict_qualification_eligible"] for case in target_cases
    ):
        raise ValueError("all target book fixture cases must be strict-qualification eligible")
    return validated


__all__ = [
    "STORY_PROJECT_SEMANTIC_FIXTURE_SCHEMA_VERSION",
    "STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION",
    "validate_story_project_semantic_fixture_manifest",
    "validate_story_project_semantic_state",
]
