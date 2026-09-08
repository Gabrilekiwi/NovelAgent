from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.engine.locked_chapter_state import resolved_execution_ids
from core.engine.chapter_candidate_resolution import (
    abandoned_provider_attempts,
    provider_attempt_binding,
)
from core.engine.chapter_fact_extraction_resolution import (
    abandoned_fact_extraction_provider_attempts,
    fact_extraction_provider_attempt_binding,
)
from core.engine.persistence import persistence_run_lock
from core.model_calls import ModelCallStore


CANDIDATE_EXECUTION_PREFIX = "execution_candidate_"
FACT_EXTRACTION_EXECUTION_PREFIX = "execution_fx_"


@contextmanager
def provider_execution_lock(run_dir: str | Path) -> Iterator[None]:
    """Serialize the project-wide check/Intent/Provider boundary."""

    lock_root = Path(run_dir).resolve() / ".provider-boundary-lock"
    with persistence_run_lock(lock_root):
        yield


def list_unresolved_provider_calls(run_dir: str | Path) -> list[dict[str, Any]]:
    """Return unresolved provider attempts for every live project execution.

    An Intent without a Receipt may already have crossed the provider boundary.
    Callers must treat any returned item as a project-wide stop condition before
    starting another physical provider request.
    """

    runtime_runs = Path(run_dir)
    executions_root = runtime_runs / "executions"
    if not executions_root.is_dir():
        return []

    handled = resolved_execution_ids(runtime_runs)
    abandoned_attempts = abandoned_provider_attempts(runtime_runs)
    abandoned_fact_attempts = abandoned_fact_extraction_provider_attempts(
        runtime_runs,
        runtime_root=runtime_runs.parent,
    )
    unresolved: list[dict[str, Any]] = []
    for execution_dir in sorted(executions_root.glob("execution_*")):
        candidate_execution = execution_dir.name.startswith(CANDIDATE_EXECUTION_PREFIX)
        fact_extraction_execution = execution_dir.name.startswith(
            FACT_EXTRACTION_EXECUTION_PREFIX
        )
        if (
            execution_dir.name in handled
            and not candidate_execution
            and not fact_extraction_execution
        ):
            continue
        model_root = execution_dir / "model_calls"
        if not model_root.is_dir():
            continue
        for call in ModelCallStore(model_root).list_uncertain_calls():
            if candidate_execution and provider_attempt_binding(
                execution_dir.name,
                call,
            ) in abandoned_attempts:
                continue
            if fact_extraction_execution and fact_extraction_provider_attempt_binding(
                execution_dir.name,
                call,
            ) in abandoned_fact_attempts:
                continue
            unresolved.append(
                {
                    **call,
                    "execution_id": execution_dir.name,
                }
            )
    return sorted(
        unresolved,
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("execution_id") or ""),
            str(item.get("attempt_id") or ""),
        ),
    )


__all__ = [
    "CANDIDATE_EXECUTION_PREFIX",
    "FACT_EXTRACTION_EXECUTION_PREFIX",
    "list_unresolved_provider_calls",
    "provider_execution_lock",
]
