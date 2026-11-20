"""Portable request evidence for receipt-backed chapter retry lookup.

This binds a verified server preparation to the client's IDs and CAS values.
It is not quality approval, author authentication or permission to publish.
"""
from __future__ import annotations

import copy
import re

from core.engine.chapter_commit_prepare import PREPARATION_VERSION
from core.schema import validate_schema
from core.story_project.reviewed_state_document import digest, prepare_chapter_document


REQUEST_VERSION = "reviewed_chapter_request_v1"
REQUEST_TARGET = "reviewed_commit_request"
REQUEST_DIRECTORY = "reviewed/requests"
_REQUEST_FIELDS = {"project_id", "chapter", "review_id", "expected_draft_revision",
    "expected_review_revision", "expected_draft_sha256", "expected_facts_sha256"}


class ChapterCommitRequestError(ValueError):
    code = "chapter_commit_request_invalid"


def _identifier(value, label):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 for c in value)):
        raise ChapterCommitRequestError(f"invalid {label}")
    return value


def _sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ChapterCommitRequestError(f"invalid {label}")


def _validate_route(route):
    if not isinstance(route, dict) or set(route) != _REQUEST_FIELDS:
        raise ChapterCommitRequestError("request must contain only route IDs and revision/hash CAS")
    for field in ("project_id", "review_id"):
        _identifier(route[field], field)
    for field in ("chapter", "expected_draft_revision", "expected_review_revision"):
        if type(route[field]) is not int or route[field] < 1:
            raise ChapterCommitRequestError(f"invalid {field}")
    for field in ("expected_draft_sha256", "expected_facts_sha256"):
        _sha(route[field], field)


def chapter_commit_request(*, book_id: str, request: dict) -> dict:
    """Validate a retry before consulting mutable drafts or the next chapter.

    The book-scoped hash retains key equality without archiving the raw key.
    A restored registration keeps its historical request IDs in the archive;
    it must not rewrite those IDs to manufacture the same original request.
    """
    _identifier(book_id, "book_id")
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS | {"idempotency_key"}:
        raise ChapterCommitRequestError("invalid commit request fields")
    key = _identifier(request["idempotency_key"], "idempotency_key")
    route = {k: copy.deepcopy(v) for k, v in request.items() if k != "idempotency_key"}
    _validate_route(route)
    result = {"version": REQUEST_VERSION, "book_id": book_id, "request": route,
        "idempotency_scope_sha256": digest({"book_id": book_id, "key": key})}
    result["client_request_sha256"] = digest(result)
    return result


def validate_chapter_commit_request(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"version", "book_id", "request",
            "idempotency_scope_sha256", "client_request_sha256"}
            or value["version"] != REQUEST_VERSION):
        raise ChapterCommitRequestError("invalid archived commit request")
    _identifier(value["book_id"], "book_id")
    _validate_route(value["request"])
    _sha(value["idempotency_scope_sha256"], "idempotency scope")
    if value["client_request_sha256"] != digest({k: v for k, v in value.items() if k != "client_request_sha256"}):
        raise ChapterCommitRequestError("commit request checksum changed")
    return copy.deepcopy(value)


def prepare_commit_request_evidence(*, preparation: dict, request: dict) -> dict:
    """Build the required request artifact from server-verified preparation."""
    p = preparation
    if (not isinstance(p, dict) or p.get("preparation_version") != PREPARATION_VERSION
            or p.get("stage") != "facts_prepared" or p.get("publication_authorized") is not False
            or p.get("preparation_sha256") != digest({k: v for k, v in p.items() if k != "preparation_sha256"})):
        raise ChapterCommitRequestError("invalid fact preparation")
    b = p["binding"]
    r = validate_chapter_commit_request(request)
    expected = {"project_id": b["project_id"], "chapter": b["chapter"], "review_id": b["review_id"],
        "expected_draft_revision": b["draft_revision"], "expected_review_revision": b["review_revision"],
        "expected_draft_sha256": b["draft_sha256"], "expected_facts_sha256": b["facts_sha256"]}
    read_set = validate_schema(p["read_set"], "story_project_read_set.schema.json")
    if (r["request"] != expected or r["book_id"] != b["book_id"]
            or r["idempotency_scope_sha256"] != p["idempotency_scope_sha256"]
            or p["request_sha256"] != digest({"version": PREPARATION_VERSION, "binding": b})
            or read_set["book_id"] != b["book_id"] or read_set["chapter_index"] != b["chapter"]
            or read_set["identity_revision"] != b["identity_revision"]
            or digest(read_set) != b["read_set_sha256"]):
        raise ChapterCommitRequestError("commit request differs from fact preparation")
    evidence = {"record_type": "reviewed_chapter_request", "version": REQUEST_VERSION,
        "request": r, "preparation": copy.deepcopy(p)}
    evidence["evidence_sha256"] = digest(evidence)
    return evidence


def verify_commit_request_evidence(*, evidence: dict, previous_document: bytes, document: bytes,
                                   receipt: dict, manifest: dict, final_record: dict) -> dict:
    """Verify request/content bindings after the *actual receipt* is verified.

    Historical read sets are compared to their immutable transaction, never
    re-evaluated against a later chapter or the physical location after restore.
    Physical history verification remains the receipt reader's responsibility.
    """
    if not isinstance(evidence, dict):
        raise ChapterCommitRequestError("missing commit request evidence")
    expected = prepare_commit_request_evidence(preparation=evidence.get("preparation"),
                                               request=evidence.get("request"))
    if evidence != expected:
        raise ChapterCommitRequestError("commit request evidence changed")
    p = evidence["preparation"]
    r = evidence["request"]
    b = p["binding"]
    immutable = manifest["immutable"]
    actual_document = prepare_chapter_document(document=previous_document, book_id=b["book_id"],
        preparation=p, run_id=receipt["run_id"])
    if (actual_document != document or immutable["story_project_read_set"] != p["read_set"]
            or receipt["book_id"] != b["book_id"]
            or receipt["context_digest"] != b["context_sha256"]
            or receipt["generation_input_context_digest"] != b["context_sha256"]
            or final_record.get("client_request_sha256") != r["client_request_sha256"]
            or final_record.get("idempotency_scope_sha256") != r["idempotency_scope_sha256"]
            or final_record.get("request_evidence_sha256") != evidence["evidence_sha256"]):
        raise ChapterCommitRequestError("published transaction differs from the archived request")
    return copy.deepcopy(r)
