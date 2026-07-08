from __future__ import annotations

from copy import deepcopy
from typing import Any


CANONICAL_MEMORY_SCHEMA_VERSION = "2.0"


def create_empty_canonical_memory(
    book_id: str = "default",
    title: str = "Untitled",
    language: str = "zh-CN",
) -> dict[str, Any]:
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
            "created_by": "NovelAgent Memory System V2",
        },
    }


def clone_canonical_memory(memory: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(memory)


__all__ = [
    "CANONICAL_MEMORY_SCHEMA_VERSION",
    "clone_canonical_memory",
    "create_empty_canonical_memory",
]
