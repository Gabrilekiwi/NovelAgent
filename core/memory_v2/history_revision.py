from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping, Sequence
import uuid

from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import (
    MemoryIntegrityError,
    create_memory_event_batch,
    load_memory_event_batches,
    memory_patch_content_hash,
    memory_projection_hash,
    replay_memory_events,
    validate_memory_event_batch,
)
from core.memory_v2.events import create_memory_event_context
from core.memory_v2.patch import MEMORY_PATCH_CREATED_BY, create_memory_patch
from core.memory_v2.projection import rebuild_memory_projections
from core.memory_v2.reducer import CURRENT_REDUCER_VERSION, apply_memory_events, apply_memory_patch
from core.memory_v2.storage import load_canonical_memory
from core.memory_v2.validator import validate_canonical_memory
from core.schema import SchemaValidationError, validate_schema
from core.engine.safe_paths import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    SafePathError,
    assert_safe_local_tree,
)
from core.story_project.authority import (
    AUTHORITY_MODE_EVENT,
    prepare_event_authority_advance,
)
from core.story_project.identity import (
    ProjectIdentity,
    load_project_identity,
    project_identity_path,
    validate_project_identity,
)
from core.story_project.read_set import (
    SOURCE_DIRECTORIES,
    capture_story_project_read_set,
    verify_story_project_read_set,
)
from core.story_project.paths import resolve_prose


HISTORICAL_REVISION_SCHEMA_VERSION = "1.0"
HISTORICAL_REVISION_KINDS = frozenset({"amend", "import", "retcon"})
_PUBLICATION_STATUS = {
    "amend": "amended",
    "import": "imported",
    "retcon": "retconned",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_FORBIDDEN_PROSE_OPS = frozenset(
    {"replace_chapter", "overwrite_chapter", "write_prose", "publish_chapter"}
)
_TERMINAL_SESSION_STATES = frozenset({"complete", "completed", "cancelled", "abandoned"})


class HistoricalRevisionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def assert_event_authority_reconciliation_ready(projection: Mapping[str, Any]) -> None:
    """Block generation while a historical revision has stale downstream state."""

    current_state = projection.get("current_state")
    if not isinstance(current_state, Mapping) or "historical_revision" not in current_state:
        return
    marker = current_state.get("historical_revision")
    if not isinstance(marker, Mapping):
        raise HistoricalRevisionError(
            "event_authority_reconciliation_marker_invalid",
            "canonical historical revision marker is malformed",
        )
    required = marker.get("requires_downstream_reconciliation")
    if not isinstance(required, bool):
        raise HistoricalRevisionError(
            "event_authority_reconciliation_marker_invalid",
            "canonical historical revision marker has no boolean readiness state",
        )
    invalidated = any(
        bool(marker.get(field))
        for field in (
            "invalidated_event_batch_ids",
            "invalidated_outline_ids",
            "invalidated_session_ids",
        )
    )
    reconciliation = marker.get("reconciliation")
    dependency_inventory_hash = marker.get("dependency_inventory_hash")
    _require_sha256(
        "historical_revision.dependency_inventory_hash", dependency_inventory_hash
    )
    if required:
        if reconciliation is not None:
            raise HistoricalRevisionError(
                "event_authority_reconciliation_marker_invalid",
                "blocking historical revision cannot contain a reconciliation receipt",
            )
        raise HistoricalRevisionError(
            "event_authority_downstream_reconciliation_required",
            "historical revision invalidated downstream events, outlines, or sessions",
        )
    if invalidated and not isinstance(reconciliation, Mapping):
        raise HistoricalRevisionError(
            "event_authority_reconciliation_marker_invalid",
            "cleared downstream invalidations require an audited reconciliation binding",
        )
    if isinstance(reconciliation, Mapping):
        _validate_reconciliation_record(reconciliation)
        expected_ids = {
            "resolved_event_batch_ids": list(
                marker.get("invalidated_event_batch_ids") or []
            ),
            "resolved_outline_ids": list(marker.get("invalidated_outline_ids") or []),
            "resolved_session_ids": list(marker.get("invalidated_session_ids") or []),
        }
        if (
            any(
                reconciliation[field] != expected
                for field, expected in expected_ids.items()
            )
            or reconciliation["resolved_dependency_inventory_hash"]
            != dependency_inventory_hash
        ):
            raise HistoricalRevisionError(
                "event_authority_reconciliation_marker_invalid",
                "reconciliation receipt does not exactly acknowledge marker invalidations",
            )


def prepare_amend_transaction(**kwargs: Any) -> dict[str, Any]:
    return _prepare_historical_revision_transaction(revision_kind="amend", **kwargs)


def prepare_import_transaction(**kwargs: Any) -> dict[str, Any]:
    return _prepare_historical_revision_transaction(revision_kind="import", **kwargs)


def prepare_retcon_transaction(**kwargs: Any) -> dict[str, Any]:
    return _prepare_historical_revision_transaction(revision_kind="retcon", **kwargs)


def prepare_historical_revision_transaction(
    *, revision_kind: str, **kwargs: Any
) -> dict[str, Any]:
    """Prepare an append-only amend/import/retcon transaction.

    This API never returns a published-prose target.  Its StoryProject write set
    contains only the ProjectIdentity authority-head transition; all correction
    evidence and impact records are immutable runtime artifacts.
    """

    return _prepare_historical_revision_transaction(revision_kind=revision_kind, **kwargs)


def capture_historical_revision_dependency_inventory(
    *,
    story_project_root: str | Path,
    book_id: str,
    authority_epoch: int,
    head_event_hash: str,
    historical_chapter_index: int,
    canonical_next_chapter_index: int,
) -> dict[str, Any]:
    """Capture every live outline checkpoint and durable autonomy session.

    The caller-provided inventory used by prepare is a CAS declaration only;
    prepare captures this filesystem view again and requires byte-for-byte
    semantic equality so a caller cannot omit a dependency or invent a hash.
    """

    from core.autonomy.outline import validate_outline_checkpoint
    from core.autonomy.plans import validate_instruction_plan
    from core.autonomy.session import validate_session_event, validate_session_genesis
    from core.runtime_paths import RuntimePaths

    story = Path(story_project_root).resolve()
    epoch = _positive_int("authority_epoch", authority_epoch)
    head = _require_sha256("head_event_hash", head_event_hash)
    historical = _positive_int("historical_chapter_index", historical_chapter_index)
    canonical_next = _positive_int(
        "canonical_next_chapter_index", canonical_next_chapter_index
    )
    if canonical_next <= historical:
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "historical chapter must precede the canonical next chapter",
        )
    autonomy_root = RuntimePaths.for_story_project(story).runtime_dir / "autonomy"
    if autonomy_root.exists():
        _assert_inventory_directory(autonomy_root, "autonomy root")
    outlines: list[dict[str, Any]] = []
    outline_root = autonomy_root / "outline_checkpoints"
    if outline_root.exists() and not outline_root.is_dir():
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "outline checkpoint root is not a directory",
        )
    if outline_root.is_dir():
        _assert_inventory_directory(outline_root, "outline checkpoint root")
        for session_dir in sorted(outline_root.iterdir(), key=lambda item: item.name):
            if not session_dir.is_dir() or session_dir.is_symlink():
                raise HistoricalRevisionError(
                    "historical_revision_dependency_inventory_invalid",
                    "outline checkpoint session entry is unsafe",
                )
            _assert_inventory_directory(session_dir, "outline checkpoint session")
            for chapter_dir in sorted(session_dir.iterdir(), key=lambda item: item.name):
                if not chapter_dir.is_dir() or chapter_dir.is_symlink():
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        "outline checkpoint chapter entry is unsafe",
                    )
                _assert_inventory_directory(chapter_dir, "outline checkpoint chapter")
                latest_path = chapter_dir / "latest.json"
                if not latest_path.is_file() or latest_path.is_symlink():
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_incomplete",
                        "outline checkpoint history has no trusted latest pointer",
                    )
                pointer = _load_inventory_json(latest_path)
                if set(pointer) != {"schema_version", "checkpoint_id", "checkpoint_hash"}:
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        "outline checkpoint latest pointer is malformed",
                    )
                checkpoint_id = _required_text(pointer, "checkpoint_id")
                checkpoint_hash = _require_sha256(
                    "outline.checkpoint_hash", pointer.get("checkpoint_hash")
                )
                checkpoint_path = chapter_dir / "revisions" / f"{checkpoint_id}.json"
                revisions_root = chapter_dir / "revisions"
                if revisions_root.is_dir():
                    _assert_inventory_directory(revisions_root, "outline checkpoint revisions")
                if not checkpoint_path.is_file() or checkpoint_path.is_symlink():
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_incomplete",
                        "outline latest pointer target is missing",
                    )
                try:
                    checkpoint = validate_outline_checkpoint(
                        _load_inventory_json(checkpoint_path)
                    )
                except Exception as exc:
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        f"outline checkpoint is invalid: {exc}",
                    ) from exc
                if (
                    checkpoint["checkpoint_id"] != checkpoint_id
                    or checkpoint["checkpoint_hash"] != checkpoint_hash
                    or checkpoint["session_id"] != session_dir.name
                    or checkpoint["book_id"] != book_id
                ):
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        "outline checkpoint scope differs from its durable path",
                    )
                checkpoint_epoch = _positive_int(
                    "outline.authority_epoch", checkpoint["authority"]["epoch"]
                )
                checkpoint_head = _require_sha256(
                    "outline.head_event_hash",
                    checkpoint["authority"].get("head_event_hash"),
                )
                outlines.append(
                    {
                        "outline_id": checkpoint_id,
                        "chapter_index": int(checkpoint["chapter_index"]),
                        "artifact_sha256": checkpoint_hash,
                        "authority_epoch": checkpoint_epoch,
                        "head_event_hash": checkpoint_head,
                    }
                )

    sessions: list[dict[str, Any]] = []
    sessions_root = autonomy_root / "sessions"
    if sessions_root.exists() and not sessions_root.is_dir():
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "session root is not a directory",
        )
    if sessions_root.is_dir():
        _assert_inventory_directory(sessions_root, "session root")
        for session_dir in sorted(sessions_root.iterdir(), key=lambda item: item.name):
            if session_dir.name == "latest.json" and session_dir.is_file():
                continue
            if not session_dir.is_dir() or session_dir.is_symlink():
                raise HistoricalRevisionError(
                    "historical_revision_dependency_inventory_invalid",
                    "session root contains an unsafe entry",
                )
            _assert_inventory_directory(session_dir, "session directory")
            try:
                genesis = validate_session_genesis(
                    _load_inventory_json(session_dir / "genesis.json")
                )
                plan = validate_instruction_plan(
                    _load_inventory_json(session_dir / "instruction_plan.json")
                )
            except Exception as exc:
                raise HistoricalRevisionError(
                    "historical_revision_dependency_inventory_invalid",
                    f"session immutable records are invalid: {exc}",
                ) from exc
            session_id = str(genesis["session_id"])
            if (
                session_dir.name != session_id
                or genesis["book_id"] != book_id
                or genesis["plan_id"] != plan["plan_id"]
                or genesis["plan_hash"] != plan["plan_hash"]
                or genesis["source_snapshot"] != plan["source_snapshot"]
            ):
                raise HistoricalRevisionError(
                    "historical_revision_dependency_inventory_invalid",
                    "session scope differs from its immutable plan/genesis",
                )
            events_root = session_dir / "events"
            if events_root.is_dir():
                _assert_inventory_directory(events_root, "session events")
            event_paths = sorted(events_root.glob("*.json")) if events_root.is_dir() else []
            if not event_paths:
                raise HistoricalRevisionError(
                    "historical_revision_dependency_inventory_incomplete",
                    "durable session has no append-only event chain",
                )
            events: list[dict[str, Any]] = []
            previous_hash: str | None = None
            for sequence, event_path in enumerate(event_paths, start=1):
                if event_path.is_symlink() or not event_path.name.startswith(f"{sequence:06d}-"):
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        "session event sequence is unsafe or discontinuous",
                    )
                try:
                    event = validate_session_event(_load_inventory_json(event_path))
                except Exception as exc:
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        f"session event is invalid: {exc}",
                    ) from exc
                if (
                    int(event["sequence"]) != sequence
                    or event["previous_event_hash"] != previous_hash
                    or event["session_id"] != session_id
                    or event["book_id"] != book_id
                    or event["plan_id"] != plan["plan_id"]
                    or event["plan_hash"] != plan["plan_hash"]
                    or event["genesis_hash"] != genesis["genesis_hash"]
                ):
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        "session event chain scope or hash link differs",
                    )
                expected_event_name = f"{sequence:06d}-{event['event_hash'][:20]}.json"
                if event_path.name != expected_event_name:
                    raise HistoricalRevisionError(
                        "historical_revision_dependency_inventory_invalid",
                        "session event filename does not bind its immutable event hash",
                    )
                events.append(event)
                previous_hash = str(event["event_hash"])
            event_type = str(events[-1]["event_type"])
            status = "active" if event_type in {"started", "resumed"} else event_type
            source = genesis["source_snapshot"]
            session_epoch = _positive_int(
                "session.authority_epoch", source["authority_epoch"]
            )
            session_head = _require_sha256(
                "session.head_event_hash", source.get("authority_head_event_hash")
            )
            sessions.append(
                {
                    "session_id": session_id,
                    "first_chapter_index": int(plan["chapter_start"]),
                    "last_chapter_index": int(plan["chapter_end"]),
                    "artifact_sha256": canonical_json_hash(
                        {
                            "genesis_hash": genesis["genesis_hash"],
                            "plan_hash": plan["plan_hash"],
                            "event_hashes": [event["event_hash"] for event in events],
                        }
                    ),
                    "authority_epoch": session_epoch,
                    "head_event_hash": session_head,
                    "status": status,
                }
            )

    inventory = {
        "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
        "inventory_kind": "complete_downstream_dependencies",
        "book_id": _required_text({"book_id": book_id}, "book_id"),
        "authority_epoch": epoch,
        "head_event_hash": head,
        "historical_chapter_index": historical,
        "canonical_next_chapter_index": canonical_next,
        "outline_inventory_complete": True,
        "session_inventory_complete": True,
        "outline_dependencies": sorted(
            outlines, key=lambda item: (item["outline_id"], item["chapter_index"])
        ),
        "session_dependencies": sorted(
            sessions, key=lambda item: item["session_id"]
        ),
    }
    inventory["inventory_hash"] = canonical_json_hash(inventory)
    return validate_historical_revision_dependency_inventory(inventory)


def _prepare_historical_revision_transaction(
    *,
    revision_kind: str,
    memory_root: str | Path,
    story_project_root: str | Path,
    story_project_root_uuid: str,
    transaction_id: str,
    historical_chapter_index: int,
    historical_chapter_path: str | Path,
    expected_historical_chapter_sha256: str,
    revision_source_path: str | Path,
    expected_revision_source_sha256: str,
    evidence_spans: Sequence[Mapping[str, Any]],
    operations: Sequence[Mapping[str, Any]],
    authority_epoch: int,
    expected_head_event_hash: str,
    expected_revision: int,
    source_project_digest: str,
    context_digest: str,
    dependency_inventory: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None = None,
    projection_root: str | Path | None = None,
) -> dict[str, Any]:
    kind = _revision_kind(revision_kind)
    tx_id = _safe_id(transaction_id)
    chapter_index = _positive_int("historical_chapter_index", historical_chapter_index)
    epoch = _positive_int("authority_epoch", authority_epoch)
    revision = _positive_int("expected_revision", expected_revision)
    root_uuid = _canonical_uuid(story_project_root_uuid)
    for field, value in (
        ("expected_historical_chapter_sha256", expected_historical_chapter_sha256),
        ("expected_revision_source_sha256", expected_revision_source_sha256),
        ("expected_head_event_hash", expected_head_event_hash),
        ("source_project_digest", source_project_digest),
        ("context_digest", context_digest),
    ):
        _require_sha256(field, value)

    memory = Path(memory_root).resolve()
    story = Path(story_project_root).resolve()
    historical_path = Path(historical_chapter_path).resolve()
    revision_path = Path(revision_source_path).resolve()
    if not story.is_dir():
        raise HistoricalRevisionError("story_project_missing", "StoryProject root is not a directory")
    if historical_path == revision_path:
        raise HistoricalRevisionError(
            "published_prose_in_place_edit_forbidden",
            "revision evidence must be a separate source from immutable published prose",
        )
    if not historical_path.is_file():
        raise HistoricalRevisionError("historical_chapter_missing", "published chapter does not exist")
    if not revision_path.is_file():
        raise HistoricalRevisionError("revision_source_missing", "revision evidence source does not exist")

    identity = load_project_identity(story)
    if identity is None:
        raise HistoricalRevisionError("project_identity_missing", "ProjectIdentity is required")
    authority = identity.authority or {}
    if authority.get("mode") != AUTHORITY_MODE_EVENT:
        raise HistoricalRevisionError(
            "event_authority_required", "historical revision requires event_v1 authority"
        )
    if int(authority.get("authority_epoch") or 0) != epoch:
        raise HistoricalRevisionError("stale_authority_epoch", "ProjectIdentity authority epoch changed")
    if authority.get("head_event_hash") != expected_head_event_hash:
        raise HistoricalRevisionError("stale_authority_head", "ProjectIdentity event head changed")

    event_store = memory / "events"
    canonical_path = memory / "canonical_memory.json"
    replay, base = _verified_authority_base(
        event_store=event_store,
        canonical_path=canonical_path,
        identity=identity,
        authority_epoch=epoch,
        expected_head_event_hash=expected_head_event_hash,
        expected_revision=revision,
    )
    batches = load_memory_event_batches(event_store)
    canonical_next_chapter = _canonical_next_chapter(base, batches)
    read_set = capture_story_project_read_set(
        story,
        canonical_next_chapter,
        project_identity=identity,
    )
    relative_historical = _story_relative_published_prose(
        story, historical_path, read_set, chapter_index=chapter_index
    )
    historical_bytes = historical_path.read_bytes()
    historical_sha256 = hashlib.sha256(historical_bytes).hexdigest()
    if historical_sha256 != expected_historical_chapter_sha256:
        raise HistoricalRevisionError(
            "historical_chapter_source_drift", "published chapter bytes changed before prepare"
        )
    if context_digest != read_set["context_digest"]:
        raise HistoricalRevisionError(
            "historical_revision_context_digest_mismatch",
            "context_digest must bind the complete StoryProject read-set",
        )
    if source_project_digest != read_set["membership_fingerprint"]:
        raise HistoricalRevisionError(
            "historical_revision_source_digest_mismatch",
            "source_project_digest must bind StoryProject source membership",
        )
    revision_bytes = revision_path.read_bytes()
    revision_source_sha256 = hashlib.sha256(revision_bytes).hexdigest()
    if revision_source_sha256 != expected_revision_source_sha256:
        raise HistoricalRevisionError(
            "revision_source_drift", "revision evidence bytes changed before prepare"
        )
    try:
        revision_text = revision_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoricalRevisionError(
            "revision_source_encoding_invalid", "revision evidence must be UTF-8"
        ) from exc
    if not revision_text:
        raise HistoricalRevisionError("revision_source_empty", "revision evidence must not be empty")
    event_context = create_memory_event_context(
        chapter_body=revision_text,
        evidence_spans=[dict(item) for item in evidence_spans],
        authority_epoch=epoch,
    )

    anchor, downstream_batches, anchor_evidence = _historical_anchor(
        batches,
        chapter_index,
        story_root=story,
        projection=base,
        historical_relative_path=relative_historical,
        historical_chapter_sha256=historical_sha256,
    )
    inventory = _bind_dependency_inventory(
        dependency_inventory,
        story_project_root=story,
        book_id=identity.book_id,
        authority_epoch=epoch,
        head_event_hash=expected_head_event_hash,
        historical_chapter_index=chapter_index,
        canonical_next_chapter_index=canonical_next_chapter,
    )
    event_invalidations = _event_invalidations(downstream_batches)
    outline_invalidations = _outline_invalidations(
        inventory["outline_dependencies"], chapter_index
    )
    session_invalidations = _session_invalidations(
        inventory["session_dependencies"], chapter_index
    )
    reconciliation_record = _prepare_reconciliation(
        reconciliation,
        current_marker=(base.get("current_state") or {}).get("historical_revision"),
        event_invalidations=event_invalidations,
        outline_invalidations=outline_invalidations,
        session_invalidations=session_invalidations,
        dependency_inventory_hash=str(inventory["inventory_hash"]),
        revision_source_sha256=revision_source_sha256,
    )

    source_hashes = {
        "historical_chapter_sha256": historical_sha256,
        "revision_source_sha256": revision_source_sha256,
        "source_project_digest": source_project_digest,
        "context_digest": context_digest,
        "dependency_inventory_hash": inventory["inventory_hash"],
    }
    authority_before = {
        "authority_epoch": epoch,
        "revision": revision,
        "head_event_hash": expected_head_event_hash,
    }
    anchor_binding = _anchor_binding(anchor, anchor_evidence=anchor_evidence)
    impact_basis = {
        "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
        "transaction_id": tx_id,
        "revision_kind": kind,
        "book_id": identity.book_id,
        "historical_chapter_index": chapter_index,
        "historical_chapter_relative_path": relative_historical,
        "source_hashes": source_hashes,
        "authority_before": authority_before,
        "anchor_batch": anchor_binding,
        "event_invalidations": event_invalidations,
        "outline_invalidations": outline_invalidations,
        "session_invalidations": session_invalidations,
        "reconciliation": reconciliation_record,
    }
    impact_basis_hash = canonical_json_hash(impact_basis)

    prepared_operations = _revision_operations(
        operations,
        transaction_id=tx_id,
        revision_kind=kind,
        historical_chapter_index=chapter_index,
        historical_chapter_relative_path=relative_historical,
        historical_chapter_sha256=historical_sha256,
        revision_source_sha256=revision_source_sha256,
        impact_basis_hash=impact_basis_hash,
        dependency_inventory_hash=str(inventory["inventory_hash"]),
        event_invalidations=event_invalidations,
        outline_invalidations=outline_invalidations,
        session_invalidations=session_invalidations,
        reconciliation=reconciliation_record,
    )
    patch = create_memory_patch(
        patch_id=f"history_{kind}_{tx_id}",
        source_kind=kind,
        source_path=f"history-revision:{tx_id}",
        operations=prepared_operations,
        metadata={
            "historical_revision": True,
            "transaction_id": tx_id,
            "historical_chapter_index": chapter_index,
            "historical_chapter_relative_path": relative_historical,
            "historical_chapter_sha256": historical_sha256,
            "revision_source_sha256": revision_source_sha256,
            "impact_basis_hash": impact_basis_hash,
            "dependency_inventory_hash": inventory["inventory_hash"],
        },
    )
    if str(patch["patch_id"]) in replay["patch_index"]:
        raise HistoricalRevisionError(
            "historical_revision_already_committed", "transaction patch id already exists"
        )
    try:
        updated, events = apply_memory_patch(
            base,
            patch,
            reducer_version=CURRENT_REDUCER_VERSION,
            event_context=event_context,
        )
    except Exception as exc:
        raise HistoricalRevisionError("historical_revision_operation_invalid", str(exc)) from exc
    if not events:
        raise HistoricalRevisionError(
            "historical_revision_empty", "historical revision must produce at least one event"
        )
    if apply_memory_events(base, events, reducer_version=CURRENT_REDUCER_VERSION) != updated:
        raise MemoryIntegrityError("historical revision events do not reproduce canonical projection")

    batch = create_memory_event_batch(
        book_id=identity.book_id,
        patch=patch,
        events=events,
        expected_revision=revision,
        previous_batch_hash=str(replay["last_batch_hash"]),
        source_project_digest=source_project_digest,
        context_digest=context_digest,
        batch_kind=kind,
        publication_status=_PUBLICATION_STATUS[kind],
        quality_state={
            "historical_revision": True,
            "impact_basis_hash": impact_basis_hash,
        },
        schema_version="2.2",
        reducer_version=CURRENT_REDUCER_VERSION,
    )
    authority_after = {
        "authority_epoch": int(updated["authority_epoch"]),
        "revision": int(updated["revision"]),
        "head_event_hash": str(updated["head_event_hash"]),
    }
    advanced_identity = prepare_event_authority_advance(
        identity,
        expected_authority_epoch=epoch,
        expected_head_event_hash=expected_head_event_hash,
        new_head_event_hash=authority_after["head_event_hash"],
    )

    evidence = _build_evidence(
        transaction_id=tx_id,
        revision_kind=kind,
        book_id=identity.book_id,
        historical_chapter_index=chapter_index,
        historical_chapter_relative_path=relative_historical,
        historical_chapter_sha256=historical_sha256,
        revision_source_sha256=revision_source_sha256,
        revision_text=revision_text,
        evidence_spans=event_context["evidence_spans"],
    )
    invalidation_manifest = _build_invalidation_manifest(
        transaction_id=tx_id,
        revision_kind=kind,
        book_id=identity.book_id,
        historical_chapter_index=chapter_index,
        historical_chapter_relative_path=relative_historical,
        authority_before=authority_before,
        authority_after=authority_after,
        event_invalidations=event_invalidations,
        outline_invalidations=outline_invalidations,
        session_invalidations=session_invalidations,
        impact_basis_hash=impact_basis_hash,
        batch_hash=str(batch["batch_hash"]),
        evidence_hash=str(evidence["evidence_hash"]),
        reconciliation=reconciliation_record,
    )
    impact_report = _build_impact_report(
        transaction_id=tx_id,
        revision_kind=kind,
        book_id=identity.book_id,
        historical_chapter_index=chapter_index,
        historical_chapter_relative_path=relative_historical,
        source_hashes=source_hashes,
        anchor_batch=anchor_binding,
        authority_before=authority_before,
        authority_after=authority_after,
        event_invalidations=event_invalidations,
        outline_invalidations=outline_invalidations,
        session_invalidations=session_invalidations,
        impact_basis_hash=impact_basis_hash,
        batch_hash=str(batch["batch_hash"]),
        evidence_hash=str(evidence["evidence_hash"]),
        invalidation_manifest_hash=str(invalidation_manifest["manifest_hash"]),
        reconciliation=reconciliation_record,
    )

    identity_path = project_identity_path(story)
    identity_before = identity_path.read_bytes()
    identity_before_sha256 = hashlib.sha256(identity_before).hexdigest()
    if identity_before_sha256 != read_set["identity_revision"]:
        raise HistoricalRevisionError(
            "project_identity_source_drift", "ProjectIdentity changed during prepare"
        )
    identity_content = _json_text(advanced_identity.to_dict())
    identity_after_bytes = identity_content.encode("utf-8")
    identity_after_sha256 = hashlib.sha256(identity_after_bytes).hexdigest()
    identity_declaration = {
        "relative_path": ".novelagent/project.json",
        "role": "project_identity",
        "action": "replace",
        "after_sha256": identity_after_sha256,
        "after_size": len(identity_after_bytes),
        "book_id": identity.book_id,
        "expected_authority_epoch": epoch,
        "expected_head_event_hash": expected_head_event_hash,
        "after_authority_epoch": int(advanced_identity.authority["authority_epoch"]),
        "after_head_event_hash": str(advanced_identity.authority["head_event_hash"]),
    }
    source_revision_after = {
        "schema_version": "1.0",
        "book_id": identity.book_id,
        "root_uuid": root_uuid,
        "identity_sha256": identity_after_sha256,
        "authority_epoch": identity_declaration["after_authority_epoch"],
        "head_event_hash": identity_declaration["after_head_event_hash"],
    }

    target_roles = sorted(
        {
            "memory_event_batch",
            "memory_projection",
            "memory_snapshot_projection",
            "memory_tracking_projection",
            "memory_snapshot_projection_receipt",
            "memory_tracking_projection_receipt",
            "historical_revision_evidence",
            "historical_revision_dependency_inventory",
            "historical_revision_invalidation_manifest",
            "historical_revision_impact_report",
            "historical_revision_transaction",
            "project_identity",
        }
    )
    transaction = _build_transaction_record(
        transaction_id=tx_id,
        revision_kind=kind,
        book_id=identity.book_id,
        historical_chapter_index=chapter_index,
        historical_chapter_relative_path=relative_historical,
        patch=patch,
        batch=batch,
        source_hashes=source_hashes,
        authority_before=authority_before,
        authority_after=authority_after,
        identity_before_sha256=identity_before_sha256,
        identity_after_sha256=identity_after_sha256,
        evidence_hash=str(evidence["evidence_hash"]),
        invalidation_manifest_hash=str(invalidation_manifest["manifest_hash"]),
        impact_report_hash=str(impact_report["report_hash"]),
        reconciliation=reconciliation_record,
        target_roles=target_roles,
    )

    revision_root = memory / "history_revisions" / tx_id
    _require_within(memory, revision_root, field="revision_root")
    if _revision_artifact_exists(revision_root):
        raise HistoricalRevisionError(
            "historical_revision_artifact_collision", "immutable transaction directory already exists"
        )
    selected_projection_root = (
        Path(projection_root).resolve() if projection_root is not None else memory / "projections"
    )
    _require_within(memory, selected_projection_root, field="projection_root")
    projection_targets, projection_receipts = _projection_targets(
        projection_root=selected_projection_root,
        artifacts=rebuild_memory_projections(updated),
    )
    batch_path = event_store / "batches" / f"{batch['batch_id']}.json"
    if batch_path.exists():
        raise HistoricalRevisionError(
            "historical_revision_batch_collision", "immutable batch path already exists"
        )
    targets = [
        _json_target("memory_event_batch", batch_path, batch),
        _json_target("memory_projection", canonical_path, updated),
        *projection_targets,
        _json_target("historical_revision_evidence", revision_root / "evidence.json", evidence),
        _json_target(
            "historical_revision_dependency_inventory",
            revision_root / "dependency_inventory.json",
            inventory,
        ),
        _json_target(
            "historical_revision_invalidation_manifest",
            revision_root / "invalidations.json",
            invalidation_manifest,
        ),
        _json_target(
            "historical_revision_impact_report",
            revision_root / "impact_report.json",
            impact_report,
        ),
        _json_target(
            "historical_revision_transaction",
            revision_root / "transaction.json",
            transaction,
        ),
        _text_target(
            "project_identity",
            identity_path,
            identity_content,
        ),
    ]
    if any(target["kind"] == "prose" or Path(target["path"]).resolve() == historical_path for target in targets):
        raise HistoricalRevisionError(
            "published_prose_in_place_edit_forbidden", "historical revision attempted a prose write"
        )

    verify_story_project_read_set(
        read_set,
        declared_writes=[identity_declaration],
        phase="prepare",
    )
    prepared = {
        "status": "prepared",
        "revision_kind": kind,
        "transaction_id": tx_id,
        "historical_chapter_relative_path": relative_historical,
        "targets": targets,
        "batch": copy.deepcopy(batch),
        "base_projection": copy.deepcopy(base),
        "projection": copy.deepcopy(updated),
        "anchor_batch_source": copy.deepcopy(anchor),
        "projection_receipts": projection_receipts,
        "evidence": evidence,
        "dependency_inventory": inventory,
        "invalidation_manifest": invalidation_manifest,
        "impact_report": impact_report,
        "transaction": transaction,
        "project_identity_before": identity.to_dict(),
        "project_identity_after": advanced_identity.to_dict(),
        "story_project_read_set": read_set,
        "read_set_declared_writes": [identity_declaration],
        "story_project_source_revision_after": source_revision_after,
        "audit": {
            "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
            "reducer_version": CURRENT_REDUCER_VERSION,
            "batch_hash": batch["batch_hash"],
            "previous_head_event_hash": expected_head_event_hash,
            "head_event_hash": authority_after["head_event_hash"],
            "previous_revision": revision,
            "revision": authority_after["revision"],
            "projection_hash": memory_projection_hash(updated),
            "impact_report_hash": impact_report["report_hash"],
            "transaction_hash": transaction["transaction_hash"],
        },
    }
    validate_historical_revision_bundle(prepared)
    return prepared


def validate_historical_revision_evidence(value: Any) -> dict[str, Any]:
    validated = _schema(value, "historical_revision_evidence.schema.json")
    _common_revision_identity(validated)
    for field in (
        "historical_chapter_sha256",
        "revision_source_sha256",
        "revision_evidence_text_sha256",
        "evidence_hash",
    ):
        _require_sha256(field, validated[field])
    text = str(validated["revision_evidence_text"])
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != validated["revision_evidence_text_sha256"]:
        raise HistoricalRevisionError("evidence_text_hash_mismatch", "revision evidence text hash differs")
    if validated["revision_source_sha256"] != validated["revision_evidence_text_sha256"]:
        raise HistoricalRevisionError(
            "evidence_source_hash_mismatch",
            "revision source bytes and embedded UTF-8 evidence differ",
        )
    _verify_spans(text, validated["evidence_spans"])
    _verify_self_hash(validated, "evidence_hash")
    return validated


def validate_historical_revision_dependency_inventory(value: Any) -> dict[str, Any]:
    validated = _schema(value, "historical_revision_dependency_inventory.schema.json")
    if validated["schema_version"] != HISTORICAL_REVISION_SCHEMA_VERSION:
        raise HistoricalRevisionError(
            "unknown_historical_revision_schema", "dependency inventory schema is unsupported"
        )
    if validated["inventory_kind"] != "complete_downstream_dependencies":
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "dependency inventory kind is unsupported",
        )
    _required_text(validated, "book_id")
    _positive_int("authority_epoch", validated["authority_epoch"])
    _require_sha256("head_event_hash", validated["head_event_hash"])
    historical = _positive_int(
        "historical_chapter_index", validated["historical_chapter_index"]
    )
    canonical_next = _positive_int(
        "canonical_next_chapter_index", validated["canonical_next_chapter_index"]
    )
    if canonical_next <= historical:
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "historical chapter must precede the canonical next chapter",
        )
    if (
        validated["outline_inventory_complete"] is not True
        or validated["session_inventory_complete"] is not True
    ):
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_incomplete",
            "outline and session inventories must be explicitly complete",
        )

    outline_ids: list[str] = []
    for item in validated["outline_dependencies"]:
        record = _dependency_object(item, "outline dependency")
        outline_ids.append(_required_text(record, "outline_id"))
        _positive_int("outline.chapter_index", record["chapter_index"])
        _require_sha256("outline.artifact_sha256", record["artifact_sha256"])
        _positive_int("outline.authority_epoch", record["authority_epoch"])
        _require_sha256("outline.head_event_hash", record["head_event_hash"])
    session_ids: list[str] = []
    for item in validated["session_dependencies"]:
        record = _dependency_object(item, "session dependency")
        session_ids.append(_required_text(record, "session_id"))
        first = _positive_int("session.first_chapter_index", record["first_chapter_index"])
        last = _positive_int("session.last_chapter_index", record["last_chapter_index"])
        if last < first:
            raise HistoricalRevisionError(
                "historical_revision_dependency_inventory_invalid",
                "session dependency chapter bounds are reversed",
            )
        _require_sha256("session.artifact_sha256", record["artifact_sha256"])
        _positive_int("session.authority_epoch", record["authority_epoch"])
        _require_sha256("session.head_event_hash", record["head_event_hash"])
        _required_text(record, "status")
    if len(outline_ids) != len(set(outline_ids)) or outline_ids != sorted(outline_ids):
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "outline dependencies must have unique, sorted IDs",
        )
    if len(session_ids) != len(set(session_ids)) or session_ids != sorted(session_ids):
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            "session dependencies must have unique, sorted IDs",
        )
    _require_sha256("inventory_hash", validated["inventory_hash"])
    _verify_self_hash(validated, "inventory_hash")
    return validated


def validate_historical_revision_invalidation_manifest(value: Any) -> dict[str, Any]:
    validated = _schema(value, "historical_revision_invalidation_manifest.schema.json")
    _common_revision_identity(validated)
    _validate_authority_binding(validated["authority_before"], "authority_before")
    _validate_authority_binding(validated["authority_after"], "authority_after")
    _validate_authority_transition(validated["authority_before"], validated["authority_after"])
    for field in ("impact_basis_hash", "batch_hash", "evidence_hash", "manifest_hash"):
        _require_sha256(field, validated[field])
    _validate_invalidation_entries(validated)
    _validate_reconciliation_record(validated["reconciliation"])
    _verify_self_hash(validated, "manifest_hash")
    return validated


def validate_historical_revision_impact_report(value: Any) -> dict[str, Any]:
    validated = _schema(value, "historical_revision_impact_report.schema.json")
    _common_revision_identity(validated)
    _validate_source_hashes(validated["source_hashes"])
    _validate_anchor(validated["anchor_batch"])
    _validate_authority_binding(validated["authority_before"], "authority_before")
    _validate_authority_binding(validated["authority_after"], "authority_after")
    _validate_authority_transition(validated["authority_before"], validated["authority_after"])
    for field in (
        "impact_basis_hash",
        "batch_hash",
        "evidence_hash",
        "invalidation_manifest_hash",
        "report_hash",
    ):
        _require_sha256(field, validated[field])
    _verify_self_hash(validated, "report_hash")
    _validate_reconciliation_record(validated["reconciliation"])
    return validated


def validate_historical_revision_transaction(value: Any) -> dict[str, Any]:
    validated = _schema(value, "historical_revision_transaction.schema.json")
    _common_revision_identity(validated)
    if validated["reducer_version"] != CURRENT_REDUCER_VERSION:
        raise HistoricalRevisionError("unknown_reducer", "transaction reducer is unsupported")
    _validate_source_hashes(validated["source_hashes"])
    _validate_authority_binding(validated["authority_before"], "authority_before")
    _validate_authority_binding(validated["authority_after"], "authority_after")
    _validate_authority_transition(validated["authority_before"], validated["authority_after"])
    for field in (
        "patch_content_hash",
        "batch_hash",
        "identity_before_sha256",
        "identity_after_sha256",
        "evidence_hash",
        "invalidation_manifest_hash",
        "impact_report_hash",
        "transaction_hash",
    ):
        _require_sha256(field, validated[field])
    forbidden = _FORBIDDEN_TARGET_ROLES.intersection(validated["target_roles"])
    if forbidden:
        raise HistoricalRevisionError(
            "published_prose_in_place_edit_forbidden",
            f"transaction contains forbidden target roles: {sorted(forbidden)}",
        )
    _verify_self_hash(validated, "transaction_hash")
    _validate_reconciliation_record(validated["reconciliation"])
    return validated


def validate_historical_revision_bundle(value: Any) -> dict[str, Any]:
    """Cross-check all immutable records and authority CAS values as one bundle."""

    if not isinstance(value, dict):
        raise HistoricalRevisionError("historical_revision_bundle_invalid", "bundle must be an object")
    required = {
        "batch",
        "base_projection",
        "projection",
        "anchor_batch_source",
        "evidence",
        "dependency_inventory",
        "invalidation_manifest",
        "impact_report",
        "transaction",
        "targets",
        "project_identity_before",
        "project_identity_after",
        "story_project_read_set",
        "historical_chapter_relative_path",
    }
    if not required.issubset(value):
        raise HistoricalRevisionError("historical_revision_bundle_invalid", "bundle records are missing")
    batch = validate_memory_event_batch(value["batch"])
    base_projection = validate_canonical_memory(value["base_projection"])
    projection = validate_canonical_memory(value["projection"])
    anchor_batch_source = validate_memory_event_batch(value["anchor_batch_source"])
    evidence = validate_historical_revision_evidence(value["evidence"])
    inventory = validate_historical_revision_dependency_inventory(
        value["dependency_inventory"]
    )
    manifest = validate_historical_revision_invalidation_manifest(value["invalidation_manifest"])
    report = validate_historical_revision_impact_report(value["impact_report"])
    transaction = validate_historical_revision_transaction(value["transaction"])
    identity_before = validate_project_identity(value["project_identity_before"])
    identity_after = validate_project_identity(value["project_identity_after"])
    targets = value["targets"]
    if not isinstance(targets, list) or not targets or any(not isinstance(item, dict) for item in targets):
        raise HistoricalRevisionError("historical_revision_bundle_invalid", "targets must be objects")
    target_roles = {str(item.get("kind") or "") for item in targets}
    if target_roles != set(transaction["target_roles"]):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "transaction target roles are not exact"
        )
    if _FORBIDDEN_TARGET_ROLES.intersection(target_roles):
        raise HistoricalRevisionError(
            "published_prose_in_place_edit_forbidden", "bundle contains a published prose target"
        )
    identity_targets = [item for item in targets if item.get("kind") == "project_identity"]
    if len(identity_targets) != 1:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "bundle requires exactly one identity target"
        )
    identity_target = identity_targets[0]
    identity_content = identity_target.get("content")
    if not isinstance(identity_content, str):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "identity target content must be UTF-8 text"
        )
    if (
        identity_target.get("expected_before_exists") is not True
        or identity_target.get("expected_before_sha256") != transaction["identity_before_sha256"]
        or hashlib.sha256(identity_content.encode("utf-8")).hexdigest()
        != transaction["identity_after_sha256"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "identity target CAS hashes differ"
        )

    common = {
        "transaction_id": transaction["transaction_id"],
        "revision_kind": transaction["revision_kind"],
        "book_id": transaction["book_id"],
        "historical_chapter_index": transaction["historical_chapter_index"],
        "historical_chapter_relative_path": transaction[
            "historical_chapter_relative_path"
        ],
    }
    for record in (evidence, manifest, report):
        if any(record[field] != expected for field, expected in common.items()):
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", "immutable records identify different revisions"
            )
    if value["historical_chapter_relative_path"] != common[
        "historical_chapter_relative_path"
    ]:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "historical chapter path binding differs"
        )
    read_set = value["story_project_read_set"]
    if not isinstance(read_set, Mapping):
        raise HistoricalRevisionError(
            "historical_revision_bundle_invalid", "StoryProject read-set is missing"
        )
    story_root = Path(read_set["root_identity"]["resolved_path"]).resolve()
    try:
        verified_anchor, _, verified_anchor_evidence = _historical_anchor(
            [anchor_batch_source],
            int(common["historical_chapter_index"]),
            story_root=story_root,
            projection=base_projection,
            historical_relative_path=str(common["historical_chapter_relative_path"]),
            historical_chapter_sha256=str(
                transaction["source_hashes"]["historical_chapter_sha256"]
            ),
        )
        verified_anchor_binding = _anchor_binding(
            verified_anchor, anchor_evidence=verified_anchor_evidence
        )
    except Exception as exc:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", f"historical anchor verification failed: {exc}"
        ) from exc
    if verified_anchor_binding != report["anchor_batch"]:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "historical anchor binding differs"
        )
    if batch["batch_kind"] != common["revision_kind"] or batch["book_id"] != common["book_id"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "batch identity differs")
    if batch["batch_id"] != transaction["batch_id"] or batch["batch_hash"] != transaction["batch_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "batch hash differs")
    if memory_patch_content_hash(batch["patch"]) != transaction["patch_content_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "patch hash differs")
    if evidence["evidence_hash"] != transaction["evidence_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "evidence hash differs")
    if manifest["manifest_hash"] != transaction["invalidation_manifest_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "manifest hash differs")
    if report["report_hash"] != transaction["impact_report_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "impact report hash differs")
    if (
        manifest["batch_hash"] != batch["batch_hash"]
        or report["batch_hash"] != batch["batch_hash"]
        or manifest["evidence_hash"] != evidence["evidence_hash"]
        or report["evidence_hash"] != evidence["evidence_hash"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "batch/evidence record links differ"
        )
    if report["invalidation_manifest_hash"] != manifest["manifest_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "impact report points elsewhere")
    if not (
        manifest["reconciliation"]
        == report["reconciliation"]
        == transaction["reconciliation"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "reconciliation bindings differ"
        )
    if report["source_hashes"] != transaction["source_hashes"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "source hashes differ")
    if (
        inventory["inventory_hash"]
        != transaction["source_hashes"]["dependency_inventory_hash"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "dependency inventory hash differs"
        )
    source_hashes = transaction["source_hashes"]
    if (
        evidence["historical_chapter_sha256"]
        != source_hashes["historical_chapter_sha256"]
        or evidence["revision_source_sha256"]
        != source_hashes["revision_source_sha256"]
        or report["anchor_batch"]["historical_chapter_sha256"]
        != source_hashes["historical_chapter_sha256"]
        or batch["source_project_digest"] != source_hashes["source_project_digest"]
        or batch["context_digest"] != source_hashes["context_digest"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "evidence/source/read-set bindings differ"
        )
    if manifest["authority_before"] != report["authority_before"] or report["authority_before"] != transaction["authority_before"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "before authority differs")
    if manifest["authority_after"] != report["authority_after"] or report["authority_after"] != transaction["authority_after"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "after authority differs")
    after = transaction["authority_after"]
    before = transaction["authority_before"]
    if (
        base_projection["book_id"] != common["book_id"]
        or base_projection["authority_epoch"] != before["authority_epoch"]
        or base_projection["revision"] != before["revision"]
        or base_projection["head_event_hash"] != before["head_event_hash"]
        or batch["expected_revision"] != before["revision"]
        or batch["first_revision"] != before["revision"] + 1
        or batch["last_revision"] != after["revision"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "base authority/event revisions differ"
        )
    first_event = batch["events"][0]
    if (
        first_event["precondition"]["expected_revision"] != before["revision"]
        or first_event["precondition"]["expected_head_event_hash"]
        != before["head_event_hash"]
        or batch["events"][-1]["event_hash"] != after["head_event_hash"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "event authority precondition/head differs"
        )
    try:
        replayed_projection = apply_memory_events(
            base_projection,
            batch["events"],
            reducer_version=CURRENT_REDUCER_VERSION,
        )
    except Exception as exc:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", f"prepared event replay failed: {exc}"
        ) from exc
    if replayed_projection != projection:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "prepared events do not reproduce projection"
        )
    if (
        projection["book_id"] != common["book_id"]
        or projection["authority_epoch"] != after["authority_epoch"]
        or projection["revision"] != after["revision"]
        or projection["head_event_hash"] != after["head_event_hash"]
    ):
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "projection authority differs")
    before_authority = identity_before.authority or {}
    after_authority = identity_after.authority or {}
    if (
        identity_before.book_id != common["book_id"]
        or identity_after.book_id != common["book_id"]
        or before_authority.get("authority_epoch") != transaction["authority_before"]["authority_epoch"]
        or before_authority.get("head_event_hash") != transaction["authority_before"]["head_event_hash"]
        or after_authority.get("authority_epoch") != after["authority_epoch"]
        or after_authority.get("head_event_hash") != after["head_event_hash"]
    ):
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "ProjectIdentity CAS differs")

    impact_basis = {
        "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
        **common,
        "source_hashes": report["source_hashes"],
        "authority_before": report["authority_before"],
        "anchor_batch": report["anchor_batch"],
        "event_invalidations": manifest["event_invalidations"],
        "outline_invalidations": manifest["outline_invalidations"],
        "session_invalidations": manifest["session_invalidations"],
        "reconciliation": report["reconciliation"],
    }
    if canonical_json_hash(impact_basis) != report["impact_basis_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "impact basis hash differs")
    if manifest["impact_basis_hash"] != report["impact_basis_hash"]:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "manifest impact basis differs")
    event_invalidations = manifest["event_invalidations"]
    first_revisions = [int(item["first_revision"]) for item in event_invalidations]
    last_revisions = [int(item["last_revision"]) for item in event_invalidations]
    expected_summary = {
        "event_count": len(event_invalidations),
        "outline_count": len(manifest["outline_invalidations"]),
        "session_count": len(manifest["session_invalidations"]),
        "earliest_invalid_revision": min(first_revisions) if first_revisions else None,
        "latest_invalid_revision": max(last_revisions) if last_revisions else None,
    }
    if report["summary"] != expected_summary:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "impact summary differs from invalidations"
        )
    patch_metadata = batch["patch"].get("metadata", {})
    expected_patch_metadata = {
        "created_by": MEMORY_PATCH_CREATED_BY,
        "historical_revision": True,
        "transaction_id": common["transaction_id"],
        "historical_chapter_index": common["historical_chapter_index"],
        "historical_chapter_relative_path": common["historical_chapter_relative_path"],
        "historical_chapter_sha256": source_hashes["historical_chapter_sha256"],
        "revision_source_sha256": source_hashes["revision_source_sha256"],
        "impact_basis_hash": report["impact_basis_hash"],
        "dependency_inventory_hash": source_hashes["dependency_inventory_hash"],
    }
    if patch_metadata != expected_patch_metadata:
        raise HistoricalRevisionError("historical_revision_bundle_mismatch", "batch impact basis differs")
    if any(
        event["chapter_body_sha256"] != evidence["revision_evidence_text_sha256"]
        or event["evidence_spans"] != evidence["evidence_spans"]
        for event in batch["events"]
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "event evidence differs from immutable evidence"
        )
    current_state = projection.get("current_state")
    marker = current_state.get("historical_revision") if isinstance(current_state, dict) else None
    expected_marker = {
        "transaction_id": common["transaction_id"],
        "revision_kind": common["revision_kind"],
        "historical_chapter_index": common["historical_chapter_index"],
        "impact_basis_hash": report["impact_basis_hash"],
        "dependency_inventory_hash": report["source_hashes"][
            "dependency_inventory_hash"
        ],
        "invalidated_event_batch_ids": [
            item["batch_id"] for item in manifest["event_invalidations"]
        ],
        "invalidated_outline_ids": [
            item["outline_id"] for item in manifest["outline_invalidations"]
        ],
        "invalidated_session_ids": [
            item["session_id"] for item in manifest["session_invalidations"]
        ],
        "requires_downstream_reconciliation": bool(
            manifest["event_invalidations"]
            or manifest["outline_invalidations"]
            or manifest["session_invalidations"]
        )
        and report["reconciliation"] is None,
        "reconciliation": copy.deepcopy(report["reconciliation"]),
    }
    if marker != expected_marker:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "canonical invalidation marker differs"
        )
    _validate_prepared_targets(
        value,
        batch=batch,
        projection=projection,
        evidence=evidence,
        inventory=inventory,
        manifest=manifest,
        report=report,
        transaction=transaction,
        identity_after=identity_after.to_dict(),
    )
    return value


_FORBIDDEN_TARGET_ROLES = frozenset({"prose", "chapter_prose", "published_chapter"})


def _validate_prepared_targets(
    bundle: Mapping[str, Any],
    *,
    batch: Mapping[str, Any],
    projection: Mapping[str, Any],
    evidence: Mapping[str, Any],
    inventory: Mapping[str, Any],
    manifest: Mapping[str, Any],
    report: Mapping[str, Any],
    transaction: Mapping[str, Any],
    identity_after: Mapping[str, Any],
) -> None:
    targets = bundle["targets"]
    exact_fields = {
        "kind",
        "path",
        "content",
        "expected_before_exists",
        "expected_before_sha256",
    }
    resolved_paths: list[Path] = []
    by_role: dict[str, list[dict[str, Any]]] = {}
    for raw in targets:
        if set(raw) != exact_fields:
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", "target fields are not exact"
            )
        role = _required_text(raw, "kind")
        path = raw["path"]
        content = raw["content"]
        before_exists = raw["expected_before_exists"]
        before_sha = raw["expected_before_sha256"]
        if not isinstance(path, Path) or not path.is_absolute():
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", "target path must be an absolute Path"
            )
        if not isinstance(content, str) or not isinstance(before_exists, bool):
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", "target content/CAS state is invalid"
            )
        if before_exists:
            _require_sha256("target.expected_before_sha256", before_sha)
        elif before_sha is not None:
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", "absent target has a before hash"
            )
        resolved_paths.append(path.resolve())
        by_role.setdefault(role, []).append(raw)
    if len(resolved_paths) != len(set(resolved_paths)):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "target paths are not unique"
        )

    singleton_roles = {
        "memory_event_batch",
        "memory_projection",
        "memory_snapshot_projection",
        "memory_snapshot_projection_receipt",
        "memory_tracking_projection_receipt",
        "historical_revision_evidence",
        "historical_revision_dependency_inventory",
        "historical_revision_invalidation_manifest",
        "historical_revision_impact_report",
        "historical_revision_transaction",
        "project_identity",
    }
    if any(len(by_role.get(role, [])) != 1 for role in singleton_roles):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "singleton target count differs"
        )
    if not by_role.get("memory_tracking_projection"):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "tracking projection targets are missing"
        )
    if set(by_role) != set(transaction["target_roles"]):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "target role inventory differs"
        )

    canonical_target = by_role["memory_projection"][0]
    memory_root = Path(canonical_target["path"]).parent.resolve()
    if Path(canonical_target["path"]).resolve() != memory_root / "canonical_memory.json":
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "canonical target path differs"
        )
    tx_id = str(transaction["transaction_id"])
    batch_path = memory_root / "events" / "batches" / f"{batch['batch_id']}.json"
    revision_root = memory_root / "history_revisions" / tx_id
    exact_json_targets = {
        "memory_event_batch": (batch_path, batch),
        "memory_projection": (memory_root / "canonical_memory.json", projection),
        "historical_revision_evidence": (revision_root / "evidence.json", evidence),
        "historical_revision_dependency_inventory": (
            revision_root / "dependency_inventory.json",
            inventory,
        ),
        "historical_revision_invalidation_manifest": (
            revision_root / "invalidations.json",
            manifest,
        ),
        "historical_revision_impact_report": (
            revision_root / "impact_report.json",
            report,
        ),
        "historical_revision_transaction": (
            revision_root / "transaction.json",
            transaction,
        ),
    }
    for role, (expected_path, payload) in exact_json_targets.items():
        target = by_role[role][0]
        if Path(target["path"]).resolve() != expected_path.resolve():
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", f"{role} target path differs"
            )
        if _target_json(target) != dict(payload):
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", f"{role} target content differs"
            )
    for role in (
        "memory_event_batch",
        "historical_revision_evidence",
        "historical_revision_dependency_inventory",
        "historical_revision_invalidation_manifest",
        "historical_revision_impact_report",
        "historical_revision_transaction",
    ):
        target = by_role[role][0]
        if target["expected_before_exists"] or target["expected_before_sha256"] is not None:
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", f"immutable {role} target already exists"
            )

    projection_artifacts = rebuild_memory_projections(dict(projection))
    snapshot_target = by_role["memory_snapshot_projection"][0]
    projection_root = Path(snapshot_target["path"]).parent.resolve()
    if _target_json(snapshot_target) != projection_artifacts["snapshot"]:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "snapshot projection content differs"
        )
    tracking_expected = {
        (projection_root / Path(relative)).resolve(): content
        for relative, content in projection_artifacts["tracking"].items()
    }
    tracking_actual = {
        Path(target["path"]).resolve(): target["content"]
        for target in by_role["memory_tracking_projection"]
    }
    if tracking_actual != tracking_expected:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "tracking projection targets differ"
        )
    receipt_targets = {
        "memory_snapshot_projection_receipt": (
            projection_root / "receipts" / "snapshot.json",
            projection_artifacts["snapshot_receipt"],
        ),
        "memory_tracking_projection_receipt": (
            projection_root / "receipts" / "tracking.json",
            projection_artifacts["tracking_receipt"],
        ),
    }
    for role, (expected_path, payload) in receipt_targets.items():
        target = by_role[role][0]
        if (
            Path(target["path"]).resolve() != expected_path.resolve()
            or _target_json(target) != payload
        ):
            raise HistoricalRevisionError(
                "historical_revision_bundle_mismatch", f"{role} target differs"
            )

    read_set = bundle.get("story_project_read_set")
    if not isinstance(read_set, Mapping):
        raise HistoricalRevisionError(
            "historical_revision_bundle_invalid", "StoryProject read-set is missing"
        )
    story_root = Path(read_set["root_identity"]["resolved_path"]).resolve()
    identity_target = by_role["project_identity"][0]
    if (
        Path(identity_target["path"]).resolve()
        != (story_root / ".novelagent" / "project.json").resolve()
        or _target_json(identity_target) != dict(identity_after)
    ):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "ProjectIdentity target path/content differs"
        )
    historical_relative = bundle.get("historical_chapter_relative_path")
    if isinstance(historical_relative, str):
        historical_target = (story_root / Path(historical_relative)).resolve()
        if historical_target in resolved_paths:
            raise HistoricalRevisionError(
                "published_prose_in_place_edit_forbidden", "bundle targets published prose"
            )


def _target_json(target: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(target["content"]))
    except json.JSONDecodeError as exc:
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "JSON target content is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise HistoricalRevisionError(
            "historical_revision_bundle_mismatch", "JSON target content must be an object"
        )
    return payload


def _verified_authority_base(
    *,
    event_store: Path,
    canonical_path: Path,
    identity: ProjectIdentity,
    authority_epoch: int,
    expected_head_event_hash: str,
    expected_revision: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not canonical_path.is_file():
        raise HistoricalRevisionError("canonical_memory_missing", "canonical Memory 2.2 cache is required")
    replay = replay_memory_events(event_store)
    base = copy.deepcopy(load_canonical_memory(canonical_path))
    projection = replay.get("projection")
    if replay.get("schema_version") != "2.2" or base.get("schema_version") != "2.2":
        raise HistoricalRevisionError("memory_schema_mismatch", "historical revision requires Memory 2.2")
    if replay.get("reducer_version") != CURRENT_REDUCER_VERSION:
        raise HistoricalRevisionError("unknown_reducer", "Memory replay reducer is unsupported")
    if projection != base or replay.get("projection_hash") != memory_projection_hash(base):
        raise HistoricalRevisionError(
            "canonical_replay_drift", "canonical cache differs from authoritative replay"
        )
    if str(base.get("book_id")) != identity.book_id:
        raise HistoricalRevisionError("book_id_mismatch", "Memory belongs to another book")
    if int(base.get("authority_epoch") or 0) != authority_epoch:
        raise HistoricalRevisionError("stale_authority_epoch", "canonical authority epoch changed")
    if base.get("head_event_hash") != expected_head_event_hash:
        raise HistoricalRevisionError("stale_authority_head", "canonical event head changed")
    if int(base.get("revision") or 0) != expected_revision:
        raise HistoricalRevisionError("stale_memory_revision", "canonical revision changed")
    return copy.deepcopy(replay), base


def _historical_anchor(
    batches: Sequence[Mapping[str, Any]],
    chapter_index: int,
    *,
    story_root: Path,
    projection: Mapping[str, Any],
    historical_relative_path: str,
    historical_chapter_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    expected_path = f"chapter:{chapter_index}"
    matches = [
        (index, dict(batch))
        for index, batch in enumerate(batches)
        if batch.get("batch_kind") == "chapter"
        and (batch.get("patch") or {}).get("source", {}).get("path") == expected_path
    ]
    if len(matches) == 1:
        index, anchor = matches[0]
        historical_path = story_root / Path(historical_relative_path)
        try:
            historical_text = historical_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HistoricalRevisionError(
                "historical_chapter_encoding_invalid", "published prose must be UTF-8"
            ) from exc
        published_evidence_sha256 = hashlib.sha256(
            historical_text.encode("utf-8")
        ).hexdigest()
        evidence_hashes = {
            str(event.get("chapter_body_sha256")) for event in anchor.get("events", [])
        }
        if evidence_hashes != {published_evidence_sha256}:
            raise HistoricalRevisionError(
                "historical_chapter_published_evidence_drift",
                "published prose no longer matches its immutable chapter event evidence",
            )
        evidence = {
            "evidence_kind": "chapter_event",
            "historical_chapter_sha256": historical_chapter_sha256,
            "published_evidence_sha256": published_evidence_sha256,
            "source_record_hash": canonical_json_hash(
                {
                    "batch_hash": anchor["batch_hash"],
                    "chapter_index": chapter_index,
                    "historical_chapter_sha256": historical_chapter_sha256,
                    "chapter_body_sha256": published_evidence_sha256,
                }
            ),
        }
        return anchor, [dict(item) for item in batches[index + 1 :]], evidence
    if matches:
        raise HistoricalRevisionError(
            "historical_chapter_event_anchor_invalid",
            "published chapter maps to multiple committed chapter batches",
        )

    current_state = projection.get("current_state")
    migration = current_state.get("migration_baseline") if isinstance(current_state, Mapping) else None
    if not isinstance(migration, Mapping) or migration.get("history_policy") != "source_sync_only":
        raise HistoricalRevisionError(
            "historical_chapter_event_anchor_invalid",
            "published chapter has neither an immutable chapter event nor a migration source snapshot",
        )
    plan_hash = _require_sha256("migration_baseline.plan_hash", migration.get("plan_hash"))
    # Keep MigrationPlan validation lazy: importing migration_v2 must remain a
    # clean, read-only entry point and must not recurse through memory_v2.__init__.
    from core.story_project.migration_v2 import validate_migration_plan

    plans_root = story_root / ".novelagent" / "migration-v2" / "artifacts" / "plans"
    plans: list[dict[str, Any]] = []
    if plans_root.is_dir():
        for path in sorted(plans_root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                plan = validate_migration_plan(raw)
            except Exception as exc:
                raise HistoricalRevisionError(
                    "migration_source_snapshot_invalid", f"cannot validate {path.name}: {exc}"
                ) from exc
            if plan.get("plan_hash") == plan_hash:
                plans.append(plan)
    if len(plans) != 1:
        raise HistoricalRevisionError(
            "migration_source_snapshot_invalid",
            "migration baseline must resolve to exactly one immutable MigrationPlan",
        )
    source_matches = [
        item
        for item in plans[0]["sources"]
        if item.get("role") == "published_prose"
        and item.get("chapter_index") == chapter_index
    ]
    if len(source_matches) != 1:
        raise HistoricalRevisionError(
            "migration_source_snapshot_invalid",
            "MigrationPlan has no unique source record for the historical chapter",
        )
    source = source_matches[0]
    if (
        source.get("relative_path") != historical_relative_path
        or source.get("sha256") != historical_chapter_sha256
    ):
        raise HistoricalRevisionError(
            "historical_chapter_published_evidence_drift",
            "published prose differs from the approved migration source snapshot",
        )
    baseline_matches = [
        (index, dict(batch))
        for index, batch in enumerate(batches)
        if batch.get("batch_kind") == "source_sync"
        and (batch.get("patch") or {}).get("metadata", {}).get("plan_hash") == plan_hash
        and (batch.get("patch") or {}).get("metadata", {}).get("history_policy")
        == "source_sync_only"
    ]
    if len(baseline_matches) != 1:
        raise HistoricalRevisionError(
            "migration_source_snapshot_invalid", "migration source_sync anchor is missing or ambiguous"
        )
    index, anchor = baseline_matches[0]
    evidence = {
        "evidence_kind": "migration_source_snapshot",
        "historical_chapter_sha256": historical_chapter_sha256,
        "published_evidence_sha256": historical_chapter_sha256,
        "source_record_hash": canonical_json_hash(source),
    }
    return anchor, [dict(item) for item in batches[index + 1 :]], evidence


def _canonical_next_chapter(
    projection: Mapping[str, Any], batches: Sequence[Mapping[str, Any]]
) -> int:
    current_state = projection.get("current_state")
    if isinstance(current_state, Mapping):
        candidate = current_state.get("chapter_index")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 1:
            return candidate
    committed: list[int] = []
    for batch in batches:
        if batch.get("batch_kind") != "chapter":
            continue
        source_path = (batch.get("patch") or {}).get("source", {}).get("path")
        if not isinstance(source_path, str) or not source_path.startswith("chapter:"):
            raise HistoricalRevisionError(
                "canonical_next_chapter_unavailable",
                "committed chapter batch has no canonical chapter source binding",
            )
        try:
            chapter = int(source_path.partition(":")[2])
        except ValueError as exc:
            raise HistoricalRevisionError(
                "canonical_next_chapter_unavailable", "chapter source binding is invalid"
            ) from exc
        if chapter < 1:
            raise HistoricalRevisionError(
                "canonical_next_chapter_unavailable", "chapter source binding is invalid"
            )
        committed.append(chapter)
    if not committed:
        return 1
    expected = list(range(1, max(committed) + 1))
    if sorted(committed) != expected:
        raise HistoricalRevisionError(
            "canonical_next_chapter_unavailable", "committed chapter history is not contiguous"
        )
    return max(committed) + 1


def _event_invalidations(batches: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "batch_id": str(batch["batch_id"]),
            "batch_hash": str(batch["batch_hash"]),
            "batch_kind": str(batch["batch_kind"]),
            "first_revision": int(batch["first_revision"]),
            "last_revision": int(batch["last_revision"]),
            "reason": "historical_revision_invalidates_downstream_event",
        }
        for batch in batches
    ]


def _outline_invalidations(
    values: Sequence[Mapping[str, Any]], historical_chapter_index: int
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        item = _dependency_object(raw, "outline dependency")
        outline_id = _required_text(item, "outline_id")
        if outline_id in seen:
            raise HistoricalRevisionError("duplicate_outline_dependency", outline_id)
        seen.add(outline_id)
        chapter = _positive_int("outline.chapter_index", item.get("chapter_index"))
        digest = _require_sha256("outline.artifact_sha256", item.get("artifact_sha256"))
        epoch = _positive_int("outline.authority_epoch", item.get("authority_epoch"))
        head = _require_sha256("outline.head_event_hash", item.get("head_event_hash"))
        if chapter > historical_chapter_index:
            normalized.append(
                {
                    "outline_id": outline_id,
                    "chapter_index": chapter,
                    "artifact_sha256": digest,
                    "authority_epoch": epoch,
                    "head_event_hash": head,
                    "reason": "historical_revision_invalidates_downstream_outline",
                }
            )
    return sorted(normalized, key=lambda item: (item["chapter_index"], item["outline_id"]))


def _session_invalidations(
    values: Sequence[Mapping[str, Any]], historical_chapter_index: int
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        item = _dependency_object(raw, "session dependency")
        session_id = _required_text(item, "session_id")
        if session_id in seen:
            raise HistoricalRevisionError("duplicate_session_dependency", session_id)
        seen.add(session_id)
        first = _positive_int("session.first_chapter_index", item.get("first_chapter_index"))
        last = _positive_int("session.last_chapter_index", item.get("last_chapter_index"))
        if last < first:
            raise HistoricalRevisionError("session_dependency_invalid", "last chapter precedes first")
        digest = _require_sha256("session.artifact_sha256", item.get("artifact_sha256"))
        epoch = _positive_int("session.authority_epoch", item.get("authority_epoch"))
        head = _require_sha256("session.head_event_hash", item.get("head_event_hash"))
        status = _required_text(item, "status")
        if last > historical_chapter_index and status not in _TERMINAL_SESSION_STATES:
            normalized.append(
                {
                    "session_id": session_id,
                    "first_chapter_index": first,
                    "last_chapter_index": last,
                    "artifact_sha256": digest,
                    "authority_epoch": epoch,
                    "head_event_hash": head,
                    "status": status,
                    "reason": "historical_revision_invalidates_downstream_session",
                }
            )
    return sorted(normalized, key=lambda item: (item["first_chapter_index"], item["session_id"]))


def _bind_dependency_inventory(
    value: Mapping[str, Any],
    *,
    story_project_root: Path,
    book_id: str,
    authority_epoch: int,
    head_event_hash: str,
    historical_chapter_index: int,
    canonical_next_chapter_index: int,
) -> dict[str, Any]:
    inventory = validate_historical_revision_dependency_inventory(dict(value))
    expected = {
        "book_id": book_id,
        "authority_epoch": authority_epoch,
        "head_event_hash": head_event_hash,
        "historical_chapter_index": historical_chapter_index,
        "canonical_next_chapter_index": canonical_next_chapter_index,
    }
    if any(inventory[field] != expected_value for field, expected_value in expected.items()):
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_stale",
            "dependency inventory does not bind the current authority/read boundary",
        )
    captured = capture_historical_revision_dependency_inventory(
        story_project_root=story_project_root,
        book_id=book_id,
        authority_epoch=authority_epoch,
        head_event_hash=head_event_hash,
        historical_chapter_index=historical_chapter_index,
        canonical_next_chapter_index=canonical_next_chapter_index,
    )
    if inventory != captured:
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_mismatch",
            "declared dependency inventory differs from the recaptured durable artifacts",
        )
    return inventory


def _prepare_reconciliation(
    value: Mapping[str, Any] | None,
    *,
    current_marker: Any,
    event_invalidations: Sequence[Mapping[str, Any]],
    outline_invalidations: Sequence[Mapping[str, Any]],
    session_invalidations: Sequence[Mapping[str, Any]],
    dependency_inventory_hash: str,
    revision_source_sha256: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _dependency_object(value, "reconciliation")
    expected_fields = {
        "blocked_transaction_id",
        "blocked_impact_basis_hash",
        "resolved_event_batch_ids",
        "resolved_outline_ids",
        "resolved_session_ids",
        "resolved_dependency_inventory_hash",
    }
    if set(item) != expected_fields:
        raise HistoricalRevisionError(
            "historical_revision_reconciliation_invalid", "reconciliation fields are not exact"
        )
    if not isinstance(current_marker, Mapping) or current_marker.get(
        "requires_downstream_reconciliation"
    ) is not True:
        raise HistoricalRevisionError(
            "historical_revision_reconciliation_invalid",
            "there is no blocked historical revision to reconcile",
        )
    blocked_transaction_id = _safe_id(item.get("blocked_transaction_id"))
    blocked_impact_basis_hash = _require_sha256(
        "blocked_impact_basis_hash", item.get("blocked_impact_basis_hash")
    )
    if (
        blocked_transaction_id != current_marker.get("transaction_id")
        or blocked_impact_basis_hash != current_marker.get("impact_basis_hash")
    ):
        raise HistoricalRevisionError(
            "historical_revision_reconciliation_stale",
            "reconciliation does not bind the current blocking revision",
        )
    resolved_inventory_hash = _require_sha256(
        "resolved_dependency_inventory_hash",
        item.get("resolved_dependency_inventory_hash"),
    )
    if resolved_inventory_hash != dependency_inventory_hash:
        raise HistoricalRevisionError(
            "historical_revision_reconciliation_stale",
            "reconciliation does not bind the recaptured dependency inventory",
        )
    expected_ids = {
        "resolved_event_batch_ids": [str(entry["batch_id"]) for entry in event_invalidations],
        "resolved_outline_ids": [str(entry["outline_id"]) for entry in outline_invalidations],
        "resolved_session_ids": [str(entry["session_id"]) for entry in session_invalidations],
    }
    for field, expected in expected_ids.items():
        actual = item.get(field)
        if not isinstance(actual, list) or actual != expected or len(actual) != len(set(actual)):
            raise HistoricalRevisionError(
                "historical_revision_reconciliation_incomplete",
                f"{field} must exactly acknowledge the current invalidation inventory",
            )
    record = {
        "blocked_transaction_id": blocked_transaction_id,
        "blocked_impact_basis_hash": blocked_impact_basis_hash,
        **expected_ids,
        "resolved_dependency_inventory_hash": resolved_inventory_hash,
        "revision_source_sha256": revision_source_sha256,
    }
    record["reconciliation_hash"] = canonical_json_hash(record)
    return record


def _revision_operations(
    operations: Sequence[Mapping[str, Any]],
    *,
    transaction_id: str,
    revision_kind: str,
    historical_chapter_index: int,
    historical_chapter_relative_path: str,
    historical_chapter_sha256: str,
    revision_source_sha256: str,
    impact_basis_hash: str,
    dependency_inventory_hash: str,
    event_invalidations: Sequence[Mapping[str, Any]],
    outline_invalidations: Sequence[Mapping[str, Any]],
    session_invalidations: Sequence[Mapping[str, Any]],
    reconciliation: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)) or not operations:
        raise HistoricalRevisionError("historical_revision_empty", "operations must not be empty")
    binding = {
        "transaction_id": transaction_id,
        "revision_kind": revision_kind,
        "historical_chapter_index": historical_chapter_index,
        "historical_chapter_relative_path": historical_chapter_relative_path,
        "historical_chapter_sha256": historical_chapter_sha256,
        "revision_source_sha256": revision_source_sha256,
        "impact_basis_hash": impact_basis_hash,
        "dependency_inventory_hash": dependency_inventory_hash,
    }
    result: list[dict[str, Any]] = []
    for raw in operations:
        if not isinstance(raw, Mapping):
            raise HistoricalRevisionError("historical_revision_operation_invalid", "operation must be an object")
        operation = copy.deepcopy(dict(raw))
        op = str(operation.get("op") or "")
        if op in _FORBIDDEN_PROSE_OPS:
            raise HistoricalRevisionError(
                "published_prose_in_place_edit_forbidden", f"operation {op!r} attempts a prose write"
            )
        data = operation.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise HistoricalRevisionError("historical_revision_operation_invalid", "operation.data must be an object")
        if "historical_revision" in data:
            raise HistoricalRevisionError(
                "historical_revision_binding_reserved", "operation.data.historical_revision is reserved"
            )
        operation["data"] = {**copy.deepcopy(data), "historical_revision": copy.deepcopy(binding)}
        result.append(operation)
    result.append(
        {
            "op": "update_current_state",
            "value": {
                "historical_revision": {
                    "transaction_id": transaction_id,
                    "revision_kind": revision_kind,
                    "historical_chapter_index": historical_chapter_index,
                    "impact_basis_hash": impact_basis_hash,
                    "dependency_inventory_hash": dependency_inventory_hash,
                    "invalidated_event_batch_ids": [
                        str(item["batch_id"]) for item in event_invalidations
                    ],
                    "invalidated_outline_ids": [
                        str(item["outline_id"]) for item in outline_invalidations
                    ],
                    "invalidated_session_ids": [
                        str(item["session_id"]) for item in session_invalidations
                    ],
                    "requires_downstream_reconciliation": bool(
                        event_invalidations or outline_invalidations or session_invalidations
                    )
                    and reconciliation is None,
                    "reconciliation": copy.deepcopy(dict(reconciliation))
                    if reconciliation is not None
                    else None,
                }
            },
            "data": {"historical_revision": copy.deepcopy(binding)},
        }
    )
    return result


def _build_evidence(**values: Any) -> dict[str, Any]:
    text = str(values.pop("revision_text"))
    payload = {
        "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
        **values,
        "revision_evidence_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "revision_evidence_text": text,
    }
    payload["evidence_hash"] = canonical_json_hash(payload)
    return validate_historical_revision_evidence(payload)


def _build_invalidation_manifest(**values: Any) -> dict[str, Any]:
    payload = {"schema_version": HISTORICAL_REVISION_SCHEMA_VERSION, **copy.deepcopy(values)}
    payload["manifest_hash"] = canonical_json_hash(payload)
    return validate_historical_revision_invalidation_manifest(payload)


def _build_impact_report(
    *,
    event_invalidations: Sequence[Mapping[str, Any]],
    outline_invalidations: Sequence[Mapping[str, Any]],
    session_invalidations: Sequence[Mapping[str, Any]],
    **values: Any,
) -> dict[str, Any]:
    first_revisions = [int(item["first_revision"]) for item in event_invalidations]
    last_revisions = [int(item["last_revision"]) for item in event_invalidations]
    payload = {
        "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
        **copy.deepcopy(values),
        "summary": {
            "event_count": len(event_invalidations),
            "outline_count": len(outline_invalidations),
            "session_count": len(session_invalidations),
            "earliest_invalid_revision": min(first_revisions) if first_revisions else None,
            "latest_invalid_revision": max(last_revisions) if last_revisions else None,
        },
    }
    payload["report_hash"] = canonical_json_hash(payload)
    return validate_historical_revision_impact_report(payload)


def _build_transaction_record(*, patch: Mapping[str, Any], batch: Mapping[str, Any], **values: Any) -> dict[str, Any]:
    payload = {
        "schema_version": HISTORICAL_REVISION_SCHEMA_VERSION,
        "reducer_version": CURRENT_REDUCER_VERSION,
        "patch_id": str(patch["patch_id"]),
        "patch_content_hash": memory_patch_content_hash(dict(patch)),
        "batch_id": str(batch["batch_id"]),
        "batch_hash": str(batch["batch_hash"]),
        **copy.deepcopy(values),
    }
    payload["transaction_hash"] = canonical_json_hash(payload)
    return validate_historical_revision_transaction(payload)


def _projection_targets(
    *, projection_root: Path, artifacts: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    snapshot = artifacts.get("snapshot")
    tracking = artifacts.get("tracking")
    snapshot_receipt = artifacts.get("snapshot_receipt")
    tracking_receipt = artifacts.get("tracking_receipt")
    if not all(isinstance(item, Mapping) for item in (snapshot, tracking, snapshot_receipt, tracking_receipt)):
        raise HistoricalRevisionError("projection_invalid", "projection renderer returned invalid artifacts")
    targets = [_json_target("memory_snapshot_projection", projection_root / "snapshot.json", snapshot)]
    for relative_name in sorted(str(name) for name in tracking):
        relative = Path(relative_name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise HistoricalRevisionError("projection_path_unsafe", relative_name)
        content = tracking[relative_name]
        if not isinstance(content, str):
            raise HistoricalRevisionError("projection_invalid", "tracking projection must be text")
        targets.append(_text_target("memory_tracking_projection", projection_root / relative, content))
    targets.extend(
        [
            _json_target(
                "memory_snapshot_projection_receipt",
                projection_root / "receipts" / "snapshot.json",
                snapshot_receipt,
            ),
            _json_target(
                "memory_tracking_projection_receipt",
                projection_root / "receipts" / "tracking.json",
                tracking_receipt,
            ),
        ]
    )
    return targets, {
        "snapshot": copy.deepcopy(dict(snapshot_receipt)),
        "tracking": copy.deepcopy(dict(tracking_receipt)),
    }


def _story_relative_published_prose(
    story_root: Path,
    historical_path: Path,
    read_set: Mapping[str, Any],
    *,
    chapter_index: int,
) -> str:
    try:
        relative = historical_path.relative_to(story_root).as_posix()
    except ValueError as exc:
        raise HistoricalRevisionError(
            "historical_chapter_outside_story_project", "published chapter is outside StoryProject"
        ) from exc
    if not relative.startswith(SOURCE_DIRECTORIES[1] + "/"):
        raise HistoricalRevisionError(
            "historical_chapter_not_published_prose", "historical source is not under the prose directory"
        )
    membership = {str(item["relative_path"]): item for item in read_set["membership"]}
    if relative not in membership:
        raise HistoricalRevisionError(
            "historical_chapter_not_in_read_set", "published chapter is absent from StoryProject read-set"
        )
    resolved = resolve_prose(story_root, chapter_index)
    if resolved.path is None or resolved.path.resolve() != historical_path:
        raise HistoricalRevisionError(
            "historical_chapter_path_mismatch",
            "historical_chapter_path does not uniquely resolve to historical_chapter_index",
        )
    return relative


def _anchor_binding(
    anchor: Mapping[str, Any], *, anchor_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    binding = {
        "batch_id": str(anchor["batch_id"]),
        "batch_hash": str(anchor["batch_hash"]),
        "first_revision": int(anchor["first_revision"]),
        "last_revision": int(anchor["last_revision"]),
        **copy.deepcopy(dict(anchor_evidence)),
    }
    _validate_anchor(binding)
    return binding


def _json_target(kind: str, path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _text_target(kind, path, _json_text(payload))


def _text_target(kind: str, path: Path, content: str) -> dict[str, Any]:
    before_exists = path.is_file()
    before = path.read_bytes() if before_exists else b""
    return {
        "kind": kind,
        "path": path,
        "content": content,
        "expected_before_exists": before_exists,
        "expected_before_sha256": hashlib.sha256(before).hexdigest() if before_exists else None,
    }


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _load_inventory_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_incomplete",
            f"required dependency artifact is missing: {path.name}",
        )
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            f"cannot inspect dependency artifact: {path.name}",
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            f"dependency artifact is a link, reparse point, or non-file: {path.name}",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            f"dependency artifact is unreadable: {path.name}",
        ) from exc
    if not isinstance(value, dict):
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            f"dependency artifact must be an object: {path.name}",
        )
    return value


def _assert_inventory_directory(path: Path, label: str) -> None:
    try:
        assert_safe_local_tree(path)
    except SafePathError as exc:
        raise HistoricalRevisionError(
            "historical_revision_dependency_inventory_invalid",
            f"{label} is unsafe: {exc}",
        ) from exc


def _schema(value: Any, schema_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HistoricalRevisionError("historical_revision_schema_invalid", "value must be an object")
    try:
        return validate_schema(value, schema_name)
    except SchemaValidationError as exc:
        raise HistoricalRevisionError("historical_revision_schema_invalid", str(exc)) from exc


def _common_revision_identity(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != HISTORICAL_REVISION_SCHEMA_VERSION:
        raise HistoricalRevisionError("unknown_historical_revision_schema", "schema version is unsupported")
    _revision_kind(value.get("revision_kind"))
    _safe_id(value.get("transaction_id"))
    _required_text(value, "book_id")
    _positive_int("historical_chapter_index", value.get("historical_chapter_index"))
    _validate_historical_relative_path(value.get("historical_chapter_relative_path"))


def _validate_source_hashes(value: Any) -> None:
    item = _dependency_object(value, "source_hashes")
    expected = {
        "historical_chapter_sha256",
        "revision_source_sha256",
        "source_project_digest",
        "context_digest",
        "dependency_inventory_hash",
    }
    if set(item) != expected:
        raise HistoricalRevisionError("source_hashes_invalid", "source hash fields are not exact")
    for field in sorted(expected):
        _require_sha256(field, item[field])


def _validate_authority_binding(value: Any, field: str) -> None:
    item = _dependency_object(value, field)
    expected = {"authority_epoch", "revision", "head_event_hash"}
    if set(item) != expected:
        raise HistoricalRevisionError("authority_binding_invalid", f"{field} fields are not exact")
    _positive_int(f"{field}.authority_epoch", item["authority_epoch"])
    _positive_int(f"{field}.revision", item["revision"])
    _require_sha256(f"{field}.head_event_hash", item["head_event_hash"])


def _validate_authority_transition(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if int(before["authority_epoch"]) != int(after["authority_epoch"]):
        raise HistoricalRevisionError(
            "historical_revision_epoch_transition_forbidden",
            "amend/import/retcon append within the current authority epoch",
        )
    if int(after["revision"]) <= int(before["revision"]):
        raise HistoricalRevisionError(
            "historical_revision_authority_not_advanced", "canonical revision did not advance"
        )
    if after["head_event_hash"] == before["head_event_hash"]:
        raise HistoricalRevisionError(
            "historical_revision_authority_not_advanced", "event authority head did not advance"
        )


def _validate_anchor(value: Any) -> None:
    item = _dependency_object(value, "anchor_batch")
    if set(item) != {
        "batch_id",
        "batch_hash",
        "first_revision",
        "last_revision",
        "evidence_kind",
        "historical_chapter_sha256",
        "published_evidence_sha256",
        "source_record_hash",
    }:
        raise HistoricalRevisionError("anchor_batch_invalid", "anchor fields are not exact")
    _required_text(item, "batch_id")
    _require_sha256("anchor_batch.batch_hash", item["batch_hash"])
    first = _positive_int("anchor_batch.first_revision", item["first_revision"])
    last = _positive_int("anchor_batch.last_revision", item["last_revision"])
    if last < first:
        raise HistoricalRevisionError("anchor_batch_invalid", "anchor revision bounds are reversed")
    if item["evidence_kind"] not in {"chapter_event", "migration_source_snapshot"}:
        raise HistoricalRevisionError("anchor_batch_invalid", "anchor evidence kind is unsupported")
    for field in (
        "historical_chapter_sha256",
        "published_evidence_sha256",
        "source_record_hash",
    ):
        _require_sha256(f"anchor_batch.{field}", item[field])


def _validate_invalidation_entries(value: Mapping[str, Any]) -> None:
    event_ids: list[str] = []
    event_ranges: list[tuple[int, int]] = []
    for item in value["event_invalidations"]:
        record = _dependency_object(item, "event invalidation")
        event_ids.append(_required_text(record, "batch_id"))
        _require_sha256("event.batch_hash", record.get("batch_hash"))
        first = _positive_int("event.first_revision", record.get("first_revision"))
        last = _positive_int("event.last_revision", record.get("last_revision"))
        if last < first:
            raise HistoricalRevisionError(
                "historical_revision_dependency_invalid", "event revision bounds are reversed"
            )
        event_ranges.append((first, last))
        _required_text(record, "reason")
    if len(event_ids) != len(set(event_ids)) or any(
        current[0] <= previous[1]
        for previous, current in zip(event_ranges, event_ranges[1:])
    ):
        raise HistoricalRevisionError(
            "historical_revision_dependency_invalid",
            "event invalidations are duplicated, overlapping, or out of order",
        )
    outline_ids: list[str] = []
    outline_order: list[tuple[int, str]] = []
    for item in value["outline_invalidations"]:
        record = _dependency_object(item, "outline invalidation")
        outline_id = _required_text(record, "outline_id")
        outline_ids.append(outline_id)
        _require_sha256("outline.artifact_sha256", record.get("artifact_sha256"))
        chapter = _positive_int("outline.chapter_index", record.get("chapter_index"))
        outline_order.append((chapter, outline_id))
        _require_sha256("outline.head_event_hash", record.get("head_event_hash"))
        _positive_int("outline.authority_epoch", record.get("authority_epoch"))
        _required_text(record, "reason")
    if len(outline_ids) != len(set(outline_ids)) or outline_order != sorted(outline_order):
        raise HistoricalRevisionError(
            "historical_revision_dependency_invalid",
            "outline invalidations are duplicated or out of order",
        )
    session_ids: list[str] = []
    session_order: list[tuple[int, str]] = []
    for item in value["session_invalidations"]:
        record = _dependency_object(item, "session invalidation")
        session_id = _required_text(record, "session_id")
        session_ids.append(session_id)
        _require_sha256("session.artifact_sha256", record.get("artifact_sha256"))
        first = _positive_int("session.first_chapter_index", record.get("first_chapter_index"))
        last = _positive_int("session.last_chapter_index", record.get("last_chapter_index"))
        if last < first:
            raise HistoricalRevisionError(
                "historical_revision_dependency_invalid", "session revision bounds are reversed"
            )
        session_order.append((first, session_id))
        _require_sha256("session.head_event_hash", record.get("head_event_hash"))
        _positive_int("session.authority_epoch", record.get("authority_epoch"))
        _required_text(record, "reason")
    if len(session_ids) != len(set(session_ids)) or session_order != sorted(session_order):
        raise HistoricalRevisionError(
            "historical_revision_dependency_invalid",
            "session invalidations are duplicated or out of order",
        )


def _validate_reconciliation_record(value: Any) -> None:
    if value is None:
        return
    record = _dependency_object(value, "reconciliation")
    expected = {
        "blocked_transaction_id",
        "blocked_impact_basis_hash",
        "resolved_event_batch_ids",
        "resolved_outline_ids",
        "resolved_session_ids",
        "resolved_dependency_inventory_hash",
        "revision_source_sha256",
        "reconciliation_hash",
    }
    if set(record) != expected:
        raise HistoricalRevisionError(
            "historical_revision_reconciliation_invalid", "reconciliation fields are not exact"
        )
    _safe_id(record["blocked_transaction_id"])
    for field in (
        "blocked_impact_basis_hash",
        "resolved_dependency_inventory_hash",
        "revision_source_sha256",
        "reconciliation_hash",
    ):
        _require_sha256(field, record[field])
    for field in (
        "resolved_event_batch_ids",
        "resolved_outline_ids",
        "resolved_session_ids",
    ):
        items = record[field]
        if (
            not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
            or len(items) != len(set(items))
        ):
            raise HistoricalRevisionError(
                "historical_revision_reconciliation_invalid", f"{field} is invalid"
            )
    if record["reconciliation_hash"] != canonical_json_hash(
        record, exclude_fields=("reconciliation_hash",)
    ):
        raise HistoricalRevisionError(
            "historical_revision_reconciliation_invalid", "reconciliation hash mismatch"
        )


def _verify_spans(text: str, spans: Any) -> None:
    if not isinstance(spans, list) or not spans:
        raise HistoricalRevisionError("evidence_spans_invalid", "at least one evidence span is required")
    for item in spans:
        record = _dependency_object(item, "evidence span")
        if set(record) != {"start", "end", "quote"}:
            raise HistoricalRevisionError("evidence_spans_invalid", "span fields are not exact")
        start = record["start"]
        end = record["end"]
        quote = record["quote"]
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or not isinstance(quote, str)
            or text[start:end] != quote
        ):
            raise HistoricalRevisionError("evidence_spans_invalid", "span does not match evidence text")


def _verify_self_hash(value: Mapping[str, Any], field: str) -> None:
    if value[field] != canonical_json_hash(dict(value), exclude_fields=(field,)):
        raise HistoricalRevisionError(f"{field}_mismatch", f"{field} does not match canonical bytes")


def _dependency_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalRevisionError("historical_revision_dependency_invalid", f"{field} must be an object")
    return dict(value)


def _revision_kind(value: Any) -> str:
    if not isinstance(value, str) or value not in HISTORICAL_REVISION_KINDS:
        raise HistoricalRevisionError("unknown_historical_revision_kind", f"unsupported kind: {value!r}")
    return value


def _validate_historical_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalRevisionError(
            "historical_chapter_path_invalid", "historical chapter relative path is required"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != SOURCE_DIRECTORIES[1]
        or path.as_posix() != value
    ):
        raise HistoricalRevisionError(
            "historical_chapter_path_invalid",
            "historical chapter path must be normalized published prose",
        )
    return value


def _safe_id(value: Any) -> str:
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    base = value.split(".", 1)[0].upper() if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or not value
        or not _SAFE_ID.fullmatch(value)
        or value.endswith((".", " "))
        or base in reserved
    ):
        raise HistoricalRevisionError("historical_revision_id_invalid", "transaction_id is unsafe")
    return value


def _required_text(value: Mapping[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise HistoricalRevisionError("historical_revision_field_invalid", f"{field} is required")
    return result


def _positive_int(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HistoricalRevisionError("historical_revision_field_invalid", f"{field} must be positive")
    return value


def _require_sha256(field: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HistoricalRevisionError("historical_revision_hash_invalid", f"{field} must be SHA-256")
    return value


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise HistoricalRevisionError("root_uuid_invalid", "StoryProject root UUID is required")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise HistoricalRevisionError("root_uuid_invalid", "StoryProject root UUID is invalid") from exc
    canonical = str(parsed)
    if canonical != value:
        raise HistoricalRevisionError("root_uuid_invalid", "StoryProject root UUID must be canonical")
    return canonical


def _require_within(root: Path, path: Path, *, field: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HistoricalRevisionError(
            "historical_revision_target_outside_memory_root", f"{field} escapes memory root"
        ) from exc


def _revision_artifact_exists(revision_root: Path) -> bool:
    if not revision_root.exists():
        return False
    if not revision_root.is_dir() or revision_root.is_symlink():
        return True
    return any(path.is_file() or path.is_symlink() for path in revision_root.rglob("*"))


__all__ = [
    "HISTORICAL_REVISION_KINDS",
    "HISTORICAL_REVISION_SCHEMA_VERSION",
    "HistoricalRevisionError",
    "assert_event_authority_reconciliation_ready",
    "capture_historical_revision_dependency_inventory",
    "prepare_amend_transaction",
    "prepare_historical_revision_transaction",
    "prepare_import_transaction",
    "prepare_retcon_transaction",
    "validate_historical_revision_dependency_inventory",
    "validate_historical_revision_evidence",
    "validate_historical_revision_bundle",
    "validate_historical_revision_impact_report",
    "validate_historical_revision_invalidation_manifest",
    "validate_historical_revision_transaction",
]
