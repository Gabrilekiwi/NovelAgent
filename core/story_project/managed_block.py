from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable

from core.schema import SchemaValidationError, validate_schema


MANAGED_BLOCK_SCHEMA_VERSION = "1.0"
MANAGED_BLOCK_START = "<!-- NovelAgent:semantic-state version=1 -->"
MANAGED_BLOCK_END = "<!-- /NovelAgent:semantic-state -->"
_START_RE = re.compile(r"(?m)^(?:\ufeff)?[ \t]*<!-- NovelAgent:semantic-state version=1 -->[ \t]*(?:\r?\n|$)")
_END_RE = re.compile(r"(?m)^[ \t]*<!-- /NovelAgent:semantic-state -->[ \t]*(?:\r?\n|$)")
_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
_TOMBSTONE_RE = re.compile(
    r"^[ \t]*<!-- NovelAgent:tombstone field=([^\s]+)(?: superseded_by=([^\s]+))? -->[ \t]*\r?$",
    re.MULTILINE,
)
_MISSING = object()


class ManagedBlockError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ParsedManagedBlock:
    projection: dict[str, Any]
    start_char: int
    end_char: int
    raw: str


@dataclass(frozen=True)
class ManagedMergeResult:
    projection: dict[str, Any] | None
    conflicts: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return not self.conflicts and self.projection is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "projection": self.projection,
            "conflicts": [dict(item) for item in self.conflicts],
        }


def build_managed_projection(
    *,
    scope: str,
    book_id: str,
    run_id: str,
    chapter: int,
    parser_version: str,
    base_revision: str,
    base_source_digest: str,
    owned_fields: Iterable[str],
    values: dict[str, Any],
    tombstones: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    projection = {
        "schema_version": MANAGED_BLOCK_SCHEMA_VERSION,
        "scope": scope,
        "book_id": book_id,
        "run_id": run_id,
        "chapter": chapter,
        "parser_version": parser_version,
        "base_revision": base_revision,
        "base_source_digest": base_source_digest,
        "owned_fields": sorted(dict.fromkeys(str(item) for item in owned_fields)),
        "values": dict(values),
        "tombstones": _normalize_tombstones(tombstones),
        "payload_sha256": "",
    }
    projection["payload_sha256"] = managed_payload_sha256(projection)
    return validate_managed_projection(projection)


def validate_managed_projection(value: Any, *, verify_hash: bool = True) -> dict[str, Any]:
    try:
        projection = validate_schema(value, "story_project_managed_projection.schema.json")
    except SchemaValidationError as exc:
        raise ManagedBlockError("managed_projection_schema_invalid", str(exc)) from exc
    if len(set(projection["owned_fields"])) != len(projection["owned_fields"]):
        raise ManagedBlockError("managed_projection_owned_fields_duplicate", "owned_fields must be unique")
    tombstone_fields = [item["field_path"] for item in projection["tombstones"]]
    if len(set(tombstone_fields)) != len(tombstone_fields):
        raise ManagedBlockError("managed_projection_tombstone_duplicate", "tombstone field paths must be unique")
    if set(tombstone_fields) & set(projection["values"]):
        raise ManagedBlockError(
            "managed_projection_value_tombstone_overlap",
            "a field cannot have both a value and a tombstone",
        )
    for field_name in ("base_source_digest", "payload_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", projection[field_name]):
            raise ManagedBlockError("managed_projection_digest_invalid", f"{field_name} must be lowercase SHA-256")
    for tombstone in projection["tombstones"]:
        superseded_by = tombstone.get("superseded_by")
        if isinstance(superseded_by, str) and not superseded_by.strip():
            raise ManagedBlockError(
                "managed_tombstone_invalid",
                "superseded_by must be null or a non-empty stable field path",
            )
    if verify_hash and projection["payload_sha256"] != managed_payload_sha256(projection):
        raise ManagedBlockError("managed_projection_hash_mismatch", "payload_sha256 does not match canonical payload")
    return projection


def managed_payload_sha256(projection: dict[str, Any]) -> str:
    payload = dict(projection)
    payload.pop("payload_sha256", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_managed_block(document: bytes | str) -> ParsedManagedBlock | None:
    text = _decode_document(document)
    starts = list(_START_RE.finditer(text))
    ends = list(_END_RE.finditer(text))
    if not starts and not ends:
        if MANAGED_BLOCK_START in text or MANAGED_BLOCK_END in text:
            raise ManagedBlockError(
                "malformed_managed_block",
                "managed block markers must occupy exclusive lines",
            )
        return None
    if len(starts) != 1 or len(ends) != 1:
        code = "duplicate_managed_block" if len(starts) > 1 or len(ends) > 1 else "malformed_managed_block"
        raise ManagedBlockError(code, "document must contain exactly one start marker and one end marker")
    start, end = starts[0], ends[0]
    if end.start() < start.end():
        raise ManagedBlockError("nested_or_reversed_managed_block", "managed block markers are nested or reversed")
    start_char = start.start() + (1 if text[start.start() :].startswith("\ufeff") else 0)
    raw = text[start_char : end.end()]
    fences = list(_JSON_FENCE_RE.finditer(raw))
    if len(fences) != 1:
        raise ManagedBlockError("managed_projection_json_missing", "managed block must contain exactly one JSON fence")
    try:
        projection = json.loads(fences[0].group(1))
    except json.JSONDecodeError as exc:
        raise ManagedBlockError("managed_projection_json_invalid", str(exc)) from exc
    validated = validate_managed_projection(projection)
    return ParsedManagedBlock(
        projection=validated,
        start_char=start_char,
        end_char=end.end(),
        raw=raw,
    )


def render_managed_block(projection: dict[str, Any], *, newline: str = "\n") -> str:
    validated = validate_managed_projection(projection)
    if newline not in {"\n", "\r\n"}:
        raise ManagedBlockError("managed_block_newline_invalid", "newline must be LF or CRLF")
    values = validated["values"]
    tombstones = validated["tombstones"]
    lines = [
        MANAGED_BLOCK_START,
        "## NovelAgent Managed Projection",
        "",
        f"- scope: {validated['scope']}",
        f"- run_id: {validated['run_id']}",
        f"- chapter: {validated['chapter']}",
        "",
        "### Managed Fields",
    ]
    lines.extend(
        f"- `{field_path}`: {_compact_json(values[field_path])}" for field_path in sorted(values)
    )
    if not values:
        lines.append("- (none)")
    if tombstones:
        lines.extend(["", "### Tombstones"])
        for item in tombstones:
            suffix = f" -> {item['superseded_by']}" if item.get("superseded_by") else ""
            lines.append(f"- `{item['field_path']}`{suffix}: {item['reason']}")
    lines.extend(
        [
            "",
            "```json",
            json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2),
            "```",
            MANAGED_BLOCK_END,
        ]
    )
    rendered = "\n".join(lines)
    return rendered if newline == "\n" else rendered.replace("\n", "\r\n")


def write_managed_block(document: bytes | str, projection: dict[str, Any]) -> bytes:
    text = _decode_document(document)
    parsed = parse_managed_block(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    rendered = render_managed_block(projection, newline=newline)
    if parsed is not None:
        trailing_newline = newline if parsed.raw.endswith(("\n", "\r")) else ""
        updated = text[: parsed.start_char] + rendered + trailing_newline + text[parsed.end_char :]
    else:
        separator = "" if not text else (newline if text.endswith(("\n", "\r")) else newline + newline)
        updated = text + separator + rendered + newline
    return updated.encode("utf-8")


def compute_base_source_digest(
    document: bytes | str,
    *,
    external_sources: Iterable[tuple[str, str]] = (),
) -> str:
    text = _decode_document(document)
    parsed = parse_managed_block(text)
    manual = text if parsed is None else text[: parsed.start_char] + text[parsed.end_char :]
    payload = {
        "manual_sha256": hashlib.sha256(manual.encode("utf-8")).hexdigest(),
        "external_sources": [
            {"source": str(source), "sha256": str(digest)}
            for source, digest in sorted(external_sources)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_manual_tombstones(document: bytes | str) -> list[dict[str, Any]]:
    text = _decode_document(document)
    parsed = parse_managed_block(text)
    manual = text if parsed is None else text[: parsed.start_char] + text[parsed.end_char :]
    return _normalize_tombstones(
        {
            "field_path": match.group(1),
            "reason": "manual_tombstone",
            "superseded_by": match.group(2),
        }
        for match in _TOMBSTONE_RE.finditer(manual)
    )


def three_way_merge_managed(
    *,
    base: dict[str, Any] | None,
    current: dict[str, Any] | None,
    proposed: dict[str, Any],
    manual_values: dict[str, Any] | None = None,
    manual_tombstones: Iterable[dict[str, Any]] = (),
    ambiguous_fields: Iterable[str] = (),
) -> ManagedMergeResult:
    proposed_valid = validate_managed_projection(proposed)
    base_valid = validate_managed_projection(base) if base is not None else None
    current_valid = validate_managed_projection(current) if current is not None else None
    manual_value_map = dict(manual_values or {})
    manual_tombstone_map = _tombstone_map(_normalize_tombstones(manual_tombstones))
    manual_resolution_fields = set(manual_value_map) | set(manual_tombstone_map)
    conflicts: list[dict[str, Any]] = []
    for other_name, other in (("base", base_valid), ("current", current_valid)):
        if other is None:
            continue
        for key in ("scope", "book_id"):
            if other[key] != proposed_valid[key]:
                conflicts.append(_merge_conflict(key, "managed_projection_identity_mismatch", other_name))
    owned = set(proposed_valid["owned_fields"])
    illegal = sorted(set(proposed_valid["values"]) - owned)
    for field_path in illegal:
        conflicts.append(_merge_conflict(field_path, "managed_field_not_owned", "proposed"))
    for field_path in sorted(set(ambiguous_fields)):
        conflicts.append(_merge_conflict(field_path, "manual_change_ambiguous", "manual"))
    if conflicts:
        return ManagedMergeResult(None, tuple(conflicts))

    base_values = dict((base_valid or {}).get("values") or {})
    current_values = dict((current_valid or base_valid or {}).get("values") or {})
    proposed_values = dict(proposed_valid["values"])
    merged_values = dict(current_values)
    current_tombstones = _tombstone_map((current_valid or base_valid or {}).get("tombstones") or [])
    proposed_tombstones = _tombstone_map(proposed_valid["tombstones"])

    for field_path in sorted(owned):
        if field_path in current_tombstones and field_path in proposed_values:
            continue
        base_value = base_values.get(field_path, _MISSING)
        current_value = current_values.get(field_path, _MISSING)
        proposed_value = proposed_values.get(field_path, _MISSING)
        if proposed_value is _MISSING:
            continue
        current_changed = current_value != base_value
        proposed_changed = proposed_value != base_value
        if (
            current_changed
            and proposed_changed
            and current_value != proposed_value
            and field_path not in manual_resolution_fields
        ):
            conflicts.append(_merge_conflict(field_path, "concurrent_managed_edit", "current,proposed"))
            continue
        merged_values[field_path] = proposed_value

    merged_tombstones = dict(current_tombstones)
    for field_path, tombstone in proposed_tombstones.items():
        if field_path not in owned:
            conflicts.append(_merge_conflict(field_path, "managed_field_not_owned", "proposed_tombstone"))
            continue
        merged_tombstones[field_path] = tombstone
        merged_values.pop(field_path, None)
    for field_path, tombstone in manual_tombstone_map.items():
        merged_tombstones[field_path] = tombstone
        merged_values.pop(field_path, None)
    for field_path, value in manual_value_map.items():
        merged_values[field_path] = value
        merged_tombstones.pop(field_path, None)

    if conflicts:
        return ManagedMergeResult(None, tuple(conflicts))
    merged = dict(proposed_valid)
    merged["values"] = merged_values
    merged["tombstones"] = [merged_tombstones[key] for key in sorted(merged_tombstones)]
    merged["payload_sha256"] = managed_payload_sha256(merged)
    return ManagedMergeResult(validate_managed_projection(merged), ())


def _decode_document(document: bytes | str) -> str:
    if isinstance(document, str):
        return document
    try:
        return document.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManagedBlockError("managed_block_encoding_invalid", "tracking file must be UTF-8") from exc


def _normalize_tombstones(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for raw in items:
        field_path = str(raw.get("field_path") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not field_path or not reason:
            raise ManagedBlockError("managed_tombstone_invalid", "tombstones require field_path and reason")
        superseded_by = raw.get("superseded_by")
        normalized[field_path] = {
            "field_path": field_path,
            "reason": reason,
            "superseded_by": str(superseded_by).strip() if superseded_by else None,
        }
    return [normalized[key] for key in sorted(normalized)]


def _tombstone_map(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["field_path"]: dict(item) for item in items}


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_conflict(field_path: str, code: str, sources: str) -> dict[str, Any]:
    return {
        "field_path": field_path,
        "code": code,
        "blocking": True,
        "sources": sources.split(","),
    }


__all__ = [
    "MANAGED_BLOCK_END",
    "MANAGED_BLOCK_SCHEMA_VERSION",
    "MANAGED_BLOCK_START",
    "ManagedBlockError",
    "ManagedMergeResult",
    "ParsedManagedBlock",
    "build_managed_projection",
    "compute_base_source_digest",
    "managed_payload_sha256",
    "parse_managed_block",
    "parse_manual_tombstones",
    "render_managed_block",
    "three_way_merge_managed",
    "validate_managed_projection",
    "write_managed_block",
]
