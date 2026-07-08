# Chapter Quality Evaluation

`scripts/evaluate_chapter_quality.py` evaluates generated chapter text with local deterministic heuristics. It does not generate prose, call an LLM, call external APIs, repair text, or change the runtime generation flow.

The harness is intended to make chapter quality regressions visible before adding a larger narrative rule system.

## CLI

```bash
python -B scripts/evaluate_chapter_quality.py \
  --chapter tests/fixtures/chapter_quality/good_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --previous tests/fixtures/chapter_quality/previous_chapter.md \
  --out .tmp/chapter_quality_report.json
```

Use `--json` when stdout must contain only the report JSON:

```bash
python -B scripts/evaluate_chapter_quality.py \
  --chapter tests/fixtures/chapter_quality/good_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --previous tests/fixtures/chapter_quality/previous_chapter.md \
  --json
```

## Report

The output is a `chapter_quality_report` JSON object:

- `status`: overall `pass`, `warning`, or `fail`.
- `score`: deterministic score from 0 to 100.
- `summary`: counts of passed, warning, failed, and skipped checks.
- `checks`: stable check results with `code`, `status`, `severity`, `message`, and `evidence`.
- `metrics`: length, paragraph, dialogue, and repetition metrics.
- `snapshot_refs`: snapshot continuity fields used during evaluation.

The report is validated against `schemas/chapter_quality_report.schema.json`.

## Current Checks

The first version checks:

- continuation from previous ending or required opening bridge
- last-scene location continuity
- last-scene character continuity
- open thread or active conflict touch points
- premature resolution markers
- meta output such as analysis, JSON, or Markdown headings
- zh-CN language consistency
- obvious repetition or stalling prose
- chapter length
- snapshot compatibility

These checks are intentionally heuristic. They are suitable for regression tests and smoke checks, not final literary judgment.

## Next Steps

Later PRs can connect this harness to a Narrative Rule Pack and eventually to rule-aware validation or repair. PR 6 keeps those concerns out of the runtime path.
