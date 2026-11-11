from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
import asyncio
import hashlib
import json
import os
import re
import secrets
import weakref

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config import get_config
from core.engine.locked_chapter import verified_final_recovery_draft
from core.engine.persistence import atomic_write_text
from core.engine.persistence import atomic_create_json
from core.runtime_paths import RuntimePaths
from core.state.snapshot import load_snapshot
from core.story_project.activation import (
    activate_story_state,
    evaluate_story_state_activation,
    load_story_state_calibration_report,
    semantic_fields_without_provenance,
    validate_story_state_calibration_report,
)
from core.story_project.amendments import read_current_chapter_text
from core.story_project.identity import load_project_identity, project_identity_for_operation
from core.story_project.paths import (
    outline_chapter_index,
    prose_chapter_index,
    resolve_outline,
    resolve_prose,
    scan_prose_chapters,
)
from core.story_project.semantic_parser import (
    build_story_project_shadow_report,
    parse_story_project_semantic_state,
)
from core.story_project.validator import validate_story_project
from workbench.amendments import AmendmentError, AmendmentService
from workbench.chapter_cards import (
    ChapterCardConflict,
    ChapterCardError,
    ChapterCardService,
)
from workbench.chapter_candidates import (
    ChapterCandidateService,
    ChapterCandidateWorkbenchConflict,
    ChapterCandidateWorkbenchError,
    ChapterCandidateWorkbenchIntegrity,
    ChapterCandidateWorkbenchNotFound,
)
from workbench.chapter_facts import (
    ChapterFactReviewConflict,
    ChapterFactReviewError,
    ChapterFactReviewIntegrity,
    ChapterFactReviewNotFound,
    ChapterFactReviewService,
)
from workbench.chapter_fact_extractions import (
    ChapterFactExtractionService,
    ChapterFactExtractionWorkbenchConflict,
    ChapterFactExtractionWorkbenchError,
    ChapterFactExtractionWorkbenchIntegrity,
    ChapterFactExtractionWorkbenchNotFound,
)
from workbench.cohorts import CohortError, CohortService, SCORE_DIMENSIONS
from workbench.database import (
    WorkbenchDatabase, WorkbenchDatabaseError, WorkbenchDatabaseInitializationPending,
)
from workbench.instance import InstanceLease
from workbench.recovery_guidance import attention_label_for_project, durable_polished_draft_available
from workbench.review_status import resolve_review_status
from workbench.jobs import (
    JobError,
    JobStore,
    JobSupervisor,
    PAUSED_JOB_STATES,
    TERMINAL_JOB_STATES,
    compute_project_state_head,
    verify_committed_chapter,
)
from workbench.outlines import OutlineBatchConflict, OutlineBatchError, OutlineBatchService
from workbench.planning import OutlinePlanner, OutlinePlanningError
from workbench.projects import ProjectRecord, ProjectRegistry, ProjectRegistryError
from workbench.chapter_publication_api import install_chapter_publication_routes
from workbench.reviewed_workflow import install_reviewed_workflow
from workbench.reviewed_project_views import ProjectDisplayRegistry, ReviewedProjectViews
from workbench.maintenance import maintenance_for, MaintenanceBlocked, DeferredStartup
from workbench.maintenance_api import MaintenanceRoute, install_maintenance_status
from workbench.backup_api import install_backup_api
from workbench.recovery_api import install_recovery_api
from workbench.restore_scope import RestoreScopeError, install_restore_scope_api


SESSION_COOKIE = "novelagent_workbench"
SAFE_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver", "::1"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def create_app(*, database_path: str | Path | None = None,
               instance_lease: InstanceLease | None = None, **kwargs: Any) -> FastAPI:
    db_path = Path(database_path or _local_app_root() / "workbench.db").expanduser().resolve()
    lease = instance_lease or InstanceLease(db_path.parent).acquire()
    lease.require_held()
    if lease.directory != db_path.parent:
        raise ValueError("Instance lease does not cover the workbench data directory")
    try:
        app = _create_app_owned(database_path=db_path, instance_lease=lease, **kwargs)
    except BaseException:
        lease.close()
        raise
    weakref.finalize(app, _close_owned_app, app.state.supervisor, lease, app.state.maintenance)
    return app


def _close_owned_app(supervisor: JobSupervisor, lease: InstanceLease, maintenance=None) -> None:
    supervisor.shutdown()
    if maintenance is not None and lease.held:maintenance.require_no_active()
    lease.close()


def _create_app_owned(
    *,
    instance_lease: InstanceLease,
    database_path: str | Path | None = None,
    allowed_workspaces: Sequence[str | Path] | None = None,
    repo_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    start_worker: bool = True,
    auto_import_active: bool = True,
    outline_provider: Any | None = None,
    amendment_provider: Any | None = None,
    chapter_candidate_executor_factory: Any | None = None,
    chapter_fact_extraction_executor_factory: Any | None = None,
    chapter_publication_machine_store: str | Path | None = None,
    reviewed_chapter_candidate_executor_factory: Any | None = None,
    reviewed_chapter_fact_extraction_executor_factory: Any | None = None,
    python_executable: str | Path | None = None,
) -> FastAPI:
    repository = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    local_root = _local_app_root()
    db_path = Path(database_path or local_root / "workbench.db")
    task_root = Path(runtime_root or local_root / "jobs")
    maintenance=maintenance_for(instance_lease)
    workspaces = tuple(allowed_workspaces or _configured_workspaces(repository))
    # Schema registration is separate from construction. Even later-installed
    # reviewed services must wait for the same maintenance admission.
    database = WorkbenchDatabase(db_path, defer_initialization=True, machine_store=chapter_publication_machine_store,
        allow_recovery_controller=True)
    restore_scope = database.restore_scope
    if restore_scope is not None:
        start_worker = auto_import_active = False
        workspaces = (restore_scope.book_root,)
        task_root = restore_scope.task_runtime_root
    projects = ProjectRegistry(database, allowed_workspaces=workspaces)
    outlines = OutlineBatchService(database, projects)
    chapter_cards = ChapterCardService(projects)
    jobs = JobStore(database)
    chapter_candidates = ChapterCandidateService(
        projects=projects,
        chapter_cards=chapter_cards,
        database=database,
        executor_factory=chapter_candidate_executor_factory,
    )
    chapter_facts = ChapterFactReviewService(
        database=database,
        projects=projects,
        candidate_drafts=chapter_candidates.drafts,
    )
    chapter_fact_extractions = ChapterFactExtractionService(
        projects=projects,
        candidate_drafts=chapter_candidates.drafts,
        chapter_facts=chapter_facts,
        executor_factory=chapter_fact_extraction_executor_factory,
    )
    planner = OutlinePlanner(database=database, outlines=outlines, provider=outline_provider)
    amendments = AmendmentService(
        database=database,
        projects=projects,
        provider=amendment_provider,
    )
    cohorts = CohortService(
        database=database,
        projects=projects,
        jobs=jobs,
        repo_root=repository,
    )
    bootstrap_warnings: list[str] = []
    def recover_startup():
        database.complete_initialization()
        if restore_scope is not None:
            bootstrap_warnings.append('恢复副本已启用；只访问目标作品，重启不会自动执行任务。' if restore_scope.active
                else '恢复副本尚未启用，当前只供核对资料。')
            return
        recovered_plans=planner.recover_after_restart()
        recovered_amendments=amendments.recover_after_restart()
        blocked = len(getattr(planner, 'recovery_blocked', ())) + len(getattr(amendments, 'recovery_blocked', ()))
        if blocked:
            bootstrap_warnings.append(f"{blocked} 个中断操作所属作品当前无法取得写入资格，原记录已保留。")
        if recovered_plans:bootstrap_warnings.append(f"{recovered_plans} 个中断的章纲调用已锁定，需显式恢复。")
        if recovered_amendments:bootstrap_warnings.append(f"{recovered_amendments} 个中断的润色审核已锁定，需显式恢复。")
        if auto_import_active:
            try:_import_active_project(repository,projects)
            except Exception as exc:bootstrap_warnings.append(f"活动作品未自动导入：{exc}")
    startup_recovery=DeferredStartup(recover_startup)
    supervisor = JobSupervisor(
        store=jobs,
        project_lookup=projects.get,
        repo_root=repository,
        runtime_root=task_root,
        python_executable=python_executable,
        instance_lease=instance_lease,
        startup_recovery=startup_recovery.run,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            instance_lease.require_held()
            if start_worker:
                supervisor.start()
            yield
        finally:
            await maintenance.drain_async()
            if start_worker:
                supervisor.shutdown()
            maintenance.require_no_active()
            instance_lease.close()

    app = FastAPI(
        title="NovelAgent 本地创作工作台",
        version="0.1.0-beta",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.router.route_class=MaintenanceRoute
    app.state.maintenance=maintenance
    app.state.startup_recovery=startup_recovery
    install_maintenance_status(app)
    install_backup_api(app,projects=projects,maintenance=maintenance,task_runtime_root=task_root,
        worker_enabled=start_worker,auto_import_active=auto_import_active,
        machine_store=chapter_publication_machine_store)
    app.state.session_token = secrets.token_urlsafe(32)
    install_recovery_api(app,projects=projects,maintenance=maintenance,backups=app.state.backups,
        machine_store=chapter_publication_machine_store,repository=repository,python_executable=python_executable)
    app.state.instance_lease = instance_lease
    app.state.database = database
    app.state.restore_scope = restore_scope
    install_restore_scope_api(app, database)
    app.state.projects = projects
    app.state.outlines = outlines
    app.state.chapter_cards = chapter_cards
    app.state.chapter_candidates = chapter_candidates
    app.state.chapter_facts = chapter_facts
    app.state.chapter_fact_extractions = chapter_fact_extractions
    app.state.jobs = jobs
    app.state.planner = planner
    app.state.amendments = amendments
    app.state.cohorts = cohorts
    app.state.supervisor = supervisor
    app.state.bootstrap_warnings = bootstrap_warnings
    app.state.allowed_workspaces = [str(path) for path in workspaces]
    app.state.repo_root = repository

    @app.middleware("http")
    async def local_security_boundary(request: Request, call_next: Any):
        if not instance_lease.held:
            return JSONResponse({"error": {"code": "workbench_stopped", "message": "工作台已停止，请重新启动。"}}, status_code=503)
        host_header = request.headers.get("host", "")
        host = _host_name(host_header)
        if host not in SAFE_HOSTS:
            return JSONResponse(
                {"error": {"code": "host_not_allowed", "message": "工作台仅接受本机 Host。"}},
                status_code=400,
            )
        if request.method.upper() in MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin and not _same_origin(origin, host_header):
                return JSONResponse(
                    {"error": {"code": "origin_not_allowed", "message": "跨站请求已拒绝。"}},
                    status_code=403,
                )
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return JSONResponse(
                    {"error": {"code": "cross_site_request", "message": "跨站请求已拒绝。"}},
                    status_code=403,
                )
            cookie_token = request.cookies.get(SESSION_COOKIE, "")
            header_token = request.headers.get("x-novelagent-token", "")
            supplied = header_token or cookie_token
            if not supplied or not secrets.compare_digest(supplied, app.state.session_token):
                return JSONResponse(
                    {"error": {"code": "session_token_required", "message": "启动令牌无效，请刷新工作台。"}},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/api/instance")
    async def instance_identity():
        return {"instance_id": instance_lease.instance_id}

    @app.exception_handler(WorkbenchDatabaseInitializationPending)
    async def database_initialization_pending(_: Request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=503)

    @app.exception_handler(RestoreScopeError)
    async def restore_scope_error(_: Request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=409)

    @app.exception_handler(ProjectRegistryError)
    @app.exception_handler(OutlineBatchError)
    @app.exception_handler(OutlinePlanningError)
    @app.exception_handler(JobError)
    @app.exception_handler(AmendmentError)
    @app.exception_handler(ChapterCardError)
    @app.exception_handler(ChapterCandidateWorkbenchError)
    @app.exception_handler(ChapterFactReviewError)
    @app.exception_handler(ChapterFactExtractionWorkbenchError)
    @app.exception_handler(CohortError)
    @app.exception_handler(ValueError)
    async def product_error(_: Request, exc: Exception):
        if isinstance(exc, ChapterCandidateWorkbenchNotFound):
            status = 404
        elif isinstance(exc, ChapterFactReviewNotFound):
            status = 404
        elif isinstance(exc, ChapterFactExtractionWorkbenchNotFound):
            status = 404
        elif isinstance(
            exc,
            (
                ChapterCandidateWorkbenchIntegrity,
                ChapterFactReviewIntegrity,
                ChapterFactExtractionWorkbenchIntegrity,
            ),
        ):
            status = 500
        elif isinstance(
            exc,
            (
                OutlineBatchConflict,
                ChapterCardConflict,
                ChapterCandidateWorkbenchConflict,
                ChapterFactReviewConflict,
                ChapterFactExtractionWorkbenchConflict,
                JobError,
            ),
        ):
            status = 409
        else:
            status = 400
        return JSONResponse(
            {"error": {"code": _error_code(exc), "message": str(exc)}},
            status_code=status,
        )

    chapter_publication = install_chapter_publication_routes(app, database=database, allowed_workspaces=workspaces,
        machine_store=chapter_publication_machine_store)
    from workbench.chapter_budgets import install_chapter_budget_routes
    install_chapter_budget_routes(app,chapter_publication)
    chapter_cards, chapter_candidates, chapter_facts, chapter_fact_extractions = install_reviewed_workflow(app,
        publication=chapter_publication, cards=chapter_cards, candidates=chapter_candidates,
        facts=chapter_facts, extractions=chapter_fact_extractions,
        candidate_executor_factory=reviewed_chapter_candidate_executor_factory,
        extraction_executor_factory=reviewed_chapter_fact_extraction_executor_factory)
    display_projects = ProjectDisplayRegistry(database, allowed_workspaces=workspaces)
    reviewed_views = ReviewedProjectViews(chapter_publication)

    @app.get("/api/projects/{project_id}/model-calls")
    async def project_model_calls(project_id: str) -> dict[str, Any]:
        from workbench.model_usage import read_project_model_usage
        record = display_projects.get(project_id)
        try:
            report = await asyncio.to_thread(read_project_model_usage, record.root, book_id=record.book_id)
        except (OSError, ValueError, RuntimeError) as exc:
            # Preserve evidence and avoid returning a misleading partial total.
            return JSONResponse({"error":{"code":"model_usage_evidence_invalid",
                "message":"模型调用证据未能完整核验，暂不能显示用量合计。请保留原记录并检查项目恢复状态。"}},status_code=409)
        return {"project_id":record.id,**report}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        current_scope = getattr(request.state,'restore_scope',restore_scope)
        scope_view = current_scope.view() if current_scope is not None else None
        if scope_view is not None and getattr(request.state,'restore_restart_required',False):
            scope_view.update(restart_required=True,message='原作品写资格已恢复；请重启此实例恢复写作。')
        return {
            "name": "NovelAgent",
            "version": app.version,
            "local_only": True,
            "provider": _provider_status(),
            "allowed_workspaces": list(app.state.allowed_workspaces),
            "warnings": list(app.state.bootstrap_warnings),
            "restore_scope": scope_view,
        }

    @app.get("/api/projects")
    async def list_projects() -> dict[str, Any]:
        return {"items": [reviewed_views.summary(record) if app.state.reviewed_workflow.is_reviewed(record.id)
            else _project_summary(record, jobs) for record in display_projects.list_projects()]}

    @app.post("/api/projects", status_code=201)
    async def create_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(payload.get("name") or payload.get("title") or "").strip()
        explicit_target = payload.get("target") or payload.get("path")
        if explicit_target:
            target = Path(str(explicit_target))
        else:
            parent_value = payload.get("save_location") or payload.get("root_path")
            if not parent_value:
                raise ProjectRegistryError("target or save_location is required")
            target = Path(str(parent_value)) / _safe_directory_name(name)
        record = projects.create_project(
            target,
            name=name,
            genre=str(payload.get("genre") or ""),
            synopsis=str(payload.get("synopsis") or payload.get("summary") or ""),
            language=str(payload.get("language") or "zh-CN"),
        )
        return {"project": _project_summary(record, jobs)}

    @app.post("/api/projects/import", status_code=201)
    async def import_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        root = payload.get("root") or payload.get("path")
        if not root:
            raise ProjectRegistryError("StoryProject path is required")
        record = projects.import_project(
            str(root),
            name=str(payload.get("name") or "").strip() or None,
        )
        return {"project": _project_summary(record, jobs)}

    @app.get("/api/projects/{project_id}")
    async def project_detail(project_id: str) -> dict[str, Any]:
        record = display_projects.get(project_id)
        if app.state.reviewed_workflow.is_reviewed(project_id):
            return await asyncio.to_thread(reviewed_views.detail, record)
        return {
            "project": _project_summary(record, jobs),
            "overview": _project_overview(record, outlines, jobs),
        }

    @app.get("/api/projects/{project_id}/chapters")
    async def project_chapters(project_id: str) -> dict[str, Any]:
        record = display_projects.get(project_id)
        if app.state.reviewed_workflow.is_reviewed(project_id):
            return {"items":await asyncio.to_thread(reviewed_views.chapters, record)}
        return {"items": _chapter_list(record, amendments)}

    @app.get("/api/projects/{project_id}/chapters/{chapter}")
    async def project_chapter(project_id: str, chapter: int) -> dict[str, Any]:
        record = display_projects.get(project_id)
        if app.state.reviewed_workflow.is_reviewed(project_id):
            detail = await asyncio.to_thread(reviewed_views.chapter, record, chapter)
        else:
            detail = _chapter_detail(record, chapter, amendments)
        draft = await asyncio.to_thread(jobs.get_project_chapter_draft, record.id, chapter)
        if draft is not None:
            if (draft.project_id != record.id or draft.chapter != chapter or draft.revision < 1
                    or hashlib.sha256(draft.text.encode('utf-8')).hexdigest() != draft.sha256):
                raise JobError('保留的作者草稿内容或修订无法核验，请保留数据并核对。')
            detail['retained_author_draft'] = {name:getattr(draft,name)
                for name in ('id','project_id','chapter','revision','sha256','text','state')}
        return detail

    @app.get("/api/projects/{project_id}/chapters/{chapter}/card")
    async def project_chapter_card(project_id: str, chapter: int) -> dict[str, Any]:
        card = await asyncio.to_thread(chapter_cards.get, project_id, chapter)
        return {"chapter_card": card.to_dict()}

    @app.post("/api/projects/{project_id}/chapters/{chapter}/card/confirm")
    async def confirm_project_chapter_card(
        project_id: str,
        chapter: int,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        confirmation = await asyncio.to_thread(
            chapter_cards.confirm,
            project_id,
            chapter,
            payload,
        )
        return {
            "confirmation": confirmation,
            "message": "章节意图已在当前工作台会话中确认；尚未创建正文候选。",
        }

    @app.get("/api/projects/{project_id}/chapters/{chapter}/candidates")
    async def project_chapter_candidates(
        project_id: str,
        chapter: int,
    ) -> dict[str, Any]:
        items = await asyncio.to_thread(
            chapter_candidates.list_for_chapter,
            project_id,
            chapter,
        )
        return {"items": list(items)}

    @app.post("/api/projects/{project_id}/chapters/{chapter}/candidates")
    async def generate_project_chapter_candidate(
        project_id: str,
        chapter: int,
        payload: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        candidate = await asyncio.to_thread(
            chapter_candidates.generate,
            project_id,
            chapter,
            confirmation=payload.get("confirmation"),
            idempotency_key=(idempotency_key or payload.get("idempotency_key") or ""),
        )
        return {
            "candidate": candidate,
            "message": _candidate_result_message(candidate),
        }

    @app.post(
        "/api/projects/{project_id}/chapters/{chapter}/candidates/{candidate_id}/resume"
    )
    async def resume_project_chapter_candidate(
        project_id: str,
        chapter: int,
        candidate_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        candidate = await asyncio.to_thread(
            chapter_candidates.resume,
            project_id,
            chapter,
            candidate_id,
            expected_intent_hash=str(payload.get("expected_intent_hash") or ""),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "candidate": candidate,
            "message": _candidate_result_message(candidate),
        }

    @app.post(
        "/api/projects/{project_id}/chapters/{chapter}/candidates/{candidate_id}/draft"
    )
    async def create_project_chapter_candidate_draft(
        project_id: str,
        chapter: int,
        candidate_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        candidate = await asyncio.to_thread(
            chapter_candidates.create_editable_draft,
            project_id,
            chapter,
            candidate_id,
            expected_prose_sha256=str(payload.get("expected_prose_sha256") or ""),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "candidate": candidate,
            "message": "正文候选已复制为工作台可编辑草稿；正式正文和权威状态仍未修改。",
        }

    @app.get(
        "/api/projects/{project_id}/chapters/{chapter}/candidates/{candidate_id}"
    )
    async def project_chapter_candidate(
        project_id: str,
        chapter: int,
        candidate_id: str,
    ) -> dict[str, Any]:
        candidate = await asyncio.to_thread(
            chapter_candidates.get,
            project_id,
            chapter,
            candidate_id,
        )
        return {"candidate": candidate}

    @app.put("/api/candidate-drafts/{draft_id}")
    async def update_candidate_draft(
        draft_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        draft = await asyncio.to_thread(
            chapter_candidates.update_editable_draft,
            draft_id,
            text=payload.get("text"),
            expected_revision=payload.get("expected_revision"),
        )
        return {
            "draft": draft,
            "message": "工作台草稿已保存；正式正文和权威状态仍未修改。",
        }

    @app.get("/api/candidate-drafts/{draft_id}/fact-extractions")
    async def candidate_draft_fact_extractions(draft_id: str) -> dict[str, Any]:
        items = await asyncio.to_thread(
            chapter_fact_extractions.list_for_draft,
            draft_id,
        )
        return {"items": list(items)}

    @app.post("/api/candidate-drafts/{draft_id}/fact-extractions")
    async def start_candidate_draft_fact_extraction(
        draft_id: str,
        payload: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        extraction = await asyncio.to_thread(
            chapter_fact_extractions.start,
            draft_id,
            expected_draft_revision=payload.get("expected_draft_revision"),
            expected_draft_sha256=str(payload.get("expected_draft_sha256") or ""),
            idempotency_key=(
                idempotency_key or payload.get("idempotency_key") or ""
            ),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_extraction": extraction,
            "message": _fact_extraction_result_message(extraction),
        }

    @app.get(
        "/api/candidate-drafts/{draft_id}/fact-extractions/{extraction_id}"
    )
    async def candidate_draft_fact_extraction(
        draft_id: str,
        extraction_id: str,
    ) -> dict[str, Any]:
        extraction = await asyncio.to_thread(
            chapter_fact_extractions.get,
            draft_id,
            extraction_id,
        )
        return {"fact_extraction": extraction}

    @app.post(
        "/api/candidate-drafts/{draft_id}/fact-extractions/{extraction_id}/resume"
    )
    async def resume_candidate_draft_fact_extraction(
        draft_id: str,
        extraction_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        extraction = await asyncio.to_thread(
            chapter_fact_extractions.resume,
            draft_id,
            extraction_id,
            expected_intent_hash=str(payload.get("expected_intent_hash") or ""),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_extraction": extraction,
            "message": _fact_extraction_result_message(extraction),
        }

    @app.post(
        "/api/candidate-drafts/{draft_id}/fact-extractions/{extraction_id}/review"
    )
    async def open_candidate_draft_fact_extraction_review(
        draft_id: str,
        extraction_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        extraction = await asyncio.to_thread(
            chapter_fact_extractions.open_review,
            draft_id,
            extraction_id,
            expected_record_hash=str(payload.get("expected_record_hash") or ""),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_extraction": extraction,
            "message": _fact_extraction_result_message(extraction),
        }

    @app.post(
        "/api/candidate-drafts/{draft_id}/fact-extractions/{extraction_id}/cancel"
    )
    async def cancel_candidate_draft_fact_extraction(
        draft_id: str,
        extraction_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        extraction = await asyncio.to_thread(
            chapter_fact_extractions.cancel,
            draft_id,
            extraction_id,
            expected_intent_hash=str(payload.get("expected_intent_hash") or ""),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_extraction": extraction,
            "message": _fact_extraction_result_message(extraction),
        }

    @app.post(
        "/api/candidate-drafts/{draft_id}/fact-extractions/{extraction_id}/abandon-provider"
    )
    async def abandon_candidate_draft_fact_extraction_provider(
        draft_id: str,
        extraction_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        extraction = await asyncio.to_thread(
            chapter_fact_extractions.abandon_provider_uncertainty,
            draft_id,
            extraction_id,
            expected_intent_hash=str(payload.get("expected_intent_hash") or ""),
            expected_record_hash=(
                str(payload["expected_record_hash"])
                if payload.get("expected_record_hash") is not None
                else None
            ),
            expected_uncertainty_sha256=str(
                payload.get("expected_uncertainty_sha256") or ""
            ),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_extraction": extraction,
            "message": _fact_extraction_result_message(extraction),
        }

    @app.get("/api/candidate-drafts/{draft_id}/fact-reviews")
    async def candidate_draft_fact_reviews(draft_id: str) -> dict[str, Any]:
        items = await asyncio.to_thread(chapter_facts.list_for_draft, draft_id)
        return {"items": items}

    @app.post("/api/candidate-drafts/{draft_id}/fact-reviews", status_code=201)
    async def create_candidate_draft_fact_review(
        draft_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        review = await asyncio.to_thread(
            chapter_facts.create,
            draft_id,
            expected_draft_revision=payload.get("expected_draft_revision"),
            expected_draft_sha256=str(payload.get("expected_draft_sha256") or ""),
            proposals=payload.get("proposals"),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_review": review,
            "message": (
                "事实候选卡基于旧快照建立并已保留，但当前已失效；请从最新正文重新开始审核。"
                if review.get("state") == "stale"
                else "事实候选卡已建立；所有项目仍是待审建议，正式正文和权威状态均未修改。"
            ),
        }

    @app.get("/api/fact-reviews/{review_id}")
    async def fact_review_detail(review_id: str) -> dict[str, Any]:
        review = await asyncio.to_thread(chapter_facts.get, review_id)
        return {"fact_review": review}

    @app.put("/api/fact-reviews/{review_id}")
    async def update_fact_review(
        review_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        review = await asyncio.to_thread(
            chapter_facts.update,
            review_id,
            facts=payload.get("facts"),
            expected_revision=payload.get("expected_revision"),
        )
        return {
            "fact_review": review,
            "message": (
                "事实候选编辑已保存为旧快照证据，但当前已失效，不能用于提交。"
                if review.get("state") == "stale"
                else "事实候选卡已保存；正式正文和权威状态均未修改。"
            ),
        }

    @app.get("/api/fact-reviews/{review_id}/editor-context")
    async def fact_review_editor_context(review_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(chapter_facts.editor_context, review_id)

    @app.put("/api/fact-reviews/{review_id}/support")
    async def update_fact_review_support(review_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if set(payload) != {"support", "expected_revision", "author_confirmed"}:
            raise ValueError("身份与基线补充只接受作者决定和当前审核修订。")
        review = await asyncio.to_thread(chapter_facts.update_support, review_id,
            support=payload["support"], expected_revision=payload["expected_revision"],
            author_confirmed=payload["author_confirmed"] is True)
        return {"fact_review":review, "message":"身份与基线决定已保存；确认整章审核后方可预览提交。"}

    @app.post("/api/fact-reviews/{review_id}/confirm")
    async def confirm_fact_review(
        review_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        review = await asyncio.to_thread(
            chapter_facts.confirm,
            review_id,
            expected_revision=payload.get("expected_revision"),
            expected_facts_sha256=str(payload.get("expected_facts_sha256") or ""),
            expected_draft_revision=payload.get("expected_draft_revision"),
            expected_draft_sha256=str(payload.get("expected_draft_sha256") or ""),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "fact_review": review,
            "message": (
                "旧快照的事实审核已确认并保留，但当前已失效，不能提交；请从最新正文重建审核。"
                if review.get("state") == "stale"
                else "事实候选已由作者确认并锁定，但尚未写入权威状态或正式正文。"
            ),
        }

    @app.post(
        "/api/projects/{project_id}/chapters/{chapter}/candidates/{candidate_id}/abandon-provider"
    )
    async def abandon_project_chapter_candidate_provider(
        project_id: str,
        chapter: int,
        candidate_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        candidate = await asyncio.to_thread(
            chapter_candidates.abandon_provider_uncertainty,
            project_id,
            chapter,
            candidate_id,
            expected_intent_hash=str(payload.get("expected_intent_hash") or ""),
            expected_record_hash=(
                str(payload["expected_record_hash"])
                if payload.get("expected_record_hash") is not None
                else None
            ),
            expected_uncertainty_sha256=str(
                payload.get("expected_uncertainty_sha256") or ""
            ),
            author_confirmed=payload.get("author_confirmed") is True,
        )
        return {
            "candidate": candidate,
            "message": (
                "未知 Provider 结果已放弃采用，原始证据仍保留；现在可以创建新的正文候选。"
            ),
        }

    @app.get("/api/projects/{project_id}/export")
    async def export_project(project_id: str) -> PlainTextResponse:
        record = display_projects.get(project_id)
        if app.state.reviewed_workflow.is_reviewed(project_id):
            sections = await asyncio.to_thread(reviewed_views.export, record)
        else:
            sections = []
            for chapter in sorted(scan_prose_chapters(record.root)):
                current_text, _, _, _ = read_current_chapter_text(record.root, chapter)
                sections.append(f"===== 第 {chapter} 章 =====\n\n{current_text.rstrip()}")
        if not sections:
            raise ProjectRegistryError("作品还没有可导出的正式正文")
        response = PlainTextResponse("\n\n\n".join(sections) + "\n", media_type="text/plain; charset=utf-8")
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", record.id).strip("-") or "book"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="NovelAgent-{safe_id}.txt"'
        )
        return response

    @app.get("/api/projects/{project_id}/pending")
    async def project_pending(project_id: str) -> dict[str, Any]:
        record = projects.get(project_id)
        return _pending_payload(record, outlines, jobs)

    @app.get("/api/projects/{project_id}/history")
    async def project_history(project_id: str) -> dict[str, Any]:
        record = projects.get(project_id)
        return {
            "jobs": [item.to_dict() for item in jobs.list_jobs(project_id=record.id, limit=100)],
            "runs": _run_history(record.root),
        }

    @app.get("/api/projects/{project_id}/history/{entry_id}")
    async def project_history_detail(
        project_id: str,
        entry_id: str,
        source: str = Query("run"),
    ) -> dict[str, Any]:
        if source not in {"job", "run"}:
            raise HTTPException(status_code=422, detail="source must be 'job' or 'run'")

        record = projects.get(project_id)
        job_summary: dict[str, Any] | None = None
        run_id = entry_id
        if source == "job":
            try:
                job = jobs.get(entry_id)
            except JobError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if job.project_id != record.id:
                raise HTTPException(status_code=404, detail="job not found for this project")
            job_summary = {
                "id": job.id,
                "kind": job.kind,
                "state": job.state,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "next_chapter": job.next_chapter,
            }
            run_id = str(job.result.get("run_id") or "").strip()
            if not run_id:
                return {
                    "source": source,
                    "job": job_summary,
                    "run": None,
                    "verification": None,
                }

        payload = _history_run_by_id(record.root, run_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="run record not found for this project")
        run = _compact_run(payload)
        if run is None:
            raise HTTPException(status_code=404, detail="invalid run record")

        verification: dict[str, Any] | None = None
        if run.get("committed") is True and isinstance(run.get("chapter_index"), int):
            verification = verify_committed_chapter(record.root, run["chapter_index"], payload)
        return {
            "source": source,
            "job": job_summary,
            "run": run,
            "verification": verification,
        }

    @app.get("/api/projects/{project_id}/production")
    async def project_production(project_id: str) -> dict[str, Any]:
        record = projects.get(project_id)
        batches = outlines.list_batches(record.id)
        cohort_items = cohorts.list_for_project(record.id)
        job_payload = []
        for listed_job in jobs.list_jobs(project_id=record.id, limit=30):
            job, last_event_id = jobs.stream_snapshot(listed_job.id)
            item = job.to_dict()
            item["last_event_id"] = last_event_id
            if job.state in PAUSED_JOB_STATES:
                item["resume_ready"] = jobs.resume_ready(job.id)
            job_payload.append(item)
        return {
            "project": _project_summary(record, jobs),
            "jobs": job_payload,
            "approved_batches": [item.to_dict() for item in batches if item.status == "approved"],
            "draft_batches": [item.to_dict() for item in batches if item.status == "draft"],
            "provider": _provider_status(),
            "cohorts": [item.to_dict() for item in cohort_items],
            "score_dimensions": list(SCORE_DIMENSIONS),
            "self_use_production_ready": cohorts.production_ready(record.id),
        }

    @app.get("/api/projects/{project_id}/outline-batches")
    async def list_outline_batches(project_id: str) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in outlines.list_batches(project_id)]}

    @app.get("/api/outline-batches/{batch_id}")
    async def get_outline_batch(batch_id: str) -> dict[str, Any]:
        return {"batch": outlines.get_batch(batch_id).to_dict()}

    @app.post("/api/projects/{project_id}/outline-batches", status_code=201)
    async def create_outline_batch(
        project_id: str,
        payload: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        chapters = payload.get("chapters") or payload.get("proposal")
        if isinstance(chapters, list):
            batch = outlines.create_batch(project_id, chapters)
            return {"batch": batch.to_dict(), "provider_calls": 0}
        operation, batch = await asyncio.to_thread(
            planner.propose,
            project_id,
            direction=str(payload.get("direction") or ""),
            chapter_count=int(payload.get("chapter_count") or 10),
            idempotency_key=idempotency_key,
            recover=bool(payload.get("recover")),
        )
        return {"operation": operation.to_dict(), "batch": batch.to_dict()}

    @app.put("/api/outline-batches/{batch_id}")
    async def update_outline_batch(
        batch_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        batch = outlines.update_batch(
            batch_id,
            payload.get("chapters") or payload.get("proposal") or [],
            expected_revision=int(payload.get("expected_revision") or 0),
            expected_sha256=str(payload.get("expected_sha256") or payload.get("source_state_sha") or ""),
        )
        return {"batch": batch.to_dict()}

    @app.post("/api/outline-batches/{batch_id}/approve")
    async def approve_outline_batch(
        batch_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        batch = outlines.approve_batch(
            batch_id,
            expected_revision=int(payload.get("expected_revision") or 0),
            expected_sha256=str(payload.get("expected_sha256") or payload.get("source_state_sha") or ""),
        )
        return {"batch": batch.to_dict()}

    @app.post("/api/outline-batches/{batch_id}/adopt-existing")
    async def adopt_existing_outline_batch(
        batch_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        batch = outlines.adopt_existing_and_approve_batch(
            batch_id,
            expected_revision=int(payload.get("expected_revision") or 0),
            expected_sha256=str(payload.get("expected_sha256") or payload.get("source_state_sha") or ""),
        )
        return {"batch": batch.to_dict()}

    @app.post("/api/projects/{project_id}/sessions", status_code=202)
    async def create_session(project_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        project = projects.get(project_id)
        start = int(payload.get("start_chapter") or project.next_chapter)
        count = int(payload.get("chapter_count") or 10)
        end = int(payload.get("end_chapter") or (start + count - 1))
        batch = _approved_batch_for_range(outlines.list_batches(project.id), start, end)
        if batch is None and start == end and payload.get("recovery_confirmed") is True:
            if not _has_recovery_checkpoint(project.root, start):
                raise JobError("locked chapter must be classified before a recovery commit can start")
            outline_binding = _recovery_outline_binding(project, start)
        elif batch is None:
            raise OutlineBatchError(
                f"第 {start}—{end} 章必须先由一个已批准且冻结的章纲批次完整覆盖"
            )
        else:
            outline_binding = {
                "outline_batch_id": batch.id,
                "outline_manifest_path": batch.approval_manifest_path,
                "outline_proposal_sha256": batch.proposal_sha256,
                "outline_receipt_sha256": batch.approval_receipt_sha256,
            }
        active_cohort = next(
            (item for item in cohorts.list_for_project(project.id) if item.status == "active"),
            None,
        )
        generation_config = {
            "quality_policy": str(payload.get("quality_policy") or "strict"),
            "llm_validator": bool(payload.get("llm_validator", True)),
            "review_gate": "needs_revision",
            "review_auto_repair": True,
            "review_repair_max_attempts": 1,
            "allow_shadow": bool(
                payload.get("allow_shadow", False if active_cohort is not None else True)
            ),
        }
        if active_cohort is not None:
            if (active_cohort.start_chapter, active_cohort.end_chapter) != (start, end):
                raise CohortError("session range must match the active production cohort")
            if active_cohort.baseline["generation_config"] != generation_config:
                raise CohortError("session configuration differs from the frozen cohort baseline")
        job_config = {
            "quality_policy": generation_config["quality_policy"],
            "llm_validator": generation_config["llm_validator"],
            "allow_shadow": generation_config["allow_shadow"],
            "weekly_budget": (
                active_cohort.weekly_budget
                if active_cohort is not None
                else payload.get("weekly_budget")
            ),
            "project_state_head": compute_project_state_head(project),
            **outline_binding,
        }
        if active_cohort is not None:
            job_config["cohort_id"] = active_cohort.id
        job = jobs.create_job(
            project_id=project.id,
            kind="generation_session",
            start_chapter=start,
            end_chapter=end,
            config=job_config,
            idempotency_key=_derived_idempotency_key(
                "session", project.id, start, end, job_config
            ),
            held=True,
        )
        if active_cohort is not None:
            try:
                cohorts.bind_session(active_cohort.id, job.id)
            except Exception:
                jobs.request_cancel(job.id)
                raise
        if payload.get("pause_after_chapter") is True:
            job = jobs.request_pause(job.id)
        job = jobs.release_held(job.id)
        supervisor.notify()
        return {"job": job.to_dict()}

    @app.get("/api/projects/{project_id}/cohorts")
    async def list_cohorts(project_id: str) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in cohorts.list_for_project(project_id)],
            "score_dimensions": list(SCORE_DIMENSIONS),
        }

    @app.post("/api/projects/{project_id}/cohorts", status_code=201)
    async def create_cohort(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        start = int(payload.get("start_chapter") or 0)
        end = int(payload.get("end_chapter") or (start + 9))
        generation_config = payload.get("generation_config") or {
            "quality_policy": "strict",
            "llm_validator": True,
            "review_gate": "needs_revision",
            "review_auto_repair": True,
            "review_repair_max_attempts": 1,
            "allow_shadow": False,
        }
        cohort = cohorts.create(
            project_id,
            start_chapter=start,
            end_chapter=end,
            weekly_budget=float(payload.get("weekly_budget") or 0),
            generation_config=generation_config,
        )
        return {"cohort": cohort.to_dict(), "score_dimensions": list(SCORE_DIMENSIONS)}

    @app.get("/api/cohorts/{cohort_id}")
    async def get_cohort(cohort_id: str) -> dict[str, Any]:
        return {
            "cohort": cohorts.get(cohort_id).to_dict(),
            "live_result": cohorts.evaluate(cohort_id),
            "score_dimensions": list(SCORE_DIMENSIONS),
        }

    @app.put("/api/cohorts/{cohort_id}/chapters/{chapter}/evaluation")
    async def put_cohort_evaluation(
        cohort_id: str,
        chapter: int,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        evaluation = cohorts.record_evaluation(
            cohort_id,
            chapter,
            scores=payload.get("scores") or {},
            edit_minutes=payload.get("edit_minutes"),
            actual_cost=payload.get("actual_cost", 0),
            severe_defects=payload.get("severe_defects") or [],
            notes=str(payload.get("notes") or ""),
            expected_revision=int(payload.get("expected_revision") or 0),
        )
        return {"evaluation": evaluation.to_dict(), "live_result": cohorts.evaluate(cohort_id)}

    @app.post("/api/cohorts/{cohort_id}/close")
    async def close_cohort(cohort_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        cohort = cohorts.close(
            cohort_id,
            expected_revision=int(payload.get("expected_revision") or 0),
        )
        return {"cohort": cohort.to_dict(), "result": cohort.result}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, after_event_id: int = Query(default=0, ge=0)) -> dict[str, Any]:
        record = jobs.get(job_id)
        payload = record.to_dict()
        if record.state in PAUSED_JOB_STATES:
            payload["resume_ready"] = jobs.resume_ready(record.id)
        return {
            "job": payload,
            "events": list(jobs.events_after(job_id, after_id=after_event_id)),
        }

    @app.get("/api/jobs/{job_id}/events")
    async def stream_job_events(
        request: Request,
        job_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        jobs.get(job_id)
        header_id = request.headers.get("last-event-id")
        cursor = max(after, int(header_id) if header_id and header_id.isdigit() else 0)

        async def generate():
            nonlocal cursor
            idle_ticks = 0
            while not await request.is_disconnected():
                events = jobs.events_after(job_id, after_id=cursor)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = int(event["id"])
                        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {cursor}\nevent: {event['event']}\ndata: {data}\n\n"
                else:
                    idle_ticks += 1
                    if idle_ticks % 20 == 0:
                        yield ": heartbeat\n\n"
                    state = jobs.get(job_id).state
                    if (
                        state in TERMINAL_JOB_STATES or state in PAUSED_JOB_STATES
                    ) and idle_ticks >= 2:
                        break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/api/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> dict[str, Any]:
        return {"job": jobs.request_pause(job_id).to_dict()}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        return {"job": jobs.request_cancel(job_id).to_dict()}

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        job = jobs.resume(job_id, confirmed=payload.get("confirmed") is True)
        supervisor.notify()
        return {"job": job.to_dict()}

    @app.post("/api/jobs/{job_id}/recover", status_code=202)
    async def recover_job(job_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        parent = jobs.get(job_id)
        if parent.state not in PAUSED_JOB_STATES:
            raise JobError("only a paused or uncertain session can enter recovery")
        project = projects.get(parent.project_id)
        latest_payload = _latest_run_for_chapter(Path(project.root), parent.next_chapter)
        latest_run = (
            latest_payload.get("run")
            if isinstance(latest_payload, Mapping)
            and isinstance(latest_payload.get("run"), Mapping)
            else {}
        )
        source_run_id = str(latest_run.get("id") or "").strip() or None
        verified_source = (
            verified_final_recovery_draft(
                run_dir=Path(project.root) / ".novelagent" / "runtime" / "runs",
                run_id=source_run_id,
            )
            if source_run_id is not None
            else None
        )
        source_draft_sha256 = (
            str(verified_source.get("sha256") or "").strip() or None
            if isinstance(verified_source, Mapping)
            else None
        )
        recovery = jobs.create_job(
            project_id=parent.project_id,
            kind="locked_chapter_recovery",
            start_chapter=parent.next_chapter,
            end_chapter=parent.next_chapter,
            config={
                "parent_job_id": parent.id,
                "parent_failure_revision": parent.revision,
                "source_run_id": source_run_id,
                "source_draft_sha256": source_draft_sha256,
                "force_reset": bool(payload.get("force_reset")),
            },
            idempotency_key=_derived_idempotency_key(
                "recover-job",
                parent.id,
                parent.next_chapter,
                parent.revision,
                source_run_id,
                source_draft_sha256,
                bool(payload.get("force_reset")),
            ),
        )
        supervisor.notify()
        return {"job": recovery.to_dict(), "parent_job": parent.to_dict()}

    @app.post("/api/projects/{project_id}/recovery", status_code=202)
    async def recover_existing_project_chapter(
        project_id: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        project = projects.get(project_id)
        chapter = int(payload.get("chapter") or project.next_chapter)
        if resolve_prose(project.root, chapter).path is not None:
            raise JobError(f"chapter {chapter} already has formal prose")
        recovery = jobs.create_job(
            project_id=project.id,
            kind="locked_chapter_recovery",
            start_chapter=chapter,
            end_chapter=chapter,
            config={"force_reset": bool(payload.get("force_reset"))},
            idempotency_key=_derived_idempotency_key(
                "recover-project",
                project.id,
                chapter,
                bool(payload.get("force_reset")),
            ),
        )
        supervisor.notify()
        return {"job": recovery.to_dict()}

    @app.get("/api/drafts/{draft_id}")
    async def get_draft(draft_id: str) -> dict[str, Any]:
        return {"draft": jobs.get_draft(draft_id).to_dict()}

    @app.put("/api/drafts/{draft_id}")
    async def update_draft(draft_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        draft = jobs.update_draft(
            draft_id,
            text=payload.get("text"),
            expected_revision=int(payload.get("expected_revision") or 0),
        )
        return {"draft": draft.to_dict()}

    @app.post("/api/drafts/{draft_id}/validate", status_code=202)
    async def validate_draft(draft_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        draft = jobs.get_draft(draft_id)
        project = projects.get(draft.project_id)
        draft_path = (
            Path(project.root)
            / ".novelagent"
            / "workbench"
            / "drafts"
            / f"{draft.id}_r{draft.revision}.md"
        )
        atomic_write_text(draft_path, draft.text)
        recovery = jobs.create_job(
            project_id=project.id,
            kind="locked_chapter_recovery",
            start_chapter=draft.chapter,
            end_chapter=draft.chapter,
            config={
                "manual_draft_path": str(draft_path),
                "parent_job_id": str(payload.get("job_id") or ""),
                "draft_id": draft.id,
                "draft_revision": draft.revision,
            },
            idempotency_key=_derived_idempotency_key(
                "validate-draft",
                draft.id,
                draft.revision,
                str(payload.get("job_id") or ""),
            ),
        )
        supervisor.notify()
        return {"draft": draft.to_dict(), "job": recovery.to_dict()}

    @app.post("/api/drafts/{draft_id}/confirm")
    async def confirm_draft(draft_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        draft = jobs.get_draft(draft_id)
        if draft.state != "validated":
            raise JobError("draft must pass recovery validation before confirmation")
        parent_id = str(payload.get("job_id") or "")
        parent = jobs.get(parent_id)
        if parent.project_id != draft.project_id or parent.next_chapter != draft.chapter:
            raise JobError("draft does not belong to this session chapter")
        resumed = jobs.resume(parent.id, confirmed=payload.get("confirmed") is True)
        supervisor.notify()
        return {"draft": draft.to_dict(), "job": resumed.to_dict()}

    @app.post("/api/chapters/{chapter}/amendments", status_code=201)
    async def create_amendment(
        chapter: int,
        payload: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        record = await asyncio.to_thread(
            amendments.submit,
            project_id,
            chapter,
            text=payload.get("text"),
            expected_base_sha256=str(payload.get("expected_base_sha256") or ""),
            idempotency_key=idempotency_key,
            recover=bool(payload.get("recover")),
        )
        return {"amendment": record.to_dict()}

    @app.get("/api/projects/{project_id}/health")
    async def project_health(project_id: str) -> dict[str, Any]:
        if app.state.reviewed_workflow.is_reviewed(project_id):
            return await asyncio.to_thread(reviewed_views.health, display_projects.get(project_id))
        return _health_payload(projects.get(project_id))

    @app.post("/api/projects/{project_id}/health/activate")
    async def activate_project_health(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ProjectRegistryError("strict activation requires explicit confirmation")
        project = projects.get(project_id)
        report_payload = payload.get("calibration_report")
        if isinstance(report_payload, dict):
            # Browser-selected reports are validated in memory. The report is
            # already content-addressed, and activation persists only its hash;
            # the workbench therefore never needs arbitrary filesystem access.
            report = validate_story_state_calibration_report(report_payload)
        else:
            # Keep the stable API fallback for existing local integrations. A
            # path is accepted only when it resolves inside this StoryProject.
            raw_path = str(payload.get("calibration_report_path") or "").strip()
            if not raw_path:
                raise ProjectRegistryError("请选择经过校准流程生成的 JSON 报告")
            report_path = Path(raw_path).resolve(strict=True)
            try:
                report_path.relative_to(project.root)
            except ValueError as exc:
                raise ProjectRegistryError("calibration report must stay inside the StoryProject") from exc
            report = load_story_state_calibration_report(report_path)
        current_identity = load_project_identity(project.root) or project_identity_for_operation(
            project.root, persist=False
        )
        semantic_state = parse_story_project_semantic_state(
            project.root,
            project.next_chapter,
            project_identity=current_identity,
        )
        current_blockers: list[str] = []
        if report["book_id"] != current_identity.book_id:
            current_blockers.append("calibration_report_identity_mismatch")
        for report_field, state_field in (
            ("parser_version", "parser_version"),
            ("semantic_schema_version", "schema_version"),
            ("target_layout_profile_version", "layout_profile_version"),
        ):
            if report[report_field] != semantic_state[state_field]:
                current_blockers.append(
                    f"{state_field}:{report[report_field]}!={semantic_state[state_field]}"
                )
        if any(
            item.get("blocking")
            for item in semantic_state.get("conflicts", [])
            if isinstance(item, dict)
        ):
            current_blockers.append("blocking_semantic_conflict")
        missing_provenance = semantic_fields_without_provenance(semantic_state)
        if missing_provenance:
            current_blockers.append("production_field_provenance_incomplete")
        if current_blockers:
            raise ProjectRegistryError(
                "当前 StoryProject 与校准基线不一致：" + ", ".join(current_blockers)
            )
        identity = activate_story_state(project.root, report)
        return {"identity": identity.to_dict(), "health": _health_payload(project)}

    static_root = Path(__file__).resolve().parent / "static"
    static_root.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(repository / "NovelAgentLogo.png", media_type="image/png")

    @app.get("/")
    async def index() -> FileResponse:
        response = FileResponse(static_root / "index.html")
        response.set_cookie(
            SESSION_COOKIE,
            app.state.session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.get("/baseline-review", include_in_schema=False)
    async def author_baseline_page() -> FileResponse:
        response = FileResponse(static_root / "baseline-review.html")
        response.set_cookie(SESSION_COOKIE, app.state.session_token, httponly=True,
            samesite="strict", secure=False, path="/")
        return response

    @app.get("/upgrade", include_in_schema=False)
    async def onboarding_upgrade_page() -> FileResponse:
        response = FileResponse(static_root / "upgrade.html")
        response.set_cookie(SESSION_COOKIE, app.state.session_token, httponly=True,
            samesite="strict", secure=False, path="/")
        return response

    @app.get("/publication", include_in_schema=False)
    async def chapter_publication_page() -> FileResponse:
        response = FileResponse(static_root / "chapter-publication.html")
        response.set_cookie(SESSION_COOKIE, app.state.session_token, httponly=True,
            samesite="strict", secure=False, path="/")
        return response

    @app.get("/projects/{client_path:path}")
    async def client_route(client_path: str) -> FileResponse:
        del client_path
        response = FileResponse(static_root / "index.html")
        response.set_cookie(
            SESSION_COOKIE,
            app.state.session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    try:
        with maintenance.admit('local_writer'):
            startup_recovery.run()
    except MaintenanceBlocked:
        bootstrap_warnings.append('上次维护尚未结束；数据库初始化、自动导入、状态恢复和任务调度已延后。')
    return app


def _project_summary(record: ProjectRecord, jobs: JobStore) -> dict[str, Any]:
    identity = load_project_identity(record.root)
    prose = scan_prose_chapters(record.root)
    unfinished = jobs.list_jobs(
        project_id=record.id,
        states=("queued", "starting", "running", "pause_requested", "awaiting_input", "needs_recovery"),
        limit=10,
    )
    active = unfinished[0] if unfinished else None
    paused_chapters = {
        job.next_chapter for job in unfinished if job.state in PAUSED_JOB_STATES
    }
    inflight_generation_chapters = {
        job.next_chapter for job in unfinished if job.kind == "generation_session"
    }
    recovery = _chapter_recovery_summary(record.root, record.next_chapter)
    recovery_attention = bool(
        recovery["recoverable"]
        and record.next_chapter not in inflight_generation_chapters
    )
    pending_count = len(paused_chapters) + int(recovery_attention)
    metadata = record.metadata
    attention_recovery = recovery
    draft_state = None
    if active is not None:
        chapter = int(active.next_chapter)
        if chapter != int(record.next_chapter):
            attention_recovery = _chapter_recovery_summary(record.root, chapter)
        draft = jobs.get_project_chapter_draft(record.id, chapter)
        draft_state = draft.state if draft is not None else None
    return {
        **record.to_dict(),
        "title": record.name,
        "genre": metadata.get("genre") or "未设置题材",
        "synopsis": metadata.get("synopsis") or "",
        "summary": metadata.get("synopsis") or "",
        "language": metadata.get("language") or "zh-CN",
        "chapter_count": len(prose),
        "last_chapter": max(prose, default=0),
        "story_state_mode": identity.story_state_mode if identity else "missing",
        "experimental": identity is None or identity.story_state_mode != "strict",
        "status": active.state if active else "idle",
        "active_job": active.to_dict() if active else None,
        "pending_count": pending_count,
        "has_attention": pending_count > 0,
        "attention_label": attention_label_for_project(
            active.state if active else None,
            recovery=attention_recovery,
            draft_state=draft_state,
        ) if active else None,
    }


def _project_overview(
    record: ProjectRecord,
    outlines: OutlineBatchService,
    jobs: JobStore,
) -> dict[str, Any]:
    batches = outlines.list_batches(record.id)
    recent_jobs = jobs.list_jobs(project_id=record.id, limit=8)
    pending = [job for job in recent_jobs if job.state in PAUSED_JOB_STATES]
    prose = scan_prose_chapters(record.root)
    word_count = 0
    for chapter, candidates in prose.items():
        if len(candidates) != 1:
            continue
        try:
            current_text, _, _, _ = read_current_chapter_text(record.root, chapter)
        except (OSError, ValueError):
            continue
        word_count += _visible_character_count(current_text)
    return {
        "chapter_count": len(prose),
        "word_count": word_count,
        "word_count_label": f"{word_count:,} 字",
        "next_chapter": record.next_chapter,
        "current_tasks": [job.to_dict() for job in recent_jobs if job.state not in TERMINAL_JOB_STATES],
        "pending_count": len(pending) + sum(1 for batch in batches if batch.status == "draft"),
        "recent_history": [job.to_dict() for job in recent_jobs],
        "latest_outline_batch": batches[0].to_dict() if batches else None,
        "chapter_20_recovery": _chapter_recovery_summary(record.root, 20),
    }


def _chapter_list(record: ProjectRecord, amendments: AmendmentService) -> list[dict[str, Any]]:
    del amendments
    prose = scan_prose_chapters(record.root)
    outline_indexes = _outline_indexes(record.root)
    indexes = sorted(set(prose) | outline_indexes)
    result: list[dict[str, Any]] = []
    for chapter in indexes:
        prose_resolution = resolve_prose(record.root, chapter)
        outline_resolution = resolve_outline(record.root, chapter)
        amended = False
        current_sha = None
        word_count = 0
        if prose_resolution.path is not None and not prose_resolution.conflict:
            try:
                current_text, _, current_sha, source_kind = read_current_chapter_text(
                    record.root, chapter
                )
                amended = source_kind == "amendment_current"
                word_count = _visible_character_count(current_text)
            except Exception:
                pass
        result.append(
            {
                "chapter": chapter,
                "title": _chapter_title(prose_resolution.path or outline_resolution.path),
                "has_outline": outline_resolution.path is not None and not outline_resolution.conflict,
                "has_prose": prose_resolution.path is not None and not prose_resolution.conflict,
                "conflict": prose_resolution.conflict or outline_resolution.conflict,
                "amended": amended,
                "current_sha256": current_sha,
                "word_count": word_count,
                "word_count_label": f"{word_count:,} 字" if word_count else "—",
            }
        )
    return result


def _chapter_detail(
    record: ProjectRecord,
    chapter: int,
    amendments: AmendmentService,
) -> dict[str, Any]:
    outline = resolve_outline(record.root, chapter)
    prose = resolve_prose(record.root, chapter)
    outline_text = _read_optional_text(outline.path)
    formal_text = _read_optional_text(prose.path)
    current_text = None
    current_sha = None
    source_kind = None
    if prose.path is not None and not prose.conflict:
        current_text, _, current_sha, source_kind = read_current_chapter_text(record.root, chapter)
    versions = amendments.list_for_chapter(record.id, chapter)
    run = _latest_run_for_chapter(record.root, chapter)
    has_outline = outline.path is not None and not outline.conflict
    has_prose = prose.path is not None and not prose.conflict
    conflict = outline.conflict or prose.conflict
    return {
        "project": {"id": record.id, "title": record.name},
        "chapter": chapter,
        "title": _chapter_title(prose.path or outline.path),
        "has_outline": has_outline,
        "has_prose": has_prose,
        "conflict": conflict,
        "status": "warning" if conflict else "committed" if has_prose else "draft",
        "outline": outline_text,
        "formal_prose": formal_text,
        "current_prose": current_text,
        "current_sha256": current_sha,
        "current_source": source_kind,
        "review": _review_from_run(run),
        "run": _compact_run(run),
        "versions": [item.to_dict() for item in versions],
    }


def _pending_payload(
    record: ProjectRecord,
    outlines: OutlineBatchService,
    jobs: JobStore,
) -> dict[str, Any]:
    recent_jobs = jobs.list_jobs(project_id=record.id, limit=100)
    paused = tuple(job for job in recent_jobs if job.state in PAUSED_JOB_STATES)
    drafts = []
    with jobs.database.connection() as connection:
        rows = connection.execute(
            "SELECT id FROM drafts WHERE project_id = ? ORDER BY updated_at DESC", (record.id,)
        ).fetchall()
    for row in rows:
        drafts.append(jobs.get_draft(str(row["id"])).to_dict())
    paused_payload = []
    for job in paused:
        item = job.to_dict()
        item["resume_ready"] = jobs.resume_ready(job.id)
        paused_payload.append(item)
    active_generation_session = next(
        (
            job
            for job in recent_jobs
            if job.kind == "generation_session"
            and job.state not in TERMINAL_JOB_STATES | PAUSED_JOB_STATES
        ),
        None,
    )
    recovery_chapter = (
        paused[0].next_chapter
        if paused
        else active_generation_session.next_chapter
        if active_generation_session is not None
        else record.next_chapter
    )
    chapter_recovery = _chapter_recovery_summary(record.root, recovery_chapter)
    draft_source_conflict = _recovery_draft_source_conflict(
        root=Path(record.root),
        recovery=chapter_recovery,
        drafts=drafts,
    )
    if draft_source_conflict is not None:
        chapter_recovery = {
            **chapter_recovery,
            "draft_source_conflict": draft_source_conflict,
        }
    legacy_chapter_20_recovery = _chapter_recovery_summary(record.root, 20)
    if recovery_chapter == 20 and draft_source_conflict is not None:
        legacy_chapter_20_recovery = dict(chapter_recovery)
    active_recovery_session = active_generation_session
    if active_recovery_session is not None:
        chapter_recovery = {
            **chapter_recovery,
            "recoverable": False,
            "active_job_id": active_recovery_session.id,
        }
        if recovery_chapter == 20:
            legacy_chapter_20_recovery = dict(chapter_recovery)
    return {
        "jobs": paused_payload,
        "drafts": drafts,
        "outline_batches": [
            batch.to_dict() for batch in outlines.list_batches(record.id) if batch.status == "draft"
        ],
        "chapter_recovery": chapter_recovery,
        # Kept during the Chapter 20 vertical-slice migration for older clients.
        "chapter_20_recovery": legacy_chapter_20_recovery,
    }


def _health_payload(record: ProjectRecord) -> dict[str, Any]:
    identity = load_project_identity(record.root)
    validation = validate_story_project(
        story_project=record.root,
        chapter=record.next_chapter,
        allow_existing_prose=True,
        allow_missing_outline=True,
    )
    shadow = None
    shadow_error = None
    strict_status = None
    strict_error = None
    try:
        effective_identity = identity or project_identity_for_operation(record.root, persist=False)
        state = parse_story_project_semantic_state(
            record.root,
            record.next_chapter,
            project_identity=effective_identity,
        )
        snapshot_path = RuntimePaths.for_story_project(record.root).snapshot_path
        snapshot = load_snapshot(snapshot_path) if snapshot_path.is_file() else None
        shadow = build_story_project_shadow_report(state, snapshot=snapshot)
        if identity is not None and identity.story_state_mode == "strict":
            strict_status = evaluate_story_state_activation(identity, state)
    except Exception as exc:  # noqa: BLE001 - health page must show parser failure.
        shadow_error = {"type": type(exc).__name__, "message": str(exc)}
        if identity is not None and identity.story_state_mode == "strict":
            strict_error = dict(shadow_error)
    validation_payload = validation.to_dict()
    validation_ok = bool(validation_payload.get("ok"))
    shadow_ok = shadow is not None
    calibration_bound = bool(identity and identity.story_state_mode == "strict")
    strict_effective = bool(
        calibration_bound
        and strict_status
        and strict_status.get("configured_mode") == "strict"
        and strict_status.get("effective_mode") == "strict"
        and strict_status.get("authoritative") is True
        and strict_status.get("ready_for_next_step") is True
        and validation_ok
    )
    checks = [
        {
            "label": "StoryProject 结构预检",
            "detail": (
                "目录、章节解析和当前写入边界均可用。"
                if validation_ok
                else "存在阻断问题；修复前不会进入付费连续生产。"
            ),
            "state": "pass" if validation_ok else "failed",
        },
        {
            "label": "Shadow 语义报告",
            "detail": (
                "已生成非权威观察报告，可用于发现冲突和布局漂移。"
                if shadow_ok
                else str((shadow_error or {}).get("message") or "当前无法生成 Shadow 报告。")
            ),
            "state": "pass" if shadow_ok else "failed",
        },
        {
            "label": "目标书校准门",
            "detail": (
                "已绑定通过全部门槛的目标书校准报告。"
                if calibration_bound
                else "需用目标书样本、至少两种格式和连续 10 章 Shadow 证据生成合格报告。"
            ),
            "state": "pass" if calibration_bound else "pending",
        },
        {
            "label": "Strict 显式激活",
            "detail": (
                "Strict 当前有效；后续仍会在版本或语义证据漂移时失败关闭。"
                if strict_effective
                else (
                    str((strict_error or {}).get("message") or "当前项目已偏离校准基线，需修复并重新校准。")
                    if calibration_bound
                    else "选择合格校准报告并确认后才会激活；当前运行只能标记为实验。"
                )
            ),
            "state": "pass" if strict_effective else ("failed" if calibration_bound else "pending"),
        },
    ]
    score = (
        (35 if validation_ok else 0)
        + (25 if shadow_ok else 0)
        + (20 if calibration_bound else 0)
        + (20 if strict_effective else 0)
    )
    return {
        "project_id": record.id,
        "identity": identity.to_dict() if identity else None,
        "configured_mode": identity.story_state_mode if identity else "missing",
        "production_eligible": strict_effective,
        "run_label": "production" if strict_effective else "experimental",
        "mode_label": "Strict 生产" if strict_effective else "实验运行",
        "label": "Strict 生产" if strict_effective else "实验运行",
        "summary": (
            "目标书校准已锁定，可进入受监督的连续生产。"
            if strict_effective
            else (
                "已激活的 Strict 基线与当前项目不一致；修复并重新校准前只能实验运行。"
                if calibration_bound
                else "结构与 Shadow 检查不等于生产资格；完成目标书校准并显式激活前，只能实验运行。"
            )
        ),
        "score": score,
        "checks": checks,
        "calibration_requirements": [
            "至少 1 份目标书脱敏样本与 2 种格式变体",
            "受管块往返、必填字段精确匹配、权威精度均为 100%",
            "支持的可选字段召回率不低于 95%，且未支持结构全部被捕获",
            "连续 10 章 Shadow 无阻断冲突，并为生产字段保留完整来源",
        ],
        "validation": validation_payload,
        "shadow_report": shadow,
        "shadow_error": shadow_error,
        "strict_status": strict_status,
        "strict_error": strict_error,
    }


def _approved_batch_for_range(batches: Sequence[Any], start: int, end: int) -> Any | None:
    return next(
        (
            batch
            for batch in batches
            if batch.status == "approved"
            and batch.start_chapter <= start
            and batch.end_chapter >= end
            and batch.approval_manifest_path
            and batch.approval_receipt_sha256
        ),
        None,
    )


def _has_recovery_checkpoint(root: Path, chapter: int) -> bool:
    directory = RuntimePaths.for_story_project(root).run_dir / "locked_chapter_resolutions"
    if not directory.is_dir():
        return False
    for path in directory.glob("resolution_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("chapter_index") == chapter:
            return True
    return False


def _recovery_outline_binding(project: ProjectRecord, chapter: int) -> dict[str, str]:
    resolution = resolve_outline(project.root, chapter)
    if resolution.conflict or resolution.path is None:
        raise OutlineBatchError("recovery chapter requires one unique existing outline")
    relative = resolution.path.relative_to(project.root).as_posix()
    outline_sha = hashlib.sha256(resolution.path.read_bytes()).hexdigest()
    batch_id = f"recovery_chapter_{chapter:04d}_{outline_sha[:12]}"
    runtime = (
        Path(project.root)
        / ".novelagent"
        / "runtime"
        / "wb"
        / "recovery_outlines"
        / batch_id
    )
    runtime.mkdir(parents=True, exist_ok=True)
    manifest_path = runtime / "approval_manifest.json"
    receipt_path = runtime / "approval_receipt.json"
    manifest = {
        "batch_id": batch_id,
        "project_id": project.book_id,
        "book_id": project.book_id,
        "revision": 1,
        "proposal_sha256": outline_sha,
        "source_fingerprint": "recovery_existing_outline",
        "files": [
            {"chapter_index": chapter, "relative_path": relative, "sha256": outline_sha}
        ],
        "purpose": "locked_chapter_recovery",
    }
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    receipt = {
        "batch_id": batch_id,
        "project_id": project.book_id,
        "book_id": project.book_id,
        "revision": 1,
        "proposal_sha256": outline_sha,
        "source_fingerprint": "recovery_existing_outline",
        "manifest_sha256": manifest["manifest_sha256"],
        "purpose": "locked_chapter_recovery",
    }
    receipt["receipt_sha256"] = _canonical_hash(receipt)
    _create_or_verify_json(manifest_path, manifest)
    _create_or_verify_json(receipt_path, receipt)
    return {
        "outline_batch_id": batch_id,
        "outline_manifest_path": str(manifest_path),
        "outline_proposal_sha256": outline_sha,
        "outline_receipt_sha256": receipt["receipt_sha256"],
    }


def _create_or_verify_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OutlineBatchError(f"recovery outline evidence is unreadable: {path}") from exc
        if existing != payload:
            raise OutlineBatchError("recovery outline evidence conflicts with the current outline")
        return
    atomic_create_json(path, payload)


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_history(root: Path) -> list[dict[str, Any]]:
    runtime = RuntimePaths.for_story_project(root)
    if not runtime.run_dir.is_dir():
        return []
    values: list[tuple[int, dict[str, Any]]] = []
    for path in runtime.run_dir.glob("chapter_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            values.append((path.stat().st_mtime_ns, _compact_run(payload)))
        except (OSError, ValueError):
            continue
    values.sort(key=lambda item: item[0], reverse=True)
    return [value for _, value in values[:100]]


def _history_run_by_id(root: Path, run_id: str) -> dict[str, Any] | None:
    if not run_id:
        return None
    runtime = RuntimePaths.for_story_project(root)
    if not runtime.run_dir.is_dir():
        return None
    for path in runtime.run_dir.glob("chapter_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        compact = _compact_run(payload)
        if compact is not None and compact.get("id") == run_id:
            return payload
    return None


def _latest_run_for_chapter(root: Path, chapter: int) -> dict[str, Any] | None:
    runtime = RuntimePaths.for_story_project(root)
    candidates: list[tuple[int, dict[str, Any]]] = []
    if not runtime.run_dir.is_dir():
        return None
    for path in runtime.run_dir.glob("chapter_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            run = payload.get("run") if isinstance(payload, dict) else None
            if isinstance(run, dict) and int(run.get("chapter_index") or 0) == chapter:
                candidates.append((path.stat().st_mtime_ns, payload))
        except (OSError, ValueError, TypeError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _compact_run(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    run = payload.get("run") if isinstance(payload.get("run"), Mapping) else payload
    review = run.get("review_pipeline") if isinstance(run, Mapping) else None
    persistence = run.get("persistence") if isinstance(run, Mapping) else None
    error = run.get("error") if isinstance(run.get("error"), Mapping) else {}
    execution_evidence = (
        run.get("execution_evidence")
        if isinstance(run.get("execution_evidence"), Mapping)
        else {}
    )
    budget = (
        execution_evidence.get("budget")
        if isinstance(execution_evidence.get("budget"), Mapping)
        else {}
    )
    chapter_payload = run.get("chapter") if isinstance(run.get("chapter"), Mapping) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), Mapping) else None
    compact_validation = None
    if isinstance(validation, Mapping):
        problems: list[dict[str, Any]] = []
        raw_problems = validation.get("problems")
        if isinstance(raw_problems, list):
            for raw_problem in raw_problems[:8]:
                if not isinstance(raw_problem, Mapping):
                    continue
                evidence: list[dict[str, str]] = []
                raw_evidence = raw_problem.get("evidence")
                if isinstance(raw_evidence, list):
                    for raw_item in raw_evidence[:4]:
                        if not isinstance(raw_item, Mapping):
                            continue
                        kind = raw_item.get("kind")
                        value = raw_item.get("value")
                        if isinstance(kind, str) and isinstance(value, str):
                            evidence.append({"kind": kind, "value": value})
                problems.append(
                    {
                        "code": raw_problem.get("code"),
                        "message": raw_problem.get("message"),
                        "severity": raw_problem.get("severity"),
                        "blocking": raw_problem.get("blocking") is True,
                        "repair_action": raw_problem.get("repair_action"),
                        "repair_hint": raw_problem.get("repair_hint"),
                        "evidence": evidence,
                    }
                )
        compact_validation = {
            "ok": validation.get("ok") is True,
            "blocking_problem_count": validation.get("blocking_problem_count"),
            "manual_review_count": validation.get("manual_review_count"),
            "problems": problems,
        }
    return {
        "id": run.get("id"),
        "chapter_index": run.get("chapter_index"),
        "status": run.get("status"),
        "committed": run.get("committed"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "repair_attempts": run.get("repair_attempts"),
        "review_status": resolve_review_status(payload),
        "persistence_state": persistence.get("state") if isinstance(persistence, Mapping) else None,
        "publication": persistence.get("publication") if isinstance(persistence, Mapping) else None,
        "error_type": error.get("type"),
        "error_message": error.get("message"),
        "provider_calls": budget.get("provider_calls"),
        "unsettled_attempt_count": budget.get("unsettled_attempt_count"),
        "total_input_tokens": budget.get("total_input_tokens"),
        "total_output_tokens": budget.get("total_output_tokens"),
        "estimated_cost": budget.get("estimated_cost"),
        "chapter_chars": chapter_payload.get("chars"),
        "validation": compact_validation,
    }


def _review_from_run(payload: Mapping[str, Any] | None) -> Any:
    if not isinstance(payload, Mapping):
        return None
    run = payload.get("run") if isinstance(payload.get("run"), Mapping) else {}
    return payload.get("review_pipeline") or run.get("review_pipeline")


def _chapter_recovery_summary(root: Path, chapter: int) -> dict[str, Any]:
    runtime = RuntimePaths.for_story_project(root)
    run = _latest_run_for_chapter(root, chapter)
    resolutions = runtime.run_dir / "locked_chapter_resolutions"
    latest_checkpoint = None
    if resolutions.is_dir():
        paths = sorted(resolutions.glob("resolution_*.json"), key=lambda path: path.stat().st_mtime_ns)
        for path in reversed(paths):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("chapter_index") == chapter:
                latest_checkpoint = {
                    "path": str(path),
                    "action": value.get("action"),
                    "reason": value.get("reason"),
                    "source_run_id": value.get("source_run_id"),
                    "reusable_scene_count": len(value.get("scenes") or []),
                    "expected_scene_count": value.get("expected_scene_count"),
                    "has_complete_draft": isinstance(value.get("complete_draft"), dict),
                    "complete_draft_sha256": (
                        value.get("complete_draft", {}).get("sha256")
                        if isinstance(value.get("complete_draft"), dict)
                        else None
                    ),
                    "resolution_hash": value.get("resolution_hash"),
                }
                break
    run_record = run.get("run") if isinstance(run, dict) and isinstance(run.get("run"), dict) else {}
    latest_run_id = str(run_record.get("id") or "").strip() or None
    verified_source = (
        verified_final_recovery_draft(
            run_dir=runtime.run_dir,
            run_id=latest_run_id,
        )
        if latest_run_id is not None
        else None
    )
    verified_source_sha256 = (
        str(verified_source.get("sha256") or "").strip() or None
        if isinstance(verified_source, Mapping)
        else None
    )
    reset_discards_latest_run = bool(
        latest_checkpoint
        and latest_checkpoint.get("action") == "reset"
        and latest_run_id
        and latest_checkpoint.get("source_run_id") == latest_run_id
    )
    # A reset is append-only evidence that its source run was deliberately
    # abandoned.  Keep both records visible in history, but do not surface the
    # discarded run as a fresh recovery task after the workbench restarts.
    recoverable = bool(
        run
        and run_record.get("committed") is not True
        and not reset_discards_latest_run
    )
    checkpoint_matches_latest_run = bool(
        latest_checkpoint
        and latest_checkpoint.get("action") != "reset"
        and latest_run_id
        and latest_checkpoint.get("source_run_id") == latest_run_id
    )
    if (
        checkpoint_matches_latest_run
        and latest_checkpoint.get("has_complete_draft")
        and latest_checkpoint.get("reason") != "manual_repaired_draft_provided"
    ):
        checkpoint_matches_latest_run = bool(
            verified_source_sha256
            and latest_checkpoint.get("complete_draft_sha256")
            == verified_source_sha256
        )
    summary = {
        "chapter": chapter,
        "formal_prose_exists": bool(resolve_prose(root, chapter).path),
        "latest_run": _compact_run(run),
        "recoverable": recoverable,
        "checkpoint": latest_checkpoint,
        "checkpoint_matches_latest_run": checkpoint_matches_latest_run,
        "verified_final_sha256": verified_source_sha256,
        "needs_reclassification": bool(recoverable and not checkpoint_matches_latest_run),
    }
    summary["durable_polished_draft_available"] = durable_polished_draft_available(summary)
    return summary


def _recovery_draft_source_conflict(
    *,
    root: Path,
    recovery: Mapping[str, Any],
    drafts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Expose a verified recovery-source conflict without replacing author text.

    Recovery checkpoints are append-only domain evidence.  If the author has
    already edited the prior workbench draft, a later verified source must be
    presented for comparison rather than silently written over that draft.
    """

    checkpoint = recovery.get("checkpoint")
    chapter = int(recovery.get("chapter") or 0)
    if (
        chapter < 1
        or not recovery.get("checkpoint_matches_latest_run")
        or not isinstance(checkpoint, Mapping)
        or checkpoint.get("reason") == "manual_repaired_draft_provided"
    ):
        return None
    current = next(
        (
            draft
            for draft in drafts
            if isinstance(draft, Mapping) and int(draft.get("chapter") or 0) == chapter
        ),
        None,
    )
    if not isinstance(current, Mapping):
        return None
    current_sha256 = str(current.get("sha256") or "")
    base_sha256 = str(current.get("base_sha256") or "")
    if (
        not current_sha256
        or not base_sha256
        or current_sha256 == base_sha256
    ):
        return None
    source_sha256 = str(checkpoint.get("complete_draft_sha256") or "")
    if not source_sha256:
        return None
    checkpoint_value = checkpoint.get("path")
    if not isinstance(checkpoint_value, str) or not checkpoint_value:
        return None
    resolution_root = (
        RuntimePaths.for_story_project(root).run_dir / "locked_chapter_resolutions"
    ).resolve()
    try:
        checkpoint_path = Path(checkpoint_value).resolve(strict=True)
        checkpoint_path.relative_to(resolution_root)
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    complete_draft = payload.get("complete_draft") if isinstance(payload, Mapping) else None
    incoming_text = complete_draft.get("text") if isinstance(complete_draft, Mapping) else None
    declared_sha256 = (
        str(complete_draft.get("sha256") or "")
        if isinstance(complete_draft, Mapping)
        else ""
    )
    if not isinstance(incoming_text, str):
        return None
    if hashlib.sha256(incoming_text.encode("utf-8")).hexdigest() != source_sha256:
        return None
    normalized_text = incoming_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_text or len(normalized_text.encode("utf-8")) > 4 * 1024 * 1024:
        return None
    normalized_text += "\n"
    incoming_sha256 = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    if (
        declared_sha256 != source_sha256
        or recovery.get("verified_final_sha256") != source_sha256
        or incoming_sha256 == current_sha256
    ):
        return None
    return {
        "code": "draft_source_upgrade_conflict",
        "chapter": chapter,
        "draft_id": current.get("id"),
        "current_sha256": current_sha256,
        "base_sha256": base_sha256,
        "incoming_sha256": incoming_sha256,
        "source_sha256": source_sha256,
        "incoming_text": normalized_text,
        "automatic_overwrite": False,
    }


def _outline_indexes(root: Path) -> set[int]:
    directory = root / "大纲"
    if not directory.is_dir():
        return set()
    return {
        index
        for path in directory.iterdir()
        if path.is_file()
        for index in [outline_chapter_index(path)]
        if index is not None
    }


def _provider_status() -> dict[str, Any]:
    try:
        config = get_config()
        openai_ready = bool(config.openai_api_key)
        model = config.openai_model
    except Exception:
        openai_ready = bool(os.environ.get("OPENAI_API_KEY"))
        model = os.environ.get("OPENAI_MODEL")
    anthropic_ready = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {
        "ready": openai_ready,
        "openai_ready": openai_ready,
        "anthropic_ready": anthropic_ready,
        "model": model,
        "credentials_exposed": False,
    }


def _import_active_project(repository: Path, projects: ProjectRegistry) -> None:
    marker = repository / ".active-book"
    candidates: list[Path] = []
    if marker.is_file():
        value = marker.read_text(encoding="utf-8-sig").strip()
        if value:
            candidate = Path(value)
            candidates.append(candidate if candidate.is_absolute() else repository / candidate)
    books_root = repository / "books"
    if books_root.is_dir():
        candidates.extend(path for path in books_root.iterdir() if path.is_dir())
    seen: set[Path] = set()
    for candidate in candidates:
        canonical = candidate.resolve()
        if canonical in seen:
            continue
        seen.add(canonical)
        if all((canonical / name).is_dir() for name in ("设定", "大纲", "正文", "追踪")):
            projects.import_project(canonical)


def _configured_workspaces(repository: Path) -> tuple[Path, ...]:
    configured = os.environ.get("NOVELAGENT_WORKSPACES", "")
    if configured.strip():
        values = [Path(item.strip()) for item in configured.split(os.pathsep) if item.strip()]
        if values:
            return tuple(values)
    return (repository,)


def _local_app_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "NovelAgent"
    return Path.home() / ".local" / "share" / "NovelAgent"


def _safe_directory_name(value: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value)).strip(" .")
    if not text:
        raise ProjectRegistryError("project name cannot produce an empty directory name")
    return text[:120]


def _derived_idempotency_key(*parts: Any) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "derived-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _host_name(host_header: str) -> str:
    text = host_header.strip().casefold()
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    return text.rsplit(":", 1)[0] if ":" in text else text


def _same_origin(origin: str, host_header: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.netloc.casefold() == host_header.casefold()


def _read_optional_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _chapter_title(path: Path | None) -> str:
    if path is None:
        return "未命名章节"
    stem = path.stem
    match = re.search(r"章[_：:\s-]*(.+)$", stem)
    return match.group(1).strip() if match else stem


def _visible_character_count(text: str) -> int:
    return sum(1 for character in text if not character.isspace())


def _attention_label(state: str) -> str:
    return {
        "awaiting_input": "需要作者检查并确认",
        "needs_recovery": "Provider 状态待安全恢复",
    }.get(state, "有待处理事项")


def _candidate_result_message(candidate: Mapping[str, Any]) -> str:
    status = str(candidate.get("status") or "")
    if status == "completed":
        if isinstance(candidate.get("draft"), Mapping):
            return "正文候选已生成并复制为可编辑工作台草稿；正式正文和权威状态仍未修改。"
        return "正文候选已生成；确认后可复制为可编辑工作台草稿，正式正文和权威状态仍未修改。"
    if status == "provider_uncertain":
        return "Provider 结果不确定；系统已停止自动调用，正式正文和权威状态均未修改。"
    if status == "stale":
        return "生成期间可信输入发生变化；候选已隔离保留，正式正文和权威状态均未修改。"
    if status == "failed":
        return "正文候选生成失败；证据已保留，正式正文和权威状态均未修改。"
    if status == "cancelled":
        return "正文候选已取消；正式正文和权威状态均未修改。"
    if status == "abandoned":
        return "该不确定 Provider 结果已明确放弃采用；证据仍保留。"
    if status == "needs_recovery":
        return "候选已有持久 Intent，但没有终态记录；需作者确认后才能恢复，系统不会自动调用 Provider。"
    return "正文候选状态已更新；正式正文和权威状态均未修改。"


def _fact_extraction_result_message(extraction: Mapping[str, Any]) -> str:
    status = str(extraction.get("status") or "")
    if status == "completed":
        if isinstance(extraction.get("fact_review"), Mapping):
            return "事实候选已提取为作者审核卡；尚未写入权威状态，也未修改正式正文。"
        if extraction.get("source_state") == "stale":
            return "事实候选已保留，但正文或权威状态已经变化；请从最新草稿重新提取。"
        return "事实候选已提取并保留；尚未写入权威状态，也未修改正式正文。"
    if status == "provider_uncertain":
        return "Provider 结果不确定；系统已停止新的模型调用，草稿、正式正文和权威状态均未修改。"
    if status == "needs_recovery":
        return "事实提取已有持久 Intent，但没有终态记录；需作者确认后才能安全恢复。"
    if status == "failed":
        return "事实候选提取失败且未自动重试；草稿、正式正文和权威状态均未修改。"
    if status == "cancelled":
        return "事实候选提取已取消；草稿、正式正文和权威状态均未修改。"
    if status == "abandoned":
        return "该不确定 Provider 结果已明确放弃采用；原始证据仍保留。"
    return "事实候选提取状态已更新；草稿、正式正文和权威状态均未修改。"


def _error_code(exc: Exception) -> str:
    explicit = getattr(exc, "code", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).casefold()


__all__ = ["SESSION_COOKIE", "create_app"]
