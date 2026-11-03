"""Pure identity decisions for reviewed facts; no authority or persistence.

Uses the existing fact-context entity-directory shape. Callers must bind the
complete directory and author decisions to the real book/review/read set before
publication. A resolved name is never evidence of a known quantity or position.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any


IDENTITY_DECISION_VERSION = "chapter_fact_identity_v1"
DECLARABLE_KINDS = frozenset({"character", "location", "item"})
_LOCAL_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ChapterFactIdentityError(ValueError):
    def __init__(self, code: str, message: str, *, candidates=()):
        self.code = code
        self.candidates = copy.deepcopy(list(candidates))
        super().__init__(f"{code}: {message}")


def _fail(code, message, *, candidates=()):
    raise ChapterFactIdentityError(code, message, candidates=candidates)


def _text(value: Any, label: str, maximum: int = 300) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > maximum or any(unicodedata.category(c).startswith("C") for c in value)):
        _fail("chapter_fact_identity_invalid", f"{label} must be bounded nonempty text")
    return value


def _name(value: str) -> str:
    # Match the fact-context directory exactly; do not guess nicknames from
    # surnames, remove punctuation or accept a fuzzy match as an identity.
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def normalize_entity_directory(directory: list[dict]) -> list[dict]:
    """Validate a complete derived directory, retaining ambiguous aliases."""
    if not isinstance(directory, list):
        _fail("chapter_fact_identity_invalid", "entity directory must be an array")
    result, seen = [], set()
    for entry in directory:
        if not isinstance(entry, dict) or set(entry) != {"entity_id", "kind", "name", "aliases"}:
            _fail("chapter_fact_identity_invalid", "entity directory entry shape differs")
        kind = _text(entry["kind"], "entity kind", 50)
        entity_id = _text(entry["entity_id"], "entity id")
        name = _text(entry["name"], "entity name")
        if not isinstance(entry["aliases"], list):
            _fail("chapter_fact_identity_invalid", "entity aliases must be an array")
        aliases = {_text(alias, "alias") for alias in entry["aliases"]}
        key = (kind, entity_id)
        if key in seen:
            _fail("chapter_fact_identity_invalid", "duplicate typed entity id")
        seen.add(key)
        result.append({"entity_id":entity_id, "kind":kind, "name":name,
            "aliases":sorted(aliases, key=lambda alias: (_name(alias), alias))})
    return sorted(result, key=lambda entry:(entry["kind"], entry["entity_id"]))


def resolve_entity_reference(directory: list[dict], *, kind: str, reference: str,
                             reference_type: str = "name", selected_entity_id: str | None = None) -> dict:
    """Resolve a typed id/exact alias; ambiguity requires an explicit selection.

    selected_entity_id can only choose an actual match. Binding an unknown
    nickname to an existing character is a separate author decision below.
    """
    entries = normalize_entity_directory(directory)
    _text(kind, "entity kind", 50)
    _text(reference, "entity reference")
    if reference_type not in {"id", "name"}:
        _fail("chapter_fact_identity_invalid", "reference type must be id or name")
    candidates = [entry for entry in entries if entry["kind"] == kind and (
        entry["entity_id"] == reference if reference_type == "id" else
        _name(reference) in {_name(entry["name"]), *(_name(alias) for alias in entry["aliases"])})]
    if not candidates:
        _fail("chapter_fact_identity_unknown", "reference requires an explicit identity decision")
    if selected_entity_id is not None:
        selected = [entry for entry in candidates if entry["entity_id"] == selected_entity_id]
        if not selected:
            _fail("chapter_fact_identity_selection_invalid", "selected identity is not a matching candidate", candidates=candidates)
        return copy.deepcopy(selected[0])
    if len(candidates) != 1:
        _fail("chapter_fact_identity_ambiguous", "reference matches multiple identities", candidates=candidates)
    return copy.deepcopy(candidates[0])


def prepare_identity_decisions(*, directory: list[dict], book_id: str, review_scope: str,
                               decisions: list[dict]) -> dict:
    """Prepare explicit author declare/reuse decisions without changing state.

    Stable ids are generated from book + persisted review scope + local key,
    never from a name or array position. Each local reference can be used by
    other facts in the same review. No initial quantity/location is inferred.
    """
    before = normalize_entity_directory(directory)
    _text(book_id, "book id")
    _text(review_scope, "persisted review scope")
    if not isinstance(decisions, list) or len(decisions) > 100:
        _fail("chapter_fact_identity_invalid", "identity decisions must be an array of at most 100 items")
    working, declarations, resolutions, checked = copy.deepcopy(before), [], {}, []
    common = {"local_key", "kind", "action", "review_source", "reason"}
    ordered = []
    for decision in decisions:
        if not isinstance(decision, dict):
            _fail("chapter_fact_identity_invalid", "identity decision must be an object")
        action = _text(decision.get("action"), "identity action", 20)
        extra = {"name", "aliases"} if action == "declare" else {"entity_id", "reference"}
        if action not in {"declare", "reuse"} or set(decision) != common | extra:
            _fail("chapter_fact_identity_invalid", "identity decision fields differ")
        key = _text(decision["local_key"], "local key", 64)
        if not _LOCAL_KEY.fullmatch(key) or key in resolutions:
            _fail("chapter_fact_identity_invalid", "local keys must be canonical and unique")
        resolutions[key] = None
        kind = _text(decision["kind"], "entity kind", 50)
        if kind not in DECLARABLE_KINDS or decision["review_source"] != "author":
            _fail("chapter_fact_identity_invalid", "identity decisions require an author and a supported kind")
        _text(decision["reason"], "author reason", 1000)
        ordered.append(copy.deepcopy(decision))
    # Declaration order cannot decide which of two equal names wins.
    for decision in sorted(ordered, key=lambda item: (item["action"] != "declare", item["local_key"])):
        key, kind = decision["local_key"], decision["kind"]
        if decision["action"] == "declare":
            entity_id = {"character":"char_", "location":"location_", "item":"item_"}[kind] + _digest({
                "version":IDENTITY_DECISION_VERSION, "book_id":book_id, "review_scope":review_scope,
                "local_key":key, "kind":kind})[:32]
            entry = normalize_entity_directory([{"entity_id":entity_id, "kind":kind,
                "name":decision["name"], "aliases":decision["aliases"]}])[0]
            names = {_name(entry["name"]), *(_name(alias) for alias in entry["aliases"])}
            collisions = [old for old in working if old["kind"] == kind and (
                old["entity_id"] == entity_id or names & {_name(old["name"]), *(_name(a) for a in old["aliases"])})]
            if collisions:
                _fail("chapter_fact_identity_reuse_required", "new identity overlaps an existing name/id; explicitly reuse or disambiguate it", candidates=collisions)
            working.append(entry)
            declarations.append(entry)
        else:
            _text(decision["reference"], "author reference")
            # The author can disambiguate a nickname which was absent from the
            # global alias directory. This binding stays local to this review.
            entry = resolve_entity_reference(working, kind=kind, reference=decision["entity_id"], reference_type="id")
            matches = [old for old in working if old["kind"] == kind and _name(decision["reference"]) in
                {_name(old["name"]), *(_name(alias) for alias in old["aliases"])}]
            if matches and entry["entity_id"] not in {old["entity_id"] for old in matches}:
                _fail("chapter_fact_identity_selection_invalid", "known name/alias belongs to another identity", candidates=matches)
        resolutions[key] = {"kind":kind, "entity_id":entry["entity_id"]}
        checked.append(decision)
    result = {"version":IDENTITY_DECISION_VERSION, "publication_authorized":False,
        "book_id":book_id, "review_scope":review_scope, "directory_before_sha256":_digest(before),
        "decisions":checked, "declarations":declarations, "local_references":resolutions,
        "directory_after":normalize_entity_directory(working)}
    result["plan_sha256"] = _digest(result)
    return result
