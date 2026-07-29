from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from api.contracts import ModelResponse
from core.engine.artifacts import _format_input_pack_markdown
from core.execution_provenance import build_execution_provenance
from core.model_calls import ModelCallStore, build_scene_generation_call_id
from modules.chapter_generator import pipeline as chapter_pipeline
from scripts import replay_scene_budget as replay_module
from scripts.replay_scene_budget import (
    HISTORICAL_COVERAGE_SHA256,
    HISTORICAL_SOURCE_COMMIT,
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
            {"index": index, "text": f"按顺序完成第{index}个唯一剧情节拍"}
            for index in range(1, 10)
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


def _fixture_snapshot(
    authority: dict,
    *,
    chapter_index: int = 18,
    book_id: str = "fixture-book",
) -> dict:
    return {
        "book_id": book_id,
        "chapter_index": chapter_index,
        "world_state": {"locations": {}},
        "characters": {},
        "timeline": [],
        "story_state": {
            "last_chapter_ending": "",
            "last_scene_location": "",
            "last_scene_characters": [],
            "open_threads": [],
            "required_opening_bridge": "",
        },
        "spatial_state": {
            "spaces": {},
            "connections": [],
            "character_positions": {},
            "blocked_paths": [],
            "last_transition": {},
        },
        "authoritative_state": authority,
    }


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


def _historical_nine_scene_plan() -> dict:
    blueprint = _fixture_blueprint()
    prefix = f"{blueprint['title']}: "
    return chapter_pipeline._validate_plan(
        {
            "goal": blueprint["core_event"],
            "scenes": [
                {
                    "index": index,
                    "type": "story_project_blueprint",
                    "goal": (
                        f"{prefix}Cover StoryProject required beat group {index}: "
                        f"按顺序完成第{index}个唯一剧情节拍"
                    ),
                    "required_beats": [f"按顺序完成第{index}个唯一剧情节拍"],
                    "required_beat_indexes": [index],
                }
                for index in range(1, 10)
            ],
        },
        chapter_index=18,
    )


def _provenance(git_commit: str) -> dict:
    return build_execution_provenance(
        code_bundle_hash="b" * 64,
        code_file_count=1,
        git_commit=git_commit,
        git_dirty=False,
        prompt_hashes={},
        schema_hashes={},
        dependency_versions={},
        provider="openai",
        model="gpt-5.5",
        python_version="3.11.0",
        python_implementation="CPython",
    ).to_dict()


def _build_fixture(
    tmp_path: Path,
    *,
    include_persisted_plan: bool = True,
    source_commit: str = HISTORICAL_SOURCE_COMMIT,
) -> tuple[Path, dict]:
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
        "error": {
            "type": "ContextBudgetError",
            "message": (
                "story_project_context_headroom_exceeded: scene input requires "
                "30902 tokens; safe target is 30000; hard limit is 32000"
            ),
        },
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
    execution_dir = run_dir / "executions" / execution_id
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

    provenance = _provenance(source_commit)
    provenance_path = execution_dir / "provenance.json"
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    plan_path: Path | None = None
    plan_metadata: dict | None = None
    if include_persisted_plan:
        plan_path = (
            run_dir
            / "chapter_pipeline"
            / f"chapter_plan_{chapter_index:04d}_{run_id}.json"
        )
        plan_path.parent.mkdir()
        plan_text = json.dumps(
            _historical_nine_scene_plan(),
            ensure_ascii=False,
            indent=2,
        )
        plan_path.write_text(plan_text, encoding="utf-8")
        plan_metadata = {
            "path": str(plan_path.resolve()),
            "chars": len(plan_text),
            "format": "json",
            "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        }

    run_record = {
        **run,
        "story_project": {
            "book_id": "fixture-book",
        },
        "input_pack": {
            "chars": len(input_pack),
            "artifact": {
                "path": str(artifact_path.resolve()),
                "chars": len(input_pack),
                "format": "markdown",
                "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            },
        },
        "execution_evidence": {
            "execution_id": execution_id,
            "model_calls_ref": model_calls_ref,
            "provenance_artifact_ref": (
                f"executions/{execution_id}/provenance.json"
            ),
            "provenance_hash": provenance["provenance_hash"],
        },
        **(
            {
                "chapter": {
                    "pipeline": {
                        "artifacts": {
                            "plan": plan_metadata,
                        }
                    }
                }
            }
            if plan_metadata is not None
            else {}
        ),
    }
    run_path = run_dir / f"{run_id}.json"
    run_path.write_text(
        json.dumps({"run": run_record}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_path, {
        "run_dir": run_dir,
        "run_id": run_id,
        "input_pack_path": artifact_path,
        "response_path": response_path,
        "receipt": receipt,
        "input_pack": input_pack,
        "plan_path": plan_path,
        "provenance_path": provenance_path,
        "run_json_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
    }


def _replay_fixture(
    run_path: Path,
    fixture: dict,
    *,
    run_json_sha256: str | None = None,
    **kwargs: object,
) -> dict:
    return replay_scene_budget(
        run_path,
        expected_run_json_sha256=(
            run_json_sha256 or fixture["run_json_sha256"]
        ),
        **kwargs,
    )


def _assert_common_replay(report: dict, fixture: dict) -> None:
    historical = report["historical_plan_replay"]
    current = report["current_plan_preflight"]
    assert report["model_calls_performed"] == 0
    run_integrity = report["run"]["artifact_integrity"]
    assert run_integrity == {
        "expected_sha256": fixture["run_json_sha256"],
        "actual_sha256": fixture["run_json_sha256"],
        "source": "caller_pinned_legacy_run_json_sha256",
        "recorded": False,
        "verified": True,
    }
    assert historical["model_calls_performed"] == 0
    assert current["model_calls_performed"] == 0
    assert (
        current["authority_source"]["source_kind"]
        == "persisted_input_pack_authoritative_state"
    )
    assert current["authority_source"]["recorded"] is True
    assert (
        report["current_compile_context"]["context_digest"]
        == report["compile_context"]["context_digest"]
    )
    assert report["input_pack"]["logical_chars"] == len(fixture["input_pack"])
    assert report["input_pack"]["chars_match"] is True
    assert report["input_pack"]["artifact_integrity"]["verified"] is True
    assert report["input_pack"]["artifact_integrity"]["recorded"] is True
    assert (
        report["input_pack"]["artifact_integrity"]["source"]
        == "run_record_artifact_sha256"
    )
    assert historical["scene_one"]["receipt_hash"] == fixture["receipt"]["receipt_hash"]
    assert (
        historical["scene_one"]["response_artifact_hash"]
        == fixture["receipt"]["response_artifact_hash"]
    )
    assert historical["scene_one"]["integrity_verified"] is True
    assert historical["scene_one"]["boundary_accepted"] is True
    recorded_failure = historical["recorded_failure"]
    assert recorded_failure["evidence_kind"] == "run_recorded_error"
    assert recorded_failure["recorded"] is True
    assert recorded_failure["required_input_tokens"] == 30_902
    assert recorded_failure["safe_input_limit"] == 30_000
    assert recorded_failure["hard_input_limit"] == 32_000
    assert recorded_failure["safe_limit_excess_tokens"] == 902
    assert historical["chapter_plan"]["beat_index_groups"] == [
        [index] for index in range(1, 10)
    ]
    assert current["chapter_plan"]["scene_count"] == 4
    assert current["chapter_plan"]["beat_index_groups"] == [
        [1, 2, 3],
        [4, 5],
        [6, 7],
        [8, 9],
    ]
    assert report["model_binding"]["provider"] == "openai"
    assert report["model_binding"]["model"] == "gpt-5.5"
    assert report["model_binding"]["endpoint_type"] == "openai_compatible"
    assert report["model_binding"]["enable_model_tokenizer"] is False
    assert report["model_binding"]["count_mode"] == "calibrated_estimate"
    assert historical["scene_two"]["transport"] == "compact_json"
    assert (
        historical["scene_two"]["evidence_kind"]
        == "current_compact_transport_counterfactual"
    )
    assert historical["scene_two"]["recorded"] is False
    assert historical["scene_two"]["safe_input_limit"] == 30_000
    assert historical["scene_two"]["hard_input_limit"] == 32_000
    assert historical["scene_two"]["within_safe_limit"] is True
    assert historical["scene_two"]["within_hard_limit"] is True
    assert historical["scene_two"]["construction_error"] is None
    historical_compaction = historical["scene_two"]["transport_compaction"]
    assert historical_compaction["semantic_json_equal"] is True
    assert historical_compaction["compact_chars"] < historical_compaction["pretty_chars"]
    assert historical_compaction["chars_saved"] > 0
    assert historical_compaction["budgeted_input_tokens_saved"] > 0
    assert current["scene_one"]["transport"] == "compact_json"
    assert current["scene_one"]["within_safe_limit"] is True
    assert current["scene_one"]["within_hard_limit"] is True
    assert current["scene_one"]["construction_error"] is None
    assert current["scene_one"]["transport_compaction"]["semantic_json_equal"] is True
    synthetic = current["synthetic_bounded_continuity_preflight"]
    assert synthetic["mode"] == "synthetic_bounded_continuity_preflight"
    assert synthetic["evidence_kind"] == "synthetic_bounded_continuity_preflight"
    assert synthetic["recorded"] is False
    assert synthetic["model_calls_performed"] == 0
    assert (
        synthetic["assumptions"]["previous_scene_tail_input_chars_per_scene"]
        == 600
    )
    assert (
        synthetic["assumptions"]["prior_scene_summary_tail_input_chars"]
        == 280
    )
    assert len(synthetic["scenes"]) == 4
    expected_completed_counts = [(0, 3), (3, 5), (5, 7), (7, 9)]
    expected_event_groups = [
        [f"chapter-0018-beat-{index:03d}" for index in (1, 2, 3)],
        [f"chapter-0018-beat-{index:03d}" for index in (4, 5)],
        [f"chapter-0018-beat-{index:03d}" for index in (6, 7)],
        [f"chapter-0018-beat-{index:03d}" for index in (8, 9)],
    ]
    accumulated_event_ids: list[str] = []
    for offset, (scene, expected_counts) in enumerate(
        zip(synthetic["scenes"], expected_completed_counts),
    ):
        continuity = scene["synthetic_continuity"]
        assert continuity["recorded"] is False
        assert continuity["previous_scene_tail_input_chars"] == 600
        assert continuity["previous_scene_tail_transport_chars"] <= 600
        assert continuity["prior_scene_summary_count_input"] == offset
        assert continuity["prior_scene_summary_count_transport"] <= offset
        assert continuity["prior_scene_summary_tail_input_chars"] == [
            280
        ] * offset
        assert all(
            chars <= 280
            for chars in continuity["prior_scene_summary_tail_transport_chars"]
        )
        assert (
            continuity["accumulated_required_event_ids_before"]
            == accumulated_event_ids
        )
        assert (
            continuity["current_required_event_ids"]
            == expected_event_groups[offset]
        )
        assert (
            continuity["completed_event_ids_count_before"],
            continuity["completed_event_ids_count_after"],
        ) == expected_counts
        assert (
            continuity["completed_event_ids_count_transport"]
            == expected_counts[0]
        )
        assert continuity["boundary_accepted"] is True
        assert scene["within_safe_limit"] is True
        assert scene["within_hard_limit"] is True
        accumulated_event_ids.extend(expected_event_groups[offset])
    assert synthetic["result"]["safe"] is True
    assert synthetic["result"]["minimum_safe_headroom_tokens"] > 0
    assert report["result"]["safe"] is True


def test_replay_uses_hash_verified_persisted_plan_and_current_four_scene_preflight(
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
        report = _replay_fixture(run_path, fixture)

    provider_completion.assert_not_called()
    _assert_common_replay(report, fixture)
    evidence = report["historical_plan_replay"]["plan_evidence"]
    assert evidence["evidence_kind"] == "persisted_plan_artifact"
    assert evidence["recorded"] is True
    assert evidence["integrity_verified"] is True


def test_replay_current_snapshot_overlays_only_current_authority_preflight(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    baseline = _replay_fixture(run_path, fixture)
    authority = replay_module._extract_section_object(
        fixture["input_pack"],
        "Authoritative State",
    )
    authority["roster"] = {
        "roster_fireseed_one": {
            "roster_id": "roster_fireseed_one",
            "name": "Fireseed One",
            "aliases": ["Fireseed"],
            "baseline_evidence": {
                "source_kind": "committed_chapter_prose",
                "source_path": "chapters/17.md",
                "sha256": "a" * 64,
                "exact_evidence": [
                    {
                        "line_number": 5,
                        "text": "IMMUTABLE-AUDIT-EVIDENCE-" + ("x" * 10_000),
                    }
                ],
            },
            "introduced_chapter": 17,
            "introduced_event_id": "chapter-0017-beat-001",
            "baseline_source": "runs/chapter-17.json",
            "migration_id": "chapter17-roster-baseline-v1",
            "members": [],
            "unresolved_count": 17,
            "declared_count": 17,
            "computed_count": 17,
            "source_tier": "story_project_standard",
        }
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            _fixture_snapshot(authority),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    report = _replay_fixture(
        run_path,
        fixture,
        current_snapshot_path=snapshot_path,
    )

    source = report["current_plan_preflight"]["authority_source"]
    assert source["source_kind"] == "current_snapshot_authoritative_state"
    assert source["recorded"] is False
    assert source["snapshot_path"] == str(snapshot_path.resolve())
    assert source["snapshot_sha256"] == hashlib.sha256(
        snapshot_path.read_bytes()
    ).hexdigest()
    assert source["snapshot_chapter_index"] == 18
    assert source["overlay_scope"] == "Authoritative State section only"
    assert source["snapshot_schema_valid"] is True
    assert source["authoritative_state_valid"] is True
    assert source["roster_counts"] == {"roster_fireseed_one": 17}
    assert report["compile_context"] == baseline["compile_context"]
    assert (
        report["current_compile_context"]["context_digest"]
        != report["compile_context"]["context_digest"]
    )
    synthetic = report["current_plan_preflight"][
        "synthetic_bounded_continuity_preflight"
    ]
    assert synthetic["result"]["safe"] is True
    assert synthetic["result"]["minimum_safe_headroom_tokens"] > 0
    assert report["result"]["safe"] is True
    assert report["model_calls_performed"] == 0


def test_replay_rejects_current_snapshot_for_another_chapter(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    snapshot_path = tmp_path / "wrong-chapter-snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            _fixture_snapshot(
                replay_module._extract_section_object(
                    fixture["input_pack"],
                    "Authoritative State",
                ),
                chapter_index=19,
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="chapter_index does not match",
    ):
        _replay_fixture(
            run_path,
            fixture,
            current_snapshot_path=snapshot_path,
        )


def test_replay_rejects_current_snapshot_for_another_book(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    snapshot_path = tmp_path / "wrong-book-snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            _fixture_snapshot(
                replay_module._extract_section_object(
                    fixture["input_pack"],
                    "Authoritative State",
                ),
                book_id="another-book",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="book_id does not match",
    ):
        _replay_fixture(
            run_path,
            fixture,
            current_snapshot_path=snapshot_path,
        )


def test_replay_reconstructs_unrecorded_plan_from_verified_source_revision(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(
        tmp_path,
        include_persisted_plan=False,
    )

    report = _replay_fixture(run_path, fixture)

    _assert_common_replay(report, fixture)
    evidence = report["historical_plan_replay"]["plan_evidence"]
    assert evidence["evidence_kind"] == "source_revision_reconstruction"
    assert evidence["recorded"] is False
    assert evidence["reconstruction_label"] == "reconstructed_not_recorded"
    assert evidence["source_commit"] == HISTORICAL_SOURCE_COMMIT
    assert evidence["source_sha256"] == HISTORICAL_COVERAGE_SHA256
    assert evidence["integrity_verified"] is True


def test_replay_fails_closed_when_persisted_plan_hash_mismatches(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    assert fixture["plan_path"] is not None
    fixture["plan_path"].write_text(
        fixture["plan_path"].read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="plan artifact hash does not match",
    ):
        _replay_fixture(run_path, fixture)


@pytest.mark.parametrize("artifact_kind", ["input_pack", "plan", "provenance"])
def test_replay_rejects_run_artifact_path_escape(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    run_path, fixture = _build_fixture(
        tmp_path,
        include_persisted_plan=artifact_kind == "plan",
    )
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    if artifact_kind == "input_pack":
        source_path = fixture["input_pack_path"]
        escaped_path = tmp_path / "outside-input-pack.md"
        escaped_path.write_bytes(source_path.read_bytes())
        payload["run"]["input_pack"]["artifact"]["path"] = str(
            escaped_path.resolve()
        )
    elif artifact_kind == "plan":
        source_path = fixture["plan_path"]
        assert isinstance(source_path, Path)
        escaped_path = tmp_path / "outside-plan.json"
        escaped_path.write_bytes(source_path.read_bytes())
        payload["run"]["chapter"]["pipeline"]["artifacts"]["plan"]["path"] = str(
            escaped_path.resolve()
        )
    else:
        source_path = fixture["provenance_path"]
        escaped_path = tmp_path / "outside-provenance.json"
        escaped_path.write_bytes(source_path.read_bytes())
        payload["run"]["execution_evidence"]["provenance_artifact_ref"] = str(
            escaped_path.resolve()
        )
    run_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="escapes the run directory",
    ):
        _replay_fixture(
            run_path,
            fixture,
            run_json_sha256=hashlib.sha256(run_path.read_bytes()).hexdigest(),
        )


def test_replay_fails_closed_when_source_commit_mismatches(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(
        tmp_path,
        include_persisted_plan=False,
        source_commit="1" * 40,
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="source revision does not match",
    ):
        _replay_fixture(run_path, fixture)


def test_replay_fails_closed_when_historical_source_hash_mismatches(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(
        tmp_path,
        include_persisted_plan=False,
    )
    real_git_bytes = replay_module._git_bytes

    def tampered_git_bytes(arguments: list[str]) -> bytes:
        if arguments and arguments[0] == "show":
            return b"tampered historical coverage source"
        return real_git_bytes(arguments)

    with patch.object(
        replay_module,
        "_git_bytes",
        side_effect=tampered_git_bytes,
    ), pytest.raises(
        ReplaySceneBudgetError,
        match="source hash does not match",
    ):
        _replay_fixture(run_path, fixture)


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
        _replay_fixture(resolved, fixture)


def test_replay_rejects_input_pack_character_count_mismatch(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
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
        _replay_fixture(
            run_path,
            fixture,
            run_json_sha256=hashlib.sha256(run_path.read_bytes()).hexdigest(),
        )


def test_replay_rejects_same_length_input_pack_tamper(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    original = fixture["input_pack_path"].read_text(encoding="utf-8")
    tampered = original.replace(
        "Offline budget replay",
        "Offline budget replax",
        1,
    )
    assert tampered != original
    assert len(tampered) == len(original)
    assert len(tampered.encode("utf-8")) == len(original.encode("utf-8"))
    fixture["input_pack_path"].write_text(tampered, encoding="utf-8")

    with pytest.raises(
        ReplaySceneBudgetError,
        match="artifact sha256 does not match",
    ):
        _replay_fixture(run_path, fixture)


def test_replay_requires_caller_pin_for_legacy_unhashed_input_pack(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["run"]["input_pack"]["artifact"].pop("sha256")
    run_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pinned = hashlib.sha256(fixture["input_pack_path"].read_bytes()).hexdigest()
    run_json_pinned = hashlib.sha256(run_path.read_bytes()).hexdigest()

    with pytest.raises(
        ReplaySceneBudgetError,
        match="provide --input-pack-sha256",
    ):
        replay_scene_budget(
            run_path,
            expected_run_json_sha256=run_json_pinned,
        )

    report = replay_scene_budget(
        run_path,
        expected_input_pack_sha256=pinned,
        expected_run_json_sha256=run_json_pinned,
    )
    binding = report["input_pack"]["artifact_integrity"]
    assert binding["verified"] is True
    assert binding["recorded"] is False
    assert binding["source"] == "caller_pinned_legacy_artifact_sha256"
    assert binding["expected_sha256"] == pinned
    assert binding["actual_sha256"] == pinned


def test_replay_requires_caller_pin_for_legacy_run_json(
    tmp_path: Path,
) -> None:
    run_path, _fixture = _build_fixture(tmp_path)

    with pytest.raises(
        ReplaySceneBudgetError,
        match="provide --run-json-sha256",
    ):
        replay_scene_budget(run_path)


def test_replay_prefers_trusted_recorded_run_json_hash_when_available(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)

    report = replay_scene_budget(
        run_path,
        recorded_run_json_sha256=fixture["run_json_sha256"],
    )

    assert report["run"]["artifact_integrity"] == {
        "expected_sha256": fixture["run_json_sha256"],
        "actual_sha256": fixture["run_json_sha256"],
        "source": "recorded_run_json_sha256",
        "recorded": True,
        "verified": True,
    }


def test_replay_cli_accepts_explicit_run_json_hash() -> None:
    digest = "a" * 64

    args = replay_module.parse_args(
        [
            "--run-json",
            "run.json",
            "--run-json-sha256",
            digest,
        ]
    )

    assert args.run_json_sha256 == digest


def test_replay_rejects_tampered_recorded_headroom_failure_before_decode(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["run"]["error"]["message"] = (
        "story_project_context_headroom_exceeded: scene input requires "
        "29999 tokens; safe target is 30000; hard limit is 32000"
    )
    run_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(
        ReplaySceneBudgetError,
        match="run JSON sha256 does not match",
    ):
        _replay_fixture(run_path, fixture)


def test_replay_rejects_same_length_run_error_tamper_before_decode(
    tmp_path: Path,
) -> None:
    run_path, fixture = _build_fixture(tmp_path)
    original = run_path.read_bytes()
    tampered = original.replace(b"requires 30902 tokens", b"requires 30903 tokens", 1)
    assert tampered != original
    assert len(tampered) == len(original)
    run_path.write_bytes(tampered)

    with pytest.raises(
        ReplaySceneBudgetError,
        match="run JSON sha256 does not match",
    ):
        _replay_fixture(run_path, fixture)
