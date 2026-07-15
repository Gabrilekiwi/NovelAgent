from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    atomic_replace_json,
    canonical_hash,
    load_json_object,
    now_utc,
    positive_int,
    required_text,
    safe_id,
    sha256_digest,
    state_lock,
    validate_mapping,
)


class OutlineCheckpointError(AutonomyContractError):
    pass


def render_arc_outline(chapter_index: int, planned: Mapping[str, Any]) -> str:
    """Render a deterministic, provider-free outline from one RunArc target."""

    chapter = positive_int("chapter_index", chapter_index)
    fields = {
        name: required_text(name, planned.get(name))
        for name in (
            "mainline",
            "relationship",
            "escalation",
            "resource_cost",
            "foreshadowing",
        )
    }
    return (
        f"# 第{chapter}章自主细纲\n\n"
        f"核心事件: {fields['mainline']}\n\n"
        "## 必写节拍\n\n"
        f"- {fields['relationship']}\n"
        f"- {fields['escalation']}\n"
        f"- {fields['resource_cost']}\n\n"
        f"结尾压力: {fields['foreshadowing']}\n"
    )


def build_outline_checkpoint(
    *,
    book_id: str,
    session_id: str,
    plan_id: str,
    arc_plan_id: str,
    chapter_index: int,
    planned_target_hash: str,
    source_snapshot_hash: str,
    authority_epoch: int,
    authority_head_event_hash: str | None,
    outline_input_digest: str,
    provider_profile: str,
    execution_kind: str,
    outline_text: str,
    canonical_relative_path: str,
    canonical_before_sha256: str | None,
    created_at: str | None = None,
) -> dict[str, Any]:
    chapter = positive_int("chapter_index", chapter_index)
    if not isinstance(outline_text, str) or not outline_text.strip():
        raise OutlineCheckpointError(
            "outline_checkpoint_text_invalid", "outline_text is required"
        )
    text = outline_text
    if execution_kind not in {"model", "deterministic"}:
        raise OutlineCheckpointError(
            "outline_execution_kind_invalid", "outline execution kind is not trusted"
        )
    relative = _safe_relative_path(canonical_relative_path)
    identity = {
        "session_id": safe_id("session_id", session_id),
        "chapter_index": chapter,
        "planned_target_hash": sha256_digest(
            "planned_target_hash", planned_target_hash
        ),
        "outline_input_digest": sha256_digest(
            "outline_input_digest", outline_input_digest
        ),
        "source_snapshot_hash": sha256_digest(
            "source_snapshot_hash", source_snapshot_hash
        ),
        "authority_epoch": positive_int(
            "authority_epoch", authority_epoch, minimum=0
        ),
        "authority_head_event_hash": sha256_digest(
            "authority_head_event_hash",
            authority_head_event_hash,
            optional=True,
        ),
    }
    checkpoint_id = f"outline_{canonical_hash(identity)[:24]}"
    checkpoint = {
        "schema_version": "1.0",
        "checkpoint_id": checkpoint_id,
        "checkpoint_hash": "0" * 64,
        "book_id": safe_id("book_id", book_id),
        "session_id": identity["session_id"],
        "plan_id": safe_id("plan_id", plan_id),
        "arc_plan_id": safe_id("arc_plan_id", arc_plan_id),
        "chapter_index": chapter,
        "planned_target_hash": identity["planned_target_hash"],
        "source_snapshot_hash": identity["source_snapshot_hash"],
        "authority": {
            "epoch": identity["authority_epoch"],
            "head_event_hash": identity["authority_head_event_hash"],
        },
        "outline_input_digest": identity["outline_input_digest"],
        "provider_profile": safe_id("provider_profile", provider_profile),
        "execution_kind": execution_kind,
        "outline_text": text,
        "outline_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "canonical_relative_path": relative,
        "canonical_before_sha256": sha256_digest(
            "canonical_before_sha256", canonical_before_sha256, optional=True
        ),
        "created_at": created_at or now_utc(),
    }
    checkpoint["checkpoint_hash"] = canonical_hash(
        checkpoint, exclude_fields=("checkpoint_hash",)
    )
    return validate_outline_checkpoint(checkpoint)


def validate_outline_checkpoint(value: Any) -> dict[str, Any]:
    checkpoint = validate_mapping(
        value,
        "autonomy_outline_checkpoint.schema.json",
        "AutonomyOutlineCheckpoint",
    )
    for field in (
        "checkpoint_id",
        "book_id",
        "session_id",
        "plan_id",
        "arc_plan_id",
        "provider_profile",
    ):
        safe_id(field, checkpoint[field])
    positive_int("chapter_index", checkpoint["chapter_index"])
    for field in (
        "checkpoint_hash",
        "planned_target_hash",
        "source_snapshot_hash",
        "outline_input_digest",
        "outline_hash",
    ):
        sha256_digest(field, checkpoint[field])
    sha256_digest(
        "authority.head_event_hash",
        checkpoint["authority"]["head_event_hash"],
        optional=True,
    )
    positive_int("authority.epoch", checkpoint["authority"]["epoch"], minimum=0)
    sha256_digest(
        "canonical_before_sha256",
        checkpoint["canonical_before_sha256"],
        optional=True,
    )
    _safe_relative_path(checkpoint["canonical_relative_path"])
    text = checkpoint["outline_text"]
    if not isinstance(text, str) or not text.strip():
        raise OutlineCheckpointError(
            "outline_checkpoint_text_invalid", "outline_text is required"
        )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != checkpoint["outline_hash"]:
        raise OutlineCheckpointError(
            "outline_checkpoint_text_hash_mismatch", "outline checkpoint text changed"
        )
    expected = canonical_hash(checkpoint, exclude_fields=("checkpoint_hash",))
    if checkpoint["checkpoint_hash"] != expected:
        raise OutlineCheckpointError(
            "outline_checkpoint_hash_mismatch", "outline checkpoint was modified"
        )
    return checkpoint


class OutlineCheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load(self, session_id: str, chapter_index: int) -> dict[str, Any] | None:
        directory = self._directory(session_id, chapter_index)
        pointer_path = directory / "latest.json"
        if not pointer_path.is_file():
            return None
        pointer = load_json_object(pointer_path)
        if set(pointer) != {"schema_version", "checkpoint_id", "checkpoint_hash"}:
            raise OutlineCheckpointError(
                "outline_checkpoint_pointer_invalid", "latest outline pointer is malformed"
            )
        checkpoint_id = safe_id("checkpoint_id", pointer["checkpoint_id"])
        checkpoint_hash = sha256_digest("checkpoint_hash", pointer["checkpoint_hash"])
        checkpoint = validate_outline_checkpoint(
            load_json_object(directory / "revisions" / f"{checkpoint_id}.json")
        )
        if checkpoint["checkpoint_hash"] != checkpoint_hash:
            raise OutlineCheckpointError(
                "outline_checkpoint_pointer_invalid", "latest outline pointer hash changed"
            )
        return checkpoint

    def create(
        self,
        checkpoint: Mapping[str, Any],
        *,
        chapter_committed: bool = False,
        invalidated_at: str | None = None,
    ) -> dict[str, Any]:
        candidate = validate_outline_checkpoint(dict(checkpoint))
        directory = self._directory(
            candidate["session_id"], candidate["chapter_index"]
        )
        with state_lock(self.root, directory / "latest.json"):
            existing = self.load(
                candidate["session_id"], int(candidate["chapter_index"])
            )
            if existing is not None:
                if _checkpoint_scope(existing) == _checkpoint_scope(candidate):
                    if existing != candidate:
                        raise OutlineCheckpointError(
                            "outline_checkpoint_replay_conflict",
                            "same outline inputs produced different immutable bytes",
                        )
                    return existing
                if chapter_committed:
                    raise OutlineCheckpointError(
                        "outline_checkpoint_chapter_committed",
                        "a committed chapter cannot publish a replacement outline checkpoint",
                    )
            elif chapter_committed:
                raise OutlineCheckpointError(
                    "outline_checkpoint_chapter_committed",
                    "a committed chapter cannot create a new outline checkpoint",
                )
            atomic_append_json(
                directory / "revisions" / f"{candidate['checkpoint_id']}.json",
                candidate,
            )
            if existing is not None:
                invalidation = {
                    "schema_version": "1.0",
                    "invalidation_hash": "0" * 64,
                    "session_id": candidate["session_id"],
                    "chapter_index": candidate["chapter_index"],
                    "invalidated_checkpoint_hash": existing["checkpoint_hash"],
                    "replacement_checkpoint_hash": candidate["checkpoint_hash"],
                    "reason": "authority_or_context_changed",
                    "recorded_at": invalidated_at or now_utc(),
                }
                invalidation["invalidation_hash"] = canonical_hash(
                    invalidation, exclude_fields=("invalidation_hash",)
                )
                validate_mapping(
                    invalidation,
                    "autonomy_outline_invalidation.schema.json",
                    "AutonomyOutlineInvalidation",
                )
                atomic_append_json(
                    directory
                    / "invalidations"
                    / f"{invalidation['invalidation_hash']}.json",
                    invalidation,
                )
            atomic_replace_json(
                directory / "latest.json",
                {
                    "schema_version": "1.0",
                    "checkpoint_id": candidate["checkpoint_id"],
                    "checkpoint_hash": candidate["checkpoint_hash"],
                },
            )
            return candidate

    def history(self, session_id: str, chapter_index: int) -> list[dict[str, Any]]:
        directory = self._directory(session_id, chapter_index)
        return [
            validate_outline_checkpoint(load_json_object(path))
            for path in sorted((directory / "revisions").glob("outline_*.json"))
        ]

    def invalidations(
        self, session_id: str, chapter_index: int
    ) -> list[dict[str, Any]]:
        directory = self._directory(session_id, chapter_index)
        records = []
        for path in sorted((directory / "invalidations").glob("*.json")):
            record = validate_mapping(
                load_json_object(path),
                "autonomy_outline_invalidation.schema.json",
                "AutonomyOutlineInvalidation",
            )
            expected = canonical_hash(record, exclude_fields=("invalidation_hash",))
            if record["invalidation_hash"] != expected:
                raise OutlineCheckpointError(
                    "outline_checkpoint_invalidation_hash_mismatch",
                    "outline invalidation evidence was modified",
                )
            records.append(record)
        return records

    def _directory(self, session_id: str, chapter_index: int) -> Path:
        session = safe_id("session_id", session_id)
        chapter = positive_int("chapter_index", chapter_index)
        return (
            self.root
            / "outline_checkpoints"
            / session
            / f"chapter-{chapter:06d}"
        )


def _checkpoint_scope(checkpoint: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        checkpoint["book_id"],
        checkpoint["session_id"],
        checkpoint["plan_id"],
        checkpoint["arc_plan_id"],
        checkpoint["chapter_index"],
        checkpoint["planned_target_hash"],
        checkpoint["source_snapshot_hash"],
        checkpoint["authority"],
        checkpoint["outline_input_digest"],
        checkpoint["provider_profile"],
        checkpoint["execution_kind"],
        checkpoint["canonical_relative_path"],
        checkpoint["canonical_before_sha256"],
    )


def _safe_relative_path(value: Any) -> str:
    text = required_text("canonical_relative_path", value).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OutlineCheckpointError(
            "outline_checkpoint_path_invalid", "canonical outline path must be relative"
        )
    if str(path) != text or path.suffix.lower() != ".md":
        raise OutlineCheckpointError(
            "outline_checkpoint_path_invalid", "canonical outline must be a normalized Markdown path"
        )
    return text


__all__ = [
    "OutlineCheckpointError",
    "OutlineCheckpointStore",
    "build_outline_checkpoint",
    "render_arc_outline",
    "validate_outline_checkpoint",
]
