# NovelAgent

Version: 1.5 compatibility baseline with opt-in reliable semantic production

NovelAgent keeps the v1.5-compatible agent loop and adds an opt-in reliable semantic production path for StoryProject: calibrated semantic authority, field provenance, managed tracking projections, Memory V2.1 replay, receipt-backed persistence, durable delivery, and unified provider retry policy.

Reliability/autonomy claims are tracked by four separate levels—code exists, main-path integration, default enablement, and real verification—in the [Reliability and autonomy capability status](docs/reliability-autonomy-capability-status.md). Synthetic evidence is listed separately and is never treated as real-provider evidence. A complete 50-chapter deterministic simulation passed against code commit `bfa3d04` in 1298.864 seconds; the [retained evidence report](docs/reliability-autonomy-50-chapter-evidence.json) closes only the synthetic long-run gate and does not enable autonomy by default. The same clean commit passed 1317 unit tests (7 platform skips) and v1 smoke with 21/21 preflight checks. The billable real single-chapter, four-chapter, ten-chapter, and 20-or-more-chapter checks are retained as [operator-run post-goal validation](docs/real-autonomy-e2e.md), not as current Codex goal work or completion conditions. The operator will run them manually after the goal and give Codex only the redacted reports for analysis. The first single-chapter attempt on 2026-07-15 failed before chapter completion; no successful real report exists, so real verification remains absent and autonomy remains opt-in. No Notion call was made in this upgrade run.

Legacy-book event-authority migration is paused and outside the current Codex goal as of 2026-07-16. Its existing implementation, schemas, tests, documentation, and read-only preview history are retained; no migration decisions, `MigrationApproval`, execution, or activation will be performed. The active legacy book remains unmigrated. Resuming later requires an explicit operator decision and a fresh read-only preview against the then-current files.

Project movement is conditional, not a default capability. After an operator performs a same-volume directory rename, the explicit `--remap-roots` CLI can verify the preserved StoryProject and logical-root directory identities and forward-rebind every recognized embedded registry, with the main registry last. It requires UUID plus revision/digest CAS, pre-armed locks, an absent old path, and no pending/recovery transaction or active session/lease. The command never moves or copies data and refuses cross-volume or copy-delete moves, case-only rebinding, replaced roots, unknown registries, and external mutable EA transaction roots. This is locally tested fail-closed behavior, not real target-project verification or default portability; immutable historical manifests may still retain absolute-path snapshots.

Current v1.5 flow:

```text
Notion / Memory + StoryProject shadow/strict semantic state
  -> Snapshot Builder
  -> Director
  -> Execution Engine
  -> Chapter Pipeline (plan -> scenes -> merge)
  -> Claude Polish
  -> Language / Meta-output / Mojibake Contracts
  -> Rule Validator
  -> optional LLM Validator
  -> Scene Repair
  -> Snapshot / Run Artifacts
  -> transactional StoryProject + Memory V2 persistence
  -> durable delivery queue
```

## Quick Start

Requires Python 3.12 or newer.

Install dependencies:

```bash
pip install -r requirements.txt
```

Run preflight without model calls:

```bash
python main.py --init-runtime
python main.py --check --dry-run --memory data/notion_memory.example.json
```

`--check` prints a concise summary by default. Add `--check-json` for the full preflight JSON diagnostics.

Run one local dry-run step:

```bash
python main.py --init-runtime
python main.py --dry-run --memory data/notion_memory.example.json
```

Generation commands print a concise summary by default. Add `--output-json` for the full result or `--output-run-json` for only the run record. If Claude polish fails after base chapter generation, the run continues with the unpolished generated chapter and records the polish error in the run trace.
If local proxy environment variables interfere with provider API calls, run with `--no-proxy` or set `NOVELAGENT_NO_PROXY=1` in `.env` to clear `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` for the NovelAgent process.
High-confidence mojibake output is rejected before it can be committed as chapter prose. If OpenAI returns an invalid chapter-plan JSON payload, the pipeline makes one schema-focused JSON repair request before failing the generation step.

Select the memory source explicitly when needed:

```bash
python main.py --check --dry-run --memory-source file --memory data/notion_memory.example.json
python main.py --check --dry-run --memory-source notion
python main.py --check --dry-run --notion-memory
```

Use `--notion-sync` when a real run should read live Notion memory, write committed memory updates back to Notion, and verify them with readback:

```bash
python main.py --notion-sync --no-proxy
```

Run multiple local dry-run steps:

```bash
python main.py --dry-run --steps 2 --memory data/notion_memory.example.json
```

These steps advance a loop-local Snapshot only. Each accepted result has `status="preview"`, while the on-disk Snapshot, Memory, and run directory remain unchanged.

Run multiple StoryProject chapters with real transactional writeback:

```bash
python main.py --story-project auto --chapter 2 --steps 2 --story-project-writeback
```

StoryProject multi-step mode requires real writeback. Context, previous prose, settings, and tracking files are reloaded for every step, and the chapter cursor advances only after a complete commit.

StoryProject remains in non-authoritative shadow mode unless a target-book calibration report passes every strict gate and is explicitly activated. See [Story State Calibration and Strict Activation](docs/story-state-activation.md). Persistent strict runs require `--story-project-writeback`; parser/schema/layout drift fails closed. The opt-in, billable two-chapter real OpenAI gate is documented in [Real StoryProject Two-Chapter E2E](docs/real-storyproject-e2e.md).

Multi-step runs print progress lines to stderr by default, including step start/end, commit status, run id, and duration. Add `--no-progress` to suppress those lines, or use `--output-json` for a clean machine-readable loop result.

Run tests:

```bash
python -B -m unittest discover -s tests
```

Run the local v1 smoke gate:

```bash
python -B scripts/smoke_v1.py
```

This runs tests, preflight, one persisted dry-run, file memory writeback, run reporting, and the provider-smoke missing-config diagnostic path inside `.tmp/smoke_v1/...`.

Recover the latest failed or rejected pre-polish draft without advancing the snapshot:

```bash
python main.py --recover-latest --run-dir .tmp/runtime/runs --chapter-dir .tmp/runtime/chapters
```

Inspect and normalize a snapshot with explicit UTF-8 JSON handling:

```bash
python -B scripts/snapshot_utf8.py --snapshot .tmp/runtime/snapshot.json --write-normalized
```

The optional LLM Validator is off by default, including dry-run and CI. Enable it explicitly with `--llm-validator`; preflight will then require OpenAI configuration and schema-check the LLM validation output before any problems are merged into `validation.problems[]`.

Real provider smoke checks are available separately after local gates pass:

```bash
python -B scripts/provider_smoke.py --providers openai claude
python -B scripts/provider_smoke.py --providers notion --notion-write
```

These write diagnostics under `.tmp/runtime/provider_smoke/...` and use isolated runtime state.
Use `--openai-model`, `--openai-base-url` or `--no-openai-base-url`, `--max-input-chars`, `--max-output-tokens`, `--openai-max-retries`, `--openai-scene-limit`, `--claude-model`, `--claude-base-url`, `--claude-user-agent`, `--claude-max-tokens`, `--request-timeout`, `--retries`, and `--retry-delay-seconds` to keep live provider diagnostics bounded. Claude uses the Anthropic Messages-compatible path through the Anthropic SDK; for MicuAPI-style Claude external compatibility, set `CLAUDE_BASE_URL` or `ANTHROPIC_BASE_URL` to the Anthropic-compatible root URL, not an OpenAI `/v1` endpoint. The Claude key can be `ANTHROPIC_API_KEY` or the Micu-style `ANTHROPIC_AUTH_TOKEN`, and the model can be `CLAUDE_MODEL` or `ANTHROPIC_MODEL`. OpenAI chapter-generation smoke uses one compact scene-generation probe so it proves the real chapter-generation API path without spending a full chapter budget; the full plan/scene/merge pipeline remains covered by local smoke and normal runs. OpenAI SDK retries default to 0 in smoke so `--retries` remains the visible retry budget. Retries apply to non-writing subchecks only; Notion writeback is not retried. Add `--no-proxy` when local `HTTP_PROXY` / `HTTPS_PROXY` variables point at a proxy you do not want provider smoke to use. Add `--require-all-checks` for Phase 4 acceptance, and add `--ignore-dotenv --allow-missing` when checking the missing-credential path on a machine that has local keys.
The JSON report includes `request`, `required_checks[]`, `required_checks_ok`, and a compact `diagnostics` block with missing config names plus failed, skipped, and unrequested subchecks. Provider failures include `failure_category` and `retryable` when they can be classified. Runtime OpenAI and Claude calls default to streamed responses and timeout defaults are tuned for longer chapter-generation and polish calls, so long prose generations can begin returning content before the full response is complete. Snapshots can include `project_profile.language`, `project_profile.known_characters`, and `project_profile.known_locations`; language-aware contracts then prevent configured Chinese projects from committing English model output, and the analyzer uses profile terms to avoid inventing characters or locations. Missing provider config is aggregated per provider so one run reports all required variables it can prove are absent; `diagnostics.missing_config_groups[]` preserves alternatives such as `NOTION_DATABASE_ID` or `NOVELAGENT_NOTION_DATABASE_ID`.
The OpenAI check reports Director and chapter-generation subchecks separately.
Claude reports a `polish` subcheck, and Notion reports `read`, `writeback`, and `readback` subchecks once real credentials are available.
The report also includes `config_status` with redacted set/missing flags, selected models, timeout/token/retry limits, SDK/default endpoint status, and redacted proxy endpoint metadata; no secrets are written.
`required_checks[]` and `required_checks_ok` form the flat Phase 4 completion checklist.

Inspect recent persisted run records:

```bash
python main.py --report-runs
```

## Main Directories

- `core/director`: decision layer with schema-checked decisions and run audit records.
- `core/engine`: execution loop, schema-checked workflow plans and trace events, model-call stage diagnostics, run records, artifacts, preflight, and run reports.
- `core/cli`: argument parsing metadata, runtime/config resolution, command routers, and output formatters re-exported by `main.py`.
- `core/state`: snapshot, input pack plus metadata, memory, Notion export normalization, and schema-checked state builder audit.
- `core/story_project`: project identity, semantic parser/authority, activation, managed projection merge, read sets, and StoryProject writeback.
- `core/memory_v2`: immutable event batches, replay/checkpoint verification, canonical projection, and Snapshot adaptation.
- `core/validator`: continuity, spatial, and logic validation with explicit requested/executed/skipped coverage metadata.
- `modules`: feature modules for chapter planning/scene generation/merge, polish, schema-checked conflict analysis, and schema-checked repair planning.
- `api`: provider adapters for OpenAI, Claude, and Notion.
- `prompts`: prompt assets.
- `schemas`: JSON schema contracts.
- `docs`: runtime and memory documentation.

## Runtime Artifacts

Persistent runs write schema-checked run result envelopes:

- `data/snapshot.example.json`: committed sample snapshot state.
- `data/notion_memory.example.json`: committed sample Notion memory export.
- `.tmp/runtime/snapshot.json`: default mutable runtime snapshot.
- `.tmp/runtime/notion_memory.json`: optional initialized runtime memory copied from the sample.
- `.tmp/runtime/runs/*.json`: structured run records with Director audit, workflow plan, state update audit, trace, model output failure diagnostics, validation, analysis summaries, and schema-checked `chapter.pipeline.stages`.
- `.tmp/runtime/runs/loop_sessions/*.json`: schema-checked multi-step loop session records with per-step timing.
- `.tmp/runtime/runs/snapshot_packs/*.md`: Snapshot Builder input packs.
- `.tmp/runtime/runs/input_packs/*.md`: full input packs; run records include input pack metadata.
- `.tmp/runtime/runs/chapter_pipeline/*`: chapter plan, scene drafts with merged-chapter spans, merged chapter, validation report, and repair delta artifacts for the pipeline stages.
- `.tmp/runtime/chapters/*.md`: chapter body artifacts.
- `<StoryProject>/.novelagent/runtime/memory/v2/`: project-local canonical Memory V2.1 projection, immutable event batches, and checkpoints.

Run records, run reports, and loop session summaries expose compact validation, repair evidence, and per-step timing, so common Validator/Repair/provider stalls can be diagnosed without opening the full run-result envelope. Both directories are ignored by git by default.

Local `.env` is ignored. Use `.env.example` for variable names and recommended default model names only; real configuration is still checked by preflight before live provider calls.

Legacy imports under `core.*` remain available as compatibility wrappers. `core.orchestrator` delegates to the v1.5 executor and supports custom snapshot, memory, run, chapter, preflight, loop, and report paths. Compatibility package exports point at the v1.5 implementations for input pack metadata, snapshot builder audit, state update audit, dynamic flow plans, feature modules, and API adapters. The root `core` package lazily exposes the v1.5 executor and orchestrator entrypoints.

The v1.5 project contract keeps the established directory layout and legacy wrapper boundaries covered by `tests/test_repo_hygiene.py`, so structural drift is caught alongside runtime tests.

## More Docs

- [Architecture](docs/architecture.md)
- [Runtime Commands](docs/runtime.md)
- [Memory Input](docs/memory.md)
- [Story State Calibration and Strict Activation](docs/story-state-activation.md)
- [Real StoryProject Two-Chapter E2E](docs/real-storyproject-e2e.md)
- [Reliability and autonomy capability status](docs/reliability-autonomy-capability-status.md)
- [Operator-run real autonomy validation](docs/real-autonomy-e2e.md)
