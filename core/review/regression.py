from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.quality import evaluate_chapter_quality
from core.review.report import build_human_review_report
from core.rules import (
    build_rule_repair_plan,
    build_rule_repair_prompt,
    validate_chapter_against_rules,
)
from core.schema import validate_schema


class ReviewRegressionError(ValueError):
    pass


def run_review_regression_suite(
    *,
    manifest_path: str | Path,
    artifacts_dir: str | Path | None = None,
) -> dict:
    manifest_file = Path(manifest_path)
    manifest = _load_json(manifest_file)
    validate_schema(manifest, "review_regression_manifest.schema.json")

    case_results: list[dict[str, Any]] = []
    artifact_root = Path(artifacts_dir) if artifacts_dir is not None else None
    if artifact_root is not None:
        artifact_root.mkdir(parents=True, exist_ok=True)

    for case in manifest["cases"]:
        case_result = run_review_regression_case(
            case=case,
            base_dir=manifest_file.parent,
            artifacts_dir=artifact_root,
        )
        case_results.append(case_result)

    failed = sum(1 for case in case_results if case["status"] == "fail")
    passed = len(case_results) - failed
    summary = {
        "schema_version": "1.0",
        "suite_id": manifest["suite_id"],
        "status": "fail" if failed else "pass",
        "summary": {
            "case_count": len(case_results),
            "passed": passed,
            "failed": failed,
        },
        "cases": case_results,
        "metadata": {
            "created_by": "NovelAgent",
            "source": "review-regression",
            "manifest_path": str(manifest_file),
        },
    }
    return validate_schema(summary, "review_regression_summary.schema.json")


def run_review_regression_case(
    *,
    case: dict,
    base_dir: str | Path,
    artifacts_dir: str | Path | None = None,
) -> dict:
    base = Path(base_dir)
    case_id = str(case["case_id"])
    snapshot = _load_json(_resolve(base, case["snapshot_path"]))
    previous_chapter_text = _resolve(base, case["previous_chapter_path"]).read_text(encoding="utf-8")
    chapter_text = _resolve(base, case["chapter_path"]).read_text(encoding="utf-8")
    expected = _load_json(_resolve(base, case["expected_path"]))

    quality_report = evaluate_chapter_quality(
        chapter_text=chapter_text,
        snapshot=copy.deepcopy(snapshot),
        previous_chapter_text=previous_chapter_text,
    )
    rule_validation_report = validate_chapter_against_rules(
        chapter_text=chapter_text,
        snapshot=copy.deepcopy(snapshot),
        previous_chapter_text=previous_chapter_text,
        quality_report=quality_report,
        use_default_rules=True,
    )
    rule_repair_plan = build_rule_repair_plan(
        rule_validation_report=rule_validation_report,
    )
    rule_repair_prompt = build_rule_repair_prompt(
        chapter_text=chapter_text,
        snapshot=copy.deepcopy(snapshot),
        previous_chapter_text=previous_chapter_text,
        rule_repair_plan=rule_repair_plan,
    )
    human_review_report = build_human_review_report(
        chapter_text=chapter_text,
        chapter_quality_report=quality_report,
        rule_validation_report=rule_validation_report,
        rule_repair_plan=rule_repair_plan,
        rule_repair_prompt_metadata=rule_repair_prompt["metadata"],
        title=f"Review Regression: {case_id}",
    )

    failed_expectations = evaluate_regression_expectations(
        case_id=case_id,
        expected=expected,
        quality_report=quality_report,
        rule_validation_report=rule_validation_report,
        rule_repair_plan=rule_repair_plan,
        human_review_metadata=human_review_report["metadata"],
    )

    if artifacts_dir is not None:
        _write_case_artifacts(
            case_id=case_id,
            artifacts_dir=Path(artifacts_dir),
            quality_report=quality_report,
            rule_validation_report=rule_validation_report,
            rule_repair_plan=rule_repair_plan,
            rule_repair_prompt=rule_repair_prompt,
            human_review_report=human_review_report,
        )

    decision = str(human_review_report["metadata"]["decision"]["decision"])
    return {
        "case_id": case_id,
        "status": "fail" if failed_expectations else "pass",
        "decision": decision,
        "quality_score": int(quality_report["score"]),
        "rule_score": int(rule_validation_report["score"]),
        "repair_task_count": int(rule_repair_plan["summary"]["task_count"]),
        "blocking_task_count": int(rule_repair_plan["summary"]["blocking_task_count"]),
        "failed_expectations": failed_expectations,
        "metadata": {
            "category": str(case.get("category") or ""),
            "quality_status": str(quality_report["status"]),
            "rule_validation_status": str(rule_validation_report["status"]),
            "repair_plan_status": str(rule_repair_plan["status"]),
        },
    }


def evaluate_regression_expectations(
    *,
    case_id: str,
    expected: dict,
    quality_report: dict,
    rule_validation_report: dict,
    rule_repair_plan: dict,
    human_review_metadata: dict,
) -> list[str]:
    failures: list[str] = []
    rule_statuses = {
        str(rule["code"]): str(rule["status"])
        for rule in rule_validation_report.get("rules", [])
        if isinstance(rule, dict)
    }
    repair_types = {
        str(task["repair_type"])
        for task in rule_repair_plan.get("tasks", [])
        if isinstance(task, dict)
    }
    has_blocking = any(
        bool(task.get("blocking"))
        for task in rule_repair_plan.get("tasks", [])
        if isinstance(task, dict)
    )
    decision = str(human_review_metadata["decision"]["decision"])

    if "expected_decision" in expected:
        allowed = _expected_values(expected["expected_decision"])
        if decision not in allowed:
            failures.append(_failure(case_id, f"decision {decision!r} not in {sorted(allowed)!r}"))

    _check_min_score(failures, case_id, "quality_score", int(quality_report["score"]), expected.get("min_quality_score"))
    _check_max_score(failures, case_id, "quality_score", int(quality_report["score"]), expected.get("max_quality_score"))
    _check_min_score(failures, case_id, "rule_score", int(rule_validation_report["score"]), expected.get("min_rule_score"))
    _check_max_score(failures, case_id, "rule_score", int(rule_validation_report["score"]), expected.get("max_rule_score"))

    for rule_code in _expected_list(expected.get("expected_fail_rules")):
        if rule_statuses.get(rule_code) != "fail":
            failures.append(_failure(case_id, f"rule {rule_code!r} expected fail, got {rule_statuses.get(rule_code)!r}"))
    for rule_code in _expected_list(expected.get("expected_warning_rules")):
        if rule_statuses.get(rule_code) != "warning":
            failures.append(_failure(case_id, f"rule {rule_code!r} expected warning, got {rule_statuses.get(rule_code)!r}"))
    for rule_code in _expected_list(expected.get("expected_fail_or_warning_rules")):
        if rule_statuses.get(rule_code) not in {"fail", "warning"}:
            failures.append(_failure(case_id, f"rule {rule_code!r} expected fail/warning, got {rule_statuses.get(rule_code)!r}"))
    for rule_code in _expected_list(expected.get("forbidden_fail_rules")):
        if rule_statuses.get(rule_code) == "fail":
            failures.append(_failure(case_id, f"rule {rule_code!r} must not fail"))
    for repair_type in _expected_list(expected.get("expected_repair_types")):
        if repair_type not in repair_types:
            failures.append(_failure(case_id, f"repair_type {repair_type!r} was not produced"))

    if "expected_blocking" in expected and has_blocking is not bool(expected["expected_blocking"]):
        failures.append(_failure(case_id, f"blocking expected {bool(expected['expected_blocking'])}, got {has_blocking}"))

    return failures


def _write_case_artifacts(
    *,
    case_id: str,
    artifacts_dir: Path,
    quality_report: dict,
    rule_validation_report: dict,
    rule_repair_plan: dict,
    rule_repair_prompt: dict,
    human_review_report: dict,
) -> None:
    case_dir = artifacts_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_json(case_dir / "chapter_quality_report.json", quality_report)
    _write_json(case_dir / "rule_validation_report.json", rule_validation_report)
    _write_json(case_dir / "rule_repair_plan.json", rule_repair_plan)
    (case_dir / "rule_repair_prompt.md").write_text(rule_repair_prompt["prompt"], encoding="utf-8")
    _write_json(case_dir / "rule_repair_prompt_metadata.json", rule_repair_prompt["metadata"])
    (case_dir / "human_review_report.md").write_text(human_review_report["markdown"], encoding="utf-8")
    _write_json(case_dir / "human_review_report_metadata.json", human_review_report["metadata"])


def _resolve(base_dir: Path, path_value: Any) -> Path:
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    return base_dir / path


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ReviewRegressionError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _expected_values(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value}
    return {str(value)}


def _expected_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _check_min_score(failures: list[str], case_id: str, field: str, actual: int, expected: Any) -> None:
    if expected is not None and actual < int(expected):
        failures.append(_failure(case_id, f"{field} expected >= {int(expected)}, got {actual}"))


def _check_max_score(failures: list[str], case_id: str, field: str, actual: int, expected: Any) -> None:
    if expected is not None and actual > int(expected):
        failures.append(_failure(case_id, f"{field} expected <= {int(expected)}, got {actual}"))


def _failure(case_id: str, message: str) -> str:
    return f"{case_id}: {message}"


__all__ = [
    "ReviewRegressionError",
    "evaluate_regression_expectations",
    "run_review_regression_case",
    "run_review_regression_suite",
]
