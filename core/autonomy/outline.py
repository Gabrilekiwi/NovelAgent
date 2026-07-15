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
from core.engine.recovery_protocol import (
    MARKER_RECOVERY_PROTOCOL,
    RecoveryProtocolError,
    build_marker_envelope,
    reconcile_marker_transaction,
    validate_marker_envelope,
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
        "## 故事叙事意图与跨章阶段\n\n"
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
        "recovery_protocol": MARKER_RECOVERY_PROTOCOL,
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
    if (
        "recovery_protocol" in checkpoint
        and checkpoint["recovery_protocol"] != MARKER_RECOVERY_PROTOCOL
    ):
        raise OutlineCheckpointError(
            "outline_checkpoint_recovery_protocol_invalid",
            "outline checkpoint recovery protocol is not supported",
        )
    expected = canonical_hash(checkpoint, exclude_fields=("checkpoint_hash",))
    if checkpoint["checkpoint_hash"] != expected:
        raise OutlineCheckpointError(
            "outline_checkpoint_hash_mismatch", "outline checkpoint was modified"
        )
    return checkpoint


class OutlineCheckpointStore:
    def __init__(
        self,
        root: str | Path,
        *,
        story_project_root: str | Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.story_project_root = (
            Path(story_project_root).resolve()
            if story_project_root is not None
            else None
        )
        if self.story_project_root is not None:
            from core.runtime_paths import RuntimePaths

            expected_root = (
                RuntimePaths.for_story_project(self.story_project_root).runtime_dir
                / "autonomy"
            ).resolve()
            if self.root.resolve() != expected_root:
                raise OutlineCheckpointError(
                    "outline_checkpoint_story_root_mismatch",
                    "outline checkpoint root is not the bound StoryProject runtime",
                )

    def load(self, session_id: str, chapter_index: int) -> dict[str, Any] | None:
        expected_session_id = safe_id("session_id", session_id)
        expected_chapter_index = positive_int("chapter_index", chapter_index)
        directory = self._directory(expected_session_id, expected_chapter_index)
        with state_lock(self._dependency_fence_root(), directory / "latest.json"):
            with state_lock(self.root, directory / "latest.json"):
                return self._load_locked(
                    directory,
                    expected_session_id=expected_session_id,
                    expected_chapter_index=expected_chapter_index,
                )

    def _load_locked(
        self,
        directory: Path,
        *,
        expected_session_id: str,
        expected_chapter_index: int,
    ) -> dict[str, Any] | None:
        self._reconcile_markers(
            directory,
            expected_session_id=expected_session_id,
            expected_chapter_index=expected_chapter_index,
        )
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
        _assert_outline_store_scope(
            checkpoint,
            expected_session_id=expected_session_id,
            expected_chapter_index=expected_chapter_index,
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
        with state_lock(self._dependency_fence_root(), directory / "latest.json"):
            with state_lock(self.root, directory / "latest.json"):
                existing = self._load_locked(
                    directory,
                    expected_session_id=candidate["session_id"],
                    expected_chapter_index=candidate["chapter_index"],
                )
                self._assert_authority_current(candidate)
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
                if candidate.get("recovery_protocol") != MARKER_RECOVERY_PROTOCOL:
                    raise OutlineCheckpointError(
                        "outline_checkpoint_recovery_protocol_missing",
                        "new outline checkpoints require marker-backed publication",
                    )
                atomic_append_json(
                    directory / "staged" / f"{candidate['checkpoint_id']}.json",
                    candidate,
                )
                marker = _build_outline_commit_marker(
                    candidate,
                    previous_checkpoint_hash=(
                        existing["checkpoint_hash"] if existing is not None else None
                    ),
                    recorded_at=invalidated_at or now_utc(),
                )
                atomic_append_json(
                    directory / "commit_markers" / f"{candidate['checkpoint_id']}.json",
                    marker,
                )
                self._publish_marker(
                    directory,
                    marker,
                    expected_session_id=candidate["session_id"],
                    expected_chapter_index=candidate["chapter_index"],
                )
                return candidate

    def history(self, session_id: str, chapter_index: int) -> list[dict[str, Any]]:
        expected_session_id = safe_id("session_id", session_id)
        expected_chapter_index = positive_int("chapter_index", chapter_index)
        directory = self._directory(expected_session_id, expected_chapter_index)
        with state_lock(self._dependency_fence_root(), directory / "latest.json"):
            with state_lock(self.root, directory / "latest.json"):
                self._reconcile_markers(
                    directory,
                    expected_session_id=expected_session_id,
                    expected_chapter_index=expected_chapter_index,
                )
                checkpoints = [
                    validate_outline_checkpoint(load_json_object(path))
                    for path in sorted(
                        (directory / "revisions").glob("outline_*.json")
                    )
                ]
                for checkpoint in checkpoints:
                    _assert_outline_store_scope(
                        checkpoint,
                        expected_session_id=expected_session_id,
                        expected_chapter_index=expected_chapter_index,
                    )
                return checkpoints

    def invalidations(
        self, session_id: str, chapter_index: int
    ) -> list[dict[str, Any]]:
        expected_session_id = safe_id("session_id", session_id)
        expected_chapter_index = positive_int("chapter_index", chapter_index)
        directory = self._directory(expected_session_id, expected_chapter_index)
        with state_lock(self._dependency_fence_root(), directory / "latest.json"):
            with state_lock(self.root, directory / "latest.json"):
                self._reconcile_markers(
                    directory,
                    expected_session_id=expected_session_id,
                    expected_chapter_index=expected_chapter_index,
                )
                records = []
                for path in sorted((directory / "invalidations").glob("*.json")):
                    record = validate_mapping(
                        load_json_object(path),
                        "autonomy_outline_invalidation.schema.json",
                        "AutonomyOutlineInvalidation",
                    )
                    _assert_outline_store_scope(
                        record,
                        expected_session_id=expected_session_id,
                        expected_chapter_index=expected_chapter_index,
                    )
                    expected = canonical_hash(
                        record, exclude_fields=("invalidation_hash",)
                    )
                    if record["invalidation_hash"] != expected:
                        raise OutlineCheckpointError(
                            "outline_checkpoint_invalidation_hash_mismatch",
                            "outline invalidation evidence was modified",
                        )
                    records.append(record)
                return records

    def _dependency_fence_root(self) -> Path:
        return self.root.parent / ".root-remap-fence"

    def _assert_authority_current(self, candidate: Mapping[str, Any]) -> None:
        if self.story_project_root is None:
            return
        from core.story_project.identity import load_project_identity

        identity = load_project_identity(self.story_project_root)
        authority = identity.authority if identity is not None else None
        if (
            identity is None
            or identity.ephemeral
            or not isinstance(authority, Mapping)
            or identity.book_id != candidate["book_id"]
            or authority.get("authority_epoch") != candidate["authority"]["epoch"]
            or authority.get("head_event_hash")
            != candidate["authority"]["head_event_hash"]
        ):
            raise OutlineCheckpointError(
                "outline_checkpoint_authority_stale",
                "outline checkpoint authority changed before durable publication",
            )

    def _directory(self, session_id: str, chapter_index: int) -> Path:
        session = safe_id("session_id", session_id)
        chapter = positive_int("chapter_index", chapter_index)
        return (
            self.root
            / "outline_checkpoints"
            / session
            / f"chapter-{chapter:06d}"
        )

    def _reconcile_markers(
        self,
        directory: Path,
        *,
        expected_session_id: str,
        expected_chapter_index: int,
    ) -> None:
        marker_dir = directory / "commit_markers"
        markers = []
        if marker_dir.is_dir():
            for path in sorted(marker_dir.glob("outline_*.json")):
                envelope = load_json_object(path)
                marker = _validate_outline_commit_marker(envelope)
                _assert_outline_store_scope(
                    marker,
                    expected_session_id=expected_session_id,
                    expected_chapter_index=expected_chapter_index,
                )
                markers.append((envelope, marker))
        while True:
            pointer_path = directory / "latest.json"
            current_hash = None
            if pointer_path.is_file():
                pointer = load_json_object(pointer_path)
                if set(pointer) != {
                    "schema_version",
                    "checkpoint_id",
                    "checkpoint_hash",
                }:
                    raise OutlineCheckpointError(
                        "outline_checkpoint_pointer_invalid",
                        "latest outline pointer is malformed",
                    )
                current_hash = sha256_digest(
                    "checkpoint_hash", pointer["checkpoint_hash"]
                )
            candidates = [
                item
                for item in markers
                if item[1]["previous_checkpoint_hash"] == current_hash
                and item[1]["checkpoint_hash"] != current_hash
            ]
            if not candidates:
                self._assert_no_unmarked_completion(
                    directory,
                    markers,
                    expected_session_id=expected_session_id,
                    expected_chapter_index=expected_chapter_index,
                )
                return
            if len(candidates) != 1:
                raise OutlineCheckpointError(
                    "outline_checkpoint_recovery_ambiguous",
                    "multiple marked outline checkpoints claim the same predecessor",
                )
            self._publish_marker(
                directory,
                candidates[0][0],
                expected_session_id=expected_session_id,
                expected_chapter_index=expected_chapter_index,
            )

    @staticmethod
    def _assert_no_unmarked_completion(
        directory: Path,
        markers: list[tuple[Mapping[str, Any], Mapping[str, Any]]],
        *,
        expected_session_id: str,
        expected_chapter_index: int,
    ) -> None:
        marked_by_id: dict[str, Mapping[str, Any]] = {}
        for _, marker in markers:
            checkpoint_id = str(marker["checkpoint_id"])
            previous = marked_by_id.get(checkpoint_id)
            if previous is not None and previous != marker:
                raise OutlineCheckpointError(
                    "outline_checkpoint_recovery_ambiguous",
                    "multiple durable markers claim the same outline checkpoint",
                )
            marked_by_id[checkpoint_id] = marker
        staged_root = directory / "staged"
        revisions_root = directory / "revisions"
        for revision_path in sorted(revisions_root.glob("outline_*.json")):
            checkpoint = validate_outline_checkpoint(load_json_object(revision_path))
            _assert_outline_store_scope(
                checkpoint,
                expected_session_id=expected_session_id,
                expected_chapter_index=expected_chapter_index,
            )
            checkpoint_id = str(checkpoint["checkpoint_id"])
            marker = marked_by_id.get(checkpoint_id)
            staged_path = staged_root / revision_path.name
            marker_backed = (
                checkpoint.get("recovery_protocol") == MARKER_RECOVERY_PROTOCOL
            )
            if marker_backed and marker is None:
                raise OutlineCheckpointError(
                    "outline_checkpoint_marker_missing",
                    "completed staged checkpoint exists without its durable commit marker",
                )
            if marker_backed and not staged_path.is_file():
                raise OutlineCheckpointError(
                    "outline_checkpoint_staged_missing",
                    "marker-backed outline checkpoint exists without its staged evidence",
                )
            if (
                marker_backed
                and marker is not None
                and marker["checkpoint_hash"] != checkpoint["checkpoint_hash"]
            ):
                raise OutlineCheckpointError(
                    "outline_checkpoint_marker_mismatch",
                    "outline marker does not bind the completed checkpoint",
                )
            if staged_path.is_file():
                staged = validate_outline_checkpoint(load_json_object(staged_path))
                _assert_outline_store_scope(
                    staged,
                    expected_session_id=expected_session_id,
                    expected_chapter_index=expected_chapter_index,
                )
                if staged != checkpoint:
                    raise OutlineCheckpointError(
                        "outline_checkpoint_staged_mismatch",
                        "completed outline checkpoint differs from its staged evidence",
                    )
            if staged_path.is_file() and marker is None:
                raise OutlineCheckpointError(
                    "outline_checkpoint_marker_missing",
                    "completed staged checkpoint exists without its durable commit marker",
                )

    def _publish_marker(
        self,
        directory: Path,
        marker: Mapping[str, Any],
        *,
        expected_session_id: str,
        expected_chapter_index: int,
    ) -> None:
        committed = _validate_outline_commit_marker(marker)
        _assert_outline_store_scope(
            committed,
            expected_session_id=expected_session_id,
            expected_chapter_index=expected_chapter_index,
        )
        pointer_path = directory / "latest.json"
        pointer = load_json_object(pointer_path) if pointer_path.is_file() else None
        current_hash = pointer.get("checkpoint_hash") if isinstance(pointer, dict) else None
        completion_present = current_hash == committed["checkpoint_hash"]
        return reconcile_marker_transaction(
            marker_present=True,
            completion_present=completion_present,
            on_roll_back=lambda: self._reject_marked_rollback(),
            on_roll_forward=lambda: self._finish_marker_publication(
                directory,
                committed,
                current_hash=current_hash,
                expected_session_id=expected_session_id,
                expected_chapter_index=expected_chapter_index,
            ),
            on_completed=lambda: None,
        )

    @staticmethod
    def _reject_marked_rollback() -> None:
        raise OutlineCheckpointError(
            "outline_checkpoint_marker_invalid",
            "a durable outline marker cannot take the pre-marker rollback path",
        )

    def _finish_marker_publication(
        self,
        directory: Path,
        committed: Mapping[str, Any],
        *,
        current_hash: str | None,
        expected_session_id: str,
        expected_chapter_index: int,
    ) -> None:
        pointer_path = directory / "latest.json"
        if current_hash != committed["previous_checkpoint_hash"]:
            raise OutlineCheckpointError(
                "outline_checkpoint_recovery_conflict",
                "marked outline checkpoint no longer matches the current head",
            )
        staged_path = directory / "staged" / f"{committed['checkpoint_id']}.json"
        if not staged_path.is_file():
            raise OutlineCheckpointError(
                "outline_checkpoint_staged_missing",
                "outline marker exists without its staged checkpoint evidence",
            )
        candidate = validate_outline_checkpoint(load_json_object(staged_path))
        _assert_outline_store_scope(
            candidate,
            expected_session_id=expected_session_id,
            expected_chapter_index=expected_chapter_index,
        )
        if candidate["checkpoint_hash"] != committed["checkpoint_hash"]:
            raise OutlineCheckpointError(
                "outline_checkpoint_marker_mismatch",
                "outline marker does not bind its staged checkpoint",
            )
        atomic_append_json(
            directory / "revisions" / f"{candidate['checkpoint_id']}.json",
            candidate,
        )
        previous = committed["previous_checkpoint_hash"]
        if previous is not None:
            invalidation = {
                "schema_version": "1.0",
                "invalidation_hash": "0" * 64,
                "session_id": candidate["session_id"],
                "chapter_index": candidate["chapter_index"],
                "invalidated_checkpoint_hash": previous,
                "replacement_checkpoint_hash": candidate["checkpoint_hash"],
                "reason": "authority_or_context_changed",
                "recorded_at": committed["recorded_at"],
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
            pointer_path,
            {
                "schema_version": "1.0",
                "checkpoint_id": candidate["checkpoint_id"],
                "checkpoint_hash": candidate["checkpoint_hash"],
            },
        )


def _build_outline_commit_marker(
    checkpoint: Mapping[str, Any],
    *,
    previous_checkpoint_hash: str | None,
    recorded_at: str,
) -> dict[str, Any]:
    candidate = validate_outline_checkpoint(checkpoint)
    return build_marker_envelope(
        transaction_id=candidate["checkpoint_id"],
        intent_hash=candidate["checkpoint_hash"],
        evidence_kind="outline_checkpoint",
        evidence_hash=candidate["checkpoint_hash"],
        metadata={
            "previous_checkpoint_hash": sha256_digest(
                "previous_checkpoint_hash", previous_checkpoint_hash, optional=True
            ),
            "session_id": candidate["session_id"],
            "chapter_index": candidate["chapter_index"],
            "recorded_at": required_text("recorded_at", recorded_at),
        },
    )


def _validate_outline_commit_marker(value: Any) -> dict[str, Any]:
    try:
        envelope = validate_marker_envelope(value)
    except RecoveryProtocolError as exc:
        raise OutlineCheckpointError(
            "outline_checkpoint_marker_invalid", str(exc)
        ) from exc
    metadata = envelope["metadata"]
    required_metadata = {
        "previous_checkpoint_hash",
        "session_id",
        "chapter_index",
        "recorded_at",
    }
    if envelope["evidence_kind"] != "outline_checkpoint" or set(
        metadata
    ) != required_metadata:
        raise OutlineCheckpointError(
            "outline_checkpoint_marker_invalid", "outline marker scope is malformed"
        )
    checkpoint_id = safe_id("checkpoint_id", envelope["transaction_id"])
    checkpoint_hash = sha256_digest("checkpoint_hash", envelope["evidence_hash"])
    if envelope["intent_hash"] != checkpoint_hash:
        raise OutlineCheckpointError(
            "outline_checkpoint_marker_invalid", "outline marker intent changed"
        )
    session_id = safe_id("session_id", metadata["session_id"])
    chapter_index = positive_int("chapter_index", metadata["chapter_index"])
    sha256_digest(
        "previous_checkpoint_hash", metadata["previous_checkpoint_hash"], optional=True
    )
    recorded_at = required_text("recorded_at", metadata["recorded_at"])
    return {
        "checkpoint_id": checkpoint_id,
        "checkpoint_hash": checkpoint_hash,
        "previous_checkpoint_hash": metadata["previous_checkpoint_hash"],
        "session_id": session_id,
        "chapter_index": chapter_index,
        "recorded_at": recorded_at,
        "marker_hash": envelope["marker_hash"],
    }


def _assert_outline_store_scope(
    value: Mapping[str, Any],
    *,
    expected_session_id: str,
    expected_chapter_index: int,
) -> None:
    if (
        value.get("session_id") != expected_session_id
        or value.get("chapter_index") != expected_chapter_index
    ):
        raise OutlineCheckpointError(
            "outline_checkpoint_store_scope_mismatch",
            "outline recovery evidence belongs to another session or chapter",
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
