from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import PROSE_DIR_NAME, UNTITLED_CHAPTER, canonical_prose_path, resolve_prose
from core.story_project.managed_block import (
    build_managed_projection,
    compute_base_source_digest,
    parse_managed_block,
    parse_manual_tombstones,
    three_way_merge_managed,
    write_managed_block,
)


TRACKING_DIR_NAME = CORE_DIRECTORY_NAMES[3]
TRACKING_TARGETS = {
    "context": "上下文.md",
    "foreshadowing": "伏笔.md",
    "timeline": "时间线.md",
    "character_state": "角色状态.md",
}


@dataclass(frozen=True)
class StoryProjectWritebackConfig:
    mode: str = "none"
    overwrite: bool = False

    @property
    def enabled(self) -> bool:
        return self.mode in {"apply", "dry_run"}

    @property
    def dry_run(self) -> bool:
        return self.mode == "dry_run"


@dataclass
class StoryProjectWriteTarget:
    kind: str
    path: Path
    status: str = "planned"
    action: str = "write"
    reason: str | None = None
    existed: bool = False
    chars_before: int = 0
    chars_after: int = 0
    changed: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "existed": self.existed,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "changed": self.changed,
            "error": self.error,
        }


@dataclass
class StoryProjectWritebackPlan:
    attempted: bool
    dry_run: bool
    overwrite: bool
    story_project_root: Path | None
    chapter_index: int | None
    title: str
    targets: list[StoryProjectWriteTarget] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    story_state_mode: str = "compatible"
    project_identity: dict[str, Any] | None = None
    semantic_state: dict[str, Any] | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reasons or self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "dry_run": self.dry_run,
            "overwrite": self.overwrite,
            "story_project_root": str(self.story_project_root) if self.story_project_root else None,
            "chapter_index": self.chapter_index,
            "title": self.title,
            "targets": [target.to_dict() for target in self.targets],
            "blocked_reasons": list(self.blocked_reasons),
            "errors": list(self.errors),
            "story_state_mode": self.story_state_mode,
        }


@dataclass
class StoryProjectWritebackResult:
    attempted: bool
    applied: bool
    partial: bool
    dry_run: bool
    overwrite: bool
    targets: list[StoryProjectWriteTarget] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    failed_targets: list[str] = field(default_factory=list)
    diff_summary: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    transaction: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "applied": self.applied,
            "partial": self.partial,
            "dry_run": self.dry_run,
            "overwrite": self.overwrite,
            "targets": [target.to_dict() for target in self.targets],
            "blocked_reasons": list(self.blocked_reasons),
            "errors": list(self.errors),
            "failed_targets": list(self.failed_targets),
            "diff_summary": dict(self.diff_summary),
            "artifacts": dict(self.artifacts),
            "transaction": dict(self.transaction),
        }


@dataclass(frozen=True)
class RenderedStoryProjectWriteTarget:
    target_index: int
    kind: str
    path: Path
    content: str
    expected_before_exists: bool
    expected_before_sha256: str | None


def default_story_project_writeback() -> dict[str, Any]:
    return {
        "attempted": False,
        "applied": False,
        "partial": False,
        "dry_run": False,
        "overwrite": False,
        "targets": [],
        "blocked_reasons": ["story_project_writeback_disabled"],
        "errors": [],
        "failed_targets": [],
        "diff_summary": _diff_summary([]),
        "artifacts": {},
        "transaction": {},
    }


def prepare_story_project_writeback(
    *,
    context: dict[str, Any] | None,
    run: dict[str, Any],
    chapter_text: str,
    validation: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
    config: StoryProjectWritebackConfig,
) -> tuple[
    StoryProjectWritebackPlan,
    list[StoryProjectWriteTarget],
    list[RenderedStoryProjectWriteTarget],
    StoryProjectWritebackResult,
]:
    plan = build_story_project_writeback_plan(
        context=context,
        run=run,
        chapter_text=chapter_text,
        validation=validation,
        analysis=analysis,
        config=config,
    )
    targets = [copy.copy(target) for target in plan.targets]
    if plan.blocked:
        for target in targets:
            target.status = "skipped"
            target.reason = target.reason or "preflight_blocked"
        return plan, targets, [], _result(plan, targets, applied=False, partial=False)

    rendered: list[RenderedStoryProjectWriteTarget] = []
    for index, target in enumerate(targets):
        before_exists = target.path.exists()
        before_bytes = target.path.read_bytes() if before_exists else b""
        before = before_bytes.decode("utf-8") if before_exists else ""
        if target.kind == "prose":
            after = chapter_text.rstrip() + "\n"
        else:
            if plan.story_state_mode == "strict":
                after = _managed_tracking_content(
                    before_bytes,
                    target=target,
                    run=run,
                    plan=plan,
                    analysis=analysis or {},
                ).decode("utf-8")
                current = parse_managed_block(before_bytes)
                if current is not None and current.projection["run_id"] == str(run.get("id")):
                    target.existed = before_exists
                    target.chars_before = len(before)
                    target.chars_after = len(before)
                    target.changed = False
                    target.status = "skipped"
                    target.reason = "managed_projection_exists"
                    continue
            else:
                if _has_existing_tracking_marker(
                    target.path,
                    run_id=str(run.get("id")),
                    chapter=plan.chapter_index,
                    kind=target.kind,
                ):
                    target.existed = before_exists
                    target.chars_before = len(before)
                    target.chars_after = len(before)
                    target.changed = False
                    target.status = "skipped"
                    target.reason = "tracking_marker_exists"
                    continue
                content = _tracking_content(target=target, run=run, plan=plan, analysis=analysis or {})
                separator = "\n\n" if before.strip() else ""
                after = before.rstrip() + separator + content.strip() + "\n"
        target.existed = before_exists
        target.chars_before = len(before)
        target.chars_after = len(after)
        target.changed = before != after
        if plan.dry_run:
            target.status = "skipped"
            target.reason = "dry_run"
        else:
            target.status = "planned"
        rendered.append(
            RenderedStoryProjectWriteTarget(
                target_index=index,
                kind=target.kind,
                path=target.path,
                content=after,
                expected_before_exists=before_exists,
                expected_before_sha256=hashlib.sha256(before_bytes).hexdigest() if before_exists else None,
            )
        )
    return plan, targets, rendered, _result(plan, targets, applied=False, partial=False)


def finalize_story_project_writeback(
    plan: StoryProjectWritebackPlan,
    targets: list[StoryProjectWriteTarget],
    persistence: dict[str, Any],
) -> StoryProjectWritebackResult:
    finalized = [copy.copy(target) for target in targets]
    by_index: dict[int, dict[str, Any]] = {}
    for item in persistence.get("targets") or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        index = metadata.get("story_target_index")
        if isinstance(index, int):
            by_index[index] = item

    committed = bool(persistence.get("committed"))
    failed_targets: list[str] = []
    for index, target in enumerate(finalized):
        if target.status == "skipped":
            continue
        persisted = by_index.get(index)
        persisted_status = str((persisted or {}).get("status") or "")
        if committed and persisted_status in {"verified", "completed"}:
            target.status = "updated" if target.existed else "created"
            target.reason = None
            continue
        if persisted_status == "rolled_back":
            target.status = "rolled_back"
            target.reason = "transaction_rolled_back"
            continue
        if persisted_status == "rollback_failed":
            target.status = "rollback_failed"
            target.reason = "transaction_recovery_required"
        else:
            target.status = "failed"
            target.reason = "transaction_failed"
        target.error = str((persisted or {}).get("error") or "persistence transaction did not commit")
        failed_targets.append(str(target.path))

    partial = bool(persistence.get("partial"))
    result = _result(plan, finalized, applied=committed and not failed_targets, partial=partial)
    result.failed_targets.extend(failed_targets)
    result.errors.extend([dict(item) for item in persistence.get("errors") or [] if isinstance(item, dict)])
    result.diff_summary = _diff_summary(finalized)
    result.transaction = {
        key: persistence.get(key)
        for key in ("run_id", "state", "committed", "partial", "journal_path", "commit_marker")
        if key in persistence
    }
    return result


def run_story_project_writeback(
    *,
    context: dict[str, Any] | None,
    run: dict[str, Any],
    chapter_text: str,
    validation: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
    config: StoryProjectWritebackConfig,
) -> tuple[StoryProjectWritebackPlan, StoryProjectWritebackResult]:
    plan = build_story_project_writeback_plan(
        context=context,
        run=run,
        chapter_text=chapter_text,
        validation=validation,
        analysis=analysis,
        config=config,
    )
    result = apply_story_project_writeback_plan(plan, run=run, chapter_text=chapter_text, analysis=analysis or {})
    return plan, result


def build_story_project_writeback_plan(
    *,
    context: dict[str, Any] | None,
    run: dict[str, Any],
    chapter_text: str,
    validation: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
    config: StoryProjectWritebackConfig,
) -> StoryProjectWritebackPlan:
    chapter_index = _chapter_index(context, run)
    title = _resolve_title(context, chapter_text)
    root = _story_project_root(context)
    plan = StoryProjectWritebackPlan(
        attempted=True,
        dry_run=config.dry_run,
        overwrite=bool(config.overwrite),
        story_project_root=root,
        chapter_index=chapter_index,
        title=title,
        story_state_mode=str((context or {}).get("story_state_mode") or "compatible"),
        project_identity=(
            dict((context or {}).get("project_identity"))
            if isinstance((context or {}).get("project_identity"), dict)
            else None
        ),
        semantic_state=(
            copy.deepcopy((context or {}).get("semantic_state"))
            if isinstance((context or {}).get("semantic_state"), dict)
            else None
        ),
    )

    if root is None:
        _block(plan, "story_project_context_missing", "StoryProject context root is missing.")
        return plan
    if chapter_index is None:
        _block(plan, "chapter_index_missing", "StoryProject chapter index is missing.")
        return plan
    if not root.exists() or not root.is_dir():
        _block(plan, "story_project_root_missing", f"StoryProject root is not available: {root}")
        return plan

    prose_dir = root / PROSE_DIR_NAME
    tracking_dir = root / TRACKING_DIR_NAME
    if not prose_dir.is_dir():
        _block(plan, "prose_directory_missing", f"StoryProject prose directory is missing: {prose_dir}")
        return plan
    if tracking_dir.exists() and not tracking_dir.is_dir():
        _block(plan, "tracking_directory_unavailable", f"StoryProject tracking target is not a directory: {tracking_dir}")
        return plan

    _apply_gate(plan, run=run, validation=validation, chapter_text=chapter_text, context=context)
    _plan_prose_target(plan, root=root, chapter_index=chapter_index, title=title, overwrite=config.overwrite)
    _plan_tracking_targets(plan, root=root, tracking_dir=tracking_dir)
    return plan


def apply_story_project_writeback_plan(
    plan: StoryProjectWritebackPlan,
    *,
    run: dict[str, Any],
    chapter_text: str,
    analysis: dict[str, Any],
) -> StoryProjectWritebackResult:
    targets = [copy.copy(target) for target in plan.targets]
    if plan.blocked:
        for target in targets:
            target.status = "skipped"
            target.reason = target.reason or "preflight_blocked"
        return _result(plan, targets, applied=False, partial=False)

    if plan.dry_run:
        for target in targets:
            target.status = "skipped"
            target.reason = "dry_run"
        return _result(plan, targets, applied=False, partial=False)

    errors: list[dict[str, Any]] = []
    failed_targets: list[str] = []
    for target in targets:
        try:
            if target.kind == "prose":
                _write_target(target, chapter_text)
            elif plan.story_state_mode == "strict":
                before_bytes = target.path.read_bytes() if target.path.exists() else b""
                current = parse_managed_block(before_bytes)
                if current is not None and current.projection["run_id"] == str(run.get("id")):
                    before = before_bytes.decode("utf-8")
                    target.existed = target.path.exists()
                    target.chars_before = len(before)
                    target.chars_after = len(before)
                    target.changed = False
                    target.status = "skipped"
                    target.reason = "managed_projection_exists"
                    continue
                after = _managed_tracking_content(
                    before_bytes,
                    target=target,
                    run=run,
                    plan=plan,
                    analysis=analysis,
                ).decode("utf-8")
                _write_exact_target(target, after)
            else:
                content = _tracking_content(target=target, run=run, plan=plan, analysis=analysis)
                if _has_existing_tracking_marker(target.path, run_id=str(run.get("id")), chapter=plan.chapter_index, kind=target.kind):
                    before = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
                    target.existed = target.path.exists()
                    target.chars_before = len(before)
                    target.chars_after = len(before)
                    target.changed = False
                    target.status = "skipped"
                    target.reason = "tracking_marker_exists"
                    continue
                _append_target(target, content)
        except Exception as exc:  # noqa: BLE001 - writeback records target-level failures.
            target.status = "failed"
            target.error = f"{type(exc).__name__}: {exc}"
            errors.append({"target": str(target.path), "kind": target.kind, "error": target.error})
            failed_targets.append(str(target.path))

    succeeded = [target for target in targets if target.status in {"created", "updated", "skipped"}]
    failed = [target for target in targets if target.status == "failed"]
    applied = bool(targets) and not failed and any(target.status in {"created", "updated"} for target in targets)
    partial = bool(succeeded and failed)
    result = _result(plan, targets, applied=applied, partial=partial)
    result.errors.extend(errors)
    result.failed_targets.extend(failed_targets)
    result.diff_summary = _diff_summary(targets)
    if failed:
        result.applied = False
    return result


def _plan_prose_target(
    plan: StoryProjectWritebackPlan,
    *,
    root: Path,
    chapter_index: int,
    title: str,
    overwrite: bool,
) -> None:
    resolution = resolve_prose(root, chapter_index)
    if len(resolution.candidates) > 1:
        _block(plan, "multiple_prose_targets", f"Multiple prose files matched chapter {chapter_index}.")
        return
    if resolution.path is not None and not overwrite:
        plan.targets.append(
            StoryProjectWriteTarget(kind="prose", path=resolution.path, status="skipped", action="write", reason="target_prose_exists", existed=True)
        )
        _block(plan, "target_prose_exists", f"Target prose already exists: {resolution.path}")
        return
    target_path = resolution.path if resolution.path is not None and overwrite else canonical_prose_path(root, chapter_index, title)
    existed = target_path.exists()
    before = _read_text_len(target_path)
    plan.targets.append(
        StoryProjectWriteTarget(
            kind="prose",
            path=target_path,
            action="overwrite" if existed else "create",
            existed=existed,
            chars_before=before,
        )
    )


def _plan_tracking_targets(plan: StoryProjectWritebackPlan, *, root: Path, tracking_dir: Path) -> None:
    for kind, filename in TRACKING_TARGETS.items():
        path = tracking_dir / filename
        existed = path.exists()
        plan.targets.append(
            StoryProjectWriteTarget(
                kind=kind,
                path=path,
                action="managed_merge" if plan.story_state_mode == "strict" else "append",
                existed=existed,
                chars_before=_read_text_len(path),
            )
        )


def _apply_gate(
    plan: StoryProjectWritebackPlan,
    *,
    run: dict[str, Any],
    validation: dict[str, Any] | None,
    chapter_text: str,
    context: dict[str, Any] | None,
) -> None:
    if not context:
        _block(plan, "story_project_context_missing", "StoryProject context is missing.")
    if not run.get("committed"):
        _block(plan, "run_not_committed", "Run is not committed.")
    if not isinstance(validation, dict) or not validation.get("ok"):
        _block(plan, "validation_not_ok", "Validation did not pass.")
    if not chapter_text.strip():
        _block(plan, "chapter_text_empty", "Chapter text is empty.")
    if not isinstance((context or {}).get("chapter_blueprint"), dict):
        _block(plan, "chapter_blueprint_missing", "StoryProject chapter blueprint is missing.")
    if (context or {}).get("story_state_mode") == "strict":
        identity = (context or {}).get("project_identity")
        if not isinstance(identity, dict) or identity.get("story_state_mode") != "strict" or identity.get("ephemeral"):
            _block(plan, "strict_identity_invalid", "Strict writeback requires a stable activated project identity.")
        if not isinstance((context or {}).get("semantic_state"), dict):
            _block(plan, "strict_semantic_state_missing", "Strict writeback requires authoritative semantic state.")
    coverage = _blueprint_coverage(run)
    if not isinstance(coverage, dict):
        _block(plan, "blueprint_coverage_missing", "StoryProject blueprint coverage is missing.")
        return
    if coverage.get("missing_beat_indexes"):
        _block(plan, "missing_required_beat", "StoryProject blueprint required beats are missing.")
    if coverage.get("ending_pressure_required") and not coverage.get("ending_pressure_covered"):
        _block(plan, "missing_ending_pressure", "StoryProject ending pressure is missing.")


def _write_target(target: StoryProjectWriteTarget, content: str) -> None:
    before = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
    _atomic_write_text(target.path, content.rstrip() + "\n")
    target.existed = bool(before)
    target.chars_before = len(before)
    target.chars_after = len(content.rstrip() + "\n")
    target.changed = before != content.rstrip() + "\n"
    target.status = "updated" if before else "created"


def _write_exact_target(target: StoryProjectWriteTarget, content: str) -> None:
    before = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
    _atomic_write_text(target.path, content)
    target.existed = bool(before)
    target.chars_before = len(before)
    target.chars_after = len(content)
    target.changed = before != content
    target.status = "updated" if before else "created"


def _append_target(target: StoryProjectWriteTarget, content: str) -> None:
    before = target.path.read_text(encoding="utf-8") if target.path.exists() else ""
    separator = "\n\n" if before.strip() else ""
    after = before.rstrip() + separator + content.strip() + "\n"
    _atomic_write_text(target.path, after)
    target.existed = bool(before)
    target.chars_before = len(before)
    target.chars_after = len(after)
    target.changed = before != after
    target.status = "updated" if before else "created"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    except Exception:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _tracking_content(
    *,
    target: StoryProjectWriteTarget,
    run: dict[str, Any],
    plan: StoryProjectWritebackPlan,
    analysis: dict[str, Any],
) -> str:
    run_id = str(run.get("id") or "")
    chapter = int(plan.chapter_index or run.get("chapter_index") or 0)
    marker = _tracking_marker(run_id=run_id, chapter=chapter, kind=target.kind)
    title = plan.title or UNTITLED_CHAPTER
    body = _tracking_body(target.kind, analysis)
    coverage = _blueprint_coverage(run) or {}
    return "\n".join(
        [
            marker,
            f"## 第{chapter:03d}章 · {title}",
            "",
            f"- run_id: {run_id}",
            "- source: NovelAgent committed run",
            f"- blueprint_coverage: {_compact_value(coverage) or '未记录'}",
            f"- summary: {str(analysis.get('summary') or '').strip() or '未记录'}",
            *body,
            "<!-- /NovelAgent:story_project_writeback -->",
        ]
    )


def _managed_tracking_content(
    document: bytes,
    *,
    target: StoryProjectWriteTarget,
    run: dict[str, Any],
    plan: StoryProjectWritebackPlan,
    analysis: dict[str, Any],
) -> bytes:
    identity = plan.project_identity or {}
    book_id = str(identity.get("book_id") or "")
    if not book_id:
        raise ValueError("strict managed projection requires book_id")
    current_parsed = parse_managed_block(document)
    current = current_parsed.projection if current_parsed is not None else None
    values = dict(current.get("values") or {}) if current is not None else {}
    values.update(_managed_values(target.kind, int(plan.chapter_index or 0), analysis))
    tombstones = parse_manual_tombstones(document)
    if current is not None:
        existing_tombstones = {
            item["field_path"]: dict(item) for item in current.get("tombstones") or []
        }
        existing_tombstones.update({item["field_path"]: dict(item) for item in tombstones})
        tombstones = [existing_tombstones[key] for key in sorted(existing_tombstones)]
    tombstone_fields = {item["field_path"] for item in tombstones}
    for field_path in tombstone_fields:
        values.pop(field_path, None)
    semantic = plan.semantic_state or {}
    source_digest = str(semantic.get("source_digest") or hashlib.sha256(document).hexdigest())
    parser_version = str(semantic.get("parser_version") or "shadow-1.0")
    proposed = build_managed_projection(
        scope=target.kind,
        book_id=book_id,
        run_id=str(run.get("id") or ""),
        chapter=int(plan.chapter_index or run.get("chapter_index") or 0),
        parser_version=parser_version,
        base_revision=str((current or {}).get("payload_sha256") or source_digest),
        base_source_digest=compute_base_source_digest(document),
        owned_fields=sorted(set(values) | tombstone_fields),
        values=values,
        tombstones=tombstones,
    )
    merged = three_way_merge_managed(
        base=current,
        current=current,
        proposed=proposed,
        manual_tombstones=tombstones,
    )
    if not merged.ok or merged.projection is None:
        raise ValueError("managed_projection_merge_conflict: " + json.dumps(merged.to_dict(), ensure_ascii=False))
    return write_managed_block(document, merged.projection)


def _managed_values(kind: str, chapter: int, analysis: dict[str, Any]) -> dict[str, Any]:
    if kind == "context":
        story_state = analysis.get("story_state")
        if not isinstance(story_state, dict):
            return {}
        return {f"story_state.{key}": copy.deepcopy(value) for key, value in story_state.items()}
    if kind == "character_state":
        values: dict[str, Any] = {}
        for change in analysis.get("character_changes") or []:
            if not isinstance(change, dict) or not str(change.get("name") or "").strip():
                continue
            name = str(change["name"]).strip().replace(".", "_")
            values[f"characters.{name}"] = copy.deepcopy(change)
        spatial = analysis.get("spatial_state")
        if isinstance(spatial, dict):
            for key, value in spatial.items():
                values[f"spatial_state.{key}"] = copy.deepcopy(value)
        return values
    if kind == "timeline":
        return {
            f"timeline.chapter-{chapter:06d}": {
                "id": f"chapter-{chapter:06d}",
                "chapter_index": chapter,
                "summary": str(analysis.get("summary") or ""),
                "events": copy.deepcopy(analysis.get("events") or []),
                "world_changes": copy.deepcopy(analysis.get("world_changes") or []),
            }
        }
    if kind == "foreshadowing":
        entries = analysis.get("foreshadowing")
        if not isinstance(entries, list):
            return {}
        return {
            f"foreshadowing.{item['id']}": copy.deepcopy(item)
            for item in entries
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
    return {}


def _tracking_body(kind: str, analysis: dict[str, Any]) -> list[str]:
    fields = {
        "context": ("events",),
        "foreshadowing": ("conflicts",),
        "timeline": ("events", "world_changes"),
        "character_state": ("character_changes",),
    }.get(kind, ())
    lines: list[str] = []
    for field_name in fields:
        values = analysis.get(field_name)
        if not values:
            lines.append(f"- {field_name}: 未记录")
            continue
        lines.append(f"- {field_name}: {_compact_value(values)}")
    return lines or ["- details: 未记录"]


def _has_existing_tracking_marker(path: Path, *, run_id: str, chapter: int | None, kind: str) -> bool:
    if not path.exists():
        return False
    return _tracking_marker(run_id=run_id, chapter=int(chapter or 0), kind=kind) in path.read_text(encoding="utf-8")


def _tracking_marker(*, run_id: str, chapter: int, kind: str) -> str:
    return f"<!-- NovelAgent:story_project_writeback run_id={run_id} chapter={chapter:03d} target={kind} -->"


def _resolve_title(context: dict[str, Any] | None, chapter_text: str) -> str:
    blueprint = (context or {}).get("chapter_blueprint")
    if isinstance(blueprint, dict) and str(blueprint.get("title") or "").strip():
        return str(blueprint["title"]).strip()
    for line in chapter_text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip() or UNTITLED_CHAPTER
    return UNTITLED_CHAPTER


def _story_project_root(context: dict[str, Any] | None) -> Path | None:
    root = (context or {}).get("story_project_root")
    return Path(root) if root else None


def _chapter_index(context: dict[str, Any] | None, run: dict[str, Any]) -> int | None:
    value = (context or {}).get("chapter_index") or run.get("chapter_index")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _blueprint_coverage(run: dict[str, Any]) -> dict[str, Any] | None:
    story_project = run.get("story_project") if isinstance(run.get("story_project"), dict) else {}
    coverage = story_project.get("blueprint_coverage") if isinstance(story_project, dict) else None
    if isinstance(coverage, dict):
        return coverage
    chapter = run.get("chapter") if isinstance(run.get("chapter"), dict) else {}
    pipeline = chapter.get("pipeline") if isinstance(chapter.get("pipeline"), dict) else {}
    coverage = pipeline.get("blueprint_coverage") if isinstance(pipeline, dict) else None
    return coverage if isinstance(coverage, dict) else None


def _read_text_len(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8"))


def _block(plan: StoryProjectWritebackPlan, reason: str, message: str) -> None:
    if reason not in plan.blocked_reasons:
        plan.blocked_reasons.append(reason)
    plan.errors.append({"reason": reason, "error": message})


def _result(
    plan: StoryProjectWritebackPlan,
    targets: list[StoryProjectWriteTarget],
    *,
    applied: bool,
    partial: bool,
) -> StoryProjectWritebackResult:
    return StoryProjectWritebackResult(
        attempted=True,
        applied=applied,
        partial=partial,
        dry_run=plan.dry_run,
        overwrite=plan.overwrite,
        targets=targets,
        blocked_reasons=list(plan.blocked_reasons),
        errors=list(plan.errors),
        failed_targets=[],
        diff_summary=_diff_summary(targets),
    )


def _diff_summary(targets: list[StoryProjectWriteTarget]) -> dict[str, Any]:
    counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "planned": 0}
    chars_before = 0
    chars_after = 0
    changed = 0
    for target in targets:
        counts[target.status] = counts.get(target.status, 0) + 1
        chars_before += int(target.chars_before or 0)
        chars_after += int(target.chars_after or 0)
        if target.changed:
            changed += 1
    return {
        "status_counts": counts,
        "target_count": len(targets),
        "changed_targets": changed,
        "chars_before": chars_before,
        "chars_after": chars_after,
        "chars_delta": chars_after - chars_before,
    }


def _compact_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value[:5]:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("summary") or item))
            else:
                parts.append(str(item))
        return "; ".join(part.strip() for part in parts if part.strip())
    if isinstance(value, dict):
        return ", ".join(f"{key}={value[key]}" for key in sorted(value) if value[key] not in (None, "", []))
    return str(value)


__all__ = [
    "StoryProjectWriteTarget",
    "StoryProjectWritebackConfig",
    "StoryProjectWritebackPlan",
    "StoryProjectWritebackResult",
    "build_story_project_writeback_plan",
    "apply_story_project_writeback_plan",
    "default_story_project_writeback",
    "run_story_project_writeback",
]
