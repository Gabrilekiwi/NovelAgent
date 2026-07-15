from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import sys
from types import SimpleNamespace
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.autonomy.cli import (  # noqa: E402
    _build_autonomy_runner,
    _capture_source_snapshot_from_args,
)
from core.autonomy.common import (  # noqa: E402
    atomic_write_json,
    canonical_hash,
    load_json_object,
    sha256_digest,
)
from core.autonomy.outline import validate_outline_checkpoint  # noqa: E402
from core.autonomy.plans import compile_instruction_plan  # noqa: E402
from core.autonomy.profiles import TrustedProfiles  # noqa: E402
from core.autonomy.session import AutonomySessionStore  # noqa: E402
from core.config import PROXY_ENV_NAMES, clear_proxy_env, get_config  # noqa: E402
from core.context_budget import ContextBudgetError  # noqa: E402
from core.delivery import DeliveryQueue  # noqa: E402
from core.engine.persistence import atomic_create_json  # noqa: E402
from core.engine.persistence_v2 import verify_publication_receipt  # noqa: E402
from core.engine.safe_paths import RootBinding, SafePathResolver  # noqa: E402
from core.execution_provenance import (  # noqa: E402
    capture_execution_provenance,
    validate_execution_provenance,
)
from core.memory_v2 import (  # noqa: E402
    apply_genesis_event,
    canonical_memory_to_snapshot,
    create_genesis_memory_batch,
    load_memory_event_batches,
    replay_memory_events,
    save_canonical_memory,
    write_memory_event_batch,
)
from core.model_calls import (  # noqa: E402
    load_model_call_intent,
    load_model_call_receipt,
)
from core.path_refs import resolve_path_ref  # noqa: E402
from core.quality_decision import resolve_quality_policy  # noqa: E402
from core.runtime_paths import RuntimePaths  # noqa: E402
from core.schema import validate_schema  # noqa: E402
from core.story_project.authority import (  # noqa: E402
    activate_event_authority,
    project_identity_sha256,
)
from core.story_project.identity import (  # noqa: E402
    ensure_project_identity,
    load_project_identity,
)
from core.story_project.model import CORE_DIRECTORY_NAMES  # noqa: E402
from core.story_project.paths import (  # noqa: E402
    canonical_outline_path,
    infer_next_chapter,
    resolve_prose,
)


OPT_IN_ENV = "NOVELAGENT_REAL_AUTONOMY_E2E"
OPT_IN_PREFIX = "I_ACCEPT_BILLABLE_OPENAI_CALLS"
PROXY_MODE_ENV = "NOVELAGENT_REAL_AUTONOMY_PROXY_MODE"
REPORT_SCHEMA = "real_autonomy_e2e_report.schema.json"


class RealAutonomyE2EError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
        retain_report: bool = False,
        cleanup_completed: bool = False,
    ) -> None:
        self.code = str(code)
        self.diagnostics = _validate_failure_diagnostics(diagnostics or {})
        self.retain_report = bool(retain_report)
        self.cleanup_completed = bool(cleanup_completed)
        super().__init__(f"{self.code}: {message}")


def run_real_autonomy_e2e(
    *,
    chapter_count: int,
    output_path: str | Path | None = None,
    confirmed: bool = False,
    work_parent: str | Path | None = None,
) -> dict[str, Any]:
    """Run one isolated, billable OpenAI autonomy release gate.

    The input StoryProject is generated inside a temporary directory.  The
    only external side effect is required File Delivery into a sibling
    temporary root.  A validated, redacted report may be copied to
    ``output_path`` after the temporary project has been destroyed.
    """

    count = _validate_gate_count(chapter_count)
    _require_release_authorization(count, confirmed=confirmed)
    _assert_no_notion_configuration()
    _set_release_defaults()
    proxy_mode = _resolve_release_proxy_mode()
    config = get_config()
    _validate_provider_configuration(config)
    release_provenance = _capture_clean_release_provenance(
        config, proxy_mode=proxy_mode
    )
    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report_target = (
        _reserve_release_report_target(output_path, chapter_count=count)
        if output_path is not None
        else None
    )

    parent = Path(work_parent) if work_parent is not None else ROOT / ".tmp" / "rae"
    # Keep the isolated prefix deliberately short: deeply nested immutable
    # receipt filenames must remain below legacy Windows MAX_PATH boundaries.
    isolated_root = parent / f"g{uuid.uuid4().hex[:8]}"
    failure: RealAutonomyE2EError | None = None
    book: Path | None = None
    paths: RuntimePaths | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        isolated_root.mkdir(parents=False)
        book = isolated_root / "b"
        delivery_root = isolated_root / "d"
        delivery_root.mkdir(parents=True)
        identity, genesis = _bootstrap_generated_story_project(book)
        paths = RuntimePaths.for_story_project(book)
        input_manifest_hash = _tree_manifest_hash(book)

        delivery_root_uuid = str(uuid.uuid4())
        profiles_payload = _trusted_profiles_payload(
            book_id=identity.book_id,
            delivery_root_uuid=delivery_root_uuid,
            chapter_count=count,
            model=config.openai_model,
            max_output_tokens=config.openai_max_output_tokens,
        )
        profile_path = isolated_root / "o" / "p.json"
        root_map_path = isolated_root / "o" / "r.json"
        atomic_write_json(profile_path, profiles_payload)
        atomic_write_json(
            root_map_path,
            {
                "schema_version": "1.0",
                "roots": {delivery_root_uuid: str(delivery_root.resolve())},
            },
        )
        profiles = TrustedProfiles.load(profile_path)
        args = SimpleNamespace(
            _resolved_story_project_root=book.resolve(),
            autonomy_root_map=str(root_map_path),
            dry_run=False,
        )
        publication_roots = paths.root_map(book)
        publication_roots["runtime"] = paths.runtime_dir.resolve()
        sessions = AutonomySessionStore(
            paths.runtime_dir / "autonomy",
            trusted_profiles=profiles,
            publication_root_map=publication_roots,
        )
        source = _capture_source_snapshot_from_args(args, profiles=profiles)
        plan = compile_instruction_plan(
            f"连续写 {count}章 quality=release-strict provider=official-openai",
            trusted_profiles=profiles,
            source_snapshot=source,
        )
        runner = _build_autonomy_runner(
            args,
            story_runtime_paths=paths,
            sessions=sessions,
            profiles=profiles,
            story_profile_id="generated-release-book",
        )
        try:
            execution = runner.execute_plan(plan)
        except ContextBudgetError as exc:
            raise RealAutonomyE2EError(
                "context_budget_error",
                "a ContextBudgetError occurred; the gate is not releasable",
                diagnostics=_safe_failure_diagnostics(
                    exc,
                    phase="runner_execute",
                    evidence_counts=_failure_evidence_counts(paths, book),
                ),
                retain_report=True,
            ) from exc
        except ValueError as exc:
            raise RealAutonomyE2EError(
                "internal_value_error",
                "an internal ValueError occurred; the gate is not releasable",
                diagnostics=_safe_failure_diagnostics(
                    exc,
                    phase="runner_execute",
                    evidence_counts=_failure_evidence_counts(paths, book),
                ),
                retain_report=True,
            ) from exc
        except RealAutonomyE2EError as exc:
            exc.retain_report = True
            exc.diagnostics = _merge_failure_diagnostics(
                exc.diagnostics,
                phase="runner_execute",
                evidence_counts=_failure_evidence_counts(paths, book),
            )
            raise
        except Exception as exc:
            raise RealAutonomyE2EError(
                "autonomy_execution_failed",
                f"the isolated autonomy run failed with {type(exc).__name__}",
                diagnostics=_safe_failure_diagnostics(
                    exc,
                    phase="runner_execute",
                    evidence_counts=_failure_evidence_counts(paths, book),
                ),
                retain_report=True,
            ) from exc

        try:
            report = _verify_release_run(
                chapter_count=count,
                execution=execution,
                plan=plan,
                profiles=profiles,
                sessions=sessions,
                runner=runner,
                paths=paths,
                book=book,
                delivery_root=delivery_root,
                delivery_root_uuid=delivery_root_uuid,
                publication_roots=publication_roots,
                input_manifest_hash=input_manifest_hash,
                initial_head_event_hash=genesis["events"][-1]["event_hash"],
                model=config.openai_model,
                release_provenance=release_provenance,
                harness_sha256=harness_sha256,
                proxy_mode=proxy_mode,
            )
        except RealAutonomyE2EError as exc:
            exc.retain_report = True
            exc.diagnostics = _merge_failure_diagnostics(
                exc.diagnostics,
                phase="release_verification",
                evidence_counts=_failure_evidence_counts(paths, book),
            )
            raise
        except ContextBudgetError as exc:
            raise RealAutonomyE2EError(
                "context_budget_error",
                "verification encountered ContextBudgetError",
                diagnostics=_safe_failure_diagnostics(
                    exc,
                    phase="release_verification",
                    evidence_counts=_failure_evidence_counts(paths, book),
                ),
                retain_report=True,
            ) from exc
        except ValueError as exc:
            raise RealAutonomyE2EError(
                "internal_value_error",
                "verification encountered an internal ValueError",
                diagnostics=_safe_failure_diagnostics(
                    exc,
                    phase="release_verification",
                    evidence_counts=_failure_evidence_counts(paths, book),
                ),
                retain_report=True,
            ) from exc
        except Exception as exc:
            raise RealAutonomyE2EError(
                "release_verification_failed",
                f"release evidence verification failed with {type(exc).__name__}",
                diagnostics=_safe_failure_diagnostics(
                    exc,
                    phase="release_verification",
                    evidence_counts=_failure_evidence_counts(paths, book),
                ),
                retain_report=True,
            ) from exc
    except RealAutonomyE2EError as exc:
        failure = exc
        raise
    except Exception as exc:
        failure = RealAutonomyE2EError(
            "gate_setup_failed",
            f"isolated release gate setup failed with {type(exc).__name__}",
            diagnostics=_safe_failure_diagnostics(
                exc,
                phase="gate_setup",
                evidence_counts=_failure_evidence_counts(paths, book),
            ),
            retain_report=True,
        )
        raise failure from exc
    finally:
        try:
            if isolated_root.exists():
                _remove_isolated_tree(isolated_root, parent=parent)
        except RealAutonomyE2EError as cleanup_error:
            cleanup_error.retain_report = True
            cleanup_error.diagnostics = _merge_failure_diagnostics(
                cleanup_error.diagnostics,
                phase="cleanup",
                evidence_counts={},
            )
            if report_target is not None:
                    _publish_release_failure_report(
                        report_target,
                        chapter_count=count,
                        error=cleanup_error,
                        proxy_mode=proxy_mode,
                    )
            raise
        else:
            if failure is not None:
                failure.cleanup_completed = True
                if report_target is not None:
                    _publish_release_failure_report(
                        report_target,
                        chapter_count=count,
                        error=failure,
                        proxy_mode=proxy_mode,
                    )

    if report_target is not None:
        _require_release_report_reservation(report_target, chapter_count=count)
        atomic_write_json(report_target, report)
    return report


def _validate_gate_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RealAutonomyE2EError(
            "release_gate_count_invalid", "chapter count must be a positive integer"
        )
    if value not in {1, 4, 10} and value < 20:
        raise RealAutonomyE2EError(
            "release_gate_count_invalid", "allowed release gates are 1, 4, 10, or at least 20 chapters"
        )
    return value


def _require_release_authorization(
    chapter_count: int,
    *,
    confirmed: bool,
    environ: Mapping[str, str] | None = None,
) -> None:
    count = _validate_gate_count(chapter_count)
    env = os.environ if environ is None else environ
    expected = f"{OPT_IN_PREFIX}:{count}"
    if not confirmed or str(env.get(OPT_IN_ENV, "")).strip() != expected:
        raise RealAutonomyE2EError(
            "real_provider_opt_in_required",
            f"pass --confirm-real-provider-calls and set {OPT_IN_ENV}={expected}",
        )
    if not str(env.get("OPENAI_API_KEY", "")).strip():
        raise RealAutonomyE2EError(
            "openai_not_configured", "OPENAI_API_KEY must be present in the process environment"
        )


def _assert_no_notion_configuration(
    *,
    environ: Mapping[str, str] | None = None,
    argv: list[str] | None = None,
) -> None:
    env = os.environ if environ is None else environ
    configured = sorted(
        name
        for name, value in env.items()
        if "NOTION" in str(name).upper() and str(value).strip()
    )
    requested = [part for part in (argv or []) if "notion" in str(part).lower()]
    if configured or requested:
        raise RealAutonomyE2EError(
            "notion_configuration_forbidden",
            "real autonomy release gates refuse every Notion setting and command-line flag",
        )


def _set_release_defaults() -> None:
    # A real release gate must not load credentials (especially Notion) from a
    # workspace .env.  OPENAI_API_KEY was already required directly above.
    os.environ["NOVELAGENT_SKIP_DOTENV"] = "1"
    os.environ.setdefault("OPENAI_TIMEOUT_SECONDS", "120")
    os.environ.setdefault("OPENAI_MAX_OUTPUT_TOKENS", "6000")
    os.environ.setdefault("OPENAI_STREAM", "0")
    os.environ.setdefault("PROVIDER_MAX_ATTEMPTS", "2")
    os.environ.setdefault("PROVIDER_RETRY_DEADLINE_SECONDS", "180")


def _resolve_release_proxy_mode(
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    choice = str(env.get(PROXY_MODE_ENV, "")).strip().lower()
    if choice not in {"", "direct", "inherit", "clear"}:
        raise RealAutonomyE2EError(
            "release_proxy_mode_invalid",
            f"{PROXY_MODE_ENV} must be direct, inherit, or clear",
        )
    configured = any(str(env.get(name, "")).strip() for name in PROXY_ENV_NAMES)
    if configured and choice not in {"inherit", "clear"}:
        raise RealAutonomyE2EError(
            "release_proxy_mode_required",
            "configured proxy variables require an explicit inherit or clear release choice",
        )
    if choice == "clear":
        if environ is not None:
            raise RealAutonomyE2EError(
                "release_proxy_mode_invalid",
                "proxy clearing is available only for the live process environment",
            )
        clear_proxy_env()
        return "cleared"
    if configured:
        return "inherited"
    return "direct"


def _validate_provider_configuration(config: Any) -> None:
    if not config.openai_api_key:
        raise RealAutonomyE2EError("openai_not_configured", "OPENAI_API_KEY is required")
    if config.openai_base_url:
        raise RealAutonomyE2EError(
            "official_openai_endpoint_required", "release gates do not accept compatible or custom endpoints"
        )
    if not 1 <= int(config.openai_timeout_seconds) <= 180:
        raise RealAutonomyE2EError(
            "provider_limits_invalid", "OPENAI_TIMEOUT_SECONDS must be between 1 and 180"
        )
    if not 6000 <= int(config.openai_max_output_tokens) <= 8000:
        raise RealAutonomyE2EError(
            "provider_limits_invalid",
            "OPENAI_MAX_OUTPUT_TOKENS must be 6000-8000 for the 3000-4500 character chapter gate",
        )
    if not 1 <= int(config.provider_max_attempts) <= 2:
        raise RealAutonomyE2EError(
            "provider_limits_invalid", "PROVIDER_MAX_ATTEMPTS must be 1 or 2"
        )
    if int(config.openai_max_retries) != 0:
        raise RealAutonomyE2EError(
            "provider_limits_invalid",
            "OPENAI_MAX_RETRIES must be unset or zero; release retry policy is PROVIDER_MAX_ATTEMPTS",
        )


def _capture_clean_release_provenance(
    config: Any,
    *,
    proxy_mode: str,
) -> dict[str, Any]:
    provenance = capture_execution_provenance(
        ROOT,
        provider="openai",
        model=str(config.openai_model),
        config={
            "gate": "real_autonomy_e2e",
            "max_output_tokens": int(config.openai_max_output_tokens),
            "provider_max_attempts": int(config.provider_max_attempts),
            "proxy_mode": proxy_mode,
        },
        feature_flags={
            "official_endpoint": True,
            "strict_validator": True,
            "required_file_delivery": True,
        },
    ).to_dict()
    git = provenance["code"]["git"]
    if git["dirty"] is not False or git["commit"] is None:
        raise RealAutonomyE2EError(
            "release_worktree_not_clean",
            "real release evidence requires a clean Git commit before any provider call",
        )
    return provenance


def _reserve_release_report_target(
    output_path: str | Path,
    *,
    chapter_count: int,
) -> Path:
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    reservation: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "real_autonomy_e2e_report_reservation",
        "redacted": True,
        "ok": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_chapters": _validate_gate_count(chapter_count),
        "reservation_hash": "0" * 64,
    }
    reservation["reservation_hash"] = canonical_hash(
        reservation, exclude_fields=("reservation_hash",)
    )
    try:
        atomic_create_json(target, reservation)
    except OSError as exc:
        raise RealAutonomyE2EError(
            "release_report_target_unavailable",
            "--out must name a new, writable report target",
        ) from exc
    return target


def _require_release_report_reservation(target: Path, *, chapter_count: int) -> None:
    try:
        reservation = load_json_object(target)
    except (OSError, ValueError) as exc:
        raise RealAutonomyE2EError(
            "release_report_reservation_changed",
            "the pre-provider report reservation is unreadable",
        ) from exc
    expected_hash = canonical_hash(
        reservation, exclude_fields=("reservation_hash",)
    )
    if (
        reservation.get("kind") != "real_autonomy_e2e_report_reservation"
        or reservation.get("requested_chapters") != chapter_count
        or reservation.get("reservation_hash") != expected_hash
    ):
        raise RealAutonomyE2EError(
            "release_report_reservation_changed",
            "the pre-provider report reservation was modified",
        )


def _trusted_profiles_payload(
    *,
    book_id: str,
    delivery_root_uuid: str,
    chapter_count: int,
    model: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "profile_set_id": "real-autonomy-release-v1",
        "story_projects": [
            {
                "profile_id": "generated-release-book",
                "book_id": book_id,
                "root_uuid": "generated-release-story-root",
            }
        ],
        "provider_models": [
            {
                "profile_id": "official-openai",
                "provider": "openai",
                "endpoint_type": "official",
                "model": model,
                "max_output_tokens": int(max_output_tokens),
            }
        ],
        "file_deliveries": [
            {
                "profile_id": "required-release-export",
                "target_kind": "file",
                "root_uuid": delivery_root_uuid,
                "path_template": "exports/chapter-{chapter_index}-{run_id}.json",
                "requires_run_id": True,
                "requires_chapter_id": True,
            }
        ],
        "budgets": [
            {
                "profile_id": "release-bounded",
                "max_chapters": chapter_count,
                "max_model_calls": chapter_count * 10,
                "max_input_tokens": chapter_count * 1_000_000,
                "max_output_tokens": chapter_count * 80_000,
                "max_wall_seconds": min(43_200, max(3_600, chapter_count * 900)),
            }
        ],
        "quality_policies": [
            {
                "profile_id": "release-strict",
                "policy": "strict",
                "minimum_score": 0,
            }
        ],
        "defaults": {
            "story_project": "generated-release-book",
            "provider_model": "official-openai",
            "file_delivery": "required-release-export",
            "budget": "release-bounded",
            "quality_policy": "release-strict",
        },
    }


def _bootstrap_generated_story_project(book: Path) -> tuple[Any, dict[str, Any]]:
    for directory in CORE_DIRECTORY_NAMES:
        (book / directory).mkdir(parents=True, exist_ok=True)
    book_id = f"release-e2e-{uuid.uuid4().hex}"
    identity = ensure_project_identity(book, book_id=book_id)
    paths = RuntimePaths.for_story_project(book)
    memory_root = paths.memory_dir / "v2"
    genesis = create_genesis_memory_batch(
        book_id=identity.book_id,
        title="Isolated Redacted Autonomy Release Gate",
        source_project_digest=canonical_hash(
            {"kind": "generated_release_fixture", "version": "1.0", "book_id": identity.book_id}
        ),
        context_digest=canonical_hash(
            {"kind": "generated_release_context", "language": "zh-CN", "version": "1.0"}
        ),
        language="zh-CN",
        authority_epoch=1,
    )
    projection = apply_genesis_event(genesis["events"][0])
    write_memory_event_batch(memory_root / "events", genesis)
    save_canonical_memory(memory_root / "canonical_memory.json", projection)
    identity = activate_event_authority(
        book,
        expected_identity_sha256=project_identity_sha256(book),
        head_event_hash=projection["head_event_hash"],
    )
    atomic_write_json(paths.snapshot_path, canonical_memory_to_snapshot(projection))
    return identity, genesis


def _verify_release_run(
    *,
    chapter_count: int,
    execution: Mapping[str, Any],
    plan: Mapping[str, Any],
    profiles: TrustedProfiles,
    sessions: AutonomySessionStore,
    runner: Any,
    paths: RuntimePaths,
    book: Path,
    delivery_root: Path,
    delivery_root_uuid: str,
    publication_roots: Mapping[str, Path],
    input_manifest_hash: str,
    initial_head_event_hash: str,
    model: str,
    release_provenance: Mapping[str, Any],
    harness_sha256: str,
    proxy_mode: str,
) -> dict[str, Any]:
    _require(
        execution.get("stopped_reason") == "completed",
        "autonomy_session_incomplete",
        "runner did not reach its completed boundary",
    )
    session = execution.get("session")
    _require(isinstance(session, Mapping), "autonomy_session_invalid", "runner returned no session")
    session_id = str(session.get("session_id") or "")
    _require(
        session.get("state") == "completed"
        and int(session.get("completed_count") or 0) == chapter_count
        and not session.get("delivery_blocked"),
        "autonomy_session_incomplete",
        "session state, count, or delivery boundary is incomplete",
    )

    expected_chapters = list(range(int(plan["chapter_start"]), int(plan["chapter_end"]) + 1))
    _require(
        expected_chapters == list(range(1, chapter_count + 1)),
        "generated_range_invalid",
        "generated release fixture did not begin at chapter one",
    )
    ledger = sessions.completion_ledger(session_id)
    completions = ledger.rebuild()
    _require(
        [item["chapter_index"] for item in completions] == expected_chapters,
        "completion_receipt_gap",
        "completion receipts are not contiguous",
    )

    event_root = paths.memory_dir / "v2" / "events"
    batches = load_memory_event_batches(event_root)
    chapter_batches = [item for item in batches if item.get("batch_kind") == "chapter"]
    _require(
        len(chapter_batches) == chapter_count,
        "event_batch_count_mismatch",
        "Memory Event batch count differs from the requested chapter count",
    )
    by_source = {str(item["patch"]["source"]["path"]): item for item in chapter_batches}
    _require(
        set(by_source) == {f"chapter:{chapter}" for chapter in expected_chapters},
        "event_batch_gap",
        "Memory Event batches skip or duplicate a chapter",
    )

    run_ids = {
        int(item["chapter_index"]): str(item["run_id"])
        for item in execution.get("runs", [])
        if isinstance(item, Mapping)
    }
    _require(
        set(run_ids) == set(expected_chapters),
        "run_result_gap",
        "runner results skip or duplicate a chapter",
    )
    run_records = _load_run_records(paths.run_dir)
    attempts_by_chapter: dict[int, int] = {chapter: 0 for chapter in expected_chapters}
    system_failures = 0
    for record in run_records:
        chapter = record.get("chapter_index")
        if chapter in attempts_by_chapter:
            attempts_by_chapter[int(chapter)] += 1
            if record.get("status") != "committed" or not record.get("committed"):
                system_failures += 1
    _require(
        all(value >= 1 for value in attempts_by_chapter.values()) and system_failures == 0,
        "system_failure_detected",
        "one or more chapter attempts did not commit",
    )

    runtime_delivery_profile = profiles.file_delivery_runtime_profile(
        "required-release-export", book_id=str(plan["source_snapshot"]["book_id"])
    )
    delivery_resolver = SafePathResolver(
        {
            runtime_delivery_profile["root_id"]: RootBinding(
                root_id=runtime_delivery_profile["root_id"],
                root_uuid=delivery_root_uuid,
                path=delivery_root,
            )
        }
    )
    queue = DeliveryQueue(paths.delivery_dir)
    previous_head = initial_head_event_hash
    previous_batch_hash = batches[0]["batch_hash"]
    expected_markdown: set[str] = set()
    chapter_reports: list[dict[str, Any]] = []
    stage_model_receipt_hashes: set[str] = set()
    provider_physical_attempts_from_trace = 0
    provider_transport_retries = 0
    quality_repairs = 0
    first_pass_chapters = 0
    execution_provenance_hashes: set[str] = set()
    execution_code_bundle_hashes: set[str] = set()
    execution_git_commits: set[str] = set()

    for chapter, completion in zip(expected_chapters, completions):
        checkpoint = runner.outlines.load(session_id, chapter)
        _require(checkpoint is not None, "outline_checkpoint_missing", "outline checkpoint is absent")
        checkpoint = validate_outline_checkpoint(checkpoint)
        outline_path = canonical_outline_path(book, chapter)
        _require(outline_path.is_file(), "canonical_outline_missing", "canonical outline is absent")
        expected_outline = str(checkpoint["outline_text"])
        if not expected_outline.endswith("\n"):
            expected_outline += "\n"
        _require(
            outline_path.read_bytes() == expected_outline.encode("utf-8"),
            "canonical_outline_hash_mismatch",
            "canonical outline bytes differ from the immutable checkpoint",
        )
        expected_markdown.add(outline_path.relative_to(book).as_posix())

        prose_resolution = resolve_prose(book, chapter)
        _require(
            prose_resolution.path is not None and len(prose_resolution.candidates) == 1,
            "canonical_prose_gap",
            "canonical prose is absent or ambiguous",
        )
        prose_path = prose_resolution.path
        assert prose_path is not None
        prose_bytes = prose_path.read_bytes()
        prose_hash = hashlib.sha256(prose_bytes).hexdigest()
        prose_text = prose_bytes.decode("utf-8")
        prose_chars = sum(1 for character in prose_text if not character.isspace())
        _require(
            3_000 <= prose_chars <= 4_500,
            "chapter_length_gate_failed",
            (
                "canonical prose is outside the 3000-4500 non-whitespace "
                f"character release range (observed {prose_chars})"
            ),
        )
        _require(
            prose_hash == completion["chapter_body_hash"],
            "chapter_body_hash_mismatch",
            "canonical prose bytes differ from the completion receipt",
        )
        expected_markdown.add(prose_path.relative_to(book).as_posix())

        batch = by_source[f"chapter:{chapter}"]
        _require(
            batch["previous_batch_hash"] == previous_batch_hash,
            "event_batch_chain_broken",
            "Memory Event batch predecessor is not continuous",
        )
        _require(
            batch["events"][0]["precondition"]["expected_head_event_hash"] == previous_head,
            "event_authority_head_broken",
            "chapter Event chain does not descend from the previous authority head",
        )
        _require(
            all(item["chapter_body_sha256"] == prose_hash for item in batch["events"]),
            "event_body_hash_mismatch",
            "Memory Events are not bound to exact canonical prose bytes",
        )
        head = batch["events"][-1]["event_hash"]
        _require(
            completion["source_snapshot_after"]["authority_head_event_hash"] == head
            and completion["source_snapshot_after"]["canonical_next_chapter"] == chapter + 1,
            "completion_authority_mismatch",
            "completion receipt does not bind the chapter Event head and next chapter",
        )

        chain = sessions.stage_receipts.load_chain(session_id, chapter)
        stages = [item["stage"] for item in chain]
        _require(
            stages[:4] == ["outline", "scene_plan", "draft", "polish"]
            and stages[-1] == "validator",
            "stage_receipt_chain_invalid",
            "required outline/draft/polish/validator StageReceipt chain is incomplete",
        )
        for item in chain:
            for digest in item.get("model_call_receipt_hashes", []):
                stage_model_receipt_hashes.add(str(digest))

        publication = ledger.load_publication(completion["publication_receipt_hash"])
        verification = verify_publication_receipt(publication, root_map=publication_roots)
        _require(
            verification.get("valid") and verification.get("committed"),
            "publication_receipt_invalid",
            "PublicationReceipt is not durably committed",
        )
        _require(
            publication["run_id"] == run_ids[chapter],
            "publication_scope_mismatch",
            "PublicationReceipt belongs to another run or chapter",
        )
        _verify_single_apply_target(
            publication,
            kind="outline",
            expected_path=outline_path,
            root_map=publication_roots,
        )
        _verify_single_apply_target(
            publication,
            kind="prose",
            expected_path=prose_path,
            root_map=publication_roots,
        )
        _verify_single_apply_target(
            publication,
            kind="memory_event_batch",
            expected_path=event_root / "batches" / f"{batch['batch_id']}.json",
            root_map=publication_roots,
        )
        outline_evidence = _single_publication_artifact(
            publication, "autonomy_outline_evidence", publication_roots
        )
        stage_evidence = _single_publication_artifact(
            publication, "autonomy_stage_evidence", publication_roots
        )
        _require(
            validate_outline_checkpoint(outline_evidence) == checkpoint,
            "published_outline_evidence_mismatch",
            "published outline evidence differs from the immutable checkpoint",
        )
        stage_evidence = validate_schema(stage_evidence, "autonomy_chapter_evidence.schema.json")
        _require(
            stage_evidence["evidence_hash"]
            == canonical_hash(stage_evidence, exclude_fields=("evidence_hash",))
            and stage_evidence["chapter_body_sha256"] == prose_hash
            and stage_evidence["final_stage_receipt_hash"]
            == completion["final_stage_receipt_hash"],
            "published_stage_evidence_mismatch",
            "published stage evidence does not bind exact prose and the final StageReceipt",
        )

        bindings = publication["delivery_jobs"]
        _require(
            len(bindings) == 1
            and bindings[0]["policy"] == {"required": True, "target": "file"},
            "required_file_delivery_missing",
            "PublicationReceipt does not bind exactly one required File Delivery",
        )
        job = queue.load(str(bindings[0]["id"]))
        _require(
            job["state"] == "succeeded"
            and job["target_type"] == "file"
            and job["policy"] == "required"
            and job["publication_receipt_hash"] == publication["receipt_hash"],
            "required_file_delivery_failed",
            "required File Delivery did not reach verified success",
        )
        delivered = delivery_resolver.resolve(job["target"]["path_ref"]).path
        expected_delivery_bytes = str(job["payload"]["content"]).encode("utf-8")
        _require(
            delivered.is_file() and delivered.read_bytes() == expected_delivery_bytes,
            "file_delivery_readback_mismatch",
            "File Delivery readback differs from the durable job payload",
        )
        delivered_payload = json.loads(delivered.read_text(encoding="utf-8"))
        _require(
            delivered_payload["chapter_index"] == chapter
            and delivered_payload["event_batch_hash"] == batch["batch_hash"]
            and delivered_payload["event_batch"] == batch
            and delivered_payload["chapter_body_sha256"] == prose_hash,
            "file_delivery_binding_mismatch",
            "File Delivery does not bind the exact Event batch and prose hash",
        )

        run_wrapper = load_json_object(paths.run_dir / f"{run_ids[chapter]}.json")
        run = run_wrapper["run"]
        provenance = _load_run_execution_provenance(run, run_dir=paths.run_dir)
        release_code = release_provenance["code"]
        provenance_git = provenance["code"]["git"]
        provenance_config = {
            str(item["name"]): item["value"] for item in provenance["config"]
        }
        provenance_flags = {
            str(item["name"]): item["enabled"]
            for item in provenance["feature_flags"]
        }
        _require(
            provenance_git["dirty"] is False
            and provenance_git["commit"] == release_code["git"]["commit"]
            and provenance["code"]["bundle_hash"] == release_code["bundle_hash"],
            "execution_provenance_release_mismatch",
            "chapter execution provenance is dirty or differs from the pre-provider release identity",
        )
        _require(
            provenance["model"] == {"provider": "openai", "model": model}
            and isinstance(provenance_config.get("configured_models"), Mapping)
            and provenance_config["configured_models"].get("openai") == model
            and provenance_flags.get("llm_validator") is True,
            "execution_provenance_provider_mismatch",
            "chapter execution provenance does not bind the release provider, model, and validator",
        )
        execution_provenance_hashes.add(str(provenance["provenance_hash"]))
        execution_code_bundle_hashes.add(str(provenance["code"]["bundle_hash"]))
        execution_git_commits.add(str(provenance_git["commit"]))
        quality_decision = run["quality_decision"]
        required_strict_policy = resolve_quality_policy("strict").to_dict()
        _require(
            run["status"] == "committed"
            and run["committed"] is True
            and run["accepted"] is True
            and quality_decision["policy"] == required_strict_policy
            and quality_decision["accepted"] is True
            and quality_decision["llm_validator"]["required"] is True
            and quality_decision["llm_validator"]["available"] is True
            and {
                "base_validation",
                "blueprint_coverage",
                "deterministic_review",
                "narrative_rules",
                "llm_validator",
            }.issubset(set(quality_decision["producers"]))
            and "llm" in run["validation"]["executed_checks"],
            "strict_quality_gate_missing",
            "strict Validator evidence is absent or did not accept the chapter",
        )
        repairs = int(run.get("repair_attempts") or 0)
        quality_repairs += repairs
        if repairs == 0:
            first_pass_chapters += 1
        chapter_provider_attempts, chapter_provider_retries = _provider_attempt_stats(run)
        provider_physical_attempts_from_trace += chapter_provider_attempts
        provider_transport_retries += chapter_provider_retries

        chapter_reports.append(
            {
                "chapter_index": chapter,
                "run_id_sha256": _identifier_hash(run_ids[chapter]),
                "outline_sha256": hashlib.sha256(outline_path.read_bytes()).hexdigest(),
                "prose_sha256": prose_hash,
                "prose_chars": prose_chars,
                "event_batch_hash": batch["batch_hash"],
                "authority_head_event_hash": head,
                "publication_receipt_hash": publication["receipt_hash"],
                "completion_receipt_hash": completion["receipt_hash"],
                "delivery_sha256": hashlib.sha256(delivered.read_bytes()).hexdigest(),
                "model_call_count": len(
                    {
                        digest
                        for item in chain
                        for digest in item.get("model_call_receipt_hashes", [])
                    }
                ),
                "provider_transport_retries": chapter_provider_retries,
                "quality_repair_attempts": repairs,
                "quality_accepted": True,
                "status": "committed",
            }
        )
        previous_head = head
        previous_batch_hash = batch["batch_hash"]

    _require(
        _source_markdown_paths(book) == expected_markdown,
        "manual_source_edit_detected",
        "generated StoryProject contains an unreceipted source Markdown change",
    )
    delivered_files = [item for item in delivery_root.rglob("*.json") if item.is_file()]
    _require(
        len(delivered_files) == chapter_count,
        "file_delivery_count_mismatch",
        "File Delivery root contains a gap or unexpected JSON artifact",
    )

    replay = replay_memory_events(event_root)
    final_identity = load_project_identity(book)
    _require(final_identity is not None, "project_identity_missing", "ProjectIdentity is absent")
    final_authority = final_identity.authority or {}
    _require(
        replay["committed_chapter_count"] == chapter_count
        and replay["projection"]["head_event_hash"] == previous_head
        and final_authority.get("head_event_hash") == previous_head
        and infer_next_chapter(book) == chapter_count + 1,
        "final_authority_mismatch",
        "replay, ProjectIdentity, and canonical source do not share one final head",
    )

    model_stats = _model_call_stats(paths.run_dir)
    _require(
        model_stats["receipt_hashes"] == stage_model_receipt_hashes,
        "model_receipt_evidence_gap",
        "durable ModelCallReceipts and StageReceipt evidence differ",
    )
    _require(
        model_stats["intent_count"] == model_stats["receipt_count"]
        and model_stats["receipt_count"] > 0,
        "provider_attempt_uncertain",
        "a provider Intent lacks a successful durable Receipt",
    )
    _require(
        provider_physical_attempts_from_trace == model_stats["intent_count"],
        "provider_attempt_accounting_mismatch",
        "provider retry telemetry and durable attempt evidence differ",
    )
    _require(
        model_stats["providers"] == {"openai"}
        and model_stats["receipt_statuses"] == {"succeeded"}
        and model_stats["endpoint_types"] == {"official"}
        and len(model_stats["actual_models"]) >= 1
        and model_stats["receipt_count"] == model_stats["actual_model_count"],
        "provider_receipt_identity_mismatch",
        "durable provider receipts do not prove successful official OpenAI calls with an actual model",
    )

    logical_attempt_values = list(attempts_by_chapter.values())
    logical_median = float(statistics.median(logical_attempt_values))
    _require(
        logical_median <= 2.0,
        "logical_attempt_slo_failed",
        "median logical chapter attempts exceeds two",
    )
    _require(
        len(execution_provenance_hashes) == 1
        and len(execution_code_bundle_hashes) == 1
        and len(execution_git_commits) == 1,
        "execution_provenance_inconsistent",
        "chapters were not produced by one stable execution provenance",
    )
    report = {
        "schema_version": "1.1",
        "kind": "real_autonomy_e2e",
        "redacted": True,
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate": {
            "requested_chapters": chapter_count,
            "tier": _gate_tier(chapter_count),
            "proxy_mode": _validate_release_proxy_mode_value(proxy_mode),
            "source_mode": "generated_isolated_temporary_story_project",
            "strict_validator_enabled": True,
            "required_file_delivery_enabled": True,
            "notion_writes": False,
            "manual_source_edits": 0,
        },
        "provider": {
            "name": "openai",
            "endpoint_type": "official",
            "model_sha256": _identifier_hash(model),
            "actual_model_hashes": sorted(
                _identifier_hash(item) for item in model_stats["actual_models"]
            ),
        },
        "identity": {
            "book_id_sha256": _identifier_hash(str(plan["source_snapshot"]["book_id"])),
            "session_id_sha256": _identifier_hash(session_id),
            "plan_hash": str(plan["plan_hash"]),
            "input_manifest_hash": input_manifest_hash,
        },
        "provenance": {
            "execution_provenance_hash": next(iter(execution_provenance_hashes)),
            "code_bundle_hash": next(iter(execution_code_bundle_hashes)),
            "git_commit": next(iter(execution_git_commits)),
            "git_dirty": False,
            "harness_sha256": sha256_digest("harness_sha256", harness_sha256),
        },
        "counts": {
            "outlines": chapter_count,
            "prose_chapters": chapter_count,
            "event_batches": chapter_count,
            "publication_receipts": chapter_count,
            "completion_receipts": chapter_count,
            "required_file_deliveries": chapter_count,
        },
        "authority": {
            "initial_head_event_hash": initial_head_event_hash,
            "final_head_event_hash": previous_head,
            "final_revision": int(replay["revision"]),
            "committed_chapter_count": int(replay["committed_chapter_count"]),
            "continuous": True,
        },
        "slo": {
            "logical_chapter_attempts": sum(logical_attempt_values),
            "logical_attempts_median_per_chapter": logical_median,
            "logical_attempts_median_limit": 2.0,
            "logical_model_calls": model_stats["logical_call_count"],
            "provider_physical_attempts": model_stats["intent_count"],
            "provider_transport_retries": provider_transport_retries,
            "provider_retry_rate": _rate(
                provider_transport_retries, model_stats["intent_count"]
            ),
            "quality_repair_attempts": quality_repairs,
            "first_pass_chapters": first_pass_chapters,
            "first_pass_rate": _rate(first_pass_chapters, chapter_count),
            "system_failures": 0,
            "context_budget_errors": 0,
            "internal_value_errors": 0,
        },
        "chapters": chapter_reports,
        "evidence": {
            "outline_and_prose_same_publication": True,
            "exact_body_hashes": True,
            "continuous_event_authority": True,
            "publication_receipts_verified": True,
            "completion_receipts_contiguous": True,
            "required_file_deliveries_read_back": True,
            "strict_quality_gate_verified": True,
            "no_unreceipted_source_edits": True,
            "notion_disabled": True,
            "execution_provenance_verified": True,
        },
        "report_hash": "0" * 64,
    }
    report["report_hash"] = canonical_hash(report, exclude_fields=("report_hash",))
    return _validate_release_report(report, secrets=(os.environ.get("OPENAI_API_KEY", ""),))


def _single_publication_artifact(
    publication: Mapping[str, Any],
    kind: str,
    root_map: Mapping[str, Path],
) -> dict[str, Any]:
    bindings = [item for item in publication["artifacts"] if item.get("kind") == kind]
    _require(len(bindings) == 1, "publication_artifact_gap", f"expected one {kind} artifact")
    binding = bindings[0]
    path = resolve_path_ref(binding["path_ref"], root_map)
    _require(path.is_file(), "publication_artifact_missing", f"{kind} artifact is absent")
    _require(
        hashlib.sha256(path.read_bytes()).hexdigest() == binding["sha256"],
        "publication_artifact_hash_mismatch",
        f"{kind} artifact readback hash differs",
    )
    return load_json_object(path)


def _verify_single_apply_target(
    publication: Mapping[str, Any],
    *,
    kind: str,
    expected_path: Path,
    root_map: Mapping[str, Path],
) -> None:
    bindings = [
        item for item in publication["apply_targets"] if item.get("kind") == kind
    ]
    _require(
        len(bindings) == 1,
        "publication_apply_target_gap",
        f"PublicationReceipt must bind exactly one {kind} apply target",
    )
    binding = bindings[0]
    bound_path = resolve_path_ref(binding["path_ref"], root_map)
    expected = expected_path.resolve()
    _require(
        bound_path == expected and expected.is_file(),
        "publication_apply_target_mismatch",
        f"PublicationReceipt {kind} target points at another file",
    )
    raw = expected.read_bytes()
    _require(
        hashlib.sha256(raw).hexdigest() == binding["sha256"]
        and len(raw) == int(binding["size"]),
        "publication_apply_target_hash_mismatch",
        f"PublicationReceipt {kind} target differs from committed bytes",
    )


def _load_run_records(run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*.json")) if run_dir.is_dir() else []:
        payload = load_json_object(path)
        run = payload.get("run") if isinstance(payload, Mapping) else None
        if isinstance(run, Mapping):
            records.append(dict(run))
    return records


def _load_run_execution_provenance(
    run: Mapping[str, Any],
    *,
    run_dir: Path,
) -> dict[str, Any]:
    evidence = run.get("execution_evidence")
    _require(
        isinstance(evidence, Mapping),
        "execution_provenance_missing",
        "committed run has no execution evidence",
    )
    assert isinstance(evidence, Mapping)
    raw_ref = evidence.get("provenance_artifact_ref")
    _require(
        isinstance(raw_ref, str) and bool(raw_ref),
        "execution_provenance_missing",
        "committed run has no provenance artifact reference",
    )
    logical = PurePosixPath(str(raw_ref))
    _require(
        not logical.is_absolute()
        and ".." not in logical.parts
        and len(logical.parts) == 3
        and logical.parts[0] == "executions"
        and logical.parts[-1] == "provenance.json",
        "execution_provenance_ref_invalid",
        "execution provenance reference escaped its controlled run root",
    )
    path = run_dir.joinpath(*logical.parts).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RealAutonomyE2EError(
            "execution_provenance_ref_invalid",
            "execution provenance reference escaped its controlled run root",
        ) from exc
    provenance = validate_execution_provenance(load_json_object(path))
    _require(
        evidence.get("provenance_hash") == provenance["provenance_hash"],
        "execution_provenance_hash_mismatch",
        "run evidence does not bind its execution provenance artifact",
    )
    return provenance


def _provider_attempt_stats(run: Mapping[str, Any]) -> tuple[int, int]:
    attempts = 0
    retries = 0
    for event in run.get("trace", []):
        if not isinstance(event, Mapping):
            continue
        for report in event.get("provider_attempts", []):
            if not isinstance(report, Mapping):
                continue
            value = int(report.get("attempts") or len(report.get("history") or []) or 0)
            attempts += value
            retries += max(0, value - 1)
    return attempts, retries


def _model_call_stats(run_dir: Path) -> dict[str, Any]:
    intents = []
    receipts = []
    for path in sorted(run_dir.glob("executions/*/model_calls/intents/*.json")):
        intents.append(load_model_call_intent(path))
    for path in sorted(run_dir.glob("executions/*/model_calls/receipts/*.json")):
        receipts.append(load_model_call_receipt(path))
    return {
        "intent_count": len(intents),
        "receipt_count": len(receipts),
        "logical_call_count": len({item["call_id"] for item in intents}),
        "receipt_hashes": {str(item["receipt_hash"]) for item in receipts},
        "providers": {str(item["provider"]) for item in intents},
        "receipt_statuses": {str(item["status"]) for item in receipts},
        "endpoint_types": {str(item["endpoint_type"]) for item in receipts},
        "actual_models": {
            str(item["actual_model"])
            for item in receipts
            if isinstance(item.get("actual_model"), str)
            and bool(str(item["actual_model"]).strip())
        },
        "actual_model_count": sum(
            isinstance(item.get("actual_model"), str)
            and bool(str(item["actual_model"]).strip())
            for item in receipts
        ),
    }


def _source_markdown_paths(book: Path) -> set[str]:
    paths: set[str] = set()
    for path in book.rglob("*.md"):
        relative = path.relative_to(book)
        if relative.parts and relative.parts[0] in {".novelagent", ".git"}:
            continue
        _require(
            path.is_file() and not path.is_symlink(),
            "manual_source_edit_detected",
            "source Markdown must be a regular file",
        )
        paths.add(relative.as_posix())
    return paths


def _tree_manifest_hash(root: Path) -> str:
    manifest = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        manifest.append(
            {"relative_path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    return canonical_hash({"files": manifest})


def _remove_isolated_tree(path: Path, *, parent: Path) -> None:
    resolved = path.resolve()
    resolved_parent = parent.resolve()
    try:
        relative = resolved.relative_to(resolved_parent)
    except ValueError as exc:
        raise RealAutonomyE2EError(
            "isolated_cleanup_scope_invalid", "generated gate directory escaped its work parent"
        ) from exc
    if len(relative.parts) != 1 or not relative.name.startswith("g"):
        raise RealAutonomyE2EError(
            "isolated_cleanup_scope_invalid", "refusing to remove a non-gate directory"
        )
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        raise RealAutonomyE2EError(
            "isolated_cleanup_failed", "generated StoryProject could not be removed safely"
        ) from exc


def _validate_release_report(
    value: Mapping[str, Any], *, secrets: tuple[str, ...] = ()
) -> dict[str, Any]:
    report = validate_schema(dict(value), REPORT_SCHEMA)
    requested = _validate_gate_count(int(report["gate"]["requested_chapters"]))
    if report["gate"]["tier"] != _gate_tier(requested):
        raise RealAutonomyE2EError(
            "release_report_gate_mismatch", "release tier differs from its requested chapter count"
        )
    actual_model_hashes = list(report["provider"]["actual_model_hashes"])
    if actual_model_hashes != sorted(set(actual_model_hashes)):
        raise RealAutonomyE2EError(
            "release_report_provider_mismatch",
            "actual provider model hashes must be sorted and unique",
        )
    _assert_report_hash_fields(report)
    expected = canonical_hash(report, exclude_fields=("report_hash",))
    if report["report_hash"] != expected:
        raise RealAutonomyE2EError(
            "release_report_hash_mismatch", "redacted release report was modified"
        )
    _assert_redacted_report(report, secrets=secrets)
    if len(report["chapters"]) != requested:
        raise RealAutonomyE2EError(
            "release_report_count_mismatch", "chapter evidence count differs from the gate"
        )
    if any(int(value) != requested for value in report["counts"].values()) or int(
        report["authority"]["committed_chapter_count"]
    ) != requested:
        raise RealAutonomyE2EError(
            "release_report_count_mismatch", "artifact counts differ from the gate"
        )
    expected = list(range(1, requested + 1))
    if [item["chapter_index"] for item in report["chapters"]] != expected:
        raise RealAutonomyE2EError(
            "release_report_chapter_gap", "redacted chapter evidence is not contiguous"
        )
    return report


def _assert_report_hash_fields(value: Any) -> None:
    hexadecimal = frozenset("0123456789abcdef")

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key.endswith("_hash") or key.endswith("_sha256"):
                    if (
                        not isinstance(child, str)
                        or len(child) != 64
                        or any(character not in hexadecimal for character in child)
                    ):
                        raise RealAutonomyE2EError(
                            "release_report_digest_invalid", f"{key} is not a lowercase SHA-256 digest"
                        )
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)


def _assert_redacted_report(value: Any, *, secrets: tuple[str, ...] = ()) -> None:
    forbidden_keys = {
        "api_key",
        "authorization",
        "credential",
        "content",
        "messages",
        "model",
        "prompt",
        "request_id",
        "response",
        "run_id",
        "session_id",
        "book_id",
        "story_project_root",
    }

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).lower()
                if key in forbidden_keys or key.endswith("_path") or "prompt" in key:
                    raise RealAutonomyE2EError(
                        "release_report_not_redacted", f"forbidden report field: {raw_key}"
                    )
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in serialized:
            raise RealAutonomyE2EError(
                "release_report_not_redacted", "a configured secret appears in the report"
            )


def _identifier_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _safe_failure_diagnostics(
    error: BaseException,
    *,
    phase: str | None = None,
    evidence_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return an allowlisted, message-free exception-chain summary."""

    chain: list[dict[str, Any]] = []
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending and len(chain) < 12:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        item: dict[str, Any] = {"exception_type": type(current).__name__}
        for attribute, output_name in (
            ("code", "error_code"),
            ("failure_category", "failure_category"),
            ("retry_stop_reason", "retry_stop_reason"),
            ("provider", "provider"),
            ("stage", "stage"),
        ):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,96}", value):
                item[output_name] = value
        status_code = getattr(current, "status_code", None)
        if (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
        ):
            item["http_status"] = status_code
        for attribute in ("retryable", "partial_content_received"):
            value = getattr(current, attribute, None)
            if isinstance(value, bool):
                item[attribute] = value
        attempts = getattr(current, "attempts", None)
        if isinstance(attempts, int) and not isinstance(attempts, bool) and 0 <= attempts <= 100:
            item["attempts"] = attempts
        elapsed_ms = getattr(current, "elapsed_ms", None)
        if (
            isinstance(elapsed_ms, int)
            and not isinstance(elapsed_ms, bool)
            and 0 <= elapsed_ms <= 86_400_000
        ):
            item["elapsed_ms"] = elapsed_ms
        chain.append(item)
        for linked in (
            current.__cause__,
            current.__context__,
            getattr(current, "cause", None),
        ):
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append(linked)
    payload: dict[str, Any] = {"exception_chain": chain}
    if phase is not None:
        payload["phase"] = phase
    payload.update(dict(evidence_counts or {}))
    return _validate_failure_diagnostics(payload)


def _merge_failure_diagnostics(
    diagnostics: Mapping[str, Any],
    *,
    phase: str,
    evidence_counts: Mapping[str, int],
) -> dict[str, Any]:
    merged = dict(diagnostics)
    merged["phase"] = phase
    merged.update(dict(evidence_counts))
    return _validate_failure_diagnostics(merged)


def _validate_failure_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("failure diagnostics must be an object")
    allowed_item_keys = {
        "exception_type",
        "error_code",
        "failure_category",
        "retry_stop_reason",
        "http_status",
        "retryable",
        "partial_content_received",
        "attempts",
        "elapsed_ms",
        "provider",
        "stage",
    }
    allowed_top_keys = {
        "exception_chain",
        "phase",
        "completed_chapters",
        "intent_count",
        "receipt_count",
        "uncertain_intent_count",
    }
    raw_chain = value.get("exception_chain", [])
    if set(value) - allowed_top_keys or not isinstance(raw_chain, list):
        raise ValueError("failure diagnostics contain unsupported fields")
    phase = value.get("phase")
    if phase is not None and phase not in {
        "gate_setup",
        "runner_execute",
        "release_verification",
        "cleanup",
    }:
        raise ValueError("failure diagnostic phase is invalid")
    counts: dict[str, int] = {}
    for field in (
        "completed_chapters",
        "intent_count",
        "receipt_count",
        "uncertain_intent_count",
    ):
        if field not in value:
            continue
        count = value[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"failure diagnostic {field} is invalid")
        counts[field] = count
    chain: list[dict[str, Any]] = []
    for raw_item in raw_chain[:12]:
        if not isinstance(raw_item, Mapping) or set(raw_item) - allowed_item_keys:
            raise ValueError("failure diagnostic entry contains unsupported fields")
        item = dict(raw_item)
        exception_type = item.get("exception_type")
        if not isinstance(exception_type, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,95}", exception_type
        ):
            raise ValueError("failure diagnostic exception type is invalid")
        for field in (
            "error_code",
            "failure_category",
            "retry_stop_reason",
            "provider",
            "stage",
        ):
            if field in item and (
                not isinstance(item[field], str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", item[field])
            ):
                raise ValueError(f"failure diagnostic {field} is invalid")
        if "http_status" in item and (
            isinstance(item["http_status"], bool)
            or not isinstance(item["http_status"], int)
            or not 100 <= item["http_status"] <= 599
        ):
            raise ValueError("failure diagnostic HTTP status is invalid")
        if "attempts" in item and (
            isinstance(item["attempts"], bool)
            or not isinstance(item["attempts"], int)
            or not 0 <= item["attempts"] <= 100
        ):
            raise ValueError("failure diagnostic attempts is invalid")
        if "elapsed_ms" in item and (
            isinstance(item["elapsed_ms"], bool)
            or not isinstance(item["elapsed_ms"], int)
            or not 0 <= item["elapsed_ms"] <= 86_400_000
        ):
            raise ValueError("failure diagnostic elapsed_ms is invalid")
        for field in ("retryable", "partial_content_received"):
            if field in item and not isinstance(item[field], bool):
                raise ValueError(f"failure diagnostic {field} is invalid")
        chain.append(item)
    result: dict[str, Any] = {"exception_chain": chain}
    if phase is not None:
        result["phase"] = phase
    result.update(counts)
    return result


def _failure_evidence_counts(
    paths: RuntimePaths | None,
    book: Path | None,
) -> dict[str, int]:
    intent_count = 0
    receipt_count = 0
    if paths is not None and paths.run_dir.is_dir():
        intent_count = len(
            list(paths.run_dir.glob("executions/*/model_calls/intents/*.json"))
        )
        receipt_count = len(
            list(paths.run_dir.glob("executions/*/model_calls/receipts/*.json"))
        )
    completed_chapters = 0
    if book is not None:
        prose_root = book / CORE_DIRECTORY_NAMES[2]
        if prose_root.is_dir():
            completed_chapters = len(
                [path for path in prose_root.glob("*.md") if path.is_file()]
            )
    return {
        "completed_chapters": completed_chapters,
        "intent_count": intent_count,
        "receipt_count": receipt_count,
        "uncertain_intent_count": max(0, intent_count - receipt_count),
    }


def _build_release_failure_report(
    *,
    chapter_count: int,
    error: RealAutonomyE2EError,
    proxy_mode: str = "unresolved",
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "real_autonomy_e2e_failure",
        "redacted": True,
        "ok": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate": {
            "requested_chapters": _validate_gate_count(chapter_count),
            "tier": _gate_tier(chapter_count),
            "endpoint_type": "official",
            "proxy_mode": _validate_release_proxy_mode_value(
                proxy_mode, allow_unresolved=True
            ),
            "notion_writes": False,
        },
        "error": error.code,
        "diagnostics": _validate_failure_diagnostics(error.diagnostics),
        "cleanup_completed": error.cleanup_completed,
        "report_hash": "0" * 64,
    }
    report["report_hash"] = canonical_hash(report, exclude_fields=("report_hash",))
    _assert_redacted_report(report, secrets=(os.environ.get("OPENAI_API_KEY", ""),))
    return report


def _publish_release_failure_report(
    target: Path,
    *,
    chapter_count: int,
    error: RealAutonomyE2EError,
    proxy_mode: str,
) -> dict[str, Any]:
    _require_release_report_reservation(target, chapter_count=chapter_count)
    report = _build_release_failure_report(
        chapter_count=chapter_count,
        error=error,
        proxy_mode=proxy_mode,
    )
    atomic_write_json(target, report)
    return report


def _rate(numerator: int, denominator: int) -> float:
    return round(float(numerator) / float(denominator), 6) if denominator else 0.0


def _gate_tier(chapter_count: int) -> str:
    if chapter_count == 1:
        return "single_chapter"
    if chapter_count == 4:
        return "four_chapter_canary"
    if chapter_count == 10:
        return "ten_chapter_unattended"
    return "long_run_20_plus"


def _validate_release_proxy_mode_value(
    value: Any,
    *,
    allow_unresolved: bool = False,
) -> str:
    allowed = {"direct", "inherited", "cleared"}
    if allow_unresolved:
        allowed.add("unresolved")
    if not isinstance(value, str) or value not in allowed:
        raise RealAutonomyE2EError(
            "release_proxy_mode_invalid",
            "release proxy mode evidence is invalid",
        )
    return value


def _require(condition: Any, code: str, message: str) -> None:
    if not condition:
        raise RealAutonomyE2EError(code, message)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a billable, generated-input, strict-validator autonomy release gate "
            "for 1, 4, 10, or at least 20 chapters."
        )
    )
    parser.add_argument("--chapters", required=True, type=int)
    parser.add_argument("--out", required=True, help="Destination for the redacted JSON report.")
    parser.add_argument("--confirm-real-provider-calls", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args: argparse.Namespace | None = None
    try:
        _assert_no_notion_configuration(argv=raw_argv)
        args = parse_args(raw_argv)
        report = run_real_autonomy_e2e(
            chapter_count=args.chapters,
            output_path=args.out,
            confirmed=args.confirm_real_provider_calls,
        )
    except RealAutonomyE2EError as exc:
        failure = {
            "ok": False,
            "redacted": True,
            "error": exc.code,
            "diagnostics": exc.diagnostics,
        }
        if args is not None and exc.retain_report:
            try:
                candidate = load_json_object(Path(args.out).resolve())
                if (
                    candidate.get("kind") == "real_autonomy_e2e_failure"
                    and candidate.get("error") == exc.code
                    and candidate.get("report_hash")
                    == canonical_hash(candidate, exclude_fields=("report_hash",))
                ):
                    _assert_redacted_report(
                        candidate,
                        secrets=(os.environ.get("OPENAI_API_KEY", ""),),
                    )
                    failure = candidate
            except Exception:
                failure["failure_report_read"] = "failed"
        print(
            json.dumps(failure, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"ok": False, "redacted": True, "error": "internal_failure"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
