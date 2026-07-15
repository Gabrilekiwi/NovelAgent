from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
import uuid

from core.engine.persistence_v2 import (
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
    committed_from_publication_receipt,
    reconcile_pending_persistence_v2,
    verify_publication_receipt,
)
from core.story_project.authority_persistence import event_authority_write_operation
from core.engine.root_registry import RootRegistryService
from core.engine.safe_paths import assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import (
    load_memory_event_batches,
    memory_patch_content_hash,
    memory_projection_hash,
    replay_memory_events,
    validate_memory_event_batch,
)
from core.memory_v2.history_revision import (
    HISTORICAL_REVISION_KINDS,
    HistoricalRevisionError,
    capture_historical_revision_dependency_inventory,
    prepare_historical_revision_transaction,
    validate_historical_revision_bundle,
    validate_historical_revision_dependency_inventory,
    validate_historical_revision_evidence,
    validate_historical_revision_impact_report,
    validate_historical_revision_invalidation_manifest,
    validate_historical_revision_transaction,
)
from core.memory_v2.reducer import CURRENT_REDUCER_VERSION, apply_genesis_event, apply_memory_events
from core.memory_v2.storage import load_canonical_memory
from core.path_refs import PathRef, path_ref_for
from core.story_project.identity import load_project_identity, project_identity_path


HISTORICAL_REVISION_EXECUTION_SCHEMA_VERSION = "1.0"
_SAFE_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_TARGET_KINDS = frozenset(
    {"prose", "chapter_prose", "published_chapter", "revision_source"}
)
_PREPARE_KWARGS = frozenset(
    {
        "memory_root",
        "story_project_root",
        "story_project_root_uuid",
        "transaction_id",
        "historical_chapter_index",
        "historical_chapter_path",
        "expected_historical_chapter_sha256",
        "revision_source_path",
        "expected_revision_source_sha256",
        "evidence_spans",
        "operations",
        "authority_epoch",
        "expected_head_event_hash",
        "expected_revision",
        "source_project_digest",
        "context_digest",
        "dependency_inventory",
        "reconciliation",
        "projection_root",
    }
)
_FaultInjector = Callable[[str, int | None, Path | None], None]


class HistoricalRevisionExecutionError(HistoricalRevisionError):
    """A fail-closed error raised by the durable execution boundary."""


def execute_amend_transaction(**kwargs: Any) -> dict[str, Any]:
    return execute_historical_revision_transaction(revision_kind="amend", **kwargs)


def execute_import_transaction(**kwargs: Any) -> dict[str, Any]:
    return execute_historical_revision_transaction(revision_kind="import", **kwargs)


def execute_retcon_transaction(**kwargs: Any) -> dict[str, Any]:
    return execute_historical_revision_transaction(revision_kind="retcon", **kwargs)


def execute_historical_revision_transaction(
    *,
    revision_kind: str,
    fault_injector: _FaultInjector | None = None,
    attempt_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    **prepare_kwargs: Any,
) -> dict[str, Any]:
    """Atomically execute one append-only amend/import/retcon transaction.

    The operation lock covers reconciliation, completed-receipt detection, and
    a fresh pure prepare.  Every prepared target is one PersistenceV2 apply
    target, with ProjectIdentity forced to be the final apply.  The only
    publication files are the immutable completion record and its receipt.
    """

    kind = _revision_kind(revision_kind)
    kwargs = dict(prepare_kwargs)
    unknown = sorted(set(kwargs).difference(_PREPARE_KWARGS))
    if unknown:
        raise HistoricalRevisionExecutionError(
            "historical_revision_request_invalid",
            f"unknown historical revision fields: {unknown}",
        )
    memory_root = _required_path(kwargs, "memory_root")
    story_root = _required_path(kwargs, "story_project_root")
    transaction_id = _transaction_id(kwargs.get("transaction_id"))
    if not story_root.is_dir():
        raise HistoricalRevisionExecutionError(
            "story_project_missing", "StoryProject root is not a directory"
        )
    if not memory_root.is_dir():
        raise HistoricalRevisionExecutionError(
            "memory_root_missing", "Memory V2 root is not a directory"
        )
    try:
        assert_safe_local_tree(story_root)
        assert_safe_local_tree(memory_root)
    except Exception as exc:
        raise HistoricalRevisionExecutionError(
            "historical_revision_unsafe_root",
            f"StoryProject and Memory V2 roots must be local non-reparse trees: {exc}",
        ) from exc
    story_root = story_root.resolve()
    memory_root = memory_root.resolve()
    historical_path = _required_path(kwargs, "historical_chapter_path")
    revision_source_path = _required_path(kwargs, "revision_source_path")
    layout = _execution_layout(memory_root, transaction_id)
    root_map = {"story_project": story_root, "runtime": memory_root}
    supplied_root_uuid = kwargs.pop("story_project_root_uuid", None)

    with event_authority_write_operation(
        story_root,
        expected_book_id=None,
        writer_kind="history_revision",
    ) as authority_operation:
        identity = load_project_identity(story_root)
        if identity is None or identity.ephemeral:
            raise HistoricalRevisionExecutionError(
                "project_identity_missing", "a stable ProjectIdentity is required"
            )

        recovery = reconcile_pending_persistence_v2(
            layout["transaction_root"], expected_book_id=identity.book_id
        )
        if not recovery.get("ok"):
            raise HistoricalRevisionExecutionError(
                "historical_revision_recovery_required",
                f"PersistenceV2 reconciliation failed: {recovery.get('recovery_required')}",
            )

        registry_service = RootRegistryService(layout["transaction_root"])
        registry = registry_service.ensure(root_map)
        resolver = registry_service.resolver(registry)
        story_root_uuid = str(registry["roots"]["story_project"]["root_uuid"])
        if supplied_root_uuid is not None and supplied_root_uuid != story_root_uuid:
            raise HistoricalRevisionExecutionError(
                "story_project_root_uuid_mismatch",
                "explicit StoryProject root UUID differs from the execution RootRegistry",
            )

        request_digest = _request_digest(
            revision_kind=kind,
            book_id=identity.book_id,
            story_root=story_root,
            memory_root=memory_root,
            story_root_uuid=story_root_uuid,
            prepare_kwargs=kwargs,
        )
        completed = _load_completed_revision(
            root_map=root_map,
            registry=registry,
            layout=layout,
            revision_kind=kind,
            transaction_id=transaction_id,
            request_digest=request_digest,
            historical_path=historical_path,
            revision_source_path=revision_source_path,
        )
        if completed is not None:
            recovered = any(
                item.get("run_id") == completed["run_id"]
                for item in (
                    list(authority_operation.recovery.get("transactions", []))
                    + list(recovery.get("transactions", []))
                )
                if isinstance(item, Mapping)
            )
            completed.update(
                {
                    "status": "recovered" if recovered else "already_committed",
                    "idempotent": True,
                    "already_committed": True,
                    "recovered": recovered,
                    "recovery": recovery,
                }
            )
            return completed

        fresh_kwargs = dict(kwargs)
        fresh_kwargs.update(
            {
                "memory_root": memory_root,
                "story_project_root": story_root,
                "story_project_root_uuid": story_root_uuid,
                "historical_chapter_path": historical_path,
                "revision_source_path": revision_source_path,
            }
        )
        prepared = prepare_historical_revision_transaction(
            revision_kind=kind, **fresh_kwargs
        )
        validate_historical_revision_bundle(prepared)
        _assert_prepared_identity(
            prepared,
            revision_kind=kind,
            transaction_id=transaction_id,
            book_id=identity.book_id,
            story_root_uuid=story_root_uuid,
        )
        _assert_source_hashes_current(
            historical_path,
            revision_source_path,
            prepared["transaction"]["source_hashes"],
        )

        apply_targets = _persistence_targets(
            prepared,
            root_map=root_map,
            registry=registry,
            memory_root=memory_root,
            story_root=story_root,
            historical_path=historical_path,
            revision_source_path=revision_source_path,
        )
        if apply_targets[-1].kind != "project_identity":
            raise HistoricalRevisionExecutionError(
                "historical_revision_identity_not_last",
                "ProjectIdentity must be the final apply target",
            )
        for target in apply_targets:
            resolver.ensure_parent(target.path_ref)

        receipt_ref = _runtime_ref(
            layout["publication_receipt"], root_map=root_map, registry=registry
        )
        final_ref = _runtime_ref(
            layout["completed_record"], root_map=root_map, registry=registry
        )
        resolver.ensure_parent(receipt_ref)
        resolver.ensure_parent(final_ref)

        attempt = attempt_id_factory()
        if not isinstance(attempt, uuid.UUID):
            raise HistoricalRevisionExecutionError(
                "historical_revision_attempt_id_invalid",
                "attempt_id_factory must return UUID",
            )
        run_id = f"hr-{kind[0]}-{layout['operation_key'][:12]}-{attempt.hex[:8]}"
        target_bindings = _target_bindings(apply_targets)
        final_record = _execution_record(
            prepared=prepared,
            run_id=run_id,
            request_digest=request_digest,
            story_root_uuid=story_root_uuid,
            target_bindings=target_bindings,
        )
        final_record = bind_final_run_record_receipt(
            final_record,
            receipt_id=layout["receipt_id"],
            receipt_path_ref=receipt_ref,
        )

        def guarded_fault(point: str, index: int | None, path: Path | None) -> None:
            if fault_injector is not None:
                fault_injector(point, index, path)
            if point in {
                "before_apply_target",
                "after_apply_target",
                "before_commit_marker",
            }:
                _assert_source_hashes_current(
                    historical_path,
                    revision_source_path,
                    prepared["transaction"]["source_hashes"],
                )
            if point in {"before_apply_target", "before_commit_marker"}:
                _assert_dependency_inventory_current(
                    prepared,
                    story_project_root=story_root,
                )

        transaction = PersistenceV2Transaction(
            transaction_root=layout["transaction_root"],
            run_id=run_id,
            book_id=identity.book_id,
            root_map=root_map,
            fault_injector=guarded_fault,
            story_project_read_set=prepared["story_project_read_set"],
            read_set_declared_writes=prepared["read_set_declared_writes"],
        )
        try:
            authority_operation.prepare_transaction(
                transaction,
                apply_targets=apply_targets,
                artifacts=[],
                final_run_record=final_record,
                final_run_path_ref=final_ref,
                receipt_id=layout["receipt_id"],
                receipt_path_ref=receipt_ref,
                context_digest=str(prepared["transaction"]["source_hashes"]["context_digest"]),
                generation_input_context_digest=request_digest,
                story_project_source_revision_after=prepared[
                    "story_project_source_revision_after"
                ],
                candidate_result=final_record,
                delivery_jobs=[],
            )
            if fault_injector is not None:
                fault_injector("before_history_revision_commit", None, None)
            committed = authority_operation.commit_transaction(transaction)
        except Exception as exc:
            # A failure outside commit's own pre-marker rollback can leave a
            # prepared journal.  Reconcile it now; marker-bearing attempts are
            # always completed forward by PersistenceV2.
            try:
                reconcile_pending_persistence_v2(
                    layout["transaction_root"], expected_book_id=identity.book_id
                )
            except Exception:
                pass
            raise HistoricalRevisionExecutionError(
                "historical_revision_atomic_execution_failed",
                f"atomic historical revision failed: {exc}",
            ) from exc

        if not committed.get("committed") or committed.get("state") != "completed":
            raise HistoricalRevisionExecutionError(
                "historical_revision_atomic_execution_incomplete",
                f"transaction requires forward recovery: {committed}",
            )

        completed = _load_completed_revision(
            root_map=root_map,
            registry=registry,
            layout=layout,
            revision_kind=kind,
            transaction_id=transaction_id,
            request_digest=request_digest,
            historical_path=historical_path,
            revision_source_path=revision_source_path,
        )
        if completed is None:
            raise HistoricalRevisionExecutionError(
                "historical_revision_receipt_missing",
                "completed transaction has no verifiable PublicationReceipt",
            )
        completed.update(
            {
                "status": "completed",
                "idempotent": False,
                "already_committed": False,
                "recovered": False,
                "recovery": recovery,
            }
        )
        return completed


def _load_completed_revision(
    *,
    root_map: Mapping[str, Path],
    registry: Mapping[str, Any],
    layout: Mapping[str, Any],
    revision_kind: str,
    transaction_id: str,
    request_digest: str,
    historical_path: Path,
    revision_source_path: Path,
) -> dict[str, Any] | None:
    receipt_path = Path(layout["publication_receipt"])
    final_path = Path(layout["completed_record"])
    if not receipt_path.exists() and not final_path.exists():
        return None
    if not receipt_path.is_file() or not final_path.is_file():
        raise HistoricalRevisionExecutionError(
            "historical_revision_completion_incomplete",
            "completion record and PublicationReceipt are not both present",
        )

    verification = verify_publication_receipt(receipt_path, root_map=root_map)
    if not verification.get("valid") or not verification.get("committed"):
        raise HistoricalRevisionExecutionError(
            "historical_revision_receipt_invalid",
            f"PublicationReceipt verification failed: {verification.get('errors')}",
        )
    if verification.get("delivery_jobs") != []:
        raise HistoricalRevisionExecutionError(
            "historical_revision_delivery_forbidden",
            "historical revision receipt unexpectedly contains delivery jobs",
        )
    record = _load_json(final_path, "historical revision completion record")
    if not committed_from_publication_receipt(
        final_path, receipt_path, root_map=root_map
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_record_uncommitted",
            "completion record is not bound by its PublicationReceipt",
        )
    _validate_execution_record(record)
    if (
        record["story_project_root_uuid"]
        != registry["roots"]["story_project"]["root_uuid"]
    ):
        raise HistoricalRevisionExecutionError(
            "story_project_root_uuid_mismatch",
            "completion record differs from the current execution RootRegistry",
        )
    expected_identity = {
        "revision_kind": revision_kind,
        "transaction_id": transaction_id,
        "request_digest": request_digest,
    }
    if any(record.get(key) != value for key, value in expected_identity.items()):
        if record.get("transaction_id") == transaction_id:
            raise HistoricalRevisionExecutionError(
                "historical_revision_idempotency_conflict",
                "transaction_id was already committed with a different request",
            )
        raise HistoricalRevisionExecutionError(
            "historical_revision_completion_mismatch",
            "completion record identifies another historical revision",
        )

    receipt = _load_json(receipt_path, "historical revision PublicationReceipt")
    if receipt.get("delivery_jobs") != [] or receipt.get("artifacts") != []:
        raise HistoricalRevisionExecutionError(
            "historical_revision_delivery_forbidden",
            "historical revisions may not publish delivery jobs or remote artifacts",
        )
    apply_bindings = receipt.get("apply_targets")
    if not isinstance(apply_bindings, list) or not apply_bindings:
        raise HistoricalRevisionExecutionError(
            "historical_revision_receipt_invalid", "receipt has no apply targets"
        )
    if canonical_json_hash(apply_bindings) != record["target_bundle_digest"]:
        raise HistoricalRevisionExecutionError(
            "historical_revision_target_bundle_mismatch",
            "receipt target bundle differs from the completion record",
        )
    if (
        record["target_count"] != len(apply_bindings)
        or record["target_roles"]
        != [str(item.get("kind") or "") for item in apply_bindings]
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_target_bundle_mismatch",
            "completion target inventory differs from the receipt",
        )
    if (
        apply_bindings[-1].get("kind") != "project_identity"
        or sum(item.get("kind") == "project_identity" for item in apply_bindings) != 1
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_identity_not_last",
            "receipt does not prove ProjectIdentity was the final apply target",
        )
    safe_resolver = RootRegistryService(layout["transaction_root"]).resolver(registry)
    resolved_targets = [
        safe_resolver.resolve(item["path_ref"]).path for item in apply_bindings
    ]
    forbidden_paths = {historical_path.resolve(), revision_source_path.resolve()}
    if forbidden_paths.intersection(path.resolve() for path in resolved_targets) or any(
        item.get("kind") in _FORBIDDEN_TARGET_KINDS for item in apply_bindings
    ):
        raise HistoricalRevisionExecutionError(
            "published_prose_in_place_edit_forbidden",
            "receipt contains a published-prose or revision-source target",
        )

    revision_root = Path(layout["revision_root"])
    evidence = validate_historical_revision_evidence(
        _load_json(revision_root / "evidence.json", "historical revision evidence")
    )
    inventory = validate_historical_revision_dependency_inventory(
        _load_json(
            revision_root / "dependency_inventory.json",
            "historical revision dependency inventory",
        )
    )
    invalidations = validate_historical_revision_invalidation_manifest(
        _load_json(revision_root / "invalidations.json", "historical revision invalidations")
    )
    report = validate_historical_revision_impact_report(
        _load_json(revision_root / "impact_report.json", "historical revision impact report")
    )
    revision_transaction = validate_historical_revision_transaction(
        _load_json(revision_root / "transaction.json", "historical revision transaction")
    )
    _verify_persisted_revision_records(
        record=record,
        evidence=evidence,
        inventory=inventory,
        invalidations=invalidations,
        report=report,
        transaction=revision_transaction,
    )

    event_store = Path(root_map["runtime"]) / "events"
    batches = load_memory_event_batches(event_store)
    matching = [item for item in batches if item["batch_id"] == record["batch_id"]]
    if len(matching) != 1:
        raise HistoricalRevisionExecutionError(
            "historical_revision_batch_missing",
            "receipt-bound historical revision batch is not unique in event authority",
        )
    batch = validate_memory_event_batch(matching[0])
    _verify_revision_batch(record, revision_transaction, batch)
    base_projection, revision_projection = _projection_around_batch(
        batches, record["batch_id"]
    )
    before = revision_transaction["authority_before"]
    after = revision_transaction["authority_after"]
    if (
        base_projection["authority_epoch"] != before["authority_epoch"]
        or base_projection["revision"] != before["revision"]
        or base_projection["head_event_hash"] != before["head_event_hash"]
        or revision_projection["authority_epoch"] != after["authority_epoch"]
        or revision_projection["revision"] != after["revision"]
        or revision_projection["head_event_hash"] != after["head_event_hash"]
        or memory_projection_hash(revision_projection) != record["projection_hash"]
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_replay_mismatch",
            "persisted batch does not reproduce its receipt-bound authority transition",
        )

    replay = replay_memory_events(event_store, use_checkpoint=False)
    canonical = load_canonical_memory(Path(root_map["runtime"]) / "canonical_memory.json")
    if replay["projection"] != canonical:
        raise HistoricalRevisionExecutionError(
            "historical_revision_canonical_drift",
            "canonical memory differs from a checkpoint-free event replay",
        )
    identity = load_project_identity(root_map["story_project"])
    authority = identity.authority if identity is not None else None
    if (
        identity is None
        or identity.book_id != record["book_id"]
        or not isinstance(authority, Mapping)
        or authority.get("authority_epoch") != canonical["authority_epoch"]
        or authority.get("head_event_hash") != canonical["head_event_hash"]
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_identity_head_mismatch",
            "ProjectIdentity and replayed canonical authority heads differ",
        )

    source_revision = receipt.get("story_project_source_revision_after")
    identity_binding = apply_bindings[-1]
    if identity_binding["sha256"] != revision_transaction["identity_after_sha256"]:
        raise HistoricalRevisionExecutionError(
            "historical_revision_identity_head_mismatch",
            "receipt identity bytes differ from the immutable revision transaction",
        )
    expected_source_revision = {
        "schema_version": "1.0",
        "book_id": record["book_id"],
        "root_uuid": registry["roots"]["story_project"]["root_uuid"],
        "identity_sha256": identity_binding["sha256"],
        "authority_epoch": after["authority_epoch"],
        "head_event_hash": after["head_event_hash"],
    }
    if source_revision != expected_source_revision:
        raise HistoricalRevisionExecutionError(
            "historical_revision_source_revision_mismatch",
            "receipt source revision does not bind the identity authority transition",
        )
    _assert_historical_chapter_current(
        historical_path, revision_transaction["source_hashes"]
    )
    return {
        "schema_version": HISTORICAL_REVISION_EXECUTION_SCHEMA_VERSION,
        "book_id": record["book_id"],
        "revision_kind": revision_kind,
        "transaction_id": transaction_id,
        "run_id": record["run_id"],
        "head_event_hash": after["head_event_hash"],
        "current_head_event_hash": canonical["head_event_hash"],
        "projection": revision_projection,
        "current_projection": canonical,
        "record": record,
        "historical_revision_transaction": revision_transaction,
        "publication_receipt": receipt,
        "verification": verification,
    }


def _verify_persisted_revision_records(
    *,
    record: Mapping[str, Any],
    evidence: Mapping[str, Any],
    inventory: Mapping[str, Any],
    invalidations: Mapping[str, Any],
    report: Mapping[str, Any],
    transaction: Mapping[str, Any],
) -> None:
    common = {
        "book_id": record["book_id"],
        "revision_kind": record["revision_kind"],
        "transaction_id": record["transaction_id"],
        "historical_chapter_index": record["historical_chapter_index"],
        "historical_chapter_relative_path": record[
            "historical_chapter_relative_path"
        ],
    }
    for value in (evidence, invalidations, report, transaction):
        if any(value.get(key) != expected for key, expected in common.items()):
            raise HistoricalRevisionExecutionError(
                "historical_revision_artifact_mismatch",
                "immutable revision artifacts identify different transactions",
            )
    if (
        transaction["transaction_hash"] != record["transaction_hash"]
        or transaction["authority_before"] != record["authority_before"]
        or transaction["authority_after"] != record["authority_after"]
        or transaction["batch_hash"] != record["batch_hash"]
        or transaction["source_hashes"]["historical_chapter_sha256"]
        != record["historical_chapter_sha256"]
        or transaction["source_hashes"]["revision_source_sha256"]
        != record["revision_source_sha256"]
        or evidence["evidence_hash"] != transaction["evidence_hash"]
        or invalidations["manifest_hash"] != transaction["invalidation_manifest_hash"]
        or report["report_hash"] != transaction["impact_report_hash"]
        or report["source_hashes"] != transaction["source_hashes"]
        or inventory["inventory_hash"]
        != transaction["source_hashes"]["dependency_inventory_hash"]
        or invalidations["authority_before"] != transaction["authority_before"]
        or invalidations["authority_after"] != transaction["authority_after"]
        or report["authority_before"] != transaction["authority_before"]
        or report["authority_after"] != transaction["authority_after"]
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_artifact_mismatch",
            "immutable revision artifact hashes or authority bindings differ",
        )


def _verify_revision_batch(
    record: Mapping[str, Any],
    transaction: Mapping[str, Any],
    batch: Mapping[str, Any],
) -> None:
    if (
        batch["book_id"] != record["book_id"]
        or batch["batch_kind"] != record["revision_kind"]
        or batch["batch_id"] != transaction["batch_id"]
        or batch["batch_hash"] != transaction["batch_hash"]
        or memory_patch_content_hash(batch["patch"])
        != transaction["patch_content_hash"]
        or batch["source_project_digest"]
        != transaction["source_hashes"]["source_project_digest"]
        or batch["context_digest"] != transaction["source_hashes"]["context_digest"]
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_batch_mismatch",
            "event batch differs from the immutable revision transaction",
        )


def _projection_around_batch(
    batches: list[dict[str, Any]], target_batch_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    projection: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    for batch in batches:
        if batch["batch_kind"] == "genesis":
            projection = apply_genesis_event(batch["events"][0])
        else:
            if projection is None:
                raise HistoricalRevisionExecutionError(
                    "historical_revision_replay_mismatch", "event history has no genesis"
                )
            if batch["batch_id"] == target_batch_id:
                before = copy.deepcopy(projection)
            reducer = str(batch.get("reducer_version") or CURRENT_REDUCER_VERSION)
            projection = apply_memory_events(
                projection, batch["events"], reducer_version=reducer
            )
        if batch["batch_id"] == target_batch_id:
            after = copy.deepcopy(projection)
            break
    if before is None or after is None:
        raise HistoricalRevisionExecutionError(
            "historical_revision_batch_missing", "cannot locate revision batch in replay"
        )
    return before, after


def _persistence_targets(
    prepared: Mapping[str, Any],
    *,
    root_map: Mapping[str, Path],
    registry: Mapping[str, Any],
    memory_root: Path,
    story_root: Path,
    historical_path: Path,
    revision_source_path: Path,
) -> list[PersistenceV2Target]:
    identity_path = project_identity_path(story_root).resolve()
    memory = memory_root.resolve()
    forbidden_paths = {historical_path.resolve(), revision_source_path.resolve()}
    converted: list[PersistenceV2Target] = []
    identity_targets: list[PersistenceV2Target] = []
    for index, raw in enumerate(prepared["targets"], start=1):
        path = Path(raw["path"]).resolve()
        kind = str(raw["kind"])
        if path in forbidden_paths or kind in _FORBIDDEN_TARGET_KINDS:
            raise HistoricalRevisionExecutionError(
                "published_prose_in_place_edit_forbidden",
                "historical revision targets immutable source bytes",
            )
        if kind == "project_identity":
            if path != identity_path:
                raise HistoricalRevisionExecutionError(
                    "historical_revision_identity_target_invalid",
                    "ProjectIdentity target path is not canonical",
                )
            root_id = "story_project"
        else:
            try:
                path.relative_to(memory)
            except ValueError as exc:
                raise HistoricalRevisionExecutionError(
                    "historical_revision_target_outside_memory_root",
                    f"non-identity target escapes Memory V2 root: {path}",
                ) from exc
            root_id = "runtime"
        target = PersistenceV2Target(
            target_id=f"hr-{index:03d}",
            kind=kind,
            path_ref=path_ref_for(
                path,
                root_id=root_id,
                root=root_map[root_id],
                root_uuid=registry["roots"][root_id]["root_uuid"],
            ),
            content=str(raw["content"]),
            metadata={"historical_revision": True, "immutable_source_safe": True},
            expected_before_exists=bool(raw["expected_before_exists"]),
            expected_before_sha256=raw["expected_before_sha256"],
        )
        if kind == "project_identity":
            identity_targets.append(target)
        else:
            converted.append(target)
    if len(identity_targets) != 1:
        raise HistoricalRevisionExecutionError(
            "historical_revision_identity_target_invalid",
            "prepared bundle must contain exactly one ProjectIdentity target",
        )
    return [*converted, identity_targets[0]]


def _target_bindings(targets: list[PersistenceV2Target]) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for target in targets:
        content = target.content_bytes()
        ref = target.path_ref
        if not isinstance(ref, PathRef):
            raise HistoricalRevisionExecutionError(
                "historical_revision_path_ref_invalid", "target PathRef is not bound"
            )
        bindings.append(
            {
                "target_id": target.target_id,
                "kind": target.kind,
                "path_ref": ref.to_dict(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    return bindings


def _execution_record(
    *,
    prepared: Mapping[str, Any],
    run_id: str,
    request_digest: str,
    story_root_uuid: str,
    target_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    transaction = prepared["transaction"]
    audit = prepared["audit"]
    return {
        "schema_version": HISTORICAL_REVISION_EXECUTION_SCHEMA_VERSION,
        "record_type": "historical_revision_execution",
        "state": "committed",
        "book_id": transaction["book_id"],
        "revision_kind": transaction["revision_kind"],
        "transaction_id": transaction["transaction_id"],
        "run_id": run_id,
        "request_digest": request_digest,
        "story_project_root_uuid": story_root_uuid,
        "historical_chapter_index": transaction["historical_chapter_index"],
        "historical_chapter_relative_path": transaction[
            "historical_chapter_relative_path"
        ],
        "historical_chapter_sha256": transaction["source_hashes"][
            "historical_chapter_sha256"
        ],
        "revision_source_sha256": transaction["source_hashes"][
            "revision_source_sha256"
        ],
        "batch_id": transaction["batch_id"],
        "batch_hash": transaction["batch_hash"],
        "transaction_hash": transaction["transaction_hash"],
        "projection_hash": audit["projection_hash"],
        "authority_before": copy.deepcopy(transaction["authority_before"]),
        "authority_after": copy.deepcopy(transaction["authority_after"]),
        "target_count": len(target_bindings),
        "target_roles": [item["kind"] for item in target_bindings],
        "target_bundle_digest": canonical_json_hash(target_bindings),
        "delivery_jobs": [],
    }


def _validate_execution_record(record: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "record_type",
        "state",
        "book_id",
        "revision_kind",
        "transaction_id",
        "run_id",
        "request_digest",
        "story_project_root_uuid",
        "historical_chapter_index",
        "historical_chapter_relative_path",
        "historical_chapter_sha256",
        "revision_source_sha256",
        "batch_id",
        "batch_hash",
        "transaction_hash",
        "projection_hash",
        "authority_before",
        "authority_after",
        "target_count",
        "target_roles",
        "target_bundle_digest",
        "delivery_jobs",
        "publication_receipt",
    }
    if not required.issubset(record) or any(
        not _SHA256.fullmatch(str(record.get(field) or ""))
        for field in (
            "request_digest",
            "historical_chapter_sha256",
            "revision_source_sha256",
            "batch_hash",
            "transaction_hash",
            "projection_hash",
            "target_bundle_digest",
        )
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_record_invalid", "completion record is malformed"
        )
    if (
        record["schema_version"] != HISTORICAL_REVISION_EXECUTION_SCHEMA_VERSION
        or record["record_type"] != "historical_revision_execution"
        or record["state"] != "committed"
        or record["revision_kind"] not in HISTORICAL_REVISION_KINDS
        or record["delivery_jobs"] != []
        or not isinstance(record["target_roles"], list)
        or record["target_count"] != len(record["target_roles"])
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_record_invalid", "completion record fields are invalid"
        )


def _assert_prepared_identity(
    prepared: Mapping[str, Any],
    *,
    revision_kind: str,
    transaction_id: str,
    book_id: str,
    story_root_uuid: str,
) -> None:
    transaction = prepared["transaction"]
    source_revision = prepared["story_project_source_revision_after"]
    if (
        prepared.get("status") != "prepared"
        or prepared.get("revision_kind") != revision_kind
        or prepared.get("transaction_id") != transaction_id
        or transaction.get("book_id") != book_id
        or source_revision.get("root_uuid") != story_root_uuid
        or source_revision.get("book_id") != book_id
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_prepare_mismatch",
            "pure prepare output differs from the locked execution request",
        )


def _assert_source_hashes_current(
    historical_path: Path,
    revision_source_path: Path,
    source_hashes: Mapping[str, Any],
) -> None:
    expected = (
        (
            historical_path,
            "historical_chapter_sha256",
            "historical_chapter_source_drift",
        ),
        (revision_source_path, "revision_source_sha256", "revision_source_drift"),
    )
    for path, field, code in expected:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise HistoricalRevisionExecutionError(
                code, f"cannot read immutable revision source: {exc}"
            ) from exc
        if hashlib.sha256(content).hexdigest() != source_hashes.get(field):
            raise HistoricalRevisionExecutionError(
                code, "immutable revision source bytes changed during execution"
            )


def _assert_historical_chapter_current(
    historical_path: Path,
    source_hashes: Mapping[str, Any],
) -> None:
    try:
        content = historical_path.read_bytes()
    except OSError as exc:
        raise HistoricalRevisionExecutionError(
            "historical_chapter_source_drift",
            f"cannot read immutable historical prose: {exc}",
        ) from exc
    if hashlib.sha256(content).hexdigest() != source_hashes.get(
        "historical_chapter_sha256"
    ):
        raise HistoricalRevisionExecutionError(
            "historical_chapter_source_drift",
            "immutable historical prose changed after the revision was committed",
        )


def _assert_dependency_inventory_current(
    prepared: Mapping[str, Any],
    *,
    story_project_root: Path,
) -> None:
    transaction = prepared["transaction"]
    inventory = validate_historical_revision_dependency_inventory(
        prepared["dependency_inventory"]
    )
    authority_before = transaction["authority_before"]
    captured = capture_historical_revision_dependency_inventory(
        story_project_root=story_project_root,
        book_id=str(transaction["book_id"]),
        authority_epoch=int(authority_before["authority_epoch"]),
        head_event_hash=str(authority_before["head_event_hash"]),
        historical_chapter_index=int(transaction["historical_chapter_index"]),
        canonical_next_chapter_index=int(inventory["canonical_next_chapter_index"]),
    )
    if (
        captured != inventory
        or captured["inventory_hash"]
        != transaction["source_hashes"]["dependency_inventory_hash"]
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_dependency_inventory_drift",
            "durable outline/session dependencies changed after prepare",
        )


def _request_digest(
    *,
    revision_kind: str,
    book_id: str,
    story_root: Path,
    memory_root: Path,
    story_root_uuid: str,
    prepare_kwargs: Mapping[str, Any],
) -> str:
    historical = Path(prepare_kwargs["historical_chapter_path"]).resolve()
    try:
        historical_relative = historical.relative_to(story_root).as_posix()
    except ValueError as exc:
        raise HistoricalRevisionExecutionError(
            "historical_chapter_outside_story_project",
            "historical chapter escapes StoryProject root",
        ) from exc
    projection_raw = prepare_kwargs.get("projection_root")
    projection = Path(projection_raw).resolve() if projection_raw is not None else memory_root / "projections"
    try:
        projection_relative = projection.relative_to(memory_root).as_posix()
    except ValueError as exc:
        raise HistoricalRevisionExecutionError(
            "historical_revision_target_outside_memory_root",
            "projection root escapes Memory V2 root",
        ) from exc
    basis = {
        "schema_version": HISTORICAL_REVISION_EXECUTION_SCHEMA_VERSION,
        "revision_kind": revision_kind,
        "book_id": book_id,
        "story_project_root_uuid": story_root_uuid,
        "transaction_id": prepare_kwargs.get("transaction_id"),
        "historical_chapter_index": prepare_kwargs.get("historical_chapter_index"),
        "historical_chapter_relative_path": historical_relative,
        "expected_historical_chapter_sha256": prepare_kwargs.get(
            "expected_historical_chapter_sha256"
        ),
        "expected_revision_source_sha256": prepare_kwargs.get(
            "expected_revision_source_sha256"
        ),
        "evidence_spans": prepare_kwargs.get("evidence_spans"),
        "operations": prepare_kwargs.get("operations"),
        "authority_epoch": prepare_kwargs.get("authority_epoch"),
        "expected_head_event_hash": prepare_kwargs.get("expected_head_event_hash"),
        "expected_revision": prepare_kwargs.get("expected_revision"),
        "source_project_digest": prepare_kwargs.get("source_project_digest"),
        "context_digest": prepare_kwargs.get("context_digest"),
        "dependency_inventory": prepare_kwargs.get("dependency_inventory"),
        "reconciliation": prepare_kwargs.get("reconciliation"),
        "projection_relative_path": projection_relative,
    }
    try:
        normalized = json.loads(
            json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise HistoricalRevisionExecutionError(
            "historical_revision_request_invalid",
            f"historical revision request is not canonical JSON: {exc}",
        ) from exc
    return canonical_json_hash(normalized)


def _execution_layout(memory_root: Path, transaction_id: str) -> dict[str, Any]:
    home = memory_root / "history_revision_execution"
    key = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()[:32]
    return {
        "home": home,
        "operation_key": key,
        # Reconciliation may roll back any marker-less pending journal in the
        # shared transaction root.  One operation lock for the whole Memory V2
        # authority prevents it from mistaking another live revision prepare
        # for an abandoned crash.
        "operation_lock": home / "operation_lock",
        "transaction_root": home / "persistence",
        "publication_receipt": home / "publication_receipts" / f"{key}.json",
        "completed_record": home / "completed" / f"{key}.json",
        "revision_root": memory_root / "history_revisions" / transaction_id,
        "receipt_id": f"history-revision-{key[:24]}",
    }


def _runtime_ref(
    path: Path,
    *,
    root_map: Mapping[str, Path],
    registry: Mapping[str, Any],
) -> PathRef:
    return path_ref_for(
        path,
        root_id="runtime",
        root=root_map["runtime"],
        root_uuid=registry["roots"]["runtime"]["root_uuid"],
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HistoricalRevisionExecutionError(
            "historical_revision_artifact_invalid", f"cannot read {label}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise HistoricalRevisionExecutionError(
            "historical_revision_artifact_invalid", f"{label} must be an object"
        )
    return value


def _required_path(kwargs: Mapping[str, Any], field: str) -> Path:
    if field not in kwargs:
        raise HistoricalRevisionExecutionError(
            "historical_revision_request_invalid", f"missing required field: {field}"
        )
    return Path(kwargs[field]).absolute()


def _transaction_id(value: Any) -> str:
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
        or not _SAFE_TRANSACTION_ID.fullmatch(value)
        or value.endswith((".", " "))
        or base in reserved
    ):
        raise HistoricalRevisionExecutionError(
            "historical_revision_transaction_id_invalid",
            "transaction_id must be a safe 1-96 character identifier",
        )
    return value


def _revision_kind(value: Any) -> str:
    if not isinstance(value, str) or value not in HISTORICAL_REVISION_KINDS:
        raise HistoricalRevisionExecutionError(
            "unknown_historical_revision_kind",
            f"unsupported historical revision kind: {value!r}",
        )
    return value


__all__ = [
    "HISTORICAL_REVISION_EXECUTION_SCHEMA_VERSION",
    "HistoricalRevisionExecutionError",
    "execute_amend_transaction",
    "execute_historical_revision_transaction",
    "execute_import_transaction",
    "execute_retcon_transaction",
]
