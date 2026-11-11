"""Author-facing history statistics for the official workbench.

These figures are computed from every durable chapter-run record and every
job row, not from the truncated list shown in the history table.

Definitions used by the API, the history page, and tests:

- total_runs: the number of chapter generation run records on disk. This is
  not the number of workbench jobs, and it is not jobs+runs. Recovery jobs
  are counted separately as recoveries.
- first_pass_rate: among chapters that have at least one committed run, the
  share whose first generation run was itself committed with zero
  repair_attempts. A later successful run, or a first run that needed
  automatic repairs, is not first-pass.
- recoveries: the number of locked_chapter_recovery jobs. This is the
  author-visible recovery work after a stall or lock. Cancelled production
  batches with manual_confirmation_required are not treated as interventions.
- cost_label / token_label: if any run has a positive estimated_cost, those
  amounts are summed. If usage tokens exist but every estimated_cost is
  missing or 0, the page shows an unpriced token state instead of a silent
  dash or an invented currency amount.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping

from core.runtime_paths import RuntimePaths

HISTORY_LIST_LIMIT = 100
RECOVERY_JOB_KIND = "locked_chapter_recovery"


def _as_int(value: Any) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_token_count(value: int) -> str:
    return f"{max(0, int(value)):,}"


def format_first_pass_rate(passed: int, denominator: int) -> str:
    if denominator <= 0:
        return "—"
    percent = int(round((100.0 * passed) / denominator))
    return f"{percent}%（{passed}/{denominator}）"


def annotate_run_usage(run: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(run, Mapping):
        return {"token_label": "—", "cost_label": "—"}
    tokens_in = _as_int(run.get("total_input_tokens"))
    tokens_out = _as_int(run.get("total_output_tokens"))
    cost = _as_float(run.get("estimated_cost"))
    token_label = (
        f"{format_token_count(tokens_in)} / {format_token_count(tokens_out)}"
        if tokens_in or tokens_out
        else "—"
    )
    if cost is not None and cost > 0:
        cost_label = f"{cost:.2f}"
    elif tokens_in or tokens_out:
        cost_label = "未计价"
    else:
        cost_label = "—"
    return {"token_label": token_label, "cost_label": cost_label}


def _run_sort_key(run: Mapping[str, Any]) -> tuple[str, str]:
    return (str(run.get("started_at") or ""), str(run.get("id") or ""))


def _chapter_first_pass(runs: list[Mapping[str, Any]]) -> bool | None:
    committed = [run for run in runs if run.get("committed") is True]
    if not committed:
        return None
    first = sorted(runs, key=_run_sort_key)[0]
    return first.get("committed") is True and _as_int(first.get("repair_attempts")) == 0


def load_compact_runs(
    root: Path,
    compact_run: Callable[[Mapping[str, Any] | None], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    runtime = RuntimePaths.for_story_project(root)
    if not runtime.run_dir.is_dir():
        return []
    values: list[tuple[int, dict[str, Any]]] = []
    for path in runtime.run_dir.glob("chapter_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            compact = compact_run(payload)
        except (OSError, ValueError):
            continue
        if compact is None:
            continue
        values.append((path.stat().st_mtime_ns, compact))
    values.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in values]


def load_job_stat_rows(job_store: Any, project_id: str) -> list[dict[str, Any]]:
    database = getattr(job_store, "database", None)
    if database is None:
        return []
    restore_scope = getattr(database, "restore_scope", None)
    if restore_scope is not None:
        database.require_project_access(project_id)
        project_id = restore_scope.project_id
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT kind, state, manual_confirmation_required
            FROM jobs
            WHERE project_id = ?
            """,
            (str(project_id),),
        ).fetchall()
    return [
        {
            "kind": row["kind"],
            "state": row["state"],
            "manual_confirmation_required": bool(row["manual_confirmation_required"]),
        }
        for row in rows
    ]


def build_history_stats(
    *,
    jobs: list[Mapping[str, Any]] | None = None,
    runs: list[Mapping[str, Any]] | None = None,
    listed_run_count: int | None = None,
) -> dict[str, Any]:
    job_rows = [job for job in (jobs or []) if isinstance(job, Mapping)]
    run_rows = [run for run in (runs or []) if isinstance(run, Mapping)]

    by_chapter: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for run in run_rows:
        chapter = run.get("chapter_index")
        if isinstance(chapter, int) and chapter >= 1:
            by_chapter[chapter].append(run)

    first_pass_count = 0
    committed_chapters = 0
    for chapter_runs in by_chapter.values():
        result = _chapter_first_pass(chapter_runs)
        if result is None:
            continue
        committed_chapters += 1
        if result:
            first_pass_count += 1

    recoveries = [job for job in job_rows if job.get("kind") == RECOVERY_JOB_KIND]
    recovery_completed = sum(1 for job in recoveries if job.get("state") == "completed")
    recovery_failed = sum(1 for job in recoveries if job.get("state") == "failed")

    total_input_tokens = sum(_as_int(run.get("total_input_tokens")) for run in run_rows)
    total_output_tokens = sum(_as_int(run.get("total_output_tokens")) for run in run_rows)
    priced_costs = [
        cost
        for cost in (_as_float(run.get("estimated_cost")) for run in run_rows)
        if cost is not None and cost > 0
    ]
    unpriced_with_tokens = any(
        (_as_int(run.get("total_input_tokens")) or _as_int(run.get("total_output_tokens")))
        and not ((_as_float(run.get("estimated_cost")) or 0) > 0)
        for run in run_rows
    )

    token_label = ""
    if total_input_tokens or total_output_tokens:
        token_label = (
            f"输入 {format_token_count(total_input_tokens)} · "
            f"输出 {format_token_count(total_output_tokens)}"
        )
    if priced_costs and not unpriced_with_tokens:
        cost_label = f"{sum(priced_costs):.2f}"
        estimated_cost: float | None = float(sum(priced_costs))
        cost_priced = True
    elif priced_costs and unpriced_with_tokens:
        cost_label = f"部分已计价 {sum(priced_costs):.2f}"
        if token_label:
            token_label += "；其余运行没有可用计价结果"
        estimated_cost = float(sum(priced_costs))
        cost_priced = False
    elif total_input_tokens or total_output_tokens:
        cost_label = "未计价"
        token_label += "；已记录用量，但没有可用的计价结果"
        estimated_cost = None
        cost_priced = False
    else:
        cost_label = "—"
        estimated_cost = None
        cost_priced = False

    total_runs = len(run_rows)
    listed = total_runs if listed_run_count is None else min(listed_run_count, total_runs)
    if total_runs and listed < total_runs:
        range_label = f"共 {total_runs} 次章节生成，不含安全恢复；列表显示最近 {listed} 次"
    elif total_runs:
        range_label = f"共 {total_runs} 次章节生成，不含安全恢复"
    else:
        range_label = "还没有章节生成运行"

    if committed_chapters:
        first_pass_note = (
            f"已提交 {committed_chapters} 章中，有 {first_pass_count} 章"
            "在第一次生成时未经自动修订即提交"
        )
    else:
        first_pass_note = "还没有已提交的章节生成"

    if recoveries:
        intervention_note = (
            f"安全恢复 {len(recoveries)} 次（成功 {recovery_completed}，失败 {recovery_failed}）"
        )
    else:
        intervention_note = "没有安全恢复记录"

    return {
        "total_runs": total_runs,
        "listed_run_count": listed,
        "range_label": range_label,
        "first_pass_rate": format_first_pass_rate(first_pass_count, committed_chapters),
        "first_pass_count": first_pass_count,
        "first_pass_denominator": committed_chapters,
        "first_pass_note": first_pass_note,
        "recoveries": len(recoveries),
        "recovery_completed": recovery_completed,
        "recovery_failed": recovery_failed,
        "manual_interventions": len(recoveries),
        "intervention_note": intervention_note,
        "cost_label": cost_label,
        "token_label": token_label,
        "cost_priced": cost_priced,
        "estimated_cost": estimated_cost,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }


def build_project_history_payload(
    *,
    root: Path,
    project_id: str,
    job_store: Any,
    compact_run: Callable[[Mapping[str, Any] | None], dict[str, Any] | None],
    list_limit: int = HISTORY_LIST_LIMIT,
) -> dict[str, Any]:
    all_runs = [
        {**run, **annotate_run_usage(run)}
        for run in load_compact_runs(root, compact_run)
    ]
    job_rows = load_job_stat_rows(job_store, project_id)
    listed_jobs = [
        item.to_dict()
        for item in job_store.list_jobs(project_id=project_id, limit=list_limit)
    ]
    limit = max(0, int(list_limit))
    return {
        "jobs": listed_jobs,
        "runs": all_runs[:limit],
        "stats": build_history_stats(
            jobs=job_rows,
            runs=all_runs,
            listed_run_count=min(limit, len(all_runs)),
        ),
    }
