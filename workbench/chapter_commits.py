"""Server-side preparation adapter; publication is added separately in T3.2."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
from typing import Any, Callable

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_commit_prepare import prepare_reviewed_chapter
from core.story_project.read_set import verify_story_project_read_set
from workbench.chapter_candidates import CandidateDraftRecord, CandidateDraftStore
from workbench.chapter_facts import ChapterFactReviewRecord, ChapterFactReviewService
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRecord, ProjectRegistry


class ChapterCommitConflict(ValueError):
    code = "chapter_commit_conflict"


class ChapterCommitIntegrityError(ValueError):
    code = "chapter_commit_integrity_error"


class ChapterCommitPreparationService:
    def __init__(self, *, database: WorkbenchDatabase, projects: ProjectRegistry,
                 candidate_drafts: CandidateDraftStore, fact_reviews: ChapterFactReviewService,
                 context_builder_factory: Callable[[Path], ChapterCandidateContextBuilder] | None = None):
        if any(store.database.path != database.path for store in
               (projects, candidate_drafts, fact_reviews.projects, fact_reviews.reviews, fact_reviews.candidate_drafts)):
            raise ChapterCommitIntegrityError("commit input stores must use the same database")
        self.database = database
        self.projects = projects
        self.drafts = candidate_drafts
        self.fact_reviews = fact_reviews
        # Internal dependency injection for isolated format integration. No
        # request can supply context, authority, source loader or prepared state.
        self.context_builder_factory = context_builder_factory or ChapterCandidateContextBuilder

    def prepare(self, *, project_id: str, chapter: int, review_id: str,
                expected_draft_revision: int, expected_review_revision: int,
                expected_draft_sha256: str, expected_facts_sha256: str,
                idempotency_key: str) -> dict[str, Any]:
        for name, value in (("chapter",chapter), ("draft revision",expected_draft_revision),
                            ("review revision",expected_review_revision)):
            if type(value) is not int or value < 1:
                raise ChapterCommitConflict(f"{name} must be a positive integer")
        for name, value in (("draft hash",expected_draft_sha256),("facts hash",expected_facts_sha256)):
            if not isinstance(value,str) or re.fullmatch(r"[0-9a-f]{64}",value) is None:
                raise ChapterCommitConflict(f"{name} must be lowercase SHA-256")
        project = self.projects.get(project_id)
        draft, review = self._read_pair(review_id)
        if (draft.project_id != project.id or review.project_id != project.id
                or draft.chapter != chapter or review.chapter != chapter):
            raise ChapterCommitConflict("review does not belong to this project and chapter")
        if (draft.revision != expected_draft_revision or draft.sha256 != expected_draft_sha256
                or review.revision != expected_review_revision or review.facts_sha256 != expected_facts_sha256):
            raise ChapterCommitConflict("draft or fact review changed; reload the preview")
        self._verify_pair(draft, review)
        # Reuse Candidate verification: input, provenance, result/prose hashes,
        # persisted identity/authority and the exact original read set.
        verified_draft, intent = self.fact_reviews.verify_draft_source(draft.id)
        if verified_draft != draft:
            raise ChapterCommitConflict("draft changed while reading Candidate evidence")
        self._verify_candidate_binding(project, review, intent)
        trusted = self.context_builder_factory(project.root).build(chapter)
        if (trusted.context_sha256 != intent["source"]["context_sha256"]
                or trusted.story_project_context["read_set"] != intent["source"]["story_project_read_set"]):
            raise ChapterCommitConflict("current context differs from the confirmed Candidate")
        result = prepare_reviewed_chapter(trusted_context=trusted, project_id=project.id,
            draft=draft.to_dict(), review=asdict(review), idempotency_key=idempotency_key)
        # End the first read snapshot before this check. A WAL reader held open
        # throughout calculation would hide concurrent author edits.
        current_draft, current_review = self._read_pair(review_id)
        if current_draft != draft or current_review != review:
            raise ChapterCommitConflict("draft or fact review changed during preparation")
        current_project = self.projects.get(project.id)
        if (current_project.root, current_project.book_id) != (project.root, project.book_id):
            raise ChapterCommitConflict("project registration changed during preparation")
        # Also revalidate artifacts after calculation, without generating again.
        after_draft, after_intent = self.fact_reviews.verify_draft_source(draft.id)
        if after_draft != draft or after_intent != intent:
            raise ChapterCommitConflict("Candidate evidence changed during preparation")
        self._verify_candidate_binding(project, current_review, after_intent)
        verify_story_project_read_set(result["read_set"], phase="prepare")
        return result

    def _read_pair(self, review_id: str) -> tuple[CandidateDraftRecord, ChapterFactReviewRecord]:
        with self.database.transaction(immediate=False) as connection:
            review = self.fact_reviews.reviews.get(review_id, connection=connection)
            draft = self.drafts.get(review.draft_id, connection=connection)
        return draft, review

    @staticmethod
    def _verify_pair(draft: CandidateDraftRecord, review: ChapterFactReviewRecord):
        # Store decoding already verifies review confirmation hash, raw source,
        # cards, evidence quotes and immutable confirmed-state metadata.
        if review.state != "confirmed":
            raise ChapterCommitConflict("fact review has not been confirmed by the author")
        if (review.candidate_id != draft.candidate_id or review.draft_id != draft.id
                or review.source_draft_revision != draft.revision
                or review.source_draft_sha256 != draft.sha256 or review.source_text != draft.text):
            raise ChapterCommitConflict("confirmed review is stale for the current draft")

    @staticmethod
    def _verify_candidate_binding(project: ProjectRecord, review: ChapterFactReviewRecord, intent: dict):
        authority = {name:intent["authority"].get(name) for name in
                     ("mode","authority_epoch","head_event_hash","identity_revision")}
        authority["authority_projection_sha256"] = intent["source"]["authority_projection_sha256"]
        if (intent["book_id"] != project.book_id or intent["chapter_index"] != review.chapter
                or intent["candidate_id"] != review.candidate_id
                or intent["intent_hash"] != review.candidate_intent_hash
                or intent["source"]["context_sha256"] != review.context_sha256
                or authority != review.authority):
            raise ChapterCommitIntegrityError("fact review is not bound to verified Candidate evidence")
        if review.source_extraction_id is not None:
            # Confirmed model suggestions must retain their extraction evidence.
            # Import lazily to keep preparation separate from extraction execution.
            from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
            store = ChapterFactExtractionStore(project.root)
            record = store.load_record(review.source_extraction_id)
            extraction = store.load_intent(review.source_extraction_id)
            if (record["record_hash"] != review.source_extraction_record_hash or record["status"] != "completed"
                    or extraction["book_id"] != project.book_id or extraction["chapter_index"] != review.chapter
                    or extraction["candidate"] != {"candidate_id":review.candidate_id,"intent_hash":review.candidate_intent_hash}
                    or extraction["draft"]["draft_id"] != review.draft_id
                    or extraction["draft"]["revision"] != review.source_draft_revision
                    or extraction["draft"]["sha256"] != review.source_draft_sha256
                    or store.load_source(review.source_extraction_id) != review.source_text
                    or extraction["context"]["context_sha256"] != review.context_sha256
                    or extraction["authority"] != intent["authority"]):
                raise ChapterCommitIntegrityError("fact extraction evidence changed after review")
