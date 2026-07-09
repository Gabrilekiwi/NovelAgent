from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.memory_v2.validator import validate_canonical_memory


def save_canonical_memory(path: str | Path, memory: dict[str, Any]) -> dict[str, Any]:
    validated = validate_canonical_memory(memory)
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # Some managed Windows workspaces deny rename/delete operations while still
    # allowing normal file writes. Write directly so compile and storage tests
    # remain usable in that environment.
    with target_path.open("w", encoding="utf-8") as f:
        json.dump(validated, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

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


__all__ = [
    "load_canonical_memory",
    "save_canonical_memory",
]
