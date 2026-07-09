# Review Artifacts Index

Runtime review writes artifacts under `<review-output-dir>/<run_id>/`. PR17 adds a root-level index:

```text
<review-output-dir>/review_index.json
```

The index records recent review runs, their status, scores, gate result, and links to generated artifacts such as `human_review_report.md`, `rule_repair_prompt.md`, and `review_pipeline_summary.json`.

## Update Behavior

The index is updated only when runtime review is explicitly enabled with `--enable-review-pipeline`. Default `main.py --dry-run`, `main.py --check --dry-run`, and smoke runs do not create or update `review_index.json`.

Review failures are indexed too, using `review_status: "error"`, so review infrastructure errors are discoverable later.

## Lookup

```bash
python main.py --review-latest --review-output-dir .tmp/runtime/reviews
python main.py --review-latest --review-output-dir .tmp/runtime/reviews --output-json
python main.py --review-list --review-output-dir .tmp/runtime/reviews --review-list-limit 10
python main.py --review-list --review-output-dir .tmp/runtime/reviews --review-status blocked --output-json
python main.py --review-list --review-output-dir .tmp/runtime/reviews --review-gate-status fail --output-json
```

The lightweight script exposes the same read-only lookup:

```bash
python -B scripts/review_latest.py --review-output-dir .tmp/runtime/reviews --json
python -B scripts/review_latest.py --review-output-dir .tmp/runtime/reviews --list --limit 10
```

## Dashboard

Build a static dashboard from the same index:

```bash
python main.py --review-dashboard --review-output-dir .tmp/runtime/reviews
python -B scripts/build_review_dashboard.py --review-output-dir .tmp/runtime/reviews --json
```

The dashboard is generated only on explicit request and defaults to `<review-output-dir>/dashboard.html`.

The index does not call an LLM, does not execute repair, does not modify chapter prose, and does not write to Memory V2.
