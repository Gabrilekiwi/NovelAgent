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
| Token counting and calibration | Yes | Conditional: context budgets use exact/tokenizer counts when safely identified and otherwise use the conservative calibrated-estimate fallback | The conservative fallback is on; a provider/model-specific fitted calibration is not installed as a production default | No | Calibration and holdout fixtures are explicitly `synthetic_acceptance_v1`; their report is local evidence only. |
| Unified quality decision and quality calibration | Yes | Yes for `QualityDecision`; calibration/report generation itself is an offline tool, not a runtime authority | Runtime selects `minimal`/`standard` by context; `strict` is explicit and autonomy pins it | No | The fixed calibration/holdout set is synthetic, not human-labelled target-book evidence. It must not be cited as real quality accuracy. |
| Memory 2.2 typed patches, immutable Event batches, canonical replay, and event authority | Yes | Conditional: the chapter runtime uses it after an approved event-authority migration | No; existing projects remain on their declared authority mode and downgrade after the first event-authority receipt is forbidden | No | Local contract, replay, tamper, drift, and fault tests exist. A real chapter gate is still required. |
| Persistence v2.1 publication/recovery and required File Delivery | Yes | Conditional: event-authority chapter publication uses Persistence v2.1; File Delivery requires a trusted profile and operator root map | No for File Delivery and no for event-authority migration; legacy-compatible runs keep their existing path | No | Local tests cover read-set checks, marker recovery, idempotent delivery, root UUID binding, readback, and fault injection. Synthetic success is not remote/provider evidence. |
| Event Authority / StoryProject data-root remap and whole-project relocation | Partial: the Python `RootRegistry` service can explicitly remap an EA-global/StoryProject data root | No unified main-path operation: there is no `remap-roots` CLI or end-to-end relocation orchestration | No | No | Remap is allowed only when the root has no pending transaction and no active session. The StoryProject-embedded Persistence v2 runtime control plane is not relocated, so this is not default end-to-end whole-project movement. `RootRegistry` is the unique mutable EA physical-root mapping; immutable historical manifests may still retain absolute-path snapshots. |
| Preview, approval, and atomic event-authority migration | Yes | No; explicit library/API entry point only. No normal generation command performs migration | No; preview, immutable approval, and CAS execution are mandatory | No | Local tests exercise preview/approval/staleness/rollback/source-sync behavior. A target book has not been migrated by this upgrade run. |
| Historical `amend` / `import` / `retcon` transactions | Yes | No; explicit library/API entry points only. No normal chapter append invokes them | No | No | Local tests exercise append-only correction events, dependency invalidation, receipts, CAS, and recovery. No published real book has been revised by this upgrade run. |
| Natural-language autonomy, trusted profiles, `RunArcPlan`, immutable outlines, `StageReceipt`, lease/session/resume | Yes | Conditional: explicit autonomy CLI commands invoke `AutonomyRunner` and the normal executor/publication path | No; instructions are previewed, trusted profiles/root maps are required, and execution needs explicit consent | No | Focused and deterministic local E2E tests use synthetic or fake-provider prose. They prove control flow, not real generation quality. |
| 50-chapter deterministic autonomy simulation | Yes, opt-in test harness | Test-only; it is not a production default | No | Not applicable (synthetic gate) | **Passed (synthetic only).** A frozen-source run against code commit `4cf3b45` completed 50 chapters in 1365.453 seconds with 50 executor requests, 50 required File Delivery JSON artifacts, and 49 linear next-target Arc adjustments. See the [retained evidence report](reliability-autonomy-50-chapter-evidence.json). |
| Real autonomy: 1 chapter | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | This process has no release-authorized `OPENAI_API_KEY` and no matching count-bound opt-in sentinel. The harness disables workspace `.env` loading and requires the official endpoint plus a schema-checked redacted report. See [Real autonomy E2E release gates](real-autonomy-e2e.md). |
| Real autonomy: 4 chapters | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | Same gate; a 1-chapter report cannot be promoted to this level. |
| Real autonomy: 10 chapters | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | Requires ten contiguous outlines, prose publications, Event batches, receipts, and verified File Deliveries. |
| Real autonomy: at least 20 chapters | Yes, opt-in billable harness | Conditional, through the normal autonomy/executor path | No | **Not run / no report** | This is the long-run release gate. Neither a 50-chapter synthetic run nor shorter real runs substitute for it. |
| Notion access during this reliability/autonomy release | Guard code exists | Rejected by autonomy validation and by the real-autonomy harness | Disabled/forbidden | **Not performed** | No Notion call was made in this upgrade run. Real Notion reads or writes are outside this upgrade's authority. Only required local File Delivery is allowed. Historical v1.5 provider-smoke evidence is not evidence for these autonomy gates. |

## Release interpretation

The local suite may establish levels 1 and 2 and can demonstrate conditional
level-3 behavior. It cannot establish level 4. In the current process no
release-authorized `OPENAI_API_KEY`, matching count-bound opt-in sentinel, or
retained real-autonomy report is available, so the 1-, 4-, 10-, and
20-or-more-chapter gates remain explicitly unverified. The harness deliberately
disables workspace `.env` loading. No Notion call was made as part of this upgrade.

The 50-chapter deterministic simulation passed against frozen code commit
`4cf3b45`, closing the synthetic long-run gate only. Autonomy remains opt-in
because the required real-provider 1-, 4-, 10-, and 20-or-more-chapter reports
are absent. Real Notion execution remains prohibited for this release regardless
of any older provider-smoke report.

Likewise, the guarded Python data-root remap must not be described as project
portability. Until the embedded Persistence v2 runtime control plane is included
and a CLI or unified relocation workflow exists, default end-to-end project movement
remains unavailable.
