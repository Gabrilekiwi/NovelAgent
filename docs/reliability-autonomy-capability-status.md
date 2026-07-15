# Reliability and autonomy capability status

Last updated: 2026-07-15

This page is the claim boundary for the reliability/autonomy upgrade. A feature is
not called "implemented" without saying which of the following four capability
levels has actually been reached:

1. **Code exists**: source, schemas, and focused tests exist.
2. **Main-path integration**: the supported runtime or CLI invokes the code; a
   library entry point or isolated harness alone does not satisfy this level.
3. **Default enablement**: an ordinary applicable run uses the capability without
   an activation flag, migration, trusted profile, or operator approval.
4. **Real verification**: a retained, redacted report proves execution against the
   real provider or target book. Unit tests, fake providers, deterministic fixtures,
   dry-runs, and synthetic chapter runs never satisfy this level.

Synthetic verification is reported explicitly in the evidence column rather than
being promoted to real verification. Thus every row exposes its code/integration
status, synthetic evidence, real evidence, and default status even though the
matrix keeps code existence and main-path integration in separate columns for
precision.

`Conditional` in the table means that the capability is in the supported main path
only after its documented explicit gate is selected. `No evidence` means that this
checkout has no retained report for the claim; it is not a statement that the
harness cannot run.

## Status matrix

| Capability | Code exists | Main-path integration | Default enablement | Real verification | Evidence boundary / next gate |
| --- | --- | --- | --- | --- | --- |
| Execution provenance, durable `ModelCallIntent`/`ModelCallReceipt`, and run budgets | Yes | Yes, in `AgentExecutor` model-call execution | Yes for persisted/model-backed executor calls; provenance can still be explicitly disabled by an embedding caller | No evidence for this upgrade | Local tests cover immutable intent-before-call ordering, receipt replay, uncertain-call fail-closed behavior, and budget reservation/settlement. |
| Token counting and calibration | Yes | Conditional: context budgets use provider-exact/tokenizer counts when safely identified and otherwise reserve with the conservative calibrated-estimate path | The default unknown-tokenizer hard reservation is on: `max(7/18 fitted tokens/UTF-8 byte with a predeclared 15% margin, 1 token/UTF-8 byte floor) + 64` fixed framing tokens | No | The `7/18` fit is telemetry from `synthetic_acceptance_v1`, not a hard bound or real-provider claim; metadata records both frozen manifest hashes and `real_provider_verified=false`. Provider usage settlement and replay are tested separately. |
| Unified quality decision, strict review gate, and quality calibration | Yes | Yes for `QualityDecision` and the independent Review Gate; calibration/report generation itself is offline evidence | Runtime selects `minimal`/`standard` by context; `strict` is explicit and autonomy pins it. In strict mode, `needs_revision`/`blocked` cannot be bypassed by a Quality Decision while warning-only remains advisory | No | A 64-sample raw synthetic fixture is split 40 calibration / 24 untouched holdout and runs through the production validator/decision path. Holdout precision and critical/high recall are 1.0 with 0 false-block rate, including both voice-conflict directions; this is neither human-labelled nor target-book/real-LLM accuracy evidence. |
| Memory 2.2 typed patches, immutable Event batches, canonical replay, and event authority | Yes | Conditional: the chapter runtime uses it after an approved event-authority migration | No; existing projects remain on their declared authority mode and downgrade after the first event-authority receipt is forbidden | No | Local contract, replay, tamper, drift, and fault tests exist. A real chapter gate is still required. |
| Persistence v2.1 publication/recovery and required File Delivery | Yes | Conditional: event-authority chapter publication uses Persistence v2.1; File Delivery requires a trusted profile and operator root map | No for File Delivery and no for event-authority migration; legacy-compatible runs keep their existing path | No | Local tests cover read-set checks, marker recovery, idempotent delivery, root UUID binding, readback, and fault injection. Synthetic success is not remote/provider evidence. |
| Event Authority / StoryProject data-root remap and whole-project relocation | Yes | Conditional: after an operator same-volume rename, the explicit `--remap-roots` CLI verifies directory identities and forward-rebinds every recognized embedded registry, main registry last, under UUID plus revision/digest CAS | No | No | The command moves no data. It requires pre-armed locks, an absent old path, stable ProjectIdentity/logical-root identities, and no pending/recovery transaction or active session/lease. Cross-volume/copy-delete/case-only moves, replaced roots, rogue registries, and external mutable EA transaction roots fail closed. Local Windows/junction/fault/concurrency tests exist; there is no real target-project report or claim of arbitrary-ancestor TOCTOU protection beyond checked no-create lock boundaries. |
| Preview, approval, and atomic event-authority migration | Yes | Conditional: read-only shadow preview is available through `--preview-event-authority-migration`; immutable approval and CAS execution remain explicit library/API operations | No; preview, immutable approval, and CAS execution are mandatory | No | Local tests prove byte-identical read-only preview, approval/staleness/rollback/source-sync behavior. The active target book has a preview only and has not been approved, migrated, or activated. |
| Historical `amend` / `import` / `retcon` transactions | Yes | No; explicit library/API entry points only. No normal chapter append invokes them | No | No | Local tests exercise append-only correction events, dependency invalidation, receipts, CAS, and recovery. No published real book has been revised by this upgrade run. |
| Natural-language autonomy, trusted profiles, `RunArcPlan`, immutable outlines, `StageReceipt`, lease/session/resume | Yes | Conditional: explicit autonomy CLI commands invoke `AutonomyRunner` and the normal executor/publication path | No; instructions are previewed, trusted profiles/root maps are required, and execution needs explicit consent | No | Focused and deterministic local E2E tests use synthetic or fake-provider prose. They prove control flow, not real generation quality. |
| 50-chapter deterministic autonomy simulation | Yes, opt-in test harness | Test-only; it is not a production default | No | Not applicable (synthetic gate) | **Passed (synthetic only).** A clean frozen-source run against code commit `491fe1d` completed in 1278.705 seconds with 50 outlines/prose publications, EA completion entries, publication receipts, Delivery intents/jobs/attempt receipts and required external JSON deliveries, plus 49 linear next-target Arc adjustments. The same commit then passed 1308 unit tests (7 platform skips) and v1 smoke with 21/21 preflight checks. See the [retained evidence report](reliability-autonomy-50-chapter-evidence.json). |
| Real autonomy: 1 chapter | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Attempted, failed / no success report** | The first one-chapter gate of the user's five-chapter authorization ran on 2026-07-15 and failed before chapter completion. The four-chapter gate was not started, that old authorization is now consumed, and this does not satisfy real verification. The hardened harness now requires a clean commit, explicit proxy choice, official endpoint, durable provider evidence, and a schema-checked redacted report. See [Real autonomy E2E release gates](real-autonomy-e2e.md). |
| Real autonomy: 4 chapters | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | Same gate; a 1-chapter report cannot be promoted to this level. |
| Real autonomy: 10 chapters | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | Requires ten contiguous outlines, prose publications, Event batches, receipts, and verified File Deliveries. |
| Real autonomy: at least 20 chapters | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | This is the long-run release gate. Neither a 50-chapter synthetic run nor shorter real runs substitute for it. |
| Notion access during this reliability/autonomy release | Guard code exists | Rejected by autonomy validation and by the real-autonomy harness | Disabled/forbidden | **Not performed** | No Notion call was made in this upgrade run. Real Notion reads or writes are outside this upgrade's authority. Only required local File Delivery is allowed. Historical v1.5 provider-smoke evidence is not evidence for these autonomy gates. |

## Release interpretation

The local suite may establish levels 1 and 2 and can demonstrate conditional
level-3 behavior. It cannot establish level 4. The first one-chapter gate of the
user's five-chapter authorization failed before completion, the four-chapter
canary was not started, and that old authorization is now consumed; therefore no
successful 1-, 4-, 10-, or 20-or-more-chapter report exists. Every later
billable gate requires a new matching count-bound authorization. The
harness deliberately disables workspace `.env` loading. No Notion call was made
as part of this upgrade.

The 50-chapter deterministic simulation passed against frozen code commit
`491fe1d`, closing the synthetic long-run gate only. Autonomy remains opt-in
because the required real-provider 1-, 4-, 10-, and 20-or-more-chapter reports
are absent. Real Notion execution remains prohibited for this release regardless
of any older provider-smoke report.

Likewise, guarded whole-StoryProject `--remap-roots` is a conditional post-rename
recovery path, not default project portability. It never moves or copies files,
accepts only a verified same-volume rename with an idle/pre-armed project, and
has local fail-closed evidence rather than real target-project verification.
