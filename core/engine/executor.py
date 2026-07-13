from __future__ import annotations

import hashlib
import json
import inspect
from pathlib import Path
from typing import Any, Callable

from api.contracts import ModelCallError, ModelOutputError
from core.chapter_contexts import ChapterContextError, resolve_committed_previous_chapter_artifact
from core.config import get_config
from core.director import decide_next_step, validate_decision
from core.project_profile import project_language
from core.quality_decision import (
    QualityPolicy,
    SEVERITY_RANK,
    build_quality_decision,
    resolve_quality_policy,
)
from core.review.gate import evaluate_review_gate
from core.review.index import build_review_index_entry, review_index_path, update_review_index
from core.review.repair_loop import ReviewRepairConfig, run_review_repair_loop, validate_review_repair_config
from core.review.runtime import RuntimeReviewConfig, run_runtime_review, validate_runtime_review_config
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH, RuntimePaths
from core.schema import validate_schema
from core.engine.artifacts import (
    chapter_artifact_metadata,
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
    atomic_write_json,
    persistence_run_lock,
    reconcile_persistence,
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
from core.story_project.identity import (
    assert_project_identity,
    create_ephemeral_project_identity,
    ensure_project_identity_for_runtime,
    validate_project_identity,
)
from core.story_project.writer import (
    StoryProjectWritebackConfig,
    default_story_project_writeback,
    finalize_story_project_writeback,
    prepare_story_project_writeback,
)
from core.story_project.read_set import capture_story_project_read_set, declared_read_set_writes
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
StoryProjectContextLoader = Callable[[dict[str, Any], dict[str, Any], int | None], Any]


class StoryProjectContextError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


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

    def run_once(self, *, persist: bool = True) -> dict[str, Any]:
        return self._invoke_once(persist=persist)

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
            if not persist:
                return self._run_once_impl(
                    persist=False,
                    snapshot_override=snapshot_override,
                    previous_result=previous_result,
                    chapter_hint=chapter_hint,
                )
            self._prepare_project_identity_for_persistence()
            with persistence_run_lock(self.run_dir, state_paths=self._persistence_state_paths()):
                self._assert_persistence_ready(expected_book_id=self._expected_book_id)
                return self._run_once_impl(
                    persist=True,
                    snapshot_override=snapshot_override,
                    previous_result=previous_result,
                    chapter_hint=chapter_hint,
                )
        finally:
            self._active_story_project_context = None

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
        memory_context = self._load_memory_context()
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
        base_snapshot, memory_context = self._apply_story_project_context(base_snapshot, memory_context)
        snapshot_pack = build_snapshot_input_pack(base_snapshot, memory_context)
        state_result = build_snapshot_state_with_audit(base_snapshot, memory_context)
        snapshot = state_result["snapshot"]
        snapshot_audit = state_result["audit"]
        memory_context["snapshot_builder_audit"] = snapshot_audit
        decision_started_at = utc_now()
        try:
            decision = validate_decision(self.director(snapshot, memory_context))
            context_chapter = (self._story_project_context_dict() or {}).get("chapter_index")
            if context_chapter is not None and int(decision["chapter_index"]) != int(context_chapter):
                raise StoryProjectContextError(
                    "story_project_chapter_mismatch",
                    f"Director chose chapter {decision['chapter_index']} for StoryProject chapter {context_chapter}",
                )
        except Exception as exc:  # noqa: BLE001 - persist Director failure diagnostics.
            director_trace = _director_trace(self.director, decision_started_at, utc_now(), status="failed", error=exc)
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
        director_trace = _director_trace(self.director, decision_started_at, utc_now())
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

        quality_decision = build_quality_decision(
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
            try:
                handler()
            except Exception as exc:  # noqa: BLE001 - preserve failed action diagnostics.
                trace.append(
                    _trace_event(
                        action,
                        started_at,
                        utc_now(),
                        state,
                        planned_step=planned_step,
                        model_trace=self._model_trace_metadata(action, state),
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
                trace.append(
                    _trace_event(
                        action,
                        started_at,
                        utc_now(),
                        state,
                        planned_step=planned_step,
                        model_trace=self._model_trace_metadata(action, state),
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
        state["chapter"] = self._polish(chapter)
        state["validation"] = None

    def _handle_validate(
        self,
        state: dict[str, Any],
        snapshot: dict[str, Any],
        decision: dict[str, Any],
    ) -> None:
        chapter = _require_chapter(state)
        if self.validator is validate_chapter:
            pipeline = state.get("chapter_pipeline") if isinstance(state.get("chapter_pipeline"), dict) else {}
            state["validation"] = validate_chapter(
                snapshot,
                chapter,
                decision,
                enable_llm=self.enable_llm_validator,
                chapter_blueprint=self._story_project_chapter_blueprint(),
                blueprint_coverage=pipeline.get("blueprint_coverage") if isinstance(pipeline, dict) else None,
            )
        else:
            state["validation"] = self.validator(snapshot, chapter, decision)

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
            state["chapter"] = self._repair(
                state["chapter"],
                state["validation"],
                input_pack,
                state["repair_plan"],
                recovery_context,
                project_language(snapshot),
                _repair_context_for_snapshot(snapshot),
            )
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
        context = self._story_project_context_dict()
        if not context:
            return snapshot, memory_context
        identity_payload = context.get("project_identity")
        identity = validate_project_identity(identity_payload) if isinstance(identity_payload, dict) else None
        if identity is not None:
            root = context.get("story_project_root")
            internal_snapshot = (
                RuntimePaths.for_story_project(root).snapshot_path.resolve()
                if root is not None
                else None
            )
            assert_project_identity(
                identity,
                str(snapshot.get("book_id")) if snapshot.get("book_id") is not None else None,
                source=str(self.snapshot_path),
                allow_missing_legacy=(
                    self._allow_legacy_snapshot_adoption
                    or (internal_snapshot is not None and self.snapshot_path.resolve() == internal_snapshot)
                ),
            )
        merged_snapshot = _deep_merge_dict(snapshot, context.get("snapshot_overlay"))
        if identity is not None:
            merged_snapshot["book_id"] = identity.book_id
        merged_memory = dict(memory_context)
        overlay = context.get("memory_context_overlay")
        if isinstance(overlay, dict):
            base_items = list(merged_memory.get("items") or [])
            overlay_items = list(overlay.get("items") or [])
            base_mappings = list(merged_memory.get("source_mappings") or [])
            adjusted_mappings: list[dict[str, Any]] = []
            for mapping in overlay.get("source_mappings") or []:
                if not isinstance(mapping, dict):
                    continue
                adjusted = dict(mapping)
                if isinstance(adjusted.get("index"), int):
                    adjusted["index"] = int(adjusted["index"]) + len(base_items)
                adjusted_mappings.append(adjusted)
            merged_memory["items"] = [*base_items, *overlay_items]
            merged_memory["source_mappings"] = [*base_mappings, *adjusted_mappings]
            merged_memory["story_project"] = {
                "enabled": True,
                "chapter_index": context.get("chapter_index"),
                "book_id": identity.book_id if identity is not None else None,
            }
        return merged_snapshot, merged_memory

    def _normalize_story_project_identity(self, context: Any, *, persist: bool) -> Any:
        if context is None:
            return None
        if hasattr(context, "to_dict"):
            payload = context.to_dict()
        elif isinstance(context, dict):
            payload = dict(context)
        else:
            return context
        root = payload.get("story_project_root")
        if not root:
            return payload
        current_payload = payload.get("project_identity")
        current = validate_project_identity(current_payload) if isinstance(current_payload, dict) else None
        if not persist:
            self._allow_legacy_snapshot_adoption = current is None or current.ephemeral
        if persist:
            stable = ensure_project_identity_for_runtime(
                root,
                persistence_dir=self.persistence_dir,
            )
            if current is not None and not current.ephemeral:
                assert_project_identity(stable, current.book_id, source="StoryProject runtime context")
            payload["project_identity"] = stable.to_dict()
            existing_read_set = payload.get("read_set") if isinstance(payload.get("read_set"), dict) else {}
            payload["read_set"] = capture_story_project_read_set(
                root,
                int(payload["chapter_index"]),
                project_identity=stable,
                parser_version=str(existing_read_set.get("parser_version") or "shadow-1.0"),
                parse_status=str(existing_read_set.get("parse_status") or "ok"),
            )
        elif current is None:
            payload["project_identity"] = create_ephemeral_project_identity(root).to_dict()
        self._last_project_identity = dict(payload["project_identity"])
        return payload

    def _story_project_context_dict(self) -> dict[str, Any] | None:
        context = self._active_story_project_context
        if context is None:
            context = self.story_project_context
        if context is None:
            return None
        if hasattr(context, "to_dict"):
            return context.to_dict()
        if isinstance(context, dict):
            return context
        return None

    def _load_story_project_context(
        self,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
        *,
        chapter_hint: int | None,
    ) -> Any:
        if self.story_project_context_loader is None:
            return self.story_project_context
        context = self.story_project_context_loader(snapshot, memory_context, chapter_hint)
        payload = context.to_dict() if hasattr(context, "to_dict") else context
        if not isinstance(payload, dict):
            raise StoryProjectContextError(
                "story_project_context_invalid",
                "context loader must return a mapping or an object with to_dict()",
            )
        chapter_index = payload.get("chapter_index")
        if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
            raise StoryProjectContextError(
                "story_project_context_invalid",
                "context loader returned an invalid chapter_index",
            )
        if chapter_hint is not None and chapter_index != chapter_hint:
            raise StoryProjectContextError(
                "story_project_sequence_drift",
                f"expected chapter {chapter_hint}, but context resolved chapter {chapter_index}",
            )
        return context

    def _story_project_chapter_blueprint(self) -> dict[str, Any] | None:
        context = self._story_project_context_dict()
        if not context:
            return None
        blueprint = context.get("chapter_blueprint")
        return blueprint if isinstance(blueprint, dict) else None

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
                bool(original_quality_decision["accepted"]),
                chapter_pipeline,
                original_review,
                review_gate,
                None,
                original_quality_decision,
            )

        def repair(current_chapter: str, current_validation: dict[str, Any], repair_plan: dict[str, Any]) -> str:
            return self._repair(
                current_chapter,
                current_validation,
                input_pack,
                repair_plan,
                recovery_context,
                project_language(snapshot),
                _repair_context_for_snapshot(snapshot),
            )

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
        if not review_repair.get("attempted"):
            return (
                chapter,
                validation,
                bool(original_quality_decision["accepted"]),
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

        if final_quality_decision["accepted"]:
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
        if self.review_config.gate_threshold == "off" or not isinstance(review, dict):
            return None
        return evaluate_review_gate(
            review_pipeline=review,
            quality_decision=quality_decision,
            threshold=self.review_config.gate_threshold,
        )

    def _effective_quality_policy(self, *, persist: bool) -> QualityPolicy:
        if self.quality_policy is not None:
            policy = self.quality_policy
        elif persist and self.story_project_writeback.mode == "apply" and self._story_project_context_dict():
            policy = resolve_quality_policy("standard")
        else:
            policy = resolve_quality_policy("minimal")
        include_review = bool(
            policy.include_review
            or self.review_config.enabled
            or self.review_repair_config.enabled
        )
        threshold = policy.threshold
        if self.review_config.enabled and self.review_config.gate_threshold != "off":
            gate_threshold = (
                "blocking"
                if self.review_config.gate_threshold == "blocked"
                else self.review_config.gate_threshold
            )
            threshold = min(
                (threshold, gate_threshold),
                key=lambda item: SEVERITY_RANK[item],
            )
        return policy.with_overrides(threshold=threshold, include_review=include_review)

    def _runtime_review_config_for_policy(self, policy: QualityPolicy) -> RuntimeReviewConfig:
        if self.review_config.enabled or not policy.include_review:
            return self.review_config
        return RuntimeReviewConfig(
            enabled=True,
            output_dir=self.review_config.output_dir,
            rules_path=self.review_config.rules_path,
            use_default_rules=self.review_config.use_default_rules,
            build_repair_prompt=self.review_config.build_repair_prompt,
            build_human_report=self.review_config.build_human_report,
            gate_threshold="off",
        )

    def _quality_decision_with_review(
        self,
        *,
        policy: QualityPolicy,
        validation: dict[str, Any],
        review: dict[str, Any],
        chapter_index: int,
    ) -> dict[str, Any]:
        upstream = review.get("quality_decision") if isinstance(review, dict) else None
        return build_quality_decision(
            policy=policy,
            validation=validation,
            upstream_decisions=[upstream] if isinstance(upstream, dict) else [],
            review_pipeline=review,
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
        validate_run_result(result)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"{result['run']['id']}.json"
        atomic_write_json(path, result)

    def _assert_persistence_ready(self, *, expected_book_id: str | None = None) -> None:
        report = reconcile_persistence(
            run_dir=self.run_dir,
            expected_book_id=expected_book_id,
            transactions_dir=self.persistence_dir,
        )
        blocking = [
            item
            for item in report.get("transactions") or []
            if isinstance(item, dict) and item.get("state") in {"commit_marked", "recovery_required"}
        ]
        if blocking:
            run_ids = ", ".join(str(item.get("run_id") or "unknown") for item in blocking)
            raise PersistenceError(f"persistence_reconciliation_required: {run_ids}")

    def _prepare_project_identity_for_persistence(self) -> None:
        root = None
        identity_payload = None
        if isinstance(self.story_project_context, dict):
            root = self.story_project_context.get("story_project_root")
            identity_payload = self.story_project_context.get("project_identity")
        elif self.story_project_context is not None and hasattr(self.story_project_context, "to_dict"):
            payload = self.story_project_context.to_dict()
            root = payload.get("story_project_root")
            identity_payload = payload.get("project_identity")
        if root is None and self.story_project_context_loader is not None:
            root = getattr(self.story_project_context_loader, "story_project_root", None)
            loader_identity = getattr(self.story_project_context_loader, "project_identity", None)
            if loader_identity is not None and hasattr(loader_identity, "to_dict"):
                identity_payload = loader_identity.to_dict()
        if root is None:
            self._expected_book_id = None
            return
        stable = ensure_project_identity_for_runtime(
            root,
            persistence_dir=self.persistence_dir,
        )
        current = validate_project_identity(identity_payload) if isinstance(identity_payload, dict) else None
        self._allow_legacy_snapshot_adoption = current is None or current.ephemeral
        if current is not None and not current.ephemeral:
            assert_project_identity(stable, current.book_id, source="StoryProject executor configuration")
        self._expected_book_id = stable.book_id
        self._last_project_identity = stable.to_dict()

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
            "mode": "compatible",
            "root": context.get("story_project_root"),
            "book_id": (context.get("project_identity") or {}).get("book_id"),
            "project_identity": context.get("project_identity"),
            "chapter_index": context.get("chapter_index"),
            "chapter_resolution": context.get("chapter_resolution"),
            "chapter_blueprint": context.get("chapter_blueprint"),
            "source_paths": context.get("source_paths"),
            "source_resolution": context.get("source_resolution"),
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

    def _anticipated_persistence(
        self,
        run_id: str,
        targets: list[PersistenceTarget],
        *,
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        journal = self.persistence_dir.resolve() / str(run_id)
        return {
            "run_id": str(run_id),
            "state": "completed",
            "committed": True,
            "partial": False,
            "journal_path": str(journal),
            "commit_marker": str(journal / "commit.marker"),
            "targets": [
                {
                    "kind": target.kind,
                    "path": str(Path(target.path).resolve()),
                    "status": "verified",
                    "metadata": dict(target.metadata),
                }
                for target in targets
            ],
            "errors": [],
            "candidate_result_path": str(journal / "candidate_result.json"),
            "publication": publication,
        }

    def _attach_persistence_payload(self, result: dict[str, Any], payload: dict[str, Any]) -> None:
        public = {
            key: value
            for key, value in payload.items()
            if key in {
                "run_id",
                "state",
                "committed",
                "partial",
                "journal_path",
                "commit_marker",
                "targets",
                "errors",
                "candidate_result_path",
                "publication",
            }
        }
        result["persistence"] = public
        result["run"]["persistence"] = public

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


def _deep_merge_dict(base: dict[str, Any], overlay: Any) -> dict[str, Any]:
    if not isinstance(overlay, dict):
        return dict(base)
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


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
