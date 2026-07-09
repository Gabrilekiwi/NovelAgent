# Runtime Review Integration

PR 15 adds an explicit runtime switch for running the PR14 Review Pipeline after a chapter has been generated.

The integration is off by default. Normal `main.py --dry-run`, `main.py --check --dry-run`, and smoke behavior do not run review and do not create review artifacts.

## Enable It

```bash
python main.py \
  --dry-run \
  --enable-review-pipeline \
  --review-output-dir .tmp/runtime/reviews
```

When enabled, runtime generation still follows the existing executor, validator, repair, and commit logic. Review is diagnostic only: a `blocked` review result does not reject a chapter, does not execute repair, and does not modify chapter prose.

## Artifacts

Artifacts are written under an isolated run directory:

```text
<review-output-dir>/<run_id>/
  chapter_quality_report.json
  rule_validation_report.json
  rule_repair_plan.json
  rule_repair_prompt.md
  rule_repair_prompt_metadata.json
  human_review_report.md
  human_review_report_metadata.json
  review_pipeline_summary.json
```

`--review-no-repair-prompt` skips `rule_repair_prompt.md` and `rule_repair_prompt_metadata.json`.

`--review-no-human-report` skips `human_review_report.md` and `human_review_report_metadata.json`; the summary uses `human_review_decision: "unknown"`.

## Run Record

When review is enabled, the run record includes:

```json
{
  "review_pipeline": {
    "enabled": true,
    "status": "blocked",
    "decision": "blocked",
    "quality_score": 49,
    "rule_score": 53,
    "repair_task_count": 2,
    "blocking_task_count": 1,
    "artifacts_dir": ".tmp/runtime/reviews/chapter_...",
    "summary_path": ".tmp/runtime/reviews/chapter_.../review_pipeline_summary.json"
  }
}
```

If review fails, the generated chapter artifact and original run record are preserved, and `review_pipeline.status` is recorded as `error`.

## Rules

By default, runtime review uses `rules/default_narrative_rule_pack.json`.

Use a custom rule pack:

```bash
python main.py --dry-run --enable-review-pipeline --review-rules rules/default_narrative_rule_pack.json
```

Disable default rules only when a custom rule pack is supplied:

```bash
python main.py --dry-run --enable-review-pipeline --review-no-default-rules --review-rules rules/default_narrative_rule_pack.json
```

This integration does not call an LLM, does not execute automatic repair, does not write repaired chapters, does not write back to Memory V2, and does not integrate oh-story, Obsidian, or vector stores.
