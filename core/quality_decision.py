from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Iterable

from core.schema import validate_schema


QUALITY_DECISION_SCHEMA_VERSION = "1.0"
QUALITY_FINDING_IDENTITY_VERSION = "1.0"
QUALITY_POLICY_VERSION = "1.0"

SEVERITY_ORDER = ("info", "warning", "needs_revision", "blocking")
SEVERITY_RANK = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}


@dataclass(frozen=True)
class QualityPolicy:
    name: str
    version: str
    threshold: str
    max_repair_attempts: int
    include_review: bool
    llm_validator_required: bool

    def to_dict(self) -> dict[str, Any]:
        producers = ["base_validation", "blueprint_coverage"]
        if self.include_review:
            producers.extend(["deterministic_review", "narrative_rules"])
        if self.llm_validator_required:
            producers.append("llm_validator")
        return {
            "name": self.name,
            "version": self.version,
            "threshold": self.threshold,
            "max_repair_attempts": self.max_repair_attempts,
            "include_review": self.include_review,
            "llm_validator_required": self.llm_validator_required,
            "producers": producers,
        }

    def with_overrides(
        self,
        *,
        threshold: str | None = None,
        include_review: bool | None = None,
    ) -> QualityPolicy:
        if threshold is not None and threshold not in SEVERITY_RANK:
            raise ValueError(f"unsupported quality threshold: {threshold}")
        return replace(
            self,
            threshold=threshold or self.threshold,
            include_review=self.include_review if include_review is None else include_review,
        )


QUALITY_POLICIES = {
    "minimal": QualityPolicy("minimal", QUALITY_POLICY_VERSION, "blocking", 1, False, False),
    "standard": QualityPolicy("standard", QUALITY_POLICY_VERSION, "needs_revision", 2, True, False),
    # Warnings are advisory for every policy.  Strict mode still requires the
    # LLM producer, but it cannot turn a warning-only chapter into a rejection.
    "strict": QualityPolicy("strict", QUALITY_POLICY_VERSION, "needs_revision", 3, True, True),
}


def resolve_quality_policy(value: str | QualityPolicy | dict[str, Any]) -> QualityPolicy:
    if isinstance(value, QualityPolicy):
        return value
    if isinstance(value, str):
        try:
            return QUALITY_POLICIES[value]
        except KeyError as exc:
            raise ValueError(f"unsupported quality policy: {value}") from exc
    if not isinstance(value, dict):
        raise TypeError("quality policy must be a policy name, QualityPolicy, or mapping")
    base = resolve_quality_policy(str(value.get("name") or "minimal"))
    threshold = str(value.get("threshold") or base.threshold)
    if threshold not in SEVERITY_RANK:
        raise ValueError(f"unsupported quality threshold: {threshold}")
    return QualityPolicy(
        name=base.name,
        version=str(value.get("version") or base.version),
        threshold=threshold,
        max_repair_attempts=_bounded_int(value.get("max_repair_attempts"), base.max_repair_attempts),
        include_review=bool(value.get("include_review", base.include_review)),
        llm_validator_required=bool(value.get("llm_validator_required", base.llm_validator_required)),
    )


def build_quality_decision(
    *,
    policy: str | QualityPolicy | dict[str, Any],
    validation: dict[str, Any] | None = None,
    chapter_quality_report: dict[str, Any] | None = None,
    rule_validation_report: dict[str, Any] | None = None,
    upstream_decisions: Iterable[dict[str, Any]] = (),
    review_pipeline: dict[str, Any] | None = None,
    chapter_index: int | None = None,
    llm_validator_metadata: dict[str, Any] | None = None,
    source_artifacts: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    resolved_policy = resolve_quality_policy(policy)
    coverage = _coverage(validation)
    candidates: list[dict[str, Any]] = []
    executed_producers: set[str] = set()
    if isinstance(validation, dict):
        executed_producers.add("base_validation")
        if "story_project" in coverage["executed_checks"]:
            executed_producers.add("blueprint_coverage")
        if "llm" in coverage["executed_checks"]:
            executed_producers.add("llm_validator")
        candidates.extend(_validation_candidates(validation, chapter_index=chapter_index))
    if resolved_policy.include_review:
        review_producers: set[str] = set()
        if isinstance(chapter_quality_report, dict):
            review_producers.add("deterministic_review")
            executed_producers.add("deterministic_review")
            candidates.extend(_quality_report_candidates(chapter_quality_report, chapter_index=chapter_index))
        if isinstance(rule_validation_report, dict):
            review_producers.add("narrative_rules")
            executed_producers.add("narrative_rules")
            candidates.extend(_rule_report_candidates(rule_validation_report, chapter_index=chapter_index))
        upstream_added = False
        for decision in upstream_decisions:
            if not isinstance(decision, dict):
                continue
            upstream_added = True
            review_producers.update(
                producer
                for producer in decision.get("producers") or []
                if producer in {"deterministic_review", "narrative_rules"}
            )
            executed_producers.update(str(producer) for producer in decision.get("producers") or [])
            candidates.extend(_upstream_candidates(decision))
            coverage = _merge_coverage(coverage, decision.get("validation_coverage"))
        if not upstream_added and isinstance(review_pipeline, dict):
            aggregate = _review_pipeline_candidate(review_pipeline, chapter_index=chapter_index)
            if aggregate is not None:
                candidates.append(aggregate)
            if review_pipeline.get("status") != "error":
                review_producers.update({"deterministic_review", "narrative_rules"})
                executed_producers.update({"deterministic_review", "narrative_rules"})
        for missing_producer in sorted(
            {"deterministic_review", "narrative_rules"} - review_producers
        ):
            candidates.append(
                _candidate(
                    producer=missing_producer,
                    code=f"{missing_producer}_unavailable",
                    category="quality_evidence_availability",
                    severity="blocking",
                    subject="chapter",
                    predicate=f"{missing_producer}_available",
                    time_range=_chapter_time_range(chapter_index),
                    evidence=[{"kind": "policy", "value": resolved_policy.name}],
                    source_artifact="quality_policy",
                    repair_action="manual_review",
                    repair_parameters={},
                    validation_coverage=coverage,
                )
            )

    llm_executed = "llm" in coverage["executed_checks"]
    llm_metadata = _llm_metadata(
        required=resolved_policy.llm_validator_required,
        executed=llm_executed,
        supplied=(
            llm_validator_metadata
            if isinstance(llm_validator_metadata, dict)
            else _llm_metadata_from_validation(validation)
        ),
    )
    if resolved_policy.llm_validator_required and not llm_metadata["available"]:
        candidates.append(
            _candidate(
                producer="llm_validator",
                code="llm_validator_unavailable",
                category="validator_availability",
                severity="blocking",
                subject="chapter",
                predicate="llm_validator_available",
                time_range=_chapter_time_range(chapter_index),
                evidence=[{"kind": "policy", "value": resolved_policy.name}],
                source_artifact="quality_policy",
                repair_action="manual_review",
                repair_parameters={},
                validation_coverage=coverage,
            )
        )

    findings = _merge_findings(
        candidates,
        policy_version=resolved_policy.version,
        source_artifacts=source_artifacts or {},
    )
    threshold_rank = SEVERITY_RANK[resolved_policy.threshold]
    accepted = not any(SEVERITY_RANK[item["severity"]] >= threshold_rank for item in findings)
    max_severity = max(
        (item["severity"] for item in findings),
        key=lambda item: SEVERITY_RANK[item],
        default="info",
    )
    payload = {
        "schema_version": QUALITY_DECISION_SCHEMA_VERSION,
        "policy": resolved_policy.to_dict(),
        "accepted": accepted,
        "status": "accepted" if accepted else ("blocked" if max_severity == "blocking" else "needs_revision"),
        "max_severity": max_severity,
        "findings": findings,
        "finding_ids": [item["id"] for item in findings],
        "blocking_finding_ids": [
            item["id"] for item in findings if SEVERITY_RANK[item["severity"]] >= threshold_rank
        ],
        "manual_review_finding_ids": [
            item["id"] for item in findings if item["repair"]["action"] == "manual_review"
        ],
        "validation_coverage": coverage,
        "producers": sorted(
            executed_producers
            | {
                evidence["producer"]
                for item in findings
                for evidence in item["producer_evidence"]
            }
        ),
        "llm_validator": llm_metadata,
        "decision_digest": "",
    }
    payload["decision_digest"] = _digest({key: value for key, value in payload.items() if key != "decision_digest"})
    return validate_schema(payload, "quality_decision.schema.json")


def quality_decision_accepted(decision: dict[str, Any]) -> bool:
    return bool(validate_schema(decision, "quality_decision.schema.json")["accepted"])


def quality_decision_review_status(decision: dict[str, Any]) -> str:
    validated = validate_schema(decision, "quality_decision.schema.json")
    maximum = str(validated["max_severity"])
    return {
        "info": "pass",
        "warning": "warning",
        "needs_revision": "needs_revision",
        "blocking": "blocked",
    }[maximum]


def _validation_candidates(validation: dict[str, Any], *, chapter_index: int | None) -> list[dict[str, Any]]:
    coverage = _coverage(validation)
    result: list[dict[str, Any]] = []
    for problem in validation.get("problems") or []:
        if not isinstance(problem, dict):
            continue
        code = str(problem.get("code") or "validation_problem")
        validator = str(problem.get("validator") or "base_validation")
        producer = (
            "blueprint_coverage"
            if validator == "story_project" or code.startswith("missing_required_beat") or code == "missing_ending_pressure"
            else "llm_validator"
            if validator == "llm"
            else "base_validation"
        )
        result.append(
            _candidate(
                producer=producer,
                code=code,
                category=str(problem.get("category") or _code_family(code)),
                severity="blocking" if problem.get("blocking") else "warning",
                subject=_canonical_subject(problem),
                predicate=str(problem.get("predicate") or _code_family(code)),
                time_range=str(problem.get("time_range") or _chapter_time_range(chapter_index)),
                evidence=_evidence(problem.get("evidence"), message=problem.get("message")),
                source_artifact="validation_result",
                repair_action=str(problem.get("repair_action") or "manual_review"),
                repair_parameters=problem.get("repair_parameters") if isinstance(problem.get("repair_parameters"), dict) else {},
                validation_coverage=coverage,
            )
        )
    return result


def _quality_report_candidates(report: dict[str, Any], *, chapter_index: int | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for check in report.get("checks") or []:
        if not isinstance(check, dict) or check.get("status") not in {"warning", "fail"}:
            continue
        code = str(check.get("code") or "deterministic_review")
        result.append(
            _candidate(
                producer="deterministic_review",
                code=code,
                category=_code_family(code),
                severity=_report_severity(str(check.get("status")), str(check.get("severity") or "medium")),
                subject="chapter",
                predicate=_code_family(code),
                time_range=_chapter_time_range(chapter_index),
                evidence=_evidence(check.get("evidence"), message=check.get("message")),
                source_artifact="chapter_quality_report",
                repair_action="manual_review",
                repair_parameters={},
                validation_coverage=_empty_coverage(),
            )
        )
    return result


def _rule_report_candidates(report: dict[str, Any], *, chapter_index: int | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for violation in report.get("violations") or []:
        if not isinstance(violation, dict):
            continue
        code = str(violation.get("rule_code") or "narrative_rule")
        result.append(
            _candidate(
                producer="narrative_rules",
                code=code,
                category=str(violation.get("category") or _code_family(code)),
                severity=_report_severity(str(violation.get("status") or "warning"), str(violation.get("severity") or "medium")),
                subject="chapter",
                predicate=_code_family(code),
                time_range=_chapter_time_range(chapter_index),
                evidence=_evidence(
                    {"quality_check_codes": violation.get("quality_check_codes") or []},
                    message=violation.get("message"),
                ),
                source_artifact="rule_validation_report",
                repair_action="manual_review",
                repair_parameters={},
                validation_coverage=_empty_coverage(),
            )
        )
    return result


def _upstream_candidates(decision: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for finding in decision.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        canonical = finding.get("canonical") if isinstance(finding.get("canonical"), dict) else {}
        repair = finding.get("repair") if isinstance(finding.get("repair"), dict) else {}
        producer_evidence = finding.get("producer_evidence") or []
        if not isinstance(producer_evidence, list) or not producer_evidence:
            producer_evidence = [{"producer": "deterministic_review", "code": "upstream_finding", "evidence": []}]
        for evidence in producer_evidence:
            if not isinstance(evidence, dict):
                continue
            result.append(
                _candidate(
                    producer=str(evidence.get("producer") or "deterministic_review"),
                    code=str(evidence.get("code") or (finding.get("codes") or ["upstream_finding"])[0]),
                    category=str(finding.get("category") or finding.get("code_family") or "review"),
                    severity=str(finding.get("severity") or "needs_revision"),
                    subject=str(canonical.get("subject") or "chapter"),
                    predicate=str(canonical.get("predicate") or finding.get("code_family") or "review"),
                    time_range=str(canonical.get("time_range") or "current_chapter"),
                    evidence=evidence.get("evidence") if isinstance(evidence.get("evidence"), list) else [],
                    source_artifact=str(evidence.get("source_artifact") or "review_pipeline"),
                    repair_action=str(repair.get("action") or "manual_review"),
                    repair_parameters=repair.get("parameters") if isinstance(repair.get("parameters"), dict) else {},
                    validation_coverage=finding.get("validation_coverage") if isinstance(finding.get("validation_coverage"), dict) else _empty_coverage(),
                )
            )
    return result


def _review_pipeline_candidate(review: dict[str, Any], *, chapter_index: int | None) -> dict[str, Any] | None:
    status = str(review.get("status") or "")
    if status in {"", "pass", "warning"}:
        if status != "warning":
            return None
        severity = "warning"
    elif status == "needs_revision":
        severity = "needs_revision"
    else:
        severity = "blocking"
    return _candidate(
        producer="deterministic_review",
        code="review_pipeline_" + (status or "error"),
        category="review_pipeline",
        severity=severity,
        subject="chapter",
        predicate="review_pipeline_status",
        time_range=_chapter_time_range(chapter_index),
        evidence=_evidence({"status": status, "decision": review.get("decision")}, message=review.get("error")),
        source_artifact=str(review.get("summary_path") or "review_pipeline"),
        repair_action="manual_review",
        repair_parameters={},
        validation_coverage=_empty_coverage(),
    )


def _candidate(**values: Any) -> dict[str, Any]:
    return values


def _merge_findings(
    candidates: list[dict[str, Any]],
    *,
    policy_version: str,
    source_artifacts: dict[str, str | None],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        severity = str(candidate.get("severity") or "needs_revision")
        if severity not in SEVERITY_RANK:
            severity = "needs_revision"
        code = str(candidate.get("code") or "quality_finding")
        family = _code_family(code)
        canonical = {
            "subject": _normalized_identity_text(candidate.get("subject") or "chapter"),
            "predicate": _normalized_identity_text(candidate.get("predicate") or family),
            "time_range": _normalized_identity_text(candidate.get("time_range") or "current_chapter"),
        }
        identity = {
            "identity_version": QUALITY_FINDING_IDENTITY_VERSION,
            "policy_version": policy_version,
            "subject": canonical["subject"],
            "predicate": canonical["predicate"],
            "time_range": canonical["time_range"],
            "code_family": family,
        }
        finding_id = "qf:v1:" + _digest(identity)
        producer = str(candidate.get("producer") or "unknown")
        source_artifact = str(source_artifacts.get(producer) or candidate.get("source_artifact") or producer)
        producer_item = {
            "producer": producer,
            "code": code,
            "source_artifact": source_artifact,
            "evidence": _evidence(candidate.get("evidence")),
        }
        repair_action = str(candidate.get("repair_action") or "manual_review")
        repair_parameters = candidate.get("repair_parameters") if isinstance(candidate.get("repair_parameters"), dict) else {}
        if finding_id not in merged:
            merged[finding_id] = {
                "id": finding_id,
                "identity_version": QUALITY_FINDING_IDENTITY_VERSION,
                "code_family": family,
                "codes": [code],
                "category": str(candidate.get("category") or family),
                "severity": severity,
                "blocking": severity == "blocking",
                "canonical": canonical,
                "evidence": list(producer_item["evidence"]),
                "source_artifacts": [source_artifact],
                "repair": {"action": repair_action, "parameters": repair_parameters},
                "validation_coverage": _normalize_coverage(candidate.get("validation_coverage")),
                "producer_evidence": [producer_item],
            }
            continue
        finding = merged[finding_id]
        if code not in finding["codes"]:
            finding["codes"].append(code)
        if SEVERITY_RANK[severity] > SEVERITY_RANK[finding["severity"]]:
            finding["severity"] = severity
            finding["blocking"] = severity == "blocking"
        if source_artifact not in finding["source_artifacts"]:
            finding["source_artifacts"].append(source_artifact)
        finding["producer_evidence"].append(producer_item)
        finding["evidence"].extend(item for item in producer_item["evidence"] if item not in finding["evidence"])
        finding["validation_coverage"] = _merge_coverage(
            finding["validation_coverage"], candidate.get("validation_coverage")
        )
        if finding["repair"]["action"] == "manual_review" and repair_action != "manual_review":
            finding["repair"] = {"action": repair_action, "parameters": repair_parameters}
    result = list(merged.values())
    for finding in result:
        finding["codes"].sort()
        finding["source_artifacts"].sort()
        finding["producer_evidence"].sort(
            key=lambda item: (item["producer"], item["code"], item["source_artifact"], _digest(item["evidence"]))
        )
    return sorted(result, key=lambda item: item["id"])


def _code_family(code: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(code).lower()).strip("_") or "quality"
    aliases = (
        (("opening", "bridge", "last_scene_continuity"), "opening_continuity"),
        (("conflict", "thread", "progress"), "conflict_progression"),
        (("repeat", "stall"), "repetition_stalling"),
        (("location", "spatial", "transition", "position"), "spatial_continuity"),
        (("language",), "language_consistency"),
        (("meta", "wrapper", "output"), "output_contract"),
        (("length", "short", "empty"), "chapter_length"),
        (("required_beat", "ending_pressure", "blueprint"), "blueprint_coverage"),
        (("inactive_character",), "character_availability"),
    )
    for terms, family in aliases:
        if any(term in normalized for term in terms):
            return family
    return normalized


def _canonical_subject(problem: dict[str, Any]) -> str:
    for key in ("subject", "character", "term", "beat_text", "location", "expected"):
        value = problem.get(key)
        if value not in (None, ""):
            return str(value)
    return "chapter"


def _report_severity(status: str, legacy_severity: str) -> str:
    if status == "warning":
        return "warning"
    return "blocking" if legacy_severity == "critical" else "needs_revision"


def _coverage(validation: dict[str, Any] | None) -> dict[str, list[str]]:
    if not isinstance(validation, dict):
        return _empty_coverage()
    return {
        "requested_checks": _strings(validation.get("requested_focus")),
        "executed_checks": _strings(validation.get("executed_checks")),
        "skipped_checks": _strings(validation.get("skipped_checks")),
    }


def _empty_coverage() -> dict[str, list[str]]:
    return {"requested_checks": [], "executed_checks": [], "skipped_checks": []}


def _normalize_coverage(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return _empty_coverage()
    return {
        "requested_checks": _strings(value.get("requested_checks")),
        "executed_checks": _strings(value.get("executed_checks")),
        "skipped_checks": _strings(value.get("skipped_checks")),
    }


def _merge_coverage(left: Any, right: Any) -> dict[str, list[str]]:
    first = _normalize_coverage(left)
    second = _normalize_coverage(right)
    return {
        key: sorted(set(first[key]) | set(second[key]))
        for key in ("requested_checks", "executed_checks", "skipped_checks")
    }


def _llm_metadata(*, required: bool, executed: bool, supplied: dict[str, Any] | None) -> dict[str, Any]:
    metadata = supplied if isinstance(supplied, dict) else {}
    audit_complete = bool(
        isinstance(metadata.get("provider"), str)
        and metadata.get("provider")
        and isinstance(metadata.get("model"), str)
        and metadata.get("model")
        and isinstance(metadata.get("prompt_hash"), str)
        and len(metadata.get("prompt_hash")) == 64
        and isinstance(metadata.get("attempt_history"), list)
        and metadata.get("attempt_history")
    )
    return {
        "required": required,
        "configured": bool(metadata.get("configured", executed)),
        "available": bool(metadata.get("available", executed)) and (audit_complete or not required),
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "prompt_hash": metadata.get("prompt_hash"),
        "policy_version": QUALITY_POLICY_VERSION,
        "attempt_history": list(metadata.get("attempt_history") or []),
    }


def _llm_metadata_from_validation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(validation, dict):
        return None
    for check in validation.get("checks") or []:
        if not isinstance(check, dict) or check.get("name") != "llm":
            continue
        metadata = check.get("metadata")
        if isinstance(metadata, dict):
            return {
                "configured": True,
                "available": True,
                "provider": metadata.get("provider"),
                "model": metadata.get("model"),
                "prompt_hash": metadata.get("prompt_hash"),
                "attempt_history": list(metadata.get("attempt_history") or []),
            }
    return None


def _evidence(value: Any, *, message: Any = None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = [{"kind": str(key), "value": item} for key, item in sorted(value.items())]
    elif value in (None, ""):
        values = []
    else:
        values = [{"kind": "value", "value": value}]
    for item in values:
        if isinstance(item, dict):
            kind = str(item.get("kind") or item.get("code") or "evidence")
            raw = item.get("value", item.get("message", item))
        else:
            kind = "evidence"
            raw = item
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if text:
            candidate = {"kind": kind, "value": text}
            if candidate not in result:
                result.append(candidate)
    if message not in (None, ""):
        candidate = {"kind": "message", "value": str(message)}
        if candidate not in result:
            result.append(candidate)
    return result


def _chapter_time_range(chapter_index: int | None) -> str:
    return f"chapter:{chapter_index}" if isinstance(chapter_index, int) and chapter_index > 0 else "current_chapter"


def _normalized_identity_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower()) or "unknown"


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if item not in (None, "")})


def _bounded_int(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, min(3, value))
    return default


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "QUALITY_DECISION_SCHEMA_VERSION",
    "QUALITY_FINDING_IDENTITY_VERSION",
    "QUALITY_POLICIES",
    "QUALITY_POLICY_VERSION",
    "QualityPolicy",
    "SEVERITY_ORDER",
    "SEVERITY_RANK",
    "build_quality_decision",
    "quality_decision_accepted",
    "quality_decision_review_status",
    "resolve_quality_policy",
]
