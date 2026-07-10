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

The gate reads `review_pipeline.status` and records a `review_gate` object. When the gate is enabled, `fail` or `error` rejects the run before any canonical Snapshot, Memory, or StoryProject transaction, and the CLI exits with code `1`; the rejected RunRecord remains available as audit evidence.

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

Gate failure sets `accepted=false`, `committed=false`, and `status="rejected"`; Snapshot, Memory, StoryProject prose, and tracking files remain unchanged. With automatic review repair enabled, every attempt reruns Validation, Review, and Gate, including strict `warning` gates. Review `error` fails closed and is not repaired. The gate itself does not call an LLM, execute oh-story, or contact an external API.

Runtime review also updates `review_index.json`; gate status is stored there so `python main.py --review-list --review-gate-status fail` can find recent gate failures.

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
