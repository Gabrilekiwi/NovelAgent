# Review Gate

Review Gate is an optional CLI exit-code layer on top of the runtime Review Pipeline. It is disabled by default and only works when `--enable-review-pipeline` is also supplied.

```bash
python main.py \
  --dry-run \
  --persist-dry-run \
  --enable-review-pipeline \
  --review-gate blocked \
  --review-output-dir .tmp/runtime/reviews \
  --memory data/notion_memory.example.json
```

The gate reads `review_pipeline.status`, records a `review_gate` object in the run record, prints normal output first, then exits with code `1` when the configured threshold is met.

## Thresholds

`off` disables the gate.

`blocked` fails for review status `blocked` or `error`.

`needs_revision` fails for `needs_revision`, `blocked`, or `error`.

`warning` fails for `warning`, `needs_revision`, `blocked`, or `error`.

## Run Record

When enabled, the run record includes:

```json
{
  "review_gate": {
    "schema_version": "1.0",
    "enabled": true,
    "threshold": "blocked",
    "status": "fail",
    "matched": true,
    "review_status": "blocked",
    "reason": "review status blocked meets gate threshold blocked",
    "exit_code": 1
  }
}
```

Gate failure does not change `committed`, `rejected`, snapshot writes, memory writeback, validator behavior, repair behavior, or chapter prose. It does not call an LLM, does not write back to Memory V2, and does not integrate oh-story or external APIs.

## CI Usage

Use `--output-run-json` when CI needs both a machine-readable run record and a failing exit code:

```bash
python main.py \
  --dry-run \
  --persist-dry-run \
  --enable-review-pipeline \
  --review-gate needs_revision \
  --memory data/notion_memory.example.json \
  --output-run-json
```

The JSON is printed before the process exits with the gate code.
