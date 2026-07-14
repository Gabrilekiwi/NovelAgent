from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from core.context_budget import CalibratedTokenEstimator, SAFE_ENDPOINT_TYPES
from core.schema import validate_schema


TOKEN_CALIBRATION_SCHEMA_VERSION = "1.0"
TOKEN_CALIBRATION_METHOD = "max-observed-tokens-per-utf8-byte-v1"
TOKEN_CALIBRATION_SOURCE_SYNTHETIC = "synthetic_acceptance_v1"
_LANGUAGE_PROFILES = frozenset({"zh", "en", "mixed"})


class TokenCalibrationError(ValueError):
    pass


def fit_token_estimator(
    calibration_samples: Iterable[Mapping[str, Any]],
    *,
    version: str,
) -> CalibratedTokenEstimator:
    """Fit a conservative byte ratio using calibration samples only.

    The maximum observed ratio is intentional: the fallback is a budget guard,
    not a tokenizer replacement.  Holdout samples are rejected here so they
    cannot silently tune the estimator later used to report evaluation error.
    """

    samples = _normalize_samples(calibration_samples, required_split="calibration")
    if not samples:
        raise TokenCalibrationError("at least one calibration sample is required")
    ratios = [sample["actual_tokens"] / sample["utf8_bytes"] for sample in samples]
    return CalibratedTokenEstimator(
        version=version,
        tokens_per_utf8_byte=max(ratios),
        fixed_overhead_tokens=0,
    )


def build_token_calibration_report(
    *,
    estimator: CalibratedTokenEstimator,
    calibration_samples: Iterable[Mapping[str, Any]],
    holdout_samples: Iterable[Mapping[str, Any]],
    dataset_source: str,
) -> dict[str, Any]:
    """Evaluate estimator error exclusively on an isolated holdout set."""

    calibration = _normalize_samples(calibration_samples, required_split="calibration")
    holdout = _normalize_samples(holdout_samples, required_split="holdout")
    if not calibration or not holdout:
        raise TokenCalibrationError("calibration and holdout sets must both be non-empty")

    calibration_ids = {sample["id"] for sample in calibration}
    holdout_ids = {sample["id"] for sample in holdout}
    overlap = sorted(calibration_ids & holdout_ids)
    if overlap:
        raise TokenCalibrationError(f"calibration/holdout sample ids overlap: {overlap}")

    calibration_fingerprints = {_sample_fingerprint(sample) for sample in calibration}
    holdout_fingerprints = {_sample_fingerprint(sample) for sample in holdout}
    if calibration_fingerprints & holdout_fingerprints:
        raise TokenCalibrationError("calibration/holdout content fingerprints overlap")

    if not isinstance(dataset_source, str) or not dataset_source.strip():
        raise TokenCalibrationError("dataset_source must be explicit")

    coverage = _coverage(holdout)
    missing_endpoints = sorted({"official", "openai_compatible", "unknown"} - set(coverage["endpoint_types"]))
    missing_languages = sorted(_LANGUAGE_PROFILES - set(coverage["language_profiles"]))
    if missing_endpoints or missing_languages:
        raise TokenCalibrationError(
            "holdout coverage incomplete: "
            f"endpoint_types={missing_endpoints}, language_profiles={missing_languages}"
        )

    evaluated: list[dict[str, Any]] = []
    absolute_percentage_errors: list[float] = []
    underestimate_ratios: list[float] = []
    for sample in holdout:
        estimated = estimator.estimate(sample["text"])
        actual = sample["actual_tokens"]
        absolute_error = abs(estimated - actual)
        percentage_error = absolute_error / actual
        underestimate = max(0.0, (actual - estimated) / actual)
        absolute_percentage_errors.append(percentage_error)
        underestimate_ratios.append(underestimate)
        evaluated.append(
            {
                "id": sample["id"],
                "provider": sample["provider"],
                "model": sample["model"],
                "endpoint_type": sample["endpoint_type"],
                "language_profile": sample["language_profile"],
                "reference_count_mode": _reference_count_mode(sample),
                "actual_tokens": actual,
                "estimated_tokens": estimated,
                "absolute_error_tokens": absolute_error,
                "absolute_percentage_error": round(percentage_error, 6),
                "underestimate_ratio": round(underestimate, 6),
            }
        )

    report = {
        "schema_version": TOKEN_CALIBRATION_SCHEMA_VERSION,
        "method": TOKEN_CALIBRATION_METHOD,
        "dataset_source": dataset_source.strip(),
        "estimator": {
            "version": estimator.version,
            "tokens_per_utf8_byte": estimator.tokens_per_utf8_byte,
            "fixed_overhead_tokens": estimator.fixed_overhead_tokens,
        },
        "split": {
            "calibration_sample_count": len(calibration),
            "holdout_sample_count": len(holdout),
            "calibration_manifest_hash": _manifest_hash(calibration),
            "holdout_manifest_hash": _manifest_hash(holdout),
            "sample_ids_disjoint": True,
            "content_fingerprints_disjoint": True,
            "error_evaluated_on": "holdout",
        },
        "coverage": coverage,
        "metrics": {
            "mean_absolute_percentage_error": round(
                sum(absolute_percentage_errors) / len(absolute_percentage_errors), 6
            ),
            "maximum_absolute_percentage_error": round(max(absolute_percentage_errors), 6),
            "maximum_underestimate_ratio": round(max(underestimate_ratios), 6),
        },
        "holdout_results": evaluated,
    }
    return validate_schema(report, "token_calibration_report.schema.json")


def load_token_calibration_fixture(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TokenCalibrationError("token calibration fixture must be an object")
    source = payload.get("dataset_source")
    samples = payload.get("samples")
    if not isinstance(source, str) or not isinstance(samples, list):
        raise TokenCalibrationError("fixture requires dataset_source and samples")
    calibration = [item for item in samples if isinstance(item, dict) and item.get("split") == "calibration"]
    holdout = [item for item in samples if isinstance(item, dict) and item.get("split") == "holdout"]
    if len(calibration) + len(holdout) != len(samples):
        raise TokenCalibrationError("every fixture sample must use calibration or holdout split")
    return calibration, holdout, source


def _normalize_samples(
    values: Iterable[Mapping[str, Any]],
    *,
    required_split: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, Mapping):
            raise TokenCalibrationError("samples must be objects")
        sample = dict(raw)
        required = {
            "id",
            "split",
            "text",
            "actual_tokens",
            "provider",
            "model",
            "endpoint_type",
            "language_profile",
            "model_is_known",
            "reference_source",
        }
        if set(sample) != required:
            raise TokenCalibrationError(f"sample fields must be exactly {sorted(required)}")
        sample_id = sample["id"]
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in seen:
            raise TokenCalibrationError("sample ids must be unique non-empty strings")
        seen.add(sample_id)
        if sample["split"] != required_split:
            raise TokenCalibrationError(
                f"{required_split} input contains {sample['split']!r} sample {sample_id}"
            )
        text = sample["text"]
        actual = sample["actual_tokens"]
        if not isinstance(text, str) or not text:
            raise TokenCalibrationError(f"sample {sample_id} text must be non-empty")
        if isinstance(actual, bool) or not isinstance(actual, int) or actual < 1:
            raise TokenCalibrationError(f"sample {sample_id} actual_tokens must be positive")
        if sample["endpoint_type"] not in SAFE_ENDPOINT_TYPES:
            raise TokenCalibrationError(f"sample {sample_id} endpoint_type is invalid")
        if sample["language_profile"] not in _LANGUAGE_PROFILES:
            raise TokenCalibrationError(f"sample {sample_id} language_profile is invalid")
        if not isinstance(sample["model_is_known"], bool):
            raise TokenCalibrationError(f"sample {sample_id} model_is_known must be boolean")
        for field in ("provider", "model", "reference_source"):
            if not isinstance(sample[field], str) or not sample[field].strip():
                raise TokenCalibrationError(f"sample {sample_id} {field} must be explicit")
        sample["utf8_bytes"] = len(text.encode("utf-8"))
        result.append(sample)
    return result


def _reference_count_mode(sample: Mapping[str, Any]) -> str:
    if (
        sample["endpoint_type"] == "official"
        and sample["model_is_known"]
        and sample["reference_source"] == "provider_usage"
    ):
        return "provider_exact"
    return "calibration_reference"


def _coverage(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "providers": sorted({sample["provider"] for sample in samples}),
        "models": sorted({sample["model"] for sample in samples}),
        "endpoint_types": sorted({sample["endpoint_type"] for sample in samples}),
        "language_profiles": sorted({sample["language_profile"] for sample in samples}),
        "known_model_count": sum(bool(sample["model_is_known"]) for sample in samples),
        "unknown_model_count": sum(not bool(sample["model_is_known"]) for sample in samples),
    }


def _sample_fingerprint(sample: Mapping[str, Any]) -> str:
    payload = {
        "text": sample["text"],
        "actual_tokens": sample["actual_tokens"],
        "provider": sample["provider"],
        "model": sample["model"],
        "endpoint_type": sample["endpoint_type"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_hash(samples: list[dict[str, Any]]) -> str:
    manifest = [
        {"id": sample["id"], "fingerprint": _sample_fingerprint(sample)}
        for sample in sorted(samples, key=lambda item: item["id"])
    ]
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "TOKEN_CALIBRATION_METHOD",
    "TOKEN_CALIBRATION_SCHEMA_VERSION",
    "TOKEN_CALIBRATION_SOURCE_SYNTHETIC",
    "TokenCalibrationError",
    "build_token_calibration_report",
    "fit_token_estimator",
    "load_token_calibration_fixture",
]
