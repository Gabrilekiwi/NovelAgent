from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from core.quality import evaluate_chapter_quality
from core.rules.narrative_rules import (
    load_default_narrative_rule_pack,
    load_narrative_rule_pack,
    validate_narrative_rule_pack,
)
from core.schema import validate_schema


SEVERITY_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

FAIL_DEDUCTIONS = {
    "critical": 35,
    "high": 25,
    "medium": 15,
    "low": 5,
}

WARNING_DEDUCTIONS = {
    "critical": 20,
    "high": 12,
    "medium": 8,
    "low": 3,
}


class RuleValidationError(ValueError):
    pass


def validate_chapter_against_rules(
    *,
    chapter_text: str,
    snapshot: dict,
    rule_pack: dict | None = None,
    rule_pack_path: str | Path | None = None,
    use_default_rules: bool = False,
    previous_chapter_text: str | None = None,
    quality_report: dict | None = None,
    applies_to: str = "validation",
    min_severity: str | None = None,
    categories: list[str] | None = None,
) -> dict:
    snapshot_copy = copy.deepcopy(snapshot)
    selected_pack = _resolve_rule_pack(
        rule_pack=copy.deepcopy(rule_pack) if rule_pack is not None else None,
        rule_pack_path=rule_pack_path,
        use_default_rules=use_default_rules,
    )
    quality = (
        validate_schema(copy.deepcopy(quality_report), "chapter_quality_report.schema.json")
        if quality_report is not None
        else evaluate_chapter_quality(
            chapter_text=chapter_text,
            snapshot=snapshot_copy,
            previous_chapter_text=previous_chapter_text,
        )
    )

    rules = _select_rules(
        selected_pack,
        applies_to=applies_to,
        min_severity=min_severity,
        categories=categories,
    )
    checks_by_code = {str(check["code"]): check for check in quality["checks"]}
    rule_results = [_validate_rule(rule, checks_by_code) for rule in rules]
    summary = _summary(rule_results)
    score = _score_rules(rule_results)
    status = _overall_status(rule_results, score)
    violations = [_violation(rule) for rule in rule_results if rule["status"] in {"fail", "warning"}]

    report = {
        "schema_version": "1.0",
        "status": status,
        "score": score,
        "summary": summary,
        "rule_pack": {
            "rule_pack_id": selected_pack["rule_pack_id"],
            "version": selected_pack["version"],
            "language": selected_pack["language"],
        },
        "quality_report": {
            "status": quality["status"],
            "score": quality["score"],
            "check_count": len(quality["checks"]),
        },
        "rules": rule_results,
        "violations": violations,
        "metadata": {
            "created_by": "NovelAgent",
            "source": "rule-aware-validation",
            "ready_for_next_flow": status != "fail",
        },
    }
    return validate_schema(report, "rule_validation_report.schema.json")


def _resolve_rule_pack(
    *,
    rule_pack: dict | None,
    rule_pack_path: str | Path | None,
    use_default_rules: bool,
) -> dict[str, Any]:
    if rule_pack is not None:
        return validate_narrative_rule_pack(rule_pack)
    if rule_pack_path is not None:
        return load_narrative_rule_pack(rule_pack_path)
    if use_default_rules:
        return load_default_narrative_rule_pack()
    raise RuleValidationError("rule pack is required; pass rule_pack, rule_pack_path, or use_default_rules=True")


def _select_rules(
    rule_pack: dict[str, Any],
    *,
    applies_to: str,
    min_severity: str | None,
    categories: list[str] | None,
) -> list[dict[str, Any]]:
    validated = validate_narrative_rule_pack(rule_pack)
    min_rank = _severity_rank(min_severity) if min_severity else None
    allowed_categories = {str(category) for category in categories} if categories else None
    selected: list[dict[str, Any]] = []
    for rule in validated["rules"]:
        if rule.get("enabled") is not True:
            continue
        if applies_to and applies_to not in (rule.get("applies_to") or []):
            continue
        if allowed_categories is not None and rule.get("category") not in allowed_categories:
            continue
        if min_rank is not None and _severity_rank(str(rule.get("severity"))) < min_rank:
            continue
        selected.append(rule)
    return selected


def _validate_rule(rule: dict[str, Any], checks_by_code: dict[str, dict[str, Any]]) -> dict[str, Any]:
    quality_check_codes = [str(code) for code in rule.get("quality_check_codes") or [] if str(code).strip()]
    matched_quality_checks = [checks_by_code[code] for code in quality_check_codes if code in checks_by_code]
    reason: str | None = None

    if not quality_check_codes:
        status = "skip"
        reason = "no_quality_check_mapping"
    elif len(matched_quality_checks) != len(quality_check_codes):
        status = "skip"
        reason = "quality_check_missing"
    else:
        check_statuses = [str(check["status"]) for check in matched_quality_checks]
        if all(check_status == "skip" for check_status in check_statuses):
            status = "skip"
            reason = "all_quality_checks_skipped"
        elif "fail" in check_statuses:
            status = "fail"
        elif "warning" in check_statuses:
            status = "warning"
        elif all(check_status == "pass" for check_status in check_statuses):
            status = "pass"
        else:
            status = "skip"
            reason = "all_quality_checks_skipped"

    return {
        "code": rule["code"],
        "title": rule["title"],
        "category": rule["category"],
        "severity": rule["severity"],
        "status": status,
        "quality_check_codes": quality_check_codes,
        "matched_quality_checks": matched_quality_checks,
        "reason": reason,
    }


def _violation(rule_result: dict[str, Any]) -> dict[str, Any]:
    status = str(rule_result["status"])
    quality_codes = [str(code) for code in rule_result["quality_check_codes"]]
    return {
        "rule_code": rule_result["code"],
        "status": status,
        "severity": rule_result["severity"],
        "category": rule_result["category"],
        "message": (
            f"Rule {status} because mapped quality check(s) "
            f"{', '.join(quality_codes) if quality_codes else 'none'} {status}."
        ),
        "quality_check_codes": quality_codes,
    }


def _summary(rule_results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for rule in rule_results if rule["status"] == "pass"),
        "warnings": sum(1 for rule in rule_results if rule["status"] == "warning"),
        "failed": sum(1 for rule in rule_results if rule["status"] == "fail"),
        "skipped": sum(1 for rule in rule_results if rule["status"] == "skip"),
    }


def _score_rules(rule_results: list[dict[str, Any]]) -> int:
    score = 100
    for rule in rule_results:
        severity = str(rule.get("severity") or "low")
        if rule["status"] == "fail":
            score -= FAIL_DEDUCTIONS.get(severity, 5)
        elif rule["status"] == "warning":
            score -= WARNING_DEDUCTIONS.get(severity, 3)
    return max(0, min(100, score))


def _overall_status(rule_results: list[dict[str, Any]], score: int) -> str:
    if any(rule["status"] == "fail" and rule["severity"] in {"critical", "high"} for rule in rule_results):
        return "fail"
    if score < 60:
        return "fail"
    if any(rule["status"] == "warning" for rule in rule_results) or score < 85:
        return "warning"
    return "pass"


def _severity_rank(severity: str | None) -> int:
    if severity not in SEVERITY_RANK:
        raise RuleValidationError(f"unsupported severity: {severity}")
    return SEVERITY_RANK[severity]


__all__ = [
    "RuleValidationError",
    "validate_chapter_against_rules",
]
