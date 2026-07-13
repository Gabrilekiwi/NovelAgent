# StoryProject Semantic Fixtures

These fixtures define the field-level golden corpus for the reliable semantic production work.

- `synthetic_standard` is a deterministic, redacted-format baseline.
- `legacy_append_variant` covers historical headings and an old NovelAgent append block.
- `malformed_variant` covers duplicate managed markers, missing fields, and unsupported free text.
- `soak_spec.json` deterministically describes a 100-chapter long-run corpus without storing 100 duplicated projects.

The repository does not currently contain a redacted sample from the user's target book. `manifest.json` therefore records `target_sample_present=false` and `strict_eligible=false`. Synthetic fixtures may develop and verify shadow mode, but they must never qualify strict authority mode by themselves.

Expected semantic states are contracts for the future shadow parser. Commit 1 validates the fixtures and records current behavior; it does not connect these states to Director, Validator, Snapshot, Memory, or writeback.
