"""D1 source-rule experiment; deliberately not imported by production entry points.

Only capture_sources reads files. All mutations return managed-document bytes for
an isolated harness. This is not a transaction writer or a supported book format.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from core.engine.chapter_fact_prepare import prepare_confirmed_fact_changes
from core.state.authoritative import validate_authoritative_state
from core.story_project.managed_block import (
    build_managed_projection,
    parse_managed_block,
    write_managed_block,
)


FORMAT = "d1-reviewed-state-experiment-2"
RELATIVE_PATH = "追踪/已审核事实.md"
HEADER = "# 已审核事实（D1 隔离试验，禁止用于正式作品）\n"


class SourceRuleError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def capture_sources(root: Path) -> dict[str, Any]:
    """Bind membership plus manual content; never read a source outside the book."""
    root = root.resolve(strict=True)
    entries = {}
    for directory in ("追踪", "设定"):
        source_dir = root / directory
        if source_dir.exists() and not source_dir.resolve().is_relative_to(root):
            raise SourceRuleError("source_outside_book")
        for path in sorted(source_dir.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            if relative == RELATIVE_PATH:
                continue
            if not path.resolve(strict=True).is_relative_to(root):
                raise SourceRuleError("source_outside_book")
            # The managed writer adds a separator on first installation. Strip
            # its block and normalize only final CR/LF so that formatting cannot
            # masquerade as a new manual baseline. Other bytes remain relevant.
            manual = path.read_bytes().decode("utf-8")
            parsed = parse_managed_block(manual)
            if parsed is not None:
                manual = manual[:parsed.start_char] + manual[parsed.end_char:]
            entries[relative] = digest(manual.rstrip("\r\n").encode("utf-8"))
    return {"entries": entries, "sha256": digest(entries)}


def baseline_request(*, book_id: str, chapter: int, state: dict, sources: dict,
                     static_references: list[dict] | None = None) -> dict:
    if not book_id or type(chapter) is not int or chapter < 1:
        raise SourceRuleError("baseline_identity_invalid")
    state = validate_authoritative_state(copy.deepcopy(state))
    return {"book_id": book_id, "chapter": chapter, "state_sha256": digest(state),
            "source_sha256": sources["sha256"], "static_references_sha256": digest(static_references or []),
            "decision": "accept_baseline_as_of_chapter"}


def create_baseline(*, root: Path, book_id: str, chapter: int, state: dict,
                    confirmed_baseline: dict, static_references: list[dict] | None = None) -> bytes:
    sources = capture_sources(root)
    state = validate_authoritative_state(copy.deepcopy(state))
    references = copy.deepcopy(static_references or [])
    _verify_static_references(root, references)
    expected = baseline_request(book_id=book_id, chapter=chapter, state=state, sources=sources,
                                static_references=references)
    if confirmed_baseline != expected:
        raise SourceRuleError("baseline_confirmation_stale_or_missing")
    value = {"format": FORMAT, "initial_baseline": {"state": state, "confirmation": expected},
             "source_revision": sources, "current_state": copy.deepcopy(state),
             "committed_through": chapter, "source_decisions": [], "last_transition": None,
             "static_references": references}
    return _render(HEADER.encode("utf-8"), value, book_id=book_id, previous="baseline")


def _read(document: bytes, *, book_id: str) -> tuple[dict, dict]:
    parsed = parse_managed_block(document)
    if parsed is None:
        raise SourceRuleError("reviewed_state_missing")
    projection = parsed.projection
    if projection["book_id"] != book_id or projection["scope"] != "context":
        raise SourceRuleError("reviewed_state_identity_mismatch")
    if set(projection["values"]) != {"reviewed_state"} or projection["tombstones"]:
        raise SourceRuleError("reviewed_state_shape_invalid")
    value = copy.deepcopy(projection["values"]["reviewed_state"])
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise SourceRuleError("reviewed_state_format_invalid")
    if set(value) != {"format", "initial_baseline", "source_revision", "current_state",
                      "committed_through", "source_decisions", "last_transition", "static_references"}:
        raise SourceRuleError("reviewed_state_shape_invalid")
    source = value["source_revision"]
    if source.get("sha256") != digest(source.get("entries")):
        raise SourceRuleError("reviewed_source_hash_invalid")
    if type(value["committed_through"]) is not int or value["committed_through"] < 1:
        raise SourceRuleError("reviewed_chapter_invalid")
    if projection["chapter"] != value["committed_through"]:
        raise SourceRuleError("reviewed_chapter_invalid")
    validate_authoritative_state(value["current_state"])
    return value, projection


def read_current(*, root: Path, document: bytes, book_id: str, next_chapter: int) -> dict:
    value, _ = _read(document, book_id=book_id)
    if value["source_revision"] != capture_sources(root):
        raise SourceRuleError("manual_source_changed")
    _verify_static_references(root, value["static_references"])
    if type(next_chapter) is not int or next_chapter != value["committed_through"] + 1:
        raise SourceRuleError("next_chapter_mismatch")
    return copy.deepcopy(value)


def describe_source_conflict(*, root: Path, document: bytes, book_id: str) -> dict:
    value, projection = _read(document, book_id=book_id)
    current = capture_sources(root)
    before = value["source_revision"]
    if current == before:
        raise SourceRuleError("no_source_conflict")
    differences = [
        {"path": path, "before": before["entries"].get(path), "after": current["entries"].get(path)}
        for path in sorted(set(before["entries"]) | set(current["entries"]))
        if before["entries"].get(path) != current["entries"].get(path)
    ]
    return {"book_id": book_id, "head_sha256": projection["payload_sha256"],
            "document_sha256": digest(document), "state_sha256": digest(value["current_state"]),
            "chapter": value["committed_through"], "from_source": before["sha256"],
            "to_source": current["sha256"], "differences": differences}


def resolve_source_conflict(*, root: Path, document: bytes, book_id: str, decision: dict) -> bytes:
    expected = describe_source_conflict(root=root, document=document, book_id=book_id)
    if (set(decision) != {"conflict", "action", "reason", "review_source"}
            or decision.get("conflict") != expected
            or decision.get("action") != "keep_reviewed_current"
            or decision.get("review_source") != "author"
            or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()):
        raise SourceRuleError("source_decision_stale_or_invalid")
    value, projection = _read(document, book_id=book_id)
    _verify_static_references(root, value["static_references"])
    current = capture_sources(root)
    if current["sha256"] != expected["to_source"]:
        raise SourceRuleError("manual_source_changed")
    value["source_revision"] = current
    value["source_decisions"].append(copy.deepcopy(decision))
    return _render(document, value, book_id=book_id, previous=projection["payload_sha256"])


def review_request(*, root: Path, document: bytes, book_id: str, chapter: int,
                   chapter_text: str, draft_revision: str, facts: list[dict]) -> dict:
    value = read_current(root=root, document=document, book_id=book_id, next_chapter=chapter)
    if not isinstance(draft_revision, str) or not draft_revision:
        raise SourceRuleError("review_revision_required")
    return {"book_id": book_id, "chapter": chapter, "document_sha256": digest(document),
            "source_sha256": value["source_revision"]["sha256"],
            "draft_revision": draft_revision, "chapter_sha256": digest(chapter_text.encode("utf-8")),
            "facts_sha256": digest(facts)}


def prepare_chapter(*, root: Path, document: bytes, book_id: str, chapter: int,
                    chapter_text: str, draft_revision: str, review_revision: str,
                    facts: list[dict], review_binding: dict) -> dict:
    if not isinstance(review_revision, str) or not review_revision:
        raise SourceRuleError("review_revision_required")
    expected = review_request(root=root, document=document, book_id=book_id, chapter=chapter,
        chapter_text=chapter_text, draft_revision=draft_revision, facts=facts)
    if review_binding != expected:
        raise SourceRuleError("fact_decisions_stale")
    value = read_current(root=root, document=document, book_id=book_id, next_chapter=chapter)
    prepared = prepare_confirmed_fact_changes(
        facts=facts, chapter_text=chapter_text, base_state=value["current_state"])
    return {"book_id": book_id, "chapter": chapter, "document_sha256": digest(document),
            "source_sha256": value["source_revision"]["sha256"],
            "draft_revision": draft_revision, "review_revision": review_revision,
            "facts_sha256": digest(facts), "review_binding": copy.deepcopy(review_binding),
            "prepared": prepared}


def apply_prepared(*, plan: dict, root: Path, document: bytes, book_id: str, chapter: int,
                   chapter_text: str, draft_revision: str, review_revision: str,
                   facts: list[dict], review_binding: dict) -> bytes:
    # Recompute against the current inputs, including review identities, before
    # returning bytes. The eventual transaction must also enforce read-set CAS.
    current = prepare_chapter(root=root, document=document, book_id=book_id, chapter=chapter,
        chapter_text=chapter_text, draft_revision=draft_revision,
        review_revision=review_revision, facts=facts, review_binding=review_binding)
    if current != plan:
        raise SourceRuleError("chapter_plan_stale")
    value, projection = _read(document, book_id=book_id)
    value["current_state"] = copy.deepcopy(current["prepared"]["state_after"])
    value["committed_through"] = chapter
    value["last_transition"] = {key: copy.deepcopy(item) for key, item in current.items() if key != "prepared"}
    value["last_transition"].update({
        "chapter_sha256": current["prepared"]["chapter_sha256"],
        "before_sha256": current["prepared"]["base_state_sha256"],
        "after_sha256": digest(value["current_state"]), "facts": copy.deepcopy(facts),
    })
    return _render(document, value, book_id=book_id, previous=projection["payload_sha256"])


def _render(document: bytes, value: dict, *, book_id: str, previous: str) -> bytes:
    projection = build_managed_projection(
        scope="context", book_id=book_id, run_id="d1-source-experiment",
        chapter=value["committed_through"], parser_version=FORMAT, base_revision=previous,
        base_source_digest=value["source_revision"]["sha256"],
        owned_fields=["reviewed_state"], values={"reviewed_state": value})
    return write_managed_block(document, projection)


def load_runtime_view(context: dict) -> dict:
    """Explicit source adapter for the real candidate/context APIs in D1 only."""
    from core.story_project.reviewed_state_context import build_reviewed_state_context
    root = Path(context["story_project_root"])
    document = (root / RELATIVE_PATH).read_bytes()
    value = read_current(root=root, document=document, book_id=context["project_identity"]["book_id"],
                         next_chapter=context["chapter_index"])
    return build_reviewed_state_context(context, authoritative_state=value["current_state"],
        source_relative_path=RELATIVE_PATH, source_sha256=digest(document),
        manual_sources_sha256=value["source_revision"]["sha256"],
        committed_through=value["committed_through"], static_references=value["static_references"])


def _verify_static_references(root: Path, references: list[dict]) -> None:
    if not isinstance(references, list):
        raise SourceRuleError("static_reference_invalid")
    for ref in references:
        if not isinstance(ref, dict) or set(ref) != {"relative_path", "sha256", "text"}:
            raise SourceRuleError("static_reference_invalid")
        relative = ref["relative_path"]
        if (not isinstance(relative, str) or not relative.startswith(("追踪/", "设定/"))
                or "\\" in relative or ".." in Path(relative).parts or ":" in relative):
            raise SourceRuleError("static_reference_invalid")
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root.resolve()):
            raise SourceRuleError("static_reference_invalid")
        raw = path.read_bytes()
        if (digest(raw) != ref["sha256"] or not isinstance(ref["text"], str)
                or not ref["text"].strip() or ref["text"] not in raw.decode("utf-8")):
            raise SourceRuleError("static_reference_changed")
