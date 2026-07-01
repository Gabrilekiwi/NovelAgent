from __future__ import annotations

from typing import Any

__all__ = [
    "AgentExecutor",
    "check_runtime",
    "report_runs",
    "run_agent_loop",
    "run_agent_once",
]


def __getattr__(name: str) -> Any:
    if name == "AgentExecutor":
        from core.engine.executor import AgentExecutor

        return AgentExecutor
    if name in {"check_runtime", "report_runs", "run_agent_loop", "run_agent_once"}:
        from core.orchestrator import check_runtime, report_runs, run_agent_loop, run_agent_once

        return {
            "check_runtime": check_runtime,
            "report_runs": report_runs,
            "run_agent_loop": run_agent_loop,
            "run_agent_once": run_agent_once,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
