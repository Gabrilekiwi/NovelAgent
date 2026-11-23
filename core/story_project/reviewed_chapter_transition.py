"""Typed reviewed chapter evidence shared by persistence and cold verification.

This internal boundary consumes server preparations; checksums do not turn a
client payload into author consent. Book ownership and DB CAS belong to service.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.story_project.reviewed_state_document import (
    RELATIVE_PATH, decode_document, digest, prepare_chapter_document, read_project_bytes, verify_current_sources,
)

CHAPTER_TRANSITION = "legacy_reviewed_chapter_commit_v1"
QUALITY_TARGET = "reviewed_commit_quality"
QUALITY_DIRECTORY = "reviewed/quality"


class ReviewedChapterTransitionError(ValueError):
    code = "reviewed_chapter_transition_invalid"


def _require(condition, message):
    if not condition:
        raise ReviewedChapterTransitionError(message)


def is_reviewed_chapter_transition(declarations):
    return any(d.get("transition") == CHAPTER_TRANSITION for d in declarations)


# Only blueprint interpretation is delegated to the author. A new producer code
# must be explicitly reviewed before it can be offered here.
_AUTHOR_DECIDABLE = {"missing_required_beat", "missing_ending_pressure"}


def offered_author_quality_choice(q):
    decision = q["local_policy_decision"]
    if (q["binding"].get("acceptance_kind") != "author" or q["quality_coverage_complete"] is not True
            or q["pending_gates"] != [] or q["review_gate"]["status"] != "pass"
            or q["final_artifact_integrity"]["accepted"] is not True
            or q["authoritative_state_validation"]["accepted"] is not True
            or q["local_quality_passed"] is not False or decision["accepted"] is not False):
        return None
    ids = sorted(decision["blocking_finding_ids"])
    findings = {f["id"]: f for f in decision["findings"]}
    if (not ids or len(set(ids)) != len(ids) or any(i not in findings or not findings[i]["codes"]
            or not set(findings[i]["codes"]) <= _AUTHOR_DECIDABLE for i in ids)):
        return None
    # The full quality report includes per-run timings. Bind consent to the
    # actual manuscript, verified facts, rules and findings, not to wall time.
    fingerprint = digest({"version": 1, "binding": q["binding"], "coverage": q["blueprint_coverage"],
        "findings": [{"id": i, "codes": findings[i]["codes"], "evidence": findings[i]["evidence"]} for i in ids]})
    return {"review_sha256": fingerprint, "finding_ids": ids}


def verify_quality_binding(quality, preparation, *, author_quality_choice=None):
    from core.quality_decision import resolve_quality_policy
    from core.story_project.coverage import build_text_blueprint_coverage
    p, q = preparation, quality
    quality_policy=resolve_quality_policy('standard').to_dict()
    if p['binding'].get('acceptance_kind')=='policy':
        from core.engine.reviewed_policy_review import verify_policy_preparation
        from core.quality.reviewed_disposition import project_reviewed_quality_disposition
        review=verify_policy_preparation(p)
        quality_policy=review['batch_grant']['manifest']['quality_policy']
        action_policy=review['batch_grant']['manifest']['action_policy']
        _require(project_reviewed_quality_disposition(preparation=p,quality=q,policy_snapshot=action_policy)['automatic_action']=='continue',
            'policy publication requires all automatic-admission gates to pass')
    waived = False
    if author_quality_choice is not None:
        _require(p['binding'].get('acceptance_kind') == 'author'
            and author_quality_choice == offered_author_quality_choice(q),
            '作者选择与本次拦截项不一致，或仍有不可忽略的检查。请重新生成预览。')
        waived = True
    _require(isinstance(q, dict) and q.get("quality_preparation_version") == "reviewed_candidate_quality_v1"
        and q.get("stage") == "candidate_quality_checked" and q.get("publication_authorized") is False
        and q.get("quality_coverage_complete") is True and q.get("pending_gates") == []
        and (q.get("local_quality_passed") is True or waived)
        and q.get("quality_sha256") == digest({k: v for k, v in q.items() if k != "quality_sha256"}),
        "chapter requires complete passing Candidate quality evidence")
    binding = q["binding"]
    _require(all(binding.get(k) == v for k, v in p["binding"].items())
        and binding["chapter_preparation_sha256"] == p["preparation_sha256"]
        and binding["request_sha256"] == p["request_sha256"]
        and binding["idempotency_scope_sha256"] == p["idempotency_scope_sha256"]
        and binding["quality_policy"] == quality_policy
        and digest(binding['quality_policy']) == digest(quality_policy)
        and binding["state_after_sha256"] == digest(p["fact_preparation"]["state_after"])
        and binding["rule_pack_sha256"] == digest(q["rule_pack"])
        and binding["candidate_evidence_sha256"] == digest(q["candidate_evidence"])
        and binding["blueprint_coverage_sha256"] == digest(q["blueprint_coverage"]),
        "quality evidence differs from the chapter preparation")
    _require((q["local_policy_decision"]["accepted"] is True or waived) and q["review_gate"]["status"] == "pass"
        and q["final_artifact_integrity"]["accepted"] is True and q["authoritative_state_validation"]["accepted"] is True,
        "quality acceptance differs from its producer reports")
    candidate = q["candidate_evidence"]
    _require(candidate["intent_hash"] == p["binding"]["candidate_intent_hash"]
        and candidate["candidate_id"] == p["binding"]["candidate_id"]
        and candidate["chapter_blueprint_sha256"] == digest(candidate["chapter_blueprint"]), "quality Candidate differs")
    coverage = build_text_blueprint_coverage(candidate["chapter_blueprint"], p["final_prose"])
    _require(coverage == q["blueprint_coverage"], "quality blueprint coverage differs from final prose")


def verify_chapter_bundle(*, previous_document, document, prose, evidence, quality, final_record,
                          read_set, transaction_binding):
    from core.engine.chapter_commit_request import prepare_commit_request_evidence
    p, request = evidence["preparation"], evidence["request"]
    _require(evidence == prepare_commit_request_evidence(preparation=p, request=request), "request evidence changed")
    book_id, run_id = p["binding"]["book_id"], transaction_binding["run_id"]
    _require(p["read_set"] == read_set and prose == p["final_prose"].encode("utf-8"), "transaction inputs differ")
    _require(prepare_chapter_document(document=previous_document, book_id=book_id, preparation=p, run_id=run_id)
        == document, "chapter document differs from the confirmed fact replay")
    value = decode_document(document, book_id=book_id)
    head = value["head"]
    ref = transaction_binding["receipt_path_ref"]
    _require(transaction_binding["context_digest"] == p["binding"]["context_sha256"]
        and transaction_binding["generation_input_context_digest"] == p["binding"]["context_sha256"]
        and transaction_binding["receipt_id"] == head["receipt_id"]
        and ref["root_id"] == "runtime" and ref["relative_path"] == head["receipt_relative_path"],
        "chapter transaction context or receipt binding differs")
    verify_quality_binding(quality, p, author_quality_choice=request["request"].get("author_quality_choice"))
    expected = {"record_type": "reviewed_legacy_commit", "id": run_id, "book_id": book_id, "kind": "chapter",
        "revision": value["revision"], "committed": True, "request_sha256": p["request_sha256"],
        "client_request_sha256": request["client_request_sha256"],
        "idempotency_scope_sha256": request["idempotency_scope_sha256"],
        "request_evidence_sha256": evidence["evidence_sha256"], "quality_sha256": quality["quality_sha256"],
        "chapter_transition": CHAPTER_TRANSITION,
        "publication_receipt": {"id": head["receipt_id"], "path_ref": ref}}
    _require(final_record == expected, "chapter final record differs from its verified evidence")
    return value


def validate_reviewed_chapter_transaction_targets(*, targets, declarations, read_set, transaction_binding, phase, root):
    from core.story_project.paths import canonical_prose_path, infer_next_chapter
    from core.story_project.reviewed_identity import (
        REVIEWED_WRITER_CONTRACT, _load_verified_reviewed_identity, validate_reviewed_identity,
    )
    from core.story_project.reviewed_state_store import source_revision_after
    _require(phase in {"prepare", "pre_apply", "pre_marker"}, "unsupported chapter write phase")
    by_id = {t["target_id"]: t for t in targets}
    required = {"reviewed_state", "reviewed_chapter", "reviewed_state_archive", "reviewed_chapter_archive",
        "reviewed_commit_request", QUALITY_TARGET, "final-run-record"}
    _require(set(by_id) == required and len(targets) == len(required)
        and {t["target_id"] for t in targets if t["phase"] == "apply"} == {"reviewed_state", "reviewed_chapter"},
        "reviewed chapter requires exactly state/prose plus request, quality and immutable evidence")
    _require(all(isinstance(t.get("_content"), bytes) for t in targets), "chapter target bytes must be verified")
    state, chapter = by_id["reviewed_state"], by_id["reviewed_chapter"]
    previous = state.get("_before")
    _require(isinstance(previous, bytes) and state["before_exists"] is True and chapter["before_exists"] is False
        and digest(previous) == state["before_sha256"], "chapter before-images differ")
    source = next((s for s in read_set["membership"] if s["relative_path"] == RELATIVE_PATH), None)
    _require(source is not None and source["sha256"] == digest(previous) and source["size"] == len(previous),
        "chapter parent differs from original read-set")
    raw_identity = read_project_bytes(root, ".novelagent/project.json")
    _require(digest(raw_identity) == read_set["identity_revision"], "chapter identity changed")
    verified = _load_verified_reviewed_identity(root, _parent_document=previous if phase == "pre_marker" else None)
    identity = validate_reviewed_identity(verified["identity"])
    _require(identity.book_id == read_set["book_id"]
        and identity.authority["minimum_writer_contract"] <= REVIEWED_WRITER_CONTRACT
        and verified["history"]["document_sha256"] == digest(previous), "chapter parent is not the verified history tip")
    verify_current_sources(root=root, value=verified["history"]["value"], next_chapter=read_set["chapter_index"])
    expected_cursor = read_set["chapter_index"] + (1 if phase == "pre_marker" else 0)
    _require(infer_next_chapter(root) == expected_cursor, "chapter prose cursor differs")
    run_id = transaction_binding["run_id"]
    locations = {
        "reviewed_state": ("story_project", RELATIVE_PATH),
        "reviewed_chapter": ("story_project", canonical_prose_path(root, read_set["chapter_index"]).relative_to(root).as_posix()),
        "reviewed_state_archive": ("runtime", f"reviewed/states/{run_id}.md"),
        "reviewed_chapter_archive": ("runtime", f"reviewed/chapters/{run_id}.md"),
        "reviewed_commit_request": ("runtime", f"reviewed/requests/{run_id}.json"),
        QUALITY_TARGET: ("runtime", f"{QUALITY_DIRECTORY}/{run_id}.json"),
        "final-run-record": ("runtime", f"reviewed/runs/{run_id}.json"),
    }
    for target_id, (root_id, relative) in locations.items():
        target = by_id[target_id]
        _require(target["path_ref"]["root_id"] == root_id and target["path_ref"]["relative_path"] == relative
            and digest(target["_content"]) == target["after_sha256"] and len(target["_content"]) == target["after_size"],
            "chapter evidence target path or bytes differ")
    _require(by_id["reviewed_state_archive"]["_content"] == state["_content"]
        and by_id["reviewed_chapter_archive"]["_content"] == chapter["_content"], "chapter archives differ from applied bytes")
    expected_declarations = []
    for target in (state, chapter):
        item = {"relative_path": target["path_ref"]["relative_path"], "action": "replace" if target["before_exists"] else "create",
            "after_sha256": target["after_sha256"], "after_size": target["after_size"]}
        if target is state:
            item.update(transition=CHAPTER_TRANSITION, source_revision_after=source_revision_after(state["_content"], book_id=identity.book_id))
        expected_declarations.append(item)
    _require(sorted(declarations, key=lambda d: d["relative_path"]) == sorted(expected_declarations, key=lambda d: d["relative_path"]),
        "chapter declarations must match the exact typed transition")
    verify_chapter_bundle(previous_document=previous, document=state["_content"], prose=chapter["_content"],
        evidence=json.loads(by_id["reviewed_commit_request"]["_content"]), quality=json.loads(by_id[QUALITY_TARGET]["_content"]),
        final_record=json.loads(by_id["final-run-record"]["_content"]), read_set=read_set, transaction_binding=transaction_binding)
    return identity
