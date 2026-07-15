from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.quality_decision import build_quality_decision
from core.schema import validate_schema
from core.validator import validate_chapter


FIXTURE_SOURCE = "synthetic_acceptance_v1"
FIXTURE_SCHEMA_VERSION = "1.0"
RAW_FIXTURE_SOURCE = "synthetic_raw_production_v1"
RAW_FIXTURE_SCHEMA_VERSION = "2.0"
RAW_LABEL_SOURCE = "synthetic_independent_recipe_v1"
REPORT_SCHEMA_VERSION = "1.0"
CALIBRATION_SPLIT = "calibration_set"
HOLDOUT_SPLIT = "holdout_set"
MINIMUM_SAMPLE_COUNT = 60

SEVERITY_RANK = {
    "clean": 0,
    "warning": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
BLOCKING_THRESHOLD_CANDIDATES = ("warning", "medium", "high", "critical")
ACCEPTANCE_CRITERIA = {
    "blocking_precision_min": 0.85,
    "critical_high_recall_min": 0.90,
    "clean_false_block_rate_max": 0.10,
}


class QualityCalibrationError(ValueError):
    """Raised when calibration provenance or split isolation is invalid."""


class FixtureIntegrityError(QualityCalibrationError):
    """Raised when a fixture no longer matches its recorded digest."""


def load_quality_calibration_fixture(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FixtureIntegrityError("quality calibration fixture must be a JSON object")
    validate_quality_calibration_fixture(value)
    return value


def fixture_sha256(fixture: Mapping[str, Any]) -> str:
    unsigned = copy.deepcopy(dict(fixture))
    unsigned.pop("fixture_sha256", None)
    return _canonical_sha256(unsigned)


def validate_quality_calibration_fixture(fixture: Mapping[str, Any]) -> Mapping[str, Any]:
    _reject_confidence_fields(fixture)
    if fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise FixtureIntegrityError("unsupported quality calibration fixture schema_version")
    if fixture.get("source") != FIXTURE_SOURCE:
        raise FixtureIntegrityError(
            "fixtures must disclose synthetic_acceptance_v1; they are not human-labelled evidence"
        )
    fixture_id = fixture.get("fixture_id")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise FixtureIntegrityError("fixture_id must be a non-empty string")
    recorded_digest = fixture.get("fixture_sha256")
    if not isinstance(recorded_digest, str) or len(recorded_digest) != 64:
        raise FixtureIntegrityError("fixture_sha256 must be a 64-character digest")
    if recorded_digest != fixture_sha256(fixture):
        raise FixtureIntegrityError("fixture_sha256 mismatch; fixture content or split was tampered")

    samples = fixture.get("samples")
    if not isinstance(samples, list) or len(samples) < MINIMUM_SAMPLE_COUNT:
        raise FixtureIntegrityError(f"fixture must contain at least {MINIMUM_SAMPLE_COUNT} fixed samples")
    _validate_samples(samples)
    calibration, holdout = split_quality_calibration_samples(samples)
    if not calibration or not holdout:
        raise FixtureIntegrityError("both calibration_set and holdout_set must be non-empty")

    calibration_ids = {sample["sample_id"] for sample in calibration}
    holdout_ids = {sample["sample_id"] for sample in holdout}
    if calibration_ids & holdout_ids:
        raise FixtureIntegrityError("calibration and holdout sample IDs must not overlap")

    directions = {
        (
            sample["input"].get("declared_gender"),
            sample["input"].get("reference_gender"),
        )
        for sample in holdout
        if sample["expected"]["issue_type"] == "gender_contradiction"
    }
    if not {("male", "female"), ("female", "male")}.issubset(directions):
        raise FixtureIntegrityError(
            "holdout_set must contain both male-to-female and female-to-male contradictions"
        )
    return fixture


def split_quality_calibration_samples(
    samples: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    calibration = tuple(sample for sample in samples if sample.get("split") == CALIBRATION_SPLIT)
    holdout = tuple(sample for sample in samples if sample.get("split") == HOLDOUT_SPLIT)
    unknown = [sample.get("sample_id", "<missing>") for sample in samples if sample.get("split") not in {CALIBRATION_SPLIT, HOLDOUT_SPLIT}]
    if unknown:
        raise FixtureIntegrityError(f"unknown fixture split for sample(s): {', '.join(map(str, unknown))}")
    return calibration, holdout


def detect_quality_sample(sample: Mapping[str, Any], *, blocking_threshold: str) -> dict[str, Any]:
    _validate_sample(sample)
    if blocking_threshold not in BLOCKING_THRESHOLD_CANDIDATES:
        raise QualityCalibrationError(f"unsupported blocking threshold: {blocking_threshold}")

    sample_input = sample["input"]
    issues: list[dict[str, str]] = []
    declared_gender = sample_input.get("declared_gender")
    reference_gender = sample_input.get("reference_gender")
    if declared_gender in {"male", "female"} and reference_gender in {"male", "female"}:
        if declared_gender != reference_gender:
            issues.append(
                {
                    "issue_type": "gender_contradiction",
                    "severity": "critical",
                    "rule_id": "declared_vs_reference_gender",
                }
            )

    for signal in sample_input["signals"]:
        if not signal["evidence_verified"]:
            continue
        issues.append(
            {
                "issue_type": signal["issue_type"],
                "severity": signal["severity"],
                "rule_id": signal["rule_id"],
            }
        )

    issues.sort(key=lambda issue: (-SEVERITY_RANK[issue["severity"]], issue["issue_type"], issue["rule_id"]))
    predicted_severity = issues[0]["severity"] if issues else "clean"
    predicted_blocking = SEVERITY_RANK[predicted_severity] >= SEVERITY_RANK[blocking_threshold]
    return {
        "sample_id": sample["sample_id"],
        "predicted_severity": predicted_severity,
        "predicted_blocking": predicted_blocking,
        "issues": issues,
    }


def calibrate_blocking_policy(
    calibration_samples: Sequence[Mapping[str, Any]],
    *,
    fixture_id: str,
    fixture_source: str = FIXTURE_SOURCE,
) -> dict[str, Any]:
    samples = tuple(calibration_samples)
    _require_split(samples, CALIBRATION_SPLIT, "threshold calibration")
    if fixture_source != FIXTURE_SOURCE:
        raise QualityCalibrationError("only disclosed synthetic_acceptance_v1 fixtures are supported")
    if not fixture_id:
        raise QualityCalibrationError("fixture_id must be non-empty")

    candidates: list[tuple[str, dict[str, Any]]] = []
    for threshold in BLOCKING_THRESHOLD_CANDIDATES:
        metrics = evaluate_quality_samples(samples, blocking_threshold=threshold)
        if _metrics_pass(metrics):
            candidates.append((threshold, metrics))
    if not candidates:
        raise QualityCalibrationError("no calibration-only threshold satisfies the acceptance criteria")

    # Higher F1 wins. A stricter threshold wins a deterministic tie. Holdout data is
    # never an argument to this selection and therefore cannot tune the threshold.
    threshold, metrics = max(
        candidates,
        key=lambda item: (_blocking_f1(item[1]), SEVERITY_RANK[item[0]]),
    )
    ordered_samples = sorted(samples, key=lambda sample: sample["sample_id"])
    policy = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "fixture_source": fixture_source,
        "blocking_threshold": threshold,
        "threshold_source": CALIBRATION_SPLIT,
        "calibration_sample_ids": [sample["sample_id"] for sample in ordered_samples],
        "calibration_sha256": _canonical_sha256(ordered_samples),
        "criteria": dict(ACCEPTANCE_CRITERIA),
        "calibration_metrics": metrics,
    }
    policy["policy_sha256"] = _policy_sha256(policy)
    return policy


def verify_blocking_policy(
    policy: Mapping[str, Any],
    calibration_samples: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    _reject_confidence_fields(policy)
    recorded = policy.get("policy_sha256")
    if not isinstance(recorded, str) or recorded != _policy_sha256(policy):
        raise QualityCalibrationError("policy_sha256 mismatch; calibrated policy was tampered")
    expected = calibrate_blocking_policy(
        calibration_samples,
        fixture_id=str(policy.get("fixture_id", "")),
        fixture_source=str(policy.get("fixture_source", "")),
    )
    if dict(policy) != expected:
        raise QualityCalibrationError(
            "policy does not match a calibration-set-only derivation; holdout tuning is forbidden"
        )
    return policy


def evaluate_quality_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    blocking_threshold: str,
) -> dict[str, Any]:
    values = tuple(samples)
    if not values:
        raise QualityCalibrationError("quality evaluation requires at least one sample")
    _validate_samples(values)
    predictions = [detect_quality_sample(sample, blocking_threshold=blocking_threshold) for sample in values]

    true_positive = false_positive = false_negative = true_negative = 0
    critical_high_total = critical_high_detected = 0
    clean_total = clean_false_blocked = 0
    for sample, prediction in zip(values, predictions):
        expected_blocking = sample["expected"]["blocking"]
        predicted_blocking = prediction["predicted_blocking"]
        if expected_blocking and predicted_blocking:
            true_positive += 1
        elif not expected_blocking and predicted_blocking:
            false_positive += 1
        elif expected_blocking and not predicted_blocking:
            false_negative += 1
        else:
            true_negative += 1

        if sample["expected"]["severity"] in {"critical", "high"}:
            critical_high_total += 1
            if predicted_blocking:
                critical_high_detected += 1
        if sample["expected"]["severity"] == "clean":
            clean_total += 1
            if predicted_blocking:
                clean_false_blocked += 1

    precision_denominator = true_positive + false_positive
    precision = true_positive / precision_denominator if precision_denominator else 1.0
    recall = critical_high_detected / critical_high_total if critical_high_total else 1.0
    clean_false_block_rate = clean_false_blocked / clean_total if clean_total else 0.0
    return {
        "sample_count": len(values),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "blocking_precision": _rounded(precision),
        "critical_high_recall": _rounded(recall),
        "clean_false_block_rate": _rounded(clean_false_block_rate),
    }


def evaluate_holdout(
    *,
    policy: Mapping[str, Any],
    calibration_samples: Sequence[Mapping[str, Any]],
    holdout_samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    calibration = tuple(calibration_samples)
    holdout = tuple(holdout_samples)
    _require_split(calibration, CALIBRATION_SPLIT, "policy verification")
    _require_split(holdout, HOLDOUT_SPLIT, "holdout evaluation")
    calibration_ids = {sample["sample_id"] for sample in calibration}
    holdout_ids = {sample["sample_id"] for sample in holdout}
    if calibration_ids & holdout_ids:
        raise QualityCalibrationError("calibration and holdout sample IDs overlap")
    verify_blocking_policy(policy, calibration)
    return evaluate_quality_samples(holdout, blocking_threshold=str(policy["blocking_threshold"]))


def build_quality_calibration_report(fixture: Mapping[str, Any]) -> dict[str, Any]:
    validate_quality_calibration_fixture(fixture)
    calibration, holdout = split_quality_calibration_samples(fixture["samples"])
    policy = calibrate_blocking_policy(
        calibration,
        fixture_id=str(fixture["fixture_id"]),
        fixture_source=str(fixture["source"]),
    )
    holdout_metrics = evaluate_holdout(
        policy=policy,
        calibration_samples=calibration,
        holdout_samples=holdout,
    )
    checks = _acceptance_checks(holdout_metrics)

    gender_cases = [
        sample
        for sample in holdout
        if sample["expected"]["issue_type"] == "gender_contradiction"
    ]
    detected_gender_ids = []
    for sample in gender_cases:
        prediction = detect_quality_sample(sample, blocking_threshold=str(policy["blocking_threshold"]))
        if prediction["predicted_blocking"] and any(
            issue["issue_type"] == "gender_contradiction" for issue in prediction["issues"]
        ):
            detected_gender_ids.append(sample["sample_id"])
    gender_case_ids = sorted(sample["sample_id"] for sample in gender_cases)
    gender_passed = sorted(detected_gender_ids) == gender_case_ids and bool(gender_case_ids)

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": fixture["fixture_id"],
        "fixture_source": fixture["source"],
        "fixture_sha256": fixture["fixture_sha256"],
        "sample_counts": {
            "total": len(fixture["samples"]),
            "calibration_set": len(calibration),
            "holdout_set": len(holdout),
        },
        "policy": policy,
        "holdout_metrics": holdout_metrics,
        "acceptance_checks": checks,
        "gender_contradiction": {
            "case_ids": gender_case_ids,
            "detected_ids": sorted(detected_gender_ids),
            "passed": gender_passed,
        },
        "passed": all(check["passed"] for check in checks) and gender_passed,
    }
    report["report_sha256"] = _report_sha256(report)
    return validate_schema(report, "quality_calibration_report.schema.json")


def load_raw_quality_calibration_fixture(path: str | Path) -> dict[str, Any]:
    """Load and deterministically expand the frozen raw-input calibration recipe.

    Expected labels remain outside ``input``.  The production validator receives
    only the expanded snapshot, prose, and validation focus.
    """

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FixtureIntegrityError("raw quality calibration fixture must be a JSON object")
    validate_raw_quality_calibration_fixture(value)
    return expand_raw_quality_calibration_fixture(value)


def validate_raw_quality_calibration_fixture(
    fixture: Mapping[str, Any],
) -> Mapping[str, Any]:
    _reject_confidence_fields(fixture)
    if fixture.get("schema_version") != RAW_FIXTURE_SCHEMA_VERSION:
        raise FixtureIntegrityError("unsupported raw quality calibration fixture schema_version")
    if fixture.get("source") != RAW_FIXTURE_SOURCE:
        raise FixtureIntegrityError(
            "raw fixtures must disclose synthetic_raw_production_v1; they are not human-labelled evidence"
        )
    fixture_id = fixture.get("fixture_id")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise FixtureIntegrityError("raw fixture_id must be a non-empty string")
    recorded_digest = fixture.get("fixture_sha256")
    if not isinstance(recorded_digest, str) or len(recorded_digest) != 64:
        raise FixtureIntegrityError("raw fixture_sha256 must be a 64-character digest")
    if recorded_digest != fixture_sha256(fixture):
        raise FixtureIntegrityError("raw fixture_sha256 mismatch; recipe or split was tampered")

    expanded = expand_raw_quality_calibration_fixture(fixture, validate_recipe_only=True)
    recipe = fixture["recipe"]
    templates = recipe["templates"]
    groups = recipe["groups"]
    calibration_template_ids = {
        str(group["template_id"])
        for group in groups
        if group["split"] == CALIBRATION_SPLIT
    }
    holdout_template_ids = {
        str(group["template_id"])
        for group in groups
        if group["split"] == HOLDOUT_SPLIT
    }
    if calibration_template_ids & holdout_template_ids:
        raise FixtureIntegrityError(
            "raw calibration and holdout groups must use independent frozen templates"
        )
    calibration_template_texts = {
        str(templates[template_id]) for template_id in calibration_template_ids
    }
    holdout_template_texts = {
        str(templates[template_id]) for template_id in holdout_template_ids
    }
    if calibration_template_texts & holdout_template_texts:
        raise FixtureIntegrityError(
            "raw calibration and holdout template prose must not be reused"
        )
    samples = expanded["samples"]
    if len(samples) < MINIMUM_SAMPLE_COUNT:
        raise FixtureIntegrityError(
            f"raw fixture must expand to at least {MINIMUM_SAMPLE_COUNT} fixed samples"
        )
    _validate_raw_samples(samples)
    calibration, holdout = split_quality_calibration_samples(samples)
    if not calibration or not holdout:
        raise FixtureIntegrityError("raw fixture requires both calibration_set and holdout_set")
    if len(calibration) != 40 or len(holdout) != 24:
        raise FixtureIntegrityError("raw fixture must retain the frozen 40/24 calibration/holdout split")

    calibration_ids = {sample["sample_id"] for sample in calibration}
    holdout_ids = {sample["sample_id"] for sample in holdout}
    if calibration_ids & holdout_ids:
        raise FixtureIntegrityError("raw calibration and holdout sample IDs must not overlap")
    calibration_inputs = {_canonical_sha256(sample["input"]) for sample in calibration}
    holdout_inputs = {_canonical_sha256(sample["input"]) for sample in holdout}
    if calibration_inputs & holdout_inputs:
        raise FixtureIntegrityError("raw calibration and holdout inputs must not overlap")

    holdout_directions = {
        sample["expected"]["direction"]
        for sample in holdout
        if sample["expected"]["issue_type"] == "character_voice_gender_conflict"
    }
    if not {"male_to_female", "female_to_male"}.issubset(holdout_directions):
        raise FixtureIntegrityError(
            "raw holdout_set must contain both male-to-female and female-to-male voice contradictions"
        )
    return fixture


def expand_raw_quality_calibration_fixture(
    fixture: Mapping[str, Any],
    *,
    validate_recipe_only: bool = False,
) -> dict[str, Any]:
    recipe = fixture.get("recipe")
    if not isinstance(recipe, Mapping):
        raise FixtureIntegrityError("raw fixture recipe must be an object")
    if recipe.get("recipe_version") != "named-voice-continuity-v1":
        raise FixtureIntegrityError("unsupported raw fixture recipe_version")
    templates = recipe.get("templates")
    groups = recipe.get("groups")
    if not isinstance(templates, Mapping) or not templates:
        raise FixtureIntegrityError("raw fixture recipe requires templates")
    if not isinstance(groups, list) or not groups:
        raise FixtureIntegrityError("raw fixture recipe requires groups")
    normalized_templates: dict[str, str] = {}
    for raw_id, raw_template in templates.items():
        template_id = str(raw_id)
        if not template_id or not isinstance(raw_template, str) or not raw_template:
            raise FixtureIntegrityError("raw fixture templates require non-empty string ids and prose")
        if "{name}" not in raw_template or "{variant}" not in raw_template:
            raise FixtureIntegrityError(
                f"raw fixture template {template_id} must bind name and variant"
            )
        normalized_templates[template_id] = raw_template

    samples: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    sequence = 0
    required_group_fields = {
        "group_id",
        "split",
        "count",
        "snapshot_voice_gender",
        "character_status",
        "template_id",
        "expected",
    }
    for raw_group in groups:
        if not isinstance(raw_group, Mapping) or set(raw_group) != required_group_fields:
            raise FixtureIntegrityError(
                f"raw fixture groups must contain exactly {sorted(required_group_fields)}"
            )
        group = dict(raw_group)
        group_id = str(group["group_id"])
        if not group_id or group_id in seen_groups:
            raise FixtureIntegrityError("raw fixture group ids must be unique non-empty strings")
        seen_groups.add(group_id)
        split = str(group["split"])
        if split not in {CALIBRATION_SPLIT, HOLDOUT_SPLIT}:
            raise FixtureIntegrityError(f"raw fixture group {group_id} has an invalid split")
        count = group["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise FixtureIntegrityError(f"raw fixture group {group_id} count must be positive")
        voice_gender = str(group["snapshot_voice_gender"])
        if voice_gender not in {"male", "female"}:
            raise FixtureIntegrityError(f"raw fixture group {group_id} voice gender is invalid")
        status = str(group["character_status"])
        if status not in {"active", "dead"}:
            raise FixtureIntegrityError(f"raw fixture group {group_id} status is invalid")
        template_id = str(group["template_id"])
        if template_id not in normalized_templates:
            raise FixtureIntegrityError(f"raw fixture group {group_id} references an unknown template")
        expected = copy.deepcopy(group["expected"])
        if not isinstance(expected, dict):
            raise FixtureIntegrityError(f"raw fixture group {group_id} expected label must be an object")

        for group_index in range(1, count + 1):
            sequence += 1
            sample_id = f"{group_id}-{group_index:03d}"
            character_name = f"角色{sequence:02d}"
            chapter_text = normalized_templates[template_id].format(
                name=character_name,
                variant=sample_id,
            )
            snapshot = _raw_fixture_snapshot(
                character_name=character_name,
                voice_gender=voice_gender,
                status=status,
            )
            samples.append(
                {
                    "sample_id": sample_id,
                    "split": split,
                    "label_source": RAW_LABEL_SOURCE,
                    "input": {
                        "snapshot": snapshot,
                        "chapter_text": chapter_text,
                        "decision": {"validation_focus": ["continuity"]},
                    },
                    "expected": copy.deepcopy(expected),
                }
            )

    expanded = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": str(fixture.get("fixture_id") or ""),
        "source": str(fixture.get("source") or ""),
        "description": str(fixture.get("description") or ""),
        "fixture_sha256": str(fixture.get("fixture_sha256") or ""),
        "samples": samples,
    }
    if not validate_recipe_only:
        _validate_raw_samples(samples)
    return expanded


def detect_raw_quality_sample(
    sample: Mapping[str, Any],
    *,
    blocking_threshold: str,
) -> dict[str, Any]:
    """Run one raw case through production validation and QualityDecision."""

    _validate_raw_sample(sample)
    if blocking_threshold not in BLOCKING_THRESHOLD_CANDIDATES:
        raise QualityCalibrationError(f"unsupported blocking threshold: {blocking_threshold}")
    sample_input = sample["input"]
    validation = validate_chapter(
        copy.deepcopy(sample_input["snapshot"]),
        str(sample_input["chapter_text"]),
        copy.deepcopy(sample_input["decision"]),
    )
    quality_decision = build_quality_decision(
        policy="minimal",
        validation=validation,
        chapter_index=int(sample_input["snapshot"]["chapter_index"]),
    )
    issues = [
        {
            "issue_type": str(problem.get("code") or "validation_problem"),
            "severity": _calibration_severity(problem.get("severity")),
            "rule_id": str(problem.get("validator") or "base_validation"),
        }
        for problem in validation.get("problems") or []
        if isinstance(problem, Mapping)
    ]
    issues.sort(
        key=lambda issue: (
            -SEVERITY_RANK[issue["severity"]],
            issue["issue_type"],
            issue["rule_id"],
        )
    )
    predicted_severity = issues[0]["severity"] if issues else "clean"
    predicted_blocking = (
        not bool(quality_decision["accepted"])
        and SEVERITY_RANK[predicted_severity] >= SEVERITY_RANK[blocking_threshold]
    )
    return {
        "sample_id": sample["sample_id"],
        "predicted_severity": predicted_severity,
        "predicted_blocking": predicted_blocking,
        "issues": issues,
        "production_path": {
            "validator": "validate_chapter",
            "quality_decision": "build_quality_decision",
            "validation_ok": bool(validation["ok"]),
            "quality_decision_accepted": bool(quality_decision["accepted"]),
            "quality_decision_digest": quality_decision["decision_digest"],
        },
    }


def calibrate_raw_blocking_policy(
    calibration_samples: Sequence[Mapping[str, Any]],
    *,
    fixture_id: str,
    fixture_source: str = RAW_FIXTURE_SOURCE,
) -> dict[str, Any]:
    samples = tuple(calibration_samples)
    _require_raw_split(samples, CALIBRATION_SPLIT, "raw threshold calibration")
    if fixture_source != RAW_FIXTURE_SOURCE:
        raise QualityCalibrationError("raw policy requires synthetic_raw_production_v1 provenance")
    if not fixture_id:
        raise QualityCalibrationError("raw fixture_id must be non-empty")

    candidates: list[tuple[str, dict[str, Any]]] = []
    for threshold in BLOCKING_THRESHOLD_CANDIDATES:
        metrics = evaluate_raw_quality_samples(samples, blocking_threshold=threshold)
        if _metrics_pass(metrics):
            candidates.append((threshold, metrics))
    if not candidates:
        raise QualityCalibrationError(
            "no calibration-only raw threshold satisfies the acceptance criteria"
        )
    threshold, metrics = max(
        candidates,
        key=lambda item: (_blocking_f1(item[1]), SEVERITY_RANK[item[0]]),
    )
    ordered_samples = sorted(samples, key=lambda sample: sample["sample_id"])
    policy = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "fixture_source": fixture_source,
        "blocking_threshold": threshold,
        "threshold_source": CALIBRATION_SPLIT,
        "calibration_sample_ids": [sample["sample_id"] for sample in ordered_samples],
        "calibration_sha256": _canonical_sha256(ordered_samples),
        "criteria": dict(ACCEPTANCE_CRITERIA),
        "calibration_metrics": metrics,
    }
    policy["policy_sha256"] = _policy_sha256(policy)
    return policy


def verify_raw_blocking_policy(
    policy: Mapping[str, Any],
    calibration_samples: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    _reject_confidence_fields(policy)
    recorded = policy.get("policy_sha256")
    if not isinstance(recorded, str) or recorded != _policy_sha256(policy):
        raise QualityCalibrationError("raw policy_sha256 mismatch; calibrated policy was tampered")
    expected = calibrate_raw_blocking_policy(
        calibration_samples,
        fixture_id=str(policy.get("fixture_id") or ""),
        fixture_source=str(policy.get("fixture_source") or ""),
    )
    if dict(policy) != expected:
        raise QualityCalibrationError(
            "raw policy does not match a calibration-set-only derivation; holdout tuning is forbidden"
        )
    return policy


def evaluate_raw_quality_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    blocking_threshold: str,
) -> dict[str, Any]:
    values = tuple(samples)
    if not values:
        raise QualityCalibrationError("raw quality evaluation requires at least one sample")
    _validate_raw_samples(values)
    predictions = [
        detect_raw_quality_sample(sample, blocking_threshold=blocking_threshold)
        for sample in values
    ]
    return _quality_metrics(values, predictions)


def evaluate_raw_holdout(
    *,
    policy: Mapping[str, Any],
    calibration_samples: Sequence[Mapping[str, Any]],
    holdout_samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    calibration = tuple(calibration_samples)
    holdout = tuple(holdout_samples)
    _require_raw_split(calibration, CALIBRATION_SPLIT, "raw policy verification")
    _require_raw_split(holdout, HOLDOUT_SPLIT, "raw holdout evaluation")
    calibration_ids = {sample["sample_id"] for sample in calibration}
    holdout_ids = {sample["sample_id"] for sample in holdout}
    if calibration_ids & holdout_ids:
        raise QualityCalibrationError("raw calibration and holdout sample IDs overlap")
    calibration_inputs = {_canonical_sha256(sample["input"]) for sample in calibration}
    holdout_inputs = {_canonical_sha256(sample["input"]) for sample in holdout}
    if calibration_inputs & holdout_inputs:
        raise QualityCalibrationError("raw calibration and holdout inputs overlap")
    verify_raw_blocking_policy(policy, calibration)
    return evaluate_raw_quality_samples(
        holdout,
        blocking_threshold=str(policy["blocking_threshold"]),
    )


def build_raw_quality_calibration_report(fixture: Mapping[str, Any]) -> dict[str, Any]:
    if fixture.get("source") != RAW_FIXTURE_SOURCE:
        raise FixtureIntegrityError("raw quality report requires expanded raw fixture provenance")
    samples = fixture.get("samples")
    if not isinstance(samples, list):
        raise FixtureIntegrityError("expanded raw fixture requires samples")
    _validate_raw_samples(samples)
    calibration, holdout = split_quality_calibration_samples(samples)
    policy = calibrate_raw_blocking_policy(
        calibration,
        fixture_id=str(fixture.get("fixture_id") or ""),
        fixture_source=str(fixture.get("source") or ""),
    )
    holdout_metrics = evaluate_raw_holdout(
        policy=policy,
        calibration_samples=calibration,
        holdout_samples=holdout,
    )
    checks = _acceptance_checks(holdout_metrics)
    gender_cases = [
        sample
        for sample in holdout
        if sample["expected"]["issue_type"] == "character_voice_gender_conflict"
    ]
    detected_gender_ids: list[str] = []
    for sample in gender_cases:
        prediction = detect_raw_quality_sample(
            sample,
            blocking_threshold=str(policy["blocking_threshold"]),
        )
        if prediction["predicted_blocking"] and any(
            issue["issue_type"] == "character_voice_gender_conflict"
            for issue in prediction["issues"]
        ):
            detected_gender_ids.append(sample["sample_id"])
    gender_case_ids = sorted(sample["sample_id"] for sample in gender_cases)
    gender_passed = sorted(detected_gender_ids) == gender_case_ids and bool(gender_case_ids)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "fixture_id": fixture["fixture_id"],
        "fixture_source": fixture["source"],
        "fixture_sha256": fixture["fixture_sha256"],
        "sample_counts": {
            "total": len(samples),
            "calibration_set": len(calibration),
            "holdout_set": len(holdout),
        },
        "policy": policy,
        "holdout_metrics": holdout_metrics,
        "acceptance_checks": checks,
        "gender_contradiction": {
            "case_ids": gender_case_ids,
            "detected_ids": sorted(detected_gender_ids),
            "passed": gender_passed,
        },
        "passed": all(check["passed"] for check in checks) and gender_passed,
    }
    report["report_sha256"] = _report_sha256(report)
    return validate_schema(report, "quality_calibration_report.schema.json")


def _raw_fixture_snapshot(
    *,
    character_name: str,
    voice_gender: str,
    status: str,
) -> dict[str, Any]:
    return {
        "chapter_index": 12,
        "world_state": {},
        "characters": {
            character_name: {
                "status": status,
                "voice_gender": voice_gender,
            }
        },
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
    }


def _validate_raw_samples(samples: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for sample in samples:
        _validate_raw_sample(sample)
        sample_id = str(sample["sample_id"])
        if sample_id in seen:
            raise FixtureIntegrityError(f"duplicate raw sample_id: {sample_id}")
        seen.add(sample_id)


def _validate_raw_sample(sample: Mapping[str, Any]) -> None:
    _reject_confidence_fields(sample)
    if not isinstance(sample, Mapping):
        raise FixtureIntegrityError("each raw quality sample must be an object")
    if set(sample) != {"sample_id", "split", "label_source", "input", "expected"}:
        raise FixtureIntegrityError("raw quality sample fields are invalid")
    sample_id = sample.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id:
        raise FixtureIntegrityError("raw sample_id must be a non-empty string")
    if sample.get("split") not in {CALIBRATION_SPLIT, HOLDOUT_SPLIT}:
        raise FixtureIntegrityError(f"raw sample {sample_id} has an invalid split")
    if sample.get("label_source") != RAW_LABEL_SOURCE:
        raise FixtureIntegrityError(f"raw sample {sample_id} label provenance is invalid")
    sample_input = sample.get("input")
    if not isinstance(sample_input, Mapping) or set(sample_input) != {
        "snapshot",
        "chapter_text",
        "decision",
    }:
        raise FixtureIntegrityError(
            f"raw sample {sample_id} input must contain only snapshot, chapter_text, and decision"
        )
    _reject_raw_label_fields(sample_input)
    if not isinstance(sample_input["snapshot"], Mapping):
        raise FixtureIntegrityError(f"raw sample {sample_id} snapshot must be an object")
    if not isinstance(sample_input["chapter_text"], str) or not sample_input["chapter_text"]:
        raise FixtureIntegrityError(f"raw sample {sample_id} chapter_text must be non-empty")
    if sample_input["decision"] != {"validation_focus": ["continuity"]}:
        raise FixtureIntegrityError(f"raw sample {sample_id} must use the production continuity focus")

    expected = sample.get("expected")
    if not isinstance(expected, Mapping) or set(expected) != {
        "severity",
        "blocking",
        "issue_type",
        "direction",
    }:
        raise FixtureIntegrityError(f"raw sample {sample_id} expected label is invalid")
    if expected.get("severity") not in SEVERITY_RANK:
        raise FixtureIntegrityError(f"raw sample {sample_id} expected severity is invalid")
    if not isinstance(expected.get("blocking"), bool):
        raise FixtureIntegrityError(f"raw sample {sample_id} expected blocking must be boolean")
    if expected["blocking"] != (expected["severity"] in {"critical", "high"}):
        raise FixtureIntegrityError(f"raw sample {sample_id} expected blocking conflicts with severity")
    if not isinstance(expected.get("issue_type"), str) or not expected["issue_type"]:
        raise FixtureIntegrityError(f"raw sample {sample_id} expected issue_type must be non-empty")
    if expected.get("direction") not in {"none", "male_to_female", "female_to_male"}:
        raise FixtureIntegrityError(f"raw sample {sample_id} direction is invalid")
    is_voice_conflict = expected["issue_type"] == "character_voice_gender_conflict"
    if is_voice_conflict != (expected["direction"] != "none"):
        raise FixtureIntegrityError(f"raw sample {sample_id} voice direction conflicts with issue label")


def _reject_raw_label_fields(value: Any, path: str = "$.input") -> None:
    forbidden = {"expected", "severity", "blocking", "signals", "label", "label_source"}
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in forbidden:
                raise FixtureIntegrityError(
                    f"raw detector input must not contain label field {path}.{key}"
                )
            _reject_raw_label_fields(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_raw_label_fields(child, f"{path}[{index}]")


def _require_raw_split(
    samples: Sequence[Mapping[str, Any]],
    split: str,
    purpose: str,
) -> None:
    if not samples:
        raise QualityCalibrationError(f"{purpose} requires at least one {split} sample")
    _validate_raw_samples(samples)
    wrong = [sample["sample_id"] for sample in samples if sample["split"] != split]
    if wrong:
        raise QualityCalibrationError(
            f"{purpose} accepts only {split}; rejected sample(s): {', '.join(wrong)}"
        )


def _calibration_severity(value: Any) -> str:
    severity = str(value or "medium")
    if severity == "low":
        return "warning"
    return severity if severity in SEVERITY_RANK else "medium"


def _quality_metrics(
    samples: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    true_positive = false_positive = false_negative = true_negative = 0
    critical_high_total = critical_high_detected = 0
    clean_total = clean_false_blocked = 0
    for sample, prediction in zip(samples, predictions):
        expected_blocking = bool(sample["expected"]["blocking"])
        predicted_blocking = bool(prediction["predicted_blocking"])
        if expected_blocking and predicted_blocking:
            true_positive += 1
        elif not expected_blocking and predicted_blocking:
            false_positive += 1
        elif expected_blocking and not predicted_blocking:
            false_negative += 1
        else:
            true_negative += 1
        if sample["expected"]["severity"] in {"critical", "high"}:
            critical_high_total += 1
            if predicted_blocking:
                critical_high_detected += 1
        if sample["expected"]["severity"] == "clean":
            clean_total += 1
            if predicted_blocking:
                clean_false_blocked += 1
    precision_denominator = true_positive + false_positive
    return {
        "sample_count": len(samples),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "blocking_precision": _rounded(
            true_positive / precision_denominator if precision_denominator else 1.0
        ),
        "critical_high_recall": _rounded(
            critical_high_detected / critical_high_total if critical_high_total else 1.0
        ),
        "clean_false_block_rate": _rounded(
            clean_false_blocked / clean_total if clean_total else 0.0
        ),
    }


def _validate_samples(samples: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for sample in samples:
        _validate_sample(sample)
        sample_id = sample["sample_id"]
        if sample_id in seen:
            raise FixtureIntegrityError(f"duplicate sample_id: {sample_id}")
        seen.add(sample_id)


def _validate_sample(sample: Mapping[str, Any]) -> None:
    _reject_confidence_fields(sample)
    if not isinstance(sample, Mapping):
        raise FixtureIntegrityError("each quality calibration sample must be an object")
    sample_id = sample.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id:
        raise FixtureIntegrityError("sample_id must be a non-empty string")
    if sample.get("split") not in {CALIBRATION_SPLIT, HOLDOUT_SPLIT}:
        raise FixtureIntegrityError(f"invalid split for sample {sample_id}")
    if sample.get("label_source") != FIXTURE_SOURCE:
        raise FixtureIntegrityError(f"sample {sample_id} must disclose synthetic label provenance")

    sample_input = sample.get("input")
    if not isinstance(sample_input, Mapping):
        raise FixtureIntegrityError(f"sample {sample_id} input must be an object")
    declared = sample_input.get("declared_gender")
    referenced = sample_input.get("reference_gender")
    if declared not in {None, "male", "female"} or referenced not in {None, "male", "female"}:
        raise FixtureIntegrityError(f"sample {sample_id} has an unsupported gender marker")
    signals = sample_input.get("signals")
    if not isinstance(signals, list):
        raise FixtureIntegrityError(f"sample {sample_id} signals must be an array")
    for signal in signals:
        if not isinstance(signal, Mapping):
            raise FixtureIntegrityError(f"sample {sample_id} signal must be an object")
        if not all(isinstance(signal.get(key), str) and signal.get(key) for key in ("rule_id", "issue_type")):
            raise FixtureIntegrityError(f"sample {sample_id} signal identity is incomplete")
        if signal.get("severity") not in {"warning", "medium", "high", "critical"}:
            raise FixtureIntegrityError(f"sample {sample_id} signal severity is invalid")
        if not isinstance(signal.get("evidence_verified"), bool):
            raise FixtureIntegrityError(f"sample {sample_id} evidence_verified must be boolean")

    expected = sample.get("expected")
    if not isinstance(expected, Mapping):
        raise FixtureIntegrityError(f"sample {sample_id} expected label must be an object")
    if expected.get("severity") not in SEVERITY_RANK:
        raise FixtureIntegrityError(f"sample {sample_id} expected severity is invalid")
    if not isinstance(expected.get("blocking"), bool):
        raise FixtureIntegrityError(f"sample {sample_id} expected blocking must be boolean")
    if expected["blocking"] != (expected["severity"] in {"critical", "high"}):
        raise FixtureIntegrityError(f"sample {sample_id} expected blocking conflicts with severity")
    if not isinstance(expected.get("issue_type"), str) or not expected["issue_type"]:
        raise FixtureIntegrityError(f"sample {sample_id} expected issue_type must be non-empty")
    if expected["issue_type"] == "gender_contradiction" and declared == referenced:
        raise FixtureIntegrityError(f"sample {sample_id} gender contradiction label lacks a contradiction")


def _require_split(samples: Sequence[Mapping[str, Any]], split: str, purpose: str) -> None:
    if not samples:
        raise QualityCalibrationError(f"{purpose} requires at least one {split} sample")
    _validate_samples(samples)
    wrong = [sample["sample_id"] for sample in samples if sample["split"] != split]
    if wrong:
        raise QualityCalibrationError(
            f"{purpose} accepts only {split}; rejected sample(s): {', '.join(wrong)}"
        )


def _metrics_pass(metrics: Mapping[str, Any]) -> bool:
    return (
        metrics["blocking_precision"] >= ACCEPTANCE_CRITERIA["blocking_precision_min"]
        and metrics["critical_high_recall"] >= ACCEPTANCE_CRITERIA["critical_high_recall_min"]
        and metrics["clean_false_block_rate"] <= ACCEPTANCE_CRITERIA["clean_false_block_rate_max"]
    )


def _blocking_f1(metrics: Mapping[str, Any]) -> float:
    true_positive = int(metrics["true_positive"])
    false_positive = int(metrics["false_positive"])
    false_negative = int(metrics["false_negative"])
    denominator = (2 * true_positive) + false_positive + false_negative
    return (2 * true_positive) / denominator if denominator else 1.0


def _acceptance_checks(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": "blocking_precision",
            "comparator": ">=",
            "actual": metrics["blocking_precision"],
            "target": ACCEPTANCE_CRITERIA["blocking_precision_min"],
            "passed": metrics["blocking_precision"] >= ACCEPTANCE_CRITERIA["blocking_precision_min"],
        },
        {
            "name": "critical_high_recall",
            "comparator": ">=",
            "actual": metrics["critical_high_recall"],
            "target": ACCEPTANCE_CRITERIA["critical_high_recall_min"],
            "passed": metrics["critical_high_recall"] >= ACCEPTANCE_CRITERIA["critical_high_recall_min"],
        },
        {
            "name": "clean_false_block_rate",
            "comparator": "<=",
            "actual": metrics["clean_false_block_rate"],
            "target": ACCEPTANCE_CRITERIA["clean_false_block_rate_max"],
            "passed": metrics["clean_false_block_rate"] <= ACCEPTANCE_CRITERIA["clean_false_block_rate_max"],
        },
    ]


def _reject_confidence_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized == "confidence" or normalized.endswith("_confidence"):
                raise QualityCalibrationError(
                    f"LLM/self-reported confidence is forbidden in calibration evidence: {path}.{key}"
                )
            _reject_confidence_fields(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_confidence_fields(child, f"{path}[{index}]")


def _policy_sha256(policy: Mapping[str, Any]) -> str:
    unsigned = copy.deepcopy(dict(policy))
    unsigned.pop("policy_sha256", None)
    return _canonical_sha256(unsigned)


def _report_sha256(report: Mapping[str, Any]) -> str:
    unsigned = copy.deepcopy(dict(report))
    unsigned.pop("report_sha256", None)
    return _canonical_sha256(unsigned)


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rounded(value: float) -> float:
    return round(float(value), 6)


__all__ = [
    "ACCEPTANCE_CRITERIA",
    "CALIBRATION_SPLIT",
    "FIXTURE_SOURCE",
    "FixtureIntegrityError",
    "HOLDOUT_SPLIT",
    "QualityCalibrationError",
    "RAW_FIXTURE_SOURCE",
    "RAW_LABEL_SOURCE",
    "build_quality_calibration_report",
    "build_raw_quality_calibration_report",
    "calibrate_blocking_policy",
    "calibrate_raw_blocking_policy",
    "detect_quality_sample",
    "detect_raw_quality_sample",
    "evaluate_holdout",
    "evaluate_quality_samples",
    "evaluate_raw_holdout",
    "evaluate_raw_quality_samples",
    "expand_raw_quality_calibration_fixture",
    "fixture_sha256",
    "load_quality_calibration_fixture",
    "load_raw_quality_calibration_fixture",
    "split_quality_calibration_samples",
    "validate_quality_calibration_fixture",
    "validate_raw_quality_calibration_fixture",
    "verify_blocking_policy",
    "verify_raw_blocking_policy",
]
