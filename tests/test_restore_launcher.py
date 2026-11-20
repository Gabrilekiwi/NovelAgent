"""Launch selection and fail-closed startup, separate from restore certification."""
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

import run_workbench
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_restore_scope import logical
from tests.test_workbench_chapter_candidates import _strict_book
from tests.test_workbench_instance import ROOT, HIDDEN, _wait
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.instance import existing_instance_url
from workbench.restore_scope import install_restore_restriction


@pytest.fixture
def launch_scope(tmp_path):
    # Only the launch boundary uses a synthetic restriction here; this is not a
    # complete archive restore or ownership-handoff acceptance sample.
    root = _strict_book(tmp_path)
    directory = tmp_path / '恢复 工作台'
    path = directory / 'workbench.sqlite'
    app = create_app(database_path=path, runtime_root=directory/'jobs',
        allowed_workspaces=[tmp_path], start_worker=False, auto_import_active=False)
    with TestClient(app):
        project = app.state.projects.import_project(root)
        other = app.state.projects.create_project(tmp_path/'other', name='other')
        draft = app.state.jobs.upsert_draft(project_id=project.id, chapter=2, text='恢复前作者草稿')
    install_restore_restriction(path, project_id=project.id, book_id=project.book_id,
        book_root=root, task_runtime_root=directory/'jobs', plan_sha256='1'*64,
        archive_receipt_sha256='2'*64)
    return dict(base=tmp_path, directory=directory, path=path, root=root,
        project=project, other=other, draft=draft)


def test_normal_new_data_directory_keeps_existing_launcher_contract(tmp_path):
    directory = tmp_path/'new'
    assert run_workbench._launch_database(directory) == (directory/'workbench.db', False)
    assert not directory.exists()


@pytest.mark.parametrize('argument', ['--restore-dir', '--data-dir'])
def test_valid_scope_selects_original_database_and_disables_automatic_work(launch_scope, monkeypatch, argument):
    c = launch_scope
    before_book, before_sql = inventory_tree(c['root']), logical(c['path'])
    monkeypatch.setattr(sys, 'argv', ['run_workbench.py', argument, str(c['directory']), '--no-browser', '--port', '0'])
    def forbidden(*args, **kwargs):
        pytest.fail('restored launcher entered ordinary recovery, automatic import or worker')
    monkeypatch.setattr('workbench.jobs.JobSupervisor._start_worker', forbidden)
    monkeypatch.setattr('workbench.planning.OutlinePlanner.recover_after_restart', forbidden)
    monkeypatch.setattr('workbench.amendments.AmendmentService.recover_after_restart', forbidden)
    monkeypatch.setattr('workbench.app._import_active_project', forbidden)
    class Server:
        started = False
        def __init__(self, config):
            self.app = config.app
        def run(self, *, sockets):
            assert sockets[0].getsockname()[0] == '127.0.0.1'
            assert self.app.state.database.path == c['path']
            with TestClient(self.app) as client:
                assert client.get('/api/restore-scope').json()['read_only']
                assert [p['id'] for p in client.get('/api/projects').json()['items']] == [c['project'].id]
                assert client.get('/api/drafts/'+c['draft'].id).json()['draft']['text'] == c['draft'].text
                assert client.get('/api/projects/'+c['other'].id).status_code == 409
                assert self.app.state.supervisor._thread is None
            self.started = True
    monkeypatch.setattr(run_workbench.uvicorn, 'Server', Server)
    assert run_workbench.main() == 0
    assert not (c['directory']/'workbench.db').exists()
    assert inventory_tree(c['root']) == before_book and logical(c['path']) == before_sql


@pytest.mark.parametrize('damage', ['empty', 'intent_without_database', 'unqualified_database', 'ambiguous', 'scope_lost'])
def test_unqualified_or_ambiguous_restore_refuses_before_lease_or_empty_database(launch_scope, monkeypatch, damage):
    c = launch_scope
    directory = c['directory']
    if damage in {'empty', 'intent_without_database'}:
        directory = c['base']/'unfinished'
        directory.mkdir()
        if damage == 'intent_without_database':
            (directory/'restore-intent.json').write_text('{}', encoding='utf-8')
    elif damage == 'unqualified_database':
        directory = c['base']/'unqualified'
        directory.mkdir()
        (directory/'workbench.sqlite').write_bytes(c['path'].read_bytes())
    elif damage == 'ambiguous':
        (directory/'workbench.db').write_bytes(b'do not replace')
    else:
        c['path'].with_name('workbench.sqlite.restore-scope.json').unlink()
    before = inventory_tree(directory)
    def forbidden(*args, **kwargs):
        pytest.fail('invalid restore acquired ownership or constructed a server')
    monkeypatch.setattr(run_workbench.InstanceLease, 'acquire', forbidden)
    monkeypatch.setattr(run_workbench, '_bound_listener', forbidden)
    monkeypatch.setattr(sys, 'argv', ['run_workbench.py', '--restore-dir', str(directory), '--no-browser'])
    assert run_workbench.main() == 2
    assert inventory_tree(directory) == before


def test_nonexistent_explicit_restore_directory_is_not_created(tmp_path, monkeypatch):
    directory = tmp_path/'missing'
    monkeypatch.setattr(sys, 'argv', ['run_workbench.py', '--restore-dir', str(directory), '--no-browser'])
    assert run_workbench.main() == 2
    assert not directory.exists()


def test_conflicting_directory_options_are_rejected():
    with pytest.raises(SystemExit) as error:
        run_workbench.build_parser().parse_args(['--data-dir', 'normal', '--restore-dir', 'copy'])
    assert error.value.code == 2


def test_duplicate_actual_cli_restored_launch_reuses_live_server(launch_scope):
    c = launch_scope
    before_book, before_sql = inventory_tree(c['root']), logical(c['path'])
    command = [sys.executable, '-B', 'run_workbench.py', '--restore-dir', str(c['directory']),
        '--no-browser', '--port', '0']
    with (c['base']/'restored-launch.log').open('wb') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, **HIDDEN)
        try:
            def started():
                if process.poll() is not None:
                    pytest.fail((c['base']/'restored-launch.log').read_text(encoding='utf-8', errors='replace'))
                return existing_instance_url(c['directory'], timeout=0)
            url = _wait(started)
            duplicate = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1'), timeout=15, **HIDDEN)
            assert duplicate.returncode == 0, duplicate.stderr
            assert url in duplicate.stdout
            assert not (c['directory']/'workbench.db').exists()
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
    assert inventory_tree(c['root']) == before_book and logical(c['path']) == before_sql
