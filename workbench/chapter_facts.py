from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.engine.chapter_candidate import (
    ChapterCandidateIntegrityError,
    ChapterCandidateStore,
)
from core.engine.chapter_candidate_context import (
    ChapterCandidateContextBuilder,
    ChapterCandidateContextError,
)
from core.engine.chapter_fact_candidate import (
    ChapterFactCandidateError,
    normalize_fact_proposal,
    normalize_fact_review_items,
    require_confirmed_fact_review,
)
from core.engine.chapter_fact_extraction import (
    ChapterFactExtractionError,
    ChapterFactExtractionStore,
)
from core.engine.safe_paths import SafePathError
from core.schema import SchemaValidationError
from core.story_project.identity import ProjectIdentityError
from core.story_project.read_set import StoryProjectSourceDriftError
from workbench.chapter_candidates import (
    CandidateDraftRecord,
    CandidateDraftStore,
    ChapterCandidateWorkbenchError,
)
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry


_REVIEW_ID = re.compile(r"^fact_review_[0-9a-f]{32}$")
_EXTRACTION_ID = re.compile(r"^fact_extraction_[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
FACT_REVIEW_RECORD_TYPE = "chapter_fact_review"
FACT_REVIEW_SCHEMA_VERSION = "1.0"


class ChapterFactReviewError(RuntimeError):
    code = "chapter_fact_review_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class ChapterFactReviewConflict(ChapterFactReviewError):
    code = "chapter_fact_review_conflict"


class ChapterFactReviewNotFound(ChapterFactReviewError):
    code = "chapter_fact_review_not_found"


class ChapterFactReviewIntegrity(ChapterFactReviewError):
    code = "chapter_fact_review_integrity_error"


@dataclass(frozen=True)
class ChapterFactReviewRecord:
    record_type: str
    schema_version: str
    id: str
    project_id: str
    chapter: int
    candidate_id: str
    candidate_intent_hash: str
    draft_id: str
    source_draft_revision: int
    source_draft_sha256: str
    source_text: str
    context_sha256: str
    source_extraction_id: str | None
    source_extraction_record_hash: str | None
    authority: dict[str, Any]
    authority_sha256: str
    facts: list[dict[str, Any]]
    facts_sha256: str
    revision: int
    state: str
    confirmation_sha256: str | None
    created_at: str
    updated_at: str
    confirmed_at: str | None


class ChapterFactReviewStore:
    """Revision-CAS storage for non-authoritative, author-reviewed fact cards."""

    def __init__(self, database: WorkbenchDatabase) -> None:
        self.database = database
        self.database.initialize()
        with self.database.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chapter_fact_reviews (
                    id TEXT PRIMARY KEY,
                    record_type TEXT NOT NULL DEFAULT 'chapter_fact_review',
                    schema_version TEXT NOT NULL DEFAULT '1.0',
                    project_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL CHECK (chapter >= 1),
                    candidate_id TEXT NOT NULL,
                    candidate_intent_hash TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    source_draft_revision INTEGER NOT NULL CHECK (source_draft_revision >= 1),
                    source_draft_sha256 TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    context_sha256 TEXT NOT NULL,
                    source_extraction_id TEXT,
                    source_extraction_record_hash TEXT,
                    authority_json TEXT NOT NULL,
                    authority_sha256 TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    facts_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                    state TEXT NOT NULL CHECK (state IN ('editing', 'confirmed')),
                    confirmation_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY (draft_id) REFERENCES chapter_candidate_drafts(id) ON DELETE CASCADE,
                    UNIQUE (draft_id, source_draft_revision, source_draft_sha256)
                );

                CREATE INDEX IF NOT EXISTS idx_chapter_fact_reviews_project_chapter
                ON chapter_fact_reviews(project_id, chapter, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_chapter_fact_reviews_draft
                ON chapter_fact_reviews(draft_id, source_draft_revision DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(chapter_fact_reviews)"
                ).fetchall()
            }
            if "record_type" not in columns:
                connection.execute(
                    "ALTER TABLE chapter_fact_reviews ADD COLUMN "
                    "record_type TEXT NOT NULL DEFAULT 'chapter_fact_review'"
                )
            if "schema_version" not in columns:
                connection.execute(
                    "ALTER TABLE chapter_fact_reviews ADD COLUMN "
                    "schema_version TEXT NOT NULL DEFAULT '1.0'"
                )
            if "source_extraction_id" not in columns:
                connection.execute(
                    "ALTER TABLE chapter_fact_reviews ADD COLUMN source_extraction_id TEXT"
                )
            if "source_extraction_record_hash" not in columns:
                connection.execute(
                    "ALTER TABLE chapter_fact_reviews ADD COLUMN "
                    "source_extraction_record_hash TEXT"
                )

    def create(
        self,
        *,
        project_id: str,
        chapter: int,
        candidate_id: str,
        candidate_intent_hash: str,
        draft_id: str,
        source_draft_revision: int,
        source_draft_sha256: str,
        source_text: str,
        context_sha256: str,
        authority: Mapping[str, Any],
        facts: Sequence[Mapping[str, Any]],
        source_extraction_id: str | None = None,
        source_extraction_record_hash: str | None = None,
    ) -> ChapterFactReviewRecord:
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise ChapterFactReviewError("chapter must be a positive integer")
        source_revision = _positive_revision(source_draft_revision)
        source_sha = _sha256("source_draft_sha256", source_draft_sha256)
        if _sha256_text(source_text) != source_sha:
            raise ChapterFactReviewIntegrity("fact review source text hash mismatch")
        intent_hash = _sha256("candidate_intent_hash", candidate_intent_hash)
        context_hash = _sha256("context_sha256", context_sha256)
        extraction_id, extraction_record_hash = _extraction_binding(
            source_extraction_id,
            source_extraction_record_hash,
        )
        authority_value = _json_object(authority, "authority")
        authority_json = _canonical_json(authority_value)
        authority_sha = _sha256_text(authority_json)
        try:
            normalized_facts = normalize_fact_review_items(
                _fact_item_list(facts, "facts"),
                chapter_text=source_text,
            )
        except ChapterFactCandidateError as exc:
            raise ChapterFactReviewError(
                _candidate_error_message(exc), code=exc.code
            ) from exc
        facts_json = _canonical_json(normalized_facts)
        facts_sha = _sha256_text(facts_json)
        review_id = _review_id(draft_id, source_revision, source_sha)
        now = _utc_now()

        with self.database.transaction() as connection:
            _require_current_draft_binding(
                connection,
                project_id=str(project_id),
                chapter=chapter,
                candidate_id=str(candidate_id),
                draft_id=str(draft_id),
                revision=source_revision,
                sha256=source_sha,
            )
            existing = connection.execute(
                "SELECT * FROM chapter_fact_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
            if existing is not None:
                record = _review_from_row(existing)
                expected = (
                    str(project_id),
                    chapter,
                    candidate_id,
                    intent_hash,
                    draft_id,
                    source_revision,
                    source_sha,
                    source_text,
                    context_hash,
                    extraction_id,
                    extraction_record_hash,
                    authority_sha,
                    facts_sha,
                )
                actual = (
                    record.project_id,
                    record.chapter,
                    record.candidate_id,
                    record.candidate_intent_hash,
                    record.draft_id,
                    record.source_draft_revision,
                    record.source_draft_sha256,
                    record.source_text,
                    record.context_sha256,
                    record.source_extraction_id,
                    record.source_extraction_record_hash,
                    record.authority_sha256,
                    record.facts_sha256,
                )
                if actual != expected:
                    raise ChapterFactReviewConflict(
                        "fact review id is already bound to different source or facts"
                    )
                return record
            connection.execute(
                """
                INSERT INTO chapter_fact_reviews(
                    id, record_type, schema_version,
                    project_id, chapter, candidate_id, candidate_intent_hash,
                    draft_id, source_draft_revision, source_draft_sha256,
                    source_text, context_sha256,
                    source_extraction_id, source_extraction_record_hash,
                    authority_json, authority_sha256,
                    facts_json, facts_sha256, revision, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'editing', ?, ?)
                """,
                (
                    review_id,
                    FACT_REVIEW_RECORD_TYPE,
                    FACT_REVIEW_SCHEMA_VERSION,
                    str(project_id),
                    chapter,
                    str(candidate_id),
                    intent_hash,
                    str(draft_id),
                    source_revision,
                    source_sha,
                    source_text,
                    context_hash,
                    extraction_id,
                    extraction_record_hash,
                    authority_json,
                    authority_sha,
                    facts_json,
                    facts_sha,
                    now,
                    now,
                ),
            )
        return self.get(review_id)

    def get(self, review_id: str, *, connection: Any | None = None) -> ChapterFactReviewRecord:
        review_id = _validated_review_id(review_id)
        if connection is None:
            with self.database.connection() as current:
                return self.get(review_id, connection=current)
        else:
            row = connection.execute(
                "SELECT * FROM chapter_fact_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        if row is None:
            raise ChapterFactReviewNotFound("unknown fact review")
        return _review_from_row(row)

    def list_for_draft(self, draft_id: str) -> tuple[ChapterFactReviewRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chapter_fact_reviews
                WHERE draft_id = ?
                ORDER BY source_draft_revision DESC, created_at DESC, id DESC
                """,
                (str(draft_id),),
            ).fetchall()
        return tuple(_review_from_row(row) for row in rows)

    def update_facts(
        self,
        review_id: str,
        *,
        facts: Sequence[Mapping[str, Any]],
        expected_revision: int,
    ) -> ChapterFactReviewRecord:
        review_id = _validated_review_id(review_id)
        expected = _positive_revision(expected_revision)
        current = self.get(review_id)
        if current.state != "editing":
            raise ChapterFactReviewConflict(
                "confirmed fact review is immutable; create a review for a newer draft revision"
            )
        try:
            normalized = normalize_fact_review_items(
                _fact_item_list(facts, "facts"),
                chapter_text=current.source_text,
            )
        except ChapterFactCandidateError as exc:
            raise ChapterFactReviewError(
                _candidate_error_message(exc), code=exc.code
            ) from exc
        normalized = _author_update_transition(current.facts, normalized)
        facts_json = _canonical_json(normalized)
        facts_sha = _sha256_text(facts_json)
        now = _utc_now()
        with self.database.transaction() as connection:
            _require_current_draft_binding(
                connection,
                project_id=current.project_id,
                chapter=current.chapter,
                candidate_id=current.candidate_id,
                draft_id=current.draft_id,
                revision=current.source_draft_revision,
                sha256=current.source_draft_sha256,
            )
            updated = connection.execute(
                """
                UPDATE chapter_fact_reviews
                SET facts_json = ?, facts_sha256 = ?, revision = revision + 1,
                    confirmation_sha256 = NULL, confirmed_at = NULL, updated_at = ?
                WHERE id = ? AND revision = ? AND state = 'editing'
                """,
                (facts_json, facts_sha, now, review_id, expected),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT revision, state FROM chapter_fact_reviews WHERE id = ?",
                    (review_id,),
                ).fetchone()
                if row is None:
                    raise ChapterFactReviewNotFound("unknown fact review")
                raise ChapterFactReviewConflict(
                    f"fact review changed: expected revision {expected}, current {row['revision']}"
                )
        return self.get(review_id)

    def confirm(
        self,
        review_id: str,
        *,
        expected_revision: int,
        expected_facts_sha256: str,
    ) -> ChapterFactReviewRecord:
        review_id = _validated_review_id(review_id)
        expected = _positive_revision(expected_revision)
        expected_facts = _sha256("expected_facts_sha256", expected_facts_sha256)
        current = self.get(review_id)
        if current.revision != expected or current.facts_sha256 != expected_facts:
            raise ChapterFactReviewConflict("fact review changed before confirmation")
        try:
            require_confirmed_fact_review(
                current.facts,
                chapter_text=current.source_text,
            )
        except ChapterFactCandidateError as exc:
            raise ChapterFactReviewConflict(
                _candidate_error_message(exc), code=exc.code
            ) from exc
        confirmation_sha = _fact_confirmation_sha256(current)
        if current.state == "confirmed":
            if current.confirmation_sha256 != confirmation_sha:
                raise ChapterFactReviewIntegrity("fact confirmation hash mismatch")
            return current
        now = _utc_now()
        with self.database.transaction() as connection:
            _require_current_draft_binding(
                connection,
                project_id=current.project_id,
                chapter=current.chapter,
                candidate_id=current.candidate_id,
                draft_id=current.draft_id,
                revision=current.source_draft_revision,
                sha256=current.source_draft_sha256,
            )
            updated = connection.execute(
                """
                UPDATE chapter_fact_reviews
                SET state = 'confirmed', confirmation_sha256 = ?,
                    confirmed_at = ?, updated_at = ?
                WHERE id = ? AND revision = ? AND facts_sha256 = ? AND state = 'editing'
                """,
                (confirmation_sha, now, now, review_id, expected, expected_facts),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM chapter_fact_reviews WHERE id = ?",
                    (review_id,),
                ).fetchone()
                if row is not None:
                    winner = _review_from_row(row)
                    if (
                        winner.state == "confirmed"
                        and winner.revision == expected
                        and winner.facts_sha256 == expected_facts
                        and winner.confirmation_sha256
                        == _fact_confirmation_sha256(winner)
                    ):
                        return winner
                raise ChapterFactReviewConflict(
                    "fact review changed concurrently before confirmation"
                )
        return self.get(review_id)


class ChapterFactReviewService:
    """Author review boundary; no method writes prose or authoritative state."""

    def __init__(
        self,
        *,
        database: WorkbenchDatabase,
        projects: ProjectRegistry,
        candidate_drafts: CandidateDraftStore,
    ) -> None:
        self.projects = projects
        self.candidate_drafts = candidate_drafts
        self.reviews = ChapterFactReviewStore(database)

    def create(
        self,
        draft_id: str,
        *,
        expected_draft_revision: int,
        expected_draft_sha256: str,
        proposals: Sequence[Mapping[str, Any]] | None,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterFactReviewConflict(
                "explicit author confirmation is required to start fact review"
            )
        draft = self.candidate_drafts.get(draft_id)
        expected_revision = _positive_revision(expected_draft_revision)
        expected_sha = _sha256("expected_draft_sha256", expected_draft_sha256)
        if draft.revision != expected_revision or draft.sha256 != expected_sha:
            raise ChapterFactReviewConflict(
                "candidate draft changed before fact review was created",
                code="chapter_fact_source_draft_changed",
            )
        intent = self._verified_candidate_intent(draft)
        raw_proposals = (
            []
            if proposals is None
            else _fact_item_list(proposals, "proposals")
        )
        try:
            facts = normalize_fact_proposal(
                {"schema_version": "1.0", "facts": raw_proposals},
                chapter_text=draft.text,
                origin="author",
            )
        except ChapterFactCandidateError as exc:
            raise ChapterFactReviewError(
                _candidate_error_message(exc), code=exc.code
            ) from exc
        authority = _review_authority_binding(intent)
        record = self.reviews.create(
            project_id=draft.project_id,
            chapter=draft.chapter,
            candidate_id=draft.candidate_id,
            candidate_intent_hash=intent["intent_hash"],
            draft_id=draft.id,
            source_draft_revision=draft.revision,
            source_draft_sha256=draft.sha256,
            source_text=draft.text,
            context_sha256=intent["source"]["context_sha256"],
            authority=authority,
            facts=facts,
        )
        return self._public(record, include_facts=True)

    def get(self, review_id: str) -> dict[str, Any]:
        return self._public(self.reviews.get(review_id), include_facts=True)

    def verify_draft_source(
        self,
        draft_id: str,
    ) -> tuple[CandidateDraftRecord, dict[str, Any]]:
        """Return the current draft and its fully verified Candidate Intent."""

        draft = self.candidate_drafts.get(draft_id)
        return draft, self._verified_candidate_intent(draft)

    def create_from_extraction(
        self,
        draft_id: str,
        extraction_id: str,
        *,
        expected_extraction_record_hash: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        """Create a review only from a fully verified model extraction.

        The model proposal remains non-authoritative. This method binds its
        immutable execution record to the editable draft and creates cards in
        the ordinary author-owned review state machine.
        """

        if author_confirmed is not True:
            raise ChapterFactReviewConflict(
                "explicit author confirmation is required to open extracted fact cards"
            )
        draft = self.candidate_drafts.get(draft_id)
        candidate_intent = self._verified_candidate_intent(draft)
        project = self.projects.get(draft.project_id)
        expected_record_hash = _sha256(
            "expected_extraction_record_hash",
            expected_extraction_record_hash,
        )
        try:
            extraction_store = ChapterFactExtractionStore(project.root)
            extraction = extraction_store.load(extraction_id)
            source_text = extraction_store.load_source(extraction_id)
            authority_view = extraction_store.load_authority_view(extraction_id)
        except ChapterFactExtractionError as exc:
            raise ChapterFactReviewIntegrity(
                "fact-extraction evidence cannot be verified; fact review has stopped"
            ) from exc
        extraction_intent = extraction["intent"]
        extraction_record = extraction["record"]
        proposal = extraction["proposal"]
        if not isinstance(extraction_record, Mapping) or not isinstance(proposal, Mapping):
            raise ChapterFactReviewConflict(
                "fact extraction has not produced a reviewable proposal"
            )
        if extraction_record["status"] != "completed":
            raise ChapterFactReviewConflict(
                "only a completed fact extraction can create review cards"
            )
        if extraction_record["record_hash"] != expected_record_hash:
            raise ChapterFactReviewConflict(
                "fact-extraction record changed before review creation"
            )

        expected_authority = _review_authority_binding(candidate_intent)
        authority_view_binding = authority_view.get("authority_binding")
        actual_authority = {
            **copy.deepcopy(extraction_intent["authority"]),
            "authority_projection_sha256": (
                authority_view_binding.get("authority_projection_sha256")
                if isinstance(authority_view_binding, Mapping)
                else None
            ),
        }
        if (
            extraction_intent["book_id"] != project.book_id
            or extraction_intent["chapter_index"] != draft.chapter
            or extraction_intent["candidate"]
            != {
                "candidate_id": draft.candidate_id,
                "intent_hash": candidate_intent["intent_hash"],
            }
            or extraction_intent["draft"]["draft_id"] != draft.id
            or extraction_intent["draft"]["revision"] != draft.revision
            or extraction_intent["draft"]["sha256"] != draft.sha256
            or source_text != draft.text
            or extraction_intent["context"]["context_sha256"]
            != candidate_intent["source"]["context_sha256"]
            or actual_authority != expected_authority
        ):
            raise ChapterFactReviewConflict(
                "fact extraction is stale or belongs to a different Candidate draft",
                code="chapter_fact_extraction_stale",
            )
        facts = proposal.get("facts")
        if not isinstance(facts, list) or any(
            item.get("review", {}).get("origin") != "model"
            or item.get("review", {}).get("decision") != "pending"
            for item in facts
            if isinstance(item, Mapping)
        ) or any(not isinstance(item, Mapping) for item in facts):
            raise ChapterFactReviewIntegrity(
                "fact-extraction proposal is not an untouched model suggestion set"
            )
        record = self.reviews.create(
            project_id=draft.project_id,
            chapter=draft.chapter,
            candidate_id=draft.candidate_id,
            candidate_intent_hash=candidate_intent["intent_hash"],
            draft_id=draft.id,
            source_draft_revision=draft.revision,
            source_draft_sha256=draft.sha256,
            source_text=draft.text,
            context_sha256=candidate_intent["source"]["context_sha256"],
            authority=expected_authority,
            facts=facts,
            source_extraction_id=extraction_intent["extraction_id"],
            source_extraction_record_hash=extraction_record["record_hash"],
        )
        return self._public(record, include_facts=True)

    def list_for_draft(self, draft_id: str) -> tuple[dict[str, Any], ...]:
        # Resolving the draft first prevents cross-project orphan discovery.
        self.candidate_drafts.get(draft_id)
        return tuple(
            self._public(record, include_facts=False)
            for record in self.reviews.list_for_draft(draft_id)
        )

    def update(
        self,
        review_id: str,
        *,
        facts: Sequence[Mapping[str, Any]],
        expected_revision: int,
    ) -> dict[str, Any]:
        current = self.reviews.get(review_id)
        self._require_fresh(current)
        updated = self.reviews.update_facts(
            review_id,
            facts=facts,
            expected_revision=expected_revision,
        )
        return self._public(updated, include_facts=True)

    def confirm(
        self,
        review_id: str,
        *,
        expected_revision: int,
        expected_facts_sha256: str,
        expected_draft_revision: int,
        expected_draft_sha256: str,
        author_confirmed: bool,
    ) -> dict[str, Any]:
        if author_confirmed is not True:
            raise ChapterFactReviewConflict(
                "explicit author confirmation is required to confirm fact cards"
            )
        current = self.reviews.get(review_id)
        if (
            current.source_draft_revision != _positive_revision(expected_draft_revision)
            or current.source_draft_sha256
            != _sha256("expected_draft_sha256", expected_draft_sha256)
        ):
            raise ChapterFactReviewConflict(
                "fact review source binding changed before confirmation",
                code="chapter_fact_source_draft_changed",
            )
        self._require_fresh(current)
        confirmed = self.reviews.confirm(
            review_id,
            expected_revision=expected_revision,
            expected_facts_sha256=expected_facts_sha256,
        )
        return self._public(confirmed, include_facts=True)

    def _verified_candidate_intent(
        self,
        draft: CandidateDraftRecord,
    ) -> dict[str, Any]:
        project = self.projects.get(draft.project_id)
        try:
            store = ChapterCandidateStore(project.root)
            intent = store.load_intent(draft.candidate_id)
            record = store.load_record(draft.candidate_id)
        except (ChapterCandidateIntegrityError, SafePathError, OSError) as exc:
            raise ChapterFactReviewIntegrity(
                "Candidate evidence cannot be verified; fact review has stopped"
            ) from exc
        if (
            intent["book_id"] != project.book_id
            or intent["chapter_index"] != draft.chapter
            or record["status"] != "completed"
            or record["prose"]["sha256"] != draft.source_sha256
        ):
            raise ChapterFactReviewIntegrity(
                "Candidate draft is not bound to a completed Candidate"
            )
        try:
            ChapterCandidateContextBuilder(project.root).verify_binding(
                chapter_index=draft.chapter,
                source=intent["source"],
                authority=intent["authority"],
                # Fact review is a read-only consumer of the exact Candidate
                # read set.  `prepare` checks both source membership and the
                # active candidate/amendment fingerprints without allowing
                # any declared write transition.
                phase="prepare",
            )
        except StoryProjectSourceDriftError as exc:
            raise ChapterFactReviewConflict(
                "trusted chapter sources changed; create a new Candidate before reviewing facts",
                code="chapter_fact_authority_changed",
            ) from exc
        except ChapterCandidateContextError as exc:
            raise ChapterFactReviewConflict(
                "trusted chapter context or authority binding changed; create a new Candidate",
                code="chapter_fact_authority_changed",
            ) from exc
        except (
            ProjectIdentityError,
            SchemaValidationError,
            SafePathError,
            OSError,
            ValueError,
        ) as exc:
            raise ChapterFactReviewIntegrity(
                "trusted chapter evidence is unreadable or corrupt; fact review has stopped"
            ) from exc
        return intent

    def _freshness(self, record: ChapterFactReviewRecord) -> tuple[bool, str | None]:
        try:
            draft = self.candidate_drafts.get(record.draft_id)
        except ChapterCandidateWorkbenchError as exc:
            raise ChapterFactReviewIntegrity(
                "fact review source draft is missing or corrupt"
            ) from exc
        if (
            draft.project_id != record.project_id
            or draft.chapter != record.chapter
            or draft.candidate_id != record.candidate_id
        ):
            raise ChapterFactReviewIntegrity("fact review route binding is corrupt")
        if (
            draft.revision != record.source_draft_revision
            or draft.sha256 != record.source_draft_sha256
        ):
            return False, "candidate_draft_changed"
        try:
            intent = self._verified_candidate_intent(draft)
        except ChapterFactReviewConflict:
            return False, "authority_or_source_changed"
        if (
            intent["intent_hash"] != record.candidate_intent_hash
            or intent["source"]["context_sha256"] != record.context_sha256
            or _review_authority_binding(intent) != record.authority
        ):
            raise ChapterFactReviewIntegrity("fact review Candidate binding is corrupt")
        return True, None

    def _require_fresh(self, record: ChapterFactReviewRecord) -> None:
        fresh, reason = self._freshness(record)
        if not fresh:
            raise ChapterFactReviewConflict(
                "fact review is stale; preserve it and create a new review from the current draft",
                code=(
                    "chapter_fact_source_draft_changed"
                    if reason == "candidate_draft_changed"
                    else "chapter_fact_authority_changed"
                ),
            )

    def _public(
        self,
        record: ChapterFactReviewRecord,
        *,
        include_facts: bool,
    ) -> dict[str, Any]:
        fresh, stale_reason = self._freshness(record)
        state = record.state if fresh else "stale"
        facts = copy.deepcopy(record.facts) if include_facts else None
        counts = {
            decision: sum(
                1
                for item in record.facts
                if item["review"]["decision"] == decision
            )
            for decision in ("pending", "accepted", "rejected")
        }
        source = {
            "draft_revision": record.source_draft_revision,
            "draft_sha256": record.source_draft_sha256,
            "candidate_intent_hash": record.candidate_intent_hash,
            "context_sha256": record.context_sha256,
        }
        if record.source_extraction_id is not None:
            source["extraction_id"] = record.source_extraction_id
            source["extraction_record_hash"] = (
                record.source_extraction_record_hash
            )
        payload: dict[str, Any] = {
            "record_type": record.record_type,
            "schema_version": record.schema_version,
            "id": record.id,
            "project_id": record.project_id,
            "chapter": record.chapter,
            "candidate_id": record.candidate_id,
            "draft_id": record.draft_id,
            "source": source,
            "authority": copy.deepcopy(record.authority),
            "authority_sha256": record.authority_sha256,
            "state": state,
            "stored_state": record.state,
            "stale_reason": stale_reason,
            "revision": record.revision,
            "facts_sha256": record.facts_sha256,
            "fact_counts": {"total": len(record.facts), **counts},
            "confirmation_sha256": record.confirmation_sha256,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "confirmed_at": record.confirmed_at,
        }
        if include_facts:
            payload["facts"] = facts
        return payload


def _review_from_row(row: Any) -> ChapterFactReviewRecord:
    try:
        return _decode_review_from_row(row)
    except ChapterFactReviewIntegrity:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise ChapterFactReviewIntegrity(
            "fact review database row is corrupt"
        ) from exc


def _decode_review_from_row(row: Any) -> ChapterFactReviewRecord:
    try:
        authority = json.loads(str(row["authority_json"]))
        facts = json.loads(str(row["facts_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChapterFactReviewIntegrity("fact review JSON evidence is corrupt") from exc
    if not isinstance(authority, dict) or not isinstance(facts, list):
        raise ChapterFactReviewIntegrity("fact review JSON evidence has the wrong shape")
    record = ChapterFactReviewRecord(
        record_type=str(row["record_type"]),
        schema_version=str(row["schema_version"]),
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        chapter=int(row["chapter"]),
        candidate_id=str(row["candidate_id"]),
        candidate_intent_hash=str(row["candidate_intent_hash"]),
        draft_id=str(row["draft_id"]),
        source_draft_revision=int(row["source_draft_revision"]),
        source_draft_sha256=str(row["source_draft_sha256"]),
        source_text=str(row["source_text"]),
        context_sha256=str(row["context_sha256"]),
        source_extraction_id=(
            str(row["source_extraction_id"])
            if row["source_extraction_id"] is not None
            else None
        ),
        source_extraction_record_hash=(
            str(row["source_extraction_record_hash"])
            if row["source_extraction_record_hash"] is not None
            else None
        ),
        authority=authority,
        authority_sha256=str(row["authority_sha256"]),
        facts=facts,
        facts_sha256=str(row["facts_sha256"]),
        revision=int(row["revision"]),
        state=str(row["state"]),
        confirmation_sha256=(
            str(row["confirmation_sha256"])
            if row["confirmation_sha256"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        confirmed_at=(str(row["confirmed_at"]) if row["confirmed_at"] else None),
    )
    if (
        record.record_type != FACT_REVIEW_RECORD_TYPE
        or record.schema_version != FACT_REVIEW_SCHEMA_VERSION
    ):
        raise ChapterFactReviewIntegrity(
            "fact review record type or schema version is unsupported"
        )
    if _REVIEW_ID.fullmatch(record.id) is None:
        raise ChapterFactReviewIntegrity("fact review id is invalid")
    for label, value in (
        ("candidate_intent_hash", record.candidate_intent_hash),
        ("source_draft_sha256", record.source_draft_sha256),
        ("context_sha256", record.context_sha256),
        ("authority_sha256", record.authority_sha256),
        ("facts_sha256", record.facts_sha256),
    ):
        _sha256(label, value, integrity=True)
    _extraction_binding(
        record.source_extraction_id,
        record.source_extraction_record_hash,
        integrity=True,
    )
    if record.chapter < 1 or record.source_draft_revision < 1 or record.revision < 1:
        raise ChapterFactReviewIntegrity("fact review numeric binding is invalid")
    if record.id != _review_id(
        record.draft_id,
        record.source_draft_revision,
        record.source_draft_sha256,
    ):
        raise ChapterFactReviewIntegrity("fact review id does not match its source binding")
    if record.state not in {"editing", "confirmed"}:
        raise ChapterFactReviewIntegrity("fact review state is invalid")
    if _sha256_text(record.source_text) != record.source_draft_sha256:
        raise ChapterFactReviewIntegrity("fact review source text hash mismatch")
    if _sha256_text(_canonical_json(record.authority)) != record.authority_sha256:
        raise ChapterFactReviewIntegrity("fact review authority hash mismatch")
    try:
        normalized = normalize_fact_review_items(
            record.facts,
            chapter_text=record.source_text,
        )
    except ChapterFactCandidateError as exc:
        raise ChapterFactReviewIntegrity(_candidate_error_message(exc)) from exc
    if normalized != record.facts:
        raise ChapterFactReviewIntegrity("fact review items are not canonical")
    if _sha256_text(_canonical_json(record.facts)) != record.facts_sha256:
        raise ChapterFactReviewIntegrity("fact review facts hash mismatch")
    if record.state == "confirmed":
        try:
            require_confirmed_fact_review(
                record.facts,
                chapter_text=record.source_text,
            )
        except ChapterFactCandidateError as exc:
            raise ChapterFactReviewIntegrity(
                _candidate_error_message(exc)
            ) from exc
        if (
            record.confirmation_sha256 is None
            or record.confirmed_at is None
            or record.confirmation_sha256 != _fact_confirmation_sha256(record)
        ):
            raise ChapterFactReviewIntegrity("confirmed fact review evidence is incomplete")
    elif record.confirmation_sha256 is not None or record.confirmed_at is not None:
        raise ChapterFactReviewIntegrity("editing fact review cannot contain confirmation")
    return record


def _review_authority_binding(intent: Mapping[str, Any]) -> dict[str, Any]:
    authority = intent.get("authority")
    source = intent.get("source")
    if not isinstance(authority, Mapping) or not isinstance(source, Mapping):
        raise ChapterFactReviewIntegrity("Candidate has no authority binding")
    return {
        "mode": authority.get("mode"),
        "authority_epoch": authority.get("authority_epoch"),
        "head_event_hash": authority.get("head_event_hash"),
        "identity_revision": authority.get("identity_revision"),
        "authority_projection_sha256": source.get("authority_projection_sha256"),
    }


def _require_current_draft_binding(
    connection: Any,
    *,
    project_id: str,
    chapter: int,
    candidate_id: str,
    draft_id: str,
    revision: int,
    sha256: str,
) -> None:
    row = connection.execute(
        """
        SELECT project_id, chapter, candidate_id, revision, sha256
        FROM chapter_candidate_drafts
        WHERE id = ?
        """,
        (draft_id,),
    ).fetchone()
    if row is None:
        raise ChapterFactReviewIntegrity(
            "fact review source draft disappeared before the state transition"
        )
    if (
        str(row["project_id"]) != project_id
        or int(row["chapter"]) != chapter
        or str(row["candidate_id"]) != candidate_id
    ):
        raise ChapterFactReviewIntegrity(
            "fact review source draft identity changed before the state transition"
        )
    if int(row["revision"]) != revision or str(row["sha256"]) != sha256:
        raise ChapterFactReviewConflict(
            "candidate draft changed before the fact review state transition",
            code="chapter_fact_source_draft_changed",
        )


def _author_update_transition(
    current: Sequence[Mapping[str, Any]],
    proposed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve card identity/provenance while deriving author-edit evidence.

    A UI "delete" is a rejected card with an audit reason.  Silently dropping a
    model suggestion would make the review impossible to reconstruct.
    """

    current_by_id = {str(item["fact_id"]): item for item in current}
    proposed_by_id = {str(item["fact_id"]): item for item in proposed}
    if set(current_by_id) != set(proposed_by_id):
        raise ChapterFactReviewConflict(
            "fact cards cannot be added or removed during review; mark unwanted cards "
            "rejected with reason author_deleted"
        )

    result: list[dict[str, Any]] = []
    content_fields = (
        "local_key",
        "kind",
        "summary",
        "evidence_spans",
        "proposal",
    )
    for item in proposed:
        fact_id = str(item["fact_id"])
        before = current_by_id[fact_id]
        before_review = before["review"]
        after = copy.deepcopy(dict(item))
        after_review = after["review"]
        if after_review["origin"] != before_review["origin"]:
            raise ChapterFactReviewConflict(
                f"fact card {fact_id} origin is immutable"
            )
        content_changed = any(after[field] != before[field] for field in content_fields)
        after_review["author_edited"] = bool(
            before_review["author_edited"] or content_changed
        )
        result.append(after)
    return result


def _fact_confirmation_sha256(record: ChapterFactReviewRecord) -> str:
    payload = {
        "record_type": record.record_type,
        "schema_version": record.schema_version,
        "review_id": record.id,
        "revision": record.revision,
        "facts_sha256": record.facts_sha256,
        "draft_id": record.draft_id,
        "source_draft_revision": record.source_draft_revision,
        "source_draft_sha256": record.source_draft_sha256,
        "candidate_intent_hash": record.candidate_intent_hash,
        "context_sha256": record.context_sha256,
        "authority_sha256": record.authority_sha256,
        "author_confirmed": True,
    }
    if record.source_extraction_id is not None:
        payload["source_extraction_id"] = record.source_extraction_id
        payload["source_extraction_record_hash"] = (
            record.source_extraction_record_hash
        )
    return _sha256_text(
        _canonical_json(payload)
    )


def _extraction_binding(
    extraction_id: Any,
    record_hash: Any,
    *,
    integrity: bool = False,
) -> tuple[str | None, str | None]:
    error = ChapterFactReviewIntegrity if integrity else ChapterFactReviewError
    if extraction_id is None and record_hash is None:
        return None, None
    if extraction_id is None or record_hash is None:
        raise error(
            "fact review extraction id and record hash must both be present or absent"
        )
    normalized_id = str(extraction_id)
    if _EXTRACTION_ID.fullmatch(normalized_id) is None:
        raise error("fact review source extraction id is invalid")
    try:
        normalized_hash = _sha256(
            "source_extraction_record_hash",
            record_hash,
            integrity=integrity,
        )
    except ChapterFactReviewError:
        raise
    return normalized_id, normalized_hash


def _review_id(draft_id: str, revision: int, sha256: str) -> str:
    digest = _sha256_text(f"{draft_id}\0{revision}\0{sha256}")[:32]
    return f"fact_review_{digest}"


def _validated_review_id(value: Any) -> str:
    text = str(value or "")
    if _REVIEW_ID.fullmatch(text) is None:
        raise ChapterFactReviewError("unsafe fact review id")
    return text


def _fact_item_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise ChapterFactReviewError(f"{label} must be an array")
    return list(value)


def _candidate_error_message(exc: ChapterFactCandidateError) -> str:
    prefix = f"{exc.code}: "
    message = str(exc)
    return message[len(prefix) :] if message.startswith(prefix) else message


def _positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterFactReviewError("expected revision must be a positive integer")
    return value


def _sha256(label: str, value: Any, *, integrity: bool = False) -> str:
    text = str(value or "")
    if _SHA256.fullmatch(text) is None:
        error = ChapterFactReviewIntegrity if integrity else ChapterFactReviewError
        raise error(f"{label} must be a lowercase SHA-256 digest")
    return text


def _json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterFactReviewError(f"{label} must be an object")
    try:
        return json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChapterFactReviewError(f"{label} must contain finite JSON") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "FACT_REVIEW_RECORD_TYPE",
    "FACT_REVIEW_SCHEMA_VERSION",
    "ChapterFactReviewConflict",
    "ChapterFactReviewError",
    "ChapterFactReviewIntegrity",
    "ChapterFactReviewNotFound",
    "ChapterFactReviewRecord",
    "ChapterFactReviewService",
    "ChapterFactReviewStore",
]
