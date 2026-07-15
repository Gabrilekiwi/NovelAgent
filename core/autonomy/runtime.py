from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    canonical_hash,
    load_json_object,
    positive_int,
    safe_id,
    sha256_digest,
    validate_mapping,
)
from core.autonomy.outline import validate_outline_checkpoint
from core.autonomy.plans import validate_instruction_plan, validate_source_snapshot
from core.autonomy.receipts import StageReceiptStore
from core.autonomy.session import AutonomySessionStore
from core.context_budget import ContextBudgetError, RunBudgetLimits, RunBudgetTracker
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from core.stage_control import (
    assert_stage_authorized,
    build_stage_authorization,
    build_stage_receipt,
    derive_draft_readiness,
    validate_draft_readiness,
    validate_outline_readiness,
)


class AutonomyRuntimeError(AutonomyContractError):
    pass


@dataclass
class _StageToken:
    authorization: dict[str, Any]
    replay_receipt: dict[str, Any] | None
    cursor: int
    operation_key: str
    cached_output: Any | None = None
    cached_model_call_receipt_hashes: tuple[str, ...] = ()


def record_outline_stage(
    *,
    sessions: AutonomySessionStore,
    session_id: str,
    plan: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    outline_readiness: Mapping[str, Any],
    lease_hash: str,
    lease_ttl_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Record or replay the provider-gated outline stage before draft work."""

    validated_plan = validate_instruction_plan(plan)
    validated_checkpoint = validate_outline_checkpoint(checkpoint)
    readiness = validate_outline_readiness(dict(outline_readiness))
    if not readiness["ok"]:
        raise AutonomyRuntimeError(
            "outline_readiness_rejected", ", ".join(readiness["reasons"])
        )
    status = sessions.status(session_id)
    renewed = sessions.leases.renew(
        book_id=status["book_id"],
        session_id=session_id,
        plan_id=status["plan_id"],
        expected_lease_hash=lease_hash,
        ttl_seconds=lease_ttl_seconds,
    )
    sessions.assert_outline_provider_allowed(session_id)
    chain = sessions.stage_receipts.load_chain(
        session_id, int(validated_checkpoint["chapter_index"])
    )
    if chain:
        if chain[0]["stage"] != "outline":
            raise AutonomyRuntimeError(
                "outline_stage_chain_invalid",
                "outline replay chain does not begin with the outline stage",
            )
        receipt = chain[0]
        authorization = sessions.stage_receipts.authorization_for(receipt)
        _assert_outline_replay(
            authorization,
            receipt,
            plan=validated_plan,
            checkpoint=validated_checkpoint,
        )
        return authorization, receipt, renewed["lease_hash"]
    authorization = build_stage_authorization(
        stage="outline",
        book_id=validated_checkpoint["book_id"],
        session_id=session_id,
        plan_id=validated_plan["plan_id"],
        chapter_index=int(validated_checkpoint["chapter_index"]),
        authority_epoch=int(validated_checkpoint["authority"]["epoch"]),
        authority_head_event_hash=validated_checkpoint["authority"]["head_event_hash"],
        input_digest=validated_checkpoint["outline_input_digest"],
        previous_stage_receipt_hash=None,
        provider_profile=validated_checkpoint["provider_profile"],
        max_output_tokens=int(
            validated_plan["selections"]["provider_model"]["max_output_tokens"]
        ),
        execution_kind=validated_checkpoint["execution_kind"],
    )
    assert_stage_authorized(
        authorization,
        stage="outline",
        book_id=validated_checkpoint["book_id"],
        session_id=session_id,
        plan_id=validated_plan["plan_id"],
        chapter_index=int(validated_checkpoint["chapter_index"]),
        authority_epoch=int(validated_checkpoint["authority"]["epoch"]),
        authority_head_event_hash=validated_checkpoint["authority"]["head_event_hash"],
        input_digest=validated_checkpoint["outline_input_digest"],
        previous_stage_receipt_hash=None,
        provider_profile=validated_checkpoint["provider_profile"],
        requested_max_output_tokens=int(
            validated_plan["selections"]["provider_model"]["max_output_tokens"]
        ),
        execution_kind=validated_checkpoint["execution_kind"],
    )
    evidence_hash = canonical_hash(
        {
            "execution_kind": validated_checkpoint["execution_kind"],
            "checkpoint_hash": validated_checkpoint["checkpoint_hash"],
            "outline_hash": validated_checkpoint["outline_hash"],
        }
    )
    if validated_checkpoint["execution_kind"] == "model":
        raise AutonomyRuntimeError(
            "outline_model_receipt_required",
            "model-generated outlines must be supplied through a receipt-producing outline driver",
        )
    receipt = build_stage_receipt(
        authorization,
        status="succeeded",
        output_digest=validated_checkpoint["outline_hash"],
        model_call_receipt_hash=None,
        execution_evidence_hash=evidence_hash,
    )
    stored = sessions.stage_receipts.append(
        receipt,
        authorization=authorization,
        expected_lease_hash=renewed["lease_hash"],
    )
    return authorization, stored, renewed["lease_hash"]


class AutonomyChapterRuntime:
    """Trusted lifecycle hook consumed by exactly one AgentExecutor chapter run."""

    def __init__(
        self,
        *,
        sessions: AutonomySessionStore,
        session_id: str,
        plan: Mapping[str, Any],
        arc_plan_id: str,
        planned_target_hash: str,
        source_snapshot: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        outline_readiness: Mapping[str, Any],
        outline_stage_receipt: Mapping[str, Any],
        lease_hash: str,
        lease_ttl_seconds: int = 300,
        deterministic_stages: Sequence[str] = (),
        run_dir: str | Path,
        session_started_at: str,
    ) -> None:
        self.sessions = sessions
        self.session_id = safe_id("session_id", session_id)
        self.plan = validate_instruction_plan(plan)
        self.arc_plan_id = safe_id("arc_plan_id", arc_plan_id)
        self.planned_target_hash = sha256_digest(
            "planned_target_hash", planned_target_hash
        )
        self.source_snapshot = validate_source_snapshot(source_snapshot)
        self.checkpoint = validate_outline_checkpoint(checkpoint)
        self.outline_readiness = validate_outline_readiness(dict(outline_readiness))
        self.lease_hash = sha256_digest("lease_hash", lease_hash)
        self.lease_ttl_seconds = positive_int(
            "lease_ttl_seconds", lease_ttl_seconds
        )
        self.provider_profile = str(
            self.plan["selections"]["provider_model"]["profile_id"]
        )
        self.max_output_tokens = int(
            self.plan["selections"]["provider_model"]["max_output_tokens"]
        )
        self.deterministic_stages = frozenset(str(item) for item in deterministic_stages)
        self.run_dir = Path(run_dir).resolve()
        self.session_started_at = str(session_started_at)
        try:
            datetime.fromisoformat(self.session_started_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AutonomyRuntimeError(
                "autonomy_session_time_invalid", "session start timestamp is invalid"
            ) from exc
        self.chapter_index = int(self.checkpoint["chapter_index"])
        self._cursor = 1
        self._last_receipt = dict(outline_stage_receipt)
        chain = self.sessions.stage_receipts.load_chain(
            self.session_id, self.chapter_index
        )
        if not chain or chain[0] != self._last_receipt:
            raise AutonomyRuntimeError(
                "autonomy_outline_receipt_missing",
                "chapter runtime requires the durable outline StageReceipt",
            )
        self.draft_readiness = derive_draft_readiness(
            outline_stage_receipt=self._last_receipt,
            book_id=self.checkpoint["book_id"],
            session_id=self.session_id,
            plan_id=self.plan["plan_id"],
            chapter_index=self.chapter_index,
            authority_epoch=int(self.checkpoint["authority"]["epoch"]),
            authority_head_event_hash=self.checkpoint["authority"]["head_event_hash"],
            current_outline_input_digest=self.checkpoint["outline_input_digest"],
            current_outline_hash=self.checkpoint["outline_hash"],
        )
        validate_draft_readiness(self.draft_readiness)
        if not self.draft_readiness["ok"]:
            raise AutonomyRuntimeError(
                "draft_readiness_rejected", ", ".join(self.draft_readiness["reasons"])
            )

    def expected_run_budget_limits(self) -> RunBudgetLimits:
        budget = self.plan["selections"]["budget"]
        return RunBudgetLimits(
            max_provider_calls=int(budget["max_model_calls"]),
            max_total_input_tokens=int(budget["max_input_tokens"]),
            max_total_output_tokens=int(budget["max_output_tokens"]),
            max_elapsed_seconds=float(budget["max_wall_seconds"]),
        )

    def assert_executor_budget_limits(self, limits: RunBudgetLimits) -> None:
        expected = self.expected_run_budget_limits()
        if limits != expected:
            raise AutonomyRuntimeError(
                "autonomy_budget_binding_mismatch",
                "AgentExecutor budget limits must exactly match the trusted session budget profile",
            )

    def hydrate_budget_tracker(self, tracker: RunBudgetTracker) -> list[str]:
        """Charge a fresh tracker from every durable receipt in this session."""

        self.assert_executor_budget_limits(tracker.limits)
        hydrated: list[str] = []
        for chapter_index in range(
            int(self.plan["chapter_start"]), int(self.plan["chapter_end"]) + 1
        ):
            identity = hashlib.sha256(
                f"{self.session_id}:{chapter_index}".encode("utf-8")
            ).hexdigest()[:40]
            store = ModelCallStore(
                self.run_dir
                / "executions"
                / f"execution_autonomy_{identity}"
                / "model_calls"
            )
            hydrated.extend(
                ModelCallRuntimeContext(store, tracker=tracker).hydrate_tracker_from_store()
            )
        return hydrated

    def _assert_session_wall_budget(self) -> None:
        started = datetime.fromisoformat(
            self.session_started_at.replace("Z", "+00:00")
        )
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        maximum = int(self.plan["selections"]["budget"]["max_wall_seconds"])
        if elapsed > maximum:
            raise ContextBudgetError(
                "run_elapsed_budget_exceeded",
                "trusted session max_wall_seconds exceeded",
            )

    def validate_executor_scope(self, *, chapter_index: int, authority: Mapping[str, Any]) -> None:
        if int(chapter_index) != self.chapter_index:
            raise AutonomyRuntimeError(
                "autonomy_executor_chapter_drift", "executor selected another chapter"
            )
        expected = {
            "epoch": int(self.checkpoint["authority"]["epoch"]),
            "head_event_hash": self.checkpoint["authority"]["head_event_hash"],
        }
        actual = {
            "epoch": int(authority.get("authority_epoch") or authority.get("epoch") or 0),
            "head_event_hash": authority.get("head_event_hash"),
        }
        if actual != expected:
            raise AutonomyRuntimeError(
                "autonomy_executor_authority_drift", "executor authority changed after outline readiness"
            )

    def execution_kind(self, stage: str, *, default_model: bool) -> str:
        if stage in self.deterministic_stages:
            return "deterministic"
        return "model" if default_model else "deterministic"

    def ensure_scene_plan(self, input_value: Any) -> dict[str, Any]:
        output = {
            "outline_checkpoint_hash": self.checkpoint["checkpoint_hash"],
            "planned_target_hash": self.planned_target_hash,
            "chapter_index": self.chapter_index,
        }
        token = self.before_stage(
            stage="scene_plan",
            input_value={
                "outline_hash": self.checkpoint["outline_hash"],
                "generation_input": _digest_value(input_value),
            },
            execution_kind="deterministic",
        )
        return self.after_stage(token, output_value=output, model_call_receipt_hashes=())

    def ensure_polish(self, chapter: str) -> dict[str, Any]:
        chain = self.sessions.stage_receipts.load_chain(
            self.session_id, self.chapter_index
        )
        if len(chain) > self._cursor and chain[self._cursor]["stage"] == "polish":
            token = self.before_stage(
                stage="polish",
                input_value={"chapter_sha256": _digest_value(chapter)},
                execution_kind="deterministic",
            )
            return self.after_stage(
                token, output_value=chapter, model_call_receipt_hashes=()
            )
        if self._last_receipt["stage"] == "polish":
            return self._last_receipt
        token = self.before_stage(
            stage="polish",
            input_value={"chapter_sha256": _digest_value(chapter)},
            execution_kind="deterministic",
        )
        return self.after_stage(token, output_value=chapter, model_call_receipt_hashes=())

    def before_stage(
        self, *, stage: str, input_value: Any, execution_kind: str
    ) -> _StageToken:
        self._assert_session_wall_budget()
        input_digest = _digest_value(input_value)
        renewed = self.sessions.leases.renew(
            book_id=self.checkpoint["book_id"],
            session_id=self.session_id,
            plan_id=self.plan["plan_id"],
            expected_lease_hash=self.lease_hash,
            ttl_seconds=self.lease_ttl_seconds,
        )
        self.lease_hash = renewed["lease_hash"]
        self.sessions.assert_stage_provider_allowed(
            self.session_id, stage=stage
        )
        chain = self.sessions.stage_receipts.load_chain(
            self.session_id, self.chapter_index
        )
        replay = chain[self._cursor] if self._cursor < len(chain) else None
        if replay is not None:
            authorization = self.sessions.stage_receipts.authorization_for(replay)
        else:
            authorization = build_stage_authorization(
                stage=stage,
                book_id=self.checkpoint["book_id"],
                session_id=self.session_id,
                plan_id=self.plan["plan_id"],
                chapter_index=self.chapter_index,
                authority_epoch=int(self.checkpoint["authority"]["epoch"]),
                authority_head_event_hash=self.checkpoint["authority"]["head_event_hash"],
                input_digest=input_digest,
                previous_stage_receipt_hash=self._last_receipt["receipt_hash"],
                provider_profile=self.provider_profile,
                max_output_tokens=self.max_output_tokens,
                execution_kind=execution_kind,
            )
        assert_stage_authorized(
            authorization,
            stage=stage,
            book_id=self.checkpoint["book_id"],
            session_id=self.session_id,
            plan_id=self.plan["plan_id"],
            chapter_index=self.chapter_index,
            authority_epoch=int(self.checkpoint["authority"]["epoch"]),
            authority_head_event_hash=self.checkpoint["authority"]["head_event_hash"],
            input_digest=input_digest,
            previous_stage_receipt_hash=self._last_receipt["receipt_hash"],
            provider_profile=self.provider_profile,
            requested_max_output_tokens=self.max_output_tokens,
            execution_kind=execution_kind,
        )
        operation_key = canonical_hash(
            {
                "session_id": self.session_id,
                "chapter_index": self.chapter_index,
                "stage": stage,
                "input_digest": input_digest,
                "previous_stage_receipt_hash": self._last_receipt["receipt_hash"],
                "execution_kind": execution_kind,
            }
        )
        cached_output = None
        cached_hashes: tuple[str, ...] = ()
        output_record = self._load_stage_output(operation_key)
        if output_record is not None:
            cached_output = copy.deepcopy(output_record["output_value"])
            cached_hashes = tuple(output_record["model_call_receipt_hashes"])
        if replay is not None and output_record is None:
            raise AutonomyRuntimeError(
                "autonomy_stage_output_missing",
                "a durable StageReceipt has no replayable stage output checkpoint",
            )
        return _StageToken(
            authorization,
            replay,
            self._cursor,
            operation_key,
            cached_output,
            cached_hashes,
        )

    def after_stage(
        self,
        token: _StageToken,
        *,
        output_value: Any,
        model_call_receipt_hashes: Sequence[str],
    ) -> dict[str, Any]:
        output_digest = _digest_value(output_value)
        execution_kind = token.authorization["execution_kind"]
        hashes = [sha256_digest("model_call_receipt_hash", item) for item in model_call_receipt_hashes]
        evidence_hash = canonical_hash(
            {
                "execution_kind": execution_kind,
                "stage": token.authorization["stage"],
                "input_digest": token.authorization["input_digest"],
                "output_digest": output_digest,
                "model_call_receipt_hashes": hashes,
                "implementation": "novelagent-autonomy-stage-v1",
            }
        )
        output_record = {
            "schema_version": "1.0",
            "record_hash": "0" * 64,
            "operation_key": token.operation_key,
            "session_id": self.session_id,
            "chapter_index": self.chapter_index,
            "stage": token.authorization["stage"],
            "input_digest": token.authorization["input_digest"],
            "previous_stage_receipt_hash": token.authorization[
                "previous_stage_receipt_hash"
            ],
            "execution_kind": execution_kind,
            "output_digest": output_digest,
            "output_value": copy.deepcopy(output_value),
            "model_call_receipt_hashes": hashes,
            "execution_evidence_hash": evidence_hash,
        }
        output_record["record_hash"] = canonical_hash(
            output_record, exclude_fields=("record_hash",)
        )
        output_path = self._stage_output_path(token.operation_key)
        atomic_append_json(output_path, output_record)
        if token.replay_receipt is not None:
            expected = token.replay_receipt
            if (
                expected["output_digest"] != output_digest
                or expected["execution_kind"] != execution_kind
                or expected["execution_evidence_hash"] != evidence_hash
                or expected["model_call_receipt_hashes"] != hashes
            ):
                raise AutonomyRuntimeError(
                    "autonomy_stage_replay_drift",
                    "replayed stage output or execution evidence changed",
                )
            receipt = expected
        else:
            receipt = build_stage_receipt(
                token.authorization,
                status="succeeded",
                output_digest=output_digest,
                model_call_receipt_hash=hashes[-1] if hashes else None,
                model_call_receipt_hashes=hashes,
                execution_evidence_hash=evidence_hash,
            )
            receipt = self.sessions.stage_receipts.append(
                receipt,
                authorization=token.authorization,
                expected_lease_hash=self.lease_hash,
            )
        self._last_receipt = receipt
        self._cursor = token.cursor + 1
        return receipt

    def _stage_output_path(self, operation_key: str) -> Path:
        digest = sha256_digest("operation_key", operation_key)
        return (
            self.sessions.root
            / "stage_outputs"
            / self.session_id
            / f"chapter-{self.chapter_index:06d}"
            / f"{digest}.json"
        )

    def _load_stage_output(self, operation_key: str) -> dict[str, Any] | None:
        path = self._stage_output_path(operation_key)
        if not path.is_file():
            return None
        record = load_json_object(path)
        required = {
            "schema_version",
            "record_hash",
            "operation_key",
            "session_id",
            "chapter_index",
            "stage",
            "input_digest",
            "previous_stage_receipt_hash",
            "execution_kind",
            "output_digest",
            "output_value",
            "model_call_receipt_hashes",
            "execution_evidence_hash",
        }
        if set(record) != required or record["schema_version"] != "1.0":
            raise AutonomyRuntimeError(
                "autonomy_stage_output_invalid", "stage output checkpoint is malformed"
            )
        if record["operation_key"] != operation_key:
            raise AutonomyRuntimeError(
                "autonomy_stage_output_invalid", "stage output checkpoint scope changed"
            )
        expected = canonical_hash(record, exclude_fields=("record_hash",))
        if record["record_hash"] != expected:
            raise AutonomyRuntimeError(
                "autonomy_stage_output_invalid", "stage output checkpoint hash changed"
            )
        if _digest_value(record["output_value"]) != record["output_digest"]:
            raise AutonomyRuntimeError(
                "autonomy_stage_output_invalid", "stage output bytes changed"
            )
        return record

    def failed_stage(self, token: _StageToken, *, status: str) -> dict[str, Any]:
        if token.replay_receipt is not None:
            return token.replay_receipt
        receipt = build_stage_receipt(
            token.authorization,
            status=status,
            output_digest=None,
            model_call_receipt_hash=None,
        )
        stored = self.sessions.stage_receipts.append(
            receipt,
            authorization=token.authorization,
            expected_lease_hash=self.lease_hash,
        )
        self._last_receipt = stored
        self._cursor = token.cursor + 1
        return stored

    @property
    def final_stage_receipt(self) -> dict[str, Any]:
        chain = self.sessions.stage_receipts.load_chain(
            self.session_id, self.chapter_index
        )
        if not chain or chain[-1]["stage"] != "validator" or chain[-1]["status"] != "succeeded":
            raise AutonomyRuntimeError(
                "autonomy_final_stage_missing",
                "chapter publication requires a successful validator StageReceipt",
            )
        return chain[-1]

    def publication_artifacts(
        self, *, run_id: str, output_root: str | Path, chapter_body_sha256: str
    ) -> list[dict[str, Any]]:
        final_stage = self.final_stage_receipt
        body_hash = sha256_digest("chapter_body_sha256", chapter_body_sha256)
        chain = self.sessions.stage_receipts.load_chain(
            self.session_id, self.chapter_index
        )
        authorizations = [
            self.sessions.stage_receipts.authorization_for(item) for item in chain
        ]
        evidence = {
            "schema_version": "1.0",
            "evidence_hash": "0" * 64,
            "book_id": self.checkpoint["book_id"],
            "session_id": self.session_id,
            "plan_id": self.plan["plan_id"],
            "arc_plan_id": self.arc_plan_id,
            "chapter_index": self.chapter_index,
            "planned_target_hash": self.planned_target_hash,
            "outline_checkpoint_hash": self.checkpoint["checkpoint_hash"],
            "outline_hash": self.checkpoint["outline_hash"],
            "outline_readiness": copy.deepcopy(self.outline_readiness),
            "draft_readiness": copy.deepcopy(self.draft_readiness),
            "stage_authorizations": authorizations,
            "stage_receipts": chain,
            "final_stage_receipt_hash": final_stage["receipt_hash"],
            "chapter_body_sha256": body_hash,
        }
        evidence["evidence_hash"] = canonical_hash(
            evidence, exclude_fields=("evidence_hash",)
        )
        validate_mapping(
            evidence,
            "autonomy_chapter_evidence.schema.json",
            "AutonomyChapterEvidence",
        )
        root = Path(output_root)
        prefix = f"{safe_id('run_id', run_id)}-{self.session_id}-{self.chapter_index:06d}"
        return [
            {
                "kind": "autonomy_outline_evidence",
                "path": root / f"{prefix}-outline.json",
                "content": _json_text(self.checkpoint),
                "root_id": "runtime",
                "metadata": {
                    "session_id": self.session_id,
                    "chapter_index": self.chapter_index,
                    "checkpoint_hash": self.checkpoint["checkpoint_hash"],
                    "outline_hash": self.checkpoint["outline_hash"],
                },
            },
            {
                "kind": "autonomy_stage_evidence",
                "path": root / f"{prefix}-stages.json",
                "content": _json_text(evidence),
                "root_id": "runtime",
                "metadata": {
                    "session_id": self.session_id,
                    "chapter_index": self.chapter_index,
                    "evidence_hash": evidence["evidence_hash"],
                    "final_stage_receipt_hash": final_stage["receipt_hash"],
                    "chapter_body_sha256": body_hash,
                },
            },
        ]


def _assert_outline_replay(
    authorization: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> None:
    expected = (
        "outline",
        checkpoint["book_id"],
        checkpoint["session_id"],
        plan["plan_id"],
        checkpoint["chapter_index"],
        checkpoint["authority"],
        checkpoint["outline_input_digest"],
        checkpoint["provider_profile"],
        checkpoint["execution_kind"],
        checkpoint["outline_hash"],
    )
    actual = (
        authorization["stage"],
        authorization["book_id"],
        authorization["session_id"],
        authorization["plan_id"],
        authorization["chapter_index"],
        authorization["authority"],
        authorization["input_digest"],
        authorization["provider_profile"],
        authorization.get("execution_kind"),
        receipt["output_digest"],
    )
    if actual != expected:
        raise AutonomyRuntimeError(
            "outline_stage_replay_drift", "stored outline StageReceipt no longer matches checkpoint"
        )


def _digest_value(value: Any) -> str:
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return canonical_hash(value)


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ) + "\n"


__all__ = [
    "AutonomyChapterRuntime",
    "AutonomyRuntimeError",
    "record_outline_stage",
]
