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

Add `--review-gate blocked`, `--review-gate needs_revision`, or `--review-gate warning` when the CLI should exit with code `1` after printing output if the review status meets that threshold. The gate still does not repair, reject, or rewrite the chapter.

Each enabled runtime review also updates `<review-output-dir>/review_index.json`, which can be queried with `--review-latest` or `--review-list`.

## Review Auto Repair

Review-driven repair is also off by default. It only runs when both switches are present:

```bash
python main.py \
  --enable-review-pipeline \
  --review-auto-repair
```

`--review-auto-repair` is a configuration error without `--enable-review-pipeline`.

Use `--review-repair-max-attempts N` to cap attempts. The accepted range is `1..3`, and the default is `1`.

Use `--review-repair-dry-run` with `--review-auto-repair` to build the repair plan and runtime artifacts without changing the runtime chapter text. Dry-run repair does not allow StoryProject writeback to proceed.

Auto repair only triggers when the original runtime review status is `needs_revision` or `blocked`. `pass` and `warning` reviews remain diagnostic and do not trigger repair.

A repaired chapter is accepted only after deterministic validation passes and the post-repair review returns `pass` or `warning`. If a review gate is configured, the post-repair gate must also pass. Rejected, failed, or dry-run repairs produce a rejected run and do not write StoryProject files.

Review artifacts are isolated:

```text
<review-output-dir>/<run_id>/original/
<review-output-dir>/<run_id>/repair_attempt_01/
```

Review repair artifacts are stored under the runtime run directory:

```text
<run-dir>/review_repairs/<run_id>/
```

Build a static dashboard from the index after reviews have been written:

```bash
python main.py --review-dashboard --review-output-dir .tmp/runtime/reviews
```

The dashboard is not generated automatically during runtime review.

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

When `--review-gate` is enabled, the run record also includes `review_gate` with the threshold, pass/fail status, matched review status, reason, and exit code. See `docs/review-gate.md` for threshold behavior.

When the index update succeeds, the run record includes `review_index` with the index path, latest run id, and entry count.

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

With `--review-auto-repair`, the integration still does not execute oh-story scripts, does not call an oh-story API provider, does not summarize tracking with an LLM, and does not write StoryProject files directly. StoryProject writeback, if explicitly enabled, receives only the final accepted runtime chapter.
