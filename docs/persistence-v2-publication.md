# Persistence v2 and Publication Receipts

Persistence v2 is available through `core.engine.persistence_v2`. It is kept
separate from the v1 production transaction while the reliable semantic path
is still behind its activation gate.

## Commit proof

A commit is externally reported as committed only when its immutable
Publication Receipt validates. Neither a standalone Final RunRecord nor a
commit marker is sufficient.

The hash dependency is acyclic:

```text
context/read set -> candidate digest
staged publication artifacts -> artifact bundle digest
manifest immutable section -> manifest digest
manifest + candidate + artifact bundle + Final RunRecord -> marker hash
marker + Final RunRecord + artifact bindings -> receipt hash
```

The Final RunRecord contains only the predetermined Receipt id and `PathRef`.
It never contains the Receipt hash. The Receipt is excluded from the artifact
bundle and is created atomically after all publication targets are verified.

## Recovery

The pending registry is the only startup scan surface. Recovery obtains locks
from the historical manifest root map and target `PathRef`s.

- Before the marker, recovery validates the pending candidate and performs a
  compare-and-swap rollback. An external edit is preserved and moves the
  transaction to `recovery_required`.
- After the marker, recovery never rolls back committed state. It idempotently
  publishes missing artifacts, the Final RunRecord, and the Receipt.
- Completed transactions are removed from the pending registry. Startup does
  not open completed candidates or scan historical journal directories.

Receipt verification re-hashes the marker, Final RunRecord, and immutable
artifacts. Editing any immutable Final RunRecord field invalidates committed
status. Main StoryProject state hashes remain historical receipt evidence and
are not compared with later legitimate edits.

## Retention

`gc_persistence_v2()` retains completed and rolled-back full journals using
separate limits (10 each by default). Older journals keep their manifest,
marker/Receipt binding, failure receipt, and registry audit entry while staged
bytes, backups, and candidates are eligible for deletion.

GC refuses to run while pending reconciliation or `recovery_required` entries
exist. Dry-run and real execution use the same deterministic deletion set and
report reclaimed bytes.
