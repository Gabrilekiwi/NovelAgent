from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from api.contracts import (
    CHAPTER_CONTRACT,
    ModelOutputError,
    validate_language_output,
)


class SceneSourceProvenanceError(ValueError):
    """A recovered Scene is not bound to durable model-call evidence."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_FIELDS = (
    "source_run_id",
    "execution_id",
    "call_id",
    "attempt_id",
    "receipt_hash",
    "response_artifact_hash",
)
_FIELDS = (
    "source_run_id",
    "source_run_sha256",
    "execution_id",
    "call_id",
    "attempt_id",
    "receipt_hash",
    "response_artifact_hash",
)
_ID_FIELDS = frozenset(
    {"source_run_id", "execution_id", "call_id", "attempt_id"}
)
_HASH_FIELDS = frozenset(
    {"source_run_sha256", "receipt_hash", "response_artifact_hash"}
)


def validate_scene_source_provenance(value: Any) -> dict[str, str]:
    return _validate_scene_source_provenance_fields(value, fields=_FIELDS)


def validate_legacy_scene_source_provenance(value: Any) -> dict[str, str]:
    """Validate readable pre-1.2 history that is never reusable."""

    return _validate_scene_source_provenance_fields(
        value,
        fields=_LEGACY_FIELDS,
    )


def _validate_scene_source_provenance_fields(
    value: Any,
    *,
    fields: tuple[str, ...],
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise SceneSourceProvenanceError(
            "scene source_provenance must contain exactly the required evidence fields"
        )
    result: dict[str, str] = {}
    for field in fields:
        raw = value.get(field)
        if field in _ID_FIELDS:
            if not isinstance(raw, str) or not _SAFE_ID.fullmatch(raw):
                raise SceneSourceProvenanceError(
                    f"scene source_provenance.{field} must be a safe identifier"
                )
        elif field in _HASH_FIELDS:
            if not isinstance(raw, str) or not _SHA256.fullmatch(raw):
                raise SceneSourceProvenanceError(
                    f"scene source_provenance.{field} must be a lowercase sha256 digest"
                )
        else:
            raise SceneSourceProvenanceError(
                f"scene source_provenance contains unsupported field {field}"
            )
        result[field] = raw
    if not result["execution_id"].startswith("execution_"):
        raise SceneSourceProvenanceError(
            "scene source_provenance.execution_id must identify an execution"
        )
    return result


def verified_scene_source_provenance(
    run_dir: str | Path,
    *,
    source_run_id: str,
    execution_id: str,
    source_run_sha256: str | None = None,
    attempt_id: str | None = None,
    call_id: str | None = None,
    expected_chapter_index: int | None = None,
    expected_book_id: str | None = None,
) -> dict[str, str]:
    """Resolve one Scene source from immutable intent/receipt/response evidence."""

    run_root = Path(run_dir).resolve()
    source_run_id = _safe_id("source_run_id", source_run_id)
    execution_id = _safe_id("execution_id", execution_id)
    if not execution_id.startswith("execution_"):
        raise SceneSourceProvenanceError("execution_id must identify an execution")
    requested_attempt = _optional_safe_id("attempt_id", attempt_id)
    requested_call = _optional_safe_id("call_id", call_id)
    if requested_attempt is None and requested_call is None:
        raise SceneSourceProvenanceError("attempt_id or call_id is required")
    expected_source_run_sha256 = (
        _sha256("source_run_sha256", source_run_sha256)
        if source_run_sha256 is not None
        else None
    )

    source_run, actual_source_run_sha256 = _verify_source_run_binding(
        run_root,
        source_run_id=source_run_id,
        execution_id=execution_id,
        expected_source_run_sha256=expected_source_run_sha256,
    )
    _verify_source_run_scope(
        source_run,
        expected_chapter_index=expected_chapter_index,
        expected_book_id=expected_book_id,
    )
    from core.model_calls import ModelCallStore

    store = ModelCallStore(
        _execution_model_call_root(run_root, execution_id=execution_id)
    )
    if requested_attempt is not None:
        candidates = [requested_attempt]
    else:
        candidates = _attempts_for_call(store, requested_call or "")
    for candidate in reversed(candidates):
        try:
            intent = store.load_intent(candidate)
            receipt = store.load_receipt(candidate)
        except (OSError, ValueError, RuntimeError):
            if requested_attempt is not None:
                break
            continue
        if (
            intent.get("stage") != "chapter_generation"
            or receipt.get("status") != "succeeded"
            or not receipt.get("response_artifact_ref")
            or (requested_call is not None and intent.get("call_id") != requested_call)
        ):
            if requested_attempt is not None:
                break
            continue
        try:
            _verify_response_artifact(store, receipt)
        except SceneSourceProvenanceError:
            if requested_attempt is not None:
                break
            continue
        return {
            "source_run_id": source_run_id,
            "source_run_sha256": actual_source_run_sha256,
            "execution_id": execution_id,
            "call_id": str(intent["call_id"]),
            "attempt_id": str(intent["attempt_id"]),
            "receipt_hash": str(receipt["receipt_hash"]),
            "response_artifact_hash": str(receipt["response_artifact_hash"]),
        }
    raise SceneSourceProvenanceError(
        "no matching successful chapter-generation receipt with a verified response artifact"
    )


def verify_scene_source_provenance(
    run_dir: str | Path,
    value: Any,
    *,
    expected_source_run_id: str | None = None,
    expected_chapter_index: int | None = None,
    expected_book_id: str | None = None,
) -> dict[str, str]:
    declared = validate_scene_source_provenance(value)
    if (
        expected_source_run_id is not None
        and declared["source_run_id"] != _safe_id(
            "expected_source_run_id",
            expected_source_run_id,
        )
    ):
        raise SceneSourceProvenanceError(
            "scene source_provenance.source_run_id does not match its recovery marker"
        )
    actual = verified_scene_source_provenance(
        run_dir,
        source_run_id=declared["source_run_id"],
        execution_id=declared["execution_id"],
        source_run_sha256=declared["source_run_sha256"],
        attempt_id=declared["attempt_id"],
        call_id=declared["call_id"],
        expected_chapter_index=expected_chapter_index,
        expected_book_id=expected_book_id,
    )
    if actual != declared:
        raise SceneSourceProvenanceError(
            "scene source_provenance does not match its immutable model-call evidence"
        )
    return declared


def verified_scene_source_response(
    run_dir: str | Path,
    value: Any,
    *,
    language: str | None = None,
    expected_source_run_id: str | None = None,
    expected_chapter_index: int | None = None,
    expected_book_id: str | None = None,
) -> dict[str, Any]:
    """Return the normalized Scene response bound to verified source evidence."""

    declared = verify_scene_source_provenance(
        run_dir,
        value,
        expected_source_run_id=expected_source_run_id,
        expected_chapter_index=expected_chapter_index,
        expected_book_id=expected_book_id,
    )
    from core.model_calls import ModelCallStore

    store = ModelCallStore(
        _execution_model_call_root(
            Path(run_dir).resolve(),
            execution_id=declared["execution_id"],
        )
    )
    try:
        receipt = store.load_receipt(declared["attempt_id"])
        raw = _read_verified_response_artifact(store, receipt)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SceneSourceProvenanceError(
            "scene source response artifact cannot be read from its verified receipt"
        ) from exc
    response = normalize_scene_response(raw, language=language)
    if response is None:
        raise SceneSourceProvenanceError(
            "scene source response is not a valid chapter-generation Scene payload"
        )
    return response


def normalize_scene_response(
    raw: str,
    *,
    language: str | None = None,
) -> dict[str, Any] | None:
    """Normalize the structured Scene contract or a legacy prose response."""

    text = str(raw or "").strip()
    fenced = None
    if text.startswith("```"):
        closing = text.rfind("```")
        if closing > 3:
            opening_end = text.find("\n")
            if 0 <= opening_end < closing:
                fenced = text[opening_end + 1 : closing].strip()
    candidate = fenced if fenced is not None else text
    value: Any = None
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if 0 <= start < end:
            try:
                value = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                value = None

    if isinstance(value, dict):
        if "prose" not in value:
            return None
        try:
            prose = validate_language_output(
                value.get("prose"),
                CHAPTER_CONTRACT,
                language=language,
            )
        except ModelOutputError:
            return None
        raw_events = value.get("events")
        raw_deltas = value.get("deltas")
        if not isinstance(raw_events, list) or not isinstance(raw_deltas, dict):
            return None
        events: list[dict[str, Any]] = []
        for item in raw_events:
            if not isinstance(item, dict):
                return None
            event_id = str(item.get("event_id") or "").strip()
            event_type = str(item.get("type") or "").strip()
            if not event_id or not event_type:
                return None
            events.append(
                {
                    "event_id": event_id,
                    "type": event_type,
                    "subjects": [
                        str(subject)
                        for subject in item.get("subjects") or []
                        if str(subject).strip()
                    ],
                    "objects": [
                        str(obj)
                        for obj in item.get("objects") or []
                        if str(obj).strip()
                    ],
                    "location": str(item.get("location") or ""),
                    "status": str(item.get("status") or "completed"),
                }
            )
        deltas: dict[str, list[dict[str, Any]]] = {}
        for key in (
            "characters",
            "relationships",
            "rosters",
            "locations",
            "inventory",
            "counters",
        ):
            items = raw_deltas.get(key) or []
            if not isinstance(items, list) or any(
                not isinstance(item, dict)
                for item in items
            ):
                return None
            deltas[key] = [dict(item) for item in items]
        return {
            "text": prose,
            "events": events,
            "deltas": deltas,
            "continuity_note": str(value.get("continuity_note") or ""),
        }

    if text.startswith(("{", "```")):
        return None
    try:
        prose = validate_language_output(
            text,
            CHAPTER_CONTRACT,
            language=language,
        )
    except ModelOutputError:
        return None
    return {"text": prose}


def _verify_source_run_binding(
    run_dir: Path,
    *,
    source_run_id: str,
    execution_id: str,
    expected_source_run_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    path = (run_dir / f"{source_run_id}.json").resolve()
    try:
        path.relative_to(run_dir)
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise SceneSourceProvenanceError(
            "scene source run record is unavailable or unsafe"
        ) from exc
    actual_source_run_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        expected_source_run_sha256 is not None
        and actual_source_run_sha256 != expected_source_run_sha256
    ):
        raise SceneSourceProvenanceError(
            "scene source run raw bytes do not match source_run_sha256"
        )
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SceneSourceProvenanceError(
            "scene source run record is invalid JSON"
        ) from exc
    run = payload.get("run") if isinstance(payload, dict) else None
    evidence = run.get("execution_evidence") if isinstance(run, dict) else None
    if (
        not isinstance(evidence, dict)
        or str(run.get("id") or "") != source_run_id
        or str(evidence.get("execution_id") or "") != execution_id
    ):
        raise SceneSourceProvenanceError(
            "scene source run is not bound to the declared execution"
        )
    return run, actual_source_run_sha256


def _verify_source_run_scope(
    run: dict[str, Any],
    *,
    expected_chapter_index: int | None,
    expected_book_id: str | None,
) -> None:
    if (
        expected_chapter_index is not None
        and int(run.get("chapter_index") or 0) != int(expected_chapter_index)
    ):
        raise SceneSourceProvenanceError(
            "scene source run chapter_index does not match its recovery marker"
        )
    if expected_book_id is None:
        return
    story_project = run.get("story_project")
    project_identity = (
        story_project.get("project_identity")
        if isinstance(story_project, dict)
        else None
    )
    source_book_id = (
        run.get("book_id")
        or (
            story_project.get("book_id")
            if isinstance(story_project, dict)
            else None
        )
        or (
            project_identity.get("book_id")
            if isinstance(project_identity, dict)
            else None
        )
    )
    if str(source_book_id or "") != str(expected_book_id):
        raise SceneSourceProvenanceError(
            "scene source run book_id does not match its recovery marker"
        )


def _execution_model_call_root(run_dir: Path, *, execution_id: str) -> Path:
    root = (run_dir / "executions" / execution_id / "model_calls").resolve()
    try:
        root.relative_to(run_dir)
    except ValueError as exc:
        raise SceneSourceProvenanceError(
            "scene source execution escaped the runtime evidence root"
        ) from exc
    return root


def _attempts_for_call(store: Any, call_id: str) -> list[str]:
    candidates: list[tuple[str, str]] = []
    if not store.intents_dir.is_dir():
        return []
    for path in sorted(store.intents_dir.glob("*.json")):
        try:
            intent = store.load_intent(path.stem)
        except (OSError, ValueError, RuntimeError):
            continue
        if str(intent.get("call_id") or "") != call_id:
            continue
        candidates.append(
            (str(intent.get("created_at") or ""), str(intent.get("attempt_id") or ""))
        )
    return [
        attempt_id
        for _created_at, attempt_id in sorted(candidates)
        if attempt_id
    ]


def _verify_response_artifact(
    store: Any,
    receipt: dict[str, Any],
) -> None:
    _read_verified_response_artifact(store, receipt)


def _read_verified_response_artifact(
    store: Any,
    receipt: dict[str, Any],
) -> str:
    from core.model_calls import model_response_artifact_hash

    relative = Path(str(receipt["response_artifact_ref"]))
    try:
        path = (store.root / relative).resolve(strict=True)
        path.relative_to(store.root)
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise SceneSourceProvenanceError(
            "scene source response artifact is unavailable or unsafe"
        ) from exc
    if model_response_artifact_hash(raw) != receipt["response_artifact_hash"]:
        raise SceneSourceProvenanceError(
            "scene source response artifact hash does not match its receipt"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SceneSourceProvenanceError(
            "scene source response artifact is not UTF-8 text"
        ) from exc


def _safe_id(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise SceneSourceProvenanceError(f"{field} must be a safe identifier")
    return value


def _optional_safe_id(field: str, value: Any) -> str | None:
    if value is None:
        return None
    return _safe_id(field, value)


def _sha256(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SceneSourceProvenanceError(
            f"{field} must be a lowercase sha256 digest"
        )
    return value


__all__ = [
    "SceneSourceProvenanceError",
    "normalize_scene_response",
    "validate_legacy_scene_source_provenance",
    "validate_scene_source_provenance",
    "verified_scene_source_provenance",
    "verified_scene_source_response",
    "verify_scene_source_provenance",
]
