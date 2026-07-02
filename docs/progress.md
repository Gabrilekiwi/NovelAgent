# Project Progress

Last updated: 2026-07-02

## v1.2 Status

NovelAgent is fixed at version 1.2 as the current project baseline.

The v1.2 baseline is a working long-form fiction agent loop with memory ingestion, directed execution, chapter planning, scene drafting, merge, polish, deterministic validation, optional LLM validation, repair, snapshot updates, memory writeback, provider diagnostics, and schema-checked runtime artifacts.

The project remains source-compatible with the established v1.0 command and file names where those names are part of the existing developer workflow, such as `scripts/smoke_v1.py` and `.tmp/smoke_v1/...`.

## Completed Scope

Phase 1, engineering stabilization, is complete. Committed sample state is separated from mutable runtime state. Sample files live at `data/snapshot.example.json` and `data/notion_memory.example.json`; default runtime files live under `.tmp/runtime/`. The CLI includes `python main.py --init-runtime`, `.env.example` contains only variable names and recommended default models, `.env` remains ignored, and CI runs the unit suite plus `scripts/smoke_v1.py`.

Phase 2, validation layering, is complete. Rule validation remains the required hard gate for continuity, spatial, and logic checks. The LLM validator is optional, disabled by default for dry-run and CI, and schema-checks output before merging `validator="llm"` problems into `validation.problems[]` with evidence, severity, repair hints, and area metadata.

Phase 3, generation pipeline split, is complete. Chapter generation is split into `plan_chapter`, `generate_scenes`, `merge_scenes`, `validate`, `repair`, and `commit`. Runtime records and artifacts include the chapter plan, scene drafts, merged chapter, validation report, repair deltas, and scene span metadata.

Phase 4, real provider smoke, is complete. The successful acceptance run used `--no-proxy` because the local proxy environment previously pointed provider SDK traffic at a dead proxy. The real smoke report passed all required checks:

- OpenAI Director
- OpenAI chapter generation
- Claude polish
- Notion read
- Notion writeback
- Notion readback

Report path:

```text
.tmp/runtime/provider_smoke/phase4_full_no_proxy_check/provider_smoke_report.json
```

## Current Baseline Capabilities

- Runtime entrypoints: `main.py`, `core.orchestrator`, and `core.engine.executor.AgentExecutor`.
- Memory sources: normalized file memory, Notion export JSON, and live Notion database reads.
- Generation flow: Director decision, workflow plan, chapter plan, scene drafts, merged chapter, Claude polish, validation, repair, analysis, commit.
- Validation: continuity, spatial, logic, and optional OpenAI-backed story-level LLM validation.
- Repair: schema-checked repair plans, deterministic dry-run strategies, model-backed non-dry-run repair payloads, and repair effectiveness deltas.
- Artifacts: run records, loop sessions, input packs, snapshot packs, chapter pipeline files, chapter bodies, validation reports, repair deltas, and memory writeback mappings.
- Provider diagnostics: OpenAI Director, OpenAI chapter generation, Claude polish, Notion read, Notion writeback, and Notion readback.
- Contracts: schema validation for runtime records, memory contexts, workflow plans, trace events, validation results, repair plans, state updates, loop sessions, and provider reports.

## Latest Verification

The latest verified local gates passed:

```bash
python -B -m unittest discover -s tests
python -B scripts/smoke_v1.py
```

Observed result:

```text
Ran 342 tests ... OK
Smoke v1: OK
```

## Operational Notes

Use `--no-proxy` for real provider smoke if `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` point at an unavailable local proxy.

Provider smoke writes isolated runtime state and diagnostics under `.tmp/runtime/provider_smoke/...`, so it does not pollute committed samples.

Before release tagging or handoff, review the final working tree and stage only intentional project changes. Local IDE files and Python bytecode were removed from Git tracking in commit `26980e4 Clean tracked local artifacts`.
