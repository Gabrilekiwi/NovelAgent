from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.memory_v2.validator import validate_canonical_memory


def save_canonical_memory(path: str | Path, memory: dict[str, Any]) -> dict[str, Any]:
    validated = validate_canonical_memory(memory)
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _tmp_path_for(target_path)

    _write_json(tmp_path, validated)
    try:
        _atomic_replace(tmp_path, target_path)
    except PermissionError as exc:
        if not _is_windows_permission_denied(exc):
            raise
        _write_json(target_path, validated)

    return validated


def load_canonical_memory(path: str | Path) -> dict[str, Any]:
    memory_path = Path(path)
    with memory_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return validate_canonical_memory(payload)


def _tmp_path_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp")


def _atomic_replace(tmp_path: Path, target_path: Path) -> None:
    tmp_path.replace(target_path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _is_windows_permission_denied(exc: PermissionError) -> bool:
    return getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) == 13


__all__ = [
    "load_canonical_memory",
    "save_canonical_memory",
]
