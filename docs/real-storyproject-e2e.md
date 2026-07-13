# Real StoryProject Two-Chapter E2E

`scripts/real_storyproject_e2e.py` is the billable, opt-in release gate for strict semantic production. It copies a user-supplied redacted real StoryProject into a temporary directory, clears only the copied runtime, activates the supplied qualified calibration report, and runs two chapters through the real OpenAI generation path.

The source sample is never modified. The default test suite skips the provider run.

## Prerequisites

- A redacted real StoryProject with two consecutive outlines and the preceding committed prose.
- A stable `.novelagent/project.json` in the sample.
- A qualified, tamper-evident calibration report bound to that `book_id`.
- `OPENAI_API_KEY` and the intended `OPENAI_MODEL`/endpoint configuration.

Synthetic fixtures cannot satisfy this release gate.

## Run

PowerShell example:

```powershell
$env:NOVELAGENT_REAL_STORYPROJECT_E2E='1'
$env:NOVELAGENT_REAL_STORYPROJECT_SAMPLE='D:\redacted\book'
$env:NOVELAGENT_REAL_STORYPROJECT_CALIBRATION_REPORT='D:\redacted\calibration.json'
python scripts/real_storyproject_e2e.py `
  --sample $env:NOVELAGENT_REAL_STORYPROJECT_SAMPLE `
  --calibration-report $env:NOVELAGENT_REAL_STORYPROJECT_CALIBRATION_REPORT `
  --out .tmp/real_storyproject_e2e_report.json
```

Alternatively, pass `--confirm-real-provider-calls` as the explicit opt-in. The pytest entrypoint is:

```bash
python -m pytest -p no:cacheprovider tests/test_real_storyproject_e2e.py -q
```

## Bounded profile

The gate fixes two chapters and at most two scenes per chapter. It refuses unsafe configuration above:

- 2 provider attempts per operation;
- 90 seconds per OpenAI request;
- 2,000 output tokens per request;
- 8 observed OpenAI attempts across the run.

When the corresponding environment variables are unset, the script uses stricter defaults: one provider attempt, 90-second timeout, and 1,200 output tokens.

## Evidence and redaction

Success requires proof that:

- both chapters used authoritative strict state and committed;
- the second chapter read the first chapter's committed prose by verified SHA-256;
- the second semantic parse read a managed projection;
- Memory V2 replay verifies a two-chapter hash chain at the final run revision.

The schema-checked output report stores only model/limit metadata, hashed run identifiers, chapter indexes/statuses, Memory revisions, the calibration report hash, and booleans for the required evidence. It does not store sample paths, prose, prompts, provider responses, or credentials.

Claude and Notion remain separate real-environment gates through `scripts/provider_smoke.py`; the two-chapter StoryProject gate intentionally isolates OpenAI semantic production from those delivery checks.
