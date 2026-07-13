from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from core.memory_v2.canonical import CANONICAL_JSON_ALGORITHM, canonical_json_hash
from core.memory_v2.events import MEMORY_EVENT_SCHEMA_VERSION, validate_memory_event
from core.memory_v2.models import create_empty_canonical_memory
from core.memory_v2.patch import validate_memory_patch
from core.memory_v2.reducer import apply_memory_patch
from core.memory_v2.storage import load_canonical_memory, save_canonical_memory
from core.memory_v2.validator import validate_canonical_memory
from core.schema import SchemaValidationError, validate_schema


MEMORY_EVENT_BATCH_SCHEMA_VERSION = "2.1"
MEMORY_CHECKPOINT_SCHEMA_VERSION = "2.1"
CHECKPOINT_CHAPTER_INTERVAL = 20
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_BATCH_FILE = re.compile(r"^batch_(\d{12})_(\d{12})_[0-9a-f]{12}\.json$")


class MemoryEventStoreError(ValueError):
    pass


class MemoryIntegrityError(MemoryEventStoreError):
    pass


class MemoryPatchConflictError(MemoryEventStoreError):
    pass


def memory_patch_content_hash(patch: dict[str, Any]) -> str:
    validated = copy.deepcopy(validate_memory_patch(patch))
    validated.pop("schema_version", None)
    metadata = validated.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("created_by", None)
    return canonical_json_hash(validated)


def memory_projection_hash(projection: dict[str, Any]) -> str:
    return canonical_json_hash(validate_canonical_memory(projection))


def create_memory_event_batch(
    *,
    book_id: str,
    patch: dict[str, Any],
    events: list[dict[str, Any]],
    expected_revision: int,
    previous_batch_hash: str | None,
    source_project_digest: str,
    context_digest: str,
    batch_kind: str = "source_sync",
    publication_status: str | None = None,
    base_projection: dict[str, Any] | None = None,
    quality_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validated_patch = copy.deepcopy(validate_memory_patch(patch))
    validated_events = [copy.deepcopy(validate_memory_event(event)) for event in events]
    if not validated_events:
        raise MemoryEventStoreError("Memory 2.1 event batches must contain at least one event")
    if any(event.get("schema_version") != MEMORY_EVENT_SCHEMA_VERSION for event in validated_events):
        raise MemoryEventStoreError("new event batches require Memory 2.1 events")

    status = publication_status or ("committed" if batch_kind == "chapter" else "source_sync")
    _validate_publication_boundary(batch_kind, status)
    _require_sha256("source_project_digest", source_project_digest)
    _require_sha256("context_digest", context_digest)
    if previous_batch_hash is not None:
        _require_sha256("previous_batch_hash", previous_batch_hash)

    revisions = [int(event["revision"]) for event in validated_events]
    expected_revisions = list(range(expected_revision + 1, expected_revision + len(validated_events) + 1))
    if revisions != expected_revisions:
        raise MemoryIntegrityError(
            f"event revisions must be contiguous after {expected_revision}: {revisions}"
        )
    if previous_batch_hash is None:
        if base_projection is None:
            raise MemoryEventStoreError("the first Memory 2.1 batch requires base_projection")
        validated_base = copy.deepcopy(validate_canonical_memory(base_projection))
        if int(validated_base["revision"]) != expected_revision:
            raise MemoryIntegrityError("base_projection revision does not match expected_revision")
        if str(validated_base["book_id"]) != book_id:
            raise MemoryIntegrityError("base_projection book_id does not match batch book_id")
    elif base_projection is not None:
        raise MemoryEventStoreError("only the first Memory 2.1 batch may contain base_projection")
    else:
        validated_base = None

    patch_hash = memory_patch_content_hash(validated_patch)
    first_revision = revisions[0]
    last_revision = revisions[-1]
    batch_id = f"batch_{first_revision:012d}_{last_revision:012d}_{patch_hash[:12]}"
    batch: dict[str, Any] = {
        "schema_version": MEMORY_EVENT_BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "book_id": book_id,
        "batch_kind": batch_kind,
        "publication_status": status,
        "first_revision": first_revision,
        "last_revision": last_revision,
        "previous_batch_hash": previous_batch_hash,
        "patch_id": str(validated_patch["patch_id"]),
        "patch_content_hash": patch_hash,
        "expected_revision": expected_revision,
        "source_project_digest": source_project_digest,
        "context_digest": context_digest,
        "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
        "patch": validated_patch,
        "events": validated_events,
        "base_projection": validated_base,
        "quality_state": copy.deepcopy(quality_state or {}),
    }
    batch["batch_hash"] = _batch_hash(batch)
    return validate_memory_event_batch(batch)


def validate_memory_event_batch(batch: Any) -> dict[str, Any]:
    if not isinstance(batch, dict):
        raise MemoryEventStoreError("memory event batch must be a JSON object")
    try:
        validated = validate_schema(batch, "memory_event_batch.schema.json")
    except SchemaValidationError as exc:
        raise MemoryEventStoreError(str(exc)) from exc
    if not _SAFE_ID.fullmatch(str(validated["batch_id"])):
        raise MemoryEventStoreError("batch_id contains unsafe characters")
    for field in (
        "batch_hash",
        "patch_content_hash",
        "source_project_digest",
        "context_digest",
    ):
        _require_sha256(field, validated[field])
    previous_hash = validated.get("previous_batch_hash")
    if previous_hash is not None:
        _require_sha256("previous_batch_hash", previous_hash)

    patch = validate_memory_patch(validated["patch"])
    if patch["patch_id"] != validated["patch_id"]:
        raise MemoryIntegrityError("batch patch_id does not match embedded patch")
    if memory_patch_content_hash(patch) != validated["patch_content_hash"]:
        raise MemoryIntegrityError("batch patch content hash mismatch")

    events = [validate_memory_event(event) for event in validated["events"]]
    if any(event.get("schema_version") != MEMORY_EVENT_SCHEMA_VERSION for event in events):
        raise MemoryIntegrityError("Memory 2.1 batch contains a legacy event")
    revisions = [int(event["revision"]) for event in events]
    expected_revision = int(validated["expected_revision"])
    expected_revisions = list(range(expected_revision + 1, expected_revision + len(events) + 1))
    if revisions != expected_revisions:
        raise MemoryIntegrityError("batch event revisions are not contiguous")
    if int(validated["first_revision"]) != revisions[0] or int(validated["last_revision"]) != revisions[-1]:
        raise MemoryIntegrityError("batch revision bounds do not match its events")

    base_projection = validated.get("base_projection")
    if previous_hash is None:
        if not isinstance(base_projection, dict):
            raise MemoryIntegrityError("first batch is missing base_projection")
        base = validate_canonical_memory(base_projection)
        if int(base["revision"]) != expected_revision or str(base["book_id"]) != validated["book_id"]:
            raise MemoryIntegrityError("first batch base_projection identity mismatch")
    elif base_projection is not None:
        raise MemoryIntegrityError("non-initial batch contains base_projection")

    _validate_publication_boundary(str(validated["batch_kind"]), str(validated["publication_status"]))
    if validated["canonical_json_algorithm"] != CANONICAL_JSON_ALGORITHM:
        raise MemoryIntegrityError("unsupported canonical JSON algorithm")
    if validated["batch_hash"] != _batch_hash(validated):
        raise MemoryIntegrityError("memory event batch hash mismatch")
    return validated


def write_memory_event_batch(store_dir: str | Path, batch: dict[str, Any]) -> Path:
    validated = validate_memory_event_batch(batch)
    path = _batch_path(Path(store_dir), str(validated["batch_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError:
        existing = load_memory_event_batch(path)
        if existing != validated:
            raise MemoryPatchConflictError(f"immutable batch already exists with different content: {path}")
    return path


def load_memory_event_batch(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryIntegrityError(f"cannot read memory event batch {source}: {exc}") from exc
    return validate_memory_event_batch(payload)


def load_memory_event_batches(
    store_dir: str | Path,
    *,
    after_revision: int = 0,
    expected_previous_hash: str | None = None,
) -> list[dict[str, Any]]:
    root = Path(store_dir)
    batch_dir = root / "batches"
    if not batch_dir.exists():
        return []
    selected: list[tuple[int, Path]] = []
    for path in batch_dir.glob("*.json"):
        match = _BATCH_FILE.fullmatch(path.name)
        if match is None:
            raise MemoryIntegrityError(f"unexpected file in immutable batch store: {path.name}")
        first_revision = int(match.group(1))
        if first_revision > after_revision:
            selected.append((first_revision, path))
    selected.sort(key=lambda item: (item[0], item[1].name))

    batches: list[dict[str, Any]] = []
    previous_hash = expected_previous_hash
    expected_revision: int | None = (
        after_revision if after_revision > 0 or expected_previous_hash is not None else None
    )
    for _, path in selected:
        batch = load_memory_event_batch(path)
        if expected_revision is None:
            if batch["previous_batch_hash"] is not None:
                raise MemoryIntegrityError(f"memory history does not start at a root batch: {batch['batch_id']}")
            expected_revision = int(batch["expected_revision"])
        if int(batch["expected_revision"]) != expected_revision:
            raise MemoryIntegrityError(
                f"memory batch revision discontinuity at {batch['batch_id']}: "
                f"expected {expected_revision}, got {batch['expected_revision']}"
            )
        if batch["previous_batch_hash"] != previous_hash:
            raise MemoryIntegrityError(f"memory batch hash chain is broken at {batch['batch_id']}")
        batches.append(batch)
        previous_hash = str(batch["batch_hash"])
        expected_revision = int(batch["last_revision"])
    return batches


def create_memory_checkpoint(
    *,
    projection: dict[str, Any],
    last_batch: dict[str, Any],
    committed_chapter_count: int,
    patch_index: dict[str, str],
    quality_state: dict[str, Any],
) -> dict[str, Any]:
    validated_projection = copy.deepcopy(validate_canonical_memory(projection))
    validated_batch = validate_memory_event_batch(last_batch)
    revision = int(validated_projection["revision"])
    if revision != int(validated_batch["last_revision"]):
        raise MemoryIntegrityError("checkpoint projection revision does not match last batch")
    checkpoint_id = f"checkpoint_{revision:012d}_{validated_batch['batch_hash'][:12]}"
    checkpoint: dict[str, Any] = {
        "schema_version": MEMORY_CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "book_id": str(validated_projection["book_id"]),
        "revision": revision,
        "last_batch_id": str(validated_batch["batch_id"]),
        "last_batch_hash": str(validated_batch["batch_hash"]),
        "committed_chapter_count": committed_chapter_count,
        "projection_hash": memory_projection_hash(validated_projection),
        "projection": validated_projection,
        "patch_index": copy.deepcopy(patch_index),
        "quality_state": copy.deepcopy(quality_state),
        "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
    }
    checkpoint["checkpoint_hash"] = _checkpoint_hash(checkpoint)
    return validate_memory_checkpoint(checkpoint)


def validate_memory_checkpoint(checkpoint: Any) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise MemoryEventStoreError("memory checkpoint must be a JSON object")
    try:
        validated = validate_schema(checkpoint, "memory_checkpoint.schema.json")
    except SchemaValidationError as exc:
        raise MemoryEventStoreError(str(exc)) from exc
    for field in ("last_batch_hash", "projection_hash", "checkpoint_hash"):
        _require_sha256(field, validated[field])
    if not _SAFE_ID.fullmatch(str(validated["checkpoint_id"])):
        raise MemoryEventStoreError("checkpoint_id contains unsafe characters")
    projection = validate_canonical_memory(validated["projection"])
    if str(projection["book_id"]) != validated["book_id"] or int(projection["revision"]) != validated["revision"]:
        raise MemoryIntegrityError("checkpoint projection identity mismatch")
    if memory_projection_hash(projection) != validated["projection_hash"]:
        raise MemoryIntegrityError("checkpoint projection hash mismatch")
    for patch_id, patch_hash in validated["patch_index"].items():
        if not isinstance(patch_id, str) or not patch_id:
            raise MemoryIntegrityError("checkpoint patch_index contains an invalid patch id")
        _require_sha256(f"patch_index.{patch_id}", patch_hash)
    if validated["checkpoint_hash"] != _checkpoint_hash(validated):
        raise MemoryIntegrityError("memory checkpoint hash mismatch")
    return validated


def write_memory_checkpoint(store_dir: str | Path, checkpoint: dict[str, Any]) -> Path:
    validated = validate_memory_checkpoint(checkpoint)
    root = Path(store_dir)
    path = root / "checkpoints" / f"{validated['checkpoint_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError:
        existing = _load_json(path)
        if validate_memory_checkpoint(existing) != validated:
            raise MemoryPatchConflictError(f"immutable checkpoint already exists with different content: {path}")
    return path


def load_latest_memory_checkpoint(store_dir: str | Path) -> dict[str, Any] | None:
    root = Path(store_dir)
    paths = sorted((root / "checkpoints").glob("checkpoint_*.json"), reverse=True)
    if not paths:
        return None
    checkpoint = validate_memory_checkpoint(_load_json(paths[0]))
    anchor_path = _batch_path(root, str(checkpoint["last_batch_id"]))
    if not anchor_path.exists():
        raise MemoryIntegrityError("checkpoint anchor batch is missing")
    anchor = load_memory_event_batch(anchor_path)
    if anchor["batch_hash"] != checkpoint["last_batch_hash"]:
        raise MemoryIntegrityError("checkpoint anchor batch hash mismatch")
    return checkpoint


def replay_memory_events(
    store_dir: str | Path,
    *,
    initial_memory: dict[str, Any] | None = None,
    use_checkpoint: bool = True,
) -> dict[str, Any]:
    checkpoint = load_latest_memory_checkpoint(store_dir) if use_checkpoint else None
    if checkpoint is not None:
        projection = copy.deepcopy(checkpoint["projection"])
        after_revision = int(checkpoint["revision"])
        previous_hash: str | None = str(checkpoint["last_batch_hash"])
        last_batch_id: str | None = str(checkpoint["last_batch_id"])
        committed_chapters = int(checkpoint["committed_chapter_count"])
        patch_index = copy.deepcopy(checkpoint["patch_index"])
        quality_state = copy.deepcopy(checkpoint["quality_state"])
        checkpoint_id: str | None = str(checkpoint["checkpoint_id"])
    else:
        projection = copy.deepcopy(initial_memory) if initial_memory is not None else None
        after_revision = int(projection["revision"]) if isinstance(projection, dict) else 0
        previous_hash = None
        last_batch_id = None
        committed_chapters = 0
        patch_index: dict[str, str] = {}
        quality_state: dict[str, Any] = {}
        checkpoint_id = None

    batches = load_memory_event_batches(
        store_dir,
        after_revision=after_revision,
        expected_previous_hash=previous_hash,
    )
    if projection is None:
        if not batches:
            raise MemoryEventStoreError("cannot replay an empty event store without initial_memory")
        base_projection = batches[0].get("base_projection")
        if not isinstance(base_projection, dict):
            raise MemoryIntegrityError("first batch does not contain a valid base_projection")
        projection = copy.deepcopy(validate_canonical_memory(base_projection))
        after_revision = int(projection["revision"])
    else:
        projection = copy.deepcopy(validate_canonical_memory(projection))

    event_count = 0
    for batch in batches:
        if str(batch["book_id"]) != str(projection["book_id"]):
            raise MemoryIntegrityError("memory batch book_id changed during replay")
        if int(batch["expected_revision"]) != int(projection["revision"]):
            raise MemoryIntegrityError("memory batch expected_revision does not match replay projection")
        patch_id = str(batch["patch_id"])
        patch_hash = str(batch["patch_content_hash"])
        existing_patch_hash = patch_index.get(patch_id)
        if existing_patch_hash is not None:
            if existing_patch_hash == patch_hash:
                raise MemoryIntegrityError(f"duplicate patch id was persisted instead of being a no-op: {patch_id}")
            raise MemoryIntegrityError(f"conflicting patch id exists in event history: {patch_id}")

        updated, generated_events = apply_memory_patch(projection, batch["patch"])
        if generated_events != batch["events"]:
            raise MemoryIntegrityError(f"batch events do not reproduce patch semantics: {batch['batch_id']}")
        if int(updated["revision"]) != int(batch["last_revision"]):
            raise MemoryIntegrityError("reducer revision does not match batch last_revision")
        projection = updated
        patch_index[patch_id] = patch_hash
        event_count += len(generated_events)
        previous_hash = str(batch["batch_hash"])
        last_batch_id = str(batch["batch_id"])
        if batch["batch_kind"] == "chapter":
            committed_chapters += 1
        if batch.get("quality_state"):
            quality_state[str(batch["batch_id"])] = copy.deepcopy(batch["quality_state"])

    report = {
        "schema_version": "2.1",
        "status": "ok",
        "book_id": str(projection["book_id"]),
        "revision": int(projection["revision"]),
        "batch_count": len(batches),
        "event_count": event_count,
        "committed_chapter_count": committed_chapters,
        "last_batch_id": last_batch_id,
        "last_batch_hash": previous_hash,
        "checkpoint_id": checkpoint_id,
        "projection_hash": memory_projection_hash(projection),
        "patch_index": patch_index,
        "quality_state": quality_state,
        "projection": projection,
    }
    try:
        return validate_schema(report, "memory_replay_report.schema.json")
    except SchemaValidationError as exc:
        raise MemoryEventStoreError(str(exc)) from exc


def verify_memory_projection(
    store_dir: str | Path,
    projection: dict[str, Any] | str | Path,
    *,
    initial_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = load_canonical_memory(projection) if isinstance(projection, (str, Path)) else validate_canonical_memory(projection)
    replay = replay_memory_events(store_dir, initial_memory=initial_memory)
    actual_hash = memory_projection_hash(candidate)
    expected_hash = str(replay["projection_hash"])
    return {
        "schema_version": "2.1",
        "status": "ok" if actual_hash == expected_hash else "mismatch",
        "matches": actual_hash == expected_hash,
        "expected_revision": int(replay["revision"]),
        "actual_revision": int(candidate["revision"]),
        "expected_projection_hash": expected_hash,
        "actual_projection_hash": actual_hash,
        "checkpoint_id": replay["checkpoint_id"],
        "last_batch_hash": replay["last_batch_hash"],
    }


def rebuild_canonical_memory(
    store_dir: str | Path,
    canonical_path: str | Path,
    *,
    initial_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replay = replay_memory_events(store_dir, initial_memory=initial_memory)
    projection = save_canonical_memory(canonical_path, replay["projection"])
    verification = verify_memory_projection(store_dir, projection, initial_memory=initial_memory)
    if not verification["matches"]:
        raise MemoryIntegrityError("rebuilt canonical memory failed projection verification")
    return projection


def commit_memory_patch(
    *,
    store_dir: str | Path,
    canonical_path: str | Path,
    patch: dict[str, Any],
    source_project_digest: str,
    context_digest: str,
    initial_memory: dict[str, Any] | None = None,
    batch_kind: str = "source_sync",
    publication_status: str | None = None,
    quality_state: dict[str, Any] | None = None,
    checkpoint_interval: int = CHECKPOINT_CHAPTER_INTERVAL,
) -> dict[str, Any]:
    root = Path(store_dir)
    validated_patch = copy.deepcopy(validate_memory_patch(patch))
    patch_id = str(validated_patch["patch_id"])
    patch_hash = memory_patch_content_hash(validated_patch)
    has_batches = any((root / "batches").glob("*.json"))

    if has_batches:
        replay = replay_memory_events(root)
        base_projection = copy.deepcopy(replay["projection"])
    else:
        if initial_memory is not None:
            base_projection = copy.deepcopy(validate_canonical_memory(initial_memory))
        elif Path(canonical_path).exists():
            base_projection = load_canonical_memory(canonical_path)
        else:
            base_projection = create_empty_canonical_memory()
        replay = _empty_replay_state(base_projection)

    existing_hash = replay["patch_index"].get(patch_id)
    if existing_hash is not None:
        if existing_hash != patch_hash:
            raise MemoryPatchConflictError(f"patch id {patch_id} already exists with different content")
        saved = save_canonical_memory(canonical_path, replay["projection"])
        return {
            "status": "no_op",
            "previous_revision": int(saved["revision"]),
            "projection": saved,
            "events": [],
            "batch": None,
            "checkpoint": None,
            "replay_report": replay,
        }

    previous_revision = int(base_projection["revision"])
    updated, events = apply_memory_patch(base_projection, validated_patch)
    if not events:
        raise MemoryEventStoreError("empty patches are not persisted as Memory 2.1 batches")
    batch = create_memory_event_batch(
        book_id=str(base_projection["book_id"]),
        patch=validated_patch,
        events=events,
        expected_revision=previous_revision,
        previous_batch_hash=replay["last_batch_hash"],
        source_project_digest=source_project_digest,
        context_digest=context_digest,
        batch_kind=batch_kind,
        publication_status=publication_status,
        base_projection=base_projection if replay["last_batch_hash"] is None else None,
        quality_state=quality_state,
    )
    write_memory_event_batch(root, batch)

    patch_index = copy.deepcopy(replay["patch_index"])
    patch_index[patch_id] = patch_hash
    committed_chapters = int(replay["committed_chapter_count"]) + (1 if batch_kind == "chapter" else 0)
    accumulated_quality_state = copy.deepcopy(replay["quality_state"])
    if quality_state:
        accumulated_quality_state[str(batch["batch_id"])] = copy.deepcopy(quality_state)

    checkpoint = None
    if batch_kind == "chapter" and checkpoint_interval > 0 and committed_chapters % checkpoint_interval == 0:
        checkpoint = create_memory_checkpoint(
            projection=updated,
            last_batch=batch,
            committed_chapter_count=committed_chapters,
            patch_index=patch_index,
            quality_state=accumulated_quality_state,
        )
        write_memory_checkpoint(root, checkpoint)

    saved = save_canonical_memory(canonical_path, updated)
    final_replay = replay_memory_events(root)
    if memory_projection_hash(saved) != final_replay["projection_hash"]:
        raise MemoryIntegrityError("persisted canonical cache differs from Memory 2.1 replay")
    return {
        "status": "applied",
        "previous_revision": previous_revision,
        "projection": saved,
        "events": copy.deepcopy(events),
        "batch": batch,
        "checkpoint": checkpoint,
        "replay_report": final_replay,
    }


def _empty_replay_state(projection: dict[str, Any]) -> dict[str, Any]:
    validated = copy.deepcopy(validate_canonical_memory(projection))
    return {
        "schema_version": "2.1",
        "status": "ok",
        "book_id": str(validated["book_id"]),
        "revision": int(validated["revision"]),
        "batch_count": 0,
        "event_count": 0,
        "committed_chapter_count": 0,
        "last_batch_id": None,
        "last_batch_hash": None,
        "checkpoint_id": None,
        "projection_hash": memory_projection_hash(validated),
        "patch_index": {},
        "quality_state": {},
        "projection": validated,
    }


def _validate_publication_boundary(batch_kind: str, publication_status: str) -> None:
    if batch_kind not in {"source_sync", "chapter"}:
        raise MemoryEventStoreError(f"unsupported memory batch kind: {batch_kind}")
    required_status = "committed" if batch_kind == "chapter" else "source_sync"
    if publication_status != required_status:
        raise MemoryEventStoreError(
            f"{batch_kind} memory batches require publication_status={required_status}; "
            "rejected, failed, and preview content cannot enter world memory"
        )


def _batch_hash(batch: dict[str, Any]) -> str:
    return canonical_json_hash(batch, exclude_fields=("batch_hash",))


def _checkpoint_hash(checkpoint: dict[str, Any]) -> str:
    return canonical_json_hash(checkpoint, exclude_fields=("checkpoint_hash",))


def _batch_path(root: Path, batch_id: str) -> Path:
    if not _SAFE_ID.fullmatch(batch_id):
        raise MemoryEventStoreError("batch_id contains unsafe characters")
    return root / "batches" / f"{batch_id}.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryIntegrityError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MemoryIntegrityError(f"{path} must contain a JSON object")
    return payload


def _require_sha256(field: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise MemoryIntegrityError(f"{field} must be a lowercase SHA-256 digest")


__all__ = [
    "CANONICAL_JSON_ALGORITHM",
    "CHECKPOINT_CHAPTER_INTERVAL",
    "MEMORY_CHECKPOINT_SCHEMA_VERSION",
    "MEMORY_EVENT_BATCH_SCHEMA_VERSION",
    "MemoryEventStoreError",
    "MemoryIntegrityError",
    "MemoryPatchConflictError",
    "commit_memory_patch",
    "create_memory_checkpoint",
    "create_memory_event_batch",
    "load_latest_memory_checkpoint",
    "load_memory_event_batch",
    "load_memory_event_batches",
    "memory_patch_content_hash",
    "memory_projection_hash",
    "rebuild_canonical_memory",
    "replay_memory_events",
    "validate_memory_checkpoint",
    "validate_memory_event_batch",
    "verify_memory_projection",
    "write_memory_checkpoint",
    "write_memory_event_batch",
]
