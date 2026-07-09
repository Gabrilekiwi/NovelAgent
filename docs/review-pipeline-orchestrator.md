# Review Pipeline Orchestrator

PR 14 adds an explicit, deterministic entry point for the chapter review chain. It runs the existing review components in order:

1. PR6 Chapter Quality Evaluation
2. PR9 Rule-aware Validation
3. PR10 Rule-aware Repair Plan
4. PR11 Rule-aware Repair Prompt
5. PR12 Human-readable Review Report

The orchestrator is intentionally a standalone API and CLI. It is not wired into `main.py`, the executor, or the chapter pipeline, so default generation behavior remains unchanged.

## Inputs

- `chapter.md`: generated chapter prose. It is read only and never modified.
- `snapshot.json`: runtime snapshot context.
- `previous_chapter.md`: optional previous chapter context.
- `--rules`: optional custom Narrative Rule Pack.
- `--default-rules`: uses `rules/default_narrative_rule_pack.json`; this is the CLI default.
- `--no-default-rules`: disables default rules and requires `--rules`.

## Artifacts

When `--out-dir` is provided, the pipeline writes:

- `chapter_quality_report.json`
- `rule_validation_report.json`
- `rule_repair_plan.json`
- `rule_repair_prompt.md`
- `rule_repair_prompt_metadata.json`
- `human_review_report.md`
- `human_review_report_metadata.json`
- `review_pipeline_summary.json`

`--no-repair-prompt` skips the repair prompt artifacts. The human report can still be generated; its metadata records that no repair prompt metadata was provided.

`--no-human-report` skips the Markdown human report and metadata. In that mode the summary decision is `unknown`, and the summary status is derived from the quality report, rule validation report, and repair plan.

## Summary

`review_pipeline_summary.json` records:

- overall `status`
- review `decision`
- quality and rule scores
- source report statuses
- repair task counts
- artifact paths
- flags for previous chapter, repair prompt, human report, and default rules

The summary is validated against `schemas/review_pipeline_summary.schema.json`.

## CLI

```bash
python -B scripts/run_review_pipeline.py \
  --chapter tests/fixtures/review_regression/cases/metro_meta_output_bad/chapter.md \
  --snapshot tests/fixtures/review_regression/cases/metro_meta_output_bad/snapshot.json \
  --previous tests/fixtures/review_regression/cases/metro_meta_output_bad/previous_chapter.md \
  --out-dir .tmp/review_pipeline/metro_meta_output_bad
```

For machine-readable output:

```bash
python -B scripts/run_review_pipeline.py \
  --chapter tests/fixtures/review_regression/cases/metro_meta_output_bad/chapter.md \
  --snapshot tests/fixtures/review_regression/cases/metro_meta_output_bad/snapshot.json \
  --previous tests/fixtures/review_regression/cases/metro_meta_output_bad/previous_chapter.md \
  --json
```

The PR13 regression fixtures are useful smoke inputs for the orchestrator because they already cover continuity, meta output, spatial jumps, missing characters, repetition, and language drift.

This PR does not call an LLM, does not execute repair, does not write repaired chapters, does not write back to Memory V2, and does not integrate oh-story or Obsidian. A later PR can add optional runtime integration behind an explicit switch.
