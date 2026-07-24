from __future__ import annotations

import json
from typing import Any


STORY_STATE_CONTEXT_KEYS = (
    "last_chapter_ending",
    "last_scene_location",
    "last_scene_characters",
    "open_threads",
    "required_opening_bridge",
)
STORY_STATE_SECTION_MAX_CHARS = 4_096
STORY_STATE_SOFT_MAX_CHARS = 2_500

_LAST_CHAPTER_ENDING_MAX_CHARS = 500
_LAST_SCENE_LOCATION_MAX_CHARS = 100
_LAST_SCENE_CHARACTER_MAX_CHARS = 50
_LAST_SCENE_CHARACTER_MAX_ITEMS = 20
_OPEN_THREAD_MAX_CHARS = 120
_OPEN_THREAD_MAX_ITEMS = 15
_REQUIRED_OPENING_BRIDGE_MAX_CHARS = 300


def project_story_state_for_model(value: Any) -> dict[str, Any]:
    """Return the bounded semantic Story State used in model prompts.

    Durable snapshots may contain provenance, source excerpts, and other audit
    fields. Those remain on disk but are intentionally excluded from prompts.
    Recent open threads are preferred because canonical memory appends newer
    threads after older ones.
    """
    story_state = value if isinstance(value, dict) else {}
    characters = _bounded_items(
        story_state.get("last_scene_characters"),
        max_items=_LAST_SCENE_CHARACTER_MAX_ITEMS,
        max_item_chars=_LAST_SCENE_CHARACTER_MAX_CHARS,
        prefer_recent=False,
    )
    open_threads = _bounded_items(
        story_state.get("open_threads"),
        max_items=_OPEN_THREAD_MAX_ITEMS,
        max_item_chars=_OPEN_THREAD_MAX_CHARS,
        prefer_recent=True,
    )
    projected = {
        "last_chapter_ending": _bounded_text(
            story_state.get("last_chapter_ending"),
            _LAST_CHAPTER_ENDING_MAX_CHARS,
        ),
        "last_scene_location": _bounded_text(
            story_state.get("last_scene_location"),
            _LAST_SCENE_LOCATION_MAX_CHARS,
        ),
        "last_scene_characters": characters,
        "open_threads": open_threads,
        "required_opening_bridge": _bounded_text(
            story_state.get("required_opening_bridge"),
            _REQUIRED_OPENING_BRIDGE_MAX_CHARS,
        ),
    }

    # The soft target keeps routine prompts compact. Drop the oldest open
    # threads first, but keep every other continuity field and at least the
    # newest open thread. The 4096-character section cap remains the hard stop.
    while len(_json_text(projected)) > STORY_STATE_SOFT_MAX_CHARS and len(open_threads) > 1:
        open_threads.pop(0)
    return projected


def _bounded_items(
    value: Any,
    *,
    max_items: int,
    max_item_chars: int,
    prefer_recent: bool,
) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [_bounded_text(item, max_item_chars) for item in value]
    items = [item for item in items if item]
    if prefer_recent:
        return items[-max_items:]
    return items[:max_items]


def _bounded_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


__all__ = [
    "STORY_STATE_CONTEXT_KEYS",
    "STORY_STATE_SECTION_MAX_CHARS",
    "STORY_STATE_SOFT_MAX_CHARS",
    "project_story_state_for_model",
]
