from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.story_project.identity import ensure_project_identity
from core.story_project.semantic_parser import parse_story_project_semantic_state
from scripts.story_state_calibration_audit import (
    _measure_authoritative_text,
    _measure_optional_context_recall,
    collect_calibration_audit,
    write_calibration_audit,
)


def test_calibration_audit_collects_source_bound_observation_without_qualifying_strict(
    tmp_path: Path,
) -> None:
    source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
    book = tmp_path / "book"
    shutil.copytree(source, book)
    ensure_project_identity(book)

    audit = collect_calibration_audit(book, start_chapter=2, chapter_count=1)

    assert audit["book_id"]
    assert audit["chapter_range"] == {"start": 2, "end": 2, "count": 1}
    assert audit["observations"][0]["mode"] == "shadow"
    assert audit["observations"][0]["authoritative"] is False
    assert audit["calibration_evidence"]["target_sample_count"] == 1
    assert audit["calibration_evidence"]["format_variant_count"] >= 2
    assert audit["automated_measurements"]["provenance_span_integrity_rate"] == 1.0
    assert audit["managed_round_trip"]["ok"] is True
    assert audit["qualification"]["strict_eligible"] is False
    assert "consecutive_shadow_chapters_below_10" in audit["qualification"]["blockers"]

    destination = write_calibration_audit(tmp_path / "audit.json", audit)
    persisted = json.loads(destination.read_text(encoding="utf-8"))
    assert persisted["audit_sha256"] == audit["audit_sha256"]


def test_authoritative_text_measurement_detects_a_value_not_present_in_source(tmp_path: Path) -> None:
    source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
    book = tmp_path / "book"
    shutil.copytree(source, book)
    identity = ensure_project_identity(book)
    state = parse_story_project_semantic_state(book, 2, project_identity=identity)

    baseline = _measure_authoritative_text(state, book)
    assert baseline["expected_count"] > 0
    assert baseline["matched_count"] == baseline["expected_count"]

    state["story_state"]["current_location"] = "不存在的校准地点"
    altered = _measure_authoritative_text(state, book)
    assert altered["matched_count"] < altered["expected_count"]


def test_optional_context_measurement_ignores_legacy_writeback_blocks(tmp_path: Path) -> None:
    source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
    book = tmp_path / "book"
    shutil.copytree(source, book)
    identity = ensure_project_identity(book)
    state = parse_story_project_semantic_state(book, 2, project_identity=identity)
    context_path = book / "追踪" / "上下文.md"
    context_path.write_text(
        context_path.read_text(encoding="utf-8")
        + "\n<!-- NovelAgent:story_project_writeback -->\n- run_id: legacy-run\n",
        encoding="utf-8",
    )

    measured = _measure_optional_context_recall(state, book)

    assert measured["expected_count"] == 5
    assert measured["matched_count"] == 5
