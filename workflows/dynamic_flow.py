from __future__ import annotations

from typing import Any

from core.engine.workflow import build_workflow, build_workflow_plan


def build_dynamic_flow(decision: dict[str, Any]) -> list[str]:
    return build_workflow(decision)


def build_dynamic_flow_plan(decision: dict[str, Any]) -> dict[str, Any]:
    return build_workflow_plan(decision)


__all__ = ["build_dynamic_flow", "build_dynamic_flow_plan"]
