# Narrative Rule Pack

Narrative Rule Pack is NovelAgent's structured rule layer for long-form chapter generation. It describes writing rules, output constraints, continuity requirements, and stable rule codes that later systems can reuse.

PR 7 only defines, validates, loads, and renders the rule pack. It does not change the default generation flow.

## Why This Comes First

Rules need a stable representation before they are injected into prompts or validators. Keeping PR 7 separate avoids mixing rule definition with runtime behavior changes.

This PR does not:

- modify `main.py`
- modify the executor
- modify the chapter pipeline
- inject rules into input packs
- call an LLM or external API
- connect to any external story system

## Rule Pack Structure

The default rule pack is:

```text
rules/default_narrative_rule_pack.json
```

It contains:

- `schema_version`
- `rule_pack_id`
- `title`
- `language`
- `version`
- `rules`
- `output_contract`
- `metadata`

Each rule has a stable `code`, `category`, `severity`, `enabled`, `title`, `instruction`, and `applies_to`. Rules can also map to Chapter Quality Evaluation Harness checks through `quality_check_codes`.

## Validation

Validate the default rules:

```bash
python -B scripts/validate_narrative_rules.py \
  --rules rules/default_narrative_rule_pack.json
```

JSON summary:

```bash
python -B scripts/validate_narrative_rules.py --json
```

## Rendering

Render the rule pack as Markdown:

```bash
python -B scripts/validate_narrative_rules.py --render
```

Write the rendered contract:

```bash
python -B scripts/validate_narrative_rules.py \
  --render \
  --out .tmp/narrative_contract.md
```

The static human-readable default contract is also available at:

```text
prompts/rules/narrative_contract.md
```

## Relationship To Chapter Quality Evaluation

PR 6 added `chapter_quality_report` and local heuristic checks. PR 7 maps narrative rules to those stable check codes, for example:

- `continue_previous_ending` -> `continues_previous_ending`
- `preserve_last_scene_location` -> `preserves_last_scene_location`
- `prose_only_no_meta_output` -> `no_meta_output`

PR 7 does not change PR 6 scoring or evaluation behavior.

## Next Steps

PR 8 can build rule-aware input packs:

```text
Snapshot + Narrative Rule Pack -> Input Pack
```

PR 9 can consider rule-aware validation and repair. Those behaviors are intentionally not part of PR 7.
