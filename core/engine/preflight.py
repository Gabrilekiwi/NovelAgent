from __future__ import annotations

from importlib.util import find_spec
import json
from pathlib import Path
from typing import Any

from core.config import get_config
from core.context_budget import preview_chinese_output_compatibility
from core.director import decide_next_step
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH, RuntimePaths
from core.engine.report import build_run_report
from core.engine.run_record import load_latest_run_summary
from core.memory_v2.compile import compile_memory_v2
from core.schema import SchemaValidationError, validate_schema_consistency, validate_schema_keywords
from core.state.builder import build_snapshot_state_with_audit
from core.state.memory import load_memory_context
from core.state.memory_writer import DEFAULT_MEMORY_OUTBOX, resolve_memory_writeback_mode
from core.state.snapshot import load_snapshot
from core.story_project.runtime import build_generation_story_project_context
from core.story_project.identity import load_project_identity
from core.story_project.oh_story_detection import (
    detect_oh_story_compatibility,
    failed_oh_story_compatibility_report,
)
from core.story_project.validator import validate_story_project
from workflows.dynamic_flow import build_dynamic_flow, build_dynamic_flow_plan


PROMPT_ASSETS = (
    Path("prompts/chapter_prompt.md"),
    Path("prompts/director_prompt.md"),
    Path("prompts/polish_prompt.md"),
    Path("prompts/repair_prompt.md"),
    Path("prompts/snapshot_prompt.md"),
)

SCHEMA_ASSETS = (
    Path("core/director/schema.json"),
    Path("schemas/analysis_result.schema.json"),
    Path("schemas/attempt_context.schema.json"),
    Path("schemas/chapter_blueprint.schema.json"),
    Path("schemas/chapter_pipeline.schema.json"),
    Path("schemas/context_budget_report.schema.json"),
    Path("schemas/director_decision.schema.json"),
    Path("schemas/director_audit.schema.json"),
    Path("schemas/delivery_attempt_receipt.schema.json"),
    Path("schemas/delivery_job.schema.json"),
    Path("schemas/delivery_outcome.schema.json"),
    Path("schemas/input_pack_metadata.schema.json"),
    Path("schemas/execution_provenance.schema.json"),
    Path("schemas/llm_validation.schema.json"),
    Path("schemas/loop_session.schema.json"),
    Path("schemas/memory_context.schema.json"),
    Path("schemas/memory_writeback.schema.json"),
    Path("schemas/model_call_intent.schema.json"),
    Path("schemas/model_call_receipt.schema.json"),
    Path("schemas/model_response.schema.json"),
    Path("schemas/next_step_context_preflight.schema.json"),
    Path("schemas/oh_story_compatibility.schema.json"),
    Path("schemas/path_ref.schema.json"),
    Path("schemas/project_identity.schema.json"),
    Path("schemas/quality_decision.schema.json"),
    Path("schemas/quality_calibration_report.schema.json"),
    Path("schemas/readiness_decision.schema.json"),
    Path("schemas/prompt_context_bundle.schema.json"),
    Path("schemas/previous_chapter_context.schema.json"),
    Path("schemas/provider_smoke_report.schema.json"),
    Path("schemas/provider_retry_report.schema.json"),
    Path("schemas/real_storyproject_e2e_report.schema.json"),
    Path("schemas/repair_plan.schema.json"),
    Path("schemas/recovery_context.schema.json"),
    Path("schemas/review_gate_result.schema.json"),
    Path("schemas/review_index.schema.json"),
    Path("schemas/review_pipeline_summary.schema.json"),
    Path("schemas/run_record.schema.json"),
    Path("schemas/run_result.schema.json"),
    Path("schemas/snapshot_builder_audit.schema.json"),
    Path("schemas/snapshot.schema.json"),
    Path("schemas/story_project_runtime_migration.schema.json"),
    Path("schemas/story_project_managed_projection.schema.json"),
    Path("schemas/story_project_read_set.schema.json"),
    Path("schemas/story_project_semantic_fixture_manifest.schema.json"),
    Path("schemas/story_project_shadow_report.schema.json"),
    Path("schemas/story_state_calibration_report.schema.json"),
    Path("schemas/story_project_semantic_state.schema.json"),
    Path("schemas/state_update_audit.schema.json"),
    Path("schemas/trace_event.schema.json"),
    Path("schemas/token_calibration_report.schema.json"),
    Path("schemas/validation_result.schema.json"),
    Path("schemas/workflow_plan.schema.json"),
)

V1_STRUCTURE_PATHS = (
    Path("core/director/director.py"),
    Path("core/director/prompt.md"),
    Path("core/director/schema.json"),
    Path("core/engine/executor.py"),
    Path("core/execution_provenance.py"),
    Path("core/model_call_runtime.py"),
    Path("core/model_calls.py"),
    Path("core/structured_context.py"),
    Path("core/token_calibration.py"),
    Path("core/engine/delivery_coordinator.py"),
    Path("core/engine/persistence_coordinator.py"),
    Path("core/engine/quality_coordinator.py"),
    Path("core/engine/story_project_context.py"),
    Path("core/engine/workflow.py"),
    Path("core/runtime_paths.py"),
    Path("core/state/builder.py"),
    Path("core/state/input_pack.py"),
    Path("core/state/memory.py"),
    Path("core/state/snapshot.py"),
    Path("core/validator/continuity.py"),
    Path("core/validator/spatial.py"),
    Path("core/validator/logic.py"),
    Path("modules/chapter_generator/generator.py"),
    Path("modules/chapter_generator/pipeline.py"),
    Path("modules/claude_polish/polisher.py"),
    Path("modules/scene_repair/repairer.py"),
    Path("modules/scene_repair/plan.py"),
    Path("modules/conflict_engine/analyzer.py"),
    Path("prompts/director_prompt.md"),
    Path("prompts/snapshot_prompt.md"),
    Path("prompts/chapter_prompt.md"),
    Path("workflows/dynamic_flow.py"),
    Path("api/openai_client.py"),
    Path("api/claude_client.py"),
    Path("api/notion_client.py"),
    Path("main.py"),
    Path("core/cli/arguments.py"),
    Path("core/cli/commands.py"),
    Path("core/cli/config.py"),
    Path("core/cli/output.py"),
)


def run_preflight(
    *,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    memory_path: str | Path | None = None,
    memory_source: str = "auto",
    run_dir: str | Path = DEFAULT_RUN_DIR,
    chapter_dir: str | Path = DEFAULT_CHAPTER_DIR,
    dry_run: bool = False,
    require_claude: bool = False,
    director_model: str | None = None,
    memory_writeback: str = "none",
    memory_outbox: str | Path | None = None,
    notion_readback: bool = False,
    enable_llm_validator: bool = False,
    persist: bool | None = None,
    steps: int = 1,
    continue_on_rejection: bool = False,
    check_memory_v2: bool = False,
    memory_v2_output_dir: str | Path = Path("data/memory_v2/default"),
    story_project: str | Path | None = None,
    chapter: str | int | None = "auto",
    allow_story_state_shadow_downgrade: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    planned_workflow: list[str] | None = None
    effective_persist = (not dry_run) if persist is None else bool(persist)

    _check_prompt_assets(checks)
    _check_schema_assets(checks)
    _check_v1_structure(checks)
    _capture_detail_check(
        checks,
        "schema_consistency",
        _schema_consistency_details,
    )
    _check_loop_parameters(checks, steps=steps)
    story_project_validation = None
    story_project_identity = None
    if story_project is not None:
        story_project_validation = _check_story_project_structure(checks, story_project=story_project, chapter=chapter)
        _check_oh_story_detection(checks, story_project_validation)
        story_project_identity = _check_story_project_identity(
            checks,
            story_project_validation=story_project_validation,
            persist=effective_persist,
            snapshot_path=snapshot_path,
            run_dir=run_dir,
            chapter_dir=chapter_dir,
        )

    snapshot = _capture_check(checks, "snapshot", lambda: load_snapshot(snapshot_path))
    if story_project is not None and snapshot is not None:
        _check_story_project_snapshot_identity(
            checks,
            identity=story_project_identity,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
        )
    memory = _capture_memory_check(checks, memory_path=memory_path, memory_source=memory_source)
    if (
        story_project is not None
        and story_project_validation is not None
        and story_project_validation.ok
        and snapshot is not None
        and memory is not None
    ):
        _check_story_project_runtime_context(
            checks,
            story_project_validation=story_project_validation,
            snapshot=snapshot,
            memory=memory,
            identity=story_project_identity,
            allow_story_state_shadow_downgrade=allow_story_state_shadow_downgrade,
        )
    if check_memory_v2:
        _check_memory_v2_compile(
            checks,
            memory_path=memory_path,
            memory_source=memory_source,
            output_dir=memory_v2_output_dir,
        )
    _check_run_history(checks, run_dir=run_dir)

    if snapshot is not None and memory is not None:
        state_result = _capture_check(
            checks,
            "state_builder",
            lambda: build_snapshot_state_with_audit(snapshot, memory),
        )
        if state_result is not None:
            runtime_snapshot = state_result["snapshot"]
            snapshot_audit = state_result["audit"]
            checks.append(
                {
                    "name": "runtime_state_summary",
                    "ok": True,
                    "details": {
                        "chapter_index": runtime_snapshot.get("chapter_index"),
                        "memory_items": (runtime_snapshot.get("memory") or {}).get("item_count", 0),
                        "constraints": len(runtime_snapshot.get("constraints", [])),
                    },
                }
            )
            checks.append({"name": "state_builder_audit", "ok": True, "details": snapshot_audit})
            planned_workflow = _capture_detail_check(
                checks,
                "planned_workflow",
                lambda: _plan_workflow(
                    runtime_snapshot,
                    memory,
                    run_dir=run_dir,
                    director_model=director_model,
                ),
            )
            _capture_detail_check(
                checks,
                "planned_flow",
                lambda: _plan_flow(
                    runtime_snapshot,
                    memory,
                    run_dir=run_dir,
                    director_model=director_model,
                ),
            )

    config = get_config()
    _check_output_token_compatibility(
        checks,
        provider="openai",
        model=config.openai_model,
        max_output_tokens=config.openai_max_output_tokens,
        required=True,
    )
    checks.append(
        {
            "name": "execution_mode",
            "ok": True,
            "details": _execution_mode_details(
                dry_run=dry_run,
                persist=effective_persist,
                steps=steps,
                continue_on_rejection=continue_on_rejection,
                enable_llm_validator=enable_llm_validator,
                director_model=director_model,
                planned_workflow=planned_workflow,
                snapshot_path=snapshot_path,
                memory_path=memory_path,
                memory_source=memory_source,
                config=config,
                run_dir=run_dir,
                chapter_dir=chapter_dir,
            ),
        }
    )
    checks.append(
        {
            "name": "director",
            "ok": True,
            "details": {
                "mode": "model" if director_model else "rule",
                "model": director_model,
            },
        }
    )

    requires_openai = (not dry_run) or bool(director_model)
    if enable_llm_validator:
        requires_openai = True
    if not requires_openai:
        checks.append({"name": "api_keys", "ok": True, "details": {"mode": "dry_run"}})
    else:
        _check_python_dependency(
            checks,
            "dependency:openai",
            "openai",
            "Install the openai package for non-dry-run generation or model Director.",
        )
        _check_value(
            checks,
            "env:OPENAI_API_KEY",
            config.openai_api_key,
            "OPENAI_API_KEY is required for non-dry-run generation or model Director.",
        )

    if require_claude or _requires_claude_check(
        dry_run=dry_run,
        director_model=director_model,
        planned_workflow=planned_workflow,
    ):
        _check_output_token_compatibility(
            checks,
            provider="anthropic",
            model=config.claude_model or "unconfigured",
            max_output_tokens=config.claude_max_tokens,
            required=True,
        )
        _check_python_dependency(
            checks,
            "dependency:anthropic",
            "anthropic",
            "Install the anthropic package for non-dry-run Claude polish.",
        )
        _check_value(
            checks,
            "env:ANTHROPIC_API_KEY",
            config.anthropic_api_key,
            "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required",
        )
        _check_value(
            checks,
            "env:CLAUDE_MODEL",
            config.claude_model,
            "CLAUDE_MODEL or ANTHROPIC_MODEL is required",
        )

    _check_memory_writeback(
        checks,
        config=config,
        mode=memory_writeback,
        outbox_path=memory_outbox,
        notion_readback=notion_readback,
    )
    _check_artifact_targets(checks, run_dir=run_dir, chapter_dir=chapter_dir)
    checks.append(
        {
            "name": "llm_validator",
            "ok": True,
            "details": {"enabled": bool(enable_llm_validator), "provider": "openai" if enable_llm_validator else None},
        }
    )

    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def _check_output_token_compatibility(
    checks: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
    max_output_tokens: int,
    required: bool,
) -> None:
    """Preview whether a configured model cap can emit a complete chapter.

    This deliberately runs before any provider call.  Unknown or compatible
    endpoints use the conservative calibrated estimate and are never labelled
    as an exact tokenizer result.
    """

    name = f"output_token_compatibility:{provider}"
    try:
        details = preview_chinese_output_compatibility(max_output_tokens)
    except Exception as exc:  # noqa: BLE001 - preflight reports configuration faults.
        checks.append(
            {
                "name": name,
                "ok": False,
                "details": {
                    "provider": provider,
                    "model": model,
                    "max_output_tokens": max_output_tokens,
                    "required": required,
                },
                "error": str(exc),
            }
        )
        return

    details = {
        "provider": provider,
        "model": model,
        "required": required,
        **details,
    }
    compatible = bool(details["full_target_range_compatible"])
    check: dict[str, Any] = {
        "name": name,
        "ok": compatible or not required,
        "details": details,
    }
    if required and not compatible:
        check["error"] = (
            f"{provider} model {model} cannot cover the configured "
            "3,000-4,500 Chinese-character target: "
            f"{max_output_tokens} output tokens configured, "
            f"{details['maximum_required_tokens']} required by "
            f"{details['calibration_version']}"
        )
    checks.append(check)


def _capture_check(checks: list[dict[str, Any]], name: str, fn) -> Any:
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - preflight reports all startup failures.
        checks.append({"name": name, "ok": False, "error": str(exc)})
        return None

    checks.append({"name": name, "ok": True})
    return value


def _capture_detail_check(checks: list[dict[str, Any]], name: str, fn) -> Any:
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - preflight reports all startup failures.
        checks.append({"name": name, "ok": False, "error": str(exc)})
        return None

    checks.append({"name": name, "ok": True, "details": value})
    return value


def _capture_memory_check(
    checks: list[dict[str, Any]],
    *,
    memory_path: str | Path | None,
    memory_source: str,
) -> dict[str, Any] | None:
    input_details = _memory_input_details(memory_path=memory_path, memory_source=memory_source)
    if input_details.get("valid"):
        checks.append({"name": "memory_input", "ok": True, "details": input_details})
    else:
        checks.append(
            {
                "name": "memory_input",
                "ok": False,
                "details": input_details,
                "error": "memory source must be one of: auto, file, notion",
            }
        )
    try:
        memory = load_memory_context(memory_path, source=memory_source)
    except Exception as exc:  # noqa: BLE001 - preflight reports all startup failures.
        checks.append(
            {
                "name": "memory",
                "ok": False,
                "details": {
                    "requested_source": memory_source,
                    "path": str(memory_path) if memory_path else None,
                },
                "error": str(exc),
            }
        )
        return None

    details: dict[str, Any] = {
        "requested_source": memory_source,
        "source": memory.get("source"),
        "status": memory.get("status"),
        "item_count": len(memory.get("items") or []),
        **_memory_source_mapping_details(memory.get("source_mappings")),
    }
    if memory_path:
        details["path"] = str(memory_path)
    if memory.get("note"):
        details["note"] = memory.get("note")
    checks.append({"name": "memory", "ok": True, "details": details})
    return memory


def _memory_input_details(*, memory_path: str | Path | None, memory_source: str) -> dict[str, Any]:
    config = get_config()
    requested_source = (memory_source or "auto").strip().lower()
    path = Path(memory_path) if memory_path is not None else None
    configured_path = config.memory_path
    details: dict[str, Any] = {
        "requested_source": memory_source,
        "normalized_source": requested_source,
        "valid": requested_source in {"auto", "file", "notion"},
        "explicit_path": str(path) if path is not None else None,
        "configured_path": str(configured_path),
        "notion_api_configured": config.has_notion_api,
        "notion_api_key_configured": bool(config.notion_api_key),
        "notion_database_configured": bool(config.notion_database_id),
    }
    if not details["valid"]:
        details["resolved_source"] = None
        details["resolved_path"] = None
        details["resolution_reason"] = "invalid_source"
        return details

    uses_notion = requested_source == "notion" or (
        requested_source == "auto"
        and path is None
        and config.has_notion_api
    )
    if uses_notion:
        details["resolved_source"] = "notion-api"
        details["resolved_path"] = None
        if requested_source == "notion":
            details["resolution_reason"] = "forced_notion"
            details["ignored_explicit_path"] = str(path) if path is not None else None
        else:
            details["resolution_reason"] = "auto_notion_configured"
        return details

    resolved_path = path or configured_path
    details["resolved_source"] = "file"
    details["resolved_path"] = str(resolved_path)
    details["path_exists"] = resolved_path.exists()
    if path is not None:
        details["resolution_reason"] = "explicit_memory_path"
    elif requested_source == "file":
        details["resolution_reason"] = "forced_file"
    else:
        details["resolution_reason"] = "default_memory_path"
    return details


def _memory_source_mapping_details(source_mappings: Any) -> dict[str, Any]:
    mappings = [mapping for mapping in source_mappings if isinstance(mapping, dict)] if isinstance(source_mappings, list) else []
    source_counts: dict[str, int] = {}
    file_mapping_count = 0
    line_mapping_count = 0
    notion_page_mapping_count = 0
    notion_page_url_count = 0
    for mapping in mappings:
        source = str(mapping.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        if mapping.get("path"):
            file_mapping_count += 1
        if mapping.get("line_number") is not None:
            line_mapping_count += 1
        if mapping.get("page_id"):
            notion_page_mapping_count += 1
        if mapping.get("page_url"):
            notion_page_url_count += 1
    return {
        "source_mapping_count": len(mappings),
        "source_mapping_sources": [
            {"source": source, "count": count}
            for source, count in sorted(source_counts.items())
        ],
        "file_mapping_count": file_mapping_count,
        "line_mapping_count": line_mapping_count,
        "notion_page_mapping_count": notion_page_mapping_count,
        "notion_page_url_count": notion_page_url_count,
    }


def _check_memory_v2_compile(
    checks: list[dict[str, Any]],
    *,
    memory_path: str | Path | None,
    memory_source: str,
    output_dir: str | Path,
) -> None:
    input_details = _memory_input_details(memory_path=memory_path, memory_source=memory_source)
    details: dict[str, Any] = {
        "enabled": True,
        "dry_run": True,
        "reset": True,
        "input": input_details,
        "output_dir": str(output_dir),
    }
    source_path = input_details.get("resolved_path")
    if input_details.get("resolved_source") != "file" or not source_path:
        checks.append(
            {
                "name": "memory_v2_compile",
                "ok": False,
                "details": details,
                "error": "Memory V2 compile check requires a file memory input.",
            }
        )
        return

    try:
        report = compile_memory_v2(
            memory_path=source_path,
            output_dir=output_dir,
            reset=True,
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001 - preflight reports all startup failures.
        checks.append({"name": "memory_v2_compile", "ok": False, "details": details, "error": str(exc)})
        return

    details.update(
        {
            "status": report.get("status"),
            "memory_path": report.get("memory_path"),
            "operation_count": (report.get("patch") or {}).get("operation_count"),
            "operation_types": (report.get("patch") or {}).get("operation_types"),
            "event_count": (report.get("events") or {}).get("event_count"),
            "canonical_revision": (report.get("canonical_memory") or {}).get("revision"),
            "snapshot_preview": report.get("snapshot_preview"),
        }
    )
    checks.append({"name": "memory_v2_compile", "ok": True, "details": details})


def _check_value(checks: list[dict[str, Any]], name: str, value: str | None, error: str) -> None:
    checks.append(
        {
            "name": name,
            "ok": bool(value),
            "error": None if value else error,
        }
    )


def _check_python_dependency(checks: list[dict[str, Any]], name: str, module_name: str, error: str) -> None:
    ok = find_spec(module_name) is not None
    checks.append(
        {
            "name": name,
            "ok": ok,
            "details": {"module": module_name},
            "error": None if ok else error,
        }
    )


def _check_loop_parameters(checks: list[dict[str, Any]], *, steps: int) -> None:
    details = {"steps": steps}
    if isinstance(steps, bool):
        checks.append({"name": "loop_parameters", "ok": False, "details": details, "error": "steps must be an integer"})
        return
    try:
        step_count = int(steps)
    except (TypeError, ValueError):
        checks.append({"name": "loop_parameters", "ok": False, "details": details, "error": "steps must be an integer"})
        return
    details["steps"] = step_count
    if step_count < 1:
        checks.append({"name": "loop_parameters", "ok": False, "details": details, "error": "steps must be at least 1"})
        return
    checks.append({"name": "loop_parameters", "ok": True, "details": details})


def _check_story_project_structure(
    checks: list[dict[str, Any]],
    *,
    story_project: str | Path,
    chapter: str | int | None,
) -> Any:
    try:
        result = validate_story_project(story_project=story_project, chapter=chapter)
    except Exception as exc:  # noqa: BLE001 - preflight reports all startup failures.
        checks.append({"name": "story_project_structure", "ok": False, "error": str(exc)})
        return None

    details = result.to_dict()
    blocking = [problem for problem in details.get("problems", []) if problem.get("blocking")]
    if blocking:
        checks.append(
            {
                "name": "story_project_structure",
                "ok": False,
                "details": details,
                "error": "; ".join(str(problem.get("message")) for problem in blocking),
            }
        )
        return result
    checks.append({"name": "story_project_structure", "ok": True, "details": details})
    return result


def _check_oh_story_detection(checks: list[dict[str, Any]], story_project_validation: Any) -> None:
    root = None
    if story_project_validation is not None and story_project_validation.root_resolution is not None:
        root = story_project_validation.root_resolution.root
    try:
        details = detect_oh_story_compatibility(root, workspace_root=Path.cwd())
    except Exception as exc:  # noqa: BLE001 - oh-story detection is always non-blocking.
        details = failed_oh_story_compatibility_report(root, exc, workspace_root=Path.cwd())
    checks.append({"name": "oh_story_detection", "ok": True, "details": details})


def _check_story_project_identity(
    checks: list[dict[str, Any]],
    *,
    story_project_validation: Any,
    persist: bool,
    snapshot_path: str | Path,
    run_dir: str | Path,
    chapter_dir: str | Path,
) -> Any:
    root = None
    if story_project_validation is not None and story_project_validation.root_resolution is not None:
        root = story_project_validation.root_resolution.root
    if root is None:
        checks.append(
            {
                "name": "story_project_identity",
                "ok": False,
                "error": "StoryProject identity requires a resolved root.",
            }
        )
        return None
    try:
        identity = load_project_identity(root)
    except Exception as exc:  # noqa: BLE001 - malformed identity must fail preflight.
        checks.append({"name": "story_project_identity", "ok": False, "error": str(exc)})
        return None
    runtime_paths = RuntimePaths.for_story_project(root)
    details = {
        "status": "stable" if identity is not None else ("will_create_on_persist" if persist else "ephemeral_preview"),
        "identity": identity.to_dict() if identity is not None else None,
        "project_identity_path": str(root / ".novelagent" / "project.json"),
        "default_runtime_paths": runtime_paths.to_dict(),
        "configured_paths": {
            "snapshot_path": str(snapshot_path),
            "run_dir": str(run_dir),
            "chapter_dir": str(chapter_dir),
        },
        "created_files": False,
    }
    checks.append({"name": "story_project_identity", "ok": True, "details": details})
    return identity


def _check_story_project_snapshot_identity(
    checks: list[dict[str, Any]],
    *,
    identity: Any,
    snapshot: dict[str, Any],
    snapshot_path: str | Path,
) -> None:
    snapshot_book_id = snapshot.get("book_id")
    if identity is None:
        ok = snapshot_book_id is None
        checks.append(
            {
                "name": "story_project_snapshot_identity",
                "ok": ok,
                "details": {
                    "snapshot_path": str(snapshot_path),
                    "snapshot_book_id": snapshot_book_id,
                    "project_book_id": None,
                    "status": "legacy_unbound" if ok else "identity_missing_for_bound_snapshot",
                },
                "error": None if ok else "Snapshot has book_id but StoryProject project.json is missing or invalid.",
            }
        )
        return
    ok = snapshot_book_id in {None, identity.book_id}
    checks.append(
        {
            "name": "story_project_snapshot_identity",
            "ok": ok,
            "details": {
                "snapshot_path": str(snapshot_path),
                "snapshot_book_id": snapshot_book_id,
                "project_book_id": identity.book_id,
                "status": "matching" if snapshot_book_id == identity.book_id else "legacy_unbound",
            },
            "error": None if ok else "story_project_state_identity_mismatch",
        }
    )


def _check_story_project_runtime_context(
    checks: list[dict[str, Any]],
    *,
    story_project_validation,
    snapshot: dict[str, Any],
    memory: dict[str, Any],
    identity: Any,
    allow_story_state_shadow_downgrade: bool,
) -> None:
    root = story_project_validation.root_resolution.root if story_project_validation.root_resolution else None
    chapter_resolution = story_project_validation.chapter_resolution
    chapter_index = chapter_resolution.resolved_chapter if chapter_resolution else None
    if root is None or chapter_index is None:
        checks.append(
            {
                "name": "story_project_runtime_context",
                "ok": False,
                "error": "StoryProject runtime context requires a resolved root and chapter.",
            }
        )
        return
    try:
        context = build_generation_story_project_context(
            story_project=root,
            chapter=chapter_index,
            snapshot=snapshot,
            memory_context=memory,
            project_identity=identity,
            allow_story_state_shadow_downgrade=allow_story_state_shadow_downgrade,
        )
    except Exception as exc:  # noqa: BLE001 - preflight reports all startup failures.
        checks.append({"name": "story_project_runtime_context", "ok": False, "error": str(exc)})
        return
    checks.append({"name": "story_project_runtime_context", "ok": True, "details": context.to_dict()})


def _check_prompt_assets(checks: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    for path in PROMPT_ASSETS:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            errors.append(f"{path}: missing")
            continue
        except UnicodeDecodeError as exc:
            errors.append(f"{path}: invalid UTF-8 ({exc})")
            continue
        except OSError as exc:
            errors.append(f"{path}: unreadable ({exc})")
            continue
        if not content.strip():
            errors.append(f"{path}: empty")

    _append_asset_check(checks, "prompt_assets", PROMPT_ASSETS, errors)


def _check_schema_assets(checks: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    for path in SCHEMA_ASSETS:
        try:
            with path.open("r", encoding="utf-8-sig") as schema_file:
                schema = json.load(schema_file)
            validate_schema_keywords(schema, str(path))
        except FileNotFoundError:
            errors.append(f"{path}: missing")
            continue
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: invalid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})")
            continue
        except SchemaValidationError as exc:
            errors.append(str(exc))
            continue
        except OSError as exc:
            errors.append(f"{path}: unreadable ({exc})")
            continue

    _append_asset_check(checks, "schema_assets", SCHEMA_ASSETS, errors)


def _check_v1_structure(checks: list[dict[str, Any]]) -> None:
    errors = [f"{path}: missing" for path in V1_STRUCTURE_PATHS if not path.exists()]
    _append_asset_check(checks, "v1_structure", V1_STRUCTURE_PATHS, errors)


def _schema_consistency_details() -> dict[str, Any]:
    contracts = validate_schema_consistency()
    return {
        "count": len(contracts),
        "contracts": contracts,
    }


def _append_asset_check(
    checks: list[dict[str, Any]],
    name: str,
    paths: tuple[Path, ...],
    errors: list[str],
) -> None:
    details = {
        "count": len(paths),
        "paths": [str(path) for path in paths],
    }
    if errors:
        checks.append({"name": name, "ok": False, "details": details, "error": "; ".join(errors)})
        return
    checks.append({"name": name, "ok": True, "details": details})


def _plan_workflow(
    runtime_snapshot: dict[str, Any],
    memory_context: dict[str, Any],
    *,
    run_dir: str | Path,
    director_model: str | None,
) -> list[str] | dict[str, Any]:
    if director_model:
        return {
            "source": "model",
            "actions": None,
            "note": "Workflow will be decided by the model Director at runtime.",
        }

    director_memory = dict(memory_context)
    last_run = load_latest_run_summary(run_dir)
    if last_run:
        director_memory["last_run"] = last_run
    return build_dynamic_flow(decide_next_step(runtime_snapshot, director_memory))


def _plan_flow(
    runtime_snapshot: dict[str, Any],
    memory_context: dict[str, Any],
    *,
    run_dir: str | Path,
    director_model: str | None,
) -> dict[str, Any]:
    if director_model:
        return {
            "source": "model",
            "actions": None,
            "note": "Dynamic flow will be decided by the model Director at runtime.",
        }

    director_memory = dict(memory_context)
    last_run = load_latest_run_summary(run_dir)
    if last_run:
        director_memory["last_run"] = last_run
    return build_dynamic_flow_plan(decide_next_step(runtime_snapshot, director_memory))


def _requires_claude_check(
    *,
    dry_run: bool,
    director_model: str | None,
    planned_workflow: list[str] | dict[str, Any] | None,
) -> bool:
    if dry_run:
        return False
    if director_model:
        return True
    return isinstance(planned_workflow, list) and "polish" in planned_workflow


def _execution_mode_details(
    *,
    dry_run: bool,
    persist: bool,
    steps: int,
    continue_on_rejection: bool,
    director_model: str | None,
    planned_workflow: list[str] | dict[str, Any] | None,
    snapshot_path: str | Path,
    memory_path: str | Path | None,
    memory_source: str,
    config,
    run_dir: str | Path,
    chapter_dir: str | Path,
    enable_llm_validator: bool,
) -> dict[str, Any]:
    model_calls: list[str] = []
    if director_model:
        model_calls.append("director_openai")
    if not dry_run:
        model_calls.append("chapter_generation_openai")
        if director_model or (isinstance(planned_workflow, list) and "polish" in planned_workflow):
            model_calls.append("claude_polish")
    if enable_llm_validator:
        model_calls.append("llm_validation_openai")
    return {
        "dry_run": bool(dry_run),
        "persist": bool(persist),
        "steps": int(steps),
        "stop_on_rejection": not bool(continue_on_rejection),
        "director_mode": "model" if director_model else "rule",
        "director_model": director_model,
        "model_calls": model_calls,
        "snapshot_path": str(snapshot_path),
        "memory_source": memory_source,
        "memory_path": str(memory_path) if memory_path else str(config.memory_path),
        "run_dir": str(run_dir),
        "chapter_dir": str(chapter_dir),
        "llm_validator_enabled": bool(enable_llm_validator),
        "provider_retry": {
            "max_attempts": int(config.provider_max_attempts),
            "base_delay_seconds": float(config.provider_retry_base_delay_seconds),
            "max_delay_seconds": float(config.provider_retry_max_delay_seconds),
            "jitter_ratio": float(config.provider_retry_jitter_ratio),
            "deadline_seconds": float(config.provider_retry_deadline_seconds),
            "openai_sdk_max_retries": 0,
            "anthropic_sdk_max_retries": 0,
            "notion_create_generic_retry": False,
        },
    }


def _check_memory_writeback(
    checks: list[dict[str, Any]],
    *,
    config,
    mode: str,
    outbox_path: str | Path | None,
    notion_readback: bool,
) -> None:
    try:
        effective_mode = resolve_memory_writeback_mode(mode=mode, outbox_path=outbox_path)
    except ValueError as exc:
        checks.append({"name": "memory_writeback", "ok": False, "error": str(exc)})
        return

    details: dict[str, Any] = {"mode": effective_mode}
    if effective_mode == "none":
        checks.append({"name": "memory_writeback", "ok": True, "details": details})
        return

    if effective_mode == "file":
        details["path"] = str(outbox_path or DEFAULT_MEMORY_OUTBOX)
        checks.append({"name": "memory_writeback", "ok": True, "details": details})
        return

    details["notion_readback"] = bool(notion_readback)
    details["notion_dedupe_existing"] = True
    checks.append({"name": "memory_writeback", "ok": True, "details": details})
    _check_value(checks, "env:NOTION_API_KEY", config.notion_api_key, "NOTION_API_KEY is required for Notion memory writeback")
    _check_value(
        checks,
        "env:NOTION_DATABASE_ID",
        config.notion_database_id,
        "NOTION_DATABASE_ID or NOVELAGENT_NOTION_DATABASE_ID is required for Notion memory writeback",
    )


def _check_run_history(checks: list[dict[str, Any]], *, run_dir: str | Path) -> None:
    report = build_run_report(run_dir=run_dir, limit=1)
    skipped = report.get("skipped", [])
    skipped_sessions = report.get("skipped_loop_sessions", [])
    latest = report.get("latest") if isinstance(report.get("latest"), dict) else None
    latest_session = report.get("latest_loop_session") if isinstance(report.get("latest_loop_session"), dict) else None
    latest_validation = latest.get("validation") if isinstance(latest, dict) and isinstance(latest.get("validation"), dict) else {}
    latest_session_run = _latest_session_run_summary(latest_session)
    details = {
        "run_dir": report.get("run_dir"),
        "total": report.get("total", 0),
        "loaded": report.get("loaded", 0),
        "skipped": len(skipped) if isinstance(skipped, list) else 0,
        "loop_session_total": report.get("loop_session_total", 0),
        "loop_session_loaded": report.get("loop_session_loaded", 0),
        "skipped_loop_sessions": len(skipped_sessions) if isinstance(skipped_sessions, list) else 0,
        "latest_run_id": latest.get("id") if latest else None,
        "latest_run_status": latest.get("status") if latest else None,
        "latest_run_chapter_index": latest.get("chapter_index") if latest else None,
        "latest_run_problem_count": latest_validation.get("problem_count") if latest_validation else None,
        "latest_run_requested_focus": latest_validation.get("requested_focus", []) if latest_validation else [],
        "latest_run_executed_checks": latest_validation.get("executed_checks", []) if latest_validation else [],
        "latest_run_skipped_checks": latest_validation.get("skipped_checks", []) if latest_validation else [],
        "latest_loop_session_id": latest_session.get("id") if latest_session else None,
        "latest_loop_session_stopped_reason": latest_session.get("stopped_reason") if latest_session else None,
        "latest_loop_session_last_run_id": latest_session_run.get("id") if latest_session_run else None,
        "latest_loop_session_last_run_status": latest_session_run.get("status") if latest_session_run else None,
        "latest_loop_session_last_run_executed_checks": latest_session_run.get("executed_checks", []) if latest_session_run else [],
        "latest_loop_session_last_run_skipped_checks": latest_session_run.get("skipped_checks", []) if latest_session_run else [],
    }
    errors = []
    if isinstance(skipped, list):
        errors.extend(f"{item.get('path')}: {item.get('error')}" for item in skipped[:3] if isinstance(item, dict))
    if isinstance(skipped_sessions, list):
        errors.extend(f"{item.get('path')}: {item.get('error')}" for item in skipped_sessions[:3] if isinstance(item, dict))
    if errors:
        checks.append({"name": "run_history", "ok": False, "details": details, "error": "; ".join(errors)})
        return
    checks.append({"name": "run_history", "ok": True, "details": details})


def _latest_session_run_summary(latest_session: Any) -> dict[str, Any]:
    if not isinstance(latest_session, dict):
        return {}
    run_summaries = latest_session.get("run_summaries")
    if not isinstance(run_summaries, list) or not run_summaries:
        return {}
    last_run = run_summaries[-1]
    return last_run if isinstance(last_run, dict) else {}


def _check_artifact_targets(
    checks: list[dict[str, Any]],
    *,
    run_dir: str | Path,
    chapter_dir: str | Path,
) -> None:
    targets = {
        "run_dir": Path(run_dir),
        "snapshot_pack_dir": Path(run_dir) / "snapshot_packs",
        "input_pack_dir": Path(run_dir) / "input_packs",
        "loop_session_dir": Path(run_dir) / "loop_sessions",
        "chapter_dir": Path(chapter_dir),
    }
    errors: list[str] = []
    details = {name: str(path) for name, path in targets.items()}

    for name, target in targets.items():
        if target.exists() and not target.is_dir():
            errors.append(f"{name} {target} exists and is not a directory")
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"{name} {target} could not be created: {exc}")
            continue
        if not target.is_dir():
            errors.append(f"{name} {target} exists and is not a directory")

    if errors:
        checks.append({"name": "artifact_targets", "ok": False, "details": details, "error": "; ".join(errors)})
        return
    checks.append({"name": "artifact_targets", "ok": True, "details": details})
