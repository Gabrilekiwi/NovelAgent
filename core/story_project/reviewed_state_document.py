"""Codec for reviewed_legacy_state_v1; no publication or identity activation.

These functions return bytes, never write them. Confirmation objects must come
from verified server records. Only the receipt-backed reader may turn a document
into an authoritative runtime view; a valid managed-block hash is insufficient.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

from core.engine.chapter_fact_support import normalize_fact_support, prepare_supported_fact_changes
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, RootBinding, SafePathResolver, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.path_refs import validate_path_ref
from core.state.authoritative import empty_authoritative_state, validate_authoritative_state
from core.state.location_mirrors import validate_character_location_mirrors
from core.story_project.managed_block import build_managed_projection, parse_managed_block, write_managed_block


FORMAT = "reviewed_legacy_state_v1"
RELATIVE_PATH = "追踪/已审核事实.md"
HEADER = "# 已审核事实\n\n本文件的当前状态由提交凭证核验；修改人工来源后需要重新审核。\n"
RECEIPT_DIRECTORY = "reviewed/receipts"
STATE_DIRECTORY = "reviewed/states"
PROSE_DIRECTORY = "reviewed/chapters"
_FIELDS = {"format", "revision", "initial_baseline", "source_revision", "current_state",
           "committed_through", "source_decisions", "last_transition", "static_references", "head"}


class ReviewedStateDocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def fail(code: str):
    raise ReviewedStateDocumentError(code)


def digest(value: Any) -> str:
    return (hashlib.sha256(value).hexdigest() if isinstance(value, bytes)
            else canonical_json_hash(value, exclude_environment_fields=False))


def _shape(value: Any, fields: set[str], code: str):
    if not isinstance(value, dict) or set(value) != fields:
        fail(code)


def _sha(value: Any):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        fail("reviewed_digest_invalid")


def _positive(value: Any):
    if type(value) is not int or value < 1:
        fail("reviewed_revision_or_chapter_invalid")


def _state(value: Any) -> dict:
    expected = empty_authoritative_state()
    _shape(value, set(expected), "reviewed_state_shape_invalid")
    if any(not isinstance(value[key], dict) for key in expected if isinstance(expected[key], dict)):
        fail("reviewed_state_shape_invalid")
    if value != validate_authoritative_state(copy.deepcopy(value)):
        fail("reviewed_state_normalization_required")
    validate_character_location_mirrors(value)
    return copy.deepcopy(value)


def _relative(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value:
        fail("reviewed_path_invalid")
    validate_path_ref({"root_id": "story_project", "relative_path": value})
    return value


def _source_path(value: Any) -> str:
    value = _relative(value)
    if (not value.startswith(("追踪/", "设定/")) or not value.lower().endswith(".md")
            or value.casefold() == RELATIVE_PATH.casefold()):
        fail("reviewed_source_path_invalid")
    return value


def _ordinary(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        fail("reviewed_source_link_forbidden")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        fail("reviewed_source_hardlink_forbidden")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        fail("reviewed_source_type_invalid")
    return info


def read_project_bytes(root: Path, relative: str) -> bytes:
    """Check path lineage before reading; callers still need a commit read set."""
    root = assert_safe_local_tree(root)
    relative = _relative(relative)
    # A read-local logical UUID is enough for physical lineage guards. Receipt
    # verification separately checks the persisted registry's actual UUIDs.
    resolver = SafePathResolver({"story_project": RootBinding("story_project", "00000000-0000-4000-8000-000000000000", root)})
    resolved = resolver.resolve({"root_id": "story_project", "relative_path": relative})
    path = resolved.path
    before = _ordinary(path)
    if not stat.S_ISREG(before.st_mode):
        fail("reviewed_source_type_invalid")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (before.st_dev, before.st_ino, 1):
            fail("reviewed_source_changed_during_read")
        raw = stream.read()
    after = _ordinary(path)
    resolver.resolve(resolved.path_ref, expected_guard=resolved.guard)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        fail("reviewed_source_changed_during_read")
    return raw


def capture_sources(root: Path) -> dict:
    root = assert_safe_local_tree(root)
    entries = {}

    def visit(directory: Path):
        if not stat.S_ISDIR(_ordinary(directory).st_mode):
            fail("reviewed_source_type_invalid")
        for path in sorted(directory.iterdir()):
            info = _ordinary(path)  # Inspect before descent, including junctions.
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif path.suffix.lower() == ".md":
                relative = path.relative_to(root).as_posix()
                if relative.casefold() == RELATIVE_PATH.casefold():
                    continue
                entries[relative] = _manual_source_hash(read_project_bytes(root, relative))

    for name in ("追踪", "设定"):
        directory = root / name
        if os.path.lexists(directory):
            visit(directory)
    return {"entries": entries, "sha256": digest(entries)}


def _manual_source_hash(raw: bytes) -> str:
    text = raw.decode("utf-8")
    parsed = parse_managed_block(text)
    if parsed is not None:
        text = text[:parsed.start_char] + text[parsed.end_char:]
    return digest(text.rstrip("\r\n").encode("utf-8"))


def _sources(value: Any):
    _shape(value, {"entries", "sha256"}, "reviewed_sources_invalid")
    if not isinstance(value["entries"], dict):
        fail("reviewed_sources_invalid")
    for path, sha in value["entries"].items():
        _source_path(path)
        _sha(sha)
    if value["sha256"] != digest(value["entries"]):
        fail("reviewed_source_hash_invalid")


def _references(references: Any, *, root: Path | None = None):
    if not isinstance(references, list):
        fail("reviewed_static_reference_invalid")
    seen = set()
    for ref in references:
        _shape(ref, {"relative_path", "sha256", "text"}, "reviewed_static_reference_invalid")
        _source_path(ref["relative_path"])
        _sha(ref["sha256"])
        if not isinstance(ref["text"], str) or not ref["text"].strip() or digest(ref) in seen:
            fail("reviewed_static_reference_invalid")
        seen.add(digest(ref))
        if root is not None:
            raw = read_project_bytes(root, ref["relative_path"])
            _verify_reference_content(ref, raw)


def _verify_reference_content(ref, raw: bytes):
    if digest(raw) != ref["sha256"] or ref["text"] not in raw.decode("utf-8"):
        fail("reviewed_static_reference_changed")


def baseline_request(*, book_id: str, chapter: int, state: dict, sources: dict,
                     old_identity_sha256: str, static_references: list[dict]) -> dict:
    _positive(chapter)
    _sha(old_identity_sha256)
    if not isinstance(book_id, str) or not book_id.strip():
        fail("reviewed_book_id_invalid")
    _state(state)
    _sources(sources)
    _references(static_references)
    return {"book_id": book_id, "chapter": chapter, "state_sha256": digest(state),
            "source_sha256": sources["sha256"], "static_references_sha256": digest(static_references),
            "old_identity_sha256": old_identity_sha256, "decision": "accept_baseline_as_of_chapter"}


def publication_head(*, run_id: str, kind: str, request_sha256: str,
                     previous_document: bytes | None = None, book_id: str) -> dict:
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id) is None:
        fail("reviewed_run_id_invalid")
    if kind not in {"baseline", "chapter", "source_resolution"}:
        fail("reviewed_head_kind_invalid")
    _sha(request_sha256)
    parent = decode_document(previous_document, book_id=book_id) if previous_document is not None else None
    if (kind == "baseline") != (parent is None) or (parent and parent["head"]["run_id"] == run_id):
        fail("reviewed_head_parent_invalid")
    return {"kind": kind, "run_id": run_id, "receipt_id": f"reviewed-{run_id}",
            "receipt_relative_path": f"{RECEIPT_DIRECTORY}/{run_id}.json", "request_sha256": request_sha256,
            "previous": {"receipt_relative_path": parent["head"]["receipt_relative_path"],
                         "document_sha256": digest(previous_document)} if parent else None}


def _head(head: Any):
    _shape(head, {"kind", "run_id", "receipt_id", "receipt_relative_path", "request_sha256", "previous"},
           "reviewed_head_invalid")
    if (not isinstance(head["run_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", head["run_id"]) is None
            or head["kind"] not in {"baseline", "chapter", "source_resolution"}
            or head["receipt_id"] != f"reviewed-{head['run_id']}"
            or head["receipt_relative_path"] != f"{RECEIPT_DIRECTORY}/{head['run_id']}.json"):
        fail("reviewed_head_invalid")
    _sha(head["request_sha256"])
    previous = head["previous"]
    if head["kind"] == "baseline":
        if previous is not None:
            fail("reviewed_head_parent_invalid")
    else:
        _shape(previous, {"receipt_relative_path", "document_sha256"}, "reviewed_head_parent_invalid")
        _sha(previous["document_sha256"])
        relative = _relative(previous["receipt_relative_path"])
        if (not relative.startswith(RECEIPT_DIRECTORY + "/") or not relative.endswith(".json")
                or len(relative.split("/")) != 3 or relative == head["receipt_relative_path"]):
            fail("reviewed_head_parent_invalid")


def create_baseline(*, root: Path, book_id: str, chapter: int, state: dict,
                    confirmed_baseline: dict, old_identity_sha256: str, run_id: str,
                    static_references: list[dict] | None = None) -> bytes:
    sources = capture_sources(root)
    references = copy.deepcopy(static_references or [])
    _references(references, root=root)
    expected = baseline_request(book_id=book_id, chapter=chapter, state=state, sources=sources,
        old_identity_sha256=old_identity_sha256, static_references=references)
    if expected != confirmed_baseline:
        fail("reviewed_baseline_confirmation_stale")
    value = {"format": FORMAT, "revision": 1, "initial_baseline": {
        "state": copy.deepcopy(state), "confirmation": expected,
        "source_revision": sources, "static_references": references},
        "source_revision": sources, "current_state": copy.deepcopy(state), "committed_through": chapter,
        "source_decisions": [], "last_transition": None, "static_references": references,
        "head": publication_head(run_id=run_id, kind="baseline", request_sha256=digest(expected), book_id=book_id)}
    return _render(HEADER.encode("utf-8"), value, book_id=book_id)


def decode_document(document: bytes, *, book_id: str) -> dict:
    parsed = parse_managed_block(document)
    if parsed is None:
        fail("reviewed_state_missing")
    projection = parsed.projection
    if (projection["book_id"] != book_id or projection["scope"] != "context"
            or projection["parser_version"] != FORMAT or projection["owned_fields"] != ["reviewed_state"]
            or set(projection["values"]) != {"reviewed_state"} or projection["tombstones"]):
        fail("reviewed_projection_invalid")
    value = copy.deepcopy(projection["values"]["reviewed_state"])
    _shape(value, _FIELDS, "reviewed_document_shape_invalid")
    if value["format"] != FORMAT:
        fail("reviewed_format_invalid")
    _positive(value["revision"])
    _positive(value["committed_through"])
    _state(value["current_state"])
    _sources(value["source_revision"])
    _references(value["static_references"])
    _head(value["head"])
    baseline = value["initial_baseline"]
    _shape(baseline, {"state", "confirmation", "source_revision", "static_references"}, "reviewed_baseline_invalid")
    confirmation = baseline["confirmation"]
    if not isinstance(confirmation, dict):
        fail("reviewed_baseline_invalid")
    expected = baseline_request(book_id=book_id, chapter=confirmation.get("chapter"), state=baseline["state"],
        sources=baseline["source_revision"], old_identity_sha256=confirmation.get("old_identity_sha256"),
        static_references=baseline["static_references"])
    if confirmation != expected or value["committed_through"] < confirmation["chapter"]:
        fail("reviewed_baseline_invalid")
    head = value["head"]
    previous = head["previous"]
    if (projection["run_id"] != head["run_id"] or projection["chapter"] != value["committed_through"]
            or projection["base_source_digest"] != value["source_revision"]["sha256"]
            or projection["base_revision"] != (previous["document_sha256"] if previous else "baseline")):
        fail("reviewed_projection_binding_invalid")
    if not isinstance(value["source_decisions"], list):
        fail("reviewed_source_decisions_invalid")
    if head["kind"] == "baseline":
        if (value["revision"] != 1 or value["source_decisions"] or value["last_transition"] is not None
                or value["current_state"] != baseline["state"] or value["source_revision"] != baseline["source_revision"]
                or value["static_references"] != baseline["static_references"]
                or value["committed_through"] != confirmation["chapter"] or head["request_sha256"] != digest(expected)):
            fail("reviewed_baseline_state_mismatch")
    elif value["revision"] < 2 or not isinstance(value["last_transition"], dict):
        fail("reviewed_transition_invalid")
    return value


def verify_current_sources(*, root: Path, value: dict, next_chapter: int):
    if type(next_chapter) is not int or next_chapter != value["committed_through"] + 1:
        fail("reviewed_next_chapter_mismatch")
    if capture_sources(root) != value["source_revision"]:
        fail("reviewed_manual_source_changed")
    _references(value["static_references"], root=root)


def prepare_chapter_document(*, document: bytes, book_id: str, preparation: dict, run_id: str) -> bytes:
    from core.engine.chapter_commit_prepare import PREPARATION_VERSION, normalize_commit_prose

    value = decode_document(document, book_id=book_id)
    p = preparation
    if (p.get("preparation_version") != PREPARATION_VERSION or p.get("stage") != "facts_prepared"
            or p.get("publication_authorized") is not False
            or p.get("preparation_sha256") != digest({k: v for k, v in p.items() if k != "preparation_sha256"})):
        fail("reviewed_preparation_invalid")
    binding = p["binding"]
    if (binding["book_id"] != book_id or binding["chapter"] != value["committed_through"] + 1
            or binding["committed_through"] != value["committed_through"]
            or binding["reviewed_source_sha256"] != digest(document)
            or binding["manual_sources_sha256"] != value["source_revision"]["sha256"]
            or binding["acceptance_kind"] not in {"author","policy"}
            or p["request_sha256"] != digest({"version": PREPARATION_VERSION, "binding": binding})):
        fail("reviewed_preparation_stale")
    policy=binding['acceptance_kind']=='policy'
    if policy:
        from core.engine.reviewed_policy_review import verify_policy_preparation
        verify_policy_preparation(p)
    elif 'policy_review' in p or 'policy_acceptance' in binding:
        fail('reviewed_acceptance_source_invalid')
    support = normalize_fact_support(p.get("support"))
    if (support is not None and binding.get("support_sha256") != digest(support)
            or support is None and "support_sha256" in binding):
        fail("reviewed_support_binding_mismatch")
    fact_plan = prepare_supported_fact_changes(facts=p["facts"], chapter_text=p["final_prose"], base_state=value["current_state"],
        support=support, book_id=book_id, review_scope=binding["review_id"])
    if (p["final_prose"] != normalize_commit_prose(p["final_prose"])
            or p["fact_preparation"] != fact_plan or binding["state_before_sha256"] != digest(value["current_state"])
            or binding["final_prose_sha256"] != digest(p["final_prose"].encode("utf-8"))
            or binding["facts_sha256"] != digest(p["facts"])):
        fail("reviewed_preparation_result_mismatch")
    _sha(p["idempotency_scope_sha256"])
    value["revision"] += 1
    value["committed_through"] = binding["chapter"]
    value["current_state"] = fact_plan["state_after"]
    # Retain portable decision evidence here. The full preparation/read set is
    # a separate immutable transaction artifact, never the cold-read authority.
    value["last_transition"] = {"kind": "chapter", "facts": copy.deepcopy(p["facts"]),
        "facts_sha256": binding["facts_sha256"], "chapter_sha256": binding["final_prose_sha256"],
        "before_sha256": binding["state_before_sha256"], "after_sha256": digest(value["current_state"]),
        "draft_id": binding["draft_id"], "draft_revision": binding["draft_revision"],
        "review_id": binding["review_id"], "review_revision": binding["review_revision"],
        "acceptance_kind": binding["acceptance_kind"],
        "idempotency_scope_sha256": p["idempotency_scope_sha256"]}
    if policy:
        value['last_transition'].update(policy_review=copy.deepcopy(p['policy_review']),
            policy_acceptance=copy.deepcopy(binding['policy_acceptance']))
    else:value['last_transition']['confirmation_sha256']=binding['confirmation_sha256']
    if support is not None:
        value["last_transition"].update(support=support, support_sha256=digest(support))
    value["head"] = publication_head(run_id=run_id, kind="chapter", request_sha256=p["request_sha256"],
                                     previous_document=document, book_id=book_id)
    result = _render(document, value, book_id=book_id)
    verify_transition(document=result, previous_document=document, book_id=book_id,
                      chapter_prose=p["final_prose"].encode("utf-8"))
    return result


def describe_source_conflict(*, root: Path, document: bytes, book_id: str) -> dict:
    value = decode_document(document, book_id=book_id)
    return source_conflict(document=document, value=value, book_id=book_id, current_sources=capture_sources(root))


def source_conflict(*, document: bytes, value: dict, book_id: str, current_sources: dict) -> dict:
    before = value["source_revision"]
    _sources(current_sources)
    if before == current_sources:
        fail("reviewed_no_source_conflict")
    differences = [{"path": p, "before": before["entries"].get(p), "after": current_sources["entries"].get(p)}
        for p in sorted(set(before["entries"]) | set(current_sources["entries"]))
        if before["entries"].get(p) != current_sources["entries"].get(p)]
    return {"book_id": book_id, "document_sha256": digest(document), "revision": value["revision"],
            "chapter": value["committed_through"], "state_sha256": digest(value["current_state"]),
            "from_source": before["sha256"], "to_source": current_sources["sha256"], "differences": differences}


def prepare_source_resolution(*, root: Path, document: bytes, book_id: str, decision: dict, run_id: str) -> bytes:
    value = decode_document(document, book_id=book_id)
    current = capture_sources(root)
    expected = source_conflict(document=document, value=value, book_id=book_id, current_sources=current)
    _decision(decision, expected)
    _references(value["static_references"], root=root)
    value["revision"] += 1
    value["source_revision"] = current
    value["source_decisions"].append(copy.deepcopy(decision))
    value["last_transition"] = {"kind": "source_resolution", "decision": copy.deepcopy(decision)}
    value["head"] = publication_head(run_id=run_id, kind="source_resolution", request_sha256=digest(decision),
                                     previous_document=document, book_id=book_id)
    result = _render(document, value, book_id=book_id)
    verify_transition(document=result, previous_document=document, book_id=book_id, chapter_prose=None)
    return result


def _decision(decision: Any, expected: dict):
    _shape(decision, {"conflict", "action", "review_source", "reason"}, "reviewed_source_decision_invalid")
    if (decision["conflict"] != expected or decision["action"] != "keep_reviewed_current"
            or decision["review_source"] != "author" or not isinstance(decision["reason"], str)
            or not decision["reason"].strip()):
        fail("reviewed_source_decision_stale")


def verify_transition(*, document: bytes, previous_document: bytes, book_id: str, chapter_prose: bytes | None):
    current = decode_document(document, book_id=book_id)
    previous = decode_document(previous_document, book_id=book_id)
    if (current["revision"] != previous["revision"] + 1
            or current["initial_baseline"] != previous["initial_baseline"]
            or current["static_references"] != previous["static_references"]
            or current["head"]["previous"] != {"receipt_relative_path": previous["head"]["receipt_relative_path"],
                                                "document_sha256": digest(previous_document)}):
        fail("reviewed_history_discontinuity")
    change = current["last_transition"]
    if current["head"]["kind"] == "source_resolution":
        _shape(change, {"kind", "decision"}, "reviewed_transition_invalid")
        expected = source_conflict(document=previous_document, value=previous, book_id=book_id,
                                   current_sources=current["source_revision"])
        _decision(change["decision"], expected)
        if (change["kind"] != "source_resolution" or current["current_state"] != previous["current_state"]
                or current["committed_through"] != previous["committed_through"]
                or current["source_decisions"] != previous["source_decisions"] + [change["decision"]]
                or current["head"]["request_sha256"] != digest(change["decision"])):
            fail("reviewed_source_resolution_mismatch")
    elif current["head"]["kind"] == "chapter":
        from core.engine.chapter_commit_prepare import normalize_commit_prose

        support = normalize_fact_support(change.get("support"))
        policy=change.get('acceptance_kind')=='policy'
        approval_fields={'policy_review','policy_acceptance'} if policy else {'confirmation_sha256'}
        _shape(change, {"kind", "facts", "facts_sha256", "chapter_sha256", "before_sha256", "after_sha256",
            "draft_id", "draft_revision", "review_id", "review_revision",
            "acceptance_kind", "idempotency_scope_sha256"} | approval_fields | ({"support", "support_sha256"} if support is not None else set()), "reviewed_transition_invalid")
        if support is not None and change["support_sha256"] != digest(support):
            fail("reviewed_support_binding_mismatch")
        if (change["kind"] != "chapter" or chapter_prose is None or change["chapter_sha256"] != digest(chapter_prose)
                or current["committed_through"] != previous["committed_through"] + 1
                or current["source_revision"] != previous["source_revision"]
                or current["source_decisions"] != previous["source_decisions"]
                or change["before_sha256"] != digest(previous["current_state"])
                or change["after_sha256"] != digest(current["current_state"])
                or change["facts_sha256"] != digest(change["facts"]) or change["acceptance_kind"] not in {'author','policy'}):
            fail("reviewed_chapter_transition_mismatch")
        if policy:
            from core.engine.reviewed_policy_review import validate_policy_fact_review,policy_acceptance
            review=validate_policy_fact_review(change['policy_review'])
            if (support is not None or change['policy_acceptance']!=policy_acceptance(review)
                    or digest(change['policy_acceptance'])!=digest(policy_acceptance(review))
                    or review['book_id']!=book_id or review['chapter']!=current['committed_through']
                    or normalize_commit_prose(review['source_text']).encode('utf-8')!=chapter_prose
                    or review['facts']!=change['facts']
                    or any(change[k]!=review[v] for k,v in {'review_id':'id','review_revision':'revision',
                        'draft_id':'draft_id','draft_revision':'source_draft_revision'}.items())):
                fail('reviewed_policy_transition_mismatch')
        for key in (("idempotency_scope_sha256",) if policy else ("confirmation_sha256", "idempotency_scope_sha256")):
            _sha(change[key])
        for key in ("draft_revision", "review_revision"):
            _positive(change[key])
        for key in ("draft_id", "review_id"):
            if not isinstance(change[key], str) or not change[key].strip():
                fail("reviewed_transition_invalid")
        if chapter_prose.decode("utf-8") != normalize_commit_prose(chapter_prose.decode("utf-8")):
            fail("reviewed_prose_not_normalized")
        prepared = prepare_supported_fact_changes(facts=change["facts"], chapter_text=chapter_prose.decode("utf-8"),
            base_state=previous["current_state"], support=support, book_id=book_id, review_scope=change["review_id"])
        if prepared["state_after"] != current["current_state"]:
            fail("reviewed_chapter_replay_mismatch")
    else:
        fail("reviewed_history_discontinuity")


def _render(document: bytes, value: dict, *, book_id: str) -> bytes:
    head = value["head"]
    projection = build_managed_projection(scope="context", book_id=book_id, run_id=head["run_id"],
        chapter=value["committed_through"], parser_version=FORMAT,
        base_revision=head["previous"]["document_sha256"] if head["previous"] else "baseline",
        base_source_digest=value["source_revision"]["sha256"], owned_fields=["reviewed_state"],
        values={"reviewed_state": value})
    result = write_managed_block(document, projection)
    decode_document(result, book_id=book_id)
    return result
