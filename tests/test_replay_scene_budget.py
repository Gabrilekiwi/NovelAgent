from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from api.contracts import ModelResponse
from core.engine.artifacts import _format_input_pack_markdown
from core.model_calls import ModelCallStore, build_scene_generation_call_id
from scripts.replay_scene_budget import (
    ReplaySceneBudgetError,
    replay_scene_budget,
    resolve_run_json_path,
)


def _fixture_blueprint() -> dict:
    return {
        "chapter_index": 18,
        "outline_path": "book/outline/chapter-18.md",
        "title": "Offline budget replay",
        "core_event": "The team verifies the warning signal.",
        "required_beats": [
            {"index": 1, "text": "承接上一章并复核警报"},
            {"index": 2, "text": "确认第二段信号来源"},
        ],
        "ending_pressure": "节点开始倒计时",
        "source_path": "book/outline/chapter-18.md",
        "missing_fields": [],
    }


def _fixture_input_pack() -> str:
    sections = [
        (
            "Project Profile",
            {
                "language": "zh-CN",
                "known_characters": ["陆沉"],
                "known_locations": ["通信室"],
            },
        ),
        (
            "Director Decision",
            {
                "chapter_index": 18,
                "goal": "复核信号",
                "actions": ["generate_chapter"],
                "validation_focus": ["continuity"],
                "max_repair_attempts": 1,
            },
        ),
        (
            "Story State",
            {
                "last_chapter_ending": "警报刚刚停止。",
                "last_scene_characters": ["陆沉"],
                "last_scene_location": "通信室",
                "open_threads": [],
                "required_opening_bridge": "通信室",
            },
        ),
        (
            "Spatial State",
            {
                "spaces": {"通信室": {}},
                "connections": [],
                "character_positions": {"陆沉": "通信室"},
                "blocked_paths": [],
                "last_transition": {},
            },
        ),
        (
            "Authoritative State",
            {
                "schema_version": "1.0",
                "source_precedence": [
                    "story_project_standard",
                    "chapter_event",
                    "model_inference",
                ],
                "characters": {
                    "陆沉": {
                        "character_id": "陆沉",
                        "canonical_name": "陆沉",
                        "aliases": ["陆沉"],
                    }
                },
                "relationships": {},
                "roster": {},
                "numeric_counters": {},
                "inventory": {},
                "locations": {
                    "陆沉": {
                        "entity_id": "陆沉",
                        "location_id": "通信室",
                    }
                },
                "events": {},
            },
        ),
        (
            "StoryProject Chapter Blueprint",
            {
                "chapter_blueprint": _fixture_blueprint(),
                "read_set_context_digest": "a" * 64,
            },
        ),
        (
            "Requirements",
            {
                "chapter_index": 18,
                "language": "zh-CN",
                "story_project_writeback": True,
            },
        ),
    ]
    return "\n\n".join(
        f"# {name}\n{json.dumps(value, ensure_ascii=False, indent=2)}"
        for name, value in sections
    )


def _scene_one_response() -> str:
    return json.dumps(
        {
            "prose": (
                "警报停止后，陆沉仍守在通信室。他重新核对接线图，确认没有人离开原位。"
            ),
            "events": [
                {
                    "event_id": "chapter-0018-beat-001",
                    "type": "required_beat_1_completed",
                    "subjects": ["陆沉"],
                    "objects": [],
                    "location": "通信室",
                    "status": "completed",
                }
            ],
            "deltas": {
                "characters": [],
                "relationships": [],
                "rosters": [],
                "locations": [],
                "inventory": [],
                "counters": [],
            },
            "continuity_note": "承接通信室状态",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _build_fixture(tmp_path: Path) -> tuple[Path, dict]:
    run_dir = tmp_path / "runs"
    run_dir.mkdir()
    run_id = "chapter_18_fixture"
    chapter_index = 18
    input_pack = _fixture_input_pack()
    run = {
        "id": run_id,
        "chapter_index": chapter_index,
        "status": "failed",
        "committed": False,
        "repair_attempts": 0,
    }
    artifact_path = (
        run_dir
        / "input_packs"
        / f"input_pack_{chapter_index:04d}_{run_id}.md"
    )
    artifact_path.parent.mkdir()
    artifact_path.write_text(
        _format_input_pack_markdown(input_pack, run),
        encoding="utf-8",
    )

    execution_id = "execution_fixture"
    model_calls_ref = f"executions/{execution_id}/model_calls"
    store = ModelCallStore(run_dir / model_calls_ref)
    call_id = build_scene_generation_call_id(1)
    attempt_id = f"{call_id}-a1"
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    store.create_intent(
        call_id=call_id,
        attempt_id=attempt_id,
        provider="openai",
        model="gpt-5.5",
        stage="chapter_generation",
        budget_reservation={
            "reserved_input_tokens": 1000,
            "reserved_output_tokens": 1000,
        },
        request={"messages": [{"role": "user", "content": "not persisted"}]},
        created_at=now,
    )
    response_text = _scene_one_response()
    response_ref = f"responses/{attempt_id}.txt"
    response_path = store.root / response_ref
    response_path.parent.mkdir(parents=True)
    response_path.write_text(response_text, encoding="utf-8")
    receipt = store.create_receipt(
        attempt_id,
        response=ModelResponse(
            response_text,
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            finish_reason="stop",
            request_id="response-fixture",
            actual_model="gpt-5.5",
            endpoint_type="openai_compatible",
        ),
        response_artifact_ref=response_ref,
        status="succeeded",
        received_at=now,
    )

    run_record = {
        **run,
        "input_pack": {
            "chars": len(input_pack),
            "artifact": {
                "path": str(artifact_path.resolve()),
                "chars": len(input_pack),
                "format": "markdown",
            },
        },
        "execution_evidence": {
            "execution_id": execution_id,
            "model_calls_ref": model_calls_ref,
        },
    }
    run_path = run_dir / f"{run_id}.json"
    run_path.write_text(
        json.dumps({"run": run_record}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_path, {
        "run_dir": run_dir,
        "run_id": run_id,
        "response_path": response_path,
        "receipt": receipt,
        "input_pack": input_pack,
    }


def test_replay_rebuilds_scene_two_with_verified_receipt_and_no_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOVELAGENT_MODEL_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("NOVELAGENT_MAX_INPUT_TOKENS", raising=False)
    run_path, fixture = _build_fixture(tmp_path)

    with patch(
        "api.openai_client.chat_completion",
        side_effect=AssertionError("provider call is forbidden"),
    ) as provider_completion:
        report = replay_scene_budget(run_path)

    provider_completion.assert_not_called()
    assert report["model_calls_performed"] == 0
    assert report["input_pack"]["logical_chars"] == len(fixture["input_pack"])
    assert report["input_pack"]["chars_match"] is True
    assert report["scene_one"]["receipt_hash"] == fixture["receipt"]["receipt_hash"]
    assert (
        report["scene_one"]["response_artifact_hash"]
        == fixture["receipt"]["response_artifact_hash"]
    )
    assert report["scene_one"]["integrity_verified"] is True
    assert report["scene_one"]["boundary_accepted"] is True
    assert report["model_binding"]["provider"] == "openai"
    assert report["model_binding"]["model"] == "gpt-5.5"
    assert report["model_binding"]["endpoint_type"] == "openai_compatible"
    assert report["model_binding"]["enable_model_tokenizer"] is False
    assert report["model_binding"]["count_mode"] == "calibrated_estimate"
    assert report["scene_two"]["transport"] == "compact_json"
    assert report["scene_two"]["budgeted_input_tokens"] <= 30_000
    assert report["scene_two"]["safe_input_limit"] == 30_000
    assert report["scene_two"]["hard_input_limit"] == 32_000
    assert report["scene_two"]["within_safe_limit"] is True
    assert report["scene_two"]["within_hard_limit"] is True
    assert report["scene_two"]["construction_error"] is None
    assert report["result"]["safe"] is True


def test_replay_resolves_run_dir_and_rejects_tampered_response(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    resolved = resolve_run_json_path(
        run_dir=fixture["run_dir"],
        run_id=fixture["run_id"],
    )
    assert resolved == run_path.resolve()
    fixture["response_path"].write_text(
        _scene_one_response() + "\n篡改",
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="response artifact hash does not match",
    ):
        replay_scene_budget(resolved)


def test_replay_rejects_input_pack_character_count_mismatch(
    tmp_path: Path,
) -> None:
    run_path, _fixture = _build_fixture(tmp_path)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["run"]["input_pack"]["chars"] += 1
    run_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="does not match extracted logical chars",
    ):
        replay_scene_budget(run_path)
