"""Internal real-v3 chapter transaction for an owned, server-verified request.

The caller must hold book ownership and perform DB CAS/pending-intent handling.
This module does not expose an API, reconcile, or accept client quality reports.
"""
from __future__ import annotations

from pathlib import Path

from core.engine.chapter_commit_request import prepare_commit_request_evidence
from core.engine.persistence_v2 import PersistenceV2Target, PersistenceV2Transaction, bind_final_run_record_receipt
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.reviewed_chapter_quality import prepare_candidate_chapter_quality
from core.engine.reviewed_upgrade import json_bytes
from core.path_refs import path_ref_for
from core.story_project.paths import canonical_prose_path
from core.story_project.read_set import declared_read_set_writes
from core.story_project.reviewed_chapter_transition import (
    CHAPTER_TRANSITION, QUALITY_DIRECTORY, QUALITY_TARGET, ReviewedChapterTransitionError, verify_quality_binding,
)
from core.story_project.reviewed_state_document import RELATIVE_PATH, decode_document, digest, prepare_chapter_document, read_project_bytes
from core.story_project.reviewed_state_store import source_revision_after


def prepare_reviewed_chapter_transaction(*, trusted_context, chapter_preparation, request, run_id,
                                         quality_policy="standard", fault_injector=None):
    tx, arguments = build_reviewed_chapter_transaction(trusted_context=trusted_context,
        chapter_preparation=chapter_preparation, request=request, run_id=run_id,
        quality_policy=quality_policy, fault_injector=fault_injector)
    tx.prepare(**arguments)
    return tx


def build_reviewed_chapter_transaction(*, trusted_context, chapter_preparation, request, run_id,
                                       quality_policy="standard", fault_injector=None,candidate_source_verifier=None):
    """Verify and build without staging; service durably claims its intent next."""
    p = chapter_preparation
    root = Path(p["read_set"]["root_identity"]["resolved_path"])
    trusted = ReviewedChapterCandidateContextBuilder(root).build(p["binding"]["chapter"])
    if trusted.to_dict() != trusted_context.to_dict():
        raise ReviewedChapterTransitionError("chapter context changed before transaction preparation")
    evidence = prepare_commit_request_evidence(preparation=p, request=request)
    quality = prepare_candidate_chapter_quality(trusted_context=trusted, chapter_preparation=p, quality_policy=quality_policy,
        candidate_source_verifier=candidate_source_verifier)
    verify_quality_binding(quality, p, author_quality_choice=request["request"].get("author_quality_choice"))
    book_id = p["binding"]["book_id"]
    previous = read_project_bytes(root, RELATIVE_PATH)
    document = prepare_chapter_document(document=previous, book_id=book_id, preparation=p, run_id=run_id)
    value = decode_document(document, book_id=book_id)
    prose = p["final_prose"].encode("utf-8")
    runtime = root / ".novelagent/runtime"
    roots = {"story_project": root, "runtime": runtime}
    def ref(relative, root_id):
        return path_ref_for(roots[root_id] / relative, root_id=root_id, root=roots[root_id])
    prose_path = canonical_prose_path(root, p["binding"]["chapter"])
    apply = [
        PersistenceV2Target(target_id="reviewed_state", kind="tracking", path_ref=ref(RELATIVE_PATH, "story_project"),
            content=document, expected_before_exists=True, expected_before_sha256=digest(previous)),
        PersistenceV2Target(target_id="reviewed_chapter", kind="story_project_update",
            path_ref=ref(prose_path.relative_to(root).as_posix(), "story_project"), content=prose, expected_before_exists=False),
    ]
    head = value["head"]
    receipt_ref = ref(head["receipt_relative_path"], "runtime")
    final = bind_final_run_record_receipt({"record_type": "reviewed_legacy_commit", "id": run_id,
        "book_id": book_id, "kind": "chapter", "revision": value["revision"], "committed": True,
        "request_sha256": p["request_sha256"], "client_request_sha256": request["client_request_sha256"],
        "idempotency_scope_sha256": request["idempotency_scope_sha256"],
        "request_evidence_sha256": evidence["evidence_sha256"], "quality_sha256": quality["quality_sha256"],
        "chapter_transition": CHAPTER_TRANSITION}, receipt_id=head["receipt_id"], receipt_path_ref=receipt_ref)
    artifacts = [PersistenceV2Target(target_id=target_id, kind="review_artifact", phase="publication",
        path_ref=ref(relative, "runtime"), content=raw) for target_id, relative, raw in (
            ("reviewed_state_archive", f"reviewed/states/{run_id}.md", document),
            ("reviewed_chapter_archive", f"reviewed/chapters/{run_id}.md", prose),
            ("reviewed_commit_request", f"reviewed/requests/{run_id}.json", json_bytes(evidence)),
            (QUALITY_TARGET, f"{QUALITY_DIRECTORY}/{run_id}.json", json_bytes(quality)),
        )]
    declared = declared_read_set_writes(p["read_set"], [(root / t.path_ref.relative_path,
        digest(t.content_bytes()), len(t.content_bytes())) for t in apply])
    checkpoint = source_revision_after(document, book_id=book_id)
    next(d for d in declared if d["relative_path"] == RELATIVE_PATH).update(
        transition=CHAPTER_TRANSITION, source_revision_after=checkpoint)
    tx = PersistenceV2Transaction(transaction_root=runtime / "reviewed/p", run_id=run_id, book_id=book_id,
        root_map=roots, story_project_read_set=p["read_set"], read_set_declared_writes=declared, fault_injector=fault_injector)
    arguments = dict(apply_targets=apply, artifacts=artifacts, final_run_record=final,
        final_run_path_ref=ref(f"reviewed/runs/{run_id}.json", "runtime"), receipt_id=head["receipt_id"],
        receipt_path_ref=receipt_ref, context_digest=p["binding"]["context_sha256"],
        generation_input_context_digest=p["binding"]["context_sha256"], story_project_source_revision_after=checkpoint,
        candidate_result={"request_evidence_sha256": evidence["evidence_sha256"], "quality_sha256": quality["quality_sha256"]})
    return tx, arguments
