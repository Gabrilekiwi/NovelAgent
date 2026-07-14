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
AUTHORITY_PENDING_RELATIVE_DIR = Path(".novelagent/authority/pending")

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
    return _canonical_json_hash(dict(receipt), exclude_fields=("receipt_sha256",))


def authority_genesis_event_hash(receipt: Mapping[str, Any]) -> str:
    return _canonical_json_hash(
        dict(receipt),
        exclude_fields=("event_hash", "receipt_sha256"),
    )


def authority_genesis_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return _canonical_json_hash(dict(receipt), exclude_fields=("receipt_sha256",))


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


def prepare_event_authority_advance(
    identity: ProjectIdentity,
    *,
    expected_authority_epoch: int,
    expected_head_event_hash: str,
    new_head_event_hash: str,
    writer_contract: int = CURRENT_WRITER_CONTRACT,
) -> ProjectIdentity:
    """Build the next identity value for the same atomic chapter transaction.

    The activation receipt proves the legacy-to-event boundary and therefore
    remains unchanged as the live event head advances.  The authority epoch is
    also stable for ordinary chapter appends; amend/retcon epoch changes use a
    separate audited transition.
    """

    validated = validate_project_identity(identity.to_dict())
    expected_head = _require_sha256("expected_head_event_hash", expected_head_event_hash)
    next_head = _require_sha256("new_head_event_hash", new_head_event_hash)
    assert_authority_writer(
        validated,
        writer_mode=AUTHORITY_MODE_EVENT,
        writer_contract=writer_contract,
        expected_authority_epoch=expected_authority_epoch,
        expected_head_event_hash=expected_head,
    )
    if next_head == expected_head:
        raise AuthorityCASMismatchError("new event head must differ from the current head")
    authority = _json_copy(validated.authority or {})
    authority["head_event_hash"] = next_head
    advanced = replace(validated, authority=authority)
    return validate_project_identity(advanced.to_dict())


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
        _assert_direct_activation_has_no_published_prose(root)

        request = _activation_request_binding(
            head_event_hash=head_event_hash,
            genesis_receipt=genesis,
            canonical_state_sha256=canonical_state_sha256,
            minimum_writer_contract=minimum,
        )
        pending_path = _pending_activation_path(root, expected_sha)
        if pending_path.exists():
            pending = _load_pending_activation(
                pending_path,
                current=current,
                expected_identity_sha256=expected_sha,
                request=request,
            )
            genesis = pending["genesis_receipt"]
            activation = pending["activation_receipt"]
            activated = validate_project_identity(pending["activated_identity"])
        else:
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
            pending = _build_pending_activation(
                current=current,
                expected_identity_sha256=expected_sha,
                request=request,
                genesis_receipt=genesis,
                activation_receipt=activation,
                activated_identity=activated,
            )
            try:
                atomic_create_json(pending_path, pending)
            except FileExistsError:
                pending = _load_pending_activation(
                    pending_path,
                    current=current,
                    expected_identity_sha256=expected_sha,
                    request=request,
                )
                genesis = pending["genesis_receipt"]
                activation = pending["activation_receipt"]
                activated = validate_project_identity(pending["activated_identity"])
            except OSError as exc:
                raise AuthorityError(
                    f"could not publish activation recovery intent: {exc}",
                    code="authority_activation_intent_publish_failed",
                ) from exc

        # Receipts cross the irreversible authority boundary. They are durable
        # before ProjectIdentity changes, and legacy identity readers fail
        # closed while a receipt-first activation is awaiting recovery.
        if genesis is not None:
            _publish_immutable_receipt(root, genesis, validator=validate_authority_genesis_receipt)
        _publish_immutable_receipt(
            root,
            activation,
            validator=validate_authority_activation_receipt,
        )

        # Recheck immediately before replacing the identity.  Writers using
        # this contract share the state-path lock; the second check also fails
        # closed if a non-cooperating process changed the file meanwhile.
        latest = _read_identity_bytes(identity_file)
        latest_sha = hashlib.sha256(latest).hexdigest()
        if latest_sha != expected_sha:
            raise AuthorityCASMismatchError(
                f"ProjectIdentity changed during activation: expected {expected_sha}, actual {latest_sha}"
            )
        _assert_direct_activation_has_no_published_prose(root)
        try:
            atomic_write_json(identity_file, activated.to_dict())
        except OSError as exc:
            raise AuthorityError(
                f"event receipts are durable but ProjectIdentity publish failed: {exc}",
                code="authority_identity_publish_failed",
            ) from exc
        try:
            pending_path.unlink(missing_ok=True)
        except OSError:
            # A stale intent is harmless once v2 identity and both immutable
            # proofs are durable; readers validate the embedded receipt.
            pass
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


def assert_no_event_receipt_for_legacy_identity(
    story_project_root: str | Path,
    identity: ProjectIdentity,
) -> None:
    """Fail closed when receipt-first activation crossed its boundary.

    ``activate_event_authority`` may crash after publishing an immutable event
    receipt but before replacing ProjectIdentity.  Such a legacy identity is a
    recoverable intermediate state, never permission to resume a v1 writer.
    """

    authority = identity.authority or dict(LEGACY_AUTHORITY_PROJECTION)
    if authority.get("mode") != AUTHORITY_MODE_LEGACY:
        return
    receipt_dir = Path(story_project_root) / AUTHORITY_RECEIPTS_RELATIVE_DIR
    if not receipt_dir.exists():
        return
    try:
        entries = sorted(receipt_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise AuthorityWriterContractError(
            f"cannot prove that the legacy authority has no event receipts: {exc}",
            code="legacy_writer_forbidden_after_event_receipt",
        ) from exc
    for path in entries:
        if not path.is_file() or path.suffix.lower() != ".json":
            raise AuthorityWriterContractError(
                "authority receipt storage is ambiguous while identity is legacy",
                code="legacy_writer_forbidden_after_event_receipt",
            )
        try:
            payload = _load_json(path)
            receipt_type = payload.get("receipt_type") if isinstance(payload, dict) else None
            if receipt_type == "authority_activation":
                receipt = validate_authority_activation_receipt(payload)
            elif receipt_type == "authority_genesis":
                receipt = validate_authority_genesis_receipt(payload)
            else:
                raise AuthorityError("unknown authority receipt type")
            if receipt["book_id"] != identity.book_id:
                raise AuthorityError("authority receipt belongs to another book")
        except Exception as exc:
            raise AuthorityWriterContractError(
                f"legacy authority is contaminated by an unverifiable event receipt: {exc}",
                code="legacy_writer_forbidden_after_event_receipt",
            ) from exc
        raise AuthorityWriterContractError(
            "an immutable event receipt exists; recover activation before any legacy write",
            code="legacy_writer_forbidden_after_event_receipt",
        )


def _pending_activation_path(root: Path, expected_identity_sha256: str) -> Path:
    return root / AUTHORITY_PENDING_RELATIVE_DIR / f"{expected_identity_sha256}.json"


def _activation_request_binding(
    *,
    head_event_hash: str | None,
    genesis_receipt: Mapping[str, Any] | None,
    canonical_state_sha256: str | None,
    minimum_writer_contract: int,
) -> dict[str, Any]:
    if canonical_state_sha256 is not None:
        mode = "canonical_state"
        source_digest = _require_sha256("canonical_state_sha256", canonical_state_sha256)
    elif genesis_receipt is not None:
        validated = validate_authority_genesis_receipt(dict(genesis_receipt))
        mode = "genesis_receipt"
        source_digest = validated["receipt_sha256"]
    else:
        mode = "existing_head"
        source_digest = _require_sha256("head_event_hash", head_event_hash)
    return {
        "mode": mode,
        "source_digest": source_digest,
        "requested_head_event_hash": (
            _require_sha256("head_event_hash", head_event_hash)
            if head_event_hash is not None
            else None
        ),
        "minimum_writer_contract": minimum_writer_contract,
    }


def _build_pending_activation(
    *,
    current: ProjectIdentity,
    expected_identity_sha256: str,
    request: Mapping[str, Any],
    genesis_receipt: Mapping[str, Any] | None,
    activation_receipt: Mapping[str, Any],
    activated_identity: ProjectIdentity,
) -> dict[str, Any]:
    pending: dict[str, Any] = {
        "schema_version": "1.0",
        "book_id": current.book_id,
        "expected_identity_sha256": expected_identity_sha256,
        "request": _json_copy(dict(request)),
        "genesis_receipt": (
            _json_copy(dict(genesis_receipt)) if genesis_receipt is not None else None
        ),
        "activation_receipt": _json_copy(dict(activation_receipt)),
        "activated_identity": activated_identity.to_dict(),
        "intent_hash": "",
    }
    pending["intent_hash"] = _canonical_json_hash(pending, exclude_fields=("intent_hash",))
    return _validate_pending_activation(
        pending,
        current=current,
        expected_identity_sha256=expected_identity_sha256,
        request=request,
    )


def _load_pending_activation(
    path: Path,
    *,
    current: ProjectIdentity,
    expected_identity_sha256: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityError(
            f"cannot recover pending authority activation: {exc}",
            code="authority_activation_recovery_failed",
        ) from exc
    return _validate_pending_activation(
        payload,
        current=current,
        expected_identity_sha256=expected_identity_sha256,
        request=request,
    )


def _validate_pending_activation(
    value: Any,
    *,
    current: ProjectIdentity,
    expected_identity_sha256: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthorityError(
            "pending authority activation must be an object",
            code="authority_activation_recovery_failed",
        )
    expected_fields = {
        "schema_version",
        "book_id",
        "expected_identity_sha256",
        "request",
        "genesis_receipt",
        "activation_receipt",
        "activated_identity",
        "intent_hash",
    }
    if set(value) != expected_fields or value.get("schema_version") != "1.0":
        raise AuthorityError(
            "pending authority activation fields are invalid",
            code="authority_activation_recovery_failed",
        )
    pending = _json_copy(value)
    if pending["intent_hash"] != _canonical_json_hash(pending, exclude_fields=("intent_hash",)):
        raise AuthorityError(
            "pending authority activation hash mismatch",
            code="authority_activation_recovery_failed",
        )
    if (
        pending["book_id"] != current.book_id
        or pending["expected_identity_sha256"] != expected_identity_sha256
        or pending["request"] != dict(request)
    ):
        raise AuthorityError(
            "pending authority activation differs from this request",
            code="authority_activation_recovery_mismatch",
        )
    genesis = pending["genesis_receipt"]
    if genesis is not None:
        genesis = validate_authority_genesis_receipt(genesis)
    activation = validate_authority_activation_receipt(pending["activation_receipt"])
    activated = validate_project_identity(pending["activated_identity"])
    if (
        activation["book_id"] != current.book_id
        or activation["expected_identity_sha256"] != expected_identity_sha256
        or activation["head_event_hash"] != (activated.authority or {}).get("head_event_hash")
        or (activated.authority or {}).get("activation_receipt") != activation
    ):
        raise AuthorityError(
            "pending authority activation proof is internally inconsistent",
            code="authority_activation_recovery_failed",
        )
    if genesis is None:
        if activation["genesis_receipt_sha256"] is not None:
            raise AuthorityError(
                "pending activation lost its genesis receipt",
                code="authority_activation_recovery_failed",
            )
    elif (
        activation["genesis_receipt_sha256"] != genesis["receipt_sha256"]
        or activation["head_event_hash"] != genesis["event_hash"]
    ):
        raise AuthorityError(
            "pending genesis does not match activation",
            code="authority_activation_recovery_failed",
        )
    pending["genesis_receipt"] = genesis
    pending["activation_receipt"] = activation
    pending["activated_identity"] = activated.to_dict()
    return pending


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


def _assert_direct_activation_has_no_published_prose(root: Path) -> None:
    """Keep direct activation limited to genuinely empty/new books.

    Existing published prose requires the approved migration service, which
    binds frozen source bytes, a source_sync baseline, receipts, and identity in
    one Persistence V2 transaction.  Accepting an arbitrary caller-provided
    head here would bypass that evidence and approval boundary.
    """

    from core.story_project.paths import PROSE_DIR_NAME

    prose_root = root / PROSE_DIR_NAME
    if not prose_root.exists():
        return
    if prose_root.is_symlink() or not prose_root.is_dir():
        raise AuthorityError(
            "published prose storage is not a safe ordinary directory",
            code="migration_approval_required_for_existing_book",
        )
    try:
        has_published_prose = any(path.is_file() for path in prose_root.rglob("*"))
    except OSError as exc:
        raise AuthorityError(
            f"cannot prove that published prose is empty: {exc}",
            code="migration_approval_required_for_existing_book",
        ) from exc
    if has_published_prose:
        raise AuthorityError(
            "existing published prose requires an approved atomic migration bootstrap",
            code="migration_approval_required_for_existing_book",
        )


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


def _canonical_json_hash(value: Any, *, exclude_fields: tuple[str, ...] = ()) -> str:
    """Hash public authority JSON without importing the Memory package.

    Authority is imported by Memory's history-revision boundary, so depending
    on ``core.memory_v2`` here would make the foundational identity validator
    circular.  This is the same canonical JSON v1 normalization for the public
    payload shapes used by authority receipts.
    """

    excluded = frozenset(exclude_fields)
    environment_fields = frozenset(
        {"absolute_path", "file_path", "mtime", "mtime_ns", "storage_path"}
    )

    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): normalize(child)
                for key, child in item.items()
                if str(key) not in excluded and str(key) not in environment_fields
            }
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    try:
        content = json.dumps(
            normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityError(f"authority payload is not canonical JSON: {exc}") from exc
    return hashlib.sha256(content).hexdigest()


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
    "AUTHORITY_PENDING_RELATIVE_DIR",
    "AUTHORITY_RECEIPTS_RELATIVE_DIR",
    "CURRENT_WRITER_CONTRACT",
    "AuthorityCASMismatchError",
    "AuthorityError",
    "AuthorityWriterContractError",
    "activate_event_authority",
    "assert_authority_writer",
    "assert_no_event_receipt_for_legacy_identity",
    "authority_activation_receipt_sha256",
    "authority_genesis_event_hash",
    "authority_genesis_receipt_sha256",
    "authority_receipt_path",
    "build_authority_activation_receipt",
    "build_authority_genesis_receipt",
    "project_identity_sha256",
    "prepare_event_authority_advance",
    "validate_authority_activation_receipt",
    "validate_authority_config",
    "validate_authority_genesis_receipt",
    "validate_persisted_authority_receipts",
]
