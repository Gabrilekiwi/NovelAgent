from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.context_budget import default_context_budget
from core.prompt_compiler import compile_prompt_contexts
from core.scene_continuity import empty_scene_state
from core.state.authoritative import empty_authoritative_state
from core.state.chapter_read_set import normalize_chapter_context_read_set
from core.state.generation_state_view import (
    apply_generation_state_view_to_snapshot,
    build_generation_state_view,
)
from core.state.input_pack import build_input_pack
from core.state.snapshot import normalize_snapshot
from modules.scene_repair.plan import build_repair_plan
import modules.scene_repair.repairer as repairer_module
from scripts import replay_chapter_workset_budget as replay


def _read_set() -> dict:
    return normalize_chapter_context_read_set(
        {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 18,
            "required_state_item_ids": [
                "characters/hero",
                "locations/hero",
            ],
            "required_event_item_ids": [
                "events/chapter-0017-beat-010"
            ],
            "continuity": {
                "last_scene_location": "radio-room",
                "last_scene_character_ids": ["hero"],
                "required_opening_bridge": (
                    "Continue directly from the radio countdown."
                ),
            },
            "narrative_constraints": [
                {
                    "constraint_id": "signal-window",
                    "lifecycle_action": "enforce",
                    "instruction": "Transmit for three seconds only.",
                }
            ],
            "expected_new_entities": [
                {
                    "kind": "creature",
                    "entity_id": "seeker",
                    "display_name": "Seeker",
                }
            ],
        },
        chapter_index=18,
        source_outline_sha256="a" * 64,
    )


def _authority() -> dict:
    authority = empty_authoritative_state()
    authority["characters"] = {
        "hero": {
            "character_id": "hero",
            "canonical_name": "Hero",
            "condition": "stable",
        },
        "unselected-secret": {
            "character_id": "unselected-secret",
            "canonical_name": "RAW-AUTHORITY-MUST-REMAIN-LOCAL",
        },
    }
    authority["locations"]["hero"] = {
        "entity_id": "hero",
        "location_id": "radio-room",
        "certainty": "confirmed",
        "status": "current",
    }
    authority["events"]["chapter-0017-beat-010"] = {
        "event_id": "chapter-0017-beat-010",
        "type": "countdown_started",
        "subjects": ["hero"],
        "objects": ["radio"],
        "location": "radio-room",
        "status": "completed",
        "detail": "The receiver started a sixty-second countdown.",
    }
    return authority


def _blueprint(read_set: dict) -> dict:
    return {
        "chapter_index": 18,
        "title": "Signal",
        "core_event": "Answer the signal.",
        "required_beats": [
            {
                "index": 1,
                "text": "Continue the countdown and send one reply.",
            }
        ],
        "upgrade_or_resource_gain": [],
        "human_conflict": "Risk the shelter or remain silent.",
        "timeline_constraints": [],
        "ending_pressure": "A new node wakes below the station.",
        "missing_fields": [],
        "chapter_context_read_set": read_set,
    }


def _plan() -> dict:
    event = {
        "event_id": "chapter-0018-beat-001",
        "type": "signal_reply",
        "subjects": ["hero"],
        "objects": ["radio"],
        "location": "radio-room",
        "status": "completed",
    }
    return {
        "goal": "Answer the signal.",
        "scenes": [
            {
                "index": 1,
                "type": "opening_bridge",
                "goal": "Continue the countdown.",
                "required_beats": [
                    "Continue the countdown and send one reply."
                ],
                "required_beat_indexes": [1],
                "required_event_ids": ["chapter-0018-beat-001"],
                "forbidden_event_ids": [],
                "planned_events": [event],
            }
        ],
    }


def _validation() -> dict:
    return {
        "ok": False,
        "problems": [
            {
                "code": "missing_required_term",
                "message": "The three-second limit is absent.",
                "validator": "logic",
                "severity": "high",
                "blocking": True,
                "category": "blocking",
                "repair_hint": "Add the bounded signal window.",
                "repair_action": "add_required_term",
                "repair_parameters": {"term": "three seconds"},
                "evidence": [
                    {
                        "kind": "missing_required_term",
                        "value": "three seconds",
                    }
                ],
            }
        ],
    }


def _production_inputs() -> tuple[str, dict, dict, dict]:
    authority = _authority()
    read_set = _read_set()
    view = build_generation_state_view(authority, read_set)
    snapshot = normalize_snapshot(
        {
            "chapter_index": 18,
            "book_id": "book-test",
            "world_state": {
                "locations": {
                    "radio-room": {
                        "name": "Radio Room",
                    }
                }
            },
            "characters": {
                "hero": {
                    "name": "Hero",
                    "current_location": "stale-location",
                }
            },
            "timeline": [
                {"detail": "RAW-TIMELINE-MUST-REMAIN-LOCAL"}
            ],
            "authoritative_state": authority,
        }
    )
    snapshot = apply_generation_state_view_to_snapshot(snapshot, view)
    blueprint = _blueprint(read_set)
    input_pack = build_input_pack(
        snapshot,
        {
            "chapter_index": 18,
            "goal": "Answer the signal.",
            "actions": [
                "build_snapshot",
                "generate_chapter",
                "validate",
                "repair_if_needed",
            ],
            "validation_focus": ["continuity", "logic"],
            "max_repair_attempts": 2,
        },
        {
            "source": "test",
            "status": "ready",
            "items": [],
            "source_mappings": [],
        },
        story_project_context={
            "chapter_index": 18,
            "chapter_blueprint": blueprint,
            "read_set": {"context_digest": "b" * 64},
        },
        generation_state_view=view,
    )
    return input_pack, blueprint, view, authority


def test_production_builders_measure_plan_scene_and_repair_without_model_call() -> None:
    input_pack, blueprint, view, authority = _production_inputs()
    budget = default_context_budget(
        model="gpt-5.5",
        endpoint_type="openai_compatible",
        enable_model_tokenizer=False,
    )
    contexts = compile_prompt_contexts(input_pack, budget=budget)
    plan = _plan()
    historical_scenes = [
        {
            "index": 1,
            "text": "The countdown reached three seconds.",
            "events": [],
            "scene_state_before": empty_scene_state(),
            "scene_state_after": empty_scene_state(),
        }
    ]
    validation = _validation()
    repair_plan = build_repair_plan(validation)

    with (
        patch.object(
            replay.chapter_pipeline,
            "chat_completion",
            side_effect=AssertionError("Scene replay must stay offline"),
        ),
        patch.object(
            repairer_module,
            "chat_completion",
            side_effect=AssertionError("Repair replay must stay offline"),
        ),
    ):
        plan_measurement = replay._measure_plan_context(
            contexts.plan.text,
            report=contexts.plan.report,
            safe_target=30_000,
        )
        scene_measurements = replay._measure_scene_requests(
            budget=budget,
            input_pack=contexts.scene.text,
            plan=plan,
            blueprint=blueprint,
            historical_scenes=historical_scenes,
            authoritative_state_source=authority,
            generation_state_view_source=view,
            safe_target=30_000,
        )
        repair_measurement = replay._measure_repair_request(
            budget=budget,
            input_pack=input_pack,
            chapter_text="The receiver answered after three seconds.",
            validation=validation,
            repair_plan=repair_plan,
            recovery_context={"available": False},
            language="en",
            safe_target=30_000,
        )

    assert plan_measurement["below_safe_target"] is True
    assert scene_measurements[0]["below_safe_target"] is True
    assert repair_measurement["below_safe_target"] is True
    assert repair_measurement["single_full_chapter"] is True
    assert scene_measurements[0]["request_kind"] == (
        "production_scene_request"
    )


def test_read_set_binding_fails_closed_on_missing_selected_id() -> None:
    read_set = _read_set()
    view = build_generation_state_view(_authority(), read_set)
    stale = copy.deepcopy(view)
    stale["selected_event_item_ids"] = []

    with pytest.raises(
        replay.ReplayChapterWorksetBudgetError
    ) as raised:
        replay._assert_read_set_binding(stale, read_set)

    assert raised.value.code == "read_set_event_ids_mismatch"


def test_artifact_loader_rejects_hash_mismatch_before_decoding(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".novelagent" / "runtime"
    artifact = runtime / "runs" / "artifact.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")

    with pytest.raises(
        replay.ReplayChapterWorksetBudgetError
    ) as raised:
        replay._load_bound_artifact(
            {
                "path": str(artifact),
                "sha256": "0" * 64,
                "chars": len(artifact.read_text(encoding="utf-8")),
            },
            runtime_root=runtime,
            label="artifact",
        )

    assert raised.value.code == "artifact_sha256_mismatch"


def test_artifact_loader_rejects_path_outside_runtime(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".novelagent" / "runtime"
    runtime.mkdir(parents=True)
    artifact = tmp_path / "outside.json"
    artifact.write_text("{}\n", encoding="utf-8")
    digest = replay._sha256_bytes(artifact.read_bytes())

    with pytest.raises(
        replay.ReplayChapterWorksetBudgetError
    ) as raised:
        replay._load_bound_artifact(
            {
                "path": str(artifact),
                "sha256": digest,
                "chars": 3,
            },
            runtime_root=runtime,
            label="artifact",
        )

    assert raised.value.code == "artifact_path_escape"


def test_read_only_guard_detects_source_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text("{}\n", encoding="utf-8")
    guard = replay._ReadOnlyGuard([source])
    source.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(
        replay.ReplayChapterWorksetBudgetError
    ) as raised:
        guard.verify()

    assert raised.value.code == "read_only_source_changed"


def test_safe_target_is_strictly_less_than() -> None:
    measurement = replay._measurement(
        stage="repair",
        report={
            "budgeted_input_tokens": 30_000,
            "raw_input_tokens": 29_000,
            "count_mode": "calibrated_estimate",
            "counter_version": "test",
            "hard_input_limit": 32_000,
            "within_budget": True,
        },
        payload="payload",
        safe_target=30_000,
    )

    assert measurement["below_safe_target"] is False
    assert measurement["safe_target_headroom"] == 0


def test_cli_returns_one_when_any_path_reaches_safe_target() -> None:
    with patch.object(
        replay,
        "replay_chapter_workset_budget",
        return_value={"result": {"safe": False}},
    ):
        exit_code = replay.main(
            [
                "--story-project",
                "ignored",
                "--chapter",
                "18",
                "--run-json",
                "ignored.json",
            ]
        )

    assert exit_code == 1
