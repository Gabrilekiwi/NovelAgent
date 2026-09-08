from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from workbench.app import create_app
from workbench.database import WorkbenchDatabase
from workbench.instance import InstanceAlreadyRunning, InstanceLease
from workbench.jobs import JobError, JobStore, JobSupervisor
from workbench.projects import ProjectRegistry


ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / 'child-ready.json').write_text(json.dumps({'pid': os.getpid()}))
while not (root / 'release-child').exists():
    time.sleep(.02)
print('{}', flush=True)
"""


def _wait(predicate, *, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Timed out waiting for isolated process ownership test")


def _process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, no mutation.
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # PID no longer exists.
                return False
            raise ctypes.WinError(error)
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in {0, 258}:  # signaled / timeout
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _supervisor_case(tmp_path, monkeypatch, process_factory):
    data = tmp_path / "data"
    books = tmp_path / "books"
    books.mkdir()
    database = WorkbenchDatabase(data / "workbench.db")
    registry = ProjectRegistry(database, allowed_workspaces=[books])
    store = JobStore(database)
    first_project = registry.create_project(books / "first", name="synthetic first")
    second_project = registry.create_project(books / "second", name="synthetic second")
    first = store.create_job(project_id=first_project.id, kind="generation_session",
                             start_chapter=1, end_chapter=1)
    # Release the following task only once the worker has claimed the first, so task order
    # does not depend on timestamps or random job IDs.
    following = store.create_job(project_id=second_project.id, kind="generation_session",
                                 start_chapter=1, end_chapter=1, held=True)
    supervisor = JobSupervisor(store=store, project_lookup=registry.get, repo_root=ROOT,
                               runtime_root=data / "jobs", process_factory=process_factory,
                               poll_interval=.05)
    dispatched = []

    def run_local_child(job):
        dispatched.append(job.id)
        if job.id != first.id:
            store.fail_job(job.id, {"code": "unexpected_following_dispatch"})
            return
        command = [sys.executable, "-B", "-c", CHILD, str(tmp_path)]
        store.mark_running(job.id, chapter=1, command=[sys.executable, "synthetic-child.py"])
        store.release_held(following.id)
        supervisor._execute_subprocess(job.id, 1, command, supervisor._job_paths(job.id, 1))

    monkeypatch.setattr(supervisor, "_run_generation_session", run_local_child)
    # These local children never need an API key or model configuration.
    monkeypatch.setattr("workbench.jobs._provider_environment", lambda: {})
    return data, store, first, following, supervisor, dispatched


def _inject_monitor_failure(supervisor, store, first, monkeypatch, boundary):
    faulted = threading.Event()
    real_get = store.get
    real_ingest = supervisor._ingest_progress

    def fail_once():
        if not faulted.is_set() and supervisor._active_process is not None:
            faulted.set()
            raise RuntimeError("synthetic monitor failure while child is running")

    if boundary == "progress":
        def broken_ingest(*args, **kwargs):
            fail_once()
            return real_ingest(*args, **kwargs)
        monkeypatch.setattr(supervisor, "_ingest_progress", broken_ingest)
    else:
        def broken_get(job_id):
            if job_id == first.id:
                fail_once()
            return real_get(job_id)
        monkeypatch.setattr(store, "get", broken_get)
    return faulted, real_get


@pytest.mark.parametrize("boundary", ["progress", "store_get"])
def test_monitor_failure_stops_dispatch_reaps_real_child_and_preserves_uncertainty(
    tmp_path, monkeypatch, boundary,
):
    children = []
    actual_pids = []

    def start_real_child(command, **kwargs):
        child = subprocess.Popen(command, **kwargs)
        children.append(child)

        def ready():
            try:
                actual_pid = json.loads((tmp_path / "child-ready.json").read_text())["pid"]
            except (OSError, ValueError, KeyError):
                return False
            actual_pids.append(actual_pid)
            return True

        _wait(ready)
        assert _process_alive(actual_pids[-1]), "The monitored script must really be running"
        return child

    data, store, first, following, supervisor, dispatched = _supervisor_case(
        tmp_path, monkeypatch, start_real_child,
    )
    faulted, read_job = _inject_monitor_failure(
        supervisor, store, first, monkeypatch, boundary,
    )
    try:
        supervisor.start()
        assert faulted.wait(10), "Fault must occur after the real child starts"
        supervisor._thread.join(timeout=12)
        assert not supervisor._thread.is_alive(), "Monitor failure must stop this worker's dispatch loop"
        assert dispatched == [first.id], "A second task must not run after lost child monitoring"
        paused = read_job(first.id)
        assert paused.state == "needs_recovery"
        assert paused.manual_confirmation_required
        assert read_job(following.id).state == "queued"
        assert len(children) == 1 and children[0].poll() is not None
        assert supervisor._active_process is None
        supervisor.shutdown(timeout=2)
        assert not _process_alive(actual_pids[0]), (
            f"Launcher PID {children[0].pid} exited but Python script PID {actual_pids[0]} is still alive"
        )
        # This also checks any inner Python interpreter inherited by a Windows
        # venv launcher: a live descendant must still prevent this acquisition.
        replacement = InstanceLease(data).acquire()
        replacement.close()
    finally:
        (tmp_path / "release-child").touch()
        for child in children:
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        for actual_pid in actual_pids:
            _wait(lambda: not _process_alive(actual_pid), seconds=5)
        supervisor.shutdown(timeout=5)


class UnstoppableProcess:
    def __init__(self):
        self.returncode = None
        self.calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        raise OSError("synthetic terminate failure")

    def kill(self):
        self.calls.append("kill")
        raise OSError("synthetic kill failure")

    def communicate(self, timeout=None):
        self.calls.append("communicate")
        if self.returncode is not None:
            return "{}", ""
        raise OSError("synthetic communicate failure")

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("synthetic-child", timeout)


def test_unstoppable_child_retains_ownership_and_blocks_shutdown_restart_and_second_app(
    tmp_path, monkeypatch,
):
    child = UnstoppableProcess()
    data, store, first, following, supervisor, dispatched = _supervisor_case(
        tmp_path, monkeypatch, lambda *args, **kwargs: child,
    )
    faulted, read_job = _inject_monitor_failure(
        supervisor, store, first, monkeypatch, "progress",
    )
    try:
        supervisor.start()
        assert faulted.wait(10)
        supervisor._thread.join(timeout=12)
        assert not supervisor._thread.is_alive(), "Lost monitoring must halt further scheduling"
        assert dispatched == [first.id]
        assert supervisor._active_process is child
        assert child.poll() is None
        assert {"terminate", "kill"} <= set(child.calls)
        paused = read_job(first.id)
        assert paused.state == "needs_recovery"
        assert paused.manual_confirmation_required
        assert read_job(following.id).state == "queued"

        with pytest.raises(JobError):
            supervisor.shutdown(timeout=.1)
        with pytest.raises(JobError):
            supervisor.start()
        with pytest.raises(InstanceAlreadyRunning):
            create_app(database_path=data / "workbench.db", runtime_root=data / "jobs",
                       allowed_workspaces=[tmp_path / "books"], auto_import_active=False,
                       start_worker=False)
        assert supervisor._active_process is child
        assert read_job(following.id).state == "queued"
    finally:
        # The fake cannot leak an actual process. Allow cleanup only after all
        # assertions so an early release cannot accidentally make the test pass.
        child.returncode = 0
        supervisor.shutdown(timeout=5)
