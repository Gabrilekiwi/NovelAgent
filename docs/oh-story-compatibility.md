# oh-story Compatibility Detection

Phase 5 adds read-only oh-story compatibility detection for StoryProject mode.

The detector reports optional markers such as `.story-deployed`, `.codex/hooks.json`, agent directories, `AGENTS.md`, package scripts, and shallow oh-story config files. It also records the StoryProject core directory baseline and the NovelAgent capabilities that are already supported.

This detection is non-blocking:

- Missing oh-story markers do not affect StoryProject generation.
- Invalid marker JSON records a warning, not a fatal error.
- The detector never runs hooks, agents, npm, pnpm, node, or JavaScript scripts.
- oh-story is not an API provider or LLM provider.
- StoryProject remains the source of truth.
- Snapshot remains a runtime cache.
- RunRecord remains the audit layer.
- Memory writeback remains a sync layer.

Use:

```bash
python main.py --story-project auto --story-project-compat-report
```

`--check --story-project ...` also includes a concise `oh_story_detection` preflight check.
