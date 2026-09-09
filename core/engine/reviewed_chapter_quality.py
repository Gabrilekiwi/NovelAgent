"""Local quality evidence for a server-prepared, author-reviewed chapter.

This is not a publication gate. Candidate scene/blueprint provenance and the
commit service's ownership, freshness and transaction gates remain necessary.
Only the server may supply the verified preparation and trusted context; their
checksums cannot prove author intent or turn client payloads into trusted input.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any

from core.engine.chapter_candidate_context import TrustedChapterCandidateContext
from core.engine.chapter_commit_prepare import PREPARATION_VERSION, normalize_commit_prose
from core.engine.chapter_fact_context import validate_trusted_chapter_context
from core.engine.chapter_fact_prepare import prepare_confirmed_fact_changes
from core.memory_v2.canonical import canonical_json_hash
from core.quality.final_artifact_integrity import (
    FinalArtifactIntegrityGate, merge_integrity_report_into_validation,
)
from core.quality_decision import build_quality_decision, resolve_quality_policy
from core.review.gate import evaluate_review_gate
from core.review.pipeline import run_review_pipeline
from core.rules.narrative_rules import load_default_narrative_rule_pack
from core.schema import validate_schema
from core.state.authoritative import (
    merge_authoritative_report_into_validation, validate_authoritative_state_delta,
)
from core.story_project.identity import validate_project_identity
from core.story_project.read_set import verify_story_project_read_set
from core.story_project.reviewed_state_context import (
    reviewed_state_snapshot, validated_reviewed_state_context,
)
from core.validator import validate_chapter


QUALITY_PREPARATION_VERSION = "reviewed_chapter_local_quality_v1"


class ReviewedChapterQualityError(ValueError):
    code = "reviewed_chapter_quality_invalid"


def _hash(value: Any) -> str:
    return canonical_json_hash(value, exclude_environment_fields=False)


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject(message: str) -> None:
    raise ReviewedChapterQualityError(message)


def prepare_reviewed_chapter_quality(
    *,
    trusted_context: TrustedChapterCandidateContext,
    chapter_preparation: dict,
    quality_policy: str = "standard",
) -> dict:
    """Evaluate final bytes without providers, repairs, cache or artifact writes.

    Standard uses the same local producers as the runtime. Strict additionally
    requires the model validator and consequently stays blocked here. Neither
    policy may claim complete quality coverage until verified Candidate scene
    and blueprint evidence is connected by the final commit service.
    """
    if not isinstance(quality_policy, str) or quality_policy not in {"standard", "strict"}:
        _reject("only the unmodified standard and strict quality policies are supported")
    policy = resolve_quality_policy(quality_policy)
    context = validate_trusted_chapter_context(trusted_context)
    view = validated_reviewed_state_context(context)
    if view is None:
        _reject("a verified reviewed legacy state is required")
    prepared = copy.deepcopy(chapter_preparation)
    if (not isinstance(prepared, dict)
            or prepared.get("preparation_version") != PREPARATION_VERSION
            or prepared.get("stage") != "facts_prepared"
            or prepared.get("publication_authorized") is not False
            or prepared.get("preparation_sha256") != _hash({
                k: v for k, v in prepared.items() if k != "preparation_sha256"
            })):
        _reject("chapter preparation is unsupported or its checksum changed")
    binding = prepared.get("binding")
    if not isinstance(binding, dict):
        _reject("chapter preparation has no binding")
    final_prose = prepared.get("final_prose")
    if (not isinstance(final_prose, str) or final_prose != normalize_commit_prose(final_prose)
            or binding.get("final_prose_sha256") != _text_hash(final_prose)
            or prepared.get("request_sha256") != _hash({"version": PREPARATION_VERSION, "binding": binding})):
        _reject("final prose or request differs from its preparation binding")
    read_set = context["read_set"]
    expected_binding = {
        "book_id": view["book_id"], "chapter": trusted_context.chapter_index,
        "context_sha256": trusted_context.context_sha256, "read_set_sha256": _hash(read_set),
        "identity_revision": read_set["identity_revision"],
        "reviewed_source_sha256": view["source_sha256"],
        "manual_sources_sha256": view["manual_sources_sha256"],
        "reviewed_payload_sha256": view["payload_sha256"],
        "committed_through": view["committed_through"], "acceptance_kind": "author",
        "authority": validate_project_identity(context["project_identity"]).authority,
    }
    if (prepared.get("read_set") != read_set
            or any(binding.get(k) != v for k, v in expected_binding.items())
            or binding.get("facts_sha256") != _hash(prepared.get("facts"))):
        _reject("chapter preparation belongs to different facts, context, state or authority")
    verify_story_project_read_set(read_set, phase="prepare")
    fact_preparation = prepare_confirmed_fact_changes(
        facts=prepared["facts"], chapter_text=final_prose, base_state=view["authoritative_state"],
    )
    if (prepared.get("fact_preparation") != fact_preparation
            or binding.get("state_before_sha256") != fact_preparation["base_state_sha256"]):
        _reject("prepared facts do not replay against the current reviewed state")

    # The builder's snapshot is an intentionally empty cache seed. The runtime
    # uses this reviewed-state projection; validating the seed would lose the
    # author's current seven-state baseline and continuity constraints.
    snapshot = reviewed_state_snapshot(context)
    previous = context.get("previous_chapter_context")
    if not isinstance(previous, dict):
        _reject("reviewed continuation requires a verified previous chapter context")
    validate_schema(previous, "previous_chapter_context.schema.json")
    previous_text = previous["review_tail"]["text"]
    if (previous["chapter_index"] != trusted_context.chapter_index - 1
            or previous["committed_verified"] is not True or not previous_text.strip()):
        _reject("previous chapter review tail is missing, unverified or from another chapter")

    validation = validate_chapter(snapshot, final_prose, enable_llm=False)
    integrity = FinalArtifactIntegrityGate().evaluate(
        artifact_text=final_prose, stage="final_gate",
        expected_artifact_sha256=binding["final_prose_sha256"],
    )
    validation = merge_integrity_report_into_validation(validation, integrity)
    authoritative = validate_authoritative_state_delta(
        base_state=view["authoritative_state"], state_delta=fact_preparation["state_delta"],
        chapter_text=final_prose,
    )
    validation = merge_authoritative_report_into_validation(validation, authoritative)
    rule_pack = load_default_narrative_rule_pack()
    review = run_review_pipeline(
        chapter_text=final_prose, snapshot=snapshot, previous_chapter_text=previous_text,
        rule_pack=rule_pack, use_default_rules=False, output_dir=None,
        build_repair_prompt=False, build_human_report=True,
    )
    # The raw review remains an independent gate. Aggregate acceptance never
    # overrides it, even if future producer fusion changes severity mappings.
    review_gate = evaluate_review_gate(review_pipeline=review, threshold="needs_revision")
    decision = build_quality_decision(
        policy=policy, validation=validation, upstream_decisions=[review["quality_decision"]],
        review_pipeline=review, chapter_index=trusted_context.chapter_index,
    )
    verify_story_project_read_set(read_set, phase="prepare")
    if load_default_narrative_rule_pack() != rule_pack:
        _reject("narrative rule pack changed during quality evaluation")
    pending = ["candidate_scene_and_blueprint_evidence"]
    if policy.llm_validator_required:
        pending.append("llm_validator")
    result = {
        "quality_preparation_version": QUALITY_PREPARATION_VERSION,
        "stage": "local_quality_checked", "publication_authorized": False,
        "quality_coverage_complete": False, "pending_gates": pending,
        "model_validation_executed": False,
        "local_quality_passed": bool(decision["accepted"] and review_gate["status"] == "pass"
                                     and integrity["accepted"] and authoritative["accepted"]),
        "binding": {
            **copy.deepcopy(binding),
            "chapter_preparation_sha256": prepared["preparation_sha256"],
            "request_sha256": prepared["request_sha256"],
            "idempotency_scope_sha256": prepared["idempotency_scope_sha256"],
            "quality_policy": policy.to_dict(), "snapshot_sha256": _hash(snapshot),
            "previous_chapter_context_sha256": _hash(previous),
            "previous_review_tail_sha256": _text_hash(previous_text),
            "rule_pack_sha256": _hash(rule_pack),
            "state_after_sha256": _hash(fact_preparation["state_after"]),
        },
        "validation": validation, "final_artifact_integrity": integrity,
        "authoritative_state_validation": authoritative, "review_pipeline": review,
        "review_gate": review_gate, "local_policy_decision": decision,
        "rule_pack": copy.deepcopy(rule_pack),
    }
    result["quality_sha256"] = _hash(result)
    return result
