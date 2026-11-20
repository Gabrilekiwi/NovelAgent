from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.locked_chapter import recover_locked_chapter
from core.story_project.authority import AuthorityWriterContractError
from core.story_project.format_compatibility import assert_project_execution_compatible
from core.story_project.identity import ProjectIdentityError, load_project_identity
from core.story_project.runtime import build_generation_story_project_context_loader
from scripts.experiments.run_d2_b0_compatibility import fixture, inventory
from workbench.database import WorkbenchDatabase
from workbench.jobs import CURRENT_JOB_EXECUTION_CONTRACT, JobError, JobStore, JobSupervisor
from workbench.projects import ProjectRegistry

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("variant", ["minimum_only", "identity_v3"])
@pytest.mark.parametrize("flags", [
    ["--init-runtime"], ["--dry-run", "--persist-dry-run"],
    ["--reconcile-persistence"], ["--recover-latest"], ["--recover-locked-chapter"],
])
def test_cli_rejects_unsupported_format_before_any_book_write(tmp_path, variant, flags):
    root = tmp_path / "book"
    fixture(root, variant)
    before = inventory(root)
    process = subprocess.run(
        [sys.executable, "-B", str(REPO / "main.py"), "--story-project", str(root), *flags],
        cwd=REPO, env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 1
    expected = "writer contract 1 is below required contract 2" if variant == "minimum_only" else "schema_version"
    assert expected in process.stderr
    assert inventory(root) == before


def test_control_cli_initialization_still_writes_runtime(tmp_path):
    root = tmp_path / "book"
    fixture(root, "control")
    process = subprocess.run(
        [sys.executable, "-B", str(REPO / "main.py"), "--story-project", str(root), "--init-runtime"],
        cwd=REPO, capture_output=True, timeout=30)
    assert process.returncode == 0
    assert (root / ".novelagent/runtime/snapshot.json").is_file()


@pytest.mark.parametrize("variant,exception", [
    ("minimum_only", AuthorityWriterContractError), ("identity_v3", ProjectIdentityError)])
def test_ephemeral_loader_still_rejects_new_unsupported_format(tmp_path, variant, exception):
    root = tmp_path / "book"
    fixture(root, "control")
    identity_path = root / ".novelagent/project.json"
    control = identity_path.read_bytes()
    identity_path.unlink()
    loader = build_generation_story_project_context_loader(story_project=root, chapter=2)
    assert loader.project_identity.ephemeral
    identity_path.write_bytes(control)
    value = json.loads(control)
    if variant == "identity_v3":
        value["schema_version"] = "3.0"
    else:
        value["authority"]["minimum_writer_contract"] = 2
    identity_path.write_text(json.dumps(value), encoding="utf-8")
    before = inventory(root)
    with pytest.raises(exception):
        loader({"chapter_index":2}, {})
    assert inventory(root) == before


@pytest.mark.parametrize("variant,exception", [
    ("minimum_only", AuthorityWriterContractError), ("identity_v3", ProjectIdentityError)])
def test_direct_context_and_recovery_gate_before_runtime_creation(tmp_path, variant, exception):
    root = tmp_path / "book"
    fixture(root, variant)
    before = inventory(root)
    with pytest.raises(exception):
        ChapterCandidateContextBuilder(root).build(2)
    with pytest.raises(exception):
        recover_locked_chapter(story_project_root=root, run_dir=root / ".novelagent/runtime/runs",
                               snapshot_path=root / ".novelagent/runtime/snapshot.json",
                               expected_book_id="synthetic-d2-" + tmp_path.name)
    assert inventory(root) == before


def store_fixture(tmp_path):
    root = tmp_path / "book"
    fixture(root, "control")
    database = WorkbenchDatabase(tmp_path / "workbench.db")
    registry = ProjectRegistry(database, allowed_workspaces=[tmp_path])
    project = registry.import_project(root)
    return root, database, registry, project, JobStore(database)


@pytest.mark.parametrize("kind", ["generation_session", "locked_chapter_recovery"])
@pytest.mark.parametrize("variant", ["minimum_only", "identity_v3"])
def test_worker_rechecks_format_before_paths_or_subprocess(tmp_path, kind, variant):
    root, database, registry, project, store = store_fixture(tmp_path)
    job = store.create_job(project_id=project.id, kind=kind, start_chapter=2, end_chapter=2)
    claimed = store.claim_next()
    supervisor = JobSupervisor(store=store, project_lookup=lambda _: project, repo_root=REPO,
                               runtime_root=tmp_path / "worker", process_factory=lambda *a, **kw: pytest.fail("spawned"))
    path = root / ".novelagent/project.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["authority"]["minimum_writer_contract"] = 2
    if variant == "identity_v3":
        value["schema_version"] = "3.0"
    path.write_text(json.dumps(value), encoding="utf-8")
    before = inventory(root)
    operation = supervisor._run_generation_session if kind == "generation_session" else supervisor._run_recovery
    with pytest.raises((AuthorityWriterContractError, ProjectIdentityError)):
        operation(claimed)
    assert inventory(root) == before
    assert not list((tmp_path / "worker").iterdir())


@pytest.mark.parametrize("contract", [None, "future_execution_v99"])
@pytest.mark.parametrize("state", ["held", "queued", "awaiting_input", "needs_recovery"])
def test_old_tasks_preserve_evidence_but_cannot_run(tmp_path, contract, state):
    root, database, registry, project, store = store_fixture(tmp_path)
    job = store.create_job(project_id=project.id, kind="generation_session", start_chapter=2,
                           end_chapter=3, held=True, config={"quality_policy": "strict"})
    error = {"code": "original_provider_uncertainty", "safe_to_retry": False}
    result = {"run_id": "historical-run"}
    with database.transaction() as connection:
        connection.execute("UPDATE jobs SET execution_contract=?, state=?, error_json=?, result_json=? WHERE id=?",
                           (contract, state, json.dumps(error), json.dumps(result), job.id))
    with pytest.raises(JobError, match="执行契约不兼容"):
        store.release_held(job.id)
    assert store.resume_ready(job.id) is False
    with pytest.raises(JobError, match="执行契约不兼容"):
        store.resume(job.id, confirmed=True)
    assert store.claim_next() is None
    isolated = store.get(job.id)
    assert isolated.state == "needs_recovery"
    assert isolated.error == error and isolated.result == result and isolated.config == job.config
    assert isolated.execution_contract == contract
    assert isolated.to_dict()["execution_compatible"] is False
    before = isolated.to_dict()
    assert store.quarantine_incompatible_jobs() == 0
    assert store.get(job.id).to_dict() == before
    # An idempotency lookup may return the old record for inspection; it cannot stamp it.
    assert not store.get(job.id).to_dict()["execution_compatible"]
    with pytest.raises(JobError, match="执行契约不兼容"):
        store.create_job(project_id=project.id, kind="locked_chapter_recovery", start_chapter=2,
                         end_chapter=2, config={"parent_job_id": job.id})


def test_migrating_database_never_stamps_existing_jobs(tmp_path):
    root, database, registry, project, store = store_fixture(tmp_path)
    job = store.create_job(project_id=project.id, kind="generation_session", start_chapter=2, end_chapter=2)
    with database.transaction() as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN execution_contract")
    reopened = JobStore(database)
    assert reopened.get(job.id).execution_contract is None
    assert reopened.claim_next() is None
    reopened.request_cancel(job.id)
    # Cancellation of a recovery-needed task may remain uncertain; a different
    # project proves fresh tasks still receive the contract and can be claimed.
    second = registry.create_project(tmp_path / "new-book", name="新任务对照")
    fresh = reopened.create_job(project_id=second.id, kind="generation_session", start_chapter=1, end_chapter=1)
    assert fresh.execution_contract == CURRENT_JOB_EXECUTION_CONTRACT
    assert reopened.claim_next().id == fresh.id


def test_upgrade_quarantine_requires_live_task_shutdown(tmp_path):
    root, database, registry, project, store = store_fixture(tmp_path)
    job = store.create_job(project_id=project.id, kind="generation_session", start_chapter=2, end_chapter=2)
    store.claim_next()
    with database.transaction() as connection:
        connection.execute("UPDATE jobs SET execution_contract=NULL WHERE id=?", (job.id,))
    with pytest.raises(JobError, match="在途执行尚未停止"):
        store.quarantine_incompatible_jobs()
    assert store.get(job.id).state == "starting"
    # Only the owned startup path may classify a stopped prior process as uncertain.
    store.recover_after_restart()
    store.quarantine_incompatible_jobs()
    assert store.get(job.id).state == "needs_recovery"
    assert store.claim_next() is None


def test_minimum_contract_metadata_is_inspectable_but_not_executable(tmp_path):
    root = tmp_path / "book"
    fixture(root, "minimum_only")
    assert load_project_identity(root).authority["minimum_writer_contract"] == 2
    with pytest.raises(AuthorityWriterContractError):
        assert_project_execution_compatible(root)
