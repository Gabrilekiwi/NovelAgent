# NovelAgent

NovelAgent is being rebuilt from an MVP single-pass generator into a v1.0 agent loop for long-form fiction generation.

Current v1.0 flow:

```text
Notion / Memory
  -> Snapshot Builder
  -> Director
  -> Execution Engine
  -> Chapter Generator
  -> Claude Polish
  -> Validator
  -> Scene Repair
  -> Snapshot / Run Artifacts
```

## Quick Start

Install dependencies:

```bash
pip install -r requirements.txt
```

Run preflight without model calls:

```bash
python main.py --check --dry-run --memory data/notion_memory.example.json
```

`--check` prints a concise summary by default. Add `--check-json` for the full preflight JSON diagnostics.

Run one local dry-run step:

```bash
python main.py --dry-run --memory data/notion_memory.example.json
```

Generation commands print a concise summary by default. Add `--output-json` for the full result or `--output-run-json` for only the run record.

Select the memory source explicitly when needed:

```bash
python main.py --check --dry-run --memory-source file --memory data/notion_memory.example.json
python main.py --check --dry-run --memory-source notion
```

Run multiple local dry-run steps:

```bash
python main.py --dry-run --steps 2 --memory data/notion_memory.example.json
```

Run tests:

```bash
python -B -m unittest discover -s tests
```

Run the local v1.0 smoke gate:

```bash
python -B scripts/smoke_v1.py
```

This runs tests, preflight, one persisted dry-run, file memory writeback, and run reporting inside `.tmp/smoke_v1/...`.

Inspect recent persisted run records:

```bash
python main.py --report-runs --run-dir data/runs
```

## Main Directories

- `core/director`: decision layer with schema-checked decisions and run audit records.
- `core/engine`: execution loop, schema-checked workflow plans and trace events, model-call stage diagnostics, run records, artifacts, preflight, and run reports.
- `core/state`: snapshot, input pack plus metadata, memory, Notion export normalization, and schema-checked state builder audit.
- `core/validator`: continuity, spatial, and logic validation with explicit requested/executed/skipped coverage metadata.
- `modules`: feature modules for generation, polish, schema-checked conflict analysis, and schema-checked repair planning.
- `api`: provider adapters for OpenAI, Claude, and Notion.
- `prompts`: prompt assets.
- `schemas`: JSON schema contracts.
- `docs`: runtime and memory documentation.

## Runtime Artifacts

Persistent runs write schema-checked run result envelopes:

- `data/runs/*.json`: structured run records with Director audit, workflow plan, state update audit, trace, model output failure diagnostics, validation, and analysis summaries.
- `data/runs/loop_sessions/*.json`: schema-checked multi-step loop session records.
- `data/runs/snapshot_packs/*.md`: Snapshot Builder input packs.
- `data/runs/input_packs/*.md`: full input packs; run records include input pack metadata.
- `data/chapters/*.md`: chapter body artifacts.

Run records, run reports, and loop session summaries expose compact validation and repair evidence, so common Validator/Repair failures can be diagnosed without opening the full run-result envelope. Both directories are ignored by git by default.

Legacy imports under `core.*` remain available as compatibility wrappers. `core.orchestrator` delegates to the v1.0 executor and supports custom snapshot, memory, run, chapter, preflight, loop, and report paths. Compatibility package exports point at the v1.0 implementations for input pack metadata, snapshot builder audit, state update audit, dynamic flow plans, feature modules, and API adapters. The root `core` package lazily exposes the v1.0 executor and orchestrator entrypoints.

The v1.0 directory layout and legacy wrapper boundaries are covered by `tests/test_repo_hygiene.py`, so structural drift is caught alongside runtime tests.

## More Docs

- [Architecture](docs/architecture.md)
- [Runtime Commands](docs/runtime.md)
- [Memory Input](docs/memory.md)
