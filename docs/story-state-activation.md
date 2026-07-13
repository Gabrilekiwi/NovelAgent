# Story State Calibration and Strict Activation

StoryProject semantic parsing remains `shadow` by default. Shadow parsing produces semantic state, provenance, conflicts, unsupported excerpts, and a diff report, but it does not replace Snapshot or Memory authority.

Strict mode is book-specific and must be activated explicitly. Activation pins the parser version, semantic schema version, layout profile version, calibration report hash, and activation time in `<book>/.novelagent/project.json`.

## Calibration gate

A calibration report is schema-checked by `schemas/story_state_calibration_report.schema.json` and protected by a canonical SHA-256 digest. Strict eligibility requires all of the following:

- At least one redacted sample from the target book and at least two format variants.
- 100% managed-block round trip, required-field exact match, and authoritative precision.
- At least 95% recall for supported optional fields.
- Every unsupported structure captured as non-authoritative evidence.
- Ten consecutive shadow chapters with no blocking conflict.
- Provenance for every populated production semantic field.

The committed synthetic fixtures do not establish the target-book sample gate. They cover parser behavior only and must not be used to claim that a real book is strict-qualified.

Inspect shadow state without generation or writes:

```bash
python main.py --story-project PATH --chapter auto --story-state-shadow-report --output-json
```

Activate a qualified report:

```bash
python main.py \
  --story-project PATH \
  --activate-story-state \
  --story-state-calibration-report PATH/qualified-calibration.json
```

Activation fails if the report is malformed, tampered with, belongs to another `book_id`, is below any threshold, or pins a parser/schema version different from the running code.

## Strict runtime behavior

Strict mode makes parsed StoryProject state authoritative for generation and validation. Memory V2 remains supporting context. A persistent strict run requires transactional StoryProject apply writeback:

```bash
python main.py --story-project PATH --chapter auto --steps 2 --story-project-writeback
```

Each accepted chapter commits prose, the single managed projection in each tracking file, Snapshot, the Memory V2.1 event batch/canonical projection/checkpoint when due, and the local persistence record through one local transaction. Manual text outside managed blocks is preserved byte-for-byte. Manual tombstones prevent deleted managed fields from being resurrected.

If the pinned parser, schema, or layout profile drifts—or a blocking semantic conflict or provenance gap appears—strict mode fails closed before provider generation. An operator may inspect the project through an explicit non-authoritative downgrade:

```bash
python main.py \
  --story-project PATH \
  --chapter auto \
  --allow-story-state-shadow-downgrade \
  --check --dry-run --check-json
```

Downgrade is never silent: `effective_mode="shadow"`, `authoritative=false`, and `ready_for_next_step=false` remain in the audit. Recalibrate and explicitly activate a new qualified report before restoring strict authority.

## Audit fields

The runtime context and run record retain:

- configured/effective semantic mode and profile-match result;
- parser/schema/layout versions and source digest;
- provenance, conflict, warning, and unsupported-structure evidence;
- Memory V2 revision and projection hash;
- StoryProject read-set/context digest and transactional writeback result.

Prompt input contains compact semantic metadata only. Production semantic values are first applied to the runtime Snapshot, while full field-level provenance stays in structured audit data instead of being duplicated into model prompts.
