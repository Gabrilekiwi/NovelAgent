"""Internal preparation/transaction for a legacy reviewed-format upgrade.

No public upgrade entrypoint: the product service must first own maintenance,
stop active work and verify the T7 backup/T2 onboarding gates. Hashes below bind
verified inputs; they are not author confirmation or maintenance authorization.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_fact_context import validate_trusted_chapter_context
from core.engine.persistence_v2 import PersistenceV2Target, PersistenceV2Transaction, bind_final_run_record_receipt
from core.engine.root_registry import RootRegistryService
from core.engine.safe_paths import assert_safe_local_tree
from core.path_refs import path_ref_for
from core.story_project.activation import validate_story_state_calibration_report
from core.story_project.identity import load_project_identity
from core.story_project.paths import scan_prose_chapters
from core.story_project.read_set import declared_read_set_writes, verify_story_project_read_set
from core.story_project.reviewed_identity import (
    UPGRADE_PREPARATION_VERSION, UPGRADE_TRANSITION, build_reviewed_identity, verify_reviewed_identity_transition,
)
from core.story_project.reviewed_state_document import RELATIVE_PATH, create_baseline, decode_document, digest, read_project_bytes
from core.story_project.reviewed_state_store import source_revision_after


class ReviewedUpgradeError(ValueError):
    code = "reviewed_upgrade_invalid"


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def prepare_reviewed_upgrade(*, trusted_context, baseline_state: dict, confirmed_baseline: dict,
                             static_references: list[dict], calibration_report: dict, run_id: str) -> dict:
    context = validate_trusted_chapter_context(trusted_context)
    root = Path(context["story_project_root"])
    old_identity_bytes = read_project_bytes(root, ".novelagent/project.json")
    old = load_project_identity(root)
    if (old.to_dict() != context["project_identity"] or old.authority["mode"] != "legacy_markdown_v1"
            or old.story_state_mode != "strict" or "reviewed_state" in context):
        raise ReviewedUpgradeError("upgrade requires a fresh existing legacy Strict context")
    report = validate_story_state_calibration_report(calibration_report)
    activation = old.activation
    if (not report["strict_eligible"] or report["book_id"] != old.book_id
            or report["report_sha256"] != activation["calibration_report_sha256"]
            or report["parser_version"] != activation["parser_version"]
            or report["semantic_schema_version"] != activation["semantic_schema_version"]
            or report["target_layout_profile_version"] != activation["layout_profile_version"]):
        raise ReviewedUpgradeError("calibration report does not prove the preserved Strict activation")
    through = context["chapter_index"] - 1
    chapters = scan_prose_chapters(root)
    if through < 1 or set(chapters) != set(range(1, through + 1)) or any(len(v) != 1 for v in chapters.values()):
        raise ReviewedUpgradeError("upgrade requires a complete unambiguous existing chapter sequence")
    if (root / RELATIVE_PATH).exists():
        raise ReviewedUpgradeError("upgrade cannot adopt or overwrite an existing reviewed-state document")
    read_set = context["read_set"]
    verify_story_project_read_set(read_set, phase="prepare")
    if read_set["identity_revision"] != digest(old_identity_bytes):
        raise ReviewedUpgradeError("identity bytes changed after context capture")
    document = create_baseline(root=root, book_id=old.book_id, chapter=through, state=baseline_state,
        confirmed_baseline=confirmed_baseline, old_identity_sha256=digest(old_identity_bytes),
        run_id=run_id, static_references=static_references)
    identity = build_reviewed_identity(old_identity_bytes=old_identity_bytes, baseline_document=document)
    binding = {"book_id": old.book_id, "run_id": run_id, "committed_through": through,
        "old_identity_sha256": digest(old_identity_bytes), "new_identity_sha256": digest(json_bytes(identity)),
        "baseline_document_sha256": digest(document), "baseline_confirmation_sha256": digest(confirmed_baseline),
        "calibration_report_sha256": report["report_sha256"], "context_sha256": trusted_context.context_sha256,
        "read_set_sha256": digest(read_set)}
    result = {"version": UPGRADE_PREPARATION_VERSION, "publication_authorized": False, "binding": binding,
        "old_identity_text": old_identity_bytes.decode("utf-8"), "new_identity": identity,
        "baseline_document": document.decode("utf-8"), "calibration_report": copy.deepcopy(report),
        "read_set": copy.deepcopy(read_set), "request_sha256": digest({"version": UPGRADE_PREPARATION_VERSION, "binding": binding})}
    result["preparation_sha256"] = digest(result)
    verify_story_project_read_set(read_set, phase="prepare")
    return result


def prepare_reviewed_upgrade_transaction(*, preparation: dict, fault_injector=None) -> PersistenceV2Transaction:
    """Stage the verified upgrade for an already gated caller; no auto commit."""
    p = preparation
    if (p.get("version") != UPGRADE_PREPARATION_VERSION or p.get("publication_authorized") is not False
            or p.get("preparation_sha256") != digest({k: v for k, v in p.items() if k != "preparation_sha256"})):
        raise ReviewedUpgradeError("upgrade preparation checksum or type changed")
    binding = p["binding"]
    if p["request_sha256"] != digest({"version": UPGRADE_PREPARATION_VERSION, "binding": binding}):
        raise ReviewedUpgradeError("upgrade request binding changed")
    root = Path(p["read_set"]["root_identity"]["resolved_path"])
    runtime = root / ".novelagent/runtime"
    verify_story_project_read_set(p["read_set"], phase="prepare")
    baseline = decode_document(p["baseline_document"].encode("utf-8"), book_id=binding["book_id"])["initial_baseline"]
    current = prepare_reviewed_upgrade(
        trusted_context=ChapterCandidateContextBuilder(root).build(p["read_set"]["chapter_index"]),
        baseline_state=baseline["state"], confirmed_baseline=baseline["confirmation"],
        static_references=baseline["static_references"], calibration_report=p["calibration_report"], run_id=binding["run_id"])
    if current != p:
        raise ReviewedUpgradeError("upgrade inputs or current context changed after preparation")
    old_bytes = p["old_identity_text"].encode("utf-8")
    new_bytes = json_bytes(p["new_identity"])
    document = p["baseline_document"].encode("utf-8")
    identity = verify_reviewed_identity_transition(old_identity_bytes=old_bytes, new_identity_bytes=new_bytes,
                                                   baseline_document=document)
    report = validate_story_state_calibration_report(p["calibration_report"])
    if (read_project_bytes(root, ".novelagent/project.json") != old_bytes
            or digest(old_bytes) != binding["old_identity_sha256"] or digest(new_bytes) != binding["new_identity_sha256"]
            or digest(document) != binding["baseline_document_sha256"] or digest(p["read_set"]) != binding["read_set_sha256"]
            or binding["book_id"] != identity.book_id or p["read_set"]["book_id"] != identity.book_id
            or report["book_id"] != identity.book_id or not report["strict_eligible"]
            or report["report_sha256"] != binding["calibration_report_sha256"]
            or report["report_sha256"] != identity.payload["activation"]["calibration_report_sha256"]):
        raise ReviewedUpgradeError("upgrade prepared bytes or calibration changed")
    verify_story_project_read_set(p["read_set"], phase="prepare")
    run_id = identity.payload["reviewed_state"]["upgrade"]["run_id"]
    if run_id != binding["run_id"]:
        raise ReviewedUpgradeError("upgrade run binding changed")
    roots = {"story_project": root, "runtime": runtime}
    transaction_root = runtime / "reviewed/p"
    assert_safe_local_tree(runtime).mkdir(exist_ok=True)
    registry = RootRegistryService(transaction_root).ensure(roots)
    def ref(relative: str, root_id: str):
        return path_ref_for(roots[root_id] / relative, root_id=root_id, root=roots[root_id])
    state_target = PersistenceV2Target(target_id="reviewed_state", kind="tracking",
        path_ref=ref(RELATIVE_PATH, "story_project"), content=document, expected_before_exists=False)
    identity_target = PersistenceV2Target(target_id="reviewed_identity", kind="project_identity",
        path_ref=ref(".novelagent/project.json", "story_project"), content=new_bytes,
        expected_before_exists=True, expected_before_sha256=digest(old_bytes))
    declared = declared_read_set_writes(p["read_set"], [(root / RELATIVE_PATH, digest(document), len(document))])
    state_revision = source_revision_after(document, book_id=identity.book_id)
    declared.append({"relative_path": ".novelagent/project.json", "action": "replace", "role": "project_identity",
        "book_id": identity.book_id, "after_sha256": digest(new_bytes), "after_size": len(new_bytes),
        "expected_authority_epoch": 0, "expected_head_event_hash": None,
        "after_authority_epoch": 0, "after_head_event_hash": None, "transition": UPGRADE_TRANSITION,
        "reviewed_state": state_revision})
    checkpoint = {"schema_version": "1.0", "book_id": identity.book_id,
        "root_uuid": registry["roots"]["story_project"]["root_uuid"], "identity_sha256": digest(new_bytes),
        "authority_epoch": 0, "head_event_hash": None, "reviewed_state": state_revision}
    upgrade = identity.payload["reviewed_state"]["upgrade"]
    receipt_ref = ref(upgrade["receipt_relative_path"], "runtime")
    final = bind_final_run_record_receipt({"record_type": "reviewed_legacy_commit", "id": run_id,
        "book_id": identity.book_id, "kind": "baseline", "request_sha256": state_revision["request_sha256"],
        "revision": 1, "committed": True, "upgrade_request_sha256": p["request_sha256"]},
        receipt_id=upgrade["receipt_id"], receipt_path_ref=receipt_ref)
    artifacts = [PersistenceV2Target(target_id=target_id, kind="review_artifact", phase="publication",
        path_ref=ref(relative, "runtime"), content=raw) for target_id, relative, raw in (
            ("reviewed_state_archive", f"reviewed/states/{run_id}.md", document),
            ("reviewed_identity_before", f"reviewed/upgrades/{run_id}-before.json", old_bytes),
            ("reviewed_identity_after", f"reviewed/upgrades/{run_id}-after.json", new_bytes),
            ("reviewed_calibration", f"reviewed/upgrades/{run_id}-calibration.json", json_bytes(report)),
            ("reviewed_upgrade_preparation", f"reviewed/upgrades/{run_id}-preparation.json", json_bytes(p)))]
    tx = PersistenceV2Transaction(transaction_root=transaction_root, run_id=run_id, book_id=identity.book_id,
        root_map=roots, story_project_read_set=p["read_set"], read_set_declared_writes=declared,
        fault_injector=fault_injector)
    tx.prepare(apply_targets=[state_target, identity_target], artifacts=artifacts, final_run_record=final,
        final_run_path_ref=ref(f"reviewed/runs/{run_id}.json", "runtime"), receipt_id=upgrade["receipt_id"],
        receipt_path_ref=receipt_ref, context_digest=binding["context_sha256"],
        generation_input_context_digest=binding["context_sha256"], story_project_source_revision_after=checkpoint,
        candidate_result={"upgrade_request_sha256": p["request_sha256"]})
    return tx
