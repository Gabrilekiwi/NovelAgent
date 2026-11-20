from __future__ import annotations

import copy
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.engine.persistence import atomic_create_json
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import ModelCallStore
from core.schema import SchemaValidationError, validate_schema


CHAPTER_FACT_EXTRACTION_PROVIDER_RESOLUTION_VERSION = "1.0"
CHAPTER_FACT_EXTRACTION_PROVIDER_WARNING_VERSION = (
    "fact-extraction-provider-uncertain-abandon-v1"
)
_EXTRACTION_ID = re.compile(r"^fact_extraction_[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ChapterFactExtractionProviderResolutionError(RuntimeError):
    """Base error for append-only fact-extraction Provider resolutions."""


class ChapterFactExtractionProviderResolutionConflict(
    ChapterFactExtractionProviderResolutionError
):
    """A resolution or its author-confirmed binding changed concurrently."""


class ChapterFactExtractionProviderResolutionIntegrityError(
    ChapterFactExtractionProviderResolutionError
):
    """Persisted resolution evidence is missing, malformed, or inconsistent."""


def fact_extraction_provider_uncertainty_sha256(
    execution_id: str,
    calls: Iterable[Mapping[str, Any]],
) -> str:
    normalized = _normalized_calls(calls)
    return canonical_json_hash(
        {
            "execution_id": _safe_id("execution_id", execution_id),
            "unresolved_calls": normalized,
        },
        exclude_environment_fields=False,
    )


def build_fact_extraction_provider_resolution(
    *,
    created_at: str,
    book_id: str,
    chapter_index: int,
    extraction_id: str,
    execution_id: str,
    fact_extraction_intent_hash: str,
    fact_extraction_record_hash: str | None,
    unresolved_calls: Iterable[Mapping[str, Any]],
    author_confirmed: bool,
) -> dict[str, Any]:
    """Build an exact, author-confirmed abandonment marker.

    The marker accepts the risk of an unknown Provider outcome. It does not
    fabricate a Receipt, adopt any response, or alter the extraction record.
    """

    if author_confirmed is not True:
        raise ChapterFactExtractionProviderResolutionConflict(
            "explicit author confirmation is required to abandon Provider uncertainty"
        )
    extraction_id = _extraction_id(extraction_id)
    calls = _normalized_calls(unresolved_calls)
    payload: dict[str, Any] = {
        "schema_version": CHAPTER_FACT_EXTRACTION_PROVIDER_RESOLUTION_VERSION,
        "record_type": "chapter_fact_extraction_provider_resolution",
        "resolution_id": f"resolution_{extraction_id}",
        "created_at": str(created_at),
        "action": "abandon_uncertain_provider_result",
        "book_id": _required_text("book_id", book_id),
        "chapter_index": chapter_index,
        "extraction_id": extraction_id,
        "execution_id": str(execution_id),
        "fact_extraction_intent_hash": str(fact_extraction_intent_hash),
        "fact_extraction_record_hash": fact_extraction_record_hash,
        "uncertainty_sha256": fact_extraction_provider_uncertainty_sha256(
            execution_id,
            calls,
        ),
        "unresolved_calls": calls,
        "author_confirmation": {
            "confirmed": True,
            "warning_version": CHAPTER_FACT_EXTRACTION_PROVIDER_WARNING_VERSION,
        },
    }
    payload["resolution_hash"] = canonical_json_hash(
        payload,
        exclude_environment_fields=False,
    )
    return validate_fact_extraction_provider_resolution(payload)


def validate_fact_extraction_provider_resolution(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution must be an object"
        )
    payload = copy.deepcopy(dict(value))
    try:
        validate_schema(
            payload,
            "chapter_fact_extraction_provider_resolution.schema.json",
        )
    except (SchemaValidationError, TypeError) as exc:
        raise ChapterFactExtractionProviderResolutionIntegrityError(str(exc)) from exc

    extraction_id = _extraction_id(payload["extraction_id"])
    if payload["resolution_id"] != f"resolution_{extraction_id}":
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution id binding mismatch"
        )
    expected_execution_id = _execution_id(extraction_id)
    if payload["execution_id"] != expected_execution_id:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution execution binding mismatch"
        )
    if (
        isinstance(payload["chapter_index"], bool)
        or not isinstance(payload["chapter_index"], int)
        or payload["chapter_index"] < 1
    ):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution chapter index is invalid"
        )
    _required_text("book_id", payload["book_id"])
    _timestamp(payload["created_at"])
    _sha256(
        "fact_extraction_intent_hash",
        payload["fact_extraction_intent_hash"],
    )
    if payload["fact_extraction_record_hash"] is not None:
        _sha256(
            "fact_extraction_record_hash",
            payload["fact_extraction_record_hash"],
        )

    normalized = _normalized_calls(payload["unresolved_calls"])
    if normalized != payload["unresolved_calls"]:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution calls are not in canonical order"
        )
    expected_uncertainty = fact_extraction_provider_uncertainty_sha256(
        payload["execution_id"],
        normalized,
    )
    if payload["uncertainty_sha256"] != expected_uncertainty:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider uncertainty hash mismatch"
        )
    expected_hash = canonical_json_hash(
        payload,
        exclude_fields=("resolution_hash",),
        exclude_environment_fields=False,
    )
    if payload["resolution_hash"] != expected_hash:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution hash mismatch"
        )
    return payload


def publish_fact_extraction_provider_resolution(
    run_dir: str | Path,
    value: Mapping[str, Any],
    *,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    payload = validate_fact_extraction_provider_resolution(value)
    run_root, artifact_root = _bound_roots(run_dir, runtime_root)
    _verify_resolution_evidence(run_root, artifact_root, payload)
    path = fact_extraction_provider_resolution_path(
        run_root,
        payload["extraction_id"],
    )
    try:
        atomic_create_json(path, payload)
        return payload
    except OSError:
        if not path.is_file():
            raise
        existing = _load_resolution(path)
        _verify_resolution_evidence(run_root, artifact_root, existing)
        if existing == payload:
            return existing
        raise ChapterFactExtractionProviderResolutionConflict(
            "fact-extraction Provider resolution already exists with different immutable content"
        )


def fact_extraction_provider_resolution_path(
    run_dir: str | Path,
    extraction_id: str,
) -> Path:
    extraction_id = _extraction_id(extraction_id)
    root = Path(run_dir).resolve() / "fx_provider_resolutions"
    assert_safe_local_tree(root)
    return root / f"{extraction_id}.json"


def load_fact_extraction_provider_resolution(
    run_dir: str | Path,
    extraction_id: str,
    *,
    runtime_root: str | Path | None = None,
) -> dict[str, Any] | None:
    run_root, artifact_root = _bound_roots(run_dir, runtime_root)
    path = fact_extraction_provider_resolution_path(run_root, extraction_id)
    if not path.exists():
        return None
    resolution = _load_resolution(path)
    if resolution["extraction_id"] != _extraction_id(extraction_id):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution path binding mismatch"
        )
    _verify_resolution_evidence(run_root, artifact_root, resolution)
    return resolution


def load_fact_extraction_provider_resolutions(
    run_dir: str | Path,
    *,
    runtime_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    run_root, artifact_root = _bound_roots(run_dir, runtime_root)
    root = run_root / "fx_provider_resolutions"
    assert_safe_local_tree(root)
    if not root.is_dir():
        return []
    resolutions: list[dict[str, Any]] = []
    for path in sorted(root.glob("fact_extraction_*.json")):
        resolution = _load_resolution(path)
        expected_name = f"{resolution['extraction_id']}.json"
        if path.name != expected_name:
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "fact-extraction Provider resolution filename binding mismatch"
            )
        _verify_resolution_evidence(run_root, artifact_root, resolution)
        resolutions.append(resolution)
    return sorted(
        resolutions,
        key=lambda item: (str(item["created_at"]), str(item["resolution_id"])),
    )


def abandoned_fact_extraction_provider_attempts(
    run_dir: str | Path,
    *,
    runtime_root: str | Path | None = None,
) -> set[tuple[str, str, str, str, str]]:
    return {
        fact_extraction_provider_attempt_binding(resolution["execution_id"], call)
        for resolution in load_fact_extraction_provider_resolutions(
            run_dir,
            runtime_root=runtime_root,
        )
        for call in resolution["unresolved_calls"]
    }


def fact_extraction_provider_attempt_binding(
    execution_id: str,
    call: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    if not isinstance(call, Mapping):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "Provider call binding must be an object"
        )
    return (
        _safe_id("execution_id", execution_id),
        _safe_id("call_id", call.get("call_id")),
        _safe_id("attempt_id", call.get("attempt_id")),
        _sha256("intent_hash", call.get("intent_hash")),
        _sha256("request_digest", call.get("request_digest")),
    )


def _normalized_calls(
    calls: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if isinstance(calls, (str, bytes, Mapping)):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "unresolved_calls must be an array"
        )
    try:
        items = list(calls)
    except TypeError as exc:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "unresolved_calls must be an array"
        ) from exc
    if not items:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "unresolved_calls must not be empty"
        )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for value in items:
        if not isinstance(value, Mapping):
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "unresolved call must be an object"
            )
        item = {
            "call_id": _safe_id("call_id", value.get("call_id")),
            "attempt_id": _safe_id("attempt_id", value.get("attempt_id")),
            "intent_hash": _sha256("intent_hash", value.get("intent_hash")),
            "request_digest": _sha256(
                "request_digest",
                value.get("request_digest"),
            ),
        }
        key = (
            item["call_id"],
            item["attempt_id"],
            item["intent_hash"],
            item["request_digest"],
        )
        if key in seen:
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "unresolved_calls contains a duplicate binding"
            )
        seen.add(key)
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            item["call_id"],
            item["attempt_id"],
            item["intent_hash"],
            item["request_digest"],
        ),
    )


def _load_resolution(path: Path) -> dict[str, Any]:
    value = _load_bound_json(path, "fact-extraction Provider resolution")
    return validate_fact_extraction_provider_resolution(value)


def _verify_resolution_evidence(
    run_dir: Path,
    runtime_root: Path,
    resolution: Mapping[str, Any],
) -> None:
    extraction_root = runtime_root / "fact_extractions" / str(
        resolution["extraction_id"]
    )
    assert_safe_local_tree(extraction_root)
    intent_path = extraction_root / "intent.json"
    intent = _load_bound_json(intent_path, "chapter fact-extraction intent")
    if intent.get("record_type") != "chapter_fact_extraction_intent":
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "resolved fact-extraction Intent has the wrong record type"
        )
    try:
        actual_intent_hash = canonical_json_hash(
            intent,
            exclude_fields=("intent_hash",),
            exclude_environment_fields=False,
        )
    except (TypeError, ValueError) as exc:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "resolved fact-extraction Intent is not canonical JSON"
        ) from exc
    if actual_intent_hash != intent.get("intent_hash"):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "resolved fact-extraction Intent hash mismatch"
        )
    execution = intent.get("execution")
    if not isinstance(execution, Mapping):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "resolved fact-extraction Intent has no execution binding"
        )
    if (
        intent.get("extraction_id") != resolution["extraction_id"]
        or intent.get("book_id") != resolution["book_id"]
        or intent.get("chapter_index") != resolution["chapter_index"]
        or intent.get("intent_hash")
        != resolution["fact_extraction_intent_hash"]
        or execution.get("execution_id") != resolution["execution_id"]
    ):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution does not match its Intent"
        )
    _sha256("request_sha256", intent.get("request_sha256"))

    record_path = extraction_root / "record.json"
    expected_record_hash = resolution["fact_extraction_record_hash"]
    if expected_record_hash is None:
        if record_path.exists():
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "orphan fact-extraction resolution unexpectedly has a terminal record"
            )
    else:
        record = _load_bound_json(record_path, "chapter fact-extraction record")
        if record.get("record_type") != "chapter_fact_extraction_record":
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "resolved fact-extraction record has the wrong record type"
            )
        try:
            actual_record_hash = canonical_json_hash(
                record,
                exclude_fields=("record_hash",),
                exclude_environment_fields=False,
            )
        except (TypeError, ValueError) as exc:
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "resolved fact-extraction record is not canonical JSON"
            ) from exc
        if actual_record_hash != record.get("record_hash"):
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "resolved fact-extraction record hash mismatch"
            )
        if (
            record.get("record_hash") != expected_record_hash
            or record.get("extraction_id") != resolution["extraction_id"]
            or record.get("intent_hash")
            != resolution["fact_extraction_intent_hash"]
            or record.get("status") != "provider_uncertain"
        ):
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "fact-extraction Provider resolution does not match its uncertain record"
            )
        evidence = record.get("evidence")
        record_calls = _normalized_calls(
            evidence.get("unresolved_calls", [])
            if isinstance(evidence, Mapping)
            else []
        )
        if record_calls != resolution["unresolved_calls"]:
            raise ChapterFactExtractionProviderResolutionIntegrityError(
                "fact-extraction Provider resolution calls do not match its terminal record"
            )

    model_root = (
        run_dir
        / "executions"
        / str(resolution["execution_id"])
        / "model_calls"
    )
    store = ModelCallStore(model_root)
    actual_calls = _normalized_calls(store.list_uncertain_calls())
    if actual_calls != resolution["unresolved_calls"]:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution calls do not match durable model-call evidence"
        )
    for call in resolution["unresolved_calls"]:
        intent_evidence = store.load_intent(call["attempt_id"])
        for field in ("call_id", "attempt_id", "intent_hash", "request_digest"):
            if intent_evidence[field] != call[field]:
                raise ChapterFactExtractionProviderResolutionIntegrityError(
                    f"fact-extraction Provider resolution {field} binding mismatch"
                )


def _load_bound_json(path: Path, label: str) -> dict[str, Any]:
    _assert_plain_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"cannot read {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"{label} must contain a JSON object"
        )
    return value


def _assert_plain_file(path: Path, label: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"cannot inspect {label} {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"{label} cannot be a link or reparse point"
        )
    if not stat.S_ISREG(info.st_mode):
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"{label} must be a regular file"
        )


def _bound_roots(
    run_dir: str | Path,
    runtime_root: str | Path | None,
) -> tuple[Path, Path]:
    run_root = Path(run_dir).resolve()
    artifact_root = (
        run_root.parent if runtime_root is None else Path(runtime_root).resolve()
    )
    if artifact_root != run_root.parent:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "fact-extraction Provider resolution runtime root is not bound to run_dir"
        )
    assert_safe_local_tree(run_root)
    assert_safe_local_tree(artifact_root)
    return run_root, artifact_root


def _execution_id(extraction_id: str) -> str:
    digest = _extraction_id(extraction_id).removeprefix("fact_extraction_")
    return f"execution_fx_{digest[:24]}"


def _extraction_id(value: Any) -> str:
    text = str(value)
    if _EXTRACTION_ID.fullmatch(text) is None:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"unsafe fact-extraction id: {text!r}"
        )
    return text


def _safe_id(field: str, value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"{field} must be a safe identifier"
        )
    return value


def _sha256(field: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _required_text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            f"{field} must be non-empty trimmed text"
        )
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "created_at must be an ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "created_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChapterFactExtractionProviderResolutionIntegrityError(
            "created_at must include a timezone"
        )
    return value


__all__ = [
    "CHAPTER_FACT_EXTRACTION_PROVIDER_RESOLUTION_VERSION",
    "CHAPTER_FACT_EXTRACTION_PROVIDER_WARNING_VERSION",
    "ChapterFactExtractionProviderResolutionConflict",
    "ChapterFactExtractionProviderResolutionError",
    "ChapterFactExtractionProviderResolutionIntegrityError",
    "abandoned_fact_extraction_provider_attempts",
    "build_fact_extraction_provider_resolution",
    "fact_extraction_provider_attempt_binding",
    "fact_extraction_provider_resolution_path",
    "fact_extraction_provider_uncertainty_sha256",
    "load_fact_extraction_provider_resolution",
    "load_fact_extraction_provider_resolutions",
    "publish_fact_extraction_provider_resolution",
    "validate_fact_extraction_provider_resolution",
]
