from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping

from api.contracts import MODEL_ENDPOINT_UNKNOWN, ModelResponse
from core.engine.persistence import atomic_create_json
from core.memory_v2.canonical import CANONICAL_JSON_ALGORITHM, canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


MODEL_CALL_SCHEMA_VERSION = "1.0"
PROVIDER_CALL_UNCERTAIN = "provider_call_uncertain"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SAFE_STATUS = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_UNSET = object()
_Clock = Callable[[], datetime]

_FORBIDDEN_PERSISTED_FIELDS = frozenset(
    {
        "prompt",
        "prompts",
        "messages",
        "message",
        "system_message",
        "body",
        "content",
        "text",
        "response",
        "raw_response",
        "raw_request",
        "request",
        "headers",
        "environment",
        "env",
        "absolute_path",
        "file_path",
        "storage_path",
    }
)
_CREDENTIAL_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "auth_token",
        "access_token",
        "refresh_token",
        "bearer",
        "bearer_token",
        "secret",
        "client_secret",
        "password",
        "credential",
        "credentials",
        "private_key",
    }
)


class ModelCallEvidenceError(RuntimeError):
    """Base error for durable model-call evidence."""


class ModelCallConflictError(ModelCallEvidenceError):
    """An immutable attempt id already exists with different content."""


class ModelCallIntegrityError(ModelCallEvidenceError):
    """Persisted evidence does not match its canonical hash or linkage."""


class ModelCallSafetyError(ModelCallEvidenceError):
    """Evidence contains payload, credential, or path material that must not persist."""


@dataclass(frozen=True)
class ModelCallIntent:
    call_id: str
    attempt_id: str
    request_digest: str
    provider: str
    model: str
    stage: str
    budget_reservation: Mapping[str, Any]
    created_at: str
    intent_hash: str = ""
    schema_version: str = MODEL_CALL_SCHEMA_VERSION
    canonical_json_algorithm: str = CANONICAL_JSON_ALGORITHM

    def __post_init__(self) -> None:
        budget = copy.deepcopy(dict(self.budget_reservation))
        object.__setattr__(self, "budget_reservation", MappingProxyType(budget))
        if not self.intent_hash:
            object.__setattr__(
                self,
                "intent_hash",
                canonical_json_hash(
                    self.to_dict(),
                    exclude_fields=("intent_hash",),
                    exclude_environment_fields=False,
                ),
            )
        validate_model_call_intent(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "canonical_json_algorithm": self.canonical_json_algorithm,
            "call_id": self.call_id,
            "attempt_id": self.attempt_id,
            "request_digest": self.request_digest,
            "provider": self.provider,
            "model": self.model,
            "stage": self.stage,
            "budget_reservation": copy.deepcopy(dict(self.budget_reservation)),
            "created_at": self.created_at,
            "intent_hash": self.intent_hash,
        }

    @classmethod
    def create(cls, **kwargs: Any) -> "ModelCallIntent":
        return cls.from_dict(build_model_call_intent(**kwargs))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelCallIntent":
        payload = validate_model_call_intent(value)
        return cls(
            call_id=payload["call_id"],
            attempt_id=payload["attempt_id"],
            request_digest=payload["request_digest"],
            provider=payload["provider"],
            model=payload["model"],
            stage=payload["stage"],
            budget_reservation=payload["budget_reservation"],
            created_at=payload["created_at"],
            intent_hash=payload["intent_hash"],
            schema_version=payload["schema_version"],
            canonical_json_algorithm=payload["canonical_json_algorithm"],
        )


@dataclass(frozen=True)
class ModelCallReceipt:
    call_id: str
    attempt_id: str
    intent_hash: str
    response_artifact_hash: str
    response_artifact_ref: str | None
    usage: Mapping[str, Any]
    finish_reason: str | None
    request_id: str | None
    actual_model: str | None
    endpoint_type: str
    status: str
    received_at: str
    receipt_hash: str = ""
    schema_version: str = MODEL_CALL_SCHEMA_VERSION
    canonical_json_algorithm: str = CANONICAL_JSON_ALGORITHM

    def __post_init__(self) -> None:
        usage = copy.deepcopy(dict(self.usage))
        object.__setattr__(self, "usage", MappingProxyType(usage))
        if not self.receipt_hash:
            object.__setattr__(
                self,
                "receipt_hash",
                canonical_json_hash(
                    self.to_dict(),
                    exclude_fields=("receipt_hash",),
                    exclude_environment_fields=False,
                ),
            )
        validate_model_call_receipt(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "canonical_json_algorithm": self.canonical_json_algorithm,
            "call_id": self.call_id,
            "attempt_id": self.attempt_id,
            "intent_hash": self.intent_hash,
            "response_artifact_hash": self.response_artifact_hash,
            "response_artifact_ref": self.response_artifact_ref,
            "usage": copy.deepcopy(dict(self.usage)),
            "finish_reason": self.finish_reason,
            "request_id": self.request_id,
            "actual_model": self.actual_model,
            "endpoint_type": self.endpoint_type,
            "status": self.status,
            "received_at": self.received_at,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def create(cls, **kwargs: Any) -> "ModelCallReceipt":
        return cls.from_dict(build_model_call_receipt(**kwargs))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelCallReceipt":
        payload = validate_model_call_receipt(value)
        return cls(
            call_id=payload["call_id"],
            attempt_id=payload["attempt_id"],
            intent_hash=payload["intent_hash"],
            response_artifact_hash=payload["response_artifact_hash"],
            response_artifact_ref=payload["response_artifact_ref"],
            usage=payload["usage"],
            finish_reason=payload["finish_reason"],
            request_id=payload["request_id"],
            actual_model=payload["actual_model"],
            endpoint_type=payload["endpoint_type"],
            status=payload["status"],
            received_at=payload["received_at"],
            receipt_hash=payload["receipt_hash"],
            schema_version=payload["schema_version"],
            canonical_json_algorithm=payload["canonical_json_algorithm"],
        )


def canonical_model_request_digest(request: Any) -> str:
    """Hash the complete logical request without returning or persisting its body."""

    return canonical_json_hash(request, exclude_environment_fields=False)


# Short alias used by budget/evidence callers that already operate in a model context.
model_request_digest = canonical_model_request_digest


def model_call_intent_hash(intent: ModelCallIntent | Mapping[str, Any]) -> str:
    payload = intent.to_dict() if isinstance(intent, ModelCallIntent) else _mapping_copy(intent, "ModelCallIntent")
    return canonical_json_hash(
        payload,
        exclude_fields=("intent_hash",),
        exclude_environment_fields=False,
    )


def model_call_receipt_hash(receipt: ModelCallReceipt | Mapping[str, Any]) -> str:
    payload = receipt.to_dict() if isinstance(receipt, ModelCallReceipt) else _mapping_copy(receipt, "ModelCallReceipt")
    return canonical_json_hash(
        payload,
        exclude_fields=("receipt_hash",),
        exclude_environment_fields=False,
    )


def model_response_artifact_hash(response: ModelResponse | str | bytes) -> str:
    if isinstance(response, ModelResponse):
        content = response.text.encode("utf-8")
    elif isinstance(response, str):
        content = response.encode("utf-8")
    elif isinstance(response, bytes):
        content = response
    else:
        raise TypeError("response artifact must be ModelResponse, str, or bytes")
    return hashlib.sha256(content).hexdigest()


def build_model_call_intent(
    *,
    call_id: str,
    attempt_id: str,
    provider: str,
    model: str,
    stage: str,
    budget_reservation: Mapping[str, Any] | int,
    request_digest: str | None = None,
    request: Any = _UNSET,
    created_at: datetime | str | None = None,
) -> dict[str, Any]:
    if request_digest is None:
        if request is _UNSET:
            raise ValueError("request_digest or request is required")
        request_digest = canonical_model_request_digest(request)
    elif request is not _UNSET:
        actual_digest = canonical_model_request_digest(request)
        if actual_digest != request_digest:
            raise ModelCallIntegrityError("request_digest does not match the canonical request")

    payload: dict[str, Any] = {
        "schema_version": MODEL_CALL_SCHEMA_VERSION,
        "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
        "call_id": _validate_id("call_id", call_id),
        "attempt_id": _validate_id("attempt_id", attempt_id),
        "request_digest": _require_sha256("request_digest", request_digest),
        "provider": _require_nonempty("provider", provider),
        "model": _require_nonempty("model", model),
        "stage": _require_nonempty("stage", stage),
        "budget_reservation": _normalize_budget_reservation(budget_reservation),
        "created_at": _iso_time(created_at),
    }
    _validate_safe_evidence(payload, kind="ModelCallIntent")
    payload["intent_hash"] = model_call_intent_hash(payload)
    return validate_model_call_intent(payload)


def build_model_call_receipt(
    intent: ModelCallIntent | Mapping[str, Any],
    *,
    response: ModelResponse | Mapping[str, Any] | None = None,
    response_artifact_hash: str | None = None,
    response_artifact_ref: str | None = None,
    usage: Mapping[str, Any] | None = None,
    finish_reason: str | None = None,
    request_id: str | None = None,
    actual_model: str | None = None,
    endpoint_type: str | None = None,
    status: str = "succeeded",
    received_at: datetime | str | None = None,
) -> dict[str, Any]:
    intent_payload = validate_model_call_intent(intent)
    model_response: ModelResponse | None = None
    if response is not None:
        model_response = response if isinstance(response, ModelResponse) else ModelResponse.from_dict(response)
        try:
            validate_schema(model_response.to_dict(), "model_response.schema.json")
        except SchemaValidationError as exc:
            raise ModelCallEvidenceError(str(exc)) from exc
        actual_hash = model_response_artifact_hash(model_response)
        if response_artifact_hash is not None and response_artifact_hash != actual_hash:
            raise ModelCallIntegrityError("response_artifact_hash does not match ModelResponse.text")
        response_artifact_hash = actual_hash
        if usage is None:
            usage = model_response.usage
        if finish_reason is None:
            finish_reason = model_response.finish_reason
        if request_id is None:
            request_id = model_response.request_id
        if actual_model is None:
            actual_model = model_response.actual_model
        if endpoint_type is None:
            endpoint_type = model_response.endpoint_type

    if response_artifact_hash is None:
        raise ValueError("response or response_artifact_hash is required")
    endpoint_type = endpoint_type or MODEL_ENDPOINT_UNKNOWN
    payload: dict[str, Any] = {
        "schema_version": MODEL_CALL_SCHEMA_VERSION,
        "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
        "call_id": intent_payload["call_id"],
        "attempt_id": intent_payload["attempt_id"],
        "intent_hash": intent_payload["intent_hash"],
        "response_artifact_hash": _require_sha256(
            "response_artifact_hash", response_artifact_hash
        ),
        "response_artifact_ref": _safe_relative_ref(response_artifact_ref),
        "usage": _normalize_usage(usage or {}),
        "finish_reason": _optional_string("finish_reason", finish_reason),
        "request_id": _optional_string("request_id", request_id),
        "actual_model": _optional_string("actual_model", actual_model),
        "endpoint_type": _require_nonempty("endpoint_type", endpoint_type),
        "status": _validate_status(status),
        "received_at": _iso_time(received_at),
    }
    _validate_safe_evidence(payload, kind="ModelCallReceipt")
    payload["receipt_hash"] = model_call_receipt_hash(payload)
    return validate_model_call_receipt(payload)


def validate_model_call_intent(
    intent: ModelCallIntent | Mapping[str, Any],
) -> dict[str, Any]:
    payload = intent.to_dict() if isinstance(intent, ModelCallIntent) else _mapping_copy(intent, "ModelCallIntent")
    try:
        validate_schema(payload, "model_call_intent.schema.json")
    except SchemaValidationError as exc:
        raise ModelCallEvidenceError(str(exc)) from exc
    _validate_id("call_id", payload["call_id"])
    _validate_id("attempt_id", payload["attempt_id"])
    _require_sha256("request_digest", payload["request_digest"])
    _require_sha256("intent_hash", payload["intent_hash"])
    for field in ("provider", "model", "stage"):
        _require_nonempty(field, payload[field])
    _normalize_budget_reservation(payload["budget_reservation"])
    _parse_time("created_at", payload["created_at"])
    _validate_safe_evidence(payload, kind="ModelCallIntent")
    expected = model_call_intent_hash(payload)
    if expected != payload["intent_hash"]:
        raise ModelCallIntegrityError("ModelCallIntent canonical hash mismatch")
    return payload


def validate_model_call_receipt(
    receipt: ModelCallReceipt | Mapping[str, Any],
) -> dict[str, Any]:
    payload = receipt.to_dict() if isinstance(receipt, ModelCallReceipt) else _mapping_copy(receipt, "ModelCallReceipt")
    try:
        validate_schema(payload, "model_call_receipt.schema.json")
    except SchemaValidationError as exc:
        raise ModelCallEvidenceError(str(exc)) from exc
    _validate_id("call_id", payload["call_id"])
    _validate_id("attempt_id", payload["attempt_id"])
    for field in ("intent_hash", "response_artifact_hash", "receipt_hash"):
        _require_sha256(field, payload[field])
    _safe_relative_ref(payload["response_artifact_ref"])
    _normalize_usage(payload["usage"])
    for field in ("finish_reason", "request_id", "actual_model"):
        _optional_string(field, payload[field])
    _require_nonempty("endpoint_type", payload["endpoint_type"])
    _validate_status(payload["status"])
    _parse_time("received_at", payload["received_at"])
    _validate_safe_evidence(payload, kind="ModelCallReceipt")
    expected = model_call_receipt_hash(payload)
    if expected != payload["receipt_hash"]:
        raise ModelCallIntegrityError("ModelCallReceipt canonical hash mismatch")
    return payload


def load_model_call_intent(path: str | Path) -> dict[str, Any]:
    return validate_model_call_intent(_load_json(Path(path), "ModelCallIntent"))


def load_model_call_receipt(path: str | Path) -> dict[str, Any]:
    return validate_model_call_receipt(_load_json(Path(path), "ModelCallReceipt"))


class ModelCallStore:
    """Append-only, fsync-backed evidence for physical provider attempts."""

    def __init__(self, root: str | Path, *, clock: _Clock | None = None) -> None:
        self.root = Path(root).resolve()
        self.intents_dir = self.root / "intents"
        self.receipts_dir = self.root / "receipts"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_intent(self, **kwargs: Any) -> dict[str, Any]:
        if "created_at" not in kwargs:
            attempt_id = kwargs.get("attempt_id")
            if isinstance(attempt_id, str) and self.intent_path(attempt_id).exists():
                kwargs["created_at"] = self.load_intent(attempt_id)["created_at"]
            else:
                kwargs["created_at"] = self.clock()
        return self.record_intent(build_model_call_intent(**kwargs))

    def record_intent(
        self, intent: ModelCallIntent | Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = validate_model_call_intent(intent)
        path = self.intent_path(payload["attempt_id"])
        return _create_idempotent(
            path,
            payload,
            loader=load_model_call_intent,
            contract_name="ModelCallIntent",
        )

    write_intent = record_intent

    def create_receipt(self, attempt_id: str, **kwargs: Any) -> dict[str, Any]:
        intent = self.load_intent(attempt_id)
        if "received_at" not in kwargs:
            if self.receipt_path(attempt_id).exists():
                kwargs["received_at"] = self.load_receipt(attempt_id)["received_at"]
            else:
                kwargs["received_at"] = self.clock()
        return self.record_receipt(build_model_call_receipt(intent, **kwargs))

    def record_receipt(
        self, receipt: ModelCallReceipt | Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = validate_model_call_receipt(receipt)
        intent = self.load_intent(payload["attempt_id"])
        _verify_receipt_link(intent, payload)
        path = self.receipt_path(payload["attempt_id"])
        persisted = _create_idempotent(
            path,
            payload,
            loader=load_model_call_receipt,
            contract_name="ModelCallReceipt",
        )
        _verify_receipt_link(intent, persisted)
        return persisted

    write_receipt = record_receipt

    def load_intent(self, attempt_id: str) -> dict[str, Any]:
        return load_model_call_intent(self.intent_path(attempt_id))

    def load_receipt(self, attempt_id: str) -> dict[str, Any]:
        receipt = load_model_call_receipt(self.receipt_path(attempt_id))
        _verify_receipt_link(self.load_intent(attempt_id), receipt)
        return receipt

    def has_receipt(self, attempt_id: str) -> bool:
        return self.receipt_path(attempt_id).is_file()

    def list_uncertain_calls(self) -> list[dict[str, Any]]:
        uncertain: list[dict[str, Any]] = []
        paths = sorted(self.intents_dir.glob("*.json")) if self.intents_dir.exists() else []
        for path in paths:
            intent = load_model_call_intent(path)
            receipt_path = self.receipt_path(intent["attempt_id"])
            if receipt_path.exists():
                receipt = load_model_call_receipt(receipt_path)
                _verify_receipt_link(intent, receipt)
                continue
            uncertain.append(
                {
                    "status": PROVIDER_CALL_UNCERTAIN,
                    "call_id": intent["call_id"],
                    "attempt_id": intent["attempt_id"],
                    "intent_hash": intent["intent_hash"],
                    "request_digest": intent["request_digest"],
                    "provider": intent["provider"],
                    "model": intent["model"],
                    "stage": intent["stage"],
                    "budget_reservation": copy.deepcopy(intent["budget_reservation"]),
                    "created_at": intent["created_at"],
                }
            )
        return sorted(
            uncertain,
            key=lambda item: (item["created_at"], item["call_id"], item["attempt_id"]),
        )

    enumerate_uncertain = list_uncertain_calls

    def intent_path(self, attempt_id: str) -> Path:
        return self.intents_dir / f"{_validate_id('attempt_id', attempt_id)}.json"

    def receipt_path(self, attempt_id: str) -> Path:
        return self.receipts_dir / f"{_validate_id('attempt_id', attempt_id)}.json"


# A descriptive alias for callers that prefer the evidence-log terminology.
ModelCallEvidenceStore = ModelCallStore


def persist_model_call_intent(
    root: str | Path, intent: ModelCallIntent | Mapping[str, Any]
) -> dict[str, Any]:
    return ModelCallStore(root).record_intent(intent)


def persist_model_call_receipt(
    root: str | Path, receipt: ModelCallReceipt | Mapping[str, Any]
) -> dict[str, Any]:
    return ModelCallStore(root).record_receipt(receipt)


def enumerate_provider_call_uncertain(root: str | Path) -> list[dict[str, Any]]:
    return ModelCallStore(root).list_uncertain_calls()


enumerate_uncertain_model_calls = enumerate_provider_call_uncertain


def _verify_receipt_link(
    intent: Mapping[str, Any], receipt: Mapping[str, Any]
) -> None:
    for field in ("call_id", "attempt_id"):
        if receipt[field] != intent[field]:
            raise ModelCallIntegrityError(f"ModelCallReceipt {field} does not match its intent")
    if receipt["intent_hash"] != intent["intent_hash"]:
        raise ModelCallIntegrityError("ModelCallReceipt intent_hash does not match its intent")


def _create_idempotent(
    path: Path,
    payload: Mapping[str, Any],
    *,
    loader: Callable[[str | Path], dict[str, Any]],
    contract_name: str,
) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(payload))
    try:
        atomic_create_json(path, candidate)
        return candidate
    except OSError as exc:
        if not path.exists():
            raise ModelCallEvidenceError(f"cannot persist {contract_name} at {path}: {exc}") from exc
    existing = loader(path)
    if existing == candidate:
        return existing
    raise ModelCallConflictError(
        f"{contract_name} already exists with different immutable content: {path.name}"
    )


def _mapping_copy(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelCallEvidenceError(f"{name} must be an object")
    return copy.deepcopy(dict(value))


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelCallEvidenceError(f"cannot read {name} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ModelCallEvidenceError(f"{name} JSON must contain an object: {path}")
    return value


def _normalize_budget_reservation(value: Mapping[str, Any] | int) -> dict[str, Any]:
    if isinstance(value, int) and not isinstance(value, bool):
        value = {"reserved_total_tokens": value}
    if not isinstance(value, Mapping) or not value:
        raise ModelCallEvidenceError("budget_reservation must be a non-empty object")
    result = copy.deepcopy(dict(value))

    def check(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            if not item:
                raise ModelCallEvidenceError(f"{path} must not be empty")
            for raw_key, child in item.items():
                if not isinstance(raw_key, str) or not raw_key.strip():
                    raise ModelCallEvidenceError(f"{path} keys must be non-empty strings")
                check(child, f"{path}.{raw_key}")
            return
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ModelCallEvidenceError(f"{path} must contain only numeric reservations")
        if not math.isfinite(item) or item < 0:
            raise ModelCallEvidenceError(f"{path} reservations must be finite and non-negative")

    check(result, "budget_reservation")
    _validate_safe_evidence(result, kind="budget_reservation")
    return result


def _normalize_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelCallEvidenceError("usage must be an object")
    result = copy.deepcopy(dict(value))
    try:
        canonical_json_hash(result, exclude_environment_fields=False)
    except (TypeError, ValueError) as exc:
        raise ModelCallEvidenceError(f"usage is not canonical JSON compatible: {exc}") from exc
    _validate_safe_evidence(result, kind="usage")
    return result


def _validate_safe_evidence(value: Any, *, kind: str) -> None:
    def visit(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if key in _FORBIDDEN_PERSISTED_FIELDS:
                    raise ModelCallSafetyError(f"{kind} must not persist payload field {path}.{raw_key}")
                if key in _CREDENTIAL_FIELDS:
                    raise ModelCallSafetyError(f"{kind} must not persist credential field {path}.{raw_key}")
                visit(child, f"{path}.{raw_key}")
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        if isinstance(item, str) and _looks_like_absolute_path(item):
            raise ModelCallSafetyError(f"{kind} must not persist an absolute path at {path}")

    visit(value, "$")


def _looks_like_absolute_path(value: str) -> bool:
    text = value.strip()
    return bool(
        text.startswith(("/", "\\\\"))
        or _WINDOWS_ABSOLUTE.match(text)
        or text.startswith("file://")
    )


def _safe_relative_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelCallSafetyError("response_artifact_ref must be a trimmed relative reference")
    if "\\" in value or ":" in value or "://" in value or _looks_like_absolute_path(value):
        raise ModelCallSafetyError("response_artifact_ref must be a safe POSIX relative reference")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts) or str(path) != value:
        raise ModelCallSafetyError("response_artifact_ref contains an unsafe path segment")
    return value


def _validate_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ModelCallSafetyError(f"{name} must be a safe identifier")
    return value


def _require_sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ModelCallIntegrityError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_nonempty(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ModelCallEvidenceError(f"{name} must be a trimmed non-empty string")
    return value


def _optional_string(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelCallEvidenceError(f"{name} must be a string or None")
    return value


def _validate_status(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_STATUS.fullmatch(value):
        raise ModelCallEvidenceError("status must be a lowercase identifier")
    return value


def _iso_time(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = _parse_time("timestamp", value)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ModelCallEvidenceError("timestamp must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelCallEvidenceError("timestamp must include a timezone")
    return parsed.isoformat()


def _parse_time(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ModelCallEvidenceError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelCallEvidenceError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelCallEvidenceError(f"{name} must include a timezone")
    return parsed


__all__ = [
    "MODEL_CALL_SCHEMA_VERSION",
    "PROVIDER_CALL_UNCERTAIN",
    "ModelCallConflictError",
    "ModelCallEvidenceError",
    "ModelCallEvidenceStore",
    "ModelCallIntegrityError",
    "ModelCallIntent",
    "ModelCallReceipt",
    "ModelCallSafetyError",
    "ModelCallStore",
    "build_model_call_intent",
    "build_model_call_receipt",
    "canonical_model_request_digest",
    "enumerate_provider_call_uncertain",
    "enumerate_uncertain_model_calls",
    "load_model_call_intent",
    "load_model_call_receipt",
    "model_call_intent_hash",
    "model_call_receipt_hash",
    "model_request_digest",
    "model_response_artifact_hash",
    "persist_model_call_intent",
    "persist_model_call_receipt",
    "validate_model_call_intent",
    "validate_model_call_receipt",
]
