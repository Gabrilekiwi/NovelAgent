from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from workbench.app import create_app
from workbench.instance import InstanceAlreadyRunning, InstanceLease, existing_instance_url


ROOT = Path(__file__).resolve().parents[1]
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _wait(predicate, *, seconds=15):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for isolated test process")


def test_deep_instance_directory_keeps_exclusive_lock_and_live_address(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    directory = tmp_path
    while len(str(directory)) < 265:
        directory = directory / ('nested-' + 'x' * 24)
    lease = InstanceLease(directory).acquire()
    try:
        class IdentityHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == '/api/instance' else 404)
                self.end_headers()
                self.wfile.write(json.dumps({'instance_id': lease.instance_id}).encode())

        with HTTPServer(('127.0.0.1', 0), IdentityHandler) as server:
            url = f'http://127.0.0.1:{server.server_port}/'
            lease.publish(url)
            server.timeout = 2
            serving = Thread(target=server.handle_request)
            serving.start()
            try:
                assert existing_instance_url(directory, timeout=0) == url
            finally:
                serving.join(3)
        with pytest.raises(InstanceAlreadyRunning):
            InstanceLease(directory).acquire()
    finally:
        lease.close()
    InstanceLease(directory).acquire().close()


def test_second_app_is_rejected_before_database_initialization(tmp_path, monkeypatch):
    kwargs = dict(database_path=tmp_path / "workbench.db", runtime_root=tmp_path / "jobs",
                  allowed_workspaces=[tmp_path], auto_import_active=False, start_worker=False)
    first = create_app(**kwargs)
    with TestClient(first) as client:
        assert client.get("/api/instance").json()["instance_id"] == first.state.instance_lease.instance_id
        with monkeypatch.context() as patch:
            patch.setattr("workbench.database.WorkbenchDatabase.initialize",
                          lambda _: pytest.fail("Second app must not initialize or recover the database"))
            with pytest.raises(InstanceAlreadyRunning):
                create_app(**kwargs)
        assert client.get("/api/instance").status_code == 200
    assert not first.state.instance_lease.held
    assert client.get("/api/instance").status_code == 503
    with TestClient(create_app(**kwargs)) as recovered:
        assert recovered.get("/api/instance").status_code == 200


def test_failed_app_construction_releases_ownership(tmp_path):
    with pytest.raises((ValueError, OSError)):
        create_app(database_path=tmp_path / "workbench.db",
                   allowed_workspaces=[tmp_path / "missing"], auto_import_active=False)
    lease = InstanceLease(tmp_path).acquire()
    lease.close()


def test_stale_metadata_is_not_ownership_or_a_trusted_url(tmp_path):
    (tmp_path / "workbench-instance.json").write_text(json.dumps({
        "instance_id": "stale", "pid": os.getpid(), "url": "https://example.com/",
    }))
    assert existing_instance_url(tmp_path, timeout=0) is None
    lease = InstanceLease(tmp_path).acquire()
    assert json.loads((tmp_path / "workbench-instance.json").read_text())["instance_id"] == lease.instance_id
    lease.close()


CHILD = """
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1])
(root/'heartbeat').write_text(str(time.monotonic()))
(root/'child-ready.json').write_text(json.dumps({'pid':os.getpid()}))
while not (root/'release-child').exists():
    (root/'heartbeat').write_text(str(time.monotonic()))
    time.sleep(.03)
print('{}',flush=True)
"""

PARENT = """
import json,os,sys
from pathlib import Path
from workbench.app import create_app
root=Path(sys.argv[1]); child_code=sys.argv[2]
app=create_app(database_path=root/'workbench.db',runtime_root=root/'jobs',
               allowed_workspaces=[root],auto_import_active=False,start_worker=False)
project=app.state.projects.create_project(root/'book',name='synthetic instance test')
store=app.state.jobs
job=store.create_job(project_id=project.id,kind='generation_session',start_chapter=1,end_chapter=1)
store.claim_next()
store.mark_running(job.id,chapter=1,command=[sys.executable,'synthetic-child.py'])
(root/'parent-ready.json').write_text(json.dumps({'pid':os.getpid(),'job_id':job.id}))
supervisor=app.state.supervisor
supervisor._execute_subprocess(job.id,1,[sys.executable,'-B','-c',child_code,str(root)],supervisor._job_paths(job.id,1))
"""


def test_engine_child_keeps_ownership_after_parent_is_killed(tmp_path):
    # Exercise the actual supervisor spawn boundary, not only a fake lock object.
    with (tmp_path / "parent.log").open("wb") as log:
        parent = subprocess.Popen([sys.executable, "-B", "-c", PARENT, str(tmp_path), CHILD],
                                  cwd=ROOT, stdout=log, stderr=log, **HIDDEN)
        actual_pid = None
        try:
            def child_started():
                if parent.poll() is not None:
                    raise AssertionError((tmp_path / "parent.log").read_text(encoding="utf-8", errors="replace"))
                return (tmp_path / "child-ready.json").is_file()
            _wait(child_started)
            info = json.loads((tmp_path / "parent-ready.json").read_text())
            actual_pid = info["pid"]
            before = (tmp_path / "heartbeat").read_text()
            os.kill(actual_pid, signal.SIGTERM)
            parent.wait(timeout=10)
            _wait(lambda: (tmp_path / "heartbeat").read_text() not in {"", before})
            with pytest.raises(InstanceAlreadyRunning):
                create_app(database_path=tmp_path / "workbench.db", allowed_workspaces=[tmp_path],
                           runtime_root=tmp_path / "jobs", auto_import_active=False)
            from workbench.database import WorkbenchDatabase
            from workbench.jobs import JobStore
            store = JobStore(WorkbenchDatabase(tmp_path / "workbench.db"))
            assert store.get(info["job_id"]).state == "running"
            (tmp_path / "release-child").touch()

            def released():
                try:
                    lease = InstanceLease(tmp_path).acquire()
                except InstanceAlreadyRunning:
                    return False
                lease.close()
                return True

            _wait(released)
            recovered = create_app(database_path=tmp_path / "workbench.db", allowed_workspaces=[tmp_path],
                                   runtime_root=tmp_path / "jobs", auto_import_active=False)
            with TestClient(recovered):
                job = recovered.state.jobs.get(info["job_id"])
                assert job.state == "needs_recovery"
                assert job.manual_confirmation_required
                assert recovered.state.jobs.claim_next() is None
        finally:
            (tmp_path / "release-child").touch()
            if parent.poll() is None:
                if actual_pid is not None:
                    os.kill(actual_pid, signal.SIGTERM)
                parent.kill()
                parent.wait(timeout=10)


def test_two_concurrent_launches_reuse_one_live_server_and_leave_running_job_unchanged(tmp_path):
    data = tmp_path / "data"
    # A different local application already owns the requested port. Keep its
    # real listener open through both launches to cover the bind/rebind race.
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    requested_port = occupied.getsockname()[1]
    logs = [(tmp_path / f"launch-{i}.log").open("wb") for i in range(2)]
    command = [sys.executable, "-B", "run_workbench.py", "--port", str(requested_port), "--no-browser",
               "--no-active-import", "--data-dir", str(data), "--workspace", str(tmp_path)]
    children = []
    try:
        children = [subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, **HIDDEN) for log in logs]
        url = _wait(lambda: existing_instance_url(data, timeout=0))
        assert urlsplit(url).port != requested_port
        _wait(lambda: sum(child.poll() is not None for child in children) == 1)
        exited = next(child for child in children if child.poll() is not None)
        assert exited.returncode == 0
        from workbench.database import WorkbenchDatabase
        from workbench.jobs import JobStore
        from workbench.projects import ProjectRegistry
        db = WorkbenchDatabase(data / "workbench.db")
        registry = ProjectRegistry(db, allowed_workspaces=[tmp_path])
        project = registry.create_project(tmp_path / "synthetic-book", name="synthetic")
        store = JobStore(db)
        job = store.create_job(project_id=project.id, kind="generation_session", start_chapter=1,
                               end_chapter=1, held=True)
        with db.transaction() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=?", (job.id,))
        before = store.get(job.id)
        duplicate = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"), timeout=15, **HIDDEN)
        assert duplicate.returncode == 0, duplicate.stderr
        assert url in duplicate.stdout
        assert store.get(job.id) == before
    finally:
        metadata = data / "workbench-instance.json"
        if metadata.is_file():
            # PID belongs to the server created above, whose identity was verified.
            owner = json.loads(metadata.read_text())["pid"]
            if any(child.poll() is None for child in children):
                os.kill(owner, signal.SIGTERM)
        for child in children:
            if child.poll() is None:
                child.wait(timeout=10)
        for log in logs:
            log.close()
        occupied.close()
