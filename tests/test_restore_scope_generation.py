"""Second-copy SQL scope ancestry, real process death and target-only startup."""
from contextlib import closing, ExitStack
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import logical
from tests.test_restore_activation import full_source
from tests.test_restore_rebackup import rebackup_case
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.backup_restore_database import prepare_restored_workbench, RestoreDatabaseBindingError
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_plan import plan_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.database import WorkbenchDatabase
from workbench.restore_scope import RestoreScopeError, load_restore_scope, _sidecar, _insert_scope

REPO=Path(__file__).resolve().parents[1]


@pytest.fixture
def second_copy(rebackup_case,private_temporary_directory):
    c=rebackup_case; target=REPO/'.tmp'/('sg-'+uuid.uuid4().hex[:8])
    package=c['rebackup']; scratch=private_temporary_directory
    plan=plan_archived_restore(package,project_id=c['project'].id,book_id=c['project'].book_id,
        destination=target,temporary_root=scratch)
    assert plan['plan_ready']
    materialize_archived_restore(package,plan=plan,temporary_root=scratch)
    bind_restored_roots(package,destination=target,temporary_root=scratch)
    return {**c,'source_target':c['target'],'target':target,'package':package,'plan':plan,'scratch':scratch}


def prepare(c):return prepare_restored_workbench(c['package'],destination=c['target'],temporary_root=c['scratch'])


def contents(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory=sqlite3.Row
        return {name:[dict(row) for row in connection.execute('SELECT * FROM "'+name+'"')]
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def test_actual_second_scope_preserves_old_scope_rows_and_starts_target_only_without_calls(second_copy):
    c=second_copy; database=c['target']/'workbench.sqlite'; root=c['target']/'book'
    before=contents(database); book=inventory_tree(root)
    source=inventory_tree(c['source_target']/'book'); source_sql=logical(c['source_target']/'workbench.sqlite')
    assert load_restore_scope(c['source_target']/'workbench.sqlite',machine_store=c['store']).active
    with absent_source(c['source_target'],temporary_name='first-restored-offline'), absent_source(c['root']):
        result=prepare(c); scope=load_restore_scope(database,machine_store=c['store'])
        assert scope.read_only and scope.document['scope_generation']==2
        original=json.loads(before['workbench_restore_scope'][0]['document_json'])
        assert scope.document['predecessor_scope']==original
        assert scope.document['source_provenance_sha256']==c['plan']['source_restore_provenance']['provenance_sha256']
        assert result['predecessor_scope_preserved'] and result['scope_generation']==2
        expected=copy.deepcopy(before)
        for row in expected['projects']:
            if row['id']==c['project'].id:row['root']=str(root)
        old_root=c['source_target']/'book'
        for row in expected['drafts']:
            if row['project_id']==c['project'].id and row['source_path']:
                row['source_path']=str(root/Path(row['source_path']).relative_to(old_root))
        expected['workbench_restore_scope'][0]['document_json']=json.dumps(scope.document,ensure_ascii=False,sort_keys=True)
        assert contents(database)==expected
        after=database.read_bytes()
        assert prepare(c)==result and database.read_bytes()==after
        calls=[]
        def forbidden(*args,**kwargs):calls.append(1);pytest.fail('readonly second restore invoked execution')
        for _ in range(2):
            app=create_app(database_path=database,start_worker=True,auto_import_active=True,
                chapter_publication_machine_store=c['store'],
                reviewed_chapter_candidate_executor_factory=forbidden,
                reviewed_chapter_fact_extraction_executor_factory=forbidden)
            with TestClient(app) as client:
                assert client.get('/').status_code==200  # Establish the ordinary browser session cookie.
                assert client.get('/api/restore-scope').json()['read_only']
                assert [p['id'] for p in client.get('/api/projects').json()['items']]==[c['project'].id]
                assert client.get('/api/projects/'+c['project'].id+'/chapters/3').status_code==200
                assert client.get('/api/drafts/'+c['draft'].id).status_code==200
                assert client.get('/api/projects/'+c['other'].id).status_code==409
                assert client.put('/api/drafts/'+c['draft'].id,
                    json={'text':'禁止的修改','expected_revision':c['draft'].revision}).status_code==409
                assert app.state.jobs.claim_next() is None and app.state.supervisor._thread is None
        assert not calls and inventory_tree(root)==book
    assert inventory_tree(c['source_target']/'book')==source
    assert logical(c['source_target']/'workbench.sqlite')==source_sql


@pytest.mark.parametrize('boundary',['after_intent','before_commit_flushed',
    'after_commit_before_sidecar','after_scope_before_completion'])
def test_real_second_scope_crash_preserves_ancestry_and_explicitly_resumes(second_copy,boundary):
    c=second_copy; database=c['target']/'workbench.sqlite'; before=database.read_bytes()
    book=inventory_tree(c['target']/'book')
    code='''import os,sys
from pathlib import Path
from workbench import backup_restore_database as m
target=Path(sys.argv[2])/'workbench.sqlite'
phase=sys.argv[4]
original_create=m.atomic_create_json
def create(path,value):
    if phase=='after_commit_before_sidecar' and path.name.endswith('.restore-scope.json'):os._exit(90)
    if phase=='after_scope_before_completion' and path.name=='database-binding-done.json':os._exit(90)
    result=original_create(path,value)
    if phase=='after_intent' and path.name=='database-binding.json':os._exit(90)
    return result
m.atomic_create_json=create
original_apply=m._apply_intent_changes
def apply(connection,intent):
    actual=Path(connection.execute('PRAGMA database_list').fetchone()[2])==target
    if actual and phase=='before_commit_flushed':connection.execute('PRAGMA cache_size=1')
    result=original_apply(connection,intent)
    if actual and phase=='before_commit_flushed':os._exit(90)
    return result
m._apply_intent_changes=apply
m.prepare_restored_workbench(sys.argv[1],destination=sys.argv[2],temporary_root=sys.argv[3])
'''
    with absent_source(c['source_target'],temporary_name='first-restored-offline'), absent_source(c['root']):
        child=subprocess.run([sys.executable,'-B','-c',code,str(c['package']),str(c['target']),str(c['scratch']),boundary],
            cwd=REPO,capture_output=True,text=True,encoding='utf-8',timeout=180,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
        assert child.returncode==90,child.stdout+child.stderr
        assert (c['target']/'database-binding.json').exists()
        assert not (c['target']/'database-binding-done.json').exists()
        if boundary=='before_commit_flushed':assert database.read_bytes()!=before
        if boundary!='after_scope_before_completion':
            with pytest.raises(RestoreScopeError):WorkbenchDatabase(database,machine_store=c['store'])
        else:
            db=WorkbenchDatabase(database,machine_store=c['store'])
            with pytest.raises(RestoreScopeError):
                with db.transaction() as connection:connection.execute('DELETE FROM drafts')
        intent=(c['target']/'database-binding.json').read_bytes()
        result=prepare(c)
        assert (c['target']/'database-binding.json').read_bytes()==intent
        scope=load_restore_scope(database,machine_store=c['store'])
        assert scope.read_only and scope.document['scope_generation']==2
        assert scope.document['predecessor_scope']==c['plan']['source_restore_provenance']['scope']
        assert logical(database)==result['database_logical_sha256']
        assert not Path(str(database)+'-journal').exists() and inventory_tree(c['target']/'book')==book


@pytest.mark.parametrize('damage',['generation','prior_scope','source_provenance','downgrade'])
def test_locally_rehashed_scope_ancestry_cannot_replace_original_database_intent(second_copy,damage):
    c=second_copy; prepare(c); database=c['target']/'workbench.sqlite'; marker=_sidecar(database)
    value=json.loads(marker.read_bytes())
    if damage=='generation':value['scope_generation']+=1
    elif damage=='prior_scope':
        value['predecessor_scope']['project_id']='different-project'
        old=value['predecessor_scope']; old['record_sha256']=digest({k:v for k,v in old.items() if k!='record_sha256'})
    elif damage=='source_provenance':value['source_provenance_sha256']='f'*64
    else:
        for key in ('predecessor_scope','source_provenance_sha256','scope_generation'):value.pop(key)
        value['schema_version']='restored_workbench_scope_v1'
    value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('UPDATE workbench_restore_scope SET document_json=?',(raw,));connection.commit()
    marker.write_text(raw,encoding='utf-8')
    sql=logical(database); book=inventory_tree(c['target']/'book')
    with pytest.raises(RestoreScopeError):load_restore_scope(database,machine_store=c['store'])
    with pytest.raises((RestoreScopeError,RestoreDatabaseBindingError)):prepare(c)
    assert logical(database)==sql and inventory_tree(c['target']/'book')==book


def test_ordinary_scope_installer_cannot_overwrite_a_prior_scope(second_copy):
    c=second_copy; database=c['target']/'workbench.sqlite'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        scope=json.loads(connection.execute('SELECT document_json FROM workbench_restore_scope').fetchone()[0])
        with pytest.raises(RestoreScopeError):_insert_scope(connection,scope)
        connection.rollback()
