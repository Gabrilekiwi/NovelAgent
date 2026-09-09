"""Isolated identity upgrade transactions, with synthetic Strict qualification."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.persistence_v2 import PersistenceV2IntegrityError, PersistenceV2Transaction, reconcile_pending_persistence_v2
from core.engine.reviewed_upgrade import (
    ReviewedUpgradeError, json_bytes, prepare_reviewed_upgrade, prepare_reviewed_upgrade_transaction,
)
from core.story_project.activation import activate_story_state, build_story_state_calibration_report
from core.story_project.identity import ProjectIdentityError, ensure_project_identity, load_project_identity, validate_project_identity
from core.story_project.read_set import StoryProjectSourceDriftError, verify_story_project_read_set
from core.story_project.reviewed_identity import (
    ReviewedIdentityError, load_verified_reviewed_identity, validate_reviewed_identity, verify_reviewed_identity_transition,
)
from core.story_project.reviewed_state_document import RELATIVE_PATH, baseline_request, capture_sources, digest
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from scripts.experiments.run_d2_b0_compatibility import inventory


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def case():
    root = REPO / ".tmp/iu3" / uuid.uuid4().hex[:12] / "b"
    shutil.copytree(REPO / "tests/fixtures/story_project_semantics/cases/synthetic_standard/book", root)
    old = ensure_project_identity(root)
    report = build_story_state_calibration_report(book_id=old.book_id, parser_version=SEMANTIC_PARSER_VERSION,
        semantic_schema_version="1.0", target_layout_profile_version="canonical-zh-1",
        evidence={"target_sample_count": 1, "format_variant_count": 2, "managed_round_trip_rate": 1.0,
                  "required_field_exact_match_rate": 1.0, "authoritative_precision": 1.0,
                  "supported_optional_recall": .95, "unsupported_structure_count": 1,
                  "unsupported_structure_captured_count": 1, "consecutive_shadow_chapters": 10,
                  "blocking_conflict_count": 0, "missing_provenance_fields": []})
    old = activate_story_state(root, report)  # Synthetic only; no claim about real calibration.
    old_bytes = (root / ".novelagent/project.json").read_bytes()
    trusted = ChapterCandidateContextBuilder(root).build(2)
    state = json.loads((REPO / "tests/fixtures/chapter_fact_offline_v1/baseline.json").read_text(encoding="utf-8"))
    confirmation = baseline_request(book_id=old.book_id, chapter=1, state=state, sources=capture_sources(root),
                                    old_identity_sha256=digest(old_bytes), static_references=[])
    inputs = dict(trusted_context=trusted, baseline_state=state, confirmed_baseline=confirmation,
                  static_references=[], calibration_report=report, run_id="upgrade-1")
    return locals()


def prepared(case):
    return prepare_reviewed_upgrade(**case["inputs"])


def test_upgrade_preparation_does_not_write_or_change_old_identity(case):
    before = inventory(case["root"])
    p = prepared(case)
    new = validate_reviewed_identity(p["new_identity"])
    assert new.payload["schema_version"] == "3.0"
    assert new.book_id == case["old"].book_id
    assert new.payload["created_at"] == case["old"].created_at
    assert new.payload["activation"] == case["old"].activation
    assert new.authority["mode"] == "legacy_markdown_v1"
    assert new.authority["authority_epoch"] == 0 and new.authority["minimum_writer_contract"] == 2
    assert p["publication_authorized"] is False and inventory(case["root"]) == before


def test_actual_atomic_upgrade_and_new_process_receipt_verification(case):
    p = prepared(case)
    before = inventory(case["root"])
    tx = prepare_reviewed_upgrade_transaction(preparation=p)
    assert tx.commit()["committed"]
    result = load_verified_reviewed_identity(case["root"])
    assert result["identity"] == p["new_identity"]
    assert result["history"]["value"]["current_state"] == case["state"]
    after = inventory(case["root"])
    for path, binding in before.items():
        if path != ".novelagent/project.json":
            assert after[path] == binding
    # Ordinary entrypoints remain closed until the complete T3.2 writer is enabled.
    with pytest.raises(ProjectIdentityError):
        load_project_identity(case["root"])
    with pytest.raises(ProjectIdentityError):
        ChapterCandidateContextBuilder(case["root"]).build(2)
    code = "import json,sys; from pathlib import Path; from core.story_project.reviewed_identity import load_verified_reviewed_identity; print(json.dumps(load_verified_reviewed_identity(Path(sys.argv[1])),ensure_ascii=True))"
    child = subprocess.run([sys.executable, "-B", "-c", code, str(case["root"])], cwd=REPO,
                            capture_output=True, text=True, timeout=25, check=True)
    assert json.loads(child.stdout) == result


def test_existing_v2_legacy_strict_identity_upgrades_without_event_authority(case):
    root = case["root"]
    old_v2 = {**case["old"].to_dict(), "schema_version": "2.0", "root_hint": ".", "authority": case["old"].authority}
    validate_project_identity(old_v2)
    before = json_bytes(old_v2)
    (root / ".novelagent/project.json").write_bytes(before)
    confirmation = baseline_request(book_id=case["old"].book_id, chapter=1, state=case["state"], sources=capture_sources(root),
                                    old_identity_sha256=digest(before), static_references=[])
    p = prepare_reviewed_upgrade(**{**case["inputs"], "trusted_context": ChapterCandidateContextBuilder(root).build(2),
                                     "confirmed_baseline": confirmation})
    assert prepare_reviewed_upgrade_transaction(preparation=p).commit()["committed"]
    result = load_verified_reviewed_identity(root)
    assert result["identity"]["authority"] == {**old_v2["authority"], "minimum_writer_contract": 2}
    assert result["identity"]["reviewed_state"]["upgrade"]["old_identity_sha256"] == digest(before)


def test_cold_verifier_consumes_full_transaction_preparation_evidence(case, monkeypatch):
    import core.story_project.reviewed_identity as identity_module
    p = prepared(case)
    assert prepare_reviewed_upgrade_transaction(preparation=p).commit()["committed"]
    original = identity_module.verify_upgrade_preparation_evidence
    calls = []
    def observe(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return original(**kwargs)
    # Observe the call without replacing any receipt or semantic validation.
    monkeypatch.setattr(identity_module, "verify_upgrade_preparation_evidence", observe)
    load_verified_reviewed_identity(case["root"])
    assert len(calls) == 1 and calls[0]["preparation"] == p and calls[0]["read_set"] == p["read_set"]
    assert calls[0]["transaction_binding"]["context_digest"] == p["binding"]["context_sha256"]
    with pytest.raises(ReviewedIdentityError, match="preparation archive differs"):
        original(**{**calls[0], "preparation": {}})


@pytest.mark.parametrize("field,value", [("book_id", "foreign"), ("created_at", "changed"), ("root_hint", "elsewhere")])
def test_upgrade_cannot_change_stable_identity_fields(case, field, value):
    p = prepared(case)
    altered = copy.deepcopy(p["new_identity"])
    altered[field] = value
    with pytest.raises(ProjectIdentityError):
        verify_reviewed_identity_transition(old_identity_bytes=case["old_bytes"], new_identity_bytes=json_bytes(altered),
                                            baseline_document=p["baseline_document"].encode("utf-8"))


def test_missing_or_other_calibration_report_is_not_preserved_strict_proof(case):
    report = copy.deepcopy(case["report"])
    report["book_id"] = "another-book"
    from core.story_project.activation import calibration_report_sha256
    report["report_sha256"] = calibration_report_sha256(report)
    with pytest.raises(ReviewedUpgradeError, match="calibration report"):
        prepare_reviewed_upgrade(**{**case["inputs"], "calibration_report": report})


def test_existing_experimental_or_manual_state_cannot_be_silently_adopted(case):
    (case["root"] / RELATIVE_PATH).write_text("# 未知旧内容", encoding="utf-8")
    with pytest.raises(ReviewedUpgradeError, match="cannot adopt"):
        prepared(case)


def test_source_changed_after_preview_prevents_staging(case):
    p = prepared(case)
    (case["root"] / "追踪/上下文.md").write_text("# 并发改变", encoding="utf-8")
    with pytest.raises(StoryProjectSourceDriftError):
        prepare_reviewed_upgrade_transaction(preparation=p)
    assert not (case["root"] / ".novelagent/runtime/reviewed/p/journals/upgrade-1").exists()


def test_recomputed_hashes_do_not_hide_a_changed_preparation_binding(case):
    p = prepared(case)
    p["binding"]["context_sha256"] = "9" * 64
    p["request_sha256"] = digest({"version": p["version"], "binding": p["binding"]})
    p["preparation_sha256"] = digest({k: v for k, v in p.items() if k != "preparation_sha256"})
    with pytest.raises(ReviewedUpgradeError, match="current context changed"):
        prepare_reviewed_upgrade_transaction(preparation=p)
    assert not (case["root"] / ".novelagent/runtime").exists()


@pytest.mark.parametrize("mutation", ["missing_before_archive", "created_at", "untyped_same_epoch",
    "empty_preparation", "preparation_path", "final_request", "transaction_context"])
def test_actual_persistence_guard_rejects_invalid_upgrade_bundles(case, monkeypatch, mutation):
    p = prepared(case)
    original = PersistenceV2Transaction.prepare
    def changed(self, **kwargs):
        if mutation == "missing_before_archive":
            kwargs["artifacts"] = [t for t in kwargs["artifacts"] if t.target_id != "reviewed_identity_before"]
        elif mutation == "empty_preparation":
            kwargs["artifacts"] = [replace(t, content=b"{}") if t.target_id == "reviewed_upgrade_preparation" else t
                                   for t in kwargs["artifacts"]]
        elif mutation == "preparation_path":
            kwargs["artifacts"] = [replace(t, path_ref=replace(t.path_ref, relative_path="reviewed/upgrades/wrong.json"))
                                   if t.target_id == "reviewed_upgrade_preparation" else t for t in kwargs["artifacts"]]
        elif mutation == "final_request":
            kwargs["final_run_record"]["upgrade_request_sha256"] = "8" * 64
        elif mutation == "transaction_context":
            kwargs["context_digest"] = kwargs["generation_input_context_digest"] = "8" * 64
        else:
            target = next(t for t in kwargs["apply_targets"] if t.target_id == "reviewed_identity")
            payload = json.loads(target.content_bytes().decode("utf-8"))
            if mutation == "created_at":
                payload["created_at"] = "changed-after-confirmation"
            else:
                payload["schema_version"] = "2.0"
                payload.pop("reviewed_state")
            raw = json_bytes(payload)
            kwargs["apply_targets"] = [replace(t, content=raw) if t.target_id == "reviewed_identity" else t
                                       for t in kwargs["apply_targets"]]
            declaration = next(d for d in self.read_set_declared_writes if d.get("role") == "project_identity")
            declaration.update(after_sha256=digest(raw), after_size=len(raw))
            kwargs["story_project_source_revision_after"]["identity_sha256"] = digest(raw)
            if mutation == "untyped_same_epoch":
                declaration.pop("transition")
                declaration.pop("reviewed_state")
                kwargs["story_project_source_revision_after"].pop("reviewed_state")
        return original(self, **kwargs)
    monkeypatch.setattr(PersistenceV2Transaction, "prepare", changed)
    expected = "must advance" if mutation == "untyped_same_epoch" else "reviewed format upgrade binding invalid"
    with pytest.raises(PersistenceV2IntegrityError, match=expected):
        prepare_reviewed_upgrade_transaction(preparation=p)
    assert (case["root"] / ".novelagent/project.json").read_bytes() == case["old_bytes"]
    assert not (case["root"] / RELATIVE_PATH).exists()


def test_ordinary_read_set_cannot_treat_v3_as_an_undeclared_identity(case):
    p = prepared(case)
    tx = prepare_reviewed_upgrade_transaction(preparation=p)
    assert tx.commit()["committed"]
    with pytest.raises(StoryProjectSourceDriftError):
        verify_story_project_read_set(p["read_set"], phase="prepare")


@pytest.mark.parametrize("event,index", [("after_apply_target", 0), ("after_apply_target", 1), ("after_commit_marker", None)])
def test_upgrade_crash_restores_old_identity_or_completes_original_upgrade(case, event, index):
    p = prepared(case)
    tx = prepare_reviewed_upgrade_transaction(preparation=p)
    class PowerLoss(BaseException):
        pass
    hit = []
    def crash(name, position, path):
        if (name, position) == (event, index):
            hit.append(name)
            raise PowerLoss()
    tx._fault_injector = crash
    with pytest.raises(PowerLoss):
        tx.commit()
    assert hit
    result = reconcile_pending_persistence_v2(tx.transaction_root, expected_book_id=case["old"].book_id)
    assert result["ok"]
    if event == "after_commit_marker":
        assert load_verified_reviewed_identity(case["root"])["identity"] == p["new_identity"]
    else:
        assert (case["root"] / ".novelagent/project.json").read_bytes() == case["old_bytes"]
        assert not (case["root"] / RELATIVE_PATH).exists()
        assert load_project_identity(case["root"]).to_dict() == case["old"].to_dict()
