"""Receipt-backed cold reading for the reviewed legacy format.

Not installed in ordinary builders until the identity/upgrade/commit gates are
complete. The caller supplies trusted roots and the identity's baseline anchor.
Reads do not reconcile transactions or regenerate anything. The writer lock and
pending-transaction gate belong to the service that calls this reader.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from core.engine.persistence_v2 import verify_publication_receipt
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.paths import infer_next_chapter, resolve_prose
from core.story_project.reviewed_state_document import (
    FORMAT, PROSE_DIRECTORY, RECEIPT_DIRECTORY, RELATIVE_PATH, STATE_DIRECTORY,
    decode_document, digest, fail, read_project_bytes, verify_current_sources, verify_transition,
)


def source_revision_after(document: bytes, *, book_id: str) -> dict:
    value = decode_document(document, book_id=book_id)
    return {"format": FORMAT, "revision": value["revision"], "committed_through": value["committed_through"],
            "reviewed_document_sha256": digest(document), "request_sha256": value["head"]["request_sha256"]}


def _one(bindings: list[dict], target_id: str) -> dict:
    found = [b for b in bindings if b["target_id"] == target_id]
    if len(found) != 1:
        fail("reviewed_receipt_target_missing")
    return found[0]


def _content(binding: dict, roots: dict) -> bytes:
    ref = binding["path_ref"]
    if ref["root_id"] not in roots:
        fail("reviewed_receipt_root_missing")
    raw = read_project_bytes(Path(roots[ref["root_id"]]), ref["relative_path"])
    if binding["sha256"] != digest(raw) or binding["size"] != len(raw):
        fail("reviewed_receipt_current_target_mismatch")
    return raw


def _path(binding: dict, root_id: str, relative: str):
    if binding["path_ref"]["root_id"] != root_id or binding["path_ref"]["relative_path"] != relative:
        fail("reviewed_receipt_path_mismatch")


def verify_reviewed_history(*, root: Path, book_id: str, root_map: dict,
                            baseline_document_sha256: str) -> dict:
    result, _ = _read_reviewed_history(root=root, book_id=book_id, root_map=root_map,
                                      baseline_document_sha256=baseline_document_sha256)
    return result


def _read_reviewed_history(*, root: Path, book_id: str, root_map: dict,
                           baseline_document_sha256: str) -> tuple[dict, dict]:
    """Verify all retained receipts and require the current file to be the tip.

    Each transaction publishes an immutable state snapshot as well as applying
    the current state. Replaying the chain proves the seven collections came
    from that confirmed baseline and those fact decisions. This is local
    integrity evidence, not a signature against a fully rewritten book/history.
    """
    root = assert_safe_local_tree(root)
    if (Path(root_map["story_project"]).absolute() != root
            or not Path(root_map["runtime"]).absolute().is_relative_to(root)
            or Path(root_map["runtime"]).absolute() == root):
        fail("reviewed_root_map_invalid")
    runtime = assert_safe_local_tree(root_map["runtime"])
    current_document = read_project_bytes(root, RELATIVE_PATH)
    current = decode_document(current_document, book_id=book_id)
    directory = assert_safe_local_tree(runtime / RECEIPT_DIRECTORY)
    if not directory.is_dir():
        fail("reviewed_publication_missing")
    records = {}
    revisions = set()
    # This namespace contains only these transactions for this book. Looking
    # at all receipts detects a rollback to an older valid state or a fork.
    for path in sorted(directory.iterdir()):
        relative = path.relative_to(runtime).as_posix()
        raw = read_project_bytes(runtime, relative)
        if path.suffix != ".json":
            fail("reviewed_receipt_namespace_invalid")
        receipt = json.loads(raw.decode("utf-8"))
        result = verify_publication_receipt(receipt, root_map=root_map)
        if not result["valid"] or receipt["schema_version"] != "2.1" or receipt["book_id"] != book_id:
            fail("reviewed_publication_invalid")
        state_binding = _one(receipt["artifacts"], "reviewed_state_archive")
        run_id = receipt["run_id"]
        _path(state_binding, "runtime", f"{STATE_DIRECTORY}/{run_id}.md")
        document = _content(state_binding, root_map)
        value = decode_document(document, book_id=book_id)
        head = value["head"]
        checkpoint = source_revision_after(document, book_id=book_id)
        if any(t["target_id"] == "reviewed_identity" for t in receipt["apply_targets"]):
            if head["kind"] != "baseline":
                fail("reviewed_upgrade_receipt_kind_invalid")
            checkpoint = _upgrade_checkpoint(receipt, document=document, roots=root_map)
        if (head["run_id"] != run_id or head["receipt_id"] != receipt["receipt_id"]
                or head["receipt_relative_path"] != relative
                or value["revision"] in revisions
                or receipt["story_project_source_revision_after"] != checkpoint):
            fail("reviewed_receipt_state_binding_invalid")
        _path({"path_ref": receipt["receipt_path_ref"]}, "runtime", relative)
        applied = _one(receipt["apply_targets"], "reviewed_state")
        _path(applied, "story_project", RELATIVE_PATH)
        if applied["sha256"] != state_binding["sha256"] or applied["size"] != len(document):
            fail("reviewed_applied_state_mismatch")
        final = json.loads(_content(receipt["final_run"], root_map).decode("utf-8"))
        if (final.get("record_type") != "reviewed_legacy_commit" or final.get("id") != run_id
                or final.get("book_id") != book_id or final.get("kind") != head["kind"]
                or final.get("request_sha256") != head["request_sha256"]
                or final.get("revision") != value["revision"] or final.get("committed") is not True):
            fail("reviewed_final_record_mismatch")
        prose = None
        if head["kind"] == "chapter":
            archived = _one(receipt["artifacts"], "reviewed_chapter_archive")
            _path(archived, "runtime", f"{PROSE_DIRECTORY}/{run_id}.md")
            prose = _content(archived, root_map)
            target = _one(receipt["apply_targets"], "reviewed_chapter")
            ref = target["path_ref"]
            actual_prose = resolve_prose(root, value["committed_through"])
            if (ref["root_id"] != "story_project" or actual_prose.path is None
                    or actual_prose.path.relative_to(root).as_posix() != ref["relative_path"]):
                fail("reviewed_chapter_path_invalid")
            if _content(target, root_map) != prose:
                fail("reviewed_chapter_archive_mismatch")
        revisions.add(value["revision"])
        records[relative] = {"document": document, "value": value, "prose": prose,
                             "receipt": receipt, "final": final}
    if not records:
        fail("reviewed_publication_missing")
    archives = assert_safe_local_tree(runtime / STATE_DIRECTORY)
    expected_archives = {f"{item['value']['head']['run_id']}.md" for item in records.values()}
    if not archives.is_dir() or {path.name for path in archives.iterdir()} != expected_archives:
        fail("reviewed_archive_receipt_coverage_mismatch")
    tip = max(records.values(), key=lambda item: item["value"]["revision"])
    if tip["document"] != current_document:
        fail("reviewed_current_state_is_not_tip")
    pending = tip
    visited = set()
    while True:
        head = pending["value"]["head"]
        relative = head["receipt_relative_path"]
        if relative in visited:
            fail("reviewed_history_cycle")
        visited.add(relative)
        parent = head["previous"]
        if parent is None:
            if digest(pending["document"]) != baseline_document_sha256:
                fail("reviewed_baseline_anchor_mismatch")
            break
        before = records.get(parent["receipt_relative_path"])
        if before is None or digest(before["document"]) != parent["document_sha256"]:
            fail("reviewed_history_missing")
        verify_transition(document=pending["document"], previous_document=before["document"],
                          book_id=book_id, chapter_prose=pending["prose"])
        pending = before
    if len(visited) != len(records) or len(records) != current["revision"]:
        fail("reviewed_history_fork")
    if read_project_bytes(root, RELATIVE_PATH) != current_document:
        fail("reviewed_source_changed_during_read")
    return ({"value": copy.deepcopy(current), "document_sha256": digest(current_document),
             "receipt_count": len(records), "receipt_hash": tip["receipt"]["receipt_hash"]}, records)


def read_verified_commit_requests(*, root: Path, book_id: str, root_map: dict,
                                  baseline_document_sha256: str) -> list[dict]:
    """Read publication facts for rebuilding an index; never regenerate.

    The caller owns the book lock and pending-transaction boundary. This is
    stricter than the format-only reader: every chapter needs its complete
    immutable request artifact. A valid generic receipt alone is insufficient.
    It verifies historical publication, not permission for another publication.
    """
    from core.engine.chapter_commit_request import (
        REQUEST_DIRECTORY, REQUEST_TARGET, verify_commit_request_evidence,
    )
    from core.engine.persistence_v2 import validate_persistence_manifest_v2

    history, records = _read_reviewed_history(root=root, book_id=book_id, root_map=root_map,
                                             baseline_document_sha256=baseline_document_sha256)
    publications = []
    scopes = set()
    for item in sorted(records.values(), key=lambda item: item["value"]["revision"]):
        value, receipt = item["value"], item["receipt"]
        if value["head"]["kind"] != "chapter":
            continue
        binding = _one(receipt["artifacts"], REQUEST_TARGET)
        _path(binding, "runtime", f"{REQUEST_DIRECTORY}/{receipt['run_id']}.json")
        evidence = json.loads(_content(binding, root_map).decode("utf-8"))
        ref = receipt["manifest"]["path_ref"]
        manifest = validate_persistence_manifest_v2(json.loads(read_project_bytes(
            Path(root_map[ref["root_id"]]), ref["relative_path"]).decode("utf-8")))
        if manifest["manifest_digest"] != receipt["manifest"]["sha256"]:
            fail("reviewed_request_manifest_changed")
        parent = records[value["head"]["previous"]["receipt_relative_path"]]
        request = verify_commit_request_evidence(evidence=evidence, previous_document=parent["document"],
            document=item["document"], receipt=receipt, manifest=manifest, final_record=item["final"])
        scope = request["idempotency_scope_sha256"]
        if scope in scopes:
            fail("reviewed_duplicate_idempotency_publication")
        scopes.add(scope)
        publications.append({"record_type": "reviewed_chapter_publication", "schema_version": "1.0",
            "book_id": book_id, "chapter": value["committed_through"], "revision": value["revision"],
            "run_id": receipt["run_id"], "request": request,
            "request_sha256": value["head"]["request_sha256"],
            "preparation_sha256": evidence["preparation"]["preparation_sha256"],
            "request_evidence_sha256": evidence["evidence_sha256"],
            "receipt_id": receipt["receipt_id"], "receipt_relative_path": value["head"]["receipt_relative_path"],
            "receipt_hash": receipt["receipt_hash"], "document_sha256": digest(item["document"]),
            "prose_sha256": digest(item["prose"]), "publication_verified": True})
    if digest(read_project_bytes(root, RELATIVE_PATH)) != history["document_sha256"]:
        fail("reviewed_source_changed_during_read")
    return publications


def _upgrade_checkpoint(receipt: dict, *, document: bytes, roots: dict) -> dict:
    from core.story_project.activation import validate_story_state_calibration_report
    from core.engine.persistence_v2 import validate_persistence_manifest_v2
    from core.story_project.reviewed_identity import verify_reviewed_identity_transition, verify_upgrade_preparation_evidence

    run_id = receipt["run_id"]
    before = _one(receipt["artifacts"], "reviewed_identity_before")
    after = _one(receipt["artifacts"], "reviewed_identity_after")
    calibration = _one(receipt["artifacts"], "reviewed_calibration")
    for binding, suffix in ((before, "before"), (after, "after"), (calibration, "calibration")):
        _path(binding, "runtime", f"reviewed/upgrades/{run_id}-{suffix}.json")
    after_bytes = _content(after, roots)
    identity = verify_reviewed_identity_transition(old_identity_bytes=_content(before, roots),
        new_identity_bytes=after_bytes, baseline_document=document)
    applied = _one(receipt["apply_targets"], "reviewed_identity")
    _path(applied, "story_project", ".novelagent/project.json")
    if _content(applied, roots) != after_bytes:
        fail("reviewed_upgrade_identity_mismatch")
    report = validate_story_state_calibration_report(json.loads(_content(calibration, roots).decode("utf-8")))
    if (not report["strict_eligible"] or report["book_id"] != identity.book_id
            or report["report_sha256"] != identity.payload["activation"]["calibration_report_sha256"]):
        fail("reviewed_upgrade_calibration_mismatch")
    preparation = _one(receipt["artifacts"], "reviewed_upgrade_preparation")
    _path(preparation, "runtime", f"reviewed/upgrades/{run_id}-preparation.json")
    manifest_ref = receipt["manifest"]["path_ref"]
    manifest = validate_persistence_manifest_v2(json.loads(read_project_bytes(
        Path(roots[manifest_ref["root_id"]]), manifest_ref["relative_path"]).decode("utf-8")))
    if manifest["manifest_digest"] != receipt["manifest"]["sha256"]:
        fail("reviewed_upgrade_manifest_changed")
    verify_upgrade_preparation_evidence(
        preparation=json.loads(_content(preparation, roots).decode("utf-8")),
        final_record=json.loads(_content(receipt["final_run"], roots).decode("utf-8")),
        old_identity_bytes=_content(before, roots), new_identity_bytes=after_bytes, baseline_document=document,
        calibration_report=report, read_set=manifest["immutable"]["story_project_read_set"],
        transaction_binding={"run_id": receipt["run_id"], "context_digest": receipt["context_digest"],
            "generation_input_context_digest": receipt["generation_input_context_digest"],
            "receipt_id": receipt["receipt_id"], "receipt_path_ref": receipt["receipt_path_ref"]})
    return {"schema_version": "1.0", "book_id": identity.book_id,
        "root_uuid": applied["path_ref"]["root_uuid"], "identity_sha256": digest(after_bytes),
        "authority_epoch": 0, "head_event_hash": None,
        "reviewed_state": source_revision_after(document, book_id=identity.book_id)}


def load_verified_current(*, root: Path, book_id: str, next_chapter: int, root_map: dict,
                          baseline_document_sha256: str) -> dict:
    result = verify_reviewed_history(root=root, book_id=book_id, root_map=root_map,
                                     baseline_document_sha256=baseline_document_sha256)
    verify_current_sources(root=root, value=result["value"], next_chapter=next_chapter)
    if infer_next_chapter(root) != next_chapter:
        fail("reviewed_prose_cursor_mismatch")
    return result


def load_runtime_view(context: dict, *, root_map: dict, baseline_document_sha256: str) -> dict:
    """Internal explicit bridge; normal identity discovery remains disabled."""
    from core.story_project.reviewed_state_context import build_reviewed_state_context

    result = load_verified_current(root=Path(context["story_project_root"]),
        book_id=context["project_identity"]["book_id"], next_chapter=context["chapter_index"],
        root_map=root_map, baseline_document_sha256=baseline_document_sha256)
    value = result["value"]
    return build_reviewed_state_context(context, authoritative_state=value["current_state"],
        source_relative_path=RELATIVE_PATH, source_sha256=result["document_sha256"],
        manual_sources_sha256=value["source_revision"]["sha256"], committed_through=value["committed_through"],
        static_references=value["static_references"])
