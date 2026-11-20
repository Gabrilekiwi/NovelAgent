"""Fresh-process legacy Strict execution probe; local deterministic pipeline only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.engine.executor import AgentExecutor
from core.story_project.identity import load_project_identity
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.writer import StoryProjectWritebackConfig
from scripts.experiments.d3_restore_candidate import MachineBook, isolated


def execute(book: Path, *, owner: MachineBook, database: Path, project_id: str, generation: int) -> dict:
    book = isolated(book)
    permit = owner.permit(book, database, project_id, generation=generation)
    run_dir = isolated(Path(permit["run_dir"]))
    if not run_dir.is_relative_to(book):
        raise ValueError("D3 execution directory must stay inside fixture book")
    # This is an experiment, never a command for real works.
    if not book.is_relative_to(REPO / ".tmp/stage-b"):
        raise ValueError("D3 executor only accepts isolated stage-b fixtures")
    identity = load_project_identity(book)
    if identity is None or not identity.book_id.startswith("synthetic-"):
        raise ValueError("D3 executor requires synthetic identity")
    if identity.story_state_mode != "strict" or identity.authority["mode"] != "legacy_markdown_v1":
        raise ValueError("D3 executor must exercise legacy Strict")
    runtime = book / ".novelagent/runtime"
    snapshot = json.loads((runtime / "snapshot.json").read_text(encoding="utf-8"))
    contexts = []
    delegate = build_generation_story_project_context_loader(
        story_project=book, chapter=snapshot["chapter_index"], project_identity=identity)

    class RecordingLoader:
        story_project_root = book

        def __call__(self, snapshot, memory_context, chapter_hint=None):
            context = delegate(snapshot, memory_context, chapter_hint)
            contexts.append(context.to_dict())
            return context

    # A network attempt fails the probe; do not patch validation/persistence guards.
    with patch.object(socket.socket, "connect", side_effect=AssertionError("D3 network forbidden")):
        result = AgentExecutor(
            snapshot_path=runtime / "snapshot.json", memory_path=runtime / "d3-memory.json",
            memory_source="file", memory_loader=lambda: {},
            run_dir=run_dir, chapter_dir=runtime / "chapters",
            dry_run=True, story_project_context_loader=RecordingLoader(),
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal", enable_execution_provenance=False,
        ).run_once(persist=True)
    owner.permit(book, database, project_id, generation=generation)
    return {"result":result, "contexts":contexts, "book_id":identity.book_id,
            "story_state_mode":identity.story_state_mode, "authority":identity.authority,
            "paid_provider_calls":0, "generation":"local_deterministic_pipeline"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--machine-store", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    owner = MachineBook(args.machine_store, load_project_identity(args.book).book_id)
    with owner.locked():
        outcome = execute(args.book, owner=owner, database=args.database,
                          project_id=args.project_id, generation=args.generation)
    args.output.write_text(json.dumps(outcome, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"committed":outcome["result"]["committed"], "output":str(args.output)}))
