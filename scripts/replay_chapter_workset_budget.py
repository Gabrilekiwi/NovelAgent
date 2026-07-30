from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep the production import order used by the chapter workflow.
from modules.chapter_generator import pipeline as chapter_pipeline  # noqa: E402

from core.context_budget import (  # noqa: E402
    ContextBudget,
    ContextBudgetError,
    default_context_budget,
)
from core.engine.run_record import validate_run_result  # noqa: E402
from core.engine.story_project_context import StoryProjectContextService  # noqa: E402
from core.prompt_compiler import compile_prompt_contexts  # noqa: E402
from core.state.authoritative import (  # noqa: E402
    seed_authoritative_state_from_snapshot,
    validate_authoritative_state,
)
from core.state.authoritative_context import (  # noqa: E402
    authoritative_state_from_markdown,
)
from core.state.builder import build_snapshot_state_with_audit  # noqa: E402
from core.state.chapter_context_authority_migration import (  # noqa: E402
    _build_candidate,
    run_chapter_context_authority_migration,
)
from core.state.generation_state_view import (  # noqa: E402
    apply_generation_state_view_to_snapshot,
    build_generation_state_view,
    generation_state_view_from_markdown,
)
from core.state.input_pack import (  # noqa: E402
    build_input_pack,
    build_input_pack_metadata,
)
from core.state.snapshot import normalize_snapshot  # noqa: E402
from core.story_project.identity import load_project_identity  # noqa: E402
from core.story_project.runtime import (  # noqa: E402
    build_generation_story_project_context,
)
from modules.scene_repair.plan import build_repair_plan  # noqa: E402
from modules.scene_repair.repairer import (  # noqa: E402
    RepairContext,
    build_repair_messages,
)


REPLAY_SCHEMA_VERSION = "1.0"
DEFAULT_SAFE_TARGET = 30_000
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_ENDPOINT_TYPE = "openai_compatible"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WRAPPER_SEPARATOR = "\n---\n\n"


class ReplayChapterWorksetBudgetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class _RecordingBudget:
    """Delegate production admission and retain the last measured payload."""

    def __init__(self, budget: ContextBudget) -> None:
        self.budget = budget
        self.last_text: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.budget, name)

    def measure(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.last_text = str(text)
        return self.budget.measure(text, **kwargs)

    def require_input(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.last_text = str(text)
        return self.budget.require_input(text, **kwargs)


class _ReadOnlyGuard:
    """Bind critical source bytes and fail if a supposedly offline replay writes."""

    def __init__(self, paths: list[Path]) -> None:
        self._bindings = {
            path.resolve(strict=True): _sha256_bytes(path.read_bytes())
            for path in paths
        }

    def verify(self) -> dict[str, Any]:
        checked: list[dict[str, Any]] = []
        for path, expected in sorted(
            self._bindings.items(),
            key=lambda item: str(item[0]),
        ):
            if not path.is_file():
                _fail(
                    "read_only_source_removed",
                    f"Offline replay source disappeared: {path}",
                )
            actual = _sha256_bytes(path.read_bytes())
            if actual != expected:
                _fail(
                    "read_only_source_changed",
                    f"Offline replay changed source bytes: {path}",
                )
            checked.append(
                {
                    "path": str(path),
                    "sha256_before": expected,
                    "sha256_after": actual,
                    "unchanged": True,
                }
            )
        return {
            "writes_performed": False,
            "critical_sources_unchanged": True,
            "bindings": checked,
        }


def replay_chapter_workset_budget(
    *,
    story_project: str | Path,
    chapter_index: int,
    run_json: str | Path,
    manifest: str | Path | None = None,
    safe_target: int = DEFAULT_SAFE_TARGET,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
    endpoint_type: str = DEFAULT_ENDPOINT_TYPE,
) -> dict[str, Any]:
    """Replay current Chapter work-set requests without any model call or write."""

    root = Path(story_project).expanduser().resolve(strict=True)
    if not root.is_dir():
        _fail("story_project_invalid", f"StoryProject is not a directory: {root}")
    chapter = _positive_integer(chapter_index, "chapter_index")
    target = _positive_integer(safe_target, "safe_target")
    runtime_root = (root / ".novelagent" / "runtime").resolve(strict=True)
    snapshot_path = (runtime_root / "snapshot.json").resolve(strict=True)
    run_path = Path(run_json).expanduser().resolve(strict=True)
    _require_contained(
        run_path,
        runtime_root / "runs",
        label="run_json",
    )
    manifest_path = (
        Path(manifest).expanduser().resolve(strict=True)
        if manifest is not None
        else None
    )

    guarded_paths = [snapshot_path, run_path]
    if manifest_path is not None:
        guarded_paths.append(manifest_path)
    guard = _ReadOnlyGuard(guarded_paths)

    run_payload, run_binding = _load_run_result(run_path)
    run = run_payload["run"]
    if int(run.get("chapter_index") or 0) != chapter:
        _fail(
            "run_chapter_mismatch",
            "Selected run does not belong to the requested chapter.",
        )
    run_id = _required_text(run.get("id"), "run.id")

    snapshot_bytes = snapshot_path.read_bytes()
    snapshot = _json_object(snapshot_bytes, label="snapshot")
    snapshot = normalize_snapshot(snapshot)
    if int(snapshot.get("chapter_index") or 0) != chapter:
        _fail(
            "snapshot_chapter_mismatch",
            (
                f"Current snapshot chapter_index={snapshot.get('chapter_index')!r} "
                f"does not match requested chapter {chapter}."
            ),
        )

    project_identity = load_project_identity(root)
    if project_identity is None or project_identity.ephemeral:
        _fail(
            "stable_project_identity_required",
            "Offline replay requires a stable StoryProject identity.",
        )
    if snapshot.get("book_id") != project_identity.book_id:
        _fail(
            "snapshot_book_id_mismatch",
            "Snapshot book_id differs from ProjectIdentity.",
        )
    run_story_project = run.get("story_project")
    run_book_id = (
        run_story_project.get("book_id")
        if isinstance(run_story_project, dict)
        else None
    )
    if run_book_id != project_identity.book_id:
        _fail(
            "run_book_id_mismatch",
            "Selected run belongs to a different StoryProject book_id.",
        )

    candidate_snapshot, authority_source = _candidate_snapshot(
        root=root,
        snapshot_path=snapshot_path,
        snapshot_bytes=snapshot_bytes,
        manifest_path=manifest_path,
    )
    validate_authoritative_state(
        seed_authoritative_state_from_snapshot(candidate_snapshot)
    )

    artifact_bundle = _load_failed_run_artifacts(
        root=root,
        runtime_root=runtime_root,
        run=run,
        run_id=run_id,
        chapter_index=chapter,
    )

    memory_context = _replay_memory_context(run)
    context = build_generation_story_project_context(
        story_project=root,
        chapter=chapter,
        snapshot=candidate_snapshot,
        memory_context=memory_context,
        project_identity=project_identity,
    )
    context_dict = context.to_dict()
    service = StoryProjectContextService()
    context_snapshot, memory_context = service.apply_context(
        context_dict,
        candidate_snapshot,
        memory_context,
        snapshot_path=snapshot_path,
        allow_legacy_snapshot_adoption=False,
    )
    state_result = build_snapshot_state_with_audit(
        context_snapshot,
        memory_context,
    )
    local_snapshot = service.apply_authority(
        context_dict,
        state_result["snapshot"],
    )

    blueprint = context_dict.get("chapter_blueprint")
    if not isinstance(blueprint, dict):
        _fail(
            "chapter_blueprint_missing",
            "Current StoryProject context has no chapter blueprint.",
        )
    read_set = blueprint.get("chapter_context_read_set")
    if not isinstance(read_set, dict) or read_set.get("mode") != "explicit":
        _fail(
            "chapter_read_set_missing",
            "Current chapter outline has no explicit ChapterContextReadSet.",
        )
    if int(read_set.get("chapter_index") or 0) != chapter:
        _fail(
            "chapter_read_set_mismatch",
            "ChapterContextReadSet belongs to another chapter.",
        )

    authority = seed_authoritative_state_from_snapshot(local_snapshot)
    generation_state_view = build_generation_state_view(authority, read_set)
    _assert_read_set_binding(generation_state_view, read_set)
    local_snapshot = apply_generation_state_view_to_snapshot(
        local_snapshot,
        generation_state_view,
    )

    decision = run.get("decision")
    if not isinstance(decision, dict):
        _fail("run_decision_missing", "Selected run has no Director decision.")
    input_pack = build_input_pack(
        local_snapshot,
        decision,
        memory_context,
        story_project_context=context_dict,
        generation_state_view=generation_state_view,
    )
    input_pack_metadata = build_input_pack_metadata(
        input_pack,
        local_snapshot,
        decision,
        memory_context,
        story_project_context=context_dict,
        generation_state_view=generation_state_view,
    )
    parsed_view = generation_state_view_from_markdown(input_pack)
    if parsed_view != generation_state_view:
        _fail(
            "input_pack_generation_view_mismatch",
            "Input pack does not contain the exact constructed GenerationStateView.",
        )

    budget = default_context_budget(
        provider=provider,
        model=model,
        endpoint_type=endpoint_type,
        enable_model_tokenizer=False,
    )
    prompt_contexts = compile_prompt_contexts(
        input_pack,
        budget=budget,
    )
    plan = chapter_pipeline.plan_scenes(
        prompt_contexts.plan.text,
        chapter_index=chapter,
        dry_run=True,
        chapter_blueprint=blueprint,
    )
    _bind_current_plan_to_historical_evidence(
        plan,
        artifact_bundle["persisted_plan"],
        artifact_bundle["historical_scenes"],
    )

    plan_measurement = _measure_plan_context(
        prompt_contexts.plan.text,
        report=prompt_contexts.plan.report,
        safe_target=target,
    )
    scene_measurements = _measure_scene_requests(
        budget=budget,
        input_pack=prompt_contexts.scene.text,
        plan=plan,
        blueprint=blueprint,
        historical_scenes=artifact_bundle["historical_scenes"],
        authoritative_state_source=authoritative_state_from_markdown(
            input_pack
        ),
        generation_state_view_source=generation_state_view,
        safe_target=target,
    )
    repair_measurement = _measure_repair_request(
        budget=budget,
        input_pack=input_pack,
        chapter_text=artifact_bundle["chapter_text"],
        validation=artifact_bundle["validation"],
        repair_plan=artifact_bundle["repair_plan"],
        recovery_context=artifact_bundle["recovery_context"],
        language=str(
            (input_pack_metadata.get("snapshot") or {})
            .get("project_profile", {})
            .get("language")
            or "zh-CN"
        ),
        safe_target=target,
    )

    all_paths = [
        plan_measurement,
        *scene_measurements,
        repair_measurement,
    ]
    maximum = max(
        all_paths,
        key=lambda item: int(item["budgeted_input_tokens"]),
    )
    safe = all(bool(item["below_safe_target"]) for item in all_paths)
    read_only = guard.verify()

    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "kind": "chapter_workset_budget_replay",
        "story_project": {
            "root": str(root),
            "book_id": project_identity.book_id,
            "chapter_index": chapter,
        },
        "source_run": {
            "id": run_id,
            "status": run.get("status"),
            "path": str(run_path),
            "sha256": run_binding["sha256"],
            "historical_error": copy.deepcopy(run.get("error")),
            "historical_input_pack": artifact_bundle[
                "historical_input_pack"
            ],
        },
        "authority_source": authority_source,
        "chapter_context_read_set": {
            "contract_sha256": read_set["contract_sha256"],
            "source_outline_sha256": read_set["source_outline_sha256"],
            "required_state_item_count": len(
                read_set["required_state_item_ids"]
            ),
            "required_event_item_count": len(
                read_set["required_event_item_ids"]
            ),
            "all_ids_resolved": True,
        },
        "generation_state_view": {
            "projection_sha256": generation_state_view["projection_sha256"],
            "source_authority_sha256": generation_state_view[
                "source_authority_sha256"
            ],
            "read_set_digest": generation_state_view["read_set_digest"],
            "selected_item_ids_sha256": generation_state_view[
                "selected_item_ids_sha256"
            ],
            "selected_state_item_count": len(
                generation_state_view["selected_state_item_ids"]
            ),
            "selected_event_item_count": len(
                generation_state_view["selected_event_item_ids"]
            ),
            "rendered_chars": len(
                json.dumps(
                    generation_state_view,
                    ensure_ascii=False,
                    indent=2,
                )
            ),
        },
        "artifacts": artifact_bundle["bindings"],
        "measurements": {
            "plan": plan_measurement,
            "scenes": scene_measurements,
            "repair": repair_measurement,
        },
        "read_only": read_only,
        "model_calls_performed": 0,
        "result": {
            "safe": safe,
            "safe_target": target,
            "comparison": "strictly_less_than",
            "max_stage": maximum["stage"],
            "max_budgeted_input_tokens": maximum[
                "budgeted_input_tokens"
            ],
            "minimum_safe_target_headroom": min(
                int(item["safe_target_headroom"]) for item in all_paths
            ),
            "reason": (
                "all_production_requests_below_safe_target"
                if safe
                else "one_or_more_production_requests_reach_safe_target"
            ),
        },
    }


def _candidate_snapshot(
    *,
    root: Path,
    snapshot_path: Path,
    snapshot_bytes: bytes,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if manifest_path is None:
        snapshot = normalize_snapshot(
            _json_object(snapshot_bytes, label="snapshot")
        )
        authority = seed_authoritative_state_from_snapshot(snapshot)
        return snapshot, {
            "mode": "current_snapshot",
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": _sha256_bytes(snapshot_bytes),
            "authoritative_state_sha256": _canonical_sha256(authority),
            "manifest": None,
            "writes_performed": False,
        }

    preview = run_chapter_context_authority_migration(
        story_project=root,
        manifest_path=manifest_path,
        apply=False,
    )
    if preview.get("writes_performed") is not False:
        _fail(
            "migration_preview_wrote_state",
            "Migration preview unexpectedly reported a write.",
        )
    manifest_bytes = manifest_path.read_bytes()
    manifest = _json_object(manifest_bytes, label="migration manifest")
    candidate = _build_candidate(
        snapshot_bytes=snapshot_bytes,
        manifest=manifest,
    )
    if (
        candidate["after_sha256"]
        != (preview.get("snapshot") or {}).get("after_sha256")
    ):
        _fail(
            "migration_overlay_digest_mismatch",
            "Read-only candidate differs from the audited migration preview.",
        )
    snapshot = normalize_snapshot(candidate["after_snapshot"])
    return snapshot, {
        "mode": "read_only_manifest_overlay",
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256_before": _sha256_bytes(snapshot_bytes),
        "candidate_snapshot_sha256": candidate["after_sha256"],
        "authoritative_state_sha256": _canonical_sha256(
            seed_authoritative_state_from_snapshot(snapshot)
        ),
        "manifest": {
            "path": str(manifest_path),
            "sha256": _sha256_bytes(manifest_bytes),
            "migration_id": preview["migration_id"],
            "preview_status": preview["status"],
        },
        "upserts": copy.deepcopy(preview["upserts"]),
        "writes_performed": False,
    }


def _load_run_result(
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = path.read_bytes()
    value = _json_object(raw, label="run JSON")
    try:
        checked = validate_run_result(value)
    except (TypeError, ValueError) as exc:
        _fail("run_schema_invalid", str(exc))
    return checked, {
        "path": str(path),
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
        "schema_valid": True,
    }


def _load_failed_run_artifacts(
    *,
    root: Path,
    runtime_root: Path,
    run: dict[str, Any],
    run_id: str,
    chapter_index: int,
) -> dict[str, Any]:
    chapter = run.get("chapter")
    pipeline = chapter.get("pipeline") if isinstance(chapter, dict) else None
    artifacts = (
        pipeline.get("artifacts") if isinstance(pipeline, dict) else None
    )
    if not isinstance(artifacts, dict):
        _fail(
            "pipeline_artifacts_missing",
            "Selected run has no persisted chapter-pipeline artifacts.",
        )

    validation, validation_binding = _load_json_artifact(
        artifacts.get("validation_report"),
        runtime_root=runtime_root,
        label="validation_report",
    )
    if not isinstance(validation.get("problems"), list) or not validation[
        "problems"
    ]:
        _fail(
            "validation_problems_missing",
            "Persisted validation report has no repairable problems.",
        )

    persisted_plan, plan_binding = _load_json_artifact(
        artifacts.get("plan"),
        runtime_root=runtime_root,
        label="chapter_plan",
    )
    persisted_plan = chapter_pipeline._validate_plan(
        persisted_plan,
        chapter_index=chapter_index,
    )

    continuity, continuity_binding = _load_json_artifact(
        artifacts.get("scene_continuity"),
        runtime_root=runtime_root,
        label="scene_continuity",
    )
    structured_scenes = continuity.get("scenes")
    if not isinstance(structured_scenes, list) or not structured_scenes:
        _fail(
            "scene_continuity_missing",
            "Persisted scene continuity has no scenes.",
        )

    chapter_text, merged_binding = _load_wrapped_markdown_artifact(
        artifacts.get("merged_chapter"),
        runtime_root=runtime_root,
        label="merged_chapter",
        required_header=f"# Merged Chapter {chapter_index}",
        run_id=run_id,
    )

    scene_artifacts = artifacts.get("scene_drafts")
    if not isinstance(scene_artifacts, list) or not scene_artifacts:
        _fail(
            "scene_artifacts_missing",
            "Persisted scene draft artifact list is missing.",
        )
    if len(scene_artifacts) != len(structured_scenes):
        _fail(
            "scene_artifact_count_mismatch",
            "Scene prose and continuity artifact counts differ.",
        )

    historical_scenes: list[dict[str, Any]] = []
    scene_bindings: list[dict[str, Any]] = []
    for position, (metadata, structured) in enumerate(
        zip(scene_artifacts, structured_scenes, strict=True),
        start=1,
    ):
        if not isinstance(structured, dict):
            _fail(
                "scene_continuity_invalid",
                f"scene_continuity.scenes[{position - 1}] is not an object.",
            )
        index = int(structured.get("index") or 0)
        if index != position:
            _fail(
                "scene_index_invalid",
                "Persisted scene indexes are not contiguous.",
            )
        prose, binding = _load_wrapped_markdown_artifact(
            metadata,
            runtime_root=runtime_root,
            label=f"scene_{index}",
            required_header=f"# Scene {index}",
            run_id=run_id,
        )
        historical_scenes.append(
            {
                "index": index,
                "text": prose,
                "events": copy.deepcopy(structured.get("events") or []),
                "scene_state_before": copy.deepcopy(
                    structured.get("scene_state_before") or {}
                ),
                "scene_state_after": copy.deepcopy(
                    structured.get("scene_state_after") or {}
                ),
            }
        )
        scene_bindings.append(binding)

    repair_plan = None
    for trace in reversed(run.get("trace") or []):
        if (
            isinstance(trace, dict)
            and trace.get("action") == "repair_if_needed"
            and isinstance(trace.get("repair_plan"), dict)
        ):
            repair_plan = copy.deepcopy(trace["repair_plan"])
            break
    if repair_plan is None:
        repair_plan = build_repair_plan(validation)

    recovery_context = run.get("recovery_context")
    if not isinstance(recovery_context, dict):
        recovery_context = {"available": False}

    historical_input, historical_input_binding = _load_input_pack_artifact(
        run,
        runtime_root=runtime_root,
        run_id=run_id,
        chapter_index=chapter_index,
    )
    if not historical_input:
        _fail(
            "historical_input_pack_empty",
            "Persisted failed input pack is empty.",
        )

    return {
        "validation": validation,
        "persisted_plan": persisted_plan,
        "historical_scenes": historical_scenes,
        "chapter_text": chapter_text,
        "repair_plan": repair_plan,
        "recovery_context": copy.deepcopy(recovery_context),
        "historical_input_pack": {
            "chars": len(historical_input),
            "sha256": _sha256_text(historical_input),
            "artifact_sha256": historical_input_binding["sha256"],
            "recorded_failure_budgeted_input_tokens": _recorded_failure_tokens(
                run
            ),
        },
        "bindings": {
            "validation_report": validation_binding,
            "persisted_plan": plan_binding,
            "scene_continuity": continuity_binding,
            "merged_chapter": merged_binding,
            "scene_drafts": scene_bindings,
            "historical_input_pack": historical_input_binding,
            "all_recorded_sha256_verified": True,
        },
    }


def _load_json_artifact(
    metadata: Any,
    *,
    runtime_root: Path,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bound = _load_bound_artifact(
        metadata,
        runtime_root=runtime_root,
        label=label,
    )
    value = _json_object(bound["bytes_value"], label=label)
    return value, _public_artifact_binding(bound)


def _load_wrapped_markdown_artifact(
    metadata: Any,
    *,
    runtime_root: Path,
    label: str,
    required_header: str,
    run_id: str,
) -> tuple[str, dict[str, Any]]:
    bound = _load_bound_artifact(
        metadata,
        runtime_root=runtime_root,
        label=label,
    )
    text = bound["text"]
    if _WRAPPER_SEPARATOR not in text:
        _fail(
            "artifact_wrapper_invalid",
            f"{label} wrapper separator is missing.",
        )
    header, logical_with_newline = text.split(_WRAPPER_SEPARATOR, 1)
    if required_header not in header.splitlines():
        _fail(
            "artifact_wrapper_invalid",
            f"{label} wrapper header does not match.",
        )
    if f"- Run: `{run_id}`" not in header.splitlines():
        _fail(
            "artifact_wrapper_invalid",
            f"{label} wrapper run id does not match.",
        )
    if not logical_with_newline.endswith("\n"):
        _fail(
            "artifact_wrapper_invalid",
            f"{label} wrapper has no formatter-owned terminal newline.",
        )
    return logical_with_newline[:-1], _public_artifact_binding(bound)


def _load_input_pack_artifact(
    run: dict[str, Any],
    *,
    runtime_root: Path,
    run_id: str,
    chapter_index: int,
) -> tuple[str, dict[str, Any]]:
    summary = run.get("input_pack")
    metadata = summary.get("artifact") if isinstance(summary, dict) else None
    bound = _load_bound_artifact(
        metadata,
        runtime_root=runtime_root,
        label="historical_input_pack",
        verify_recorded_chars=False,
    )
    text = bound["text"]
    if _WRAPPER_SEPARATOR not in text:
        _fail(
            "artifact_wrapper_invalid",
            "Historical input-pack wrapper separator is missing.",
        )
    header, logical_with_newline = text.split(_WRAPPER_SEPARATOR, 1)
    required = {
        f"# Input Pack: Chapter {chapter_index}",
        f"- Run: `{run_id}`",
    }
    if not required.issubset(set(header.splitlines())):
        _fail(
            "artifact_wrapper_invalid",
            "Historical input-pack wrapper identity does not match.",
        )
    if not logical_with_newline.endswith("\n"):
        _fail(
            "artifact_wrapper_invalid",
            "Historical input-pack terminal newline is missing.",
        )
    logical = logical_with_newline[:-1]
    for field, value in (
        ("run.input_pack.chars", summary.get("chars")),
        ("run.input_pack.artifact.chars", metadata.get("chars")),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != len(logical)
        ):
            _fail(
                "artifact_chars_mismatch",
                f"{field} does not match extracted logical input-pack chars.",
            )
    public = _public_artifact_binding(bound)
    public["logical_chars"] = len(logical)
    return logical, public


def _load_bound_artifact(
    metadata: Any,
    *,
    runtime_root: Path,
    label: str,
    verify_recorded_chars: bool = True,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        _fail("artifact_metadata_missing", f"{label} metadata is missing.")
    recorded_path = _required_text(metadata.get("path"), f"{label}.path")
    path = Path(recorded_path).expanduser().resolve(strict=True)
    _require_contained(path, runtime_root, label=f"{label}.path")
    raw = path.read_bytes()
    expected = _required_sha256(metadata.get("sha256"), f"{label}.sha256")
    actual = _sha256_bytes(raw)
    if actual != expected:
        _fail(
            "artifact_sha256_mismatch",
            f"{label} SHA-256 differs from the run record.",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        _fail("artifact_encoding_invalid", f"{label} is not UTF-8: {exc}")
    normalized = _normalize_newlines(text)
    if verify_recorded_chars:
        chars = metadata.get("chars")
        if (
            isinstance(chars, bool)
            or not isinstance(chars, int)
            or chars != len(normalized)
        ):
            _fail(
                "artifact_chars_mismatch",
                f"{label} normalized chars differ from the run record.",
            )
    return {
        "path": path,
        "bytes_value": raw,
        "text": normalized,
        "sha256": actual,
        "bytes": len(raw),
        "normalized_chars": len(normalized),
    }


def _public_artifact_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": str(value["path"]),
        "sha256": value["sha256"],
        "bytes": int(value["bytes"]),
        "normalized_chars": int(value["normalized_chars"]),
        "sha256_verified": True,
    }


def _measure_plan_context(
    text: str,
    *,
    report: Mapping[str, Any],
    safe_target: int,
) -> dict[str, Any]:
    return _measurement(
        stage="plan",
        report=report,
        payload=text,
        safe_target=safe_target,
        extra={
            "provider_request_required": False,
            "request_kind": (
                "deterministic_blueprint_plan_compiled_context"
            ),
        },
    )


def _measure_scene_requests(
    *,
    budget: ContextBudget,
    input_pack: str,
    plan: dict[str, Any],
    blueprint: dict[str, Any],
    historical_scenes: list[dict[str, Any]],
    authoritative_state_source: dict[str, Any] | None,
    generation_state_view_source: dict[str, Any],
    safe_target: int,
) -> list[dict[str, Any]]:
    by_index = {int(item["index"]): item for item in historical_scenes}
    measurements: list[dict[str, Any]] = []
    prior_summaries: list[dict[str, Any]] = []
    previous_tail = ""
    for scene in plan.get("scenes") or []:
        index = int(scene["index"])
        historical = by_index[index]
        state_before = copy.deepcopy(historical["scene_state_before"])
        required_indexes = set(chapter_pipeline._scene_beat_indexes(scene))
        scene_beats = [
            beat
            for beat in blueprint.get("required_beats") or []
            if isinstance(beat, dict)
            and int(beat.get("index") or 0) in required_indexes
        ]
        recorder = _RecordingBudget(budget)
        construction_error: dict[str, Any] | None = None
        try:
            with patch.object(
                chapter_pipeline,
                "default_context_budget",
                return_value=recorder,
            ):
                payload = chapter_pipeline._scene_request_payload(
                    input_pack=input_pack,
                    plan=plan,
                    scene=scene,
                    scene_required_beats=scene_beats,
                    blueprint=blueprint,
                    previous_scene_tail=previous_tail,
                    prior_scene_summaries=prior_summaries,
                    scene_state=state_before,
                    authoritative_state_source=authoritative_state_source,
                    generation_state_view_source=(
                        generation_state_view_source
                    ),
                )
        except ContextBudgetError as exc:
            payload = recorder.last_text
            construction_error = {
                "code": str(exc.code),
                "message": str(exc),
            }
        if payload is None:
            _fail(
                "scene_payload_missing",
                f"Production Scene {index} builder produced no payload.",
            )
        protocol = chapter_pipeline._load_scene_prompt()
        report = budget.measure(
            payload,
            stage="scene",
            protocol_texts=(protocol,),
        )
        measurements.append(
            _measurement(
                stage=f"scene_{index}",
                report=report,
                payload=payload,
                safe_target=safe_target,
                extra={
                    "scene_index": index,
                    "request_kind": "production_scene_request",
                    "construction_error": construction_error,
                    "continuity_source": (
                        "hash_verified_failed_run_scene_artifacts"
                    ),
                },
            )
        )
        previous_tail = str(historical["text"])[-600:]
        prior_summaries.append(
            {
                "index": index,
                "goal": str(scene.get("goal") or ""),
                "tail": str(historical["text"])[-280:],
                "event_ids": [
                    str(event.get("event_id") or "")
                    for event in historical.get("events") or []
                    if isinstance(event, dict)
                    and str(event.get("event_id") or "")
                ],
            }
        )
    return measurements


def _measure_repair_request(
    *,
    budget: ContextBudget,
    input_pack: str,
    chapter_text: str,
    validation: dict[str, Any],
    repair_plan: dict[str, Any],
    recovery_context: dict[str, Any],
    language: str,
    safe_target: int,
) -> dict[str, Any]:
    messages = build_repair_messages(
        chapter_text,
        validation,
        input_pack,
        repair_plan,
        recovery_context,
        RepairContext(language=language),
    )
    if len(messages) != 2:
        _fail(
            "repair_message_contract_invalid",
            "Production RepairEnvelope path did not produce two messages.",
        )
    protocol = messages[0]["content"]
    payload = messages[1]["content"]
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as exc:
        _fail(
            "repair_envelope_transport_invalid",
            f"Repair user message is not JSON: {exc}",
        )
    if (
        not isinstance(envelope, dict)
        or envelope.get("envelope_kind") != "repair_envelope"
    ):
        _fail(
            "repair_envelope_transport_invalid",
            "Repair user message is not a RepairEnvelope.",
        )
    if envelope.get("chapter") != chapter_text:
        _fail(
            "repair_chapter_binding_mismatch",
            "RepairEnvelope does not contain the exact failed chapter.",
        )
    report = budget.measure(
        payload,
        stage="repair",
        protocol_texts=(protocol,),
    )
    return _measurement(
        stage="repair",
        report=report,
        payload=payload,
        safe_target=safe_target,
        extra={
            "request_kind": "production_repair_envelope_request",
            "problem_count": len(envelope.get("problems") or []),
            "envelope_sha256": envelope.get("envelope_sha256"),
            "base_chapter_sha256": envelope.get("base_chapter_sha256"),
            "generation_state_view_sha256": envelope.get(
                "generation_state_view_sha256"
            ),
            "single_full_chapter": True,
        },
    )


def _measurement(
    *,
    stage: str,
    report: Mapping[str, Any],
    payload: str,
    safe_target: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tokens = int(report["budgeted_input_tokens"])
    return {
        "stage": stage,
        "payload_chars": len(payload),
        "payload_utf8_bytes": len(payload.encode("utf-8")),
        "payload_sha256": _sha256_text(payload),
        "raw_input_tokens": int(report["raw_input_tokens"]),
        "budgeted_input_tokens": tokens,
        "count_mode": report["count_mode"],
        "counter_version": report["counter_version"],
        "hard_input_limit": int(report["hard_input_limit"]),
        "within_hard_limit": bool(report["within_budget"]),
        "safe_target": safe_target,
        "below_safe_target": tokens < safe_target,
        "safe_target_headroom": safe_target - tokens,
        **dict(extra or {}),
    }


def _assert_read_set_binding(
    view: Mapping[str, Any],
    read_set: Mapping[str, Any],
) -> None:
    if view.get("read_set_digest") != read_set.get("contract_sha256"):
        _fail(
            "read_set_digest_mismatch",
            "GenerationStateView is not bound to the current read set.",
        )
    if list(view.get("selected_state_item_ids") or []) != list(
        read_set.get("required_state_item_ids") or []
    ):
        _fail(
            "read_set_state_ids_mismatch",
            "GenerationStateView did not resolve the exact required state IDs.",
        )
    if list(view.get("selected_event_item_ids") or []) != list(
        read_set.get("required_event_item_ids") or []
    ):
        _fail(
            "read_set_event_ids_mismatch",
            "GenerationStateView did not resolve the exact required event IDs.",
        )


def _bind_current_plan_to_historical_evidence(
    current_plan: Mapping[str, Any],
    persisted_plan: Mapping[str, Any],
    historical_scenes: list[dict[str, Any]],
) -> None:
    current_indexes = [
        int(item.get("index") or 0)
        for item in current_plan.get("scenes") or []
        if isinstance(item, dict)
    ]
    persisted_indexes = [
        int(item.get("index") or 0)
        for item in persisted_plan.get("scenes") or []
        if isinstance(item, dict)
    ]
    historical_indexes = [int(item["index"]) for item in historical_scenes]
    if (
        not current_indexes
        or current_indexes != persisted_indexes
        or current_indexes != historical_indexes
    ):
        _fail(
            "scene_scope_evidence_mismatch",
            (
                "Current blueprint Scene scopes do not align one-to-one with "
                "the hash-verified failed-run evidence."
            ),
        )


def _replay_memory_context(run: Mapping[str, Any]) -> dict[str, Any]:
    validation = run.get("validation")
    decision = run.get("decision")
    error = run.get("error")
    validation = validation if isinstance(validation, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    error = error if isinstance(error, dict) else {}
    return {
        "source": "offline_failed_run_replay",
        "status": "ready",
        "items": [],
        "source_mappings": [],
        "last_run": {
            "id": run.get("id"),
            "status": run.get("status"),
            "committed": run.get("committed"),
            "chapter_index": run.get("chapter_index"),
            "goal": decision.get("goal"),
            "workflow": copy.deepcopy(run.get("workflow") or []),
            "problem_codes": copy.deepcopy(
                validation.get("problem_codes") or []
            ),
            "problem_count": validation.get("problem_count"),
            "blocking_problem_count": validation.get(
                "blocking_problem_count"
            ),
            "warning_count": validation.get("warning_count"),
            "severity_counts": copy.deepcopy(
                validation.get("severity_counts") or []
            ),
            "requested_focus": copy.deepcopy(
                validation.get("requested_focus") or []
            ),
            "executed_checks": copy.deepcopy(
                validation.get("executed_checks") or []
            ),
            "skipped_checks": copy.deepcopy(
                validation.get("skipped_checks") or []
            ),
            "repair_attempts": run.get("repair_attempts"),
            "error_type": error.get("type"),
            "error_message": error.get("message"),
        },
    }


def _recorded_failure_tokens(run: Mapping[str, Any]) -> int | None:
    error = run.get("error")
    message = (
        str(error.get("message") or "")
        if isinstance(error, dict)
        else ""
    )
    match = re.search(r"repair input requires (\d+) tokens", message)
    return int(match.group(1)) if match else None


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("json_artifact_invalid", f"{label} is not UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        _fail("json_artifact_invalid", f"{label} must contain one object.")
    return value


def _require_contained(path: Path, root: Path, *, label: str) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        _fail(
            "artifact_path_escape",
            f"{label} is outside the expected StoryProject runtime root.",
        )


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        _fail("required_value_missing", f"{label} must be non-empty.")
    return text


def _required_sha256(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256.fullmatch(digest) is None:
        _fail("sha256_invalid", f"{label} is not a lowercase SHA-256.")
    return digest


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        _fail("integer_invalid", f"{label} must be a positive integer.")
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        _fail("integer_invalid", f"{label} must be a positive integer.")
    if normalized < 1:
        _fail("integer_invalid", f"{label} must be a positive integer.")
    return normalized


def _canonical_sha256(value: Any) -> str:
    return _sha256_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(str(value).encode("utf-8"))


def _normalize_newlines(value: str) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _fail(code: str, message: str) -> None:
    raise ReplayChapterWorksetBudgetError(code, message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay ChapterContextReadSet plan/Scene/RepairEnvelope input "
            "budgets from hash-verified failed-run evidence. The replay is "
            "offline and performs no StoryProject writes or model calls."
        )
    )
    parser.add_argument("--story-project", required=True)
    parser.add_argument("--chapter", required=True, type=int)
    parser.add_argument("--run-json", required=True)
    parser.add_argument(
        "--manifest",
        help=(
            "Optional audited authority migration manifest applied as a "
            "read-only in-memory candidate overlay."
        ),
    )
    parser.add_argument(
        "--safe-target",
        type=int,
        default=DEFAULT_SAFE_TARGET,
        help=(
            "Every measured request must be strictly below this token count "
            "(default: 30000)."
        ),
    )
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint-type", default=DEFAULT_ENDPOINT_TYPE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = replay_chapter_workset_budget(
            story_project=args.story_project,
            chapter_index=args.chapter,
            run_json=args.run_json,
            manifest=args.manifest,
            safe_target=args.safe_target,
            provider=args.provider,
            model=args.model,
            endpoint_type=args.endpoint_type,
        )
    except (
        ReplayChapterWorksetBudgetError,
        ContextBudgetError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        payload = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "kind": "chapter_workset_budget_replay",
            "status": "error",
            "code": getattr(exc, "code", "replay_error"),
            "message": str(exc),
            "model_calls_performed": 0,
            "writes_performed": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
