from __future__ import annotations

import copy
import json
from pathlib import PurePath
import re
from typing import Any, Mapping

from core.delivery import DeliveryQueue, delivery_payload_hash
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import validate_memory_event_batch
from core.path_refs import PathRef, PathRefError, validate_path_ref
from core.schema import SchemaValidationError, validate_schema


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROOT_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_FORBIDDEN_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "credentials", "password", "secret", "token"}
)
_ALLOWED_TEMPLATE_FIELDS = frozenset({"run_id", "chapter_index"})
_PATH_FIELD_NAMES = frozenset(
    {
        "cwd",
        "destination",
        "directory",
        "dir",
        "evidence_path",
        "file",
        "file_path",
        "filepath",
        "location",
        "original_path_hint",
        "path",
        "root",
        "source",
        "target",
        "workspace",
    }
)
_KNOWN_POSIX_ROOTS = frozenset(
    {
        "/dev",
        "/etc",
        "/home",
        "/mnt",
        "/opt",
        "/private",
        "/proc",
        "/root",
        "/run",
        "/sys",
        "/tmp",
        "/users",
        "/usr",
        "/var",
        "/volumes",
    }
)


class DeliveryIntentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def validate_file_delivery_profile(value: Any) -> dict[str, Any]:
    profile = _validate_schema_mapping(value, "file_delivery_profile.schema.json", "FileDeliveryProfile")
    _safe_id("profile_id", profile["profile_id"])
    root_id = str(profile["root_id"])
    if not root_id.startswith("external:") or _SAFE_ID.fullmatch(root_id[len("external:") :]) is None:
        raise DeliveryIntentError(
            "file_delivery_profile_root_invalid", "file export roots must use an external:<profile> logical id"
        )
    root_uuid = profile["root_uuid"]
    if root_uuid is not None and (not isinstance(root_uuid, str) or _ROOT_UUID.fullmatch(root_uuid) is None):
        raise DeliveryIntentError(
            "file_delivery_profile_root_uuid_invalid", "root_uuid must be a canonical lowercase UUID"
        )
    directory = _safe_relative_path("relative_directory", profile["relative_directory"])
    if PurePath(directory).suffix:
        raise DeliveryIntentError(
            "file_delivery_profile_directory_invalid", "relative_directory must identify a directory"
        )
    template = str(profile["filename_template"])
    fields = _template_fields(template)
    if not fields or not fields.issubset(_ALLOWED_TEMPLATE_FIELDS):
        raise DeliveryIntentError(
            "file_delivery_profile_template_invalid",
            "filename_template may use only {run_id} and {chapter_index} and must include one",
        )
    if "/" in template or "\\" in template or ".." in template:
        raise DeliveryIntentError(
            "file_delivery_profile_template_invalid", "filename_template must be a safe filename"
        )
    if not template.endswith(".json"):
        raise DeliveryIntentError(
            "file_delivery_profile_template_invalid", "canonical chapter exports must use a .json filename"
        )
    _assert_public_value(profile)
    return profile


def build_file_delivery_intent(
    *,
    profile: Mapping[str, Any],
    book_id: str,
    run_id: str,
    chapter_index: int,
    event_batch: Mapping[str, Any],
    chapter_body_sha256: str,
    policy: str,
    created_at: str,
) -> dict[str, Any]:
    trusted = validate_file_delivery_profile(dict(profile))
    resolved_book_id = _safe_id("book_id", book_id)
    resolved_run_id = _safe_id("run_id", run_id)
    chapter = _positive_chapter(chapter_index)
    resolved_policy = str(policy)
    if resolved_policy not in {"required", "best_effort"}:
        raise DeliveryIntentError("delivery_intent_policy_invalid", "file delivery policy is invalid")
    batch = _json_copy(event_batch)
    _assert_public_value(batch)
    batch_hash = _sha256("event_batch.batch_hash", batch.get("batch_hash"))
    body_hash = _sha256("chapter_body_sha256", chapter_body_sha256)
    timestamp = _required_text("created_at", created_at)
    filename = trusted["filename_template"].format(
        run_id=resolved_run_id,
        chapter_index=f"{chapter:06d}",
    )
    relative_path = _safe_relative_path(
        "delivery target",
        f"{trusted['relative_directory'].rstrip('/')}/{filename}",
    )
    uniqueness_token = resolved_run_id if "{run_id}" in trusted["filename_template"] else f"{chapter:06d}"
    if uniqueness_token not in filename:
        raise DeliveryIntentError(
            "delivery_intent_target_not_unique", "file target must contain the run or chapter unique identifier"
        )
    target = {
        "path_ref": validate_path_ref(
            PathRef(
                root_id=trusted["root_id"],
                root_uuid=trusted["root_uuid"],
                relative_path=relative_path,
            )
        ).to_dict()
    }
    canonical_payload = {
        "schema_version": "1.0",
        "kind": "canonical_chapter_export",
        "book_id": resolved_book_id,
        "run_id": resolved_run_id,
        "chapter_index": chapter,
        "event_batch_hash": batch_hash,
        "event_batch": batch,
        "chapter_body_sha256": body_hash,
    }
    job_payload = _file_job_payload(canonical_payload)
    identity = {
        "book_id": resolved_book_id,
        "run_id": resolved_run_id,
        "chapter_index": chapter,
        "profile_id": trusted["profile_id"],
        "event_batch_hash": batch_hash,
        "chapter_body_sha256": body_hash,
    }
    intent_id = f"delivery-{chapter:06d}-{canonical_json_hash(identity)[:16]}"
    intent = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "book_id": resolved_book_id,
        "run_id": resolved_run_id,
        "chapter_index": chapter,
        "target_type": "file",
        "profile_id": trusted["profile_id"],
        "target": target,
        "policy": resolved_policy,
        "canonical_payload": canonical_payload,
        "job_payload_hash": delivery_payload_hash(job_payload),
        "created_at": timestamp,
    }
    intent["intent_hash"] = canonical_json_hash(intent)
    return validate_delivery_intent(intent)


def validate_delivery_intent(value: Any) -> dict[str, Any]:
    intent = _validate_schema_mapping(value, "delivery_intent.schema.json", "DeliveryIntent")
    for field in ("intent_id", "book_id", "run_id", "profile_id"):
        _safe_id(field, intent[field])
    _positive_chapter(intent["chapter_index"])
    _sha256("intent_hash", intent["intent_hash"])
    _sha256("job_payload_hash", intent["job_payload_hash"])
    try:
        target_payload = intent["target"]
        if not isinstance(target_payload, dict) or set(target_payload) != {"path_ref"}:
            raise PathRefError("file target must contain exactly one path_ref")
        target = validate_path_ref(target_payload["path_ref"])
    except PathRefError as exc:
        raise DeliveryIntentError("delivery_intent_target_invalid", str(exc)) from exc
    if not target.root_id.startswith("external:"):
        raise DeliveryIntentError("delivery_intent_target_invalid", "delivery target is not an export root")
    filename = PurePath(target.relative_path).name
    chapter_token = f"{int(intent['chapter_index']):06d}"
    if intent["run_id"] not in filename and chapter_token not in filename:
        raise DeliveryIntentError(
            "delivery_intent_target_not_unique", "delivery target lacks a run or chapter unique identifier"
        )
    payload = intent["canonical_payload"]
    if not isinstance(payload, dict):
        raise DeliveryIntentError("delivery_intent_payload_invalid", "canonical_payload must be an object")
    expected_fields = {
        "schema_version",
        "kind",
        "book_id",
        "run_id",
        "chapter_index",
        "event_batch_hash",
        "event_batch",
        "chapter_body_sha256",
    }
    if set(payload) != expected_fields:
        raise DeliveryIntentError("delivery_intent_payload_invalid", "canonical payload fields are invalid")
    if payload["schema_version"] != "1.0" or payload["kind"] != "canonical_chapter_export":
        raise DeliveryIntentError("delivery_intent_payload_invalid", "canonical payload version/kind is invalid")
    for field in ("book_id", "run_id", "chapter_index"):
        if payload[field] != intent[field]:
            raise DeliveryIntentError("delivery_intent_scope_mismatch", f"payload {field} differs from intent")
    batch = payload["event_batch"]
    if not isinstance(batch, dict):
        raise DeliveryIntentError("delivery_intent_payload_invalid", "event_batch must be an object")
    try:
        batch = validate_memory_event_batch(batch)
    except ValueError as exc:
        raise DeliveryIntentError(
            "delivery_intent_event_batch_invalid", "event_batch is not a valid canonical Memory batch"
        ) from exc
    batch_hash = _sha256("event_batch_hash", payload["event_batch_hash"])
    if batch.get("batch_hash") != batch_hash:
        raise DeliveryIntentError("delivery_intent_batch_hash_mismatch", "event batch hash binding differs")
    if batch.get("schema_version") != "2.2":
        raise DeliveryIntentError("delivery_intent_event_batch_invalid", "chapter delivery requires Memory 2.2")
    if batch.get("book_id") != intent["book_id"]:
        raise DeliveryIntentError("delivery_intent_scope_mismatch", "event batch belongs to another book")
    if batch.get("batch_kind") != "chapter" or batch.get("publication_status") != "committed":
        raise DeliveryIntentError(
            "delivery_intent_event_batch_invalid", "chapter delivery requires a committed chapter batch"
        )
    body_hash = _sha256("chapter_body_sha256", payload["chapter_body_sha256"])
    if any(event.get("chapter_body_sha256") != body_hash for event in batch["events"]):
        raise DeliveryIntentError(
            "delivery_intent_body_hash_mismatch", "event evidence is not bound to the exported chapter body"
        )
    _assert_public_value(intent)
    if delivery_payload_hash(_file_job_payload(payload)) != intent["job_payload_hash"]:
        raise DeliveryIntentError("delivery_intent_job_payload_hash_mismatch", "file job payload was modified")
    expected_hash = canonical_json_hash(intent, exclude_fields=("intent_hash",))
    if intent["intent_hash"] != expected_hash:
        raise DeliveryIntentError("delivery_intent_hash_mismatch", "DeliveryIntent content was modified")
    return intent


def delivery_intent_receipt_binding(intent: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_delivery_intent(dict(intent))
    return {
        "id": validated["intent_id"],
        "payload_hash": validated["job_payload_hash"],
        "policy": {
            "required": validated["policy"] == "required",
            "target": validated["target_type"],
        },
    }


def materialize_delivery_job(
    intent: Mapping[str, Any],
    *,
    publication_receipt: Mapping[str, Any],
    queue: DeliveryQueue,
) -> dict[str, Any]:
    """Idempotently create a local job only after a matching PublicationReceipt exists."""

    validated = validate_delivery_intent(dict(intent))
    receipt = _validate_schema_mapping(
        publication_receipt, "publication_receipt.schema.json", "PublicationReceipt"
    )
    receipt_hash = _sha256("publication_receipt.receipt_hash", receipt["receipt_hash"])
    expected_receipt_hash = canonical_json_hash(receipt, exclude_fields=("receipt_hash",))
    if receipt_hash != expected_receipt_hash:
        raise DeliveryIntentError("delivery_publication_receipt_hash_mismatch", "PublicationReceipt was modified")
    if receipt["book_id"] != validated["book_id"] or receipt["run_id"] != validated["run_id"]:
        raise DeliveryIntentError("delivery_publication_scope_mismatch", "PublicationReceipt belongs to another run")
    binding = delivery_intent_receipt_binding(validated)
    matching = [item for item in receipt["delivery_jobs"] if isinstance(item, dict) and item.get("id") == binding["id"]]
    if matching != [binding]:
        raise DeliveryIntentError(
            "delivery_intent_not_published", "PublicationReceipt does not contain this complete intent binding"
        )
    return queue.enqueue(
        job_id=validated["intent_id"],
        book_id=validated["book_id"],
        run_id=validated["run_id"],
        publication_receipt_hash=receipt_hash,
        target_type="file",
        target=validated["target"],
        payload=_file_job_payload(validated["canonical_payload"]),
        policy=validated["policy"],
    )


def _file_job_payload(canonical_payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "content": json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    }


def _validate_schema_mapping(value: Any, schema: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DeliveryIntentError("delivery_intent_contract_invalid", f"{label} must be an object")
    try:
        return validate_schema(copy.deepcopy(dict(value)), schema)
    except SchemaValidationError as exc:
        raise DeliveryIntentError("delivery_intent_contract_invalid", f"invalid {label}: {exc}") from exc


def _template_fields(template: str) -> set[str]:
    fields = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template))
    stripped = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "", template)
    if "{" in stripped or "}" in stripped:
        raise DeliveryIntentError("file_delivery_profile_template_invalid", "malformed filename template")
    return fields


def _safe_relative_path(label: str, value: Any) -> str:
    if not isinstance(value, str) or value.strip() != value or not value:
        raise DeliveryIntentError("delivery_relative_path_invalid", f"{label} must be non-empty relative text")
    normalized = value.replace("\\", "/")
    path = PurePath(normalized)
    if path.is_absolute() or path.drive or normalized.startswith("//"):
        raise DeliveryIntentError("delivery_relative_path_invalid", f"{label} must be relative")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise DeliveryIntentError("delivery_relative_path_invalid", f"{label} contains an unsafe segment")
    return normalized


def _safe_id(label: str, value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise DeliveryIntentError("delivery_intent_id_invalid", f"{label} is not a safe identifier")
    return value


def _positive_chapter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeliveryIntentError("delivery_intent_chapter_invalid", "chapter_index must be positive")
    return value


def _sha256(label: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DeliveryIntentError("delivery_intent_digest_invalid", f"{label} must be lowercase SHA-256")
    return value


def _required_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryIntentError("delivery_intent_text_invalid", f"{label} is required")
    return value.strip()


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise DeliveryIntentError("delivery_intent_payload_invalid", str(exc)) from exc


def _assert_public_value(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    field_name: str | None = None,
) -> None:
    if depth > 16:
        raise DeliveryIntentError("delivery_intent_payload_invalid", "payload nesting is too deep")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or any(
                token in normalized for token in ("password", "secret", "credential", "api_key")
            ):
                raise DeliveryIntentError(
                    "delivery_intent_credential_forbidden", f"credential-like field is forbidden: {path}.{key}"
                )
            _assert_public_value(
                child,
                path=f"{path}.{key}",
                depth=depth + 1,
                field_name=normalized,
            )
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_public_value(
                child,
                path=f"{path}[{index}]",
                depth=depth + 1,
                field_name=field_name,
            )
        return
    if isinstance(value, str):
        stripped = value.strip()
        normalized_field = field_name or ""
        path_field = normalized_field in _PATH_FIELD_NAMES or normalized_field.endswith(
            ("_path", "_file", "_directory", "_dir", "_root")
        )
        lowered = stripped.lower().replace("\\", "/")
        posix_root = lowered.rstrip("/") in _KNOWN_POSIX_ROOTS or any(
            lowered.startswith(f"{root}/") for root in _KNOWN_POSIX_ROOTS
        )
        windows_absolute = re.match(r"^[A-Za-z]:[\\/]", stripped) is not None
        unc_absolute = stripped.startswith(("//", "\\\\"))
        root_relative_path = stripped.startswith(("/", "\\")) and path_field
        if windows_absolute or unc_absolute or posix_root or root_relative_path:
            raise DeliveryIntentError(
                "delivery_intent_absolute_path_forbidden", f"absolute path is forbidden: {path}"
            )
        if stripped.lower().startswith(("bearer ", "sk-", "-----begin private key")):
            raise DeliveryIntentError(
                "delivery_intent_credential_forbidden", f"credential-like value is forbidden: {path}"
            )


__all__ = [
    "DeliveryIntentError",
    "build_file_delivery_intent",
    "delivery_intent_receipt_binding",
    "materialize_delivery_job",
    "validate_delivery_intent",
    "validate_file_delivery_profile",
]
