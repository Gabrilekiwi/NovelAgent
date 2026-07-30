from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from core.engine.persistence_v2 import (
    PersistenceV2Error,
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
    committed_from_publication_receipt,
    reconcile_pending_persistence_v2,
)
from core.engine.root_registry import RootRegistryService
from core.memory_v2.canonical import canonical_json_hash
from core.path_refs import PathRef, path_ref_for
from core.schema import SchemaValidationError, validate_schema
from core.state.authoritative import (
    AuthoritativeStateError,
    validate_authoritative_state,
)
from core.state.snapshot import SnapshotError, validate_snapshot
from core.story_project.authority import (
    AUTHORITY_MODE_EVENT,
    AUTHORITY_MODE_LEGACY,
)
from core.story_project.authority_persistence import (
    EventAuthorityPersistenceBarrierError,
    EventAuthorityWriteOperation,
    event_authority_write_operation,
)
from core.story_project.identity import (
    ProjectIdentityError,
    load_project_identity,
)


CHAPTER_CONTEXT_AUTHORITY_MIGRATION_SCHEMA_VERSION = "1.0"
CHAPTER_CONTEXT_AUTHORITY_MIGRATION_SCHEMA = (
    "chapter_context_authority_migration.schema.json"
)
CHAPTER_CONTEXT_AUTHORITY_MIGRATION_RECEIPT_KIND = (
    "chapter_context_authority_migration"
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COLLECTION_ID_FIELDS = {
    "characters": "character_id",
    "relationships": "relationship_id",
    "roster": "roster_id",
    "numeric_counters": "counter_id",
    "inventory": "inventory_id",
    "locations": "entity_id",
    "events": "event_id",
}
_FaultInjector = Callable[[str, int | None, Path | None], None]


class ChapterContextAuthorityMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def run_chapter_context_authority_migration(
    *,
    story_project: str | Path,
    manifest_path: str | Path,
    apply: bool = False,
    _fault_injector: _FaultInjector | None = None,
) -> dict[str, Any]:
    """Preview or apply a hash-pinned legacy authoritative-state upsert.

    Preview is the default and is deliberately lock-free and write-free.
    Apply enters the StoryProject-global authority barrier, then publishes the
    snapshot, immutable manifest/preimage, migration receipt, and PersistenceV2
    receipt as one recoverable transaction.  The private fault injector exists
    only for deterministic crash-recovery tests.
    """

    root = _story_project_root(story_project)
    manifest_file = Path(manifest_path).resolve(strict=True)
    if not manifest_file.is_file():
        _fail(
            "manifest_path_invalid",
            f"Migration manifest is not a file: {manifest_file}",
        )
    manifest_bytes = manifest_file.read_bytes()
    manifest = _json_object(manifest_bytes, label="migration manifest")
    _validate_manifest(manifest)
    manifest_sha256 = _sha256_bytes(manifest_bytes)

    snapshot_path = _project_file(
        root,
        manifest["snapshot"]["path"],
        label="snapshot.path",
    )
    runtime_root = root / ".novelagent" / "runtime"
    migration_root = (
        runtime_root
        / "migrations"
        / "cca"
        / str(manifest["migration_id"])
    )

    if not apply:
        plan = _prepare_plan(
            root=root,
            manifest_file=manifest_file,
            manifest_bytes=manifest_bytes,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            snapshot_path=snapshot_path,
            migration_root=migration_root,
        )
        return _public_result(plan, mode="preview")

    # Refuse event authority before entering any migration recovery/writer
    # control plane.  The guarded plan below repeats this check under the
    # StoryProject-global barrier.
    _validate_project_identity(root=root, manifest=manifest)
    try:
        with event_authority_write_operation(
            root,
            expected_book_id=str(manifest["book_id"]),
            writer_kind="migration",
        ) as authority_operation:
            local_recovery = reconcile_pending_persistence_v2(
                root / ".novelagent" / "migration-v2" / "tx",
                expected_book_id=str(manifest["book_id"]),
            )
            if not local_recovery.get("ok"):
                _fail(
                    "migration_local_recovery_required",
                    (
                        "Migration PersistenceV2 root requires operator "
                        f"recovery: {local_recovery.get('recovery_required')}."
                    ),
                )
            plan = _prepare_plan(
                root=root,
                manifest_file=manifest_file,
                manifest_bytes=manifest_bytes,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                snapshot_path=snapshot_path,
                migration_root=migration_root,
            )
            if plan["status"] == "already_applied":
                if _was_recovered_by_barrier(
                    plan,
                    authority_operation.recovery,
                ):
                    plan["status"] = "receipt_recovered"
                    plan["recovered_by_barrier"] = True
                return _public_result(plan, mode="apply")

            previous_status = str(plan["status"])
            committed = _commit_plan_transaction(
                plan,
                authority_operation=authority_operation,
                fault_injector=_fault_injector,
            )
            if (
                not committed.get("committed")
                or committed.get("state") != "completed"
            ):
                _fail(
                    "migration_persistence_incomplete",
                    (
                        "PersistenceV2 did not produce a completed, "
                        f"receipt-backed transaction: {committed}."
                    ),
                )
            actual_after_sha256 = _sha256_bytes(snapshot_path.read_bytes())
            if actual_after_sha256 != plan["after_sha256"]:
                _fail(
                    "snapshot_atomic_write_verification_failed",
                    (
                        "Snapshot hash after recoverable publication is "
                        f"{actual_after_sha256}, expected "
                        f"{plan['after_sha256']}."
                    ),
                )
            plan["receipt"] = _validate_receipt(
                plan["receipt_path"],
                expected=plan,
            )
            plan["status"] = (
                "receipt_recovered"
                if previous_status == "recovery_required"
                else "applied"
            )
            plan["current_snapshot_sha256"] = actual_after_sha256
            return _public_result(plan, mode="apply")
    except ChapterContextAuthorityMigrationError:
        raise
    except (
        EventAuthorityPersistenceBarrierError,
        PersistenceV2Error,
    ) as exc:
        _fail(
            "migration_persistence_failed",
            f"Recoverable authority migration failed: {exc}.",
        )


def _prepare_plan(
    *,
    root: Path,
    manifest_file: Path,
    manifest_bytes: bytes,
    manifest: dict[str, Any],
    manifest_sha256: str,
    snapshot_path: Path,
    migration_root: Path,
) -> dict[str, Any]:
    current_manifest_bytes = manifest_file.read_bytes()
    if current_manifest_bytes != manifest_bytes:
        _fail(
            "manifest_cas_mismatch",
            "Migration manifest bytes changed before the guarded operation.",
        )
    current_snapshot_path = _project_file(
        root,
        manifest["snapshot"]["path"],
        label="snapshot.path",
    )
    if current_snapshot_path != snapshot_path:
        _fail(
            "snapshot_path_cas_mismatch",
            "Resolved snapshot path changed before the guarded operation.",
        )
    identity = _validate_project_identity(root=root, manifest=manifest)
    evidence = _verify_evidence(root=root, manifest=manifest)
    before_sha256 = str(manifest["snapshot"]["sha256_before"])
    # Keep Windows paths bounded for long Chinese book names. The immutable
    # receipt binds the complete preimage SHA256; repeating it in the filename
    # adds no integrity while easily crossing classic MAX_PATH.
    backup_path = migration_root / "preimage.snapshot.json"
    immutable_manifest_path = migration_root / "manifest.json"
    receipt_path = migration_root / "receipt.json"
    persistence_receipt_path = (
        migration_root / "publication-receipt.json"
    )
    # Reuse the repository's registered migration transaction root.  Creating
    # another embedded RootRegistry would make whole-project root remapping
    # fail closed, while using runtime/persistence would look like forbidden
    # event-authority history to a legacy project.
    transaction_root = root / ".novelagent" / "migration-v2" / "tx"
    current_bytes = snapshot_path.read_bytes()
    current_sha256 = _sha256_bytes(current_bytes)
    _validated_snapshot(
        current_bytes,
        expected_book_id=str(manifest["book_id"]),
        label="current snapshot",
    )
    common = {
        "root": root,
        "manifest_file": manifest_file,
        "manifest_bytes": manifest_bytes,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "immutable_manifest_path": immutable_manifest_path,
        "snapshot_path": snapshot_path,
        "migration_root": migration_root,
        "backup_path": backup_path,
        "receipt_path": receipt_path,
        "persistence_receipt_path": persistence_receipt_path,
        "persistence_receipt_id": (
            f"cca-receipt-{manifest_sha256[:24]}"
        ),
        "transaction_root": transaction_root,
        "before_sha256": before_sha256,
        "current_snapshot_sha256": current_sha256,
        "identity": identity,
        "evidence": evidence,
    }

    if receipt_path.exists():
        if not backup_path.is_file():
            _fail(
                "migration_backup_missing",
                (
                    "An immutable migration receipt exists without its "
                    "project-scoped preimage backup."
                ),
            )
        before_bytes = backup_path.read_bytes()
        if _sha256_bytes(before_bytes) != before_sha256:
            _fail(
                "migration_backup_hash_mismatch",
                "Existing migration backup does not match snapshot.sha256_before.",
            )
        candidate = _build_candidate(
            snapshot_bytes=before_bytes,
            manifest=manifest,
        )
        plan = {
            **common,
            **candidate,
            "before_bytes": before_bytes,
        }
        _require_valid_manifest_copy(plan)
        _require_valid_backup(plan)
        plan["receipt"] = _validate_receipt(
            receipt_path,
            expected=plan,
        )
        if (
            current_sha256 == before_sha256
            and candidate["after_sha256"] != before_sha256
        ):
            _fail(
                "migration_state_receipt_conflict",
                (
                    "The immutable receipt proves application, but the current "
                    "snapshot is the distinct pinned preimage."
                ),
            )
        plan["status"] = "already_applied"
        return plan

    if current_sha256 == before_sha256:
        candidate = _build_candidate(
            snapshot_bytes=current_bytes,
            manifest=manifest,
        )
        plan = {
            **common,
            **candidate,
            "before_bytes": current_bytes,
        }
        plan["status"] = "ready"
        return plan

    if not backup_path.is_file():
        _fail(
            "snapshot_precondition_failed",
            (
                f"Snapshot SHA256 is {current_sha256}; expected pinned preimage "
                f"{before_sha256}, and no immutable preimage backup exists."
            ),
        )
    before_bytes = backup_path.read_bytes()
    if _sha256_bytes(before_bytes) != before_sha256:
        _fail(
            "migration_backup_hash_mismatch",
            "Existing migration backup does not match snapshot.sha256_before.",
        )
    candidate = _build_candidate(
        snapshot_bytes=before_bytes,
        manifest=manifest,
    )
    plan = {
        **common,
        **candidate,
        "before_bytes": before_bytes,
    }
    if candidate["after_sha256"] != current_sha256:
        _fail(
            "snapshot_precondition_failed",
            (
                f"Snapshot SHA256 is {current_sha256}; expected preimage "
                f"{before_sha256} or deterministic result "
                f"{candidate['after_sha256']}."
            ),
        )
    plan["status"] = "recovery_required"
    return plan


def _build_candidate(
    *,
    snapshot_bytes: bytes,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    snapshot = _validated_snapshot(
        snapshot_bytes,
        expected_book_id=str(manifest["book_id"]),
        label="pinned snapshot preimage",
    )
    authoritative = snapshot.get("authoritative_state")
    if not isinstance(authoritative, dict):
        _fail(
            "snapshot_authoritative_state_missing",
            "Snapshot has no authoritative_state object.",
        )
    for collection in _COLLECTION_ID_FIELDS:
        if not isinstance(authoritative.get(collection), dict):
            _fail(
                "snapshot_authoritative_collection_invalid",
                f"authoritative_state.{collection} must be an object.",
            )

    after_snapshot = copy.deepcopy(snapshot)
    after_authority = after_snapshot["authoritative_state"]
    upsert_audit: list[dict[str, Any]] = []
    for upsert in sorted(
        manifest["upserts"],
        key=lambda item: (str(item["collection"]), str(item["record_id"])),
    ):
        collection = str(upsert["collection"])
        record_id = str(upsert["record_id"])
        incoming = copy.deepcopy(upsert["record"])
        collection_records = after_authority[collection]
        if record_id not in collection_records:
            after_authority[collection][record_id] = incoming
            action = "inserted"
        elif (
            _canonical_json_bytes(collection_records[record_id])
            == _canonical_json_bytes(incoming)
        ):
            action = "identical"
        else:
            _fail(
                "authoritative_record_conflict",
                (
                    f"authoritative_state.{collection}[{record_id!r}] already "
                    "exists with non-identical content."
                ),
            )
        upsert_audit.append(
            {
                "collection": collection,
                "record_id": record_id,
                "record_sha256": _sha256_bytes(
                    _canonical_json_bytes(incoming)
                ),
                "evidence_ids": sorted(
                    str(value) for value in upsert["evidence_ids"]
                ),
                "action": action,
            }
        )

    try:
        validated = validate_authoritative_state(after_authority)
    except AuthoritativeStateError as exc:
        codes = sorted(
            {
                str(item.get("code") or "unknown")
                for item in exc.report.get("findings") or []
                if isinstance(item, dict)
            }
        )
        _fail(
            "authoritative_state_validation_failed",
            (
                "Existing authoritative_state validator rejected the "
                f"migration result: {', '.join(codes) or 'unknown'}."
            ),
        )
    except (TypeError, ValueError) as exc:
        _fail(
            "authoritative_state_validation_failed",
            (
                "Existing authoritative_state validator could not validate "
                f"the migration result: {exc}."
            ),
        )
    if _canonical_json_bytes(validated) != _canonical_json_bytes(
        after_authority
    ):
        _fail(
            "authoritative_state_normalization_mismatch",
            (
                "The existing authoritative-state validator would normalize "
                "or discard data. Migration refuses to write an object that "
                "differs from the validated authority."
            ),
        )
    try:
        validate_snapshot(after_snapshot)
    except SnapshotError as exc:
        _fail(
            "snapshot_validation_failed",
            f"Resulting snapshot is invalid: {exc}.",
        )
    after_bytes = _pretty_json_bytes(after_snapshot)
    return {
        "after_snapshot": after_snapshot,
        "after_bytes": after_bytes,
        "after_sha256": _sha256_bytes(after_bytes),
        "upsert_audit": upsert_audit,
        "validation": {
            "ok": True,
            "authoritative_state_sha256": _sha256_bytes(
                _canonical_json_bytes(validated)
            ),
            "collection_counts": {
                collection: len(after_authority[collection])
                for collection in _COLLECTION_ID_FIELDS
            },
        },
    }


def _validate_project_identity(
    *,
    root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        identity = load_project_identity(root)
    except ProjectIdentityError as exc:
        _fail("project_identity_invalid", str(exc))
    if identity is None or identity.ephemeral:
        _fail(
            "stable_project_identity_required",
            "Migration requires a persisted, non-ephemeral ProjectIdentity.",
        )
    if identity.book_id != manifest["book_id"]:
        _fail(
            "project_identity_book_id_mismatch",
            (
                f"ProjectIdentity book_id {identity.book_id!r} does not match "
                f"manifest book_id {manifest['book_id']!r}."
            ),
        )
    authority = identity.authority or {}
    mode = authority.get("mode")
    if mode == AUTHORITY_MODE_EVENT:
        _fail(
            "event_v1_history_revision_required",
            (
                "event_v1 authority is active; legacy/shadow cache migration "
                "is forbidden. Use the audited history-revision workflow."
            ),
        )
    if mode != AUTHORITY_MODE_LEGACY:
        _fail(
            "legacy_authority_required",
            f"Unsupported ProjectIdentity authority mode: {mode!r}.",
        )
    return {
        "book_id": identity.book_id,
        "schema_version": identity.schema_version,
        "story_state_mode": identity.story_state_mode,
        "authority_mode": mode,
    }


def _verify_evidence(
    *,
    root: Path,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for evidence in sorted(
        manifest["evidence"],
        key=lambda item: str(item["evidence_id"]),
    ):
        path = _project_file(
            root,
            evidence["path"],
            label=f"evidence[{evidence['evidence_id']}].path",
        )
        content = path.read_bytes()
        actual_sha256 = _sha256_bytes(content)
        if actual_sha256 != evidence["sha256"]:
            _fail(
                "evidence_sha256_mismatch",
                (
                    f"Evidence {evidence['evidence_id']!r} SHA256 is "
                    f"{actual_sha256}, expected {evidence['sha256']}."
                ),
            )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            _fail(
                "evidence_encoding_invalid",
                (
                    f"Evidence {evidence['evidence_id']!r} is not valid UTF-8: "
                    f"{exc}."
                ),
            )
        quote = str(evidence["exact_quote"])
        line = evidence.get("line")
        if line is not None:
            lines = text.splitlines()
            actual_line = (
                lines[int(line) - 1]
                if 0 < int(line) <= len(lines)
                else None
            )
            if actual_line != quote:
                _fail(
                    "evidence_exact_quote_mismatch",
                    (
                        f"Evidence {evidence['evidence_id']!r} does not exactly "
                        f"match {evidence['path']}:{line}."
                    ),
                )
            match_count = 1
        else:
            match_count = text.count(quote)
            if match_count < 1:
                _fail(
                    "evidence_exact_quote_mismatch",
                    (
                        f"Evidence {evidence['evidence_id']!r} exact quote is "
                        f"absent from {evidence['path']}."
                    ),
                )
        verified.append(
            {
                "evidence_id": str(evidence["evidence_id"]),
                "path": str(evidence["path"]),
                "sha256": actual_sha256,
                "exact_quote": quote,
                **({"line": int(line)} if line is not None else {}),
                "match_count": match_count,
            }
        )
    return verified


def _validate_manifest(manifest: dict[str, Any]) -> None:
    try:
        validate_schema(
            manifest,
            CHAPTER_CONTEXT_AUTHORITY_MIGRATION_SCHEMA,
        )
    except SchemaValidationError as exc:
        _fail("manifest_schema_invalid", str(exc))
    if (
        manifest.get("schema_version")
        != CHAPTER_CONTEXT_AUTHORITY_MIGRATION_SCHEMA_VERSION
    ):
        _fail(
            "manifest_schema_unsupported",
            "Manifest schema_version must be '1.0'.",
        )
    migration_id = manifest.get("migration_id")
    if (
        not isinstance(migration_id, str)
        or _SAFE_ID.fullmatch(migration_id) is None
    ):
        _fail("manifest_migration_id_invalid", "migration_id is unsafe.")
    book_id = manifest.get("book_id")
    if (
        not isinstance(book_id, str)
        or _SAFE_ID.fullmatch(book_id) is None
        or book_id.startswith("ephemeral:")
    ):
        _fail(
            "manifest_book_id_invalid",
            (
                "book_id must be one stable, non-ephemeral, "
                "PersistenceV2-safe identifier."
            ),
        )
    _relative_path(manifest["snapshot"]["path"], "snapshot.path")
    _require_sha256(
        manifest["snapshot"]["sha256_before"],
        "snapshot.sha256_before",
    )

    evidence_ids: set[str] = set()
    for evidence in manifest["evidence"]:
        evidence_id = str(evidence["evidence_id"])
        if evidence_id in evidence_ids:
            _fail(
                "manifest_evidence_invalid",
                f"Duplicate evidence_id: {evidence_id!r}.",
            )
        evidence_ids.add(evidence_id)
        _relative_path(
            evidence["path"],
            f"evidence[{evidence_id}].path",
        )
        _require_sha256(
            evidence["sha256"],
            f"evidence[{evidence_id}].sha256",
        )

    targets: set[tuple[str, str]] = set()
    for upsert in manifest["upserts"]:
        collection = str(upsert["collection"])
        if collection not in _COLLECTION_ID_FIELDS:
            _fail(
                "manifest_collection_invalid",
                f"Unsupported authoritative collection: {collection!r}.",
            )
        record_id = _record_id(upsert["record_id"])
        target = (collection, record_id)
        if target in targets:
            _fail(
                "manifest_upsert_duplicate",
                f"Duplicate upsert target: {collection}/{record_id}.",
            )
        targets.add(target)
        record = upsert["record"]
        if not isinstance(record, dict):
            _fail(
                "manifest_record_invalid",
                f"Upsert {collection}/{record_id} record must be an object.",
            )
        identity_field = _COLLECTION_ID_FIELDS[collection]
        if record.get(identity_field) != record_id:
            _fail(
                "manifest_record_id_mismatch",
                (
                    f"Upsert {collection}/{record_id} requires "
                    f"record.{identity_field} == record_id."
                ),
            )
        try:
            _canonical_json_bytes(record)
        except (TypeError, ValueError) as exc:
            _fail(
                "manifest_record_invalid",
                f"Upsert {collection}/{record_id} is not canonical JSON: {exc}.",
            )
        referenced = upsert["evidence_ids"]
        if (
            len(referenced) != len(set(referenced))
            or any(value not in evidence_ids for value in referenced)
        ):
            _fail(
                "manifest_evidence_reference_invalid",
                (
                    f"Upsert {collection}/{record_id} references unknown "
                    "evidence."
                ),
            )


def _commit_plan_transaction(
    plan: dict[str, Any],
    *,
    authority_operation: EventAuthorityWriteOperation,
    fault_injector: _FaultInjector | None,
) -> dict[str, Any]:
    root = Path(plan["root"])
    runtime_root = root / ".novelagent"
    transaction_root = Path(plan["transaction_root"])
    root_map = {
        "story_project": root,
        "runtime": runtime_root,
    }
    registry_service = RootRegistryService(transaction_root)
    registry = registry_service.ensure(root_map)
    resolver = registry_service.resolver(registry)

    def ref(path: Path, *, root_id: str) -> PathRef:
        physical_root = root_map[root_id]
        return path_ref_for(
            path,
            root_id=root_id,
            root=physical_root,
            root_uuid=str(
                registry["roots"][root_id]["root_uuid"]
            ),
        )

    snapshot_ref = ref(
        Path(plan["snapshot_path"]),
        root_id="story_project",
    )
    manifest_ref = ref(
        Path(plan["immutable_manifest_path"]),
        root_id="runtime",
    )
    backup_ref = ref(
        Path(plan["backup_path"]),
        root_id="runtime",
    )
    final_ref = ref(
        Path(plan["receipt_path"]),
        root_id="runtime",
    )
    persistence_receipt_ref = ref(
        Path(plan["persistence_receipt_path"]),
        root_id="runtime",
    )
    for path_ref in (
        snapshot_ref,
        manifest_ref,
        backup_ref,
        final_ref,
        persistence_receipt_ref,
    ):
        resolver.ensure_parent(path_ref)

    run_id = (
        "cca-"
        + hashlib.sha256(
            str(plan["manifest"]["migration_id"]).encode("utf-8")
        ).hexdigest()[:12]
        + "-"
        + uuid.uuid4().hex[:12]
    )
    receipt = _build_receipt(
        plan,
        persistence_run_id=run_id,
        publication_receipt_ref=persistence_receipt_ref,
    )
    guard_failure: ChapterContextAuthorityMigrationError | None = None

    def guarded_fault(
        point: str,
        index: int | None,
        path: Path | None,
    ) -> None:
        nonlocal guard_failure
        if fault_injector is not None:
            fault_injector(point, index, path)
        if point == "before_commit_marker":
            try:
                _recheck_guarded_inputs(plan)
            except ChapterContextAuthorityMigrationError as exc:
                guard_failure = exc
                raise

    transaction = PersistenceV2Transaction(
        transaction_root=transaction_root,
        run_id=run_id,
        book_id=str(plan["manifest"]["book_id"]),
        root_map=root_map,
        fault_injector=guarded_fault,
    )
    apply_targets = [
        PersistenceV2Target(
            target_id="authority-snapshot",
            kind="authoritative_snapshot",
            path_ref=snapshot_ref,
            content=bytes(plan["after_bytes"]),
            expected_before_exists=True,
            expected_before_sha256=str(
                plan["current_snapshot_sha256"]
            ),
        )
    ]
    immutable_metadata = {
        "immutable": True,
        "migration_id": str(plan["manifest"]["migration_id"]),
    }
    artifacts = [
        PersistenceV2Target(
            target_id="migration-manifest",
            kind="migration_manifest",
            path_ref=manifest_ref,
            content=bytes(plan["manifest_bytes"]),
            phase="publication",
            metadata=immutable_metadata,
        ),
        PersistenceV2Target(
            target_id="snapshot-preimage",
            kind="snapshot_preimage",
            path_ref=backup_ref,
            content=bytes(plan["before_bytes"]),
            phase="publication",
            metadata=immutable_metadata,
        ),
    ]
    context_digest = canonical_json_hash(
        {
            "book_id": plan["manifest"]["book_id"],
            "migration_id": plan["manifest"]["migration_id"],
            "manifest_sha256": plan["manifest_sha256"],
            "snapshot_before_sha256": plan["before_sha256"],
            "snapshot_after_sha256": plan["after_sha256"],
        }
    )
    authority_operation.prepare_transaction(
        transaction,
        apply_targets=apply_targets,
        artifacts=artifacts,
        final_run_record=receipt,
        final_run_path_ref=final_ref,
        receipt_id=str(plan["persistence_receipt_id"]),
        receipt_path_ref=persistence_receipt_ref,
        context_digest=context_digest,
        generation_input_context_digest=str(plan["manifest_sha256"]),
        story_project_source_revision_after={
            "schema_version": "1.0",
            "book_id": plan["manifest"]["book_id"],
            "migration_id": plan["manifest"]["migration_id"],
            "snapshot_sha256": plan["after_sha256"],
        },
        candidate_result=receipt,
        delivery_jobs=[],
    )
    committed = authority_operation.commit_transaction(transaction)
    if guard_failure is not None:
        raise guard_failure
    return committed


def _recheck_guarded_inputs(plan: Mapping[str, Any]) -> None:
    if Path(plan["manifest_file"]).read_bytes() != bytes(
        plan["manifest_bytes"]
    ):
        _fail(
            "manifest_cas_mismatch",
            "Migration manifest bytes changed before commit.",
        )
    current_snapshot_path = _project_file(
        Path(plan["root"]),
        plan["manifest"]["snapshot"]["path"],
        label="snapshot.path",
    )
    if current_snapshot_path != Path(plan["snapshot_path"]):
        _fail(
            "snapshot_path_cas_mismatch",
            "Resolved snapshot path changed before commit.",
        )
    _validate_project_identity(
        root=Path(plan["root"]),
        manifest=plan["manifest"],
    )
    evidence = _verify_evidence(
        root=Path(plan["root"]),
        manifest=plan["manifest"],
    )
    if evidence != plan["evidence"]:
        _fail(
            "evidence_cas_mismatch",
            "Verified evidence changed before commit.",
        )
    snapshot_bytes = current_snapshot_path.read_bytes()
    actual_sha256 = _sha256_bytes(snapshot_bytes)
    if actual_sha256 != plan["after_sha256"]:
        _fail(
            "snapshot_commit_cas_mismatch",
            (
                f"Snapshot SHA256 before commit marker is {actual_sha256}; "
                f"expected staged result {plan['after_sha256']}."
            ),
        )
    _validated_snapshot(
        snapshot_bytes,
        expected_book_id=str(plan["manifest"]["book_id"]),
        label="staged snapshot result",
    )


def _require_valid_manifest_copy(plan: Mapping[str, Any]) -> None:
    path = Path(plan["immutable_manifest_path"])
    if not path.is_file():
        _fail(
            "migration_manifest_copy_missing",
            "Applied migration has no immutable project-scoped manifest copy.",
        )
    if _sha256_bytes(path.read_bytes()) != plan["manifest_sha256"]:
        _fail(
            "migration_manifest_copy_hash_mismatch",
            "Immutable migration manifest copy no longer matches its hash.",
        )


def _require_valid_backup(plan: Mapping[str, Any]) -> None:
    backup_path = Path(plan["backup_path"])
    if not backup_path.is_file():
        _fail(
            "migration_backup_missing",
            "Applied migration has no immutable project-scoped preimage backup.",
        )
    if _sha256_bytes(backup_path.read_bytes()) != plan["before_sha256"]:
        _fail(
            "migration_backup_hash_mismatch",
            "Migration backup no longer matches the pinned preimage.",
        )


def _build_receipt(
    plan: Mapping[str, Any],
    *,
    persistence_run_id: str,
    publication_receipt_ref: PathRef,
) -> dict[str, Any]:
    manifest = plan["manifest"]
    receipt = {
        "schema_version": "1.0",
        "receipt_kind": (
            CHAPTER_CONTEXT_AUTHORITY_MIGRATION_RECEIPT_KIND
        ),
        "migration_id": manifest["migration_id"],
        "book_id": manifest["book_id"],
        "status": "applied",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "authority_mode": AUTHORITY_MODE_LEGACY,
        "persistence_run_id": persistence_run_id,
        "manifest": {
            "path": _relative_to_root(
                plan["root"],
                plan["immutable_manifest_path"],
            ),
            "sha256": plan["manifest_sha256"],
        },
        "snapshot": {
            "path": manifest["snapshot"]["path"],
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "backup_path": _relative_to_root(
                plan["root"],
                plan["backup_path"],
            ),
            "backup_sha256": plan["before_sha256"],
        },
        "evidence": copy.deepcopy(plan["evidence"]),
        "upserts": copy.deepcopy(plan["upsert_audit"]),
    }
    bound = bind_final_run_record_receipt(
        receipt,
        receipt_id=str(plan["persistence_receipt_id"]),
        receipt_path_ref=publication_receipt_ref,
    )
    bound["receipt_hash"] = canonical_json_hash(bound)
    return bound


def _validate_receipt(
    path: Path,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _json_object(path.read_bytes(), label="migration receipt")
    expected_receipt_fields = {
        "schema_version",
        "receipt_kind",
        "migration_id",
        "book_id",
        "status",
        "applied_at",
        "authority_mode",
        "persistence_run_id",
        "publication_receipt",
        "manifest",
        "snapshot",
        "evidence",
        "upserts",
        "receipt_hash",
    }
    if set(receipt) != expected_receipt_fields:
        _fail(
            "migration_receipt_binding_mismatch",
            "Migration receipt contains missing or unsupported fields.",
        )
    receipt_hash = receipt.get("receipt_hash")
    if (
        not isinstance(receipt_hash, str)
        or _SHA256.fullmatch(receipt_hash) is None
        or canonical_json_hash(
            receipt,
            exclude_fields=("receipt_hash",),
        )
        != receipt_hash
    ):
        _fail(
            "migration_receipt_hash_mismatch",
            "Immutable migration receipt hash is invalid.",
        )
    snapshot = receipt.get("snapshot")
    manifest_record = receipt.get("manifest")
    if (
        receipt.get("schema_version") != "1.0"
        or receipt.get("receipt_kind")
        != CHAPTER_CONTEXT_AUTHORITY_MIGRATION_RECEIPT_KIND
        or receipt.get("migration_id")
        != expected["manifest"]["migration_id"]
        or receipt.get("book_id") != expected["manifest"]["book_id"]
        or receipt.get("status") != "applied"
        or receipt.get("authority_mode") != AUTHORITY_MODE_LEGACY
        or not isinstance(manifest_record, dict)
        or set(manifest_record) != {"path", "sha256"}
        or manifest_record.get("path")
        != _relative_to_root(
            expected["root"],
            expected["immutable_manifest_path"],
        )
        or manifest_record.get("sha256") != expected["manifest_sha256"]
        or not isinstance(snapshot, dict)
        or set(snapshot)
        != {
            "path",
            "before_sha256",
            "after_sha256",
            "backup_path",
            "backup_sha256",
        }
        or snapshot.get("path")
        != expected["manifest"]["snapshot"]["path"]
        or snapshot.get("before_sha256") != expected["before_sha256"]
        or snapshot.get("after_sha256") != expected["after_sha256"]
        or snapshot.get("backup_sha256") != expected["before_sha256"]
        or snapshot.get("backup_path")
        != _relative_to_root(expected["root"], expected["backup_path"])
        or receipt.get("evidence") != expected["evidence"]
        or receipt.get("upserts") != expected["upsert_audit"]
        or not isinstance(receipt.get("persistence_run_id"), str)
        or not receipt.get("persistence_run_id")
    ):
        _fail(
            "migration_receipt_binding_mismatch",
            (
                "Migration receipt does not bind the stable book, manifest, "
                "evidence, upserts, backup, and historical snapshot result."
            ),
        )
    if not isinstance(receipt.get("applied_at"), str) or not receipt[
        "applied_at"
    ]:
        _fail(
            "migration_receipt_binding_mismatch",
            "Migration receipt applied_at is missing.",
        )
    _require_valid_manifest_copy(expected)
    _require_valid_backup(expected)
    publication_receipt_path = Path(
        expected["persistence_receipt_path"]
    )
    if not publication_receipt_path.is_file():
        _fail(
            "migration_persistence_receipt_missing",
            (
                "Migration final receipt has no immutable PersistenceV2 "
                "publication receipt."
            ),
        )
    try:
        publication_receipt = _json_object(
            publication_receipt_path.read_bytes(),
            label="PersistenceV2 publication receipt",
        )
        committed = committed_from_publication_receipt(
            path,
            publication_receipt_path,
            root_map={
                "story_project": Path(expected["root"]),
                "runtime": Path(expected["root"]) / ".novelagent",
            },
        )
    except (OSError, PersistenceV2Error, ValueError) as exc:
        _fail(
            "migration_persistence_receipt_invalid",
            f"PersistenceV2 receipt validation failed: {exc}.",
        )
    pointer = receipt.get("publication_receipt")
    if (
        not committed
        or not isinstance(pointer, dict)
        or pointer
        != {
            "id": publication_receipt.get("receipt_id"),
            "path_ref": publication_receipt.get("receipt_path_ref"),
        }
        or publication_receipt.get("receipt_id")
        != expected["persistence_receipt_id"]
        or publication_receipt.get("run_id")
        != receipt.get("persistence_run_id")
    ):
        _fail(
            "migration_persistence_receipt_binding_mismatch",
            (
                "Migration receipt is not the final record bound by the "
                "expected completed PersistenceV2 transaction."
            ),
        )
    return receipt


def _was_recovered_by_barrier(
    plan: Mapping[str, Any],
    recovery: Mapping[str, Any],
) -> bool:
    receipt = plan.get("receipt")
    run_id = (
        receipt.get("persistence_run_id")
        if isinstance(receipt, Mapping)
        else None
    )
    transactions = recovery.get("transactions")
    if not isinstance(run_id, str) or not isinstance(transactions, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("run_id") == run_id
        and item.get("state") == "completed"
        for item in transactions
    )


def _public_result(
    plan: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    status = str(plan["status"])
    return {
        "schema_version": "1.0",
        "mode": mode,
        "status": status,
        "migration_id": plan["manifest"]["migration_id"],
        "book_id": plan["manifest"]["book_id"],
        "story_project": str(plan["root"]),
        "manifest": {
            "source_path": str(plan["manifest_file"]),
            "immutable_path": _relative_to_root(
                plan["root"],
                plan["immutable_manifest_path"],
            ),
            "sha256": plan["manifest_sha256"],
        },
        "identity": copy.deepcopy(plan["identity"]),
        "snapshot": {
            "path": plan["manifest"]["snapshot"]["path"],
            "current_sha256": plan["current_snapshot_sha256"],
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "backup_path": _relative_to_root(
                plan["root"],
                plan["backup_path"],
            ),
        },
        "evidence": copy.deepcopy(plan["evidence"]),
        "upserts": copy.deepcopy(plan["upsert_audit"]),
        "validation": copy.deepcopy(plan["validation"]),
        "receipt_path": _relative_to_root(
            plan["root"],
            plan["receipt_path"],
        ),
        "persistence_receipt_path": _relative_to_root(
            plan["root"],
            plan["persistence_receipt_path"],
        ),
        "writes_performed": (
            mode == "apply"
            and status in {"applied", "receipt_recovered"}
        ),
    }


def _story_project_root(value: str | Path) -> Path:
    root = Path(value).resolve(strict=True)
    if not root.is_dir():
        _fail(
            "story_project_invalid",
            f"StoryProject root is not a directory: {root}.",
        )
    return root


def _project_file(root: Path, value: Any, *, label: str) -> Path:
    relative = _relative_path(value, label)
    candidate = (
        root / Path(*PurePosixPath(relative).parts)
    ).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError:
        _fail(
            "manifest_path_escape",
            f"{label} escapes the StoryProject root.",
        )
    if not candidate.is_file():
        _fail(
            "manifest_path_invalid",
            f"{label} is not a regular file: {candidate}.",
        )
    return candidate


def _relative_path(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
    ):
        _fail(
            "manifest_relative_path_invalid",
            f"{label} must be one normalized project-relative POSIX path.",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        _fail(
            "manifest_relative_path_invalid",
            f"{label} must be one normalized project-relative POSIX path.",
        )
    return value


def _record_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        _fail(
            "manifest_record_id_invalid",
            f"record_id is unsafe: {value!r}.",
        )
    return value


def _relative_to_root(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root).as_posix()


def _validated_snapshot(
    content: bytes,
    *,
    expected_book_id: str,
    label: str,
) -> dict[str, Any]:
    snapshot = _json_object(content, label=label)
    try:
        validate_snapshot(snapshot)
    except SnapshotError as exc:
        _fail(
            "snapshot_validation_failed",
            f"{label} is not a valid runtime snapshot: {exc}.",
        )
    book_id = snapshot.get("book_id")
    if book_id != expected_book_id:
        _fail(
            "snapshot_book_id_mismatch",
            (
                f"{label} book_id {book_id!r} does not match manifest "
                f"book_id {expected_book_id!r}."
            ),
        )
    return snapshot


def _json_object(content: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                _fail(
                    "json_duplicate_key",
                    f"{label} contains duplicate JSON key {key!r}.",
                )
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value!r}")

    try:
        value = json.loads(
            content.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except ChapterContextAuthorityMigrationError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        _fail(
            "json_object_invalid",
            f"Could not decode {label}: {exc}.",
        )
    if not isinstance(value, dict):
        _fail("json_object_invalid", f"{label} must be one JSON object.")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(
            "manifest_sha256_invalid",
            f"{label} must be lowercase SHA-256.",
        )
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fail(code: str, message: str) -> None:
    raise ChapterContextAuthorityMigrationError(code, message)


__all__ = [
    "CHAPTER_CONTEXT_AUTHORITY_MIGRATION_RECEIPT_KIND",
    "CHAPTER_CONTEXT_AUTHORITY_MIGRATION_SCHEMA",
    "CHAPTER_CONTEXT_AUTHORITY_MIGRATION_SCHEMA_VERSION",
    "ChapterContextAuthorityMigrationError",
    "run_chapter_context_authority_migration",
]
