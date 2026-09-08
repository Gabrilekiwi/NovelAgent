"""Consistency checks for legacy character fields that mirror location ledgers."""
from __future__ import annotations

CHARACTER_LOCATION_FIELDS = ("location", "location_id", "current_location")


def validate_character_location_mirrors(state: dict) -> None:
    for entity_id, character in state.get("characters", {}).items():
        location = state.get("locations", {}).get(entity_id, {}).get("location_id")
        for field in CHARACTER_LOCATION_FIELDS:
            if field in character and character[field] != location:
                raise ValueError(f"character_location_mirror_conflict: {entity_id}.{field} differs from its location ledger")
