from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SEVERITY_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class RepairNotConvergingError(RuntimeError):
    code = "chapter_repair_not_converging"

    def __init__(
        self,
        *,
        history: list[dict[str, Any]],
        transition: dict[str, Any],
    ) -> None:
        self.history = [dict(item) for item in history]
        self.transition = dict(transition)
        counts = " -> ".join(str(item["problem_count"]) for item in history)
        repeated = ", ".join(transition.get("repeated_problem_fingerprints") or []) or "none"
        super().__init__(
            f"{self.code}: repair validation did not converge; "
            f"problem_counts={counts}; reason={transition['reason']}; "
            f"repeated_problems={repeated}"
        )


def build_validation_checkpoint(
    validation: dict[str, Any],
    *,
    chapter_text: str,
) -> dict[str, Any]:
    problems = _validation_problems(validation)
    normalized = [_problem_summary(problem) for problem in problems]
    severity_counts = {
        severity: sum(item["severity"] == severity for item in normalized)
        for severity in ("critical", "high", "medium", "low")
    }
    blocking_count = sum(item["blocking"] for item in normalized)
    payload = {
        "ok": bool(validation.get("ok")),
        "problem_count": len(normalized),
        "blocking_problem_count": blocking_count,
        "severity_counts": severity_counts,
        "problem_codes": [item["code"] for item in normalized],
        "problem_fingerprints": [item["fingerprint"] for item in normalized],
        "problems": normalized,
        "chapter_sha256": hashlib.sha256(chapter_text.encode("utf-8")).hexdigest(),
    }
    payload["checkpoint_id"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def compare_validation_checkpoints(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    before_count = int(before.get("problem_count") or 0)
    after_count = int(after.get("problem_count") or 0)
    before_fingerprints = set(before.get("problem_fingerprints") or [])
    after_fingerprints = set(after.get("problem_fingerprints") or [])
    repeated = sorted(before_fingerprints & after_fingerprints)
    new_problems = [
        item
        for item in after.get("problems") or []
        if item.get("fingerprint") not in before_fingerprints
    ]
    before_max_severity = max(
        (_SEVERITY_RANK.get(str(item.get("severity") or "medium"), 2) for item in before.get("problems") or []),
        default=0,
    )
    new_critical = any(item.get("severity") == "critical" for item in new_problems)
    new_more_severe = any(
        _SEVERITY_RANK.get(str(item.get("severity") or "medium"), 2) > before_max_severity
        for item in new_problems
    )

    if bool(after.get("ok")):
        status = "passed"
        reason = "validation_passed"
    elif after_count > before_count:
        status = "regressed"
        reason = "problem_count_increased"
    elif after_count == before_count:
        status = "stalled"
        reason = "problem_count_not_reduced"
    elif new_critical:
        status = "regressed"
        reason = "new_critical_problem"
    elif new_more_severe:
        status = "regressed"
        reason = "new_more_severe_problem"
    else:
        status = "improved"
        reason = "problem_count_reduced"

    transition = {
        "status": status,
        "reason": reason,
        "before_problem_count": before_count,
        "after_problem_count": after_count,
        "repeated_problem_fingerprints": repeated,
        "new_problem_fingerprints": sorted(
            str(item.get("fingerprint") or "") for item in new_problems if item.get("fingerprint")
        ),
        "new_critical_problem": new_critical,
        "new_more_severe_problem": new_more_severe,
        "eligible_for_elastic_budget": status == "improved",
    }
    transition["authorization_id"] = hashlib.sha256(
        (
            f"{before.get('checkpoint_id', '')}:{after.get('checkpoint_id', '')}:"
            f"{status}:{reason}"
        ).encode("utf-8")
    ).hexdigest()
    return transition


def validation_quality_score(checkpoint: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    severities = checkpoint.get("severity_counts") or {}
    return (
        0 if checkpoint.get("ok") else 1,
        int(severities.get("critical") or 0),
        int(severities.get("high") or 0),
        int(severities.get("medium") or 0),
        int(checkpoint.get("blocking_problem_count") or 0),
        int(checkpoint.get("problem_count") or 0),
    )


def _validation_problems(validation: dict[str, Any]) -> list[dict[str, Any]]:
    problems = validation.get("problems")
    if isinstance(problems, list):
        return [dict(item) for item in problems if isinstance(item, dict)]
    flattened: list[dict[str, Any]] = []
    for check in validation.get("checks") or []:
        if not isinstance(check, dict) or not isinstance(check.get("problems"), list):
            continue
        flattened.extend(dict(item) for item in check["problems"] if isinstance(item, dict))
    return flattened


def _problem_summary(problem: dict[str, Any]) -> dict[str, Any]:
    code = str(problem.get("code") or "unknown_problem")
    validator = str(problem.get("validator") or "unknown")
    area = str(problem.get("area") or "")
    message = _normalize_message(str(problem.get("message") or ""))
    severity = str(problem.get("severity") or "medium").lower()
    if severity not in _SEVERITY_RANK:
        severity = "medium"
    # Validator codes and areas are intentionally primary: an LLM may reword
    # the same finding between passes, which must not disguise a stalled repair.
    fingerprint_parts = [validator, code, area]
    if code == "unknown_problem":
        fingerprint_parts.append(message)
    fingerprint_source = "|".join(fingerprint_parts)
    return {
        "code": code,
        "validator": validator,
        "area": area,
        "severity": severity,
        "blocking": bool(problem.get("blocking", True)),
        "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:20],
    }


def _normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message.strip().lower())[:500]


__all__ = [
    "RepairNotConvergingError",
    "build_validation_checkpoint",
    "compare_validation_checkpoints",
    "validation_quality_score",
]
