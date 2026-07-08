# Rule-aware Input Pack

Rule-aware Input Pack is an optional preview layer that combines a runtime Snapshot with a Narrative Rule Pack and adds a compact rule contract to the chapter input pack.

PR 8 does not change the default generation flow. Executors, chapter pipeline, validators, API clients, Memory V2 schema, and `main.py` defaults are unchanged.

## Why Optional Injection

Rules should be visible and testable before they affect production generation. This PR keeps rule injection explicit:

- no rules passed: normal input pack
- rules passed: input pack includes a dedicated rule section

The rule section is not mixed into memory, snapshot, story state, or spatial state.

## CLI

Build a rule-aware input pack with the default rule pack:

```bash
python -B scripts/build_rule_aware_input_pack.py \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --default-rules \
  --out .tmp/rule_aware_input_pack.md
```

Build a plain input pack without rules:

```bash
python -B scripts/build_rule_aware_input_pack.py \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --out .tmp/plain_input_pack.md
```

Print a JSON summary only:

```bash
python -B scripts/build_rule_aware_input_pack.py \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --default-rules \
  --json
```

## Rule Filters

The default injection scope is:

```text
applies_to = generation
min_severity = high
```

This keeps the input pack compact and focuses on high-impact rules.

Useful filters:

- `--min-severity medium`: include medium, high, and critical generation rules.
- `--category continuity --category character`: include only selected categories.
- `--max-rules 8`: limit injected rules while prioritizing critical and high rules.

## Python Helpers

```python
from core.rules import build_rule_aware_input_pack

input_pack = build_rule_aware_input_pack(
    snapshot,
    use_default_rules=True,
    min_severity="high",
)
```

For lower-level rendering:

```python
from core.rules import render_generation_rules_for_input_pack
```

## Relationship To Earlier PRs

PR 6 introduced Chapter Quality Evaluation, which evaluates generated chapters.

PR 7 introduced Narrative Rule Pack, which defines structured writing rules.

PR 8 connects those foundations only at the input-pack preview layer. PR 9 can later consider rule-aware validation and repair.

This PR does not connect to any external story system.
