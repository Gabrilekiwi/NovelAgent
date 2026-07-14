from __future__ import annotations

from copy import deepcopy
from typing import Any


CANONICAL_MEMORY_SCHEMA_VERSION = "2.0"
TYPED_CANONICAL_MEMORY_SCHEMA_VERSION = "2.2"
MEMORY_SYSTEM_CREATED_BY = "NovelAgent Memory System V2"


def create_empty_canonical_memory(
    book_id: str = "default",
    title: str = "Untitled",
    language: str = "zh-CN",
    *,
    schema_version: str = CANONICAL_MEMORY_SCHEMA_VERSION,
    authority_epoch: int = 1,
) -> dict[str, Any]:
    if schema_version == TYPED_CANONICAL_MEMORY_SCHEMA_VERSION:
        return create_empty_typed_canonical_memory(
            book_id=book_id,
            title=title,
            language=language,
            authority_epoch=authority_epoch,
        )
    if schema_version != CANONICAL_MEMORY_SCHEMA_VERSION:
        raise ValueError(f"unsupported canonical memory schema_version: {schema_version}")
    return {
        "schema_version": CANONICAL_MEMORY_SCHEMA_VERSION,
        "revision": 1,
        "book_id": book_id,
        "title": title,
        "language": language,
        "world": {},
        "current_state": {},
        "characters": {},
        "locations": {},
        "timeline": [],
        "open_threads": [],
        "chapter_states": {},
        "constraints": [],
        "style_rules": [],
        "source_index": {},
        "source_resolution": {},
        "metadata": {
            "source": "empty",
            "created_by": MEMORY_SYSTEM_CREATED_BY,
        },
    }


def create_empty_typed_canonical_memory(
    book_id: str = "default",
    title: str = "Untitled",
    language: str = "zh-CN",
    *,
    authority_epoch: int = 1,
) -> dict[str, Any]:
    if isinstance(authority_epoch, bool) or not isinstance(authority_epoch, int) or authority_epoch < 1:
        raise ValueError("authority_epoch must be a positive integer")
    return {
        "schema_version": TYPED_CANONICAL_MEMORY_SCHEMA_VERSION,
        "revision": 1,
        "head_event_hash": None,
        "authority_epoch": authority_epoch,
        "book_id": book_id,
        "title": title,
        "language": language,
        "world": {},
        "current_state": {},
        "characters": {},
        "locations": {},
        "relationships": {},
        "injuries": {},
        "inventories": {},
        "resources": {},
        "glossary": {},
        "corruption": {},
        "story_time": {
            "label": "unknown",
            "elapsed_minutes": 0,
            "chapter_index": 0,
            "scene_index": 0,
        },
        "foreshadowing": {},
        "timeline": [],
        "open_threads": [],
        "chapter_states": {},
        "constraints": [],
        "style_rules": [],
        "source_index": {},
        "source_resolution": {},
        "metadata": {
            "source": "genesis",
            "created_by": MEMORY_SYSTEM_CREATED_BY,
        },
    }


def clone_canonical_memory(memory: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(memory)


__all__ = [
    "CANONICAL_MEMORY_SCHEMA_VERSION",
    "TYPED_CANONICAL_MEMORY_SCHEMA_VERSION",
    "clone_canonical_memory",
    "create_empty_canonical_memory",
    "create_empty_typed_canonical_memory",
]
