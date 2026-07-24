from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Sequence


STRUCTURED_CONTEXT_SCHEMA_VERSION = "1.0"
_OMISSION_MARKER = "[…完整条目已省略…]"


class StructuredContextError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class StructuredEntry:
    item_id: str
    text: str
    ordinal: int
    start_char: int | None = None
    end_char: int | None = None


@dataclass(frozen=True)
class TextSelection:
    text: str
    source_sha256: str
    original_chars: int
    selected_items: tuple[dict[str, Any], ...]
    omitted_count: int
    policy: str

    @property
    def ranges(self) -> list[tuple[int, int]]:
        return [
            (int(item["start_char"]), int(item["end_char"]))
            for item in self.selected_items
            if item.get("start_char") is not None and item.get("end_char") is not None
        ]

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": STRUCTURED_CONTEXT_SCHEMA_VERSION,
            "policy": self.policy,
            "source_sha256": self.source_sha256,
            "original_chars": self.original_chars,
            "selected_items": [dict(item) for item in self.selected_items],
            "omitted_count": self.omitted_count,
        }


@dataclass(frozen=True)
class JsonSelection:
    items: tuple[Any, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class _Section:
    name: str
    text: str
    ordinal: int
    start_char: int
    end_char: int


@dataclass(frozen=True)
class _CompactedSection:
    name: str
    text: str
    ordinal: int
    selected_item_ids: tuple[str, ...]
    source_text: str


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def select_text_blocks(
    text: str,
    *,
    max_chars: int,
    query: str = "",
    required: str | Iterable[int] = "edges",
    prefer_recent: bool = True,
    policy: str = "paragraph_relevance_v1",
) -> TextSelection:
    """Select whole paragraphs (or whole lines when a document has no blank-line breaks).

    Character ranges always identify exact structural blocks in ``text``.  If a
    required block cannot fit, selection fails instead of returning a fragment.
    """
    _validate_limit(max_chars)
    digest = sha256_text(text)
    if len(text) <= max_chars:
        selected = () if not text else (_selection_item("document", text, 0, len(text)),)
        return TextSelection(text, digest, len(text), selected, 0, policy)

    entries = _paragraph_entries(text)
    if not entries:
        return TextSelection("", digest, len(text), (), 1 if text else 0, policy)
    required_indexes = _required_indexes(required, len(entries))

    def render(indexes: set[int]) -> str:
        ordered = sorted(indexes)
        parts: list[str] = []
        previous: int | None = None
        for index in ordered:
            if previous is not None and index != previous + 1:
                parts.append(_OMISSION_MARKER)
            parts.append(entries[index].text)
            previous = index
        return "\n\n".join(parts)

    selected_indexes = _choose_indexes(
        entries,
        required_indexes=required_indexes,
        max_chars=max_chars,
        query=query,
        prefer_recent=prefer_recent,
        renderer=render,
        failure_code="required_structured_entry_exceeds_budget",
    )
    rendered = render(selected_indexes)
    selected_items = tuple(
        _selection_item(
            entries[index].item_id,
            entries[index].text,
            entries[index].start_char,
            entries[index].end_char,
        )
        for index in sorted(selected_indexes)
    )
    omitted_count = len(entries) - len(selected_indexes)
    if omitted_count == 0 and rendered != text:
        omitted_count = 1
    return TextSelection(
        rendered,
        digest,
        len(text),
        selected_items,
        omitted_count,
        policy,
    )


def select_json_items(
    values: Sequence[Any],
    *,
    max_chars: int,
    query: str = "",
    required_indexes: Iterable[int] = (),
    max_items: int | None = None,
    prefer_recent: bool = False,
    policy: str = "json_item_relevance_v1",
) -> JsonSelection:
    """Select complete JSON list items and return an auditable manifest."""
    _validate_limit(max_chars)
    source_text = _json_text(list(values), compact=True)
    entries = [
        StructuredEntry(
            item_id=f"item:{index}",
            text=_json_text(value, compact=True),
            ordinal=index,
        )
        for index, value in enumerate(values)
    ]
    required = {int(index) for index in required_indexes if 0 <= int(index) < len(entries)}

    def render(indexes: set[int]) -> str:
        return _json_text([values[index] for index in sorted(indexes)], compact=True)

    selected = _choose_indexes(
        entries,
        required_indexes=required,
        max_chars=max_chars,
        query=query,
        prefer_recent=prefer_recent,
        renderer=render,
        failure_code="required_json_item_exceeds_budget",
        max_items=max_items,
    )
    selected_items = tuple(values[index] for index in sorted(selected))
    manifest = _manifest(
        source_text=source_text,
        policy=policy,
        selected_item_ids=[entries[index].item_id for index in sorted(selected)],
        omitted_count=len(entries) - len(selected),
    )
    return JsonSelection(selected_items, manifest)


def compact_markdown_context(
    text: str,
    *,
    max_chars: int,
    per_section_max_chars: int,
    query: str = "",
    required_sections: Iterable[str] = (),
    excluded_sections: Iterable[str] = (),
    required_json_keys: dict[str, Iterable[str]] | None = None,
    allowed_json_keys: dict[str, Iterable[str]] | None = None,
    section_max_chars: dict[str, int] | None = None,
    prefer_recent: bool = True,
    policy: str = "markdown_relevance_v1",
) -> TextSelection:
    """Select complete Markdown sections and complete paragraph/JSON children.

    The returned text ends with a compact, valid JSON manifest containing the
    full source hash, original length, and selected item identifiers.
    """
    _validate_limit(max_chars)
    _validate_limit(per_section_max_chars)
    section_limits = {str(name): value for name, value in (section_max_chars or {}).items()}
    for value in section_limits.values():
        _validate_limit(value)
    required_names = {str(name) for name in required_sections}
    excluded_names = {str(name) for name in excluded_sections}
    sections = [section for section in _markdown_sections(text) if section.name not in excluded_names]
    if not sections:
        content_budget = max(1, max_chars - 256)
        base = select_text_blocks(
            text,
            max_chars=content_budget,
            query=query,
            required="edges" if text else (),
            prefer_recent=prefer_recent,
            policy=policy,
        )
        return _with_text_manifest(base, max_chars=max_chars)

    compacted: list[_CompactedSection] = []
    key_map = required_json_keys or {}
    allowed_key_map = allowed_json_keys or {}
    for section in sections:
        compacted.append(
            _compact_section(
                section,
                max_chars=section_limits.get(section.name, per_section_max_chars),
                query=query,
                required=section.name in required_names,
                required_json_keys={str(key) for key in key_map.get(section.name, ())},
                allowed_json_keys=(
                    {str(key) for key in allowed_key_map[section.name]}
                    if section.name in allowed_key_map
                    else None
                ),
            )
        )

    required_indexes = {index for index, section in enumerate(compacted) if section.name in required_names}

    def selected_ids(indexes: set[int]) -> list[str]:
        ids: list[str] = []
        for index in sorted(indexes):
            section = compacted[index]
            ids.append(f"section:{section.ordinal}:{section.name}")
            ids.extend(section.selected_item_ids)
        return ids

    def render(indexes: set[int]) -> str:
        body = "\n\n".join(compacted[index].text for index in sorted(indexes)).strip()
        selected = selected_ids(indexes)
        manifest = _manifest(
            source_text=text,
            policy=policy,
            selected_item_ids=selected,
            omitted_count=_context_omitted_count(compacted, indexes),
            query=query,
        )
        suffix = "# Structured Context Manifest\n" + _json_text(manifest, compact=True)
        return f"{body}\n\n{suffix}" if body else suffix

    while len(render(required_indexes)) > max_chars:
        overflow = len(render(required_indexes)) - max_chars
        progress = False
        candidates = sorted(required_indexes, key=lambda index: (-len(compacted[index].text), index))
        for index in candidates:
            current = compacted[index]
            next_limit = max(1, len(current.text) - overflow - 32)
            try:
                replacement = _compact_section(
                    sections[index],
                    max_chars=next_limit,
                    query=query,
                    required=True,
                    required_json_keys={str(key) for key in key_map.get(sections[index].name, ())},
                    allowed_json_keys=(
                        {str(key) for key in allowed_key_map[sections[index].name]}
                        if sections[index].name in allowed_key_map
                        else None
                    ),
                )
            except StructuredContextError:
                continue
            if len(replacement.text) >= len(current.text):
                continue
            compacted[index] = replacement
            progress = True
            break
        if not progress:
            raise StructuredContextError(
                "required_markdown_context_exceeds_budget",
                f"required complete Markdown/JSON entries exceed {max_chars} characters",
            )

    entries = [
        StructuredEntry(
            item_id=f"section:{section.ordinal}:{section.name}",
            text=section.text,
            ordinal=section.ordinal,
        )
        for section in compacted
    ]

    chosen = _choose_indexes(
        entries,
        required_indexes=required_indexes,
        max_chars=max_chars,
        query=query,
        prefer_recent=prefer_recent,
        renderer=render,
        failure_code="required_markdown_context_exceeds_budget",
    )
    rendered = render(chosen)
    manifest = _manifest(
        source_text=text,
        policy=policy,
        selected_item_ids=selected_ids(chosen),
        omitted_count=_context_omitted_count(compacted, chosen),
        query=query,
    )
    selected_items = tuple(
        {
            "id": item_id,
        }
        for item_id in manifest["selected_items"]
    )
    return TextSelection(
        rendered,
        manifest["source_sha256"],
        manifest["original_chars"],
        selected_items,
        manifest["omitted_count"],
        policy,
    )


def compact_markdown_section(
    name: str,
    text: str,
    *,
    max_chars: int,
    query: str = "",
    required_json_keys: Iterable[str] = (),
    policy: str = "markdown_section_relevance_v1",
) -> str:
    if len(text) <= max_chars:
        return text
    selection = compact_markdown_context(
        text,
        max_chars=max_chars,
        per_section_max_chars=max(1, max_chars - 256),
        query=query,
        required_sections={name},
        required_json_keys={name: set(required_json_keys)},
        policy=policy,
    )
    return selection.text


def rank_texts(texts: Sequence[str], *, query: str, prefer_recent: bool = False) -> list[int]:
    entries = [StructuredEntry(f"item:{index}", text, index) for index, text in enumerate(texts)]
    terms = _query_terms(query)
    return sorted(
        range(len(entries)),
        key=lambda index: (
            -_score(entries[index], terms, len(entries), prefer_recent),
            entries[index].ordinal,
            entries[index].item_id,
        ),
    )


def _compact_section(
    section: _Section,
    *,
    max_chars: int,
    query: str,
    required: bool,
    required_json_keys: set[str],
    allowed_json_keys: set[str] | None,
) -> _CompactedSection:
    if len(section.text) <= max_chars and allowed_json_keys is None:
        return _CompactedSection(section.name, section.text, section.ordinal, (), section.text)
    heading_match = re.match(r"(?m)^# [^\r\n]+\r?\n?", section.text)
    if heading_match is None:
        heading = f"# {section.name}"
        body = section.text
        body_offset = 0
    else:
        heading = heading_match.group(0).rstrip("\r\n")
        body_offset = heading_match.end()
        body = section.text[body_offset:].strip()
    body_limit = max_chars - len(heading) - 2
    if body_limit < 1:
        if required:
            raise StructuredContextError(
                "required_markdown_section_exceeds_budget",
                f"required section heading {section.name!r} exceeds its budget",
            )
        return _CompactedSection(section.name, heading, section.ordinal, (), section.text)

    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        parsed = None
        is_json = False
    else:
        is_json = True

    if is_json:
        compact_body, selected_ids = _compact_json_value(
            parsed,
            max_chars=body_limit,
            query=query,
            required=required,
            required_keys=required_json_keys,
            allowed_keys=allowed_json_keys,
            section_name=section.name,
        )
        text = f"{heading}\n{compact_body}"
        return _CompactedSection(section.name, text, section.ordinal, selected_ids, section.text)

    selection = select_text_blocks(
        body,
        max_chars=body_limit,
        query=query,
        required="edges" if required else (),
        prefer_recent=True,
        policy="markdown_paragraph_relevance_v1",
    )
    selected_ids = tuple(
        f"section:{section.ordinal}:{section.name}/{item['id']}"
        for item in selection.selected_items
    )
    text = f"{heading}\n{selection.text}" if selection.text else heading
    return _CompactedSection(section.name, text, section.ordinal, selected_ids, section.text)


def _compact_json_value(
    value: Any,
    *,
    max_chars: int,
    query: str,
    required: bool,
    required_keys: set[str],
    allowed_keys: set[str] | None,
    section_name: str,
) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, dict):
        keys = [key for key in value if allowed_keys is None or key in allowed_keys]
        entries = [
            StructuredEntry(
                item_id=f"json:{key}",
                text=f"{key}\n{_json_text(value[key], compact=True)}",
                ordinal=index,
            )
            for index, key in enumerate(keys)
        ]
        required_indexes = {
            index
            for index, key in enumerate(keys)
            if key in required_keys or (required and not required_keys)
        }

        def render(indexes: set[int]) -> str:
            return _json_text({keys[index]: value[keys[index]] for index in sorted(indexes)})

        selected = _choose_indexes(
            entries,
            required_indexes=required_indexes,
            max_chars=max_chars,
            query=query,
            prefer_recent=True,
            renderer=render,
            failure_code="required_json_item_exceeds_budget",
        )
        ids = tuple(f"{section_name}/json:{keys[index]}" for index in sorted(selected))
        return render(selected), ids
    if isinstance(value, list):
        entries = [
            StructuredEntry(f"json:{index}", _json_text(item, compact=True), index)
            for index, item in enumerate(value)
        ]
        required_indexes = ({0, len(entries) - 1} if required and entries else set())

        def render(indexes: set[int]) -> str:
            return _json_text([value[index] for index in sorted(indexes)])

        selected = _choose_indexes(
            entries,
            required_indexes=required_indexes,
            max_chars=max_chars,
            query=query,
            prefer_recent=True,
            renderer=render,
            failure_code="required_json_item_exceeds_budget",
        )
        ids = tuple(f"{section_name}/json:{index}" for index in sorted(selected))
        return render(selected), ids
    rendered = _json_text(value)
    if len(rendered) > max_chars:
        if required:
            raise StructuredContextError(
                "required_json_item_exceeds_budget",
                f"required scalar JSON value in {section_name!r} exceeds its budget",
            )
        return "null", ()
    return rendered, (f"{section_name}/json:value",)


def _choose_indexes(
    entries: Sequence[StructuredEntry],
    *,
    required_indexes: set[int],
    max_chars: int,
    query: str,
    prefer_recent: bool,
    renderer: Callable[[set[int]], str],
    failure_code: str,
    max_items: int | None = None,
) -> set[int]:
    chosen = set(required_indexes)
    if max_items is not None and len(chosen) > max_items:
        raise StructuredContextError(failure_code, "required entries exceed max_items")
    if len(renderer(chosen)) > max_chars:
        raise StructuredContextError(failure_code, f"required entries exceed {max_chars} characters")
    order = rank_texts([entry.text for entry in entries], query=query, prefer_recent=prefer_recent)
    for index in order:
        if index in chosen:
            continue
        if max_items is not None and len(chosen) >= max_items:
            break
        candidate = chosen | {index}
        if len(renderer(candidate)) <= max_chars:
            chosen = candidate
    return chosen


def _paragraph_entries(text: str) -> list[StructuredEntry]:
    separators = list(re.finditer(r"\r?\n[ \t]*\r?\n+", text))
    spans: list[tuple[int, int]] = []
    if separators:
        start = 0
        for separator in separators:
            _append_nonblank_span(text, spans, start, separator.start())
            start = separator.end()
        _append_nonblank_span(text, spans, start, len(text))
    else:
        for match in re.finditer(r"[^\r\n]+", text):
            _append_nonblank_span(text, spans, match.start(), match.end())
    return [
        StructuredEntry(
            item_id=f"paragraph:{index}",
            text=text[start:end],
            ordinal=index,
            start_char=start,
            end_char=end,
        )
        for index, (start, end) in enumerate(spans)
    ]


def _append_nonblank_span(text: str, spans: list[tuple[int, int]], start: int, end: int) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        spans.append((start, end))


def _required_indexes(required: str | Iterable[int], count: int) -> set[int]:
    if required == "all":
        return set(range(count))
    if required == "edges":
        return {0, count - 1} if count else set()
    if required == "tail":
        return {count - 1} if count else set()
    if isinstance(required, str):
        raise StructuredContextError("structured_required_policy_invalid", f"unknown required policy: {required}")
    return {int(index) for index in required if 0 <= int(index) < count}


def _markdown_sections(text: str) -> list[_Section]:
    matches = list(re.finditer(r"(?m)^# ([^\r\n]+)\r?$", text))
    sections: list[_Section] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            _Section(
                name=match.group(1).strip(),
                text=text[match.start():end].rstrip(),
                ordinal=index,
                start_char=match.start(),
                end_char=end,
            )
        )
    return sections


def _query_terms(query: str) -> tuple[str, ...]:
    lowered = str(query or "").lower()
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", lowered):
        terms.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            terms.extend(token[index:index + 2] for index in range(len(token) - 1))
    unique = list(dict.fromkeys(terms))
    return tuple(unique[:256])


def _score(entry: StructuredEntry, terms: Sequence[str], count: int, prefer_recent: bool) -> int:
    lowered = entry.text.lower()
    relevance = sum(1 for term in terms if term in lowered)
    recency = entry.ordinal + 1 if prefer_recent else count - entry.ordinal
    return relevance * 10_000 + recency


def _selection_item(
    item_id: str,
    text: str,
    start_char: int | None,
    end_char: int | None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "sha256": sha256_text(text),
        "original_chars": len(text),
    }
    if start_char is not None and end_char is not None:
        item["start_char"] = start_char
        item["end_char"] = end_char
    return item


def _manifest(
    *,
    source_text: str,
    policy: str,
    selected_item_ids: Sequence[str],
    omitted_count: int,
    query: str = "",
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": STRUCTURED_CONTEXT_SCHEMA_VERSION,
        "policy": policy,
        "source_sha256": sha256_text(source_text),
        "original_chars": len(source_text),
        "selected_items": list(selected_item_ids),
        "omitted_count": max(0, int(omitted_count)),
    }
    if query:
        manifest["query_sha256"] = sha256_text(query)
    return manifest


def _with_text_manifest(selection: TextSelection, *, max_chars: int) -> TextSelection:
    manifest_text = "# Structured Context Manifest\n" + _json_text(selection.manifest(), compact=True)
    rendered = f"{selection.text}\n\n{manifest_text}" if selection.text else manifest_text
    if len(rendered) > max_chars:
        raise StructuredContextError(
            "required_structured_manifest_exceeds_budget",
            f"selection and manifest exceed {max_chars} characters",
        )
    return TextSelection(
        rendered,
        selection.source_sha256,
        selection.original_chars,
        selection.selected_items,
        selection.omitted_count,
        selection.policy,
    )


def _context_omitted_count(sections: Sequence[_CompactedSection], selected_indexes: set[int]) -> int:
    omitted_sections = len(sections) - len(selected_indexes)
    return omitted_sections + sum(
        1
        for index, section in enumerate(sections)
        if index in selected_indexes and len(section.text) < len(section.source_text)
    )


def _json_text(value: Any, *, compact: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )


def _validate_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StructuredContextError("structured_context_limit_invalid", "max_chars must be positive")


__all__ = [
    "JsonSelection",
    "STRUCTURED_CONTEXT_SCHEMA_VERSION",
    "StructuredContextError",
    "TextSelection",
    "compact_markdown_context",
    "compact_markdown_section",
    "rank_texts",
    "select_json_items",
    "select_text_blocks",
    "sha256_text",
]
