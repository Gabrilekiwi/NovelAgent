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
    validate_memory_checkpoint,
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
from core.story_project.mapper import SETTING_DIR_NAME
from core.story_project.migration_v2 import (
    MIGRATION_BASELINE_CONTRACT,
    MIGRATION_BASELINE_MAPPER_VERSION,
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
    baseline = _build_baseline_mapping(plan, approval)
    evidence_anchor = "migration-evidence:" + canonical_json_hash(
        {
            "plan_hash": plan["plan_hash"],
            "approval_hash": approval["approval_hash"],
            "baseline_audit_hash": baseline["audit"]["audit_hash"],
        }
    )
    evidence_document = {
        "evidence_anchor": evidence_anchor,
        "plan": {
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "source_digest": plan["source_digest"],
            "shadow_candidate_hash": plan["shadow_candidate_hash"],
            "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
            "sources": copy.deepcopy(plan["sources"]),
        },
        "approval": {
            "approval_id": approval["approval_id"],
            "approval_hash": approval["approval_hash"],
            "decision_digest": approval["decision_digest"],
            "decisions": copy.deepcopy(approval["decisions"]),
        },
        "baseline_audit": copy.deepcopy(baseline["audit"]),
    }
    evidence_text = json.dumps(
        evidence_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    evidence_start = evidence_text.index(evidence_anchor)
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
    operations = baseline["operations"]
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
            "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
            "semantic_baseline_hash": baseline["audit"]["semantic_baseline_hash"],
            "operations_hash": baseline["audit"]["operations_hash"],
        },
    )
    event_context = create_memory_event_context(
        chapter_body=evidence_text,
        evidence_spans=[
            {
                "start": evidence_start,
                "end": evidence_start + len(evidence_anchor),
                "quote": evidence_anchor,
            }
        ],
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
        baseline_audit=baseline["audit"],
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


def _build_baseline_mapping(
    plan: dict[str, Any], approval: dict[str, Any]
) -> dict[str, Any]:
    decisions = approval["decisions"]
    candidate = plan.get("shadow_candidate")
    state = candidate.get("state") if isinstance(candidate, dict) else None
    if not isinstance(state, dict):
        raise MigrationExecutionError(
            "migration_semantic_candidate_unavailable",
            "approved migration has no complete shadow semantic candidate",
        )

    selected_prose = _selected_published_prose(plan, decisions)
    chapters = [int(item["chapter_index"]) for item in selected_prose]
    last_source_chapter = max(chapters, default=0)
    static_import = _static_semantic_import(plan, state)
    semantic_basis = {
        "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "semantic_state_hash": candidate["state_hash"],
        "decision_digest": approval["decision_digest"],
        "published_prose": [
            {
                "chapter_index": item["chapter_index"],
                "relative_path": item["relative_path"],
                "sha256": item["sha256"],
            }
            for item in selected_prose
        ],
        "static_import": static_import["semantic_payload"],
    }
    semantic_baseline_hash = canonical_json_hash(semantic_basis)
    operations: list[dict[str, Any]] = []

    if static_import["world"]:
        operations.append(
            {
                "op": "update_world",
                "value": copy.deepcopy(static_import["world"]),
                "data": {
                    "baseline_mapper_version": MIGRATION_BASELINE_MAPPER_VERSION,
                    "evidence_class": "static_constraint",
                    "field_paths": copy.deepcopy(static_import["world_field_paths"]),
                    "evidence_hash": canonical_json_hash(static_import["world_evidence"]),
                },
            }
        )
    for location in static_import["locations"]:
        operations.append(
            {
                "op": "upsert_location",
                "id": _record_id("location", location["external_id"]),
                "value": {
                    "name": location["external_id"],
                    "status": "unknown",
                    "data": {
                        "external_id": location["external_id"],
                        "fields": copy.deepcopy(location["fields"]),
                        "facts": copy.deepcopy(location["facts"]),
                        "migration_evidence": copy.deepcopy(location["evidence"]),
                    },
                },
                "data": {
                    "baseline_mapper_version": MIGRATION_BASELINE_MAPPER_VERSION,
                    "evidence_class": "static_constraint",
                    "field_paths": copy.deepcopy(location["field_paths"]),
                },
            }
        )
    for constraint in static_import["constraints"]:
        operations.append(
            {
                "op": "upsert_constraint",
                "id": _record_id("constraint", constraint["external_id"]),
                "value": {
                    "content": constraint["content"],
                    "status": "active",
                    "data": {
                        "external_id": constraint["external_id"],
                        "migration_evidence": copy.deepcopy(constraint["evidence"]),
                    },
                },
                "data": {
                    "baseline_mapper_version": MIGRATION_BASELINE_MAPPER_VERSION,
                    "evidence_class": "static_constraint",
                    "field_path": constraint["field_path"],
                },
            }
        )

    character_operations, character_positions = _approved_character_operations(
        decisions, plan=plan, approval=approval
    )
    static_location_names = {
        item["external_id"] for item in static_import["locations"]
    }
    for location_name in sorted(set(character_positions.values())):
        if location_name in static_location_names:
            continue
        operations.append(
            {
                "op": "upsert_location",
                "id": _record_id("location", location_name),
                "value": {
                    "name": location_name,
                    "status": "unknown",
                    "data": {
                        "source": "approved_chapter_10_character_state",
                        "decision_digest": approval["decision_digest"],
                    },
                },
                "data": _decision_operation_data(
                    plan, approval, "chapter_10_character_state"
                ),
            }
        )
    current_value: dict[str, Any] = {
        "chapter_index": last_source_chapter + 1,
        "last_published_source_chapter_index": last_source_chapter,
        "migration_baseline": {
            "plan_hash": plan["plan_hash"],
            "approval_hash": approval["approval_hash"],
            "source_digest": plan["source_digest"],
            "shadow_candidate_hash": plan["shadow_candidate_hash"],
            "semantic_baseline_hash": semantic_baseline_hash,
            "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
            "published_source_chapters": chapters,
            "history_policy": "source_sync_only",
        },
    }
    if character_positions:
        current_value["spatial_state"] = {
            "character_positions": copy.deepcopy(character_positions)
        }
    operations.extend(
        [
            {
                "op": "update_current_state",
                "value": current_value,
                "data": _decision_operation_data(plan, approval, "chapter_10_character_state"),
            },
            {
                "op": "update_story_time",
                "value": {
                    "label": "approved_migration_baseline",
                    "elapsed_minutes": decisions["timeline_elapsed_minutes"],
                    "chapter_index": last_source_chapter,
                    "scene_index": 0,
                },
                "data": _decision_operation_data(plan, approval, "timeline_elapsed_minutes"),
            },
        ]
    )
    operations.extend(character_operations)

    open_thread_ids: set[str] = set()
    for index, raw in enumerate(decisions["open_foreshadowing"]):
        item = copy.deepcopy(raw) if isinstance(raw, dict) else {"description": str(raw)}
        external_id = (
            _execution_decision_identifier(
                f"open_foreshadowing[{index}].id", item.pop("id")
            )
            if "id" in item
            else f"item-{index + 1}"
        )
        description_candidates = [
            item.pop("description", None),
            item.pop("content", None),
            item.pop("title", None),
        ]
        description = next(
            (
                value.strip()
                for value in description_candidates
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        if not description:
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"open_foreshadowing[{index}] must provide non-empty text",
            )
        approved_status = item.pop("status", None)
        if approved_status in {"resolved", "abandoned", "cancelled"}:
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"open_foreshadowing[{index}] cannot use terminal status {approved_status}",
            )
        raw_data = item.pop("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"approved_data": raw_data}
        if item:
            raw_data["approved_fields"] = item
        record_id = _record_id("thread", external_id)
        if record_id in open_thread_ids:
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"open_foreshadowing contains a duplicate id: {external_id}",
            )
        open_thread_ids.add(record_id)
        record_data = {
            **raw_data,
            "external_id": external_id,
            "decision_digest": approval["decision_digest"],
            **({"approved_status": approved_status} if approved_status is not None else {}),
        }
        operations.append(
            {
                "op": "upsert_foreshadowing",
                "id": record_id,
                "value": {
                    "description": description or external_id,
                    "status": (
                        approved_status
                        if approved_status in {"seeded", "developing", "ripe"}
                        else "seeded"
                    ),
                    "data": copy.deepcopy(record_data),
                },
                "data": _decision_operation_data(
                    plan, approval, f"open_foreshadowing[{index}]"
                ),
            }
        )
        operations.append(
            {
                "op": "upsert_open_thread",
                "id": record_id,
                "value": {
                    "title": description or external_id,
                    "status": "open",
                    "data": {**copy.deepcopy(record_data), "foreshadowing_id": record_id},
                },
                "data": _decision_operation_data(
                    plan, approval, f"open_foreshadowing[{index}]"
                ),
            }
        )

    for owner_id, raw_items in sorted(decisions["inventory"].items()):
        _execution_decision_identifier(f"inventory.{owner_id}", owner_id)
        if not isinstance(raw_items, dict):
            raise MigrationExecutionError(
                "migration_decisions_invalid", f"inventory.{owner_id} must be an object"
            )
        items = {}
        for item_id, raw_item in sorted(raw_items.items()):
            _execution_decision_identifier(
                f"inventory.{owner_id}.{item_id}", item_id
            )
            items[_record_id("item", item_id)] = _inventory_item(item_id, raw_item)
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
                    "data": {
                        "external_id": str(owner_id),
                        "decision_digest": approval["decision_digest"],
                    },
                },
                "data": _decision_operation_data(plan, approval, f"inventory.{owner_id}"),
            }
        )
    for term, raw in sorted(decisions["lexicon"].items()):
        _execution_decision_identifier(f"lexicon.{term}", term)
        if isinstance(raw, dict):
            value = copy.deepcopy(raw)
        elif isinstance(raw, str) and raw.strip():
            value = {"definition": raw}
        else:
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"lexicon.{term} must provide a non-empty text definition",
            )
        definition_value = value.pop("definition", None)
        if not isinstance(definition_value, str) or not definition_value.strip():
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"lexicon.{term}.definition must be non-empty text",
            )
        definition = definition_value.strip()
        operations.append(
            {
                "op": "upsert_glossary_entry",
                "id": _record_id("term", term),
                "value": {
                    "term": str(term),
                    "definition": definition,
                    "status": "active",
                    "data": {
                        "external_id": str(term),
                        "decision_digest": approval["decision_digest"],
                        **value,
                    },
                },
                "data": _decision_operation_data(plan, approval, f"lexicon.{term}"),
            }
        )
    for subject_id, raw in sorted(decisions["corruption"].items()):
        _execution_decision_identifier(f"corruption.{subject_id}", subject_id)
        value = copy.deepcopy(raw) if isinstance(raw, dict) else {"level": raw}
        level = value.pop("level", None)
        if (
            isinstance(level, bool)
            or not isinstance(level, (int, float))
            or not 0 <= level <= 100
        ):
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"corruption.{subject_id}.level must be between 0 and 100",
            )
        approved_status = value.pop("status", None)
        raw_data = value.pop("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"approved_data": raw_data}
        if value:
            raw_data["approved_fields"] = value
        operations.append(
            {
                "op": "upsert_corruption",
                "id": _record_id("corruption", subject_id),
                "value": {
                    "subject_id": (
                        _record_id("character", subject_id)
                        if subject_id in decisions["chapter_10_character_state"]
                        else "world"
                    ),
                    "level": level,
                    "status": (
                        approved_status
                        if approved_status in {"stable", "rising", "falling", "cleansed"}
                        else "stable"
                    ),
                    "data": {
                        **raw_data,
                        "external_id": str(subject_id),
                        "decision_digest": approval["decision_digest"],
                        **(
                            {"approved_status": approved_status}
                            if approved_status is not None
                            else {}
                        ),
                    },
                },
                "data": _decision_operation_data(
                    plan, approval, f"corruption.{subject_id}"
                ),
            }
        )

    audit = {
        "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "semantic_state_hash": candidate["state_hash"],
        "semantic_baseline_hash": semantic_baseline_hash,
        "published_prose_import": [
            {
                "chapter_index": item["chapter_index"],
                "relative_path": item["relative_path"],
                "sha256": item["sha256"],
            }
            for item in selected_prose
        ],
        "static_constraint_import": {
            "world_field_paths": copy.deepcopy(static_import["world_field_paths"]),
            "location_count": len(static_import["locations"]),
            "constraint_count": len(static_import["constraints"]),
            "source_paths": copy.deepcopy(static_import["source_paths"]),
        },
        "approved_decision_topics": [
            "timeline_elapsed_minutes",
            "chapter_10_character_state",
            "open_foreshadowing",
            "inventory",
            "lexicon",
            "corruption",
        ],
        "excluded_unknown": {
            "tracking_projection_imported_as_fact": False,
            "story_state_count": len(state.get("story_state", {})),
            "spatial_state_count": len(state.get("spatial_state", {})),
            "character_count": len(state.get("characters", {})),
            "timeline_count": len(state.get("timeline", [])),
            "foreshadowing_count": len(state.get("foreshadowing", [])),
            "unqualified_static_field_paths": copy.deepcopy(
                static_import["excluded_field_paths"]
            ),
            "warning_count": len(candidate.get("warnings", [])),
            "warnings_hash": canonical_json_hash(candidate.get("warnings", [])),
            "unsupported_count": len(candidate.get("unsupported", [])),
            "unsupported_hash": canonical_json_hash(candidate.get("unsupported", [])),
        },
    }
    audit["operations_hash"] = canonical_json_hash(operations)
    audit["audit_hash"] = canonical_json_hash(audit)
    return {"operations": operations, "audit": audit}


def _selected_published_prose(
    plan: dict[str, Any], decisions: dict[str, Any]
) -> list[dict[str, Any]]:
    by_chapter: dict[int, list[dict[str, Any]]] = {}
    for source in plan["sources"]:
        if source["role"] != "published_prose" or source["chapter_index"] is None:
            continue
        by_chapter.setdefault(int(source["chapter_index"]), []).append(source)
    resolutions = decisions.get("conflict_resolutions")
    if not isinstance(resolutions, dict):
        resolutions = {}
    selected: list[dict[str, Any]] = []
    for chapter, sources in sorted(by_chapter.items()):
        ordered = sorted(sources, key=lambda item: item["relative_path"])
        if len(ordered) == 1:
            selected.append(copy.deepcopy(ordered[0]))
            continue
        key = f"duplicate_chapter_source:published_prose:{chapter}"
        chosen_path = resolutions.get(key)
        chosen = next(
            (item for item in ordered if item["relative_path"] == chosen_path), None
        )
        if chosen is None:
            raise MigrationExecutionError(
                "migration_conflict_resolution_invalid",
                f"published prose chapter {chapter} has no approved source",
            )
        selected.append(copy.deepcopy(chosen))
    return selected


def _static_semantic_import(
    plan: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    sources = {item["relative_path"]: item for item in plan["sources"]}
    provenance = [
        item for item in state.get("provenance", []) if isinstance(item, dict)
    ]
    world_state = state.get("world_state")
    if not isinstance(world_state, dict):
        world_state = {}
    filtered_settings: dict[str, Any] = {}
    filtered_locations: dict[str, Any] = {}
    locations: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    world_field_paths: list[str] = []
    world_evidence: list[dict[str, Any]] = []
    excluded_field_paths: list[str] = []

    settings = world_state.get("settings")
    if isinstance(settings, dict):
        for subject, raw_section in sorted(settings.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_section, dict):
                continue
            section: dict[str, Any] = {"fields": {}, "facts": []}
            section_field_paths: list[str] = []
            section_evidence: list[dict[str, Any]] = []
            raw_fields = raw_section.get("fields")
            if isinstance(raw_fields, dict):
                for key, value in sorted(raw_fields.items(), key=lambda item: str(item[0])):
                    field_path = f"world_state.settings.{subject}.fields.{key}"
                    evidence = _qualified_static_evidence(
                        field_path, provenance=provenance, sources=sources
                    )
                    if not evidence:
                        excluded_field_paths.append(field_path)
                        continue
                    section["fields"][str(key)] = copy.deepcopy(value)
                    section_field_paths.append(field_path)
                    section_evidence.extend(evidence)
                    world_field_paths.append(field_path)
                    world_evidence.extend(evidence)
            raw_facts = raw_section.get("facts")
            if isinstance(raw_facts, list):
                for index, value in enumerate(raw_facts):
                    field_path = f"world_state.settings.{subject}.facts[{index}]"
                    evidence = _qualified_static_evidence(
                        field_path, provenance=provenance, sources=sources
                    )
                    if not evidence:
                        excluded_field_paths.append(field_path)
                        continue
                    section["facts"].append(copy.deepcopy(value))
                    section_field_paths.append(field_path)
                    section_evidence.extend(evidence)
                    world_field_paths.append(field_path)
                    world_evidence.extend(evidence)
            if section["fields"] or section["facts"]:
                filtered_settings[str(subject)] = section
                normalized_section_evidence = _dedupe_evidence(section_evidence)
                if normalized_section_evidence and all(
                    item["source_path"].startswith(f"{SETTING_DIR_NAME}/地点/")
                    for item in normalized_section_evidence
                ):
                    locations.append(
                        {
                            "external_id": str(subject),
                            "fields": copy.deepcopy(section["fields"]),
                            "facts": copy.deepcopy(section["facts"]),
                            "field_paths": section_field_paths,
                            "evidence": normalized_section_evidence,
                        }
                    )

    raw_locations = world_state.get("locations")
    if isinstance(raw_locations, dict):
        for subject, raw_fields in sorted(raw_locations.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_fields, dict):
                continue
            fields: dict[str, Any] = {}
            field_paths: list[str] = []
            evidence_items: list[dict[str, Any]] = []
            for key, value in sorted(raw_fields.items(), key=lambda item: str(item[0])):
                field_path = f"world_state.locations.{subject}.{key}"
                evidence = _qualified_static_evidence(
                    field_path, provenance=provenance, sources=sources
                )
                if not evidence:
                    excluded_field_paths.append(field_path)
                    continue
                fields[str(key)] = copy.deepcopy(value)
                field_paths.append(field_path)
                evidence_items.extend(evidence)
                world_field_paths.append(field_path)
                world_evidence.extend(evidence)
            if fields:
                filtered_locations[str(subject)] = copy.deepcopy(fields)
                existing = next(
                    (item for item in locations if item["external_id"] == str(subject)),
                    None,
                )
                if existing is None:
                    locations.append(
                        {
                            "external_id": str(subject),
                            "fields": fields,
                            "facts": [],
                            "field_paths": field_paths,
                            "evidence": _dedupe_evidence(evidence_items),
                        }
                    )
                else:
                    existing["fields"].update(fields)
                    existing["field_paths"] = sorted(
                        set(existing["field_paths"]) | set(field_paths)
                    )
                    existing["evidence"] = _dedupe_evidence(
                        existing["evidence"] + evidence_items
                    )

    for index, raw in enumerate(state.get("constraints", [])):
        if not isinstance(raw, dict):
            continue
        external_id = str(raw.get("id") or f"item-{index + 1}")
        field_path = f"constraints.{external_id}"
        evidence = _qualified_static_evidence(
            field_path, provenance=provenance, sources=sources
        )
        if not evidence:
            excluded_field_paths.append(field_path)
            continue
        content = str(raw.get("content") or raw.get("text") or "").strip()
        if not content:
            raise MigrationExecutionError(
                "migration_semantic_evidence_invalid",
                f"qualified static constraint has no content: {field_path}",
            )
        constraints.append(
            {
                "external_id": external_id,
                "content": content,
                "field_path": field_path,
                "evidence": _dedupe_evidence(evidence),
            }
        )

    world: dict[str, Any] = {}
    if filtered_settings:
        world["settings"] = filtered_settings
    if filtered_locations:
        world["locations"] = filtered_locations
    normalized_world_evidence = _dedupe_evidence(world_evidence)
    semantic_payload = {
        "world": copy.deepcopy(world),
        "locations": [
            {
                "external_id": item["external_id"],
                "fields": copy.deepcopy(item["fields"]),
                "facts": copy.deepcopy(item["facts"]),
            }
            for item in locations
        ],
        "constraints": [
            {"external_id": item["external_id"], "content": item["content"]}
            for item in constraints
        ],
    }
    return {
        "world": world,
        "locations": locations,
        "constraints": constraints,
        "world_field_paths": sorted(world_field_paths),
        "world_evidence": normalized_world_evidence,
        "excluded_field_paths": sorted(set(excluded_field_paths)),
        "source_paths": sorted(
            {item["source_path"] for item in normalized_world_evidence}
            | {
                item["source_path"]
                for constraint in constraints
                for item in constraint["evidence"]
            }
        ),
        "semantic_payload": semantic_payload,
    }


def _qualified_static_evidence(
    field_path: str,
    *,
    provenance: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    matches = [item for item in provenance if item.get("field_path") == field_path]
    if not matches:
        return []
    qualified: list[dict[str, Any]] = []
    for item in matches:
        source_path = item.get("source_path")
        source = sources.get(source_path) if isinstance(source_path, str) else None
        if source is None:
            raise MigrationExecutionError(
                "migration_semantic_evidence_invalid",
                f"semantic provenance is not bound to a frozen source: {field_path}",
            )
        if source.get("role") != "explicit_setting" or source.get("evidence_class") != "static_constraint":
            raise MigrationExecutionError(
                "migration_semantic_evidence_invalid",
                f"semantic provenance has the wrong evidence class: {field_path}",
            )
        if item.get("source_kind") != "setting" or item.get("authority_class") != "authoritative":
            return []
        qualified.append(
            {
                "field_path": field_path,
                "source_path": source_path,
                "semantic_source_sha256": str(item.get("source_sha256") or ""),
                "frozen_source_sha256": source["sha256"],
                "start_char": int(item.get("start_char", 0)),
                "end_char": int(item.get("end_char", 0)),
                "parser_version": str(item.get("parser_version") or ""),
            }
        )
    return _dedupe_evidence(qualified)


def _dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item
        for item in items
    }
    return [copy.deepcopy(unique[key]) for key in sorted(unique)]


def _approved_character_operations(
    decisions: dict[str, Any], *, plan: dict[str, Any], approval: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    operations: list[dict[str, Any]] = []
    positions: dict[str, str] = {}
    record_ids: set[str] = set()
    for external_id, raw in sorted(decisions["chapter_10_character_state"].items()):
        _execution_decision_identifier(
            f"chapter_10_character_state.{external_id}", external_id
        )
        if not isinstance(raw, dict):
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"chapter_10_character_state.{external_id} must be an object",
            )
        value = copy.deepcopy(raw)
        raw_name = value.pop("name", external_id)
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"chapter_10_character_state.{external_id}.name must be non-empty text",
            )
        name = raw_name.strip()
        approved_status = value.pop("status", None)
        raw_data = value.pop("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"approved_data": raw_data}
        raw_state = value.pop("state", {})
        if not isinstance(raw_state, dict):
            raw_data["approved_state"] = raw_state
            raw_state = {}
        location = value.pop("location", value.pop("current_location", None))
        if location is None:
            location = raw_state.get("current_location")
        if location is not None:
            if not isinstance(location, str) or not location.strip():
                raise MigrationExecutionError(
                    "migration_decisions_invalid",
                    f"chapter_10_character_state.{external_id}.location must be text",
                )
            location = location.strip()
            raw_state["current_location"] = location
            existing = positions.get(name)
            if existing is not None and existing != location:
                raise MigrationExecutionError(
                    "migration_decisions_invalid",
                    f"chapter_10_character_state contains conflicting locations for {name}",
                )
            positions[name] = location
        if value:
            raw_data["approved_fields"] = value
        record_value: dict[str, Any] = {
            "name": name,
            "status": (
                approved_status
                if approved_status in {"active", "missing", "dead", "unknown"}
                else "unknown"
            ),
            "data": {
                **raw_data,
                "external_id": str(external_id),
                "source_chapter": 10,
                "decision_digest": approval["decision_digest"],
                **(
                    {"approved_status": approved_status}
                    if approved_status is not None
                    else {}
                ),
            },
        }
        if raw_state:
            record_value["state"] = raw_state
        record_id = _record_id("character", external_id)
        if record_id in record_ids:
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"chapter_10_character_state contains a duplicate id: {external_id}",
            )
        record_ids.add(record_id)
        operations.append(
            {
                "op": "upsert_character",
                "id": record_id,
                "value": record_value,
                "data": _decision_operation_data(
                    plan, approval, f"chapter_10_character_state.{external_id}"
                ),
            }
        )
    return operations, positions


def _decision_operation_data(
    plan: dict[str, Any], approval: dict[str, Any], decision_path: str
) -> dict[str, Any]:
    return {
        "baseline_mapper_version": MIGRATION_BASELINE_MAPPER_VERSION,
        "evidence_class": "user_approved_decision",
        "plan_hash": plan["plan_hash"],
        "decision_digest": approval["decision_digest"],
        "decision_path": decision_path,
    }


def _baseline_manifest(
    *,
    plan: dict[str, Any],
    approval: dict[str, Any],
    genesis: dict[str, Any],
    source_sync: dict[str, Any],
    canonical: dict[str, Any],
    checkpoint: dict[str, Any],
    baseline_audit: dict[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema_version": MIGRATION_EXECUTION_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "approval_id": approval["approval_id"],
        "approval_hash": approval["approval_hash"],
        "book_id": plan["book_id"],
        "source_digest": plan["source_digest"],
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
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
            "published_prose_bound_as_occurred_event_evidence": bool(
                baseline_audit["published_prose_import"]
            ),
            "approved_shadow_static_constraints_imported": bool(
                baseline_audit["static_constraint_import"]["world_field_paths"]
                or baseline_audit["static_constraint_import"]["constraint_count"]
            ),
        },
        "evidence_summary": copy.deepcopy(plan["evidence_summary"]),
        "baseline_audit": copy.deepcopy(baseline_audit),
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
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "baseline_contract": copy.deepcopy(plan["baseline_contract"]),
        "semantic_baseline_hash": bootstrap["baseline_manifest"]["baseline_audit"][
            "semantic_baseline_hash"
        ],
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
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise MigrationExecutionError(
            "migration_receipt_invalid", f"cannot read migration receipt: {exc}"
        ) from exc
    expected_semantic_baseline_hash = _build_baseline_mapping(plan, approval)["audit"][
        "semantic_baseline_hash"
    ]
    expected = {
        "book_id": plan["book_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "approval_id": approval["approval_id"],
        "approval_hash": approval["approval_hash"],
        "source_digest": plan["source_digest"],
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "baseline_contract": plan["baseline_contract"],
        "semantic_baseline_hash": expected_semantic_baseline_hash,
        "authority_epoch": 1,
        "history_policy": "source_sync_only",
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise MigrationExecutionError(
            "migration_completion_mismatch", "completion record differs from the supplied plan or approval"
        )
    try:
        baseline_manifest = json.loads(
            layout["baseline_artifact"].read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError) as exc:
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            f"cannot read migration baseline manifest: {exc}",
        ) from exc
    if (
        baseline_manifest.get("manifest_hash") != record.get("baseline_manifest_hash")
        or canonical_json_hash(
            baseline_manifest, exclude_fields=("manifest_hash",)
        )
        != baseline_manifest.get("manifest_hash")
        or baseline_manifest.get("baseline_contract") != plan["baseline_contract"]
        or baseline_manifest.get("baseline_audit", {}).get("semantic_baseline_hash")
        != expected_semantic_baseline_hash
    ):
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            "migration baseline manifest is not bound to the approved semantic baseline",
        )
    identity = load_project_identity(root)
    if identity is None or (identity.authority or {}).get("mode") != AUTHORITY_MODE_EVENT:
        raise MigrationExecutionError(
            "migration_authority_missing", "receipt exists but event authority is not active"
        )
    if (identity.authority or {}).get("authority_epoch") != record.get("authority_epoch"):
        raise MigrationExecutionError(
            "migration_authority_epoch_mismatch",
            "ProjectIdentity authority epoch differs from the migration baseline",
        )
    event_store = layout["memory_root"] / "events"
    batches = load_memory_event_batches(event_store)
    if len(batches) < 2 or [item["batch_kind"] for item in batches[:2]] != [
        "genesis",
        "source_sync",
    ]:
        raise MigrationExecutionError(
            "migration_history_policy_violated",
            "migration baseline must begin with genesis and source_sync batches",
        )
    source_sync = batches[1]
    baseline_audit = baseline_manifest.get("baseline_audit")
    if not isinstance(baseline_audit, dict):
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid", "baseline audit is missing"
        )
    operations_hash = canonical_json_hash(source_sync["patch"]["operations"])
    if (
        operations_hash != source_sync["patch"].get("metadata", {}).get("operations_hash")
        or operations_hash != baseline_audit.get("operations_hash")
        or canonical_json_hash(baseline_audit, exclude_fields=("audit_hash",))
        != baseline_audit.get("audit_hash")
    ):
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            "source_sync operations are not bound to the baseline audit",
        )
    checkpoint = _load_baseline_checkpoint(
        event_store,
        expected_hash=baseline_manifest.get("checkpoint_hash"),
    )
    _verify_baseline_receipt_bytes(
        receipt,
        paths={
            "mg": event_store / "batches" / f"{batches[0]['batch_id']}.json",
            "ms": event_store / "batches" / f"{source_sync['batch_id']}.json",
            "mc": event_store
            / "checkpoints"
            / f"{checkpoint['checkpoint_id']}.json",
        },
    )
    manifest_identity = {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "approval_id": approval["approval_id"],
        "approval_hash": approval["approval_hash"],
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "baseline_contract": plan["baseline_contract"],
        "genesis_batch_hash": batches[0]["batch_hash"],
        "source_sync_batch_hash": source_sync["batch_hash"],
        "checkpoint_hash": checkpoint["checkpoint_hash"],
    }
    if any(
        baseline_manifest.get(key) != value for key, value in manifest_identity.items()
    ):
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            "migration baseline manifest differs from its approved event-store artifacts",
        )
    baseline_head = record.get("head_event_hash")
    if (
        baseline_manifest.get("head_event_hash") != baseline_head
        or source_sync["events"][-1].get("event_hash") != baseline_head
        or checkpoint.get("last_batch_hash") != source_sync["batch_hash"]
        or checkpoint.get("projection", {}).get("head_event_hash") != baseline_head
    ):
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            "migration baseline head is not bound to its source_sync batch and checkpoint",
        )
    checkpoint_replay = replay_memory_events(event_store)
    replay = replay_memory_events(event_store, use_checkpoint=False)
    canonical = load_canonical_memory(layout["memory_root"] / "canonical_memory.json")
    if (
        checkpoint_replay["projection"] != replay["projection"]
        or replay["projection"] != canonical
    ):
        raise MigrationExecutionError(
            "migration_baseline_drift",
            "canonical memory does not match the current authoritative event replay",
        )
    current_head = canonical.get("head_event_hash")
    if (
        canonical.get("authority_epoch") != record.get("authority_epoch")
        or (identity.authority or {}).get("head_event_hash") != current_head
    ):
        raise MigrationExecutionError(
            "migration_authority_head_mismatch",
            "ProjectIdentity head differs from the current authoritative event replay",
        )
    return {
        "schema_version": MIGRATION_EXECUTION_SCHEMA_VERSION,
        "status": "completed",
        "book_id": plan["book_id"],
        "plan_id": plan["plan_id"],
        "approval_id": approval["approval_id"],
        "authority_epoch": 1,
        "head_event_hash": current_head,
        "baseline_head_event_hash": baseline_head,
        "record": record,
        "publication_receipt": receipt,
        "verification": verification,
    }


def _load_baseline_checkpoint(
    event_store: Path, *, expected_hash: Any
) -> dict[str, Any]:
    if not isinstance(expected_hash, str):
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            "migration baseline checkpoint hash is missing",
        )
    matches: list[dict[str, Any]] = []
    for path in sorted((event_store / "checkpoints").glob("checkpoint_*.json")):
        try:
            checkpoint = validate_memory_checkpoint(
                json.loads(path.read_text(encoding="utf-8-sig"))
            )
        except (OSError, ValueError) as exc:
            raise MigrationExecutionError(
                "migration_baseline_manifest_invalid",
                f"cannot validate migration checkpoint {path.name}: {exc}",
            ) from exc
        if checkpoint.get("checkpoint_hash") == expected_hash:
            matches.append(checkpoint)
    if len(matches) != 1:
        raise MigrationExecutionError(
            "migration_baseline_manifest_invalid",
            "migration baseline checkpoint is missing or ambiguous",
        )
    return matches[0]


def _verify_baseline_receipt_bytes(
    receipt: dict[str, Any], *, paths: dict[str, Path]
) -> None:
    raw_targets = receipt.get("apply_targets")
    targets = {
        item.get("target_id"): item
        for item in (raw_targets if isinstance(raw_targets, list) else [])
        if isinstance(item, dict) and item.get("target_id") in paths
    }
    if set(targets) != set(paths):
        raise MigrationExecutionError(
            "migration_baseline_artifact_drift",
            "migration receipt does not bind every immutable baseline artifact",
        )
    for target_id, path in paths.items():
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise MigrationExecutionError(
                "migration_baseline_artifact_drift",
                f"cannot read immutable baseline artifact {target_id}: {exc}",
            ) from exc
        binding = targets[target_id]
        if (
            len(content) != binding.get("size")
            or hashlib.sha256(content).hexdigest() != binding.get("sha256")
        ):
            raise MigrationExecutionError(
                "migration_baseline_artifact_drift",
                f"immutable baseline artifact bytes changed: {target_id}",
            )


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
    original = str(value)
    raw = original.strip()
    if original == raw and _SAFE_COMPONENT.fullmatch(raw):
        suffix = raw
    else:
        suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{suffix}"


def _execution_decision_identifier(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MigrationExecutionError(
            "migration_decisions_invalid",
            f"{label} must be non-empty text without surrounding whitespace",
        )
    return value


def _inventory_item(item_id: Any, raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        value = copy.deepcopy(raw)
        quantity = value.pop("quantity", 1)
        raw_name = value.pop("name", item_id)
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise MigrationExecutionError(
                "migration_decisions_invalid",
                f"inventory.{item_id}.name must be non-empty text",
            )
        name = raw_name.strip()
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
