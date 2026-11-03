"""Maintenance restart must preserve even an old/missing database until admitted."""
from contextlib import closing
import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_workbench_app import _client, _unlock, _create_project
from workbench.app import create_app
from workbench.database import WorkbenchDatabase, WorkbenchDatabaseInitializationPending
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('source',['current','old','missing'])
def test_interrupted_maintenance_preserves_database_and_defers_all_schema_work(tmp_path,source):
    database=tmp_path/'workbench.db'
    if source=='current':
        client,workspace=_client(tmp_path)
        with client:
            _unlock(client)
            _create_project(client,workspace,name='已存在作品')
            op=client.app.state.maintenance.begin()['operation_id']
    else:
        workspace=tmp_path/'books';workspace.mkdir()
        if source=='old':
            # Only the base schema exists; other components require CREATE and ALTER.
            WorkbenchDatabase(database).initialize()
        with closing(InstanceLease(tmp_path).acquire()) as lease:
            op=maintenance_for(lease).begin()['operation_id']
    before=digest(database) if database.exists() else None
    if database.exists():
        with closing(sqlite3.connect(database)) as connection:
            schema_before=connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    app=create_app(database_path=database,runtime_root=tmp_path/'jobs',allowed_workspaces=[workspace],
        start_worker=False,auto_import_active=False)
    with TestClient(app) as restarted:
        _unlock(restarted)
        assert app.state.database.initialization_pending
        assert restarted.get('/api/maintenance').json()['state']=='interrupted'
        # Missing or old schemas stay diagnosable; compatible reads still work.
        response=restarted.get('/api/jobs/missing')
        assert response.status_code==(409 if source=='current' else 503),response.text
        if response.status_code==503:
            assert response.json()['error']['code']=='workbench_database_initialization_pending'
        assert restarted.post('/api/projects',json={}).status_code==423
        assert (digest(database) if database.exists() else None)==before
        if before is not None:
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()==schema_before
        app.state.maintenance.resume(op)
        app.state.maintenance.finish(op)
        _create_project(restarted,workspace)
        assert not app.state.database.initialization_pending
        assert restarted.get('/api/projects').status_code==200
        with app.state.database.connection() as connection:
            assert 'execution_contract' in {row['name'] for row in connection.execute('PRAGMA table_info(jobs)')}
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='chapter_publication_index'").fetchone()


def test_deferred_database_reads_cannot_write_or_change_journal_mode(tmp_path):
    database=WorkbenchDatabase(tmp_path/'workbench.db');database.initialize()
    with closing(sqlite3.connect(database.path,isolation_level=None)) as connection:
        connection.execute('PRAGMA journal_mode=DELETE')
    deferred=WorkbenchDatabase(database.path,defer_initialization=True)
    before=digest(database.path)
    with deferred.connection() as connection:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
        assert connection.execute('SELECT count(*) FROM projects').fetchone()[0]==0
    with pytest.raises(WorkbenchDatabaseInitializationPending):
        with deferred.connection() as connection:connection.execute('PRAGMA user_version=9')
    with pytest.raises(WorkbenchDatabaseInitializationPending):
        with deferred.transaction():pytest.fail('pending database cannot admit writes')
    assert digest(database.path)==before


def test_failed_initialization_retries_only_unfinished_components(tmp_path):
    database=WorkbenchDatabase(tmp_path/'workbench.db',defer_initialization=True)
    called=[]
    database.initialize()
    def first():called.append('first')
    def flaky():
        called.append('flaky')
        if called.count('flaky')==1:raise RuntimeError('synthetic migration failure')
    database.initialize_component(first);database.initialize_component(flaky)
    with pytest.raises(RuntimeError,match='synthetic'):database.complete_initialization()
    assert database.initialization_pending
    database.complete_initialization();database.complete_initialization()
    assert called==['first','flaky','flaky'] and not database.initialization_pending
