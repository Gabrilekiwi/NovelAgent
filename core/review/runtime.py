from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.review.pipeline import run_review_pipeline


@dataclass(frozen=True)
class RuntimeReviewConfig:
    enabled: bool = False
    output_dir: Path | None = None
    rules_path: Path | None = None
    use_default_rules: bool = True
    build_repair_prompt: bool = True
    build_human_report: bool = True
    gate_threshold: str = "off"


def disabled_review_summary() -> dict[str, Any]:
    return {"enabled": False}


def run_runtime_review(
    *,
    chapter_text: str,
    snapshot: dict,
    previous_chapter_text: str | None,
    run_id: str,
    config: RuntimeReviewConfig,
) -> dict[str, Any]:
    if not config.enabled:
        return disabled_review_summary()

    root_dir = config.output_dir or Path(".tmp/runtime/reviews")
    artifacts_dir = root_dir / _safe_run_dir_name(run_id)
    try:
        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous_chapter_text,
            rule_pack_path=config.rules_path,
            use_default_rules=config.use_default_rules,
            output_dir=artifacts_dir,
            build_repair_prompt=config.build_repair_prompt,
            build_human_report=config.build_human_report,
        )
    except Exception as exc:  # noqa: BLE001 - runtime review is diagnostic; chapter generation remains intact.
        return {
            "enabled": True,
            "status": "error",
            "decision": None,
            "quality_score": None,
            "rule_score": None,
            "repair_task_count": None,
            "blocking_task_count": None,
            "artifacts_dir": str(artifacts_dir),
            "summary_path": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return summarize_review_pipeline(summary, artifacts_dir=artifacts_dir)


def summarize_review_pipeline(summary: dict[str, Any], *, artifacts_dir: str | Path) -> dict[str, Any]:
    return {
        "enabled": True,
        "status": summary["status"],
        "decision": summary["decision"]["decision"],
        "quality_score": summary["scores"]["quality_score"],
        "rule_score": summary["scores"]["rule_score"],
        "repair_task_count": summary["tasks"]["repair_task_count"],
        "blocking_task_count": summary["tasks"]["blocking_task_count"],
        "artifacts_dir": str(artifacts_dir),
        "summary_path": summary["artifacts"]["review_pipeline_summary"],
    }


def validate_runtime_review_config(config: RuntimeReviewConfig) -> RuntimeReviewConfig:
    if config.gate_threshold not in {"off", "warning", "needs_revision", "blocked"}:
        raise ValueError(f"unsupported review gate threshold: {config.gate_threshold}")
    if not config.enabled and config.gate_threshold != "off":
        raise ValueError("--review-gate requires --enable-review-pipeline")
    if config.enabled and not config.use_default_rules and config.rules_path is None:
        raise ValueError("review rules are required when default review rules are disabled")
    return config


def _safe_run_dir_name(run_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(run_id))
    return safe or "review"


__all__ = [
    "RuntimeReviewConfig",
    "disabled_review_summary",
    "run_runtime_review",
    "summarize_review_pipeline",
    "validate_runtime_review_config",
]
