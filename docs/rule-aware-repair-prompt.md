# Rule-aware Repair Prompt

Rule-aware Repair Prompt turns a PR10 `rule_repair_plan` into a Markdown prompt artifact.

The flow is:

```text
chapter_text + snapshot + rule_repair_plan
        -> repair_prompt.md + metadata
```

PR11 stops at prompt generation. It does not call an LLM, execute repair, modify chapter text, produce a repaired chapter, write back Memory V2, or connect to oh-story.

## Purpose

The prompt makes repair intent explicit before any model call exists. It tells a future repair model:

- which original chapter to repair
- which Snapshot facts must remain stable
- which tasks are blocking
- which tasks are suggestions
- which evidence caused each task
- which output contract must be followed
- which acceptance criteria the repaired chapter should satisfy

## Prompt Sections

Generated prompts contain:

- `Role`: the model's repair role
- `Non-negotiable Output Contract`: hard output rules
- `Snapshot Context`: full snapshot JSON for now
- `Previous Chapter Context`: included only when provided
- `Narrative Rules`: included only when provided
- `Original Chapter`: the chapter to repair
- `Repair Tasks`: selected tasks from the plan
- `Acceptance Criteria`: checks the repaired chapter should satisfy

When there are no tasks, the prompt still builds and instructs the receiver to preserve the original chapter without expansion or rewriting.

## Metadata

`rule_repair_prompt_metadata.json` records:

- `chars`: prompt length
- `source_plan`: source plan status and task counts
- `prompt.task_count`: tasks included in this prompt
- `prompt.blocking_task_count`: blocking tasks included in this prompt
- `prompt.included_non_blocking`: whether non-blocking tasks were included
- `prompt.has_previous_chapter`: whether previous chapter context is present
- `prompt.has_narrative_rules`: whether narrative rules text is present

The metadata is schema-checked with `schemas/rule_repair_prompt_metadata.schema.json`.

## CLI

Build prompt and metadata:

```bash
python -B scripts/build_rule_repair_prompt.py \
  --chapter tests/fixtures/chapter_quality/bad_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --previous tests/fixtures/chapter_quality/previous_chapter.md \
  --rule-repair-plan .tmp/rule_repair_plan.json \
  --out .tmp/rule_repair_prompt.md \
  --metadata-out .tmp/rule_repair_prompt_metadata.json
```

Print only metadata JSON:

```bash
python -B scripts/build_rule_repair_prompt.py \
  --chapter tests/fixtures/chapter_quality/bad_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --rule-repair-plan .tmp/rule_repair_plan.json \
  --json
```

Print the prompt:

```bash
python -B scripts/build_rule_repair_prompt.py \
  --chapter tests/fixtures/chapter_quality/bad_chapter.md \
  --snapshot tests/fixtures/chapter_quality/snapshot.json \
  --rule-repair-plan .tmp/rule_repair_plan.json \
  --print
```

Options:

- `--blocking-only`: include only blocking tasks.
- `--max-tasks N`: include only the first N selected tasks.
- `--narrative-rules path/to/rules.md`: include extra rendered rules text.

If `--json` and `--print` are both supplied, `--json` wins so stdout remains machine-readable.

## Python

```python
from core.rules import build_rule_repair_prompt

result = build_rule_repair_prompt(
    chapter_text=chapter_text,
    snapshot=snapshot,
    rule_repair_plan=plan,
    previous_chapter_text=previous,
)

prompt = result["prompt"]
metadata = result["metadata"]
```

The function does not mutate `chapter_text`, `snapshot`, or `rule_repair_plan`.

## Future Work

PR12 can turn quality, validation, plan, and prompt metadata into a human-readable review report. Later PRs can add repair prompt execution or model-backed repair. Those steps should remain explicit and separate from this prompt-only layer.
