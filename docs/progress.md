# Project Progress

Last updated: 2026-07-15

Current reliability/autonomy claims are governed by the four-level [Reliability and autonomy capability status](reliability-autonomy-capability-status.md): code exists, main-path integration, default enablement, and real verification. Synthetic verification is recorded separately and never promoted to real evidence. Historical completion notes below do not imply that the new autonomy path is default-enabled or real-verified. See [Real autonomy E2E release gates](real-autonomy-e2e.md) for the separate billable gate.

Current upgrade evidence snapshot:

- **Code/integration:** the explicit `--remap-roots` CLI can update an existing EA-global/StoryProject logical data-root binding only when no transaction is pending and no session is active. It requires the existing root UUID plus revision/digest CAS and does not move data. It is not a unified relocation path because the StoryProject-embedded Persistence v2 runtime control plane is not moved. `RootRegistry` remains the unique mutable EA physical-root mapping, while immutable historical manifests may retain absolute-path snapshots.
- **Synthetic verification:** a complete 50-chapter deterministic autonomy simulation passed against code commit `1a2af7d` in 1374.619 seconds, with 50 completed chapters, 50 executor requests, 50 required File Delivery JSON artifacts, and 49 linear next-target Arc adjustments. See the [retained evidence report](reliability-autonomy-50-chapter-evidence.json). This closes only the synthetic long-run gate.
- **Real verification:** one exactly authorized one-chapter attempt entered the real runner/provider path on 2026-07-15 but failed before chapter completion; it is not a successful gate. The four-chapter canary was not started, and no successful 1/4/10/20+ report exists. The hardened harness disables workspace `.env` loading, requires a clean commit and an explicit proxy choice, and retains future redacted failure diagnostics. No Notion call was made in this upgrade run.
- **Default enablement:** autonomy and event-authority migration remain explicit/conditional. Neither default autonomous generation nor default end-to-end whole-project movement is claimed.

## Reliable semantic production status

The code and local-test scope of commits 1–14 in `docs/reliable-semantic-production-plan.md` is complete. The current StoryProject default remains shadow. Strict authority now requires a qualified target-book calibration report and explicit activation; the committed synthetic fixtures do not qualify a real target book. Strict execution is fail-closed on pinned profile drift and requires transactional apply writeback for persistent runs.

The offline two-chapter strict integration gate proves that chapter N+1 reads chapter N prose, managed semantic projection, and the incremented replayable Memory V2.1 revision. The billable real OpenAI two-chapter gate is present as an explicit opt-in and remains unexecuted unless a redacted real sample, its qualified calibration report, and provider credentials are supplied.

Commit 14 is complete. The final behavior-preserving split introduced `StoryProjectContextService`, `QualityCoordinator`, `PersistenceCoordinator`, and `DeliveryCoordinator`, plus `core.cli` argument/config/command/output modules. `AgentExecutor`, `core.engine`, and `main.py` retain compatibility entrypoints and re-exports.

## v1.5 Status

NovelAgent is fixed at version 1.5 as the current project baseline.

The v1.5 baseline keeps the v1.4 real-provider safeguards and adds the next usability/stability layer for long real-novel runs: high-confidence mojibake output rejection, one-shot chapter-plan JSON repair, multi-step progress lines, per-step loop timing artifacts, run-report timing summaries, and Notion shortcut flags for live memory and read/write/readback workflows.

The project remains source-compatible with the established v1.0 command and file names where those names are part of the existing developer workflow, such as `scripts/smoke_v1.py` and `.tmp/smoke_v1/...`.

## Completed Scope

Phase 1, engineering stabilization, is complete. Committed sample state is separated from mutable runtime state. Sample files live at `data/snapshot.example.json` and `data/notion_memory.example.json`; default runtime files live under `.tmp/runtime/`. The CLI includes `python main.py --init-runtime`, `.env.example` contains only variable names and recommended default models, `.env` remains ignored, and CI runs the unit suite plus `scripts/smoke_v1.py`.

Phase 2, validation layering, is complete. Rule validation remains the required hard gate for continuity, spatial, and logic checks. The LLM validator is optional, disabled by default for dry-run and CI, and schema-checks output before merging `validator="llm"` problems into `validation.problems[]` with evidence, severity, repair hints, and area metadata.

Phase 3, generation pipeline split, is complete. Chapter generation is split into `plan_chapter`, `generate_scenes`, `merge_scenes`, `validate`, `repair`, and `commit`. Runtime records and artifacts include the chapter plan, scene drafts, merged chapter, validation report, repair deltas, and scene span metadata.

Phase 4 records a historical v1.5 provider-smoke run. It is not evidence for the current event-authority/autonomy 1/4/10/20+ gates. At the time, the acceptance run used `--no-proxy` because the local proxy environment previously pointed provider SDK traffic at a dead proxy, and its report recorded these checks:

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

Phase 6, v1.5 operational usability, is complete. Problems found during real multi-step provider runs were addressed:

- High-confidence Latin-1/replacement-character mojibake output is rejected by the shared model-output contract before chapter text can be committed.
- CJK mojibake signatures are exposed through diagnostics for auditing without blindly rejecting rare valid fantasy terms.
- Chapter-plan JSON failures get one model-backed repair request with a JSON-only schema instruction before the run is failed.
- Multi-step loops emit progress lines to stderr for concise runs and record `duration_ms` in trace events plus `step_timings[]` in loop sessions.
- `--report-runs` surfaces loop step timing summaries for provider latency and stall triage.
- `--notion-memory` and `--notion-sync` reduce the command surface for live Notion memory input and writeback/readback workflows.

## Current Baseline Capabilities

- Runtime entrypoints: `main.py`, `core.orchestrator`, and `core.engine.executor.AgentExecutor`.
- Memory sources: normalized file memory, Notion export JSON, and live Notion database reads.
- Generation flow: Director decision, workflow plan, chapter plan, scene drafts, merged chapter, Claude polish, validation, repair, analysis, commit.
- Validation: continuity, spatial, logic, and optional OpenAI-backed story-level LLM validation.
- Repair: schema-checked repair plans, deterministic dry-run strategies, model-backed non-dry-run repair payloads, and repair effectiveness deltas.
- Artifacts: run records, loop sessions, input packs, snapshot packs, chapter pipeline files, chapter bodies, validation reports, repair deltas, and memory writeback mappings.
- Loop observability: stderr progress for long concise loops, trace event durations, loop-session step timings, and run-report timing summaries.
- Provider diagnostics: OpenAI Director, OpenAI chapter generation, Claude polish, Notion read, Notion writeback, and Notion readback.
- Contracts: schema validation for runtime records, memory contexts, workflow plans, trace events, validation results, repair plans, state updates, loop sessions, and provider reports.
- Project profile: optional snapshot-level language, known character, and known location configuration used by input packs, generation contracts, and analysis.
- Recovery tooling: explicit latest-draft recovery for failed or rejected runs.
- Snapshot maintenance: UTF-8 inspection and normalization script for runtime JSON files.

## Earlier baseline verification

The following result was recorded for the earlier v1.5 baseline. It is not the current reliability/autonomy acceptance result and must not be used as real-autonomy evidence:

```bash
python -m pytest -p no:cacheprovider
python -B -m unittest discover -s tests
python scripts/smoke_v1.py --skip-tests
```

Observed final result:

```text
891 passed, 2 skipped
Ran 893 tests ... OK (skipped=2)
Smoke v1: OK; preflight 20 passed, 0 failed
```

## Operational Notes

Use `--no-proxy` for real provider smoke if `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` point at an unavailable local proxy.

Provider smoke writes isolated runtime state and diagnostics under `.tmp/runtime/provider_smoke/...`, so it does not pollute committed samples.

Before release tagging or handoff, review the final working tree and stage only intentional project changes. Local IDE files and Python bytecode were removed from Git tracking in commit `26980e4 Clean tracked local artifacts`.
