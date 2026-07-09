from __future__ import annotations

import copy
import json
from typing import Any

from core.schema import validate_schema


class RuleRepairPromptError(ValueError):
    pass


def build_rule_repair_prompt(
    *,
    chapter_text: str,
    snapshot: dict,
    rule_repair_plan: dict,
    previous_chapter_text: str | None = None,
    narrative_rules: str | None = None,
    max_tasks: int | None = None,
    include_non_blocking: bool = True,
) -> dict:
    if max_tasks is not None and max_tasks < 0:
        raise RuleRepairPromptError("max_tasks must be >= 0")

    chapter = str(chapter_text)
    snapshot_copy = copy.deepcopy(snapshot)
    plan = validate_schema(copy.deepcopy(rule_repair_plan), "rule_repair_plan.schema.json")
    previous = str(previous_chapter_text) if previous_chapter_text is not None else None
    rules_text = narrative_rules.strip() if isinstance(narrative_rules, str) and narrative_rules.strip() else None

    tasks = _select_tasks(
        plan["tasks"],
        include_non_blocking=include_non_blocking,
        max_tasks=max_tasks,
    )
    prompt = _render_prompt(
        chapter_text=chapter,
        snapshot=snapshot_copy,
        previous_chapter_text=previous,
        narrative_rules=rules_text,
        tasks=tasks,
    )
    metadata = {
        "schema_version": "1.0",
        "kind": "rule_repair_prompt",
        "chars": len(prompt),
        "source_plan": {
            "status": plan["status"],
            "task_count": plan["summary"]["task_count"],
            "blocking_task_count": plan["summary"]["blocking_task_count"],
            "human_review_task_count": plan["summary"]["human_review_task_count"],
        },
        "prompt": {
            "task_count": len(tasks),
            "blocking_task_count": sum(1 for task in tasks if task["blocking"]),
            "included_non_blocking": include_non_blocking,
            "has_previous_chapter": previous is not None,
            "has_narrative_rules": rules_text is not None,
        },
        "metadata": {
            "created_by": "NovelAgent",
            "source": "rule-aware-repair-prompt",
        },
    }
    return {
        "prompt": prompt,
        "metadata": validate_schema(metadata, "rule_repair_prompt_metadata.schema.json"),
    }


def _select_tasks(
    tasks: list[dict[str, Any]],
    *,
    include_non_blocking: bool,
    max_tasks: int | None,
) -> list[dict[str, Any]]:
    selected = [copy.deepcopy(task) for task in sorted(tasks, key=lambda item: int(item["priority"]))]
    if not include_non_blocking:
        selected = [task for task in selected if task["blocking"]]
    if max_tasks is not None:
        selected = selected[:max_tasks]
    return selected


def _render_prompt(
    *,
    chapter_text: str,
    snapshot: dict,
    previous_chapter_text: str | None,
    narrative_rules: str | None,
    tasks: list[dict[str, Any]],
) -> str:
    sections = [
        "# Rule-aware Chapter Repair Prompt",
        "",
        "## Role",
        "",
        "You are NovelAgent's chapter repair module. Repair only the provided chapter according to the repair plan.",
        "",
        "## Non-negotiable Output Contract",
        "",
        "- Output only the repaired chapter prose.",
        "- Do not output analysis.",
        "- Do not output JSON.",
        "- Do not output Markdown headings.",
        "- Do not explain how you repaired the chapter.",
        "- Do not change established facts from the Snapshot.",
        "- Do not remove original chapter material that still provides valid story progress.",
        "- Resolve blocking repair tasks first.",
        "",
        "## Snapshot Context",
        "",
        "```json",
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ]
    if previous_chapter_text is not None:
        sections.extend([
            "## Previous Chapter Context",
            "",
            previous_chapter_text.strip(),
            "",
        ])
    if narrative_rules is not None:
        sections.extend([
            "## Narrative Rules",
            "",
            narrative_rules.strip(),
            "",
        ])
    sections.extend([
        "## Original Chapter",
        "",
        chapter_text.strip(),
        "",
        "## Repair Tasks",
        "",
    ])
    if tasks:
        for task in tasks:
            sections.extend(_render_task(task))
    else:
        sections.extend([
            "当前没有需要修复的任务。请原样保留章节正文，不要扩写或改写。",
            "",
        ])
    sections.extend([
        "## Acceptance Criteria",
        "",
        "- The output contains only repaired chapter prose.",
        "- The output contains no analysis, headings, JSON, author notes, or model explanations.",
        "- The repair does not change established Snapshot facts.",
        "- The repair does not remove valid story progress from the original chapter.",
        "- Blocking tasks are fixed before non-blocking tasks.",
        "- The repaired chapter should pass Chapter Quality Evaluation.",
        "- The repaired chapter should pass Rule-aware Validation.",
    ])
    return "\n".join(sections).rstrip() + "\n"


def _render_task(task: dict[str, Any]) -> list[str]:
    return [
        f"### {task['task_id']} - {task['repair_type']}",
        "",
        f"- task_id: `{task['task_id']}`",
        f"- rule_code: `{task['rule_code']}`",
        f"- rule_status: `{task['rule_status']}`",
        f"- severity: `{task['severity']}`",
        f"- blocking: `{str(task['blocking']).lower()}`",
        f"- repair_type: `{task['repair_type']}`",
        f"- instruction: {task['instruction']}",
        "- evidence:",
        "",
        "```json",
        json.dumps(task.get("evidence") or {}, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ]


__all__ = [
    "RuleRepairPromptError",
    "build_rule_repair_prompt",
]
