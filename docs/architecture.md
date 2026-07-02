# Architecture

NovelAgent v1.0 separates long-term memory, runtime state, decision making, execution, validation, and repair.

## Data Flow

```text
Memory source
  -> core.state.memory / core.state.notion_export
  -> core.state.builder
  -> core.director
  -> core.engine.executor
  -> modules.chapter_generator pipeline
  -> modules.claude_polish
  -> core.validator
  -> modules.scene_repair
  -> core.state.snapshot
  -> run + chapter artifacts
```

## Layers

### Memory

Memory is normalized before the loop runs. Supported sources:

- Normalized memory JSON.
- Notion export JSON.
- Notion database query through the Notion API adapter.

The normalized format is a list of typed memory items such as `world_state`, `location`, `character`, `constraint`, and `timeline_event`.
Notion export files are recorded as `notion-export`; live API reads are recorded as `notion-api`. Both paths unwrap Notion property objects before schema validation.
Normalized memory contexts include `source_mappings` so each memory item can be traced back to its file path and line number, or to its Notion page id/page URL/page index. Runtime input packs expose a compact version of these mappings in the Memory Index, and expose last-run recovery signals in a separate Recovery Context section.

### Snapshot Builder

`core.state.builder.build_snapshot_state()` merges base snapshot data with normalized memory. The runtime snapshot is the source of truth during one execution step.

Named memory such as locations and characters overwrites the matching runtime object. List-style memory such as constraints and timeline events is deduplicated by stable memory id, name, or content-derived key.

`core.state.builder.build_snapshot_state_with_audit()` returns the runtime snapshot plus a schema-checked compact audit of memory application. The audit records memory source/status, item counts, applied items, skipped items, and deduplicated items so Director inputs can be reviewed after a run. Applied and skipped audit items include compact `source_mapping` data when available, so each memory decision can be traced back to a file line or Notion page. Skipped items keep a human-readable `reason` and also expose stable `reason_code`, `severity`, `category`, and `blocking` fields, with top-level reason/severity counts for reporting.

### Director

`core.director.decide_next_step()` produces a structured decision:

- `goal`
- `actions`
- `validation_focus`
- `max_repair_attempts`
- `notes`

The Director can use the last rejected or failed run summary to enter a recovery workflow. The summary is compact but includes status, chapter index, goal, workflow, validation problem codes, blocking/warning counts, severity counts, compact validation and repair evidence, repair attempts, Director mode/status, and error type/message when available.

The default Director is deterministic and offline. `core.director.ModelDirector` is an optional model-backed adapter with the same callable interface. It uses `prompts/director_prompt.md`, sends snapshot, compact memory context, and the DirectorDecision schema to the model, then validates the returned JSON through the same contract before the engine can execute it. The compact memory context includes last-run validation coverage, repair summaries, Snapshot Builder applied/skipped type counts, skipped reason/severity/blocking counts, and limited source mapping samples for applied/skipped memory, so model decisions can account for recovery coverage and Memory quality without receiving full Memory payloads. If the model-backed Director fails during provider calls, the failed run record includes provider, stage, model, cause type, and message under `run.director.model_call`.

Recovery decisions use structured validation severity and coverage. A rejected or failed run normally raises the repair budget to 2 and skips polish; critical validation severity or three or more blocking problems raise the repair budget to 3. Validation focus starts from problem codes, then prioritizes any continuity/spatial/logic checks skipped by the previous run before a recovery commit can be trusted. Director notes include the blocking count, severity summary, and previous validation coverage for auditability.

Current-run memory quality also feeds Director decisions. The execution engine attaches `memory_context.snapshot_builder_audit` after building the runtime Snapshot and before calling Director. The audit records applied/skipped counts by memory type, skipped reason/severity counts, blocking skipped counts, and compact source mapping samples. Low-severity duplicate memory skips remain informational; medium-or-higher skipped memory such as `missing_name` raises the repair budget, prioritizes continuity/spatial validation, and skips polish so scene repair can run before prose refinement.

Loop recovery also uses prior repair effectiveness and repair plan risk. `load_latest_run_summary()` extracts compact `repair_plan`, `repair_deltas`, validation evidence, and repair evidence from the previous run trace, including risk level, repair budget, attempt number, manual-review count, and whether repair stalled or introduced new problem codes. The Director uses remaining/new problem codes to adjust validation focus and raises the recovery budget when the last repair attempt did not reduce validation risk, consumed its budget, carried critical risk, or required manual review.

### Contracts

Schema files under `schemas/` are runtime contracts, not only documentation. `core.schema.validate_schema()` is dependency-free and validates the JSON Schema subset used by the project.

Contracts that duplicate standalone schemas are guarded by tests and preflight so copied blocks and legacy assets do not drift from their authoritative files. This currently covers the legacy `core/director/schema.json` mirror of the Director decision schema plus nested run-record contracts for Director audit, input pack metadata, Snapshot Builder audit, workflow plans, state update audit, trace events, and repair plans.

Run-result artifacts use `core.engine.run_record.validate_run_result()` as the shared boundary: it validates the envelope with `run_result.schema.json` and the embedded `run` object with the full `run_record.schema.json`. The executor, report builder, loop failure recovery, previous-run history loader, and preflight `run_history` check all use this boundary before trusting persisted run JSON.

Currently enforced contracts:

- `analysis_result.schema.json` in `modules.conflict_engine.analyze_chapter()` and `AgentExecutor` analyzer handling.
- `director_audit.schema.json` in `core.engine.executor._director_trace()` and run record construction.
- `director_decision.schema.json` in `core.director.validate_decision()`.
- `input_pack_metadata.schema.json` in `core.state.input_pack.build_input_pack_metadata()`.
- `memory_context.schema.json` in `core.state.memory.normalize_memory_context()` and memory writeback.
- `memory_writeback.schema.json` in `core.state.memory_writer` results and `core.engine.run_record.validate_run_result()`.
- `loop_session.schema.json` in `core.engine.run_record.build_loop_session_record()` and persisted loop session artifacts.
- `repair_plan.schema.json` in `modules.scene_repair.build_repair_plan()`.
- `snapshot_builder_audit.schema.json` in `core.state.builder.build_snapshot_state_with_audit()`.
- `snapshot.schema.json` in `core.state.snapshot.validate_snapshot()`.
- `state_update_audit.schema.json` in `core.state.snapshot.build_state_update_audit()` and run record construction.
- `validation_result.schema.json` in `core.validator.validate_chapter()`.
- `run_record.schema.json` in `core.engine.run_record.build_run_record()`.
- `run_result.schema.json` plus the embedded full run record in `core.engine.run_record.validate_run_result()`.
- `trace_event.schema.json` in `core.engine.executor._trace_event()`.
- `workflow_plan.schema.json` in `core.engine.workflow.build_workflow_plan()`.

### Execution Engine

`core.engine.executor.AgentExecutor` runs one or more loop steps. It supports dependency injection for generator, polisher, validator, repairer, analyzer, director, and memory loader.

Committed runs update the snapshot. Rejected runs do not advance the snapshot, but still write diagnostics when persistence is enabled.

Legacy MVP imports remain as compatibility wrappers, but they delegate into the v1.0 layers. `core.orchestrator` now exposes `run_agent_once()`, `run_agent_loop()`, `check_runtime()`, and `report_runs()` with the same runtime path, memory, Director, writeback, persistence, and loop-mode options as the CLI-oriented engine. Legacy package exports also expose v1.0 surfaces such as input pack metadata, snapshot builder audit, state update audit, dynamic flow plans, feature modules, and API adapters without duplicating implementation logic. The root `core` package lazily exposes `AgentExecutor` and the orchestrator entrypoints. New code should prefer `core.engine.executor.AgentExecutor` or the CLI, but old imports no longer bypass v1.0 artifacts and diagnostics.

If the Director raises an exception or returns an invalid decision, `persist=True` writes a `failed` run record with `validation.problem_codes=["director_error"]`, an empty workflow, and the failed `director` audit block, then re-raises the original exception.

If the Director returns a valid decision whose action order cannot be normalized into a workflow, `persist=True` writes a `failed` run record with `validation.problem_codes=["workflow_error"]`, the original decision, an empty workflow, and no input pack artifact, then re-raises the original exception.

If a workflow action raises an exception after the input pack has been built, `persist=True` writes a `failed` run record with the failed action trace and input pack artifact, then re-raises the original exception. Failed runs do not advance the snapshot or write memory updates.

Director actions are normalized by `workflows.dynamic_flow.build_dynamic_flow()` and executed through an action handler table in `AgentExecutor`. `workflows.dynamic_flow.build_dynamic_flow_plan()` also produces a schema-checked auditable plan with step inputs, outputs, validation focus, repair budget, and whether the flow is a recovery flow. The current action set is:

- `generate_chapter`
- `polish`
- `validate`
- `repair_if_needed`

Workflow validation rejects duplicate actions, missing required `generate_chapter` or `validate` actions, repair before validation, and polish after validation. If a decision omits the `actions` field entirely, the workflow layer uses the default v1.0 action order; an explicitly empty action list is rejected instead of being treated as a default.

Preflight exposes `run_history` plus both `planned_workflow` for the executable action list and `planned_flow` for the richer plan view. `run_history` summarizes loaded/skipped persisted runs, latest run validation coverage, latest loop-session status, and the latest loop-session last-run validation coverage. It fails diagnostics when saved run or loop-session artifacts no longer satisfy their schemas, so recovery planning does not silently drop invalid history. The richer flow plan records each step's required inputs, produced state, execution mode (`required`, `optional`, or `conditional`), skip condition, and failure policy. Workflow plan validation also checks that `actions` and `steps` stay aligned, step indexes are contiguous, and step metadata still matches the registered action metadata. The executor stores that same schema-checked plan as `run.workflow_plan`, while `run.workflow` remains the plain action list used by the handler table.

Each executed action appends a schema-checked `run.trace` event with timestamps, action status, the matching workflow plan step index, plan step mode, failure policy, chapter length, validation state when available, and repair attempt count. Model-capable actions also record `model_stage`, `model_provider`, `model_name`, and `model_invocation` so successful dry-run, injected, skipped, and provider-backed paths are distinguishable without relying on failure diagnostics. This makes generation, polish, validation, and repair behavior auditable from the run record and lets reviewers compare the Director's plan with the action path actually executed.

Each run record also includes a schema-checked `director` audit block with the decision source, mode, model name when applicable, status, timestamps, duration, and model-call diagnostics when applicable. This keeps rule, model, and injected Director decisions distinguishable when reviewing artifacts.

Multi-step execution also builds a schema-checked loop session record. The session captures requested and completed steps, stop reason, persistence mode, stop-on-rejection policy, committed/rejected/failed counts, first and last chapter indexes, the last run id, compact summaries for each run, validation coverage for each step, compact validation and repair evidence, each run's workflow action list, trace action list, whether the trace remains aligned with the workflow plan, recovery links between a run and the previous run attached as `memory_context.last_run`, and the session-level error when a loop stops on an exception. Persistent sessions are written under `.tmp/runtime/runs/loop_sessions/` by default and include their own `artifact` pointer so the saved session is self-describing. If a failing step persisted a failed run record before raising, that failed run is included in the session summary.

### Generation Pipeline And Polish

`modules.chapter_generator` now runs a chapter pipeline before polish:

- `plan_chapter`
- `generate_scenes`
- `merge_scenes`

Dry-run uses deterministic local plan and scene drafts. Non-dry-run uses OpenAI-backed chapter planning and scene drafting, then merges scene text locally before the existing polish, validation, repair, and commit boundary. The executor still exposes this as the `generate_chapter` workflow action for compatibility, but run records store schema-checked `chapter.pipeline.stages` for `plan_chapter`, `generate_scenes`, `merge_scenes`, `validate`, `repair`, and `commit`. The pipeline also records `scene_spans`, mapping each scene draft to its character range in the merged chapter, so later validation or repair can target a scene without rewriting the entire chapter. Each stage records a status plus compact summary data, so validation, repair, and commit outcomes can be audited without reconstructing them from filenames.

Pipeline artifacts are written under `.tmp/runtime/runs/chapter_pipeline/` by default:

- chapter plan JSON
- scene draft Markdown files
- merged chapter Markdown
- validation report JSON
- repair deltas JSON

`modules.claude_polish` calls the Claude adapter unless dry-run is enabled.

Chapter generation, polish, and repair use model output contracts so empty output, structured data, fenced code, Markdown wrappers/headings, standalone chapter headings such as `Chapter 4`, and common assistant commentary such as `Here is...`, `As an AI...`, or `Error:...` are rejected early as `ModelOutputError`.

Provider failures and malformed provider responses are wrapped as `ModelCallError` with provider, stage, model, cause type, and message. Stages are specific to the runtime call site: `director_decision`, `chapter_generation`, `claude_polish`, and `scene_repair`. Successful workflow trace events record the intended stage/provider/model and invocation mode, while failures additionally record provider error metadata under `trace[].model_call` so API, configuration, and provider response-shape failures can be diagnosed from artifacts.

Prompt assets live under `prompts/` and are plain UTF-8 Markdown. Snapshot Builder input packs are built from `prompts/snapshot_prompt.md`, the base snapshot, and normalized memory context so state construction is auditable. Runtime chapter input packs are built by `core.state.input_pack.build_input_pack()` from Snapshot, Director Decision, constraints, timeline, a compact Memory Index, and a compact Recovery Context with last-run problem codes, validation coverage, and repair summaries. `core.state.input_pack.build_recovery_context()` is also passed directly into scene repair, so repair prompts do not need to parse recovery signals back out of Markdown. `core.state.input_pack.build_input_pack_metadata()` adds a schema-checked summary of the same generation input to the run record without duplicating full memory payload data.

### Validation

`core.validator` always runs the cheap rule-validator layer: continuity, spatial, and logic checks. Validation can reject:

- Empty or too-short chapters.
- Missing conflict signal.
- Unknown or omitted character locations.
- Inactive character actions.
- Forbidden or missing constraint terms.
- Declared chapter index mismatches.

The Validator honors `decision.validation_focus`. A focused recovery run can execute only the selected check groups, while missing or empty focus falls back to the full continuity, spatial, and logic set. Each validation result records `requested_focus`, `executed_checks`, and `skipped_checks`, so a run artifact shows both the Director's requested coverage and the Validator coverage actually applied.

Each validation problem is enriched with its source `validator`, `severity`, `blocking`, `category`, `repair_hint`, `repair_action`, normalized `repair_parameters`, and structured `evidence` entries that capture the concrete mismatch or missing fact. The run record stores a compact `validation.problem_evidence` summary so reports, loop sessions, and recovery diagnostics can show the concrete evidence without opening the full run-result envelope. The top-level validation result also records `blocking_problem_count`, `warning_count`, `severity_counts`, `deterministic_repair_count`, `manual_review_count`, and `repair_action_counts`, so Director, Repair, reports, and run artifacts can distinguish hard failures, lower-priority warnings, deterministic repairs, and manual-review work without reinterpreting problem codes.

An optional OpenAI-backed LLM Validator can be enabled with `--llm-validator`. It checks story-level issues that are expensive or brittle as rules: complex plot logic, character motivation consistency, timeline causality, setup/payoff, and emotional or theme drift. It is disabled by default and dry-run/CI do not enable it implicitly. When enabled, preflight requires OpenAI configuration. The model response must satisfy `schemas/llm_validation.schema.json`; accepted problems are merged into `validation.problems[]` with `validator="llm"`, structured `evidence`, `severity`, `repair_hint`, `area`, and `repair_action="manual_review"`.

### Repair

`modules.scene_repair` first converts Validator output into a schema-checked repair plan with ordered actions, source validators, priorities, strategies, severity, blocking status, repair hints, structured evidence, registered problem parameters, risk summary, repair budget, attempt number, and a compact `repair_plan.recovery` summary. The recovery summary records repeated, unresolved, and newly introduced problem codes plus skipped validation checks, prior manual-review requirements, and exhausted prior repair budgets as stable `failure_modes`. The plan prefers Validator-supplied `repair_action` and `repair_parameters`, then falls back to registered problem-code metadata. Known parameter contracts include `term`, `suggested_term`, `character`, `location`, `expected`, and `actual`; unknown problems are isolated under `raw_problem` for manual review and counted separately from deterministic steps. Non-dry-run model repair receives this plan along with the raw validation result, runtime input pack, explicit Recovery Context, and `prompts/repair_prompt.md`. Dry-run mode executes the same plan through an explicit action-to-strategy registry, applying `repair_plan.steps[]` in priority order and using each step's parameters as the strategy input. Registered deterministic strategies cover missing conflict signals, short chapters, constraint terms, location anchoring, chapter index mismatches, inactive character actions, and manual-review no-ops. If validation still fails after allowed attempts, the chapter is rejected.

When `repair_if_needed` runs, the workflow trace records whether the conditional step was skipped and why. If repair executes, the trace records the schema-checked repair plan used for the attempt, including step-level evidence. It also records `repair_deltas` for each repair attempt, including before/after validation status, problem counts, problem codes, and resolved/new/remaining problem code sets. Reports and loop sessions expose compact `repair_evidence` summaries from that plan, keeping validation failures, scene repair decisions, skip decisions, and repair effectiveness connected without requiring manual artifact spelunking.

### State Update

Committed schema-checked analysis updates:

- `snapshot.timeline`
- `snapshot.characters`
- `world_state.locations`
- `world_state.last_world_changes`

Rejected runs leave snapshot state unchanged.

`core.state.snapshot.update_snapshot()` and `build_state_update_audit()` both validate `analysis_result.schema.json` before applying or summarizing state changes, so the state layer does not rely on callers to pre-check analyzer output. Each run record includes `state_update`, a schema-checked audit of whether the update was applied, the chapter index transition, timeline additions, extracted character/location/world changes, and generated memory update counts by type. This makes the commit boundary reviewable without diffing the full Snapshot.

### Memory Writeback

Committed runs can also produce normalized memory updates through `core.state.memory_updates.build_memory_updates()`.

Writeback is explicit:

- `FileMemoryWriter` appends JSONL outbox records for review or later sync.
- `NotionMemoryWriter` creates Notion database pages with `Type`, `Name`, and `Data` properties.
- Writeback results include `item_mappings`, linking each memory item id/type/name to its target, write status, file line number, or Notion page id/page URL plus property names.
- File writeback records readback `verification` by loading the mapped JSONL lines and checking `id`, `type`, and `name` against the written item mappings.
- Notion writeback records response-level verification by default. When `--notion-readback` or `NotionMemoryWriter(verify_remote_readback=True)` is used, it queries the database after writes and verifies `Memory ID`, `Type`, and `Name` against the written item mappings.
- Character changes are written as `character` memory items so status and location updates can feed later runs.
- CLI writeback modes are `none`, `file`, and `notion`; `--memory-outbox` remains a compatibility shortcut for file writeback.
- Writeback-generated memory items include stable `id` values. File writeback skips duplicate ids already present in the outbox. CLI-created Notion writers query existing database pages before writing and skip duplicate `Memory ID` values while preserving the existing page id/page URL in `item_mappings`.
- The execution engine applies a writeback quality gate before calling the writer. A run must be committed, final validation must be ok, and the final repair delta must not contain new, remaining, or post-repair problem codes. The gate records pending memory update count/type summaries, and blocked writeback is recorded under `run.memory.writeback.gate`.

Rejected and failed runs do not write memory updates.

### Artifacts

Persistent execution writes:

- Run JSON records under `.tmp/runtime/runs/` by default.
- Loop session JSON records under `.tmp/runtime/runs/loop_sessions/`.
- Snapshot Builder input packs under `.tmp/runtime/runs/snapshot_packs/`.
- Full input pack markdown under `.tmp/runtime/runs/input_packs/`.
- Chapter pipeline artifacts under `.tmp/runtime/runs/chapter_pipeline/`.
- Chapter markdown under `.tmp/runtime/chapters/`.

Committed examples stay separate from mutable runtime state: `data/snapshot.example.json` and `data/notion_memory.example.json` are source-control samples, while `python main.py --init-runtime` copies them into `.tmp/runtime/` for local execution.

Run records include State Builder audit details, Director audit details, workflow plan details, Validation coverage and evidence summaries, State Update audit details, workflow trace events, input pack metadata, and pointers to the chapter artifact and input pack artifact. The persisted outer run result envelope is also schema-checked before writing. Committed runs include the memory writeback result under `run.memory.writeback`. These artifacts make debugging and manual review possible without re-running generation.

`core.engine.report.build_run_report()` provides a read-only audit view over persisted run records and loop sessions. It summarizes recent runs, status counts, validation problem counts, compact validation evidence, compact repair evidence, compact Director details, workflow plan summaries, trace status, loop session status, and whether referenced artifacts still exist. The CLI exposes this through `python main.py --report-runs`.

### Real Provider Smoke

`scripts/provider_smoke.py` is the opt-in real-environment gate. It uses `.tmp/runtime/provider_smoke/<timestamp>/`, initializes runtime state from examples, and writes `provider_smoke_report.json` with request intent, redacted config/proxy status, per-provider diagnostics, and a compact `diagnostics` summary for missing config, one-of config groups, failed checks, skipped checks, and unrequested checks. Provider failures are classified with `failure_category` and `retryable` when possible, and `--retry-delay-seconds` controls the wait between non-writing retry attempts. OpenAI smoke covers the model Director plus one compact real chapter-generation probe; the full plan/scene/merge pipeline is covered by local smoke and normal runtime artifacts. Claude smoke covers polish. Notion smoke reads by default and performs writeback/readback only when `--notion-write` is supplied.
