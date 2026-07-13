from __future__ import annotations

from typing import Any

from core.quality_decision import QualityPolicy, SEVERITY_RANK, build_quality_decision, resolve_quality_policy
from core.review.gate import evaluate_review_gate
from core.review.repair_loop import ReviewRepairConfig
from core.review.runtime import RuntimeReviewConfig


class QualityCoordinator:
    """Centralizes effective quality policy, review fusion, and gate derivation."""

    @staticmethod
    def effective_policy(
        *,
        configured_policy: QualityPolicy | None,
        persist: bool,
        story_project_apply: bool,
        has_story_project_context: bool,
        review_config: RuntimeReviewConfig,
        review_repair_config: ReviewRepairConfig,
    ) -> QualityPolicy:
        if configured_policy is not None:
            policy = configured_policy
        elif persist and story_project_apply and has_story_project_context:
            policy = resolve_quality_policy("standard")
        else:
            policy = resolve_quality_policy("minimal")
        include_review = bool(policy.include_review or review_config.enabled or review_repair_config.enabled)
        threshold = policy.threshold
        if review_config.enabled and review_config.gate_threshold != "off":
            gate_threshold = "blocking" if review_config.gate_threshold == "blocked" else review_config.gate_threshold
            threshold = min((threshold, gate_threshold), key=lambda item: SEVERITY_RANK[item])
        return policy.with_overrides(threshold=threshold, include_review=include_review)

    @staticmethod
    def decide(
        *,
        policy: QualityPolicy,
        validation: dict[str, Any],
        review: dict[str, Any] | None = None,
        chapter_index: int | None = None,
    ) -> dict[str, Any]:
        upstream = review.get("quality_decision") if isinstance(review, dict) else None
        return build_quality_decision(
            policy=policy,
            validation=validation,
            upstream_decisions=[upstream] if isinstance(upstream, dict) else [],
            review_pipeline=review,
            chapter_index=chapter_index,
        )

    @staticmethod
    def review_gate(
        *,
        review_config: RuntimeReviewConfig,
        review: dict[str, Any] | None,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any] | None:
        if review_config.gate_threshold == "off" or not isinstance(review, dict):
            return None
        return evaluate_review_gate(
            review_pipeline=review,
            quality_decision=quality_decision,
            threshold=review_config.gate_threshold,
        )

    @staticmethod
    def runtime_review_config(
        policy: QualityPolicy,
        configured: RuntimeReviewConfig,
    ) -> RuntimeReviewConfig:
        if configured.enabled or not policy.include_review:
            return configured
        return RuntimeReviewConfig(
            enabled=True,
            output_dir=configured.output_dir,
            rules_path=configured.rules_path,
            use_default_rules=configured.use_default_rules,
            build_repair_prompt=configured.build_repair_prompt,
            build_human_report=configured.build_human_report,
            gate_threshold="off",
        )


__all__ = ["QualityCoordinator"]
