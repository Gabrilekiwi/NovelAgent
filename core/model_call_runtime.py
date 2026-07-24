from __future__ import annotations

import hashlib
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from api.contracts import ModelResponse, coerce_model_response
from core.context_budget import (
    conservative_calibrated_token_estimate,
    model_token_counter,
)
from core.engine.persistence import atomic_create_json, atomic_create_text
from core.memory_v2.canonical import canonical_json_bytes
from core.model_calls import (
    PROVIDER_PARTIAL_RESPONSE_FAILED,
    PROVIDER_REQUEST_NOT_SENT,
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
_FaultInjector = Callable[[str, str, Path | None], None]


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


class ProviderPartialResponseError(ModelCallEvidenceError):
    """A streamed call stopped after a partial response was durably recorded."""

    failure_category = "provider_partial_response"
    retryable = False
    partial_content_received = True

    def __init__(
        self,
        *,
        call_id: str,
        attempt_id: str,
        receipt_hash: str,
        response_artifact_ref: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            "provider_partial_response: streamed attempt "
            f"{attempt_id} stopped after partial content; durable failure receipt exists"
        )
        self.call_id = call_id
        self.attempt_id = attempt_id
        self.receipt_hash = receipt_hash
        self.response_artifact_ref = response_artifact_ref
        self.cause = cause


class ProviderRequestNotSentError(ModelCallEvidenceError):
    """A connection failed before an HTTP request could be dispatched."""

    failure_category = "connection"
    retryable = True
    partial_content_received = False

    def __init__(
        self,
        *,
        call_id: str,
        attempt_id: str,
        receipt_hash: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            "provider_request_not_sent: connection setup failed before dispatch "
            f"for attempt {attempt_id}; durable terminal receipt exists"
        )
        self.call_id = call_id
        self.attempt_id = attempt_id
        self.receipt_hash = receipt_hash
        self.cause = cause


class ProviderRejectedError(ModelCallEvidenceError):
    """The provider definitively rejected the request without a model result."""

    failure_category = "configuration"
    retryable = False
    partial_content_received = False

    def __init__(
        self,
        *,
        call_id: str,
        attempt_id: str,
        receipt_hash: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            "provider_rejected: provider definitively rejected attempt "
            f"{attempt_id}; durable terminal receipt exists"
        )
        self.call_id = call_id
        self.attempt_id = attempt_id
        self.receipt_hash = receipt_hash
        self.cause = cause


_CHARGED_TERMINAL_FAILURE_STATUSES = frozenset(
    {"provider_rejected", PROVIDER_PARTIAL_RESPONSE_FAILED}
)
_UNCHARGED_TERMINAL_FAILURE_STATUSES = frozenset(
    {"budget_rejected", PROVIDER_REQUEST_NOT_SENT}
)


@dataclass
class ModelCallOperationScope:
    """Stable logical-call namespace for one autonomy stage operation."""

    operation_key: str
    ordinal: int = 0
    receipt_hashes: list[str] = field(default_factory=list)


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
    fault_injector: _FaultInjector | None = None
    _operation_scope: ModelCallOperationScope | None = field(
        default=None, init=False, repr=False
    )

    @contextmanager
    def bind_operation(
        self, operation_key: str
    ) -> Iterator[ModelCallOperationScope]:
        """Bind logical call IDs to a durable stage operation key.

        A restarted stage begins its ordinal at one again, producing the same
        call IDs and therefore replaying already-published response receipts.
        """

        key = str(operation_key)
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ModelCallEvidenceError(
                "model-call operation_key must be a lowercase SHA-256 digest"
            )
        if self._operation_scope is not None:
            raise ModelCallEvidenceError("nested model-call operation scopes are forbidden")
        scope = ModelCallOperationScope(operation_key=key)
        self._operation_scope = scope
        try:
            yield scope
        finally:
            self._operation_scope = None

    def new_call_id(self, *, provider: str, stage: str) -> str:
        prefix = _safe_label(f"{provider}-{stage}")[:80]
        if not prefix:
            prefix = "model-call"
        scope = self._operation_scope
        if scope is not None:
            scope.ordinal += 1
            suffix = hashlib.sha256(
                (
                    f"{scope.operation_key}:{scope.ordinal}:"
                    f"{provider}:{stage}"
                ).encode("utf-8")
            ).hexdigest()[:40]
            return f"{prefix}-{scope.ordinal:03d}-{suffix}"[:180].rstrip("._-")
        suffix = _safe_label(str(self.id_factory()))[:64]
        if not suffix:
            suffix = uuid.uuid4().hex
        return f"{prefix}-{suffix}"[:180].rstrip("._-")

    def estimate_input_tokens(
        self,
        request: Any,
        *,
        provider: str | None = None,
        model: str | None = None,
        endpoint_type: str | None = None,
    ) -> int:
        if self.input_token_counter is not None:
            value = self.input_token_counter(request)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ModelCallEvidenceError(
                    "model-call input_token_counter returned an invalid value"
                )
            return value
        canonical_request = canonical_json_bytes(
            request, exclude_environment_fields=False
        ).decode("utf-8")
        if provider and model and endpoint_type:
            counter = model_token_counter(
                provider=provider,
                model=model,
                endpoint_type=endpoint_type,
            )
            if counter is not None:
                return counter.count(canonical_request)
        # This remains a pre-provider reservation, never a claim of exact
        # provider usage. Unknown tokenizers use the calibrated fallback.
        return conservative_calibrated_token_estimate(canonical_request)

    def hydrate_tracker_from_store(self) -> list[str]:
        """Rebuild charged budget usage from immutable attempt evidence.

        A terminal local budget rejection never crossed the provider boundary
        and is therefore not charged. Every succeeded receipt is reserved and
        settled exactly once in the fresh in-memory tracker. Terminal provider
        failures keep their full reservation charged. An Intent without a
        Receipt crossed the durable provider boundary, so its full output
        reservation also remains charged while the attempt stays uncertain.
        """

        hydrated: list[str] = []
        receipts: dict[str, dict[str, Any]] = {}
        if self.store.receipts_dir.is_dir():
            for path in sorted(self.store.receipts_dir.glob("*.json")):
                receipt = self.store.load_receipt(path.stem)
                receipts[receipt["attempt_id"]] = receipt
        if not self.store.intents_dir.is_dir():
            return hydrated

        for path in sorted(self.store.intents_dir.glob("*.json")):
            intent = self.store.load_intent(path.stem)
            receipt = receipts.get(intent["attempt_id"])
            if (
                receipt is not None
                and receipt["status"] in _UNCHARGED_TERMINAL_FAILURE_STATUSES
            ):
                continue
            if (
                receipt is not None
                and receipt["status"] != "succeeded"
                and receipt["status"] not in _CHARGED_TERMINAL_FAILURE_STATUSES
            ):
                raise ModelCallIntegrityError(
                    f"unsupported terminal model receipt status: {receipt['status']}"
                )
            self._restore_tracker_reservation(intent)
            if receipt is not None and receipt["status"] == "succeeded":
                response = self._response_from_receipt(receipt["attempt_id"])
                self._record_tracker_response(
                    response,
                    call_id=receipt["call_id"],
                    attempt_id=receipt["attempt_id"],
                )
                hydrated.append(receipt["receipt_hash"])
        return hydrated

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

        if receipt_path.exists():
            intent = self.store.load_intent(attempt_id)
            self._require_expected_intent(
                intent,
                call_id=call_id,
                attempt_id=attempt_id,
                provider=provider,
                model=model,
                stage=stage,
                request=request,
                max_output_tokens=max_output_tokens,
                explicit_input_tokens=input_tokens,
            )
            return self._replay_receipt(intent)
        if intent_path.exists():
            intent = self.store.load_intent(attempt_id)
            self._require_expected_intent(
                intent,
                call_id=call_id,
                attempt_id=attempt_id,
                provider=provider,
                model=model,
                stage=stage,
                request=request,
                max_output_tokens=max_output_tokens,
                explicit_input_tokens=input_tokens,
            )
            self._restore_tracker_reservation(intent)
            raise ProviderCallUncertainError(
                call_id=intent["call_id"],
                attempt_id=attempt_id,
            )

        reserved_input = (
            self.estimate_input_tokens(
                request,
                provider=provider,
                model=model,
                endpoint_type=endpoint_type,
            )
            if input_tokens is None
            else _non_negative_int("input_tokens", input_tokens)
        )
        reservation = {
            "reserved_input_tokens": reserved_input,
            "reserved_output_tokens": max_output_tokens,
            "reserved_total_tokens": reserved_input + max_output_tokens,
        }

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
                call_id=call_id,
                attempt_id=attempt_id,
                provider=provider,
                model=model,
                stage=stage,
                request=request,
                max_output_tokens=max_output_tokens,
                explicit_input_tokens=input_tokens,
            )
            if receipt_path.exists():
                return self._replay_receipt(existing)
            self._restore_tracker_reservation(existing)
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
                stage=stage,
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
            artifact_path = _resolve_artifact(self.store.root, relative_ref)
            self._inject_fault(
                "after_provider_response_before_artifact",
                attempt_id,
                artifact_path,
            )
            self._persist_response_artifact(relative_ref, response.text)
            self._inject_fault(
                "after_response_artifact_before_receipt",
                attempt_id,
                artifact_path,
            )
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
            partial_response = getattr(exc, "partial_response", None)
            if isinstance(partial_response, ModelResponse) and partial_response.text:
                relative_ref = f"responses/{attempt_id}.txt"
                artifact_path = _resolve_artifact(self.store.root, relative_ref)
                try:
                    self._inject_fault(
                        "after_provider_response_before_artifact",
                        attempt_id,
                        artifact_path,
                    )
                    self._persist_response_artifact(relative_ref, partial_response.text)
                    self._inject_fault(
                        "after_response_artifact_before_receipt",
                        attempt_id,
                        artifact_path,
                    )
                    failed_receipt = build_model_call_receipt(
                        intent,
                        response=partial_response,
                        response_artifact_ref=relative_ref,
                        status=PROVIDER_PARTIAL_RESPONSE_FAILED,
                        received_at=self.clock(),
                    )
                    self.store.record_receipt(failed_receipt)
                except Exception as evidence_error:
                    raise ProviderCallUncertainError(
                        call_id=call_id,
                        attempt_id=attempt_id,
                        cause=evidence_error,
                        partial_content_received=True,
                    ) from evidence_error
                raise ProviderPartialResponseError(
                    call_id=call_id,
                    attempt_id=attempt_id,
                    receipt_hash=failed_receipt["receipt_hash"],
                    response_artifact_ref=relative_ref,
                    cause=exc,
                ) from exc
            if _is_request_not_sent_connection_failure(exc):
                failed_response = ModelResponse(
                    "",
                    usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    finish_reason=PROVIDER_REQUEST_NOT_SENT,
                    actual_model=model,
                    endpoint_type=endpoint_type,
                )
                relative_ref = f"responses/{attempt_id}.txt"
                artifact_path = _resolve_artifact(self.store.root, relative_ref)
                try:
                    self._inject_fault(
                        "after_provider_response_before_artifact",
                        attempt_id,
                        artifact_path,
                    )
                    self._persist_response_artifact(relative_ref, failed_response.text)
                    self._inject_fault(
                        "after_response_artifact_before_receipt",
                        attempt_id,
                        artifact_path,
                    )
                    failed_receipt = build_model_call_receipt(
                        intent,
                        response=failed_response,
                        response_artifact_ref=relative_ref,
                        status=PROVIDER_REQUEST_NOT_SENT,
                        received_at=self.clock(),
                    )
                    self.store.record_receipt(failed_receipt)
                except Exception as evidence_error:
                    raise ProviderCallUncertainError(
                        call_id=call_id,
                        attempt_id=attempt_id,
                        cause=evidence_error,
                    ) from evidence_error
                self._record_tracker_response(
                    failed_response,
                    call_id=call_id,
                    attempt_id=attempt_id,
                )
                raise ProviderRequestNotSentError(
                    call_id=call_id,
                    attempt_id=attempt_id,
                    receipt_hash=failed_receipt["receipt_hash"],
                    cause=exc,
                ) from exc
            if _is_definitive_provider_rejection(exc):
                failed_response = ModelResponse(
                    "",
                    usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    finish_reason="provider_rejected",
                    actual_model=model,
                    endpoint_type=endpoint_type,
                )
                relative_ref = f"responses/{attempt_id}.txt"
                artifact_path = _resolve_artifact(self.store.root, relative_ref)
                try:
                    self._inject_fault(
                        "after_provider_response_before_artifact",
                        attempt_id,
                        artifact_path,
                    )
                    self._persist_response_artifact(relative_ref, failed_response.text)
                    self._inject_fault(
                        "after_response_artifact_before_receipt",
                        attempt_id,
                        artifact_path,
                    )
                    failed_receipt = build_model_call_receipt(
                        intent,
                        response=failed_response,
                        response_artifact_ref=relative_ref,
                        status="provider_rejected",
                        received_at=self.clock(),
                    )
                    self.store.record_receipt(failed_receipt)
                except Exception as evidence_error:
                    raise ProviderCallUncertainError(
                        call_id=call_id,
                        attempt_id=attempt_id,
                        cause=evidence_error,
                    ) from evidence_error
                raise ProviderRejectedError(
                    call_id=call_id,
                    attempt_id=attempt_id,
                    receipt_hash=failed_receipt["receipt_hash"],
                    cause=exc,
                ) from exc
            raise ProviderCallUncertainError(
                call_id=call_id,
                attempt_id=attempt_id,
                cause=exc,
                partial_content_received=bool(
                    getattr(exc, "partial_content_received", False)
                ),
            ) from exc

        self._record_tracker_response(response, call_id=call_id, attempt_id=attempt_id)
        self._record_operation_receipt(receipt["receipt_hash"])
        return response

    def _inject_fault(
        self,
        event: str,
        attempt_id: str,
        path: Path | None,
    ) -> None:
        if self.fault_injector is not None:
            self.fault_injector(event, attempt_id, path)

    def _require_expected_intent(
        self,
        intent: dict[str, Any],
        *,
        call_id: str,
        attempt_id: str,
        provider: str,
        model: str,
        stage: str,
        request: Any,
        max_output_tokens: int,
        explicit_input_tokens: int | None,
    ) -> dict[str, int]:
        expected = {
            "call_id": call_id,
            "attempt_id": attempt_id,
            "provider": provider,
            "model": model,
            "stage": stage,
            "request_digest": canonical_model_request_digest(request),
        }
        for field, value in expected.items():
            if intent[field] != value:
                raise ModelCallConflictError(
                    f"model-call {field} conflicts with immutable intent {intent['attempt_id']}"
                )
        reservation = self._stored_reservation(intent)
        if reservation["reserved_output_tokens"] != max_output_tokens:
            raise ModelCallConflictError(
                "model-call reserved_output_tokens conflicts with immutable intent "
                f"{intent['attempt_id']}"
            )
        if explicit_input_tokens is not None:
            explicit = _non_negative_int("input_tokens", explicit_input_tokens)
            if reservation["reserved_input_tokens"] != explicit:
                raise ModelCallConflictError(
                    "model-call reserved_input_tokens conflicts with immutable intent "
                    f"{intent['attempt_id']}"
                )
        return reservation

    @staticmethod
    def _stored_reservation(intent: dict[str, Any]) -> dict[str, int]:
        raw = intent.get("budget_reservation")
        if not isinstance(raw, dict):
            raise ModelCallIntegrityError(
                "durable model-call intent has no budget reservation object"
            )
        try:
            reservation = {
                "reserved_input_tokens": _non_negative_int(
                    "reserved_input_tokens", raw.get("reserved_input_tokens")
                ),
                "reserved_output_tokens": _non_negative_int(
                    "reserved_output_tokens", raw.get("reserved_output_tokens")
                ),
                "reserved_total_tokens": _non_negative_int(
                    "reserved_total_tokens", raw.get("reserved_total_tokens")
                ),
            }
        except ValueError as exc:
            raise ModelCallIntegrityError(
                "durable model-call intent has an invalid budget reservation"
            ) from exc
        if reservation["reserved_total_tokens"] != (
            reservation["reserved_input_tokens"]
            + reservation["reserved_output_tokens"]
        ):
            raise ModelCallIntegrityError(
                "durable model-call intent has an inconsistent total reservation"
            )
        return reservation

    def _restore_tracker_reservation(self, intent: dict[str, Any]) -> None:
        reservation = self._stored_reservation(intent)
        self._reserve_tracker(
            input_tokens=reservation["reserved_input_tokens"],
            max_output_tokens=reservation["reserved_output_tokens"],
            call_id=intent["call_id"],
            attempt_id=intent["attempt_id"],
            stage=str(intent.get("stage") or ""),
            restore=True,
        )

    def _replay_receipt(self, intent: dict[str, Any]) -> ModelResponse:
        receipt = self.store.load_receipt(intent["attempt_id"])
        # Local budget rejection is terminal but never crossed the provider
        # boundary and must remain uncharged.
        if receipt["status"] == PROVIDER_PARTIAL_RESPONSE_FAILED:
            self._restore_tracker_reservation(intent)
            self._load_response_from_receipt(receipt)
            raise ProviderPartialResponseError(
                call_id=intent["call_id"],
                attempt_id=intent["attempt_id"],
                receipt_hash=receipt["receipt_hash"],
                response_artifact_ref=receipt["response_artifact_ref"],
            )
        if receipt["status"] == PROVIDER_REQUEST_NOT_SENT:
            self._load_response_from_receipt(receipt)
            raise ProviderRequestNotSentError(
                call_id=intent["call_id"],
                attempt_id=intent["attempt_id"],
                receipt_hash=receipt["receipt_hash"],
            )
        if receipt["status"] == "provider_rejected":
            self._load_response_from_receipt(receipt)
            raise ProviderRejectedError(
                call_id=intent["call_id"],
                attempt_id=intent["attempt_id"],
                receipt_hash=receipt["receipt_hash"],
            )
        if receipt["status"] != "succeeded":
            return self._response_from_receipt(intent["attempt_id"])
        self._restore_tracker_reservation(intent)
        response = self._response_from_receipt(intent["attempt_id"])
        self._record_tracker_response(
            response,
            call_id=intent["call_id"],
            attempt_id=intent["attempt_id"],
        )
        return response

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
        response = self._load_response_from_receipt(receipt)
        if receipt["status"] != "succeeded":
            raise ModelCallIntegrityError(
                f"cannot replay model receipt with status {receipt['status']}"
            )
        self._record_operation_receipt(receipt["receipt_hash"])
        return response

    def _load_response_from_receipt(self, receipt: dict[str, Any]) -> ModelResponse:
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
        response = ModelResponse(
            text,
            usage=receipt["usage"],
            finish_reason=receipt["finish_reason"],
            request_id=receipt["request_id"],
            actual_model=receipt["actual_model"],
            endpoint_type=receipt["endpoint_type"],
        )
        return response

    def _record_operation_receipt(self, receipt_hash: str) -> None:
        scope = self._operation_scope
        if scope is None:
            return
        digest = str(receipt_hash)
        if digest not in scope.receipt_hashes:
            scope.receipt_hashes.append(digest)

    def _reserve_tracker(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        call_id: str,
        attempt_id: str,
        stage: str | None = None,
        restore: bool = False,
    ) -> None:
        tracker = self.tracker
        if tracker is None:
            return
        if restore:
            restore_attempt = getattr(tracker, "restore_model_call", None)
            if callable(restore_attempt):
                restore_attempt(
                    input_tokens=input_tokens,
                    max_output_tokens=max_output_tokens,
                    call_id=call_id,
                    attempt_id=attempt_id,
                    stage=stage,
                )
                return
        ensure_attempt = getattr(tracker, "ensure_model_call", None)
        if callable(ensure_attempt):
            ensure_attempt(
                input_tokens=input_tokens,
                max_output_tokens=max_output_tokens,
                call_id=call_id,
                attempt_id=attempt_id,
                stage=stage,
            )
            return
        reserve_attempt = getattr(tracker, "reserve_model_call", None)
        if callable(reserve_attempt):
            reserve_attempt(
                input_tokens=input_tokens,
                max_output_tokens=max_output_tokens,
                call_id=call_id,
                attempt_id=attempt_id,
                stage=stage,
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


def _is_request_not_sent_connection_failure(error: BaseException) -> bool:
    """Recognize transport failures that occur before any request body is sent.

    Read/write/protocol errors deliberately remain uncertain because the
    provider may already have accepted the request.  HTTPX/HTTPCore use the
    ConnectError and ConnectTimeout types only while establishing a connection.
    """

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if bool(getattr(current, "partial_content_received", False)):
            return False
        module = type(current).__module__.split(".", 1)[0]
        if module in {"httpx", "httpcore"} and type(current).__name__ in {
            "ConnectError",
            "ConnectTimeout",
        }:
            return True
        nested = getattr(current, "cause", None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__ or current.__context__
        current = nested if isinstance(nested, BaseException) else None
    return False


def _is_definitive_provider_rejection(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if status in {400, 401, 403, 404, 405, 410, 422}:
            return True
        if type(current).__name__ == "BadRequestError":
            return True
        nested = getattr(current, "cause", None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__ or current.__context__
        current = nested if isinstance(nested, BaseException) else None
    return False


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
    "ModelCallOperationScope",
    "ModelCallRuntimeContext",
    "ProviderCallUncertainError",
    "ProviderPartialResponseError",
    "ProviderRequestNotSentError",
    "ProviderRejectedError",
    "current_model_call_runtime",
    "reset_model_call_runtime",
    "resolve_model_call_runtime",
    "set_model_call_runtime",
    "use_model_call_runtime",
]
