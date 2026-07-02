# Memory Input

NovelAgent v1.0 treats memory as data that is normalized before the agent loop runs.

Supported inputs:

- `.tmp/runtime/notion_memory.json`: initialized local runtime memory copied from the Notion memory example.
- `NOVELAGENT_MEMORY_PATH`: environment override.
- `--memory path/to/file.json`: CLI override.
- `--memory-source auto|file|notion`: explicit memory input mode.
- `--memory path/to/memory_outbox.jsonl`: JSONL outbox produced by writeback.
- Notion export JSON with a top-level `pages` array.
- Notion API database query via `NOTION_API_KEY` plus `NOTION_DATABASE_ID` or `NOVELAGENT_NOTION_DATABASE_ID`.

`--memory-source auto` is the default. It reads `--memory` or `NOVELAGENT_MEMORY_PATH` when a path is provided, otherwise uses `.tmp/runtime/notion_memory.json`, and falls back to live Notion API only when no memory path is provided and Notion API configuration is present. Use `python main.py --init-runtime` to copy `data/notion_memory.example.json` into that local runtime path. Use `--memory-source file` to force local file memory even when Notion credentials exist. Use `--memory-source notion` to force live Notion API memory. Preflight reports the resolution under `memory_input`, including whether Notion API credentials are configured, the resolved source, the resolved file path for file-backed memory, and the reason the source was selected.

Normalized memory context:

```json
{
  "source": "notion-export",
  "status": "ready",
  "items": [
    {
      "id": "chapter_1:location:shelter",
      "type": "location",
      "name": "shelter",
      "source_run_id": "run_id",
      "data": {
        "aliases": ["sealed gate"],
        "risk": "rising"
      }
    }
  ]
}
```

Supported item types are a runtime contract. Types are normalized to lowercase, and unknown types fail during memory loading or preflight instead of being ignored:

- `world_state`: merged into `snapshot.world_state`.
- `location`: added to `snapshot.world_state.locations`.
- `character`: added to `snapshot.characters`.
- `constraint`: appended to `snapshot.constraints`.
- `timeline_event`: appended to `snapshot.timeline`.

List-style memory items are deduplicated while building the runtime Snapshot. `constraint` items use their rule text when no stable id is present. Committed Snapshot timeline entries store stable `memory_id`, `memory_ids`, `name`, and `source_run_id` metadata, and `timeline_event` memory items preserve the same identity fields, so repeated outbox imports do not append the same summary or event again. Snapshot Builder audit entries mark duplicate skips as `duplicate_memory` with low severity and missing named-memory labels as `missing_name` with medium severity.

Loaded memory also carries `source_mappings`. JSON files map items to the file path and index, JSONL outboxes map items to file path and line number, and Notion sources map items to page id/page URL/page index. The chapter input pack includes a compact Memory Index with these mappings for auditability without duplicating full item `data`, and a separate Recovery Context for compact last-run validation and repair signals. Snapshot Builder applied/skipped audit items reuse the same mappings so memory decisions can be traced back to their source. Run records also store compact mapping coverage counts, including mapping source counts, file path count, JSONL line count, Notion page id count, and Notion page URL count.

Optional metadata:

- `id`: stable memory item identity. Writeback-generated ids include the chapter index, item type, and name.
- `source_run_id`: run id that produced the memory item.

Machine-checkable constraints:

```json
{
  "type": "constraint",
  "data": {
    "rule": "Keep serum in focus.",
    "required_terms": ["serum"],
    "forbidden_terms": ["serum conflict resolved"]
  }
}
```

Notion export shape:

```json
{
  "pages": [
    {
      "properties": {
        "Type": "location",
        "Name": "shelter",
        "Risk": "rising"
      }
    }
  ]
}
```

Property names are converted to snake case, so `Current Location` becomes `current_location`.

Notion API mode:

```bash
set NOTION_API_KEY=secret_xxx
set NOTION_DATABASE_ID=your_database_id
python main.py --check --dry-run --memory-source notion
```

The API response is normalized through the same `pages[].properties` converter used by the export format, but the runtime memory source is recorded as `notion-api`. Real Notion property wrappers are unwrapped before memory validation, including `select`, `status`, `title`, `rich_text`, `multi_select`, `date`, `url`, `email`, `phone_number`, `people`, `relation`, `files`, and created/edited metadata.

## Memory Writeback

Committed runs can produce memory updates from chapter analysis:

- `timeline_event` for summaries and extracted events.
- `world_state` for extracted world changes.
- `character` for extracted character status and location changes.
- `location` for newly detected locations.

Local outbox mode:

```bash
python main.py --persist-dry-run --dry-run --memory data/notion_memory.example.json --memory-writeback file
```

The outbox is JSONL, one normalized memory item per line. `--memory-outbox` implies `--memory-writeback file` for compatibility. You can also use `--memory-writeback file`; without an explicit outbox path it writes to `.tmp/runtime/memory_outbox.jsonl`. File writeback skips duplicate items that have the same `id`. CLI-created Notion writeback also performs a pre-write database query and skips updates whose `Memory ID` already exists.

Writeback is gated by run quality. A committed run writes memory only when final validation is ok and repair deltas do not show new, remaining, or post-repair problem codes. The gate records pending memory update count/type summaries before it allows or blocks writes. Blocked writeback is recorded in the run artifact under `memory.writeback.gate` and writes no JSONL or Notion pages.

Writeback results include `item_mappings`. File writeback maps each memory id to the outbox path and line number, while Notion writeback maps each memory id to the created or existing page id, page URL, database id, and property names sent to Notion. File writeback also records a `verification` block after append by reading the mapped JSONL lines back and checking `id`, `type`, and `name`. Notion writeback records response-level verification by default; with `--notion-readback`, it also queries the database after writing and verifies each written `Memory ID`, `Type`, and `Name`. This makes it possible to audit which run produced a long-term memory item, where it was stored, whether it was skipped as a duplicate, and whether configured storage can be read back consistently.

The same outbox can be used as memory input for a later run:

```bash
python main.py --dry-run --memory .tmp/runtime/memory_outbox.jsonl
```

The chapter input pack receives a compact Memory Index rather than a second full copy of every memory item's `data`. Facts needed for generation should be present in the merged Snapshot; the Memory Index keeps ids, names, types, source run ids, and source mappings for traceability. Last-run status, problem codes, validation coverage, and repair summaries are exposed separately as Recovery Context so generation can see recovery intent in the input pack and model-backed scene repair can receive the same context as explicit payload data without scanning the full memory payload.

Programmatic Notion writeback is available through `core.state.memory_writer.NotionMemoryWriter`. It writes the same normalized memory updates as Notion pages with `Memory ID`, `Type`, `Name`, and `Data` properties, then returns page ids in `item_mappings`.

CLI Notion writeback:

```bash
python main.py --memory data/notion_memory.example.json --memory-writeback notion
```

This requires `NOTION_API_KEY` plus `NOTION_DATABASE_ID` or `NOVELAGENT_NOTION_DATABASE_ID`.

To verify that Notion pages can be queried back after writeback:

```bash
python main.py --memory data/notion_memory.example.json --memory-writeback notion --notion-readback
```
