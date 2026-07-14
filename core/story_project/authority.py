from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from core.engine.persistence import (
    atomic_create_json,
    atomic_write_json,
    persistence_run_lock,
)
from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema
from core.story_project.identity import (
    LEGACY_AUTHORITY_PROJECTION,
    PROJECT_IDENTITY_V2_SCHEMA_VERSION,
    ProjectIdentity,
    ProjectIdentityError,
    project_identity_path,
    validate_project_identity,
)


AUTHORITY_ACTIVATION_RECEIPT_SCHEMA_VERSION = "1.0"
AUTHORITY_GENESIS_RECEIPT_SCHEMA_VERSION = "1.0"
AUTHORITY_MODE_LEGACY = "legacy_markdown_v1"
AUTHORITY_MODE_EVENT = "event_v1"
CURRENT_WRITER_CONTRACT = 1
AUTHORITY_RECEIPTS_RELATIVE_DIR = Path(".novelagent/authority/receipts")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2})")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "environment",
        "password",
        "secret",
        "token",
    }
)
_SECRET_VALUE_PREFIXES = ("bearer ", "sk-", "-----begin private key")


class AuthorityError(ProjectIdentityError):
    code = "project_authority_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class AuthorityCASMismatchError(AuthorityError):
    code = "project_authority_identity_cas_mismatch"


class AuthorityWriterContractError(AuthorityError):
    code = "project_authority_writer_contract_blocked"


def authority_receipt_path(story_project_root: str | Path, receipt_sha256: str) -> Path:
    digest = _require_sha256("receipt_sha256", receipt_sha256)
    return Path(story_project_root) / AUTHORITY_RECEIPTS_RELATIVE_DIR / f"{digest}.json"


def project_identity_sha256(story_project_root: str | Path) -> str:
    path = project_identity_path(story_project_root)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise AuthorityError(f"cannot read ProjectIdentity for hashing: {exc}") from exc
    return hashlib.sha256(content).hexdigest()


def authority_activation_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return canonical_json_hash(dict(receipt), exclude_fields=("receipt_sha256",))


def authority_genesis_event_hash(receipt: Mapping[str, Any]) -> str:
    return canonical_json_hash(
        dict(receipt),
        exclude_fields=("event_hash", "receipt_sha256"),
    )


def authority_genesis_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return canonical_json_hash(dict(receipt), exclude_fields=("receipt_sha256",))


def build_authority_genesis_receipt(
    *,
    book_id: str,
    canonical_state_sha256: str,
    minimum_writer_contract: int = CURRENT_WRITER_CONTRACT,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build an empty-state authority genesis without any shadow-chapter gate."""

    _require_writer_contract_number(minimum_writer_contract)
    receipt: dict[str, Any] = {
        "schema_version": AUTHORITY_GENESIS_RECEIPT_SCHEMA_VERSION,
        "receipt_type": "authority_genesis",
        "book_id": _required_safe_text("book_id", book_id),
        "authority_epoch": 1,
        "previous_event_hash": None,
        "canonical_state_sha256": _require_sha256(
            "canonical_state_sha256", canonical_state_sha256
        ),
        "minimum_writer_contract": minimum_writer_contract,
        "created_at": _utc_timestamp(now),
        "event_hash": "",
        "receipt_sha256": "",
    }
    receipt["event_hash"] = authority_genesis_event_hash(receipt)
    receipt["receipt_sha256"] = authority_genesis_receipt_sha256(receipt)
    return validate_authority_genesis_receipt(receipt)


def validate_authority_genesis_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthorityError("authority genesis receipt must be a JSON object")
    try:
        validated = validate_schema(value, "authority_genesis_receipt.schema.json")
    except SchemaValidationError as exc:
        raise AuthorityError(str(exc), code="authority_genesis_receipt_invalid") from exc
    receipt = _json_copy(validated)
    _assert_safe_public_value(receipt)
    for field in ("canonical_state_sha256", "event_hash", "receipt_sha256"):
        _require_sha256(field, receipt[field])
    _require_writer_contract_number(receipt["minimum_writer_contract"])
    if receipt["event_hash"] != authority_genesis_event_hash(receipt):
        raise AuthorityError(
            "genesis event_hash does not match its canonical payload",
            code="authority_genesis_event_hash_mismatch",
        )
    if receipt["receipt_sha256"] != authority_genesis_receipt_sha256(receipt):
        raise AuthorityError(
            "genesis receipt_sha256 does not match its canonical payload",
            code="authority_genesis_receipt_hash_mismatch",
        )
    return receipt


def build_authority_activation_receipt(
    *,
    book_id: str,
    expected_identity_sha256: str,
    head_event_hash: str,
    authority_epoch: int,
    minimum_writer_contract: int,
    genesis_receipt_sha256: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    if authority_epoch < 1:
        raise AuthorityError("event authority epoch must be at least 1")
    _require_writer_contract_number(minimum_writer_contract)
    receipt: dict[str, Any] = {
        "schema_version": AUTHORITY_ACTIVATION_RECEIPT_SCHEMA_VERSION,
        "receipt_type": "authority_activation",
        "book_id": _required_safe_text("book_id", book_id),
        "from_mode": AUTHORITY_MODE_LEGACY,
        "to_mode": AUTHORITY_MODE_EVENT,
        "authority_epoch": authority_epoch,
        "expected_identity_sha256": _require_sha256(
            "expected_identity_sha256", expected_identity_sha256
        ),
        "head_event_hash": _require_sha256("head_event_hash", head_event_hash),
        "genesis_receipt_sha256": (
            _require_sha256("genesis_receipt_sha256", genesis_receipt_sha256)
            if genesis_receipt_sha256 is not None
            else None
        ),
        "minimum_writer_contract": minimum_writer_contract,
        "activated_at": _utc_timestamp(now),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = authority_activation_receipt_sha256(receipt)
    return validate_authority_activation_receipt(receipt)


def validate_authority_activation_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthorityError("authority activation receipt must be a JSON object")
    try:
        validated = validate_schema(value, "authority_activation_receipt.schema.json")
    except SchemaValidationError as exc:
        raise AuthorityError(str(exc), code="authority_activation_receipt_invalid") from exc
    receipt = _json_copy(validated)
    _assert_safe_public_value(receipt)
    for field in ("expected_identity_sha256", "head_event_hash", "receipt_sha256"):
        _require_sha256(field, receipt[field])
    if receipt["genesis_receipt_sha256"] is not None:
        _require_sha256("genesis_receipt_sha256", receipt["genesis_receipt_sha256"])
    _require_writer_contract_number(receipt["minimum_writer_contract"])
    if receipt["receipt_sha256"] != authority_activation_receipt_sha256(receipt):
        raise AuthorityError(
            "activation receipt_sha256 does not match its canonical payload",
            code="authority_activation_receipt_hash_mismatch",
        )
    return receipt


def validate_authority_config(
    value: Any,
    *,
    book_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthorityError("ProjectIdentity 2.0 authority must be an object")
    expected_fields = {
        "mode",
        "authority_epoch",
        "head_event_hash",
        "activation_receipt",
        "minimum_writer_contract",
    }
    if set(value) != expected_fields:
        missing = sorted(expected_fields - set(value))
        extra = sorted(set(value) - expected_fields)
        raise AuthorityError(f"authority fields mismatch; missing={missing} extra={extra}")
    authority = _json_copy(value)
    _assert_safe_public_value(authority)
    mode = authority["mode"]
    epoch = authority["authority_epoch"]
    minimum = authority["minimum_writer_contract"]
    if mode not in {AUTHORITY_MODE_LEGACY, AUTHORITY_MODE_EVENT}:
        raise AuthorityError(f"unsupported authority mode: {mode!r}")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise AuthorityError("authority_epoch must be a non-negative integer")
    _require_writer_contract_number(minimum)

    if mode == AUTHORITY_MODE_LEGACY:
        if epoch != 0 or authority["head_event_hash"] is not None or authority["activation_receipt"] is not None:
            raise AuthorityError(
                "legacy authority requires epoch 0 with no event head or activation receipt"
            )
        return authority

    if epoch < 1:
        raise AuthorityError("event authority requires authority_epoch >= 1")
    head = _require_sha256("head_event_hash", authority["head_event_hash"])
    receipt = validate_authority_activation_receipt(authority["activation_receipt"])
    if book_id is not None and receipt["book_id"] != book_id:
        raise AuthorityError("activation receipt book_id does not match ProjectIdentity")
    if receipt["authority_epoch"] != epoch:
        raise AuthorityError("activation receipt authority_epoch does not match ProjectIdentity")
    if receipt["minimum_writer_contract"] != minimum:
        raise AuthorityError(
            "activation receipt minimum_writer_contract does not match ProjectIdentity"
        )
    authority["activation_receipt"] = receipt
    return authority


def assert_authority_writer(
    identity: ProjectIdentity,
    *,
    writer_mode: str,
    writer_contract: int,
    expected_authority_epoch: int | None = None,
    expected_head_event_hash: str | None = None,
) -> None:
    """Fail closed before any writer operates against the authority head."""

    validated = validate_project_identity(identity.to_dict())
    authority = validated.authority or dict(LEGACY_AUTHORITY_PROJECTION)
    contract = _require_writer_contract_number(writer_contract)
    minimum = int(authority["minimum_writer_contract"])
    if contract < minimum:
        raise AuthorityWriterContractError(
            f"writer contract {contract} is below required contract {minimum}"
        )
    if writer_mode != authority["mode"]:
        code = (
            "legacy_writer_forbidden_after_event_activation"
            if authority["mode"] == AUTHORITY_MODE_EVENT and writer_mode == AUTHORITY_MODE_LEGACY
            else "project_authority_writer_mode_mismatch"
        )
        raise AuthorityWriterContractError(
            f"writer mode {writer_mode!r} cannot write authority mode {authority['mode']!r}",
            code=code,
        )
    if expected_authority_epoch is not None and authority["authority_epoch"] != expected_authority_epoch:
        raise AuthorityCASMismatchError(
            f"authority epoch changed: expected {expected_authority_epoch}, "
            f"actual {authority['authority_epoch']}"
        )
    if expected_head_event_hash is not None:
        expected_head = _require_sha256("expected_head_event_hash", expected_head_event_hash)
        if authority["head_event_hash"] != expected_head:
            raise AuthorityCASMismatchError(
                "event authority head changed: "
                f"expected {expected_head}, actual {authority['head_event_hash']}"
            )


def activate_event_authority(
    story_project_root: str | Path,
    *,
    expected_identity_sha256: str,
    head_event_hash: str | None = None,
    genesis_receipt: Mapping[str, Any] | None = None,
    canonical_state_sha256: str | None = None,
    minimum_writer_contract: int = CURRENT_WRITER_CONTRACT,
    writer_contract: int = CURRENT_WRITER_CONTRACT,
    now: Callable[[], datetime] | None = None,
) -> ProjectIdentity:
    """CAS-activate event authority and durably publish its immutable receipts.

    For a new, empty book pass ``canonical_state_sha256``; the function builds
    and publishes a genesis receipt.  Existing books pass the already-proven
    ``head_event_hash``.  No chapter-count or shadow-history condition exists.
    """

    root = Path(story_project_root).resolve()
    if not root.is_dir():
        raise AuthorityError(f"StoryProject root is not a directory: {root}")
    expected_sha = _require_sha256("expected_identity_sha256", expected_identity_sha256)
    minimum = _require_writer_contract_number(minimum_writer_contract)
    caller_contract = _require_writer_contract_number(writer_contract)
    if caller_contract < minimum:
        raise AuthorityWriterContractError(
            f"writer contract {caller_contract} is below requested minimum {minimum}"
        )
    if genesis_receipt is not None and canonical_state_sha256 is not None:
        raise AuthorityError("pass genesis_receipt or canonical_state_sha256, not both")
    if genesis_receipt is not None:
        genesis = validate_authority_genesis_receipt(dict(genesis_receipt))
    elif canonical_state_sha256 is not None:
        # The book id is read under the CAS lock below, so construction is
        # intentionally deferred.
        genesis = None
    else:
        genesis = None
    if head_event_hash is None and genesis_receipt is None and canonical_state_sha256 is None:
        raise AuthorityError(
            "activation requires an existing head_event_hash or an empty-state genesis"
        )

    identity_file = project_identity_path(root)
    lock_dir = root / ".novelagent" / "authority" / "cas"
    with persistence_run_lock(lock_dir, state_paths=(identity_file,)):
        before = _read_identity_bytes(identity_file)
        actual_sha = hashlib.sha256(before).hexdigest()
        if actual_sha != expected_sha:
            raise AuthorityCASMismatchError(
                f"ProjectIdentity changed: expected {expected_sha}, actual {actual_sha}"
            )
        current = _identity_from_bytes(before)
        if current.ephemeral:
            raise AuthorityError("ephemeral ProjectIdentity cannot activate event authority")
        current_authority = current.authority or dict(LEGACY_AUTHORITY_PROJECTION)
        if current_authority["mode"] == AUTHORITY_MODE_EVENT:
            raise AuthorityWriterContractError(
                "event authority is already active and cannot return to a legacy writer",
                code="legacy_writer_forbidden_after_event_activation",
            )
        if current_authority != LEGACY_AUTHORITY_PROJECTION:
            raise AuthorityError("legacy authority projection is not the supported epoch-0 state")

        if canonical_state_sha256 is not None:
            genesis = build_authority_genesis_receipt(
                book_id=current.book_id,
                canonical_state_sha256=canonical_state_sha256,
                minimum_writer_contract=minimum,
                now=now,
            )
        if genesis is not None:
            if genesis["book_id"] != current.book_id:
                raise AuthorityError("genesis receipt belongs to another ProjectIdentity")
            if genesis["minimum_writer_contract"] != minimum:
                raise AuthorityError("genesis writer contract does not match activation")
            derived_head = genesis["event_hash"]
            if head_event_hash is not None and _require_sha256("head_event_hash", head_event_hash) != derived_head:
                raise AuthorityError("head_event_hash does not match genesis event_hash")
            head = derived_head
            genesis_sha = genesis["receipt_sha256"]
        else:
            head = _require_sha256("head_event_hash", head_event_hash)
            genesis_sha = None

        epoch = int(current_authority["authority_epoch"]) + 1
        activation = build_authority_activation_receipt(
            book_id=current.book_id,
            expected_identity_sha256=actual_sha,
            head_event_hash=head,
            authority_epoch=epoch,
            minimum_writer_contract=minimum,
            genesis_receipt_sha256=genesis_sha,
            now=now,
        )
        activated = replace(
            current,
            schema_version=PROJECT_IDENTITY_V2_SCHEMA_VERSION,
            root_hint=".",
            authority={
                "mode": AUTHORITY_MODE_EVENT,
                "authority_epoch": epoch,
                "head_event_hash": head,
                "activation_receipt": activation,
                "minimum_writer_contract": minimum,
            },
        )
        activated = validate_project_identity(activated.to_dict())

        # Recheck immediately before replacing the identity.  Writers using
        # this contract share the state-path lock; the second check also fails
        # closed if a non-cooperating process changed the file meanwhile.
        latest = _read_identity_bytes(identity_file)
        latest_sha = hashlib.sha256(latest).hexdigest()
        if latest_sha != expected_sha:
            raise AuthorityCASMismatchError(
                f"ProjectIdentity changed during activation: expected {expected_sha}, actual {latest_sha}"
            )
        atomic_write_json(identity_file, activated.to_dict())
        # The identity is switched first and embeds the hashed activation
        # receipt.  If publishing either external no-clobber receipt fails,
        # readers see v2 and fail closed on the missing proof; there is never a
        # durable receipt alongside an identity that still authorizes v1.
        if genesis is not None:
            _publish_immutable_receipt(root, genesis, validator=validate_authority_genesis_receipt)
        _publish_immutable_receipt(
            root,
            activation,
            validator=validate_authority_activation_receipt,
        )
        return activated


def validate_persisted_authority_receipts(
    story_project_root: str | Path,
    identity: ProjectIdentity,
) -> None:
    authority = identity.authority or dict(LEGACY_AUTHORITY_PROJECTION)
    if authority["mode"] != AUTHORITY_MODE_EVENT:
        return
    activation = validate_authority_activation_receipt(authority["activation_receipt"])
    persisted_activation = _load_immutable_receipt(
        story_project_root,
        activation["receipt_sha256"],
        validator=validate_authority_activation_receipt,
    )
    if persisted_activation != activation:
        raise AuthorityError("persisted activation receipt differs from ProjectIdentity")
    genesis_sha = activation["genesis_receipt_sha256"]
    if genesis_sha is not None:
        genesis = _load_immutable_receipt(
            story_project_root,
            genesis_sha,
            validator=validate_authority_genesis_receipt,
        )
        if genesis["book_id"] != identity.book_id:
            raise AuthorityError("persisted genesis receipt belongs to another ProjectIdentity")
        if genesis["event_hash"] != activation["head_event_hash"]:
            raise AuthorityError("persisted genesis event does not match activation head")


def _publish_immutable_receipt(
    root: Path,
    receipt: Mapping[str, Any],
    *,
    validator: Callable[[Any], dict[str, Any]],
) -> None:
    validated = validator(dict(receipt))
    path = authority_receipt_path(root, validated["receipt_sha256"])
    try:
        atomic_create_json(path, validated)
    except FileExistsError:
        existing = _load_json(path)
        if validator(existing) != validated:
            raise AuthorityError(f"immutable authority receipt conflicts at {path.name}")
    except OSError as exc:
        raise AuthorityError(
            f"could not publish immutable authority receipt {path.name}: {exc}",
            code="authority_receipt_publish_failed",
        ) from exc


def _load_immutable_receipt(
    root: str | Path,
    receipt_sha256: str,
    *,
    validator: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    path = authority_receipt_path(root, receipt_sha256)
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"cannot read immutable authority receipt {path.name}: {exc}") from exc
    validated = validator(payload)
    if validated["receipt_sha256"] != receipt_sha256:
        raise AuthorityError("authority receipt filename does not match receipt hash")
    return validated


def _identity_from_bytes(content: bytes) -> ProjectIdentity:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"ProjectIdentity is not valid UTF-8 JSON: {exc}") from exc
    return validate_project_identity(payload)


def _read_identity_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AuthorityError(f"cannot read ProjectIdentity: {exc}") from exc


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _require_sha256(field: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AuthorityError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_writer_contract_number(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AuthorityWriterContractError("writer contract must be a positive integer")
    return value


def _required_safe_text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityError(f"{field} must be non-empty text")
    _assert_safe_public_value(value, field=field)
    return value


def _assert_safe_public_value(value: Any, *, field: str = "$", depth: int = 0) -> None:
    if depth > 12:
        raise AuthorityError("authority public payload nesting is too deep")
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS or any(part in normalized for part in ("password", "secret", "credential", "api_key")):
                raise AuthorityError(f"unsafe credential field is forbidden: {field}.{key}")
            _assert_safe_public_value(child, field=f"{field}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_safe_public_value(child, field=f"{field}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        lowered = value.strip().lower()
        if _ABSOLUTE_PATH_RE.match(value.strip()):
            raise AuthorityError(f"absolute path is forbidden in authority public payload: {field}")
        if lowered.startswith(_SECRET_VALUE_PREFIXES):
            raise AuthorityError(f"credential-like value is forbidden in authority public payload: {field}")


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise AuthorityError(f"authority payload is not canonical JSON: {exc}") from exc


def _utc_timestamp(now: Callable[[], datetime] | None) -> str:
    value = now() if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "AUTHORITY_ACTIVATION_RECEIPT_SCHEMA_VERSION",
    "AUTHORITY_GENESIS_RECEIPT_SCHEMA_VERSION",
    "AUTHORITY_MODE_EVENT",
    "AUTHORITY_MODE_LEGACY",
    "AUTHORITY_RECEIPTS_RELATIVE_DIR",
    "CURRENT_WRITER_CONTRACT",
    "AuthorityCASMismatchError",
    "AuthorityError",
    "AuthorityWriterContractError",
    "activate_event_authority",
    "assert_authority_writer",
    "authority_activation_receipt_sha256",
    "authority_genesis_event_hash",
    "authority_genesis_receipt_sha256",
    "authority_receipt_path",
    "build_authority_activation_receipt",
    "build_authority_genesis_receipt",
    "project_identity_sha256",
    "validate_authority_activation_receipt",
    "validate_authority_config",
    "validate_authority_genesis_receipt",
    "validate_persisted_authority_receipts",
]
