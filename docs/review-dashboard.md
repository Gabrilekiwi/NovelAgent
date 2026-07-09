# Review Dashboard

PR18 adds a static HTML dashboard built from `<review-output-dir>/review_index.json`.

The dashboard is read-only. It does not call an LLM, does not run repair, does not modify chapter prose, and does not update Memory V2. It is generated only when explicitly requested.

## Build From Main

```bash
python main.py --review-dashboard --review-output-dir .tmp/runtime/reviews
python main.py --review-dashboard --review-output-dir .tmp/runtime/reviews --review-dashboard-out .tmp/runtime/reviews/dashboard.html
python main.py --review-dashboard --review-output-dir .tmp/runtime/reviews --output-json
```

By default, the output file is:

```text
<review-output-dir>/dashboard.html
```

## Build From Script

```bash
python -B scripts/build_review_dashboard.py --review-output-dir .tmp/runtime/reviews
python -B scripts/build_review_dashboard.py --review-output-dir .tmp/runtime/reviews --out .tmp/runtime/reviews/dashboard.html
python -B scripts/build_review_dashboard.py --review-output-dir .tmp/runtime/reviews --json
```

## Contents

The page includes:

- summary counts from `review_index.json`
- latest review details
- recent review table
- links to `human_review_report.md`
- links to `rule_repair_prompt.md`
- links to `review_pipeline_summary.json`

When the index is empty, the dashboard still renders a valid page with an empty-state message.

## Scope

The HTML is self-contained and uses only inline CSS. There is no external JavaScript, CDN, frontend build step, Node, or npm dependency.
