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

Next recommended step:

- Stop after Phase 1. Phase 2 should separately handle generation integration, StoryProject writeback, and Chapter Pipeline `required_beats` / `ending_pressure` hard-contract enforcement.
