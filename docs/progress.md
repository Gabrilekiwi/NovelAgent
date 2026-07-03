# Project Progress

Last updated: 2026-07-03

## v1.3 Status

NovelAgent is fixed at version 1.3 as the current project baseline.

The v1.3 baseline keeps the v1.2 long-form fiction loop and adds four stability safeguards for real novel production: recoverable failed drafts, project language/profile contracts, profile-backed character/location analysis, and UTF-8 snapshot maintenance tooling.

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

Phase 5, real-novel stability hardening, is complete. Problems found while advancing the riftwalker novel were addressed in code without changing the default execution path:

- Failed or rejected runs can recover the latest pre-polish draft with `python main.py --recover-latest`; recovery writes a separate chapter artifact and does not advance the snapshot.
- Snapshot `project_profile.language` drives language-aware output contracts. For `zh-CN` projects, generated scenes and Claude polish output must remain Chinese before they can be committed.
- Snapshot `project_profile.known_characters` and `project_profile.known_locations` feed the analyzer so new novels can initialize their own names and places instead of relying only on hardcoded terms.
- `scripts/snapshot_utf8.py` validates and normalizes snapshots through Python UTF-8 JSON handling, reducing the risk of PowerShell encoding corruption.

## Current Baseline Capabilities

- Runtime entrypoints: `main.py`, `core.orchestrator`, and `core.engine.executor.AgentExecutor`.
- Memory sources: normalized file memory, Notion export JSON, and live Notion database reads.
- Generation flow: Director decision, workflow plan, chapter plan, scene drafts, merged chapter, Claude polish, validation, repair, analysis, commit.
- Validation: continuity, spatial, logic, and optional OpenAI-backed story-level LLM validation.
- Repair: schema-checked repair plans, deterministic dry-run strategies, model-backed non-dry-run repair payloads, and repair effectiveness deltas.
- Artifacts: run records, loop sessions, input packs, snapshot packs, chapter pipeline files, chapter bodies, validation reports, repair deltas, and memory writeback mappings.
- Provider diagnostics: OpenAI Director, OpenAI chapter generation, Claude polish, Notion read, Notion writeback, and Notion readback.
- Contracts: schema validation for runtime records, memory contexts, workflow plans, trace events, validation results, repair plans, state updates, loop sessions, and provider reports.
- Project profile: optional snapshot-level language, known character, and known location configuration used by input packs, generation contracts, and analysis.
- Recovery tooling: explicit latest-draft recovery for failed or rejected runs.
- Snapshot maintenance: UTF-8 inspection and normalization script for runtime JSON files.

## Latest Verification

The latest verified local gates passed:

```bash
python -B -m unittest discover -s tests
python -B scripts/smoke_v1.py
```

Observed v1.3 result:

```text
Ran 364 tests ... OK
Runtime preflight for riftwalker: OK, 24 checks passed
```

## Operational Notes

Use `--no-proxy` for real provider smoke if `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` point at an unavailable local proxy.

Provider smoke writes isolated runtime state and diagnostics under `.tmp/runtime/provider_smoke/...`, so it does not pollute committed samples.

Before release tagging or handoff, review the final working tree and stage only intentional project changes. Local IDE files and Python bytecode were removed from Git tracking in commit `26980e4 Clean tracked local artifacts`.
