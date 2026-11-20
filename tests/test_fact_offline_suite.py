from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from core.quality.fact_offline_suite import run_fact_offline_suite


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tests/fixtures/chapter_fact_offline_v1"


def test_offline_suite_preserves_unchecked_semantic_cases_instead_of_counting_them_as_passes():
    report = run_fact_offline_suite(SUITE)
    assert report["regressions"] == 0
    assert report["case_count"] == 12 and report["verified"] == 8
    gaps = {item["id"]: item for item in report["results"] if item["outcome"] == "policy_gap"}
    assert set(gaps) == {"omitted_inventory", "empty_extraction_with_changes", "ambiguous_nickname",
                         "unexplained_location_contradiction"}
    assert all(item["observed"]["disposition"] == "accept" for item in gaps.values())
    assert report["paid_provider_calls"] == 0
    assert not report["human_quality_scored"] and not report["model_effectiveness_evaluated"]
    assert report["local_sources"] == []  # Portable CI does not access private book paths.


def test_offline_fixture_changed_without_manifest_update_is_rejected(tmp_path):
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite)
    case = suite / "cases/seven_kinds.json"
    value = json.loads(case.read_text(encoding="utf-8"))
    value["material"]["proposal"]["facts"] = []
    case.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        run_fact_offline_suite(suite)


def test_complete_policy_gate_fails_while_preserving_the_readable_gap_report(tmp_path):
    output = tmp_path / "policy-report.json"
    completed = subprocess.run([sys.executable, "-B", "scripts/check_fact_offline_suite.py",
                                "--output", str(output), "--require-policy-complete"],
                               cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 1, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["regressions"] == 0 and report["policy_gaps"] == 4
