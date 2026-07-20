# Runtime Commands

## StoryProject semantic modes

StoryProject semantic parsing defaults to read-only `shadow`. Inspect it without changing generation authority:

```bash
python main.py --story-project PATH --chapter auto --story-state-shadow-report --output-json
```

Strict authority requires a qualified target-book calibration report and an explicit activation command:

```bash
python main.py --story-project PATH --activate-story-state --story-state-calibration-report PATH/report.json
```

After activation, persistent generation must use transactional apply writeback:

```bash
python main.py --story-project PATH --chapter auto --steps 2 --story-project-writeback
```

Pinned parser/schema/layout drift fails before provider generation. `--allow-story-state-shadow-downgrade` is an explicit diagnostic fallback only; it leaves the semantic state non-authoritative and `ready_for_next_step=false`. Full calibration thresholds and audit behavior are in [Story State Calibration and Strict Activation](story-state-activation.md).

The billable two-chapter real OpenAI gate is isolated in `scripts/real_storyproject_e2e.py`, is skipped by default, and emits a schema-checked redacted report. See [Real StoryProject Two-Chapter E2E](real-storyproject-e2e.md).

The newer reliability/autonomy path has a separate claim boundary. See the [Reliability and autonomy capability status](reliability-autonomy-capability-status.md) before treating source or synthetic tests as real release evidence. A complete 50-chapter deterministic simulation passed against code commit `bfa3d04` in 1298.864 seconds; its [retained report](reliability-autonomy-50-chapter-evidence.json) closes only the synthetic long-run gate. The same clean commit passed 1317 unit tests (7 platform skips) and v1 smoke with 21/21 preflight checks. The opt-in, billable real single-chapter, four-chapter, ten-chapter, and 20-or-more-chapter harness is documented in [Operator-run real autonomy validation](real-autonomy-e2e.md). Those runs are outside the current Codex goal: after goal completion the operator runs them manually and gives Codex only the redacted reports for analysis. The failed 2026-07-15 single-chapter attempt remains historical failure evidence, not a success. Until qualifying reports exist, real verification remains absent and autonomy remains opt-in. The harness rejects every Notion setting and allows only required trusted File Delivery; no Notion call was made in this upgrade run.

Legacy-book event-authority migration is paused and outside the current Codex goal. Keep the existing preview, approval, and execution tools as dormant retained functionality; do not run a new preview, build a `MigrationApproval`, execute migration, or activate the active legacy book. If the operator later resumes this work explicitly, start from a fresh read-only preview because retained preview data may have become stale.

Whole-project remap is an explicit post-move recovery operation. The operator first performs a same-volume directory rename, then runs `--remap-roots` against the moved StoryProject's canonical persistence control plane with the original root UUID and revision/digest CAS. The CLI verifies the preserved StoryProject and every logical-root directory identity, requires pre-armed locks and an absent old path, blocks pending/recovery transactions and active sessions/leases, and forward-rebinds every recognized embedded main/EA/migration/history registry with the main registry last. It never moves or copies files; cross-volume, copy-delete, case-only, replaced-root, unknown-registry, and external mutable EA-root cases fail closed. This is not default portability, has no retained real target-project report, and does not claim protection against arbitrary ancestor replacement beyond the documented checked/no-create lock boundaries. Immutable historical manifests may retain absolute-path snapshots.

Initialize local runtime state from committed examples:

```bash
python main.py --init-runtime
```

This creates `.tmp/runtime/snapshot.json`, `.tmp/runtime/notion_memory.json`, `.tmp/runtime/runs/`, and `.tmp/runtime/chapters/` from `data/snapshot.example.json` and `data/notion_memory.example.json`. Existing runtime files are skipped unless `--force-init-runtime` is supplied.

Preflight without model calls:

```bash
python main.py --check --dry-run --memory data/notion_memory.example.json
```

Local v1.0 smoke gate:

```bash
python -B scripts/smoke_v1.py
```

The smoke gate runs `unittest` discovery, preflight, one persisted dry-run, file memory writeback, run reporting, and a provider-smoke missing-config diagnostic check. It writes an isolated temporary snapshot, run directory, chapter directory, memory outbox, and provider smoke report under `.tmp/smoke_v1/...`, then asserts that the committed run, chapter artifact, input pack artifact, snapshot pack artifact, chapter pipeline artifacts, scene span metadata, report summary, outbox, and provider missing-config report were produced. Use `--skip-tests` to exercise only the runtime CLI flow.

By default, `--check` prints a concise human-readable summary. Use `--check-json` when full machine-readable diagnostics are needed:

```bash
python main.py --check --check-json --dry-run --memory data/notion_memory.example.json
```

Preflight diagnostics include Memory input resolution (`auto`, `file`, or `notion`), the selected Memory source, source-mapping counts, run-history contract status, latest run validation coverage, latest loop-session last-run validation coverage, execution mode, persistence mode, expected model-call stages, the schema-checked `state_builder_audit`, `planned_workflow` for the default rule Director, and `planned_flow` with the schema-checked auditable dynamic flow plan. The plain preflight summary prints compact State Builder counts for applied/skipped memory types, skipped reason/severity counts, deduplicated items, and blocking skipped items while `--check-json` retains the full item-level audit and source mappings. Loaded Memory contexts include item-level source mappings, and runtime input packs expose a compact Memory Index for tracing loaded items back to file lines or Notion page ids/URLs plus a Recovery Context for last-run problem codes, validation coverage gaps, and repair summaries. In non-dry-run mode, it checks that the OpenAI SDK is installed and `OPENAI_API_KEY` is set. If the planned workflow includes `polish`, preflight also checks that the Anthropic SDK is installed plus a Claude key (`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`) and a Claude model (`CLAUDE_MODEL` or `ANTHROPIC_MODEL`).
It also verifies the required v1.0 engineering structure, prompt Markdown files, JSON schema assets, embedded schema consistency, and the selected Memory input before any generation step runs.
Artifact targets are checked as well: `--run-dir`, its `snapshot_packs`, `input_packs`, and `loop_sessions` subdirectories, and `--chapter-dir` must be directories or be creatable as directories before generation starts.

Inspect recent persisted run records without generating a chapter:

```bash
python main.py --report-runs
```

Use `--report-limit 0` when only aggregate counts are needed. The report includes status counts, validation problem counts, compact validation evidence, compact Director details, workflow plan summaries, trace summaries with repair evidence, memory writeback status/type counts, loop session summaries, and artifact path existence checks. Malformed run result envelopes or run objects that fail the full run-record contract are skipped and listed under `skipped`; malformed loop sessions are listed under `skipped_loop_sessions`.

Single dry-run step:

```bash
python main.py --dry-run --memory data/notion_memory.example.json
```

Generation commands print a concise chapter/run/validation summary by default, including requested validation focus, executed checks, and skipped checks when available. Failed run summaries include the run error plus compact model-call diagnostics when a Director, generation, or repair provider call failed. If Claude polish fails after base chapter generation, the run continues with the unpolished generated chapter; the default summary reports that polish was skipped after failure without printing the raw provider error, while the run trace still records the full diagnostics. Use `--output-json` for the full result object, or `--output-run-json` for only the run record. For `--steps` greater than 1, `--output-json` returns the full loop result with `session`, all step `runs`, `completed_steps`, `stopped_reason`, and `last_result`; `--output-run-json` still prints only the last run record.
For multi-step concise runs, progress lines are printed to stderr at loop start, step start, step end, failure, and loop end. These lines include step number, run id, commit status, and `duration_ms`, so a long real-provider loop shows where it is waiting without corrupting JSON output. Add `--no-progress` to suppress them.
Trace events and loop sessions also store `duration_ms`; loop sessions include `step_timings[]`, and `--report-runs` summarizes those timings.
Chapter-plan JSON failures get one repair attempt: the invalid payload is sent back to the model with a JSON-only schema instruction. If the repaired payload is still invalid, the run fails as a chapter-generation failure. High-confidence mojibake output is rejected by model-output contracts before commit.

```bash
python main.py --dry-run --output-json --memory data/notion_memory.example.json
python main.py --dry-run --output-run-json --memory data/notion_memory.example.json
```

Memory input mode can be selected explicitly:

```bash
python main.py --check --dry-run --memory-source file --memory data/notion_memory.example.json
python main.py --check --dry-run --memory-source notion
python main.py --check --dry-run --notion-memory
```

`auto` remains the default. It uses a provided memory file path first, otherwise uses live Notion API when `NOTION_API_KEY` and a Notion database id are configured, otherwise falls back to `NOVELAGENT_MEMORY_PATH` or `.tmp/runtime/notion_memory.json`. Preflight reports this decision under `memory_input`, including the resolved source, resolved file path when applicable, Notion configuration flags, and the resolution reason.
`--notion-memory` is a shortcut for `--memory-source notion`.
`--notion-sync` is the live Notion end-to-end shortcut: it reads memory from Notion, writes committed memory updates back to Notion, and enables readback verification. It is equivalent to selecting Notion memory plus `--memory-writeback notion --notion-readback`.

Use a model-backed Director instead of the default offline rule Director:

```bash
python main.py --director-model gpt-4.1-mini --memory data/notion_memory.example.json
```

This mode uses the OpenAI client and requires `OPENAI_API_KEY`.
That requirement applies even with `--dry-run`, because `--dry-run` only replaces chapter generation and polish outputs, not the model-backed Director.
For non-dry-run model Director mode, preflight conservatively checks Claude configuration because the model may choose a workflow containing `polish`.

Run the optional story-level LLM Validator:

```bash
python main.py --check --dry-run --llm-validator
python main.py --llm-validator
```

This uses OpenAI and is never enabled implicitly by dry-run or CI. Preflight requires `OPENAI_API_KEY` and the OpenAI package before this stage can run. LLM validation output is schema-checked and merged as `validator="llm"` problems with evidence, severity, repair hints, and an `area` enum covering complex plot logic, character motivation consistency, timeline causality, setup/payoff, and emotional/theme drift.

If local proxy variables point provider SDK traffic at an unavailable proxy, add `--no-proxy` to `main.py` commands or set `NOVELAGENT_NO_PROXY=1` in `.env`. This clears `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and their lowercase variants before OpenAI, Claude, or Notion calls run.

Run real provider smoke checks after local gates are stable:

```bash
python -B scripts/provider_smoke.py --providers openai claude
python -B scripts/provider_smoke.py --providers notion --notion-write
```

The provider smoke writes diagnostics under `.tmp/runtime/provider_smoke/<timestamp>/` by default and records `provider_smoke_report.json`. It initializes isolated runtime state from examples, can override the OpenAI smoke model with `--openai-model`, can override or clear a custom OpenAI endpoint with `--openai-base-url` or `--no-openai-base-url`, caps OpenAI input-pack size with `--max-input-chars`, caps OpenAI response size with `--max-output-tokens`, controls hidden OpenAI SDK retries with `--openai-max-retries`, can override Claude with `--claude-model`, `--claude-base-url`, `--claude-user-agent`, and `--claude-max-tokens`, applies `--request-timeout` to OpenAI, Claude, and Notion requests, and limits chapter generation to a small smoke path. Claude uses the Anthropic Messages-compatible path through the Anthropic SDK. For MicuAPI-style Claude external compatibility, set `CLAUDE_BASE_URL` or `ANTHROPIC_BASE_URL` to the Anthropic-compatible root URL, not an OpenAI `/v1` endpoint; use `ANTHROPIC_AUTH_TOKEN` if the gateway documents that variable name, and set `CLAUDE_USER_AGENT` when the gateway requires a Claude CLI user agent. Use `--retries` and `--retry-delay-seconds` for bounded retries on non-writing subchecks such as OpenAI model calls, Claude polish, and Notion reads; Notion writeback is intentionally not retried to avoid duplicate pages. OpenAI SDK retries default to 0 in provider smoke so the visible `--retries` budget controls repeat attempts. Add `--no-proxy` to clear `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` for the smoke process when local proxy environment variables point at a dead or unwanted proxy. OpenAI diagnostics report Director and chapter-generation subchecks separately; if the model Director fails, the generation subcheck uses a rule Director fallback so chapter generation is still tested. Claude reports a `polish` subcheck after credentials are available. Notion writes are opt-in with `--notion-write`; without that flag the Notion check reports `writeback` and `readback` as skipped, and with the flag it reports `read`, `writeback`, and remote `readback` subchecks. The schema-checked report includes `request` for requested providers, Notion write intent, missing-config mode, dotenv handling, and proxy clearing intent. It includes `config_status` with redacted `set`/`missing` flags, selected models, timeout/token/retry limits, SDK/default endpoint status, and redacted proxy endpoint metadata, but never writes credential values. It also includes `required_checks[]` plus `required_checks_ok`, a flat Phase 4 completion checklist for OpenAI Director, OpenAI chapter generation, Claude polish, Notion read, Notion writeback, and Notion readback. It also includes `diagnostics.status`, `diagnostics.missing_config`, `diagnostics.missing_config_groups`, `diagnostics.failed_checks`, `diagnostics.skipped_checks`, and `diagnostics.unrequested_checks` so provider failures can be triaged without expanding the full report. Missing provider config is aggregated per provider, so Claude can report either `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, either `CLAUDE_MODEL` or `ANTHROPIC_MODEL`, and Notion can report `NOTION_API_KEY` plus the accepted database id variables in one run. `diagnostics.missing_config_groups[]` keeps one-of requirements explicit, such as `NOTION_DATABASE_ID` or `NOVELAGENT_NOTION_DATABASE_ID`. Use `--require-all-checks` when the command should fail unless every required check passes. Use `--allow-missing` when you want a local diagnostic report that skips providers whose real credentials are not configured. Add `--ignore-dotenv` to prove the missing-credential path on a machine that has local `.env` keys.

Multi-step dry-run without persistence:

```bash
python main.py --dry-run --steps 2 --memory data/notion_memory.example.json
```

Accepted dry-run steps are `preview` results: the loop advances an in-memory Snapshot and passes the previous result into the next step, but leaves the on-disk Snapshot, Memory, chapters, and run records unchanged. `--steps` must be at least 1. CLI parsing rejects invalid values before execution, and preflight reports the same constraint under `loop_parameters`.

StoryProject multi-step production:

```bash
python main.py --story-project auto --chapter 2 --steps 2 --story-project-writeback
```

StoryProject `--steps > 1` requires real `--story-project-writeback`; it rejects global dry-run, writeback preview, and writeback-disabled combinations. The first explicit `--chapter N` is the starting chapter. Each later step rebuilds StoryProject Context from current files and advances to `N+1` only after a complete transaction. With `--chapter auto`, a rescan that does not equal the expected next chapter stops with `story_project_sequence_drift`. `--continue-on-rejection` consumes an attempt but retries the same chapter; context failure, writeback failure, or a missing next outline always stops before another provider call.

Legacy/local persistence accepted runs use a journal under `<run-dir>/transactions/<run-id>/`. Event-authority runs instead use the project-local Persistence v2.1 control plane behind the Event Authority global recovery barrier. Both paths bind StoryProject prose/tracking targets and Snapshot with before/after hashes and a durable marker before the final chapter artifact and RunRecord are published. Startup reconciles unfinished local journals automatically; the explicit legacy/local recovery-only command is:

```bash
python main.py --reconcile-persistence --run-dir .tmp/runtime/runs
```

The command performs only deterministic rollback or marker-backed publication. It never force-overwrites an externally changed target.

Persist dry-run state and run records:

```bash
python main.py --dry-run --persist-dry-run --steps 2 --memory data/notion_memory.example.json
```

Persist dry-run state and append committed memory updates to a local outbox:

```bash
python main.py --dry-run --persist-dry-run --memory data/notion_memory.example.json --memory-writeback file
```

The explicit equivalent is:

```bash
python main.py --dry-run --persist-dry-run --memory data/notion_memory.example.json --memory-writeback file --memory-outbox .tmp/runtime/memory_outbox.jsonl
```

If `--memory-writeback file` is used without `--memory-outbox`, the default path is `.tmp/runtime/memory_outbox.jsonl`.

Write committed memory updates directly to Notion:

```bash
python main.py --memory data/notion_memory.example.json --memory-writeback notion
python main.py --notion-sync
```

Notion writeback requires `NOTION_API_KEY` plus `NOTION_DATABASE_ID` or `NOVELAGENT_NOTION_DATABASE_ID`.

Add `--notion-readback` to query the Notion database after writeback and verify that written `Memory ID`, `Type`, and `Name` values can be read back:

```bash
python main.py --memory data/notion_memory.example.json --memory-writeback notion --notion-readback
```

Reuse that outbox as the next memory source:

```bash
python main.py --dry-run --memory .tmp/runtime/memory_outbox.jsonl
```

Use custom artifact directories:

```bash
python main.py --dry-run --persist-dry-run --run-dir .tmp/runs --chapter-dir .tmp/chapters --memory-outbox .tmp/memory_outbox.jsonl
```

## Local State Boundaries

Runtime output should stay out of source control. The default local-only targets are ignored by git:

- `.tmp/`: tests, smoke runs, and disposable local experiments.
- `.tmp/runtime/snapshot.json`: default mutable runtime snapshot.
- `.tmp/runtime/runs/`: persisted run envelopes plus snapshot and input pack artifacts.
- `.tmp/runtime/chapters/`: generated chapter bodies.
- `.tmp/runtime/memory_outbox.jsonl`: default file-based memory writeback queue.
- `data/snapshot.json`, `data/memory.json`, `data/memory_outbox.jsonl`, and `data/memory_outbox*.jsonl`: legacy local runtime files.

Committed sample state stays under `data/*.example.json`, especially `data/snapshot.example.json` and `data/notion_memory.example.json`.

Python and tooling caches such as `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, coverage output, logs, `.env`, `.venv/`, and `.idea/` are also ignored. If a cache or local config file was already tracked before these rules existed, clean it up as an explicit git-index maintenance step rather than mixing it into feature work.

Persistent runs write schema-checked run result envelopes:

- `.tmp/runtime/runs/*.json`: structured run records with schema-checked `chapter.pipeline.stages`.
- `.tmp/runtime/runs/loop_sessions/*.json`: schema-checked loop session records for multi-step runs, including per-step timings.
- `.tmp/runtime/runs/snapshot_packs/*.md`: Snapshot Builder input packs built from base snapshot plus normalized memory.
- `.tmp/runtime/runs/input_packs/*.md`: full input packs used for generation and repair context; run records also store schema-checked input pack metadata.
- `.tmp/runtime/runs/chapter_pipeline/*`: chapter plan, scene drafts, merged chapter, validation report, and repair delta artifacts for the pipeline stages.
- `.tmp/runtime/chapters/*.md`: chapter body artifacts for committed and rejected runs.
- `.tmp/runtime/memory_outbox.jsonl`: default committed memory updates when `--memory-writeback file` is set without `--memory-outbox`.

Run result envelopes pass a shared runtime validator that checks both the top-level `run_result.schema.json` envelope and the embedded full `run_record.schema.json` object. The executor uses this before writing a run artifact, loop failure recovery uses it before reloading the newest failed run, reports use it before summarizing saved runs, and history recovery uses it before attaching `memory_context.last_run`. Preflight exposes the same boundary under `run_history`; invalid saved run or loop-session artifacts fail startup diagnostics instead of being silently ignored by recovery planning.

Run records include Snapshot Builder artifact links, State Builder memory-application audit, a compact `recovery_context`, the schema-checked `workflow_plan`, schema-checked `state_update`, and schema-checked `trace` events for each executed workflow action. The run `chapter.pipeline.stages` list records `plan_chapter`, `generate_scenes`, `merge_scenes`, `validate`, `repair`, and `commit` with completed/skipped/failed status, artifact keys, and compact summaries. `chapter.pipeline.scene_spans` maps each scene draft to its character range in the merged chapter, and scene draft artifacts include the same span metadata. The run `memory` summary stores item count plus source-mapping coverage counts by mapping source, file path, JSONL line, Notion page id, and Notion page URL, so persisted artifacts can prove how loaded Memory was traced even without opening the full input pack. Workflow plan steps include execution mode, skip condition, and failure policy so optional and conditional work can be audited separately from required work; runtime validation also rejects plan/action mismatch, non-contiguous step indexes, and step metadata drift. Trace events carry the matching workflow plan step index, step mode, failure policy, and `duration_ms` so the Director's plan can be compared directly with the action path actually executed. Multi-step loops also write a schema-checked `loop_session` record with requested/completed steps, stop reason, committed/rejected/failed counts, run ids, `step_timings[]`, per-run validation coverage, compact validation evidence, compact repair evidence, per-run workflow actions, per-run trace actions, per-run trace/plan alignment, recovery links, artifact pointer, and a session-level error when the loop stops on an exception. Validation summaries include problem codes, blocking/warning counts, severity counts, deterministic/manual-review repair counts, repair action counts, requested validation focus, executed checks, skipped checks, and `problem_evidence`. Full Validator results enrich each problem with `repair_action`, normalized `repair_parameters`, and structured `evidence`, which the run record summarizes and the repair plan consumes before falling back to problem-code metadata. `state_update` records whether the commit was applied, the chapter index transition, timeline additions, extracted change counts, and memory update counts by type. Each trace event records timestamps, completion status, chapter length, validation state when present, repair attempt count, model stage/provider/model/invocation details for model-capable actions, model-call diagnostics when provider calls fail, and the schema-checked repair plan when `repair_if_needed` runs. Conditional repair trace events record `skipped` plus `skip_reason` when validation is already ok or the Director supplied no repair budget. Repair trace events also include repair plan risk level, budget, deterministic/manual-review counts, `repair_plan.recovery.failure_modes`, step evidence, and per-attempt `repair_deltas` with before/after problem counts and resolved/new/remaining problem codes, making repair effectiveness auditable without diffing full validation payloads. Run reports and loop sessions expose compact `repair_evidence` summaries and timing data from these records. Repair plan step parameters are restricted to registered fields, so unexpected Validator fields cannot silently become strategy inputs. Dry-run repair executes that plan by dispatching ordered `repair_plan.steps[]` through registered local strategies, so the trace action list matches the actual repair path. Non-dry-run repair sends the same plan plus explicit Recovery Context to the model repair stage. Model-call diagnostics use stage names such as `director_decision`, `chapter_generation`, `claude_polish`, and `scene_repair`.

Model output contracts reject non-prose returns before they can be committed as chapter text. Empty output, JSON-like structured data, fenced code blocks, Markdown wrappers/headings, standalone chapter headings such as `Chapter 4`, and assistant commentary such as `Here is...`, `As an AI...`, or `Error:...` raise `ModelOutputError`. Chapter-generation contract failures remain failed-run diagnostics. Claude polish contract failures are recorded on the polish trace event, then validation continues against the unpolished generated chapter.

Snapshots may include a `project_profile` object:

```json
{
  "project_profile": {
    "language": "zh-CN",
    "known_characters": ["陆砚", "阿照"],
    "known_locations": ["黑月集市", "第七码头"]
  }
}
```

The profile is included in the input pack. When `language` is set to `zh-CN`, scene generation and Claude polish output must remain Simplified Chinese before the chapter can pass the model output contract. Known characters and locations are also available to the analyzer, which reduces false character/location extraction for Chinese novels and new projects.

Committed runs validate the full chapter analysis result before the Snapshot is advanced or memory writeback is considered. The run record stores a compact analysis summary, while the state update path consumes the schema-checked full analysis object. Memory writeback then passes through a quality gate: final validation must be ok, and the final repair delta must not contain new, remaining, or post-repair problem codes. The gate records pending memory update count/type summaries before it allows or blocks writes. If the gate blocks writeback, the run still records schema-checked `run.memory.writeback.gate` with the reasons and writes no outbox or Notion pages. Successful or skipped writeback records schema-checked `item_mappings` so each generated memory item can be traced to an outbox line or Notion page id/URL. File writeback records readback `verification` for written JSONL lines. CLI-created Notion writeback queries existing pages before writing and skips duplicate `Memory ID` values while recording the existing page mapping. Notion writeback records response-level verification by default, and `--notion-readback` upgrades this to database readback verification with `status="verified"` when every written memory item can be queried back by `Memory ID`.

Schema consistency checks compare duplicated embedded run-record blocks with their standalone schema files, including the nullable embedded workflow plan and state update audit, so artifact contracts fail fast if one side changes without the other. The same guard runs in automated tests and preflight.

If the Director raises an exception or returns an invalid decision during a persistent run, the engine writes a `failed` run record with schema-checked `director_error` diagnostics before re-raising the original exception. Model-backed Director provider failures include `run.director.model_call` diagnostics. If the decision is valid but cannot be normalized into a legal workflow, the engine writes `workflow_error` diagnostics. If chapter generation or another required workflow action raises an exception, the engine writes a `failed` run record and input pack artifact before re-raising the original exception. Claude polish failures are the exception: once a base chapter exists, polish `ModelCallError` and `ModelOutputError` diagnostics are stored in the trace and the run continues with the unpolished chapter.

If a multi-step loop stops because one step raises after writing a failed run record, the loop session includes that failed run summary, `stopped_reason="failed"`, and the original error type/message. If the exception happens before a run record can be written, the loop session still records the session error and any earlier completed runs.

Continue after a rejected or failed run so the next step can use recovery context:

```bash
python main.py --steps 2 --continue-on-rejection --memory data/notion_memory.example.json
```

Recover the latest failed or rejected pre-polish draft without updating the snapshot:

```bash
python main.py --recover-latest --run-dir .tmp/runtime/runs --chapter-dir .tmp/runtime/chapters
```

This writes `chapter_XXXX_recovered_<run_id>.md` to the chapter directory. It is intentionally separate from normal commit logic, so it cannot silently advance chapter state.

Recovery context includes validation problem codes plus blocking/warning counts, severity counts, validation coverage, compact validation evidence, compact repair plan summaries, compact repair evidence, and compact repair delta summaries from the last run. The rule Director uses that structured summary to choose validation focus and repair budget, model-backed Director receives the same compact `last_run` payload, chapter generation receives a first-class `# Recovery Context` section in the runtime input pack, and scene repair receives the same structure as explicit model payload context. Each run record stores which last run was attached under `recovery_context`; loop sessions collect those edges under `recovery_links` so multi-step recovery can be audited after the fact. If the previous run skipped continuity, spatial, or logic checks, the next recovery decision prioritizes those skipped checks before commit. If the previous repair plan carried critical risk, needed manual review, or exhausted its budget without a commit, the next recovery budget is raised before polish. If the previous repair stalled or introduced new problem codes, recovery budget is also raised and validation focus is derived from remaining/new problem codes. The current run's `snapshot_builder_audit` is also attached before Director executes; it includes applied/skipped memory type counts, skipped reason/severity counts, blocking skipped counts, and source mapping samples. Medium-or-higher skipped memory quality issues can raise the repair budget, prioritize continuity/spatial validation, and skip polish before repair.

Non-dry-run preflight requires the `openai` package and `OPENAI_API_KEY`. Use `--require-claude` to also require the `anthropic` package, a Claude key (`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`), and a Claude model (`CLAUDE_MODEL` or `ANTHROPIC_MODEL`).

Runtime configuration is centralized in `core.config` and is loaded from `.env` plus process environment variables. Empty strings are treated as missing values. `.env.example` lists variable names and recommended model defaults only; it must not contain real keys.

Use the UTF-8 snapshot maintenance helper after manual snapshot edits, especially on Windows shells:

```bash
python -B scripts/snapshot_utf8.py --snapshot .tmp/runtime/snapshot.json --write-normalized
```

The script loads JSON as UTF-8, validates it through the runtime snapshot normalizer, reports suspicious replacement text such as `????` or `\ufffd`, and optionally rewrites normalized UTF-8 JSON.

Common variables:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_TIMEOUT_SECONDS`
- `OPENAI_MAX_OUTPUT_TOKENS`
- `OPENAI_MAX_RETRIES`
- `OPENAI_STREAM`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_AUTH_TOKEN`
- `CLAUDE_BASE_URL`
- `ANTHROPIC_BASE_URL`
- `CLAUDE_USER_AGENT`
- `CLAUDE_MODEL`
- `ANTHROPIC_MODEL`
- `CLAUDE_MAX_TOKENS`
- `CLAUDE_TIMEOUT_SECONDS`
- `CLAUDE_STREAM`
- `NOVELAGENT_MEMORY_PATH`
- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `NOVELAGENT_NOTION_DATABASE_ID`
- `NOTION_TIMEOUT_SECONDS`
