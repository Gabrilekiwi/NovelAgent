# Memory V2.1 Event Store

Memory V2.1 uses immutable JSON event batches as the source of truth. The
`canonical_memory.json` file is only a projection cache and may be deleted and
rebuilt.

## Storage layout

```text
<memory-output>/
  canonical_memory.json
  memory_events/
    batches/
      batch_<first-revision>_<last-revision>_<patch-hash>.json
    checkpoints/
      checkpoint_<revision>_<batch-hash>.json
```

Each batch binds the book identity, contiguous revision range, previous batch
hash, patch id and content hash, source/context digests, canonical JSON
algorithm, immutable patch, and the events reproduced by that patch. Event and
batch hashes exclude their own hash field. A chain gap, revision fork, event
tamper, or patch/event semantic mismatch fails closed.

Patch ids are idempotency keys. Reusing an id with the same semantic content is
a no-op; reusing it with different content is a conflict. Source-sync patches
advance revision but do not advance the committed-chapter checkpoint interval.
Rejected, failed, and preview chapter patches are refused.

Every 20 committed chapter batches creates an immutable checkpoint containing
the projection, patch index, independent quality-state summaries, and the
anchor batch hash. Startup validates the newest checkpoint and replays only its
tail. `replay_memory_events()`, `verify_memory_projection()`, and
`rebuild_canonical_memory()` provide the recovery path.

Memory 2.0 JSONL events remain readable through `load_memory_events()`. New
compiler writes go to the 2.1 batch directory. `--reset` cannot replace an
existing immutable chain; use a new output directory for a new history.
