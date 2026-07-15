from __future__ import annotations

import hashlib
import json
import inspect
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from api.contracts import ModelCallError, ModelOutputError
from api.retry import consume_retry_telemetry, reset_retry_telemetry
from core.chapter_contexts import ChapterContextError, resolve_committed_previous_chapter_artifact
from core.config import get_config
from core.context_budget import ContextBudgetError, RunBudgetLimits, RunBudgetTracker
from core.execution_provenance import ExecutionProvenance, capture_execution_provenance
from core.model_call_runtime import (
    ModelCallRuntimeContext,
    ProviderCallUncertainError,
    use_model_call_runtime,
)
from core.model_calls import ModelCallStore, load_model_call_receipt
from core.path_refs import path_ref_for
from core.delivery import DeliveryQueue
from core.delivery_intents import (
    build_file_delivery_intent,
    delivery_intent_receipt_binding,
    validate_file_delivery_profile,
)
from core.director import decide_next_step, validate_decision
from core.project_profile import project_language
from core.quality_decision import (
    QualityPolicy,
    resolve_quality_policy,
)
from core.review.index import build_review_index_entry, review_index_path, update_review_index
from core.review.repair_loop import ReviewRepairConfig, run_review_repair_loop, validate_review_repair_config
from core.review.runtime import RuntimeReviewConfig, run_runtime_review, validate_runtime_review_config
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH, RuntimePaths
from core.memory_v2 import (
    canonical_memory_to_snapshot,
    ensure_memory_v2_storage_layout,
    prepare_chapter_memory_commit,
    prepare_event_authority_chapter_commit,
)
from core.schema import validate_schema
from core.engine.artifacts import (
    chapter_artifact_metadata,
    prepare_chapter_artifact,
    prepare_chapter_pipeline_artifacts,
    prepare_input_pack_artifact,
    prepare_review_repair_artifacts,
    prepare_snapshot_pack_artifact,
    prepare_story_project_writeback_artifacts,
    save_chapter_artifact,
    save_chapter_pipeline_artifacts,
    save_input_pack_artifact,
    save_loop_session_artifact,
    save_review_repair_artifacts,
    save_snapshot_pack_artifact,
    save_story_project_writeback_artifacts,
)
from core.engine.persistence import (
    LocalPersistenceTransaction,
    PersistenceError,
    PersistencePreparationError,
    PersistenceTarget,
    atomic_create_json,
    atomic_write_json,
    persistence_run_lock,
)
from core.engine.persistence_coordinator import PersistenceCoordinator
from core.engine.persistence_v2 import (
    PersistenceV2Target,
    bind_final_run_record_receipt,
    verify_publication_receipt,
)
from core.engine.delivery_intent_recovery import (
    DELIVERY_INTENT_ARTIFACT_KIND,
    recover_completed_delivery_jobs,
    recover_delivery_jobs_for_receipt,
)
from core.engine.root_registry import RootRegistryService
from core.engine.quality_coordinator import QualityCoordinator
from core.engine.story_project_context import (
    StoryProjectContextError,
    StoryProjectContextLoader,
    StoryProjectContextService,
)
from core.engine.run_record import (
    build_run_id,
    build_loop_session_record,
    build_director_failed_run_record,
    build_failed_run_record,
    build_run_record,
    build_workflow_failed_run_record,
    load_latest_run_summary,
    utc_now,
    validate_run_result,
)
from core.state.builder import build_snapshot_state_with_audit
from core.state.input_pack import (
    build_input_pack,
    build_input_pack_metadata,
    build_recovery_context,
    build_snapshot_input_pack,
)
from core.state.memory import load_memory_context
from core.state.memory_updates import build_memory_updates
from core.state.memory_writer import MemoryWriter, validate_memory_writeback_result, write_memory_updates
from core.state.snapshot import build_state_update_audit, load_snapshot, normalize_snapshot, update_snapshot
from core.story_project.coverage import build_blueprint_coverage
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.writer import (
    StoryProjectWritebackConfig,
    default_story_project_writeback,
    finalize_story_project_writeback,
    prepare_story_project_writeback,
)
from core.story_project.authority import prepare_event_authority_advance
from core.story_project.authority_persistence import (
    EventAuthorityWriteOperation,
    event_authority_write_operation,
)
from core.story_project.identity import project_identity_path, validate_project_identity
from core.story_project.read_set import declared_read_set_writes
from core.validator import validate_chapter
from core.validator.spatial import validate_bridge_preconditions
from modules.chapter_generator import PIPELINE_STAGE_NAMES, generate_chapter, run_chapter_pipeline
from modules.claude_polish import polish_chapter
from modules.conflict_engine import analyze_chapter
from modules.scene_repair import RepairContext, repair_scene
from modules.scene_repair import build_repair_plan
from workflows.dynamic_flow import build_dynamic_flow_plan


ChapterGenerator = Callable[[str], str]
ChapterPolisher = Callable[[str], str]
ChapterRepairer = Callable[..., str]
ChapterAnalyzer = Callable[[str, dict[str, Any]], dict[str, Any]]
ChapterValidator = Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any]]
Director = Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]]
MemoryLoader = Callable[[], dict[str, Any]]
LoopObserver = Callable[[dict[str, Any]], None]

_PROVENANCE_CACHE: dict[tuple[Any, ...], ExecutionProvenance] = {}
_PROVENANCE_CACHE_LOCK = threading.Lock()


class AgentExecutor:
    def __init__(
        self,
        *,
        snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
        memory_path: str | Path | None = None,
        memory_source: str = "auto",
        run_dir: str | Path = DEFAULT_RUN_DIR,
        chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
        persistence_dir: str | Path | None = None,
        dry_run: bool = False,
        enable_llm_validator: bool = False,
        scene_limit: int | None = None,
        use_run_history: bool = True,
        memory_loader: MemoryLoader | None = None,
        director: Director | None = None,
        generator: ChapterGenerator | None = None,
        polisher: ChapterPolisher | None = None,
        validator: ChapterValidator | None = None,
        repairer: ChapterRepairer | None = None,
        analyzer: ChapterAnalyzer | None = None,
        memory_writer: MemoryWriter | None = None,
        review_config: RuntimeReviewConfig | None = None,
        review_repair_config: ReviewRepairConfig | None = None,
        story_project_context: Any = None,
        story_project_context_loader: StoryProjectContextLoader | None = None,
        story_project_oh_story_report: dict[str, Any] | None = None,
        story_project_writeback: StoryProjectWritebackConfig | None = None,
        quality_policy: str | QualityPolicy | None = None,
        repository_root: str | Path | None = None,
        enable_execution_provenance: bool = True,
        run_budget_limits: RunBudgetLimits | None = None,
        file_delivery_profile: Mapping[str, Any] | None = None,
        delivery_queue: DeliveryQueue | None = None,
        autonomy_run_context: Any | None = None,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.memory_path = Path(memory_path) if memory_path else None
        self.memory_source = memory_source
        self.run_dir = Path(run_dir)
        self.chapter_dir = Path(chapter_dir)
        self.persistence_dir = Path(persistence_dir) if persistence_dir is not None else self.run_dir / "transactions"
        self.dry_run = dry_run
        self.enable_llm_validator = enable_llm_validator
        self.scene_limit = scene_limit
        self.use_run_history = use_run_history
        self.memory_loader = memory_loader
        self.director = director or decide_next_step
        self.generator = generator
        self.polisher = polisher
        self.validator = validator or validate_chapter
        self.repairer = repairer
        self.analyzer = analyzer or analyze_chapter
        self.memory_writer = memory_writer
        self.review_config = validate_runtime_review_config(review_config or RuntimeReviewConfig())
        self.review_repair_config = validate_review_repair_config(review_repair_config or ReviewRepairConfig())
        if story_project_context is not None and story_project_context_loader is not None:
            raise ValueError("story_project_context and story_project_context_loader are mutually exclusive")
        self.story_project_context = story_project_context
        self.story_project_context_loader = story_project_context_loader
        self._active_story_project_context: Any = None
        self._last_project_identity: dict[str, Any] | None = None
        self._expected_book_id: str | None = None
        self._allow_legacy_snapshot_adoption = False
        self.story_project_oh_story_report = story_project_oh_story_report
        self.story_project_writeback = story_project_writeback or StoryProjectWritebackConfig()
        self.quality_policy = resolve_quality_policy(quality_policy) if quality_policy is not None else None
        self.repository_root = (
            Path(repository_root)
            if repository_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.enable_execution_provenance = bool(enable_execution_provenance)
        self.run_budget_limits = run_budget_limits or RunBudgetLimits()
        if (file_delivery_profile is None) != (delivery_queue is None):
            raise ValueError(
                "file_delivery_profile and delivery_queue must be configured together"
            )
        self.file_delivery_profile = (
            validate_file_delivery_profile(dict(file_delivery_profile))
            if file_delivery_profile is not None
            else None
        )
        if (
            self.file_delivery_profile is not None
            and self.file_delivery_profile.get("root_uuid") is None
        ):
            raise ValueError(
                "event-authority file delivery requires a trusted external root_uuid"
            )
        self.delivery_queue = delivery_queue
        self.autonomy_run_context = autonomy_run_context
        if self.autonomy_run_context is not None:
            self.autonomy_run_context.assert_executor_budget_limits(
                self.run_budget_limits
            )
        self._execution_scope_depth = 0
        self._execution_evidence: dict[str, Any] | None = None
        self._run_budget_tracker: RunBudgetTracker | None = None
        self._model_call_runtime: ModelCallRuntimeContext | None = None
        self.story_project_context_service = StoryProjectContextService()
        self.quality_coordinator = QualityCoordinator()
        self.persistence_coordinator = PersistenceCoordinator(
            run_dir=self.run_dir,
            persistence_dir=self.persistence_dir,
        )
        self._event_authority_root_map: dict[str, Path] | None = None
        self._event_authority_operation: EventAuthorityWriteOperation | None = None

    def run_once(self, *, persist: bool = True) -> dict[str, Any]:
        with self._execution_scope(persist=persist):
            return self._invoke_once(persist=persist)

    @contextmanager
    def _execution_scope(self, *, persist: bool):
        if self._execution_scope_depth:
            self._execution_scope_depth += 1
            try:
                yield
            finally:
                self._execution_scope_depth -= 1
            return

        self._execution_scope_depth = 1
        previous_evidence = self._execution_evidence
        previous_tracker = self._run_budget_tracker
        previous_runtime = self._model_call_runtime
        try:
            self._execution_evidence = self._begin_execution_evidence(persist=persist)
            self._run_budget_tracker = RunBudgetTracker(self.run_budget_limits)
            model_calls_ref = (
                self._execution_evidence.get("model_calls_ref")
                if isinstance(self._execution_evidence, dict)
                else None
            )
            self._model_call_runtime = (
                ModelCallRuntimeContext(
                    ModelCallStore(self.run_dir / Path(model_calls_ref)),
                    tracker=self._run_budget_tracker,
                )
                if isinstance(model_calls_ref, str) and model_calls_ref
                else None
            )
            if self.autonomy_run_context is not None:
                self.autonomy_run_context.hydrate_budget_tracker(
                    self._run_budget_tracker
                )
            if self._model_call_runtime is None:
                yield
            else:
                with use_model_call_runtime(self._model_call_runtime):
                    yield
        finally:
            self._execution_evidence = previous_evidence
            self._run_budget_tracker = previous_tracker
            self._model_call_runtime = previous_runtime
            self._execution_scope_depth = 0

    def _begin_execution_evidence(self, *, persist: bool) -> dict[str, Any] | None:
        if not self.enable_execution_provenance:
            return None
        persist_evidence = (
            persist
            or not self.dry_run
            or self.enable_llm_validator
            or _director_mode(self.director) == "model"
        )
        if persist_evidence:
            unresolved = _unresolved_provider_calls(self.run_dir)
            if unresolved:
                first = unresolved[0]
                raise ProviderCallUncertainError(
                    call_id=str(first["call_id"]),
                    attempt_id=str(first["attempt_id"]),
                )
        config = get_config()
        public_config = {
            "configured_models": {
                "openai": config.openai_model,
                "anthropic": config.claude_model,
            },
            "openai_max_output_tokens": config.openai_max_output_tokens,
            "claude_max_tokens": config.claude_max_tokens,
            "provider_max_attempts": config.provider_max_attempts,
            "provider_retry_deadline_seconds": config.provider_retry_deadline_seconds,
            "quality_policy": self.quality_policy.name if self.quality_policy is not None else "runtime_default",
        }
        feature_flags = {
            "dry_run": self.dry_run,
            "llm_validator": self.enable_llm_validator,
            "memory_v2": True,
            "review_gate": self.review_config.enabled,
            "story_project_writeback": self.story_project_writeback.enabled,
        }
        provenance = _capture_execution_provenance_cached(
            self.repository_root,
            provider="openai",
            model=config.openai_model,
            config=public_config,
            feature_flags=feature_flags,
        )
        payload = validate_schema(provenance.to_dict(), "execution_provenance.schema.json")
        if self.autonomy_run_context is not None:
            session_id = str(self.autonomy_run_context.session_id)
            chapter_index = int(self.autonomy_run_context.chapter_index)
            stable_identity = hashlib.sha256(
                f"{session_id}:{chapter_index}".encode("utf-8")
            ).hexdigest()[:40]
            execution_id = f"execution_autonomy_{stable_identity}"
        else:
            execution_id = f"execution_{uuid.uuid4().hex}"
        provenance_ref: str | None = None
        model_calls_ref: str | None = None
        if persist_evidence:
            provenance_ref = f"executions/{execution_id}/provenance.json"
            model_calls_ref = f"executions/{execution_id}/model_calls"
            provenance_path = self.run_dir / Path(provenance_ref)
            try:
                atomic_create_json(provenance_path, payload)
            except OSError as exc:
                if not provenance_path.is_file():
                    raise
                try:
                    existing = json.loads(provenance_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as read_exc:
                    raise PersistencePreparationError(
                        "stable autonomy execution provenance is unreadable"
                    ) from read_exc
                if existing != payload:
                    raise PersistencePreparationError(
                        "stable autonomy execution provenance changed across retry"
                    ) from exc
        return {
            "execution_id": execution_id,
            "provenance_hash": provenance.provenance_hash,
            "provenance_artifact_ref": provenance_ref,
            "model_calls_ref": model_calls_ref,
        }

    def _attach_execution_evidence(self, result: dict[str, Any]) -> None:
        if self._execution_evidence is None:
            return
        run = result.get("run") if isinstance(result, dict) else None
        if isinstance(run, dict):
            evidence = dict(self._execution_evidence)
            if self._run_budget_tracker is not None:
                evidence["budget"] = self._run_budget_tracker.report()
            run["execution_evidence"] = evidence

    def _invoke_once(
        self,
        *,
        persist: bool,
        snapshot_override: dict[str, Any] | None = None,
        previous_result: dict[str, Any] | None = None,
        chapter_hint: int | None = None,
    ) -> dict[str, Any]:
        self._active_story_project_context = None
        try:
            self.story_project_context_service.require_apply_persistence(
                enabled=self.story_project_context is not None or self.story_project_context_loader is not None,
                persist=persist,
                writeback_mode=self.story_project_writeback.mode,
            )
            if not persist:
                return self._run_once_impl(
                    persist=False,
                    snapshot_override=snapshot_override,
                    previous_result=previous_result,
                    chapter_hint=chapter_hint,
                )
            story_root = self._configured_story_project_root()
            if story_root is not None:
                # The global fence is acquired before ProjectIdentity is read.
                # Legacy identity creation is allowed only while the same fence
                # remains held, then the operation is bound to its new book id.
                with event_authority_write_operation(
                    story_root,
                    expected_book_id=None,
                    writer_kind="chapter",
                    allow_identity_missing=True,
                ) as authority_operation:
                    self._event_authority_operation = authority_operation
                    return self._invoke_persist_once(
                        snapshot_override=snapshot_override,
                        previous_result=previous_result,
                        chapter_hint=chapter_hint,
                    )
            return self._invoke_persist_once(
                snapshot_override=snapshot_override,
                previous_result=previous_result,
                chapter_hint=chapter_hint,
            )
        finally:
            self._active_story_project_context = None
            self._event_authority_operation = None

    def _invoke_persist_once(
        self,
        *,
        snapshot_override: dict[str, Any] | None,
        previous_result: dict[str, Any] | None,
        chapter_hint: int | None,
    ) -> dict[str, Any]:
        self._prepare_project_identity_for_persistence()
        if (
            self._event_authority_operation is not None
            and self._expected_book_id is not None
        ):
            self._event_authority_operation.bind_book_id(self._expected_book_id)
        self._configure_authority_persistence_backend()
        if self.persistence_coordinator.backend_id == "v2":
            self._assert_persistence_ready(expected_book_id=self._expected_book_id)
            return self._run_once_impl(
                persist=True,
                snapshot_override=snapshot_override,
                previous_result=previous_result,
                chapter_hint=chapter_hint,
            )
        with persistence_run_lock(
            self.run_dir, state_paths=self._persistence_state_paths()
        ):
            self._assert_persistence_ready(expected_book_id=self._expected_book_id)
            return self._run_once_impl(
                persist=True,
                snapshot_override=snapshot_override,
                previous_result=previous_result,
                chapter_hint=chapter_hint,
            )

    def _run_once_impl(
        self,
        *,
        persist: bool,
        snapshot_override: dict[str, Any] | None,
        previous_result: dict[str, Any] | None,
        chapter_hint: int | None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        snapshot_before = _capture_file_version(self.snapshot_path)
        base_snapshot = (
            normalize_snapshot(snapshot_override)
            if snapshot_override is not None
            else load_snapshot(self.snapshot_path)
        )
        reset_retry_telemetry()
        memory_context = self._load_memory_context()
        memory_provider_attempts = consume_retry_telemetry()
        if self.use_run_history:
            self._attach_last_run(memory_context, previous_result=previous_result)
        self._active_story_project_context = self._load_story_project_context(
            base_snapshot,
            memory_context,
            chapter_hint=chapter_hint,
        )
        self._active_story_project_context = self._normalize_story_project_identity(
            self._active_story_project_context,
            persist=persist,
        )
        snapshot_after_context = _capture_file_version(self.snapshot_path)
        if snapshot_after_context != snapshot_before:
            context = self._story_project_context_dict() or {}
            memory_v2 = context.get("memory_v2")
            cache_status = (
                memory_v2.get("cache_status")
                if isinstance(memory_v2, dict)
                else None
            )
            projection = (
                memory_v2.get("projection")
                if isinstance(memory_v2, dict)
                else None
            )
            if cache_status != "rebuilt" or not isinstance(projection, dict):
                raise PersistencePreparationError(
                    "runtime snapshot changed while StoryProject context was loading"
                )
            recovered_snapshot = load_snapshot(self.snapshot_path)
            if normalize_snapshot(recovered_snapshot) != normalize_snapshot(
                canonical_memory_to_snapshot(projection)
            ):
                raise PersistencePreparationError(
                    "recovered runtime snapshot differs from immutable event authority"
                )
            # The context loader repaired a disposable cache from already
            # pinned immutable authority.  Adopt that repair as the new CAS
            # base; subsequent mutations remain protected by expected-before.
            snapshot_before = snapshot_after_context
            base_snapshot = recovered_snapshot
        self._require_strict_story_project_writeback(persist=persist)
        base_snapshot, memory_context = self._apply_story_project_context(base_snapshot, memory_context)
        snapshot_pack = build_snapshot_input_pack(base_snapshot, memory_context)
        state_result = build_snapshot_state_with_audit(base_snapshot, memory_context)
        snapshot = self._apply_story_project_authority(state_result["snapshot"])
        snapshot_audit = state_result["audit"]
        memory_context["snapshot_builder_audit"] = snapshot_audit
        if self.autonomy_run_context is not None:
            if _director_mode(self.director) == "model":
                raise PersistencePreparationError(
                    "autonomy execution does not permit an unreceipted model Director stage"
                )
            context = self._story_project_context_dict() or {}
            identity = context.get("project_identity")
            authority = identity.get("authority") if isinstance(identity, dict) else None
            if not isinstance(authority, dict):
                raise PersistencePreparationError(
                    "autonomy execution requires event-authority StoryProject context"
                )
            self.autonomy_run_context.validate_executor_scope(
                chapter_index=int(context.get("chapter_index") or 0),
                authority=authority,
            )
        decision_started_at = utc_now()
        reset_retry_telemetry()
        try:
            decision = validate_decision(self.director(snapshot, memory_context))
            context_chapter = (self._story_project_context_dict() or {}).get("chapter_index")
            if context_chapter is not None and int(decision["chapter_index"]) != int(context_chapter):
                raise StoryProjectContextError(
                    "story_project_chapter_mismatch",
                    f"Director chose chapter {decision['chapter_index']} for StoryProject chapter {context_chapter}",
                )
        except Exception as exc:  # noqa: BLE001 - persist Director failure diagnostics.
            director_trace = _director_trace(
                self.director,
                decision_started_at,
                utc_now(),
                status="failed",
                error=exc,
                provider_attempts=[*memory_provider_attempts, *consume_retry_telemetry()],
            )
            if persist:
                failed_run = build_director_failed_run_record(
                    started_at=started_at,
                    finished_at=utc_now(),
                    base_snapshot=base_snapshot,
                    runtime_snapshot=snapshot,
                    memory_context=memory_context,
                    director_trace=director_trace,
                    error=exc,
                    snapshot_pack=snapshot_pack,
                    snapshot_audit=snapshot_audit,
                )
                self._attach_snapshot_pack_artifact(failed_run, snapshot_pack)
                self._save_run_record({"run": failed_run})
            raise
        director_trace = _director_trace(
            self.director,
            decision_started_at,
            utc_now(),
            provider_attempts=[*memory_provider_attempts, *consume_retry_telemetry()],
        )
        try:
            workflow_plan = build_dynamic_flow_plan(decision)
            workflow = list(workflow_plan["actions"])
        except Exception as exc:  # noqa: BLE001 - persist workflow planning diagnostics.
            if persist:
                failed_run = build_workflow_failed_run_record(
                    started_at=started_at,
                    finished_at=utc_now(),
                    base_snapshot=base_snapshot,
                    runtime_snapshot=snapshot,
                    memory_context=memory_context,
                    decision=decision,
                    director_trace=director_trace,
                    error=exc,
                    snapshot_pack=snapshot_pack,
                    snapshot_audit=snapshot_audit,
                )
                self._attach_snapshot_pack_artifact(failed_run, snapshot_pack)
                self._save_run_record({"run": failed_run})
            raise

        story_project_context = self._story_project_context_dict()
        quality_policy = self._effective_quality_policy(persist=persist)
        input_pack = build_input_pack(
            snapshot,
            decision,
            memory_context,
            story_project_context=story_project_context,
        )
        input_pack_metadata = build_input_pack_metadata(
            input_pack,
            snapshot,
            decision,
            memory_context,
            story_project_context=story_project_context,
        )
        recovery_context = build_recovery_context(memory_context)
        try:
            chapter, validation, repair_attempts, workflow_trace, chapter_pipeline = self._execute_workflow(
                workflow=workflow,
                workflow_plan=workflow_plan,
                snapshot=snapshot,
                decision=decision,
                input_pack=input_pack,
                recovery_context=recovery_context,
            )
        except WorkflowExecutionError as exc:
            if persist:
                failed_chapter_pipeline = _finalize_chapter_pipeline(
                    exc.chapter_pipeline,
                    validation=exc.validation,
                    repair_deltas=_trace_repair_deltas(exc.trace),
                    workflow_trace=exc.trace,
                    committed=False,
                    commit_status="skipped",
                )
                failed_run = build_failed_run_record(
                    started_at=started_at,
                    finished_at=utc_now(),
                    base_snapshot=base_snapshot,
                    runtime_snapshot=snapshot,
                    memory_context=memory_context,
                    decision=decision,
                    workflow=workflow,
                    workflow_plan=workflow_plan,
                    input_pack=input_pack,
                    input_pack_metadata=input_pack_metadata,
                    chapter=exc.chapter,
                    validation=exc.validation,
                    repair_attempts=exc.repair_attempts,
                    workflow_trace=exc.trace,
                    director_trace=director_trace,
                    error=exc.original,
                    snapshot_pack=snapshot_pack,
                    snapshot_audit=snapshot_audit,
                    chapter_pipeline=failed_chapter_pipeline,
                )
                failed_result = {
                    "run": failed_run,
                    "decision": decision,
                    "workflow": workflow,
                    "workflow_plan": workflow_plan,
                    "chapter": exc.chapter,
                    "validation": exc.validation,
                    "analysis": _empty_analysis(exc.validation or {"ok": False}),
                    "snapshot": base_snapshot,
                    "repair_attempts": exc.repair_attempts,
                    "accepted": False,
                    "committed": False,
                }
                input_pack_artifact = save_input_pack_artifact(
                    input_pack=input_pack,
                    run=failed_result["run"],
                    output_dir=self.run_dir / "input_packs",
                )
                failed_result["run"]["input_pack"]["artifact"] = input_pack_artifact
                self._attach_snapshot_pack_artifact(failed_result["run"], snapshot_pack)
                self._attach_chapter_pipeline_artifacts(
                    failed_result["run"],
                    failed_chapter_pipeline,
                    exc.validation,
                    _trace_repair_deltas(exc.trace),
                )
                self._attach_chapter_artifact(failed_result)
                self._save_run_record(failed_result)
            raise exc.original from exc

        quality_decision = self.quality_coordinator.decide(
            policy=quality_policy.with_overrides(include_review=False),
            validation=validation,
            chapter_index=int(decision["chapter_index"]),
        )
        accepted = bool(quality_decision["accepted"])
        planned_run_id = build_run_id(int(decision["chapter_index"]), started_at)
        review_pipeline = None
        review_gate = None
        review_repair = None
        chapter, validation, accepted, chapter_pipeline, review_pipeline, review_gate, review_repair, quality_decision = (
            self._run_runtime_review_before_commit(
                chapter=chapter,
                validation=validation,
                committed=accepted,
                chapter_pipeline=chapter_pipeline,
                snapshot=snapshot,
                decision=decision,
                input_pack=input_pack,
                recovery_context=recovery_context,
                previous_chapter_text=_previous_chapter_text(
                    memory_context,
                    story_project_context=story_project_context,
                    chapter_index=int(decision["chapter_index"]),
                    run_dir=self.run_dir,
                    chapter_artifact_root=self.chapter_dir,
                ),
                run_id=planned_run_id,
                quality_policy=quality_policy,
                base_quality_decision=quality_decision,
            )
        )
        committed = bool(accepted and persist)
        try:
            analysis = (
                validate_schema(self._analyze(chapter, validation, snapshot), "analysis_result.schema.json")
                if accepted
                else _empty_analysis(validation)
            )
            next_snapshot = (
                update_snapshot(snapshot, analysis, validation, source_run_id=planned_run_id)
                if accepted
                else base_snapshot
            )
            memory_updates = (
                build_memory_updates({"id": planned_run_id, "chapter_index": decision["chapter_index"]}, analysis)
                if committed
                else []
            )
            state_update_audit = build_state_update_audit(
                snapshot=snapshot,
                next_snapshot=next_snapshot,
                analysis=analysis,
                memory_updates=memory_updates,
                applied=committed,
            )
        except Exception as exc:  # noqa: BLE001 - persist post-validation failure diagnostics.
            if persist:
                failed_chapter_pipeline = _finalize_chapter_pipeline(
                    chapter_pipeline,
                    validation=validation,
                    repair_deltas=_trace_repair_deltas(workflow_trace),
                    workflow_trace=workflow_trace,
                    committed=False,
                    commit_status="failed" if validation and validation.get("ok") else "skipped",
                )
                failed_run = build_failed_run_record(
                    started_at=started_at,
                    finished_at=utc_now(),
                    base_snapshot=base_snapshot,
                    runtime_snapshot=snapshot,
                    memory_context=memory_context,
                    decision=decision,
                    workflow=workflow,
                    workflow_plan=workflow_plan,
                    input_pack=input_pack,
                    input_pack_metadata=input_pack_metadata,
                    chapter=chapter,
                    validation=None,
                    repair_attempts=repair_attempts,
                    workflow_trace=workflow_trace,
                    director_trace=director_trace,
                    error=exc,
                    snapshot_pack=snapshot_pack,
                    snapshot_audit=snapshot_audit,
                    chapter_pipeline=failed_chapter_pipeline,
                )
                failed_result = {
                    "run": failed_run,
                    "decision": decision,
                    "workflow": workflow,
                    "workflow_plan": workflow_plan,
                    "chapter": chapter,
                    "validation": None,
                    "analysis": _empty_analysis({"ok": False}),
                    "snapshot": base_snapshot,
                    "repair_attempts": repair_attempts,
                    "accepted": False,
                    "committed": False,
                }
                input_pack_artifact = save_input_pack_artifact(
                    input_pack=input_pack,
                    run=failed_result["run"],
                    output_dir=self.run_dir / "input_packs",
                )
                failed_result["run"]["input_pack"]["artifact"] = input_pack_artifact
                self._attach_snapshot_pack_artifact(failed_result["run"], snapshot_pack)
                self._attach_chapter_pipeline_artifacts(
                    failed_result["run"],
                    failed_chapter_pipeline,
                    None,
                    _trace_repair_deltas(workflow_trace),
                )
                self._attach_chapter_artifact(failed_result)
                self._save_run_record(failed_result)
            raise
        finished_at = utc_now()
        chapter_pipeline = _finalize_chapter_pipeline(
            chapter_pipeline,
            validation=validation,
            repair_deltas=_trace_repair_deltas(workflow_trace),
            workflow_trace=workflow_trace,
            committed=committed,
        )
        run_record = build_run_record(
            started_at=started_at,
            finished_at=finished_at,
            base_snapshot=base_snapshot,
            runtime_snapshot=snapshot,
            memory_context=memory_context,
            decision=decision,
            workflow=workflow,
            workflow_plan=workflow_plan,
            input_pack=input_pack,
            input_pack_metadata=input_pack_metadata,
            chapter=chapter,
            validation=validation,
            analysis=analysis,
            repair_attempts=repair_attempts,
            committed=committed,
            workflow_trace=workflow_trace,
            director_trace=director_trace,
            snapshot_pack=snapshot_pack,
            snapshot_audit=snapshot_audit,
            state_update_audit=state_update_audit,
            chapter_pipeline=chapter_pipeline,
            quality_decision=quality_decision,
            accepted=accepted,
            status="preview" if accepted and not persist else None,
        )

        result = {
            "run": run_record,
            "decision": decision,
            "workflow": workflow,
            "workflow_plan": workflow_plan,
            "chapter": chapter,
            "validation": validation,
            "analysis": analysis,
            "snapshot": next_snapshot,
            "repair_attempts": repair_attempts,
            "accepted": accepted,
            "quality_decision": quality_decision,
            "committed": committed,
            "state_update": state_update_audit,
        }
        self._attach_precomputed_runtime_review(result, review_pipeline, review_gate, review_repair)
        self._attach_story_project_audit(result)
        self._attach_execution_evidence(result)

        if persist:
            if accepted:
                self._persist_accepted_result(
                    result,
                    base_snapshot=base_snapshot,
                    runtime_snapshot=snapshot,
                    next_snapshot=next_snapshot,
                    analysis=analysis,
                    validation=validation,
                    workflow_trace=workflow_trace,
                    snapshot_before=snapshot_before,
                    snapshot_pack=snapshot_pack,
                    input_pack=input_pack,
                    chapter_pipeline=chapter_pipeline,
                    review_repair=review_repair,
                )
            else:
                self._attach_execution_artifacts(
                    result,
                    snapshot_pack=snapshot_pack,
                    input_pack=input_pack,
                    chapter_pipeline=chapter_pipeline,
                    validation=validation,
                    workflow_trace=workflow_trace,
                    review_repair=review_repair,
                )
                self._attach_chapter_artifact(result)
                self._save_run_record(result)

        return result

    def run_loop(
        self,
        *,
        steps: int,
        persist: bool = True,
        stop_on_rejection: bool = True,
        observer: LoopObserver | None = None,
    ) -> dict[str, Any]:
        with self._execution_scope(persist=persist):
            return self._run_loop_impl(
                steps=steps,
                persist=persist,
                stop_on_rejection=stop_on_rejection,
                observer=observer,
            )

    def _run_loop_impl(
        self,
        *,
        steps: int,
        persist: bool = True,
        stop_on_rejection: bool = True,
        observer: LoopObserver | None = None,
    ) -> dict[str, Any]:
        if steps < 1:
            raise ValueError("steps must be at least 1")
        story_project_enabled = self.story_project_context is not None or self.story_project_context_loader is not None
        if story_project_enabled and steps > 1:
            if self.story_project_context_loader is None:
                raise ValueError("StoryProject multi-step execution requires story_project_context_loader")
            if not persist or self.story_project_writeback.mode != "apply":
                raise ValueError("StoryProject multi-step execution requires persisted apply writeback")

        started_at = utc_now()
        runs: list[dict[str, Any]] = []
        step_timings: list[dict[str, Any]] = []
        loop_snapshot: dict[str, Any] | None = None
        previous_result: dict[str, Any] | None = None
        chapter_hint: int | None = None
        stopped_reason = "max_steps"
        _notify_loop(observer, {"event": "loop_start", "requested_steps": steps})
        for step_number in range(1, steps + 1):
            known_run_ids = _persisted_run_ids(self.run_dir) if persist else set()
            step_started_at = utc_now()
            _notify_loop(observer, {"event": "step_start", "step": step_number, "requested_steps": steps})
            try:
                result = self._invoke_once(
                    persist=persist,
                    snapshot_override=loop_snapshot,
                    previous_result=previous_result,
                    chapter_hint=chapter_hint,
                )
            except Exception as exc:
                if persist:
                    failed_result = _load_newest_persisted_result(self.run_dir, known_run_ids)
                    if failed_result is not None:
                        runs.append(failed_result)
                        step_timings.append(_loop_step_timing(step_number, step_started_at, failed_result, error=exc))
                    else:
                        step_timings.append(_loop_step_timing(step_number, step_started_at, None, error=exc))
                else:
                    step_timings.append(_loop_step_timing(step_number, step_started_at, None, error=exc))
                _notify_loop(
                    observer,
                    {
                        "event": "step_failed",
                        "step": step_number,
                        "requested_steps": steps,
                        "duration_ms": step_timings[-1]["duration_ms"],
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                stopped_reason = "failed"
                session = self._build_loop_session(
                    started_at=started_at,
                    requested_steps=steps,
                    stopped_reason=stopped_reason,
                    persist=persist,
                    stop_on_rejection=stop_on_rejection,
                    runs=runs,
                    step_timings=step_timings,
                    error=exc,
                )
                raise LoopExecutionError(original=exc, session=session, runs=runs) from exc
            else:
                runs.append(result)
                if not persist and result.get("accepted") and isinstance(result.get("snapshot"), dict):
                    loop_snapshot = normalize_snapshot(result["snapshot"])
                if story_project_enabled:
                    resolved_chapter = int(result["run"]["chapter_index"])
                    chapter_hint = resolved_chapter + 1 if result.get("committed") else resolved_chapter
                previous_result = result
                step_timings.append(_loop_step_timing(step_number, step_started_at, result))
                _notify_loop(
                    observer,
                    {
                        "event": "step_end",
                        "step": step_number,
                        "requested_steps": steps,
                        "duration_ms": step_timings[-1]["duration_ms"],
                        "run_id": result["run"]["id"],
                        "chapter_index": result["run"]["chapter_index"],
                        "committed": result["committed"],
                        "status": result["run"]["status"],
                    },
                )
                step_failure_reasons = _result_failure_reasons(result)
                if any(
                    reason in {"run_failed", "story_project_writeback_failed", "memory_delivery_failed"}
                    for reason in step_failure_reasons
                ):
                    stopped_reason = "failed"
                    break
                if stop_on_rejection and "run_rejected" in step_failure_reasons:
                    stopped_reason = "rejected"
                    break

        session = self._build_loop_session(
            started_at=started_at,
            requested_steps=steps,
            stopped_reason=stopped_reason,
            persist=persist,
            stop_on_rejection=stop_on_rejection,
            runs=runs,
            step_timings=step_timings,
        )
        _notify_loop(
            observer,
            {
                "event": "loop_end",
                "requested_steps": steps,
                "completed_steps": len(runs),
                "stopped_reason": stopped_reason,
            },
        )

        return {
            "session": session,
            "runs": runs,
            "completed_steps": len(runs),
            "stopped_reason": stopped_reason,
            "last_result": runs[-1],
            "succeeded": bool(session.get("succeeded")),
            "exit_code": int(session.get("exit_code") or 0),
            "failure_reasons": list(session.get("failure_reasons") or []),
        }

    def _build_loop_session(
        self,
        *,
        started_at,
        requested_steps: int,
        stopped_reason: str,
        persist: bool,
        stop_on_rejection: bool,
        runs: list[dict[str, Any]],
        step_timings: list[dict[str, Any]],
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        session = build_loop_session_record(
            started_at=started_at,
            finished_at=utc_now(),
            requested_steps=requested_steps,
            completed_steps=len(runs),
            stopped_reason=stopped_reason,
            persist=persist,
            stop_on_rejection=stop_on_rejection,
            runs=runs,
            step_timings=step_timings,
            book_id=(self._last_project_identity or {}).get("book_id"),
            error=error,
        )
        if persist:
            artifact = save_loop_session_artifact(session=session, output_dir=self.run_dir / "loop_sessions")
            session["artifact"] = artifact
            validate_schema(session, "loop_session.schema.json")
        return session

    def _generate(self, input_pack: str) -> str:
        if self.generator:
            return self.generator(input_pack)
        return generate_chapter(input_pack, dry_run=self.dry_run)

    def _execute_autonomy_stage(
        self,
        *,
        stage: str,
        input_value: Any,
        operation: Callable[[], Any],
        default_model: bool,
    ) -> Any:
        context = self.autonomy_run_context
        if context is None:
            return operation()
        execution_kind = context.execution_kind(
            stage, default_model=bool(default_model)
        )
        token = context.before_stage(
            stage=stage,
            input_value=input_value,
            execution_kind=execution_kind,
        )
        if token.cached_output is not None:
            output = token.cached_output
            context.after_stage(
                token,
                output_value=output,
                model_call_receipt_hashes=token.cached_model_call_receipt_hashes,
            )
            return output
        runtime = self._model_call_runtime
        try:
            if runtime is None:
                output = operation()
                operation_receipt_hashes: list[str] = []
            else:
                with runtime.bind_operation(token.operation_key) as call_scope:
                    output = operation()
                    operation_receipt_hashes = list(call_scope.receipt_hashes)
        except ProviderCallUncertainError:
            context.failed_stage(token, status="provider_call_uncertain")
            raise
        except ContextBudgetError:
            context.failed_stage(token, status="budget_rejected")
            raise
        if execution_kind == "deterministic" and operation_receipt_hashes:
            raise PersistencePreparationError(
                "deterministic autonomy stage performed an unbound model call"
            )
        context.after_stage(
            token,
            output_value=output,
            model_call_receipt_hashes=(
                operation_receipt_hashes if execution_kind == "model" else ()
            ),
        )
        return output

    def _model_call_receipt_hashes(self) -> list[str]:
        runtime = self._model_call_runtime
        if runtime is None:
            return []
        directory = runtime.store.receipts_dir
        if not directory.is_dir():
            return []
        return [
            str(load_model_call_receipt(path)["receipt_hash"])
            for path in sorted(directory.glob("*.json"))
        ]

    def _polish(self, chapter: str) -> str:
        if self.polisher:
            return self.polisher(chapter)
        return polish_chapter(chapter, dry_run=self.dry_run)

    def _repair(
        self,
        chapter: str,
        validation: dict[str, Any],
        input_pack: str,
        repair_plan: dict[str, Any],
        recovery_context: dict[str, Any],
        language: str = "en",
        repair_context: RepairContext | None = None,
    ) -> str:
        if self.repairer:
            if _repairer_accepts_recovery_context(self.repairer):
                return self.repairer(chapter, validation, input_pack, repair_plan, recovery_context)
            if _repairer_accepts_plan(self.repairer):
                return self.repairer(chapter, validation, input_pack, repair_plan)
            return self.repairer(chapter, validation, input_pack)
        return repair_scene(
            chapter,
            validation,
            input_pack,
            dry_run=self.dry_run,
            repair_plan=repair_plan,
            recovery_context=recovery_context,
            language=language,
            repair_context=repair_context,
        )

    def _analyze(self, chapter: str, validation: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        if self.analyzer is analyze_chapter:
            return analyze_chapter(chapter, validation, snapshot=snapshot)
        return self.analyzer(chapter, validation)

    def _execute_workflow(
        self,
        *,
        workflow: list[str],
        workflow_plan: dict[str, Any],
        snapshot: dict[str, Any],
        decision: dict[str, Any],
        input_pack: str,
        recovery_context: dict[str, Any],
    ) -> tuple[str, dict[str, Any], int, list[dict[str, Any]], dict[str, Any] | None]:
        state: dict[str, Any] = {
            "chapter": "",
            "chapter_pipeline": None,
            "chapter_index": int(decision.get("chapter_index") or 1),
            "snapshot_ready": False,
            "bridge_prevalidated": False,
            "commit_snapshot_requested": False,
            "validation": None,
            "repair_attempts": 0,
            "repair_plan": None,
            "repair_deltas": [],
            "workflow_skip_reason": None,
        }
        trace: list[dict[str, Any]] = []
        planned_steps = _workflow_steps_by_action(workflow_plan)
        handlers = {
            "build_snapshot": lambda: self._handle_build_snapshot(state),
            "pre_validate_bridge": lambda: self._handle_pre_validate_bridge(state, snapshot),
            "generate_chapter": lambda: self._handle_generate(state, input_pack, snapshot),
            "polish": lambda: self._handle_polish(state),
            "validate": lambda: self._handle_validate(state, snapshot, decision),
            "repair_if_needed": lambda: self._handle_repair_if_needed(
                state,
                snapshot,
                decision,
                input_pack,
                recovery_context,
            ),
            "commit_snapshot": lambda: self._handle_commit_snapshot(state),
        }

        for action in workflow:
            handler = handlers.get(action)
            if handler is None:
                raise ValueError(f"Unknown workflow action: {action}")
            planned_step = planned_steps.get(action)
            started_at = utc_now()
            reset_retry_telemetry()
            try:
                handler()
            except Exception as exc:  # noqa: BLE001 - preserve failed action diagnostics.
                provider_attempts = consume_retry_telemetry()
                trace.append(
                    _trace_event(
                        action,
                        started_at,
                        utc_now(),
                        state,
                        planned_step=planned_step,
                        model_trace=self._model_trace_metadata(action, state),
                        provider_attempts=provider_attempts,
                        status="failed",
                        error=exc,
                    )
                )
                if _can_continue_after_polish_error(action, state, exc):
                    state["validation"] = None
                    continue
                raise WorkflowExecutionError(
                    original=exc,
                    trace=trace,
                    chapter=state["chapter"] if isinstance(state.get("chapter"), str) else "",
                    chapter_pipeline=state["chapter_pipeline"] if isinstance(state.get("chapter_pipeline"), dict) else None,
                    validation=state["validation"] if isinstance(state.get("validation"), dict) else None,
                    repair_attempts=int(state.get("repair_attempts") or 0),
                ) from exc
            else:
                provider_attempts = consume_retry_telemetry()
                trace.append(
                    _trace_event(
                        action,
                        started_at,
                        utc_now(),
                        state,
                        planned_step=planned_step,
                        model_trace=self._model_trace_metadata(action, state),
                        provider_attempts=provider_attempts,
                    )
                )

        if state["validation"] is None:
            started_at = utc_now()
            self._handle_validate(state, snapshot, decision)
            trace.append(
                _trace_event(
                    "validate",
                    started_at,
                    utc_now(),
                    state,
                    planned_step=planned_steps.get("validate"),
                    implicit=True,
                )
            )

        return state["chapter"], state["validation"], state["repair_attempts"], trace, state.get("chapter_pipeline")

    def _handle_generate(self, state: dict[str, Any], input_pack: str, snapshot: dict[str, Any]) -> None:
        if self.autonomy_run_context is not None:
            scene_receipt = self.autonomy_run_context.ensure_scene_plan(input_pack)

            def generate() -> dict[str, Any]:
                if self.generator:
                    return {"chapter": self._generate(input_pack), "pipeline": None}
                pipeline = run_chapter_pipeline(
                    input_pack,
                    chapter_index=int(state.get("chapter_index") or 1),
                    dry_run=self.dry_run,
                    scene_limit=self.scene_limit,
                    language=project_language(snapshot),
                    chapter_blueprint=self._story_project_chapter_blueprint(),
                )
                return {
                    "chapter": str(pipeline["merged_chapter"]),
                    "pipeline": pipeline,
                }

            generated = self._execute_autonomy_stage(
                stage="draft",
                input_value={
                    "input_pack_sha256": hashlib.sha256(
                        input_pack.encode("utf-8")
                    ).hexdigest(),
                    "scene_plan_receipt_hash": scene_receipt["receipt_hash"],
                },
                operation=generate,
                default_model=not self.dry_run,
            )
            state["chapter"] = str(generated["chapter"])
            state["chapter_pipeline"] = generated.get("pipeline")
            state["validation"] = None
            return
        if self.generator:
            state["chapter"] = self._generate(input_pack)
            state["chapter_pipeline"] = None
        else:
            pipeline = run_chapter_pipeline(
                input_pack,
                chapter_index=int(state.get("chapter_index") or 1),
                dry_run=self.dry_run,
                scene_limit=self.scene_limit,
                language=project_language(snapshot),
                chapter_blueprint=self._story_project_chapter_blueprint(),
            )
            state["chapter"] = str(pipeline["merged_chapter"])
            state["chapter_pipeline"] = pipeline
        state["validation"] = None

    def _handle_build_snapshot(self, state: dict[str, Any]) -> None:
        state["snapshot_ready"] = True

    def _handle_pre_validate_bridge(self, state: dict[str, Any], snapshot: dict[str, Any]) -> None:
        state["snapshot_ready"] = True
        precheck = validate_bridge_preconditions(snapshot)
        state["bridge_prevalidated"] = True
        state["bridge_precheck"] = precheck
        if not precheck["ok"]:
            raise ValueError(f"bridge pre-validation failed: {', '.join(precheck['problem_codes'])}")

    def _handle_commit_snapshot(self, state: dict[str, Any]) -> None:
        if state["validation"] is None:
            raise ValueError("commit_snapshot requires validation before commit")
        state["commit_snapshot_requested"] = True

    def _handle_polish(self, state: dict[str, Any]) -> None:
        chapter = _require_chapter(state)
        if self.autonomy_run_context is not None:
            state["chapter"] = self._execute_autonomy_stage(
                stage="polish",
                input_value={"chapter_sha256": hashlib.sha256(chapter.encode("utf-8")).hexdigest()},
                operation=lambda: self._polish(chapter),
                default_model=not self.dry_run,
            )
        else:
            state["chapter"] = self._polish(chapter)
        state["validation"] = None

    def _handle_validate(
        self,
        state: dict[str, Any],
        snapshot: dict[str, Any],
        decision: dict[str, Any],
    ) -> None:
        chapter = _require_chapter(state)
        if self.autonomy_run_context is not None:
            self.autonomy_run_context.ensure_polish(chapter)

        def run_validation() -> dict[str, Any]:
            if self.validator is validate_chapter:
                pipeline = state.get("chapter_pipeline") if isinstance(state.get("chapter_pipeline"), dict) else {}
                return validate_chapter(
                    snapshot,
                    chapter,
                    decision,
                    enable_llm=self.enable_llm_validator,
                    chapter_blueprint=self._story_project_chapter_blueprint(),
                    blueprint_coverage=pipeline.get("blueprint_coverage") if isinstance(pipeline, dict) else None,
                )
            return self.validator(snapshot, chapter, decision)

        if self.autonomy_run_context is not None:
            state["validation"] = self._execute_autonomy_stage(
                stage="validator",
                input_value={
                    "chapter_sha256": hashlib.sha256(chapter.encode("utf-8")).hexdigest(),
                    "decision": decision,
                },
                operation=run_validation,
                default_model=bool(self.enable_llm_validator),
            )
        else:
            state["validation"] = run_validation()

    def _handle_repair_if_needed(
        self,
        state: dict[str, Any],
        snapshot: dict[str, Any],
        decision: dict[str, Any],
        input_pack: str,
        recovery_context: dict[str, Any],
    ) -> None:
        state["workflow_skip_reason"] = None
        if state["validation"] is None:
            self._handle_validate(state, snapshot, decision)
        state["repair_deltas"] = []
        max_repair_attempts = int(decision.get("max_repair_attempts", 0))

        if state["validation"]["ok"]:
            state["workflow_skip_reason"] = "validation_ok"
            return

        if state["repair_attempts"] >= max_repair_attempts:
            state["workflow_skip_reason"] = "max_repair_attempts_exhausted"
            return

        while (
            not state["validation"]["ok"]
            and state["repair_attempts"] < max_repair_attempts
        ):
            before_validation = state["validation"]
            attempt = int(state.get("repair_attempts") or 0) + 1
            state["repair_plan"] = build_repair_plan(
                state["validation"],
                repair_budget=max_repair_attempts,
                attempt=attempt,
                recovery_context=recovery_context,
            )
            repair_operation = lambda: self._repair(
                state["chapter"],
                state["validation"],
                input_pack,
                state["repair_plan"],
                recovery_context,
                project_language(snapshot),
                _repair_context_for_snapshot(snapshot),
            )
            if self.autonomy_run_context is not None:
                state["chapter"] = self._execute_autonomy_stage(
                    stage="repair",
                    input_value={
                        "chapter_sha256": hashlib.sha256(
                            state["chapter"].encode("utf-8")
                        ).hexdigest(),
                        "validation": state["validation"],
                        "repair_plan": state["repair_plan"],
                    },
                    operation=repair_operation,
                    default_model=not self.dry_run,
                )
            else:
                state["chapter"] = repair_operation()
            self._handle_validate(state, snapshot, decision)
            state["repair_attempts"] += 1
            state["repair_deltas"].append(
                _repair_delta(
                    attempt=attempt,
                    before=before_validation,
                    after=state["validation"],
                )
            )

    def _load_memory_context(self) -> dict[str, Any]:
        if self.memory_loader:
            return self.memory_loader()
        return load_memory_context(self.memory_path, source=self.memory_source)

    def _apply_story_project_context(
        self,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.story_project_context_service.apply_context(
            self._story_project_context_dict(),
            snapshot,
            memory_context,
            snapshot_path=self.snapshot_path,
            allow_legacy_snapshot_adoption=self._allow_legacy_snapshot_adoption,
        )

    def _apply_story_project_authority(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self.story_project_context_service.apply_authority(
            self._story_project_context_dict(),
            snapshot,
        )

    def _normalize_story_project_identity(self, context: Any, *, persist: bool) -> Any:
        normalized = self.story_project_context_service.normalize_identity(
            context,
            persist=persist,
            persistence_dir=self.persistence_dir,
            allow_legacy_snapshot_adoption=self._allow_legacy_snapshot_adoption,
        )
        self._allow_legacy_snapshot_adoption = normalized.allow_legacy_snapshot_adoption
        if normalized.last_project_identity is not None:
            self._last_project_identity = normalized.last_project_identity
        return normalized.context

    def _story_project_context_dict(self) -> dict[str, Any] | None:
        return self.story_project_context_service.context_dict(
            self._active_story_project_context,
            self.story_project_context,
        )

    def _load_story_project_context(
        self,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
        *,
        chapter_hint: int | None,
    ) -> Any:
        return self.story_project_context_service.load(
            configured_context=self.story_project_context,
            loader=self.story_project_context_loader,
            snapshot=snapshot,
            memory_context=memory_context,
            chapter_hint=chapter_hint,
        )

    def _story_project_chapter_blueprint(self) -> dict[str, Any] | None:
        return self.story_project_context_service.chapter_blueprint(self._story_project_context_dict())

    def _require_strict_story_project_writeback(self, *, persist: bool) -> None:
        self.story_project_context_service.require_strict_writeback(
            self._story_project_context_dict(),
            persist=persist,
            writeback_mode=self.story_project_writeback.mode,
        )

    def _model_trace_metadata(self, action: str, state: dict[str, Any]) -> dict[str, Any] | None:
        config = get_config()
        if action in {"build_snapshot", "pre_validate_bridge", "commit_snapshot"}:
            return None
        if action == "generate_chapter":
            if self.generator:
                return _model_trace("chapter_generation", provider="injected", model=None, invocation="injected")
            if self.dry_run:
                return _model_trace("chapter_generation", provider="local", model=None, invocation="dry_run")
            return _model_trace("chapter_generation", provider="openai", model=config.openai_model, invocation="model")
        if action == "polish":
            if self.polisher:
                return _model_trace("claude_polish", provider="injected", model=None, invocation="injected")
            if self.dry_run:
                return _model_trace("claude_polish", provider="local", model=None, invocation="dry_run")
            return _model_trace("claude_polish", provider="anthropic", model=config.claude_model, invocation="model")
        if action == "repair_if_needed":
            if state.get("workflow_skip_reason"):
                return _model_trace("scene_repair", provider=None, model=None, invocation="none")
            if self.repairer:
                return _model_trace("scene_repair", provider="injected", model=None, invocation="injected")
            if self.dry_run:
                return _model_trace("scene_repair", provider="local", model=None, invocation="dry_run")
            return _model_trace("scene_repair", provider="openai", model=config.openai_model, invocation="model")
        return None

    def _attach_last_run(
        self,
        memory_context: dict[str, Any],
        *,
        previous_result: dict[str, Any] | None = None,
    ) -> None:
        last_run = _loop_local_run_summary(previous_result) if previous_result is not None else load_latest_run_summary(self.run_dir)
        if last_run:
            memory_context["last_run"] = last_run

    def _run_runtime_review_before_commit(
        self,
        *,
        chapter: str,
        validation: dict[str, Any],
        committed: bool,
        chapter_pipeline: dict[str, Any] | None,
        snapshot: dict[str, Any],
        decision: dict[str, Any],
        input_pack: str,
        recovery_context: dict[str, Any],
        previous_chapter_text: str | None,
        run_id: str,
        quality_policy: QualityPolicy,
        base_quality_decision: dict[str, Any],
    ) -> tuple[
        str,
        dict[str, Any],
        bool,
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any],
    ]:
        review_config = self._runtime_review_config_for_policy(quality_policy)
        if not committed or not base_quality_decision["accepted"]:
            return chapter, validation, False, chapter_pipeline, None, None, None, base_quality_decision
        if not review_config.enabled:
            return chapter, validation, True, chapter_pipeline, None, None, None, base_quality_decision

        original_chapter = chapter
        original_review = self._run_runtime_review_for_chapter(
            chapter_text=chapter,
            snapshot=snapshot,
            previous_chapter_text=previous_chapter_text,
            run_id=run_id,
            artifact_suffix="original" if self.review_repair_config.enabled else None,
            config=review_config,
        )
        original_quality_decision = self._quality_decision_with_review(
            policy=quality_policy,
            validation=validation,
            review=original_review,
            chapter_index=int(decision["chapter_index"]),
        )
        review_gate = self._review_gate_for_review(original_review, original_quality_decision)
        if not self.review_repair_config.enabled:
            return (
                chapter,
                validation,
                bool(original_quality_decision["accepted"]) and _review_gate_allows_commit(review_gate),
                chapter_pipeline,
                original_review,
                review_gate,
                None,
                original_quality_decision,
            )

        review_provider_attempts: list[dict[str, Any]] = []

        def repair(current_chapter: str, current_validation: dict[str, Any], repair_plan: dict[str, Any]) -> str:
            reset_retry_telemetry()
            try:
                return self._repair(
                    current_chapter,
                    current_validation,
                    input_pack,
                    repair_plan,
                    recovery_context,
                    project_language(snapshot),
                    _repair_context_for_snapshot(snapshot),
                )
            finally:
                review_provider_attempts.extend(consume_retry_telemetry())

        def validate(repaired_chapter: str) -> dict[str, Any]:
            return self._validate_repaired_chapter(
                chapter=repaired_chapter,
                snapshot=snapshot,
                decision=decision,
                chapter_pipeline=chapter_pipeline,
            )

        def review(repaired_chapter: str, attempt: int) -> dict[str, Any]:
            return self._run_runtime_review_for_chapter(
                chapter_text=repaired_chapter,
                snapshot=snapshot,
                previous_chapter_text=previous_chapter_text,
                run_id=run_id,
                artifact_suffix=f"repair_attempt_{attempt:02d}",
                config=review_config,
            )

        def decide_quality(
            current_validation: dict[str, Any],
            current_review: dict[str, Any],
        ) -> dict[str, Any]:
            return self._quality_decision_with_review(
                policy=quality_policy,
                validation=current_validation,
                review=current_review,
                chapter_index=int(decision["chapter_index"]),
            )

        review_repair = run_review_repair_loop(
            chapter_text=chapter,
            validation=validation,
            before_review=original_review,
            config=self.review_repair_config,
            repair=repair,
            validate=validate,
            review=review,
            quality_policy=quality_policy,
            decide=decide_quality,
        )
        if review_provider_attempts:
            review_repair["provider_attempts"] = review_provider_attempts
        if not review_repair.get("attempted"):
            return (
                chapter,
                validation,
                bool(original_quality_decision["accepted"]) and _review_gate_allows_commit(review_gate),
                chapter_pipeline,
                original_review,
                review_gate,
                review_repair,
                original_quality_decision,
            )

        final_review = (
            review_repair.get("final_review")
            if isinstance(review_repair.get("final_review"), dict)
            else original_review
        )
        final_quality_decision = (
            review_repair.get("final_quality_decision")
            if isinstance(review_repair.get("final_quality_decision"), dict)
            else decide_quality(
                review_repair.get("final_validation")
                if isinstance(review_repair.get("final_validation"), dict)
                else validation,
                final_review,
            )
        )
        final_gate = self._review_gate_for_review(final_review, final_quality_decision)

        if final_quality_decision["accepted"] and _review_gate_allows_commit(final_gate):
            repaired_chapter = str(review_repair.get("final_chapter") or chapter)
            repaired_validation = (
                review_repair.get("final_validation")
                if isinstance(review_repair.get("final_validation"), dict)
                else validation
            )
            repaired_pipeline = self._chapter_pipeline_after_repair(chapter_pipeline, repaired_chapter)
            return (
                repaired_chapter,
                repaired_validation,
                True,
                repaired_pipeline,
                final_review,
                final_gate,
                review_repair,
                final_quality_decision,
            )

        final_chapter = str(review_repair.get("final_chapter") or original_chapter)
        final_validation = (
            review_repair.get("final_validation")
            if isinstance(review_repair.get("final_validation"), dict)
            else validation
        )
        final_pipeline = self._chapter_pipeline_after_repair(chapter_pipeline, final_chapter)
        return (
            final_chapter,
            final_validation,
            False,
            final_pipeline,
            final_review,
            final_gate,
            review_repair,
            final_quality_decision,
        )

    def _run_runtime_review_for_chapter(
        self,
        *,
        chapter_text: str,
        snapshot: dict[str, Any],
        previous_chapter_text: str | None,
        run_id: str,
        artifact_suffix: str | None,
        config: RuntimeReviewConfig | None = None,
    ) -> dict[str, Any]:
        return run_runtime_review(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous_chapter_text,
            run_id=run_id,
            config=config or self.review_config,
            artifact_suffix=artifact_suffix,
        )

    def _review_gate_for_review(
        self,
        review: dict[str, Any] | None,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.quality_coordinator.review_gate(
            review_config=self.review_config,
            review=review,
            quality_decision=quality_decision,
        )

    def _effective_quality_policy(self, *, persist: bool) -> QualityPolicy:
        return self.quality_coordinator.effective_policy(
            configured_policy=self.quality_policy,
            persist=persist,
            story_project_apply=self.story_project_writeback.mode == "apply",
            has_story_project_context=bool(self._story_project_context_dict()),
            review_config=self.review_config,
            review_repair_config=self.review_repair_config,
        )

    def _runtime_review_config_for_policy(self, policy: QualityPolicy) -> RuntimeReviewConfig:
        return self.quality_coordinator.runtime_review_config(policy, self.review_config)

    def _quality_decision_with_review(
        self,
        *,
        policy: QualityPolicy,
        validation: dict[str, Any],
        review: dict[str, Any],
        chapter_index: int,
    ) -> dict[str, Any]:
        return self.quality_coordinator.decide(
            policy=policy,
            validation=validation,
            review=review,
            chapter_index=chapter_index,
        )

    def _validate_repaired_chapter(
        self,
        *,
        chapter: str,
        snapshot: dict[str, Any],
        decision: dict[str, Any],
        chapter_pipeline: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if self.validator is not validate_chapter:
            return self.validator(snapshot, chapter, decision)
        blueprint = self._story_project_chapter_blueprint()
        coverage = None
        if isinstance(blueprint, dict):
            coverage = build_blueprint_coverage(blueprint, _synthetic_repaired_scene_drafts(blueprint), chapter)
            if isinstance(chapter_pipeline, dict):
                chapter_pipeline["blueprint_coverage"] = coverage
        return validate_chapter(
            snapshot,
            chapter,
            decision,
            enable_llm=self.enable_llm_validator,
            chapter_blueprint=blueprint,
            blueprint_coverage=coverage,
        )

    def _chapter_pipeline_after_repair(
        self,
        chapter_pipeline: dict[str, Any] | None,
        chapter: str,
    ) -> dict[str, Any] | None:
        if not isinstance(chapter_pipeline, dict):
            return None
        updated = dict(chapter_pipeline)
        updated["merged_chapter"] = chapter
        blueprint = self._story_project_chapter_blueprint()
        if isinstance(blueprint, dict):
            updated["blueprint_coverage"] = build_blueprint_coverage(
                blueprint,
                _synthetic_repaired_scene_drafts(blueprint),
                chapter,
            )
        return updated

    def _attach_precomputed_runtime_review(
        self,
        result: dict[str, Any],
        review: dict[str, Any] | None,
        gate: dict[str, Any] | None,
        review_repair: dict[str, Any] | None,
    ) -> None:
        run = result.get("run")
        if not isinstance(run, dict):
            return
        if isinstance(review, dict):
            run["review_pipeline"] = review
            result["review_pipeline"] = review
        if isinstance(gate, dict):
            run["review_gate"] = gate
            result["review_gate"] = gate
        if isinstance(review_repair, dict):
            public_payload = _review_repair_run_payload(review_repair)
            run["review_repair"] = public_payload
            result["review_repair"] = public_payload
        if isinstance(review, dict):
            self._attach_review_index(result)

    def _attach_review_repair_artifacts(
        self,
        result: dict[str, Any],
        review_repair: dict[str, Any] | None,
    ) -> None:
        if not isinstance(review_repair, dict) or not review_repair.get("attempted"):
            return
        run = result.get("run")
        if not isinstance(run, dict):
            return
        artifacts = save_review_repair_artifacts(
            review_repair=review_repair,
            run=run,
            output_dir=self.run_dir / "review_repairs",
        )
        review_repair = dict(review_repair)
        review_repair["artifacts"] = artifacts
        public_payload = _review_repair_run_payload(review_repair)
        run["review_repair"] = public_payload
        result["review_repair"] = public_payload

    def _save_run_record(self, result: dict[str, Any]) -> None:
        self._attach_story_project_audit(result)
        self._attach_execution_evidence(result)
        validate_run_result(result)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"{result['run']['id']}.json"
        atomic_write_json(path, result)

    def _assert_persistence_ready(self, *, expected_book_id: str | None = None) -> None:
        self.persistence_coordinator.assert_ready(expected_book_id=expected_book_id)

    def _prepare_project_identity_for_persistence(self) -> None:
        prepared = self.story_project_context_service.prepare_identity_for_persistence(
            configured_context=self.story_project_context,
            loader=self.story_project_context_loader,
            persistence_dir=self.persistence_dir,
        )
        self._expected_book_id = prepared.expected_book_id
        self._last_project_identity = prepared.last_project_identity
        self._allow_legacy_snapshot_adoption = prepared.allow_legacy_snapshot_adoption

    def _configure_authority_persistence_backend(self) -> None:
        identity = self._last_project_identity or {}
        authority = identity.get("authority") if isinstance(identity.get("authority"), dict) else {}
        mode = authority.get("mode")
        story_root = self._configured_story_project_root()
        if mode == "event_v1":
            if story_root is None:
                raise PersistenceError(
                    "event-authority persistence requires a configured StoryProject root"
                )
            root_map = self._build_event_authority_root_map(story_root)
            self.persistence_coordinator = PersistenceCoordinator(
                run_dir=self.run_dir,
                persistence_dir=self.persistence_dir,
                backend="v2",
                root_map=root_map,
            )
            self._event_authority_root_map = root_map
            if self.delivery_queue is not None:
                recover_completed_delivery_jobs(
                    self.persistence_dir,
                    root_map=root_map,
                    queue=self.delivery_queue,
                )
            return
        if self.autonomy_run_context is not None:
            raise PersistenceError(
                "durable autonomy requires event-authority persistence v2"
            )
        if self.file_delivery_profile is not None:
            raise PersistenceError(
                "required file delivery requires event-authority persistence v2"
            )
        if story_root is not None and self._event_authority_was_activated(story_root):
            raise PersistenceError(
                "event-authority downgrade detected; legacy persistence is permanently disabled"
            )
        self.persistence_coordinator = PersistenceCoordinator(
            run_dir=self.run_dir,
            persistence_dir=self.persistence_dir,
            backend="v1",
        )
        self._event_authority_root_map = None

    def _configured_story_project_root(self) -> Path | None:
        configured = self.story_project_context
        if isinstance(configured, dict) and configured.get("story_project_root"):
            return Path(str(configured["story_project_root"])).resolve()
        if self.story_project_context_loader is not None:
            root = getattr(self.story_project_context_loader, "story_project_root", None)
            if root is not None:
                return Path(root).resolve()
        return None

    def _build_event_authority_root_map(self, story_root: Path) -> dict[str, Path]:
        runtime_candidates = [self.run_dir.resolve(), self.persistence_dir.resolve()]
        try:
            runtime_root = Path(os.path.commonpath([str(path) for path in runtime_candidates]))
        except ValueError as exc:
            raise PersistenceError(
                "event-authority run and persistence directories must share a local runtime root"
            ) from exc
        if runtime_root == Path(runtime_root.anchor):
            raise PersistenceError(
                "event-authority runtime root is too broad; configure run and persistence siblings"
            )
        delivery_root = (
            self.delivery_queue.root
            if self.delivery_queue is not None
            else runtime_root / "deliveries"
        )
        roots = {
            "story_project": story_root.resolve(),
            "runtime": runtime_root.resolve(),
            "snapshot": self.snapshot_path.parent.resolve(),
            "chapter_artifacts": self.chapter_dir.resolve(),
            "delivery_store": delivery_root.resolve(),
        }
        for path in roots.values():
            path.mkdir(parents=True, exist_ok=True)
        for path in (
            self.run_dir,
            self.run_dir / "publication_receipts",
            self.run_dir / "snapshot_packs",
            self.run_dir / "input_packs",
            self.run_dir / "chapter_pipeline",
            self.run_dir / "review_repairs",
            self.run_dir / "story_project_writebacks",
            self.run_dir / "delivery_intents",
            self.run_dir / "autonomy_evidence",
        ):
            path.mkdir(parents=True, exist_ok=True)
        memory_root = RuntimePaths.for_story_project(story_root).memory_dir / "v2"
        ensure_memory_v2_storage_layout(memory_root)
        (memory_root / "projections").mkdir(parents=True, exist_ok=True)
        (memory_root / "projections" / "receipts").mkdir(parents=True, exist_ok=True)
        (memory_root / "projections" / CORE_DIRECTORY_NAMES[3]).mkdir(
            parents=True, exist_ok=True
        )
        return roots

    def _event_authority_was_activated(self, story_root: Path) -> bool:
        receipts = story_root / ".novelagent" / "authority" / "receipts"
        if receipts.is_dir():
            for path in receipts.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    # An unreadable authority receipt is itself ambiguous; do
                    # not allow that ambiguity to select the legacy writer.
                    return True
                if isinstance(payload, dict) and payload.get("receipt_type") == "authority_activation":
                    return True
        completed = self.persistence_dir / "registry" / "completed"
        if completed.is_dir() and any(completed.glob("*.json")):
            return True
        return False

    def _persistence_state_paths(self) -> list[Path]:
        paths = [self.snapshot_path]
        context = self._story_project_context_dict()
        root = context.get("story_project_root") if isinstance(context, dict) else None
        if not root and self.story_project_context_loader is not None:
            root = getattr(self.story_project_context_loader, "story_project_root", None)
        if root:
            paths.append(Path(str(root)))
        if hasattr(self.memory_writer, "path"):
            paths.append(Path(getattr(self.memory_writer, "path")))
        return paths

    def _attach_story_project_audit(self, result: dict[str, Any]) -> None:
        context = self._story_project_context_dict()
        run = result.get("run") if isinstance(result, dict) else None
        if not context or not isinstance(run, dict):
            return
        chapter = run.get("chapter") if isinstance(run.get("chapter"), dict) else {}
        pipeline_summary = chapter.get("pipeline") if isinstance(chapter.get("pipeline"), dict) else {}
        existing = run.get("story_project") if isinstance(run.get("story_project"), dict) else {}
        writeback = existing.get("writeback") if isinstance(existing.get("writeback"), dict) else default_story_project_writeback()
        run["story_project"] = {
            "enabled": True,
            "mode": context.get("story_state_mode") or "compatible",
            "root": context.get("story_project_root"),
            "book_id": (context.get("project_identity") or {}).get("book_id"),
            "project_identity": context.get("project_identity"),
            "chapter_index": context.get("chapter_index"),
            "chapter_resolution": context.get("chapter_resolution"),
            "chapter_blueprint": context.get("chapter_blueprint"),
            "source_paths": context.get("source_paths"),
            "source_resolution": context.get("source_resolution"),
            "semantic_state": context.get("semantic_audit"),
            "memory_v2": {
                key: (context.get("memory_v2") or {}).get(key)
                for key in ("status", "canonical_path", "event_store", "revision", "projection_hash")
            },
            "blueprint_coverage": pipeline_summary.get("blueprint_coverage"),
            "writeback": writeback,
        }
        if isinstance(self.story_project_oh_story_report, dict):
            run["story_project"]["oh_story"] = self.story_project_oh_story_report

    def _persist_accepted_result(
        self,
        result: dict[str, Any],
        *,
        base_snapshot: dict[str, Any],
        runtime_snapshot: dict[str, Any],
        next_snapshot: dict[str, Any],
        analysis: dict[str, Any],
        validation: dict[str, Any],
        workflow_trace: list[dict[str, Any]],
        snapshot_before: dict[str, Any],
        snapshot_pack: str,
        input_pack: str,
        chapter_pipeline: dict[str, Any] | None,
        review_repair: dict[str, Any] | None,
    ) -> None:
        if self.persistence_coordinator.backend_id == "v2":
            self._persist_event_authority_result_v2(
                result,
                base_snapshot=base_snapshot,
                runtime_snapshot=runtime_snapshot,
                analysis=analysis,
                validation=validation,
                workflow_trace=workflow_trace,
                snapshot_before=snapshot_before,
                snapshot_pack=snapshot_pack,
                input_pack=input_pack,
                chapter_pipeline=chapter_pipeline,
                review_repair=review_repair,
            )
            return
        context = self._story_project_context_dict()
        run = result.get("run") if isinstance(result.get("run"), dict) else None
        if not isinstance(run, dict):
            raise PersistenceError("run record is required for persistence")

        plan = None
        story_targets = []
        rendered_targets = []
        if self.story_project_writeback.enabled:
            plan, story_targets, rendered_targets, prepared_result = prepare_story_project_writeback(
                context=context,
                run=run,
                chapter_text=str(result.get("chapter") or ""),
                validation=validation,
                analysis=analysis,
                config=self.story_project_writeback,
            )
            self._attach_story_project_writeback_payload(result, plan.to_dict(), prepared_result.to_dict())
            if plan.dry_run:
                self._mark_result_preview(
                    result,
                    runtime_snapshot=runtime_snapshot,
                    next_snapshot=next_snapshot,
                    analysis=analysis,
                )
                self._attach_execution_artifacts(
                    result,
                    snapshot_pack=snapshot_pack,
                    input_pack=input_pack,
                    chapter_pipeline=chapter_pipeline,
                    validation=validation,
                    workflow_trace=workflow_trace,
                    review_repair=review_repair,
                )
                self._attach_chapter_artifact(result)
                self._save_run_record(result)
                return
            if plan.blocked:
                self._mark_result_persistence_failed(
                    result,
                    base_snapshot=base_snapshot,
                    runtime_snapshot=runtime_snapshot,
                    next_snapshot=next_snapshot,
                    analysis=analysis,
                    persistence={
                        "run_id": run["id"],
                        "state": "preparation_failed",
                        "committed": False,
                        "partial": False,
                        "errors": list(plan.errors),
                        "targets": [],
                    },
                )
                self._attach_execution_artifacts(
                    result,
                    snapshot_pack=snapshot_pack,
                    input_pack=input_pack,
                    chapter_pipeline=chapter_pipeline,
                    validation=validation,
                    workflow_trace=workflow_trace,
                    review_repair=review_repair,
                )
                self._attach_chapter_artifact(result)
                self._save_run_record(result)
                return

        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        persistence_targets: list[PersistenceTarget] = []
        for rendered in rendered_targets:
            persistence_targets.append(
                PersistenceTarget(
                    rendered.kind,
                    rendered.path,
                    rendered.content,
                    metadata={"story_target_index": rendered.target_index},
                    expected_before_exists=rendered.expected_before_exists,
                    expected_before_sha256=rendered.expected_before_sha256,
                )
            )
        persistence_targets.append(
            PersistenceTarget(
                "snapshot",
                self.snapshot_path,
                json.dumps(normalize_snapshot(next_snapshot), ensure_ascii=False, indent=2) + "\n",
                metadata={"chapter_index": run.get("chapter_index")},
                expected_before_exists=bool(snapshot_before["exists"]),
                expected_before_sha256=snapshot_before.get("sha256"),
            )
        )
        allowed_roots = [self.snapshot_path.parent]
        if plan is not None and plan.story_project_root is not None:
            allowed_roots.insert(0, plan.story_project_root)

        result["run"]["chapter"]["artifact"] = chapter_artifact_metadata(
            chapter_text=str(result.get("chapter") or ""),
            run=result["run"],
            output_dir=self.chapter_dir,
        )
        memory_updates = build_memory_updates(result["run"], analysis)
        memory_v2_commit = self._prepare_strict_memory_v2_commit(
            run=result["run"],
            analysis=analysis,
            runtime_snapshot=runtime_snapshot,
        )
        if memory_v2_commit is not None:
            result["run"].setdefault("memory", {})["v2"] = memory_v2_commit["audit"]
            for target in memory_v2_commit["targets"]:
                persistence_targets.append(
                    PersistenceTarget(
                        str(target["kind"]),
                        target["path"],
                        str(target["content"]),
                        metadata={"memory_v2": True},
                        expected_before_exists=bool(target["expected_before_exists"]),
                        expected_before_sha256=target.get("expected_before_sha256"),
                    )
                )
        writeback_gate = _memory_writeback_gate(
            committed=True,
            validation=validation,
            workflow_trace=workflow_trace,
            memory_updates=memory_updates,
        )
        publication = _persistence_publication(
            chapter_artifact=result["run"]["chapter"]["artifact"],
            memory_updates=memory_updates,
            writeback_gate=writeback_gate,
            writer=self.memory_writer,
        )
        anticipated = self._anticipated_persistence(run["id"], persistence_targets, publication=publication)
        self._attach_persistence_payload(result, anticipated)
        if plan is not None:
            expected_writeback = finalize_story_project_writeback(plan, story_targets, anticipated).to_dict()
            self._attach_story_project_writeback_payload(
                result,
                plan.to_dict(),
                expected_writeback,
                publish_artifacts=False,
            )
        story_context = self._story_project_context_dict() or {}
        read_set = story_context.get("read_set") if isinstance(story_context.get("read_set"), dict) else None
        read_set_writes = (
            declared_read_set_writes(
                read_set,
                (
                    (
                        target.path,
                        hashlib.sha256(target.content_bytes()).hexdigest(),
                        len(target.content_bytes()),
                    )
                    for target in persistence_targets
                ),
            )
            if read_set is not None
            else []
        )
        transaction = LocalPersistenceTransaction(
            run_dir=self.run_dir,
            run_id=str(run["id"]),
            allowed_roots=allowed_roots,
            book_id=(self._last_project_identity or {}).get("book_id"),
            transactions_dir=self.persistence_dir,
            story_project_read_set=read_set,
            read_set_declared_writes=read_set_writes,
        )
        try:
            validate_run_result(result)
            transaction.prepare(persistence_targets, candidate_result=result)
            persistence_result = transaction.commit().to_dict()
        except (PersistenceError, PersistencePreparationError, OSError, ValueError) as exc:
            persistence_result = {
                "run_id": str(run["id"]),
                "state": "preparation_failed",
                "committed": False,
                "partial": False,
                "journal_path": str(transaction.journal_dir),
                "commit_marker": str(transaction.commit_marker_path),
                "targets": [],
                "errors": [{"code": "persistence_prepare_failed", "error": f"{type(exc).__name__}: {exc}"}],
            }

        raw_persistence_result = dict(persistence_result)
        if persistence_result.get("committed") and persistence_result.get("state") in {"commit_marked", "completed"}:
            persistence_result = {**persistence_result, "state": "completed", "publication": publication}
        self._attach_persistence_payload(result, persistence_result)
        if plan is not None:
            actual_writeback = finalize_story_project_writeback(plan, story_targets, persistence_result).to_dict()
            self._attach_story_project_writeback_payload(result, plan.to_dict(), actual_writeback)

        if not persistence_result.get("committed"):
            self._mark_result_persistence_failed(
                result,
                base_snapshot=base_snapshot,
                runtime_snapshot=runtime_snapshot,
                next_snapshot=next_snapshot,
                analysis=analysis,
                persistence=persistence_result,
            )
            self._attach_execution_artifacts(
                result,
                snapshot_pack=snapshot_pack,
                input_pack=input_pack,
                chapter_pipeline=chapter_pipeline,
                validation=validation,
                workflow_trace=workflow_trace,
                review_repair=review_repair,
            )
            self._attach_chapter_artifact(result)
            self._save_run_record(result)
            return

        self._attach_execution_artifacts(
            result,
            snapshot_pack=snapshot_pack,
            input_pack=input_pack,
            chapter_pipeline=chapter_pipeline,
            validation=validation,
            workflow_trace=workflow_trace,
            review_repair=review_repair,
        )
        self._attach_chapter_artifact(result)
        result["run"]["state_update"] = build_state_update_audit(
            snapshot=runtime_snapshot,
            next_snapshot=next_snapshot,
            analysis=analysis,
            memory_updates=memory_updates,
            applied=True,
        )
        result["state_update"] = result["run"]["state_update"]
        if writeback_gate["allowed"]:
            try:
                result["memory_write"] = write_memory_updates(memory_updates, self.memory_writer)
            except Exception as exc:  # noqa: BLE001 - canonical commit already succeeded; record delivery failure.
                target = _memory_writer_target(self.memory_writer)
                result["memory_write"] = validate_memory_writeback_result(
                    {
                        "target": target,
                        "written": 0,
                        "item_mappings": [],
                        "verification": {
                            "status": "failed",
                            "target": target,
                            "failures": [{"error": f"{type(exc).__name__}: {exc}"}],
                        },
                    }
                )
            result["memory_write"]["gate"] = writeback_gate
        else:
            result["memory_write"] = _blocked_memory_writeback(writeback_gate)
        result["run"]["memory"]["writeback"] = result["memory_write"]
        verification = result["memory_write"].get("verification") if isinstance(result["memory_write"], dict) else {}
        delivery_failed = isinstance(verification, dict) and verification.get("status") in {
            "failed",
            "error",
            "readback_failed",
        }
        delivery_status = "failed" if delivery_failed else "delivered"
        _set_memory_publication_status(result, delivery_status)
        if _memory_writer_target(self.memory_writer) == "file" and delivery_failed:
            pending = {**raw_persistence_result, "publication": publication}
            pending["publication"] = _publication_with_memory_status(publication, "failed")
            self._attach_persistence_payload(result, pending)
            if plan is not None:
                pending_writeback = finalize_story_project_writeback(plan, story_targets, pending).to_dict()
                self._attach_story_project_writeback_payload(result, plan.to_dict(), pending_writeback)
            return
        self._save_run_record(result)
        transaction.complete_publication()

    def _persist_event_authority_result_v2(
        self,
        result: dict[str, Any],
        *,
        base_snapshot: dict[str, Any],
        runtime_snapshot: dict[str, Any],
        analysis: dict[str, Any],
        validation: dict[str, Any],
        workflow_trace: list[dict[str, Any]],
        snapshot_before: dict[str, Any],
        snapshot_pack: str,
        input_pack: str,
        chapter_pipeline: dict[str, Any] | None,
        review_repair: dict[str, Any] | None,
    ) -> None:
        context = self._story_project_context_dict()
        run = result.get("run") if isinstance(result.get("run"), dict) else None
        if not isinstance(context, dict) or not isinstance(run, dict):
            raise PersistenceError("event-authority persistence requires run and StoryProject context")
        identity_payload = context.get("project_identity")
        read_set = context.get("read_set")
        semantic_audit = context.get("semantic_audit")
        memory_context = context.get("memory_v2")
        if not all(
            isinstance(item, dict)
            for item in (identity_payload, read_set, semantic_audit, memory_context)
        ):
            raise PersistenceError(
                "event-authority persistence requires identity, read-set, parser audit, and Memory 2.2 context"
            )
        identity = validate_project_identity(identity_payload)
        authority = identity.authority or {}
        if authority.get("mode") != "event_v1":
            raise PersistenceError("v2 persistence cannot run without event_v1 authority")
        root_map = self._event_authority_root_map
        if not isinstance(root_map, dict):
            raise PersistenceError("event-authority root map was not selected before generation")
        story_root = Path(str(context["story_project_root"])).resolve()
        registry = RootRegistryService(self.persistence_dir).ensure(root_map)

        # Event authority deliberately excludes Markdown semantic state from
        # the canonical context.  Reuse the legacy writer only to render the
        # prose target; its strict managed-tracking gate is inapplicable
        # because every non-prose projection is skipped below and rebuilt from
        # the immutable Memory 2.2 event stream instead.
        prose_writeback_context = dict(context)
        prose_writeback_context["story_state_mode"] = "compatible"
        plan, story_targets, rendered_targets, prepared_writeback = prepare_story_project_writeback(
            context=prose_writeback_context,
            run=run,
            chapter_text=str(result.get("chapter") or ""),
            validation=validation,
            analysis=analysis,
            config=self.story_project_writeback,
        )
        if plan.dry_run or plan.blocked:
            reasons = ", ".join(plan.blocked_reasons) or "dry-run writeback"
            raise PersistencePreparationError(
                f"event-authority StoryProject writeback is not committable: {reasons}"
            )
        for target in story_targets:
            if target.kind != "prose" and target.status == "planned":
                target.status = "skipped"
                target.reason = "event_authority_canonical_projection"
        prose_targets = [target for target in rendered_targets if target.kind == "prose"]
        if len(prose_targets) != 1:
            raise PersistencePreparationError(
                "event-authority chapter commit requires exactly one prose target"
            )

        # Canonical chapter evidence is always computed from the exact UTF-8
        # bytes rendered to StoryProject, including the writer's terminal
        # newline normalization.  Raw model text is not a publication byte
        # contract.
        chapter_body = str(prose_targets[0].content)
        chapter_body_sha256 = hashlib.sha256(chapter_body.encode("utf-8")).hexdigest()
        result["chapter"] = chapter_body
        memory_root = RuntimePaths.for_story_project(story_root).memory_dir / "v2"
        ensure_memory_v2_storage_layout(memory_root)
        quality = run.get("quality_decision") if isinstance(run.get("quality_decision"), dict) else {}
        memory_commit = prepare_event_authority_chapter_commit(
            memory_root=memory_root,
            book_id=identity.book_id,
            run_id=str(run["id"]),
            chapter_index=int(run["chapter_index"]),
            analysis=analysis,
            chapter_body=chapter_body,
            chapter_body_sha256=chapter_body_sha256,
            evidence_spans=_chapter_evidence_spans(chapter_body, analysis),
            authority_epoch=int(authority["authority_epoch"]),
            expected_head_event_hash=str(authority["head_event_hash"]),
            expected_revision=int(memory_context["revision"]),
            source_project_digest=str(semantic_audit["source_digest"]),
            context_digest=str(read_set["context_digest"]),
            quality_state={
                "accepted": bool(run.get("accepted")),
                "policy": quality.get("policy"),
                "decision_id": quality.get("decision_id"),
            },
        )
        if memory_commit.get("status") != "prepared" or not isinstance(memory_commit.get("batch"), dict):
            raise PersistencePreparationError(
                "event-authority chapter run did not prepare a new immutable Memory batch"
            )
        delivery_intent = None
        if self.file_delivery_profile is not None:
            delivery_intent = build_file_delivery_intent(
                profile=self.file_delivery_profile,
                book_id=identity.book_id,
                run_id=str(run["id"]),
                chapter_index=int(run["chapter_index"]),
                event_batch=memory_commit["batch"],
                chapter_body_sha256=chapter_body_sha256,
                policy="required",
                created_at=str(run.get("finished_at") or utc_now().isoformat()),
            )
        next_snapshot = canonical_memory_to_snapshot(memory_commit["projection"])
        memory_updates = build_memory_updates(run, analysis)
        state_update = build_state_update_audit(
            snapshot=runtime_snapshot,
            next_snapshot=next_snapshot,
            analysis=analysis,
            memory_updates=memory_updates,
            applied=True,
        )
        result["snapshot"] = next_snapshot
        result["state_update"] = state_update
        run["state_update"] = state_update
        run.setdefault("snapshot", {})["next_chapter_index"] = next_snapshot["chapter_index"]
        run.setdefault("memory", {})["v2"] = memory_commit["audit"]

        advanced_identity = prepare_event_authority_advance(
            identity,
            expected_authority_epoch=int(authority["authority_epoch"]),
            expected_head_event_hash=str(authority["head_event_hash"]),
            new_head_event_hash=str(memory_commit["projection"]["head_event_hash"]),
        )
        identity_path = project_identity_path(story_root)
        identity_before = identity_path.read_bytes()
        identity_content = (
            json.dumps(
                advanced_identity.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        identity_bytes = identity_content.encode("utf-8")

        apply_targets: list[PersistenceV2Target] = []
        autonomy_outline_declarations: list[dict[str, Any]] = []
        if self.autonomy_run_context is not None:
            checkpoint = self.autonomy_run_context.checkpoint
            relative_outline = Path(str(checkpoint["canonical_relative_path"]))
            outline_path = (story_root / relative_outline).resolve(strict=False)
            try:
                outline_path.relative_to(story_root)
            except ValueError as exc:
                raise PersistencePreparationError(
                    "autonomy outline checkpoint escapes StoryProject"
                ) from exc
            before_hash = checkpoint.get("canonical_before_sha256")
            if before_hash is None:
                outline_content = str(checkpoint["outline_text"])
                if not outline_content.endswith("\n"):
                    outline_content += "\n"
                outline_bytes = outline_content.encode("utf-8")
                apply_targets.append(
                    PersistenceV2Target(
                        target_id="story-outline-01",
                        kind="outline",
                        path_ref=self._event_path_ref(
                            outline_path,
                            registry=registry,
                            preferred_root="story_project",
                        ),
                        content=outline_content,
                        metadata={
                            "autonomy": True,
                            "checkpoint_hash": checkpoint["checkpoint_hash"],
                            "outline_hash": hashlib.sha256(outline_bytes).hexdigest(),
                        },
                        expected_before_exists=False,
                    )
                )
                autonomy_outline_declarations = declared_read_set_writes(
                    read_set,
                    ((outline_path, hashlib.sha256(outline_bytes).hexdigest(), len(outline_bytes)),),
                )
            else:
                if not outline_path.is_file() or hashlib.sha256(
                    outline_path.read_bytes()
                ).hexdigest() != before_hash:
                    raise PersistencePreparationError(
                        "canonical outline changed after the autonomy checkpoint"
                    )
        for index, rendered in enumerate(prose_targets, start=1):
            apply_targets.append(
                PersistenceV2Target(
                    target_id=f"story-prose-{index:02d}",
                    kind=rendered.kind,
                    path_ref=self._event_path_ref(
                        rendered.path, registry=registry, preferred_root="story_project"
                    ),
                    content=rendered.content,
                    metadata={"story_target_index": rendered.target_index},
                    expected_before_exists=rendered.expected_before_exists,
                    expected_before_sha256=rendered.expected_before_sha256,
                )
            )
        for index, target in enumerate(memory_commit["targets"], start=1):
            apply_targets.append(
                PersistenceV2Target(
                    target_id=f"memory-{index:02d}",
                    kind=str(target["kind"]),
                    path_ref=self._event_path_ref(Path(target["path"]), registry=registry),
                    content=str(target["content"]),
                    metadata={"memory_v2": True},
                    expected_before_exists=bool(target["expected_before_exists"]),
                    expected_before_sha256=target.get("expected_before_sha256"),
                )
            )
        apply_targets.append(
            PersistenceV2Target(
                target_id="runtime-snapshot",
                kind="snapshot",
                path_ref=self._event_path_ref(
                    self.snapshot_path, registry=registry, preferred_root="snapshot"
                ),
                content=json.dumps(next_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                metadata={"chapter_index": run.get("chapter_index")},
                expected_before_exists=bool(snapshot_before["exists"]),
                expected_before_sha256=snapshot_before.get("sha256"),
            )
        )
        apply_targets.append(
            PersistenceV2Target(
                target_id="project-identity",
                kind="project_identity",
                path_ref=self._event_path_ref(
                    identity_path, registry=registry, preferred_root="story_project"
                ),
                content=identity_content,
                metadata={"authority_transition": True},
                expected_before_exists=True,
                expected_before_sha256=hashlib.sha256(identity_before).hexdigest(),
            )
        )

        prose_declarations = declared_read_set_writes(
            read_set,
            (
                (
                    rendered.path,
                    hashlib.sha256(rendered.content.encode("utf-8")).hexdigest(),
                    len(rendered.content.encode("utf-8")),
                )
                for rendered in prose_targets
            ),
        )
        identity_declaration = {
            "relative_path": ".novelagent/project.json",
            "role": "project_identity",
            "action": "replace",
            "after_sha256": hashlib.sha256(identity_bytes).hexdigest(),
            "after_size": len(identity_bytes),
            "book_id": identity.book_id,
            "expected_authority_epoch": int(authority["authority_epoch"]),
            "expected_head_event_hash": str(authority["head_event_hash"]),
            "after_authority_epoch": int(advanced_identity.authority["authority_epoch"]),
            "after_head_event_hash": str(advanced_identity.authority["head_event_hash"]),
        }
        declared_writes = [
            *autonomy_outline_declarations,
            *prose_declarations,
            identity_declaration,
        ]

        receipt_id = f"receipt-{run['id']}"
        receipt_path = self.run_dir / "publication_receipts" / f"{run['id']}.json"
        final_path = self.run_dir / f"{run['id']}.json"
        runtime_uuid = registry["roots"]["runtime"]["root_uuid"]
        receipt_ref = path_ref_for(
            receipt_path,
            root_id="runtime",
            root=root_map["runtime"],
            root_uuid=runtime_uuid,
        )
        final_ref = path_ref_for(
            final_path,
            root_id="runtime",
            root=root_map["runtime"],
            root_uuid=runtime_uuid,
        )

        preliminary = self._event_anticipated_persistence(
            str(run["id"]),
            apply_targets,
            receipt_id=receipt_id,
            receipt_path=receipt_path,
        )
        expected_writeback = finalize_story_project_writeback(
            plan, story_targets, preliminary
        ).to_dict()
        prepared_artifacts = self._prepare_event_publication_artifacts(
            result,
            snapshot_pack=snapshot_pack,
            input_pack=input_pack,
            chapter_pipeline=chapter_pipeline,
            validation=validation,
            workflow_trace=workflow_trace,
            review_repair=review_repair,
            writeback_plan=plan.to_dict(),
            writeback_result=expected_writeback,
        )
        if delivery_intent is not None:
            prepared_artifacts.append(
                {
                    "kind": DELIVERY_INTENT_ARTIFACT_KIND,
                    "path": self.run_dir
                    / "delivery_intents"
                    / f"{run['id']}.json",
                    "content": json.dumps(
                        delivery_intent,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    "root_id": "runtime",
                    "metadata": {
                        "intent_id": delivery_intent["intent_id"],
                        "policy": delivery_intent["policy"],
                    },
                }
            )
        if self.autonomy_run_context is not None:
            prepared_artifacts.extend(
                self.autonomy_run_context.publication_artifacts(
                    run_id=str(run["id"]),
                    output_root=self.run_dir / "autonomy_evidence",
                    chapter_body_sha256=chapter_body_sha256,
                )
            )
        artifact_targets = [
            PersistenceV2Target(
                target_id=f"artifact-{index:03d}",
                kind=str(target["kind"]),
                path_ref=self._event_path_ref(
                    Path(target["path"]), registry=registry, preferred_root=target.get("root_id")
                ),
                content=str(target["content"]),
                phase="publication",
                metadata=dict(target.get("metadata") or {}),
                expected_before_exists=False,
            )
            for index, target in enumerate(prepared_artifacts, start=1)
        ]
        anticipated = self._event_anticipated_persistence(
            str(run["id"]),
            [*apply_targets, *artifact_targets],
            receipt_id=receipt_id,
            receipt_path=receipt_path,
        )
        self._attach_persistence_payload(result, anticipated)
        expected_writeback = finalize_story_project_writeback(
            plan, story_targets, anticipated
        ).to_dict()
        expected_writeback["artifacts"] = run["story_project"]["writeback"].get(
            "artifacts", {}
        )
        run["story_project"]["writeback"] = expected_writeback

        bound_result = bind_final_run_record_receipt(
            result,
            receipt_id=receipt_id,
            receipt_path_ref=receipt_ref,
        )
        validate_run_result(bound_result)
        result.clear()
        result.update(bound_result)

        source_revision_after = {
            "schema_version": "1.0",
            "book_id": identity.book_id,
            "root_uuid": registry["roots"]["story_project"]["root_uuid"],
            "identity_sha256": identity_declaration["after_sha256"],
            "authority_epoch": identity_declaration["after_authority_epoch"],
            "head_event_hash": identity_declaration["after_head_event_hash"],
        }
        transaction = self.persistence_coordinator.create_transaction(
            run_id=str(run["id"]),
            book_id=identity.book_id,
            story_project_read_set=read_set,
            read_set_declared_writes=declared_writes,
        )
        delivery_jobs = (
            [delivery_intent_receipt_binding(delivery_intent)]
            if delivery_intent is not None
            else []
        )
        authority_operation = self._event_authority_operation
        if authority_operation is None:
            raise PersistenceError(
                "event-authority transaction is outside the StoryProject recovery barrier"
            )
        authority_operation.prepare_transaction(
            transaction,
            apply_targets=apply_targets,
            artifacts=artifact_targets,
            final_run_record=result,
            final_run_path_ref=final_ref,
            receipt_id=receipt_id,
            receipt_path_ref=receipt_ref,
            context_digest=str(read_set["context_digest"]),
            generation_input_context_digest=hashlib.sha256(
                input_pack.encode("utf-8")
            ).hexdigest(),
            story_project_source_revision_after=source_revision_after,
            candidate_result=result,
            delivery_jobs=delivery_jobs,
        )
        committed = authority_operation.commit_transaction(transaction)
        if not committed.get("committed") or committed.get("state") != "completed":
            raise PersistenceError(
                f"event-authority persistence did not produce a PublicationReceipt: {committed}"
            )
        verification = verify_publication_receipt(receipt_path, root_map=root_map)
        if not verification.get("valid") or not verification.get("committed"):
            raise PersistenceError(
                "event-authority PublicationReceipt failed durable verification"
            )
        if delivery_intent is not None:
            if self.delivery_queue is None:
                raise PersistenceError(
                    "event-authority file delivery queue disappeared after commit"
                )
            recover_delivery_jobs_for_receipt(
                receipt_path,
                root_map=root_map,
                queue=self.delivery_queue,
            )

    def _event_path_ref(
        self,
        path: Path,
        *,
        registry: dict[str, Any],
        preferred_root: str | None = None,
    ):
        root_map = self._event_authority_root_map or {}
        order = [preferred_root] if preferred_root else []
        order.extend(
            root_id
            for root_id in (
                "snapshot",
                "chapter_artifacts",
                "delivery_store",
                "runtime",
                "story_project",
            )
            if root_id not in order
        )
        for root_id in order:
            if root_id is None or root_id not in root_map:
                continue
            try:
                return path_ref_for(
                    path,
                    root_id=root_id,
                    root=root_map[root_id],
                    root_uuid=registry["roots"][root_id]["root_uuid"],
                )
            except ValueError:
                continue
        raise PersistencePreparationError(f"event-authority target is outside trusted roots: {path}")

    def _event_anticipated_persistence(
        self,
        run_id: str,
        targets: list[PersistenceV2Target],
        *,
        receipt_id: str,
        receipt_path: Path,
    ) -> dict[str, Any]:
        journal = self.persistence_dir / "journals" / run_id
        return {
            "run_id": run_id,
            "state": "completed",
            "committed": True,
            "partial": False,
            "journal_path": str(journal),
            "commit_marker": str(journal / "commit.marker"),
            "targets": [
                {
                    "kind": target.kind,
                    "path": str(self._event_path_ref_target_path(target)),
                    "status": "verified",
                    "metadata": dict(target.metadata),
                }
                for target in targets
            ],
            "errors": [],
            "candidate_result_path": str(journal / "candidate_result.json"),
            "publication": {
                "status": "receipt_backed",
                "receipt_id": receipt_id,
                "receipt_path": str(receipt_path),
            },
        }

    def _event_path_ref_target_path(self, target: PersistenceV2Target) -> Path:
        root_map = self._event_authority_root_map or {}
        ref = target.path_ref
        root_id = ref.root_id if hasattr(ref, "root_id") else str(ref["root_id"])
        relative = ref.relative_path if hasattr(ref, "relative_path") else str(ref["relative_path"])
        return Path(root_map[root_id]) / Path(relative)

    def _prepare_event_publication_artifacts(
        self,
        result: dict[str, Any],
        *,
        snapshot_pack: str,
        input_pack: str,
        chapter_pipeline: dict[str, Any] | None,
        validation: dict[str, Any],
        workflow_trace: list[dict[str, Any]],
        review_repair: dict[str, Any] | None,
        writeback_plan: dict[str, Any],
        writeback_result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        run = result["run"]
        prepared: list[tuple[str, dict[str, Any], str]] = []
        chapter = prepare_chapter_artifact(
            chapter_text=str(result.get("chapter") or ""),
            run=run,
            output_dir=self.chapter_dir,
        )
        run["chapter"]["artifact"] = chapter["metadata"]
        prepared.append(("chapter_artifact", chapter, "chapter_artifacts"))

        snapshot = prepare_snapshot_pack_artifact(
            snapshot_pack=snapshot_pack,
            run=run,
            output_dir=self.run_dir / "snapshot_packs",
        )
        run["snapshot_builder"]["artifact"] = snapshot["metadata"]
        prepared.append(("snapshot_pack", snapshot, "runtime"))
        input_artifact = prepare_input_pack_artifact(
            input_pack=input_pack,
            run=run,
            output_dir=self.run_dir / "input_packs",
        )
        run["input_pack"]["artifact"] = input_artifact["metadata"]
        prepared.append(("input_pack", input_artifact, "runtime"))

        if isinstance(chapter_pipeline, dict):
            pipeline = prepare_chapter_pipeline_artifacts(
                pipeline=chapter_pipeline,
                validation=validation,
                repair_deltas=_trace_repair_deltas(workflow_trace),
                run=run,
                output_dir=self.run_dir / "chapter_pipeline",
            )
            run.setdefault("chapter", {}).setdefault("pipeline", {})[
                "artifacts"
            ] = pipeline["metadata"]
            prepared.append(("chapter_pipeline", pipeline, "runtime"))
        if isinstance(review_repair, dict) and review_repair.get("attempted"):
            repair = prepare_review_repair_artifacts(
                review_repair=review_repair,
                run=run,
                output_dir=self.run_dir / "review_repairs",
            )
            with_artifacts = dict(review_repair)
            with_artifacts["artifacts"] = repair["metadata"]
            public = _review_repair_run_payload(with_artifacts)
            run["review_repair"] = public
            result["review_repair"] = public
            prepared.append(("review_repair", repair, "runtime"))

        writeback = prepare_story_project_writeback_artifacts(
            plan=writeback_plan,
            result=writeback_result,
            run=run,
            output_dir=self.run_dir / "story_project_writebacks",
        )
        writeback_result = dict(writeback_result)
        writeback_result["artifacts"] = writeback["metadata"]
        run.setdefault("story_project", {})["writeback"] = writeback_result
        prepared.append(("story_project_writeback", writeback, "runtime"))

        targets: list[dict[str, Any]] = []
        for kind, bundle, root_id in prepared:
            for target in bundle["targets"]:
                targets.append(
                    {
                        "kind": kind,
                        "path": target["path"],
                        "content": target["content"],
                        "root_id": root_id,
                        "metadata": {},
                    }
                )
        return targets

    def _prepare_strict_memory_v2_commit(
        self,
        *,
        run: dict[str, Any],
        analysis: dict[str, Any],
        runtime_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        context = self._story_project_context_dict()
        if not isinstance(context, dict) or context.get("story_state_mode") != "strict":
            return None
        identity = context.get("project_identity")
        semantic = context.get("semantic_state")
        read_set = context.get("read_set")
        if not isinstance(identity, dict) or not isinstance(semantic, dict) or not isinstance(read_set, dict):
            raise PersistenceError("strict Memory V2 commit requires identity, semantic state, and read set")
        root = Path(str(context["story_project_root"]))
        memory_root = RuntimePaths.for_story_project(root).memory_dir / "v2"
        ensure_memory_v2_storage_layout(memory_root)
        source_digest = str(semantic.get("source_digest") or "")
        context_digest = str(read_set.get("context_digest") or "")
        quality = run.get("quality_decision") if isinstance(run.get("quality_decision"), dict) else {}
        blueprint = context.get("chapter_blueprint") if isinstance(context.get("chapter_blueprint"), dict) else {}
        return prepare_chapter_memory_commit(
            memory_root=memory_root,
            book_id=str(identity.get("book_id") or ""),
            run_id=str(run.get("id") or ""),
            chapter_index=int(run.get("chapter_index") or 0),
            analysis=analysis,
            source_project_digest=source_digest,
            context_digest=context_digest,
            quality_state={
                "accepted": bool(run.get("accepted")),
                "policy": quality.get("policy"),
                "decision_id": quality.get("decision_id"),
            },
            title=str(blueprint.get("title") or "Untitled"),
            language=project_language(runtime_snapshot) or "zh-CN",
        )

    def _anticipated_persistence(
        self,
        run_id: str,
        targets: list[PersistenceTarget],
        *,
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        return self.persistence_coordinator.anticipated(run_id, targets, publication=publication)

    def _attach_persistence_payload(self, result: dict[str, Any], payload: dict[str, Any]) -> None:
        self.persistence_coordinator.attach(result, payload)

    def _attach_story_project_writeback_payload(
        self,
        result: dict[str, Any],
        plan: dict[str, Any],
        payload: dict[str, Any],
        *,
        publish_artifacts: bool = True,
    ) -> None:
        run = result["run"]
        payload = dict(payload)
        if publish_artifacts:
            payload["artifacts"] = save_story_project_writeback_artifacts(
                plan=plan,
                result=payload,
                run=run,
                output_dir=self.run_dir / "story_project_writebacks",
            )
        story_project = run.setdefault("story_project", {})
        story_project["writeback"] = payload

    def _mark_result_preview(
        self,
        result: dict[str, Any],
        *,
        runtime_snapshot: dict[str, Any],
        next_snapshot: dict[str, Any],
        analysis: dict[str, Any],
    ) -> None:
        result["committed"] = False
        result["run"]["committed"] = False
        result["run"]["status"] = "preview"
        result["run"]["snapshot"]["next_chapter_index"] = runtime_snapshot.get("chapter_index")
        result["run"]["state_update"] = build_state_update_audit(
            snapshot=runtime_snapshot,
            next_snapshot=next_snapshot,
            analysis=analysis,
            memory_updates=[],
            applied=False,
        )
        result["state_update"] = result["run"]["state_update"]
        self._attach_persistence_payload(
            result,
            {
                "run_id": result["run"]["id"],
                "state": "preview",
                "committed": False,
                "partial": False,
                "targets": [],
                "errors": [],
            },
        )

    def _mark_result_persistence_failed(
        self,
        result: dict[str, Any],
        *,
        base_snapshot: dict[str, Any],
        runtime_snapshot: dict[str, Any],
        next_snapshot: dict[str, Any],
        analysis: dict[str, Any],
        persistence: dict[str, Any],
    ) -> None:
        result["committed"] = False
        result["run"]["committed"] = False
        result["run"]["status"] = "failed"
        result["snapshot"] = base_snapshot
        result["run"]["snapshot"]["next_chapter_index"] = runtime_snapshot.get("chapter_index")
        result["run"]["state_update"] = build_state_update_audit(
            snapshot=runtime_snapshot,
            next_snapshot=next_snapshot,
            analysis=analysis,
            memory_updates=[],
            applied=False,
        )
        result["state_update"] = result["run"]["state_update"]
        self._attach_persistence_payload(result, persistence)

    def _attach_snapshot_pack_artifact(self, run: dict[str, Any], snapshot_pack: str) -> None:
        artifact = save_snapshot_pack_artifact(
            snapshot_pack=snapshot_pack,
            run=run,
            output_dir=self.run_dir / "snapshot_packs",
        )
        run["snapshot_builder"]["artifact"] = artifact

    def _attach_execution_artifacts(
        self,
        result: dict[str, Any],
        *,
        snapshot_pack: str,
        input_pack: str,
        chapter_pipeline: dict[str, Any] | None,
        validation: dict[str, Any] | None,
        workflow_trace: list[dict[str, Any]],
        review_repair: dict[str, Any] | None,
    ) -> None:
        run = result["run"]
        self._attach_snapshot_pack_artifact(run, snapshot_pack)
        run["input_pack"]["artifact"] = save_input_pack_artifact(
            input_pack=input_pack,
            run=run,
            output_dir=self.run_dir / "input_packs",
        )
        self._attach_chapter_pipeline_artifacts(
            run,
            chapter_pipeline,
            validation,
            _trace_repair_deltas(workflow_trace),
        )
        self._attach_review_repair_artifacts(result, review_repair)

    def _attach_chapter_artifact(self, result: dict[str, Any]) -> None:
        chapter = result.get("chapter")
        run = result.get("run")
        if not isinstance(chapter, str) or not chapter.strip() or not isinstance(run, dict):
            return
        artifact = save_chapter_artifact(
            chapter_text=chapter,
            run=run,
            output_dir=self.chapter_dir,
        )
        run["chapter"]["artifact"] = artifact

    def _attach_chapter_pipeline_artifacts(
        self,
        run: dict[str, Any],
        chapter_pipeline: dict[str, Any] | None,
        validation: dict[str, Any] | None,
        repair_deltas: list[dict[str, Any]] | None,
    ) -> None:
        if not isinstance(chapter_pipeline, dict):
            return
        artifacts = save_chapter_pipeline_artifacts(
            pipeline=chapter_pipeline,
            validation=validation,
            repair_deltas=repair_deltas,
            run=run,
            output_dir=self.run_dir / "chapter_pipeline",
        )
        chapter = run.setdefault("chapter", {})
        pipeline_summary = chapter.setdefault("pipeline", {})
        pipeline_summary["artifacts"] = artifacts

    def _attach_review_index(self, result: dict[str, Any]) -> None:
        run = result.get("run")
        if not isinstance(run, dict):
            return
        review = run.get("review_pipeline")
        if not isinstance(review, dict):
            return
        gate = run.get("review_gate") if isinstance(run.get("review_gate"), dict) else None
        output_dir = self.review_config.output_dir or Path(".tmp/runtime/reviews")
        index_path = review_index_path(output_dir)
        try:
            entry = build_review_index_entry(
                run_id=str(run["id"]),
                review_pipeline=review,
                review_gate=gate,
                artifacts_dir=review.get("artifacts_dir"),
            )
            index = update_review_index(review_output_dir=output_dir, entry=entry)
            summary = {
                "enabled": True,
                "status": "updated",
                "index_path": str(index_path),
                "latest_run_id": index["latest_run_id"],
                "entry_count": index["summary"]["entry_count"],
            }
        except Exception as exc:  # noqa: BLE001 - review index is diagnostic; generation remains intact.
            summary = {
                "enabled": True,
                "status": "error",
                "index_path": str(index_path),
                "latest_run_id": None,
                "entry_count": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        run["review_index"] = summary
        result["review_index"] = summary


def _capture_execution_provenance_cached(
    repository_root: Path,
    *,
    provider: str,
    model: str,
    config: dict[str, Any],
    feature_flags: dict[str, bool],
) -> ExecutionProvenance:
    resolved_root = repository_root.resolve(strict=True)
    key = (
        str(resolved_root),
        provider,
        model,
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        json.dumps(feature_flags, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    with _PROVENANCE_CACHE_LOCK:
        cached = _PROVENANCE_CACHE.get(key)
        if cached is not None:
            return cached
        captured = capture_execution_provenance(
            resolved_root,
            provider=provider,
            model=model,
            config=config,
            feature_flags=feature_flags,
        )
        _PROVENANCE_CACHE[key] = captured
        return captured


def _unresolved_provider_calls(run_dir: Path) -> list[dict[str, Any]]:
    executions_root = run_dir / "executions"
    if not executions_root.is_dir():
        return []
    unresolved: list[dict[str, Any]] = []
    for execution_dir in sorted(executions_root.glob("execution_*")):
        model_root = execution_dir / "model_calls"
        if not model_root.is_dir():
            continue
        unresolved.extend(ModelCallStore(model_root).list_uncertain_calls())
    return sorted(
        unresolved,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("attempt_id") or "")),
    )


def _chapter_evidence_spans(
    chapter_body: str,
    analysis: dict[str, Any],
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    if not chapter_body:
        raise PersistencePreparationError(
            "event-authority memory evidence requires a non-empty chapter body"
        )
    candidates: list[str] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 5 or len(candidates) >= limit * 8:
            return
        if isinstance(value, str):
            text = value.strip()
            if 2 <= len(text) <= 160:
                candidates.append(text)
            return
        if isinstance(value, dict):
            for key in sorted(value):
                collect(value[key], depth + 1)
            return
        if isinstance(value, list):
            for item in value:
                collect(item, depth + 1)

    collect(analysis)
    spans: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for text in candidates:
        start = chapter_body.find(text)
        if start < 0:
            continue
        end = start + len(text)
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        spans.append({"start": start, "end": end, "quote": chapter_body[start:end]})
        if len(spans) >= limit:
            break
    if not spans:
        start = next(
            (index for index, char in enumerate(chapter_body) if not char.isspace()),
            0,
        )
        end = min(len(chapter_body), start + 80)
        while end > start and chapter_body[end - 1].isspace():
            end -= 1
        if end <= start:
            raise PersistencePreparationError(
                "event-authority memory evidence could not locate chapter text"
            )
        spans.append(
            {"start": start, "end": end, "quote": chapter_body[start:end]}
        )
    return spans


def run_once(*, dry_run: bool = False, persist: bool = True, enable_llm_validator: bool = False) -> dict[str, Any]:
    return AgentExecutor(dry_run=dry_run, enable_llm_validator=enable_llm_validator).run_once(persist=persist)


def run_loop(
    *,
    steps: int,
    dry_run: bool = False,
    persist: bool = True,
    stop_on_rejection: bool = True,
    enable_llm_validator: bool = False,
) -> dict[str, Any]:
    return AgentExecutor(dry_run=dry_run, enable_llm_validator=enable_llm_validator).run_loop(
        steps=steps,
        persist=persist,
        stop_on_rejection=stop_on_rejection,
    )


def _synthetic_repaired_scene_drafts(chapter_blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    beat_indexes = []
    for position, beat in enumerate(chapter_blueprint.get("required_beats") or [], start=1):
        if isinstance(beat, dict):
            raw_index = beat.get("index")
            index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else position
        else:
            index = position
        beat_indexes.append(int(index))
    return [
        {
            "index": 1,
            "goal": "Post-review repaired chapter coverage check.",
            "covered_beat_indexes": beat_indexes,
        }
    ]


def _review_repair_run_payload(review_repair: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in review_repair.items()
        if key not in {"final_chapter"}
    }


def _review_repair_rejected_validation(validation: dict[str, Any], *, reason: str) -> dict[str, Any]:
    existing_checks = validation.get("executed_checks")
    executed_checks = list(existing_checks) if isinstance(existing_checks, list) and existing_checks else ["logic"]
    problem = {
        "code": reason,
        "message": f"Review repair did not produce an accepted chapter: {reason}.",
        "validator": "review_repair",
        "severity": "high",
        "blocking": True,
        "category": "blocking",
        "repair_hint": "Inspect review repair artifacts and revise the chapter manually or rerun with adjusted input.",
        "repair_action": "manual_review",
        "repair_parameters": {},
        "evidence": [{"kind": "review_repair", "value": reason}],
    }
    return validate_schema(
        {
            "ok": False,
            "requested_focus": list(validation.get("requested_focus") or ["logic"]),
            "executed_checks": executed_checks,
            "skipped_checks": list(validation.get("skipped_checks") or []),
            "checks": [{"name": "review_repair", "ok": False, "problems": [problem]}],
            "problems": [problem],
            "blocking_problem_count": 1,
            "warning_count": 0,
            "severity_counts": [{"severity": "high", "count": 1}],
            "deterministic_repair_count": 0,
            "manual_review_count": 1,
            "repair_action_counts": [{"action": "manual_review", "count": 1}],
        },
        "validation_result.schema.json",
    )


def _notify_loop(observer: LoopObserver | None, event: dict[str, Any]) -> None:
    if observer is None:
        return
    observer(event)


def _repair_context_for_snapshot(snapshot: dict[str, Any]) -> RepairContext:
    hint: str | None = None
    world_state = snapshot.get("world_state") if isinstance(snapshot.get("world_state"), dict) else {}
    story_state = snapshot.get("story_state") if isinstance(snapshot.get("story_state"), dict) else {}
    for collection in (world_state.get("active_conflicts"), story_state.get("open_threads")):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if isinstance(item, dict):
                value = item.get("description") or item.get("name") or item.get("title")
            else:
                value = item
            if isinstance(value, str) and value.strip():
                hint = value.strip()
                break
        if hint:
            break
    return RepairContext(
        language=project_language(snapshot) or "en",
        known_conflict_hint=hint,
    )


def _previous_chapter_text(
    memory_context: dict[str, Any],
    *,
    story_project_context: dict[str, Any] | None = None,
    chapter_index: int | None = None,
    run_dir: str | Path | None = None,
    chapter_artifact_root: str | Path | None = None,
) -> str | None:
    previous = (
        story_project_context.get("previous_chapter_context")
        if isinstance(story_project_context, dict)
        else None
    )
    review_tail = previous.get("review_tail") if isinstance(previous, dict) else None
    text = review_tail.get("text") if isinstance(review_tail, dict) else None
    if isinstance(text, str) and text.strip():
        return text
    last_run = memory_context.get("last_run")
    if not isinstance(last_run, dict):
        last_run = {}
    if chapter_index is None or chapter_index <= 1:
        return None
    last_run_verified = (
        last_run.get("committed") is True
        and last_run.get("status") == "committed"
        and last_run.get("chapter_index") == chapter_index - 1
        and last_run.get("artifact_hash_verified") is True
    )
    if last_run_verified:
        for key in ("chapter_text", "chapter", "draft_text"):
            value = last_run.get(key)
            if isinstance(value, str) and value.strip():
                expected_hash = last_run.get("chapter_text_sha256")
                actual_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
                if expected_hash == actual_hash:
                    return value
                break
    if run_dir is not None and chapter_artifact_root is not None:
        try:
            fallback = resolve_committed_previous_chapter_artifact(
                chapter_index=chapter_index,
                run_dir=run_dir,
                chapter_artifact_root=chapter_artifact_root,
            )
        except ChapterContextError:
            return None
        return fallback.review_tail["text"] if fallback is not None else None
    return None


def _review_gate_allows_commit(gate: dict[str, Any] | None) -> bool:
    if not isinstance(gate, dict) or not gate.get("enabled"):
        return True
    return gate.get("status") == "pass" and int(gate.get("exit_code") or 0) == 0


def _loop_local_run_summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    validation = run.get("validation") if isinstance(run.get("validation"), dict) else {}
    decision = run.get("decision") if isinstance(run.get("decision"), dict) else {}
    return {
        "id": run.get("id"),
        "status": run.get("status"),
        "accepted": run.get("accepted"),
        "committed": run.get("committed"),
        "chapter_index": run.get("chapter_index"),
        "chapter_text": result.get("chapter"),
        "goal": decision.get("goal"),
        "workflow": run.get("workflow", []),
        "problem_codes": validation.get("problem_codes", []),
        "problem_count": validation.get("problem_count", 0),
        "blocking_problem_count": validation.get("blocking_problem_count", 0),
        "warning_count": validation.get("warning_count", 0),
        "requested_focus": validation.get("requested_focus", []),
        "executed_checks": validation.get("executed_checks", []),
        "skipped_checks": validation.get("skipped_checks", []),
        "repair_attempts": run.get("repair_attempts", 0),
    }


def _result_failure_reasons(result: dict[str, Any]) -> list[str]:
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    reasons: list[str] = []
    status = str(run.get("status") or "")
    if status == "rejected":
        reasons.append("run_rejected")
    elif status == "failed":
        reasons.append("run_failed")
    gate = run.get("review_gate") if isinstance(run.get("review_gate"), dict) else {}
    if gate.get("status") in {"fail", "error"}:
        reasons.append("review_gate_failed")
    story_project = run.get("story_project") if isinstance(run.get("story_project"), dict) else {}
    writeback = story_project.get("writeback") if isinstance(story_project.get("writeback"), dict) else {}
    if writeback.get("attempted") and not writeback.get("dry_run"):
        if not writeback.get("applied") or writeback.get("partial"):
            reasons.append("story_project_writeback_failed")
    memory = run.get("memory") if isinstance(run.get("memory"), dict) else {}
    memory_writeback = memory.get("writeback") if isinstance(memory.get("writeback"), dict) else {}
    verification = memory_writeback.get("verification") if isinstance(memory_writeback.get("verification"), dict) else {}
    if verification.get("status") in {"failed", "error"}:
        reasons.append("memory_delivery_failed")
    return list(dict.fromkeys(reasons))


def _memory_writer_target(writer: MemoryWriter | None) -> str | None:
    if writer is None:
        return None
    if hasattr(writer, "path"):
        return "file"
    if hasattr(writer, "database_id"):
        return "notion"
    return None


def _persistence_publication(
    *,
    chapter_artifact: dict[str, Any],
    memory_updates: list[dict[str, Any]],
    writeback_gate: dict[str, Any],
    writer: MemoryWriter | None,
) -> dict[str, Any]:
    target = _memory_writer_target(writer)
    allowed = bool(writeback_gate.get("allowed"))
    memory: dict[str, Any] = {
        "target": target,
        "status": "not_applicable",
        "update_count": len(memory_updates),
        "gate": dict(writeback_gate),
    }
    if target == "file" and allowed and memory_updates:
        memory.update(
            {
                "status": "pending",
                "path": str(Path(getattr(writer, "path")).resolve()),
                "updates": [dict(item) for item in memory_updates],
            }
        )
    elif target == "notion" and allowed and memory_updates:
        memory["status"] = "external_pending"
    elif not allowed:
        memory["status"] = "blocked"
    return {"chapter_artifact": dict(chapter_artifact), "memory_outbox": memory}


def _publication_with_memory_status(publication: dict[str, Any], status: str) -> dict[str, Any]:
    updated = dict(publication)
    memory = dict(updated.get("memory_outbox") or {})
    memory["status"] = status
    updated["memory_outbox"] = memory
    return updated


def _set_memory_publication_status(result: dict[str, Any], status: str) -> None:
    for persistence in (
        result.get("persistence"),
        (result.get("run") or {}).get("persistence") if isinstance(result.get("run"), dict) else None,
    ):
        if not isinstance(persistence, dict):
            continue
        publication = persistence.get("publication")
        if isinstance(publication, dict):
            persistence["publication"] = _publication_with_memory_status(publication, status)


def _capture_file_version(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {"exists": False, "sha256": None}
    return {"exists": True, "sha256": hashlib.sha256(content).hexdigest()}


def _loop_step_timing(
    step: int,
    started_at,
    result: dict[str, Any] | None,
    *,
    error: BaseException | None = None,
) -> dict[str, Any]:
    finished_at = utc_now()
    run = result.get("run") if isinstance(result, dict) else None
    run = run if isinstance(run, dict) else {}
    status = str(run.get("status") or "failed")
    duration_ms = int(max(0.0, (finished_at - started_at).total_seconds() * 1000))
    timing: dict[str, Any] = {
        "step": int(step),
        "status": status if status in {"preview", "committed", "rejected", "failed"} else "failed",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "run_id": run.get("id"),
        "chapter_index": run.get("chapter_index"),
        "committed": result.get("committed") if isinstance(result, dict) else None,
        "error_type": type(error).__name__ if error is not None else None,
    }
    return timing


def _empty_analysis(validation: dict[str, Any]) -> dict[str, Any]:
    return validate_schema({
        "events": [],
        "character_changes": [],
        "world_changes": [],
        "new_locations": [],
        "story_state": {
            "last_chapter_ending": "",
            "last_scene_location": "",
            "last_scene_characters": [],
            "open_threads": [],
            "required_opening_bridge": "",
        },
        "spatial_state": {
            "spaces": {},
            "connections": [],
            "character_positions": {},
            "blocked_paths": [],
            "last_transition": {},
        },
        "conflicts": [],
        "validation_ok": bool(validation.get("ok")),
        "summary": "",
    }, "analysis_result.schema.json")


def _require_chapter(state: dict[str, Any]) -> str:
    chapter = state.get("chapter")
    if not isinstance(chapter, str) or not chapter:
        raise ValueError("Workflow action requires chapter text")
    return chapter


def _repairer_accepts_plan(repairer: ChapterRepairer) -> bool:
    positional_count = _repairer_positional_count(repairer)
    return positional_count is None or positional_count >= 4


def _repairer_accepts_recovery_context(repairer: ChapterRepairer) -> bool:
    positional_count = _repairer_positional_count(repairer)
    return positional_count is None or positional_count >= 5


def _repairer_positional_count(repairer: ChapterRepairer) -> int | None:
    try:
        signature = inspect.signature(repairer)
    except (TypeError, ValueError):
        return 0
    positional_count = 0
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return None
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional_count += 1
    return positional_count


def _trace_event(
    action: str,
    started_at,
    finished_at,
    state: dict[str, Any],
    *,
    planned_step: dict[str, Any] | None = None,
    model_trace: dict[str, Any] | None = None,
    provider_attempts: list[dict[str, Any]] | None = None,
    implicit: bool = False,
    status: str = "completed",
    error: BaseException | None = None,
) -> dict[str, Any]:
    chapter = state.get("chapter")
    validation = state.get("validation")
    duration_ms = int(max(0.0, (finished_at - started_at).total_seconds() * 1000))
    event: dict[str, Any] = {
        "action": action,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "chapter_chars": len(chapter) if isinstance(chapter, str) else 0,
        "repair_attempts": int(state.get("repair_attempts") or 0),
    }
    if isinstance(planned_step, dict):
        event["plan_step_index"] = planned_step.get("index")
        event["plan_step_mode"] = planned_step.get("mode")
        event["plan_failure_policy"] = planned_step.get("failure_policy")
    if isinstance(model_trace, dict):
        event.update(model_trace)
    if provider_attempts:
        event["provider_attempts"] = [dict(report) for report in provider_attempts]
    if isinstance(validation, dict):
        problems = validation.get("problems", [])
        event["validation_ok"] = bool(validation.get("ok"))
        event["problem_count"] = len(problems) if isinstance(problems, list) else 0
    if isinstance(state.get("repair_plan"), dict):
        event["repair_plan"] = state["repair_plan"]
    if isinstance(state.get("repair_deltas"), list) and state["repair_deltas"]:
        event["repair_deltas"] = state["repair_deltas"]
    if isinstance(state.get("bridge_precheck"), dict):
        event["bridge_precheck"] = state["bridge_precheck"]
    if action == "repair_if_needed":
        skip_reason = state.get("workflow_skip_reason")
        event["skipped"] = bool(skip_reason)
        if skip_reason:
            event["skip_reason"] = str(skip_reason)
    if implicit:
        event["implicit"] = True
    if error is not None:
        event["error_type"] = type(error).__name__
        event["error_message"] = str(error)
        if hasattr(error, "to_dict"):
            event["model_call"] = error.to_dict()
    return validate_schema(event, "trace_event.schema.json")


def _model_trace(
    stage: str,
    *,
    provider: str | None,
    model: str | None,
    invocation: str,
) -> dict[str, Any]:
    return {
        "model_stage": stage,
        "model_provider": provider,
        "model_name": model,
        "model_invocation": invocation,
    }


def _workflow_steps_by_action(workflow_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    steps = workflow_plan.get("steps")
    if not isinstance(steps, list):
        return {}
    return {
        str(step.get("action")): step
        for step in steps
        if isinstance(step, dict) and step.get("action")
    }


def _can_continue_after_polish_error(action: str, state: dict[str, Any], exc: BaseException) -> bool:
    if action != "polish" or not _has_generated_chapter(state):
        return False
    if isinstance(exc, ModelOutputError):
        return True
    return isinstance(exc, ModelCallError) and exc.provider == "anthropic" and exc.stage == "claude_polish"


def _has_generated_chapter(state: dict[str, Any]) -> bool:
    chapter = state.get("chapter")
    return isinstance(chapter, str) and bool(chapter.strip())


def _repair_delta(*, attempt: int, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_codes = _problem_codes(before)
    after_codes = _problem_codes(after)
    before_set = set(before_codes)
    after_set = set(after_codes)
    return {
        "attempt": attempt,
        "before_ok": bool(before.get("ok")),
        "after_ok": bool(after.get("ok")),
        "before_problem_count": _problem_count(before),
        "after_problem_count": _problem_count(after),
        "before_problem_codes": before_codes,
        "after_problem_codes": after_codes,
        "resolved_problem_codes": sorted(before_set - after_set),
        "new_problem_codes": sorted(after_set - before_set),
        "remaining_problem_codes": sorted(before_set & after_set),
    }


def _problem_count(validation: dict[str, Any]) -> int:
    explicit = validation.get("problem_count")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    problems = validation.get("problems", [])
    return len(problems) if isinstance(problems, list) else 0


def _int_value(value: Any, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _problem_codes(validation: dict[str, Any]) -> list[str]:
    raw_codes = validation.get("problem_codes")
    if isinstance(raw_codes, list):
        return [str(code) for code in raw_codes if code]
    problems = validation.get("problems", [])
    if not isinstance(problems, list):
        return []
    codes: list[str] = []
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        code = problem.get("code")
        if code:
            codes.append(str(code))
    return codes


def _memory_writeback_gate(
    *,
    committed: bool,
    validation: dict[str, Any],
    workflow_trace: list[dict[str, Any]],
    memory_updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not committed:
        reasons.append("not_committed")
    if not bool(validation.get("ok")):
        reasons.append("validation_not_ok")

    deltas = _trace_repair_deltas(workflow_trace)
    final_delta = deltas[-1] if deltas else None
    if final_delta:
        if _int_value(final_delta.get("after_problem_count"), default=0) > 0:
            reasons.append("repair_after_problems_remaining")
        if final_delta.get("new_problem_codes"):
            reasons.append("repair_introduced_new_problem_codes")
        if final_delta.get("remaining_problem_codes"):
            reasons.append("repair_left_remaining_problem_codes")

    return {
        "allowed": not reasons,
        "reasons": reasons,
        "pending_update_count": len(memory_updates or []),
        "pending_update_types": _memory_update_type_counts(memory_updates or []),
        "repair_attempt_count": len(deltas),
        "final_repair_delta": final_delta,
    }


def _memory_update_type_counts(memory_updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for update in memory_updates:
        if not isinstance(update, dict):
            continue
        update_type = update.get("type")
        if not update_type:
            continue
        key = str(update_type)
        counts[key] = counts.get(key, 0) + 1
    order = ["world_state", "story_state", "spatial_state", "location", "character", "constraint", "timeline_event"]
    return [{"type": item_type, "count": counts[item_type]} for item_type in order if item_type in counts]


def _trace_repair_deltas(workflow_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for event in workflow_trace:
        if not isinstance(event, dict):
            continue
        raw_deltas = event.get("repair_deltas")
        if not isinstance(raw_deltas, list):
            continue
        deltas.extend(delta for delta in raw_deltas if isinstance(delta, dict))
    return deltas


def _finalize_chapter_pipeline(
    chapter_pipeline: dict[str, Any] | None,
    *,
    validation: dict[str, Any] | None,
    repair_deltas: list[dict[str, Any]] | None,
    workflow_trace: list[dict[str, Any]],
    committed: bool,
    commit_status: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(chapter_pipeline, dict):
        return None

    existing_stages = {
        str(stage.get("name")): stage
        for stage in chapter_pipeline.get("stages", [])
        if isinstance(stage, dict) and stage.get("name")
    }
    deltas = repair_deltas or []
    stage_overrides = {
        "validate": {
            "status": _validation_stage_status(validation, workflow_trace),
            "artifact_key": "validation_report",
            "summary": _validation_stage_summary(validation),
        },
        "repair": {
            "status": _repair_stage_status(deltas, workflow_trace),
            "artifact_key": "repair_deltas",
            "summary": _repair_stage_summary(deltas),
        },
        "commit": {
            "status": commit_status or ("completed" if committed else "skipped"),
            "artifact_key": "chapter",
            "summary": {"committed": bool(committed)},
        },
    }
    stages: list[dict[str, Any]] = []
    for name in PIPELINE_STAGE_NAMES:
        stage = dict(existing_stages.get(name, {"name": name, "status": "pending"}))
        override = stage_overrides.get(name)
        if override:
            stage.update(override)
        stages.append(stage)

    finalized = dict(chapter_pipeline)
    finalized["stages"] = stages
    return validate_schema(finalized, "chapter_pipeline.schema.json")


def _validation_stage_status(validation: dict[str, Any] | None, workflow_trace: list[dict[str, Any]]) -> str:
    event = _latest_trace_event(workflow_trace, "validate")
    if event and event.get("status") == "failed":
        return "failed"
    if isinstance(validation, dict):
        return "completed"
    return "pending"


def _validation_stage_summary(validation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(validation, dict):
        return {"ok": False, "problem_count": 0}
    problems = validation.get("problems")
    problem_count = len(problems) if isinstance(problems, list) else 0
    return {
        "ok": bool(validation.get("ok")),
        "problem_count": problem_count,
        "blocking_count": _int_value(validation.get("blocking_count"), default=0),
        "warning_count": _int_value(validation.get("warning_count"), default=0),
    }


def _repair_stage_status(repair_deltas: list[dict[str, Any]], workflow_trace: list[dict[str, Any]]) -> str:
    event = _latest_trace_event(workflow_trace, "repair_if_needed")
    if event and event.get("status") == "failed":
        return "failed"
    if repair_deltas:
        return "completed"
    if event and event.get("skipped"):
        return "skipped"
    if event:
        return "completed"
    return "skipped"


def _repair_stage_summary(repair_deltas: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"attempt_count": len(repair_deltas)}
    if repair_deltas:
        final_delta = repair_deltas[-1]
        summary.update(
            {
                "after_ok": bool(final_delta.get("after_ok")),
                "resolved_problem_codes": final_delta.get("resolved_problem_codes", []),
                "new_problem_codes": final_delta.get("new_problem_codes", []),
                "remaining_problem_codes": final_delta.get("remaining_problem_codes", []),
            }
        )
    return summary


def _latest_trace_event(workflow_trace: list[dict[str, Any]], action: str) -> dict[str, Any] | None:
    for event in reversed(workflow_trace):
        if isinstance(event, dict) and event.get("action") == action:
            return event
    return None


def _blocked_memory_writeback(gate: dict[str, Any]) -> dict[str, Any]:
    return validate_memory_writeback_result({
        "target": None,
        "written": 0,
        "skipped": True,
        "gate": gate,
        "item_mappings": [],
        "verification": {"status": "not_applicable", "target": None, "reason": "gate_blocked"},
    })


def _director_trace(
    director: Director,
    started_at,
    finished_at,
    *,
    status: str = "completed",
    error: BaseException | None = None,
    provider_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    duration_ms = int(max(0.0, (finished_at - started_at).total_seconds() * 1000))
    trace = {
        "mode": _director_mode(director),
        "source": _callable_source(director),
        "model": getattr(director, "model", None),
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
    }
    if error is not None:
        trace["error_type"] = type(error).__name__
        trace["error_message"] = str(error)
        if hasattr(error, "to_dict"):
            trace["model_call"] = error.to_dict()
    if provider_attempts:
        trace["provider_attempts"] = [dict(report) for report in provider_attempts]
    return validate_schema(trace, "director_audit.schema.json")


def _director_mode(director: Director) -> str:
    if director is decide_next_step:
        return "rule"
    if director.__class__.__name__ == "ModelDirector":
        return "model"
    return "injected"


def _callable_source(director: Director) -> str:
    module = getattr(director, "__module__", None)
    qualname = getattr(director, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    return f"{director.__class__.__module__}.{director.__class__.__qualname__}"


def _persisted_run_ids(run_dir: Path) -> set[str]:
    if not run_dir.exists() or not run_dir.is_dir():
        return set()
    return {path.name for path in run_dir.glob("chapter_*.json")}


def _load_newest_persisted_result(run_dir: Path, known_run_ids: set[str]) -> dict[str, Any] | None:
    if not run_dir.exists() or not run_dir.is_dir():
        return None
    candidates = [
        path
        for path in run_dir.glob("chapter_*.json")
        if path.name not in known_run_ids
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8-sig") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("run"), dict):
            return validate_run_result(payload)
    return None


class LoopExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        original: BaseException,
        session: dict[str, Any],
        runs: list[dict[str, Any]],
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.session = session
        self.runs = runs


class WorkflowExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        original: BaseException,
        trace: list[dict[str, Any]],
        chapter: str,
        chapter_pipeline: dict[str, Any] | None,
        validation: dict[str, Any] | None,
        repair_attempts: int,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.trace = trace
        self.chapter = chapter
        self.chapter_pipeline = chapter_pipeline
        self.validation = validation
        self.repair_attempts = repair_attempts
