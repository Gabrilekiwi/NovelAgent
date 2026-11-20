from __future__ import annotations

import json
from pathlib import Path

from tests.test_workbench_app import _client, _create_project, _unlock
from workbench.history_stats import (
    annotate_run_usage,
    build_history_stats,
    build_project_history_payload,
    format_first_pass_rate,
)


STATIC = Path(__file__).resolve().parents[1] / "workbench" / "static"
TEMPLATE = Path(__file__).resolve().parents[1] / "workbench" / "templates" / "history.html"


def _run(
    chapter: int,
    *,
    run_id: str,
    started_at: str,
    committed: bool = False,
    status: str = "failed",
    repair_attempts: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost: float = 0.0,
) -> dict:
    return {
        "id": run_id,
        "chapter_index": chapter,
        "status": "committed" if committed else status,
        "committed": committed,
        "started_at": started_at,
        "repair_attempts": repair_attempts,
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "estimated_cost": estimated_cost,
    }


def test_first_pass_ignores_retries_and_in_run_repairs() -> None:
    """First-pass is first committed run with zero repairs, not later clean commits."""
    runs = [
        _run(1, run_id="c1", started_at="2026-01-01T10:00:00+08:00", committed=True, repair_attempts=0),
        _run(2, run_id="c2a", started_at="2026-01-01T11:00:00+08:00", committed=True, repair_attempts=1),
        _run(3, run_id="c3a", started_at="2026-01-01T12:00:00+08:00", status="failed"),
        _run(3, run_id="c3b", started_at="2026-01-01T13:00:00+08:00", committed=True, repair_attempts=0),
    ]
    jobs = [
        {"kind": "generation_session", "state": "cancelled", "manual_confirmation_required": True},
        {"kind": "generation_session", "state": "cancelled", "manual_confirmation_required": True},
        {"kind": "locked_chapter_recovery", "state": "completed", "manual_confirmation_required": False},
        {"kind": "locked_chapter_recovery", "state": "failed", "manual_confirmation_required": False},
    ]
    stats = build_history_stats(jobs=jobs, runs=runs, listed_run_count=100)
    assert stats["total_runs"] == 4
    assert stats["first_pass_count"] == 1
    assert stats["first_pass_denominator"] == 3
    assert stats["first_pass_rate"] == format_first_pass_rate(1, 3)
    assert "第一次生成时未经自动修订即提交" in stats["first_pass_note"]
    assert stats["recoveries"] == 2
    assert stats["recovery_completed"] == 1
    assert stats["recovery_failed"] == 1
    assert stats["manual_interventions"] == 2
    assert "安全恢复 2 次" in stats["intervention_note"]
    assert stats["cost_label"] == "—"


def test_total_runs_is_chapter_runs_not_jobs_plus_runs() -> None:
    jobs = [
        {"kind": "generation_session", "state": "completed", "manual_confirmation_required": False},
        {"kind": "locked_chapter_recovery", "state": "completed", "manual_confirmation_required": False},
    ]
    runs = [
        _run(1, run_id="c1", started_at="2026-01-01T10:00:00+08:00", committed=True),
    ]
    stats = build_history_stats(jobs=jobs, runs=runs)
    assert stats["total_runs"] == 1
    assert stats["total_runs"] != len(jobs) + len(runs)
    assert "不含安全恢复" in stats["range_label"]


def test_unpriced_tokens_are_shown_without_inventing_a_cost() -> None:
    runs = [
        _run(
            1,
            run_id="c1",
            started_at="2026-01-01T10:00:00+08:00",
            committed=True,
            input_tokens=12056784,
            output_tokens=1444861,
            estimated_cost=0.0,
        )
    ]
    stats = build_history_stats(runs=runs)
    assert stats["cost_label"] == "未计价"
    assert stats["cost_priced"] is False
    assert stats["estimated_cost"] is None
    assert stats["token_label"] == (
        "输入 12,056,784 · 输出 1,444,861；已记录用量，但没有可用的计价结果"
    )
    assert annotate_run_usage(runs[0]) == {
        "token_label": "12,056,784 / 1,444,861",
        "cost_label": "未计价",
    }


def test_priced_and_mixed_cost_labels_do_not_invent_currency() -> None:
    priced = build_history_stats(
        runs=[
            _run(
                1,
                run_id="c1",
                started_at="2026-01-01T10:00:00+08:00",
                committed=True,
                input_tokens=10,
                output_tokens=5,
                estimated_cost=1.25,
            ),
            _run(
                2,
                run_id="c2",
                started_at="2026-01-01T11:00:00+08:00",
                committed=True,
                input_tokens=20,
                output_tokens=5,
                estimated_cost=0.75,
            ),
        ]
    )
    assert priced["cost_label"] == "2.00"
    assert priced["estimated_cost"] == 2.0
    assert priced["cost_priced"] is True
    assert "输入 30" in priced["token_label"]

    mixed = build_history_stats(
        runs=[
            _run(
                1,
                run_id="c1",
                started_at="2026-01-01T10:00:00+08:00",
                committed=True,
                input_tokens=10,
                output_tokens=5,
                estimated_cost=1.25,
            ),
            _run(
                2,
                run_id="c2",
                started_at="2026-01-01T11:00:00+08:00",
                committed=True,
                input_tokens=20,
                output_tokens=5,
                estimated_cost=0.0,
            ),
        ]
    )
    assert mixed["cost_label"] == "部分已计价 1.25"
    assert mixed["cost_priced"] is False
    assert "没有可用计价结果" in mixed["token_label"]


def test_history_list_is_windowed_but_stats_use_all_runs(tmp_path: Path) -> None:
    root = tmp_path / "book"
    run_dir = root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)

    def compact(payload):
        run = payload["run"]
        return {
            "id": run["id"],
            "chapter_index": run["chapter_index"],
            "status": run["status"],
            "committed": run["committed"],
            "started_at": run["started_at"],
            "repair_attempts": run.get("repair_attempts", 0),
            "total_input_tokens": 100,
            "total_output_tokens": 10,
            "estimated_cost": 0.0,
        }

    for index in range(1, 106):
        hour = index
        started = f"2026-01-01T{hour:02d}:00:00+08:00" if hour <= 23 else f"2026-01-02T{hour - 24:02d}:00:00+08:00"
        if hour > 47:
            started = f"2026-01-03T{hour - 48:02d}:00:00+08:00"
        payload = {
            "run": {
                "id": f"chapter_{index}_run",
                "chapter_index": index,
                "status": "committed",
                "committed": True,
                "started_at": started,
                "repair_attempts": 0 if index == 1 else 1,
            }
        }
        (run_dir / f"chapter_{index}_run.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    class _Job:
        def to_dict(self):
            return {"kind": "generation_session", "state": "completed"}

    class _Store:
        database = None

        def list_jobs(self, *, project_id, limit):
            return [_Job()]

    payload = build_project_history_payload(
        root=root,
        project_id="project_test",
        job_store=_Store(),
        compact_run=compact,
        list_limit=100,
    )
    assert len(payload["runs"]) == 100
    assert payload["stats"]["total_runs"] == 105
    assert payload["stats"]["first_pass_count"] == 1
    assert payload["stats"]["first_pass_denominator"] == 105
    assert payload["stats"]["cost_label"] == "未计价"
    assert payload["runs"][0]["cost_label"] == "未计价"
    assert "列表显示最近 100 次" in payload["stats"]["range_label"]


def test_history_api_returns_author_stats_instead_of_frontend_fallback(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="历史统计测试")
    run_dir = Path(project["root"]) / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)

    def write_run(name: str, body: dict) -> None:
        (run_dir / name).write_text(json.dumps({"run": body}, ensure_ascii=False), encoding="utf-8")

    write_run(
        "chapter_1_ok.json",
        {
            "id": "chapter_1_ok",
            "chapter_index": 1,
            "status": "committed",
            "committed": True,
            "started_at": "2026-01-01T10:00:00+08:00",
            "repair_attempts": 0,
            "execution_evidence": {
                "budget": {
                    "total_input_tokens": 100,
                    "total_output_tokens": 20,
                    "estimated_cost": 0.0,
                }
            },
        },
    )
    write_run(
        "chapter_2_fail.json",
        {
            "id": "chapter_2_fail",
            "chapter_index": 2,
            "status": "failed",
            "committed": False,
            "started_at": "2026-01-01T11:00:00+08:00",
            "repair_attempts": 0,
            "execution_evidence": {
                "budget": {
                    "total_input_tokens": 50,
                    "total_output_tokens": 10,
                    "estimated_cost": 0.0,
                }
            },
        },
    )
    write_run(
        "chapter_2_ok.json",
        {
            "id": "chapter_2_ok",
            "chapter_index": 2,
            "status": "committed",
            "committed": True,
            "started_at": "2026-01-01T12:00:00+08:00",
            "repair_attempts": 0,
            "execution_evidence": {
                "budget": {
                    "total_input_tokens": 50,
                    "total_output_tokens": 10,
                    "estimated_cost": 0.0,
                }
            },
        },
    )

    response = client.get(f"/api/projects/{project['id']}/history")
    assert response.status_code == 200, response.text
    payload = response.json()
    stats = payload["stats"]
    assert stats["total_runs"] == 3
    assert stats["first_pass_count"] == 1
    assert stats["first_pass_denominator"] == 2
    assert stats["first_pass_rate"] == format_first_pass_rate(1, 2)
    assert stats["recoveries"] == 0
    assert stats["cost_label"] == "未计价"
    assert stats["total_input_tokens"] == 200
    assert stats["total_output_tokens"] == 40
    assert all(run["cost_label"] == "未计价" for run in payload["runs"])


def test_history_ui_uses_api_stats_and_does_not_invent_fallbacks() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    css = (STATIC / "workbench.css").read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "data-history-total-note" in html
    assert "data-history-pass-note" in html
    assert "data-history-recoveries" in html
    assert "安全恢复" in html
    assert "用量与费用" in html
    history_block = html.split('data-spa-view="history"', 1)[1][:2500]
    assert "data-history-manual" not in history_block
    assert "人工介入" not in history_block
    assert "估算总费用" not in history_block

    render = spa.split("const renderHistory = (payload) => {", 1)[1].split("const loadHistory = async", 1)[0]
    assert "completed / jobRecords.length" not in render
    assert "jobRecords.filter((job) => job.manual_confirmation_required)" not in render
    assert "state.runs.length" not in render
    assert "stats.total_runs" in render
    assert "stats.first_pass_rate" in render
    assert "stats.recoveries" in render
    assert "stats.cost_label" in render
    assert "data-history-pass-note" in render
    assert "data-history-recoveries" in render

    css_note = css.split(".history-metrics .metric-note", 1)[1][:200]
    assert "white-space: normal" in css_note

    assert "安全恢复" in template
    assert "用量与费用" in template
    assert "stats.recoveries" in template
