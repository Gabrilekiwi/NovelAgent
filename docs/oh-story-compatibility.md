# oh-story Compatibility Detection

Phase 5 provides read-only oh-story compatibility detection for StoryProject mode. The detector distinguishes the StoryProject root (book data) from the optional workspace root (deployed skills, hooks, agents, and scripts). Existing one-argument callers remain valid and use the StoryProject root as both scopes.

Detection is deliberately based on verified assets instead of broad names:

- `.story-deployed` is an installation signal.
- `story-setup` counts only when a supported skill path contains a readable `SKILL.md` with story deployment content.
- `.codex/hooks.json` counts only when it is valid JSON and routes to `story_codex_hook.py`; the adapter is reported separately and must contain recognizable oh-story guard logic.
- `AGENTS.md` counts only when it contains an explicit `story-setup` route plus another recognized story workflow.
- The agent report checks the seven canonical roles by exact filename and validates their declared names/content. An arbitrary agents directory or unrelated agent never counts.
- The quality-script report checks the exact `check-ai-patterns.js`, `check-degeneration.js`, and `normalize-punctuation.js` assets under supported story skill roots. Package scripts count only when a valid `package.json` references one of those exact filenames; names such as `history` do not match accidentally.

The report keeps StoryProject capabilities separate from deployment capabilities. Core directories, `.active-book` matching, chapter blueprints, writeback readiness, story setup, Codex hooks, the complete seven-agent set, and the complete three-script set are derived from inspected state. Missing or invalid roots therefore report those capabilities as false instead of assuming they exist.

This detection remains non-blocking:

- Missing oh-story markers do not affect StoryProject generation.
- Invalid JSON/TOML or unreadable text records a warning and does not become a positive signal.
- The detector never imports or runs hooks, agents, npm, pnpm, node, Python adapters, or JavaScript scripts.
- oh-story is not an API provider or LLM provider.
- StoryProject remains the source of truth, Snapshot remains a runtime cache, RunRecord remains the audit layer, and Memory writeback remains a sync layer.

Use:

```bash
python main.py --story-project auto --story-project-compat-report
```

`--check --story-project ...` also includes a concise, non-blocking `oh_story_detection` preflight check.
