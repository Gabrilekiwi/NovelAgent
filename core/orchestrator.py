from __future__ import annotations

from pathlib import Path
from typing import Any

from core.director import ModelDirector
from core.engine.executor import AgentExecutor
from core.engine.preflight import run_preflight
from core.engine.report import build_run_report
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH
from core.state.memory_writer import build_memory_writer


def run_once(
    *,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    memory_path: str | Path | None = None,
    memory_source: str = "auto",
    run_dir: str | Path = DEFAULT_RUN_DIR,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    dry_run: bool = False,
    persist: bool = True,
    director_model: str | None = None,
    enable_llm_validator: bool = False,
    memory_writeback: str = "none",
    memory_outbox: str | Path | None = None,
    notion_readback: bool = False,
) -> str:
    result = run_agent_once(
        snapshot_path=snapshot_path,
        memory_path=memory_path,
        memory_source=memory_source,
        run_dir=run_dir,
        chapter_dir=chapter_dir,
        dry_run=dry_run,
        persist=persist,
        director_model=director_model,
        enable_llm_validator=enable_llm_validator,
        memory_writeback=memory_writeback,
        memory_outbox=memory_outbox,
        notion_readback=notion_readback,
    )
    return result["chapter"]


def run_agent_once(
    *,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    memory_path: str | Path | None = None,
    memory_source: str = "auto",
    run_dir: str | Path = DEFAULT_RUN_DIR,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    dry_run: bool = False,
    persist: bool = True,
    director_model: str | None = None,
    enable_llm_validator: bool = False,
    memory_writeback: str = "none",
    memory_outbox: str | Path | None = None,
    notion_readback: bool = False,
    use_run_history: bool = True,
) -> dict[str, Any]:
    return _build_executor(
        snapshot_path=snapshot_path,
        memory_path=memory_path,
        memory_source=memory_source,
        run_dir=run_dir,
        chapter_dir=chapter_dir,
        dry_run=dry_run,
        director_model=director_model,
        enable_llm_validator=enable_llm_validator,
        memory_writeback=memory_writeback,
        memory_outbox=memory_outbox,
        notion_readback=notion_readback,
        use_run_history=use_run_history,
    ).run_once(persist=persist)


def run_agent_loop(
    *,
    steps: int,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    memory_path: str | Path | None = None,
    memory_source: str = "auto",
    run_dir: str | Path = DEFAULT_RUN_DIR,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    dry_run: bool = False,
    persist: bool = True,
    stop_on_rejection: bool = True,
    director_model: str | None = None,
    enable_llm_validator: bool = False,
    memory_writeback: str = "none",
    memory_outbox: str | Path | None = None,
    notion_readback: bool = False,
    use_run_history: bool = True,
) -> dict[str, Any]:
    return _build_executor(
        snapshot_path=snapshot_path,
        memory_path=memory_path,
        memory_source=memory_source,
        run_dir=run_dir,
        chapter_dir=chapter_dir,
        dry_run=dry_run,
        director_model=director_model,
        enable_llm_validator=enable_llm_validator,
        memory_writeback=memory_writeback,
        memory_outbox=memory_outbox,
        notion_readback=notion_readback,
        use_run_history=use_run_history,
    ).run_loop(steps=steps, persist=persist, stop_on_rejection=stop_on_rejection)


def check_runtime(
    *,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    memory_path: str | Path | None = None,
    memory_source: str = "auto",
    run_dir: str | Path = DEFAULT_RUN_DIR,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    dry_run: bool = False,
    require_claude: bool = False,
    director_model: str | None = None,
    enable_llm_validator: bool = False,
    memory_writeback: str = "none",
    memory_outbox: str | Path | None = None,
    notion_readback: bool = False,
    persist: bool | None = None,
    steps: int = 1,
    continue_on_rejection: bool = False,
) -> dict[str, Any]:
    return run_preflight(
        snapshot_path=snapshot_path,
        memory_path=memory_path,
        memory_source=memory_source,
        run_dir=run_dir,
        chapter_dir=chapter_dir,
        dry_run=dry_run,
        require_claude=require_claude,
        director_model=director_model,
        enable_llm_validator=enable_llm_validator,
        memory_writeback=memory_writeback,
        memory_outbox=memory_outbox,
        notion_readback=notion_readback,
        persist=persist,
        steps=steps,
        continue_on_rejection=continue_on_rejection,
    )


def report_runs(
    *,
    run_dir: str | Path = DEFAULT_RUN_DIR,
    limit: int | None = 5,
) -> dict[str, Any]:
    return build_run_report(run_dir=run_dir, limit=limit)


def _build_executor(
    *,
    snapshot_path: str | Path,
    memory_path: str | Path | None,
    memory_source: str,
    run_dir: str | Path,
    chapter_dir: str | Path,
    dry_run: bool,
    director_model: str | None,
    enable_llm_validator: bool,
    memory_writeback: str,
    memory_outbox: str | Path | None,
    notion_readback: bool,
    use_run_history: bool,
) -> AgentExecutor:
    return AgentExecutor(
        snapshot_path=snapshot_path,
        memory_path=memory_path,
        memory_source=memory_source,
        run_dir=run_dir,
        chapter_dir=chapter_dir,
        dry_run=dry_run,
        enable_llm_validator=enable_llm_validator,
        use_run_history=use_run_history,
        director=ModelDirector(model=director_model) if director_model else None,
        memory_writer=build_memory_writer(
            mode=memory_writeback,
            outbox_path=memory_outbox,
            notion_readback=notion_readback,
        ),
    )


__all__ = [
    "check_runtime",
    "report_runs",
    "run_agent_loop",
    "run_agent_once",
    "run_once",
]
