from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.delivery import DeliveryQueue
from core.delivery_intents import (
    delivery_intent_receipt_binding,
    materialize_delivery_job,
    validate_delivery_intent,
)
from core.engine.persistence_v2 import (
    load_persistence_manifest_v2,
    validate_publication_receipt,
    verify_publication_receipt,
)
from core.engine.safe_paths import RootBinding, SafePathResolver, assert_safe_local_tree
from core.path_refs import validate_path_ref
from core.schema import SchemaValidationError, validate_schema


DELIVERY_INTENT_RECOVERY_SCHEMA_VERSION = "1.0"
DELIVERY_INTENT_ARTIFACT_KIND = "delivery_intent"


class DeliveryIntentRecoveryError(RuntimeError):
    """A fail-closed error while rebuilding local jobs from committed evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class _IntentSource:
    intent: dict[str, Any]
    source: str


@dataclass(frozen=True)
class _ReceiptRecoveryPlan:
    receipt: dict[str, Any]
    verification: dict[str, Any]
    intents: tuple[_IntentSource, ...]


def recover_delivery_jobs_for_receipt(
    publication_receipt: Mapping[str, Any] | str | Path,
    *,
    root_map: Mapping[str, str | Path],
    queue: DeliveryQueue,
    intent_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Materialize every job committed by one durable PublicationReceipt.

    The function is suitable for the post-commit/pre-enqueue crash window.  It
    never invokes a delivery adapter: successful recovery only creates or
    returns local ``DeliveryJob`` records in ``queue``.

    DeliveryIntent publication artifacts are discovered automatically.  The
    optional paths support older/future callers that persisted the intent at a
    separately managed location.  In both cases the complete set of intents
    must match ``receipt.delivery_jobs`` exactly before any job is materialized.
    """

    explicit = tuple(_load_explicit_intent(path) for path in _intent_path_tuple(intent_paths))
    plan = _prepare_receipt_recovery(
        publication_receipt,
        root_map=root_map,
        explicit_intents=explicit,
    )
    return _materialize_plans((plan,), queue=queue, source="publication_receipt")


def recover_completed_delivery_jobs(
    transaction_root: str | Path,
    *,
    root_map: Mapping[str, str | Path],
    queue: DeliveryQueue,
    intent_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Recover jobs for all trusted entries in persistence ``registry/completed``.

    Discovery is two phase.  Every completed registry entry, manifest, durable
    receipt, Final RunRecord, artifact and intent binding is verified first.
    Jobs are materialized only when the entire scan is valid, so a corrupt or
    orphan intent cannot be silently skipped while other jobs are enqueued.
    Repeated calls are idempotent through ``DeliveryQueue.enqueue``.
    """

    root = assert_safe_local_tree(transaction_root)
    explicit = tuple(_load_explicit_intent(path) for path in _intent_path_tuple(intent_paths))
    explicit_by_scope: dict[tuple[str, str], list[_IntentSource]] = {}
    for item in explicit:
        key = (str(item.intent["book_id"]), str(item.intent["run_id"]))
        explicit_by_scope.setdefault(key, []).append(item)

    completed_root = root / "registry" / "completed"
    if not completed_root.exists():
        if explicit:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_orphan_intent",
                "explicit DeliveryIntent has no completed persistence receipt",
            )
        return _empty_report(source="completed_registry")
    assert_safe_local_tree(completed_root)

    plans: list[_ReceiptRecoveryPlan] = []
    matched_scopes: set[tuple[str, str]] = set()
    for entry_path in sorted(completed_root.glob("*.json")):
        entry = _load_completed_registry_entry(entry_path)
        run_id = str(entry["run_id"])
        book_id = str(entry["book_id"])
        if entry_path.stem != run_id:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "completed registry filename does not match run_id",
            )
        expected_journal = f"journals/{run_id}"
        if entry["journal_relative_path"] != expected_journal:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "completed registry journal path is not canonical",
            )
        journal = root / "journals" / run_id
        assert_safe_local_tree(journal)
        manifest_path = journal / "manifest.json"
        manifest = load_persistence_manifest_v2(manifest_path)
        immutable = manifest["immutable"]
        if (
            manifest["state"] != "completed"
            or manifest["manifest_digest"] != entry["manifest_digest"]
            or immutable["book_id"] != book_id
            or immutable["run_id"] != run_id
        ):
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "completed registry entry does not match its completed manifest",
            )
        manifest_ref = _safe_resolve_path_ref(immutable["manifest_path_ref"], root_map)
        if not _same_path(manifest_ref, manifest_path):
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "completed manifest PathRef resolves to another journal",
            )

        receipt_summary = entry.get("receipt")
        receipt_plan = immutable["publication_receipt"]
        if not isinstance(receipt_summary, Mapping):
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "completed registry entry has no PublicationReceipt binding",
            )
        if (
            receipt_summary.get("id") != receipt_plan["id"]
            or receipt_summary.get("path_ref") != receipt_plan["path_ref"]
        ):
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "completed registry PublicationReceipt binding differs from manifest",
            )
        receipt_path = _safe_resolve_path_ref(receipt_plan["path_ref"], root_map)
        scope = (book_id, run_id)
        plan = _prepare_receipt_recovery(
            receipt_path,
            root_map=root_map,
            explicit_intents=tuple(explicit_by_scope.get(scope, ())),
        )
        receipt = plan.receipt
        receipt_manifest = receipt.get("manifest")
        if (
            receipt["book_id"] != book_id
            or receipt["run_id"] != run_id
            or receipt["receipt_id"] != receipt_plan["id"]
            or receipt["receipt_path_ref"] != receipt_plan["path_ref"]
            or not isinstance(receipt_manifest, Mapping)
            or receipt_manifest.get("path_ref") != immutable["manifest_path_ref"]
            or receipt_manifest.get("sha256") != manifest["manifest_digest"]
            or receipt_summary.get("receipt_hash") != receipt["receipt_hash"]
        ):
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_completed_registry_invalid",
                "durable PublicationReceipt does not belong to the scanned completed entry",
            )
        plans.append(plan)
        matched_scopes.add(scope)

    orphan_scopes = sorted(set(explicit_by_scope).difference(matched_scopes))
    if orphan_scopes:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_orphan_intent",
            f"explicit DeliveryIntent has no completed receipt for scope {orphan_scopes[0]!r}",
        )
    return _materialize_plans(tuple(plans), queue=queue, source="completed_registry")


def _prepare_receipt_recovery(
    publication_receipt: Mapping[str, Any] | str | Path,
    *,
    root_map: Mapping[str, str | Path],
    explicit_intents: tuple[_IntentSource, ...],
) -> _ReceiptRecoveryPlan:
    verification = verify_publication_receipt(publication_receipt, root_map=root_map)
    if not verification.get("valid") or not verification.get("committed"):
        errors = verification.get("errors")
        detail = (
            errors[0].get("error")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict)
            else "unknown verification failure"
        )
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_receipt_untrusted",
            f"PublicationReceipt failed durable verification: {detail}",
        )
    raw_receipt = (
        _load_json_object(Path(publication_receipt), label="PublicationReceipt")
        if isinstance(publication_receipt, (str, Path))
        else copy.deepcopy(dict(publication_receipt))
    )
    try:
        receipt = validate_publication_receipt(raw_receipt)
    except Exception as exc:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_receipt_untrusted",
            f"PublicationReceipt contract is invalid: {type(exc).__name__}: {exc}",
        ) from exc
    if any(
        verification.get(field) != receipt[field]
        for field in ("book_id", "run_id", "receipt_id", "receipt_hash", "delivery_jobs")
    ):
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_receipt_untrusted",
            "durable verification result differs from PublicationReceipt content",
        )

    sources: list[_IntentSource] = []
    for artifact in receipt["artifacts"]:
        if artifact.get("kind") != DELIVERY_INTENT_ARTIFACT_KIND:
            continue
        sources.append(_load_receipt_artifact_intent(artifact, root_map=root_map))
    sources.extend(explicit_intents)

    expected_by_id: dict[str, dict[str, Any]] = {}
    for raw_binding in receipt["delivery_jobs"]:
        if not isinstance(raw_binding, dict) or not isinstance(raw_binding.get("id"), str):
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_receipt_untrusted",
                "PublicationReceipt contains an invalid DeliveryJob binding",
            )
        identifier = raw_binding["id"]
        if identifier in expected_by_id:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_receipt_untrusted",
                f"PublicationReceipt contains duplicate DeliveryJob id {identifier!r}",
            )
        expected_by_id[identifier] = copy.deepcopy(raw_binding)

    intents_by_id: dict[str, _IntentSource] = {}
    for source in sources:
        intent = source.intent
        identifier = str(intent["intent_id"])
        if intent["book_id"] != receipt["book_id"] or intent["run_id"] != receipt["run_id"]:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_orphan_intent",
                f"DeliveryIntent {identifier!r} belongs to another receipt scope",
            )
        binding = delivery_intent_receipt_binding(intent)
        if expected_by_id.get(identifier) != binding:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_orphan_intent",
                f"DeliveryIntent {identifier!r} is not exactly committed by PublicationReceipt",
            )
        existing = intents_by_id.get(identifier)
        if existing is not None:
            if existing.intent != intent:
                raise DeliveryIntentRecoveryError(
                    "delivery_recovery_intent_collision",
                    f"multiple DeliveryIntents claim id {identifier!r} with different content",
                )
            continue
        intents_by_id[identifier] = source

    missing = sorted(set(expected_by_id).difference(intents_by_id))
    extra = sorted(set(intents_by_id).difference(expected_by_id))
    if missing:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_intent_missing",
            f"PublicationReceipt has no recoverable DeliveryIntent for job {missing[0]!r}",
        )
    if extra:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_orphan_intent",
            f"DeliveryIntent {extra[0]!r} is not committed by PublicationReceipt",
        )
    ordered = tuple(intents_by_id[identifier] for identifier in sorted(expected_by_id))
    return _ReceiptRecoveryPlan(
        receipt=receipt,
        verification=copy.deepcopy(verification),
        intents=ordered,
    )


def _materialize_plans(
    plans: tuple[_ReceiptRecoveryPlan, ...],
    *,
    queue: DeliveryQueue,
    source: str,
) -> dict[str, Any]:
    _preflight_queue(plans, queue=queue)
    jobs: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for plan in plans:
        receipt_jobs: list[dict[str, Any]] = []
        for item in plan.intents:
            try:
                job = materialize_delivery_job(
                    item.intent,
                    publication_receipt=plan.receipt,
                    queue=queue,
                )
            except Exception as exc:
                raise DeliveryIntentRecoveryError(
                    "delivery_recovery_materialization_failed",
                    f"failed to materialize DeliveryJob {item.intent['intent_id']!r}: {type(exc).__name__}: {exc}",
                ) from exc
            summary = {
                "job_id": job["job_id"],
                "book_id": job["book_id"],
                "run_id": job["run_id"],
                "state": job["state"],
                "policy": job["policy"],
                "target_type": job["target_type"],
                "publication_receipt_hash": job["publication_receipt_hash"],
                "intent_source": item.source,
            }
            jobs.append(summary)
            receipt_jobs.append(summary)
        receipts.append(
            {
                "book_id": plan.receipt["book_id"],
                "run_id": plan.receipt["run_id"],
                "receipt_id": plan.receipt["receipt_id"],
                "receipt_hash": plan.receipt["receipt_hash"],
                "job_count": len(receipt_jobs),
                "jobs": receipt_jobs,
            }
        )
    return {
        "schema_version": DELIVERY_INTENT_RECOVERY_SCHEMA_VERSION,
        "ok": True,
        "source": source,
        "receipt_count": len(receipts),
        "job_count": len(jobs),
        "receipts": receipts,
        "jobs": jobs,
    }


def _preflight_queue(
    plans: tuple[_ReceiptRecoveryPlan, ...],
    *,
    queue: DeliveryQueue,
) -> None:
    """Reject existing job collisions before materializing any new job."""

    planned_ids: dict[str, dict[str, Any]] = {}
    for plan in plans:
        for item in plan.intents:
            intent = item.intent
            identifier = str(intent["intent_id"])
            immutable = {
                "job_id": identifier,
                "book_id": intent["book_id"],
                "run_id": intent["run_id"],
                "publication_receipt_hash": plan.receipt["receipt_hash"],
                "target_type": "file",
                "target": copy.deepcopy(intent["target"]),
                "payload_hash": intent["job_payload_hash"],
                "policy": intent["policy"],
            }
            previous = planned_ids.get(identifier)
            if previous is not None and previous != immutable:
                raise DeliveryIntentRecoveryError(
                    "delivery_recovery_job_collision",
                    f"multiple committed receipts claim incompatible DeliveryJob id {identifier!r}",
                )
            planned_ids[identifier] = immutable

    for identifier, expected in planned_ids.items():
        path = queue.jobs_dir / f"{identifier}.json"
        if not path.exists():
            continue
        try:
            existing = queue.load(identifier)
        except Exception as exc:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_job_collision",
                f"existing DeliveryJob {identifier!r} is invalid: {type(exc).__name__}: {exc}",
            ) from exc
        actual = {field: existing.get(field) for field in expected}
        if actual != expected:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_job_collision",
                f"existing DeliveryJob {identifier!r} has incompatible immutable content",
            )


def _empty_report(*, source: str) -> dict[str, Any]:
    return {
        "schema_version": DELIVERY_INTENT_RECOVERY_SCHEMA_VERSION,
        "ok": True,
        "source": source,
        "receipt_count": 0,
        "job_count": 0,
        "receipts": [],
        "jobs": [],
    }


def _load_receipt_artifact_intent(
    artifact: Mapping[str, Any],
    *,
    root_map: Mapping[str, str | Path],
) -> _IntentSource:
    path = _safe_resolve_path_ref(artifact.get("path_ref"), root_map)
    content = _read_regular_file(path, label="DeliveryIntent artifact")
    expected_size = artifact.get("size")
    expected_hash = artifact.get("sha256")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or len(content) != expected_size
        or not isinstance(expected_hash, str)
        or hashlib.sha256(content).hexdigest() != expected_hash
    ):
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_artifact_tampered",
            "DeliveryIntent artifact no longer matches its PublicationReceipt binding",
        )
    return _IntentSource(
        intent=_decode_and_validate_intent(content, label="DeliveryIntent artifact"),
        source=f"receipt_artifact:{artifact.get('target_id')}",
    )


def _load_explicit_intent(path: str | Path) -> _IntentSource:
    resolved = Path(path).absolute()
    content = _read_regular_file(resolved, label="explicit DeliveryIntent")
    return _IntentSource(
        intent=_decode_and_validate_intent(content, label="explicit DeliveryIntent"),
        source="explicit",
    )


def _decode_and_validate_intent(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = _decode_json_object(content, label=label)
        return validate_delivery_intent(payload)
    except DeliveryIntentRecoveryError:
        raise
    except Exception as exc:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_intent_invalid",
            f"{label} is invalid: {type(exc).__name__}: {exc}",
        ) from exc


def _load_completed_registry_entry(path: Path) -> dict[str, Any]:
    payload = _load_json_object(path, label="completed persistence registry entry")
    try:
        entry = validate_schema(payload, "persistence_registry_entry.schema.json")
    except SchemaValidationError as exc:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_completed_registry_invalid",
            f"completed registry contract is invalid: {exc}",
        ) from exc
    if entry["state"] != "completed":
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_completed_registry_invalid",
            "non-completed entry exists in completed registry",
        )
    return entry


def _safe_resolve_path_ref(
    raw_ref: Any,
    root_map: Mapping[str, str | Path],
) -> Path:
    try:
        ref = validate_path_ref(raw_ref)
        if ref.root_uuid is None:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_path_untrusted",
                "recovery requires root-UUID-bound PathRefs",
            )
        if ref.root_id not in root_map:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_path_untrusted",
                f"logical root {ref.root_id!r} is not mapped",
            )
        resolver = SafePathResolver(
            {
                ref.root_id: RootBinding(
                    root_id=ref.root_id,
                    root_uuid=ref.root_uuid,
                    path=Path(root_map[ref.root_id]).absolute(),
                )
            }
        )
        return resolver.resolve(ref).path
    except DeliveryIntentRecoveryError:
        raise
    except Exception as exc:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_path_untrusted",
            f"cannot safely resolve persistence PathRef: {type(exc).__name__}: {exc}",
        ) from exc


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    return _decode_json_object(_read_regular_file(path, label=label), label=label)


def _decode_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, DeliveryIntentRecoveryError) as exc:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_json_invalid",
            f"{label} is not unambiguous UTF-8 JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_json_invalid",
            f"{label} must contain a JSON object",
        )
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DeliveryIntentRecoveryError(
                "delivery_recovery_json_invalid",
                f"duplicate JSON key {key!r}",
            )
        payload[key] = value
    return payload


def _read_regular_file(path: Path, *, label: str) -> bytes:
    try:
        assert_safe_local_tree(path.parent)
        info = os.lstat(path)
    except Exception as exc:
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_path_untrusted",
            f"cannot safely inspect {label}: {type(exc).__name__}: {exc}",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DeliveryIntentRecoveryError(
            "delivery_recovery_path_untrusted",
            f"{label} is not a regular local file",
        )
    return path.read_bytes()


def _intent_path_tuple(paths: Iterable[str | Path]) -> tuple[str | Path, ...]:
    if isinstance(paths, (str, Path)):
        return (paths,)
    return tuple(paths)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.absolute())) == os.path.normcase(str(right.absolute()))


__all__ = [
    "DELIVERY_INTENT_ARTIFACT_KIND",
    "DELIVERY_INTENT_RECOVERY_SCHEMA_VERSION",
    "DeliveryIntentRecoveryError",
    "recover_completed_delivery_jobs",
    "recover_delivery_jobs_for_receipt",
]
