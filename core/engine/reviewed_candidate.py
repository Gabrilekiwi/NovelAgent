"""Explicit internal Candidate executor for verified identity-3 reviewed books.

Writes only Candidate artifacts. Ordinary entrypoints do not discover this
executor until the complete reviewed commit and job workflow is installed.
"""
from core.engine.chapter_candidate import ChapterCandidateExecutor
from core.engine.chapter_candidate_context import ChapterCandidateContextError
from core.engine.reviewed_candidate_context import (
    ReviewedChapterCandidateContextBuilder, validate_preparation_identity,
)
from core.story_project.reviewed_state_context import (
    reviewed_state_memory_context, reviewed_state_snapshot,
)


class ReviewedChapterCandidateExecutor(ChapterCandidateExecutor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context_builder = ReviewedChapterCandidateContextBuilder(self.story_project_root)

    def _apply_candidate_context(self, context, snapshot, memory):
        identity = validate_preparation_identity(context)
        if snapshot.get("book_id") not in (None, identity.book_id):
            raise ChapterCandidateContextError("Candidate cache seed belongs to a different book")
        return reviewed_state_snapshot(context), reviewed_state_memory_context(context)
