from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from core.schema import validate_schema


INDEX_FILENAME = "review_index.json"
REVIEW_STATUSES = {"pass", "warning", "needs_revision", "blocked", "error", "unknown"}
GATE_STATUSES = {"disabled", "pass", "fail", "error"}


def build_review_index_entry(
    *,
    run_id: str,
    review_pipeline: dict[str, Any],
    review_gate: dict[str, Any] | None = None,
    artifacts_dir: str | Path | None = None,
) -> dict[str, Any]:
    raw_artifacts_dir = artifacts_dir or review_pipeline.get("artifacts_dir")
    artifact_root = Path(str(raw_artifacts_dir)) if raw_artifacts_dir is not None else None
    artifact_root_text = str(artifact_root) if artifact_root is not None else None
    human_report = _artifact_if_exists(artifact_root, "human_review_report.md")
    repair_prompt = _artifact_if_exists(artifact_root, "rule_repair_prompt.md")
    gate = review_gate if isinstance(review_gate, dict) else {}
    entry = {
        "run_id": str(run_id),
        "created_at": _created_at_from_run_id(str(run_id)),
        "chapter_index": _chapter_index_from_run_id(str(run_id)),
        "review_status": _review_status(review_pipeline.get("status")),
        "review_decision": _nullable_str(review_pipeline.get("decision")),
        "quality_score": _nullable_int(review_pipeline.get("quality_score")),
        "rule_score": _nullable_int(review_pipeline.get("rule_score")),
        "repair_task_count": _nullable_int(review_pipeline.get("repair_task_count")),
        "blocking_task_count": _nullable_int(review_pipeline.get("blocking_task_count")),
        "gate_enabled": bool(gate.get("enabled")) if gate else False,
        "gate_threshold": _nullable_str(gate.get("threshold")) if gate else "off",
        "gate_status": _nullable_str(gate.get("status")) if gate else "disabled",
        "gate_exit_code": _nullable_int(gate.get("exit_code")) if gate else 0,
        "artifacts_dir": artifact_root_text,
        "summary_path": _nullable_str(review_pipeline.get("summary_path")),
        "human_report_path": human_report,
        "repair_prompt_path": repair_prompt,
    }
    validate_schema(_index_for_entries(review_output_dir="", entries=[entry]), "review_index.schema.json")
    return entry


def update_review_index(
    *,
    review_output_dir: str | Path,
    entry: dict[str, Any],
    max_entries: int | None = 200,
) -> dict[str, Any]:
    output_dir = Path(review_output_dir)
    index = load_review_index(review_output_dir=output_dir)
    entries = [item for item in index.get("entries", []) if item.get("run_id") != entry.get("run_id")]
    entries.append(dict(entry))
    entries = _sort_entries(entries)
    if max_entries is not None:
        entries = entries[: max(0, int(max_entries))]
    updated = _index_for_entries(review_output_dir=str(output_dir), entries=entries)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = review_index_path(output_dir)
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return validate_schema(updated, "review_index.schema.json")


def load_review_index(*, review_output_dir: str | Path) -> dict[str, Any]:
    output_dir = Path(review_output_dir)
    path = review_index_path(output_dir)
    if not path.exists():
        return _index_for_entries(review_output_dir=str(output_dir), entries=[])
    with path.open("r", encoding="utf-8-sig") as f:
        value = json.load(f)
    return validate_schema(value, "review_index.schema.json")


def get_latest_review(*, review_output_dir: str | Path) -> dict[str, Any] | None:
    index = load_review_index(review_output_dir=review_output_dir)
    entries = index.get("entries", [])
    return entries[0] if entries else None


def list_recent_reviews(
    *,
    review_output_dir: str | Path,
    limit: int = 10,
    status: str | None = None,
    gate_status: str | None = None,
) -> list[dict[str, Any]]:
    entries = list(load_review_index(review_output_dir=review_output_dir).get("entries", []))
    if status is not None:
        entries = [entry for entry in entries if entry.get("review_status") == status]
    if gate_status is not None:
        entries = [entry for entry in entries if entry.get("gate_status") == gate_status]
    return entries[: max(0, int(limit))]


def review_index_path(review_output_dir: str | Path) -> Path:
    return Path(review_output_dir) / INDEX_FILENAME


def _index_for_entries(*, review_output_dir: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_entries = _sort_entries(entries)
    index = {
        "schema_version": "1.0",
        "kind": "review_index",
        "review_output_dir": str(review_output_dir),
        "updated_at": _utc_now(),
        "latest_run_id": sorted_entries[0]["run_id"] if sorted_entries else None,
        "summary": _summary(sorted_entries),
        "entries": sorted_entries,
        "metadata": {
            "created_by": "NovelAgent",
            "source": "review-artifacts-index",
        },
    }
    return validate_schema(index, "review_index.schema.json")


def _summary(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "entry_count": len(entries),
        "pass_count": _count(entries, "pass"),
        "warning_count": _count(entries, "warning"),
        "needs_revision_count": _count(entries, "needs_revision"),
        "blocked_count": _count(entries, "blocked"),
        "error_count": _count(entries, "error"),
        "gate_fail_count": sum(1 for entry in entries if entry.get("gate_status") == "fail"),
    }


def _count(entries: list[dict[str, Any]], status: str) -> int:
    return sum(1 for entry in entries if entry.get("review_status") == status)


def _sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: (str(entry.get("created_at") or ""), str(entry.get("run_id") or "")), reverse=True)


def _created_at_from_run_id(run_id: str) -> str:
    parts = run_id.rsplit("_", 1)
    if len(parts) == 2:
        try:
            value = datetime.strptime(parts[1], "%Y%m%dT%H%M%S%fZ")
            return value.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return _utc_now()


def _chapter_index_from_run_id(run_id: str) -> int | None:
    parts = run_id.split("_")
    if len(parts) >= 2 and parts[0] == "chapter":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def _artifact_if_exists(root: Path | None, filename: str) -> str | None:
    if root is None:
        return None
    path = root / filename
    return str(path) if path.exists() else None


def _review_status(value: Any) -> str:
    text = str(value) if value is not None else "unknown"
    return text if text in REVIEW_STATUSES else "unknown"


def _nullable_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _nullable_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "INDEX_FILENAME",
    "build_review_index_entry",
    "get_latest_review",
    "list_recent_reviews",
    "load_review_index",
    "review_index_path",
    "update_review_index",
]
