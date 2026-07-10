# StoryProject v2 Progress

Date: 2026-07-09

## Phase 0: StoryProject compatible baseline

Status: implemented.

Completed:

- Added an independent `core/story_project` package for StoryProject path resolution, loading, and validation.
- Added `.active-book` first-line parsing. Relative paths resolve from the workspace root; absolute paths are accepted.
- Added `--story-project auto|PATH` and `--chapter N|auto` CLI arguments.
- Added `run_preflight()` `story_project_structure` validation when `--story-project` is explicitly requested.
- Preserved the existing JSON memory path mode. `python main.py --check --dry-run --memory data/notion_memory.example.json` still runs without requiring StoryProject.
- Implemented required core directory checks for `设定/`, `大纲/`, `正文/`, and `追踪/`.
- Missing `.story-deployed` and `.codex/hooks.json` are not checked as blocking requirements.
- Implemented the Phase 0 filename resolver:
  - Canonical outline write path: `大纲/细纲_第003章.md`.
  - Compatible outline reads: `细纲_第3章.md`, `细纲_第003章.md`, `细纲_第3章_*.md`, `细纲_第003章_*.md`.
  - Canonical prose write path: `正文/第003章_章名.md`.
  - Compatible prose reads: `第3章.md`, `第003章.md`, `第3章_*.md`, `第003章_*.md`.
  - Multiple outline matches for the same chapter are blocking.
  - Multiple prose matches for the same chapter are blocking.
  - `--chapter auto` infers the first missing chapter from existing `正文/` chapter files.

Modified files:

- `core/story_project/__init__.py`
- `core/story_project/model.py`
- `core/story_project/paths.py`
- `core/story_project/loader.py`
- `core/story_project/validator.py`
- `core/engine/preflight.py`
- `core/memory_v2/storage.py`
- `main.py`
- `tests/test_story_project.py`
- `tests/test_cli.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_story_project tests.test_cli`: passed, 32 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 606 tests.
- `python -B scripts/smoke_v1.py`: passed.

## Phase 0.1: hardening

Status: implemented.

Completed:

- Replaced `Path.cwd()` default arguments in `core/story_project/paths.py` and `core/story_project/validator.py` with `None`, resolving the workspace root at call time.
- Restored Memory V2 canonical memory writes to prefer temp file plus replace.
- Added a managed Windows fallback: if atomic replace raises `PermissionError` / WinError 5, `core/memory_v2/storage.py` writes directly to the target path.
- Added tests for call-time cwd resolution, preferred atomic replace, Windows permission fallback, and non-fallback replace errors.

Modified files:

- `core/story_project/paths.py`
- `core/story_project/validator.py`
- `core/memory_v2/storage.py`
- `tests/test_story_project.py`
- `tests/test_memory_v2_storage.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_story_project tests.test_memory_v2_storage`: passed, 15 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 609 tests.
- `python -B scripts/smoke_v1.py`: passed.

## Phase 1: StoryProject -> Runtime mapping

Status: implemented.

Completed:

- Added `core/story_project/mapper.py` with `build_story_project_runtime_context(...)` as the stable Phase 1 entry point.
- Extended `core/story_project/model.py` with `StoryProjectRuntimeContext`, `ChapterBlueprint`, `SourcePathSet`, `SourceResolution`, and `SourceResolutionEntry`.
- Built deterministic runtime context from StoryProject files:
  - Current outline text.
  - Previous prose when `chapter_index > 1`.
  - Recursive `设定/**/*.md` context.
  - `追踪/*.md` context, with missing tracking files recorded as warnings / missing fields.
  - `snapshot_overlay`, `memory_context_overlay`, `source_paths`, and `source_resolution`.
- Added conservative `chapter_blueprint` extraction from Markdown headings, labels, and list items.
- Missing `core_event`, `required_beats`, and `ending_pressure` are recorded in `missing_fields` instead of blocking Phase 1 mapping.
- Added `story_project_runtime_context` preflight check after `story_project_structure` passes.
- Kept legacy preflight behavior unchanged when `--story-project` is not requested.
- Added mapper and preflight tests covering outline, tracking, recursive settings, previous prose, blueprint title/missing fields, and runtime context check visibility.

Modified files:

- `core/story_project/__init__.py`
- `core/story_project/model.py`
- `core/story_project/mapper.py`
- `core/engine/preflight.py`
- `main.py`
- `tests/test_story_project_mapper.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_story_project_mapper`: passed, 12 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 621 tests.
- `python -B scripts/smoke_v1.py`: passed.

Explicitly not done in Phase 1:

- No chapter generation integration.
- No StoryProject writeback.
- No Chapter Pipeline `required_beats` hard-contract enforcement.
- No `AgentExecutor` direct StoryProject file reads.
- No oh-story API provider or JS script execution.

## Phase 1.1: Runtime path normalization

Status: implemented.

Completed:

- Normalized StoryProject runtime relative paths to POSIX-style keys via `Path.as_posix()`.
- `tracking_files`, `setting_files`, `source_paths.tracking_paths`, and `source_paths.setting_paths` now use `/` separators consistently across Windows, Linux, and macOS.
- Updated mapper tests to assert `角色/主角.md` instead of Windows-style `角色\主角.md`.

Modified files:

- `core/story_project/mapper.py`
- `tests/test_story_project_mapper.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_story_project_mapper`: passed, 12 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 621 tests.
- `python -B scripts/smoke_v1.py`: passed.

Next recommended step:

- Stop after Phase 1. Phase 2 should separately handle generation integration, StoryProject writeback, and Chapter Pipeline `required_beats` / `ending_pressure` hard-contract enforcement.

## Phase 2: Chapter Blueprint contract and coverage

Status: implemented.

Completed:

- Added `schemas/chapter_blueprint.schema.json` for the stable StoryProject chapter blueprint contract.
- Added `core/story_project/runtime.py` as the generation-mode StoryProject adapter. It builds an already-resolved `StoryProjectRuntimeContext` for `main.py` and keeps StoryProject file reading out of `AgentExecutor`.
- Added `core/story_project/coverage.py` for deterministic blueprint plan derivation and coverage validation.
- Wired `main.py --dry-run --story-project auto --chapter auto` into StoryProject generation mode.
- Kept `--check --story-project auto` as preflight-only behavior.
- Extended `AgentExecutor` to consume an already-built `story_project_context` without parsing `.active-book` or reading StoryProject directories.
- Extended input packs with a StoryProject Chapter Blueprint section only when StoryProject context is present.
- Made StoryProject `chapter_blueprint.required_beats` a hard Chapter Pipeline contract:
  - StoryProject mode derives `plan_chapter()` from the blueprint.
  - StoryProject mode does not call model planning logic.
  - `scene_limit` groups beats into fewer scenes instead of truncating beats.
- Added deterministic `blueprint_coverage` with:
  - `required_beat_count`
  - `covered_beat_indexes`
  - `missing_beat_indexes`
  - `ending_pressure_required`
  - `ending_pressure_covered`
- Added StoryProject coverage validation problems:
  - `missing_required_beat`
  - `missing_ending_pressure`
- Used existing `manual_review` repair action for StoryProject coverage problems to avoid expanding repairer responsibility.
- Recorded StoryProject audit data in persisted run records when StoryProject context is active, including `writeback.attempted=false`.
- Preserved legacy non-StoryProject pipeline behavior with StoryProject fields absent or null.
- Preserved check-only behavior: missing `ending_pressure` remains a runtime context `missing_fields` entry and does not fail ordinary preflight.
- Enforced generation-mode blocking when `core_event`, `required_beats`, or `ending_pressure` is missing from the StoryProject blueprint.

Modified files:

- `core/engine/executor.py`
- `core/engine/preflight.py`
- `core/engine/run_record.py`
- `core/state/input_pack.py`
- `core/story_project/coverage.py`
- `core/story_project/runtime.py`
- `core/validator/__init__.py`
- `core/validator/common.py`
- `main.py`
- `modules/chapter_generator/pipeline.py`
- `schemas/chapter_blueprint.schema.json`
- `schemas/chapter_pipeline.schema.json`
- `schemas/input_pack_metadata.schema.json`
- `schemas/run_record.schema.json`
- `schemas/validation_result.schema.json`
- `tests/test_chapter_pipeline.py`
- `tests/test_executor.py`
- `tests/test_story_project_mapper.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_chapter_pipeline`: passed, 10 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 627 tests.
- `python -B scripts/smoke_v1.py`: passed.

Explicitly not done in Phase 2:

- No Phase 3 oh-story enhanced detection.

## Phase 2.1: Run record validation coverage retention

Status: implemented.

Completed:

- Added `story_project` to `core/engine/run_record.py` validation name retention so run record summaries preserve StoryProject validation coverage.
- Updated run record and loop session schemas to accept `story_project` in validation coverage fields.
- Added a regression assertion that StoryProject run records keep `validation.executed_checks` containing `story_project`.

Modified files:

- `core/engine/run_record.py`
- `schemas/run_record.schema.json`
- `schemas/loop_session.schema.json`
- `tests/test_executor.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_executor.AgentExecutorTest.test_story_project_context_records_blueprint_coverage_without_writeback`: passed.

Explicitly not done in Phase 2.1:

- No StoryProject writeback.
- No writes to `正文/` or `追踪/`.
- No Phase 3 oh-story enhanced detection.

## Phase 3: StoryProject writeback

Status: implemented.

Completed:

- Added explicit StoryProject writeback modes:
  - Default mode remains disabled and records `story_project.writeback.attempted=false`.
  - `--story-project-writeback` enables real StoryProject file writeback.
  - `--story-project-writeback-dry-run` builds runtime writeback artifacts without modifying StoryProject files.
  - `--story-project-overwrite` allows overwriting only one uniquely resolved existing prose file.
- Added `core/story_project/writer.py` for StoryProject writeback preflight, plan building, real apply, dry-run result generation, target-level status, partial apply reporting, tracking marker dedupe, and diff summaries.
- Implemented real-write preflight before any StoryProject file mutation.
- Implemented prose conflict rules: `target_prose_exists` for one existing prose without overwrite, and `multiple_prose_targets` for multiple same-chapter prose files even with overwrite enabled.
- Implemented tracking append blocks for `追踪/上下文.md`, `追踪/伏笔.md`, `追踪/时间线.md`, and `追踪/角色状态.md`.
- Added stable tracking markers and skip-on-duplicate behavior for the same `run_id + chapter + target`.
- Added target-level partial apply reporting with `partial`, `failed_targets`, `errors`, and per-target `created` / `updated` / `skipped` / `failed` status.
- Added temp-file plus replace writes for prose and tracking files, with managed Windows `PermissionError` fallback to direct write.
- Wired executor save order as generation / validation, run record build, StoryProject writeback plan/apply, attach writeback result, then validate/save run record.
- Added CLI configuration errors for `--dry-run + --story-project-writeback` and for StoryProject writeback flags without `--story-project`.
- Added CLI non-zero exit for explicit real writeback blocked or failed results after run record save.
- Expanded `schemas/run_record.schema.json` for StoryProject writeback audit fields.
- Added writer regressions for `missing_ending_pressure`, `validation_not_ok`, and `run_not_committed` blocking without StoryProject file writes.

Modified files:

- `core/engine/artifacts.py`
- `core/engine/executor.py`
- `core/story_project/__init__.py`
- `core/story_project/writer.py`
- `main.py`
- `schemas/run_record.schema.json`
- `tests/test_cli.py`
- `tests/test_executor.py`
- `tests/test_story_project_writer.py`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_story_project_writer`: passed, 11 tests.
- `python -B -m unittest tests.test_story_project_writer tests.test_executor.AgentExecutorTest.test_story_project_context_records_blueprint_coverage_without_writeback tests.test_executor.AgentExecutorTest.test_story_project_real_writeback_blocked_is_recorded tests.test_cli.CliTest.test_parse_args_accepts_story_project_writeback_flags tests.test_cli.CliTest.test_story_project_real_writeback_conflicts_with_global_dry_run tests.test_cli.CliTest.test_story_project_writeback_requires_story_project tests.test_cli.CliTest.test_story_project_real_writeback_failure_exits_nonzero`: passed, 14 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 643 tests.
- `python -B scripts/smoke_v1.py`: passed.

Explicitly not done in Phase 3:

- StoryProject writeback is not enabled by default.
- Real writeback must be explicitly enabled.
- Dry-run writeback only writes runtime artifacts and does not modify StoryProject `正文/` or `追踪/`.
- No default prose overwrite.
- No oh-story JS execution.
- No oh-story API provider.
- No `api/oh_story_client.py`.
- No Review repair loop.
- No automatic prose repair.
- No LLM tracking summarization.
- No mixing StoryProject writeback with Notion or Memory writeback.

Next recommended step:

- Stop after Phase 3. Phase 4 should be planned separately if enhanced detection, review repair, or richer writeback reconciliation is needed.

## Phase 4: Review Repair Loop

Status: implemented.

Completed:

- Added explicit review-driven repair controls:
  - `--review-auto-repair` enables automatic repair only when `--enable-review-pipeline` is also set.
  - `--review-repair-max-attempts N` caps repair attempts to `1..3`, defaulting to `1`.
  - `--review-repair-dry-run` builds repair plan/runtime artifacts without changing runtime chapter text.
- Added `core/review/repair_loop.py` for review repair config, plan building, attempt tracking, acceptance/rejection semantics, and structured result payloads.
- Reused existing `modules.scene_repair` through a review-derived repair plan instead of adding a new repair engine.
- Moved runtime review and optional repair before snapshot, memory, and StoryProject writeback side effects.
- Re-runs deterministic validation after repair.
- Re-runs Review Pipeline in isolated artifact directories:
  - `<review-output-dir>/<run_id>/original/`
  - `<review-output-dir>/<run_id>/repair_attempt_01/`
- Rechecks StoryProject blueprint coverage after accepted repair when StoryProject context is present.
- Accepts repaired chapters only when validation passes, review status is `pass` or `warning`, and the configured review gate passes.
- Rejects failed, blocked, or dry-run repairs before StoryProject writeback.
- Added runtime repair artifacts under `<run-dir>/review_repairs/<run_id>/`.
- Added optional `run.review_repair` / result `review_repair` schema support.
- Kept diagnostic review behavior unchanged when `--review-auto-repair` is not set.

Modified files:

- `core/engine/artifacts.py`
- `core/engine/executor.py`
- `core/review/__init__.py`
- `core/review/repair_loop.py`
- `core/review/runtime.py`
- `main.py`
- `schemas/review_repair.schema.json`
- `schemas/run_record.schema.json`
- `schemas/run_result.schema.json`
- `tests/test_cli.py`
- `tests/test_executor.py`
- `tests/test_review_repair_loop.py`
- `docs/runtime-review-integration.md`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_review_repair_loop`: passed, 4 tests.
- `python -B -m unittest tests.test_runtime_review_integration`: passed, 8 tests.
- `python -B -m unittest tests.test_runtime_review_gate_integration`: passed, 6 tests.
- `python -B -m unittest tests.test_review_index`: passed, 9 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 653 tests.
- `python -B scripts/smoke_v1.py`: passed.

Explicitly not done in Phase 4:

- Review repair remains disabled by default.
- StoryProject writeback remains disabled by default and must still be explicitly enabled.
- Review repair dry-run only writes runtime artifacts and does not change runtime chapter text.
- No direct StoryProject file writes from review repair.
- No oh-story JS execution.
- No oh-story API provider.
- No `api/oh_story_client.py`.
- No oh-story enhanced detection.
- No LLM tracking summarization.
- No dashboard redesign.
- No Phase 5 work.

Next recommended step:

- Stop after Phase 4. Phase 5 should be planned separately if additional writeback reconciliation or enhanced detection is needed.

## Phase 5: oh-story Enhanced Detection / Compatibility Report

Status: implemented.

Completed:

- Added read-only oh-story compatibility detection for StoryProject roots.
- Detected optional markers including `.story-deployed`, `.codex/hooks.json`, `.claude/agents/`, `.codex/agents/`, `AGENTS.md`, package scripts, shallow oh-story config files, and StoryProject core directories.
- Added a structured compatibility report with `detected`, `confidence`, `markers`, `capabilities`, `warnings`, `unsupported`, and `recommendations`.
- Integrated `oh_story_detection` into `--check --story-project ...` as a non-blocking preflight check.
- Added `--story-project-compat-report` as a read-only report command.
- Recorded optional `run.story_project.oh_story` in StoryProject generation run records.
- Kept `AgentExecutor` attach-only: it receives an already-built report and does not scan StoryProject files.

Modified files:

- `core/story_project/oh_story_detection.py`
- `core/story_project/__init__.py`
- `core/engine/preflight.py`
- `core/engine/executor.py`
- `main.py`
- `schemas/oh_story_compatibility.schema.json`
- `schemas/run_record.schema.json`
- `tests/test_oh_story_detection.py`
- `tests/test_story_project.py`
- `tests/test_cli.py`
- `tests/test_executor.py`
- `docs/oh-story-compatibility.md`
- `docs/oh-story-claudecode-storyproject-plan.md`
- `docs/storyproject-v2-progress.md`

Test results:

- `python -B -m unittest tests.test_oh_story_detection`: passed, 7 tests.
- Targeted preflight / CLI / executor regressions: passed, 6 tests.
- `python main.py --check --dry-run --memory data/notion_memory.example.json`: passed, 20 checks.
- `python -B -m unittest discover -s tests`: passed, 662 tests.
- `python -B scripts/smoke_v1.py`: passed.

Explicitly not done in Phase 5:

- No oh-story JS execution.
- No `npm`, `pnpm`, or `node` execution.
- No oh-story API provider or LLM provider.
- No `api/oh_story_client.py`.
- No requirement for `.story-deployed`, `.codex/hooks.json`, or story agents.
- No StoryProject file writes from detection.
- No StoryProject writeback policy changes.
- No Review Repair Loop behavior changes.
- No Phase 6 work.

Next recommended step:

- Stop after Phase 5. Phase 6 should be planned separately if optional script execution, deeper agent integration, or setup assistance is needed.
