from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Discourse, temporal, and grammatical tokens that the lexical analyzer can
# mistake for place names.  A mention of「最后，她把旧取水图摊回桌面」is not a
# location called「最后」, even if an earlier writeback registered that token.
_UNUSABLE_LOCATION_NAMES = frozenset(
    {
        # Chapter read sets use this literal when no previous location exists.
        "unknown",
        "最后",
        "最终",
        "然后",
        "随后",
        "接着",
        "于是",
        "此后",
        "此前",
        "此时",
        "这时",
        "那时",
        "当时",
        "当初",
        "起初",
        "最初",
        "开始",
        "后来",
        "现在",
        "目前",
        "刚才",
        "同时",
        "突然",
        "立刻",
        "立即",
        "马上",
        "已经",
        "仍旧",
        "仍然",
        "还是",
        "不过",
        "但是",
        "可是",
        "而且",
        "此外",
        "另外",
        "因此",
        "所以",
        "因为",
        "如果",
        "虽然",
        "尽管",
        "无论",
        "首先",
        "其次",
        "再次",
        "终于",
        "总之",
        "其实",
        "当然",
        "接下来",
        "不久",
        "原来",
        "原本",
        "相反",
        "反之",
        "原处",
        "的人",
        "和撤回",
        "finally",
        "then",
        "later",
        "afterwards",
        "meanwhile",
        "suddenly",
        "now",
        "currently",
        "however",
        "therefore",
        "thus",
    }
)
_UNUSABLE_LOCATION_PREFIXES = "的和与并或及"


def is_usable_location_name(value: str | None) -> bool:
    """Return whether ``value`` can serve as a continuity location name."""

    name = str(value or "").strip()
    if len(name) < 2:
        return False
    if name in _UNUSABLE_LOCATION_NAMES or name.casefold() in _UNUSABLE_LOCATION_NAMES:
        return False
    if name[0] in _UNUSABLE_LOCATION_PREFIXES:
        return False
    return True


def fallback_cast_location(snapshot: Mapping[str, Any] | None) -> str:
    """Return the last-scene cast's agreeing usable position, if one exists.

    When writeback has already stored a discourse token as last_scene_location,
    character_positions usually still hold the real place and should be used
    for the next chapter's opening continuity.
    """

    if not isinstance(snapshot, Mapping):
        return ""
    story_state = snapshot.get("story_state")
    if not isinstance(story_state, Mapping):
        story_state = {}
    spatial_state = snapshot.get("spatial_state")
    if not isinstance(spatial_state, Mapping):
        spatial_state = {}
    positions = spatial_state.get("character_positions")
    if not isinstance(positions, Mapping):
        positions = {}
    names = [
        str(item).strip()
        for item in (story_state.get("last_scene_characters") or [])
        if str(item).strip()
    ]
    found: list[str] = []
    for name in names:
        location = str(positions.get(name) or "").strip()
        if is_usable_location_name(location):
            found.append(location)
    if found and all(item == found[0] for item in found):
        return found[0]
    if found:
        return found[0]
    for value in positions.values():
        location = str(value or "").strip()
        if is_usable_location_name(location):
            return location
    return ""
