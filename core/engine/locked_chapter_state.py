from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


LOCKED_CHAPTER_RESOLUTION_VERSION = "1.0"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LockedChapterStateError(ValueError):
    pass


def validate_locked_chapter_resolution(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LockedChapterStateError("locked chapter resolution must be an object")
    payload = dict(value)
    try:
        validate_schema(payload, "locked_chapter_resolution.schema.json")
    except SchemaValidationError as exc:
        raise LockedChapterStateError(str(exc)) from exc

    for field in (
        "id",
        "book_id",
        "source_run_id",
        "reason",
    ):
        _require_safe_text(field, payload[field])
    for field in (
        "resolved_execution_ids",
        "resolved_attempt_ids",
        "discarded_run_ids",
    ):
        _require_unique_safe_ids(field, payload[field])

    try:
        datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LockedChapterStateError("created_at must be an ISO-8601 timestamp") from exc

    draft = payload.get("complete_draft")
    if isinstance(draft, dict):
        _require_sha256("complete_draft.sha256", draft["sha256"])
        if _content_sha256(draft["text"]) != draft["sha256"]:
            raise LockedChapterStateError("complete draft hash mismatch")
        if draft.get("source_attempt_id") is not None:
            _require_safe_text(
                "complete_draft.source_attempt_id",
                draft["source_attempt_id"],
            )

    indexes: list[int] = []
    for scene in payload["scenes"]:
        index = int(scene["index"])
        if index in indexes:
            raise LockedChapterStateError("scene indexes must be unique")
        indexes.append(index)
        _require_safe_text("scenes.source_attempt_id", scene["source_attempt_id"])
        _require_sha256("scenes.sha256", scene["sha256"])
        if _content_sha256(scene["text"]) != scene["sha256"]:
            raise LockedChapterStateError(f"scene {index} hash mismatch")
    if indexes and indexes != list(range(1, len(indexes) + 1)):
        raise LockedChapterStateError("recovered scenes must form a contiguous prefix starting at 1")

    action = payload["action"]
    if action == "repair_draft" and not isinstance(draft, dict):
        raise LockedChapterStateError("repair_draft requires complete_draft")
    if action == "resume_scenes" and not payload["scenes"]:
        raise LockedChapterStateError("resume_scenes requires at least one scene")
    if action == "reset" and (draft is not None or payload["scenes"]):
        raise LockedChapterStateError("reset must not retain a draft or scenes")

    _require_sha256("resolution_hash", payload["resolution_hash"])
    expected = canonical_json_hash(
        payload,
        exclude_fields=("resolution_hash",),
        exclude_environment_fields=False,
    )
    if expected != payload["resolution_hash"]:
        raise LockedChapterStateError("locked chapter resolution hash mismatch")
    return payload


def load_locked_chapter_resolutions(run_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(run_dir) / "locked_chapter_resolutions"
    if not root.is_dir():
        return []
    resolutions: list[dict[str, Any]] = []
    for path in sorted(root.glob("resolution_*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LockedChapterStateError(f"cannot read locked chapter resolution {path}: {exc}") from exc
        resolution = validate_locked_chapter_resolution(value)
        resolution = dict(resolution)
        resolution["_path"] = str(path.resolve())
        resolutions.append(resolution)
    return sorted(
        resolutions,
        key=lambda item: (str(item["created_at"]), str(item["id"])),
    )


def resolved_execution_ids(run_dir: str | Path) -> set[str]:
    return {
        str(execution_id)
        for resolution in load_locked_chapter_resolutions(run_dir)
        for execution_id in resolution["resolved_execution_ids"]
    }


def discarded_run_ids(run_dir: str | Path) -> set[str]:
    return {
        str(run_id)
        for resolution in load_locked_chapter_resolutions(run_dir)
        for run_id in resolution["discarded_run_ids"]
    }


def active_locked_chapter_checkpoint(
    run_dir: str | Path,
    *,
    chapter_index: int,
    expected_book_id: str | None = None,
) -> dict[str, Any] | None:
    matching = [
        resolution
        for resolution in load_locked_chapter_resolutions(run_dir)
        if int(resolution["chapter_index"]) == int(chapter_index)
        and (expected_book_id is None or resolution["book_id"] == expected_book_id)
    ]
    if not matching:
        return None
    latest = matching[-1]
    return None if latest["action"] == "reset" else latest


def _require_unique_safe_ids(field: str, values: list[Any]) -> None:
    seen: set[str] = set()
    for value in values:
        text = _require_safe_text(field, value)
        if text in seen:
            raise LockedChapterStateError(f"{field} must not contain duplicates")
        seen.add(text)


def _require_safe_text(field: str, value: Any) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise LockedChapterStateError(f"{field} contains an unsafe identifier")
    return text


def _require_sha256(field: str, value: Any) -> str:
    text = str(value or "")
    if not _SHA256.fullmatch(text):
        raise LockedChapterStateError(f"{field} must be a lowercase sha256 digest")
    return text


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "LOCKED_CHAPTER_RESOLUTION_VERSION",
    "LockedChapterStateError",
    "active_locked_chapter_checkpoint",
    "discarded_run_ids",
    "load_locked_chapter_resolutions",
    "resolved_execution_ids",
    "validate_locked_chapter_resolution",
]
