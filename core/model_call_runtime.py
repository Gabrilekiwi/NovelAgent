from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from api.contracts import ModelResponse, coerce_model_response
from core.engine.persistence import atomic_create_json, atomic_create_text
from core.memory_v2.canonical import canonical_json_bytes
from core.model_calls import (
    ModelCallConflictError,
    ModelCallEvidenceError,
    ModelCallIntegrityError,
    ModelCallStore,
    build_model_call_intent,
    build_model_call_receipt,
    canonical_model_request_digest,
    model_response_artifact_hash,
)


_ACTIVE_MODEL_CALL_RUNTIME: ContextVar["ModelCallRuntimeContext | None"] = ContextVar(
    "novelagent_model_call_runtime",
    default=None,
)
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


class ProviderCallUncertainError(ModelCallEvidenceError):
    """A physical call crossed its durable Intent boundary without a Receipt."""

    failure_category = "provider_call_uncertain"
    retryable = False

    def __init__(
        self,
        *,
        call_id: str,
        attempt_id: str,
        cause: BaseException | None = None,
        partial_content_received: bool = False,
    ) -> None:
        super().__init__(
            f"provider_call_uncertain: intent {attempt_id} exists without a durable receipt"
        )
        self.call_id = call_id
        self.attempt_id = attempt_id
        self.cause = cause
        self.partial_content_received = bool(partial_content_received)


@dataclass
class ModelCallRuntimeContext:
    """Runtime-only bridge from providers to an append-only ModelCallStore.

    The context stores no prompt itself.  It hashes the complete logical
    request, writes one Intent per physical attempt, then publishes response
    text and its Receipt before allowing the result back to the caller.
    """

    store: ModelCallStore
    tracker: Any | None = None
    input_token_counter: Callable[[Any], int] | None = None
    id_factory: Callable[[], str] = field(default=lambda: uuid.uuid4().hex)
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc)
    )

    def new_call_id(self, *, provider: str, stage: str) -> str:
        prefix = _safe_label(f"{provider}-{stage}")[:80]
        suffix = _safe_label(str(self.id_factory()))[:64]
        if not prefix:
            prefix = "model-call"
        if not suffix:
            suffix = uuid.uuid4().hex
        return f"{prefix}-{suffix}"[:180].rstrip("._-")

    def estimate_input_tokens(self, request: Any) -> int:
        if self.input_token_counter is not None:
            value = self.input_token_counter(request)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelCallEvidenceError(
                    "model-call input_token_counter returned an invalid value"
                )
            return value
        # One token per canonical UTF-8 byte is deliberately conservative and
        # is explicitly a reservation, not a claim of provider-exact usage.
        return len(
            canonical_json_bytes(request, exclude_environment_fields=False)
        )

    def execute_attempt(
        self,
        *,
        call_id: str,
        attempt_number: int,
        provider: str,
        model: str,
        stage: str,
        endpoint_type: str,
        request: Any,
        max_output_tokens: int,
        operation: Callable[[], ModelResponse | str],
        input_tokens: int | None = None,
    ) -> ModelResponse:
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 0:
            raise ValueError("max_output_tokens must be a non-negative integer")
        attempt_id = f"{call_id}-a{attempt_number}"
        # Validate the derived path id before touching any network or disk.
        intent_path = self.store.intent_path(attempt_id)
        receipt_path = self.store.receipt_path(attempt_id)

        reserved_input = (
            self.estimate_input_tokens(request)
            if input_tokens is None
            else _non_negative_int("input_tokens", input_tokens)
        )
        reservation = {
            "reserved_input_tokens": reserved_input,
            "reserved_output_tokens": max_output_tokens,
            "reserved_total_tokens": reserved_input + max_output_tokens,
        }

        if receipt_path.exists():
            self._require_expected_intent(
                self.store.load_intent(attempt_id),
                provider=provider,
                model=model,
                stage=stage,
                request=request,
                reservation=reservation,
            )
            return self._response_from_receipt(attempt_id)
        if intent_path.exists():
            intent = self.store.load_intent(attempt_id)
            self._require_expected_intent(
                intent,
                provider=provider,
                model=model,
                stage=stage,
                request=request,
                reservation=reservation,
            )
            raise ProviderCallUncertainError(
                call_id=intent["call_id"],
                attempt_id=attempt_id,
            )

        intent = build_model_call_intent(
            call_id=call_id,
            attempt_id=attempt_id,
            provider=provider,
            model=model,
            stage=stage,
            budget_reservation=reservation,
            request=request,
            created_at=self.clock(),
        )
        try:
            atomic_create_json(intent_path, intent)
        except OSError as exc:
            if not intent_path.is_file():
                raise ModelCallEvidenceError(
                    f"cannot claim model-call intent {attempt_id}: {exc}"
                ) from exc
            existing = self.store.load_intent(attempt_id)
            self._require_expected_intent(
                existing,
                provider=provider,
                model=model,
                stage=stage,
                request=request,
                reservation=reservation,
            )
            if receipt_path.exists():
                return self._response_from_receipt(attempt_id)
            raise ProviderCallUncertainError(
                call_id=call_id,
                attempt_id=attempt_id,
            ) from exc
        try:
            self._reserve_tracker(
                input_tokens=reserved_input,
                max_output_tokens=max_output_tokens,
                call_id=call_id,
                attempt_id=attempt_id,
            )
        except Exception:
            # The durable Intent exists, but the provider was provably never
            # invoked.  Close the attempt with a terminal Receipt so recovery
            # does not misclassify a local budget rejection as an uncertain
            # provider side effect.
            rejected = ModelResponse(
                "",
                usage={},
                finish_reason="budget_rejected",
                actual_model=model,
                endpoint_type=endpoint_type,
            )
            relative_ref = f"responses/{attempt_id}.txt"
            self._persist_response_artifact(relative_ref, rejected.text)
            self.store.record_receipt(
                build_model_call_receipt(
                    intent,
                    response=rejected,
                    response_artifact_ref=relative_ref,
                    status="budget_rejected",
                    received_at=self.clock(),
                )
            )
            raise

        try:
            raw_response = operation()
            response = coerce_model_response(
                raw_response,
                actual_model=model,
                endpoint_type=endpoint_type,
            )
            relative_ref = f"responses/{attempt_id}.txt"
            self._persist_response_artifact(relative_ref, response.text)
            receipt = build_model_call_receipt(
                intent,
                response=response,
                response_artifact_ref=relative_ref,
                status="succeeded",
                received_at=self.clock(),
            )
            self.store.record_receipt(receipt)
        except ProviderCallUncertainError:
            raise
        except Exception as exc:
            raise ProviderCallUncertainError(
                call_id=call_id,
                attempt_id=attempt_id,
                cause=exc,
                partial_content_received=bool(
                    getattr(exc, "partial_content_received", False)
                ),
            ) from exc

        self._record_tracker_response(response, call_id=call_id, attempt_id=attempt_id)
        return response

    def _require_expected_intent(
        self,
        intent: dict[str, Any],
        *,
        provider: str,
        model: str,
        stage: str,
        request: Any,
        reservation: dict[str, int],
    ) -> None:
        expected = {
            "provider": provider,
            "model": model,
            "stage": stage,
            "request_digest": canonical_model_request_digest(request),
            "budget_reservation": reservation,
        }
        for field, value in expected.items():
            if intent[field] != value:
                raise ModelCallConflictError(
                    f"model-call {field} conflicts with immutable intent {intent['attempt_id']}"
                )

    def _persist_response_artifact(self, relative_ref: str, text: str) -> Path:
        path = _resolve_artifact(self.store.root, relative_ref)
        try:
            atomic_create_text(path, text)
        except (OSError, UnicodeError) as exc:
            if not path.is_file():
                raise ModelCallEvidenceError(
                    f"cannot persist model response artifact {relative_ref}: {exc}"
                ) from exc
            existing = path.read_bytes()
            if model_response_artifact_hash(existing) != model_response_artifact_hash(text):
                raise ModelCallConflictError(
                    f"model response artifact already exists with different content: {relative_ref}"
                ) from exc
        return path

    def _response_from_receipt(self, attempt_id: str) -> ModelResponse:
        receipt = self.store.load_receipt(attempt_id)
        relative_ref = receipt.get("response_artifact_ref")
        if not isinstance(relative_ref, str) or not relative_ref:
            raise ModelCallIntegrityError(
                "durable model-call receipt has no response artifact reference"
            )
        path = _resolve_artifact(self.store.root, relative_ref)
        try:
            content = path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ModelCallIntegrityError(
                f"cannot read durable model response artifact {relative_ref}: {exc}"
            ) from exc
        if model_response_artifact_hash(content) != receipt["response_artifact_hash"]:
            raise ModelCallIntegrityError(
                "durable model response artifact hash mismatch"
            )
        if receipt["status"] != "succeeded":
            raise ModelCallIntegrityError(
                f"cannot replay model receipt with status {receipt['status']}"
            )
        return ModelResponse(
            text,
            usage=receipt["usage"],
            finish_reason=receipt["finish_reason"],
            request_id=receipt["request_id"],
            actual_model=receipt["actual_model"],
            endpoint_type=receipt["endpoint_type"],
        )

    def _reserve_tracker(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        call_id: str,
        attempt_id: str,
    ) -> None:
        tracker = self.tracker
        if tracker is None:
            return
        reserve_attempt = getattr(tracker, "reserve_model_call", None)
        if callable(reserve_attempt):
            reserve_attempt(
                input_tokens=input_tokens,
                max_output_tokens=max_output_tokens,
                call_id=call_id,
                attempt_id=attempt_id,
            )
            return

        reserve_call = getattr(tracker, "reserve_call", None)
        reserve_output = getattr(tracker, "record_output", None)
        if not callable(reserve_call) or not callable(reserve_output):
            raise ModelCallEvidenceError(
                "model-call tracker must support reserve_model_call or reserve_call+record_output"
            )
        limits = getattr(tracker, "limits", None)
        maximum = getattr(limits, "max_total_output_tokens", None)
        current = getattr(tracker, "total_output_tokens", None)
        if isinstance(maximum, int) and isinstance(current, int):
            if current + max_output_tokens > maximum:
                raise ModelCallEvidenceError(
                    "run_output_token_budget_exceeded: reserved output exceeds remaining budget"
                )
        reserve_call(input_tokens)
        # Existing RunBudgetTracker has no separate reservation ledger.  Charge
        # the maximum before the network attempt so timeouts remain accounted.
        reserve_output(max_output_tokens)

    def _record_tracker_response(
        self,
        response: ModelResponse,
        *,
        call_id: str,
        attempt_id: str,
    ) -> None:
        recorder = getattr(self.tracker, "record_model_response", None)
        if callable(recorder):
            recorder(response=response, call_id=call_id, attempt_id=attempt_id)


def current_model_call_runtime() -> ModelCallRuntimeContext | None:
    return _ACTIVE_MODEL_CALL_RUNTIME.get()


def resolve_model_call_runtime(
    explicit: ModelCallRuntimeContext | None = None,
) -> ModelCallRuntimeContext | None:
    return explicit if explicit is not None else current_model_call_runtime()


def set_model_call_runtime(
    context: ModelCallRuntimeContext | None,
) -> Token[ModelCallRuntimeContext | None]:
    return _ACTIVE_MODEL_CALL_RUNTIME.set(context)


def reset_model_call_runtime(token: Token[ModelCallRuntimeContext | None]) -> None:
    _ACTIVE_MODEL_CALL_RUNTIME.reset(token)


@contextmanager
def use_model_call_runtime(
    context: ModelCallRuntimeContext,
) -> Iterator[ModelCallRuntimeContext]:
    token = set_model_call_runtime(context)
    try:
        yield context
    finally:
        reset_model_call_runtime(token)


def _resolve_artifact(root: Path, relative_ref: str) -> Path:
    pure = PurePosixPath(relative_ref)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or str(pure) != relative_ref
    ):
        raise ModelCallIntegrityError("unsafe model response artifact reference")
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*pure.parts)).resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ModelCallIntegrityError(
            "model response artifact reference escapes its store"
        ) from exc
    return candidate


def _safe_label(value: str) -> str:
    return _SAFE_LABEL.sub("-", str(value)).strip("._-")


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "ModelCallRuntimeContext",
    "ProviderCallUncertainError",
    "current_model_call_runtime",
    "reset_model_call_runtime",
    "resolve_model_call_runtime",
    "set_model_call_runtime",
    "use_model_call_runtime",
]
