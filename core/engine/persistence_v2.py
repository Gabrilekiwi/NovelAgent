from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.engine.persistence import (
    _atomic_create_from_bytes,
    _atomic_replace_from_bytes,
    _fsync_directory,
    persistence_run_lock,
)
from core.memory_v2.canonical import CANONICAL_JSON_ALGORITHM, canonical_json_hash
from core.path_refs import PathRef, path_ref_for, resolve_path_ref, validate_path_ref
from core.schema import SchemaValidationError, validate_schema


PERSISTENCE_V2_SCHEMA_VERSION = "2.0"
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
    bound["publication_receipt"] = {
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
    ) -> None:
        self.transaction_root = Path(transaction_root).resolve()
        self.run_id = _validate_id("run_id", run_id)
        self.book_id = _validate_id("book_id", book_id)
        self.root_map = _validate_root_map(root_map)
        self.journal_dir = self.transaction_root / "journals" / self.run_id
        self.manifest_path = self.journal_dir / "manifest.json"
        self.marker_path = self.journal_dir / "commit.marker"
        self.candidate_path = self.journal_dir / "candidate_result.json"
        self.registry_root = self.transaction_root / "registry"
        self.pending_entry_path = self.registry_root / "pending" / f"{self.run_id}.json"
        self._fault_injector = fault_injector

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
        if self.journal_dir.exists() or self.pending_entry_path.exists():
            raise PersistenceV2PreparationError(f"persistence transaction already exists: {self.run_id}")
        _require_sha256("context_digest", context_digest)
        _require_sha256("generation_input_context_digest", generation_input_context_digest)
        receipt_id = _validate_id("receipt_id", receipt_id)
        receipt_ref = validate_path_ref(receipt_path_ref)
        final_ref = validate_path_ref(final_run_path_ref)
        receipt_path = resolve_path_ref(receipt_ref, self.root_map)
        final_path = resolve_path_ref(final_ref, self.root_map)
        if receipt_path == final_path:
            raise PersistenceV2PreparationError("Final RunRecord and Publication Receipt paths must differ")

        final_record = _validate_final_run_receipt_pointer(final_run_record, receipt_id, receipt_ref)
        final_bytes = _json_bytes(final_record)
        candidate_bytes = _json_bytes(dict(candidate_result))
        candidate_digest = _sha256(candidate_bytes)

        apply_items = list(apply_targets)
        artifact_items = list(artifacts)
        final_target = PersistenceV2Target(
            target_id="final-run-record",
            kind="final_run_record",
            path_ref=final_ref,
            content=final_bytes,
            phase="publication",
            metadata={"immutable": True},
        )
        all_targets = [*apply_items, *artifact_items, final_target]
        prepared_targets = self._prepare_target_records(all_targets, receipt_ref=receipt_ref)
        if not any(item["phase"] == "apply" for item in prepared_targets):
            raise PersistenceV2PreparationError("at least one apply target is required")
        artifact_records = [
            _target_hash_summary(item)
            for item in prepared_targets
            if item["phase"] == "publication" and item["kind"] != "final_run_record"
        ]
        final_record_target = next(item for item in prepared_targets if item["kind"] == "final_run_record")
        apply_records = [_target_hash_summary(item) for item in prepared_targets if item["phase"] == "apply"]
        artifact_bundle_digest = canonical_json_hash(sorted(artifact_records, key=lambda item: item["target_id"]))
        apply_target_bundle_digest = canonical_json_hash(sorted(apply_records, key=lambda item: item["target_id"]))
        delivery_summaries = _validate_delivery_jobs(delivery_jobs)

        self.transaction_root.mkdir(parents=True, exist_ok=True)
        provisional_entry = {
            "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
            "book_id": self.book_id,
            "run_id": self.run_id,
            "state": "pending",
            "journal_relative_path": f"journals/{self.run_id}",
            "manifest_digest": "0" * 64,
            "registered_at": _utc_now(),
            "receipt": None,
            "error_count": 0,
        }
        _write_registry_entry_new(self.pending_entry_path, provisional_entry)
        try:
            self.journal_dir.mkdir(parents=True, exist_ok=False)
            (self.journal_dir / "staged").mkdir()
            (self.journal_dir / "backups").mkdir()
            for item in prepared_targets:
                content = item.pop("_content")
                before = item.pop("_before")
                _write_new_file(self.journal_dir / item["staged_relative_path"], content)
                if item["phase"] == "apply" and item["before_exists"]:
                    _write_new_file(self.journal_dir / str(item["backup_relative_path"]), before)
            _write_new_file(self.candidate_path, candidate_bytes)

            manifest_ref = path_ref_for(
                self.manifest_path,
                root_id="runtime",
                root=self.root_map["runtime"],
            )
            marker_ref = path_ref_for(
                self.marker_path,
                root_id="runtime",
                root=self.root_map["runtime"],
            )
            immutable = {
                "book_id": self.book_id,
                "run_id": self.run_id,
                "root_map": _root_map_manifest(self.root_map),
                "context_digest": context_digest,
                "generation_input_context_digest": generation_input_context_digest,
                "story_project_source_revision_after": copy.deepcopy(dict(story_project_source_revision_after)),
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
                "publication_receipt": {"id": receipt_id, "path_ref": receipt_ref.to_dict()},
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
            _write_new_file(self.manifest_path, _json_bytes(validate_persistence_manifest_v2(manifest)))
            provisional_entry["manifest_digest"] = manifest["manifest_digest"]
            _replace_registry_entry(self.pending_entry_path, provisional_entry)
            _fsync_directory(self.journal_dir)
            return copy.deepcopy(manifest)
        except Exception as exc:
            try:
                provisional_entry["error_count"] = 1
                _replace_registry_entry(self.pending_entry_path, provisional_entry)
            except Exception:
                pass
            raise PersistenceV2PreparationError(f"failed to prepare persistence v2 transaction: {exc}") from exc

    def commit(self) -> dict[str, Any]:
        manifest = load_persistence_manifest_v2(self.manifest_path)
        apply_paths = _resolved_target_paths(manifest, phase="apply")
        with persistence_run_lock(self.transaction_root, state_paths=apply_paths):
            manifest = load_persistence_manifest_v2(self.manifest_path)
            if manifest["state"] != "prepared":
                raise PersistenceV2Error(f"transaction is not prepared: {manifest['state']}")
            try:
                _validate_pending_candidate(self.journal_dir, manifest)
                manifest["state"] = "applying"
                _write_manifest(self.manifest_path, manifest)
                for index, target in enumerate(_targets(manifest, phase="apply")):
                    path = _resolve_manifest_target(manifest, target)
                    _inject(self._fault_injector, "before_apply_target", index, path)
                    _assert_before_image(path, target)
                    content = _load_staged_content(self.journal_dir, target)
                    if _path_sha256(path) != target["after_sha256"]:
                        _atomic_replace_from_bytes(path, content)
                    if _path_sha256(path) != target["after_sha256"]:
                        raise PersistenceV2IntegrityError(f"apply target after-hash mismatch: {path}")
                    manifest["progress"][target["target_id"]] = "applied"
                    _write_manifest(self.manifest_path, manifest)
                    _inject(self._fault_injector, "after_apply_target", index, path)

                _verify_apply_targets(manifest)
                marker = _create_commit_marker(manifest)
                _inject(self._fault_injector, "before_commit_marker", None, self.marker_path)
                _write_new_file(self.marker_path, _json_bytes(marker))
                _inject(self._fault_injector, "after_commit_marker", None, self.marker_path)
                manifest["state"] = "commit_marked"
                _write_manifest(self.manifest_path, manifest)
                return self._complete_publication_locked()
            except Exception as exc:
                manifest = _safe_reload_manifest(self.manifest_path, manifest)
                _append_error(manifest, "commit_failed", exc)
                if self.marker_path.exists():
                    manifest["state"] = "commit_marked"
                    _write_manifest_best_effort(self.manifest_path, manifest)
                    return _result(manifest, receipt_valid=False)
                return _rollback_pre_marker(
                    transaction_root=self.transaction_root,
                    journal_dir=self.journal_dir,
                    manifest_path=self.manifest_path,
                    manifest=manifest,
                    pending_entry_path=self.pending_entry_path,
                )

    def complete_publication(self) -> dict[str, Any]:
        manifest = load_persistence_manifest_v2(self.manifest_path)
        apply_paths = _resolved_target_paths(manifest, phase="apply")
        with persistence_run_lock(self.transaction_root, state_paths=apply_paths):
            return self._complete_publication_locked()

    def _complete_publication_locked(self) -> dict[str, Any]:
        manifest = load_persistence_manifest_v2(self.manifest_path)
        if manifest["state"] == "completed":
            receipt = _load_planned_receipt(manifest)
            verification = verify_publication_receipt(receipt, root_map=_root_map_from_manifest(manifest))
            return _result(manifest, receipt_valid=bool(verification["valid"]), receipt=receipt)
        if not self.marker_path.exists():
            raise PersistenceV2Error("publication cannot start before commit marker")
        try:
            marker = load_commit_marker_v2(self.marker_path)
            _verify_marker_against_manifest(marker, manifest)
            _verify_apply_targets(manifest)
            manifest["state"] = "publishing"
            _write_manifest(self.manifest_path, manifest)
            for index, target in enumerate(_targets(manifest, phase="publication")):
                path = _resolve_manifest_target(manifest, target)
                content = _load_staged_content(self.journal_dir, target)
                _inject(self._fault_injector, "before_publication_target", index, path)
                _publish_immutable(path, content, str(target["after_sha256"]))
                manifest["progress"][target["target_id"]] = "published"
                _write_manifest(self.manifest_path, manifest)
                _inject(self._fault_injector, "after_publication_target", index, path)

            receipt = _build_publication_receipt(manifest, marker)
            receipt_path = resolve_path_ref(
                receipt["receipt_path_ref"],
                _root_map_from_manifest(manifest),
            )
            _inject(self._fault_injector, "before_publication_receipt", None, receipt_path)
            if receipt_path.exists():
                existing = _load_json(receipt_path)
                verification = verify_publication_receipt(existing, root_map=_root_map_from_manifest(manifest))
                if not verification["valid"] or existing["run_id"] != self.run_id:
                    raise PersistenceV2IntegrityError("existing Publication Receipt is invalid or belongs to another run")
                receipt = existing
            else:
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
    ) -> list[dict[str, Any]]:
        if not targets:
            raise PersistenceV2PreparationError("at least one target is required")
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_paths: set[Path] = set()
        receipt_path = resolve_path_ref(receipt_ref, self.root_map)
        for index, target in enumerate(targets):
            if not isinstance(target, PersistenceV2Target):
                raise PersistenceV2PreparationError(f"target {index} must be PersistenceV2Target")
            target_id = _validate_id("target_id", target.target_id)
            if target_id in seen_ids:
                raise PersistenceV2PreparationError(f"duplicate target id: {target_id}")
            seen_ids.add(target_id)
            if target.phase not in {"apply", "publication"}:
                raise PersistenceV2PreparationError(f"target {target_id} has invalid phase: {target.phase}")
            ref = validate_path_ref(target.path_ref)
            path = resolve_path_ref(ref, self.root_map)
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
    _validate_manifest_immutable(immutable)
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
        manifest_path = resolve_path_ref(manifest_binding["path_ref"], roots)
        manifest = load_persistence_manifest_v2(manifest_path)
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
        marker_path = resolve_path_ref(marker_binding["path_ref"], roots)
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
        final_path = resolve_path_ref(final_binding["path_ref"], roots)
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
            artifact_path = resolve_path_ref(artifact["path_ref"], roots)
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
            "schema_version": "2.0",
            "valid": True,
            "committed": True,
            "book_id": validated["book_id"],
            "run_id": validated["run_id"],
            "receipt_id": validated["receipt_id"],
            "receipt_hash": validated["receipt_hash"],
            "errors": [],
        }
    except Exception as exc:
        return {
            "schema_version": "2.0",
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


def reconcile_pending_persistence_v2(transaction_root: str | Path) -> dict[str, Any]:
    root = Path(transaction_root).resolve()
    pending_dir = root / "registry" / "pending"
    entries = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    results: list[dict[str, Any]] = []
    for entry_path in entries:
        try:
            entry = _load_registry_entry(entry_path)
            journal = _registry_journal_path(root, entry)
            manifest_path = journal / "manifest.json"
            manifest = load_persistence_manifest_v2(manifest_path)
            if manifest["manifest_digest"] != entry["manifest_digest"]:
                raise PersistenceV2IntegrityError("pending registry manifest digest mismatch")
            apply_paths = _resolved_target_paths(manifest, phase="apply")
            with persistence_run_lock(root, state_paths=apply_paths):
                manifest = load_persistence_manifest_v2(manifest_path)
                marker_path = journal / "commit.marker"
                receipt_path = resolve_path_ref(
                    manifest["immutable"]["publication_receipt"]["path_ref"],
                    _root_map_from_manifest(manifest),
                )
                if receipt_path.exists():
                    receipt = _load_json(receipt_path)
                    verification = verify_publication_receipt(receipt, root_map=_root_map_from_manifest(manifest))
                    if not verification["valid"]:
                        raise PersistenceV2IntegrityError("existing receipt failed reconciliation validation")
                    manifest["state"] = "completed"
                    _write_manifest(manifest_path, manifest)
                    _transition_registry(root, manifest, "completed", receipt=receipt)
                    results.append(_result(manifest, receipt_valid=True, receipt=receipt))
                    continue
                if marker_path.exists():
                    marker = load_commit_marker_v2(marker_path)
                    _verify_marker_against_manifest(marker, manifest)
                    _verify_apply_targets(manifest)
                    transaction = PersistenceV2Transaction(
                        transaction_root=root,
                        run_id=str(manifest["immutable"]["run_id"]),
                        book_id=str(manifest["immutable"]["book_id"]),
                        root_map=_root_map_from_manifest(manifest),
                    )
                    results.append(transaction._complete_publication_locked())
                    continue
                _validate_pending_candidate(journal, manifest)
                results.append(
                    _rollback_pre_marker(
                        transaction_root=root,
                        journal_dir=journal,
                        manifest_path=manifest_path,
                        manifest=manifest,
                        pending_entry_path=entry_path,
                    )
                )
        except Exception as exc:
            result = _mark_registry_recovery_required(root, entry_path, exc)
            results.append(result)
    return {
        "schema_version": "2.0",
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
            "schema_version": "2.0",
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

    unique = sorted(set(path.resolve() for path in deletion_candidates), key=lambda item: os.path.normcase(str(item)))
    for path in unique:
        if not _is_relative_to(path, root):
            raise PersistenceV2IntegrityError(f"GC path escapes transaction root: {path}")
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
        "schema_version": "2.0",
        "dry_run": dry_run,
        "deleted": [str(path) for path in unique],
        "reclaimed_bytes": reclaimed,
        "skipped_reasons": [],
    }


def _create_commit_marker(manifest: dict[str, Any]) -> dict[str, Any]:
    immutable = manifest["immutable"]
    marker = {
        "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
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
    roots = _root_map_from_manifest(manifest)
    marker_path = resolve_path_ref(immutable["marker_path_ref"], roots)
    marker_size = marker_path.stat().st_size
    manifest_path = resolve_path_ref(immutable["manifest_path_ref"], roots)
    final_run = copy.deepcopy(immutable["final_run"])
    artifacts = [
        _target_receipt_binding(item)
        for item in _targets(manifest, phase="publication")
        if item["kind"] != "final_run_record"
    ]
    apply_targets = [_target_receipt_binding(item) for item in _targets(manifest, phase="apply")]
    receipt_plan = immutable["publication_receipt"]
    receipt = {
        "schema_version": PERSISTENCE_V2_SCHEMA_VERSION,
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


def _validate_manifest_immutable(immutable: dict[str, Any]) -> None:
    for field in ("book_id", "run_id", "root_map", "context_digest", "generation_input_context_digest", "candidate", "targets", "artifact_bundle_digest", "apply_target_bundle_digest", "final_run", "manifest_path_ref", "marker_path_ref", "publication_receipt", "delivery_jobs", "canonical_json_algorithm"):
        if field not in immutable:
            raise PersistenceV2IntegrityError(f"manifest immutable.{field} is required")
    _validate_id("book_id", immutable["book_id"])
    _validate_id("run_id", immutable["run_id"])
    _require_sha256("context_digest", immutable["context_digest"])
    _require_sha256("generation_input_context_digest", immutable["generation_input_context_digest"])
    _require_sha256("candidate.digest", immutable["candidate"].get("digest"))
    _require_sha256("artifact_bundle_digest", immutable["artifact_bundle_digest"])
    _require_sha256("apply_target_bundle_digest", immutable["apply_target_bundle_digest"])
    _root_map_from_immutable(immutable)
    validate_path_ref(immutable["manifest_path_ref"])
    validate_path_ref(immutable["marker_path_ref"])
    receipt = immutable["publication_receipt"]
    _validate_id("receipt_id", receipt.get("id"))
    validate_path_ref(receipt.get("path_ref"))
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
        _require_sha256(f"target.{target_id}.after_sha256", target.get("after_sha256"))
        if not isinstance(target.get("after_size"), int) or target["after_size"] < 0:
            raise PersistenceV2IntegrityError(f"target.{target_id}.after_size is invalid")
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


def _verify_apply_targets(manifest: dict[str, Any]) -> None:
    for target in _targets(manifest, phase="apply"):
        path = _resolve_manifest_target(manifest, target)
        if _path_sha256(path) != target["after_sha256"]:
            raise PersistenceV2IntegrityError(f"committed apply target drift: {path}")


def _rollback_pre_marker(
    *,
    transaction_root: Path,
    journal_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    pending_entry_path: Path,
) -> dict[str, Any]:
    manifest["state"] = "rolling_back"
    _write_manifest_best_effort(manifest_path, manifest)
    failures: list[str] = []
    for target in reversed(_targets(manifest, phase="apply")):
        path = _resolve_manifest_target(manifest, target)
        actual = _path_sha256(path)
        try:
            if actual == target.get("before_sha256"):
                manifest["progress"][target["target_id"]] = "rolled_back"
                continue
            if actual != target["after_sha256"]:
                raise PersistenceV2IntegrityError(f"rollback CAS mismatch: {path}")
            if target["before_exists"]:
                backup = journal_dir / str(target["backup_relative_path"])
                content = backup.read_bytes()
                if _sha256(content) != target["before_sha256"]:
                    raise PersistenceV2IntegrityError(f"rollback backup hash mismatch: {backup}")
                _atomic_replace_from_bytes(path, content)
            else:
                path.unlink(missing_ok=False)
                _fsync_directory(path.parent)
            if _path_sha256(path) != target.get("before_sha256"):
                raise PersistenceV2IntegrityError(f"rollback verification failed: {path}")
            manifest["progress"][target["target_id"]] = "rolled_back"
        except Exception as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
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
        "schema_version": "2.0",
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
        "schema_version": "2.0",
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
        "schema_version": "2.0",
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
        "schema_version": "2.0",
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
        "schema_version": "2.0",
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
    pointer = record.get("publication_receipt")
    expected = {"id": receipt_id, "path_ref": validate_path_ref(receipt_path_ref).to_dict()}
    if pointer != expected:
        raise PersistenceV2PreparationError(
            "Final RunRecord must contain only the predetermined publication receipt id and PathRef"
        )
    forbidden = {"receipt_hash", "hash", "publication_receipt_hash"}
    if forbidden.intersection(pointer):
        raise PersistenceV2PreparationError("Final RunRecord cannot contain a Publication Receipt hash")
    return record


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
        result.append({"id": job_id, "payload_hash": job["payload_hash"], "policy": job["policy"]})
    return sorted(result, key=lambda item: item["id"])


def _root_map_manifest(root_map: Mapping[str, Path]) -> dict[str, dict[str, str]]:
    return {
        root_id: {
            "path": str(path),
            "identity_sha256": hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest(),
        }
        for root_id, path in sorted(root_map.items())
    }


def _root_map_from_immutable(immutable: Mapping[str, Any]) -> dict[str, Path]:
    raw = immutable.get("root_map")
    if not isinstance(raw, dict) or not raw:
        raise PersistenceV2IntegrityError("historical manifest root_map is missing")
    roots: dict[str, Path] = {}
    for root_id, binding in raw.items():
        if not isinstance(binding, dict):
            raise PersistenceV2IntegrityError(f"root_map binding is invalid: {root_id}")
        path = Path(str(binding.get("path"))).resolve(strict=False)
        expected = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
        if binding.get("identity_sha256") != expected:
            raise PersistenceV2IntegrityError(f"historical root identity mismatch: {root_id}")
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
        path = Path(raw).resolve(strict=False)
        # Validate the root id through a harmless child PathRef.
        validate_path_ref(PathRef(root_id=str(root_id), relative_path=".root-identity"))
        roots[str(root_id)] = path
    return roots


def _targets(manifest: Mapping[str, Any], *, phase: str) -> list[dict[str, Any]]:
    return [item for item in manifest["immutable"]["targets"] if item.get("phase") == phase]


def _resolved_target_paths(manifest: Mapping[str, Any], *, phase: str) -> list[Path]:
    return [_resolve_manifest_target(manifest, target) for target in _targets(manifest, phase=phase)]


def _resolve_manifest_target(manifest: Mapping[str, Any], target: Mapping[str, Any]) -> Path:
    return resolve_path_ref(target["path_ref"], _root_map_from_manifest(manifest))


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
    path = (journal_dir / str(target["staged_relative_path"])).resolve(strict=False)
    if not _is_relative_to(path, journal_dir):
        raise PersistenceV2IntegrityError("staged path escapes journal")
    content = path.read_bytes()
    if _sha256(content) != target["after_sha256"] or len(content) != target["after_size"]:
        raise PersistenceV2IntegrityError(f"staged target hash mismatch: {path}")
    return content


def _publish_immutable(path: Path, content: bytes, expected_hash: str) -> None:
    if path.exists():
        if _path_sha256(path) != expected_hash:
            raise PersistenceV2IntegrityError(f"immutable publication collision: {path}")
        return
    _write_new_file(path, content)
    if _path_sha256(path) != expected_hash:
        raise PersistenceV2IntegrityError(f"immutable publication verification failed: {path}")


def _validate_pending_candidate(journal_dir: Path, manifest: Mapping[str, Any]) -> None:
    candidate = manifest["immutable"]["candidate"]
    path = (journal_dir / str(candidate["journal_relative_path"])).resolve(strict=False)
    if not _is_relative_to(path, journal_dir):
        raise PersistenceV2IntegrityError("candidate path escapes journal")
    content = path.read_bytes()
    if _sha256(content) != candidate["digest"] or len(content) != candidate["size"]:
        raise PersistenceV2IntegrityError("pending candidate hash mismatch")


def _verify_file_binding(path: Path, content: bytes, binding: Mapping[str, Any]) -> None:
    if _sha256(content) != binding.get("sha256") or len(content) != binding.get("size"):
        raise PersistenceV2IntegrityError(f"published file binding mismatch: {path}")


def _load_planned_receipt(manifest: Mapping[str, Any]) -> dict[str, Any]:
    path = resolve_path_ref(
        manifest["immutable"]["publication_receipt"]["path_ref"],
        _root_map_from_manifest(manifest),
    )
    return _load_json(path)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _utc_now()
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
    _write_new_file(path, _json_bytes(entry))


def _replace_registry_entry(path: Path, entry: dict[str, Any]) -> None:
    validate_schema(entry, "persistence_registry_entry.schema.json")
    _atomic_replace_from_bytes(path, _json_bytes(entry))


def _write_registry_entry_idempotent(path: Path, entry: dict[str, Any]) -> None:
    validate_schema(entry, "persistence_registry_entry.schema.json")
    if path.exists():
        existing = _load_registry_entry(path)
        if existing.get("run_id") != entry.get("run_id") or existing.get("manifest_digest") != entry.get("manifest_digest"):
            raise PersistenceV2IntegrityError(f"registry entry collision: {path}")
        return
    _write_new_file(path, _json_bytes(entry))


def _registry_journal_path(root: Path, entry: Mapping[str, Any]) -> Path:
    journal = (root / str(entry["journal_relative_path"])).resolve(strict=False)
    if not _is_relative_to(journal, root):
        raise PersistenceV2IntegrityError("registry journal path escapes transaction root")
    return journal


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
