from __future__ import annotations

import json
import inspect
from pathlib import Path
from typing import Any, Callable

from api.contracts import ModelCallError, ModelOutputError
from core.config import get_config
from core.director import decide_next_step, validate_decision
from core.project_profile import project_language
from core.review.gate import evaluate_review_gate
from core.review.runtime import RuntimeReviewConfig, run_runtime_review, validate_runtime_review_config
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH
from core.schema import validate_schema
from core.engine.artifacts import (
    save_chapter_artifact,
    save_chapter_pipeline_artifacts,
    save_input_pack_artifact,
    save_loop_session_artifact,
    save_snapshot_pack_artifact,
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
from core.state.snapshot import build_state_update_audit, load_snapshot, save_snapshot, update_snapshot
from core.validator import validate_chapter
from core.validator.spatial import validate_bridge_preconditions
from modules.chapter_generator import PIPELINE_STAGE_NAMES, generate_chapter, run_chapter_pipeline
from modules.claude_polish import polish_chapter
from modules.conflict_engine import analyze_chapter
from modules.scene_repair import repair_scene
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


class AgentExecutor:
    def __init__(
        self,
        *,
        snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
        memory_path: str | Path | None = None,
        memory_source: str = "auto",
        run_dir: str | Path = DEFAULT_RUN_DIR,
        chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
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
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.memory_path = Path(memory_path) if memory_path else None
        self.memory_source = memory_source
        self.run_dir = Path(run_dir)
        self.chapter_dir = Path(chapter_dir)
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

    def run_once(self, *, persist: bool = True) -> dict[str, Any]:
        started_at = utc_now()
        base_snapshot = load_snapshot(self.snapshot_path)
        memory_context = self._load_memory_context()
        if self.use_run_history:
            self._attach_last_run(memory_context)
        snapshot_pack = build_snapshot_input_pack(base_snapshot, memory_context)
        state_result = build_snapshot_state_with_audit(base_snapshot, memory_context)
        snapshot = state_result["snapshot"]
        snapshot_audit = state_result["audit"]
        memory_context["snapshot_builder_audit"] = snapshot_audit
        decision_started_at = utc_now()
        try:
            decision = validate_decision(self.director(snapshot, memory_context))
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

        input_pack = build_input_pack(snapshot, decision, memory_context)
        input_pack_metadata = build_input_pack_metadata(input_pack, snapshot, decision, memory_context)
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
                self._attach_failed_chapter_artifact(failed_result)
                self._save_run_record(failed_result)
            raise exc.original from exc

        committed = bool(validation["ok"])
        planned_run_id = build_run_id(int(decision["chapter_index"]), started_at)
        try:
            analysis = (
                validate_schema(self._analyze(chapter, validation, snapshot), "analysis_result.schema.json")
                if committed
                else _empty_analysis(validation)
            )
            next_snapshot = update_snapshot(snapshot, analysis, validation, source_run_id=planned_run_id) if committed else base_snapshot
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
                self._attach_failed_chapter_artifact(failed_result)
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
            "committed": committed,
            "state_update": state_update_audit,
        }

        if persist:
            self._attach_snapshot_pack_artifact(result["run"], snapshot_pack)
            input_pack_artifact = save_input_pack_artifact(
                input_pack=input_pack,
                run=result["run"],
                output_dir=self.run_dir / "input_packs",
            )
            result["run"]["input_pack"]["artifact"] = input_pack_artifact
            if committed:
                save_snapshot(next_snapshot, self.snapshot_path)
                memory_updates = build_memory_updates(result["run"], analysis)
                result["run"]["state_update"] = build_state_update_audit(
                    snapshot=snapshot,
                    next_snapshot=next_snapshot,
                    analysis=analysis,
                    memory_updates=memory_updates,
                    applied=True,
                )
                result["state_update"] = result["run"]["state_update"]
                writeback_gate = _memory_writeback_gate(
                    committed=committed,
                    validation=validation,
                    workflow_trace=workflow_trace,
                    memory_updates=memory_updates,
                )
                if writeback_gate["allowed"]:
                    result["memory_write"] = write_memory_updates(memory_updates, self.memory_writer)
                    result["memory_write"]["gate"] = writeback_gate
                else:
                    result["memory_write"] = _blocked_memory_writeback(writeback_gate)
                result["run"]["memory"]["writeback"] = result["memory_write"]
            artifact = save_chapter_artifact(
                chapter_text=chapter,
                run=result["run"],
                output_dir=self.chapter_dir,
            )
            result["run"]["chapter"]["artifact"] = artifact
            self._attach_chapter_pipeline_artifacts(
                result["run"],
                chapter_pipeline,
                validation,
                _trace_repair_deltas(workflow_trace),
            )
        self._attach_runtime_review(
            result,
            snapshot=snapshot,
            previous_chapter_text=_previous_chapter_text(memory_context),
        )

        if persist:
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

        started_at = utc_now()
        runs: list[dict[str, Any]] = []
        step_timings: list[dict[str, Any]] = []
        stopped_reason = "max_steps"
        _notify_loop(observer, {"event": "loop_start", "requested_steps": steps})
        for step_number in range(1, steps + 1):
            known_run_ids = _persisted_run_ids(self.run_dir) if persist else set()
            step_started_at = utc_now()
            _notify_loop(observer, {"event": "step_start", "step": step_number, "requested_steps": steps})
            try:
                result = self.run_once(persist=persist)
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
                if stop_on_rejection and not result["committed"]:
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
            state["validation"] = validate_chapter(
                snapshot,
                chapter,
                decision,
                enable_llm=self.enable_llm_validator,
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

    def _attach_last_run(self, memory_context: dict[str, Any]) -> None:
        last_run = load_latest_run_summary(self.run_dir)
        if last_run:
            memory_context["last_run"] = last_run

    def _save_run_record(self, result: dict[str, Any]) -> None:
        validate_run_result(result)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"{result['run']['id']}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    def _attach_snapshot_pack_artifact(self, run: dict[str, Any], snapshot_pack: str) -> None:
        artifact = save_snapshot_pack_artifact(
            snapshot_pack=snapshot_pack,
            run=run,
            output_dir=self.run_dir / "snapshot_packs",
        )
        run["snapshot_builder"]["artifact"] = artifact

    def _attach_failed_chapter_artifact(self, result: dict[str, Any]) -> None:
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

    def _attach_runtime_review(
        self,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        previous_chapter_text: str | None,
    ) -> None:
        if not self.review_config.enabled:
            return
        run = result.get("run")
        chapter = result.get("chapter")
        if not isinstance(run, dict) or not isinstance(chapter, str) or not chapter.strip():
            return
        review = run_runtime_review(
            chapter_text=chapter,
            snapshot=snapshot,
            previous_chapter_text=previous_chapter_text,
            run_id=str(run["id"]),
            config=self.review_config,
        )
        run["review_pipeline"] = review
        result["review_pipeline"] = review
        if self.review_config.gate_threshold != "off":
            gate = evaluate_review_gate(
                review_pipeline=review,
                threshold=self.review_config.gate_threshold,
            )
            run["review_gate"] = gate
            result["review_gate"] = gate


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


def _notify_loop(observer: LoopObserver | None, event: dict[str, Any]) -> None:
    if observer is None:
        return
    observer(event)


def _previous_chapter_text(memory_context: dict[str, Any]) -> str | None:
    last_run = memory_context.get("last_run")
    if not isinstance(last_run, dict):
        return None
    for key in ("chapter_text", "chapter", "draft_text"):
        value = last_run.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


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
        "status": status if status in {"committed", "rejected", "failed"} else "failed",
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
