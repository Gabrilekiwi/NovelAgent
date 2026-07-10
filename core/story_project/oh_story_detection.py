from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Iterable

from core.schema import validate_schema
from core.story_project.model import CORE_DIRECTORY_NAMES


UNSUPPORTED_CAPABILITIES = ("oh_story_js_execution", "oh_story_api_provider")
STORY_AGENT_NAMES = (
    "story-architect",
    "character-designer",
    "narrative-writer",
    "consistency-checker",
    "story-researcher",
    "story-explorer",
    "chapter-extractor",
)
QUALITY_SCRIPT_NAMES = (
    "check-ai-patterns.js",
    "check-degeneration.js",
    "normalize-punctuation.js",
)
SKILL_ROOTS = (
    "skills",
    ".codex/skills",
    ".claude/skills",
    ".opencode/skills",
    ".agents/skills",
)
QUALITY_SKILLS = ("story-deslop", "story-long-write", "story-short-write")
STORY_ROUTING_NAMES = (
    "story-setup",
    "story-long-write",
    "story-short-write",
    "story-review",
    "story-deslop",
    "story-import",
)
MAX_INSPECTION_CHARS = 512_000


def detect_oh_story_compatibility(
    story_project_root: str | Path | None,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect oh-story deployment assets without executing any discovered code.

    The one-argument form remains compatible with Phase 5: when ``workspace_root``
    is omitted, deployment assets are inspected beside the StoryProject root.
    Callers that keep books below a larger workspace should pass both roots.
    """

    root = Path(story_project_root) if story_project_root is not None else None
    workspace = Path(workspace_root) if workspace_root is not None else root
    warnings: list[str] = []
    markers: list[dict[str, Any]] = []

    if root is None:
        return _report(
            root=None,
            workspace_root=workspace,
            markers=markers,
            warnings=["StoryProject root is not available."],
        )
    if not root.exists():
        return _report(
            root=root,
            workspace_root=workspace,
            markers=markers,
            warnings=[f"StoryProject root does not exist: {root}"],
        )
    if not root.is_dir():
        return _report(
            root=root,
            workspace_root=workspace,
            markers=markers,
            warnings=[f"StoryProject root is not a directory: {root}"],
        )

    markers.extend(_core_directory_markers(root))
    if workspace is None or not workspace.exists() or not workspace.is_dir():
        if workspace is None:
            warnings.append("Workspace root is not available.")
        elif not workspace.exists():
            warnings.append(f"Workspace root does not exist: {workspace}")
        else:
            warnings.append(f"Workspace root is not a directory: {workspace}")
        markers.extend(_missing_workspace_markers())
    else:
        markers.append(_active_book_marker(workspace, root, warnings=warnings))
        markers.append(_path_marker(workspace, ".story-deployed", "deployment_marker"))
        markers.append(_story_setup_marker(workspace, warnings=warnings))
        markers.append(_codex_hooks_marker(workspace, warnings=warnings))
        markers.append(_codex_hook_adapter_marker(workspace, warnings=warnings))
        markers.append(_agents_doc_marker(workspace, warnings=warnings))
        markers.append(_story_agents_marker(workspace, warnings=warnings))
        markers.extend(_quality_script_markers(workspace, warnings=warnings))
        markers.append(_package_scripts_marker(workspace, warnings=warnings))

    return _report(root=root, workspace_root=workspace, markers=markers, warnings=warnings)


def failed_oh_story_compatibility_report(
    story_project_root: str | Path | None,
    error: BaseException,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(story_project_root) if story_project_root is not None else None
    workspace = Path(workspace_root) if workspace_root is not None else root
    return _report(
        root=root,
        workspace_root=workspace,
        markers=[],
        warnings=[f"oh-story compatibility detection failed: {type(error).__name__}: {error}"],
    )


def _report(
    *,
    root: Path | None,
    workspace_root: Path | None,
    markers: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    unsupported = list(UNSUPPORTED_CAPABILITIES)
    present_count = sum(1 for marker in markers if marker["present"])
    optional_missing_count = sum(1 for marker in markers if not marker["present"] and not marker["required"])
    confidence = _confidence(markers)
    core_dirs = _core_dirs_present(markers)
    report = {
        "enabled": True,
        "detected": confidence != "none",
        "confidence": confidence,
        "root": str(root) if root is not None else None,
        "workspace_root": str(workspace_root) if workspace_root is not None else None,
        "markers": markers,
        "summary": {
            "present_count": present_count,
            "optional_missing_count": optional_missing_count,
            "unsupported_count": len(unsupported),
        },
        "capabilities": {
            "story_project_core_dirs": core_dirs,
            "active_book": _active_book_matches(markers),
            "chapter_blueprint": bool(root is not None and root.is_dir() and (root / "大纲").is_dir()),
            "story_project_writeback": core_dirs,
            "review_repair_loop": core_dirs,
            "story_setup": _marker_present(markers, "story_setup_skill"),
            "codex_hooks": (
                _marker_present(markers, "codex_hooks")
                and _marker_present(markers, "codex_hook_adapter")
            ),
            "story_agents": _story_agents_complete(markers),
            "quality_scripts": all(
                _named_marker_present(markers, "quality_script", script_name)
                for script_name in QUALITY_SCRIPT_NAMES
            ),
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
            details={"scope": "story_project"},
        )
        for directory_name in CORE_DIRECTORY_NAMES
    ]


def _active_book_marker(workspace: Path, story_root: Path, *, warnings: list[str]) -> dict[str, Any]:
    path = workspace / ".active-book"
    details: dict[str, Any] = {"scope": "workspace", "matches_story_project": False}
    text = _read_text_if_file(path, warnings=warnings, label=".active-book")
    if text is not None:
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        details["value"] = first_line
        if first_line:
            candidate = Path(first_line)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                resolved = candidate.resolve()
                details["resolved_path"] = str(resolved)
                details["target_exists"] = resolved.is_dir()
                details["matches_story_project"] = resolved == story_root.resolve()
            except OSError as exc:
                warnings.append(f".active-book: cannot resolve target ({exc})")
        else:
            warnings.append(".active-book: first line is empty")
    return _marker(
        name=".active-book",
        path=path,
        present=path.is_file(),
        kind="active_book",
        details=details,
    )


def _path_marker(root: Path, relative_path: str, kind: str) -> dict[str, Any]:
    path = root / relative_path
    return _marker(
        name=relative_path,
        path=path,
        present=path.is_file(),
        kind=kind,
        details={"scope": "workspace"},
    )


def _story_setup_marker(workspace: Path, *, warnings: list[str]) -> dict[str, Any]:
    candidates = [workspace / root / "story-setup" / "SKILL.md" for root in SKILL_ROOTS]
    valid_path: Path | None = None
    existing_paths: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        existing_paths.append(str(path))
        text = _read_text_if_file(path, warnings=warnings, label=str(path.relative_to(workspace)))
        if text is None:
            continue
        lowered = text.lower()
        if "story-setup" in lowered and any(
            token in lowered for token in (".story-deployed", "hooks", "agents", ".codex")
        ):
            valid_path = path
            break
    return _marker(
        name="skills/story-setup/SKILL.md",
        path=valid_path or (candidates[0] if candidates else None),
        present=valid_path is not None,
        kind="story_setup_skill",
        details={
            "scope": "workspace",
            "content_valid": valid_path is not None,
            "existing_paths": existing_paths,
            "candidate_paths": [str(path) for path in candidates],
        },
    )


def _codex_hooks_marker(workspace: Path, *, warnings: list[str]) -> dict[str, Any]:
    path = workspace / ".codex" / "hooks.json"
    details: dict[str, Any] = {
        "scope": "workspace",
        "exists": path.is_file(),
        "json_valid": False,
        "story_routes": [],
    }
    if not path.is_file():
        return _marker(name=".codex/hooks.json", path=path, present=False, kind="codex_hooks", details=details)

    text = _read_text_if_file(path, warnings=warnings, label=".codex/hooks.json")
    if text is None:
        return _marker(name=".codex/hooks.json", path=path, present=False, kind="codex_hooks", details=details)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f".codex/hooks.json: invalid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})")
        return _marker(name=".codex/hooks.json", path=path, present=False, kind="codex_hooks", details=details)

    details["json_valid"] = True
    if isinstance(payload, dict):
        details["top_level_keys"] = sorted(str(key) for key in payload.keys())[:20]
    strings = [value for value in _iter_strings(payload)]
    routes = sorted({value for value in strings if "story_codex_hook.py" in value.replace("\\", "/")})
    details["story_routes"] = routes[:20]
    return _marker(
        name=".codex/hooks.json",
        path=path,
        present=bool(routes),
        kind="codex_hooks",
        details=details,
    )


def _codex_hook_adapter_marker(workspace: Path, *, warnings: list[str]) -> dict[str, Any]:
    path = workspace / ".codex" / "hooks" / "story_codex_hook.py"
    text = _read_text_if_file(path, warnings=warnings, label=".codex/hooks/story_codex_hook.py")
    matched_tokens: list[str] = []
    if text is not None:
        lowered = text.lower()
        matched_tokens = [
            token
            for token in ("oh-story", "story-setup", ".story-deployed", "pre-tool-prose-guard")
            if token in lowered
        ]
    valid = text is not None and "oh-story" in matched_tokens and len(matched_tokens) >= 2
    return _marker(
        name=".codex/hooks/story_codex_hook.py",
        path=path,
        present=valid,
        kind="codex_hook_adapter",
        details={
            "scope": "workspace",
            "exists": path.is_file(),
            "content_valid": valid,
            "matched_tokens": matched_tokens,
        },
    )


def _agents_doc_marker(workspace: Path, *, warnings: list[str]) -> dict[str, Any]:
    path = workspace / "AGENTS.md"
    text = _read_text_if_file(path, warnings=warnings, label="AGENTS.md")
    matched_routes: list[str] = []
    if text is not None:
        lowered = text.lower()
        matched_routes = [name for name in STORY_ROUTING_NAMES if name in lowered]
    valid = "story-setup" in matched_routes and len(matched_routes) >= 2
    return _marker(
        name="AGENTS.md:story-routing",
        path=path,
        present=valid,
        kind="agents_story_routing",
        details={
            "scope": "workspace",
            "exists": path.is_file(),
            "content_valid": valid,
            "matched_routes": matched_routes,
        },
    )


def _story_agents_marker(workspace: Path, *, warnings: list[str]) -> dict[str, Any]:
    agent_entries: list[dict[str, Any]] = []
    valid_paths: list[Path] = []
    for agent_name in STORY_AGENT_NAMES:
        candidates = (
            workspace / ".codex" / "agents" / f"{agent_name}.toml",
            workspace / ".claude" / "agents" / f"{agent_name}.md",
            workspace / ".opencode" / "agents" / f"{agent_name}.md",
        )
        valid_path: Path | None = None
        existing_paths: list[str] = []
        for path in candidates:
            if not path.is_file():
                continue
            existing_paths.append(str(path))
            if _valid_agent_file(path, agent_name, workspace=workspace, warnings=warnings):
                valid_path = path
                valid_paths.append(path)
                break
        agent_entries.append(
            {
                "name": agent_name,
                "present": valid_path is not None,
                "path": str(valid_path) if valid_path is not None else None,
                "existing_paths": existing_paths,
            }
        )

    missing = [entry["name"] for entry in agent_entries if not entry["present"]]
    return _marker(
        name="story-agents",
        path=valid_paths[0].parent if valid_paths else workspace / ".codex" / "agents",
        present=len(valid_paths) == len(STORY_AGENT_NAMES),
        kind="story_agents",
        details={
            "scope": "workspace",
            "expected_count": len(STORY_AGENT_NAMES),
            "present_count": len(valid_paths),
            "complete": len(valid_paths) == len(STORY_AGENT_NAMES),
            "missing_agents": missing,
            "agents": agent_entries,
        },
    )


def _valid_agent_file(
    path: Path,
    agent_name: str,
    *,
    workspace: Path,
    warnings: list[str],
) -> bool:
    label = str(path.relative_to(workspace))
    text = _read_text_if_file(path, warnings=warnings, label=label)
    if text is None:
        return False
    if path.suffix.lower() == ".toml":
        try:
            payload = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            warnings.append(f"{label}: invalid TOML ({exc})")
            return False
        if not isinstance(payload, dict) or payload.get("name") != agent_name:
            return False
        return any(bool(str(payload.get(key) or "").strip()) for key in ("developer_instructions", "description"))

    name_pattern = re.compile(rf"(?im)^\s*name\s*:\s*['\"]?{re.escape(agent_name)}['\"]?\s*$")
    return bool(name_pattern.search(text)) and len(text.strip()) >= 40


def _quality_script_markers(workspace: Path, *, warnings: list[str]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for script_name in QUALITY_SCRIPT_NAMES:
        candidates = [
            workspace / skill_root / skill_name / "scripts" / script_name
            for skill_root in SKILL_ROOTS
            for skill_name in QUALITY_SKILLS
        ]
        valid_path: Path | None = None
        existing_paths: list[str] = []
        for path in candidates:
            if not path.is_file():
                continue
            existing_paths.append(str(path))
            text = _read_text_if_file(path, warnings=warnings, label=str(path.relative_to(workspace)))
            if text is not None and _looks_like_javascript(text):
                valid_path = path
                break
        markers.append(
            _marker(
                name=script_name,
                path=valid_path or candidates[0],
                present=valid_path is not None,
                kind="quality_script",
                details={
                    "scope": "workspace",
                    "content_valid": valid_path is not None,
                    "existing_paths": existing_paths,
                    "candidate_paths": [str(path) for path in candidates],
                },
            )
        )
    return markers


def _package_scripts_marker(workspace: Path, *, warnings: list[str]) -> dict[str, Any]:
    path = workspace / "package.json"
    details: dict[str, Any] = {
        "scope": "workspace",
        "exists": path.is_file(),
        "json_valid": False,
        "scripts": [],
    }
    if not path.is_file():
        return _marker(name="package.json:quality-scripts", path=path, present=False, kind="package_scripts", details=details)

    text = _read_text_if_file(path, warnings=warnings, label="package.json")
    if text is None:
        return _marker(name="package.json:quality-scripts", path=path, present=False, kind="package_scripts", details=details)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"package.json: invalid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})")
        return _marker(name="package.json:quality-scripts", path=path, present=False, kind="package_scripts", details=details)

    details["json_valid"] = True
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    matches: list[dict[str, Any]] = []
    if isinstance(scripts, dict):
        for name, command in sorted(scripts.items(), key=lambda item: str(item[0])):
            if not isinstance(command, str):
                continue
            referenced = [script for script in QUALITY_SCRIPT_NAMES if _command_references_script(command, script)]
            if referenced:
                matches.append({"name": str(name), "command": command, "quality_scripts": referenced})
    details["scripts"] = matches
    return _marker(
        name="package.json:quality-scripts",
        path=path,
        present=bool(matches),
        kind="package_scripts",
        details=details,
    )


def _missing_workspace_markers() -> list[dict[str, Any]]:
    markers = [
        _marker(name=".active-book", path=None, present=False, kind="active_book", details={"scope": "workspace"}),
        _marker(name=".story-deployed", path=None, present=False, kind="deployment_marker", details={"scope": "workspace"}),
        _marker(name="skills/story-setup/SKILL.md", path=None, present=False, kind="story_setup_skill", details={"scope": "workspace"}),
        _marker(name=".codex/hooks.json", path=None, present=False, kind="codex_hooks", details={"scope": "workspace"}),
        _marker(name=".codex/hooks/story_codex_hook.py", path=None, present=False, kind="codex_hook_adapter", details={"scope": "workspace"}),
        _marker(name="AGENTS.md:story-routing", path=None, present=False, kind="agents_story_routing", details={"scope": "workspace"}),
        _marker(
            name="story-agents",
            path=None,
            present=False,
            kind="story_agents",
            details={"scope": "workspace", "expected_count": len(STORY_AGENT_NAMES), "present_count": 0, "complete": False},
        ),
    ]
    markers.extend(
        _marker(name=name, path=None, present=False, kind="quality_script", details={"scope": "workspace"})
        for name in QUALITY_SCRIPT_NAMES
    )
    markers.append(
        _marker(name="package.json:quality-scripts", path=None, present=False, kind="package_scripts", details={"scope": "workspace"})
    )
    return markers


def _read_text_if_file(path: Path, *, warnings: list[str], label: str) -> str | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return handle.read(MAX_INSPECTION_CHARS)
    except (OSError, UnicodeError) as exc:
        warnings.append(f"{label}: unreadable text ({exc})")
        return None


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _iter_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_strings(nested)


def _looks_like_javascript(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 16:
        return False
    lowered = stripped.lower()
    return any(
        token in lowered
        for token in ("const ", "let ", "var ", "function ", "require(", "import ", "process.", "console.")
    )


def _command_references_script(command: str, script_name: str) -> bool:
    normalized = command.replace("\\", "/")
    pattern = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(script_name)}(?![A-Za-z0-9_.-])")
    return bool(pattern.search(normalized))


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
    signal_markers = [
        marker
        for marker in markers
        if marker["present"] and marker["kind"] not in {"story_project_core_dir", "active_book"}
    ]
    if not signal_markers:
        return "none"
    signal_kinds = {str(marker["kind"]) for marker in signal_markers}
    if "deployment_marker" in signal_kinds and len(signal_kinds) >= 4:
        return "high"
    if len(signal_kinds) >= 2 or signal_kinds & {"story_setup_skill", "story_agents"}:
        return "medium"
    return "low"


def _core_dirs_present(markers: list[dict[str, Any]]) -> bool:
    core_markers = [marker for marker in markers if marker["kind"] == "story_project_core_dir"]
    return bool(core_markers) and all(marker["present"] for marker in core_markers)


def _active_book_matches(markers: list[dict[str, Any]]) -> bool:
    for marker in markers:
        if marker["kind"] == "active_book":
            return bool(marker["present"] and marker["details"].get("matches_story_project"))
    return False


def _story_agents_complete(markers: list[dict[str, Any]]) -> bool:
    for marker in markers:
        if marker["kind"] == "story_agents":
            return bool(marker["details"].get("complete"))
    return False


def _marker_present(markers: list[dict[str, Any]], kind: str) -> bool:
    return any(marker["kind"] == kind and marker["present"] for marker in markers)


def _named_marker_present(markers: list[dict[str, Any]], kind: str, name: str) -> bool:
    return any(marker["kind"] == kind and marker["name"] == name and marker["present"] for marker in markers)


def _recommendations(confidence: str) -> list[str]:
    if confidence == "none":
        return ["No verified oh-story deployment assets were detected; StoryProject compatible mode remains available."]
    return ["Treat verified oh-story assets as read-only compatibility signals; NovelAgent will not execute them."]


__all__ = [
    "detect_oh_story_compatibility",
    "failed_oh_story_compatibility_report",
]
