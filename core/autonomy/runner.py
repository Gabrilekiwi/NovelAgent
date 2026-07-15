from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from core.autonomy.arc import ArcPlanError
from core.autonomy.common import (
    AutonomyContractError,
    canonical_hash,
    load_json_object,
    positive_int,
    safe_id,
    sha256_digest,
)
from core.autonomy.outline import (
    OutlineCheckpointStore,
    build_outline_checkpoint,
    render_arc_outline,
)
from core.autonomy.plans import validate_instruction_plan, validate_source_snapshot
from core.autonomy.runtime import AutonomyChapterRuntime, record_outline_stage
from core.autonomy.session import AutonomySessionStore
from core.delivery import DeliveryQueue, SafeFileDeliveryAdapter, load_delivery_job
from core.engine.safe_paths import RootBinding, assert_safe_local_tree
from core.engine.delivery_intent_recovery import recover_delivery_jobs_for_receipt
from core.engine.persistence_v2 import (
    validate_publication_receipt,
    verify_publication_receipt,
)
from core.path_refs import resolve_path_ref
from core.stage_control import derive_outline_readiness
from core.story_project.paths import canonical_outline_path


class AutonomyRunnerError(AutonomyContractError):
    pass


@dataclass(frozen=True)
class AutonomyExecutionRequest:
    session_id: str
    chapter_index: int
    source_snapshot: dict[str, Any]
    outline_checkpoint: dict[str, Any]
    runtime_context: AutonomyChapterRuntime
    provider_profile: dict[str, Any]
    budget_profile: dict[str, Any]
    quality_profile: dict[str, Any]
    file_delivery_profile: dict[str, Any]


class ChapterExecutor(Protocol):
    def run_once(self, *, persist: bool = True) -> dict[str, Any]: ...


ExecutorFactory = Callable[[AutonomyExecutionRequest], ChapterExecutor]
SourceSnapshotLoader = Callable[[], Mapping[str, Any]]


class AutonomyRunner:
    """Receipt-driven multi-chapter execution over AgentExecutor.

    The runner owns orchestration only.  AgentExecutor remains the sole
    generation/persistence implementation, while this class supplies durable
    lease, outline, stage, completion, delivery, and resume boundaries.
    """

    def __init__(
        self,
        *,
        sessions: AutonomySessionStore,
        source_snapshot_loader: SourceSnapshotLoader,
        executor_factory: ExecutorFactory,
        story_project_root: str | Path,
        run_dir: str | Path,
        persistence_dir: str | Path,
        publication_root_map: Mapping[str, str | Path],
        delivery_queue: DeliveryQueue,
        operator_delivery_roots: Mapping[str, str | Path],
        lease_ttl_seconds: int = 300,
        deterministic_stages: Sequence[str] = (),
        delivery_worker_id: str = "autonomy-runner",
    ) -> None:
        self.sessions = sessions
        self.source_snapshot_loader = source_snapshot_loader
        self.executor_factory = executor_factory
        self.story_project_root = Path(story_project_root).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.persistence_dir = Path(persistence_dir).resolve()
        self.publication_root_map = {
            str(key): Path(value).resolve() for key, value in publication_root_map.items()
        }
        self.delivery_queue = delivery_queue
        self.operator_delivery_roots: dict[str, Path] = {}
        for raw_uuid, raw_path in operator_delivery_roots.items():
            root_uuid = str(raw_uuid)
            try:
                parsed_uuid = uuid.UUID(root_uuid)
            except ValueError as exc:
                raise AutonomyRunnerError(
                    "autonomy_operator_root_invalid",
                    "operator delivery roots must be keyed by canonical root UUID",
                ) from exc
            root_path = Path(raw_path).absolute()
            if str(parsed_uuid) != root_uuid:
                raise AutonomyRunnerError(
                    "autonomy_operator_root_invalid",
                    "operator delivery root binding must name an existing regular directory",
                )
            try:
                self.operator_delivery_roots[root_uuid] = assert_safe_local_tree(
                    root_path
                )
            except RuntimeError as exc:
                raise AutonomyRunnerError(
                    "autonomy_operator_root_invalid",
                    "operator delivery root contains a link, junction, or unsafe component",
                ) from exc
        self.delivery_adapter: SafeFileDeliveryAdapter | None = None
        self._file_delivery_runtime_profile: dict[str, Any] | None = None
        self.lease_ttl_seconds = positive_int(
            "lease_ttl_seconds", lease_ttl_seconds
        )
        self.deterministic_stages = tuple(str(item) for item in deterministic_stages)
        self.delivery_worker_id = safe_id(
            "delivery_worker_id", delivery_worker_id
        )
        self.outlines = OutlineCheckpointStore(self.sessions.root)
        self._active_plan: dict[str, Any] | None = None
        verifier = self._delivery_resolved_for_completion
        existing_verifier = self.sessions.delivery_resolution_verifier
        if existing_verifier is not None and existing_verifier != verifier:
            raise AutonomyRunnerError(
                "autonomy_delivery_verifier_conflict",
                "session store is bound to a different delivery resolution authority",
            )
        self.sessions.delivery_resolution_verifier = verifier

    def execute_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_instruction_plan(
            plan, trusted_profiles=self.sessions.trusted_profiles
        )
        self._bind_plan_runtime(validated)
        self._active_plan = validated
        status = self.sessions.execute_plan(
            validated,
            source_snapshot_loader=self.source_snapshot_loader,
            lease_ttl_seconds=self.lease_ttl_seconds,
            recover_committed_boundary=self._recover_committed_boundary,
        )
        return self._drive(status["session_id"])

    def resume(self, session_id: str | None) -> dict[str, Any]:
        resolved = self.sessions.resolve_session_id(session_id)
        self._active_plan = self.sessions.load_instruction_plan(resolved)
        self._bind_plan_runtime(self._active_plan)
        status = self.sessions.resume(
            resolved,
            source_snapshot_loader=self.source_snapshot_loader,
            lease_ttl_seconds=self.lease_ttl_seconds,
            recover_committed_boundary=self._recover_committed_boundary,
        )
        return self._drive(status["session_id"])

    def _drive(self, session_id: str) -> dict[str, Any]:
        plan = self.sessions.load_instruction_plan(session_id)
        self._active_plan = plan
        self._bind_plan_runtime(plan)
        recovered = self._recover_committed_boundary(session_id)
        self._reconcile_arc_fulfillment(session_id)
        self._retry_blocked_deliveries(session_id)
        runs: list[dict[str, Any]] = []
        while True:
            status = self.sessions.status(session_id)
            if status["state"] != "active":
                return {
                    "schema_version": "1.0",
                    "session": status,
                    "runs": runs,
                    "recovered_chapters": recovered,
                    "stopped_reason": status["state"],
                }
            if status["delivery_blocked"]:
                return {
                    "schema_version": "1.0",
                    "session": status,
                    "runs": runs,
                    "recovered_chapters": recovered,
                    "stopped_reason": "required_delivery_blocked",
                }
            if status["completed_count"] == status["requested_chapter_count"]:
                completed = self.sessions.complete(session_id)
                return {
                    "schema_version": "1.0",
                    "session": completed,
                    "runs": runs,
                    "recovered_chapters": recovered,
                    "stopped_reason": "completed",
                }
            chapter = int(status["canonical_next_chapter"])
            expected_source = self.sessions.completion_ledger(
                session_id
            ).expected_source_snapshot()
            actual_source = validate_source_snapshot(self.source_snapshot_loader())
            if actual_source["snapshot_hash"] != expected_source["snapshot_hash"]:
                raise AutonomyRunnerError(
                    "autonomy_runner_source_drift",
                    "StoryProject changed outside the verified completion chain",
                )
            renewed = self.sessions.leases.renew(
                book_id=status["book_id"],
                session_id=session_id,
                plan_id=status["plan_id"],
                expected_lease_hash=status["lease_hash"],
                ttl_seconds=self.lease_ttl_seconds,
            )
            checkpoint, outline_readiness = self._prepare_outline(
                session_id=session_id,
                plan=plan,
                chapter_index=chapter,
                source_snapshot=expected_source,
            )
            _, outline_receipt, lease_hash = record_outline_stage(
                sessions=self.sessions,
                session_id=session_id,
                plan=plan,
                checkpoint=checkpoint,
                outline_readiness=outline_readiness,
                lease_hash=renewed["lease_hash"],
                lease_ttl_seconds=self.lease_ttl_seconds,
            )
            runtime_context = AutonomyChapterRuntime(
                sessions=self.sessions,
                session_id=session_id,
                plan=plan,
                arc_plan_id=status["arc_plan_id"],
                planned_target_hash=checkpoint["planned_target_hash"],
                source_snapshot=expected_source,
                checkpoint=checkpoint,
                outline_readiness=outline_readiness,
                outline_stage_receipt=outline_receipt,
                lease_hash=lease_hash,
                lease_ttl_seconds=self.lease_ttl_seconds,
                deterministic_stages=self.deterministic_stages,
                run_dir=self.run_dir,
                session_started_at=self.sessions.session_started_at(session_id),
            )
            request = AutonomyExecutionRequest(
                session_id=session_id,
                chapter_index=chapter,
                source_snapshot=copy.deepcopy(expected_source),
                outline_checkpoint=copy.deepcopy(checkpoint),
                runtime_context=runtime_context,
                provider_profile=copy.deepcopy(plan["selections"]["provider_model"]),
                budget_profile=copy.deepcopy(plan["selections"]["budget"]),
                quality_profile=copy.deepcopy(plan["selections"]["quality_policy"]),
                file_delivery_profile=copy.deepcopy(
                    self._require_file_delivery_runtime_profile()
                ),
            )
            executor = self.executor_factory(request)
            if getattr(executor, "autonomy_run_context", None) is not runtime_context:
                raise AutonomyRunnerError(
                    "autonomy_executor_context_unbound",
                    "executor factory did not bind the trusted autonomy lifecycle context",
                )
            result = executor.run_once(persist=True)
            if not result.get("committed") or not result.get("accepted"):
                raise AutonomyRunnerError(
                    "autonomy_chapter_not_committed",
                    "AgentExecutor did not produce an accepted durable chapter",
                )
            receipt = self._publication_for_result(result)
            evidence = self._require_autonomy_evidence(
                receipt,
                session_id=session_id,
                plan_id=plan["plan_id"],
                chapter_index=chapter,
            )
            delivery_ok = self._attempt_receipt_deliveries(receipt)
            after = validate_source_snapshot(self.source_snapshot_loader())
            if int(after["canonical_next_chapter"]) != chapter + 1:
                raise AutonomyRunnerError(
                    "autonomy_postcommit_source_invalid",
                    "StoryProject did not advance exactly one canonical chapter",
                )
            completion = self.sessions.completion_ledger(session_id).append(
                final_stage_receipt=runtime_context.final_stage_receipt,
                publication_receipt=receipt,
                planned_target_hash=checkpoint["planned_target_hash"],
                chapter_body_hash=evidence["chapter_body_sha256"],
                source_snapshot_after=after,
                status=(
                    "committed"
                    if delivery_ok
                    else "local_committed_delivery_blocked"
                ),
            )
            self._record_arc_fulfillment(session_id, completion)
            runs.append(
                {
                    "run_id": result["run"]["id"],
                    "chapter_index": chapter,
                    "publication_receipt_hash": receipt["receipt_hash"],
                    "completion_receipt_hash": completion["receipt_hash"],
                    "delivery_succeeded": delivery_ok,
                }
            )
            if not delivery_ok:
                return {
                    "schema_version": "1.0",
                    "session": self.sessions.status(session_id),
                    "runs": runs,
                    "recovered_chapters": recovered,
                    "stopped_reason": "required_delivery_blocked",
                }

    def _prepare_outline(
        self,
        *,
        session_id: str,
        plan: Mapping[str, Any],
        chapter_index: int,
        source_snapshot: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        status = self.sessions.status(session_id)
        self.sessions.assert_outline_provider_allowed(session_id)
        arc = self.sessions.arc_plans.load(status["arc_plan_id"])
        target = next(
            (
                item
                for item in arc["targets"]
                if int(item["chapter_index"]) == int(chapter_index)
            ),
            None,
        )
        if not isinstance(target, dict) or target.get("fulfilled") is not None:
            raise AutonomyRunnerError(
                "autonomy_arc_target_invalid",
                "canonical next chapter has no unfulfilled RunArc target",
            )
        canonical_path = canonical_outline_path(
            self.story_project_root, chapter_index
        )
        canonical_before = (
            hashlib.sha256(canonical_path.read_bytes()).hexdigest()
            if canonical_path.is_file()
            else None
        )
        provider = plan["selections"]["provider_model"]
        outline_input_digest = canonical_hash(
            {
                "planned_target_hash": target["target_hash"],
                "source_snapshot_hash": source_snapshot["snapshot_hash"],
                "authority_epoch": source_snapshot["authority_epoch"],
                "authority_head_event_hash": source_snapshot[
                    "authority_head_event_hash"
                ],
                "provider_profile_hash": provider["profile_hash"],
                "canonical_before_sha256": canonical_before,
            }
        )
        readiness = derive_outline_readiness(
            book_id=source_snapshot["book_id"],
            expected_book_id=plan["selections"]["story_project"]["book_id"],
            requested_chapter=chapter_index,
            canonical_next_chapter=status["canonical_next_chapter"],
            authority_epoch=int(source_snapshot["authority_epoch"]),
            authority_head_event_hash=source_snapshot[
                "authority_head_event_hash"
            ],
            context_digest=outline_input_digest,
            book_lease_held=bool(status["lease_held"]),
            required_delivery_allows_progress=not status["delivery_blocked"],
            sources_current=(
                source_snapshot["snapshot_hash"]
                == self.sessions.completion_ledger(
                    session_id
                ).expected_source_snapshot()["snapshot_hash"]
            ),
            outline_exists=canonical_before is not None,
        )
        if not readiness["ok"]:
            raise AutonomyRunnerError(
                "outline_readiness_rejected", ", ".join(readiness["reasons"])
            )
        existing = self.outlines.load(session_id, chapter_index)
        scope = (
            source_snapshot["snapshot_hash"],
            int(source_snapshot["authority_epoch"]),
            source_snapshot["authority_head_event_hash"],
            outline_input_digest,
            target["target_hash"],
            canonical_before,
        )
        if existing is not None:
            existing_scope = (
                existing["source_snapshot_hash"],
                int(existing["authority"]["epoch"]),
                existing["authority"]["head_event_hash"],
                existing["outline_input_digest"],
                existing["planned_target_hash"],
                existing["canonical_before_sha256"],
            )
            if existing_scope == scope:
                return existing, readiness
        if canonical_before is None:
            outline_text = render_arc_outline(chapter_index, target["planned"])
        else:
            outline_text = canonical_path.read_text(encoding="utf-8-sig")
        checkpoint = build_outline_checkpoint(
            book_id=source_snapshot["book_id"],
            session_id=session_id,
            plan_id=plan["plan_id"],
            arc_plan_id=arc["arc_plan_id"],
            chapter_index=chapter_index,
            planned_target_hash=target["target_hash"],
            source_snapshot_hash=source_snapshot["snapshot_hash"],
            authority_epoch=int(source_snapshot["authority_epoch"]),
            authority_head_event_hash=source_snapshot[
                "authority_head_event_hash"
            ],
            outline_input_digest=outline_input_digest,
            provider_profile=provider["profile_id"],
            execution_kind="deterministic",
            outline_text=outline_text,
            canonical_relative_path=canonical_path.relative_to(
                self.story_project_root
            ).as_posix(),
            canonical_before_sha256=canonical_before,
        )
        return self.outlines.create(checkpoint), readiness

    def _publication_for_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        run = result.get("run")
        pointer = run.get("publication_receipt") if isinstance(run, Mapping) else None
        if not isinstance(pointer, Mapping) or not isinstance(
            pointer.get("path_ref"), Mapping
        ):
            raise AutonomyRunnerError(
                "autonomy_publication_receipt_missing",
                "AgentExecutor result has no PublicationReceipt pointer",
            )
        path = resolve_path_ref(pointer["path_ref"], self.publication_root_map)
        verification = verify_publication_receipt(
            path, root_map=self.publication_root_map
        )
        if not verification.get("valid") or not verification.get("committed"):
            raise AutonomyRunnerError(
                "autonomy_publication_receipt_invalid",
                "AgentExecutor PublicationReceipt is not durably committed",
            )
        return validate_publication_receipt(load_json_object(path))

    def _require_autonomy_evidence(
        self,
        receipt: Mapping[str, Any],
        *,
        session_id: str,
        plan_id: str,
        chapter_index: int,
    ) -> dict[str, Any]:
        stage_bindings = [
            item
            for item in receipt["artifacts"]
            if item.get("kind") == "autonomy_stage_evidence"
        ]
        outline_bindings = [
            item
            for item in receipt["artifacts"]
            if item.get("kind") == "autonomy_outline_evidence"
        ]
        if len(stage_bindings) != 1 or len(outline_bindings) != 1:
            raise AutonomyRunnerError(
                "autonomy_publication_evidence_missing",
                "chapter transaction must publish one outline and one stage evidence artifact",
            )
        stage_path = resolve_path_ref(
            stage_bindings[0]["path_ref"], self.publication_root_map
        )
        outline_path = resolve_path_ref(
            outline_bindings[0]["path_ref"], self.publication_root_map
        )
        if hashlib.sha256(stage_path.read_bytes()).hexdigest() != stage_bindings[0][
            "sha256"
        ] or hashlib.sha256(outline_path.read_bytes()).hexdigest() != outline_bindings[0][
            "sha256"
        ]:
            raise AutonomyRunnerError(
                "autonomy_publication_evidence_invalid",
                "published autonomy artifact readback hash differs",
            )
        evidence = load_json_object(stage_path)
        expected_hash = canonical_hash(evidence, exclude_fields=("evidence_hash",))
        if evidence.get("evidence_hash") != expected_hash:
            raise AutonomyRunnerError(
                "autonomy_publication_evidence_invalid",
                "published stage evidence hash differs",
            )
        scope = (
            evidence.get("session_id"),
            evidence.get("plan_id"),
            evidence.get("chapter_index"),
        )
        if scope != (session_id, plan_id, chapter_index):
            raise AutonomyRunnerError(
                "autonomy_publication_evidence_scope_mismatch",
                "published stage evidence belongs to another chapter",
            )
        return evidence

    def _recover_committed_boundary(self, session_id: str) -> list[int]:
        if self._active_plan is None:
            self._active_plan = self.sessions.load_instruction_plan(session_id)
        plan = self._active_plan
        recovered: list[int] = []
        while True:
            ledger = self.sessions.completion_ledger(session_id)
            chapter = int(ledger.summary()["canonical_next_chapter"])
            if chapter > int(plan["chapter_end"]):
                break
            matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
            receipt_dir = self.run_dir / "publication_receipts"
            for path in sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []:
                verification = verify_publication_receipt(
                    path, root_map=self.publication_root_map
                )
                if not verification.get("valid") or not verification.get("committed"):
                    continue
                receipt = validate_publication_receipt(load_json_object(path))
                autonomy_kinds = {
                    str(item.get("kind"))
                    for item in receipt.get("artifacts", [])
                    if isinstance(item, Mapping)
                    and str(item.get("kind", "")).startswith("autonomy_")
                }
                if not autonomy_kinds:
                    continue
                try:
                    evidence = self._require_autonomy_evidence(
                        receipt,
                        session_id=session_id,
                        plan_id=plan["plan_id"],
                        chapter_index=chapter,
                    )
                except AutonomyRunnerError as exc:
                    if exc.code == "autonomy_publication_evidence_scope_mismatch":
                        continue
                    raise
                matches.append((receipt, evidence))
            if not matches:
                break
            if len(matches) != 1:
                raise AutonomyRunnerError(
                    "autonomy_publication_boundary_ambiguous",
                    "multiple committed publications claim the same session chapter",
                )
            receipt, evidence = matches[0]
            self._attempt_receipt_deliveries(receipt)
            after = validate_source_snapshot(self.source_snapshot_loader())
            if int(after["canonical_next_chapter"]) != chapter + 1:
                raise AutonomyRunnerError(
                    "autonomy_postcommit_source_invalid",
                    "committed publication does not match current canonical next chapter",
                )
            final_stage = evidence["stage_receipts"][-1]
            delivery_ok = self._delivery_resolved_for_publication(
                receipt["receipt_hash"]
            )
            completion = ledger.append(
                final_stage_receipt=final_stage,
                publication_receipt=receipt,
                planned_target_hash=evidence["planned_target_hash"],
                chapter_body_hash=evidence["chapter_body_sha256"],
                source_snapshot_after=after,
                status=(
                    "committed"
                    if delivery_ok
                    else "local_committed_delivery_blocked"
                ),
            )
            self._record_arc_fulfillment(session_id, completion)
            recovered.append(chapter)
            if not delivery_ok:
                break
        return recovered

    def _attempt_receipt_deliveries(self, receipt: Mapping[str, Any]) -> bool:
        recover_delivery_jobs_for_receipt(
            dict(receipt),
            root_map=self.publication_root_map,
            queue=self.delivery_queue,
        )
        for binding in receipt["delivery_jobs"]:
            policy = binding.get("policy") if isinstance(binding, Mapping) else None
            if not isinstance(policy, Mapping) or not policy.get("required"):
                continue
            if policy.get("target") != "file":
                raise AutonomyRunnerError(
                    "autonomy_delivery_target_forbidden",
                    "autonomy permits required local File Delivery only",
                )
            job = self.delivery_queue.load(str(binding["id"]))
            if job["state"] != "succeeded":
                if self.delivery_adapter is None:
                    raise AutonomyRunnerError(
                        "autonomy_operator_root_unbound",
                        "required File Delivery has no operator-owned physical root binding",
                    )
                self.delivery_queue.attempt(
                    job["job_id"],
                    worker_id=self.delivery_worker_id,
                    adapter=self.delivery_adapter,
                )
        return self._delivery_resolved_for_publication(receipt["receipt_hash"])

    def _delivery_resolved_for_completion(
        self, completion: Mapping[str, Any]
    ) -> bool:
        return self._delivery_resolved_for_publication(
            str(completion["publication_receipt_hash"])
        )

    def _delivery_resolved_for_publication(self, receipt_hash: str) -> bool:
        digest = sha256_digest("publication_receipt_hash", receipt_hash)
        jobs = []
        if self.delivery_queue.jobs_dir.is_dir():
            for path in sorted(self.delivery_queue.jobs_dir.glob("*.json")):
                job = load_delivery_job(path)
                if job["publication_receipt_hash"] == digest and job["policy"] == "required":
                    jobs.append(job)
        return bool(jobs) and all(job["state"] == "succeeded" for job in jobs)

    def _retry_blocked_deliveries(self, session_id: str) -> None:
        ledger = self.sessions.completion_ledger(session_id)
        for completion in ledger.rebuild():
            if completion["status"] != "local_committed_delivery_blocked":
                continue
            receipt = ledger.load_publication(
                completion["publication_receipt_hash"]
            )
            self._attempt_receipt_deliveries(receipt)

    def _bind_plan_runtime(self, plan: Mapping[str, Any]) -> None:
        profiles = self.sessions.trusted_profiles
        if profiles is None:
            raise AutonomyRunnerError(
                "autonomy_trusted_profiles_missing",
                "execution requires the trusted profile source, not only its public snapshot",
            )
        selection = plan["selections"]["file_delivery"]
        runtime_profile = profiles.file_delivery_runtime_profile(
            str(selection["profile_id"]),
            book_id=str(plan["source_snapshot"]["book_id"]),
        )
        root_uuid = str(runtime_profile["root_uuid"])
        root_path = self.operator_delivery_roots.get(root_uuid)
        if root_path is None:
            raise AutonomyRunnerError(
                "autonomy_operator_root_unbound",
                "operator root map has no physical directory bound to the trusted File Delivery root_uuid",
            )
        protected_roots = (
            self.story_project_root,
            self.run_dir,
            self.persistence_dir,
            self.delivery_queue.root,
            self.sessions.root,
        )
        if any(_paths_overlap(root_path, protected) for protected in protected_roots):
            raise AutonomyRunnerError(
                "autonomy_operator_root_overlaps_runtime",
                "external File Delivery root must not contain or be contained by StoryProject/runtime storage",
            )
        self._file_delivery_runtime_profile = runtime_profile
        self.delivery_adapter = SafeFileDeliveryAdapter(
            binding=RootBinding(
                root_id=str(runtime_profile["root_id"]),
                root_uuid=root_uuid,
                path=root_path,
            )
        )

    def _require_file_delivery_runtime_profile(self) -> dict[str, Any]:
        if self._file_delivery_runtime_profile is None:
            raise AutonomyRunnerError(
                "autonomy_delivery_profile_unbound",
                "trusted File Delivery runtime profile has not been resolved",
            )
        return self._file_delivery_runtime_profile

    def _record_arc_fulfillment(
        self, session_id: str, completion: Mapping[str, Any]
    ) -> None:
        status = self.sessions.status(session_id)
        arc = self.sessions.arc_plans.load(status["arc_plan_id"])
        target = next(
            item
            for item in arc["targets"]
            if int(item["chapter_index"]) == int(completion["chapter_index"])
        )
        self.sessions.arc_plans.record_fulfillment(
            arc["arc_plan_id"],
            chapter_index=int(completion["chapter_index"]),
            fulfilled=copy.deepcopy(target["planned"]),
            completion_receipt_hash=completion["receipt_hash"],
            expected_arc_plan_hash=arc["arc_plan_hash"],
        )

    def _reconcile_arc_fulfillment(self, session_id: str) -> None:
        for completion in self.sessions.completion_ledger(session_id).rebuild():
            try:
                self._record_arc_fulfillment(session_id, completion)
            except ArcPlanError as exc:
                if exc.code != "arc_target_already_committed":
                    raise


def bind_delivery_resolution_verifier(
    runner: AutonomyRunner,
) -> Callable[[Mapping[str, Any]], bool]:
    return runner._delivery_resolved_for_completion


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_text = os.path.normcase(str(left.absolute()))
        right_text = os.path.normcase(str(right.absolute()))
        common = os.path.commonpath((left_text, right_text))
    except ValueError:
        return False
    return common in {left_text, right_text}


__all__ = [
    "AutonomyExecutionRequest",
    "AutonomyRunner",
    "AutonomyRunnerError",
    "ExecutorFactory",
    "bind_delivery_resolution_verifier",
]
