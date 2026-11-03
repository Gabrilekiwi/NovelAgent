from __future__ import annotations

import copy
import json
import socket
from pathlib import Path

import pytest

from core.engine.chapter_candidate import ChapterCandidateIntegrityError
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.reviewed_chapter_quality import ReviewedChapterQualityError, prepare_candidate_chapter_quality
from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate
from core.story_project.coverage import build_text_blueprint_coverage
from core.story_project.read_set import StoryProjectSourceDriftError
from core.story_project.reviewed_state_document import digest
from scripts.experiments.run_d2_b0_compatibility import inventory
from tests.test_workbench_chapter_commits import case
from workbench.chapter_commits import ChapterCommitConflict


pytestmark = pytest.mark.parametrize("case", ["formal"], indirect=True)


def author_revision(case, text):
    draft = case["drafts"].get(case["draft"].id)
    draft = case["drafts"].update(draft.id, text=text, expected_revision=draft.revision)
    review = case["reviews"].create(draft.id, expected_draft_revision=draft.revision,
        expected_draft_sha256=draft.sha256, proposals=[p for p in fact_contract_example()["proposal"]["facts"]
            if p["kind"] in {"event", "counter"}], author_confirmed=True)
    facts = copy.deepcopy(review["facts"])
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    changed = case["reviews"].update(review["id"], facts=facts, expected_revision=review["revision"])
    confirmed = case["reviews"].confirm(review["id"], expected_revision=changed["revision"],
        expected_facts_sha256=changed["facts_sha256"], expected_draft_revision=draft.revision,
        expected_draft_sha256=draft.sha256, author_confirmed=True)
    case["request"] = {**case["request"], "review_id": confirmed["id"],
        "expected_draft_revision": draft.revision, "expected_review_revision": confirmed["revision"],
        "expected_draft_sha256": draft.sha256, "expected_facts_sha256": confirmed["facts_sha256"]}
    return draft


def complete_text(case):
    blueprint = case["builder"](case["root"]).build(3).story_project_context["chapter_blueprint"]
    body = (Path("tests/fixtures/chapter_quality/good_chapter.md").read_text(encoding="utf-8")
            .replace("林雪", "林澈").replace("陈岚", "周禾"))
    return (case["text"] + "\n\n" + body + "\n\n" + "。".join(b["text"] for b in blueprint["required_beats"])
            + "。\n\n" + blueprint["ending_pressure"] + "。\n")


def test_server_candidate_quality_runs_blueprint_gate_on_final_revision_without_calls_or_writes(case, monkeypatch):
    author_revision(case, complete_text(case))
    before = inventory(case["root"])
    def deny(*args, **kwargs):
        pytest.fail("quality attempted a Provider/network call")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    result = case["service"].prepare_quality(**case["request"])
    quality = result["quality"]
    assert quality["stage"] == "candidate_quality_checked"
    assert quality["quality_coverage_complete"] is True and quality["pending_gates"] == []
    assert quality["local_quality_passed"] is True, quality["local_policy_decision"]["findings"]
    assert quality["publication_authorized"] is False and result["publication_authorized"] is False
    assert "story_project" in quality["validation"]["executed_checks"]
    assert quality["validation"]["skipped_checks"] == []
    evidence = quality["candidate_evidence"]
    assert evidence["record_hash"] == case["record"]["record_hash"]
    assert evidence["original_prose_sha256"] == case["record"]["prose"]["sha256"]
    assert evidence["original_prose_sha256"] != quality["binding"]["final_prose_sha256"]
    assert evidence["scene_evidence"] == {"status": "not_applicable", "reason": "whole_chapter_candidate_1_1",
                                          "generated_scene_count": 0}
    assert quality["blueprint_coverage"]["scene_declarations_used"] is False
    assert quality["blueprint_coverage"]["missing_beat_indexes"] == []
    assert quality["binding"]["candidate_evidence_sha256"] == digest(evidence)
    assert quality["quality_sha256"] == digest({k: v for k, v in quality.items() if k != "quality_sha256"})
    repeated = case["service"].prepare_quality(**case["request"])
    # Raw quality evidence keeps the real evaluation timestamp. Request/input
    # bindings and decisions are stable; do not require two evaluations to have
    # identical clocks or use the timestamp-bearing quality hash as the key.
    assert repeated["preparation"] == result["preparation"]
    assert repeated["quality"]["binding"] == quality["binding"]
    first_report, second_report = copy.deepcopy(quality), copy.deepcopy(repeated["quality"])
    for report in (first_report, second_report):
        report.pop("quality_sha256")
        report["final_artifact_integrity"].pop("timestamp")
    assert first_report == second_report
    assert inventory(case["root"]) == before and len(case["calls"]) == 1


def test_missing_beats_are_blocking_in_actual_server_quality(case):
    result = case["service"].prepare_quality(**case["request"])["quality"]
    assert result["quality_coverage_complete"] is True
    assert result["blueprint_coverage"]["missing_beat_indexes"]
    assert result["local_quality_passed"] is False
    assert any(p["code"] == "missing_required_beat" and p["blocking"] for p in result["validation"]["problems"])


def test_quality_cannot_reuse_old_pass_after_author_removes_required_material(case):
    author_revision(case, complete_text(case))
    before = case["service"].prepare_quality(**case["request"])["quality"]
    author_revision(case, case["text"])
    after = case["service"].prepare_quality(**case["request"])["quality"]
    assert before["local_quality_passed"] is True and after["local_quality_passed"] is False
    assert before["binding"]["candidate_evidence_sha256"] == after["binding"]["candidate_evidence_sha256"]
    assert before["quality_sha256"] != after["quality_sha256"]


def test_strict_still_requires_model_even_with_complete_blueprint(case):
    author_revision(case, complete_text(case))
    quality = case["service"].prepare_quality(**case["request"], quality_policy="strict")["quality"]
    assert quality["blueprint_coverage"]["missing_beat_indexes"] == []
    assert quality["pending_gates"] == ["llm_validator"]
    assert quality["quality_coverage_complete"] is False and quality["local_quality_passed"] is False
    assert quality["model_validation_executed"] is False


@pytest.mark.parametrize("field", ["candidate_intent_hash", "candidate_id"])
def test_valid_preparation_hash_cannot_substitute_another_candidate(case, field):
    prepared = case["service"].prepare(**case["request"])
    prepared["binding"][field] = "0" * 64 if field == "candidate_intent_hash" else "candidate_" + "0" * 40
    prepared["request_sha256"] = digest({"version": prepared["preparation_version"], "binding": prepared["binding"]})
    prepared["preparation_sha256"] = digest({k: v for k, v in prepared.items() if k != "preparation_sha256"})
    with pytest.raises((ReviewedChapterQualityError, ChapterCandidateIntegrityError)):
        prepare_candidate_chapter_quality(trusted_context=case["builder"](case["root"]).build(3),
                                         chapter_preparation=prepared)


@pytest.mark.parametrize("file", ["record.json", "input.md", "draft.md"])
def test_candidate_archive_damage_during_quality_cannot_pass(case, monkeypatch, file):
    store = case["executor"].store
    original = FinalArtifactIntegrityGate.evaluate
    def evaluate_and_damage(self, **kwargs):
        result = original(self, **kwargs)
        (store.candidate_dir(case["record"]["candidate_id"]) / file).write_text("damaged", encoding="utf-8")
        return result
    monkeypatch.setattr(FinalArtifactIntegrityGate, "evaluate", evaluate_and_damage)
    with pytest.raises((ValueError, RuntimeError)):
        case["service"].prepare_quality(**case["request"])
    assert len(case["calls"]) == 1


def test_author_edit_during_quality_cannot_return_an_old_pass(case, monkeypatch):
    original = FinalArtifactIntegrityGate.evaluate
    def evaluate_and_edit(self, **kwargs):
        result = original(self, **kwargs)
        draft = case["drafts"].get(case["draft"].id)
        case["drafts"].update(draft.id, text=draft.text + "作者并发改稿。", expected_revision=draft.revision)
        return result
    monkeypatch.setattr(FinalArtifactIntegrityGate, "evaluate", evaluate_and_edit)
    with pytest.raises(ChapterCommitConflict, match="changed"):
        case["service"].prepare_quality(**case["request"])


def test_outline_edit_during_quality_invalidates_frozen_blueprint(case, monkeypatch):
    from core.story_project.paths import canonical_outline_path
    original = FinalArtifactIntegrityGate.evaluate
    def evaluate_and_edit(self, **kwargs):
        result = original(self, **kwargs)
        outline = canonical_outline_path(case["root"], 3)
        outline.write_bytes(outline.read_bytes() + "\n作者改变章节意图。".encode("utf-8"))
        return result
    monkeypatch.setattr(FinalArtifactIntegrityGate, "evaluate", evaluate_and_edit)
    with pytest.raises(StoryProjectSourceDriftError):
        case["service"].prepare_quality(**case["request"])


def test_scene_fields_cannot_be_smuggled_into_whole_chapter_record(case):
    path = case["executor"].store.record_path(case["record"]["candidate_id"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["scene_drafts"] = [{"covered_beat_indexes": [1, 2, 3]}]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        case["service"].prepare_quality(**case["request"])


def test_fulltext_coverage_reuses_existing_rule_with_explicit_ending_window(case):
    blueprint = case["builder"](case["root"]).build(3).story_project_context["chapter_blueprint"]
    required = "\n".join(beat["text"] for beat in blueprint["required_beats"])
    report = build_text_blueprint_coverage(blueprint, blueprint["ending_pressure"] + "\n" + required + "\n" + "风。" * 700)
    assert report["missing_beat_indexes"] == []
    assert report["ending_pressure_covered"] is False
    assert report["scene_declarations_used"] is False
    assert report["basis"] == "whole_chapter_text_heuristic_v1"
