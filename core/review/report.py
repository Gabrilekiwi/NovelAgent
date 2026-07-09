from __future__ import annotations

import copy
import json
from typing import Any

from core.schema import validate_schema


SEVERITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

DECISION_LABELS = {
    "accept": "可接受",
    "accept_with_warnings": "可接受但有警告",
    "needs_revision": "需要修复",
    "blocked": "阻塞，必须修复",
}


def build_human_review_report(
    *,
    chapter_text: str | None = None,
    chapter_quality_report: dict,
    rule_validation_report: dict,
    rule_repair_plan: dict,
    rule_repair_prompt_metadata: dict | None = None,
    title: str | None = None,
) -> dict:
    quality = validate_schema(copy.deepcopy(chapter_quality_report), "chapter_quality_report.schema.json")
    validation = validate_schema(copy.deepcopy(rule_validation_report), "rule_validation_report.schema.json")
    repair_plan = validate_schema(copy.deepcopy(rule_repair_plan), "rule_repair_plan.schema.json")
    prompt_metadata = (
        validate_schema(copy.deepcopy(rule_repair_prompt_metadata), "rule_repair_prompt_metadata.schema.json")
        if rule_repair_prompt_metadata is not None
        else None
    )
    chapter = str(chapter_text) if chapter_text is not None else None

    decision = _review_decision(quality, validation, repair_plan)
    severe_rules = _rules_by_status(validation, "fail")
    warning_rules = _rules_by_status(validation, "warning")
    skipped_rules = _rules_by_status(validation, "skip")
    tasks_by_rule = {str(task["rule_code"]): task for task in repair_plan["tasks"]}
    markdown = _render_markdown(
        title=title or "小说章节审稿报告",
        chapter_text=chapter,
        quality=quality,
        validation=validation,
        repair_plan=repair_plan,
        prompt_metadata=prompt_metadata,
        decision=decision,
        severe_rules=severe_rules,
        warning_rules=warning_rules,
        skipped_rules=skipped_rules,
        tasks_by_rule=tasks_by_rule,
    )
    metadata = {
        "schema_version": "1.0",
        "kind": "human_review_report",
        "chars": len(markdown),
        "decision": decision,
        "source_reports": {
            "quality_status": quality["status"],
            "quality_score": quality["score"],
            "rule_validation_status": validation["status"],
            "rule_validation_score": validation["score"],
            "repair_plan_status": repair_plan["status"],
            "repair_task_count": repair_plan["summary"]["task_count"],
            "blocking_task_count": repair_plan["summary"]["blocking_task_count"],
            "has_repair_prompt_metadata": prompt_metadata is not None,
        },
        "summary": {
            "critical_issue_count": sum(1 for rule in severe_rules if rule["severity"] == "critical"),
            "high_issue_count": sum(1 for rule in severe_rules if rule["severity"] == "high"),
            "warning_issue_count": len(warning_rules),
            "skipped_rule_count": len(skipped_rules),
        },
        "metadata": {
            "created_by": "NovelAgent",
            "source": "human-review-report",
        },
    }
    return {
        "markdown": markdown,
        "metadata": validate_schema(metadata, "human_review_report_metadata.schema.json"),
    }


def _review_decision(quality: dict[str, Any], validation: dict[str, Any], repair_plan: dict[str, Any]) -> dict[str, Any]:
    if repair_plan["status"] == "blocked":
        decision = "blocked"
        reason = "There are blocking repair tasks."
        allowed_next_steps = ["build_repair_prompt", "manual_review"]
    elif repair_plan["status"] == "needs_repair":
        decision = "needs_revision"
        reason = "There are non-blocking repair tasks."
        allowed_next_steps = ["build_repair_prompt", "manual_review"]
    elif validation["status"] == "warning" or quality["status"] == "warning":
        decision = "accept_with_warnings"
        reason = "The chapter has warnings but no repair task is required."
        allowed_next_steps = ["manual_review", "continue_generation"]
    else:
        decision = "accept"
        reason = "No blocking issue or repair task was found."
        allowed_next_steps = ["continue_generation"]

    return {
        "decision": decision,
        "label": DECISION_LABELS[decision],
        "reason": reason,
        "allowed_next_steps": allowed_next_steps,
    }


def _render_markdown(
    *,
    title: str,
    chapter_text: str | None,
    quality: dict[str, Any],
    validation: dict[str, Any],
    repair_plan: dict[str, Any],
    prompt_metadata: dict[str, Any] | None,
    decision: dict[str, Any],
    severe_rules: list[dict[str, Any]],
    warning_rules: list[dict[str, Any]],
    skipped_rules: list[dict[str, Any]],
    tasks_by_rule: dict[str, dict[str, Any]],
) -> str:
    lines = [
        f"# {title}",
        "",
        "## 1. 总体结论",
        "",
        f"- 审稿结果：{decision['label']}",
        f"- 决策代码：`{decision['decision']}`",
        f"- 质量评分：{quality['score']} / 100",
        f"- 规则评分：{validation['score']} / 100",
        f"- 修复状态：`{repair_plan['status']}`",
        f"- 是否建议进入下一步：{'否' if decision['decision'] in {'blocked', 'needs_revision'} else '是'}",
        f"- 原因：{decision['reason']}",
        "",
        "## 2. 摘要",
        "",
        _summary_text(severe_rules, warning_rules, skipped_rules, repair_plan),
        "",
        "## 3. 严重问题",
        "",
    ]
    lines.extend(_render_rule_section(severe_rules, tasks_by_rule))
    lines.extend([
        "## 4. 警告问题",
        "",
    ])
    lines.extend(_render_rule_section(warning_rules, tasks_by_rule))
    lines.extend([
        "## 5. 已跳过规则",
        "",
    ])
    if skipped_rules:
        for rule in skipped_rules:
            lines.extend([
                f"- `{rule['code']}` [{rule['severity']}] {rule['title']}，原因：`{rule.get('reason') or 'unknown'}`",
            ])
    else:
        lines.append("暂无。")
    lines.extend([
        "",
        "## 6. 修复计划摘要",
        "",
        f"- 修复任务数：{repair_plan['summary']['task_count']}",
        f"- 阻塞任务数：{repair_plan['summary']['blocking_task_count']}",
        f"- 需要人工确认：{repair_plan['summary']['human_review_task_count']}",
        f"- fail 任务数：{repair_plan['summary']['fail_task_count']}",
        f"- warning 任务数：{repair_plan['summary']['warning_task_count']}",
    ])
    if prompt_metadata is not None:
        lines.extend([
            f"- 已生成 repair prompt metadata：是，prompt 字符数 {prompt_metadata['chars']}",
        ])
    else:
        lines.append("- 已生成 repair prompt metadata：否")

    lines.extend([
        "",
        "## 7. 修复优先级",
        "",
    ])
    if repair_plan["tasks"]:
        for task in repair_plan["tasks"]:
            lines.append(
                f"{task['priority']}. `{task['task_id']}` - `{task['repair_type']}` "
                f"rule=`{task['rule_code']}` blocking=`{str(task['blocking']).lower()}`"
            )
    else:
        lines.append("暂无。")

    lines.extend([
        "",
        "## 8. 审稿证据",
        "",
    ])
    evidence_lines = _render_evidence(validation)
    lines.extend(evidence_lines if evidence_lines else ["暂无。"])

    lines.extend([
        "",
        "## 9. 下一步建议",
        "",
    ])
    for step in decision["allowed_next_steps"]:
        lines.append(f"- `{step}`")
    if repair_plan["summary"]["blocking_task_count"] > 0:
        lines.append("- 先处理 blocking task，再重新运行 Chapter Quality Evaluation 和 Rule-aware Validation。")
    elif repair_plan["summary"]["task_count"] > 0:
        lines.append("- 可先生成 repair prompt 或进行人工审查，再决定是否继续生成。")
    else:
        lines.append("- 当前没有修复任务，可按决策进入下一步。")

    if chapter_text is not None:
        lines.extend([
            "",
            "## 附录：章节长度",
            "",
            f"- 原章节字符数：{len(chapter_text)}",
        ])

    return "\n".join(lines).rstrip() + "\n"


def _summary_text(
    severe_rules: list[dict[str, Any]],
    warning_rules: list[dict[str, Any]],
    skipped_rules: list[dict[str, Any]],
    repair_plan: dict[str, Any],
) -> str:
    if not severe_rules and not warning_rules and not repair_plan["tasks"]:
        return "本章没有发现需要修复的问题。"
    leading = []
    if severe_rules:
        leading.append(f"{len(severe_rules)} 个严重问题")
    if warning_rules:
        leading.append(f"{len(warning_rules)} 个警告问题")
    if skipped_rules:
        leading.append(f"{len(skipped_rules)} 条规则被跳过")
    return (
        f"本章存在{'、'.join(leading)}。"
        f"修复计划包含 {repair_plan['summary']['task_count']} 个任务，"
        f"其中 {repair_plan['summary']['blocking_task_count']} 个为阻塞任务。"
    )


def _render_rule_section(rules: list[dict[str, Any]], tasks_by_rule: dict[str, dict[str, Any]]) -> list[str]:
    if not rules:
        return ["暂无。", ""]
    lines: list[str] = []
    for index, rule in enumerate(rules, start=1):
        task = tasks_by_rule.get(str(rule["code"]))
        lines.extend([
            f"### {index}. `{rule['code']}`",
            "",
            f"- 标题：{rule['title']}",
            f"- 严重级别：`{rule['severity']}`",
            f"- 状态：`{rule['status']}`",
            f"- 分类：`{rule['category']}`",
            f"- 关联检查：{', '.join(rule['quality_check_codes']) if rule['quality_check_codes'] else '暂无'}",
        ])
        if task:
            lines.extend([
                f"- 修复任务：`{task['task_id']}`",
                f"- repair_type：`{task['repair_type']}`",
                f"- blocking：`{str(task['blocking']).lower()}`",
                f"- 修复建议：{task['instruction']}",
            ])
        else:
            lines.append("- 修复建议：暂无。")
        lines.append("")
    return lines


def _render_evidence(validation: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    rules = _rules_by_status(validation, "fail") + _rules_by_status(validation, "warning")
    for rule in rules:
        checks = rule.get("matched_quality_checks") if isinstance(rule.get("matched_quality_checks"), list) else []
        if not checks:
            continue
        lines.append(f"### `{rule['code']}`")
        lines.append("")
        for check in checks:
            lines.append(f"- check：`{check.get('code')}`，status：`{check.get('status')}`，message：{check.get('message')}")
            evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
            lines.extend([
                "",
                "```json",
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            ])
    return lines


def _rules_by_status(report: dict[str, Any], status: str) -> list[dict[str, Any]]:
    rules = [rule for rule in report["rules"] if rule["status"] == status]
    return sorted(rules, key=lambda rule: (SEVERITY_ORDER.get(str(rule.get("severity")), 99), str(rule.get("code"))))


__all__ = ["build_human_review_report"]
