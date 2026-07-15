from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.engine.persistence import (
    PersistenceLockError,
    _atomic_create_from_bytes,
    _atomic_replace_from_bytes,
    _fsync_directory,
    _windows_move_file,
    persistence_run_lock,
)
from core.memory_v2.canonical import CANONICAL_JSON_ALGORITHM, canonical_json_hash
from core.path_refs import PathRef, path_ref_for, validate_path_ref
from core.engine.root_registry import (
    RootRegistryService,
    load_root_registry,
    root_registry_manifest_binding,
    validate_registry_manifest_binding,
)
from core.engine.safe_paths import (
    RootBinding,
    SafePathResolver,
    assert_safe_local_tree,
)
from core.engine.recovery_protocol import reconcile_marker_transaction
from core.schema import SchemaValidationError, validate_schema


PERSISTENCE_V2_SCHEMA_VERSION = "2.1"
PERSISTENCE_V2_COMPATIBLE_SCHEMA_VERSIONS = frozenset({"2.0", "2.1"})
PERSISTENCE_V2_STATES = frozenset(
    {
        "prepared",
        "applying",
        "commit_marked",
        "publishing",
        "completed",
        "rolling_back",
        "rolled_back",
        "recovery_required",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FaultInjector = Callable[[str, int | None, Path | None], None]


class PersistenceV2Error(RuntimeError):
    pass


class PersistenceV2IntegrityError(PersistenceV2Error):
    pass


class PersistenceV2PreparationError(PersistenceV2Error):
    pass


@dataclass(frozen=True)
class PersistenceV2Target:
    target_id: str
    kind: str
    path_ref: PathRef | Mapping[str, Any]
    content: str | bytes
    phase: str = "apply"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    encoding: str = "utf-8"
    expected_before_exists: bool | None = None
    expected_before_sha256: str | None = None

    def content_bytes(self) -> bytes:
        if isinstance(self.content, bytes):
            return self.content
        if isinstance(self.content, str):
            return self.content.encode(self.encoding)
        raise TypeError(f"persistence target content must be str or bytes, got {type(self.content).__name__}")


def bind_final_run_record_receipt(
    final_run_record: Mapping[str, Any],
    *,
    receipt_id: str,
    receipt_path_ref: PathRef | Mapping[str, Any],
) -> dict[str, Any]:
    _validate_id("receipt_id", receipt_id)
    bound = copy.deepcopy(dict(final_run_record))
    pointer_owner = _final_run_receipt_pointer_owner(bound)
    pointer_owner["publication_receipt"] = {
        "id": receipt_id,
        "path_ref": validate_path_ref(receipt_path_ref).to_dict(),
    }
    return _validate_final_run_receipt_pointer(bound, receipt_id, receipt_path_ref)


class PersistenceV2Transaction:
    """Receipt-backed local transaction with a deliberately acyclic hash DAG.

    Apply targets become durable before the marker. Publication targets and the
    immutable Final RunRecord are staged before the marker but published after
    it. Only a validated Publication Receipt proves ``committed``.
    """

    def __init__(
        self,
        *,
        transaction_root: str | Path,
        run_id: str,
        book_id: str,
        root_map: Mapping[str, str | Path],
        fault_injector: _FaultInjector | None = None,
        story_project_read_set: Mapping[str, Any] | None = None,
        read_set_declared_writes: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self.transaction_root = assert_safe_local_tree(transaction_root)
        self.run_id = _validate_id("run_id", run_id)
        self.book_id = _validate_id("book_id", book_id)
        self.root_map = _validate_root_map(root_map)
        self.root_registry = RootRegistryService(self.transaction_root)
        self.journal_dir = self.transaction_root / "journals" / self.run_id
        self.staging_dir = self.transaction_root / "staging" / self.run_id
        self.manifest_path = self.journal_dir / "manifest.json"
        self.marker_path = self.journal_dir / "commit.marker"
        self.candidate_path = self.journal_dir / "candidate_result.json"
        self.registry_root = self.transaction_root / "registry"
        self.pending_entry_path = self.registry_root / "pending" / f"{self.run_id}.json"
        self._fault_injector = fault_injector
        self.story_project_read_set = (
            copy.deepcopy(dict(story_project_read_set))
            if story_project_read_set is not None
            else None
        )
        self.read_set_declared_writes = [copy.deepcopy(dict(item)) for item in read_set_declared_writes]
        _json_bytes(self.story_project_read_set)
        _json_bytes(self.read_set_declared_writes)

    def prepare(
        self,
        *,
        apply_targets: Iterable[PersistenceV2Target],
        artifacts: Iterable[PersistenceV2Target],
        final_run_record: Mapping[str, Any],
        final_run_path_ref: PathRef | Mapping[str, Any],
        receipt_id: str,
        receipt_path_ref: PathRef | Mapping[str, Any],
        context_digest: str,
        generation_input_context_digest: str,
        story_project_source_revision_after: Mapping[str, Any],
        candidate_result: Mapping[str, Any],
        delivery_jobs: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        _require_sha256("context_digest", context_digest)
        _require_sha256("generation_input_context_digest", generation_input_context_digest)
        receipt_id = _validate_id("receipt_id", receipt_id)
        raw_receipt_ref = validate_path_ref(receipt_path_ref)
        raw_final_ref = validate_path_ref(final_run_path_ref)
        apply_items = list(apply_targets)
        artifact_items = list(artifacts)
        delivery_summaries = _validate_delivery_jobs(delivery_jobs)
        candidate_bytes = _json_bytes(dict(candidate_result))
        candidate_digest = _sha256(candidate_bytes)

        self.transaction_root.mkdir(parents=True, exist_ok=True)
        with persistence_run_lock(self.transaction_root):
            if self.journal_dir.exists() or self.pending_entry_path.exists() or self.staging_dir.exists():
                raise PersistenceV2PreparationError(
                    f"persistence transaction already exists or requires reconcile: {self.run_id}"
                )
            self._verify_story_project_read_set(phase="prepare")
            registry = self.root_registry.ensure(self.root_map)
            resolver = self.root_registry.resolver(registry)
            receipt_ref = resolver.bind(raw_receipt_ref)
            final_ref = resolver.bind(raw_final_ref)
            receipt_resolution = resolver.resolve(receipt_ref)
            receipt_path = receipt_resolution.path
            final_path = resolver.resolve(final_ref).path
            if receipt_path == final_path:
                raise PersistenceV2PreparationError(
                    "Final RunRecord and Publication Receipt paths must differ"
                )

            final_record = _validate_final_run_receipt_pointer(
                final_run_record, receipt_id, raw_receipt_ref
            )
            _final_run_receipt_pointer_owner(final_record)["publication_receipt"][
                "path_ref"
            ] = receipt_ref.to_dict()
            final_target = PersistenceV2Target(
                target_id="final-run-record",
                kind="final_run_record",
                path_ref=final_ref,
                content=_json_bytes(final_record),
                phase="publication",
                metadata={"immutable": True},
            )
            prepared_targets = self._prepare_target_records(
                [*apply_items, *artifact_items, final_target],
                receipt_ref=receipt_ref,
                resolver=resolver,
            )
            self._verify_read_set_target_binding(
                registry=registry,
                targets=prepared_targets,
                read_set=self.story_project_read_set,
                declared_writes=self.read_set_declared_writes,
            )
            source_revision_after = _validate_story_project_source_revision_after(
                story_project_source_revision_after,
                book_id=self.book_id,
                registry=registry,
                declared_writes=self.read_set_declared_writes,
            )
            if not any(item["phase"] == "apply" for item in prepared_targets):
                raise PersistenceV2PreparationError("at least one apply target is required")
            artifact_records = [
                _target_hash_summary(item)
                for item in prepared_targets
                if item["phase"] == "publication" and item["kind"] != "final_run_record"
            ]
            final_record_target = next(
                item for item in prepared_targets if item["kind"] == "final_run_record"
            )
            apply_records = [
                _target_hash_summary(item)
                for item in prepared_targets
                if item["phase"] == "apply"
            ]
            artifact_bundle_digest = canonical_json_hash(
                sorted(artifact_records, key=lambda item: item["target_id"])
            )
            apply_target_bundle_digest = canonical_json_hash(
                sorted(apply_records, key=lambda item: item["target_id"])
            )
            staged_payloads = [
                (item.pop("_content"), item.pop("_before")) for item in prepared_targets
            ]
            runtime_uuid = registry["roots"]["runtime"]["root_uuid"]
            manifest_ref = path_ref_for(
                self.manifest_path,
                root_id="runtime",
                root=self.root_map["runtime"],
                root_uuid=runtime_uuid,
            )
            marker_ref = path_ref_for(
                self.marker_path,
                root_id="runtime",
                root=self.root_map["runtime"],
                root_uuid=runtime_uuid,
            )
            immutable = {
                "book_id": self.book_id,
                "run_id": self.run_id,
                "root_map": _root_map_manifest(registry),
                "root_registry": root_registry_manifest_binding(registry),
                "context_digest": context_digest,
                "generation_input_context_digest": generation_input_context_digest,
                "story_project_source_revision_after": source_revision_after,
                "story_project_read_set": copy.deepcopy(self.story_project_read_set),
                "read_set_declared_writes": copy.deepcopy(self.read_set_declared_writes),
                "candidate": {
                    "journal_relative_path": "candidate_result.json",
                    "digest": candidate_digest,
                    "size": len(candidate_bytes),
                },
                "targets": prepared_targets,
                "artifact_bundle_digest": artifact_bundle_digest,
                "apply_target_bundle_digest": apply_target_bundle_digest,
                "final_run": _target_hash_summary(final_record_target),
                "manifest_path_ref": manifest_ref.to_dict(),
                "marker_path_ref": marker_ref.to_dict(),
                "publication_receipt": {
                    "id": receipt_id,
                    "path_ref": receipt_ref.to_dict(),
                    "path_guard": receipt_resolution.guard,
                },
                "delivery_jobs": delivery_summaries,
                "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
            }
            now = _utc_now()
            manifest = {
                "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
                "immutable": immutable,
                "manifest_digest": canonical_json_hash(immutable),
                "state": "prepared",
                "progress": {item["target_id"]: "prepared" for item in prepared_targets},
                "errors": [],
                "created_at": now,
                "updated_at": now,
            }
            manifest = validate_persistence_manifest_v2(manifest)
            pending_entry = {
                "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
                "book_id": self.book_id,
                "run_id": self.run_id,
                "state": "pending",
                "journal_relative_path": f"journals/{self.run_id}",
                "manifest_digest": manifest["manifest_digest"],
                "registered_at": _utc_now(),
                "receipt": None,
                "error_count": 0,
            }
            try:
                self.staging_dir.mkdir(parents=True, exist_ok=False)
                (self.staging_dir / "staged").mkdir()
                (self.staging_dir / "backups").mkdir()
                _inject(self._fault_injector, "after_temporary_journal_created", None, self.staging_dir)
                for index, (item, payloads) in enumerate(
                    zip(prepared_targets, staged_payloads, strict=True)
                ):
                    content, before = payloads
                    _write_new_file(self.staging_dir / item["staged_relative_path"], content)
                    if item["phase"] == "apply" and item["before_exists"]:
                        _write_new_file(
                            self.staging_dir / str(item["backup_relative_path"]), before
                        )
                    _inject(
                        self._fault_injector,
                        "after_prepare_target_staged",
                        index,
                        self.staging_dir / item["staged_relative_path"],
                    )
                _write_new_file(self.staging_dir / "candidate_result.json", candidate_bytes)
                _write_new_file(
                    self.staging_dir / "manifest.json",
                    _json_bytes(validate_persistence_manifest_v2(manifest)),
                )
                _validate_complete_staged_journal(self.staging_dir, manifest)
                _fsync_journal_tree(self.staging_dir)
                _inject(
                    self._fault_injector,
                    "before_journal_publish",
                    None,
                    self.journal_dir,
                )
                _atomic_publish_journal(self.staging_dir, self.journal_dir, self.transaction_root)
                _inject(
                    self._fault_injector,
                    "after_journal_publish",
                    None,
                    self.journal_dir,
                )
                _inject(
                    self._fault_injector,
                    "before_pending_registry",
                    None,
                    self.pending_entry_path,
                )
                _assert_internal_path_safe(
                    self.transaction_root, self.pending_entry_path
                )
                _write_registry_entry_new(self.pending_entry_path, pending_entry)
                _fsync_directory(self.pending_entry_path.parent)
                _inject(
                    self._fault_injector,
                    "after_pending_registry",
                    None,
                    self.pending_entry_path,
                )
                return copy.deepcopy(manifest)
            except Exception as exc:
                raise PersistenceV2PreparationError(
                    f"failed to prepare persistence v2.1 transaction: {exc}"
                ) from exc

    def commit(self) -> dict[str, Any]:
        manifest = load_persistence_manifest_v2(self.manifest_path)
        resolver = self._resolver_for_manifest(manifest, require_same_revision=True)
        apply_paths = _resolved_target_paths(manifest, phase="apply", resolver=resolver)
        with persistence_run_lock(self.transaction_root, state_paths=apply_paths):
            manifest = load_persistence_manifest_v2(self.manifest_path)
            if manifest["state"] != "prepared":
                raise PersistenceV2Error(f"transaction is not prepared: {manifest['state']}")
            try:
                resolver = self._resolver_for_manifest(manifest, require_same_revision=True)
                self._verify_story_project_read_set(phase="pre_apply", manifest=manifest)
                _validate_pending_candidate(self.journal_dir, manifest)
                manifest["state"] = "applying"
                _write_manifest(self.manifest_path, manifest)
                for index, target in enumerate(_targets(manifest, phase="apply")):
                    path = _resolve_manifest_target(
                        manifest, target, resolver=resolver, enforce_guard=True
                    )
                    _inject(self._fault_injector, "before_apply_target", index, path)
                    path = _resolve_manifest_target(
                        manifest, target, resolver=resolver, enforce_guard=True
                    )
                    _assert_before_image(path, target)
                    content = _load_staged_content(self.journal_dir, target)
                    if _path_sha256(path) != target["after_sha256"]:
                        resolver.ensure_parent(
                            target["path_ref"], expected_guard=target.get("path_guard")
                        )
                        _resolve_manifest_target(
                            manifest,
                            target,
                            resolver=resolver,
                            enforce_guard=True,
                        )
                        _atomic_replace_from_bytes(path, content)
                    if _path_sha256(path) != target["after_sha256"]:
                        raise PersistenceV2IntegrityError(f"apply target after-hash mismatch: {path}")
                    manifest["progress"][target["target_id"]] = "applied"
                    _write_manifest(self.manifest_path, manifest)
                    _inject(self._fault_injector, "after_apply_target", index, path)

                _verify_apply_targets(manifest, resolver=resolver)
                marker = _create_commit_marker(manifest)
                _inject(self._fault_injector, "before_commit_marker", None, self.marker_path)
                self._verify_story_project_read_set(phase="pre_marker", manifest=manifest)
                _verify_apply_targets(manifest, resolver=resolver)
                _assert_internal_path_safe(self.transaction_root, self.marker_path)
                _write_new_file(self.marker_path, _json_bytes(marker))
                _inject(self._fault_injector, "after_commit_marker", None, self.marker_path)
                manifest["state"] = "commit_marked"
                _write_manifest(self.manifest_path, manifest)
                return self._complete_publication_locked()
            except Exception as exc:
                manifest = _safe_reload_manifest(self.manifest_path, manifest)
                _append_error(manifest, "commit_failed", exc)
                def roll_forward() -> dict[str, Any]:
                    manifest["state"] = "commit_marked"
                    _write_manifest_best_effort(self.manifest_path, manifest)
                    return _result(manifest, receipt_valid=False)

                return reconcile_marker_transaction(
                    marker_present=self.marker_path.exists(),
                    completion_present=False,
                    on_roll_back=lambda: _rollback_pre_marker(
                        transaction_root=self.transaction_root,
                        journal_dir=self.journal_dir,
                        manifest_path=self.manifest_path,
                        manifest=manifest,
                        pending_entry_path=self.pending_entry_path,
                        resolver=resolver,
                    ),
                    on_roll_forward=roll_forward,
                    on_completed=lambda: _result(manifest, receipt_valid=False),
                )

    def complete_publication(self) -> dict[str, Any]:
        manifest = load_persistence_manifest_v2(self.manifest_path)
        resolver = self._resolver_for_manifest(manifest, require_same_revision=True)
        apply_paths = _resolved_target_paths(manifest, phase="apply", resolver=resolver)
        with persistence_run_lock(self.transaction_root, state_paths=apply_paths):
            return self._complete_publication_locked()

    def _complete_publication_locked(self) -> dict[str, Any]:
        manifest = load_persistence_manifest_v2(self.manifest_path)
        if manifest["state"] == "completed":
            receipt = _load_planned_receipt(manifest)
            verification = verify_publication_receipt(receipt, root_map=_root_map_from_manifest(manifest))
            if not verification["valid"]:
                raise PersistenceV2IntegrityError("completed transaction receipt is invalid")
            _assert_receipt_matches_manifest(receipt, manifest)
            return _result(manifest, receipt_valid=True, receipt=receipt)
        if not self.marker_path.exists():
            raise PersistenceV2Error("publication cannot start before commit marker")
        try:
            resolver = self._resolver_for_manifest(manifest, require_same_revision=True)
            marker = load_commit_marker_v2(self.marker_path)
            _verify_marker_against_manifest(marker, manifest)
            _verify_apply_targets(manifest, resolver=resolver)
            manifest["state"] = "publishing"
            _write_manifest(self.manifest_path, manifest)
            for index, target in enumerate(_targets(manifest, phase="publication")):
                already_published = (
                    manifest.get("progress", {}).get(target["target_id"]) == "published"
                )
                path = _resolve_manifest_target(
                    manifest,
                    target,
                    resolver=resolver,
                    enforce_guard=True,
                    allow_guard_extension=already_published,
                )
                content = _load_staged_content(self.journal_dir, target)
                _inject(self._fault_injector, "before_publication_target", index, path)
                path = _resolve_manifest_target(
                    manifest,
                    target,
                    resolver=resolver,
                    enforce_guard=True,
                    allow_guard_extension=already_published,
                )
                if not already_published:
                    resolver.ensure_parent(
                        target["path_ref"], expected_guard=target.get("path_guard")
                    )
                _publish_immutable(
                    path,
                    content,
                    str(target["after_sha256"]),
                    resolver=resolver,
                    path_ref=target["path_ref"],
                    path_guard=target.get("path_guard"),
                )
                manifest["progress"][target["target_id"]] = "published"
                _write_manifest(self.manifest_path, manifest)
                _inject(self._fault_injector, "after_publication_target", index, path)

            receipt = _build_publication_receipt(manifest, marker)
            unguarded_receipt_path = resolver.resolve(receipt["receipt_path_ref"]).path
            receipt_resolved = resolver.resolve(
                receipt["receipt_path_ref"],
                expected_guard=manifest["immutable"]["publication_receipt"].get(
                    "path_guard"
                ),
                allow_guard_extension=unguarded_receipt_path.exists(),
            )
            receipt_path = receipt_resolved.path
            _inject(self._fault_injector, "before_publication_receipt", None, receipt_path)
            if receipt_path.exists():
                existing = _load_json(receipt_path)
                verification = verify_publication_receipt(existing, root_map=_root_map_from_manifest(manifest))
                if not verification["valid"]:
                    raise PersistenceV2IntegrityError("existing Publication Receipt is invalid or belongs to another run")
                _assert_receipt_matches_manifest(existing, manifest)
                receipt = existing
            else:
                resolver.ensure_parent(
                    receipt["receipt_path_ref"], expected_guard=receipt_resolved.guard
                )
                receipt_path = resolver.resolve(
                    receipt["receipt_path_ref"],
                    expected_guard=receipt_resolved.guard,
                    allow_guard_extension=True,
                ).path
                _write_new_file(receipt_path, _json_bytes(receipt))
            _inject(self._fault_injector, "after_publication_receipt", None, receipt_path)
            verification = verify_publication_receipt(receipt, root_map=_root_map_from_manifest(manifest))
            if not verification["valid"]:
                raise PersistenceV2IntegrityError("Publication Receipt verification failed")
            manifest["state"] = "completed"
            _write_manifest(self.manifest_path, manifest)
            _transition_registry(self.transaction_root, manifest, "completed", receipt=receipt)
            return _result(manifest, receipt_valid=True, receipt=receipt)
        except Exception as exc:
            manifest = _safe_reload_manifest(self.manifest_path, manifest)
            manifest["state"] = "commit_marked"
            _append_error(manifest, "publication_failed", exc)
            _write_manifest_best_effort(self.manifest_path, manifest)
            return _result(manifest, receipt_valid=False)

    def _prepare_target_records(
        self,
        targets: list[PersistenceV2Target],
        *,
        receipt_ref: PathRef,
        resolver: SafePathResolver,
    ) -> list[dict[str, Any]]:
        if not targets:
            raise PersistenceV2PreparationError("at least one target is required")
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_paths: set[Path] = set()
        receipt_path = resolver.resolve(receipt_ref).path
        for index, target in enumerate(targets):
            if not isinstance(target, PersistenceV2Target):
                raise PersistenceV2PreparationError(f"target {index} must be PersistenceV2Target")
            target_id = _validate_id("target_id", target.target_id)
            if target_id in seen_ids:
                raise PersistenceV2PreparationError(f"duplicate target id: {target_id}")
            seen_ids.add(target_id)
            if target.phase not in {"apply", "publication"}:
                raise PersistenceV2PreparationError(f"target {target_id} has invalid phase: {target.phase}")
            ref = resolver.bind(target.path_ref)
            resolved = resolver.resolve(ref)
            path = resolved.path
            if path == receipt_path:
                raise PersistenceV2PreparationError("Publication Receipt cannot be included in target bundle")
            if path in seen_paths:
                raise PersistenceV2PreparationError(f"duplicate target path: {path}")
            seen_paths.add(path)
            content = target.content_bytes()
            after_hash = _sha256(content)
            before_exists = path.exists()
            if before_exists and not path.is_file():
                raise PersistenceV2PreparationError(f"target is not a regular file: {path}")
            before = path.read_bytes() if before_exists else b""
            before_hash = _sha256(before) if before_exists else None
            if target.phase == "apply":
                if not path.parent.is_dir():
                    raise PersistenceV2PreparationError(
                        f"apply target parent directory does not exist: {path.parent}"
                    )
                _verify_expected_before(target, path, before_exists, before_hash)
            elif before_exists and before_hash != after_hash:
                raise PersistenceV2PreparationError(f"immutable publication target already exists with different bytes: {path}")
            metadata = copy.deepcopy(dict(target.metadata))
            _json_bytes(metadata)
            records.append(
                {
                    "index": index,
                    "target_id": target_id,
                    "kind": str(target.kind),
                    "phase": target.phase,
                    "path_ref": ref.to_dict(),
                    "path_guard": resolved.guard,
                    "before_exists": before_exists,
                    "before_sha256": before_hash,
                    "before_size": len(before) if before_exists else None,
                    "after_sha256": after_hash,
                    "after_size": len(content),
                    "staged_relative_path": f"staged/{index:03d}-{target_id}.bin",
                    "backup_relative_path": (
                        f"backups/{index:03d}-{target_id}.bin"
                        if target.phase == "apply" and before_exists
                        else None
                    ),
                    "metadata": metadata,
                    "_content": content,
                    "_before": before,
                }
            )
        return records

    def _resolver_for_manifest(
        self,
        manifest: Mapping[str, Any],
        *,
        require_same_revision: bool,
    ) -> SafePathResolver:
        immutable = manifest["immutable"]
        if manifest.get("schema_version") == "2.1":
            registry = self.root_registry.load()
            validate_registry_manifest_binding(
                immutable["root_registry"],
                registry,
                require_same_revision=require_same_revision,
            )
            return self.root_registry.resolver(registry)
        return _legacy_safe_resolver(_root_map_from_immutable(immutable))

    def _verify_story_project_read_set(
        self,
        *,
        phase: str,
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        immutable = manifest.get("immutable") if isinstance(manifest, Mapping) else None
        read_set = (
            immutable.get("story_project_read_set")
            if isinstance(immutable, Mapping)
            else self.story_project_read_set
        )
        if not isinstance(read_set, dict):
            return
        declared = (
            immutable.get("read_set_declared_writes", [])
            if isinstance(immutable, Mapping)
            else self.read_set_declared_writes
        )
        from core.story_project.read_set import verify_story_project_read_set

        verify_story_project_read_set(
            copy.deepcopy(read_set),
            declared_writes=copy.deepcopy(list(declared)),
            phase=phase,
        )
        if isinstance(immutable, Mapping) and manifest is not None:
            self._verify_read_set_target_binding(
                registry=self.root_registry.load(),
                targets=list(immutable.get("targets") or []),
                read_set=read_set,
                declared_writes=list(declared),
            )

    def _verify_read_set_target_binding(
        self,
        *,
        registry: Mapping[str, Any],
        targets: list[Mapping[str, Any]],
        read_set: Mapping[str, Any] | None,
        declared_writes: Iterable[Mapping[str, Any]],
    ) -> None:
        _validate_read_set_transaction_binding(
            book_id=self.book_id,
            registry=registry,
            targets=targets,
            read_set=read_set,
            declared_writes=declared_writes,
        )


def validate_persistence_manifest_v2(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise PersistenceV2IntegrityError("PersistenceManifestV2 must be an object")
    try:
        validated = validate_schema(manifest, "persistence_manifest_v2.schema.json")
    except SchemaValidationError as exc:
        raise PersistenceV2IntegrityError(str(exc)) from exc
    immutable = validated["immutable"]
    if not isinstance(immutable, dict):
        raise PersistenceV2IntegrityError("manifest immutable section must be an object")
    _require_sha256("manifest_digest", validated["manifest_digest"])
    if canonical_json_hash(immutable) != validated["manifest_digest"]:
        raise PersistenceV2IntegrityError("PersistenceManifestV2 immutable digest mismatch")
    if validated["state"] not in PERSISTENCE_V2_STATES:
        raise PersistenceV2IntegrityError(f"invalid PersistenceManifestV2 state: {validated['state']}")
    if validated["schema_version"] not in PERSISTENCE_V2_COMPATIBLE_SCHEMA_VERSIONS:
        raise PersistenceV2IntegrityError("PersistenceManifestV2 schema version is unsupported")
    _validate_manifest_immutable(immutable, schema_version=validated["schema_version"])
    return validated


def load_persistence_manifest_v2(path: str | Path) -> dict[str, Any]:
    return validate_persistence_manifest_v2(_load_json(Path(path)))


def validate_commit_marker_v2(marker: Any) -> dict[str, Any]:
    if not isinstance(marker, dict):
        raise PersistenceV2IntegrityError("CommitMarkerV2 must be an object")
    try:
        validated = validate_schema(marker, "commit_marker_v2.schema.json")
    except SchemaValidationError as exc:
        raise PersistenceV2IntegrityError(str(exc)) from exc
    for field in (
        "manifest_digest",
        "candidate_digest",
        "artifact_bundle_digest",
        "final_run_hash",
        "apply_target_bundle_digest",
        "marker_hash",
    ):
        _require_sha256(field, validated[field])
    validate_path_ref(validated["manifest"])
    if validated["canonical_json_algorithm"] != CANONICAL_JSON_ALGORITHM:
        raise PersistenceV2IntegrityError("CommitMarkerV2 canonical JSON algorithm is unsupported")
    if canonical_json_hash(validated, exclude_fields=("marker_hash",)) != validated["marker_hash"]:
        raise PersistenceV2IntegrityError("CommitMarkerV2 hash mismatch")
    return validated


def load_commit_marker_v2(path: str | Path) -> dict[str, Any]:
    return validate_commit_marker_v2(_load_json(Path(path)))


def validate_publication_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise PersistenceV2IntegrityError("PublicationReceipt must be an object")
    try:
        validated = validate_schema(receipt, "publication_receipt.schema.json")
    except SchemaValidationError as exc:
        raise PersistenceV2IntegrityError(str(exc)) from exc
    for field in ("context_digest", "generation_input_context_digest", "candidate_digest", "artifact_bundle_digest", "receipt_hash"):
        _require_sha256(field, validated[field])
    validate_path_ref(validated["receipt_path_ref"])
    if canonical_json_hash(validated, exclude_fields=("receipt_hash",)) != validated["receipt_hash"]:
        raise PersistenceV2IntegrityError("PublicationReceipt hash mismatch")
    for binding_name in ("manifest", "marker", "final_run"):
        binding = validated[binding_name]
        if not isinstance(binding, dict) or not isinstance(binding.get("path_ref"), dict):
            raise PersistenceV2IntegrityError(f"PublicationReceipt {binding_name} binding is invalid")
        validate_path_ref(binding["path_ref"])
        _require_sha256(f"{binding_name}.sha256", binding.get("sha256"))
    for artifact in validated["artifacts"]:
        validate_path_ref(artifact.get("path_ref"))
        _require_sha256("artifact.sha256", artifact.get("sha256"))
    if validated["canonical_json_algorithm"] != CANONICAL_JSON_ALGORITHM:
        raise PersistenceV2IntegrityError("PublicationReceipt canonical JSON algorithm is unsupported")
    return validated


def verify_publication_receipt(
    receipt: Mapping[str, Any] | str | Path,
    *,
    root_map: Mapping[str, str | Path],
) -> dict[str, Any]:
    roots = _validate_root_map(root_map)
    try:
        payload = _load_json(Path(receipt)) if isinstance(receipt, (str, Path)) else dict(receipt)
        validated = validate_publication_receipt(payload)
        manifest_binding = validated["manifest"]
        initial_ref = validate_path_ref(manifest_binding["path_ref"])
        initial_bindings = {
            root_id: RootBinding(
                root_id=root_id,
                root_uuid=(
                    initial_ref.root_uuid
                    if root_id == initial_ref.root_id and initial_ref.root_uuid is not None
                    else str(uuid.uuid5(uuid.NAMESPACE_URL, f"novelagent:supplied:{root_id}:{path}"))
                ),
                path=path,
            )
            for root_id, path in roots.items()
        }
        manifest_path = SafePathResolver(initial_bindings).resolve(initial_ref).path
        manifest = load_persistence_manifest_v2(manifest_path)
        resolver = _verification_resolver(
            manifest_path=manifest_path,
            manifest=manifest,
            supplied_roots=roots,
        )
        if manifest["manifest_digest"] != manifest_binding["sha256"]:
            raise PersistenceV2IntegrityError("receipt manifest digest mismatch")
        immutable = manifest["immutable"]
        receipt_identity = {
            "book_id": validated["book_id"],
            "run_id": validated["run_id"],
            "context_digest": validated["context_digest"],
            "generation_input_context_digest": validated["generation_input_context_digest"],
            "story_project_source_revision_after": validated["story_project_source_revision_after"],
            "receipt_id": validated["receipt_id"],
        }
        expected_identity = {
            "book_id": immutable["book_id"],
            "run_id": immutable["run_id"],
            "context_digest": immutable["context_digest"],
            "generation_input_context_digest": immutable["generation_input_context_digest"],
            "story_project_source_revision_after": immutable["story_project_source_revision_after"],
            "receipt_id": immutable["publication_receipt"]["id"],
        }
        if receipt_identity != expected_identity:
            raise PersistenceV2IntegrityError("receipt identity/context does not match manifest")
        if manifest_binding["path_ref"] != immutable["manifest_path_ref"]:
            raise PersistenceV2IntegrityError("receipt manifest PathRef mismatch")
        if validated["receipt_path_ref"] != immutable["publication_receipt"]["path_ref"]:
            raise PersistenceV2IntegrityError("receipt planned PathRef mismatch")

        marker_binding = validated["marker"]
        marker_path = resolver.resolve(marker_binding["path_ref"]).path
        marker = load_commit_marker_v2(marker_path)
        if marker["marker_hash"] != marker_binding["sha256"]:
            raise PersistenceV2IntegrityError("receipt marker hash mismatch")
        if marker_binding["path_ref"] != immutable["marker_path_ref"]:
            raise PersistenceV2IntegrityError("receipt marker PathRef mismatch")
        _verify_marker_against_manifest(marker, manifest)
        if marker["candidate_digest"] != validated["candidate_digest"]:
            raise PersistenceV2IntegrityError("receipt candidate digest mismatch")
        if marker["artifact_bundle_digest"] != validated["artifact_bundle_digest"]:
            raise PersistenceV2IntegrityError("receipt artifact bundle digest mismatch")

        final_binding = validated["final_run"]
        expected_final_binding = {
            "target_id": immutable["final_run"]["target_id"],
            "kind": immutable["final_run"]["kind"],
            "path_ref": immutable["final_run"]["path_ref"],
            "sha256": immutable["final_run"]["sha256"],
            "size": immutable["final_run"]["size"],
        }
        if final_binding != expected_final_binding:
            raise PersistenceV2IntegrityError("receipt Final RunRecord binding mismatch")
        final_path = resolver.resolve(final_binding["path_ref"]).path
        final_bytes = final_path.read_bytes()
        _verify_file_binding(final_path, final_bytes, final_binding)
        final_record = json.loads(final_bytes.decode("utf-8"))
        _validate_final_run_receipt_pointer(
            final_record,
            str(validated["receipt_id"]),
            manifest["immutable"]["publication_receipt"]["path_ref"],
        )
        if marker["final_run_hash"] != final_binding["sha256"]:
            raise PersistenceV2IntegrityError("marker Final RunRecord hash mismatch")

        artifact_summaries: list[dict[str, Any]] = []
        for artifact in validated["artifacts"]:
            artifact_path = resolver.resolve(artifact["path_ref"]).path
            content = artifact_path.read_bytes()
            _verify_file_binding(artifact_path, content, artifact)
            artifact_summaries.append(_artifact_digest_summary(artifact))
        actual_bundle = canonical_json_hash(sorted(artifact_summaries, key=lambda item: item["target_id"]))
        if actual_bundle != validated["artifact_bundle_digest"]:
            raise PersistenceV2IntegrityError("receipt artifact bundle content mismatch")
        expected_artifacts = [
            _target_receipt_binding(item)
            for item in _targets(manifest, phase="publication")
            if item["kind"] != "final_run_record"
        ]
        if validated["artifacts"] != expected_artifacts:
            raise PersistenceV2IntegrityError("receipt artifact bindings do not match manifest")
        expected_apply = [_target_receipt_binding(item) for item in _targets(manifest, phase="apply")]
        if validated["apply_targets"] != expected_apply:
            raise PersistenceV2IntegrityError("receipt apply target summaries do not match manifest")
        if validated["delivery_jobs"] != immutable["delivery_jobs"]:
            raise PersistenceV2IntegrityError("receipt DeliveryJob bindings do not match manifest")
        return {
            "schema_version": validated["schema_version"],
            "valid": True,
            "committed": True,
            "book_id": validated["book_id"],
            "run_id": validated["run_id"],
            "receipt_id": validated["receipt_id"],
            "receipt_hash": validated["receipt_hash"],
            "delivery_jobs": copy.deepcopy(validated["delivery_jobs"]),
            "errors": [],
        }
    except Exception as exc:
        return {
            "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
            "valid": False,
            "committed": False,
            "errors": [{"code": "publication_receipt_invalid", "error": f"{type(exc).__name__}: {exc}"}],
        }


def committed_from_publication_receipt(
    final_run_record: Mapping[str, Any] | str | Path,
    receipt: Mapping[str, Any] | str | Path,
    *,
    root_map: Mapping[str, str | Path],
) -> bool:
    verification = verify_publication_receipt(receipt, root_map=root_map)
    if not verification["valid"]:
        return False
    payload = _load_json(Path(receipt)) if isinstance(receipt, (str, Path)) else dict(receipt)
    final_binding = payload["final_run"]
    if isinstance(final_run_record, (str, Path)):
        content = Path(final_run_record).read_bytes()
    else:
        content = _json_bytes(dict(final_run_record))
    return _sha256(content) == final_binding["sha256"] and len(content) == final_binding["size"]


def _discover_persistence_v2_transactions(root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # A crash while staging is harmless to external state. A complete staged
    # journal is published and then reconciled normally; an incomplete one is
    # atomically quarantined as an abandoned preparation.
    staging_root = root / "staging"
    for staging in sorted(staging_root.iterdir()) if staging_root.exists() else []:
        if not staging.is_dir():
            continue
        try:
            manifest = _validate_complete_staged_journal(staging)
            run_id = str(manifest["immutable"]["run_id"])
            if staging.name != run_id:
                raise PersistenceV2IntegrityError("staging directory/run id mismatch")
            destination = root / "journals" / run_id
            _atomic_publish_journal(staging, destination, root)
        except Exception as exc:
            run_id = staging.name
            abandoned = root / "abandoned" / f"{run_id}-{_timestamp_id()}"
            try:
                _atomic_publish_journal(staging, abandoned, root)
            except Exception:
                abandoned = staging
            results.append(
                {
                    "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
                    "run_id": run_id,
                    "book_id": "unknown",
                    "state": "rolled_back",
                    "committed": False,
                    "errors": [
                        {
                            "code": "abandoned_prepare",
                            "error": f"{type(exc).__name__}: {exc}",
                            "quarantined_path": str(abandoned),
                        }
                    ],
                }
            )

    # Pending is intentionally registered last. Discover a fully published
    # journal if power failed in the small journal->registry interval.
    known_run_ids: set[str] = set()
    for state in ("pending", "completed", "rolled_back", "recovery_required"):
        directory = root / "registry" / state
        if directory.exists():
            known_run_ids.update(path.stem for path in directory.glob("*.json"))
    journals_root = root / "journals"
    for journal in sorted(journals_root.iterdir()) if journals_root.exists() else []:
        if not journal.is_dir() or journal.name in known_run_ids:
            continue
        try:
            manifest = load_persistence_manifest_v2(journal / "manifest.json")
        except Exception:
            # Historical behavior ignored unregistered junk directories. They
            # carry no pending claim and cannot mutate external state.
            continue
        immutable = manifest["immutable"]
        if journal.name != immutable["run_id"]:
            continue
        state = str(manifest["state"])
        if state in {"completed", "rolled_back", "recovery_required"}:
            receipt = None
            receipt_valid = False
            if state == "completed":
                try:
                    receipt = _load_planned_receipt(manifest)
                    verification = verify_publication_receipt(
                        receipt, root_map=_root_map_from_manifest(manifest)
                    )
                    if not verification["valid"]:
                        raise PersistenceV2IntegrityError(
                            "discovered completed receipt failed durable verification"
                        )
                    _assert_receipt_matches_manifest(receipt, manifest)
                    receipt_valid = True
                except Exception as exc:
                    state = "recovery_required"
                    manifest["state"] = state
                    _append_error(manifest, "completed_discovery_failed", exc)
                    _write_manifest(journal / "manifest.json", manifest)
            _transition_registry(
                root, manifest, state, receipt=receipt if receipt_valid else None
            )
            results.append(
                _result(
                    manifest,
                    receipt_valid=receipt_valid,
                    receipt=receipt if receipt_valid else None,
                )
            )
            continue
        _write_registry_entry_new(
            root / "registry" / "pending" / f"{journal.name}.json",
            _pending_registry_entry(manifest),
        )
        known_run_ids.add(journal.name)
    return results


def reconcile_pending_persistence_v2(
    transaction_root: str | Path,
    *,
    expected_book_id: str | None = None,
) -> dict[str, Any]:
    root = assert_safe_local_tree(transaction_root)
    if not root.exists():
        return {
            "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
            "ok": True,
            "scanned_registry": str(root / "registry" / "pending"),
            "transaction_count": 0,
            "transactions": [],
            "recovery_required": [],
        }
    results: list[dict[str, Any]] = []

    # Discovery mutates staging/journal/registry state and therefore shares
    # the same global lock as prepare. It must never quarantine a journal that
    # another process is still staging.
    with persistence_run_lock(root):
        results.extend(_discover_persistence_v2_transactions(root))

    pending_dir = root / "registry" / "pending"
    entries = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    for entry_path in entries:
        try:
            entry = _load_registry_entry(entry_path)
            journal = _registry_journal_path(root, entry)
            manifest_path = journal / "manifest.json"
            manifest = load_persistence_manifest_v2(manifest_path)
            if manifest["manifest_digest"] != entry["manifest_digest"]:
                raise PersistenceV2IntegrityError("pending registry manifest digest mismatch")
            if expected_book_id is not None and manifest["immutable"]["book_id"] != expected_book_id:
                raise PersistenceV2IntegrityError(
                    "story_project_state_identity_mismatch: "
                    f"journal book_id {manifest['immutable']['book_id']!r} does not match "
                    f"expected {expected_book_id!r}"
                )
            transaction = PersistenceV2Transaction(
                transaction_root=root,
                run_id=str(manifest["immutable"]["run_id"]),
                book_id=str(manifest["immutable"]["book_id"]),
                root_map=_root_map_from_manifest(manifest),
            )
            resolver = transaction._resolver_for_manifest(
                manifest, require_same_revision=True
            )
            apply_paths = _resolved_target_paths(
                manifest, phase="apply", resolver=resolver
            )
            with persistence_run_lock(root, state_paths=apply_paths):
                manifest = load_persistence_manifest_v2(manifest_path)
                marker_path = journal / "commit.marker"
                receipt_path = resolver.resolve(
                    manifest["immutable"]["publication_receipt"]["path_ref"]
                ).path
                def recover_completed() -> dict[str, Any]:
                    receipt = _load_json(receipt_path)
                    verification = verify_publication_receipt(
                        receipt, root_map=_root_map_from_manifest(manifest)
                    )
                    if not verification["valid"]:
                        raise PersistenceV2IntegrityError("existing receipt failed reconciliation validation")
                    _assert_receipt_matches_manifest(receipt, manifest)
                    manifest["state"] = "completed"
                    _write_manifest(manifest_path, manifest)
                    _transition_registry(root, manifest, "completed", receipt=receipt)
                    return _result(manifest, receipt_valid=True, receipt=receipt)

                def recover_forward() -> dict[str, Any]:
                    marker = load_commit_marker_v2(marker_path)
                    _verify_marker_against_manifest(marker, manifest)
                    _verify_apply_targets(manifest, resolver=resolver)
                    return transaction._complete_publication_locked()

                def recover_rollback() -> dict[str, Any]:
                    _validate_pending_candidate(journal, manifest)
                    return _rollback_pre_marker(
                        transaction_root=root,
                        journal_dir=journal,
                        manifest_path=manifest_path,
                        manifest=manifest,
                        pending_entry_path=entry_path,
                        resolver=resolver,
                    )

                results.append(
                    reconcile_marker_transaction(
                        marker_present=marker_path.exists(),
                        completion_present=receipt_path.exists(),
                        on_roll_back=recover_rollback,
                        on_roll_forward=recover_forward,
                        on_completed=recover_completed,
                    )
                )
        except PersistenceLockError:
            # Another normal writer won the lock after discovery. Busy is not
            # evidence of corruption and must not mutate the registry state.
            raise
        except Exception as exc:
            result = _mark_registry_recovery_required(root, entry_path, exc)
            results.append(result)
    return {
        "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
        "ok": not any(item.get("state") == "recovery_required" for item in results),
        "scanned_registry": str(pending_dir),
        "transaction_count": len(results),
        "transactions": results,
        "recovery_required": [item.get("run_id") for item in results if item.get("state") == "recovery_required"],
    }


def gc_persistence_v2(
    transaction_root: str | Path,
    *,
    dry_run: bool = True,
    completed_keep: int = 10,
    rolled_back_keep: int = 10,
) -> dict[str, Any]:
    root = Path(transaction_root).resolve()
    if completed_keep < 0 or rolled_back_keep < 0:
        raise ValueError("retention counts must be non-negative")
    pending = sorted((root / "registry" / "pending").glob("*.json"))
    recovery = sorted((root / "registry" / "recovery_required").glob("*.json"))
    if pending or recovery:
        reasons = []
        if pending:
            reasons.append("pending_transactions_require_reconcile")
        if recovery:
            reasons.append("recovery_required_transactions_are_permanently_retained")
        return {
            "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
            "dry_run": dry_run,
            "deleted": [],
            "reclaimed_bytes": 0,
            "skipped_reasons": reasons,
        }

    deletion_candidates: list[Path] = []
    for state, keep in (("completed", completed_keep), ("rolled_back", rolled_back_keep)):
        entries = [_load_registry_entry(path) for path in (root / "registry" / state).glob("*.json")]
        entries.sort(key=lambda item: str(item["registered_at"]), reverse=True)
        for entry in entries[keep:]:
            journal = _registry_journal_path(root, entry)
            for relative in ("staged", "backups"):
                directory = journal / relative
                if directory.exists():
                    deletion_candidates.extend(path for path in directory.rglob("*") if path.is_file())
            candidate = journal / "candidate_result.json"
            if candidate.exists() and candidate.is_file():
                deletion_candidates.append(candidate)

    unique = sorted(
        set(path.absolute() for path in deletion_candidates),
        key=lambda item: os.path.normcase(str(item)),
    )
    for path in unique:
        _assert_internal_path_safe(root, path)
    reclaimed = sum(path.stat().st_size for path in unique if path.exists())
    if not dry_run:
        for path in unique:
            path.unlink(missing_ok=True)
        for directory in sorted({path.parent for path in unique}, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
        "dry_run": dry_run,
        "deleted": [str(path) for path in unique],
        "reclaimed_bytes": reclaimed,
        "skipped_reasons": [],
    }


def _create_commit_marker(manifest: dict[str, Any]) -> dict[str, Any]:
    immutable = manifest["immutable"]
    marker = {
        "schema_version": manifest["schema_version"],
        "book_id": immutable["book_id"],
        "run_id": immutable["run_id"],
        "manifest": copy.deepcopy(immutable["manifest_path_ref"]),
        "manifest_digest": manifest["manifest_digest"],
        "candidate_digest": immutable["candidate"]["digest"],
        "artifact_bundle_digest": immutable["artifact_bundle_digest"],
        "final_run_hash": immutable["final_run"]["sha256"],
        "apply_target_bundle_digest": immutable["apply_target_bundle_digest"],
        "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
        "marked_at": _utc_now(),
    }
    marker["marker_hash"] = canonical_json_hash(marker, exclude_fields=("marker_hash",))
    return validate_commit_marker_v2(marker)


def _build_publication_receipt(manifest: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    immutable = manifest["immutable"]
    resolver = _safe_resolver_from_manifest_root_map(manifest)
    marker_path = resolver.resolve(immutable["marker_path_ref"]).path
    marker_size = marker_path.stat().st_size
    manifest_path = resolver.resolve(immutable["manifest_path_ref"]).path
    final_run = copy.deepcopy(immutable["final_run"])
    artifacts = [
        _target_receipt_binding(item)
        for item in _targets(manifest, phase="publication")
        if item["kind"] != "final_run_record"
    ]
    apply_targets = [_target_receipt_binding(item) for item in _targets(manifest, phase="apply")]
    receipt_plan = immutable["publication_receipt"]
    receipt = {
        "schema_version": manifest["schema_version"],
        "receipt_id": receipt_plan["id"],
        "receipt_path_ref": copy.deepcopy(receipt_plan["path_ref"]),
        "book_id": immutable["book_id"],
        "run_id": immutable["run_id"],
        "context_digest": immutable["context_digest"],
        "generation_input_context_digest": immutable["generation_input_context_digest"],
        "story_project_source_revision_after": copy.deepcopy(immutable["story_project_source_revision_after"]),
        "manifest": {
            "path_ref": copy.deepcopy(immutable["manifest_path_ref"]),
            "sha256": manifest["manifest_digest"],
            "size": manifest_path.stat().st_size,
        },
        "marker": {
            "path_ref": copy.deepcopy(immutable["marker_path_ref"]),
            "sha256": marker["marker_hash"],
            "size": marker_size,
        },
        "candidate_digest": immutable["candidate"]["digest"],
        "artifact_bundle_digest": immutable["artifact_bundle_digest"],
        "final_run": {
            "target_id": final_run["target_id"],
            "kind": final_run["kind"],
            "path_ref": copy.deepcopy(final_run["path_ref"]),
            "sha256": final_run["sha256"],
            "size": final_run["size"],
        },
        "artifacts": artifacts,
        "apply_targets": apply_targets,
        "delivery_jobs": copy.deepcopy(immutable["delivery_jobs"]),
        "canonical_json_algorithm": CANONICAL_JSON_ALGORITHM,
        "published_at": _utc_now(),
    }
    receipt["receipt_hash"] = canonical_json_hash(receipt, exclude_fields=("receipt_hash",))
    return validate_publication_receipt(receipt)


def _validate_manifest_immutable(immutable: dict[str, Any], *, schema_version: str) -> None:
    for field in ("book_id", "run_id", "root_map", "context_digest", "generation_input_context_digest", "candidate", "targets", "artifact_bundle_digest", "apply_target_bundle_digest", "final_run", "manifest_path_ref", "marker_path_ref", "publication_receipt", "delivery_jobs", "canonical_json_algorithm"):
        if field not in immutable:
            raise PersistenceV2IntegrityError(f"manifest immutable.{field} is required")
    _validate_id("book_id", immutable["book_id"])
    _validate_id("run_id", immutable["run_id"])
    _require_sha256("context_digest", immutable["context_digest"])
    _require_sha256("generation_input_context_digest", immutable["generation_input_context_digest"])
    _require_sha256("candidate.digest", immutable["candidate"].get("digest"))
    if immutable["candidate"].get("journal_relative_path") != "candidate_result.json":
        raise PersistenceV2IntegrityError("manifest candidate path is invalid")
    _require_sha256("artifact_bundle_digest", immutable["artifact_bundle_digest"])
    _require_sha256("apply_target_bundle_digest", immutable["apply_target_bundle_digest"])
    _root_map_from_immutable(immutable)
    if schema_version == "2.1":
        for field in ("root_registry", "story_project_read_set", "read_set_declared_writes"):
            if field not in immutable:
                raise PersistenceV2IntegrityError(f"manifest immutable.{field} is required for v2.1")
        if not isinstance(immutable["root_registry"], dict):
            raise PersistenceV2IntegrityError("manifest root_registry binding is invalid")
        if not isinstance(immutable["read_set_declared_writes"], list):
            raise PersistenceV2IntegrityError("manifest declared read-set writes must be an array")
        registry_binding = immutable["root_registry"]
        try:
            registry_id = str(uuid.UUID(str(registry_binding.get("registry_id"))))
        except ValueError as exc:
            raise PersistenceV2IntegrityError("manifest root registry id is invalid") from exc
        if registry_id != registry_binding.get("registry_id"):
            raise PersistenceV2IntegrityError("manifest root registry id is not canonical")
        if not isinstance(registry_binding.get("revision"), int) or isinstance(
            registry_binding.get("revision"), bool
        ):
            raise PersistenceV2IntegrityError("manifest root registry revision is invalid")
        _require_sha256("root_registry.registry_digest", registry_binding.get("registry_digest"))
        expected_root_uuids = {
            root_id: binding.get("root_uuid")
            for root_id, binding in immutable["root_map"].items()
        }
        if registry_binding.get("roots") != expected_root_uuids:
            raise PersistenceV2IntegrityError("manifest logical root UUID registry mismatch")
        if immutable["story_project_read_set"] is not None:
            try:
                validate_schema(
                    immutable["story_project_read_set"], "story_project_read_set.schema.json"
                )
            except SchemaValidationError as exc:
                raise PersistenceV2IntegrityError(str(exc)) from exc
        for write in immutable["read_set_declared_writes"]:
            if not isinstance(write, dict):
                raise PersistenceV2IntegrityError("declared read-set write must be an object")
            relative = str(write.get("relative_path") or "")
            if not relative or "\\" in relative or any(
                part in {"", ".", ".."} for part in relative.split("/")
            ):
                raise PersistenceV2IntegrityError("declared read-set write path is unsafe")
            if write.get("action") not in {"create", "replace", "delete"}:
                raise PersistenceV2IntegrityError("declared read-set write action is invalid")
    validate_path_ref(immutable["manifest_path_ref"])
    validate_path_ref(immutable["marker_path_ref"])
    receipt = immutable["publication_receipt"]
    _validate_id("receipt_id", receipt.get("id"))
    validate_path_ref(receipt.get("path_ref"))
    if schema_version == "2.1":
        for label, raw_ref in (
            ("manifest", immutable["manifest_path_ref"]),
            ("marker", immutable["marker_path_ref"]),
            ("receipt", receipt["path_ref"]),
        ):
            ref = validate_path_ref(raw_ref)
            binding = immutable["root_map"].get(ref.root_id, {})
            if ref.root_uuid is None or ref.root_uuid != binding.get("root_uuid"):
                raise PersistenceV2IntegrityError(
                    f"manifest {label} PathRef is not bound to its logical root UUID"
                )
        receipt_guard = receipt.get("path_guard")
        if not isinstance(receipt_guard, dict):
            raise PersistenceV2IntegrityError("manifest receipt safe path guard is missing")
        receipt_ref = validate_path_ref(receipt["path_ref"])
        if (
            receipt_guard.get("root_id") != receipt_ref.root_id
            or receipt_guard.get("root_uuid") != receipt_ref.root_uuid
            or receipt_guard.get("relative_path") != receipt_ref.relative_path
        ):
            raise PersistenceV2IntegrityError("manifest receipt safe path guard is invalid")
    targets = immutable["targets"]
    if not isinstance(targets, list) or not targets:
        raise PersistenceV2IntegrityError("manifest targets must be a non-empty list")
    ids: set[str] = set()
    for target in targets:
        target_id = _validate_id("target_id", target.get("target_id"))
        if target_id in ids:
            raise PersistenceV2IntegrityError(f"duplicate manifest target id: {target_id}")
        ids.add(target_id)
        if target.get("phase") not in {"apply", "publication"}:
            raise PersistenceV2IntegrityError("manifest target phase is invalid")
        validate_path_ref(target.get("path_ref"))
        if schema_version == "2.1":
            ref = validate_path_ref(target.get("path_ref"))
            root_binding = immutable["root_map"].get(ref.root_id, {})
            if ref.root_uuid is None or ref.root_uuid != root_binding.get("root_uuid"):
                raise PersistenceV2IntegrityError(
                    f"target.{target_id} is not bound to its logical root UUID"
                )
            if not isinstance(target.get("path_guard"), dict):
                raise PersistenceV2IntegrityError(f"target.{target_id} safe path guard is missing")
        _require_sha256(f"target.{target_id}.after_sha256", target.get("after_sha256"))
        if not isinstance(target.get("after_size"), int) or target["after_size"] < 0:
            raise PersistenceV2IntegrityError(f"target.{target_id}.after_size is invalid")
        staged_relative = str(target.get("staged_relative_path") or "")
        if not staged_relative.startswith("staged/"):
            raise PersistenceV2IntegrityError(f"target.{target_id} staged path is invalid")
        _validate_safe_relative(staged_relative)
        backup_relative = target.get("backup_relative_path")
        if backup_relative is not None:
            if not str(backup_relative).startswith("backups/"):
                raise PersistenceV2IntegrityError(f"target.{target_id} backup path is invalid")
            _validate_safe_relative(str(backup_relative))
    final_targets = [target for target in targets if target.get("kind") == "final_run_record"]
    if len(final_targets) != 1 or _target_hash_summary(final_targets[0]) != immutable["final_run"]:
        raise PersistenceV2IntegrityError("manifest must bind exactly one matching Final RunRecord")
    artifact_records = [
        _target_hash_summary(target)
        for target in targets
        if target.get("phase") == "publication" and target.get("kind") != "final_run_record"
    ]
    expected_artifact_digest = canonical_json_hash(
        sorted(artifact_records, key=lambda item: item["target_id"])
    )
    if expected_artifact_digest != immutable["artifact_bundle_digest"]:
        raise PersistenceV2IntegrityError("manifest artifact bundle digest mismatch")
    apply_records = [
        _target_hash_summary(target)
        for target in targets
        if target.get("phase") == "apply"
    ]
    expected_apply_digest = canonical_json_hash(sorted(apply_records, key=lambda item: item["target_id"]))
    if expected_apply_digest != immutable["apply_target_bundle_digest"]:
        raise PersistenceV2IntegrityError("manifest apply target bundle digest mismatch")
    if immutable["canonical_json_algorithm"] != CANONICAL_JSON_ALGORITHM:
        raise PersistenceV2IntegrityError("manifest canonical JSON algorithm is unsupported")


def _verify_marker_against_manifest(marker: dict[str, Any], manifest: dict[str, Any]) -> None:
    immutable = manifest["immutable"]
    if marker.get("schema_version") != manifest.get("schema_version"):
        raise PersistenceV2IntegrityError("CommitMarkerV2 schema version mismatch")
    expected = {
        "book_id": immutable["book_id"],
        "run_id": immutable["run_id"],
        "manifest_digest": manifest["manifest_digest"],
        "candidate_digest": immutable["candidate"]["digest"],
        "artifact_bundle_digest": immutable["artifact_bundle_digest"],
        "final_run_hash": immutable["final_run"]["sha256"],
        "apply_target_bundle_digest": immutable["apply_target_bundle_digest"],
    }
    for field, value in expected.items():
        if marker.get(field) != value:
            raise PersistenceV2IntegrityError(f"CommitMarkerV2 {field} does not match manifest")
    if marker.get("manifest") != immutable["manifest_path_ref"]:
        raise PersistenceV2IntegrityError("CommitMarkerV2 manifest PathRef mismatch")
    if marker.get("canonical_json_algorithm") != immutable["canonical_json_algorithm"]:
        raise PersistenceV2IntegrityError("CommitMarkerV2 canonical JSON algorithm mismatch")


def _verify_apply_targets(
    manifest: dict[str, Any],
    *,
    resolver: SafePathResolver | None = None,
) -> None:
    for target in _targets(manifest, phase="apply"):
        path = _resolve_manifest_target(
            manifest,
            target,
            resolver=resolver,
            enforce_guard=manifest.get("schema_version") == "2.1",
            allow_guard_extension=_target_guard_extension_allowed(manifest, target),
        )
        if _path_sha256(path) != target["after_sha256"]:
            raise PersistenceV2IntegrityError(f"committed apply target drift: {path}")


def _rollback_pre_marker(
    *,
    transaction_root: Path,
    journal_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    pending_entry_path: Path,
    resolver: SafePathResolver | None = None,
) -> dict[str, Any]:
    manifest["state"] = "rolling_back"
    _write_manifest_best_effort(manifest_path, manifest)
    failures: list[str] = []
    for target in reversed(_targets(manifest, phase="apply")):
        path: Path | None = None
        try:
            path = _resolve_manifest_target(
                manifest,
                target,
                resolver=resolver,
                enforce_guard=manifest.get("schema_version") == "2.1",
                allow_guard_extension=_target_guard_extension_allowed(manifest, target),
            )
            actual = _path_sha256(path)
            if actual == target.get("before_sha256"):
                manifest["progress"][target["target_id"]] = "rolled_back"
                continue
            if actual != target["after_sha256"]:
                raise PersistenceV2IntegrityError(f"rollback CAS mismatch: {path}")
            if target["before_exists"]:
                backup = _safe_journal_child(
                    journal_dir, str(target["backup_relative_path"])
                )
                content = backup.read_bytes()
                if _sha256(content) != target["before_sha256"]:
                    raise PersistenceV2IntegrityError(f"rollback backup hash mismatch: {backup}")
                if resolver is not None:
                    _resolve_manifest_target(
                        manifest,
                        target,
                        resolver=resolver,
                        enforce_guard=True,
                        allow_guard_extension=True,
                    )
                _atomic_replace_from_bytes(path, content)
            else:
                if resolver is not None:
                    _resolve_manifest_target(
                        manifest,
                        target,
                        resolver=resolver,
                        enforce_guard=True,
                        allow_guard_extension=True,
                    )
                path.unlink(missing_ok=False)
                _fsync_directory(path.parent)
            if _path_sha256(path) != target.get("before_sha256"):
                raise PersistenceV2IntegrityError(f"rollback verification failed: {path}")
            manifest["progress"][target["target_id"]] = "rolled_back"
        except Exception as exc:
            failures.append(f"{path or target.get('path_ref')}: {type(exc).__name__}: {exc}")
            manifest["progress"][target["target_id"]] = "rollback_failed"
    if failures:
        manifest["state"] = "recovery_required"
        for error in failures:
            manifest["errors"].append({"code": "rollback_failed", "error": error})
        _write_manifest_best_effort(manifest_path, manifest)
        _transition_registry(transaction_root, manifest, "recovery_required")
        return _result(manifest, receipt_valid=False)
    manifest["state"] = "rolled_back"
    _write_manifest(manifest_path, manifest)
    failure_receipt = {
        "schema_version": manifest["schema_version"],
        "book_id": manifest["immutable"]["book_id"],
        "run_id": manifest["immutable"]["run_id"],
        "manifest_digest": manifest["manifest_digest"],
        "state": "rolled_back",
        "errors": copy.deepcopy(manifest["errors"]),
        "recorded_at": _utc_now(),
    }
    failure_receipt["receipt_hash"] = canonical_json_hash(failure_receipt, exclude_fields=("receipt_hash",))
    path = journal_dir / "failure_receipt.json"
    if not path.exists():
        _write_new_file(path, _json_bytes(failure_receipt))
    _transition_registry(transaction_root, manifest, "rolled_back")
    return _result(manifest, receipt_valid=False)


def _transition_registry(
    transaction_root: Path,
    manifest: dict[str, Any],
    state: str,
    *,
    receipt: dict[str, Any] | None = None,
) -> None:
    immutable = manifest["immutable"]
    entry = {
        "schema_version": manifest["schema_version"],
        "book_id": immutable["book_id"],
        "run_id": immutable["run_id"],
        "state": state,
        "journal_relative_path": f"journals/{immutable['run_id']}",
        "manifest_digest": manifest["manifest_digest"],
        "registered_at": _utc_now(),
        "receipt": (
            {
                "id": receipt["receipt_id"],
                "path_ref": copy.deepcopy(immutable["publication_receipt"]["path_ref"]),
                "receipt_hash": receipt["receipt_hash"],
            }
            if receipt is not None
            else None
        ),
        "error_count": len(manifest["errors"]),
    }
    destination = transaction_root / "registry" / state / f"{immutable['run_id']}.json"
    _write_registry_entry_idempotent(destination, entry)
    (transaction_root / "registry" / "pending" / f"{immutable['run_id']}.json").unlink(missing_ok=True)


def _pending_registry_entry(manifest: Mapping[str, Any]) -> dict[str, Any]:
    immutable = manifest["immutable"]
    return {
        "schema_version": manifest["schema_version"],
        "book_id": immutable["book_id"],
        "run_id": immutable["run_id"],
        "state": "pending",
        "journal_relative_path": f"journals/{immutable['run_id']}",
        "manifest_digest": manifest["manifest_digest"],
        "registered_at": _utc_now(),
        "receipt": None,
        "error_count": len(manifest.get("errors") or []),
    }


def _mark_registry_recovery_required(root: Path, pending_entry_path: Path, exc: Exception) -> dict[str, Any]:
    try:
        entry = _load_registry_entry(pending_entry_path)
        run_id = str(entry["run_id"])
        book_id = str(entry["book_id"])
        digest = str(entry["manifest_digest"])
        journal_relative = str(entry["journal_relative_path"])
    except Exception:
        run_id = pending_entry_path.stem
        book_id = "unknown"
        digest = "0" * 64
        journal_relative = f"journals/{run_id}"
    manifest_path = (root / journal_relative / "manifest.json").resolve(strict=False)
    if _is_relative_to(manifest_path, root) and manifest_path.exists():
        try:
            manifest = load_persistence_manifest_v2(manifest_path)
            manifest["state"] = "recovery_required"
            _append_error(manifest, "reconcile_failed", exc)
            _write_manifest(manifest_path, manifest)
            digest = str(manifest["manifest_digest"])
        except Exception:
            pass
    recovery_entry = {
        "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
        "book_id": book_id,
        "run_id": run_id,
        "state": "recovery_required",
        "journal_relative_path": journal_relative,
        "manifest_digest": digest,
        "registered_at": _utc_now(),
        "receipt": None,
        "error_count": 1,
    }
    destination = root / "registry" / "recovery_required" / f"{run_id}.json"
    try:
        _write_registry_entry_idempotent(destination, recovery_entry)
        pending_entry_path.unlink(missing_ok=True)
    except Exception:
        pass
    return {
        "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
        "run_id": run_id,
        "book_id": book_id,
        "state": "recovery_required",
        "committed": False,
        "errors": [{"code": "reconcile_failed", "error": f"{type(exc).__name__}: {exc}"}],
    }


def _result(
    manifest: dict[str, Any],
    *,
    receipt_valid: bool,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    immutable = manifest["immutable"]
    return {
        "schema_version": manifest["schema_version"],
        "run_id": immutable["run_id"],
        "book_id": immutable["book_id"],
        "state": manifest["state"],
        "committed": manifest["state"] == "completed" and receipt_valid,
        "manifest_digest": manifest["manifest_digest"],
        "marker_path_ref": copy.deepcopy(immutable["marker_path_ref"]),
        "publication_receipt": (
            {
                "id": receipt["receipt_id"],
                "path_ref": copy.deepcopy(immutable["publication_receipt"]["path_ref"]),
                "receipt_hash": receipt["receipt_hash"],
                "valid": receipt_valid,
            }
            if receipt is not None
            else {
                "id": immutable["publication_receipt"]["id"],
                "path_ref": copy.deepcopy(immutable["publication_receipt"]["path_ref"]),
                "valid": False,
            }
        ),
        "errors": copy.deepcopy(manifest["errors"]),
    }


def _validate_final_run_receipt_pointer(
    final_run_record: Mapping[str, Any],
    receipt_id: str,
    receipt_path_ref: PathRef | Mapping[str, Any],
) -> dict[str, Any]:
    record = copy.deepcopy(dict(final_run_record))
    pointer_owner = _final_run_receipt_pointer_owner(record)
    pointer = pointer_owner.get("publication_receipt")
    expected = {"id": receipt_id, "path_ref": validate_path_ref(receipt_path_ref).to_dict()}
    if pointer != expected:
        raise PersistenceV2PreparationError(
            "Final RunRecord must contain only the predetermined publication receipt id and PathRef"
        )
    forbidden = {"receipt_hash", "hash", "publication_receipt_hash"}
    if forbidden.intersection(pointer):
        raise PersistenceV2PreparationError("Final RunRecord cannot contain a Publication Receipt hash")
    return record


def _final_run_receipt_pointer_owner(record: dict[str, Any]) -> dict[str, Any]:
    """Return the RunRecord object that owns the immutable receipt pointer.

    Persistence V2 accepts both a bare RunRecord and the existing RunResult
    envelope emitted by ``validate_run_result``.  The envelope itself is the
    published/hash-bound file, while its nested ``run`` object exclusively owns
    the pointer.  A pointer at both levels would make the commitment ambiguous,
    so even identical duplicate values are rejected.
    """

    if "run" not in record:
        return record
    if "publication_receipt" in record:
        raise PersistenceV2PreparationError(
            "RunResult envelope cannot contain an outer publication receipt pointer"
        )
    run_record = record.get("run")
    if not isinstance(run_record, Mapping):
        raise PersistenceV2PreparationError(
            "RunResult envelope run must be an object"
        )
    if not isinstance(run_record, dict):
        run_record = copy.deepcopy(dict(run_record))
        record["run"] = run_record
    return run_record


def _validate_delivery_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in jobs:
        job = copy.deepcopy(dict(raw))
        job_id = _validate_id("delivery_job.id", job.get("id"))
        if job_id in ids:
            raise PersistenceV2PreparationError(f"duplicate DeliveryJob id: {job_id}")
        ids.add(job_id)
        _require_sha256("delivery_job.payload_hash", job.get("payload_hash"))
        if not isinstance(job.get("policy"), dict):
            raise PersistenceV2PreparationError(f"DeliveryJob {job_id} policy must be an object")
        policy = job["policy"]
        if type(policy.get("required")) is not bool:
            raise PersistenceV2PreparationError(
                f"DeliveryJob {job_id} policy.required must be a boolean"
            )
        if policy.get("target") not in {"none", "file", "notion"}:
            raise PersistenceV2PreparationError(
                f"DeliveryJob {job_id} policy.target is unsupported"
            )
        if policy["target"] == "none" and policy["required"]:
            raise PersistenceV2PreparationError(
                f"DeliveryJob {job_id} cannot require a none target"
            )
        result.append({"id": job_id, "payload_hash": job["payload_hash"], "policy": job["policy"]})
    return sorted(result, key=lambda item: item["id"])


def _root_map_manifest(registry: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {
        root_id: {
            "root_uuid": str(binding["root_uuid"]),
            "path": str(binding["path"]),
            "identity_sha256": hashlib.sha256(
                os.path.normcase(str(Path(str(binding["path"])).absolute())).encode("utf-8")
            ).hexdigest(),
        }
        for root_id, binding in sorted(registry["roots"].items())
    }


def _root_map_from_immutable(immutable: Mapping[str, Any]) -> dict[str, Path]:
    raw = immutable.get("root_map")
    if not isinstance(raw, dict) or not raw:
        raise PersistenceV2IntegrityError("historical manifest root_map is missing")
    roots: dict[str, Path] = {}
    for root_id, binding in raw.items():
        if not isinstance(binding, dict):
            raise PersistenceV2IntegrityError(f"root_map binding is invalid: {root_id}")
        path = Path(str(binding.get("path"))).absolute()
        expected = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
        if binding.get("identity_sha256") != expected:
            raise PersistenceV2IntegrityError(f"historical root identity mismatch: {root_id}")
        if binding.get("root_uuid") is not None:
            try:
                if str(uuid.UUID(str(binding["root_uuid"]))) != binding["root_uuid"]:
                    raise ValueError("not canonical")
            except ValueError as exc:
                raise PersistenceV2IntegrityError(
                    f"historical root UUID is invalid: {root_id}"
                ) from exc
        roots[str(root_id)] = path
    return _validate_root_map(roots)


def _root_map_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Path]:
    return _root_map_from_immutable(manifest["immutable"])


def _validate_root_map(root_map: Mapping[str, str | Path]) -> dict[str, Path]:
    if not isinstance(root_map, Mapping) or "runtime" not in root_map:
        raise PersistenceV2PreparationError("root_map must include runtime")
    roots: dict[str, Path] = {}
    for root_id, raw in root_map.items():
        text = str(raw)
        if text.startswith("\\\\") or text.startswith("//"):
            raise PersistenceV2PreparationError(f"network roots are not supported: {text}")
        path = Path(raw).absolute()
        # Validate the root id through a harmless child PathRef.
        validate_path_ref(PathRef(root_id=str(root_id), relative_path=".root-identity"))
        roots[str(root_id)] = path
    return roots


def _validate_read_set_transaction_binding(
    *,
    book_id: str,
    registry: Mapping[str, Any],
    targets: list[Mapping[str, Any]],
    read_set: Mapping[str, Any] | None,
    declared_writes: Iterable[Mapping[str, Any]],
) -> None:
    from core.story_project.read_set import SOURCE_DIRECTORIES

    story_binding = registry.get("roots", {}).get("story_project")
    if not isinstance(story_binding, Mapping):
        raise PersistenceV2IntegrityError(
            "root registry is missing the StoryProject binding"
        )
    story_root = Path(str(story_binding.get("path"))).absolute()
    story_root_resolved = story_root.resolve()
    try:
        resolver = SafePathResolver(registry.get("roots", {}))
    except Exception as exc:
        raise PersistenceV2IntegrityError(
            f"cannot resolve persistence roots for StoryProject binding: {exc}"
        ) from exc

    source_targets: dict[str, Mapping[str, Any]] = {}
    identity_targets: dict[str, Mapping[str, Any]] = {}
    for target in targets:
        if target.get("phase") != "apply":
            continue
        ref = validate_path_ref(target.get("path_ref"))
        try:
            target_path = resolver.resolve(ref).path.resolve()
            physical_relative = target_path.relative_to(story_root_resolved)
        except ValueError:
            continue
        except Exception as exc:
            raise PersistenceV2IntegrityError(
                f"cannot classify persistence target against StoryProject: {exc}"
            ) from exc
        relative = physical_relative.as_posix()
        if relative == ".novelagent/project.json":
            identity_targets[relative] = target
        elif any(
            relative == directory or relative.startswith(directory + "/")
            for directory in SOURCE_DIRECTORIES
        ):
            source_targets[relative] = target

    declarations: dict[str, dict[str, Any]] = {}
    for raw in declared_writes:
        if not isinstance(raw, Mapping):
            raise PersistenceV2IntegrityError("declared read-set write must be an object")
        item = copy.deepcopy(dict(raw))
        relative = str(item.get("relative_path") or "").replace("\\", "/")
        if relative in declarations:
            raise PersistenceV2IntegrityError(
                f"duplicate declared read-set write: {relative}"
            )
        declarations[relative] = item

    relevant_targets = {**source_targets, **identity_targets}
    if relevant_targets and read_set is None:
        raise PersistenceV2IntegrityError(
            "StoryProject apply targets require a complete StoryProject read-set"
        )
    if read_set is None:
        if declarations:
            raise PersistenceV2IntegrityError(
                "declared read-set writes require a StoryProject read-set"
            )
        return
    try:
        validated = validate_schema(
            dict(read_set), "story_project_read_set.schema.json"
        )
    except SchemaValidationError as exc:
        raise PersistenceV2IntegrityError(str(exc)) from exc
    if validated["book_id"] != book_id:
        raise PersistenceV2IntegrityError(
            "StoryProject read-set book_id does not match the transaction"
        )
    info = os.stat(story_root)
    expected_root_identity = {
        "root_id": "story_project",
        "resolved_path": str(story_root.resolve()),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }
    if validated["root_identity"] != expected_root_identity:
        raise PersistenceV2IntegrityError(
            "StoryProject read-set root identity does not match the root registry"
        )

    try:
        from core.story_project.authority import AUTHORITY_MODE_EVENT
        from core.story_project.identity import load_project_identity

        current_identity = load_project_identity(story_root)
    except Exception as exc:
        raise PersistenceV2IntegrityError(
            f"cannot validate the StoryProject authority mode: {exc}"
        ) from exc
    current_authority = current_identity.authority or {}
    if (
        source_targets
        and current_authority.get("mode") == AUTHORITY_MODE_EVENT
        and set(identity_targets) != {".novelagent/project.json"}
    ):
        raise PersistenceV2IntegrityError(
            "event-authority StoryProject source writes require an atomic "
            "ProjectIdentity head transition"
        )

    if set(declarations) != set(relevant_targets):
        raise PersistenceV2IntegrityError(
            "declared read-set writes do not exactly match StoryProject apply targets"
        )
    for relative, target in relevant_targets.items():
        declaration = declarations[relative]
        expected_action = "replace" if target.get("before_exists") else "create"
        if (
            declaration.get("action") != expected_action
            or declaration.get("after_sha256") != target.get("after_sha256")
            or declaration.get("after_size") != target.get("after_size")
        ):
            raise PersistenceV2IntegrityError(
                f"declared read-set write does not bind target bytes: {relative}"
            )
        if relative in source_targets and declaration.get("role") not in (None, "source"):
            raise PersistenceV2IntegrityError(
                f"StoryProject source write has an invalid role: {relative}"
            )
        if relative in identity_targets:
            _validate_identity_transition_binding(
                read_set=validated,
                declaration=declaration,
                target=target,
            )


def _validate_identity_transition_binding(
    *,
    read_set: Mapping[str, Any],
    declaration: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    if declaration.get("role") != "project_identity":
        raise PersistenceV2IntegrityError(
            "ProjectIdentity target requires a project_identity declaration"
        )
    if target.get("before_sha256") != read_set.get("identity_revision"):
        raise PersistenceV2IntegrityError(
            "ProjectIdentity target before hash does not match the read-set"
        )
    required = {
        "book_id",
        "expected_authority_epoch",
        "expected_head_event_hash",
        "after_authority_epoch",
        "after_head_event_hash",
    }
    if not required.issubset(declaration):
        raise PersistenceV2IntegrityError(
            "ProjectIdentity declaration is missing authority CAS fields"
        )
    if declaration.get("book_id") != read_set.get("book_id"):
        raise PersistenceV2IntegrityError(
            "ProjectIdentity declaration belongs to another book"
        )
    content = target.get("_content")
    before = target.get("_before")
    if content is None or before is None:
        return
    try:
        from core.story_project.identity import validate_project_identity

        before_payload = json.loads(bytes(before).decode("utf-8-sig"))
        after_payload = json.loads(bytes(content).decode("utf-8-sig"))
        before_identity = validate_project_identity(before_payload)
        after_identity = validate_project_identity(after_payload)
    except Exception as exc:
        raise PersistenceV2IntegrityError(
            f"ProjectIdentity transition contains invalid JSON: {exc}"
        ) from exc
    before_authority = before_identity.authority or {}
    after_authority = after_identity.authority or {}
    if before_authority.get("mode") == "event_v1" and after_authority.get("mode") != "event_v1":
        raise PersistenceV2IntegrityError(
            "an event-authority ProjectIdentity cannot be downgraded"
        )
    actual = {
        "book_id": before_identity.book_id,
        "expected_authority_epoch": before_authority.get("authority_epoch"),
        "expected_head_event_hash": before_authority.get("head_event_hash"),
        "after_authority_epoch": after_authority.get("authority_epoch"),
        "after_head_event_hash": after_authority.get("head_event_hash"),
    }
    expected = {key: declaration.get(key) for key in actual}
    if actual != expected or after_identity.book_id != before_identity.book_id:
        raise PersistenceV2IntegrityError(
            "ProjectIdentity declaration does not match the before/after authority transition"
        )
    before_head = (
        before_authority.get("authority_epoch"),
        before_authority.get("head_event_hash"),
    )
    after_head = (
        after_authority.get("authority_epoch"),
        after_authority.get("head_event_hash"),
    )
    if after_head == before_head:
        raise PersistenceV2IntegrityError(
            "ProjectIdentity authority transition must advance its epoch or event head"
        )


def _validate_story_project_source_revision_after(
    value: Mapping[str, Any],
    *,
    book_id: str,
    registry: Mapping[str, Any],
    declared_writes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind the post-commit source checkpoint to an identity authority write.

    Legacy transactions that do not advance ProjectIdentity retain their
    existing opaque source-revision object.  Once a transaction declares an
    identity transition, however, the checkpoint is a strict projection of
    the bytes and authority tuple already bound by that declaration.
    """

    if not isinstance(value, Mapping):
        raise PersistenceV2IntegrityError(
            "story_project_source_revision_after must be an object"
        )
    identity_declarations = []
    for raw in declared_writes:
        if not isinstance(raw, Mapping):
            continue
        relative = str(raw.get("relative_path") or "").replace("\\", "/")
        if relative == ".novelagent/project.json":
            identity_declarations.append(raw)
    if not identity_declarations:
        return copy.deepcopy(dict(value))
    if len(identity_declarations) != 1:
        raise PersistenceV2IntegrityError(
            "exactly one ProjectIdentity declaration is required"
        )

    story_binding = registry.get("roots", {}).get("story_project")
    if not isinstance(story_binding, Mapping) or not story_binding.get("root_uuid"):
        raise PersistenceV2IntegrityError(
            "root registry is missing the StoryProject root UUID"
        )
    declaration = identity_declarations[0]
    expected = {
        "schema_version": "1.0",
        "book_id": book_id,
        "root_uuid": str(story_binding["root_uuid"]),
        "identity_sha256": declaration.get("after_sha256"),
        "authority_epoch": declaration.get("after_authority_epoch"),
        "head_event_hash": declaration.get("after_head_event_hash"),
    }
    if dict(value) != expected:
        raise PersistenceV2IntegrityError(
            "story_project_source_revision_after does not match the committed "
            "ProjectIdentity authority transition"
        )
    return copy.deepcopy(expected)


def _legacy_safe_resolver(root_map: Mapping[str, Path]) -> SafePathResolver:
    return SafePathResolver(
        {
            root_id: RootBinding(
                root_id=root_id,
                root_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"novelagent:legacy-root:{root_id}:{path}")),
                path=path,
            )
            for root_id, path in root_map.items()
        }
    )


def _safe_resolver_from_manifest_root_map(
    manifest: Mapping[str, Any],
) -> SafePathResolver:
    immutable = manifest["immutable"]
    roots = _root_map_from_immutable(immutable)
    raw_bindings = immutable.get("root_map") or {}
    bindings: dict[str, RootBinding] = {}
    for root_id, path in roots.items():
        raw = raw_bindings.get(root_id) if isinstance(raw_bindings, dict) else None
        root_uuid = raw.get("root_uuid") if isinstance(raw, dict) else None
        if root_uuid is None:
            root_uuid = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"novelagent:legacy-root:{root_id}:{path}")
            )
        bindings[root_id] = RootBinding(root_id, str(root_uuid), path)
    return SafePathResolver(bindings)


def _verification_resolver(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    supplied_roots: Mapping[str, Path],
) -> SafePathResolver:
    if manifest.get("schema_version") != "2.1":
        return _legacy_safe_resolver(supplied_roots)
    try:
        transaction_root = manifest_path.parents[2]
    except IndexError as exc:
        raise PersistenceV2IntegrityError("cannot locate v2.1 root registry") from exc
    registry = load_root_registry(transaction_root / "root_registry.json")
    validate_registry_manifest_binding(
        manifest["immutable"]["root_registry"],
        registry,
        require_same_revision=False,
    )
    for root_id in manifest["immutable"]["root_registry"]["roots"]:
        if root_id not in supplied_roots:
            raise PersistenceV2IntegrityError(f"verification root is missing: {root_id}")
        expected = os.path.normcase(str(Path(registry["roots"][root_id]["path"]).absolute()))
        actual = os.path.normcase(str(Path(supplied_roots[root_id]).absolute()))
        if expected != actual:
            raise PersistenceV2IntegrityError(
                f"verification root requires explicit remap-roots: {root_id}"
            )
    return SafePathResolver(
        {
            root_id: RootBinding(
                root_id,
                str(binding["root_uuid"]),
                Path(str(binding["path"])),
            )
            for root_id, binding in registry["roots"].items()
        }
    )


def _targets(manifest: Mapping[str, Any], *, phase: str) -> list[dict[str, Any]]:
    return [item for item in manifest["immutable"]["targets"] if item.get("phase") == phase]


def _resolved_target_paths(
    manifest: Mapping[str, Any],
    *,
    phase: str,
    resolver: SafePathResolver | None = None,
) -> list[Path]:
    return [
        _resolve_manifest_target(
            manifest,
            target,
            resolver=resolver,
            enforce_guard=manifest.get("schema_version") == "2.1",
            allow_guard_extension=_target_guard_extension_allowed(manifest, target),
        )
        for target in _targets(manifest, phase=phase)
    ]


def _resolve_manifest_target(
    manifest: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    resolver: SafePathResolver | None = None,
    enforce_guard: bool = False,
    allow_guard_extension: bool = False,
) -> Path:
    if resolver is None:
        resolver = _safe_resolver_from_manifest_root_map(manifest)
    expected = target.get("path_guard") if enforce_guard else None
    return resolver.resolve(
        target["path_ref"],
        expected_guard=expected,
        allow_guard_extension=allow_guard_extension,
    ).path


def _target_guard_extension_allowed(
    manifest: Mapping[str, Any],
    target: Mapping[str, Any],
) -> bool:
    return manifest.get("progress", {}).get(target.get("target_id")) in {
        "applied",
        "published",
    }


def _target_hash_summary(target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_id": target["target_id"],
        "kind": target["kind"],
        "path_ref": copy.deepcopy(target["path_ref"]),
        "sha256": target["after_sha256"],
        "size": target["after_size"],
    }


def _target_receipt_binding(target: Mapping[str, Any]) -> dict[str, Any]:
    return _target_hash_summary(target)


def _artifact_digest_summary(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_id": binding["target_id"],
        "kind": binding["kind"],
        "path_ref": copy.deepcopy(binding["path_ref"]),
        "sha256": binding["sha256"],
        "size": binding["size"],
    }


def _verify_expected_before(
    target: PersistenceV2Target,
    path: Path,
    exists: bool,
    before_hash: str | None,
) -> None:
    if target.expected_before_exists is None:
        return
    if exists != target.expected_before_exists:
        raise PersistenceV2PreparationError(f"target existence changed before prepare: {path}")
    if exists:
        _require_sha256("expected_before_sha256", target.expected_before_sha256)
        if before_hash != target.expected_before_sha256:
            raise PersistenceV2PreparationError(f"target hash changed before prepare: {path}")
    elif target.expected_before_sha256 is not None:
        raise PersistenceV2PreparationError("missing target cannot have expected_before_sha256")


def _assert_before_image(path: Path, target: Mapping[str, Any]) -> None:
    actual = _path_sha256(path)
    if actual not in {target.get("before_sha256"), target.get("after_sha256")}:
        raise PersistenceV2IntegrityError(
            f"apply target CAS mismatch: {path}; expected={target.get('before_sha256')} actual={actual}"
        )


def _load_staged_content(journal_dir: Path, target: Mapping[str, Any]) -> bytes:
    path = _safe_journal_child(journal_dir, str(target["staged_relative_path"]))
    content = path.read_bytes()
    if _sha256(content) != target["after_sha256"] or len(content) != target["after_size"]:
        raise PersistenceV2IntegrityError(f"staged target hash mismatch: {path}")
    return content


def _validate_complete_staged_journal(
    staging_dir: Path,
    expected_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_persistence_manifest_v2(staging_dir / "manifest.json")
    if expected_manifest is not None and (
        manifest["manifest_digest"] != expected_manifest.get("manifest_digest")
        or manifest["immutable"] != expected_manifest.get("immutable")
    ):
        raise PersistenceV2IntegrityError("staged journal manifest changed before publish")
    _validate_pending_candidate(staging_dir, manifest)
    for target in manifest["immutable"]["targets"]:
        _load_staged_content(staging_dir, target)
        if target.get("phase") == "apply" and target.get("before_exists"):
            backup = _safe_journal_child(
                staging_dir, str(target.get("backup_relative_path"))
            )
            content = backup.read_bytes()
            if (
                _sha256(content) != target.get("before_sha256")
                or len(content) != target.get("before_size")
            ):
                raise PersistenceV2IntegrityError(f"staged backup hash mismatch: {backup}")
    return manifest


def _fsync_journal_tree(journal_dir: Path) -> None:
    for relative in ("staged", "backups"):
        directory = journal_dir / relative
        if directory.exists():
            _fsync_directory(directory)
    _fsync_directory(journal_dir)
    _fsync_directory(journal_dir.parent)


def _atomic_publish_journal(source: Path, destination: Path, transaction_root: Path) -> None:
    _assert_internal_path_safe(transaction_root, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_internal_path_safe(transaction_root, destination)
    if os.path.lexists(destination):
        raise FileExistsError(f"journal publish target already exists: {destination}")
    if os.name == "nt":
        _windows_move_file(source, destination, replace_existing=False)
    else:
        # Linux renameat2 provides a true directory no-clobber operation. The
        # fallback is still serialized by persistence_run_lock.
        used_renameat2 = False
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(destination),
                1,
            )
            if result != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))
            used_renameat2 = True
        except AttributeError:
            pass
        if not used_renameat2:
            os.rename(source, destination)
    _fsync_directory(destination.parent)


def _assert_internal_path_safe(root: Path, path: Path) -> None:
    root = Path(root).absolute()
    path = Path(path).absolute()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PersistenceV2IntegrityError(f"internal persistence path escapes root: {path}") from exc
    if relative in {"", "."}:
        assert_safe_local_tree(root)
        return
    root_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"novelagent:internal:{root}"))
    resolver = SafePathResolver(
        {"runtime": RootBinding("runtime", root_uuid, root)}
    )
    resolver.resolve(PathRef("runtime", relative, root_uuid=root_uuid))


def _safe_journal_child(journal_dir: Path, relative: str) -> Path:
    _validate_safe_relative(relative)
    path = journal_dir.joinpath(*relative.replace("\\", "/").split("/"))
    _assert_internal_path_safe(journal_dir, path)
    return path


def _validate_safe_relative(relative: str) -> None:
    normalized = relative.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or ":" in normalized.split("/")[0]
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise PersistenceV2IntegrityError(f"unsafe journal-relative path: {relative!r}")


def _publish_immutable(
    path: Path,
    content: bytes,
    expected_hash: str,
    *,
    resolver: SafePathResolver | None = None,
    path_ref: Mapping[str, Any] | PathRef | None = None,
    path_guard: Mapping[str, Any] | None = None,
) -> None:
    if path.exists():
        if _path_sha256(path) != expected_hash:
            raise PersistenceV2IntegrityError(f"immutable publication collision: {path}")
        return
    if resolver is not None and path_ref is not None:
        resolver.resolve(
            path_ref,
            expected_guard=path_guard,
            allow_guard_extension=True,
        )
    _write_new_file(path, content)
    if _path_sha256(path) != expected_hash:
        raise PersistenceV2IntegrityError(f"immutable publication verification failed: {path}")


def _validate_pending_candidate(journal_dir: Path, manifest: Mapping[str, Any]) -> None:
    candidate = manifest["immutable"]["candidate"]
    path = _safe_journal_child(journal_dir, str(candidate["journal_relative_path"]))
    content = path.read_bytes()
    if _sha256(content) != candidate["digest"] or len(content) != candidate["size"]:
        raise PersistenceV2IntegrityError("pending candidate hash mismatch")


def _verify_file_binding(path: Path, content: bytes, binding: Mapping[str, Any]) -> None:
    if _sha256(content) != binding.get("sha256") or len(content) != binding.get("size"):
        raise PersistenceV2IntegrityError(f"published file binding mismatch: {path}")


def _load_planned_receipt(manifest: Mapping[str, Any]) -> dict[str, Any]:
    path = _safe_resolver_from_manifest_root_map(manifest).resolve(
        manifest["immutable"]["publication_receipt"]["path_ref"]
    ).path
    receipt = _load_json(path)
    _assert_receipt_matches_manifest(receipt, manifest)
    return receipt


def _assert_receipt_matches_manifest(
    receipt: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    """Bind an otherwise valid receipt to this exact pending transaction."""

    validated = validate_publication_receipt(dict(receipt))
    immutable = manifest["immutable"]
    receipt_identity = {
        "schema_version": validated["schema_version"],
        "book_id": validated["book_id"],
        "run_id": validated["run_id"],
        "context_digest": validated["context_digest"],
        "generation_input_context_digest": validated["generation_input_context_digest"],
        "story_project_source_revision_after": validated[
            "story_project_source_revision_after"
        ],
        "receipt_id": validated["receipt_id"],
        "receipt_path_ref": validated["receipt_path_ref"],
    }
    expected_identity = {
        "schema_version": manifest["schema_version"],
        "book_id": immutable["book_id"],
        "run_id": immutable["run_id"],
        "context_digest": immutable["context_digest"],
        "generation_input_context_digest": immutable[
            "generation_input_context_digest"
        ],
        "story_project_source_revision_after": immutable[
            "story_project_source_revision_after"
        ],
        "receipt_id": immutable["publication_receipt"]["id"],
        "receipt_path_ref": immutable["publication_receipt"]["path_ref"],
    }
    if receipt_identity != expected_identity:
        raise PersistenceV2IntegrityError(
            "Publication Receipt identity does not match the pending manifest"
        )
    manifest_binding = validated["manifest"]
    if (
        manifest_binding.get("path_ref") != immutable["manifest_path_ref"]
        or manifest_binding.get("sha256") != manifest["manifest_digest"]
    ):
        raise PersistenceV2IntegrityError(
            "Publication Receipt is bound to another persistence manifest"
        )
    if validated["marker"].get("path_ref") != immutable["marker_path_ref"]:
        raise PersistenceV2IntegrityError(
            "Publication Receipt marker belongs to another transaction"
        )
    expected_final = {
        key: immutable["final_run"][key]
        for key in ("target_id", "kind", "path_ref", "sha256", "size")
    }
    expected_artifacts = [
        _target_receipt_binding(item)
        for item in _targets(manifest, phase="publication")
        if item["kind"] != "final_run_record"
    ]
    expected_apply = [
        _target_receipt_binding(item) for item in _targets(manifest, phase="apply")
    ]
    if (
        validated["candidate_digest"] != immutable["candidate"]["digest"]
        or validated["artifact_bundle_digest"] != immutable["artifact_bundle_digest"]
        or validated["final_run"] != expected_final
        or validated["artifacts"] != expected_artifacts
        or validated["apply_targets"] != expected_apply
        or validated["delivery_jobs"] != immutable["delivery_jobs"]
    ):
        raise PersistenceV2IntegrityError(
            "Publication Receipt payload does not match the pending manifest"
        )


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _utc_now()
    try:
        transaction_root = path.parents[2]
    except IndexError as exc:
        raise PersistenceV2IntegrityError("manifest path is not inside a transaction root") from exc
    _assert_internal_path_safe(transaction_root, path)
    _atomic_replace_from_bytes(path, _json_bytes(validate_persistence_manifest_v2(manifest)))


def _write_manifest_best_effort(path: Path, manifest: dict[str, Any]) -> None:
    try:
        _write_manifest(path, manifest)
    except Exception:
        pass


def _append_error(manifest: dict[str, Any], code: str, exc: Exception) -> None:
    manifest.setdefault("errors", []).append({"code": code, "error": f"{type(exc).__name__}: {exc}"})


def _safe_reload_manifest(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        return load_persistence_manifest_v2(path)
    except Exception:
        return fallback


def _load_registry_entry(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    try:
        return validate_schema(payload, "persistence_registry_entry.schema.json")
    except SchemaValidationError as exc:
        raise PersistenceV2IntegrityError(str(exc)) from exc


def _write_registry_entry_new(path: Path, entry: dict[str, Any]) -> None:
    validate_schema(entry, "persistence_registry_entry.schema.json")
    _assert_registry_entry_path_safe(path)
    _write_new_file(path, _json_bytes(entry))


def _replace_registry_entry(path: Path, entry: dict[str, Any]) -> None:
    validate_schema(entry, "persistence_registry_entry.schema.json")
    _assert_registry_entry_path_safe(path)
    _atomic_replace_from_bytes(path, _json_bytes(entry))


def _write_registry_entry_idempotent(path: Path, entry: dict[str, Any]) -> None:
    validate_schema(entry, "persistence_registry_entry.schema.json")
    _assert_registry_entry_path_safe(path)
    if path.exists():
        existing = _load_registry_entry(path)
        if existing.get("run_id") != entry.get("run_id") or existing.get("manifest_digest") != entry.get("manifest_digest"):
            raise PersistenceV2IntegrityError(f"registry entry collision: {path}")
        return
    _write_new_file(path, _json_bytes(entry))


def _assert_registry_entry_path_safe(path: Path) -> None:
    try:
        transaction_root = path.parents[2]
    except IndexError as exc:
        raise PersistenceV2IntegrityError("registry entry path is invalid") from exc
    if path.parent.parent.name != "registry":
        raise PersistenceV2IntegrityError("registry entry path is outside the registry")
    _assert_internal_path_safe(transaction_root, path)


def _registry_journal_path(root: Path, entry: Mapping[str, Any]) -> Path:
    relative = str(entry["journal_relative_path"])
    if not relative.startswith("journals/"):
        raise PersistenceV2IntegrityError("registry journal path is invalid")
    return _safe_journal_child(root, relative)


def _write_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_create_from_bytes(path, content)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceV2IntegrityError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PersistenceV2IntegrityError(f"JSON file must contain an object: {path}")
    return payload


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _path_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        return "not-a-regular-file"
    return _sha256(path.read_bytes())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _require_sha256(field: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PersistenceV2IntegrityError(f"{field} must be a lowercase SHA-256 digest")


def _validate_id(field: str, value: Any) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise PersistenceV2PreparationError(f"{field} is invalid: {value!r}")
    return text


def _inject(injector: _FaultInjector | None, event: str, index: int | None, path: Path | None) -> None:
    if injector is not None:
        injector(event, index, path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


__all__ = [
    "PERSISTENCE_V2_SCHEMA_VERSION",
    "PERSISTENCE_V2_STATES",
    "PersistenceV2Error",
    "PersistenceV2IntegrityError",
    "PersistenceV2PreparationError",
    "PersistenceV2Target",
    "PersistenceV2Transaction",
    "bind_final_run_record_receipt",
    "committed_from_publication_receipt",
    "gc_persistence_v2",
    "load_commit_marker_v2",
    "load_persistence_manifest_v2",
    "reconcile_pending_persistence_v2",
    "validate_commit_marker_v2",
    "validate_persistence_manifest_v2",
    "validate_publication_receipt",
    "verify_publication_receipt",
]
