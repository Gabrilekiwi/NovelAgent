"""Read-only T3.2 preparation boundary for reviewed legacy chapters.

Inputs must come from verified server-side draft/review stores and a trusted
context builder. Checksums are integrity bindings, not proof of author intent.
This stage does not grant publication: transaction/quality/ownership gates and
receipt verification must be installed before a caller can commit its output.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from core.engine.chapter_candidate_context import TrustedChapterCandidateContext
from core.engine.chapter_fact_context import validate_trusted_chapter_context
from core.engine.chapter_fact_prepare import prepare_confirmed_fact_changes
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.read_set import verify_story_project_read_set
from core.story_project.identity import validate_project_identity
from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_context import validated_reviewed_state_context


PREPARATION_VERSION = "reviewed_chapter_prepare_v1"


class ChapterCommitPreparationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject(code: str, message: str):
    raise ChapterCommitPreparationError(code, message)


def _hash(value: Any) -> str:
    return canonical_json_hash(value, exclude_environment_fields=False)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_commit_prose(text: str) -> str:
    """Match the existing writer's final bytes; do not renumber evidence spans."""
    if not isinstance(text, str) or not text.strip():
        _reject("chapter_commit_prose_invalid", "final prose must not be empty")
    return text.rstrip() + "\n"


def _revision(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        _reject("chapter_commit_revision_invalid", f"{label} must be a positive integer")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        _reject("chapter_commit_identifier_invalid", f"{label} is invalid")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _reject("chapter_commit_binding_invalid", f"{label} must be lowercase SHA-256")
    return value


def prepare_reviewed_chapter(*, trusted_context: TrustedChapterCandidateContext,
                            project_id: str, draft: dict, review: dict,
                            idempotency_key: str) -> dict:
    """Recheck persisted bindings and calculate facts against reviewed state.

    ``review`` is the server store's confirmed author record, with the field
    names already used by ChapterFactReviewRecord. Automatic policy acceptance
    needs its own typed adapter; never manufacture an author confirmation here.
    """
    project_id = _identifier(project_id, "project_id")
    key = _identifier(idempotency_key, "idempotency_key")
    if not isinstance(draft, dict) or not isinstance(review, dict):
        _reject("chapter_commit_binding_invalid", "verified draft and review objects are required")
    text = draft.get("text")
    final_prose = normalize_commit_prose(text)
    raw_sha256 = _text_hash(text)
    draft_revision = _revision(draft.get("revision"), "draft revision")
    review_revision = _revision(review.get("revision"), "review revision")
    draft_id = _identifier(draft.get("id"), "draft id")
    review_id = _identifier(review.get("id"), "review id")
    if draft.get("state") != "editing" or draft.get("sha256") != raw_sha256:
        _reject("chapter_commit_draft_invalid", "draft state or content hash differs from its record")
    if (review.get("state") != "confirmed" or review.get("review_source", "author") != "author"
            or review.get("record_type") != "chapter_fact_review" or review.get("schema_version") != "1.0"):
        _reject("chapter_commit_review_unconfirmed", "an explicit author review record is required")
    confirmation = _sha(review.get("confirmation_sha256"), "review confirmation")
    facts = review.get("facts")
    if not isinstance(facts, list) or review.get("facts_sha256") != _hash(facts):
        _reject("chapter_commit_review_invalid", "fact cards no longer match the stored review")
    # Preparation does not build a model prompt: a fact extraction character
    # budget must not limit deterministic validation of an already reviewed book.
    context = validate_trusted_chapter_context(trusted_context)
    view = validated_reviewed_state_context(context)
    if view is None:
        _reject("chapter_commit_reviewed_source_required", "reviewed legacy state has not been installed")
    chapter = trusted_context.chapter_index
    if infer_next_chapter(context["story_project_root"]) != chapter:
        _reject("chapter_commit_sequence_changed", "only the next uncommitted chapter may be prepared")
    if (type(draft.get("chapter")) is not int or type(review.get("chapter")) is not int
            or draft.get("chapter") != chapter or review.get("chapter") != chapter
            or draft.get("project_id") != project_id or review.get("project_id") != project_id
            or review.get("draft_id") != draft_id
            or review.get("source_draft_revision") != draft_revision
            or review.get("source_draft_sha256") != raw_sha256
            or review.get("source_text") != text
            or review.get("context_sha256") != trusted_context.context_sha256
            or review.get("candidate_id") != draft.get("candidate_id")):
        _reject("chapter_commit_review_stale", "review belongs to another project, draft revision or context")
    authority = validate_project_identity(context["project_identity"]).authority
    read_set = context["read_set"]
    # Server adapter separately validates the store's full authority binding and
    # candidate intent. The prepare record binds their exact verified hashes.
    candidate_intent = _sha(review.get("candidate_intent_hash"), "candidate intent")
    review_authority = review.get("authority")
    if not isinstance(review_authority, dict) or review.get("authority_sha256") != _hash(review_authority):
        _reject("chapter_commit_authority_invalid", "review authority hash is invalid")
    expected_authority = {"mode":authority["mode"], "authority_epoch":authority["authority_epoch"],
        "head_event_hash":authority["head_event_hash"], "identity_revision":read_set["identity_revision"],
        "authority_projection_sha256":trusted_context.authority_projection_sha256}
    if review_authority != expected_authority:
        _reject("chapter_commit_authority_invalid", "review authority differs from current trusted context")
    verify_story_project_read_set(read_set, phase="prepare")
    prepared = prepare_confirmed_fact_changes(facts=facts, chapter_text=final_prose,
                                             base_state=view["authoritative_state"])
    # Recheck after preparation so a source edit during calculation cannot
    # produce a preview which appears fresh. Commit must repeat under its lock.
    verify_story_project_read_set(read_set, phase="prepare")
    book_id = view["book_id"]
    binding = {
        "book_id":book_id, "project_id":project_id, "chapter":chapter,
        "draft_id":draft_id, "draft_revision":draft_revision,
        "draft_sha256":raw_sha256, "final_prose_sha256":_text_hash(final_prose),
        "review_id":review_id, "review_revision":review_revision,
        "facts_sha256":review["facts_sha256"], "confirmation_sha256":confirmation,
        "acceptance_kind":"author", "candidate_intent_hash":candidate_intent,
        "candidate_id":_identifier(draft.get("candidate_id"), "candidate id"),
        "context_sha256":trusted_context.context_sha256,
        "read_set_sha256":_hash(read_set), "identity_revision":read_set["identity_revision"],
        "authority":copy.deepcopy(authority), "review_authority_sha256":review["authority_sha256"],
        "reviewed_source_sha256":view["source_sha256"],
        "manual_sources_sha256":view["manual_sources_sha256"],
        "reviewed_payload_sha256":view["payload_sha256"],
        "state_before_sha256":prepared["base_state_sha256"],
        "committed_through":view["committed_through"],
    }
    payload = {"preparation_version":PREPARATION_VERSION, "stage":"facts_prepared",
        "publication_authorized":False, "idempotency_scope_sha256":_hash({"book_id":book_id,"key":key}),
        "binding":binding, "final_prose":final_prose, "facts":copy.deepcopy(facts),
        "fact_preparation":prepared, "read_set":copy.deepcopy(read_set)}
    payload["request_sha256"] = _hash({"version":PREPARATION_VERSION, "binding":binding})
    payload["preparation_sha256"] = _hash(payload)
    return payload


def verify_preparation_unchanged(prepared: dict, **current_inputs) -> dict:
    """Return a re-prepared current value or reject; never trust a client plan."""
    current = prepare_reviewed_chapter(**current_inputs)
    if current != prepared:
        _reject("chapter_commit_preparation_stale", "draft, review, source or prepared output changed")
    return current


def require_same_idempotent_request(existing: dict, incoming: dict) -> None:
    """Store/transaction caller must perform this check while holding its lock."""
    for item in (existing, incoming):
        if not isinstance(item, dict):
            _reject("chapter_commit_binding_invalid", "idempotency record must be an object")
        _sha(item.get("idempotency_scope_sha256"), "idempotency scope")
        _sha(item.get("request_sha256"), "idempotency request")
    if (existing.get("idempotency_scope_sha256") != incoming.get("idempotency_scope_sha256")
            or existing.get("request_sha256") != incoming.get("request_sha256")):
        _reject("chapter_commit_idempotency_conflict", "idempotency key was reused for different content")
