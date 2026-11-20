from __future__ import annotations

import copy
from pathlib import Path
import socket

import pytest

from core.engine.chapter_commit_prepare import prepare_reviewed_chapter
from core.engine.reviewed_chapter_quality import (
    ReviewedChapterQualityError, prepare_reviewed_chapter_quality,
)
from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate
from core.story_project.read_set import StoryProjectSourceDriftError
from core.story_project.reviewed_state_context import reviewed_state_snapshot
from scripts.experiments.run_d2_b0_compatibility import inventory
from tests.test_chapter_commit_prepare import case, hash_value, text_hash


def _prepare(case, *, text_suffix="", policy="standard"):
    inputs = copy.deepcopy(case["inputs"])
    if text_suffix:
        text = inputs["draft"]["text"] + text_suffix
        inputs["draft"].update(text=text, sha256=text_hash(text))
        inputs["review"].update(source_text=text, source_draft_sha256=text_hash(text))
    prepared = prepare_reviewed_chapter(**inputs)
    quality = prepare_reviewed_chapter_quality(
        trusted_context=inputs["trusted_context"], chapter_preparation=prepared, quality_policy=policy,
    )
    return prepared, quality


def _rehash(prepared):
    prepared["request_sha256"] = hash_value({
        "version": prepared["preparation_version"], "binding": prepared["binding"],
    })
    prepared["preparation_sha256"] = hash_value({
        k: v for k, v in prepared.items() if k != "preparation_sha256"
    })


def test_real_local_producers_bind_final_bytes_without_network_or_writes(case, monkeypatch):
    def deny_network(*args, **kwargs):
        pytest.fail("local quality preparation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    before = inventory(case["root"])
    inputs = copy.deepcopy(case["inputs"])
    prepared, result = _prepare(case)
    assert inventory(case["root"]) == before
    assert case["inputs"] == inputs
    assert result["stage"] == "local_quality_checked"
    assert result["publication_authorized"] is False
    assert result["quality_coverage_complete"] is False
    assert result["pending_gates"] == ["candidate_scene_and_blueprint_evidence"]
    assert result["model_validation_executed"] is False
    assert result["local_policy_decision"]["llm_validator"]["available"] is False
    assert {"continuity", "spatial", "logic", "final_artifact_integrity", "authoritative_state"} <= set(
        result["validation"]["executed_checks"]
    )
    assert "story_project" not in result["validation"]["executed_checks"]
    assert {"deterministic_review", "narrative_rules"} <= set(result["local_policy_decision"]["producers"])
    assert result["review_gate"]["enabled"] is True
    assert result["review_pipeline"]["artifacts"]["output_dir"] is None
    assert all(value is None for value in result["review_pipeline"]["artifacts"].values())
    assert result["binding"]["final_prose_sha256"] == text_hash(prepared["final_prose"])
    assert result["final_artifact_integrity"]["artifact_sha256"] == text_hash(prepared["final_prose"])
    assert result["binding"]["chapter_preparation_sha256"] == prepared["preparation_sha256"]
    assert result["binding"]["read_set_sha256"] == hash_value(prepared["read_set"])
    actual_snapshot = reviewed_state_snapshot(inputs["trusted_context"].story_project_context)
    assert actual_snapshot != inputs["trusted_context"].snapshot
    assert result["binding"]["snapshot_sha256"] == hash_value(actual_snapshot)
    assert result["authoritative_state_validation"]["state_after"] == prepared["fact_preparation"]["state_after"]
    assert result["quality_sha256"] == hash_value({k: v for k, v in result.items() if k != "quality_sha256"})


def test_strict_policy_does_not_invent_model_validation(case):
    _, result = _prepare(case, policy="strict")
    assert result["local_quality_passed"] is False
    assert result["local_policy_decision"]["accepted"] is False
    assert "llm_validator" in result["pending_gates"]
    assert any("llm_validator_unavailable" in finding["codes"]
               for finding in result["local_policy_decision"]["findings"])
    assert result["local_policy_decision"]["llm_validator"]["required"] is True
    assert result["model_validation_executed"] is False


def test_meta_output_is_checked_against_final_author_text(case):
    _, result = _prepare(case, text_suffix="\n作为AI，以下是本章分析：\n```json\n{}\n```\n")
    assert result["review_gate"]["status"] == "fail"
    assert result["local_policy_decision"]["accepted"] is False
    assert result["local_quality_passed"] is False


def test_final_artifact_duplicates_are_blocking(case):
    paragraph = "风从沙站北面的门缝中钻进来，把墙角的细沙一点点推向桌边。林澈抬头看了一眼窗外的暮色，又低头翻过手里的纸页，仔细核对墨迹尚未干透的地名。"
    _, result = _prepare(case, text_suffix="\n\n" + paragraph + "\n\n" + paragraph + "\n")
    assert result["final_artifact_integrity"]["accepted"] is False
    assert result["local_quality_passed"] is False
    assert any(problem["blocking"] for problem in result["validation"]["problems"]
               if problem.get("producer") == "final_artifact_integrity"
               or "duplicate" in problem.get("code", ""))


def test_new_author_edit_gets_new_quality_binding(case):
    first_prepared, first = _prepare(case)
    second_prepared, second = _prepare(case, text_suffix="\n林澈停下笔，重新检查纸上的地名。")
    assert first_prepared["binding"]["final_prose_sha256"] != second_prepared["binding"]["final_prose_sha256"]
    assert first["binding"]["request_sha256"] != second["binding"]["request_sha256"]
    assert first["quality_sha256"] != second["quality_sha256"]


@pytest.mark.parametrize("field,value", [
    ("chapter", 9), ("book_id", "another-book"), ("context_sha256", "f" * 64),
    ("reviewed_source_sha256", "f" * 64), ("acceptance_kind", "automatic_policy"),
])
def test_rehashed_wrong_context_or_acceptance_is_rejected(case, field, value):
    prepared = prepare_reviewed_chapter(**case["inputs"])
    prepared["binding"][field] = value
    _rehash(prepared)
    reason = ("chapter acceptance source is invalid" if field == "acceptance_kind"
              else "chapter preparation belongs to different facts, context, state or authority")
    with pytest.raises(ReviewedChapterQualityError, match=f"^{reason}$"):
        prepare_reviewed_chapter_quality(
            trusted_context=case["inputs"]["trusted_context"], chapter_preparation=prepared,
        )


def test_rehashed_false_after_state_is_replayed_and_rejected(case):
    prepared = prepare_reviewed_chapter(**case["inputs"])
    prepared["fact_preparation"]["state_after"]["numeric_counters"]["alert_level"]["current_value"] = 91
    _rehash(prepared)
    with pytest.raises(ReviewedChapterQualityError, match="do not replay"):
        prepare_reviewed_chapter_quality(
            trusted_context=case["inputs"]["trusted_context"], chapter_preparation=prepared,
        )


def test_prose_changed_without_binding_is_rejected(case):
    prepared = prepare_reviewed_chapter(**case["inputs"])
    prepared["final_prose"] += "新的改稿。\n"
    _rehash(prepared)
    with pytest.raises(ReviewedChapterQualityError, match="final prose or request"):
        prepare_reviewed_chapter_quality(
            trusted_context=case["inputs"]["trusted_context"], chapter_preparation=prepared,
        )


@pytest.mark.parametrize("policy", ["minimal", "unknown", {"name": "strict", "llm_validator_required": False}])
def test_caller_cannot_relax_quality_policy(case, policy):
    prepared = prepare_reviewed_chapter(**case["inputs"])
    with pytest.raises(ReviewedChapterQualityError, match="unmodified standard and strict"):
        prepare_reviewed_chapter_quality(
            trusted_context=case["inputs"]["trusted_context"], chapter_preparation=prepared, quality_policy=policy,
        )


@pytest.mark.parametrize("during_evaluation", [False, True])
def test_source_edit_before_or_during_quality_is_rejected(case, monkeypatch, during_evaluation):
    prepared = prepare_reviewed_chapter(**case["inputs"])
    source = case["root"] / "追踪/上下文.md"

    def edit_source():
        source.write_text(source.read_text(encoding="utf-8") + "\n作者补充。", encoding="utf-8")

    if during_evaluation:
        original = FinalArtifactIntegrityGate.evaluate

        def evaluate_and_edit(self, **kwargs):
            report = original(self, **kwargs)
            edit_source()
            return report

        monkeypatch.setattr(FinalArtifactIntegrityGate, "evaluate", evaluate_and_edit)
    else:
        edit_source()
    with pytest.raises(StoryProjectSourceDriftError):
        prepare_reviewed_chapter_quality(
            trusted_context=case["inputs"]["trusted_context"], chapter_preparation=prepared,
        )


def test_warning_only_local_review_does_not_become_publication_permission(case):
    prose = (Path("tests/fixtures/chapter_quality/good_chapter.md").read_text(encoding="utf-8")
             .replace("林雪", "林澈").replace("陈岚", "周禾"))
    _, result = _prepare(case, text_suffix="\n" + prose)
    assert result["review_pipeline"]["status"] in {"pass", "warning"}
    assert result["review_gate"]["status"] == "pass"
    assert result["local_quality_passed"] is True, result["local_policy_decision"]["findings"]
    assert result["publication_authorized"] is False
    assert result["pending_gates"] == ["candidate_scene_and_blueprint_evidence"]
