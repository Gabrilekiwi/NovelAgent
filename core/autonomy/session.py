from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Mapping

from core.autonomy.arc import ArcPlanStore, build_run_arc_plan, validate_run_arc_plan
from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    atomic_replace_json,
    canonical_hash,
    load_json_object,
    now_utc,
    positive_int,
    required_text,
    safe_id,
    sha256_digest,
    state_lock,
    validate_mapping,
)
from core.autonomy.lease import BookLeaseError, BookLeaseStore
from core.autonomy.operations import AutonomyOperationError, AutonomyOperationStore
from core.autonomy.plans import validate_instruction_plan, validate_source_snapshot
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.receipts import (
    CompletionLedger,
    DeliveryResolutionVerifier,
    PublicationVerifier,
    StageReceiptStore,
)


_TERMINAL_STATES = frozenset({"abandoned", "completed"})


class AutonomySessionError(AutonomyContractError):
    pass


def build_session_genesis(
    *,
    session_id: str,
    instruction_plan: Mapping[str, Any],
    arc_plan: Mapping[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    plan = validate_instruction_plan(instruction_plan)
    arc = validate_run_arc_plan(arc_plan)
    if arc["instruction_plan_hash"] != plan["plan_hash"]:
        raise AutonomySessionError(
            "autonomy_session_arc_mismatch", "RunArcPlan belongs to another InstructionPlan"
        )
    genesis = {
        "schema_version": "1.0",
        "session_id": safe_id("session_id", session_id),
        "book_id": plan["source_snapshot"]["book_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "arc_plan_id": arc["arc_plan_id"],
        "initial_arc_plan_hash": arc["arc_plan_hash"],
        "source_snapshot": copy.deepcopy(plan["source_snapshot"]),
        "created_at": created_at or now_utc(),
    }
    genesis["genesis_hash"] = canonical_hash(genesis, exclude_fields=("genesis_hash",))
    return validate_session_genesis(genesis)


def validate_session_genesis(value: Any) -> dict[str, Any]:
    genesis = validate_mapping(
        value, "autonomy_session_genesis.schema.json", "AutonomySessionGenesis"
    )
    for field in ("session_id", "book_id", "plan_id", "arc_plan_id"):
        safe_id(field, genesis[field])
    for field in ("genesis_hash", "plan_hash", "initial_arc_plan_hash"):
        sha256_digest(field, genesis[field])
    validate_source_snapshot(genesis["source_snapshot"])
    expected = canonical_hash(genesis, exclude_fields=("genesis_hash",))
    if genesis["genesis_hash"] != expected:
        raise AutonomySessionError(
            "autonomy_session_genesis_hash_mismatch", "session genesis was modified"
        )
    return genesis


def build_session_event(
    *,
    genesis: Mapping[str, Any],
    sequence: int,
    event_type: str,
    previous_event_hash: str | None,
    reason: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    origin = validate_session_genesis(genesis)
    event = {
        "schema_version": "1.0",
        "session_id": origin["session_id"],
        "book_id": origin["book_id"],
        "plan_id": origin["plan_id"],
        "plan_hash": origin["plan_hash"],
        "genesis_hash": origin["genesis_hash"],
        "sequence": positive_int("sequence", sequence),
        "event_type": str(event_type),
        "previous_event_hash": sha256_digest(
            "previous_event_hash", previous_event_hash, optional=True
        ),
        "reason": required_text("reason", reason) if reason is not None else None,
        "recorded_at": recorded_at or now_utc(),
    }
    event["event_hash"] = canonical_hash(event, exclude_fields=("event_hash",))
    return validate_session_event(event)


def validate_session_event(value: Any) -> dict[str, Any]:
    event = validate_mapping(value, "autonomy_session_event.schema.json", "AutonomySessionEvent")
    for field in ("session_id", "book_id", "plan_id"):
        safe_id(field, event[field])
    for field in ("event_hash", "plan_hash", "genesis_hash"):
        sha256_digest(field, event[field])
    sha256_digest("previous_event_hash", event["previous_event_hash"], optional=True)
    positive_int("sequence", event["sequence"])
    expected = canonical_hash(event, exclude_fields=("event_hash",))
    if event["event_hash"] != expected:
        raise AutonomySessionError(
            "autonomy_session_event_hash_mismatch", "session event was modified"
        )
    return event


class AutonomySessionStore:
    def __init__(
        self,
        root: str | Path,
        *,
        trusted_profiles: TrustedProfiles | None = None,
        publication_verifier: PublicationVerifier | None = None,
        publication_root_map: Mapping[str, str | Path] | None = None,
        delivery_resolution_verifier: DeliveryResolutionVerifier | None = None,
        reconcile_on_open: bool = True,
    ) -> None:
        self.root = Path(root)
        self.trusted_profiles = trusted_profiles
        self.leases = BookLeaseStore(self.root)
        self.arc_plans = ArcPlanStore(self.root)
        self.stage_receipts = StageReceiptStore(self.root)
        self.operations = AutonomyOperationStore(self.root)
        self._defer_status_recovery_once: set[str] = set()
        self.publication_verifier = publication_verifier
        self.publication_root_map = (
            dict(publication_root_map) if publication_root_map is not None else None
        )
        self.delivery_resolution_verifier = delivery_resolution_verifier
        # Normal runtime opening is a recovery boundary. Read-only inspectors
        # that already own the shared remap fence explicitly opt out to avoid
        # attempting to acquire the non-reentrant Windows lock twice.
        if reconcile_on_open:
            self.reconcile_orphans()

    def save_preview(self, instruction_plan: Mapping[str, Any]) -> Path:
        profiles = self._require_trusted_profiles()
        plan = validate_instruction_plan(
            instruction_plan, trusted_profiles=profiles
        )
        path = self.root / "plans" / f"{plan['plan_id']}-{plan['plan_hash'][:16]}.json"
        atomic_append_json(path, plan)
        return path

    def reconcile_orphans(self, *, at: str | None = None) -> list[dict[str, Any]]:
        """Recover or safely roll back incomplete cross-artifact operations."""

        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._reconcile_orphans_fenced(at=at)

    def _reconcile_orphans_fenced(
        self, *, at: str | None = None, defer_transient: bool = False
    ) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        timestamp = at or now_utc()
        for intent in sorted(
            self.operations.pending(),
            key=lambda item: (item["created_at"], int(item["attempt"]), item["operation_id"]),
        ):
            if defer_transient and intent["operation_id"] in self._defer_status_recovery_once:
                self._defer_status_recovery_once.remove(intent["operation_id"])
                continue
            marker = self.operations.source_verification(intent)
            if intent["operation_type"] in {"execute", "resume"}:
                if marker is None:
                    recovered.append(
                        self._rollback_unverified_source_operation(
                            intent, completed_at=timestamp
                        )
                    )
                elif intent["expected_state"] == "absent":
                    recovered.append(self._roll_forward_execute(intent, marker=marker))
                else:
                    recovered.append(
                        self._roll_forward_source_transition(intent, marker=marker)
                    )
            else:
                recovered.append(self._roll_forward_terminal_transition(intent))
        return recovered

    def _rollback_unverified_source_operation(
        self, intent: Mapping[str, Any], *, completed_at: str
    ) -> dict[str, Any]:
        operation = dict(intent)
        session_dir = self._session_dir(operation["session_id"])
        if operation["expected_state"] == "absent" and (
            (session_dir / "genesis.json").exists()
            or (session_dir / "instruction_plan.json").exists()
        ):
            raise AutonomyOperationError(
                "autonomy_operation_unverified_artifacts",
                "an unverified execute operation already published session artifacts",
            )
        if operation["expected_state"] != "absent":
            events = self._load_events(operation["session_id"])
            if events[-1]["event_hash"] != operation["expected_event_hash"]:
                raise AutonomyOperationError(
                    "autonomy_operation_unverified_event",
                    "an unverified source operation advanced the session event chain",
                )
        current = self.leases._reconcile_fenced(operation["book_id"])
        if (
            current is not None
            and current["status"] == "active"
            and current["session_id"] == operation["session_id"]
            and current["plan_id"] == operation["plan_id"]
            and (
                operation["expected_state"] != "absent"
                or current["lease_hash"] != operation["expected_lease_hash"]
            )
        ):
            current = self.leases._release_fenced(
                book_id=operation["book_id"],
                session_id=operation["session_id"],
                plan_id=operation["plan_id"],
                expected_lease_hash=current["lease_hash"],
                at=completed_at,
            )
        return self.operations.finish(
            operation,
            outcome="rolled_back",
            event_hash=None,
            lease_hash=current["lease_hash"] if current is not None else None,
            completed_at=completed_at,
        )

    def _roll_forward_execute(
        self,
        intent: Mapping[str, Any],
        *,
        marker: Mapping[str, Any] | None = None,
        plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = dict(intent)
        if operation["expected_state"] != "absent" or operation["target_event_type"] != "started":
            raise AutonomyOperationError(
                "autonomy_operation_transition_invalid",
                "execute recovery requires an absent session and started target",
            )
        verification = dict(marker or self.operations.source_verification(operation) or {})
        if not verification:
            raise AutonomyOperationError(
                "autonomy_operation_source_unverified",
                "execute cannot publish session state before source verification",
            )
        resolved_plan = validate_instruction_plan(
            dict(plan) if plan is not None else load_json_object(self._preview_path(operation))
        )
        if (
            resolved_plan["plan_id"] != operation["plan_id"]
            or resolved_plan["plan_hash"] != operation["plan_hash"]
            or resolved_plan["source_snapshot"]["book_id"] != operation["book_id"]
            or resolved_plan["source_snapshot"]["snapshot_hash"]
            != verification["source_snapshot_hash"]
        ):
            raise AutonomyOperationError(
                "autonomy_operation_plan_mismatch",
                "verified execute plan does not match its immutable intent",
            )
        lease = self.leases.load_history(
            operation["book_id"], verification["lease_hash"]
        )
        if (
            lease["session_id"] != operation["session_id"]
            or lease["plan_id"] != operation["plan_id"]
        ):
            raise AutonomyOperationError(
                "autonomy_operation_lease_scope_mismatch",
                "verified execute lease belongs to another writer",
            )
        arc = self.arc_plans.create(
            build_run_arc_plan(
                resolved_plan,
                session_id=operation["session_id"],
                created_at=operation["created_at"],
            )
        )
        genesis = build_session_genesis(
            session_id=operation["session_id"],
            instruction_plan=resolved_plan,
            arc_plan=arc,
            created_at=operation["created_at"],
        )
        directory = self._session_dir(operation["session_id"])
        atomic_append_json(directory / "genesis.json", genesis)
        atomic_append_json(directory / "instruction_plan.json", resolved_plan)
        events = self._load_events(operation["session_id"], allow_empty=True)
        expected_event = build_session_event(
            genesis=genesis,
            sequence=1,
            event_type="started",
            previous_event_hash=None,
            recorded_at=operation["created_at"],
        )
        if not events:
            event = self._append_event(
                operation["session_id"], "started", recorded_at=operation["created_at"]
            )
        elif len(events) == 1 and events[0] == expected_event:
            event = events[0]
        else:
            raise AutonomyOperationError(
                "autonomy_operation_event_conflict",
                "execute recovery found a different session event chain",
            )
        atomic_append_json(
            self._plan_index_path(operation["plan_hash"]),
            {
                "schema_version": "1.0",
                "plan_hash": operation["plan_hash"],
                "session_id": operation["session_id"],
                "genesis_hash": genesis["genesis_hash"],
            },
        )
        atomic_replace_json(
            self.root / "sessions" / "latest.json",
            {
                "schema_version": "1.0",
                "session_id": operation["session_id"],
                "genesis_hash": genesis["genesis_hash"],
            },
        )
        return self.operations.finish(
            operation,
            outcome="completed",
            event_hash=event["event_hash"],
            lease_hash=verification["lease_hash"],
            completed_at=operation["created_at"],
        )

    def _roll_forward_source_transition(
        self, intent: Mapping[str, Any], *, marker: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        operation = dict(intent)
        verification = dict(marker or self.operations.source_verification(operation) or {})
        if not verification:
            raise AutonomyOperationError(
                "autonomy_operation_source_unverified",
                "source-guarded transition lacks durable verification",
            )
        genesis = self._load_genesis(operation["session_id"])
        plan = self._load_plan(operation["session_id"])
        self._assert_operation_scope(operation, genesis=genesis, plan=plan)
        expected_source = self.completion_ledger(
            operation["session_id"]
        ).expected_source_snapshot()
        if verification["source_snapshot_hash"] != expected_source["snapshot_hash"]:
            raise AutonomyOperationError(
                "autonomy_operation_source_mismatch",
                "source verification no longer matches the completion boundary",
            )
        lease = self.leases.load_history(
            operation["book_id"], verification["lease_hash"]
        )
        if (
            lease["session_id"] != operation["session_id"]
            or lease["plan_id"] != operation["plan_id"]
        ):
            raise AutonomyOperationError(
                "autonomy_operation_lease_scope_mismatch",
                "verified transition lease belongs to another writer",
            )
        event = self._recover_expected_event(operation, genesis=genesis)
        return self.operations.finish(
            operation,
            outcome="completed",
            event_hash=event["event_hash"],
            lease_hash=verification["lease_hash"],
            completed_at=operation["created_at"],
        )

    def _roll_forward_terminal_transition(
        self, intent: Mapping[str, Any]
    ) -> dict[str, Any]:
        operation = dict(intent)
        genesis = self._load_genesis(operation["session_id"])
        plan = self._load_plan(operation["session_id"])
        self._assert_operation_scope(operation, genesis=genesis, plan=plan)
        events = self._load_events(operation["session_id"])
        already_applied = events[-1]["event_type"] == operation["target_event_type"]
        if operation["operation_type"] == "complete" and not already_applied:
            completion = self.completion_ledger(operation["session_id"]).summary()
            if (
                completion["completed_count"] != plan["requested_chapter_count"]
                or completion["delivery_blocked"]
            ):
                raise AutonomyOperationError(
                    "autonomy_operation_completion_unproven",
                    "completion intent no longer has a valid receipt proof",
                )
        current = self.leases._reconcile_fenced(operation["book_id"])
        if (
            current is not None
            and current["status"] == "active"
            and (
                current["session_id"] != operation["session_id"]
                or current["plan_id"] != operation["plan_id"]
            )
        ):
            raise AutonomyOperationError(
                "autonomy_operation_foreign_lease",
                "terminal transition cannot cross an active foreign writer lease",
            )
        event = self._recover_expected_event(operation, genesis=genesis)
        current = self.leases._reconcile_fenced(operation["book_id"])
        if current is not None and current["status"] == "active":
            current = self.leases._release_fenced(
                book_id=operation["book_id"],
                session_id=operation["session_id"],
                plan_id=operation["plan_id"],
                expected_lease_hash=current["lease_hash"],
                at=operation["created_at"],
            )
        return self.operations.finish(
            operation,
            outcome="completed",
            event_hash=event["event_hash"],
            lease_hash=current["lease_hash"] if current is not None else None,
            completed_at=operation["created_at"],
        )

    def _recover_expected_event(
        self, intent: Mapping[str, Any], *, genesis: Mapping[str, Any]
    ) -> dict[str, Any]:
        operation = dict(intent)
        events = self._load_events(operation["session_id"])
        expected_hash = operation["expected_event_hash"]
        if events[-1]["event_hash"] == expected_hash:
            if operation["target_event_type"] is None:
                return events[-1]
            return self._append_event(
                operation["session_id"],
                operation["target_event_type"],
                reason=operation["reason"],
                recorded_at=operation["created_at"],
            )
        for index, predecessor in enumerate(events[:-1]):
            if predecessor["event_hash"] != expected_hash:
                continue
            expected = build_session_event(
                genesis=genesis,
                sequence=int(predecessor["sequence"]) + 1,
                event_type=operation["target_event_type"],
                previous_event_hash=predecessor["event_hash"],
                reason=operation["reason"],
                recorded_at=operation["created_at"],
            )
            if index + 1 == len(events) - 1 and events[index + 1] == expected:
                return events[index + 1]
        raise AutonomyOperationError(
            "autonomy_operation_event_precondition_failed",
            "session event chain no longer matches the operation intent",
        )

    @staticmethod
    def _assert_operation_scope(
        intent: Mapping[str, Any], *, genesis: Mapping[str, Any], plan: Mapping[str, Any]
    ) -> None:
        if (
            genesis["session_id"] != intent["session_id"]
            or genesis["book_id"] != intent["book_id"]
            or genesis["plan_id"] != intent["plan_id"]
            or genesis["plan_hash"] != intent["plan_hash"]
            or plan["plan_hash"] != intent["plan_hash"]
        ):
            raise AutonomyOperationError(
                "autonomy_operation_scope_mismatch",
                "operation scope does not match durable session artifacts",
            )

    def _preview_path(self, intent: Mapping[str, Any]) -> Path:
        return self.root / "plans" / (
            f"{intent['plan_id']}-{str(intent['plan_hash'])[:16]}.json"
        )

    def execute_plan(
        self,
        instruction_plan: Mapping[str, Any],
        *,
        source_snapshot_loader: Callable[[], Mapping[str, Any]],
        lease_ttl_seconds: int = 300,
        recover_committed_boundary: Callable[[str], None] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._execute_plan_fenced(
                instruction_plan,
                source_snapshot_loader=source_snapshot_loader,
                lease_ttl_seconds=lease_ttl_seconds,
                recover_committed_boundary=recover_committed_boundary,
                at=at,
            )

    def _execute_plan_fenced(
        self,
        instruction_plan: Mapping[str, Any],
        *,
        source_snapshot_loader: Callable[[], Mapping[str, Any]],
        lease_ttl_seconds: int,
        recover_committed_boundary: Callable[[str], None] | None,
        at: str | None,
    ) -> dict[str, Any]:
        self._reconcile_orphans_fenced(at=at)
        profiles = self._require_trusted_profiles()
        plan = validate_instruction_plan(
            instruction_plan, trusted_profiles=profiles
        )
        source_loader = _validate_source_snapshot_loader(source_snapshot_loader)
        session_seed = canonical_hash(
            {"plan_hash": plan["plan_hash"], "book_id": plan["source_snapshot"]["book_id"]}
        )
        session_id = f"session_{session_seed[:24]}"
        index_path = self._plan_index_path(plan["plan_hash"])
        timestamp = at or now_utc()
        # Creation has its own lock namespace so lease/arc/event stores can
        # take their normal durable locks without recursively locking the same
        # byte-range handle on Windows.
        with state_lock(self.root / "_session_creation", index_path):
            if index_path.exists():
                index = load_json_object(index_path)
                if index.get("plan_hash") != plan["plan_hash"] or index.get(
                    "session_id"
                ) != session_id:
                    raise AutonomySessionError(
                        "autonomy_plan_index_conflict", "plan index points to another session"
                    )
                current = self._status_unreconciled(session_id, at=timestamp)
                if current["state"] != "active":
                    return current
                intent = self.operations.begin(
                    operation_type="execute",
                    session_id=session_id,
                    book_id=current["book_id"],
                    plan_id=plan["plan_id"],
                    plan_hash=plan["plan_hash"],
                    expected_state="active",
                    expected_event_hash=current["last_event_hash"],
                    expected_lease_hash=current["lease_hash"],
                    target_event_type=None,
                    reason=None,
                    lease_ttl_seconds=lease_ttl_seconds,
                    created_at=timestamp,
                )
                lease = self.leases._acquire_fenced(
                    book_id=current["book_id"],
                    session_id=session_id,
                    plan_id=plan["plan_id"],
                    ttl_seconds=lease_ttl_seconds,
                    at=timestamp,
                )
                try:
                    if recover_committed_boundary is not None:
                        recover_committed_boundary(session_id)
                    current_source = validate_source_snapshot(source_loader())
                    expected_source = self.completion_ledger(
                        session_id
                    ).expected_source_snapshot()
                    if current_source["snapshot_hash"] != expected_source["snapshot_hash"]:
                        raise AutonomySessionError(
                            "autonomy_session_source_snapshot_stale",
                            "current StoryProject does not match the latest verified completion boundary",
                        )
                    marker = self.operations.mark_source_verified(
                        intent,
                        source_snapshot_hash=current_source["snapshot_hash"],
                        lease_hash=lease["lease_hash"],
                        verified_at=timestamp,
                    )
                    self._roll_forward_source_transition(intent, marker=marker)
                except Exception:
                    if self.operations.source_verification(intent) is None:
                        self._rollback_unverified_source_operation(
                            intent, completed_at=timestamp
                        )
                    raise
                return self._status_unreconciled(session_id, at=timestamp)

            prior_lease = self.leases._reconcile_fenced(
                plan["source_snapshot"]["book_id"]
            )
            intent = self.operations.begin(
                operation_type="execute",
                session_id=session_id,
                book_id=plan["source_snapshot"]["book_id"],
                plan_id=plan["plan_id"],
                plan_hash=plan["plan_hash"],
                expected_state="absent",
                expected_event_hash=None,
                expected_lease_hash=(
                    prior_lease["lease_hash"] if prior_lease is not None else None
                ),
                target_event_type="started",
                reason=None,
                lease_ttl_seconds=lease_ttl_seconds,
                created_at=timestamp,
            )
            lease = self.leases._acquire_fenced(
                book_id=plan["source_snapshot"]["book_id"],
                session_id=session_id,
                plan_id=plan["plan_id"],
                ttl_seconds=lease_ttl_seconds,
                at=timestamp,
            )
            try:
                current_source = validate_source_snapshot(source_loader())
                validate_instruction_plan(
                    plan, current_source_snapshot=current_source
                )
                self.save_preview(plan)
                marker = self.operations.mark_source_verified(
                    intent,
                    source_snapshot_hash=current_source["snapshot_hash"],
                    lease_hash=lease["lease_hash"],
                    verified_at=timestamp,
                )
                self._roll_forward_execute(intent, marker=marker, plan=plan)
            except Exception:
                if self.operations.source_verification(intent) is None:
                    self._rollback_unverified_source_operation(
                        intent, completed_at=timestamp
                    )
                raise
        return self._status_unreconciled(session_id, at=timestamp)

    def status(self, session_id: str | None = None, *, at: str | None = None) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            self._reconcile_orphans_fenced(at=at, defer_transient=True)
            return self._status_unreconciled(session_id, at=at)

    def _status_unreconciled(
        self, session_id: str | None = None, *, at: str | None = None
    ) -> dict[str, Any]:
        resolved = self.resolve_session_id(session_id)
        genesis = self._load_genesis(resolved)
        plan = self._load_plan(resolved)
        if plan["plan_hash"] != genesis["plan_hash"]:
            raise AutonomySessionError(
                "autonomy_session_plan_mismatch", "session plan does not match genesis"
            )
        events = self._load_events(resolved)
        state = _state_from_events(events)
        arc = self.arc_plans.load(genesis["arc_plan_id"])
        ledger = self.completion_ledger(resolved)
        completion = ledger.summary()
        lease: dict[str, Any] | None
        lease_held = False
        try:
            lease = self.leases._reconcile_fenced(genesis["book_id"])
            if lease is None:
                raise BookLeaseError("book_lease_missing", "book has no lease record")
        except BookLeaseError as exc:
            if exc.code != "book_lease_missing":
                raise
            lease = None
        if lease is not None:
            owns_lease = (
                lease["session_id"] == resolved and lease["plan_id"] == genesis["plan_id"]
            )
            if owns_lease and lease["status"] == "active":
                try:
                    self.leases._assert_held_remap_fenced(
                        book_id=genesis["book_id"],
                        session_id=resolved,
                        plan_id=genesis["plan_id"],
                        at=at or now_utc(),
                    )
                    lease_held = True
                except BookLeaseError as exc:
                    if exc.code not in {"book_lease_not_held", "book_lease_expired"}:
                        raise
                    lease_held = False
        result = {
            "schema_version": "1.0",
            "session_id": resolved,
            "book_id": genesis["book_id"],
            "plan_id": genesis["plan_id"],
            "plan_hash": genesis["plan_hash"],
            "arc_plan_id": genesis["arc_plan_id"],
            "arc_plan_hash": arc["arc_plan_hash"],
            "state": state,
            "lease_held": lease_held,
            "lease_hash": lease["lease_hash"] if lease is not None else None,
            "event_count": len(events),
            "last_event_hash": events[-1]["event_hash"],
            "requested_chapter_count": plan["requested_chapter_count"],
            "trusted_profiles_current": (
                self.trusted_profiles is not None
                and plan["profile_set_id"] == self.trusted_profiles.profile_set_id
                and plan["profile_set_hash"] == self.trusted_profiles.profile_set_hash
            ),
            **completion,
        }
        result["finalization_required"] = (
            state == "active"
            and result["completed_count"] == result["requested_chapter_count"]
            and not result["delivery_blocked"]
        )
        result["root_remap_blocked"] = state == "active" or lease_held
        return result

    def resume(
        self,
        session_id: str | None,
        *,
        source_snapshot_loader: Callable[[], Mapping[str, Any]],
        lease_ttl_seconds: int = 300,
        recover_committed_boundary: Callable[[str], None] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._resume_fenced(
                session_id,
                source_snapshot_loader=source_snapshot_loader,
                lease_ttl_seconds=lease_ttl_seconds,
                recover_committed_boundary=recover_committed_boundary,
                at=at,
            )

    def _resume_fenced(
        self,
        session_id: str | None,
        *,
        source_snapshot_loader: Callable[[], Mapping[str, Any]],
        lease_ttl_seconds: int,
        recover_committed_boundary: Callable[[str], None] | None,
        at: str | None,
    ) -> dict[str, Any]:
        self._reconcile_orphans_fenced(at=at)
        resolved = self.resolve_session_id(session_id)
        profiles = self._require_trusted_profiles()
        plan = validate_instruction_plan(
            self._load_plan(resolved), trusted_profiles=profiles
        )
        current = self._status_unreconciled(resolved, at=at)
        if current["state"] in _TERMINAL_STATES:
            raise AutonomySessionError(
                "autonomy_session_terminal", f"cannot resume {current['state']} session"
            )
        timestamp = at or now_utc()
        intent = self.operations.begin(
            operation_type="resume",
            session_id=resolved,
            book_id=current["book_id"],
            plan_id=plan["plan_id"],
            plan_hash=plan["plan_hash"],
            expected_state=current["state"],
            expected_event_hash=current["last_event_hash"],
            expected_lease_hash=current["lease_hash"],
            target_event_type=("resumed" if current["state"] == "cancelled" else None),
            reason=None,
            lease_ttl_seconds=lease_ttl_seconds,
            created_at=timestamp,
        )
        lease = self.leases._acquire_fenced(
            book_id=current["book_id"],
            session_id=resolved,
            plan_id=plan["plan_id"],
            ttl_seconds=lease_ttl_seconds,
            at=timestamp,
        )
        try:
            if recover_committed_boundary is not None:
                recover_committed_boundary(resolved)
            current_source = validate_source_snapshot(
                _validate_source_snapshot_loader(source_snapshot_loader)()
            )
            expected_source = self.completion_ledger(resolved).expected_source_snapshot()
            if current_source["snapshot_hash"] != expected_source["snapshot_hash"]:
                raise AutonomySessionError(
                    "autonomy_session_source_snapshot_stale",
                    "current StoryProject does not match the latest verified completion boundary",
                )
            marker = self.operations.mark_source_verified(
                intent,
                source_snapshot_hash=current_source["snapshot_hash"],
                lease_hash=lease["lease_hash"],
                verified_at=timestamp,
            )
            self._roll_forward_source_transition(intent, marker=marker)
        except Exception:
            if self.operations.source_verification(intent) is None:
                self._rollback_unverified_source_operation(
                    intent, completed_at=timestamp
                )
            raise
        return self._status_unreconciled(resolved, at=timestamp)

    def cancel(
        self,
        session_id: str | None,
        *,
        reason: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._cancel_fenced(session_id, reason=reason, at=at)

    def _cancel_fenced(
        self,
        session_id: str | None,
        *,
        reason: str | None,
        at: str | None,
    ) -> dict[str, Any]:
        self._reconcile_orphans_fenced(at=at)
        resolved = self.resolve_session_id(session_id)
        current = self._status_unreconciled(resolved, at=at)
        if current["state"] == "cancelled":
            return self._finish_terminal_lease_release(resolved, current, at=at)
        if current["state"] in _TERMINAL_STATES:
            raise AutonomySessionError(
                "autonomy_session_terminal", f"cannot cancel {current['state']} session"
            )
        timestamp = at or now_utc()
        plan = self._load_plan(resolved)
        intent = self.operations.begin(
            operation_type="cancel",
            session_id=resolved,
            book_id=current["book_id"],
            plan_id=current["plan_id"],
            plan_hash=plan["plan_hash"],
            expected_state=current["state"],
            expected_event_hash=current["last_event_hash"],
            expected_lease_hash=current["lease_hash"],
            target_event_type="cancelled",
            reason=reason,
            lease_ttl_seconds=None,
            created_at=timestamp,
        )
        try:
            self._roll_forward_terminal_transition(intent)
        except Exception:
            self._defer_status_recovery_once.add(intent["operation_id"])
            raise
        return self._status_unreconciled(resolved, at=timestamp)

    def abandon(
        self,
        session_id: str | None,
        *,
        reason: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._abandon_fenced(session_id, reason=reason, at=at)

    def _abandon_fenced(
        self,
        session_id: str | None,
        *,
        reason: str | None,
        at: str | None,
    ) -> dict[str, Any]:
        self._reconcile_orphans_fenced(at=at)
        resolved = self.resolve_session_id(session_id)
        current = self._status_unreconciled(resolved, at=at)
        if current["state"] == "abandoned":
            return self._finish_terminal_lease_release(resolved, current, at=at)
        if current["state"] == "completed":
            raise AutonomySessionError(
                "autonomy_session_terminal", "completed session cannot be abandoned"
            )
        timestamp = at or now_utc()
        plan = self._load_plan(resolved)
        intent = self.operations.begin(
            operation_type="abandon",
            session_id=resolved,
            book_id=current["book_id"],
            plan_id=current["plan_id"],
            plan_hash=plan["plan_hash"],
            expected_state=current["state"],
            expected_event_hash=current["last_event_hash"],
            expected_lease_hash=current["lease_hash"],
            target_event_type="abandoned",
            reason=reason,
            lease_ttl_seconds=None,
            created_at=timestamp,
        )
        try:
            self._roll_forward_terminal_transition(intent)
        except Exception:
            self._defer_status_recovery_once.add(intent["operation_id"])
            raise
        return self._status_unreconciled(resolved, at=timestamp)

    def complete(
        self, session_id: str | None, *, at: str | None = None
    ) -> dict[str, Any]:
        """Close a session only after its receipt ledger proves the target count."""

        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._complete_fenced(session_id, at=at)

    def _complete_fenced(
        self, session_id: str | None, *, at: str | None
    ) -> dict[str, Any]:
        """Apply completion while holding the runtime remap fence."""

        self._reconcile_orphans_fenced(at=at)
        resolved = self.resolve_session_id(session_id)
        current = self._status_unreconciled(resolved, at=at)
        if current["state"] == "completed":
            return self._finish_terminal_lease_release(resolved, current, at=at)
        if current["state"] != "active":
            raise AutonomySessionError(
                "autonomy_session_not_active", "only an active session can complete"
            )
        if current["completed_count"] != current["requested_chapter_count"]:
            raise AutonomySessionError(
                "autonomy_session_incomplete",
                "completion receipt count does not satisfy the InstructionPlan",
            )
        if current["delivery_blocked"]:
            raise AutonomySessionError(
                "autonomy_session_delivery_blocked",
                "required delivery must be resolved before session completion",
            )
        timestamp = at or now_utc()
        plan = self._load_plan(resolved)
        intent = self.operations.begin(
            operation_type="complete",
            session_id=resolved,
            book_id=current["book_id"],
            plan_id=current["plan_id"],
            plan_hash=plan["plan_hash"],
            expected_state=current["state"],
            expected_event_hash=current["last_event_hash"],
            expected_lease_hash=current["lease_hash"],
            target_event_type="completed",
            reason=None,
            lease_ttl_seconds=None,
            created_at=timestamp,
        )
        try:
            self._roll_forward_terminal_transition(intent)
        except Exception:
            self._defer_status_recovery_once.add(intent["operation_id"])
            raise
        return self._status_unreconciled(resolved, at=timestamp)

    def assert_outline_provider_allowed(
        self, session_id: str | None, *, at: str | None = None
    ) -> dict[str, Any]:
        """Mandatory local gate immediately before the first outline call."""

        return self.assert_stage_provider_allowed(session_id, stage="outline", at=at)

    def assert_stage_provider_allowed(
        self,
        session_id: str | None,
        *,
        stage: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Mandatory local gate immediately before every model-backed stage."""

        resolved = self.resolve_session_id(session_id)
        status = self.status(resolved, at=at)
        if status["state"] != "active":
            raise AutonomySessionError(
                f"{safe_id('stage', stage)}_session_not_active",
                f"{stage} provider call requires an active session",
            )
        if status["delivery_blocked"]:
            raise AutonomySessionError(
                "autonomy_session_delivery_blocked",
                "required delivery is blocked; no later chapter stage may start",
            )
        return self.leases.assert_held(
            book_id=status["book_id"],
            session_id=resolved,
            plan_id=status["plan_id"],
            at=at or now_utc(),
        )

    def _release_after_failed_source_check(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        expected_lease_hash: str,
        at: str,
    ) -> None:
        """Fail closed after a fenced source read without masking its error."""

        try:
            self.leases._release_fenced(
                book_id=book_id,
                session_id=session_id,
                plan_id=plan_id,
                expected_lease_hash=expected_lease_hash,
                at=at,
            )
        except BookLeaseError:
            # A concurrent fence change is itself sufficient to make later
            # provider and commit gates reject this writer.
            return

    def _finish_terminal_lease_release(
        self,
        session_id: str,
        current: Mapping[str, Any],
        *,
        at: str | None,
    ) -> dict[str, Any]:
        """Retry the release half of a terminal transition after a crash/error."""

        if current["lease_held"]:
            self.leases._release_fenced(
                book_id=current["book_id"],
                session_id=session_id,
                plan_id=current["plan_id"],
                expected_lease_hash=current["lease_hash"],
                at=at or now_utc(),
            )
        return self._status_unreconciled(session_id, at=at)

    def completion_ledger(self, session_id: str | None = None) -> CompletionLedger:
        resolved = self.resolve_session_id(session_id)
        genesis = self._load_genesis(resolved)
        kwargs: dict[str, Any] = {}
        if self.publication_verifier is not None:
            kwargs["publication_verifier"] = self.publication_verifier
        if self.publication_root_map is not None:
            kwargs["publication_root_map"] = self.publication_root_map
        if self.delivery_resolution_verifier is not None:
            kwargs["delivery_resolution_verifier"] = self.delivery_resolution_verifier
        return CompletionLedger(
            self.root,
            instruction_plan=self._load_plan(resolved),
            session_id=resolved,
            arc_plan_id=genesis["arc_plan_id"],
            stage_receipts=self.stage_receipts,
            **kwargs,
        )

    def load_instruction_plan(self, session_id: str | None = None) -> dict[str, Any]:
        """Return the validated immutable plan bound to a durable session."""

        return self._load_plan(self.resolve_session_id(session_id))

    def session_started_at(self, session_id: str | None = None) -> str:
        resolved = self.resolve_session_id(session_id)
        events = self._load_events(resolved)
        return str(events[0]["recorded_at"])

    def resolve_session_id(self, session_id: str | None) -> str:
        if session_id not in (None, "", "latest"):
            return safe_id("session_id", session_id)
        latest_path = self.root / "sessions" / "latest.json"
        if not latest_path.is_file():
            latest = self._rebuild_latest_pointer()
        else:
            latest = load_json_object(latest_path)
        resolved = safe_id("session_id", latest.get("session_id"))
        genesis = self._load_genesis(resolved)
        if latest.get("genesis_hash") != genesis["genesis_hash"]:
            raise AutonomySessionError(
                "autonomy_latest_session_invalid", "latest session pointer was modified"
            )
        return resolved

    def _rebuild_latest_pointer(self) -> dict[str, Any]:
        """Rebuild the mutable latest cache from fully verified session facts."""

        sessions_root = self.root / "sessions"
        candidates: list[tuple[str, str, str, dict[str, Any]]] = []
        for directory in sorted(
            (item for item in sessions_root.iterdir() if item.is_dir()),
            key=lambda item: item.name,
        ) if sessions_root.is_dir() else []:
            session_id = safe_id("session_id", directory.name)
            genesis = self._load_genesis(session_id)
            if genesis["session_id"] != session_id:
                raise AutonomySessionError(
                    "autonomy_latest_session_invalid",
                    "session directory name does not match immutable genesis",
                )
            plan = self._load_plan(session_id)
            if (
                plan["plan_hash"] != genesis["plan_hash"]
                or plan["plan_id"] != genesis["plan_id"]
                or plan["source_snapshot"] != genesis["source_snapshot"]
            ):
                raise AutonomySessionError(
                    "autonomy_latest_session_invalid",
                    "session plan does not match immutable genesis",
                )
            events = self._load_events(session_id)
            if events[0]["event_type"] != "started":
                raise AutonomySessionError(
                    "autonomy_latest_session_invalid",
                    "session event chain does not begin with started",
                )
            index = load_json_object(self._plan_index_path(genesis["plan_hash"]))
            expected_index = {
                "schema_version": "1.0",
                "plan_hash": genesis["plan_hash"],
                "session_id": session_id,
                "genesis_hash": genesis["genesis_hash"],
            }
            if index != expected_index:
                raise AutonomySessionError(
                    "autonomy_latest_session_invalid",
                    "session plan index does not match immutable genesis",
                )
            pointer = {
                "schema_version": "1.0",
                "session_id": session_id,
                "genesis_hash": genesis["genesis_hash"],
            }
            candidates.append(
                (
                    str(events[0]["recorded_at"]),
                    str(genesis["created_at"]),
                    session_id,
                    pointer,
                )
            )
        if not candidates:
            raise AutonomySessionError(
                "autonomy_latest_session_missing", "no durable autonomy session exists"
            )
        latest = max(candidates, key=lambda item: item[:3])[3]
        atomic_replace_json(sessions_root / "latest.json", latest)
        return latest

    def _append_event(
        self,
        session_id: str,
        event_type: str,
        *,
        reason: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        directory = self._session_dir(session_id)
        lock_path = directory / ".event-append"
        with state_lock(self.root, lock_path):
            genesis = self._load_genesis(session_id)
            events = self._load_events(session_id, allow_empty=True)
            _assert_transition(events, str(event_type))
            event = build_session_event(
                genesis=genesis,
                sequence=len(events) + 1,
                event_type=event_type,
                previous_event_hash=events[-1]["event_hash"] if events else None,
                reason=reason,
                recorded_at=recorded_at,
            )
            atomic_append_json(
                directory
                / "events"
                / f"{event['sequence']:06d}-{event['event_hash'][:20]}.json",
                event,
            )
            return event

    def _load_events(
        self, session_id: str, *, allow_empty: bool = False
    ) -> list[dict[str, Any]]:
        genesis = self._load_genesis(session_id)
        paths = sorted(
            (self._session_dir(session_id) / "events").glob(
                "[0-9][0-9][0-9][0-9][0-9][0-9]-*.json"
            )
        )
        events: list[dict[str, Any]] = []
        previous: str | None = None
        for sequence, path in enumerate(paths, start=1):
            if not path.name.startswith(f"{sequence:06d}-"):
                raise AutonomySessionError(
                    "autonomy_session_event_sequence_broken", "session event sequence skipped"
                )
            event = validate_session_event(load_json_object(path))
            if event["sequence"] != sequence or event["previous_event_hash"] != previous:
                raise AutonomySessionError(
                    "autonomy_session_event_chain_broken", "session event chain was modified"
                )
            for event_field, genesis_field in (
                ("session_id", "session_id"),
                ("book_id", "book_id"),
                ("plan_id", "plan_id"),
                ("plan_hash", "plan_hash"),
                ("genesis_hash", "genesis_hash"),
            ):
                if event[event_field] != genesis[genesis_field]:
                    raise AutonomySessionError(
                        "autonomy_session_event_scope_mismatch",
                        f"session event changed {event_field}",
                    )
            _assert_transition(events, event["event_type"])
            events.append(event)
            previous = event["event_hash"]
        if not events and not allow_empty:
            raise AutonomySessionError(
                "autonomy_session_event_missing", "session has no started event"
            )
        return events

    def _load_genesis(self, session_id: str) -> dict[str, Any]:
        return validate_session_genesis(
            load_json_object(self._session_dir(session_id) / "genesis.json")
        )

    def _load_plan(self, session_id: str) -> dict[str, Any]:
        return validate_instruction_plan(
            load_json_object(self._session_dir(session_id) / "instruction_plan.json")
        )

    def _require_trusted_profiles(self) -> TrustedProfiles:
        if self.trusted_profiles is None:
            raise AutonomySessionError(
                "trusted_profiles_required",
                "starting or resuming a session requires current TrustedProfiles",
            )
        return self.trusted_profiles

    def _session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / safe_id("session_id", session_id)

    def _plan_index_path(self, plan_hash: str) -> Path:
        digest = sha256_digest("plan_hash", plan_hash)
        return self.root / "plan_sessions" / f"{digest[:20]}.json"


def _state_from_events(events: list[Mapping[str, Any]]) -> str:
    event_type = str(events[-1]["event_type"])
    return {
        "started": "active",
        "resumed": "active",
        "cancelled": "cancelled",
        "abandoned": "abandoned",
        "completed": "completed",
    }[event_type]


def _assert_transition(events: list[Mapping[str, Any]], event_type: str) -> None:
    if not events:
        if event_type != "started":
            raise AutonomySessionError(
                "autonomy_session_transition_invalid", "first event must be started"
            )
        return
    state = _state_from_events(events)
    allowed = {
        "active": {"cancelled", "abandoned", "completed"},
        "cancelled": {"resumed", "abandoned"},
        "abandoned": set(),
        "completed": set(),
    }
    if event_type not in allowed[state]:
        raise AutonomySessionError(
            "autonomy_session_transition_invalid",
            f"cannot apply {event_type} while session is {state}",
        )


def _validate_source_snapshot_loader(
    loader: Callable[[], Mapping[str, Any]],
) -> Callable[[], Mapping[str, Any]]:
    if not callable(loader):
        raise AutonomySessionError(
            "autonomy_source_snapshot_loader_required",
            "a callable StoryProject source snapshot loader is required",
        )
    return loader


__all__ = [
    "AutonomySessionError",
    "AutonomySessionStore",
    "build_session_event",
    "build_session_genesis",
    "validate_session_event",
    "validate_session_genesis",
]
