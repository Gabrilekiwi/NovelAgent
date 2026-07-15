from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
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
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import (
    create_genesis_memory_batch,
    create_memory_checkpoint,
    create_memory_event_batch,
    load_memory_event_batches,
    memory_patch_content_hash,
    replay_memory_events,
)
from core.memory_v2.events import create_memory_event_context
from core.memory_v2.patch import create_memory_patch
from core.memory_v2.projection import rebuild_memory_projections
from core.memory_v2.reducer import (
    CURRENT_REDUCER_VERSION,
    apply_genesis_event,
    apply_memory_events,
    apply_memory_patch,
)
from core.memory_v2.storage import load_canonical_memory
from core.path_refs import path_ref_for
from core.story_project.authority import (
    AUTHORITY_MODE_EVENT,
    AUTHORITY_MODE_LEGACY,
    CURRENT_WRITER_CONTRACT,
    authority_receipt_path,
    build_authority_activation_receipt,
)
from core.story_project.identity import (
    LEGACY_AUTHORITY_PROJECTION,
    PROJECT_IDENTITY_V2_SCHEMA_VERSION,
    ProjectIdentity,
    load_project_identity,
    project_identity_path,
    validate_project_identity,
)
from core.story_project.migration_v2 import (
    MigrationV2Error,
    assert_migration_plan_current,
    assert_migration_source_snapshot_current,
    validate_migration_approval,
    validate_migration_plan,
)
from core.story_project.read_set import capture_story_project_read_set


MIGRATION_EXECUTION_SCHEMA_VERSION = "1.0"
MIGRATION_MEMORY_RELATIVE_PREFIX = ".novelagent/runtime/memory/v2"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_FaultInjector = Callable[[str, int | None, Path | None], None]


class MigrationExecutionError(MigrationV2Error):
    pass


def execute_event_authority_migration(
    story_project_root: str | Path,
    *,
    plan: Mapping[str, Any],
    approval: Mapping[str, Any] | None,
    fault_injector: _FaultInjector | None = None,
    attempt_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> dict[str, Any]:
    """Execute one approved legacy-to-event bootstrap as one V2 transaction.

    The immutable approval is the explicit confirmation boundary.  The
    transaction writes a Memory 2.2 genesis and one ``source_sync`` batch,
    projections, checkpoint, activation proof, and finally ProjectIdentity.
    No provider or remote delivery adapter is reachable from this service.
    """

    validated_plan = validate_migration_plan(dict(plan))
    if approval is None:
        raise MigrationExecutionError(
            "migration_approval_required",
            "migration execution requires an explicit MigrationApproval",
        )
    validated_approval = validate_migration_approval(dict(approval), plan=validated_plan)
    root = Path(story_project_root).resolve()
    if not root.is_dir():
        raise MigrationExecutionError(
            "migration_story_project_invalid", "StoryProject root is not a directory"
        )
    layout = _migration_layout(root, validated_approval)
    root_map = {"story_project": root, "runtime": root / ".novelagent"}

    with event_authority_write_operation(
        root,
        expected_book_id=validated_plan["book_id"],
        writer_kind="migration",
    ) as authority_operation:
        recovery = reconcile_pending_persistence_v2(
            layout["transaction_root"], expected_book_id=validated_plan["book_id"]
        )
        if not recovery["ok"]:
            raise MigrationExecutionError(
                "migration_recovery_required",
                f"migration persistence requires recovery: {recovery['recovery_required']}",
            )
        completed = _load_completed_migration(
            root=root,
            root_map=root_map,
            layout=layout,
            plan=validated_plan,
            approval=validated_approval,
        )
        if completed is not None:
            completed["idempotent"] = True
            return completed

        identity = load_project_identity(root)
        if identity is None or identity.ephemeral:
            raise MigrationExecutionError(
                "migration_identity_missing", "a stable ProjectIdentity is required"
            )
        if identity.book_id != validated_plan["book_id"]:
            raise MigrationExecutionError(
                "migration_identity_changed", "ProjectIdentity book_id differs from the approved plan"
            )
        authority = identity.authority or {}
        if authority.get("mode") != AUTHORITY_MODE_LEGACY:
            raise MigrationExecutionError(
                "migration_event_authority_already_active",
                "event authority is already active; migration cannot replay or downgrade it",
            )
        if authority != LEGACY_AUTHORITY_PROJECTION:
            raise MigrationExecutionError(
                "migration_legacy_authority_invalid",
                "migration requires the exact epoch-0 legacy authority projection",
            )

        assert_migration_plan_current(validated_plan, root)
        _assert_event_storage_empty(layout["memory_root"])
        read_set = capture_story_project_read_set(
            root,
            _next_source_chapter(validated_plan),
            project_identity=identity,
        )
        bootstrap = _build_bootstrap(
            root=root,
            identity=identity,
            plan=validated_plan,
            approval=validated_approval,
        )

        registry_service = RootRegistryService(layout["transaction_root"])
        registry = registry_service.ensure(root_map)
        resolver = registry_service.resolver(registry)
        apply_targets = _build_apply_targets(
            root=root,
            root_map=root_map,
            registry=registry,
            layout=layout,
            bootstrap=bootstrap,
        )
        for target in apply_targets:
            resolver.ensure_parent(target.path_ref)

        identity_bytes = _json_bytes(bootstrap["identity_after"].to_dict())
        identity_after_sha256 = hashlib.sha256(identity_bytes).hexdigest()
        identity_declaration = {
            "relative_path": ".novelagent/project.json",
            "role": "project_identity",
            "action": "replace",
            "after_sha256": identity_after_sha256,
            "after_size": len(identity_bytes),
            "book_id": identity.book_id,
            "expected_authority_epoch": 0,
            "expected_head_event_hash": None,
            "after_authority_epoch": 1,
            "after_head_event_hash": bootstrap["canonical"]["head_event_hash"],
        }
        receipt_ref = _runtime_ref(
            layout["publication_receipt"], root_map=root_map, registry=registry
        )
        final_ref = _runtime_ref(layout["completed_record"], root_map=root_map, registry=registry)
        final_record = bind_final_run_record_receipt(
            _final_record(plan=validated_plan, approval=validated_approval, bootstrap=bootstrap),
            receipt_id=layout["receipt_id"],
            receipt_path_ref=receipt_ref,
        )
        artifacts = _build_publication_artifacts(
            root_map=root_map,
            registry=registry,
            layout=layout,
            plan=validated_plan,
            approval=validated_approval,
            bootstrap=bootstrap,
        )
        # Freeze all publication parent identities before prepare.  Creating a
        # sibling artifact directory after guards are captured would otherwise
        # look like a parent-swap during publication on Windows.
        for target in artifacts:
            resolver.ensure_parent(target.path_ref)
        resolver.ensure_parent(final_ref)
        resolver.ensure_parent(receipt_ref)
        source_revision_after = {
            "schema_version": "1.0",
            "book_id": identity.book_id,
            "root_uuid": registry["roots"]["story_project"]["root_uuid"],
            "identity_sha256": identity_after_sha256,
            "authority_epoch": 1,
            "head_event_hash": bootstrap["canonical"]["head_event_hash"],
        }

        def guarded_fault(point: str, index: int | None, path: Path | None) -> None:
            if fault_injector is not None:
                fault_injector(point, index, path)
            if point == "before_commit_marker":
                assert_migration_source_snapshot_current(
                    validated_plan,
                    root,
                    expected_identity_sha256=identity_after_sha256,
                    ignored_relative_prefixes=(MIGRATION_MEMORY_RELATIVE_PREFIX,),
                )

        run_id = (
            f"m-{validated_approval['approval_hash'][:8]}-"
            f"{attempt_id_factory().hex[:6]}"
        )
        transaction = PersistenceV2Transaction(
            transaction_root=layout["transaction_root"],
            run_id=run_id,
            book_id=identity.book_id,
            root_map=root_map,
            fault_injector=guarded_fault,
            story_project_read_set=read_set,
            read_set_declared_writes=[identity_declaration],
        )
        try:
            authority_operation.prepare_transaction(
                transaction,
                apply_targets=apply_targets,
                artifacts=artifacts,
                final_run_record=final_record,
                final_run_path_ref=final_ref,
                receipt_id=layout["receipt_id"],
                receipt_path_ref=receipt_ref,
                context_digest=bootstrap["context_digest"],
                generation_input_context_digest=validated_approval["approval_hash"],
                story_project_source_revision_after=source_revision_after,
                candidate_result=final_record,
                delivery_jobs=[],
            )
            committed = authority_operation.commit_transaction(transaction)
        except Exception as exc:
            raise MigrationExecutionError(
                "migration_bootstrap_failed", f"atomic bootstrap failed: {exc}"
            ) from exc
        if not committed.get("committed") or committed.get("state") != "completed":
            raise MigrationExecutionError(
                "migration_bootstrap_incomplete",
                f"atomic bootstrap did not produce a durable PublicationReceipt: {committed}",
            )

        completed = _load_completed_migration(
            root=root,
            root_map=root_map,
            layout=layout,
            plan=validated_plan,
            approval=validated_approval,
        )
        if completed is None:
            raise MigrationExecutionError(
                "migration_receipt_missing", "completed bootstrap has no verifiable receipt"
            )
        completed["idempotent"] = False
        return completed


def _build_bootstrap(
    *,
    root: Path,
    identity: ProjectIdentity,
    plan: dict[str, Any],
    approval: dict[str, Any],
) -> dict[str, Any]:
    evidence_document = {
        "plan": {
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "source_digest": plan["source_digest"],
            "sources": copy.deepcopy(plan["sources"]),
        },
        "approval": {
            "approval_id": approval["approval_id"],
            "approval_hash": approval["approval_hash"],
            "decision_digest": approval["decision_digest"],
            "decisions": copy.deepcopy(approval["decisions"]),
        },
    }
    evidence_text = json.dumps(
        evidence_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    context_digest = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
    genesis = create_genesis_memory_batch(
        book_id=identity.book_id,
        title=root.name or "Migrated StoryProject",
        source_project_digest=plan["source_digest"],
        context_digest=context_digest,
        authority_epoch=1,
        evidence_text=f"Approved migration genesis for {plan['plan_id']}",
    )
    base = apply_genesis_event(genesis["events"][0])
    operations = _baseline_operations(plan, approval)
    patch = create_memory_patch(
        patch_id=f"source_sync_{approval['approval_hash'][:24]}",
        source_kind="approved_migration_source_sync",
        source_path=f"migration-plan:{plan['plan_id']}",
        operations=operations,
        metadata={
            "source_item_count": len(plan["sources"]),
            "plan_hash": plan["plan_hash"],
            "approval_hash": approval["approval_hash"],
            "history_policy": "source_sync_only",
        },
    )
    event_context = create_memory_event_context(
        chapter_body=evidence_text,
        evidence_spans=[{"start": 0, "end": len(evidence_text), "quote": evidence_text}],
        authority_epoch=1,
    )
    canonical, events = apply_memory_patch(
        base,
        patch,
        reducer_version=CURRENT_REDUCER_VERSION,
        event_context=event_context,
    )
    if apply_memory_events(
        base, events, reducer_version=CURRENT_REDUCER_VERSION
    ) != canonical:
        raise MigrationExecutionError(
            "migration_baseline_replay_failed", "source_sync events do not reproduce canonical memory"
        )
    source_sync = create_memory_event_batch(
        book_id=identity.book_id,
        patch=patch,
        events=events,
        expected_revision=int(base["revision"]),
        previous_batch_hash=genesis["batch_hash"],
        source_project_digest=plan["source_digest"],
        context_digest=context_digest,
        batch_kind="source_sync",
        publication_status="source_sync",
        schema_version="2.2",
        reducer_version=CURRENT_REDUCER_VERSION,
    )
    checkpoint = create_memory_checkpoint(
        projection=canonical,
        last_batch=source_sync,
        committed_chapter_count=0,
        patch_index={
            genesis["patch_id"]: genesis["patch_content_hash"],
            patch["patch_id"]: memory_patch_content_hash(patch),
        },
        quality_state={},
    )
    projections = rebuild_memory_projections(canonical)
    activation = build_authority_activation_receipt(
        book_id=identity.book_id,
        expected_identity_sha256=plan["expected_identity_sha256"],
        head_event_hash=canonical["head_event_hash"],
        authority_epoch=1,
        minimum_writer_contract=CURRENT_WRITER_CONTRACT,
        now=_approved_time_factory(approval["approved_at"]),
    )
    identity_after = validate_project_identity(
        replace(
            identity,
            schema_version=PROJECT_IDENTITY_V2_SCHEMA_VERSION,
            root_hint=".",
            authority={
                "mode": AUTHORITY_MODE_EVENT,
                "authority_epoch": 1,
                "head_event_hash": canonical["head_event_hash"],
                "activation_receipt": activation,
                "minimum_writer_contract": CURRENT_WRITER_CONTRACT,
            },
        ).to_dict()
    )
    baseline_manifest = _baseline_manifest(
        plan=plan,
        approval=approval,
        genesis=genesis,
        source_sync=source_sync,
        canonical=canonical,
        checkpoint=checkpoint,
    )
    return {
        "genesis": genesis,
        "source_sync": source_sync,
        "canonical": canonical,
        "checkpoint": checkpoint,
        "projections": projections,
        "activation": activation,
        "identity_after": identity_after,
        "context_digest": context_digest,
        "baseline_manifest": baseline_manifest,
    }


def _baseline_operations(plan: dict[str, Any], approval: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = approval["decisions"]
    chapters = sorted(
        {
            int(item["chapter_index"])
            for item in plan["sources"]
            if item["role"] == "published_prose" and item["chapter_index"] is not None
        }
    )
    last_source_chapter = max(chapters, default=0)
    operations: list[dict[str, Any]] = [
        {
            "op": "update_current_state",
            "value": {
                "chapter_index": last_source_chapter + 1,
                "last_published_source_chapter_index": last_source_chapter,
                "migration_baseline": {
                    "plan_hash": plan["plan_hash"],
                    "approval_hash": approval["approval_hash"],
                    "source_digest": plan["source_digest"],
                    "published_source_chapters": chapters,
                    "history_policy": "source_sync_only",
                },
            },
        },
        {
            "op": "update_story_time",
            "value": {
                "label": "approved_migration_baseline",
                "elapsed_minutes": decisions["timeline_elapsed_minutes"],
                "chapter_index": last_source_chapter,
                "scene_index": 0,
            },
        },
    ]
    for external_id, raw in sorted(decisions["chapter_10_character_state"].items()):
        if not isinstance(raw, dict):
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"chapter_10_character_state.{external_id} must be an object",
            )
        value = copy.deepcopy(raw)
        value.setdefault("name", str(external_id))
        approved_status = value.pop("status", None)
        raw_data = value.pop("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"approved_data": raw_data}
        value["status"] = approved_status if approved_status in {"active", "missing", "dead", "unknown"} else "unknown"
        value["data"] = {
            **raw_data,
            "external_id": str(external_id),
            "source_chapter": 10,
            **({"approved_status": approved_status} if approved_status is not None else {}),
        }
        operations.append(
            {"op": "upsert_character", "id": _record_id("character", external_id), "value": value}
        )
    for index, raw in enumerate(decisions["open_foreshadowing"]):
        item = copy.deepcopy(raw) if isinstance(raw, dict) else {"description": str(raw)}
        external_id = str(item.get("id") or f"item-{index + 1}")
        item.pop("id", None)
        item.setdefault("description", external_id)
        approved_status = item.pop("status", None)
        raw_data = item.pop("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"approved_data": raw_data}
        item["status"] = (
            approved_status
            if approved_status in {"seeded", "developing", "ripe"}
            else "seeded"
        )
        item["data"] = {
            **raw_data,
            "external_id": external_id,
            **({"approved_status": approved_status} if approved_status is not None else {}),
        }
        operations.append(
            {"op": "upsert_foreshadowing", "id": _record_id("thread", external_id), "value": item}
        )
    for owner_id, raw_items in sorted(decisions["inventory"].items()):
        if not isinstance(raw_items, dict):
            raise MigrationExecutionError(
                "migration_decisions_invalid", f"inventory.{owner_id} must be an object"
            )
        items = {
            _record_id("item", item_id): _inventory_item(item_id, raw_item)
            for item_id, raw_item in sorted(raw_items.items())
        }
        owner_ref = (
            _record_id("character", owner_id)
            if owner_id in decisions["chapter_10_character_state"]
            else "world"
        )
        operations.append(
            {
                "op": "upsert_inventory",
                "id": _record_id("inventory", owner_id),
                "value": {
                    "owner_id": owner_ref,
                    "items": items,
                    "data": {"external_id": str(owner_id)},
                },
            }
        )
    for term, raw in sorted(decisions["lexicon"].items()):
        value = copy.deepcopy(raw) if isinstance(raw, dict) else {"definition": str(raw)}
        definition = str(value.pop("definition", term))
        operations.append(
            {
                "op": "upsert_glossary_entry",
                "id": _record_id("term", term),
                "value": {
                    "term": str(term),
                    "definition": definition,
                    "status": "active",
                    "data": {"external_id": str(term), **value},
                },
            }
        )
    for subject_id, raw in sorted(decisions["corruption"].items()):
        value = copy.deepcopy(raw) if isinstance(raw, dict) else {"level": raw}
        level = value.get("level")
        if (
            isinstance(level, bool)
            or not isinstance(level, (int, float))
            or not 0 <= level <= 100
        ):
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"corruption.{subject_id}.level must be between 0 and 100",
            )
        value["subject_id"] = (
            _record_id("character", subject_id)
            if subject_id in decisions["chapter_10_character_state"]
            else "world"
        )
        approved_status = value.pop("status", None)
        value["status"] = (
            approved_status
            if approved_status in {"stable", "rising", "falling", "cleansed"}
            else "stable"
        )
        raw_data = value.pop("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"approved_data": raw_data}
        value["data"] = {
            **raw_data,
            "external_id": str(subject_id),
            **({"approved_status": approved_status} if approved_status is not None else {}),
        }
        operations.append(
            {"op": "upsert_corruption", "id": _record_id("corruption", subject_id), "value": value}
        )
    return operations


def _baseline_manifest(
    *,
    plan: dict[str, Any],
    approval: dict[str, Any],
    genesis: dict[str, Any],
    source_sync: dict[str, Any],
    canonical: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema_version": MIGRATION_EXECUTION_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "approval_id": approval["approval_id"],
        "approval_hash": approval["approval_hash"],
        "book_id": plan["book_id"],
        "source_digest": plan["source_digest"],
        "authority_epoch": 1,
        "head_event_hash": canonical["head_event_hash"],
        "genesis_batch_hash": genesis["batch_hash"],
        "source_sync_batch_hash": source_sync["batch_hash"],
        "checkpoint_hash": checkpoint["checkpoint_hash"],
        "history_policy": {
            "source_sync_only": True,
            "chapter_event_batches_created": 0,
            "legacy_runtime_imported_as_history": False,
            "outline_imported_as_occurred_event": False,
            "tracking_projection_imported_as_fact": False,
        },
        "evidence_summary": copy.deepcopy(plan["evidence_summary"]),
    }
    manifest["manifest_hash"] = canonical_json_hash(manifest)
    return manifest


def _build_apply_targets(
    *,
    root: Path,
    root_map: dict[str, Path],
    registry: dict[str, Any],
    layout: dict[str, Any],
    bootstrap: dict[str, Any],
) -> list[PersistenceV2Target]:
    memory_root = layout["memory_root"]
    genesis = bootstrap["genesis"]
    source_sync = bootstrap["source_sync"]
    checkpoint = bootstrap["checkpoint"]
    projections = bootstrap["projections"]
    targets = [
        _runtime_json_target(
            "mg",
            "memory_event_batch",
            memory_root / "events" / "batches" / f"{genesis['batch_id']}.json",
            genesis,
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "ms",
            "memory_event_batch",
            memory_root / "events" / "batches" / f"{source_sync['batch_id']}.json",
            source_sync,
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "mc",
            "memory_checkpoint",
            memory_root / "events" / "checkpoints" / f"{checkpoint['checkpoint_id']}.json",
            checkpoint,
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "mm",
            "memory_projection",
            memory_root / "canonical_memory.json",
            bootstrap["canonical"],
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "ps",
            "memory_projection",
            memory_root / "projections" / "snapshot.json",
            projections["snapshot"],
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "psr",
            "memory_projection_receipt",
            memory_root / "projections" / "snapshot.receipt.json",
            projections["snapshot_receipt"],
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "pt",
            "memory_projection",
            memory_root / "projections" / "tracking.json",
            projections["tracking"],
            root_map=root_map,
            registry=registry,
        ),
        _runtime_json_target(
            "ptr",
            "memory_projection_receipt",
            memory_root / "projections" / "tracking.receipt.json",
            projections["tracking_receipt"],
            root_map=root_map,
            registry=registry,
        ),
        PersistenceV2Target(
            target_id="ar",
            kind="authority_activation_receipt",
            path_ref=path_ref_for(
                authority_receipt_path(root, bootstrap["activation"]["receipt_sha256"]),
                root_id="story_project",
                root=root_map["story_project"],
                root_uuid=registry["roots"]["story_project"]["root_uuid"],
            ),
            content=_json_bytes(bootstrap["activation"]),
            metadata={"immutable": True, "migration_bootstrap": True},
            expected_before_exists=False,
        ),
        PersistenceV2Target(
            target_id="pi",
            kind="project_identity",
            path_ref=path_ref_for(
                project_identity_path(root),
                root_id="story_project",
                root=root_map["story_project"],
                root_uuid=registry["roots"]["story_project"]["root_uuid"],
            ),
            content=_json_bytes(bootstrap["identity_after"].to_dict()),
            metadata={"authority_transition": True, "migration_bootstrap": True},
            expected_before_exists=True,
            expected_before_sha256=bootstrap["activation"]["expected_identity_sha256"],
        ),
    ]
    return targets


def _build_publication_artifacts(
    *,
    root_map: dict[str, Path],
    registry: dict[str, Any],
    layout: dict[str, Any],
    plan: dict[str, Any],
    approval: dict[str, Any],
    bootstrap: dict[str, Any],
) -> list[PersistenceV2Target]:
    values = (
        ("ap", "migration_plan", layout["plan_artifact"], plan),
        ("aa", "migration_approval", layout["approval_artifact"], approval),
        (
            "ab",
            "migration_baseline_manifest",
            layout["baseline_artifact"],
            bootstrap["baseline_manifest"],
        ),
    )
    return [
        PersistenceV2Target(
            target_id=target_id,
            kind=kind,
            path_ref=_runtime_ref(path, root_map=root_map, registry=registry),
            content=_json_bytes(value),
            phase="publication",
            metadata={"immutable": True},
            expected_before_exists=False,
        )
        for target_id, kind, path, value in values
    ]


def _final_record(
    *, plan: dict[str, Any], approval: dict[str, Any], bootstrap: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": MIGRATION_EXECUTION_SCHEMA_VERSION,
        "record_type": "event_authority_migration",
        "state": "bootstrap_committed",
        "book_id": plan["book_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "approval_id": approval["approval_id"],
        "approval_hash": approval["approval_hash"],
        "source_digest": plan["source_digest"],
        "authority_epoch": 1,
        "head_event_hash": bootstrap["canonical"]["head_event_hash"],
        "baseline_manifest_hash": bootstrap["baseline_manifest"]["manifest_hash"],
        "history_policy": "source_sync_only",
    }


def _load_completed_migration(
    *,
    root: Path,
    root_map: dict[str, Path],
    layout: dict[str, Any],
    plan: dict[str, Any],
    approval: dict[str, Any],
) -> dict[str, Any] | None:
    receipt_path = layout["publication_receipt"]
    final_path = layout["completed_record"]
    if not receipt_path.exists() and not final_path.exists():
        return None
    if not receipt_path.is_file() or not final_path.is_file():
        raise MigrationExecutionError(
            "migration_completion_incomplete", "migration completion artifacts are incomplete"
        )
    verification = verify_publication_receipt(receipt_path, root_map=root_map)
    if not verification.get("valid") or not verification.get("committed"):
        raise MigrationExecutionError(
            "migration_receipt_invalid", f"migration receipt is invalid: {verification['errors']}"
        )
    try:
        record = json.loads(final_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise MigrationExecutionError(
            "migration_record_invalid", f"cannot read migration completion record: {exc}"
        ) from exc
    if not committed_from_publication_receipt(final_path, receipt_path, root_map=root_map):
        raise MigrationExecutionError(
            "migration_record_uncommitted", "completion record is not bound by its receipt"
        )
    expected = {
        "book_id": plan["book_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "approval_id": approval["approval_id"],
        "approval_hash": approval["approval_hash"],
        "source_digest": plan["source_digest"],
        "authority_epoch": 1,
        "history_policy": "source_sync_only",
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise MigrationExecutionError(
            "migration_completion_mismatch", "completion record differs from the supplied plan or approval"
        )
    identity = load_project_identity(root)
    if identity is None or (identity.authority or {}).get("mode") != AUTHORITY_MODE_EVENT:
        raise MigrationExecutionError(
            "migration_authority_missing", "receipt exists but event authority is not active"
        )
    if (identity.authority or {}).get("head_event_hash") != record.get("head_event_hash"):
        raise MigrationExecutionError(
            "migration_authority_head_mismatch", "ProjectIdentity head differs from migration receipt"
        )
    event_store = layout["memory_root"] / "events"
    batches = load_memory_event_batches(event_store)
    if [item["batch_kind"] for item in batches] != ["genesis", "source_sync"]:
        raise MigrationExecutionError(
            "migration_history_policy_violated",
            "migration baseline must contain exactly genesis and source_sync batches",
        )
    replay = replay_memory_events(event_store)
    canonical = load_canonical_memory(layout["memory_root"] / "canonical_memory.json")
    if replay["projection"] != canonical or canonical["head_event_hash"] != record["head_event_hash"]:
        raise MigrationExecutionError(
            "migration_baseline_drift", "canonical memory does not replay to the activated authority head"
        )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    return {
        "schema_version": MIGRATION_EXECUTION_SCHEMA_VERSION,
        "status": "completed",
        "book_id": plan["book_id"],
        "plan_id": plan["plan_id"],
        "approval_id": approval["approval_id"],
        "authority_epoch": 1,
        "head_event_hash": record["head_event_hash"],
        "record": record,
        "publication_receipt": receipt,
        "verification": verification,
    }


def _migration_layout(root: Path, approval: dict[str, Any]) -> dict[str, Any]:
    # Keep the internal names deliberately short: staged temp names add their
    # own suffix and must remain below the classic Windows MAX_PATH boundary.
    home = root / ".novelagent" / "migration-v2"
    approval_hash = approval["approval_hash"]
    artifact_id = approval["approval_id"]
    return {
        "home": home,
        "operation_lock": home / "lock",
        "transaction_root": home / "tx",
        "memory_root": root / ".novelagent" / "runtime" / "memory" / "v2",
        "publication_receipt": home / "publication_receipts" / f"{artifact_id}.json",
        "completed_record": home / "completed" / f"{artifact_id}.json",
        "plan_artifact": home / "artifacts" / "plans" / f"{artifact_id}.json",
        "approval_artifact": home / "artifacts" / "approvals" / f"{artifact_id}.json",
        "baseline_artifact": home / "artifacts" / "baselines" / f"{artifact_id}.json",
        "receipt_id": f"migration-receipt-{approval_hash[:24]}",
    }


def _runtime_json_target(
    target_id: str,
    kind: str,
    path: Path,
    value: Mapping[str, Any],
    *,
    root_map: dict[str, Path],
    registry: dict[str, Any],
) -> PersistenceV2Target:
    return PersistenceV2Target(
        target_id=target_id,
        kind=kind,
        path_ref=_runtime_ref(path, root_map=root_map, registry=registry),
        content=_json_bytes(value),
        metadata={"migration_bootstrap": True},
        expected_before_exists=False,
    )


def _runtime_ref(path: Path, *, root_map: dict[str, Path], registry: dict[str, Any]):
    return path_ref_for(
        path,
        root_id="runtime",
        root=root_map["runtime"],
        root_uuid=registry["roots"]["runtime"]["root_uuid"],
    )


def _assert_event_storage_empty(memory_root: Path) -> None:
    if memory_root.exists() and any(path.is_file() for path in memory_root.rglob("*")):
        raise MigrationExecutionError(
            "migration_event_store_not_empty",
            "event-authority bootstrap refuses to merge with pre-existing Memory 2.2 files",
        )


def _next_source_chapter(plan: dict[str, Any]) -> int:
    chapters = [
        int(item["chapter_index"])
        for item in plan["sources"]
        if item["role"] == "published_prose" and item["chapter_index"] is not None
    ]
    return max(chapters, default=0) + 1


def _record_id(prefix: str, value: Any) -> str:
    raw = str(value).strip()
    if _SAFE_COMPONENT.fullmatch(raw):
        suffix = raw
    else:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{suffix}"


def _inventory_item(item_id: Any, raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        value = copy.deepcopy(raw)
        quantity = value.pop("quantity", 1)
        name = str(value.pop("name", item_id))
        status = value.pop("status", "held")
        data = value
    else:
        quantity = raw
        name = str(item_id)
        status = "held"
        data = {}
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        or quantity < 0
    ):
        raise MigrationExecutionError(
            "migration_decisions_invalid",
            f"inventory.{item_id}.quantity must be a non-negative number",
        )
    if status not in {"held", "lost", "consumed", "destroyed"}:
        data["approved_status"] = status
        status = "held"
    return {"name": name, "quantity": quantity, "status": status, "data": data}


def _approved_time_factory(value: str) -> Callable[[], datetime]:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MigrationExecutionError(
            "migration_approval_time_invalid", "approved_at must be an ISO-8601 timestamp"
        ) from exc
    return lambda: timestamp


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


__all__ = [
    "MIGRATION_EXECUTION_SCHEMA_VERSION",
    "MigrationExecutionError",
    "execute_event_authority_migration",
]
