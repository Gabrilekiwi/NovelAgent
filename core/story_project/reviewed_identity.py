"""Internal identity 3.0 codec; ordinary project readers are not enabled here."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from core.schema import SchemaValidationError, validate_schema
from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION, ProjectIdentityError, validate_project_identity
from core.story_project.reviewed_state_document import (
    FORMAT, RELATIVE_PATH, decode_document, digest, read_project_bytes,
)


REVIEWED_IDENTITY_VERSION = "3.0"
REVIEWED_WRITER_CONTRACT = 2
UPGRADE_TRANSITION = "legacy_reviewed_format_upgrade_v1"
UPGRADE_PREPARATION_VERSION = "reviewed_upgrade_prepare_v1"


class ReviewedIdentityError(ProjectIdentityError):
    code = "reviewed_identity_invalid"


@dataclass(frozen=True)
class ReviewedIdentityMetadata:
    """Metadata only, deliberately not a permit for normal execution."""
    payload: dict

    @property
    def book_id(self):
        return self.payload["book_id"]

    @property
    def authority(self):
        return copy.deepcopy(self.payload["authority"])

    @property
    def ephemeral(self):
        return False

    @property
    def story_state_mode(self):
        return self.payload["story_state_mode"]

    @property
    def activation(self):
        return copy.deepcopy(self.payload["activation"])

    def to_dict(self):
        return copy.deepcopy(self.payload)


def validate_reviewed_identity(value: Any) -> ReviewedIdentityMetadata:
    native = isinstance(value, dict) and isinstance(value.get("reviewed_state"), dict) and "genesis" in value["reviewed_state"]
    try:
        validate_schema(value, "new_book_identity.schema.json" if native else "reviewed_project_identity.schema.json")
    except SchemaValidationError as exc:
        raise ReviewedIdentityError(str(exc)) from exc
    value = copy.deepcopy(value)
    legacy_projection = {k: v for k, v in value.items() if k != "reviewed_state"}
    legacy_projection["schema_version"] = "2.0"
    # Reuse the exact identity/activation/legacy-authority validation; do not
    # duplicate or relax it merely to introduce a storage format boundary.
    if native:
        legacy_projection["story_state_mode"] = "shadow"  # Validate common metadata without claiming legacy calibration.
    legacy = validate_project_identity(legacy_projection)
    expected = {**LEGACY_AUTHORITY_PROJECTION, "minimum_writer_contract": value["authority"]["minimum_writer_contract"]}
    if native:
        if legacy.authority != expected or value["authority"]["minimum_writer_contract"] != REVIEWED_WRITER_CONTRACT:
            raise ReviewedIdentityError("invalid new book authority")
        return ReviewedIdentityMetadata(value)
    if legacy.authority != expected or legacy.story_state_mode != "strict" or legacy.activation is None:
        raise ReviewedIdentityError("reviewed identity requires persisted legacy Strict authority")
    if value["authority"]["minimum_writer_contract"] < REVIEWED_WRITER_CONTRACT:
        raise ReviewedIdentityError("reviewed identity requires writer contract 2 or later")
    binding = value["reviewed_state"]
    upgrade = binding["upgrade"]
    if (upgrade["receipt_id"] != f"reviewed-{upgrade['run_id']}"
            or upgrade["receipt_relative_path"] != f"reviewed/receipts/{upgrade['run_id']}.json"
            or upgrade["calibration_report_sha256"] != legacy.activation["calibration_report_sha256"]):
        raise ReviewedIdentityError("reviewed identity upgrade locator or calibration binding differs")
    return ReviewedIdentityMetadata(value)


def build_reviewed_identity(*, old_identity_bytes: bytes, baseline_document: bytes) -> dict:
    old = validate_project_identity(json.loads(old_identity_bytes.decode("utf-8-sig")))
    if (old.ephemeral or old.story_state_mode != "strict" or old.activation is None
            or old.authority["mode"] != "legacy_markdown_v1"
            or old.authority["minimum_writer_contract"] > REVIEWED_WRITER_CONTRACT):
        raise ReviewedIdentityError("only a supported persisted legacy Strict identity may be upgraded")
    state = decode_document(baseline_document, book_id=old.book_id)
    confirmation = state["initial_baseline"]["confirmation"]
    if state["committed_through"] < 1 or state["head"]["kind"] != "baseline" or confirmation["old_identity_sha256"] != digest(old_identity_bytes):
        raise ReviewedIdentityError("upgrade requires a baseline bound to the exact old identity bytes")
    head = state["head"]
    value = old.to_dict()
    value.update(schema_version=REVIEWED_IDENTITY_VERSION, root_hint=".",
                 authority={**old.authority, "minimum_writer_contract": REVIEWED_WRITER_CONTRACT})
    value["reviewed_state"] = {"format": FORMAT, "relative_path": RELATIVE_PATH,
        "baseline_document_sha256": digest(baseline_document), "upgrade": {
            "run_id": head["run_id"], "receipt_id": head["receipt_id"],
            "receipt_relative_path": head["receipt_relative_path"],
            "old_identity_sha256": digest(old_identity_bytes), "baseline_confirmation_sha256": digest(confirmation),
            "calibration_report_sha256": old.activation["calibration_report_sha256"]}}
    return validate_reviewed_identity(value).to_dict()


def verify_reviewed_identity_transition(*, old_identity_bytes: bytes, new_identity_bytes: bytes,
                                        baseline_document: bytes) -> ReviewedIdentityMetadata:
    expected = build_reviewed_identity(old_identity_bytes=old_identity_bytes, baseline_document=baseline_document)
    actual = validate_reviewed_identity(json.loads(new_identity_bytes.decode("utf-8-sig")))
    if actual.to_dict() != expected:
        raise ReviewedIdentityError("upgrade must preserve book, creation time, Strict activation and legacy authority")
    return actual


def declared_upgrade_identity(root: Path, *, read_set: dict, declaration: dict, phase: str):
    """Read only a hash-pinned after-image inside its declared transaction.

    This narrow path is necessary for pre-marker validation while ordinary
    identity 3.0 loading is still disabled. It does not enable generation.
    """
    if declaration.get("transition") != UPGRADE_TRANSITION or phase not in {"during_apply", "pre_marker"}:
        return None
    raw = read_project_bytes(root, ".novelagent/project.json")
    if digest(raw) == read_set["identity_revision"]:
        return None  # During apply the old identity may still be present.
    if digest(raw) != declaration.get("after_sha256"):
        raise ReviewedIdentityError("declared upgrade identity after-image changed")
    identity = validate_reviewed_identity(json.loads(raw.decode("utf-8-sig")))
    binding = identity.payload["reviewed_state"]
    if (identity.book_id != read_set["book_id"]
            or binding["upgrade"]["old_identity_sha256"] != read_set["identity_revision"]
            or binding["baseline_document_sha256"] != declaration.get("reviewed_state", {}).get("reviewed_document_sha256")):
        raise ReviewedIdentityError("declared upgrade does not bind its before-image and baseline")
    return identity


def validate_upgrade_transaction_targets(*, targets: list[dict], declaration: dict, read_set: dict,
                                         transaction_binding: dict):
    """Validate the distinct upgrade branch before the generic EA CAS check."""
    from core.story_project.activation import validate_story_state_calibration_report
    from core.story_project.reviewed_state_store import source_revision_after

    by_id = {target["target_id"]: target for target in targets}
    required = {"reviewed_state", "reviewed_identity", "reviewed_state_archive", "reviewed_identity_before",
                "reviewed_identity_after", "reviewed_calibration", "reviewed_upgrade_preparation", "final-run-record"}
    if set(by_id) != required or {t["target_id"] for t in targets if t["phase"] == "apply"} != {"reviewed_state", "reviewed_identity"}:
        raise ReviewedIdentityError("upgrade must atomically bind only identity/state plus its required evidence")
    state, identity_target = by_id["reviewed_state"], by_id["reviewed_identity"]
    if (state["path_ref"]["root_id"] != "story_project" or state["path_ref"]["relative_path"] != RELATIVE_PATH
            or identity_target["path_ref"]["root_id"] != "story_project"
            or identity_target["path_ref"]["relative_path"] != ".novelagent/project.json"
            or state["before_exists"] or not identity_target["before_exists"]
            or identity_target["before_sha256"] != read_set["identity_revision"]):
        raise ReviewedIdentityError("upgrade target paths or before-images are invalid")
    expected = {"relative_path": ".novelagent/project.json", "action": "replace", "role": "project_identity",
        "book_id": read_set["book_id"], "after_sha256": identity_target["after_sha256"],
        "after_size": identity_target["after_size"], "expected_authority_epoch": 0, "expected_head_event_hash": None,
        "after_authority_epoch": 0, "after_head_event_hash": None, "transition": UPGRADE_TRANSITION,
        "reviewed_state": declaration.get("reviewed_state")}
    if declaration != expected:
        raise ReviewedIdentityError("upgrade declaration must preserve legacy epoch/head exactly")
    if identity_target.get("_content") is None:
        return  # Immutable manifest validation uses the prepare-time bindings.
    old_raw, new_raw, document = identity_target["_before"], identity_target["_content"], state["_content"]
    identity = verify_reviewed_identity_transition(old_identity_bytes=old_raw, new_identity_bytes=new_raw,
                                                   baseline_document=document)
    state_revision = source_revision_after(document, book_id=identity.book_id)
    if (identity.book_id != read_set["book_id"] or state_revision["committed_through"] + 1 != read_set["chapter_index"]
            or declaration["reviewed_state"] != state_revision):
        raise ReviewedIdentityError("upgrade declaration does not match its baseline state")
    upgrade = identity.payload["reviewed_state"]["upgrade"]
    run_id = upgrade["run_id"]
    for target_id, relative, raw in (
        ("reviewed_state_archive", f"reviewed/states/{run_id}.md", document),
        ("reviewed_identity_before", f"reviewed/upgrades/{run_id}-before.json", old_raw),
        ("reviewed_identity_after", f"reviewed/upgrades/{run_id}-after.json", new_raw),
    ):
        item = by_id[target_id]
        if (item["path_ref"]["root_id"] != "runtime" or item["path_ref"]["relative_path"] != relative
                or item["_content"] != raw):
            raise ReviewedIdentityError("upgrade immutable before/after evidence differs from applied bytes")
    report = validate_story_state_calibration_report(json.loads(by_id["reviewed_calibration"]["_content"].decode("utf-8")))
    if (not report["strict_eligible"] or report["book_id"] != identity.book_id
            or report["report_sha256"] != upgrade["calibration_report_sha256"]):
        raise ReviewedIdentityError("upgrade calibration archive is not bound to preserved activation")
    for target_id, suffix in (("reviewed_calibration", "calibration"), ("reviewed_upgrade_preparation", "preparation")):
        ref = by_id[target_id]["path_ref"]
        if ref["root_id"] != "runtime" or ref["relative_path"] != f"reviewed/upgrades/{run_id}-{suffix}.json":
            raise ReviewedIdentityError("upgrade evidence archive path is invalid")
    verify_upgrade_preparation_evidence(
        preparation=json.loads(by_id["reviewed_upgrade_preparation"]["_content"].decode("utf-8")),
        final_record=json.loads(by_id["final-run-record"]["_content"].decode("utf-8")),
        old_identity_bytes=old_raw, new_identity_bytes=new_raw, baseline_document=document,
        calibration_report=report, read_set=read_set, transaction_binding=transaction_binding)


def verify_upgrade_preparation_evidence(*, preparation: dict, final_record: dict, old_identity_bytes: bytes,
                                       new_identity_bytes: bytes, baseline_document: bytes, calibration_report: dict,
                                       read_set: dict, transaction_binding: dict) -> dict:
    """Bind the complete preparation to actual transaction evidence, including cold reads."""
    identity = verify_reviewed_identity_transition(old_identity_bytes=old_identity_bytes,
        new_identity_bytes=new_identity_bytes, baseline_document=baseline_document)
    state = decode_document(baseline_document, book_id=identity.book_id)
    upgrade = identity.payload["reviewed_state"]["upgrade"]
    if (not isinstance(transaction_binding, dict) or transaction_binding.get("run_id") != upgrade["run_id"]
            or transaction_binding.get("receipt_id") != upgrade["receipt_id"]
            or transaction_binding.get("receipt_path_ref", {}).get("root_id") != "runtime"
            or transaction_binding["receipt_path_ref"].get("relative_path") != upgrade["receipt_relative_path"]
            or transaction_binding.get("generation_input_context_digest") != transaction_binding.get("context_digest")
            or read_set["book_id"] != identity.book_id or read_set["identity_revision"] != digest(old_identity_bytes)
            or read_set["chapter_index"] != state["committed_through"] + 1):
        raise ReviewedIdentityError("upgrade transaction identity/context differs from its preparation")
    binding = {"book_id": identity.book_id, "run_id": upgrade["run_id"], "committed_through": state["committed_through"],
        "old_identity_sha256": digest(old_identity_bytes), "new_identity_sha256": digest(new_identity_bytes),
        "baseline_document_sha256": digest(baseline_document),
        "baseline_confirmation_sha256": digest(state["initial_baseline"]["confirmation"]),
        "calibration_report_sha256": calibration_report["report_sha256"],
        "context_sha256": transaction_binding["context_digest"], "read_set_sha256": digest(read_set)}
    expected = {"version": UPGRADE_PREPARATION_VERSION, "publication_authorized": False, "binding": binding,
        "old_identity_text": old_identity_bytes.decode("utf-8"), "new_identity": identity.to_dict(),
        "baseline_document": baseline_document.decode("utf-8"), "calibration_report": calibration_report,
        "read_set": read_set, "request_sha256": digest({"version": UPGRADE_PREPARATION_VERSION, "binding": binding})}
    expected["preparation_sha256"] = digest(expected)
    if preparation != expected:
        raise ReviewedIdentityError("upgrade preparation archive differs from actual transaction inputs")
    expected_final = {"record_type": "reviewed_legacy_commit", "id": upgrade["run_id"], "book_id": identity.book_id,
        "kind": "baseline", "request_sha256": state["head"]["request_sha256"], "revision": 1, "committed": True,
        "upgrade_request_sha256": expected["request_sha256"],
        "publication_receipt": {"id": upgrade["receipt_id"], "path_ref": transaction_binding["receipt_path_ref"]}}
    if final_record != expected_final:
        raise ReviewedIdentityError("upgrade final record differs from the bound preparation and receipt")
    return copy.deepcopy(expected)


def load_verified_reviewed_identity(root: Path) -> dict:
    """Explicit diagnostic/upgrade cold read, independent of execution enablement."""
    return _load_verified_reviewed_identity(root)


def _load_verified_reviewed_identity(root: Path, *, _parent_document: bytes | None = None) -> dict:
    from core.story_project.reviewed_state_store import _read_reviewed_history

    identity_bytes = read_project_bytes(root, ".novelagent/project.json")
    identity = validate_reviewed_identity(json.loads(identity_bytes.decode("utf-8-sig")))
    runtime = Path(root) / ".novelagent/runtime"
    binding = identity.payload["reviewed_state"]
    if "upgrade" in binding:
        receipt = json.loads(read_project_bytes(runtime, binding["upgrade"]["receipt_relative_path"]).decode("utf-8"))
        targets = [t for t in receipt.get("apply_targets", []) if t.get("target_id") == "reviewed_identity"]
        if (len(targets) != 1 or targets[0].get("sha256") != digest(identity_bytes)
                or receipt.get("run_id") != binding["upgrade"]["run_id"]
                or receipt.get("receipt_id") != binding["upgrade"]["receipt_id"]):
            raise ReviewedIdentityError("identity has no matching atomic upgrade publication")
    result, _ = _read_reviewed_history(root=root, book_id=identity.book_id,
        root_map={"story_project": root, "runtime": runtime}, baseline_document_sha256=binding["baseline_document_sha256"],
        _parent_document=_parent_document)
    if read_project_bytes(root, ".novelagent/project.json") != identity_bytes:
        raise ReviewedIdentityError("identity changed during cold verification")
    return {"identity": identity.to_dict(), "history": result}
