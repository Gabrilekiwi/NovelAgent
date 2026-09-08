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


CHAPTER_CANDIDATE_PROVIDER_RESOLUTION_VERSION = "1.0"
CHAPTER_CANDIDATE_PROVIDER_WARNING_VERSION = (
    "candidate-provider-uncertain-abandon-v1"
)
_CANDIDATE_ID = re.compile(r"^candidate_[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ChapterCandidateProviderResolutionError(RuntimeError):
    pass


class ChapterCandidateProviderResolutionConflict(
    ChapterCandidateProviderResolutionError
):
    pass


class ChapterCandidateProviderResolutionIntegrityError(
    ChapterCandidateProviderResolutionError
):
    pass


def candidate_provider_uncertainty_sha256(
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


def build_candidate_provider_resolution(
    *,
    created_at: str,
    book_id: str,
    chapter_index: int,
    candidate_id: str,
    execution_id: str,
    candidate_intent_hash: str,
    candidate_record_hash: str | None,
    unresolved_calls: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    candidate_id = _candidate_id(candidate_id)
    calls = _normalized_calls(unresolved_calls)
    payload: dict[str, Any] = {
        "schema_version": CHAPTER_CANDIDATE_PROVIDER_RESOLUTION_VERSION,
        "record_type": "chapter_candidate_provider_resolution",
        "resolution_id": f"resolution_{candidate_id}",
        "created_at": str(created_at),
        "action": "abandon_uncertain_provider_result",
        "book_id": str(book_id),
        "chapter_index": chapter_index,
        "candidate_id": candidate_id,
        "execution_id": str(execution_id),
        "candidate_intent_hash": str(candidate_intent_hash),
        "candidate_record_hash": candidate_record_hash,
        "uncertainty_sha256": candidate_provider_uncertainty_sha256(
            execution_id,
            calls,
        ),
        "unresolved_calls": calls,
        "author_confirmation": {
            "confirmed": True,
            "warning_version": CHAPTER_CANDIDATE_PROVIDER_WARNING_VERSION,
        },
    }
    payload["resolution_hash"] = canonical_json_hash(
        payload,
        exclude_environment_fields=False,
    )
    return validate_candidate_provider_resolution(payload)


def validate_candidate_provider_resolution(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution must be an object"
        )
    payload = copy.deepcopy(dict(value))
    try:
        validate_schema(
            payload,
            "chapter_candidate_provider_resolution.schema.json",
        )
    except (SchemaValidationError, TypeError) as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(str(exc)) from exc

    candidate_id = _candidate_id(payload["candidate_id"])
    if payload["resolution_id"] != f"resolution_{candidate_id}":
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution id binding mismatch"
        )
    expected_execution_id = _execution_id(candidate_id)
    if payload["execution_id"] != expected_execution_id:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution execution binding mismatch"
        )
    if isinstance(payload["chapter_index"], bool):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution chapter index is invalid"
        )
    _timestamp(payload["created_at"])
    _sha256("candidate_intent_hash", payload["candidate_intent_hash"])
    if payload["candidate_record_hash"] is not None:
        _sha256("candidate_record_hash", payload["candidate_record_hash"])

    normalized = _normalized_calls(payload["unresolved_calls"])
    if normalized != payload["unresolved_calls"]:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution calls are not in canonical order"
        )
    expected_uncertainty = candidate_provider_uncertainty_sha256(
        payload["execution_id"],
        normalized,
    )
    if payload["uncertainty_sha256"] != expected_uncertainty:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider uncertainty hash mismatch"
        )
    expected_hash = canonical_json_hash(
        payload,
        exclude_fields=("resolution_hash",),
        exclude_environment_fields=False,
    )
    if payload["resolution_hash"] != expected_hash:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution hash mismatch"
        )
    return payload


def publish_candidate_provider_resolution(
    run_dir: str | Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    payload = validate_candidate_provider_resolution(value)
    _verify_resolution_evidence(Path(run_dir).resolve(), payload)
    path = candidate_provider_resolution_path(run_dir, payload["candidate_id"])
    try:
        atomic_create_json(path, payload)
        return payload
    except OSError:
        if not path.is_file():
            raise
        existing = _load_resolution(path)
        _verify_resolution_evidence(Path(run_dir).resolve(), existing)
        if existing == payload:
            return existing
        raise ChapterCandidateProviderResolutionConflict(
            "candidate provider resolution already exists with different immutable content"
        )


def candidate_provider_resolution_path(
    run_dir: str | Path,
    candidate_id: str,
) -> Path:
    candidate_id = _candidate_id(candidate_id)
    root = Path(run_dir).resolve() / "candidate_provider_resolutions"
    assert_safe_local_tree(root)
    return root / f"resolution_{candidate_id}.json"


def load_candidate_provider_resolution(
    run_dir: str | Path,
    candidate_id: str,
) -> dict[str, Any] | None:
    path = candidate_provider_resolution_path(run_dir, candidate_id)
    if not path.exists():
        return None
    resolution = _load_resolution(path)
    if resolution["candidate_id"] != _candidate_id(candidate_id):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution path binding mismatch"
        )
    _verify_resolution_evidence(Path(run_dir).resolve(), resolution)
    return resolution


def load_candidate_provider_resolutions(
    run_dir: str | Path,
) -> list[dict[str, Any]]:
    root = Path(run_dir).resolve() / "candidate_provider_resolutions"
    assert_safe_local_tree(root)
    if not root.is_dir():
        return []
    resolutions: list[dict[str, Any]] = []
    for path in sorted(root.glob("resolution_candidate_*.json")):
        resolution = _load_resolution(path)
        expected_name = f"resolution_{resolution['candidate_id']}.json"
        if path.name != expected_name:
            raise ChapterCandidateProviderResolutionIntegrityError(
                "candidate provider resolution filename binding mismatch"
            )
        _verify_resolution_evidence(Path(run_dir).resolve(), resolution)
        resolutions.append(resolution)
    return sorted(
        resolutions,
        key=lambda item: (str(item["created_at"]), str(item["resolution_id"])),
    )


def abandoned_provider_attempts(
    run_dir: str | Path,
) -> set[tuple[str, str, str, str, str]]:
    return {
        provider_attempt_binding(resolution["execution_id"], call)
        for resolution in load_candidate_provider_resolutions(run_dir)
        for call in resolution["unresolved_calls"]
    }


def provider_attempt_binding(
    execution_id: str,
    call: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    if not isinstance(call, Mapping):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "provider call binding must be an object"
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
        raise ChapterCandidateProviderResolutionIntegrityError(
            "unresolved_calls must be an array"
        )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    try:
        items = list(calls)
    except TypeError as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "unresolved_calls must be an array"
        ) from exc
    if not items:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "unresolved_calls must not be empty"
        )
    for value in items:
        if not isinstance(value, Mapping):
            raise ChapterCandidateProviderResolutionIntegrityError(
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
            raise ChapterCandidateProviderResolutionIntegrityError(
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
    _assert_plain_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"cannot read candidate provider resolution {path}: {exc}"
        ) from exc
    return validate_candidate_provider_resolution(value)


def _verify_resolution_evidence(
    run_dir: Path,
    resolution: Mapping[str, Any],
) -> None:
    runtime_root = run_dir.resolve().parent
    candidate_root = runtime_root / "candidates" / str(resolution["candidate_id"])
    assert_safe_local_tree(candidate_root)
    intent_path = candidate_root / "intent.json"
    intent = _load_bound_json(intent_path, "chapter candidate intent")
    try:
        validate_schema(intent, "chapter_candidate_intent.schema.json")
    except SchemaValidationError as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(str(exc)) from exc
    if canonical_json_hash(
        intent,
        exclude_fields=("intent_hash",),
        exclude_environment_fields=False,
    ) != intent.get("intent_hash"):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "resolved candidate intent hash mismatch"
        )
    if (
        intent.get("candidate_id") != resolution["candidate_id"]
        or intent.get("book_id") != resolution["book_id"]
        or intent.get("chapter_index") != resolution["chapter_index"]
        or intent.get("intent_hash") != resolution["candidate_intent_hash"]
        or intent.get("execution", {}).get("execution_id")
        != resolution["execution_id"]
    ):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution does not match its Candidate Intent"
        )

    record_path = candidate_root / "record.json"
    expected_record_hash = resolution["candidate_record_hash"]
    if expected_record_hash is None:
        if record_path.exists():
            raise ChapterCandidateProviderResolutionIntegrityError(
                "orphan Candidate resolution unexpectedly has a terminal record"
            )
    else:
        record = _load_bound_json(record_path, "chapter candidate record")
        try:
            validate_schema(record, "chapter_candidate_record.schema.json")
        except SchemaValidationError as exc:
            raise ChapterCandidateProviderResolutionIntegrityError(str(exc)) from exc
        if canonical_json_hash(
            record,
            exclude_fields=("record_hash",),
            exclude_environment_fields=False,
        ) != record.get("record_hash"):
            raise ChapterCandidateProviderResolutionIntegrityError(
                "resolved candidate record hash mismatch"
            )
        if (
            record.get("record_hash") != expected_record_hash
            or record.get("candidate_id") != resolution["candidate_id"]
            or record.get("intent_hash") != resolution["candidate_intent_hash"]
            or record.get("status") != "provider_uncertain"
        ):
            raise ChapterCandidateProviderResolutionIntegrityError(
                "candidate provider resolution does not match its uncertain record"
            )
        record_calls = _normalized_calls(record.get("evidence", {}).get("unresolved_calls", []))
        if record_calls != resolution["unresolved_calls"]:
            raise ChapterCandidateProviderResolutionIntegrityError(
                "candidate provider resolution calls do not match its terminal record"
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
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution calls do not match durable model-call evidence"
        )
    for call in resolution["unresolved_calls"]:
        intent_evidence = store.load_intent(call["attempt_id"])
        for field in ("call_id", "attempt_id", "intent_hash", "request_digest"):
            if intent_evidence[field] != call[field]:
                raise ChapterCandidateProviderResolutionIntegrityError(
                    f"candidate provider resolution {field} binding mismatch"
                )


def _load_bound_json(path: Path, label: str) -> dict[str, Any]:
    _assert_plain_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"cannot read {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"{label} must contain a JSON object"
        )
    return value


def _assert_plain_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"cannot inspect candidate provider resolution {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolutions cannot be links or reparse points"
        )
    if not stat.S_ISREG(info.st_mode):
        raise ChapterCandidateProviderResolutionIntegrityError(
            "candidate provider resolution must be a regular file"
        )


def _execution_id(candidate_id: str) -> str:
    return f"execution_candidate_{_candidate_id(candidate_id).removeprefix('candidate_')[:32]}"


def _candidate_id(value: Any) -> str:
    text = str(value)
    if _CANDIDATE_ID.fullmatch(text) is None:
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"unsafe candidate id: {text!r}"
        )
    return text


def _safe_id(field: str, value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"{field} must be a safe identifier"
        )
    return value


def _sha256(field: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ChapterCandidateProviderResolutionIntegrityError(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "created_at must be an ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "created_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChapterCandidateProviderResolutionIntegrityError(
            "created_at must include a timezone"
        )
    return value


__all__ = [
    "CHAPTER_CANDIDATE_PROVIDER_RESOLUTION_VERSION",
    "CHAPTER_CANDIDATE_PROVIDER_WARNING_VERSION",
    "ChapterCandidateProviderResolutionConflict",
    "ChapterCandidateProviderResolutionError",
    "ChapterCandidateProviderResolutionIntegrityError",
    "abandoned_provider_attempts",
    "build_candidate_provider_resolution",
    "candidate_provider_resolution_path",
    "candidate_provider_uncertainty_sha256",
    "load_candidate_provider_resolution",
    "load_candidate_provider_resolutions",
    "provider_attempt_binding",
    "publish_candidate_provider_resolution",
    "validate_candidate_provider_resolution",
]
