"""The first immutable record of a book created empty in a private staging directory."""
import json

from core.engine.persistence import atomic_create_json, atomic_create_text, atomic_write_json
from core.state.authoritative import empty_authoritative_state
from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION
from core.story_project.reviewed_identity import validate_reviewed_identity
from core.story_project.reviewed_state_document import (FORMAT, RELATIVE_PATH, STATE_DIRECTORY, RECEIPT_DIRECTORY,
    baseline_request, capture_sources, create_baseline, decode_document, digest, fail)

ORIGIN = "author_created_empty_book_v1"
SETTINGS = "设定/作者确认的开篇设定.md"


def _declaration(identity, references):
    return {"origin": ORIGIN, "book_id": identity["book_id"], "created_at": identity["created_at"],
            "committed_through": 0, "confirmed_settings": references}


def _receipt(identity_bytes, document, declaration):
    value = {"kind": ORIGIN, "declaration": declaration, "identity_sha256": digest(identity_bytes),
             "baseline_document_sha256": digest(document)}
    return {**value, "receipt_hash": digest(value)}


def initialize_staged_book(root, initial_identity, settings):
    if not root.name.startswith(".novelagent-create-"):
        raise ValueError("new book initialization requires a private creation directory")
    if any(any((root / name).iterdir()) for name in ("正文", "追踪", "设定", "大纲")):
        raise ValueError("new book initialization requires an empty book")
    refs = []
    if settings:
        atomic_create_text(root / SETTINGS, settings + "\n")
        refs.append({"relative_path": SETTINGS, "text": settings, "sha256": digest((root / SETTINGS).read_bytes())})
    ident = initial_identity.to_dict()
    declaration = _declaration(ident, refs)
    data = dict(book_id=ident["book_id"], chapter=0, state=empty_authoritative_state(),
                old_identity_sha256=digest(declaration), static_references=refs)
    doc = create_baseline(root=root, **data, run_id="start", confirmed_baseline=baseline_request(**data, sources=capture_sources(root)))
    ident.update(schema_version="3.0", root_hint=".", story_state_mode="strict", activation=None,
                 authority={**LEGACY_AUTHORITY_PROJECTION, "minimum_writer_contract": 2},
                 reviewed_state={"format": FORMAT, "relative_path": RELATIVE_PATH, "genesis": ORIGIN,
                                "baseline_document_sha256": digest(doc)})
    validate_reviewed_identity(ident)
    location = root / ".novelagent/project.json"
    atomic_write_json(location, ident)
    runtime = root / ".novelagent/runtime"
    for path in (root / RELATIVE_PATH, runtime / STATE_DIRECTORY / "start.md"):
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(doc)
    receipt = _receipt(location.read_bytes(), doc, declaration)
    atomic_create_json(runtime / RECEIPT_DIRECTORY / "start.json", receipt)


def verify_start(identity_bytes, receipt, document, *, book_id):
    identity = validate_reviewed_identity(json.loads(identity_bytes)).to_dict()
    value = decode_document(document, book_id=book_id)
    refs = value["initial_baseline"]["static_references"]
    declaration = _declaration(identity, refs)
    if (identity["book_id"] != book_id or identity["reviewed_state"].get("genesis") != ORIGIN
            or identity["reviewed_state"]["baseline_document_sha256"] != digest(document)
            or value["head"]["kind"] != "baseline" or value["head"]["run_id"] != "start"
            or value["committed_through"] != 0 or value["revision"] != 1
            or len(refs) > 1 or any(ref["relative_path"] != SETTINGS for ref in refs)
            or value["initial_baseline"]["confirmation"]["old_identity_sha256"] != digest(declaration)
            or receipt != _receipt(identity_bytes, document, declaration)):
        fail("reviewed_new_book_start_invalid")
    return value
