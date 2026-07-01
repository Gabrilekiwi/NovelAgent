from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from api.openai_client import chat_completion
from core.director.director import DirectorDecisionError, validate_decision


CompletionFn = Callable[[list[dict[str, str]]], str]
DEFAULT_PROMPT_PATH = Path("prompts/director_prompt.md")
LEGACY_PROMPT_PATH = Path("core/director/prompt.md")
DEFAULT_SCHEMA_PATH = Path("schemas/director_decision.schema.json")


class ModelDirector:
    def __init__(
        self,
        *,
        model: str | None = None,
        completion: CompletionFn | None = None,
        prompt_path: str | Path | None = None,
        schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    ) -> None:
        self.model = model
        self.completion = completion
        self.prompt_path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
        self.schema_path = Path(schema_path)

    def __call__(
        self,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output = self._complete(self._messages(snapshot, memory_context or {}))
        return parse_director_output(output)

    def _complete(self, messages: list[dict[str, str]]) -> str:
        if self.completion:
            return self.completion(messages)
        return chat_completion(messages, model=self.model, temperature=0.2, stage="director_decision")

    def _messages(
        self,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
    ) -> list[dict[str, str]]:
        prompt = _read_prompt(self.prompt_path)
        schema = self.schema_path.read_text(encoding="utf-8")
        payload = {
            "snapshot": snapshot,
            "memory_context": _compact_memory_context(memory_context),
            "schema": json.loads(schema),
        }
        return [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]


def parse_director_output(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(_strip_json_fence(output))
    except json.JSONDecodeError as exc:
        raise DirectorDecisionError("Director model output must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise DirectorDecisionError("Director model output must be a JSON object")
    return validate_decision(payload)


def _read_prompt(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    if path == DEFAULT_PROMPT_PATH and LEGACY_PROMPT_PATH.exists():
        return LEGACY_PROMPT_PATH.read_text(encoding="utf-8")
    return path.read_text(encoding="utf-8")


def _strip_json_fence(output: str) -> str:
    text = output.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _compact_memory_context(memory_context: dict[str, Any]) -> dict[str, Any]:
    items = memory_context.get("items") or []
    return {
        "source": memory_context.get("source"),
        "status": memory_context.get("status"),
        "item_count": len(items) if isinstance(items, list) else 0,
        "items": items[:20] if isinstance(items, list) else [],
        "last_run": memory_context.get("last_run"),
        "snapshot_builder_audit": _compact_snapshot_builder_audit(memory_context.get("snapshot_builder_audit")),
    }


def _compact_snapshot_builder_audit(audit: Any) -> dict[str, Any] | None:
    if not isinstance(audit, dict):
        return None
    applied_items = audit.get("applied_items")
    skipped_items = audit.get("skipped_items")
    return {
        "source": audit.get("source"),
        "status": audit.get("status"),
        "item_count": audit.get("item_count"),
        "applied_count": audit.get("applied_count"),
        "skipped_count": audit.get("skipped_count"),
        "deduplicated_count": audit.get("deduplicated_count"),
        "applied_type_counts": audit.get("applied_type_counts", []),
        "skipped_type_counts": audit.get("skipped_type_counts", []),
        "skipped_blocking_count": audit.get("skipped_blocking_count", 0),
        "applied_source_mapping_count": _source_mapped_count(applied_items),
        "skipped_source_mapping_count": _source_mapped_count(skipped_items),
        "applied_source_mappings": _compact_audit_source_mappings(applied_items),
        "skipped_source_mappings": _compact_audit_source_mappings(skipped_items),
        "skipped_reason_counts": audit.get("skipped_reason_counts", []),
        "skipped_severity_counts": audit.get("skipped_severity_counts", []),
        "skipped_items": skipped_items[:20] if isinstance(skipped_items, list) else [],
    }


def _source_mapped_count(items: Any) -> int:
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if isinstance(item, dict) and isinstance(item.get("source_mapping"), dict))


def _compact_audit_source_mappings(items: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    mappings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("source_mapping"), dict):
            continue
        mapping = item["source_mapping"]
        compact = _compact_source_mapping(mapping)
        if item.get("reason_code"):
            compact["reason_code"] = item.get("reason_code")
        mappings.append(compact)
        if len(mappings) >= limit:
            break
    return mappings


def _compact_source_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    allowed = ("index", "source", "memory_id", "type", "name", "path", "line_number", "page_id", "page_url", "page_index")
    return {key: mapping[key] for key in allowed if key in mapping}


__all__ = ["ModelDirector", "parse_director_output"]
