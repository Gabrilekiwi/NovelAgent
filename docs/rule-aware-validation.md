# Rule-aware Validation

Rule-aware Validation maps the local Chapter Quality Evaluation report onto the Narrative Rule Pack.

It answers a different question from the raw quality harness:

- Chapter Quality checks whether local heuristic checks passed.
- Rule-aware Validation explains which writing rules passed, warned, failed, or were skipped.

The data flow is:

```text
chapter text + snapshot + optional previous chapter
        -> chapter_quality_report
        + narrative_rule_pack
        -> rule_validation_report
```

## Scope

PR 9 is report-only.

It does not repair text, rewrite chapters, call an LLM, call external APIs, update Canonical Memory, connect to oh-story, or enable any runtime behavior by default. A later PR can use this report to draft a repair plan, but that should still be explicit and auditable.

## CLI

Validate a chapter with the default rule pack:

```bash
python -B scripts/validate_chapter_rules.py \
  --chapter tests/fixtures/chapter_quality/good_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --previous tests/fixtures/chapter_quality/previous_chapter.md \
  --default-rules \
  --out .tmp/rule_validation_report.json
```

Print only JSON:

```bash
python -B scripts/validate_chapter_rules.py \
  --chapter tests/fixtures/chapter_quality/good_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --previous tests/fixtures/chapter_quality/previous_chapter.md \
  --default-rules \
  --json
```

Reuse an existing quality report instead of recomputing it:

```bash
python -B scripts/validate_chapter_rules.py \
  --chapter tests/fixtures/chapter_quality/good_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --previous tests/fixtures/chapter_quality/previous_chapter.md \
  --default-rules \
  --quality-report .tmp/chapter_quality_report.json \
  --out .tmp/rule_validation_report.json
```

Use a custom rule pack:

```bash
python -B scripts/validate_chapter_rules.py \
  --chapter path/to/chapter.md \
  --snapshot path/to/snapshot.json \
  --rules path/to/narrative_rule_pack.json \
  --out .tmp/rule_validation_report.json
```

Filters:

- `--min-severity high` validates only high and critical rules.
- `--category output_contract` validates only one category.
- `--category` can be repeated.

## Rule Mapping

Rules are mapped through `quality_check_codes`.

```json
{
  "code": "prose_only_no_meta_output",
  "quality_check_codes": ["no_meta_output"]
}
```

Status mapping is deterministic:

- no `quality_check_codes`: `skip`, reason `no_quality_check_mapping`
- mapped check missing from the quality report: `skip`, reason `quality_check_missing`
- every mapped check skipped: `skip`, reason `all_quality_checks_skipped`
- any mapped check failed: `fail`
- otherwise any mapped check warned: `warning`
- otherwise all mapped checks passed: `pass`

For multiple checks, priority is:

```text
fail > warning > pass > skip
```

## Report Fields

`rule_validation_report.json` contains:

- `status`: overall `pass`, `warning`, or `fail`
- `score`: rule-level score from 0 to 100
- `summary`: counts of passed, warning, failed, and skipped rules
- `rule_pack`: rule pack identity
- `quality_report`: quality report status, score, and check count
- `rules`: per-rule validation results with matched quality checks and evidence
- `violations`: warning and failed rules
- `metadata.ready_for_next_flow`: false only when the rule report status is `fail`

Skipped rules are expected in PR 9. Some Narrative Rule Pack entries intentionally do not yet have local deterministic quality checks, such as character-state consistency or world-rule consistency. They remain visible as skipped so future PRs can add checks without changing report shape.

## Python

```python
from core.rules import validate_chapter_against_rules

report = validate_chapter_against_rules(
    chapter_text=chapter,
    snapshot=snapshot,
    previous_chapter_text=previous,
    use_default_rules=True,
)
```

Pass `quality_report=...` to reuse a report already created by `evaluate_chapter_quality()`.
