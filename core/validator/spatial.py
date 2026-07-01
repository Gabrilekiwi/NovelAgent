from __future__ import annotations

from typing import Any

from core.validator.common import get_location_terms, get_locations


def validate_spatial(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
    locations = get_locations(snapshot)

    location_terms = [
        term
        for location, data in locations.items()
        for term in get_location_terms(str(location), data)
    ]
    lowered = chapter_text.lower()

    if location_terms and not any(term.lower() in lowered for term in location_terms):
        problems.append(
            {
                "code": "no_known_location",
                "message": "Chapter does not mention any known location from snapshot.",
                "suggested_term": location_terms[0],
                "evidence": [{"kind": "known_location_terms", "value": ", ".join(location_terms[:10])}],
            }
        )

    characters = snapshot.get("characters") or {}
    for name, data in characters.items():
        if not isinstance(data, dict):
            continue
        current_location = data.get("current_location")
        if not current_location:
            continue
        if current_location not in locations:
            problems.append(
                {
                    "code": "character_unknown_location",
                    "message": f"Character {name} references unknown location {current_location}.",
                    "character": str(name),
                    "location": str(current_location),
                    "evidence": [
                        {"kind": "character", "value": str(name)},
                        {"kind": "unknown_location", "value": str(current_location)},
                    ],
                }
            )
            continue
        character_mentioned = str(name).lower() in lowered
        if character_mentioned:
            terms = get_location_terms(str(current_location), locations[current_location])
            if not any(term.lower() in lowered for term in terms):
                problems.append(
                    {
                        "code": "character_location_not_mentioned",
                        "message": f"Character {name} appears without current location {current_location}.",
                        "character": str(name),
                        "location": str(current_location),
                        "evidence": [
                            {"kind": "character", "value": str(name)},
                            {"kind": "current_location", "value": str(current_location)},
                        ],
                    }
                )

    return {"name": "spatial", "ok": not problems, "problems": problems}
