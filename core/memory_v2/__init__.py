from __future__ import annotations

from core.memory_v2.events import (
    MemoryEventValidationError,
    append_memory_event,
    append_memory_events,
    create_memory_event,
    load_memory_events,
    validate_memory_event,
)
from core.memory_v2.compile import MemoryCompileError, compile_memory_v2, validate_memory_compile_report
from core.memory_v2.canonical import CANONICAL_JSON_ALGORITHM, CanonicalJSONError, canonical_json_bytes, canonical_json_hash
from core.memory_v2.event_store import (
    CHECKPOINT_CHAPTER_INTERVAL,
    MEMORY_CHECKPOINT_SCHEMA_VERSION,
    MEMORY_EVENT_BATCH_SCHEMA_VERSION,
    MemoryEventStoreError,
    MemoryIntegrityError,
    MemoryPatchConflictError,
    commit_memory_patch,
    create_memory_checkpoint,
    create_memory_event_batch,
    load_latest_memory_checkpoint,
    load_memory_event_batch,
    load_memory_event_batches,
    memory_patch_content_hash,
    memory_projection_hash,
    rebuild_canonical_memory,
    replay_memory_events,
    validate_memory_checkpoint,
    validate_memory_event_batch,
    verify_memory_projection,
    write_memory_checkpoint,
    write_memory_event_batch,
)
from core.memory_v2.importer_v1 import import_v1_memory_file_to_patch, import_v1_memory_to_patch, load_v1_memory_file
from core.memory_v2.models import create_empty_canonical_memory
from core.memory_v2.patch import MemoryPatchValidationError, create_memory_patch, validate_memory_patch
from core.memory_v2.reducer import MemoryReducerError, apply_memory_patch
from core.memory_v2.snapshot_adapter import canonical_memory_to_snapshot, load_canonical_memory_snapshot, rebuild_semantic_snapshot
from core.memory_v2.storage import load_canonical_memory, save_canonical_memory
from core.memory_v2.validator import MemoryV2ValidationError, validate_canonical_memory
from core.memory_v2.runtime import ensure_memory_v2_storage_layout, prepare_chapter_memory_commit

__all__ = [
    "MemoryEventValidationError",
    "MemoryV2ValidationError",
    "MemoryPatchValidationError",
    "MemoryReducerError",
    "MemoryCompileError",
    "MemoryEventStoreError",
    "MemoryIntegrityError",
    "MemoryPatchConflictError",
    "CanonicalJSONError",
    "CANONICAL_JSON_ALGORITHM",
    "CHECKPOINT_CHAPTER_INTERVAL",
    "MEMORY_CHECKPOINT_SCHEMA_VERSION",
    "MEMORY_EVENT_BATCH_SCHEMA_VERSION",
    "append_memory_event",
    "append_memory_events",
    "apply_memory_patch",
    "canonical_memory_to_snapshot",
    "compile_memory_v2",
    "commit_memory_patch",
    "canonical_json_bytes",
    "canonical_json_hash",
    "create_memory_checkpoint",
    "create_memory_event_batch",
    "create_empty_canonical_memory",
    "create_memory_event",
    "create_memory_patch",
    "import_v1_memory_file_to_patch",
    "import_v1_memory_to_patch",
    "load_canonical_memory",
    "load_canonical_memory_snapshot",
    "load_memory_events",
    "load_latest_memory_checkpoint",
    "load_memory_event_batch",
    "load_memory_event_batches",
    "load_v1_memory_file",
    "save_canonical_memory",
    "memory_patch_content_hash",
    "memory_projection_hash",
    "ensure_memory_v2_storage_layout",
    "prepare_chapter_memory_commit",
    "rebuild_canonical_memory",
    "rebuild_semantic_snapshot",
    "replay_memory_events",
    "validate_canonical_memory",
    "validate_memory_event",
    "validate_memory_compile_report",
    "validate_memory_patch",
    "validate_memory_checkpoint",
    "validate_memory_event_batch",
    "verify_memory_projection",
    "write_memory_checkpoint",
    "write_memory_event_batch",
]
