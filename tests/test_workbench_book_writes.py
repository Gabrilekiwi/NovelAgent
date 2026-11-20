"""Actual workbench writes and startup recovery on synthetic registered books."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from core.engine.persistence import atomic_write_json
from core.story_project.paths import canonical_prose_path
from core.story_project.reviewed_state_document import digest
from tests.test_workbench_product_services import product_case, proposal_payload
from workbench.amendments import AmendmentService
from workbench import book_ownership as m
from workbench.book_writes import read_registration
from workbench.database import WorkbenchDatabase
from workbench.outlines import OutlineBatchService, render_canonical_outline
from workbench.planning import OutlinePlanner
from workbench.projects import ProjectRegistry


def bind(database,project,store):
    owner=m.BookOwnership(project.book_id,machine_store=store)
    binding=dict(book_id=project.book_id,root=str(project.root),root_lineage=m._lineage(project.root),
        database=str(database.path),database_parent_lineage=m._lineage(database.path.parent),
        database_file_identity=m._file_identity(database.path),project_id=project.id,
        identity_sha256=digest((project.root/'.novelagent/project.json').read_bytes()))
    record=dict(version='machine_book_ownership_v1',generation=1,state='active',binding=binding)
    record['record_sha256']=digest(record)
    with owner.locked():atomic_write_json(owner.record_path,record)
    return owner,record


@pytest.fixture
def wb(monkeypatch):
    base,database,projects,project,outlines=product_case('machine-writes')
    store=base/'machine';monkeypatch.setattr(m,'default_machine_store',lambda:store)
    original=('他握紧手电，示意队伍继续前进。\n\n'*45).strip()+'\n'
    formal=canonical_prose_path(project.root,1,'起点');formal.write_text(original,encoding='utf-8')
    base_sha=hashlib.sha256(formal.read_bytes()).hexdigest()
    revised=original.replace('示意','抬手示意')
    calls=[]
    def outline_provider(*args):calls.append('outline');return proposal_payload(start=2,count=2)
    def amendment_provider(*args):
        calls.append('amendment')
        return json.dumps(dict(equivalent=True,confidence=.98,critical_changes=[],uncertain=False,summary='只改措辞'))
    planner=OutlinePlanner(database=database,outlines=outlines,provider=outline_provider)
    amendments=AmendmentService(database=database,projects=projects,provider=amendment_provider)
    owner,record=bind(database,project,store)
    proposal=json.loads(proposal_payload(start=2,count=2))['chapters']
    return locals()


def suspend(c,owner=None,record=None):
    owner=owner or c['owner'];record=record or c['record']
    record={**record,'state':'restoring'}
    record['record_sha256']=digest({k:v for k,v in record.items() if k!='record_sha256'})
    with owner.locked():atomic_write_json(owner.record_path,record)


def inventory(c):
    # Live SQLite readers can create/remove WAL/SHM files. Verify the complete
    # logical database (including committed WAL frames) and every ordinary file.
    databases = [c['database'].path]
    other = c['base']/'other.sqlite'
    if other.exists():
        databases.append(other)
    sqlite_files = {Path(str(p)+suffix) for p in databases for suffix in ('', '-wal', '-shm')}
    result = {str(p.relative_to(c['base'])):p.read_bytes() for p in c['base'].rglob('*')
              if p.is_file() and p not in sqlite_files}
    for path in databases:
        with read_registration(WorkbenchDatabase(path)) as connection:
            connection.execute('BEGIN')
            result[str(path.relative_to(c['base']))] = tuple(connection.iterdump())
    return result


@pytest.mark.parametrize('entry',['create','update','approve','adopt','plan','amend'])
def test_revoked_workbench_rejects_before_files_database_or_provider(wb,entry,monkeypatch):
    c=wb
    if entry in {'update','approve','adopt'}:batch=c['outlines'].create_batch(c['project'].id,c['proposal'])
    suspend(c);before=inventory(c)
    def forbidden():
        pytest.fail('rejected request opened a potentially writable DB connection')
    monkeypatch.setattr(c['database'], '_open', forbidden)
    with pytest.raises(m.BookOwnershipError):
        if entry=='create':c['outlines'].create_batch(c['project'].id,c['proposal'])
        elif entry in {'update','approve','adopt'}:
            method=dict(update=c['outlines'].update_batch,approve=c['outlines'].approve_batch,
                        adopt=c['outlines'].adopt_existing_and_approve_batch)[entry]
            method(batch.id,*([c['proposal']] if entry=='update' else []),
                expected_revision=batch.revision,expected_sha256=batch.proposal_sha256)
        elif entry=='plan':c['planner'].propose(c['project'].id,direction='继续故事',chapter_count=2)
        else:c['amendments'].submit(c['project'].id,1,text=c['revised'],expected_base_sha256=c['base_sha'])
    assert not c['calls'] and inventory(c)==before
    assert not (c['project'].root/'.novelagent/runtime').exists()


def test_alternate_database_copy_cannot_write_the_same_registered_book(wb):
    c=wb;other=WorkbenchDatabase(c['base']/'other.sqlite')
    with read_registration(c['database']) as source,sqlite3.connect(other.path) as target:source.backup(target)
    projects=ProjectRegistry(other,allowed_workspaces=c['projects'].allowed_workspaces)
    service=OutlineBatchService(other,projects);before=inventory(c)
    with pytest.raises(m.BookOwnershipError,match='活动登记'):
        service.create_batch(c['project'].id,c['proposal'])
    assert inventory(c)==before


def test_active_registration_id_must_match_current_database_row(wb):
    c=wb;record={**c['record'],'binding':{**c['record']['binding'],'project_id':'another-registration'}}
    record['record_sha256']=digest({k:v for k,v in record.items() if k!='record_sha256'})
    with c['owner'].locked():atomic_write_json(c['owner'].record_path,record)
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError,match='活动登记'):
        c['outlines'].create_batch(c['project'].id,c['proposal'])
    assert inventory(c)==before


def child_cannot_own(c):
    code='''from pathlib import Path
import sys
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceAlreadyRunning
try:
    with BookOwnership(sys.argv[1],machine_store=Path(sys.argv[2])).locked():
        raise AssertionError('Provider ran outside machine gate')
except InstanceAlreadyRunning:sys.exit(81)
'''
    result=subprocess.run([sys.executable,'-B','-c',code,c['project'].book_id,str(c['store'])],
        capture_output=True,text=True,timeout=20)
    assert result.returncode==81,result.stderr


def test_actual_planning_and_outline_publication_share_machine_qualification(wb):
    c=wb;provider=c['planner'].provider
    def observe(*args):child_cannot_own(c);return provider(*args)
    c['planner'].provider=observe
    operation,batch=c['planner'].propose(c['project'].id,direction='继续故事',chapter_count=2)
    approved=c['outlines'].approve_batch(batch.id,expected_revision=batch.revision,expected_sha256=batch.proposal_sha256)
    assert operation.state=='completed' and approved.status=='approved' and c['calls']==['outline']
    assert Path(approved.approval_manifest_path).exists()


def test_actual_amendment_retains_machine_gate_across_semantic_provider(wb):
    c=wb;provider=c['amendments'].provider
    def observe(*args):child_cannot_own(c);return provider(*args)
    c['amendments'].provider=observe
    record=c['amendments'].submit(c['project'].id,1,text=c['revised'],expected_base_sha256=c['base_sha'])
    assert record.status=='active' and c['calls']==['amendment']
    assert c['formal'].read_text(encoding='utf-8')==c['original']


@pytest.mark.parametrize('table',['planning_operations','amendments'])
def test_restart_recovers_only_qualified_projects_and_preserves_revoked_records(wb,table):
    c=wb
    operation,_=c['planner'].propose(c['project'].id,direction='继续故事',chapter_count=2)
    amendment=c['amendments'].submit(c['project'].id,1,text=c['revised'],expected_base_sha256=c['base_sha'])
    other=c['projects'].create_project(c['project'].root.parent/'other',name='另一作品')
    other_owner,other_record=bind(c['database'],other,c['store'])
    column,prior,record_id=('state','running',operation.id) if table=='planning_operations' else ('status','validating',amendment.id)
    with c['database'].transaction() as connection:
        row=dict(connection.execute(f'SELECT * FROM {table} WHERE id=?',(record_id,)).fetchone())
        row.update(id='other-operation',project_id=other.id,**{column:prior})
        if table=='planning_operations':row['batch_id']=None
        connection.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',tuple(row.values()))
        connection.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(prior,record_id))
    suspend(c)
    with read_registration(c['database']) as connection:
        revoked_before=dict(connection.execute(f'SELECT * FROM {table} WHERE id=?',(record_id,)).fetchone())
    calls_before=list(c['calls'])
    service=c['planner'] if table=='planning_operations' else c['amendments']
    assert service.recover_after_restart()==1
    with read_registration(c['database']) as connection:
        assert dict(connection.execute(f'SELECT * FROM {table} WHERE id=?',(record_id,)).fetchone())==revoked_before
        assert connection.execute(f'SELECT {column} FROM {table} WHERE id=?',('other-operation',)).fetchone()[0]=='needs_recovery'
    assert service.recovery_blocked==[{'project_id':c['project'].id,'id':record_id,'code':'book_ownership_invalid'}]
    assert c['calls']==calls_before


def test_durable_versions_and_outline_reads_remain_available_after_revocation(wb):
    c=wb;batch=c['outlines'].create_batch(c['project'].id,c['proposal'])
    record=c['amendments'].submit(c['project'].id,1,text=c['revised'],expected_base_sha256=c['base_sha'])
    suspend(c);before=inventory(c)
    assert c['outlines'].get_batch(batch.id)==batch
    assert c['amendments'].get(record.id)==record
    assert c['outlines'].list_batches(c['project'].id)==(batch,)
    assert inventory(c)==before


def test_routing_reads_current_committed_registration_from_live_wal(wb):
    c = wb
    other = c['projects'].create_project(c['project'].root.parent/'wal-book', name='WAL登记')
    bind(c['database'], other, c['store'])
    batch = c['outlines'].create_batch(c['project'].id, c['proposal'])
    # Keep a writable connection alive so the changed owner remains in WAL.
    with c['database'].connection() as connection:
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('UPDATE outline_batches SET project_id=? WHERE id=?', (other.id, batch.id))
        assert Path(str(c['database'].path)+'-wal').stat().st_size > 0
        alternate_owner = m.BookOwnership(other.book_id, machine_store=c['store'])
        with alternate_owner.locked():
            record = json.loads(alternate_owner.record_path.read_text(encoding='utf-8'))
        suspend(c, alternate_owner, record)
        before = inventory(c)
        with pytest.raises(m.BookOwnershipError):
            c['outlines'].approve_batch(batch.id, expected_revision=batch.revision,
                                       expected_sha256=batch.proposal_sha256)
        assert inventory(c) == before
        assert not (other.root/'.novelagent/runtime').exists()


@pytest.mark.parametrize('kind', ['plan', 'amend'])
def test_registration_change_during_provider_cannot_publish_or_rewrite_failure(wb, kind):
    c = wb
    service = c['planner'] if kind == 'plan' else c['amendments']
    provider = service.provider
    observed = {}
    def change_registration(*args):
        raw = provider(*args)
        with c['database'].transaction() as connection:
            connection.execute('UPDATE projects SET book_id=? WHERE id=?', ('changed-book', c['project'].id))
        observed['after_change'] = inventory(c)
        return raw
    service.provider = change_registration
    with pytest.raises(m.BookOwnershipError):
        if kind == 'plan':
            service.propose(c['project'].id, direction='继续故事', chapter_count=2)
        else:
            service.submit(c['project'].id, 1, text=c['revised'], expected_base_sha256=c['base_sha'])
    assert inventory(c) == observed['after_change']
    assert len(c['calls']) == 1


def browser_app(c):
    from workbench.app import create_app
    return create_app(database_path=c['database'].path, allowed_workspaces=c['projects'].allowed_workspaces,
                      runtime_root=c['base']/'jobs', start_worker=False, auto_import_active=False,
                      outline_provider=c['outline_provider'], amendment_provider=c['amendment_provider'])


@pytest.mark.parametrize('entry', ['create', 'update', 'approve', 'adopt-existing', 'plan', 'amend'])
def test_http_rejects_revoked_workbench_actions_and_keeps_readable_drafts(wb, entry):
    from fastapi.testclient import TestClient
    from tests.test_workbench_app import _unlock
    c = wb
    batch = c['outlines'].create_batch(c['project'].id, c['proposal'])
    with TestClient(browser_app(c)) as client:
        _unlock(client)
        suspend(c)
        before = inventory(c)
        cas = dict(expected_revision=batch.revision, expected_sha256=batch.proposal_sha256)
        if entry in {'create', 'plan'}:
            payload = {'chapters': c['proposal']} if entry == 'create' else {'direction': '继续故事', 'chapter_count': 2}
            response = client.post(f"/api/projects/{c['project'].id}/outline-batches", json=payload)
        elif entry == 'update':
            response = client.put(f'/api/outline-batches/{batch.id}', json={**cas, 'chapters': c['proposal']})
        elif entry == 'amend':
            response = client.post('/api/chapters/1/amendments', json=dict(project_id=c['project'].id,
                                   text=c['revised'], expected_base_sha256=c['base_sha']))
        else:
            response = client.post(f'/api/outline-batches/{batch.id}/{entry}', json=cas)
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'book_ownership_invalid'
        assert '写入资格' in response.json()['error']['message']
        assert client.get(f'/api/outline-batches/{batch.id}').json()['batch']['revision'] == batch.revision
        assert client.get(f"/api/projects/{c['project'].id}/chapters/1").status_code == 200
        assert inventory(c) == before and not c['calls']


def test_startup_reports_preserved_unqualified_operations_without_provider_replay(wb):
    from fastapi.testclient import TestClient
    from tests.test_workbench_app import _unlock
    c = wb
    operation, _ = c['planner'].propose(c['project'].id, direction='继续故事', chapter_count=2)
    with c['database'].transaction() as connection:
        connection.execute("UPDATE planning_operations SET state='running' WHERE id=?", (operation.id,))
    suspend(c)
    calls_before = list(c['calls'])
    with TestClient(browser_app(c)) as client:
        _unlock(client)
        response = client.get('/api/bootstrap')
        assert response.status_code == 200
        assert any('1 个中断操作' in w and '原记录已保留' in w for w in response.json()['warnings'])
        assert c['planner'].get_operation(operation.id).state == 'running'
        assert c['calls'] == calls_before


@pytest.mark.parametrize('kind', ['plan', 'amend'])
def test_provider_record_cannot_be_rerouted_to_another_active_book(wb, kind):
    c = wb
    other = c['projects'].create_project(c['project'].root.parent/'other-owner', name='另一活动作品')
    bind(c['database'], other, c['store'])
    service = c['planner'] if kind == 'plan' else c['amendments']
    provider = service.provider
    table = 'planning_operations' if kind == 'plan' else 'amendments'
    observed = {}
    def move_record(*args):
        raw = provider(*args)
        with c['database'].transaction() as connection:
            connection.execute(f'UPDATE {table} SET project_id=? WHERE project_id=?', (other.id, c['project'].id))
        observed['after_move'] = inventory(c)
        return raw
    service.provider = move_record
    with pytest.raises(m.BookOwnershipError, match='another project'):
        if kind == 'plan':
            service.propose(c['project'].id, direction='继续故事', chapter_count=2)
        else:
            service.submit(c['project'].id, 1, text=c['revised'], expected_base_sha256=c['base_sha'])
    assert inventory(c) == observed['after_move'] and len(c['calls']) == 1


def test_startup_does_not_count_a_qualification_failure_rolled_back_by_sqlite(wb):
    c = wb
    operation, _ = c['planner'].propose(c['project'].id, direction='继续故事', chapter_count=2)
    with c['database'].transaction() as connection:
        connection.execute("UPDATE planning_operations SET state='running' WHERE id=?", (operation.id,))
        # A real SQLite trigger changes the registration inside recovery's own
        # transaction. Its final qualification check must roll everything back.
        connection.execute("""CREATE TRIGGER change_registration AFTER UPDATE OF state ON planning_operations
            WHEN NEW.state='needs_recovery' BEGIN
            UPDATE projects SET book_id='invalidated-during-recovery' WHERE id=NEW.project_id;
            END""")
    before = inventory(c)
    assert c['planner'].recover_after_restart() == 0
    assert len(c['planner'].recovery_blocked) == 1
    assert inventory(c) == before
