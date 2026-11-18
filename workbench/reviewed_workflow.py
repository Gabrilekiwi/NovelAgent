"""Explicit app dispatch for reviewed books; no global legacy writer permit."""
from __future__ import annotations

from functools import wraps
import json
from urllib.parse import urlencode

from core.engine.provider_safety import list_unresolved_provider_calls, provider_execution_lock
from core.engine.safe_paths import assert_safe_local_tree
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.chapter_fact_extraction import ChapterFactExtractionExecutor
from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.reviewed_identity import load_verified_reviewed_identity, validate_reviewed_identity
from core.story_project.reviewed_state_document import read_project_bytes
from workbench.chapter_cards import ChapterCardService
from workbench.chapter_candidates import ChapterCandidateService
from workbench.chapter_fact_extractions import ChapterFactExtractionService
from workbench.chapter_commits import ChapterCommitConflict


class ReviewedWorkflow:
    def __init__(self, publication):
        self.publication = publication
        self.database = publication.database

    def registration(self, project_id):
        with self.database.connection() as connection:
            return self.publication._registration(project_id, connection)

    def is_reviewed(self, project_id):
        registration = self.registration(project_id)
        payload = json.loads(read_project_bytes(registration["root"], ".novelagent/project.json"))
        if payload.get("schema_version") != "3.0":
            return False
        identity = validate_reviewed_identity(payload)
        if identity.book_id != registration["book_id"]:
            raise ChapterCommitConflict("registered book identity changed")
        return True

    def mutate(self, project_id, operation):
        publication = self.publication
        initial = self.registration(project_id)
        with publication._owner(initial["book_id"]).locked() as owner:
            root = initial["root"]
            runtime = assert_safe_local_tree(root / ".novelagent/runtime")
            roots = {"story_project": root, "runtime": runtime}
            owner_args = dict(root=root, database=self.database, project_id=project_id)
            # Recover before strict project lookup. Release the Provider and DB
            # locks before invoking an executor, which owns those boundaries
            # itself. The machine book lock remains held throughout the action.
            with provider_execution_lock(runtime / "runs"):
                with self.database.transaction() as connection:
                    if publication._registration(project_id, connection) != initial:
                        raise ChapterCommitConflict("project registration changed before mutation")
                    ownership = owner.require(**owner_args, connection=connection)
                    publication._require_persistence_roots(runtime / "reviewed/p", roots)
                    if list_unresolved_provider_calls(runtime / "runs"):
                        # Resolution actions must remain possible, but may not
                        # recover formal files while Provider evidence is open.
                        publication._require_settled_persistence(runtime / "reviewed/p")
                    else:
                        publication._reconcile_owned_persistence(runtime / "reviewed/p", book_id=initial["book_id"])
                    load_verified_reviewed_identity(root)
                    if owner.require(**owner_args, connection=connection) != ownership:
                        raise ChapterCommitConflict("book ownership changed during recovery")
            result = operation()
            with self.database.transaction() as connection:
                if owner.require(**owner_args, connection=connection) != ownership:
                    raise ChapterCommitConflict("book ownership changed during mutation")
            return result


class _ServiceDispatch:
    def __init__(self, *, workflow, legacy, reviewed, route_kinds, mutations):
        self.workflow, self.legacy, self.reviewed = workflow, legacy, reviewed
        self.route_kinds, self.mutations = route_kinds, frozenset(mutations)

    def _project_id(self, route_kind, identifier):
        if route_kind == "project":
            return identifier
        if route_kind == "draft":
            return self.workflow.publication.preparations.drafts.get(identifier).project_id
        return self.workflow.publication.preparations.fact_reviews.reviews.get(identifier).project_id

    @staticmethod
    def _with_publication_link(result):
        review = result.get("fact_review", result) if isinstance(result, dict) else None
        if (isinstance(review, dict) and review.get("record_type") == "chapter_fact_review"
                and review.get("state") == "confirmed"):
            review["publication_url"] = "/publication?" + urlencode({"project_id":review["project_id"],
                "chapter":review["chapter"], "review_id":review["id"]})
        return result

    def __getattr__(self, name):
        legacy_method = getattr(self.legacy, name)
        if name not in self.route_kinds:
            return legacy_method
        @wraps(legacy_method)
        def call(*args, **kwargs):
            parameter = {"project":"project_id", "draft":"draft_id", "review":"review_id"}[self.route_kinds[name]]
            identifier = args[0] if args else kwargs[parameter]
            project_id = self._project_id(self.route_kinds[name], identifier)
            if not self.workflow.is_reviewed(project_id):
                return legacy_method(*args, **kwargs)
            method = getattr(self.reviewed, name)
            if name not in self.mutations:
                # Read-only evidence remains visible while a Provider request is
                # running. It cannot grant publication or mutate the database.
                return self._with_publication_link(method(*args, **kwargs))
            def operation():
                if self._project_id(self.route_kinds[name], identifier) != project_id:
                    raise ChapterCommitConflict("input registration changed before mutation")
                return method(*args, **kwargs)
            return self._with_publication_link(self.workflow.mutate(project_id, operation))
        return call


def install_reviewed_workflow(app, *, publication, cards, candidates, facts, extractions,
                              candidate_executor_factory=None, extraction_executor_factory=None):
    workflow = ReviewedWorkflow(publication)
    reviewed_cards = ChapterCardService(publication.projects, context_builder_factory=ReviewedChapterCandidateContextBuilder)
    reviewed_candidates = ChapterCandidateService(projects=publication.projects, chapter_cards=reviewed_cards,
        database=publication.database, executor_factory=candidate_executor_factory or
        (lambda root: ReviewedChapterCandidateExecutor(story_project_root=root,execution_budget_factory=ChapterExecutionBudget)))
    reviewed_facts = publication.preparations.fact_reviews
    reviewed_extractions = ChapterFactExtractionService(projects=publication.projects,
        candidate_drafts=reviewed_candidates.drafts, chapter_facts=reviewed_facts,
        executor_factory=extraction_executor_factory or (lambda root: ChapterFactExtractionExecutor(
            story_project_root=root,execution_budget_factory=ChapterExecutionBudget)),
        context_builder_factory=ReviewedChapterCandidateContextBuilder)
    def dispatch(legacy, reviewed, route_kinds, mutations):
        return _ServiceDispatch(workflow=workflow, legacy=legacy, reviewed=reviewed,
            route_kinds=route_kinds, mutations=mutations)
    cards = dispatch(cards, reviewed_cards, {k:"project" for k in ("get", "confirm", "verify_confirmation")}, [])
    candidates = dispatch(candidates, reviewed_candidates,
        {**{k:"project" for k in ("import_author_draft", "generate", "resume", "create_editable_draft", "abandon_provider_uncertainty", "authorize_continuation", "run_continuation", "get", "list_for_chapter")},
         "update_editable_draft":"draft"},
        ["import_author_draft", "generate", "resume", "create_editable_draft", "abandon_provider_uncertainty", "authorize_continuation", "run_continuation", "update_editable_draft"])
    facts = dispatch(facts, reviewed_facts,
        {**{k:"draft" for k in ("create", "create_from_extraction", "list_for_draft", "verify_draft_source")},
         **{k:"review" for k in ("get", "editor_context", "update", "update_support", "confirm")}}, ["create", "create_from_extraction", "update", "update_support", "confirm"])
    extractions = dispatch(extractions, reviewed_extractions,
        {k:"draft" for k in ("start", "resume", "open_review", "cancel", "abandon_provider_uncertainty", "get", "list_for_draft")},
        ["start", "resume", "open_review", "cancel", "abandon_provider_uncertainty"])
    app.state.reviewed_workflow = workflow
    app.state.chapter_cards, app.state.chapter_candidates = cards, candidates
    app.state.chapter_facts, app.state.chapter_fact_extractions = facts, extractions
    return cards, candidates, facts, extractions
