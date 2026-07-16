# Operator-run real autonomy validation

> Scope decision (2026-07-16): the real single-chapter, four-chapter,
> ten-chapter, and 20-or-more-chapter runs are outside the current Codex goal.
> They are not cancelled. The operator runs them manually after the goal is
> complete and gives Codex the redacted reports for analysis. Codex must not run
> these billable gates as part of the current goal.

`scripts/real_autonomy_e2e.py` is the opt-in, billable operator harness for
those four post-goal manual validations. It is retained for the operator and is
not run by the normal test suite.

The harness creates a fresh event-authority StoryProject in an isolated short
Windows-safe temporary path. It loads a generated TrustedProfiles artifact,
uses an operator-owned UUID-to-directory File Delivery root map, executes the
normal `AutonomyRunner`/`AgentExecutor` path with the official OpenAI endpoint
and the built-in `strict` quality policy, and destroys the project after
verification. Only the redacted report named by `--out` survives.

## Safety gates

Execution requires all of the following:

- a gate count of exactly 1, 4, 10, or at least 20;
- `OPENAI_API_KEY` in the process environment (workspace `.env` loading is
  disabled);
- the exact count-bound sentinel
  `NOVELAGENT_REAL_AUTONOMY_E2E=I_ACCEPT_BILLABLE_OPENAI_CALLS:<count>`;
- the `--confirm-real-provider-calls` flag;
- the official OpenAI endpoint (custom/compatible base URLs are rejected);
- 6,000-8,000 maximum output tokens and at most two provider attempts;
- a clean, committed Git worktree before the first provider Intent;
- a new `--out` path that does not already exist;
- an explicit `NOVELAGENT_REAL_AUTONOMY_PROXY_MODE=inherit` or `clear`
  choice whenever any supported proxy variable is present;
- no environment setting or command-line flag whose name contains `NOTION`.

The final restriction is intentional: this release cycle permits only local,
required File Delivery and never initializes a Notion adapter.

## Operator manual run after goal completion

After the current goal is complete, use a clean shell with no Notion-related
variables. For the real single-chapter closed-loop test:

```powershell
$env:OPENAI_API_KEY = "<operator-provided-key>"
$env:OPENAI_MODEL = "<trusted-official-model>"
$env:OPENAI_MAX_OUTPUT_TOKENS = "6000"
$env:PROVIDER_MAX_ATTEMPTS = "1"
$env:NOVELAGENT_REAL_AUTONOMY_PROXY_MODE = "clear" # or "inherit" after explicit review
$env:NOVELAGENT_REAL_AUTONOMY_E2E = "I_ACCEPT_BILLABLE_OPENAI_CALLS:1"
python -B scripts/real_autonomy_e2e.py --chapters 1 --confirm-real-provider-calls --out .tmp/reports/real-autonomy-1.json
```

The later manual stages are the real four-chapter continuity test, real
ten-chapter unattended test, and real 20-or-more-chapter endurance test. Repeat
with matching values `4`, `10`, and `20` (or a larger long-run count) only after
reviewing the expected cost. The sentinel must match `--chapters` exactly; it
cannot authorize a different run size.

The harness reserves `--out` before creating the isolated project. It never
overwrites an operator-owned artifact. On an execution or verification failure,
the isolated project is removed first and the reservation is replaced with a
content-hashed, redacted failure report. That report contains only allowlisted
exception types, failure categories, HTTP status when safely available, attempt
counts, and Intent/Receipt/chapter counts; it never contains provider messages,
prompts, generated prose, request IDs, credentials, or absolute paths.

## Release evidence

A successful report is schema-validated and content-hashed. It contains no
absolute paths, identifiers in plaintext, prompts, chapter prose, provider
responses, request IDs, or credentials. For every chapter it proves:

- a clean Git commit and stable code-bundle hash shared by preflight and every
  chapter execution, plus the exact release-harness hash;
- durable successful OpenAI Intent/Receipt evidence for the official endpoint,
  with configured and provider-returned model identities stored only as hashes;
- one immutable outline and one canonical prose file;
- canonical prose within the 3,000-4,500 non-whitespace character release range;
- exact prose-byte hashes bound through the Memory 2.2 Event batch;
- a continuous authority head and contiguous chapter range;
- a committed PublicationReceipt and chained completion receipt;
- one required File Delivery whose bytes are read back and whose exported
  Event batch and prose hash match the publication;
- the unchanged built-in `strict` policy, available LLM Validator evidence,
  deterministic review, narrative rules, and an accepted quality decision.

The SLO section separately records logical chapter attempts, logical model
calls, physical provider attempts, provider transport retries, quality repair
attempts, first-pass chapters, and system failures. Median logical chapter
attempts above two fails the gate. Any `ContextBudgetError`, internal
`ValueError`, uncertain provider intent, unreceipted source edit, chapter gap,
or delivery gap fails without producing a successful report.

## Codex analysis after the manual run

Give Codex only the redacted JSON report produced at `--out`; do not provide
credentials, workspace `.env` files, prompts, provider responses, or generated
chapter prose. Codex will analyze the report schema and content hash, requested
and completed chapter counts, contiguous authority/Event history,
Intent/Receipt evidence, publication and File Delivery evidence, failure
category, and SLO counters. It will report which capability claim the evidence
supports and will not rerun the provider test.

Current-goal completion does not claim that any manual real validation passed.
Until qualifying reports are supplied, real verification remains absent and
autonomy remains opt-in.

## Current manual-validation status

On 2026-07-15 the first one-chapter gate of the user's five-chapter authorization
entered the normal runner/provider path but failed before completing a chapter.
The then-current harness reduced the exception to `autonomy_execution_failed`
and did not retain enough redacted diagnostics to prove the exact provider
cause. It is therefore failure evidence only, not a successful one-chapter
report. The four-chapter gate was not started, and that old authorization is now
consumed. No Notion call was made. On 2026-07-16 the operator moved every future
1/4/10/20+ run outside the current Codex goal. Any later operator-run test still
requires the harness's exact count-bound sentinel and explicit confirmation.

`tests/test_real_autonomy_e2e.py` exercises the full path with an in-memory
fake OpenAI SDK. It performs no network call and verifies that Notion entry
points are never invoked.
