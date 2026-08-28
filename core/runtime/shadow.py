from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from core.context_budget import conservative_token_estimate
from core.engine.workflow import ACTION_METADATA
from core.memory_v2.canonical import canonical_json_bytes, canonical_json_hash
from core.runtime.contracts import (
    NodeAttempt,
    NodeSpec,
    NodeUsage,
    RUNTIME_MODE_SHADOW,
    RUNTIME_SCHEMA_VERSION,
    RecoveryDecision,
    RuntimeCheckpoint,
    WorkflowSnapshot,
    validate_runtime_evidence,
)


_STAGE_TO_NODE = {
    "chapter_generation": "generate_chapter",
    "draft": "generate_chapter",
    "scene": "generate_chapter",
    "scene_plan": "generate_chapter",
    "claude_polish": "polish",
    "polish": "polish",
    "llm_validation": "validate",
    "validator": "validate",
    "repair": "repair_if_needed",
    "scene_repair": "repair_if_needed",
}
_INPUT_CONTEXTS = frozenset({"snapshot", "input_pack", "workflow_plan"})


def build_shadow_runtime_evidence(
    run: Mapping[str, Any],
    *,
    context_inputs: Mapping[str, Any] | None = None,
    model_call_receipts: Sequence[Mapping[str, Any]] = (),
    artifact_ref: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    run_id = str(run.get("id") or "").strip()
    if not run_id:
        raise ValueError("shadow runtime requires a run id")

    workflow_snapshot = _build_workflow_snapshot(
        run.get("workflow_plan"),
        run.get("trace"),
    )
    attempts = _build_attempts(
        run_id,
        run.get("trace"),
        workflow_snapshot,
        default_started_at=str(run.get("started_at") or ""),
        default_finished_at=str(run.get("finished_at") or ""),
    )
    checkpoints = tuple(
        RuntimeCheckpoint(
            checkpoint_id=f"{attempt.attempt_id}:checkpoint",
            attempt_id=attempt.attempt_id,
            node_id=attempt.node_id,
            status=(
                "observed_completed"
                if attempt.status == "completed"
                else "observed_failed"
            ),
            trace_digest=attempt.trace_digest,
            resume_capable=False,
        )
        for attempt in attempts
    )
    recovery = _recovery_decision(run, attempts)
    context_manifest = _build_context_manifest(context_inputs or {})
    usage = _build_node_usage(model_call_receipts)

    evidence = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "mode": RUNTIME_MODE_SHADOW,
        "authority": "legacy_executor",
        "execution_cutover": False,
        "run_id": run_id,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "workflow_snapshot": workflow_snapshot.to_dict(),
        "context_manifest": context_manifest,
        "attempts": [attempt.to_dict() for attempt in attempts],
        "checkpoints": [checkpoint.to_dict() for checkpoint in checkpoints],
        "recovery": recovery.to_dict(),
        "usage": [item.to_dict() for item in usage],
        "artifact_ref": artifact_ref,
    }
    evidence["evidence_hash"] = canonical_json_hash(
        evidence,
        exclude_environment_fields=False,
    )
    return validate_runtime_evidence(evidence)


def _build_workflow_snapshot(
    raw_plan: Any,
    raw_trace: Any,
) -> WorkflowSnapshot:
    raw_steps = raw_plan.get("steps") if isinstance(raw_plan, Mapping) else None
    steps = [dict(item) for item in raw_steps if isinstance(item, Mapping)] if isinstance(raw_steps, list) else []
    known = {str(item.get("action")) for item in steps}
    if isinstance(raw_trace, list):
        for event in raw_trace:
            if not isinstance(event, Mapping):
                continue
            action = str(event.get("action") or "")
            if action in ACTION_METADATA and action not in known:
                steps.append(
                    {
                        "index": len(steps) + 1,
                        "action": action,
                        **ACTION_METADATA[action],
                    }
                )
                known.add(action)

    nodes: list[NodeSpec] = []
    for index, step in enumerate(steps, start=1):
        action = str(step.get("action") or "")
        if action not in ACTION_METADATA:
            continue
        metadata = ACTION_METADATA[action]
        node_payload = {
            "node_id": action,
            "index": index,
            "action": action,
            "requires": [str(value) for value in step.get("requires", metadata["requires"])],
            "produces": [str(value) for value in step.get("produces", metadata["produces"])],
            "mode": str(step.get("mode") or metadata["mode"]),
            "skippable": bool(step.get("skippable", metadata["skippable"])),
            "failure_policy": str(
                step.get("failure_policy") or metadata["failure_policy"]
            ),
        }
        contract_digest = canonical_json_hash(
            node_payload,
            exclude_environment_fields=False,
        )
        nodes.append(
            NodeSpec(
                node_id=action,
                index=index,
                action=action,
                requires=tuple(node_payload["requires"]),
                produces=tuple(node_payload["produces"]),
                mode=node_payload["mode"],
                skippable=node_payload["skippable"],
                failure_policy=node_payload["failure_policy"],
                contract_digest=contract_digest,
            )
        )
    snapshot_payload = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "nodes": [node.to_dict() for node in nodes],
    }
    return WorkflowSnapshot(
        schema_version=RUNTIME_SCHEMA_VERSION,
        workflow_digest=canonical_json_hash(
            snapshot_payload,
            exclude_environment_fields=False,
        ),
        nodes=tuple(nodes),
    )


def _build_attempts(
    run_id: str,
    raw_trace: Any,
    workflow_snapshot: WorkflowSnapshot,
    *,
    default_started_at: str,
    default_finished_at: str,
) -> tuple[NodeAttempt, ...]:
    if not isinstance(raw_trace, list):
        return ()
    node_ids = {node.node_id for node in workflow_snapshot.nodes}
    ordinals: dict[str, int] = defaultdict(int)
    attempts: list[NodeAttempt] = []
    for trace_index, raw_event in enumerate(raw_trace, start=1):
        if not isinstance(raw_event, Mapping):
            continue
        event = dict(raw_event)
        node_id = str(event.get("action") or "")
        if node_id not in node_ids:
            continue
        ordinals[node_id] += 1
        ordinal = ordinals[node_id]
        trace_digest = canonical_json_hash(
            event,
            exclude_environment_fields=False,
        )
        attempts.append(
            NodeAttempt(
                attempt_id=f"{run_id}:{node_id}:{ordinal}",
                node_id=node_id,
                ordinal=ordinal,
                trace_index=trace_index,
                status=str(event.get("status") or "failed"),
                started_at=str(event.get("started_at") or default_started_at),
                finished_at=str(event.get("finished_at") or default_finished_at),
                duration_ms=max(0, int(event.get("duration_ms") or 0)),
                implicit=bool(event.get("implicit")),
                skipped=bool(event.get("skipped")),
                trace_digest=trace_digest,
            )
        )
    return tuple(attempts)


def _build_context_manifest(context_inputs: Mapping[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for logical_ref in (
        "snapshot",
        "input_pack",
        "workflow_plan",
        "chapter",
        "validation",
        "analysis",
    ):
        if logical_ref not in context_inputs:
            continue
        value = context_inputs[logical_ref]
        if isinstance(value, str):
            rendered = value
            content_type = "text/plain"
        else:
            rendered = canonical_json_bytes(
                value,
                exclude_environment_fields=False,
            ).decode("utf-8")
            content_type = "application/json"
        encoded = rendered.encode("utf-8")
        entries.append(
            {
                "logical_ref": logical_ref,
                "direction": "input" if logical_ref in _INPUT_CONTEXTS else "output",
                "content_type": content_type,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "chars": len(rendered),
                "estimated_tokens": conservative_token_estimate(rendered),
                "token_estimate_method": "utf8_bytes_upper_bound",
            }
        )
    manifest = {"entries": entries}
    manifest["manifest_digest"] = canonical_json_hash(
        manifest,
        exclude_environment_fields=False,
    )
    return manifest


def _recovery_decision(
    run: Mapping[str, Any],
    attempts: Sequence[NodeAttempt],
) -> RecoveryDecision:
    failed = next((attempt for attempt in attempts if attempt.status == "failed"), None)
    if failed is not None:
        return RecoveryDecision(
            decision="legacy_executor_required",
            resume_node_id=failed.node_id,
            reason=(
                "Shadow checkpoints are observational only; the legacy executor "
                "must decide whether and how to retry the failed node."
            ),
        )
    if str(run.get("status") or "") == "failed":
        return RecoveryDecision(
            decision="legacy_executor_required",
            resume_node_id=None,
            reason=(
                "The run failed outside an observed workflow node; recovery remains "
                "owned by the legacy executor."
            ),
        )
    return RecoveryDecision(
        decision="not_needed",
        resume_node_id=None,
        reason="No failed workflow node was observed.",
    )


def _build_node_usage(
    model_call_receipts: Sequence[Mapping[str, Any]],
) -> tuple[NodeUsage, ...]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for receipt in model_call_receipts:
        stage = str(receipt.get("stage") or "")
        grouped[_STAGE_TO_NODE.get(stage, "execution_support")].append(receipt)

    usage_items: list[NodeUsage] = []
    for node_id in sorted(grouped):
        receipts = sorted(
            grouped[node_id],
            key=lambda item: (
                str(item.get("received_at") or ""),
                str(item.get("attempt_id") or ""),
            ),
        )
        parsed = [_token_usage(item.get("usage")) for item in receipts]
        complete = all(item[3] for item in parsed)
        input_tokens = sum(item[0] for item in parsed if item[0] is not None) if complete else None
        output_tokens = sum(item[1] for item in parsed if item[1] is not None) if complete else None
        total_tokens = sum(item[2] for item in parsed if item[2] is not None) if complete else None
        usage_items.append(
            NodeUsage(
                node_id=node_id,
                source="model_call_receipts",
                receipt_hashes=tuple(str(item["receipt_hash"]) for item in receipts),
                call_count=len(receipts),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                complete=complete,
            )
        )
    return tuple(usage_items)


def _token_usage(value: Any) -> tuple[int | None, int | None, int | None, bool]:
    if not isinstance(value, Mapping):
        return None, None, None, False
    usage = value
    input_tokens = _usage_total(
        usage,
        ("input_tokens", "prompt_tokens"),
        nested_key="input_tokens_details",
    )
    output_tokens = _usage_total(
        usage,
        ("output_tokens", "completion_tokens"),
        nested_key="output_tokens_details",
    )
    cache_creation = _first_non_negative_int(usage, "cache_creation_input_tokens")
    cache_read = _first_non_negative_int(usage, "cache_read_input_tokens")
    if cache_creation is not None or cache_read is not None:
        input_tokens = (input_tokens or 0) + (cache_creation or 0) + (cache_read or 0)
    total_tokens = _first_non_negative_int(usage, "total_tokens")
    if total_tokens is not None:
        if input_tokens is None and output_tokens is not None and output_tokens <= total_tokens:
            input_tokens = total_tokens - output_tokens
        if output_tokens is None and input_tokens is not None and input_tokens <= total_tokens:
            output_tokens = total_tokens - input_tokens
    if input_tokens is not None and output_tokens is not None and total_tokens is None:
        total_tokens = input_tokens + output_tokens
    complete = (
        input_tokens is not None
        and output_tokens is not None
        and total_tokens is not None
        and input_tokens + output_tokens == total_tokens
    )
    return input_tokens, output_tokens, total_tokens, complete


def _usage_total(
    value: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    nested_key: str,
) -> int | None:
    selected = _first_non_negative_int(value, *keys)
    nested = value.get(nested_key)
    if isinstance(nested, Mapping):
        nested_total = _first_non_negative_int(nested, "total")
        if selected is None:
            selected = nested_total
        elif nested_total is not None and selected != nested_total:
            return None
    return selected


def _first_non_negative_int(value: Mapping[str, Any], *keys: str) -> int | None:
    selected: int | None = None
    for key in keys:
        raw = value.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            continue
        if selected is None:
            selected = raw
        elif selected != raw:
            return None
    return selected


__all__ = ["build_shadow_runtime_evidence"]
