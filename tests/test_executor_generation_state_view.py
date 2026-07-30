from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from core.engine.executor import AgentExecutor
from core.schema import validate_schema
from core.state.authoritative import empty_authoritative_state
from core.state.chapter_read_set import normalize_chapter_context_read_set
from core.structured_context import StructuredContextError


def _case_dir(name: str) -> Path:
    path = (
        Path.cwd()
        / ".tmp"
        / "test-executor-generation-view"
        / f"{name}-{uuid.uuid4().hex[:10]}"
    )
    path.mkdir(parents=True)
    return path


def _read_set(*, character_id: str = "hero") -> dict:
    return normalize_chapter_context_read_set(
        {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 18,
            "required_state_item_ids": [
                f"characters/{character_id}",
                "locations/hero",
            ],
            "required_event_item_ids": ["events/chapter-0017-beat-010"],
            "continuity": {
                "last_scene_location": "fire-station-garage",
                "last_scene_character_ids": ["hero"],
                "required_opening_bridge": "Continue from the station alarm.",
            },
            "narrative_constraints": [
                {
                    "constraint_id": "fs-siren",
                    "lifecycle_action": "resolve",
                    "instruction": "Resolve the alarm setup without revealing the red record.",
                }
            ],
            "expected_new_entities": [
                {
                    "kind": "scene_local",
                    "entity_id": "seeker-001",
                    "display_name": "Seeker",
                }
            ],
        },
        chapter_index=18,
        source_outline_sha256="a" * 64,
    )


def _snapshot() -> dict:
    authority = empty_authoritative_state()
    authority["characters"] = {
        "hero": {
            "character_id": "hero",
            "canonical_name": "Hero",
            "role": "lead",
        },
        "irrelevant": {
            "character_id": "irrelevant",
            "canonical_name": "Raw Authority Sentinel",
        },
    }
    authority["locations"] = {
        "hero": {
            "entity_id": "hero",
            "location_id": "fire-station-garage",
            "certainty": "confirmed",
            "status": "current",
            "last_reported_chapter": 17,
        }
    }
    authority["events"] = {
        "chapter-0017-beat-010": {
            "event_id": "chapter-0017-beat-010",
            "type": "checkpoint",
            "subjects": ["hero"],
            "objects": [],
            "location": "fire-station-garage",
            "status": "completed",
        }
    }
    return {
        "chapter_index": 18,
        "book_id": "book-generation-view",
        "project_profile": {
            "language": "en",
            "known_characters": ["Hero", "Raw Authority Sentinel"],
            "known_locations": ["cold-room", "fire-station-garage"],
        },
        "world_state": {
            "infection_level": "high",
            "locations": {
                "cold-room": {},
                "fire-station-garage": {},
            },
        },
        "story_state": {
            "last_scene_location": "cold-room",
            "required_opening_bridge": "cold-room",
        },
        "spatial_state": {
            "spaces": {
                "cold-room": {},
                "fire-station-garage": {},
            },
            "connections": [],
            "character_positions": {"hero": "cold-room"},
        },
        "characters": {
            "hero": {"current_location": "cold-room"},
            "irrelevant": {"current_location": "cold-room"},
        },
        "timeline": [{"summary": "RAW TIMELINE SENTINEL"}],
        "constraints": [],
        "authoritative_state": authority,
    }


def _decision() -> dict:
    return {
        "chapter_index": 18,
        "goal": "continue_existing_arc",
        "actions": ["generate_chapter", "validate"],
        "validation_focus": ["logic"],
        "max_repair_attempts": 0,
        "notes": [],
    }


def _ok_validation() -> dict:
    return validate_schema(
        {
            "ok": True,
            "requested_focus": ["logic"],
            "executed_checks": ["logic"],
            "skipped_checks": [],
            "checks": [{"name": "logic", "ok": True, "problems": []}],
            "problems": [],
            "blocking_problem_count": 0,
            "warning_count": 0,
            "severity_counts": [],
            "deterministic_repair_count": 0,
            "manual_review_count": 0,
            "repair_action_counts": [],
        },
        "validation_result.schema.json",
    )


def _analysis(chapter: str, _validation: dict) -> dict:
    return validate_schema(
        {
            "events": [{"text": chapter[:40]}],
            "character_changes": [],
            "world_changes": [],
            "new_locations": [],
            "story_state": {
                "last_chapter_ending": chapter[-80:],
                "last_scene_location": "fire-station-garage",
                "last_scene_characters": ["hero"],
                "open_threads": [],
                "required_opening_bridge": "",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [],
                "character_positions": {"hero": "fire-station-garage"},
                "blocked_paths": [],
                "last_transition": {
                    "to": "fire-station-garage",
                    "source": "chapter_analysis",
                },
            },
            "conflicts": ["danger"],
            "summary": chapter[:80],
            "validation_ok": True,
        },
        "analysis_result.schema.json",
    )


def _executor(
    root: Path,
    snapshot: dict,
    *,
    read_set: dict,
    director,
    validator,
) -> AgentExecutor:
    snapshot_path = root / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return AgentExecutor(
        snapshot_path=snapshot_path,
        memory_path=root / "missing-memory.json",
        run_dir=root / "runs",
        chapter_dir=root / "chapters",
        dry_run=True,
        use_run_history=False,
        director=director,
        generator=lambda _input_pack: (
            "Danger forces a choice and conflict at the fire-station garage. "
            "The alarm consequence remains concrete."
        ),
        validator=validator,
        analyzer=_analysis,
        story_project_context={
            "chapter_index": 18,
            "snapshot_overlay": {},
            "memory_context_overlay": {"items": [], "source_mappings": []},
            "chapter_blueprint": {
                "chapter_index": 18,
                "title": "Working set",
                "core_event": "The alarm forces a choice.",
                "required_beats": [
                    {"index": 1, "text": "Danger forces a choice and conflict."}
                ],
                "ending_pressure": "The alarm consequence remains concrete.",
                "source_path": "outline-18.md",
                "missing_fields": [],
                "chapter_context_read_set": read_set,
            },
            "source_paths": {"outline_path": "outline-18.md"},
            "source_resolution": {"entries": []},
        },
        quality_policy="minimal",
        enable_execution_provenance=False,
    )


def test_executor_uses_one_bounded_view_for_director_and_validator() -> None:
    root = _case_dir("bounded")
    director_snapshots: list[dict] = []
    validator_snapshots: list[dict] = []

    def director(snapshot: dict, _memory: dict) -> dict:
        director_snapshots.append(snapshot)
        return _decision()

    def validator(snapshot: dict, _chapter: str, _decision_value: dict) -> dict:
        validator_snapshots.append(snapshot)
        return _ok_validation()

    result = _executor(
        root,
        _snapshot(),
        read_set=_read_set(),
        director=director,
        validator=validator,
    ).run_once(persist=False)

    assert result["validation"]["ok"] is True
    assert len(director_snapshots) == 1
    assert len(validator_snapshots) == 1
    director_snapshot = director_snapshots[0]
    validator_snapshot = validator_snapshots[0]
    assert director_snapshot == validator_snapshot
    assert "authoritative_state" not in director_snapshot
    assert "timeline" not in director_snapshot
    assert set(director_snapshot["characters"]) == {"hero"}
    assert "Raw Authority Sentinel" not in json.dumps(
        director_snapshot,
        ensure_ascii=False,
    )
    assert "RAW TIMELINE SENTINEL" not in json.dumps(
        director_snapshot,
        ensure_ascii=False,
    )
    assert (
        director_snapshot["characters"]["hero"]["current_location"]
        == "fire-station-garage"
    )
    assert (
        director_snapshot["spatial_state"]["character_positions"]["hero"]
        == "fire-station-garage"
    )
    assert (
        director_snapshot["generation_state_view"]["projection_sha256"]
        == result["run"]["input_pack"]["metadata"]["generation_state_view"][
            "projection_sha256"
        ]
    )


def test_missing_read_set_item_fails_before_director_or_generator() -> None:
    root = _case_dir("missing")
    calls = {"director": 0, "validator": 0}

    def director(_snapshot: dict, _memory: dict) -> dict:
        calls["director"] += 1
        return _decision()

    def validator(_snapshot: dict, _chapter: str, _decision_value: dict) -> dict:
        calls["validator"] += 1
        return _ok_validation()

    with pytest.raises(StructuredContextError, match="characters/missing"):
        _executor(
            root,
            _snapshot(),
            read_set=_read_set(character_id="missing"),
            director=director,
            validator=validator,
        ).run_once(persist=False)

    assert calls == {"director": 0, "validator": 0}
