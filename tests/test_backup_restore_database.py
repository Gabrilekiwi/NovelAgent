"""Whole-DB conservation and actual read-only app on a restored final-format book."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from core.story_project.paths import resolve_prose
from tests.test_backup_archive import capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed
from tests.test_backup_mixed_history import transaction
from tests.test_backup_restore_materialization import prepare, new_target
from workbench.app import create_app
from workbench.backup_database import _logical_digest
from workbench.backup_files import inventory_tree
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench import backup_restore_database as binding
from workbench.book_ownership import BookOwnership
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry
from workbench.restore_scope import RestoreScopeError, _sidecar


REPO = Path(__file__).resolve().parents[1]


def make_source_workbench(c, *, before_maintenance=None, job_runtime_relative=None):
    """Full real app schema and shared fixture data; no network or live books."""
    assert transaction(c).commit()['committed']
    if job_runtime_relative is not None:
        runtime=c['root']/job_runtime_relative
        assert runtime.is_relative_to(c['root']) and '..' not in runtime.parts
        c={**c,'runtime':runtime}
    base, machine = c['root'].parent, c['root'].parent/'m'
    with BookOwnership(c['project'].book_id, machine_store=machine).locked() as owner:
        with c['db'].transaction() as connection:
            owner.enroll(root=c['root'],database=c['db'],project_id=c['project'].id,connection=connection)
    app = create_app(database_path=c['db'].path, runtime_root=c['runtime'], allowed_workspaces=[base],
        repo_root=REPO, start_worker=False, auto_import_active=False, chapter_publication_machine_store=machine)
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        projects = ProjectRegistry(app.state.database, allowed_workspaces=[base])
        other = projects.create_project(base/'other', name='其他作品')
        jobs = app.state.jobs
        prose = resolve_prose(c['root'],2).path
        assert prose is not None
        draft = jobs.upsert_draft(project_id=c['project'].id,chapter=3,text='作者保留的第三章草稿',source_path=str(prose))
        other_draft = jobs.upsert_draft(project_id=other.id,chapter=1,text='其他作品的私有草稿')
        job = jobs.create_job(project_id=c['project'].id,kind='generation_session',start_chapter=3,end_chapter=3)
        other_job = jobs.create_job(project_id=other.id,kind='generation_session',start_chapter=1,end_chapter=1)
        if before_maintenance is not None:
            before_maintenance({**c, 'machine':machine})
        gate = app.state.maintenance
        operation = gate.begin()['operation_id']
        app.state.backups.machine_store = machine
        package = Path(capture({'service':app.state.backups,'op':operation,'project':c['project']})['path'])
        gate.finish(operation)
    return {**c,'package':package,'other':other,'draft':draft,'other_draft':other_draft,
        'job':job,'other_job':other_job,'machine':machine,'source_prose':prose}


@pytest.fixture(scope='module')
def full_source():
    # Share the immutable source package, not restore outputs, to avoid repeating
    # source generation for every fault. Every test creates its own destination.
    return make_source_workbench(mixed.__wrapped__(case.__wrapped__()))


@pytest.fixture
def bound_copy(full_source, private_temporary_directory):
    c = full_source
    target = new_target()
    plan = prepare(c,c['package'],target,private_temporary_directory)
    materialize_archived_restore(c['package'],plan=plan,temporary_root=private_temporary_directory)
    bind_restored_roots(c['package'],destination=target,temporary_root=private_temporary_directory)
    return {**c,'target':target,'plan':plan,'scratch':private_temporary_directory}


def run(c):
    return binding.prepare_restored_workbench(c['package'],destination=c['target'],temporary_root=c['scratch'])


def logical(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
        return _logical_digest(connection)


def rows(path, table):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY id')]


def test_actual_restored_workbench_reads_target_and_preserves_all_other_data(bound_copy):
    c = bound_copy
    from core.autonomy.common import atomic_append_json
    from workbench.book_ownership import WITNESS_PATH, BookOwnershipError
    assert (c['target']/'book'/WITNESS_PATH).read_bytes()==(c['root']/WITNESS_PATH).read_bytes()
    database = c['target']/'workbench.sqlite'
    before = {name:rows(database,name) for name in ('projects','jobs','job_events','drafts')}
    book_before = inventory_tree(c['target']/'book')
    source_before = inventory_tree(c['root'])
    with absent_source(c['root']), absent_source(c['other'].root, temporary_name='other-offline'):
        with pytest.raises(BookOwnershipError):
            atomic_append_json(c['target']/'book'/'blocked-auxiliary/new.json', {'must_not_write':True})
        assert not (c['target']/'book'/'blocked-auxiliary').exists()
        result = run(c)
        assert result['database_rebound'] and result['read_only_app_ready']
        assert not result['write_activation_allowed'] and not result['provider_calls_allowed']
        expected = json.loads(json.dumps(before))
        for row in expected['projects']:
            if row['id'] == c['project'].id:
                row['root'] = str(c['target']/'book')
        for row in expected['drafts']:
            if row['id'] == c['draft'].id:
                row['source_path'] = str(c['target']/'book'/c['source_prose'].relative_to(c['root']))
        assert {name:rows(database,name) for name in before} == expected
        def forbidden(*args,**kwargs):
            pytest.fail('restored read-only workbench attempted model execution')
        app = create_app(database_path=database,repo_root=REPO,allowed_workspaces=[c['root'].parent],
            runtime_root=c['target']/'must-not-create',start_worker=True,auto_import_active=True,
            chapter_candidate_executor_factory=forbidden,chapter_fact_extraction_executor_factory=forbidden,
            reviewed_chapter_candidate_executor_factory=forbidden,reviewed_chapter_fact_extraction_executor_factory=forbidden)
        with TestClient(app) as client:
            assert client.get('/').status_code == 200
            assert client.get('/api/bootstrap').json()['restore_scope']['read_only']
            listing = client.get('/api/projects')
            assert listing.status_code == 200, listing.text
            assert [p['id'] for p in listing.json()['items']] == [c['project'].id]
            base = '/api/projects/'+c['project'].id
            detail = client.get(base)
            assert detail.status_code == 200, detail.text
            assert detail.json()['project']['next_chapter'] == 3
            assert client.get(base+'/chapters/2').status_code == 200
            assert client.get(base+'/export').status_code == 200
            draft = client.get('/api/drafts/'+c['draft'].id)
            assert draft.status_code == 200 and draft.json()['draft']['text'] == c['draft'].text
            for path in ('/api/projects/'+c['other'].id,'/api/jobs/'+c['other_job'].id,'/api/drafts/'+c['other_draft'].id):
                assert client.get(path).status_code == 409
            assert client.post('/api/maintenance/begin',json={}).status_code == 409
            assert app.state.jobs.claim_next() is None
            assert app.state.supervisor._thread is None
    assert inventory_tree(c['target']/'book') == book_before
    assert inventory_tree(c['root']) == source_before
    assert not (c['target']/'must-not-create').exists()
    assert not Path(c['plan']['root_plan']['task_runtime_root']).exists()
    assert logical(database) == result['database_logical_sha256']


def test_repeating_database_preparation_is_idempotent(bound_copy):
    c = bound_copy
    first = run(c)
    database = c['target']/'workbench.sqlite'
    before, book_before = database.read_bytes(), inventory_tree(c['target']/'book')
    second = run(c)
    assert second == first
    assert database.read_bytes() == before and inventory_tree(c['target']/'book') == book_before


@pytest.mark.parametrize('boundary',['before_commit','before_commit_flushed','after_commit_before_sidecar'])
def test_real_database_interruption_remains_blocked_and_explicitly_resumes(bound_copy,boundary):
    c = bound_copy
    before_database = (c['target']/'workbench.sqlite').read_bytes()
    script = '''import os,sys
from pathlib import Path
from workbench import backup_restore_database as m
target=Path(sys.argv[2])/'workbench.sqlite'
if sys.argv[4].startswith('before_commit'):
    original=m._apply_changes
    def interrupted(connection,changes,scope):
        actual=Path(connection.execute('PRAGMA database_list').fetchone()[2])==target
        if actual and sys.argv[4]=='before_commit_flushed':connection.execute('PRAGMA cache_size=1')
        original(connection,changes,scope)
        if actual:os._exit(77)
    m._apply_changes=interrupted
else:
    original=m.atomic_create_json
    def interrupted(path,value):
        if path.name.endswith('.restore-scope.json'):os._exit(77)
        return original(path,value)
    m.atomic_create_json=interrupted
m.prepare_restored_workbench(sys.argv[1],destination=sys.argv[2],temporary_root=sys.argv[3])
'''
    with absent_source(c['root']):
        process = subprocess.run([sys.executable,'-B','-c',script,str(c['package']),str(c['target']),str(c['scratch']),boundary],
            cwd=REPO,capture_output=True,text=True,timeout=180)
        assert process.returncode == 77, process.stderr
        if boundary == 'before_commit_flushed':
            # Prove the process really spilled uncommitted pages to the main
            # file; recovery must undo those bytes using SQLite's journal.
            assert (c['target']/'workbench.sqlite').read_bytes() != before_database
        assert (c['target']/'database-binding.json').exists()
        assert not (c['target']/'database-binding-done.json').exists()
        with pytest.raises(RestoreScopeError):
            WorkbenchDatabase(c['target']/'workbench.sqlite')
        result = run(c)
    assert result['read_only_app_ready'] and not result['worker_enabled']
    assert not (c['target']/'workbench.sqlite-journal').exists()
    assert logical(c['target']/'workbench.sqlite') == result['database_logical_sha256']
    assert _sidecar(c['target']/'workbench.sqlite').is_file()
    assert WorkbenchDatabase(c['target']/'workbench.sqlite').restore_scope.project_id == c['project'].id


@pytest.mark.parametrize('damage',['author_draft','other_project','new_table'])
def test_unplanned_database_changes_cannot_be_blessed_by_rebinding(bound_copy, damage):
    c = bound_copy
    database = c['target']/'workbench.sqlite'
    with closing(sqlite3.connect(database)) as connection:
        if damage == 'author_draft':
            connection.execute('UPDATE drafts SET text=? WHERE id=?',('changed',c['draft'].id))
        elif damage == 'other_project':
            connection.execute('UPDATE projects SET name=? WHERE id=?',('changed',c['other'].id))
        else:
            connection.execute('CREATE TABLE unplanned_table (id TEXT)')
        connection.commit()
    before = database.read_bytes()
    with pytest.raises(binding.RestoreDatabaseBindingError):
        run(c)
    assert not (c['target']/'database-binding.json').exists()
    assert database.read_bytes() == before


def test_changed_scope_sidecar_is_not_repaired_by_repeat(bound_copy):
    c = bound_copy
    run(c)
    database = c['target']/'workbench.sqlite'
    marker = _sidecar(database)
    marker.write_text('{}')
    before = database.read_bytes()
    with pytest.raises(binding.RestoreDatabaseBindingError):
        run(c)
    assert marker.read_text() == '{}' and database.read_bytes() == before
