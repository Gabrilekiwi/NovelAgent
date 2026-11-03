from __future__ import annotations

import copy
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.engine.persistence import (
    PersistenceLockError,
    atomic_create_json,
    atomic_write_json,
    persistence_run_lock,
)
from core.engine.project_output import project_output_lock
from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AutonomyContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(required_text("timestamp", value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyContractError("autonomy_timestamp_invalid", f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise AutonomyContractError("autonomy_timestamp_invalid", "timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def validate_mapping(value: Any, schema_name: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AutonomyContractError("autonomy_contract_invalid", f"{label} must be an object")
    try:
        return validate_schema(copy.deepcopy(dict(value)), schema_name)
    except SchemaValidationError as exc:
        raise AutonomyContractError(
            "autonomy_contract_invalid", f"invalid {label}: {exc}"
        ) from exc


def safe_id(label: str, value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise AutonomyContractError(
            "autonomy_identifier_invalid",
            f"{label} must use only letters, digits, dot, underscore, colon, or hyphen",
        )
    return value


def required_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutonomyContractError("autonomy_text_invalid", f"{label} is required")
    return value.strip()


def sha256_digest(label: str, value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AutonomyContractError(
            "autonomy_digest_invalid", f"{label} must be a lowercase SHA-256 digest"
        )
    return value


def positive_int(label: str, value: Any, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AutonomyContractError(
            "autonomy_integer_invalid", f"{label} must be an integer >= {minimum}"
        )
    return value


def canonical_hash(value: Any, *, exclude_fields: tuple[str, ...] = ()) -> str:
    return canonical_json_hash(value, exclude_fields=exclude_fields)


def load_json_object(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AutonomyContractError(
            "autonomy_artifact_unreadable", f"could not read {resolved}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AutonomyContractError(
            "autonomy_contract_invalid", f"{resolved} must contain a JSON object"
        )
    return payload


def atomic_append_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Atomically create an append-only JSON artifact.

    ``atomic_create_json`` stages, fsyncs, and publishes without replacement.
    An identical replay is idempotent; conflicting bytes are rejected.
    """

    target = Path(path)
    try:
        with project_output_lock(target):
            try:
                atomic_create_json(target, dict(payload))
            except FileExistsError:
                existing = load_json_object(target)
                if existing != dict(payload):
                    raise AutonomyContractError(
                        "autonomy_append_conflict", f"append-only artifact already differs: {target}"
                    )
    except PersistenceLockError as exc:
        raise AutonomyContractError("autonomy_state_locked", "autonomy output is locked") from exc


def atomic_replace_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    try:
        with project_output_lock(path):
            atomic_write_json(Path(path), dict(payload))
    except PersistenceLockError as exc:
        raise AutonomyContractError("autonomy_state_locked", "autonomy output is locked") from exc


@contextmanager
def _local_project_state_lock(root):
    # Acquire the project boundary before persistence_run_lock creates a root.
    with project_output_lock(Path(root) / '.autonomy-writes', lock_output_paths=False):
        with persistence_run_lock(Path(root)):
            yield


@contextmanager
def state_lock(root: str | Path, *state_paths: str | Path):
    # The local root still serializes mutable heads; the outer project lock
    # coordinates the whole operation with a snapshot. ``state_paths`` remain
    # audit labels, not additional local locks.
    del state_paths
    deadline = time.monotonic() + 5.0
    while True:
        manager = _local_project_state_lock(root)
        try:
            manager.__enter__()
            break
        except PersistenceLockError:
            if time.monotonic() >= deadline:
                raise AutonomyContractError(
                    "autonomy_state_locked", "timed out waiting for local autonomy state lock"
                )
            time.sleep(0.01)
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


__all__ = [
    "AutonomyContractError",
    "atomic_append_json",
    "atomic_replace_json",
    "canonical_hash",
    "load_json_object",
    "now_utc",
    "parse_utc",
    "positive_int",
    "required_text",
    "safe_id",
    "sha256_digest",
    "state_lock",
    "validate_mapping",
]
