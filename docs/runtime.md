# Runtime Commands

Preflight without model calls:

```bash
python main.py --check --dry-run --memory data/notion_memory.example.json
```

Local v1.0 smoke gate:

```bash
python -B scripts/smoke_v1.py
```

The smoke gate runs `unittest` discovery, preflight, one persisted dry-run, file memory writeback, and run reporting. It writes an isolated temporary snapshot, run directory, chapter directory, and memory outbox under `.tmp/smoke_v1/...`, then asserts that the committed run, chapter artifact, input pack artifact, snapshot pack artifact, report summary, and outbox were produced. Use `--skip-tests` to exercise only the runtime CLI flow.

By default, `--check` prints a concise human-readable summary. Use `--check-json` when full machine-readable diagnostics are needed:

```bash
python main.py --check --check-json --dry-run --memory data/notion_memory.example.json
```

Preflight diagnostics include Memory input resolution (`auto`, `file`, or `notion`), the selected Memory source, source-mapping counts, run-history contract status, latest run validation coverage, latest loop-session last-run validation coverage, execution mode, persistence mode, expected model-call stages, the schema-checked `state_builder_audit`, `planned_workflow` for the default rule Director, and `planned_flow` with the schema-checked auditable dynamic flow plan. The plain preflight summary prints compact State Builder counts for applied/skipped memory types, skipped reason/severity counts, deduplicated items, and blocking skipped items while `--check-json` retains the full item-level audit and source mappings. Loaded Memory contexts include item-level source mappings, and runtime input packs expose a compact Memory Index for tracing loaded items back to file lines or Notion page ids/URLs plus a Recovery Context for last-run problem codes, validation coverage gaps, and repair summaries. In non-dry-run mode, it checks that the OpenAI SDK is installed and `OPENAI_API_KEY` is set. If the planned workflow includes `polish`, preflight also checks that the Anthropic SDK is installed plus `ANTHROPIC_API_KEY` and `CLAUDE_MODEL`.
It also verifies the required v1.0 engineering structure, prompt Markdown files, JSON schema assets, embedded schema consistency, and the selected Memory input before any generation step runs.
Artifact targets are checked as well: `--run-dir`, its `snapshot_packs`, `input_packs`, and `loop_sessions` subdirectories, and `--chapter-dir` must be directories or be creatable as directories before generation starts.

Inspect recent persisted run records without generating a chapter:

```bash
python main.py --report-runs --run-dir data/runs
```

Use `--report-limit 0` when only aggregate counts are needed. The report includes status counts, validation problem counts, compact validation evidence, compact Director details, workflow plan summaries, trace summaries with repair evidence, memory writeback status/type counts, loop session summaries, and artifact path existence checks. Malformed run result envelopes or run objects that fail the full run-record contract are skipped and listed under `skipped`; malformed loop sessions are listed under `skipped_loop_sessions`.

Single dry-run step:

```bash
python main.py --dry-run --memory data/notion_memory.example.json
```

Generation commands print a concise chapter/run/validation summary by default, including requested validation focus, executed checks, and skipped checks when available. Failed run summaries include the run error plus compact model-call diagnostics when a Director, generation, polish, or repair provider call failed. Use `--output-json` for the full result object, or `--output-run-json` for only the run record. For `--steps` greater than 1, `--output-json` returns the full loop result with `session`, all step `runs`, `completed_steps`, `stopped_reason`, and `last_result`; `--output-run-json` still prints only the last run record.

```bash
python main.py --dry-run --output-json --memory data/notion_memory.example.json
python main.py --dry-run --output-run-json --memory data/notion_memory.example.json
```

Memory input mode can be selected explicitly:

```bash
python main.py --check --dry-run --memory-source file --memory data/notion_memory.example.json
python main.py --check --dry-run --memory-source notion
```

`auto` remains the default. It uses a provided memory file path first, otherwise uses live Notion API when `NOTION_API_KEY` and a Notion database id are configured, otherwise falls back to `NOVELAGENT_MEMORY_PATH` or `data/memory.json`. Preflight reports this decision under `memory_input`, including the resolved source, resolved file path when applicable, Notion configuration flags, and the resolution reason.

Use a model-backed Director instead of the default offline rule Director:

```bash
python main.py --director-model gpt-4.1-mini --memory data/notion_memory.example.json
```

This mode uses the OpenAI client and requires `OPENAI_API_KEY`.
That requirement applies even with `--dry-run`, because `--dry-run` only replaces chapter generation and polish outputs, not the model-backed Director.
For non-dry-run model Director mode, preflight conservatively checks Claude configuration because the model may choose a workflow containing `polish`.

Multi-step dry-run without persistence:

```bash
python main.py --dry-run --steps 2 --memory data/notion_memory.example.json
```

`--steps` must be at least 1. CLI parsing rejects invalid values before execution, and preflight reports the same constraint under `loop_parameters`.

Persist dry-run state and run records:

```bash
python main.py --dry-run --persist-dry-run --steps 2 --memory data/notion_memory.example.json
```

Persist dry-run state and append committed memory updates to a local outbox:

```bash
python main.py --dry-run --persist-dry-run --memory data/notion_memory.example.json --memory-outbox data/memory_outbox.jsonl
```

The explicit equivalent is:

```bash
python main.py --dry-run --persist-dry-run --memory data/notion_memory.example.json --memory-writeback file --memory-outbox data/memory_outbox.jsonl
```

If `--memory-writeback file` is used without `--memory-outbox`, the default path is `data/memory_outbox.jsonl`.

Write committed memory updates directly to Notion:

```bash
python main.py --memory data/notion_memory.example.json --memory-writeback notion
```

Notion writeback requires `NOTION_API_KEY` plus `NOTION_DATABASE_ID` or `NOVELAGENT_NOTION_DATABASE_ID`.

Add `--notion-readback` to query the Notion database after writeback and verify that written `Memory ID`, `Type`, and `Name` values can be read back:

```bash
python main.py --memory data/notion_memory.example.json --memory-writeback notion --notion-readback
```

Reuse that outbox as the next memory source:

```bash
python main.py --dry-run --memory data/memory_outbox.jsonl
```

Use custom artifact directories:

```bash
python main.py --dry-run --persist-dry-run --run-dir .tmp/runs --chapter-dir .tmp/chapters --memory-outbox .tmp/memory_outbox.jsonl
```

## Local State Boundaries

Runtime output should stay out of source control. The default local-only targets are ignored by git:

- `.tmp/`: tests, smoke runs, and disposable local experiments.
- `data/runs/`: persisted run envelopes plus snapshot and input pack artifacts.
- `data/chapters/`: generated chapter bodies.
- `data/memory_outbox.jsonl` and `data/memory_outbox*.jsonl`: file-based memory writeback queues.

Python and tooling caches such as `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, coverage output, logs, `.env`, `.venv/`, and `.idea/` are also ignored. If a cache or local config file was already tracked before these rules existed, clean it up as an explicit git-index maintenance step rather than mixing it into feature work.

Persistent runs write schema-checked run result envelopes:

- `data/runs/*.json`: structured run records.
- `data/runs/loop_sessions/*.json`: schema-checked loop session records for multi-step runs.
- `data/runs/snapshot_packs/*.md`: Snapshot Builder input packs built from base snapshot plus normalized memory.
- `data/runs/input_packs/*.md`: full input packs used for generation and repair context; run records also store schema-checked input pack metadata.
- `data/chapters/*.md`: chapter body artifacts for committed and rejected runs.
- `data/memory_outbox.jsonl`: optional committed memory updates when `--memory-outbox` is set.

Run result envelopes pass a shared runtime validator that checks both the top-level `run_result.schema.json` envelope and the embedded full `run_record.schema.json` object. The executor uses this before writing a run artifact, loop failure recovery uses it before reloading the newest failed run, reports use it before summarizing saved runs, and history recovery uses it before attaching `memory_context.last_run`. Preflight exposes the same boundary under `run_history`; invalid saved run or loop-session artifacts fail startup diagnostics instead of being silently ignored by recovery planning.

Run records include Snapshot Builder artifact links, State Builder memory-application audit, a compact `recovery_context`, the schema-checked `workflow_plan`, schema-checked `state_update`, and schema-checked `trace` events for each executed workflow action. The run `memory` summary stores item count plus source-mapping coverage counts by mapping source, file path, JSONL line, Notion page id, and Notion page URL, so persisted artifacts can prove how loaded Memory was traced even without opening the full input pack. Workflow plan steps include execution mode, skip condition, and failure policy so optional and conditional work can be audited separately from required work; runtime validation also rejects plan/action mismatch, non-contiguous step indexes, and step metadata drift. Trace events carry the matching workflow plan step index, step mode, and failure policy so the Director's plan can be compared directly with the action path actually executed. Multi-step loops also write a schema-checked `loop_session` record with requested/completed steps, stop reason, committed/rejected/failed counts, run ids, per-run validation coverage, compact validation evidence, compact repair evidence, per-run workflow actions, per-run trace actions, per-run trace/plan alignment, recovery links, artifact pointer, and a session-level error when the loop stops on an exception. Validation summaries include problem codes, blocking/warning counts, severity counts, deterministic/manual-review repair counts, repair action counts, requested validation focus, executed checks, skipped checks, and `problem_evidence`. Full Validator results enrich each problem with `repair_action`, normalized `repair_parameters`, and structured `evidence`, which the run record summarizes and the repair plan consumes before falling back to problem-code metadata. `state_update` records whether the commit was applied, the chapter index transition, timeline additions, extracted change counts, and memory update counts by type. Each trace event records timestamps, completion status, chapter length, validation state when present, repair attempt count, model stage/provider/model/invocation details for model-capable actions, model-call diagnostics when provider calls fail, and the schema-checked repair plan when `repair_if_needed` runs. Conditional repair trace events record `skipped` plus `skip_reason` when validation is already ok or the Director supplied no repair budget. Repair trace events also include repair plan risk level, budget, deterministic/manual-review counts, `repair_plan.recovery.failure_modes`, step evidence, and per-attempt `repair_deltas` with before/after problem counts and resolved/new/remaining problem codes, making repair effectiveness auditable without diffing full validation payloads. Run reports and loop sessions expose compact `repair_evidence` summaries from these repair plan steps. Repair plan step parameters are restricted to registered fields, so unexpected Validator fields cannot silently become strategy inputs. Dry-run repair executes that plan by dispatching ordered `repair_plan.steps[]` through registered local strategies, so the trace action list matches the actual repair path. Non-dry-run repair sends the same plan plus explicit Recovery Context to the model repair stage. Model-call diagnostics use stage names such as `director_decision`, `chapter_generation`, `claude_polish`, and `scene_repair`.

Model output contracts reject non-prose returns before they can be committed as chapter text. Empty output, JSON-like structured data, fenced code blocks, Markdown wrappers/headings, standalone chapter headings such as `Chapter 4`, and assistant commentary such as `Here is...`, `As an AI...`, or `Error:...` raise `ModelOutputError` and are persisted as failed-run diagnostics when persistence is enabled.

Committed runs validate the full chapter analysis result before the Snapshot is advanced or memory writeback is considered. The run record stores a compact analysis summary, while the state update path consumes the schema-checked full analysis object. Memory writeback then passes through a quality gate: final validation must be ok, and the final repair delta must not contain new, remaining, or post-repair problem codes. The gate records pending memory update count/type summaries before it allows or blocks writes. If the gate blocks writeback, the run still records schema-checked `run.memory.writeback.gate` with the reasons and writes no outbox or Notion pages. Successful or skipped writeback records schema-checked `item_mappings` so each generated memory item can be traced to an outbox line or Notion page id/URL. File writeback records readback `verification` for written JSONL lines. CLI-created Notion writeback queries existing pages before writing and skips duplicate `Memory ID` values while recording the existing page mapping. Notion writeback records response-level verification by default, and `--notion-readback` upgrades this to database readback verification with `status="verified"` when every written memory item can be queried back by `Memory ID`.

Schema consistency checks compare duplicated embedded run-record blocks with their standalone schema files, including the nullable embedded workflow plan and state update audit, so artifact contracts fail fast if one side changes without the other. The same guard runs in automated tests and preflight.

If the Director raises an exception or returns an invalid decision during a persistent run, the engine writes a `failed` run record with schema-checked `director_error` diagnostics before re-raising the original exception. Model-backed Director provider failures include `run.director.model_call` diagnostics. If the decision is valid but cannot be normalized into a legal workflow, the engine writes `workflow_error` diagnostics. If a later workflow action raises an exception, the engine writes a `failed` run record and input pack artifact before re-raising the original exception.

If a multi-step loop stops because one step raises after writing a failed run record, the loop session includes that failed run summary, `stopped_reason="failed"`, and the original error type/message. If the exception happens before a run record can be written, the loop session still records the session error and any earlier completed runs.

Continue after a rejected or failed run so the next step can use recovery context:

```bash
python main.py --steps 2 --continue-on-rejection --memory data/notion_memory.example.json
```

Recovery context includes validation problem codes plus blocking/warning counts, severity counts, validation coverage, compact validation evidence, compact repair plan summaries, compact repair evidence, and compact repair delta summaries from the last run. The rule Director uses that structured summary to choose validation focus and repair budget, model-backed Director receives the same compact `last_run` payload, chapter generation receives a first-class `# Recovery Context` section in the runtime input pack, and scene repair receives the same structure as explicit model payload context. Each run record stores which last run was attached under `recovery_context`; loop sessions collect those edges under `recovery_links` so multi-step recovery can be audited after the fact. If the previous run skipped continuity, spatial, or logic checks, the next recovery decision prioritizes those skipped checks before commit. If the previous repair plan carried critical risk, needed manual review, or exhausted its budget without a commit, the next recovery budget is raised before polish. If the previous repair stalled or introduced new problem codes, recovery budget is also raised and validation focus is derived from remaining/new problem codes. The current run's `snapshot_builder_audit` is also attached before Director executes; it includes applied/skipped memory type counts, skipped reason/severity counts, blocking skipped counts, and source mapping samples. Medium-or-higher skipped memory quality issues can raise the repair budget, prioritize continuity/spatial validation, and skip polish before repair.

Non-dry-run preflight requires the `openai` package and `OPENAI_API_KEY`. Use `--require-claude` to also require the `anthropic` package, `ANTHROPIC_API_KEY`, and `CLAUDE_MODEL`.

Runtime configuration is centralized in `core.config` and is loaded from `.env` plus process environment variables. Empty strings are treated as missing values.

Common variables:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `ANTHROPIC_API_KEY`
- `CLAUDE_MODEL`
- `CLAUDE_MAX_TOKENS`
- `NOVELAGENT_MEMORY_PATH`
- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `NOVELAGENT_NOTION_DATABASE_ID`
