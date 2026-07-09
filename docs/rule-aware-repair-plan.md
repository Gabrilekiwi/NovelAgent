# Rule-aware Repair Plan

Rule-aware Repair Plan converts a PR9 `rule_validation_report` into a structured list of repair tasks.

The flow is:

```text
chapter text + snapshot + narrative rules
        -> rule_validation_report
        -> rule_repair_plan
```

PR10 stops at the plan. It does not rewrite chapter text, call an LLM, execute repairs, update Memory V2, connect to oh-story, or change the default runtime flow.

## Why Plan Only

Repair should be auditable before it becomes executable. A plan lets a reviewer see:

- which Narrative Rule caused the issue
- whether the issue came from a failure or warning
- which repair strategy is recommended
- whether it blocks downstream flow
- whether human review is required
- which quality-check evidence caused the task

Later PRs can turn this plan into a repair prompt or an explicit repair execution path. Those steps should remain separate from PR10.

## CLI

Build a repair plan from a rule validation report:

```bash
python -B scripts/build_rule_repair_plan.py \
  --rule-validation-report .tmp/rule_validation_report.json \
  --out .tmp/rule_repair_plan.json
```

Print only JSON:

```bash
python -B scripts/build_rule_repair_plan.py \
  --rule-validation-report .tmp/rule_validation_report.json \
  --json
```

Only include failed rules:

```bash
python -B scripts/build_rule_repair_plan.py \
  --rule-validation-report .tmp/rule_validation_report.json \
  --fail-only \
  --out .tmp/rule_repair_plan.json
```

Limit output:

```bash
python -B scripts/build_rule_repair_plan.py \
  --rule-validation-report .tmp/rule_validation_report.json \
  --max-tasks 3 \
  --out .tmp/rule_repair_plan.json
```

`--chapter` and `--snapshot` are accepted for forward compatibility. PR10 reads them only and never modifies them.

## Task Fields

Each task contains:

- `task_id`: stable ordinal id such as `repair_001`
- `rule_code`: Narrative Rule code
- `rule_status`: `fail` or `warning`
- `severity`: rule severity
- `category`: rule category
- `priority`: 1-based priority after sorting
- `repair_type`: deterministic strategy label
- `title`: short human-readable task title
- `instruction`: repair guidance for a human or later prompt builder
- `source_quality_check_codes`: mapped quality checks from PR9
- `evidence`: quality-check evidence copied from PR9
- `requires_human_review`: true when no deterministic strategy is configured
- `blocking`: true for critical or high failures

## Priority

Tasks are sorted deterministically:

```text
critical fail
high fail
medium fail
low fail
critical warning
high warning
medium warning
low warning
```

Original report order is preserved within the same status and severity.

## Blocking

Blocking means the chapter should not move forward without review or repair:

- critical fail: blocking
- high fail: blocking
- medium fail: non-blocking
- low fail: non-blocking
- warning: non-blocking
- skipped rules: no repair task

## Repair Types

Known mappings include:

- `continue_previous_ending` -> `strengthen_opening_continuity`
- `preserve_last_scene_location` -> `fix_location_transition`
- `preserve_last_scene_characters` -> `restore_missing_characters`
- `advance_current_conflict` -> `advance_conflict_or_thread`
- `avoid_premature_resolution` -> `defer_premature_resolution`
- `prose_only_no_meta_output` -> `remove_meta_output`
- `follow_target_language` -> `enforce_target_language`
- `avoid_repetition_and_stalling` -> `reduce_repetition_and_stalling`
- `reasonable_chapter_length` -> `adjust_chapter_length`

Unknown rule codes fall back to `manual_review`.

## Python

```python
from core.rules import build_rule_repair_plan

plan = build_rule_repair_plan(
    rule_validation_report=report,
    include_warnings=True,
    max_tasks=None,
)
```

The function validates both the input report and output plan through JSON Schema and does not mutate the input report.
