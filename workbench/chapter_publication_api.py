"""Explicit reviewed publication routes; legacy services retain their format gate."""
from __future__ import annotations

import asyncio

from fastapi import Body, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.read_set import StoryProjectSourceDriftError
from core.quality.reviewed_disposition import ReviewedDispositionError
from workbench.book_ownership import BookOwnershipError
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_commits import (
    ChapterCommitConflict, ChapterCommitIntegrityError, ChapterCommitPreparationService, ChapterCommitRecoveryRequired,
)
from workbench.chapter_facts import ChapterFactReviewService
from workbench.chapter_publication import ChapterPublicationService
from workbench.instance import InstanceAlreadyRunning
from workbench.reviewed_projects import ReviewedProjectRegistry

_INPUT_FIELDS = {"review_id", "expected_draft_revision", "expected_review_revision",
                 "expected_draft_sha256", "expected_facts_sha256"}


def install_chapter_publication_routes(app, *, database, allowed_workspaces, machine_store=None):
    # Only this workflow receives the explicit v3 registry/context. Installing
    # these routes cannot grant the old CLI, worker or import route a v3 permit.
    projects = ReviewedProjectRegistry(database, allowed_workspaces=allowed_workspaces)
    drafts = CandidateDraftStore(database)
    facts = ChapterFactReviewService(database=database, projects=projects, candidate_drafts=drafts,
        context_builder_factory=ReviewedChapterCandidateContextBuilder)
    preparations = ChapterCommitPreparationService(database=database, projects=projects,
        candidate_drafts=drafts, fact_reviews=facts, context_builder_factory=ReviewedChapterCandidateContextBuilder)
    service = ChapterPublicationService(preparations=preparations, machine_store=machine_store)
    app.state.chapter_publication = service

    async def publication_error(request, exc):
        status = 500 if isinstance(exc, ChapterCommitIntegrityError) and not isinstance(exc, ChapterCommitRecoveryRequired) else 409
        code = getattr(exc, "code", "book_ownership_busy")
        message = ("这本作品当前没有可用的写入资格。请在活动副本中操作，或先完成恢复交接。"
                   if isinstance(exc, BookOwnershipError) else str(exc))
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    for error_type in (ChapterCommitConflict, ChapterCommitIntegrityError, ChapterCommitRecoveryRequired,
                       BookOwnershipError, InstanceAlreadyRunning, StoryProjectSourceDriftError,ReviewedDispositionError):
        app.add_exception_handler(error_type, publication_error)

    def request_values(project_id, chapter, payload, key, *, commit=False):
        expected = _INPUT_FIELDS | ({"author_confirmed"} if commit else set())
        if set(payload) != expected:
            raise HTTPException(422, "提交只接受审核编号、草稿与审核版本及摘要；不接受客户端质量或状态结果。")
        if commit and payload["author_confirmed"] is not True:
            raise HTTPException(422, "请明确确认发布本章。")
        return {"project_id": project_id, "chapter": chapter,
                **{name: payload[name] for name in _INPUT_FIELDS}, "idempotency_key": key}

    @app.get("/api/projects/{project_id}/chapters/{chapter}/publication-input")
    async def publication_input(project_id: str, chapter: int = Path(ge=1), review_id: str = Query(min_length=1)):
        return await asyncio.to_thread(service.review_input, project_id=project_id, chapter=chapter, review_id=review_id)

    @app.post("/api/projects/{project_id}/chapters/{chapter}/publication-preview")
    async def preview(project_id: str, chapter: int = Path(ge=1), payload: dict = Body(...),
                      idempotency_key: str = Header(..., alias="Idempotency-Key")):
        route = request_values(project_id, chapter, payload, idempotency_key)
        return await asyncio.to_thread(service.preview, **route)

    @app.post("/api/projects/{project_id}/chapters/{chapter}/publication")
    async def commit(project_id: str, chapter: int = Path(ge=1), payload: dict = Body(...),
                     idempotency_key: str = Header(..., alias="Idempotency-Key")):
        route = request_values(project_id, chapter, payload, idempotency_key, commit=True)
        result = await asyncio.to_thread(service.commit, **route)
        # A safely rolled back request is HTTP 200 with committed=false. The
        # caller must inspect publication_verified, not infer success from HTTP.
        return {"publication": result}

    @app.post("/api/projects/{project_id}/chapters/{chapter}/publication-status")
    async def resolve(project_id: str, chapter: int = Path(ge=1), payload: dict = Body(...),
                      idempotency_key: str = Header(..., alias="Idempotency-Key")):
        route = request_values(project_id, chapter, payload, idempotency_key)
        return {"publication": await asyncio.to_thread(service.resolve, **route)}

    @app.post("/api/projects/{project_id}/publication-recovery")
    async def recover(project_id: str, payload: dict = Body(...)):
        if set(payload) != {"author_confirmed"} or payload["author_confirmed"] is not True:
            raise HTTPException(422, "请明确确认恢复原事务。")
        return await asyncio.to_thread(service.recover, project_id=project_id)

    return service
