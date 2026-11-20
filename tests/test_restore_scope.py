"""Pre-activation access boundary over a full synthetic workbench database."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_workbench_chapter_candidates import _strict_book
from workbench.app import create_app
from workbench.backup_database import _logical_digest
from workbench.backup_files import inventory_tree
from workbench.database import WorkbenchDatabase
from workbench.jobs import JobSupervisor
from workbench.restore_scope import (RestoreScopeError, RestoreScopeBlocked,
    install_restore_restriction, load_restore_scope, _sidecar)


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared_scope(tmp_path):
    # This fixture isolates the restriction boundary. It is not evidence that a
    # full archive restore or ownership handoff has already been implemented.
    root = _strict_book(tmp_path)
    database = tmp_path / 'workbench.db'
    task_root = tmp_path / 'jobs'
    app = create_app(database_path=database, runtime_root=task_root, allowed_workspaces=[tmp_path],
                     repo_root=REPO, start_worker=False, auto_import_active=False)
    with TestClient(app):
        target = app.state.projects.import_project(root)
        other = app.state.projects.create_project(tmp_path / 'other', name='other')
        target_job = app.state.jobs.create_job(project_id=target.id, kind='generation_session', start_chapter=2, end_chapter=2)
        other_job = app.state.jobs.create_job(project_id=other.id, kind='generation_session', start_chapter=1, end_chapter=1)
        target_draft = app.state.jobs.upsert_draft(project_id=target.id, chapter=2, text='目标作者草稿')
        other_draft = app.state.jobs.upsert_draft(project_id=other.id, chapter=1, text='另一作品作者草稿')
    scope = install_restore_restriction(database, project_id=target.id, book_id=target.book_id,
        book_root=root, task_runtime_root=task_root, plan_sha256='1'*64, archive_receipt_sha256='2'*64)
    return dict(base=tmp_path, root=root, database=database, task_root=task_root, target=target, other=other,
                target_job=target_job, other_job=other_job, target_draft=target_draft, other_draft=other_draft, scope=scope)


def open_app(c, **kwargs):
    return create_app(database_path=c['database'], runtime_root=c['base']/'must-not-create',
                     allowed_workspaces=[c['base']], repo_root=REPO, **kwargs)


def logical(path):
    with closing(sqlite3.connect(path)) as connection:
        return _logical_digest(connection)


def test_restoring_overrides_startup_and_never_opens_unbacked_project(prepared_scope, monkeypatch):
    c = prepared_scope
    before = logical(c['database'])
    book_before = inventory_tree(c['root'])
    from workbench.planning import OutlinePlanner
    from workbench.amendments import AmendmentService
    def forbidden(*args, **kwargs):
        pytest.fail('restoring app entered startup recovery, automatic import or worker')
    monkeypatch.setattr(OutlinePlanner, 'recover_after_restart', forbidden)
    monkeypatch.setattr(AmendmentService, 'recover_after_restart', forbidden)
    monkeypatch.setattr(JobSupervisor, '_start_worker', forbidden)
    import workbench.app as app_module
    monkeypatch.setattr(app_module, '_import_active_project', forbidden)
    with absent_source(c['other'].root, temporary_name='other-offline'):
        app = open_app(c, start_worker=True, auto_import_active=True)
        with TestClient(app) as client:
            assert client.get('/').status_code == 200
            bootstrap = client.get('/api/bootstrap')
            assert bootstrap.status_code == 200 and bootstrap.json()['restore_scope']['read_only']
            projects = client.get('/api/projects')
            assert projects.status_code == 200, projects.text
            assert [r['id'] for r in projects.json()['items']] == [c['target'].id]
            assert client.get('/api/projects/' + c['other'].id).status_code == 409
            assert [j.id for j in app.state.jobs.list_jobs()] == [c['target_job'].id]
            with pytest.raises(RestoreScopeBlocked):
                app.state.projects.get(c['other'].id)
            assert app.state.jobs.claim_next() is None
            app.state.supervisor.start()
            assert app.state.supervisor._thread is None
    assert not (c['base'] / 'must-not-create').exists()
    assert logical(c['database']) == before and inventory_tree(c['root']) == book_before


@pytest.mark.parametrize('kind', ['project', 'job', 'job_events', 'draft', 'query', 'unknown_route'])
def test_other_entity_and_unknown_routes_are_rejected_before_handlers(prepared_scope, kind):
    c = prepared_scope
    paths = {'project':'/api/projects/' + c['other'].id,
             'job':'/api/jobs/' + c['other_job'].id,
             'job_events':'/api/jobs/' + c['other_job'].id + '/events',
             'draft':'/api/drafts/' + c['other_draft'].id,
             'query':'/api/projects/' + c['target'].id + '?project_id=' + c['other'].id,
             'unknown_route':'/api/future-global'}
    app = open_app(c)
    called = []
    @app.get('/api/future-global')
    def future_global():
        called.append(True)
        return {'unsafe':True}
    with TestClient(app) as client:
        response = client.get(paths[kind])
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'restore_scope_read_only'
        assert '另一作品作者草稿' not in response.text
    assert not called


def test_mutations_and_direct_database_writes_are_disabled(prepared_scope):
    c = prepared_scope
    before = logical(c['database'])
    app = open_app(c)
    with TestClient(app) as client:
        client.get('/')
        assert client.get('/api/drafts/' + c['target_draft'].id).status_code == 200
        assert client.put('/api/drafts/' + c['target_draft'].id,
            json={'text':'changed', 'expected_revision':1}).status_code == 409
        assert client.post('/api/projects', json={'name':'new', 'target':str(c['base']/'new')}).status_code == 409
        assert client.post('/api/jobs/' + c['target_job'].id + '/resume', json={'confirmed':True}).status_code == 409
        with pytest.raises(RestoreScopeBlocked):
            app.state.projects.import_project(c['other'].root)
        with pytest.raises(RestoreScopeBlocked):
            app.state.projects.create_project(c['base']/'new', name='new')
        with pytest.raises(RestoreScopeBlocked), app.state.database.transaction():
            pytest.fail('restoring transaction admitted')
        with app.state.database.connection() as connection, pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE drafts SET text='changed'")
    assert logical(c['database']) == before and not (c['base'] / 'new').exists()


@pytest.mark.parametrize('damage', ['missing_sidecar', 'missing_table', 'changed_sidecar', 'both_missing_with_intent'])
def test_incomplete_scope_cannot_downgrade_to_normal_startup(prepared_scope, damage):
    c = prepared_scope
    if damage in {'missing_sidecar', 'both_missing_with_intent'}:
        _sidecar(c['database']).unlink()
    if damage in {'missing_table', 'both_missing_with_intent'}:
        with closing(sqlite3.connect(c['database'])) as connection:
            connection.execute('DROP TABLE workbench_restore_scope')
            connection.commit()
    if damage == 'changed_sidecar':
        _sidecar(c['database']).write_text('{}', encoding='utf-8')
    if damage == 'both_missing_with_intent':
        (c['base']/'restore-intent.json').write_text('{}', encoding='utf-8')
    with pytest.raises(RestoreScopeError):
        open_app(c)


def test_loaded_scope_is_rechecked_before_later_database_access(prepared_scope):
    c = prepared_scope
    database = WorkbenchDatabase(c['database'])
    assert load_restore_scope(c['database']).project_id == c['target'].id
    _sidecar(c['database']).unlink()
    with pytest.raises(RestoreScopeError), database.connection():
        pytest.fail('changed scope was reused')
