from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.state.snapshot import normalize_snapshot


def inspect_snapshot_text(snapshot: dict[str, Any]) -> dict[str, Any]:
    suspicious = []
    for path, value in _walk_strings(snapshot):
        if "\ufffd" in value or "????" in value:
            suspicious.append({"path": path, "value": value[:120]})
    return {
        "ok": not suspicious,
        "suspicious_count": len(suspicious),
        "suspicious": suspicious[:20],
    }


def load_normalized_snapshot(path: str | Path) -> dict[str, Any]:
    return normalize_snapshot(json.loads(Path(path).read_text(encoding="utf-8")))


def write_normalized_snapshot(snapshot: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(normalize_snapshot(snapshot), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _walk_strings(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if isinstance(value, str):
        items.append((prefix, value))
    elif isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            items.extend(_walk_strings(key_text, f"{prefix}.{key_text}:key"))
            items.extend(_walk_strings(child, f"{prefix}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            items.extend(_walk_strings(child, f"{prefix}[{index}]"))
    return items
