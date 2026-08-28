from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping

from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


RUNTIME_SCHEMA_VERSION = "1.0"
RUNTIME_MODE_LEGACY = "legacy"
RUNTIME_MODE_SHADOW = "shadow"
RUNTIME_MODE_NODE = "node"
SUPPORTED_RUNTIME_MODES = frozenset({RUNTIME_MODE_LEGACY, RUNTIME_MODE_SHADOW})


class RuntimeContractError(ValueError):
    """Raised when runtime boundary evidence is incomplete or inconsistent."""


def resolve_runtime_mode(value: str | None) -> str:
    mode = str(value or RUNTIME_MODE_LEGACY).strip().casefold()
    if mode == RUNTIME_MODE_NODE:
        raise RuntimeContractError(
            "runtime mode 'node' is reserved for a future execution cutover and is not implemented"
        )
    if mode not in SUPPORTED_RUNTIME_MODES:
        choices = ", ".join(sorted(SUPPORTED_RUNTIME_MODES))
        raise RuntimeContractError(f"runtime mode must be one of: {choices}")
    return mode


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    index: int
    action: str
    requires: tuple[str, ...]
    produces: tuple[str, ...]
    mode: str
    skippable: bool
    failure_policy: str
    contract_digest: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requires"] = list(self.requires)
        payload["produces"] = list(self.produces)
        return payload


@dataclass(frozen=True)
class WorkflowSnapshot:
    schema_version: str
    workflow_digest: str
    nodes: tuple[NodeSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow_digest": self.workflow_digest,
            "nodes": [node.to_dict() for node in self.nodes],
        }


@dataclass(frozen=True)
class NodeAttempt:
    attempt_id: str
    node_id: str
    ordinal: int
    trace_index: int
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    implicit: bool
    skipped: bool
    trace_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeCheckpoint:
    checkpoint_id: str
    attempt_id: str
    node_id: str
    status: str
    trace_digest: str
    resume_capable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecoveryDecision:
    decision: str
    resume_node_id: str | None
    reason: str
    authoritative: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NodeUsage:
    node_id: str
    source: str
    receipt_hashes: tuple[str, ...]
    call_count: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["receipt_hashes"] = list(self.receipt_hashes)
        return payload


def validate_runtime_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError("runtime evidence must be an object")
    payload = dict(value)
    try:
        validate_schema(payload, "runtime_evidence.schema.json")
    except SchemaValidationError as exc:
        raise RuntimeContractError(str(exc)) from exc

    _validate_safe_artifact_ref(payload.get("artifact_ref"))
    _validate_workflow_snapshot(payload["workflow_snapshot"])
    _validate_context_manifest(payload["context_manifest"])
    _validate_attempts_and_checkpoints(
        payload["workflow_snapshot"],
        payload["attempts"],
        payload["checkpoints"],
    )
    _validate_usage(payload["usage"])
    _parse_timestamp(payload["created_at"], "created_at")

    expected_hash = canonical_json_hash(
        payload,
        exclude_fields={"evidence_hash"},
        exclude_environment_fields=False,
    )
    if payload["evidence_hash"] != expected_hash:
        raise RuntimeContractError("runtime evidence hash mismatch")
    return payload


def _validate_workflow_snapshot(snapshot: Mapping[str, Any]) -> None:
    node_ids: set[str] = set()
    indexes: list[int] = []
    for raw_node in snapshot["nodes"]:
        node = dict(raw_node)
        node_id = str(node["node_id"])
        if node_id in node_ids:
            raise RuntimeContractError(f"duplicate runtime node_id: {node_id}")
        node_ids.add(node_id)
        indexes.append(int(node["index"]))
        expected_contract = canonical_json_hash(
            node,
            exclude_fields={"contract_digest"},
            exclude_environment_fields=False,
        )
        if node["contract_digest"] != expected_contract:
            raise RuntimeContractError(f"runtime node contract digest mismatch: {node_id}")
    if indexes != list(range(1, len(indexes) + 1)):
        raise RuntimeContractError("runtime node indexes must be contiguous")
    expected_workflow = canonical_json_hash(
        snapshot,
        exclude_fields={"workflow_digest"},
        exclude_environment_fields=False,
    )
    if snapshot["workflow_digest"] != expected_workflow:
        raise RuntimeContractError("runtime workflow digest mismatch")


def _validate_context_manifest(manifest: Mapping[str, Any]) -> None:
    refs = [str(item["logical_ref"]) for item in manifest["entries"]]
    if len(refs) != len(set(refs)):
        raise RuntimeContractError("runtime context logical_ref values must be unique")
    expected = canonical_json_hash(
        manifest,
        exclude_fields={"manifest_digest"},
        exclude_environment_fields=False,
    )
    if manifest["manifest_digest"] != expected:
        raise RuntimeContractError("runtime context manifest digest mismatch")


def _validate_attempts_and_checkpoints(
    workflow_snapshot: Mapping[str, Any],
    attempts: list[Mapping[str, Any]],
    checkpoints: list[Mapping[str, Any]],
) -> None:
    node_ids = {str(item["node_id"]) for item in workflow_snapshot["nodes"]}
    attempt_ids: set[str] = set()
    for attempt in attempts:
        attempt_id = str(attempt["attempt_id"])
        if attempt_id in attempt_ids:
            raise RuntimeContractError(f"duplicate runtime attempt_id: {attempt_id}")
        attempt_ids.add(attempt_id)
        if str(attempt["node_id"]) not in node_ids:
            raise RuntimeContractError(
                f"runtime attempt references unknown node: {attempt['node_id']}"
            )
        _parse_timestamp(attempt["started_at"], f"attempt {attempt_id} started_at")
        _parse_timestamp(attempt["finished_at"], f"attempt {attempt_id} finished_at")

    checkpoint_ids: set[str] = set()
    checkpoint_attempt_ids: set[str] = set()
    for checkpoint in checkpoints:
        checkpoint_id = str(checkpoint["checkpoint_id"])
        if checkpoint_id in checkpoint_ids:
            raise RuntimeContractError(f"duplicate runtime checkpoint_id: {checkpoint_id}")
        checkpoint_ids.add(checkpoint_id)
        attempt_id = str(checkpoint["attempt_id"])
        if attempt_id not in attempt_ids:
            raise RuntimeContractError(
                f"runtime checkpoint references unknown attempt: {attempt_id}"
            )
        if attempt_id in checkpoint_attempt_ids:
            raise RuntimeContractError(
                f"runtime attempt has more than one checkpoint: {attempt_id}"
            )
        checkpoint_attempt_ids.add(attempt_id)
    if checkpoint_attempt_ids != attempt_ids:
        raise RuntimeContractError("every runtime attempt must have exactly one checkpoint")


def _validate_usage(usage: list[Mapping[str, Any]]) -> None:
    receipt_hashes: set[str] = set()
    for item in usage:
        hashes = [str(value) for value in item["receipt_hashes"]]
        if len(hashes) != len(set(hashes)):
            raise RuntimeContractError(
                f"runtime usage for {item['node_id']} contains duplicate receipts"
            )
        overlap = receipt_hashes.intersection(hashes)
        if overlap:
            raise RuntimeContractError(
                "model-call receipt cannot be charged to more than one runtime node"
            )
        receipt_hashes.update(hashes)
        if int(item["call_count"]) != len(hashes):
            raise RuntimeContractError(
                f"runtime usage call_count mismatch for {item['node_id']}"
            )
        if item["complete"]:
            input_tokens = item["input_tokens"]
            output_tokens = item["output_tokens"]
            total_tokens = item["total_tokens"]
            if None in {input_tokens, output_tokens, total_tokens}:
                raise RuntimeContractError(
                    f"complete runtime usage is missing token counts for {item['node_id']}"
                )
            if int(input_tokens) + int(output_tokens) != int(total_tokens):
                raise RuntimeContractError(
                    f"runtime usage token total mismatch for {item['node_id']}"
                )


def _validate_safe_artifact_ref(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise RuntimeContractError("runtime artifact_ref must be a safe POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeContractError("runtime artifact_ref must be a safe POSIX relative path")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeContractError(f"runtime {field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeContractError(
            f"runtime {field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeContractError(f"runtime {field} must include a timezone")
    return parsed


__all__ = [
    "NodeAttempt",
    "NodeSpec",
    "NodeUsage",
    "RUNTIME_MODE_LEGACY",
    "RUNTIME_MODE_NODE",
    "RUNTIME_MODE_SHADOW",
    "RUNTIME_SCHEMA_VERSION",
    "RecoveryDecision",
    "RuntimeCheckpoint",
    "RuntimeContractError",
    "SUPPORTED_RUNTIME_MODES",
    "WorkflowSnapshot",
    "resolve_runtime_mode",
    "validate_runtime_evidence",
]
