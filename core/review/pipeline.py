from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.quality import evaluate_chapter_quality
from core.quality_decision import build_quality_decision
from core.review.report import build_human_review_report
from core.rules import (
    build_rule_repair_plan,
    build_rule_repair_prompt,
    validate_chapter_against_rules,
)
from core.schema import validate_schema


ARTIFACT_FILENAMES = {
    "chapter_quality_report": "chapter_quality_report.json",
    "rule_validation_report": "rule_validation_report.json",
    "rule_repair_plan": "rule_repair_plan.json",
    "rule_repair_prompt": "rule_repair_prompt.md",
    "rule_repair_prompt_metadata": "rule_repair_prompt_metadata.json",
    "human_review_report": "human_review_report.md",
    "human_review_report_metadata": "human_review_report_metadata.json",
    "review_pipeline_summary": "review_pipeline_summary.json",
}


class ReviewPipelineError(ValueError):
    pass


def run_review_pipeline(
    *,
    chapter_text: str,
    snapshot: dict,
    previous_chapter_text: str | None = None,
    rule_pack: dict | None = None,
    rule_pack_path: str | Path | None = None,
    use_default_rules: bool = True,
    output_dir: str | Path | None = None,
    build_repair_prompt: bool = True,
    build_human_report: bool = True,
    title: str | None = None,
) -> dict:
    if rule_pack is None and rule_pack_path is None and not use_default_rules:
        raise ReviewPipelineError("missing rules: pass rule_pack, rule_pack_path, or use_default_rules=True")

    chapter = str(chapter_text)
    snapshot_copy = copy.deepcopy(snapshot)
    previous = str(previous_chapter_text) if previous_chapter_text is not None else None
    rule_pack_copy = copy.deepcopy(rule_pack) if rule_pack is not None else None

    quality_report = evaluate_chapter_quality(
        chapter_text=chapter,
        snapshot=copy.deepcopy(snapshot_copy),
        previous_chapter_text=previous,
    )
    rule_validation_report = validate_chapter_against_rules(
        chapter_text=chapter,
        snapshot=copy.deepcopy(snapshot_copy),
        rule_pack=rule_pack_copy,
        rule_pack_path=rule_pack_path,
        use_default_rules=use_default_rules,
        previous_chapter_text=previous,
        quality_report=quality_report,
    )
    rule_repair_plan = build_rule_repair_plan(
        rule_validation_report=rule_validation_report,
    )

    repair_prompt: dict[str, Any] | None = None
    if build_repair_prompt:
        repair_prompt = build_rule_repair_prompt(
            chapter_text=chapter,
            snapshot=copy.deepcopy(snapshot_copy),
            previous_chapter_text=previous,
            rule_repair_plan=rule_repair_plan,
        )

    human_review_report: dict[str, Any] | None = None
    if build_human_report:
        human_review_report = build_human_review_report(
            chapter_text=chapter,
            chapter_quality_report=quality_report,
            rule_validation_report=rule_validation_report,
            rule_repair_plan=rule_repair_plan,
            rule_repair_prompt_metadata=repair_prompt["metadata"] if repair_prompt is not None else None,
            title=title,
        )

    artifact_paths = _artifact_paths(output_dir)
    summary = _build_summary(
        output_dir=output_dir,
        artifact_paths=artifact_paths,
        quality_report=quality_report,
        rule_validation_report=rule_validation_report,
        rule_repair_plan=rule_repair_plan,
        repair_prompt=repair_prompt,
        human_review_report=human_review_report,
        previous_chapter_text=previous,
        used_default_rules=rule_pack is None and rule_pack_path is None and use_default_rules,
        chapter_index=_chapter_index(snapshot_copy),
    )

    if output_dir is not None:
        _write_artifacts(
            artifact_paths=artifact_paths,
            quality_report=quality_report,
            rule_validation_report=rule_validation_report,
            rule_repair_plan=rule_repair_plan,
            repair_prompt=repair_prompt,
            human_review_report=human_review_report,
            summary=summary,
        )

    return summary


def _build_summary(
    *,
    output_dir: str | Path | None,
    artifact_paths: dict[str, str | None],
    quality_report: dict,
    rule_validation_report: dict,
    rule_repair_plan: dict,
    repair_prompt: dict | None,
    human_review_report: dict | None,
    previous_chapter_text: str | None,
    used_default_rules: bool,
    chapter_index: int | None,
) -> dict:
    decision = _decision(human_review_report)
    artifacts = dict(artifact_paths)
    if repair_prompt is None:
        artifacts["rule_repair_prompt"] = None
        artifacts["rule_repair_prompt_metadata"] = None
    if human_review_report is None:
        artifacts["human_review_report"] = None
        artifacts["human_review_report_metadata"] = None
    summary = {
        "schema_version": "1.0",
        "status": _summary_status(
            decision=decision["decision"],
            quality_report=quality_report,
            rule_validation_report=rule_validation_report,
            rule_repair_plan=rule_repair_plan,
        ),
        "decision": decision,
        "scores": {
            "quality_score": int(quality_report["score"]),
            "rule_score": int(rule_validation_report["score"]),
        },
        "reports": {
            "quality_status": str(quality_report["status"]),
            "rule_validation_status": str(rule_validation_report["status"]),
            "repair_plan_status": str(rule_repair_plan["status"]),
            "human_review_decision": decision["decision"] if human_review_report is not None else "unknown",
        },
        "tasks": {
            "repair_task_count": int(rule_repair_plan["summary"]["task_count"]),
            "blocking_task_count": int(rule_repair_plan["summary"]["blocking_task_count"]),
            "human_review_task_count": int(rule_repair_plan["summary"]["human_review_task_count"]),
        },
        "artifacts": artifacts,
        "flags": {
            "has_previous_chapter": previous_chapter_text is not None,
            "has_repair_prompt": repair_prompt is not None,
            "has_human_review_report": human_review_report is not None,
            "used_default_rules": bool(used_default_rules),
        },
        "metadata": {
            "created_by": "NovelAgent",
            "source": "review-pipeline-orchestrator",
        },
        "quality_decision": build_quality_decision(
            policy="standard",
            chapter_quality_report=quality_report,
            rule_validation_report=rule_validation_report,
            chapter_index=chapter_index,
            source_artifacts={
                "deterministic_review": str(artifact_paths.get("chapter_quality_report") or "chapter_quality_report"),
                "narrative_rules": str(artifact_paths.get("rule_validation_report") or "rule_validation_report"),
            },
        ),
    }
    if output_dir is None:
        summary["artifacts"]["output_dir"] = None
    return validate_schema(summary, "review_pipeline_summary.schema.json")


def _chapter_index(snapshot: dict[str, Any]) -> int | None:
    value = snapshot.get("chapter_index")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _decision(human_review_report: dict | None) -> dict:
    if human_review_report is None:
        return {
            "decision": "unknown",
            "label": "unknown",
            "allowed_next_steps": [],
        }
    metadata = human_review_report["metadata"]
    decision = metadata["decision"]
    return {
        "decision": decision["decision"],
        "label": decision["label"],
        "allowed_next_steps": list(decision["allowed_next_steps"]),
    }


def _summary_status(
    *,
    decision: str,
    quality_report: dict,
    rule_validation_report: dict,
    rule_repair_plan: dict,
) -> str:
    if decision == "blocked":
        return "blocked"
    if decision == "needs_revision":
        return "needs_revision"
    if decision == "accept_with_warnings":
        return "warning"
    if decision == "accept":
        return "pass"

    if rule_repair_plan["status"] == "blocked":
        return "blocked"
    if rule_repair_plan["status"] == "needs_repair":
        return "needs_revision"
    if rule_validation_report["status"] == "warning" or quality_report["status"] == "warning":
        return "warning"
    return "pass"


def _artifact_paths(output_dir: str | Path | None) -> dict[str, str | None]:
    paths: dict[str, str | None] = {"output_dir": str(output_dir) if output_dir is not None else None}
    for key, filename in ARTIFACT_FILENAMES.items():
        paths[key] = str(Path(output_dir) / filename) if output_dir is not None else None
    return paths


def _write_artifacts(
    *,
    artifact_paths: dict[str, str | None],
    quality_report: dict,
    rule_validation_report: dict,
    rule_repair_plan: dict,
    repair_prompt: dict | None,
    human_review_report: dict | None,
    summary: dict,
) -> None:
    output_dir = artifact_paths.get("output_dir")
    if output_dir is None:
        return
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    _write_json(artifact_paths["chapter_quality_report"], quality_report)
    _write_json(artifact_paths["rule_validation_report"], rule_validation_report)
    _write_json(artifact_paths["rule_repair_plan"], rule_repair_plan)
    if repair_prompt is not None:
        _write_text(artifact_paths["rule_repair_prompt"], repair_prompt["prompt"])
        _write_json(artifact_paths["rule_repair_prompt_metadata"], repair_prompt["metadata"])
    if human_review_report is not None:
        _write_text(artifact_paths["human_review_report"], human_review_report["markdown"])
        _write_json(artifact_paths["human_review_report_metadata"], human_review_report["metadata"])
    _write_json(artifact_paths["review_pipeline_summary"], summary)


def _write_json(path: str | None, value: dict) -> None:
    if path is None:
        return
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: str | None, value: str) -> None:
    if path is None:
        return
    Path(path).write_text(value, encoding="utf-8")


__all__ = [
    "ARTIFACT_FILENAMES",
    "ReviewPipelineError",
    "run_review_pipeline",
]
