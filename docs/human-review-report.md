# Human-readable Review Report

Human-readable Review Report combines the machine-readable audit artifacts from PR6, PR9, PR10, and PR11 into one Markdown report for a human reviewer.

The flow is:

```text
chapter_quality_report
+ rule_validation_report
+ rule_repair_plan
+ optional rule_repair_prompt_metadata
        -> human_review_report.md + metadata
```

PR12 is report-only. It does not call an LLM, execute repair, modify chapter text, write back Memory V2, or connect to oh-story.

## Inputs

- `chapter_quality_report`: PR6 local quality checks and score.
- `rule_validation_report`: PR9 rule-level pass, warning, fail, and skip results.
- `rule_repair_plan`: PR10 repair task list and blocking status.
- `rule_repair_prompt_metadata`: optional PR11 prompt summary.
- `chapter_text`: optional, used only for a length note.

All JSON inputs are schema-checked before rendering.

## Report Sections

The Markdown report always contains:

- `总体结论`: decision, scores, repair status, and next-step suitability.
- `摘要`: compact human summary of severe issues, warnings, skipped rules, and repair tasks.
- `严重问题`: failed Narrative Rules with mapped repair suggestions.
- `警告问题`: warning Narrative Rules with mapped repair suggestions.
- `已跳过规则`: skipped rules and skip reasons.
- `修复计划摘要`: task counts and prompt metadata availability.
- `修复优先级`: ordered repair tasks from PR10.
- `审稿证据`: key quality-check evidence from PR9.
- `下一步建议`: allowed next steps from the decision.

Empty sections still render `暂无。`.

## Decision

The report uses a deterministic decision:

- `blocked`: repair plan status is `blocked`.
- `needs_revision`: repair plan status is `needs_repair`.
- `accept_with_warnings`: no repair task, but quality or rule validation warns.
- `accept`: no repair task and no warning status.

Allowed next steps:

- `accept`: `continue_generation`
- `accept_with_warnings`: `manual_review`, `continue_generation`
- `needs_revision`: `build_repair_prompt`, `manual_review`
- `blocked`: `build_repair_prompt`, `manual_review`

## CLI

Build report and metadata:

```bash
python -B scripts/build_human_review_report.py \
  --chapter tests/fixtures/chapter_quality/bad_chapter.md \
  --quality-report .tmp/chapter_quality_report.json \
  --rule-validation-report .tmp/rule_validation_report.json \
  --rule-repair-plan .tmp/rule_repair_plan.json \
  --repair-prompt-metadata .tmp/rule_repair_prompt_metadata.json \
  --out .tmp/human_review_report.md \
  --metadata-out .tmp/human_review_report_metadata.json
```

Print only metadata JSON:

```bash
python -B scripts/build_human_review_report.py \
  --quality-report .tmp/chapter_quality_report.json \
  --rule-validation-report .tmp/rule_validation_report.json \
  --rule-repair-plan .tmp/rule_repair_plan.json \
  --json
```

Print Markdown:

```bash
python -B scripts/build_human_review_report.py \
  --quality-report .tmp/chapter_quality_report.json \
  --rule-validation-report .tmp/rule_validation_report.json \
  --rule-repair-plan .tmp/rule_repair_plan.json \
  --print
```

If `--json` and `--print` are both supplied, `--json` wins so stdout remains machine-readable.

## Python

```python
from core.review import build_human_review_report

result = build_human_review_report(
    chapter_text=chapter,
    chapter_quality_report=quality_report,
    rule_validation_report=rule_validation_report,
    rule_repair_plan=rule_repair_plan,
    rule_repair_prompt_metadata=prompt_metadata,
)

markdown = result["markdown"]
metadata = result["metadata"]
```

The function does not mutate input dictionaries or chapter text.

## Future Work

PR13 can add real review regression fixtures. Later PRs can add repair execution, but that should remain separate from this read-only report layer.
