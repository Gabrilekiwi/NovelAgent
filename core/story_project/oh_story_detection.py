from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.schema import validate_schema
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import ACTIVE_BOOK_FILENAME


SCRIPT_KEYWORDS = ("oh-story", "story", "claudecode", "claude-code", "codex")
UNSUPPORTED_CAPABILITIES = ("oh_story_js_execution", "oh_story_api_provider")
OH_STORY_CONFIG_PATTERNS = ("oh-story.config.*", ".oh-story*", "story.config.*")


def detect_oh_story_compatibility(story_project_root: str | Path | None) -> dict[str, Any]:
    root = Path(story_project_root) if story_project_root is not None else None
    warnings: list[str] = []
    markers: list[dict[str, Any]] = []

    if root is None:
        return _report(root=None, markers=markers, warnings=["StoryProject root is not available."])
    if not root.exists():
        return _report(root=root, markers=markers, warnings=[f"StoryProject root does not exist: {root}"])
    if not root.is_dir():
        return _report(root=root, markers=markers, warnings=[f"StoryProject root is not a directory: {root}"])

    markers.extend(_core_directory_markers(root))
    markers.append(_path_marker(root, ".story-deployed", "deployment_marker"))
    markers.append(_json_marker(root, ".codex/hooks.json", "codex_hooks", warnings=warnings))
    markers.append(_path_marker(root, ".claude/agents", "claude_agents", expected_dir=True))
    markers.append(_path_marker(root, ".codex/agents", "codex_agents", expected_dir=True))
    markers.append(_path_marker(root, "AGENTS.md", "agents_doc"))
    markers.append(_package_scripts_marker(root, warnings=warnings))
    markers.extend(_config_markers(root, warnings=warnings))

    return _report(root=root, markers=markers, warnings=warnings)


def failed_oh_story_compatibility_report(
    story_project_root: str | Path | None,
    error: BaseException,
) -> dict[str, Any]:
    root = Path(story_project_root) if story_project_root is not None else None
    return _report(
        root=root,
        markers=[],
        warnings=[f"oh-story compatibility detection failed: {type(error).__name__}: {error}"],
    )


def _report(*, root: Path | None, markers: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    unsupported = list(UNSUPPORTED_CAPABILITIES)
    present_count = sum(1 for marker in markers if marker["present"])
    optional_missing_count = sum(1 for marker in markers if not marker["present"] and not marker["required"])
    confidence = _confidence(markers)
    report = {
        "enabled": True,
        "detected": confidence != "none",
        "confidence": confidence,
        "root": str(root) if root is not None else None,
        "markers": markers,
        "summary": {
            "present_count": present_count,
            "optional_missing_count": optional_missing_count,
            "unsupported_count": len(unsupported),
        },
        "capabilities": {
            "story_project_core_dirs": _core_dirs_present(markers),
            "active_book": True,
            "chapter_blueprint": True,
            "story_project_writeback": True,
            "review_repair_loop": True,
            "oh_story_js_execution": False,
            "oh_story_provider": False,
        },
        "warnings": list(dict.fromkeys(warnings)),
        "unsupported": unsupported,
        "recommendations": _recommendations(confidence),
    }
    return validate_schema(report, "oh_story_compatibility.schema.json")


def _core_directory_markers(root: Path) -> list[dict[str, Any]]:
    return [
        _marker(
            name=f"{directory_name}/",
            path=root / directory_name,
            present=(root / directory_name).is_dir(),
            kind="story_project_core_dir",
            details={},
        )
        for directory_name in CORE_DIRECTORY_NAMES
    ]


def _path_marker(
    root: Path,
    relative_path: str,
    kind: str,
    *,
    expected_dir: bool = False,
) -> dict[str, Any]:
    path = root / relative_path
    present = path.is_dir() if expected_dir else path.exists()
    return _marker(name=relative_path, path=path, present=present, kind=kind, details={})


def _json_marker(root: Path, relative_path: str, kind: str, *, warnings: list[str]) -> dict[str, Any]:
    path = root / relative_path
    present = path.exists()
    details: dict[str, Any] = {}
    if present:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            details["json_valid"] = True
            if isinstance(payload, dict):
                details["top_level_keys"] = sorted(str(key) for key in payload.keys())[:20]
        except json.JSONDecodeError as exc:
            details["json_valid"] = False
            warnings.append(f"{relative_path}: invalid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})")
        except OSError as exc:
            details["json_valid"] = False
            warnings.append(f"{relative_path}: unreadable ({exc})")
    return _marker(name=relative_path, path=path, present=present, kind=kind, details=details)


def _package_scripts_marker(root: Path, *, warnings: list[str]) -> dict[str, Any]:
    path = root / "package.json"
    present = path.exists()
    details: dict[str, Any] = {"scripts": []}
    if present:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict):
                matches = []
                for name, command in sorted(scripts.items()):
                    text = f"{name} {command}".lower()
                    if any(keyword in text for keyword in SCRIPT_KEYWORDS):
                        matches.append({"name": str(name), "command": str(command)})
                details["scripts"] = matches
                present = bool(matches)
            else:
                present = False
        except json.JSONDecodeError as exc:
            warnings.append(f"package.json: invalid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})")
            details["json_valid"] = False
            present = True
        except OSError as exc:
            warnings.append(f"package.json: unreadable ({exc})")
            details["json_valid"] = False
            present = True
    return _marker(name="package.json:scripts", path=path, present=present, kind="package_scripts", details=details)


def _config_markers(root: Path, *, warnings: list[str]) -> list[dict[str, Any]]:
    matches: list[Path] = []
    for pattern in OH_STORY_CONFIG_PATTERNS:
        try:
            matches.extend(root.glob(pattern))
        except OSError as exc:
            warnings.append(f"{pattern}: directory scan failed ({exc})")
    unique = sorted({path for path in matches if path.name != ACTIVE_BOOK_FILENAME}, key=lambda item: item.name)
    if not unique:
        return [_marker(name="oh-story config", path=None, present=False, kind="oh_story_config", details={})]
    return [
        _marker(
            name=path.name,
            path=path,
            present=True,
            kind="oh_story_config",
            details={"is_dir": path.is_dir()},
        )
        for path in unique[:20]
    ]


def _marker(
    *,
    name: str,
    path: Path | None,
    present: bool,
    kind: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": name,
        "path": str(path) if path is not None else None,
        "present": bool(present),
        "required": False,
        "kind": kind,
        "details": details,
    }


def _confidence(markers: list[dict[str, Any]]) -> str:
    present_kinds = {str(marker["kind"]) for marker in markers if marker["present"]}
    oh_specific = present_kinds - {"story_project_core_dir"}
    if not oh_specific:
        return "none"
    if "deployment_marker" in oh_specific and len(oh_specific & {"codex_hooks", "claude_agents", "codex_agents", "package_scripts"}) >= 2:
        return "high"
    if len(oh_specific) >= 2 or oh_specific & {"codex_hooks", "claude_agents", "codex_agents"}:
        return "medium"
    return "low"


def _core_dirs_present(markers: list[dict[str, Any]]) -> bool:
    core_markers = [marker for marker in markers if marker["kind"] == "story_project_core_dir"]
    return bool(core_markers) and all(marker["present"] for marker in core_markers)


def _recommendations(confidence: str) -> list[str]:
    if confidence == "none":
        return ["No oh-story optional markers were detected; NovelAgent can still use StoryProject compatible mode."]
    return ["Treat detected oh-story assets as optional compatibility signals; NovelAgent will not execute them."]


__all__ = [
    "detect_oh_story_compatibility",
    "failed_oh_story_compatibility_report",
]
