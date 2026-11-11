from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

import pytest

from workbench.database import WorkbenchDatabase
from core.story_project.paths import canonical_prose_path
from core.model_calls import PROVIDER_PARTIAL_RESPONSE_FAILED
from workbench.jobs import (
    DraftSourceConflict,
    JobError,
    JobStore,
    JobSupervisor,
    PRE_PROVIDER_CONTEXT_BUDGET_ERROR,
    PRE_PROVIDER_PROVENANCE_ERROR,
    REPAIR_NOT_CONVERGING_ERROR,
    STALE_SCENE_EVIDENCE_ERROR,
    SCENE_BOUNDARY_VALIDATION_ERROR,
    SCENE_SCHEMA_VALIDATION_ERROR,
    BRIDGE_PREVALIDATION_ERROR,
    PROVIDER_PARTIAL_RESPONSE_ERROR,
    _classify_repair_not_converging_failure,
    _classify_stale_scene_evidence_failure,
    _classify_scene_boundary_validation_failure,
    _classify_scene_schema_validation_failure,
    _classify_bridge_prevalidation_failure,
    _classify_provider_partial_response_failure,
    _classify_pre_provider_context_failure,
    _classify_pre_provider_provenance_failure,
    _git_provenance_preflight,
    _provider_environment,
    _result_is_uncertain,
    verify_committed_chapter,
)
from workbench.projects import ProjectRegistry


def _job_store(tmp_path: Path) -> tuple[Path, JobStore, str]:
    workspace = tmp_path / "books"
    workspace.mkdir()
    database = WorkbenchDatabase(tmp_path / "workbench.db")
    registry = ProjectRegistry(database, allowed_workspaces=[workspace])
    project = registry.create_project(workspace / "test-book", name="测试作品")
    return tmp_path / "workbench.db", JobStore(database), project.id


def _running_job(store: JobStore, project_id: str, *, end_chapter: int = 2):
    queued = store.create_job(
        project_id=project_id,
        kind="generation_session",
        start_chapter=1,
        end_chapter=end_chapter,
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == queued.id
    return store.mark_running(
        queued.id,
        chapter=1,
        command=["python", "main.py", "--chapter", "1"],
    )


def _review_rejected_result(*, run_id: str = "run-review-rejected") -> dict:
    return {
        "run": {
            "id": run_id,
            "chapter_index": 1,
            "status": "rejected",
            "committed": False,
            "error": None,
            "trace": [
                {
                    "action": "validate",
                    "status": "completed",
                    "provider_attempts": [
                        {
                            "completed": True,
                            "stop_reason": "succeeded",
                            "history": [
                                {
                                    "attempt": 1,
                                    "outcome": "succeeded",
                                    "failure_category": None,
                                }
                            ],
                        }
                    ],
                }
            ],
            "execution_evidence": {"budget": {"unsettled_attempt_count": 0}},
        },
        "decision": {
            "notes": [
                "Previous error: OpenAI failed (provider_call_uncertain); retained for context."
            ]
        },
        "review_pipeline": {"status": "needs_revision"},
        "accepted": False,
        "committed": False,
    }



def _write_structured_character_budget_run(
    project_root: Path,
    *,
    chapter: int = 59,
    run_id: str = "run-structured-character-budget",
    error_message: str = "required_json_item_exceeds_budget: required entries exceed 1481 characters",
    provider_calls: int = 0,
    unsettled_attempt_count: int = 0,
    generated_chapter: str = "",
) -> tuple[dict, Path, str]:
    result = {
        "chapter": generated_chapter,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {"chars": len(generated_chapter)},
            "error": {"type": "StructuredContextError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                }
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result), encoding="utf-8")
    stderr = f"StructuredContextError: {error_message}"
    return result, run_path, stderr


def _write_context_budget_run(
    project_root: Path,
    *,
    chapter: int = 1,
    run_id: str = "run-context-budget",
    provider_calls: int = 0,
    unsettled_attempt_count: int = 0,
    generated_chapter: str = "",
) -> tuple[dict, Path, str]:
    error_message = (
        "story_project_context_budget_exceeded: scene input requires "
        "33212 tokens; hard limit is 32000"
    )
    result = {
        "chapter": generated_chapter,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {"chars": len(generated_chapter)},
            "error": {"type": "ContextBudgetError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                }
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result), encoding="utf-8")
    stderr = f"ContextBudgetError: {error_message}"
    return result, run_path, stderr


def _write_provenance_failure_artifacts(
    runtime_root: Path,
    job_id: str,
    *,
    chapter: int = 1,
) -> tuple[dict[str, Path], str]:
    directory = runtime_root / job_id / f"chapter_{chapter:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "directory": directory,
        "progress": directory / "progress.jsonl",
        "stdout": directory / "stdout.json",
        "stderr": directory / "stderr.log",
    }
    started_at = datetime.now(timezone.utc)
    failed_at = started_at.replace(microsecond=min(999999, started_at.microsecond + 1))
    progress = [
        {
            "schema_version": 1,
            "sequence": 1,
            "timestamp": started_at.isoformat(),
            "event": "loop_start",
            "stage": "loop",
            "payload": {"requested_steps": 1},
        },
        {
            "schema_version": 1,
            "sequence": 2,
            "timestamp": started_at.isoformat(),
            "event": "step_start",
            "stage": "chapter",
            "payload": {"step": 1, "requested_steps": 1},
        },
        {
            "schema_version": 1,
            "sequence": 3,
            "timestamp": failed_at.isoformat(),
            "event": "step_failed",
            "stage": "chapter",
            "payload": {
                "step": 1,
                "requested_steps": 1,
                "error_type": "ProvenanceCaptureError",
            },
        },
    ]
    paths["progress"].write_text(
        "".join(json.dumps(item) + "\n" for item in progress), encoding="utf-8"
    )
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "core/execution_provenance.py", line 668, in _capture_git_state\n'
        "    subprocess.run([\"git\", \"rev-parse\"])\n"
        "FileNotFoundError: [WinError 2] missing\n\n"
        "core.execution_provenance.ProvenanceCaptureError: "
        "Git metadata capture is unavailable\n"
    )
    paths["stdout"].write_bytes(b"")
    paths["stderr"].write_text(stderr, encoding="utf-8")
    return paths, stderr


def _write_repair_not_converging_run(
    project_root: Path,
    *,
    chapter: int = 1,
    run_id: str = "run-repair-not-converging",
    provider_calls: int = 2,
    unsettled_attempt_count: int = 0,
) -> tuple[dict, Path, Path, str]:
    candidate = "雨水沿着物流园围墙流下。陆沉确认路线后，带队进入泵房检修门。"
    candidate_sha256 = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    artifact_dir = project_root / ".novelagent" / "runtime" / "chapters"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"chapter_{chapter:04d}_failed_{run_id}.md"
    artifact_path.write_text(
        f"# Chapter {chapter}\n\n- Run: `{run_id}`\n- Status: `failed`\n\n---\n\n{candidate}\n",
        encoding="utf-8",
    )
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    error_message = (
        "chapter_repair_not_converging: repair validation did not converge; "
        "problem_counts=6 -> 2; reason=new_more_severe_problem; repeated_problems=none"
    )
    provider_attempts = [
        {
            "attempts": 1,
            "completed": True,
            "stop_reason": "succeeded",
            "history": [
                {
                    "attempt": 1,
                    "outcome": "succeeded",
                    "failure_category": None,
                    "partial_content_received": False,
                }
            ],
        }
        for _ in range(provider_calls)
    ]
    result = {
        "chapter": candidate,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {
                "chars": len(candidate),
                "artifact": {
                    "chars": len(candidate),
                    "format": "markdown",
                    "path": str(artifact_path),
                    "sha256": artifact_sha256,
                },
                "integrity_audit": {"final_chapter_sha256": candidate_sha256},
            },
            "error": {"type": "RepairNotConvergingError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                }
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "trace": [
                {
                    "action": "repair_if_needed",
                    "status": "failed",
                    "error_type": "RepairNotConvergingError",
                    "error_message": error_message,
                    "provider_attempts": provider_attempts,
                    "repair_deltas": [
                        {
                            "before_problem_count": 6,
                            "after_problem_count": 2,
                            "after_ok": False,
                            "after_problem_codes": [
                                "missing_opening_bridge",
                                "missing_last_scene_continuity",
                            ],
                            "new_problem_codes": [
                                "missing_last_scene_continuity",
                                "missing_opening_bridge",
                            ],
                        }
                    ],
                }
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    stderr = f"core.engine.executor.RepairNotConvergingError: {error_message}"
    return result, run_path, artifact_path, stderr


def test_result_uncertainty_ignores_historical_narrative_notes() -> None:
    assert _result_is_uncertain(_review_rejected_result()) is False


def test_result_uncertainty_requires_current_structured_evidence() -> None:
    attempt_failure = _review_rejected_result(run_id="run-attempt-uncertain")
    attempt_failure["run"]["trace"][0]["provider_attempts"][0]["history"][0][
        "failure_category"
    ] = "provider_call_uncertain"
    assert _result_is_uncertain(attempt_failure) is True

    unsettled = _review_rejected_result(run_id="run-unsettled")
    unsettled["run"]["execution_evidence"]["budget"]["unsettled_attempt_count"] = 1
    assert _result_is_uncertain(unsettled) is True


def test_provider_environment_merges_case_variants_and_drops_unrelated_secrets() -> None:
    environment = _provider_environment(
        {
            "Path": r"C:\Python;C:\Tools",
            "PATH": r"c:\tools;C:\Git\cmd",
            "Pathext": ".EXE;.BAT",
            "PATHEXT": ".bat;.CMD",
            "SystemRoot": r"C:\Windows",
            "OPENAI_API_KEY": "provider-key",
            "UNRELATED_SECRET": "must-not-leak",
            "DATABASE_PASSWORD": "must-not-leak-either",
        }
    )

    assert all(key == key.upper() for key in environment)
    assert environment["PATH"].split(";") == [r"C:\Python", r"C:\Tools", r"C:\Git\cmd"]
    assert environment["PATHEXT"].split(";") == [".EXE", ".BAT", ".CMD"]
    assert environment["SYSTEMROOT"] == r"C:\Windows"
    assert environment["OPENAI_API_KEY"] == "provider-key"
    assert "UNRELATED_SECRET" not in environment
    assert "DATABASE_PASSWORD" not in environment


def test_provider_environment_puts_trusted_absolute_git_first(tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files"
    git_executable = program_files / "Git" / "cmd" / "git.exe"
    git_executable.parent.mkdir(parents=True)
    git_executable.write_bytes(b"fake")

    environment = _provider_environment(
        {
            "Path": r"C:\Untrusted;C:\Python",
            "PROGRAMFILES": str(program_files),
            "OPENAI_API_KEY": "provider-key",
            "NOVELAGENT_GIT_EXECUTABLE": str(tmp_path / "untrusted" / "git.exe"),
        }
    )

    assert environment["NOVELAGENT_GIT_EXECUTABLE"] == str(git_executable.resolve())
    assert environment["PATH"].split(";")[0] == str(git_executable.resolve().parent)
    assert environment["OPENAI_API_KEY"] == "provider-key"


def test_git_preflight_uses_absolute_git_and_no_provider_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_files = tmp_path / "Program Files"
    git_executable = program_files / "Git" / "cmd" / "git.exe"
    git_executable.parent.mkdir(parents=True)
    git_executable.write_bytes(b"fake")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs["env"])))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-provider-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sentinel-anthropic-secret")
    monkeypatch.setattr("workbench.jobs.subprocess.run", fake_run)

    result = _git_provenance_preflight(tmp_path)

    assert result == {"ok": True, "dependency": "git"}
    assert len(calls) == 2
    for command, environment in calls:
        assert command[0] == str(git_executable.resolve())
        assert Path(command[0]).is_absolute()
        assert "OPENAI_API_KEY" not in environment
        assert "ANTHROPIC_AUTH_TOKEN" not in environment
        assert "NOVELAGENT_GIT_EXECUTABLE" not in environment


def test_subprocess_diagnostics_redact_provider_secrets_through_event_chain(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    running = _running_job(store, project_id)
    stderr = (
        "provider setup failed; harmless_context=kept\n"
        "OPENAI_API_KEY=sentinel-openai-value\n"
        "ANTHROPIC_AUTH_TOKEN='sentinel-anthropic-value'\n"
        "NOTION_TOKEN: sentinel-notion-value\n"
        '\"PROVIDER_ACCESS_TOKEN\": \"sentinel-json-value\"\n'
    )

    class FakeProcess:
        returncode = 1

        def communicate(self, timeout=None):
            return "", stderr

        def poll(self):
            return 1

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: None,
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
        process_factory=lambda *args, **kwargs: FakeProcess(),
    )
    paths = supervisor._job_paths(running.id, 1)
    execution = supervisor._execute_subprocess(
        running.id,
        1,
        ["fake-provider-command"],
        paths,
    )
    paused = store.chapter_failed(
        running.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": execution["exit_code"],
            "stderr_tail": execution["stderr_tail"],
        },
        uncertain=False,
    )

    persisted_log = paths["stderr"].read_text(encoding="utf-8")
    serialized_error = json.dumps(paused.error, ensure_ascii=False)
    serialized_events = json.dumps(store.events_after(running.id), ensure_ascii=False)
    for sentinel in (
        "sentinel-openai-value",
        "sentinel-anthropic-value",
        "sentinel-notion-value",
        "sentinel-json-value",
    ):
        assert sentinel not in execution["stderr_tail"]
        assert sentinel not in persisted_log
        assert sentinel not in serialized_error
        assert sentinel not in serialized_events
    assert "harmless_context=kept" in persisted_log
    assert persisted_log.count("<redacted>") == 4


def test_provenance_classifier_binds_zero_call_artifacts(tmp_path: Path) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    runtime_root = tmp_path / "job-runtime"
    paths, stderr = _write_provenance_failure_artifacts(runtime_root, "job-proof")

    classified = _classify_pre_provider_provenance_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        paths=paths,
        prior_run_fingerprints={},
        prior_model_call_fingerprints={},
    )

    assert classified is not None
    error = classified["error"]
    assert error["code"] == PRE_PROVIDER_PROVENANCE_ERROR
    assert error["automatic_retry"] is False
    assert error["details"]["dependency"] == "git"
    assert error["details"]["failure_origin"] == "engine_provenance_capture"
    assert error["details"]["provider_reached"] is False
    assert error["details"]["provider_calls"] == 0
    assert error["details"]["chapter_chars"] == 0
    assert error["details"]["committed"] is False

    paths["stdout"].write_text("unexpected output", encoding="utf-8")
    assert _classify_pre_provider_provenance_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        paths=paths,
        prior_run_fingerprints={},
        prior_model_call_fingerprints={},
    ) is None


def test_repair_not_converging_classifier_binds_settled_run_and_candidate(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    _, _, _, stderr = _write_repair_not_converging_run(project_root)

    classified = _classify_repair_not_converging_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    )

    assert classified is not None
    error = classified["error"]
    assert error["code"] == REPAIR_NOT_CONVERGING_ERROR
    assert error["automatic_retry"] is False
    assert error["safe_to_retry"] is False
    assert error["recovery_required"] is True
    details = error["details"]
    assert details["failure_origin"] == "chapter_repair_convergence"
    assert details["provider_calls"] == details["settled_provider_calls"] == 2
    assert details["unsettled_attempt_count"] == 0
    assert details["candidate_chars"] > 0
    assert details["formal_prose_exists"] is False
    assert details["accepted"] is details["committed"] is False
    assert details["writeback_attempted"] is details["writeback_applied"] is False
    assert details["original_problem_count"] == 6
    assert details["final_problem_count"] == 2
    assert details["convergence_reason"] == "new_more_severe_problem"
    assert details["new_problem_codes"] == [
        "missing_last_scene_continuity",
        "missing_opening_bridge",
    ]

    _write_repair_not_converging_run(
        project_root,
        run_id="second-ambiguous-repair-run",
    )
    assert _classify_repair_not_converging_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None


def test_repair_not_converging_classifier_fails_closed_on_tamper_unsettled_or_prose(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    result, run_path, artifact_path, stderr = _write_repair_not_converging_run(
        project_root
    )

    artifact_path.write_text("tampered retained candidate", encoding="utf-8")
    assert _classify_repair_not_converging_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None

    _, run_path, _, stderr = _write_repair_not_converging_run(project_root)
    result = json.loads(run_path.read_text(encoding="utf-8"))
    result["run"]["execution_evidence"]["budget"]["unsettled_attempt_count"] = 1
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    assert _classify_repair_not_converging_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None

    _, _, _, stderr = _write_repair_not_converging_run(project_root)
    prose_path = canonical_prose_path(project_root, 1, "正式正文")
    prose_path.parent.mkdir(parents=True, exist_ok=True)
    prose_path.write_text("不应存在的正式正文", encoding="utf-8")
    assert _classify_repair_not_converging_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None


def test_legacy_repair_not_converging_pause_reconciles_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, _, stderr = _write_repair_not_converging_run(project_root)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.result is None

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    supervisor._reconcile_repair_not_converging_failures()

    reconciled = store.get(parent.id)
    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == REPAIR_NOT_CONVERGING_ERROR
    assert reconciled.result is not None
    assert reconciled.result["run_id"] == "run-repair-not-converging"
    assert reconciled.result["provider_calls"] == 2
    assert store.claim_next() is None
    assert store.resume_ready(parent.id) is False
    assert store.events_after(parent.id)[-1]["event"] == "chapter.repair_not_converging"
    with pytest.raises(JobError, match="completed, receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)


def _write_stale_scene_evidence_run(
    project_root: Path,
    *,
    chapter: int = 1,
    run_id: str = "run-stale-scene-evidence",
    provider_calls: int = 6,
    unsettled_attempt_count: int = 0,
) -> tuple[dict, Path, Path, str]:
    candidate = "雨水沿着物流园围墙流下。陆沉确认路线后，带队进入泵房检修门。"
    candidate_sha256 = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    artifact_dir = project_root / ".novelagent" / "runtime" / "chapters"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"chapter_{chapter:04d}_failed_{run_id}.md"
    artifact_path.write_text(
        f"# Chapter {chapter}\n\n- Run: `{run_id}`\n- Status: `failed`\n\n---\n\n{candidate}\n",
        encoding="utf-8",
    )
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    error_message = "stale_scene_evidence: repair operation crosses a Scene boundary or separator"
    provider_attempts = [
        {
            "attempts": 1,
            "completed": True,
            "stop_reason": "succeeded",
            "history": [
                {
                    "attempt": 1,
                    "outcome": "succeeded",
                    "failure_category": None,
                    "partial_content_received": False,
                }
            ],
        }
        for _ in range(provider_calls)
    ]
    result = {
        "chapter": candidate,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {
                "chars": len(candidate),
                "artifact": {
                    "chars": len(candidate),
                    "format": "markdown",
                    "path": str(artifact_path),
                    "sha256": artifact_sha256,
                },
                "integrity_audit": {"final_chapter_sha256": candidate_sha256},
            },
            "error": {"type": "SceneEvidenceAlignmentError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                }
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "trace": [
                {
                    "action": "generate_chapter",
                    "status": "completed",
                    "provider_attempts": provider_attempts[: max(provider_calls - 1, 0)],
                },
                {
                    "action": "repair_if_needed",
                    "status": "failed",
                    "error_type": "SceneEvidenceAlignmentError",
                    "error_message": error_message,
                    "provider_attempts": provider_attempts[max(provider_calls - 1, 0):],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    stderr = (
        "core.quality.scene_evidence.SceneEvidenceAlignmentError: "
        f"{error_message}"
    )
    return result, run_path, artifact_path, stderr


def test_stale_scene_evidence_classifier_binds_settled_run_and_candidate(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    _, _, _, stderr = _write_stale_scene_evidence_run(project_root)

    classified = _classify_stale_scene_evidence_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    )

    assert classified is not None
    error = classified["error"]
    assert error["code"] == STALE_SCENE_EVIDENCE_ERROR
    assert error["automatic_retry"] is False
    assert error["safe_to_retry"] is False
    assert error["recovery_required"] is True
    details = error["details"]
    assert details["failure_origin"] == "stale_scene_evidence"
    assert details["provider_calls"] == details["settled_provider_calls"] == 6
    assert details["unsettled_attempt_count"] == 0
    assert details["candidate_chars"] > 0
    assert details["formal_prose_exists"] is False
    assert details["accepted"] is details["committed"] is False
    assert details["writeback_attempted"] is details["writeback_applied"] is False
    assert details["source_error_type"] == "SceneEvidenceAlignmentError"
    assert details["alignment_reason"] == "repair operation crosses a Scene boundary or separator"
    assert "引擎不可读" not in error["message"]
    assert "不确定" not in error["message"]

    _write_stale_scene_evidence_run(
        project_root,
        run_id="second-ambiguous-alignment-run",
    )
    assert _classify_stale_scene_evidence_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None


def test_stale_scene_evidence_classifier_fails_closed_on_tamper_unsettled_or_prose(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    result, run_path, artifact_path, stderr = _write_stale_scene_evidence_run(
        project_root
    )

    artifact_path.write_text("tampered retained candidate", encoding="utf-8")
    assert _classify_stale_scene_evidence_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None

    _, run_path, _, stderr = _write_stale_scene_evidence_run(project_root)
    result = json.loads(run_path.read_text(encoding="utf-8"))
    result["run"]["execution_evidence"]["budget"]["unsettled_attempt_count"] = 1
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    assert _classify_stale_scene_evidence_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None

    _, _, _, stderr = _write_stale_scene_evidence_run(project_root)
    prose_path = canonical_prose_path(project_root, 1, "正式正文")
    prose_path.parent.mkdir(parents=True, exist_ok=True)
    prose_path.write_text("不应存在的正式正文", encoding="utf-8")
    assert _classify_stale_scene_evidence_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    ) is None


def test_legacy_stale_scene_evidence_pause_reconciles_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, _, stderr = _write_stale_scene_evidence_run(project_root)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.result is None
    assert legacy.error["code"] == "engine_result_unreadable"

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    supervisor._reconcile_stale_scene_evidence_failures()

    reconciled = store.get(parent.id)
    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == STALE_SCENE_EVIDENCE_ERROR
    assert reconciled.result is not None
    assert reconciled.result["run_id"] == "run-stale-scene-evidence"
    assert reconciled.result["provider_calls"] == 6
    assert reconciled.result["chapter_chars"] > 0
    assert store.claim_next() is None
    assert store.resume_ready(parent.id) is False
    assert store.events_after(parent.id)[-1]["event"] == "chapter.stale_scene_evidence"
    with pytest.raises(JobError, match="completed, receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)



def test_provenance_classifier_rejects_malformed_run_record(tmp_path: Path) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "chapter_1_partial.json").write_text("{", encoding="utf-8")
    paths, stderr = _write_provenance_failure_artifacts(
        tmp_path / "job-runtime", "job-malformed"
    )

    assert _classify_pre_provider_provenance_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        paths=paths,
        prior_run_fingerprints={},
        prior_model_call_fingerprints={},
    ) is None


def test_provenance_classifier_rejects_malformed_progress_jsonl(tmp_path: Path) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    paths, stderr = _write_provenance_failure_artifacts(
        tmp_path / "job-runtime", "job-malformed-progress"
    )
    with paths["progress"].open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    assert _classify_pre_provider_provenance_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        paths=paths,
        prior_run_fingerprints={},
        prior_model_call_fingerprints={},
    ) is None


def test_provenance_classifier_rejects_model_call_symlink(tmp_path: Path) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    model_calls = (
        project_root
        / ".novelagent"
        / "runtime"
        / "runs"
        / "executions"
        / "execution-test"
        / "model_calls"
    )
    model_calls.mkdir(parents=True)
    target = tmp_path / "outside-model-call.json"
    target.write_text("{}", encoding="utf-8")
    link = model_calls / "intent.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable on this Windows host: {exc}")
    paths, stderr = _write_provenance_failure_artifacts(
        tmp_path / "job-runtime", "job-symlink"
    )

    assert _classify_pre_provider_provenance_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        paths=paths,
        prior_run_fingerprints={},
        prior_model_call_fingerprints={},
    ) is None


def test_legacy_provenance_pause_reconciles_and_waits_for_git_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    runtime_root = tmp_path / "job-runtime"
    _, stderr = _write_provenance_failure_artifacts(runtime_root, parent.id)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.state == "awaiting_input"

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=runtime_root,
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda root: {"ok": False, "dependency": "git"},
    )
    supervisor._reconcile_pre_provider_provenance_failures()

    reconciled = store.get(parent.id)
    assert reconciled.id == parent.id
    assert reconciled.state == "awaiting_input"
    assert reconciled.next_chapter == 1
    assert reconciled.error is not None
    assert reconciled.error["code"] == PRE_PROVIDER_PROVENANCE_ERROR
    assert reconciled.error["details"]["provider_reached"] is False
    assert store.resume_ready(parent.id) is False
    assert store.claim_next() is None
    assert store.events_after(parent.id)[-1]["event"] == (
        "chapter.pre_provider_provenance_unavailable"
    )

    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda root: {"ok": True, "dependency": "git"},
    )
    assert store.resume_ready(parent.id) is True
    before = {job.id for job in store.list_jobs(project_id=project_id)}
    resumed = store.resume(parent.id, confirmed=True)
    assert resumed.id == parent.id
    assert resumed.state == "queued"
    assert resumed.next_chapter == 1
    assert {job.id for job in store.list_jobs(project_id=project_id)} == before


def test_provenance_retry_gate_fails_closed_when_artifact_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    project_root = tmp_path / "books" / "test-book"
    paths, stderr = _write_provenance_failure_artifacts(tmp_path / "job-runtime", parent.id)
    classified = _classify_pre_provider_provenance_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        paths=paths,
        prior_run_fingerprints={},
        prior_model_call_fingerprints={},
    )
    assert classified is not None
    store.chapter_failed(
        parent.id,
        chapter=1,
        error=classified["error"],
        uncertain=False,
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda root: {"ok": True, "dependency": "git"},
    )
    assert store.resume_ready(parent.id) is True

    paths["stderr"].write_text(stderr + "changed\n", encoding="utf-8")
    assert store.resume_ready(parent.id) is False
    with pytest.raises(JobError, match="provenance evidence changed"):
        store.resume(parent.id, confirmed=True)


def test_git_preflight_failure_pauses_before_process_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    queued = store.create_job(
        project_id=project_id,
        kind="generation_session",
        start_chapter=1,
        end_chapter=2,
        config={"project_state_head": "stable-head"},
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == queued.id
    project_root = tmp_path / "books" / "test-book"
    process_calls: list[object] = []

    def forbidden_process(*args, **kwargs):
        process_calls.append((args, kwargs))
        raise AssertionError("process factory must not run before Git preflight passes")

    monkeypatch.setattr("workbench.jobs.compute_project_state_head", lambda project: "stable-head")
    monkeypatch.setattr(
        "workbench.jobs.verify_approved_outline_binding",
        lambda root, chapter, config: {"ok": True, "errors": []},
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda root: {"ok": False, "dependency": "git"},
    )
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
        process_factory=forbidden_process,
    )

    supervisor._run_generation_session(claimed)

    paused = store.get(queued.id)
    assert process_calls == []
    assert paused.state == "awaiting_input"
    assert paused.error is not None
    assert paused.error["code"] == PRE_PROVIDER_PROVENANCE_ERROR
    assert paused.error["details"]["failure_origin"] == "git_preflight"
    assert paused.error["details"]["provider_reached"] is False
    assert paused.error["details"]["provider_calls"] == 0
    assert paused.result is None
    assert store.resume_ready(queued.id) is False
    assert not any(
        event["event"] == "chapter.started" for event in store.events_after(queued.id)
    )

    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda root: {"ok": True, "dependency": "git"},
    )
    assert store.resume_ready(queued.id) is True
    ids_before = {item.id for item in store.list_jobs(project_id=project_id)}
    resumed = store.resume(queued.id, confirmed=True)
    assert resumed.id == queued.id
    assert resumed.state == "queued"
    assert resumed.next_chapter == 1
    assert {item.id for item in store.list_jobs(project_id=project_id)} == ids_before



def test_structured_character_budget_classifier_uses_official_zero_call_messages(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    _, run_path, stderr = _write_structured_character_budget_run(
        project_root,
        chapter=1,
        error_message="required_json_item_exceeds_budget: required entries exceed 1481 characters",
    )

    classified = _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    )

    assert classified is not None
    error = classified["error"]
    assert error["code"] == PRE_PROVIDER_CONTEXT_BUDGET_ERROR
    assert error["automatic_retry"] is False
    assert error["safe_to_retry"] is True
    assert "1481 字符" in error["message"]
    assert "尚未调用模型" in error["message"]
    assert error["details"]["unit"] == "characters"
    assert error["details"]["code"] == "required_json_item_exceeds_budget"
    assert error["details"]["hard"] == 1481
    assert error["details"]["provider_reached"] is False
    assert error["details"]["provider_calls"] == 0
    assert error["details"]["formal_prose_exists"] is False
    assert classified["result"]["run"]["id"] == "run-structured-character-budget"

    run_path.unlink()
    _, _, markdown_stderr = _write_structured_character_budget_run(
        project_root,
        chapter=1,
        run_id="run-structured-markdown-budget",
        error_message=(
            "required_markdown_context_exceeds_budget: "
            "required complete Markdown/JSON entries exceed 10500 characters"
        ),
    )
    markdown = _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=markdown_stderr,
        prior_fingerprints={},
    )
    assert markdown is not None
    assert markdown["error"]["details"]["code"] == "required_markdown_context_exceeds_budget"
    assert markdown["error"]["details"]["hard"] == 10500
    assert markdown["error"]["details"]["unit"] == "characters"
    assert "10500 字符" in markdown["error"]["message"]

    _, unsafe_path, unsafe_stderr = _write_structured_character_budget_run(
        project_root,
        chapter=1,
        run_id="run-structured-called",
        provider_calls=1,
    )
    for leftover in (project_root / ".novelagent" / "runtime" / "runs").glob("chapter_*.json"):
        if leftover != unsafe_path:
            leftover.unlink()
    assert _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=unsafe_stderr,
        prior_fingerprints={},
    ) is None


def test_legacy_structured_character_budget_pause_reconciles_and_resumes_same_session(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, stderr = _write_structured_character_budget_run(project_root, chapter=1)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.state == "awaiting_input"
    assert store.resume_ready(parent.id) is False

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    try:
        supervisor.start()
        reconciled = store.get(parent.id)
    finally:
        supervisor.shutdown()

    assert reconciled.id == parent.id
    assert reconciled.kind == "generation_session"
    assert reconciled.state == "awaiting_input"
    assert reconciled.next_chapter == 1
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == PRE_PROVIDER_CONTEXT_BUDGET_ERROR
    assert reconciled.error["details"]["unit"] == "characters"
    assert reconciled.error["details"]["provider_calls"] == 0
    assert store.resume_ready(parent.id) is True
    assert store.events_after(parent.id)[-1]["event"] == (
        "chapter.pre_provider_context_budget_exceeded"
    )
    with pytest.raises(JobError, match="explicit author confirmation"):
        store.resume(parent.id, confirmed=False)

    job_ids_before = {job.id for job in store.list_jobs(project_id=project_id)}
    resumed = store.resume(parent.id, confirmed=True)
    assert resumed.id == parent.id
    assert resumed.state == "queued"
    assert resumed.next_chapter == 1
    assert resumed.error is None
    assert {job.id for job in store.list_jobs(project_id=project_id)} == job_ids_before


def test_context_budget_classifier_requires_complete_zero_call_evidence(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    _, run_path, stderr = _write_context_budget_run(project_root)

    classified = _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    )

    assert classified is not None
    error = classified["error"]
    assert error["code"] == PRE_PROVIDER_CONTEXT_BUDGET_ERROR
    assert error["automatic_retry"] is False
    assert error["safe_to_retry"] is True
    assert {
        key: error["details"][key]
        for key in (
            "required",
            "hard",
            "excess",
            "provider_reached",
            "provider_calls",
            "formal_prose_exists",
        )
    } == {
        "required": 33212,
        "hard": 32000,
        "excess": 1212,
        "provider_reached": False,
        "provider_calls": 0,
        "formal_prose_exists": False,
    }
    assert classified["result"]["run"]["id"] == "run-context-budget"

    _, unsafe_path, unsafe_stderr = _write_context_budget_run(
        project_root,
        run_id="run-provider-called",
        provider_calls=1,
    )
    run_path.unlink()
    assert _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=unsafe_stderr,
        prior_fingerprints={},
    ) is None

    unsafe_path.unlink()
    _, _, unsettled_stderr = _write_context_budget_run(
        project_root,
        run_id="run-unsettled",
        unsettled_attempt_count=1,
    )
    assert _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=unsettled_stderr,
        prior_fingerprints={},
    ) is None


def test_legacy_context_budget_pause_reconciles_and_resumes_same_session(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, stderr = _write_context_budget_run(project_root)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.state == "awaiting_input"
    assert store.resume_ready(parent.id) is False

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    try:
        supervisor.start()
        reconciled = store.get(parent.id)
    finally:
        supervisor.shutdown()

    assert reconciled.id == parent.id
    assert reconciled.kind == "generation_session"
    assert reconciled.state == "awaiting_input"
    assert reconciled.next_chapter == 1
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == PRE_PROVIDER_CONTEXT_BUDGET_ERROR
    assert reconciled.error["details"]["provider_calls"] == 0
    assert reconciled.error["details"]["unsettled_attempt_count"] == 0
    assert store.resume_ready(parent.id) is True
    assert store.events_after(parent.id)[-1]["event"] == (
        "chapter.pre_provider_context_budget_exceeded"
    )
    with pytest.raises(JobError, match="explicit author confirmation"):
        store.resume(parent.id, confirmed=False)

    job_ids_before = {job.id for job in store.list_jobs(project_id=project_id)}
    resumed = store.resume(parent.id, confirmed=True)
    assert resumed.id == parent.id
    assert resumed.state == "queued"
    assert resumed.next_chapter == 1
    assert resumed.error is None
    assert {job.id for job in store.list_jobs(project_id=project_id)} == job_ids_before


def test_context_budget_retry_gate_fails_closed_when_run_evidence_changes(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    project_root = tmp_path / "books" / "test-book"
    _, run_path, stderr = _write_context_budget_run(project_root)
    classified = _classify_pre_provider_context_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
        prior_fingerprints={},
    )
    assert classified is not None
    store.chapter_failed(
        parent.id,
        chapter=1,
        result=classified["result"],
        error=classified["error"],
        uncertain=False,
    )
    assert store.resume_ready(parent.id) is True

    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    run_payload["run"]["execution_evidence"]["budget"]["provider_calls"] = 1
    run_path.write_text(json.dumps(run_payload), encoding="utf-8")

    assert store.resume_ready(parent.id) is False
    with pytest.raises(JobError, match="evidence is incomplete or changed"):
        store.resume(parent.id, confirmed=True)


def test_restart_converts_active_job_to_needs_recovery_without_requeue(tmp_path: Path) -> None:
    database_path, store, project_id = _job_store(tmp_path)
    running = _running_job(store, project_id)
    assert running.state == "running"

    restarted_store = JobStore(WorkbenchDatabase(database_path))
    assert restarted_store.recover_after_restart() == 1

    recovered = restarted_store.get(running.id)
    assert recovered.state == "needs_recovery"
    assert recovered.manual_confirmation_required is True
    assert recovered.error is not None
    assert recovered.error["code"] == "workbench_restarted_during_provider_operation"
    assert restarted_store.claim_next() is None

    events = restarted_store.events_after(running.id)
    restart_event = next(item for item in events if item["event"] == "job.needs_recovery")
    assert restart_event["payload"] == {
        "automatic_retry": False,
        "reason": "service_restart",
    }
    assert restarted_store.recover_after_restart() == 0


def test_supervisor_start_reclassifies_proven_review_pause_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    result = _review_rejected_result()
    paused = store.chapter_failed(
        parent.id,
        chapter=1,
        result=result,
        error={
            "code": "chapter_not_safe_to_continue",
            "review_status": "needs_revision",
        },
        uncertain=True,
    )
    assert paused.state == "needs_recovery"

    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "chapter_1_run-review-rejected.json").write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    try:
        supervisor.start()
        reconciled = store.get(parent.id)
    finally:
        supervisor.shutdown()

    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert store.claim_next() is None
    audit = [
        event
        for event in store.events_after(parent.id)
        if event["event"] == "job.review_pause_reclassified"
    ]
    assert len(audit) == 1
    assert audit[0]["payload"] == {
        "automatic_retry": False,
        "chapter": 1,
        "reason": "current_run_has_no_provider_uncertainty",
        "review_status": "needs_revision",
        "run_id": "run-review-rejected",
    }


def test_manual_confirmation_latches_across_recovery_until_explicit_resume(tmp_path: Path) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    paused = store.chapter_failed(
        parent.id,
        chapter=1,
        error={"code": "review_needs_revision"},
        uncertain=False,
    )
    assert paused.state == "awaiting_input"
    assert paused.manual_confirmation_required is True

    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={"parent_job_id": parent.id},
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    checkpoint = (
        tmp_path
        / "books"
        / "test-book"
        / ".novelagent"
        / "runtime"
        / "locked_chapter"
        / "chapter_0001"
        / "checkpoint.json"
    )
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps({"chapter_index": 1, "reusable_scene_count": 1}),
        encoding="utf-8",
    )
    completed_recovery = store.recovery_succeeded(
        recovery.id,
        {
            "ok": True,
            "status": "recovered",
            "checkpoint_path": str(checkpoint),
            "run": {"chapter_index": 1},
        },
    )
    assert completed_recovery.state == "completed"

    # A successful recovery is evidence for the author; it must not silently
    # clear the parent session's manual-intervention latch.
    still_paused = store.get(parent.id)
    assert still_paused.state == "awaiting_input"
    assert still_paused.manual_confirmation_required is True
    assert store.resume_ready(parent.id) is True
    with store.database.transaction() as connection:
        connection.execute("UPDATE jobs SET execution_contract=NULL WHERE id=?", (recovery.id,))
    assert store.resume_ready(parent.id) is False
    with pytest.raises(JobError, match="receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)
    with store.database.transaction() as connection:
        connection.execute("UPDATE jobs SET execution_contract=? WHERE id=?",
                           (parent.execution_contract, recovery.id))
    with pytest.raises(JobError, match="explicit author confirmation"):
        store.resume(parent.id, confirmed=False)

    resumed = store.resume(parent.id, confirmed=True)
    assert resumed.state == "queued"
    assert resumed.manual_confirmation_required is False
    assert any(
        event["event"] == "job.author_confirmed_resume"
        for event in store.events_after(parent.id)
    )


def test_stale_same_run_complete_draft_cannot_unlock_parent_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    queued_parent = store.create_job(
        project_id=project_id,
        kind="generation_session",
        start_chapter=22,
        end_chapter=30,
    )
    assert store.claim_next().id == queued_parent.id
    parent = store.mark_running(
        queued_parent.id,
        chapter=22,
        command=["python", "main.py", "--chapter", "22"],
    )
    paused = store.chapter_failed(
        parent.id,
        chapter=22,
        error={"code": "chapter_not_safe_to_continue"},
        uncertain=False,
    )
    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    run_id = "chapter_22_rejected"
    (run_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run": {
                    "id": run_id,
                    "chapter_index": 22,
                    "status": "rejected",
                    "committed": False,
                }
            }
        ),
        encoding="utf-8",
    )
    verified_text = "newer verified final candidate\n"
    verified_sha256 = hashlib.sha256(verified_text.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        "workbench.jobs.verified_final_recovery_draft",
        lambda **_kwargs: {
            "text": verified_text,
            "sha256": verified_sha256,
            "source_stage": "scene_repair",
        },
    )

    def complete_recovery(name: str, text: str, sha256: str):
        recovery = store.create_job(
            project_id=project_id,
            kind="locked_chapter_recovery",
            start_chapter=22,
            end_chapter=22,
            config={
                "parent_job_id": parent.id,
                "parent_failure_revision": paused.revision,
                "source_run_id": run_id,
                "source_draft_sha256": verified_sha256,
            },
        )
        assert store.claim_next().id == recovery.id
        checkpoint = run_dir / "locked_chapter_resolutions" / f"{name}.json"
        checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "chapter_index": 22,
                    "source_run_id": run_id,
                    "action": "repair_draft",
                    "reason": "durable_repaired_draft_available",
                    "complete_draft": {"text": text, "sha256": sha256},
                }
            ),
            encoding="utf-8",
        )
        store.recovery_succeeded(
            recovery.id,
            {"ok": True, "checkpoint_path": str(checkpoint)},
        )

    old_text = "older polished candidate\n"
    complete_recovery(
        "resolution_old",
        old_text,
        hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
    )
    assert store.resume_ready(parent.id) is False

    complete_recovery(
        "resolution_verified",
        verified_text,
        verified_sha256,
    )
    assert store.resume_ready(parent.id) is True


def test_manual_draft_validation_preserves_the_edited_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    edited_text = "赵铁确认车门、座椅和后窗锁扣才是应答片的触发点。" * 40
    stale_checkpoint_text = "赵铁说车辆本身一动就会触发应答片。" * 40
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=1,
        text=edited_text,
        source_run_id="run-rejected",
    )
    draft_path = project_root / ".novelagent" / "workbench" / "drafts" / "draft-r1.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(edited_text, encoding="utf-8")
    checkpoint_path = (
        project_root
        / ".novelagent"
        / "runtime"
        / "runs"
        / "locked_chapter_resolutions"
        / "resolution_manual.json"
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps({"complete_draft": {"text": stale_checkpoint_text}}),
        encoding="utf-8",
    )
    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "manual_draft_path": str(draft_path),
        },
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    result = {
        "ok": True,
        "status": "recovered",
        "action": "repair_draft",
        "reason": "manual_repaired_draft_provided",
        "checkpoint_path": str(checkpoint_path),
        "provider_calls": 0,
    }
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    monkeypatch.setattr(
        supervisor,
        "_execute_subprocess",
        lambda *_args, **_kwargs: {
            "cancelled": False,
            "exit_code": 0,
            "result": result,
            "stderr_tail": "",
        },
    )

    supervisor._run_recovery(claimed)

    validated = store.get_draft(draft.id)
    assert validated.text == draft.text
    assert validated.sha256 == draft.sha256
    assert validated.revision == draft.revision + 1
    assert validated.state == "validated"
    assert validated.validation == result
    assert not any(
        event["event"] == "draft.validation_stale"
        for event in store.events_after(recovery.id)
    )


def test_validated_manual_draft_unblocks_recovery_after_unaccepted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    paused = store.chapter_failed(
        parent.id,
        chapter=1,
        error={"code": "engine_result_unreadable"},
        uncertain=False,
    )
    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    run_id = "chapter_1_rejected"
    (run_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run": {
                    "id": run_id,
                    "chapter_index": 1,
                    "status": "failed",
                    "committed": False,
                }
            }
        ),
        encoding="utf-8",
    )
    manual_text = "陆沉将扩音器的启用窗口写进撤离计时表。" * 40
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=1,
        text=manual_text,
        source_run_id=run_id,
    )
    draft_path = (
        project_root
        / ".novelagent"
        / "workbench"
        / "drafts"
        / f"{draft.id}_r{draft.revision}.md"
    )
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft.text, encoding="utf-8")
    checkpoint_path = run_dir / "locked_chapter_resolutions" / "resolution_manual.json"
    checkpoint_path.parent.mkdir(exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "chapter_index": 1,
                "source_run_id": run_id,
                "reason": "manual_repaired_draft_provided",
                "complete_draft": {
                    "text": draft.text,
                    "sha256": draft.sha256,
                    "source_stage": "scene_repair",
                },
            }
        ),
        encoding="utf-8",
    )
    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={
            "parent_job_id": parent.id,
            "parent_failure_revision": paused.revision,
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "manual_draft_path": str(draft_path),
        },
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    result = {
        "ok": True,
        "status": "recovered",
        "action": "repair_draft",
        "reason": "manual_repaired_draft_provided",
        "checkpoint_path": str(checkpoint_path),
        "provider_calls": 0,
    }
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    monkeypatch.setattr(
        supervisor,
        "_execute_subprocess",
        lambda *_args, **_kwargs: {
            "cancelled": False,
            "exit_code": 0,
            "result": result,
            "stderr_tail": "",
        },
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda _root: {"ok": True, "dependency": "git"},
    )

    supervisor._run_recovery(claimed)

    assert store.get_draft(draft.id).state == "validated"
    assert store.resume_ready(parent.id) is True
    assert store.resume(parent.id, confirmed=True).state == "queued"


def test_validated_manual_draft_unblocks_recovery_when_checkpoint_strips_trailing_newline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    paused = store.chapter_failed(
        parent.id,
        chapter=1,
        error={"code": "chapter_repair_not_converging"},
        uncertain=False,
    )
    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    run_id = "chapter_1_rejected"
    (run_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run": {
                    "id": run_id,
                    "chapter_index": 1,
                    "status": "failed",
                    "committed": False,
                }
            }
        ),
        encoding="utf-8",
    )
    from workbench.jobs import _sha256_text

    author_text = ("陆沉将扩音器的启用窗口写进撤离计时表。" * 40) + "\n"
    normalized = author_text.strip()
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=1,
        text=author_text,
        source_run_id=run_id,
    )
    draft_path = (
        project_root
        / ".novelagent"
        / "workbench"
        / "drafts"
        / f"{draft.id}_r{draft.revision}.md"
    )
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft.text, encoding="utf-8")
    checkpoint_path = run_dir / "locked_chapter_resolutions" / "resolution_manual.json"
    checkpoint_path.parent.mkdir(exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "chapter_index": 1,
                "source_run_id": run_id,
                "reason": "manual_repaired_draft_provided",
                "complete_draft": {
                    "text": normalized,
                    "sha256": _sha256_text(normalized),
                    "source_stage": "scene_repair",
                },
            }
        ),
        encoding="utf-8",
    )
    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={
            "parent_job_id": parent.id,
            "parent_failure_revision": paused.revision,
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "manual_draft_path": str(draft_path),
        },
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    result = {
        "ok": True,
        "status": "recovered",
        "action": "repair_draft",
        "reason": "manual_repaired_draft_provided",
        "checkpoint_path": str(checkpoint_path),
        "provider_calls": 0,
    }
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    monkeypatch.setattr(
        supervisor,
        "_execute_subprocess",
        lambda *_args, **_kwargs: {
            "cancelled": False,
            "exit_code": 0,
            "result": result,
            "stderr_tail": "",
        },
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda _root: {"ok": True, "dependency": "git"},
    )

    supervisor._run_recovery(claimed)

    validated = store.get_draft(draft.id)
    assert validated.state == "validated"
    assert validated.text == author_text
    assert validated.text != normalized
    assert store.resume_ready(parent.id) is True
    assert store.resume(parent.id, confirmed=True).state == "queued"



@pytest.mark.parametrize(
    "error_message",
    [
        "required_markdown_context_exceeds_budget: required complete Markdown/JSON entries exceed 10500 characters",
        "required_json_item_exceeds_budget: required entries exceed 1481 characters",
    ],
)
def test_validated_manual_draft_unblocks_resume_after_empty_pre_provider_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_message: str,
) -> None:
    """A paid draft from an earlier run can confirm after a 0-call context-budget rerun."""

    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    paused = store.chapter_failed(
        parent.id,
        chapter=1,
        error={"code": "engine_result_unreadable"},
        uncertain=False,
    )
    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    source_run_id = "chapter_1_20261111T000002332925Z"
    latest_run_id = "chapter_1_20261111T010312972548Z"
    (run_dir / f"{source_run_id}.json").write_text(
        json.dumps(
            {
                "accepted": False,
                "committed": False,
                "chapter": "paid polished draft already isolated",
                "run": {
                    "id": source_run_id,
                    "chapter_index": 1,
                    "status": "failed",
                    "accepted": False,
                    "committed": False,
                    "finished_at": "2026-11-11T00:05:18+00:00",
                    "chapter": {"chars": 3641},
                    "execution_evidence": {"budget": {"provider_calls": 6, "unsettled_attempt_count": 1}},
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / f"{latest_run_id}.json").write_text(
        json.dumps(
            {
                "accepted": False,
                "committed": False,
                "chapter": "",
                "run": {
                    "id": latest_run_id,
                    "chapter_index": 1,
                    "status": "failed",
                    "accepted": False,
                    "committed": False,
                    "finished_at": "2026-11-11T01:03:15+00:00",
                    "error": {
                        "type": "StructuredContextError",
                        "message": error_message,
                    },
                    "chapter": {"chars": 0},
                    "execution_evidence": {
                        "budget": {"provider_calls": 0, "unsettled_attempt_count": 0}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    manual_text = "陆沉把撤回标记抄进只读副本，不再追开着的门。" * 40
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=1,
        text=manual_text,
        source_run_id=source_run_id,
    )
    draft_path = (
        project_root
        / ".novelagent"
        / "workbench"
        / "drafts"
        / f"{draft.id}_r{draft.revision}.md"
    )
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft.text, encoding="utf-8")
    checkpoint_path = run_dir / "locked_chapter_resolutions" / "resolution_manual.json"
    checkpoint_path.parent.mkdir(exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "chapter_index": 1,
                "source_run_id": source_run_id,
                "reason": "manual_repaired_draft_provided",
                "complete_draft": {
                    "text": draft.text,
                    "sha256": draft.sha256,
                    "source_stage": "scene_repair",
                },
            }
        ),
        encoding="utf-8",
    )
    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={
            "parent_job_id": parent.id,
            "parent_failure_revision": paused.revision,
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "manual_draft_path": str(draft_path),
            "source_run_id": source_run_id,
        },
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    result = {
        "ok": True,
        "status": "recovered",
        "action": "repair_draft",
        "reason": "manual_repaired_draft_provided",
        "checkpoint_path": str(checkpoint_path),
        "provider_calls": 0,
    }
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    monkeypatch.setattr(
        supervisor,
        "_execute_subprocess",
        lambda *_args, **_kwargs: {
            "cancelled": False,
            "exit_code": 0,
            "result": result,
            "stderr_tail": "",
        },
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda _root: {"ok": True, "dependency": "git"},
    )

    supervisor._run_recovery(claimed)

    assert store.get_draft(draft.id).state == "validated"
    assert store.resume_ready(parent.id) is True
    assert store.resume(parent.id, confirmed=True).state == "queued"


def test_mismatched_manual_draft_stays_blocked_when_latest_run_produced_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id)
    paused = store.chapter_failed(
        parent.id,
        chapter=1,
        error={"code": "engine_result_unreadable"},
        uncertain=False,
    )
    project_root = tmp_path / "books" / "test-book"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    source_run_id = "chapter_1_old_paid"
    latest_run_id = "chapter_1_later_with_prose"
    (run_dir / f"{source_run_id}.json").write_text(
        json.dumps({"run": {"id": source_run_id, "chapter_index": 1, "status": "failed", "committed": False}}),
        encoding="utf-8",
    )
    (run_dir / f"{latest_run_id}.json").write_text(
        json.dumps(
            {
                "accepted": False,
                "committed": False,
                "chapter": "",
                "run": {
                    "id": latest_run_id,
                    "chapter_index": 1,
                    "status": "failed",
                    "accepted": False,
                    "committed": False,
                    "finished_at": "2026-11-11T02:00:00+00:00",
                    "error": {
                        "type": "StructuredContextError",
                        "message": "required_markdown_context_exceeds_budget: required complete Markdown/JSON entries exceed 10500 characters",
                    },
                    "chapter": {"chars": 120},
                    "execution_evidence": {
                        "budget": {"provider_calls": 1, "unsettled_attempt_count": 0}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    manual_text = "旧稿不能在后续已经写出新正文后直接当作恢复证据。" * 20
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=1,
        text=manual_text,
        source_run_id=source_run_id,
    )
    draft_path = (
        project_root
        / ".novelagent"
        / "workbench"
        / "drafts"
        / f"{draft.id}_r{draft.revision}.md"
    )
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft.text, encoding="utf-8")
    checkpoint_path = run_dir / "locked_chapter_resolutions" / "resolution_manual.json"
    checkpoint_path.parent.mkdir(exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "chapter_index": 1,
                "source_run_id": source_run_id,
                "reason": "manual_repaired_draft_provided",
                "complete_draft": {
                    "text": draft.text,
                    "sha256": draft.sha256,
                    "source_stage": "scene_repair",
                },
            }
        ),
        encoding="utf-8",
    )
    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={
            "parent_job_id": parent.id,
            "parent_failure_revision": paused.revision,
            "draft_id": draft.id,
            "draft_revision": draft.revision,
            "manual_draft_path": str(draft_path),
            "source_run_id": source_run_id,
        },
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    result = {
        "ok": True,
        "status": "recovered",
        "action": "repair_draft",
        "reason": "manual_repaired_draft_provided",
        "checkpoint_path": str(checkpoint_path),
        "provider_calls": 0,
    }
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    monkeypatch.setattr(
        supervisor,
        "_execute_subprocess",
        lambda *_args, **_kwargs: {
            "cancelled": False,
            "exit_code": 0,
            "result": result,
            "stderr_tail": "",
        },
    )
    monkeypatch.setattr(
        "workbench.jobs._git_provenance_preflight",
        lambda _root: {"ok": True, "dependency": "git"},
    )
    supervisor._run_recovery(claimed)
    assert store.get_draft(draft.id).state == "validated"
    assert store.resume_ready(parent.id) is False

def test_recovery_source_upgrade_replaces_only_an_untouched_draft(tmp_path: Path) -> None:
    _, store, project_id = _job_store(tmp_path)
    original = store.upsert_draft(
        project_id=project_id,
        chapter=22,
        text="older recovered source",
        source_run_id="run-22",
        source_path="checkpoint-old.json",
    )

    upgraded = store.upsert_draft(
        project_id=project_id,
        chapter=22,
        text="newer verified final source",
        source_run_id="run-22",
        source_path="checkpoint-final.json",
    )

    assert upgraded.id == original.id
    assert upgraded.text == "newer verified final source\n"
    assert upgraded.sha256 == upgraded.base_sha256
    assert upgraded.source_run_id == "run-22"
    assert upgraded.source_path == "checkpoint-final.json"
    assert upgraded.revision == original.revision + 1

    repeated = store.upsert_draft(
        project_id=project_id,
        chapter=22,
        text="newer verified final source",
        source_run_id="run-22",
        source_path="checkpoint-final.json",
    )
    assert repeated == upgraded


def test_recovery_source_upgrade_never_overwrites_an_author_edit(tmp_path: Path) -> None:
    _, store, project_id = _job_store(tmp_path)
    original = store.upsert_draft(
        project_id=project_id,
        chapter=22,
        text="older recovered source",
        source_run_id="run-22",
        source_path="checkpoint-old.json",
    )
    edited = store.update_draft(
        original.id,
        text="author-edited draft",
        expected_revision=original.revision,
    )

    with pytest.raises(DraftSourceConflict):
        store.upsert_draft(
            project_id=project_id,
            chapter=22,
            text="newer verified final source",
            source_run_id="run-22",
            source_path="checkpoint-final.json",
        )

    preserved = store.get_draft(original.id)
    assert preserved == edited


def test_recovery_job_reports_source_conflict_without_overwriting_author_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    original = store.upsert_draft(
        project_id=project_id,
        chapter=22,
        text="older recovered source",
        source_run_id="run-22",
        source_path="checkpoint-old.json",
    )
    edited = store.update_draft(
        original.id,
        text="author-edited draft",
        expected_revision=original.revision,
    )
    checkpoint_path = (
        project_root
        / ".novelagent"
        / "runtime"
        / "runs"
        / "locked_chapter_resolutions"
        / "resolution_final.json"
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    incoming_text = "newer verified final source"
    checkpoint_path.write_text(
        json.dumps(
            {
                "chapter_index": 22,
                "source_run_id": "run-22",
                "complete_draft": {"text": incoming_text},
            }
        ),
        encoding="utf-8",
    )
    recovery = store.create_job(
        project_id=project_id,
        kind="locked_chapter_recovery",
        start_chapter=22,
        end_chapter=22,
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == recovery.id
    result = {
        "ok": True,
        "status": "recovered",
        "action": "repair_draft",
        "reason": "durable_repaired_draft_available",
        "checkpoint_path": str(checkpoint_path),
        "provider_calls": 0,
    }
    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    monkeypatch.setattr(
        supervisor,
        "_execute_subprocess",
        lambda *_args, **_kwargs: {
            "cancelled": False,
            "exit_code": 0,
            "result": result,
            "stderr_tail": "",
        },
    )

    supervisor._run_recovery(claimed)

    completed = store.get(recovery.id)
    assert completed.state == "completed"
    assert completed.result["draft_source_conflict"]["code"] == (
        "draft_source_upgrade_conflict"
    )
    assert store.get_draft(original.id) == edited
    conflicts = [
        event
        for event in store.events_after(recovery.id)
        if event["event"] == "draft.source_upgrade_conflict"
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["payload"] == {
        "draft_id": original.id,
        "chapter": 22,
        "current_sha256": edited.sha256,
        "base_sha256": original.base_sha256,
        "incoming_sha256": hashlib.sha256(
            (incoming_text + "\n").encode("utf-8")
        ).hexdigest(),
        "source_run_id": "run-22",
        "source_path": str(checkpoint_path),
        "automatic_overwrite": False,
    }


def test_pause_finishes_current_chapter_then_stops_before_next_provider_call(tmp_path: Path) -> None:
    _, store, project_id = _job_store(tmp_path)
    running = _running_job(store, project_id, end_chapter=3)

    requested = store.request_pause(running.id)
    assert requested.state == "pause_requested"
    assert requested.pause_after_chapter is True

    paused = store.chapter_succeeded(
        running.id,
        chapter=1,
        result={"status": "success", "run": {"id": "run-1", "chapter_index": 1}},
        verification={"ok": True, "receipt": True, "readback": True},
    )
    assert paused.state == "awaiting_input"
    assert paused.next_chapter == 2
    assert paused.manual_confirmation_required is True
    assert store.claim_next() is None


def test_cancel_is_final_when_queued_but_fail_closed_when_provider_may_be_running(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    queued = store.create_job(
        project_id=project_id,
        kind="generation_session",
        start_chapter=1,
        end_chapter=1,
    )
    cancelled = store.request_cancel(queued.id)
    assert cancelled.state == "cancelled"
    assert cancelled.cancel_requested is True
    assert cancelled.manual_confirmation_required is False

    # A terminal first session no longer blocks a second one for the same book.
    running = _running_job(store, project_id)
    requested = store.request_cancel(running.id)
    assert requested.state == "running"
    assert requested.cancel_requested is True
    assert requested.manual_confirmation_required is True

    uncertain = store.mark_cancelled_uncertain(running.id, chapter=1)
    assert uncertain.state == "needs_recovery"
    assert uncertain.manual_confirmation_required is True
    assert uncertain.error == {
        "chapter": 1,
        "code": "provider_state_uncertain_after_emergency_stop",
    }
    assert store.claim_next() is None


def test_committed_verification_binds_run_text_formal_prose_and_transaction_marker(
    tmp_path: Path,
) -> None:
    _, _, _ = _job_store(tmp_path)
    project_root = tmp_path / "books" / "test-book"
    chapter_text = "第一章正文\n\n真正写入的内容。\n"
    canonical_prose_path(project_root, 1, "落笔").write_text(
        chapter_text, encoding="utf-8"
    )
    evidence = project_root / ".novelagent" / "persistence"
    evidence.mkdir(parents=True)
    candidate = evidence / "candidate.json"
    candidate.write_text('{"candidate":true}', encoding="utf-8")
    marker = evidence / "commit-marker.json"
    marker.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    result = {
        "committed": True,
        "chapter": chapter_text,
        "review_pipeline": {"status": "pass"},
        "run": {
            "id": "run-1",
            "chapter_index": 1,
            "status": "committed",
            "committed": True,
            "persistence": {
                "committed": True,
                "state": "completed",
                "commit_marker": str(marker),
                "candidate_result_path": str(candidate),
            },
        },
    }

    verified = verify_committed_chapter(project_root, 1, result)
    assert verified["ok"] is True
    assert verified["receipt_kind"] == "transaction_marker"

    missing_generated_text = {**result, "chapter": None}
    rejected = verify_committed_chapter(project_root, 1, missing_generated_text)
    assert rejected["ok"] is False
    assert "generated_chapter_missing" in rejected["errors"]


def _write_scene_boundary_validation_run(
    project_root: Path,
    *,
    chapter: int = 26,
    run_id: str = "run-scene-boundary-validation",
    provider_calls: int = 2,
    unsettled_attempt_count: int = 0,
    candidate: str = "",
    error_message: str | None = None,
) -> tuple[dict, Path, str]:
    if error_message is None:
        error_message = (
            "scene boundary validation failed: scene_boundary_state_mismatch x2, "
            "location_state_rollback; first=scene_boundary_state_mismatch: Scene 4 "
            "places PERSON at PLACE without a matching location delta.; "
            "evidence={\"entity_id\":\"PERSON\"}"
        )
    provider_attempts = [
        {
            "attempts": 1,
            "completed": True,
            "stop_reason": "succeeded",
            "history": [
                {
                    "attempt": 1,
                    "outcome": "succeeded",
                    "failure_category": None,
                    "partial_content_received": False,
                }
            ],
        }
        for _ in range(provider_calls)
    ]
    result = {
        "chapter": candidate,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {"chars": len(candidate)},
            "error": {"type": "SceneBoundaryValidationError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                }
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "trace": [
                {
                    "action": "build_snapshot",
                    "status": "completed",
                },
                {
                    "action": "generate_chapter",
                    "status": "failed",
                    "error_type": "SceneBoundaryValidationError",
                    "error_message": error_message,
                    "provider_attempts": provider_attempts,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    stderr = (
        "core.scene_continuity.SceneBoundaryValidationError: "
        f"{error_message}"
    )
    return result, run_path, stderr


def test_scene_boundary_validation_classifier_binds_settled_run(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, run_path, stderr = _write_scene_boundary_validation_run(project_root)

    classified = _classify_scene_boundary_validation_failure(
        project_root,
        26,
        exit_code=1,
        stderr_tail=stderr,
    )

    assert classified is not None
    details = classified["error"]["details"]
    assert classified["error"]["code"] == SCENE_BOUNDARY_VALIDATION_ERROR
    assert classified["error"]["automatic_retry"] is False
    assert classified["error"]["safe_to_retry"] is False
    assert details["failure_origin"] == "scene_boundary_validation"
    assert details["provider_calls"] == 2
    assert details["settled_provider_calls"] == 2
    assert details["unsettled_attempt_count"] == 0
    assert details["candidate_chars"] == 0
    assert details["formal_prose_exists"] is False
    assert details["finding_codes"] == [
        "scene_boundary_state_mismatch",
        "location_state_rollback",
    ]
    assert details["failed_scene"] == 4
    assert details["source_error_type"] == "SceneBoundaryValidationError"
    assert details["run_record_path"] == run_path.relative_to(project_root).as_posix()
    assert "尚无完整候选正文" in classified["error"]["message"]
    assert "第 4 个场景" in classified["error"]["message"]


def test_scene_boundary_validation_classifier_does_not_absorb_stale_scene(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, _, _, stderr = _write_stale_scene_evidence_run(project_root)
    assert _classify_scene_boundary_validation_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
    ) is None


def test_scene_boundary_validation_classifier_fails_closed_on_tamper_unsettled_or_prose(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"

    _, run_path, stderr = _write_scene_boundary_validation_run(
        project_root,
        unsettled_attempt_count=1,
    )
    assert _classify_scene_boundary_validation_failure(
        project_root,
        26,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, run_path, stderr = _write_scene_boundary_validation_run(
        project_root,
        run_id="run-scene-boundary-zero-exit",
    )
    assert _classify_scene_boundary_validation_failure(
        project_root,
        26,
        exit_code=0,
        stderr_tail=stderr,
    ) is None

    _, run_path, stderr = _write_scene_boundary_validation_run(
        project_root,
        run_id="run-scene-boundary-prose",
    )
    prose = canonical_prose_path(project_root, 26)
    prose.parent.mkdir(parents=True, exist_ok=True)
    prose.write_text("formal prose must not be overwritten by this failure.", encoding="utf-8")
    assert _classify_scene_boundary_validation_failure(
        project_root,
        26,
        exit_code=1,
        stderr_tail=stderr,
    ) is None


def test_legacy_scene_boundary_validation_pause_reconciles_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, stderr = _write_scene_boundary_validation_run(project_root, chapter=1)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.result is None
    assert legacy.error["code"] == "engine_result_unreadable"

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    supervisor._reconcile_scene_boundary_validation_failures()

    reconciled = store.get(parent.id)
    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == SCENE_BOUNDARY_VALIDATION_ERROR
    assert reconciled.error["automatic_retry"] is False
    assert reconciled.error["safe_to_retry"] is False
    assert reconciled.result is not None
    assert store.claim_next() is None
    assert store.resume_ready(parent.id) is False
    assert store.events_after(parent.id)[-1]["event"] == "chapter.scene_boundary_validation"
    with pytest.raises(JobError, match="completed, receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)


def _write_scene_schema_validation_run(
    project_root: Path,
    *,
    chapter: int = 41,
    run_id: str = "run-scene-schema-validation",
    provider_calls: int = 3,
    unsettled_attempt_count: int = 0,
    candidate: str = "",
    error_message: str | None = None,
) -> tuple[dict, Path, str]:
    if error_message is None:
        error_message = "Scene response was not valid structured JSON"
    provider_attempts = [
        {
            "attempts": 1,
            "completed": True,
            "stop_reason": "succeeded",
            "history": [
                {
                    "attempt": 1,
                    "outcome": "succeeded",
                    "failure_category": None,
                    "partial_content_received": False,
                }
            ],
        }
        for _ in range(provider_calls)
    ]
    result = {
        "chapter": candidate,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {"chars": len(candidate)},
            "error": {"type": "ValueError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                }
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "trace": [
                {
                    "action": "build_snapshot",
                    "status": "completed",
                },
                {
                    "action": "generate_chapter",
                    "status": "failed",
                    "error_type": "ValueError",
                    "error_message": error_message,
                    "provider_attempts": provider_attempts,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    stderr = f"ValueError: {error_message}"
    return result, run_path, stderr


def test_scene_schema_validation_classifier_binds_settled_run(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, run_path, stderr = _write_scene_schema_validation_run(project_root)

    classified = _classify_scene_schema_validation_failure(
        project_root,
        41,
        exit_code=1,
        stderr_tail=stderr,
    )

    assert classified is not None
    details = classified["error"]["details"]
    assert classified["error"]["code"] == SCENE_SCHEMA_VALIDATION_ERROR
    assert classified["error"]["automatic_retry"] is False
    assert classified["error"]["safe_to_retry"] is False
    assert details["failure_origin"] == "scene_schema_validation"
    assert details["provider_calls"] == 3
    assert details["settled_provider_calls"] == 3
    assert details["unsettled_attempt_count"] == 0
    assert details["candidate_chars"] == 0
    assert details["formal_prose_exists"] is False
    assert details["source_error_type"] == "ValueError"
    assert details["run_record_path"] == run_path.relative_to(project_root).as_posix()
    assert "尚无完整候选正文" in classified["error"]["message"]
    assert "不是合法 JSON" in classified["error"]["message"]


def test_scene_schema_validation_classifier_does_not_absorb_other_failures(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, _, _, stderr = _write_stale_scene_evidence_run(project_root)
    assert _classify_scene_schema_validation_failure(
        project_root,
        1,
        exit_code=1,
        stderr_tail=stderr,
    ) is None
    _, _, boundary_stderr = _write_scene_boundary_validation_run(project_root)
    assert _classify_scene_schema_validation_failure(
        project_root,
        26,
        exit_code=1,
        stderr_tail=boundary_stderr,
    ) is None
    _, _, schema_stderr = _write_scene_schema_validation_run(project_root)
    mixed = schema_stderr + "\n" + "bridge pre-validation failed: missing_last_scene_continuity"
    assert _classify_scene_schema_validation_failure(
        project_root,
        41,
        exit_code=1,
        stderr_tail=mixed,
    ) is None


def test_scene_schema_validation_classifier_fails_closed_on_tamper_unsettled_or_prose(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"

    _, run_path, stderr = _write_scene_schema_validation_run(
        project_root,
        unsettled_attempt_count=1,
    )
    assert _classify_scene_schema_validation_failure(
        project_root,
        41,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, run_path, stderr = _write_scene_schema_validation_run(
        project_root,
        run_id="run-scene-schema-zero-exit",
    )
    assert _classify_scene_schema_validation_failure(
        project_root,
        41,
        exit_code=0,
        stderr_tail=stderr,
    ) is None

    _, run_path, stderr = _write_scene_schema_validation_run(
        project_root,
        run_id="run-scene-schema-prose",
    )
    prose = canonical_prose_path(project_root, 41)
    prose.parent.mkdir(parents=True, exist_ok=True)
    prose.write_text("formal prose must not be overwritten by this failure.", encoding="utf-8")
    assert _classify_scene_schema_validation_failure(
        project_root,
        41,
        exit_code=1,
        stderr_tail=stderr,
    ) is None


def test_legacy_scene_schema_validation_pause_reconciles_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, stderr = _write_scene_schema_validation_run(project_root, chapter=1)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.result is None
    assert legacy.error["code"] == "engine_result_unreadable"

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    supervisor._reconcile_scene_schema_validation_failures()

    reconciled = store.get(parent.id)
    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == SCENE_SCHEMA_VALIDATION_ERROR
    assert reconciled.error["automatic_retry"] is False
    assert reconciled.error["safe_to_retry"] is False
    assert reconciled.result is not None
    assert store.claim_next() is None
    assert store.resume_ready(parent.id) is False
    assert store.events_after(parent.id)[-1]["event"] == "chapter.scene_schema_validation"
    with pytest.raises(JobError, match="completed, receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)


def _write_bridge_prevalidation_run(
    project_root: Path,
    *,
    chapter: int = 38,
    run_id: str = "run-bridge-prevalidation",
    provider_calls: int = 0,
    unsettled_attempt_count: int = 0,
    dangling_model_calls_ref: bool = True,
    error_message: str | None = None,
    problem_codes: list[str] | None = None,
) -> tuple[dict, Path, str]:
    if problem_codes is None:
        problem_codes = ["missing_last_scene_continuity"]
    if error_message is None:
        error_message = "bridge pre-validation failed: " + ", ".join(problem_codes)
    execution_evidence = {
        "budget": {
            "provider_calls": provider_calls,
            "unsettled_attempt_count": unsettled_attempt_count,
        }
    }
    if dangling_model_calls_ref:
        execution_evidence["model_calls_ref"] = f"executions/execution_{run_id}/model_calls"
    result = {
        "chapter": "",
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {"chars": 0},
            "error": {"type": "ValueError", "message": error_message},
            "execution_evidence": execution_evidence,
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "trace": [
                {
                    "action": "build_snapshot",
                    "status": "completed",
                },
                {
                    "action": "pre_validate_bridge",
                    "status": "failed",
                    "error_type": "ValueError",
                    "error_message": error_message,
                    "bridge_precheck": {
                        "name": "pre_validate_bridge",
                        "ok": False,
                        "problem_codes": problem_codes,
                    },
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    stderr = f"ValueError: {error_message}"
    return result, run_path, stderr


def test_bridge_prevalidation_classifier_binds_zero_call_run(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, run_path, stderr = _write_bridge_prevalidation_run(project_root)

    classified = _classify_bridge_prevalidation_failure(
        project_root,
        38,
        exit_code=1,
        stderr_tail=stderr,
    )

    assert classified is not None
    details = classified["error"]["details"]
    assert classified["error"]["code"] == BRIDGE_PREVALIDATION_ERROR
    assert classified["error"]["automatic_retry"] is False
    assert classified["error"]["safe_to_retry"] is False
    assert details["failure_origin"] == "bridge_prevalidation"
    assert details["provider_calls"] == 0
    assert details["settled_provider_calls"] == 0
    assert details["unsettled_attempt_count"] == 0
    assert details["candidate_chars"] == 0
    assert details["provider_reached"] is False
    assert details["formal_prose_exists"] is False
    assert details["problem_codes"] == ["missing_last_scene_continuity"]
    assert details["source_error_type"] == "ValueError"
    assert details["run_record_path"] == run_path.relative_to(project_root).as_posix()
    assert "没有调用模型" in classified["error"]["message"]
    assert "开写前衔接预检未通过" in classified["error"]["message"]


def test_bridge_prevalidation_classifier_does_not_absorb_other_failures(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, _, schema_stderr = _write_scene_schema_validation_run(project_root)
    assert _classify_bridge_prevalidation_failure(
        project_root,
        41,
        exit_code=1,
        stderr_tail=schema_stderr,
    ) is None
    _, _, boundary_stderr = _write_scene_boundary_validation_run(project_root)
    assert _classify_bridge_prevalidation_failure(
        project_root,
        26,
        exit_code=1,
        stderr_tail=boundary_stderr,
    ) is None
    _, _, bridge_stderr = _write_bridge_prevalidation_run(project_root)
    mixed = bridge_stderr + "\n" + "Scene response was not valid structured JSON"
    assert _classify_bridge_prevalidation_failure(
        project_root,
        38,
        exit_code=1,
        stderr_tail=mixed,
    ) is None


def test_bridge_prevalidation_classifier_fails_closed_on_calls_unsettled_or_prose(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"

    _, _, stderr = _write_bridge_prevalidation_run(
        project_root,
        provider_calls=1,
        dangling_model_calls_ref=False,
    )
    assert _classify_bridge_prevalidation_failure(
        project_root,
        38,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_bridge_prevalidation_run(
        project_root,
        run_id="run-bridge-unsettled",
        unsettled_attempt_count=1,
    )
    assert _classify_bridge_prevalidation_failure(
        project_root,
        38,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_bridge_prevalidation_run(
        project_root,
        run_id="run-bridge-zero-exit",
    )
    assert _classify_bridge_prevalidation_failure(
        project_root,
        38,
        exit_code=0,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_bridge_prevalidation_run(
        project_root,
        run_id="run-bridge-prose",
    )
    prose = canonical_prose_path(project_root, 38)
    prose.parent.mkdir(parents=True, exist_ok=True)
    prose.write_text("formal prose must not be overwritten by this failure.", encoding="utf-8")
    assert _classify_bridge_prevalidation_failure(
        project_root,
        38,
        exit_code=1,
        stderr_tail=stderr,
    ) is None


def test_legacy_bridge_prevalidation_pause_reconciles_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, stderr = _write_bridge_prevalidation_run(project_root, chapter=1)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.result is None
    assert legacy.error["code"] == "engine_result_unreadable"

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    supervisor._reconcile_bridge_prevalidation_failures()

    reconciled = store.get(parent.id)
    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == BRIDGE_PREVALIDATION_ERROR
    assert reconciled.error["automatic_retry"] is False
    assert reconciled.error["safe_to_retry"] is False
    assert reconciled.result is not None
    assert store.claim_next() is None
    assert store.resume_ready(parent.id) is False
    assert store.events_after(parent.id)[-1]["event"] == "chapter.bridge_prevalidation"
    with pytest.raises(JobError, match="completed, receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)


def _write_provider_partial_response_run(
    project_root: Path,
    *,
    chapter: int = 14,
    run_id: str = "run-ppr",
    succeeded_calls: int = 3,
    unsettled_attempt_count: int = 1,
    candidate: str = "",
    action: str = "generate_chapter",
    stage: str = "chapter_generation",
    write_artifact: bool = False,
    omit_partial_receipt: bool = False,
    extra_partial: bool = False,
    error_message: str | None = None,
) -> tuple[dict, Path, str]:
    if error_message is None:
        error_message = (
            "OpenAI chat completion failed "
            "(provider_partial_response; ProviderPartialResponseError)."
        )
    execution_id = f"execution_{run_id}"
    model_calls_root = (
        project_root
        / ".novelagent"
        / "runtime"
        / "runs"
        / "executions"
        / execution_id
        / "model_calls"
    )
    from core.model_calls import build_model_call_intent, build_model_call_receipt

    (model_calls_root / "intents").mkdir(parents=True, exist_ok=True)
    (model_calls_root / "receipts").mkdir(parents=True, exist_ok=True)
    (model_calls_root / "responses").mkdir(parents=True, exist_ok=True)
    request_digest = hashlib.sha256(b"partial-response-fixture-request").hexdigest()
    reservation = {
        "reserved_input_tokens": 16,
        "reserved_output_tokens": 8,
        "reserved_total_tokens": 24,
    }

    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_call(attempt_id: str, *, status: str, body: bytes) -> None:
        intent = build_model_call_intent(
            call_id=attempt_id.rsplit("-", 1)[0],
            attempt_id=attempt_id,
            provider="openai",
            model="gpt-test",
            stage=stage if status == PROVIDER_PARTIAL_RESPONSE_FAILED else "chapter_generation",
            budget_reservation=reservation,
            request_digest=request_digest,
        )
        _write_json(model_calls_root / "intents" / f"{attempt_id}.json", intent)
        response_path = model_calls_root / "responses" / f"{attempt_id}.txt"
        response_path.write_bytes(body)
        if omit_partial_receipt and status == PROVIDER_PARTIAL_RESPONSE_FAILED:
            return
        receipt = build_model_call_receipt(
            intent,
            response_artifact_hash=hashlib.sha256(body).hexdigest(),
            response_artifact_ref=f"responses/{attempt_id}.txt",
            status=status,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        _write_json(model_calls_root / "receipts" / f"{attempt_id}.json", receipt)

    for index in range(succeeded_calls):
        attempt_id = f"ok{index + 1}-a1"
        _write_call(attempt_id, status="succeeded", body=f"ok-scene-{index + 1}".encode("utf-8"))

    partial_attempt = "rp-a1" if action == "repair_if_needed" else f"p{succeeded_calls + 1}-a1"
    _write_call(
        partial_attempt,
        status=PROVIDER_PARTIAL_RESPONSE_FAILED,
        body=b"streamed partial content that stopped early",
    )
    if extra_partial:
        extra_attempt = "rx-a1"
        _write_call(
            extra_attempt,
            status=PROVIDER_PARTIAL_RESPONSE_FAILED,
            body=b"second partial should fail closed",
        )
        succeeded_calls_for_budget = succeeded_calls + 2
    else:
        succeeded_calls_for_budget = succeeded_calls + (0 if omit_partial_receipt else 1)
    provider_calls = succeeded_calls + 1 + (1 if extra_partial else 0)
    if omit_partial_receipt:
        # Intent exists without a receipt; budget still reports the attempted call.
        provider_calls = succeeded_calls + 1

    chapter_payload: dict = {"chars": len(candidate)}
    if write_artifact and candidate:
        artifact_path = (
            project_root
            / ".novelagent"
            / "runtime"
            / "chapters"
            / f"chapter_{chapter:04d}_failed_{run_id}.md"
        )
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_raw = ("failed candidate\n" + candidate + "\n").encode("utf-8")
        artifact_path.write_bytes(artifact_raw)
        chapter_payload["artifact"] = {
            "chars": len(candidate),
            "format": "markdown",
            "path": artifact_path.relative_to(project_root).as_posix(),
            "sha256": hashlib.sha256(artifact_raw).hexdigest(),
        }

    result = {
        "chapter": candidate,
        "accepted": False,
        "committed": False,
        "run": {
            "id": run_id,
            "chapter_index": chapter,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": chapter_payload,
            "error": {"type": "ModelCallError", "message": error_message},
            "execution_evidence": {
                "budget": {
                    "provider_calls": provider_calls,
                    "unsettled_attempt_count": unsettled_attempt_count,
                },
                "model_calls_ref": f"executions/{execution_id}/model_calls",
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "trace": [
                {
                    "action": "build_snapshot",
                    "status": "completed",
                },
                {
                    "action": action,
                    "status": "failed",
                    "error_type": "ModelCallError",
                    "error_message": error_message,
                    "model_call": {
                        "attempts": 1,
                        "failure_category": "provider_partial_response",
                        "message": error_message,
                        "partial_content_received": True,
                        "provider": "openai",
                        "stage": stage,
                    },
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_path = run_dir / f"chapter_{chapter}_{run_id}.json"
    run_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    stderr = f"ModelCallError: {error_message}"
    return result, run_path, stderr


def test_ppr_binds_retained_candidate(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    candidate = "保留的已付费正文候选。"
    _, run_path, stderr = _write_provider_partial_response_run(
        project_root,
        action="repair_if_needed",
        stage="scene_repair",
        candidate=candidate,
        write_artifact=True,
        unsettled_attempt_count=1,
    )

    classified = _classify_provider_partial_response_failure(
        project_root,
        14,
        exit_code=1,
        stderr_tail=stderr,
    )

    assert classified is not None
    details = classified["error"]["details"]
    assert classified["error"]["code"] == PROVIDER_PARTIAL_RESPONSE_ERROR
    assert classified["error"]["automatic_retry"] is False
    assert classified["error"]["safe_to_retry"] is False
    assert details["failure_origin"] == "provider_partial_response"
    assert details["provider_calls"] == 4
    assert details["settled_provider_calls"] == 4
    assert details["unsettled_attempt_count"] == 1
    assert details["candidate_chars"] == len(candidate)
    assert details["formal_prose_exists"] is False
    assert details["provider_reached"] is True
    assert details["partial_receipt_status"] == "partial_response_failed"
    assert details["partial_stage"] == "scene_repair"
    assert details["partial_response_bytes"] > 0
    assert details["source_error_type"] == "ModelCallError"
    assert "不是结果不确定" in classified["error"]["message"]
    assert f"{len(candidate)} 字候选正文已保留" in classified["error"]["message"]
    assert details["run_record_path"] == run_path.relative_to(project_root).as_posix()


def test_ppr_binds_zero_chapter_chars(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, _, stderr = _write_provider_partial_response_run(
        project_root,
        chapter=20,
        run_id="run-ppr-zero",
        succeeded_calls=2,
        candidate="",
        unsettled_attempt_count=1,
    )

    classified = _classify_provider_partial_response_failure(
        project_root,
        20,
        exit_code=1,
        stderr_tail=stderr,
    )

    assert classified is not None
    details = classified["error"]["details"]
    assert details["provider_calls"] == 3
    assert details["candidate_chars"] == 0
    assert details["partial_receipt_status"] == "partial_response_failed"
    assert "尚无完整候选正文，但已保留部分响应回执" in classified["error"]["message"]
    assert classified["error"]["automatic_retry"] is False


def test_ppr_does_not_absorb_other_failures(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"
    _, _, schema_stderr = _write_scene_schema_validation_run(project_root)
    assert _classify_provider_partial_response_failure(
        project_root,
        41,
        exit_code=1,
        stderr_tail=schema_stderr,
    ) is None
    _, _, stderr = _write_provider_partial_response_run(project_root)
    mixed = stderr + "\n" + "ProviderCallUncertainError: provider_call_uncertain"
    assert _classify_provider_partial_response_failure(
        project_root,
        14,
        exit_code=1,
        stderr_tail=mixed,
    ) is None
    budget_mixed = stderr + "\n" + "ContextBudgetError: story_project_context_budget_exceeded"
    assert _classify_provider_partial_response_failure(
        project_root,
        14,
        exit_code=1,
        stderr_tail=budget_mixed,
    ) is None


def test_ppr_fails_closed_on_incomplete_evidence(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    project_root = store.database.path.parent / "books" / "test-book"

    _, _, stderr = _write_provider_partial_response_run(
        project_root,
        chapter=21,
        run_id="run-ppr-zx",
    )
    assert _classify_provider_partial_response_failure(
        project_root,
        21,
        exit_code=0,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_provider_partial_response_run(
        project_root,
        chapter=22,
        run_id="run-ppr-us",
        unsettled_attempt_count=2,
    )
    assert _classify_provider_partial_response_failure(
        project_root,
        22,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_provider_partial_response_run(
        project_root,
        chapter=23,
        run_id="run-ppr-nr",
        omit_partial_receipt=True,
    )
    assert _classify_provider_partial_response_failure(
        project_root,
        23,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_provider_partial_response_run(
        project_root,
        chapter=24,
        run_id="run-ppr-ex",
        extra_partial=True,
    )
    assert _classify_provider_partial_response_failure(
        project_root,
        24,
        exit_code=1,
        stderr_tail=stderr,
    ) is None

    _, _, stderr = _write_provider_partial_response_run(
        project_root,
        chapter=25,
        run_id="run-ppr-pr",
    )
    prose = canonical_prose_path(project_root, 25)
    prose.parent.mkdir(parents=True, exist_ok=True)
    prose.write_text("formal prose must not be overwritten by this failure.", encoding="utf-8")
    assert _classify_provider_partial_response_failure(
        project_root,
        25,
        exit_code=1,
        stderr_tail=stderr,
    ) is None


def test_ppr_legacy_pause_reconciles_without_requeue(
    tmp_path: Path,
) -> None:
    _, store, project_id = _job_store(tmp_path)
    parent = _running_job(store, project_id, end_chapter=3)
    project_root = tmp_path / "books" / "test-book"
    _, _, stderr = _write_provider_partial_response_run(project_root, chapter=1)
    legacy = store.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": stderr,
        },
        uncertain=False,
    )
    assert legacy.result is None
    assert legacy.error["code"] == "engine_result_unreadable"

    supervisor = JobSupervisor(
        store=store,
        project_lookup=lambda requested_id: type(
            "Project", (), {"id": requested_id, "root": project_root}
        )(),
        repo_root=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path / "job-runtime",
    )
    supervisor._reconcile_provider_partial_response_failures()

    reconciled = store.get(parent.id)
    assert reconciled.state == "awaiting_input"
    assert reconciled.manual_confirmation_required is True
    assert reconciled.error is not None
    assert reconciled.error["code"] == PROVIDER_PARTIAL_RESPONSE_ERROR
    assert reconciled.error["automatic_retry"] is False
    assert reconciled.error["safe_to_retry"] is False
    assert reconciled.result is not None
    assert store.claim_next() is None
    assert store.resume_ready(parent.id) is False
    assert store.events_after(parent.id)[-1]["event"] == "chapter.provider_partial_response"
    with pytest.raises(JobError, match="completed, receipt-backed recovery"):
        store.resume(parent.id, confirmed=True)
