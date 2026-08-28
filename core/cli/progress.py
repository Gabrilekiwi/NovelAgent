from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, TextIO


PROGRESS_JSONL_SCHEMA_VERSION = 1

_EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_PAYLOAD_FIELDS = (
    "step",
    "requested_steps",
    "completed_steps",
    "duration_ms",
    "committed",
    "status",
    "stopped_reason",
    "error_type",
    "node_attempt_count",
)


class ProgressJSONLError(ValueError):
    """Raised when an append-only progress stream is not safe to use."""


def build_progress_jsonl_record(
    event: dict[str, Any],
    *,
    sequence: int,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build the stable, deliberately narrow public progress-event record."""

    if sequence < 1:
        raise ProgressJSONLError("progress sequence must be at least 1")
    event_name = str(event.get("event") or "")
    if not _EVENT_NAME_PATTERN.fullmatch(event_name):
        raise ProgressJSONLError("progress event name is missing or invalid")

    payload = {
        key: _safe_progress_value(event[key])
        for key in _SAFE_PAYLOAD_FIELDS
        if key in event
    }
    record = {
        "schema_version": PROGRESS_JSONL_SCHEMA_VERSION,
        "sequence": sequence,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "event": event_name,
        "stage": _progress_stage(event_name),
        "chapter_index": _optional_positive_int(event.get("chapter_index")),
        "run_id": _optional_identifier(event.get("run_id")),
        "payload": payload,
    }
    for key in ("runtime_mode", "node_id", "node_attempt_id"):
        value = _optional_identifier(event.get(key))
        if value is not None:
            record[key] = value
    return record


class ProgressJSONLWriter:
    """Append and flush one UTF-8 NDJSON record for every observed event."""

    def __init__(self, path: str | Path) -> None:
        self.path = validate_progress_jsonl_path(path)
        self._sequence = _validate_existing_stream(self.path)
        self._stream: TextIO = self.path.open("a", encoding="utf-8", newline="\n")

    def __call__(self, event: dict[str, Any]) -> None:
        self._sequence += 1
        record = build_progress_jsonl_record(event, sequence=self._sequence)
        self._stream.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> ProgressJSONLWriter:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def validate_progress_jsonl_path(path: str | Path) -> Path:
    supplied = Path(path)
    if not supplied.is_absolute():
        raise ProgressJSONLError("--progress-jsonl requires an absolute file path")
    if supplied.name in {"", ".", ".."}:
        raise ProgressJSONLError("--progress-jsonl requires a file name")
    if os.name == "nt" and (
        str(supplied).startswith(("\\\\", "//"))
        or supplied.is_reserved()
        or ":" in supplied.name
        or supplied.name.rstrip(" .") != supplied.name
    ):
        raise ProgressJSONLError("--progress-jsonl requires a normal local file")

    parent = supplied.parent
    if not parent.exists() or not parent.is_dir():
        raise ProgressJSONLError(
            "--progress-jsonl parent directory must already exist"
        )
    resolved_parent = parent.resolve(strict=True)

    if supplied.is_symlink():
        raise ProgressJSONLError("--progress-jsonl cannot target a symbolic link")
    if supplied.exists():
        if not supplied.is_file():
            raise ProgressJSONLError("--progress-jsonl must target a normal file")
        resolved = supplied.resolve(strict=True)
        if resolved.parent != resolved_parent:
            raise ProgressJSONLError(
                "--progress-jsonl target must remain under its supplied parent"
            )
        return resolved

    resolved = resolved_parent / supplied.name
    if resolved.parent != resolved_parent:
        raise ProgressJSONLError(
            "--progress-jsonl target must remain under its supplied parent"
        )
    return resolved


def _validate_existing_stream(path: Path) -> int:
    if not path.exists():
        return 0
    raw = path.read_bytes()
    if not raw:
        return 0
    if not raw.endswith(b"\n"):
        raise ProgressJSONLError(
            "existing --progress-jsonl file has an incomplete final record"
        )

    last_sequence = 0
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ProgressJSONLError(
            "existing --progress-jsonl file is not valid UTF-8"
        ) from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProgressJSONLError(
                f"existing --progress-jsonl record {line_number} is invalid JSON"
            ) from exc
        if not isinstance(record, dict):
            raise ProgressJSONLError(
                f"existing --progress-jsonl record {line_number} is not an object"
            )
        if record.get("schema_version") != PROGRESS_JSONL_SCHEMA_VERSION:
            raise ProgressJSONLError(
                f"existing --progress-jsonl record {line_number} uses an unsupported schema"
            )
        sequence = record.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != last_sequence + 1:
            raise ProgressJSONLError(
                f"existing --progress-jsonl record {line_number} has a non-contiguous sequence"
            )
        last_sequence = sequence
    return last_sequence


def _progress_stage(event_name: str) -> str:
    if event_name.startswith("loop_"):
        return "loop"
    if event_name.startswith("step_"):
        return "chapter"
    return "unknown"


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    identifier = str(value)
    if not identifier or len(identifier) > 200:
        return None
    return identifier


def _safe_progress_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:200]


__all__ = [
    "PROGRESS_JSONL_SCHEMA_VERSION",
    "ProgressJSONLError",
    "ProgressJSONLWriter",
    "build_progress_jsonl_record",
    "validate_progress_jsonl_path",
]
