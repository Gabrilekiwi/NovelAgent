# Review Regression Fixtures

PR 13 adds a deterministic review regression harness for the chapter review chain:

1. Chapter Quality Evaluation
2. Rule-aware Validation
3. Rule-aware Repair Plan
4. Rule-aware Repair Prompt
5. Human-readable Review Report

The fixtures live in `tests/fixtures/review_regression/`. Each case contains:

- `snapshot.json`: read-only runtime snapshot context.
- `previous_chapter.md`: previous chapter tail used for continuity checks.
- `chapter.md`: candidate chapter text under test.
- `expected.json`: deterministic expectations for scores, rule statuses, repair types, and blocking state.

Run the suite:

```bash
python -B scripts/run_review_regression.py --manifest tests/fixtures/review_regression/manifest.json --out .tmp/review_regression/summary.json --artifacts-dir .tmp/review_regression/artifacts
```

The harness does not call an LLM, does not run repair, and does not modify chapter files. It only renders intermediate reports and optional artifacts so review behavior can be regression-tested before runtime integration.
