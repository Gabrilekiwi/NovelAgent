# Real autonomy E2E release gates

`scripts/real_autonomy_e2e.py` is the opt-in, billable release harness for the
1-, 4-, 10-, and 20-or-more-chapter gates in the reliability/autonomy plan.
It is not run by the normal test suite.

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
- no environment setting or command-line flag whose name contains `NOTION`.

The final restriction is intentional: this release cycle permits only local,
required File Delivery and never initializes a Notion adapter.

## Running a gate

Use a clean shell with no Notion-related variables. For the one-chapter gate:

```powershell
$env:OPENAI_API_KEY = "<operator-provided-key>"
$env:OPENAI_MODEL = "<trusted-official-model>"
$env:OPENAI_MAX_OUTPUT_TOKENS = "6000"
$env:PROVIDER_MAX_ATTEMPTS = "1"
$env:NOVELAGENT_REAL_AUTONOMY_E2E = "I_ACCEPT_BILLABLE_OPENAI_CALLS:1"
python -B scripts/real_autonomy_e2e.py --chapters 1 --confirm-real-provider-calls --out .tmp/reports/real-autonomy-1.json
```

Repeat with matching values `4`, `10`, and `20` (or a larger long-run count)
only after reviewing the expected cost. The sentinel must match `--chapters`
exactly; it cannot authorize a different run size.

## Release evidence

A successful report is schema-validated and content-hashed. It contains no
absolute paths, identifiers in plaintext, prompts, chapter prose, provider
responses, request IDs, or credentials. For every chapter it proves:

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

`tests/test_real_autonomy_e2e.py` exercises the full path with an in-memory
fake OpenAI SDK. It performs no network call and verifies that Notion entry
points are never invoked.
