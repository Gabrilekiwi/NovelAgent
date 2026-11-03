from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tests.test_workbench_jobs import _job_store
from workbench.backup_database import capture_database, owned_database_snapshot, BackupDatabaseError
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for, MaintenanceBlocked, MaintenanceError
from workbench.projects import ProjectRegistry


@pytest.fixture
def case(tmp_path):
    _,store,project_id=_job_store(tmp_path)
    registry=ProjectRegistry(store.database,allowed_workspaces=[tmp_path/'books'])
    other=registry.create_project(tmp_path/'books'/'另一部书',name='另一部书')
    draft=store.upsert_draft(project_id=project_id,chapter=2,text='作者未发布的草稿。')
    store.create_job(project_id=project_id,kind='generation_session',start_chapter=2,end_chapter=3)
    lease=InstanceLease(tmp_path).acquire();gate=maintenance_for(lease)
    op=gate.begin()['operation_id']
    stage=tmp_path/'备份 暂存';stage.mkdir()
    try:yield dict(root=tmp_path,database=store.database,store=store,gate=gate,op=op,
        destination=stage/'workbench.sqlite',draft=draft,project=project_id,other=other.id)
    finally:lease.close()


def test_whole_wal_database_and_draft_are_copied_while_external_writer_is_blocked(case):
    c=case;blocked=[]
    code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0);\ntry:\n c.execute('BEGIN IMMEDIATE')\nexcept sqlite3.OperationalError as e:\n print(str(e));sys.exit(17)\nsys.exit(99)"
    def progress(*unused):
        if blocked:return
        result=subprocess.run([sys.executable,'-B','-c',code,str(c['database'].path)],capture_output=True,text=True,timeout=5,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
        assert result.returncode==17 and 'locked' in result.stdout
        blocked.append(True)
        with pytest.raises(MaintenanceBlocked):c['gate'].finish(c['op'])
        with pytest.raises(MaintenanceBlocked):c['gate'].capture(c['op'])
    with c['gate'].capture(c['op']) as permit:
        assert c['gate'].status()['state']=='capturing'
        with owned_database_snapshot(c['database'],snapshot_permit=permit) as snapshot:
            snapshot=capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit,progress=progress)
    assert blocked and c['gate'].status()['state']=='quiescent'
    with sqlite3.connect(c['destination']) as copied:
        assert {row[0] for row in copied.execute('SELECT id FROM projects')}=={c['project'],c['other']}
        assert copied.execute('SELECT text FROM drafts WHERE id=?',(c['draft'].id,)).fetchone()[0]==c['draft'].text
        assert copied.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
        assert copied.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert snapshot['scope']=='whole_database' and snapshot['other_projects_default_disabled']
    assert len(snapshot['sha256'])==64 and snapshot['size']>0
    assert not Path(str(c['destination'])+'-wal').exists()


@pytest.mark.parametrize('change',[
    "UPDATE projects SET name='uncommitted'",
    "UPDATE projects SET rowid=rowid+100",
    "PRAGMA application_id=937",
])
def test_uncommitted_caller_transaction_is_never_used_for_snapshot(case,change):
    c=case
    with c['gate'].capture(c['op']) as permit,c['database'].transaction() as caller:
        caller.execute(change)
        with pytest.raises(BackupDatabaseError,match='新建事务'):
            capture_database(c['database'],c['destination'],snapshot=caller,snapshot_permit=permit)
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            with owned_database_snapshot(c['database'],snapshot_permit=permit):
                pytest.fail('another writer must retain the fence')
        assert not c['destination'].exists()


def test_existing_destination_is_preserved_and_ended_snapshot_is_rejected(case):
    c=case;c['destination'].write_bytes(b'original')
    with c['gate'].capture(c['op']) as permit:
        with owned_database_snapshot(c['database'],snapshot_permit=permit) as snapshot:
            with pytest.raises(BackupDatabaseError,match='新的独立文件'):
                capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit)
        with pytest.raises(BackupDatabaseError,match='事务已结束'):
            capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit)
    assert c['destination'].read_bytes()==b'original'


def test_capture_failure_leaves_maintenance_active_and_no_success_receipt(case):
    c=case
    def fault(*unused):raise RuntimeError('synthetic interruption')
    with pytest.raises(RuntimeError,match='synthetic interruption'):
        with c['gate'].capture(c['op']) as permit,owned_database_snapshot(c['database'],snapshot_permit=permit) as snapshot:
            capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit,progress=fault)
    assert c['gate'].status()['state']=='quiescent' and c['destination'].exists()
    with pytest.raises(MaintenanceBlocked):c['gate'].admit('http_write')
    with pytest.raises(MaintenanceError):permit.require()


def test_maintenance_record_changed_during_backup_prevents_success(case):
    c=case
    def damage(*unused):(c['gate'].root/'control.json').write_text('{}',encoding='utf-8')
    with pytest.raises(MaintenanceError):
        with c['gate'].capture(c['op']) as permit,owned_database_snapshot(c['database'],snapshot_permit=permit) as snapshot:
            capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit,progress=damage)


@pytest.mark.parametrize('statement',[
    'UPDATE projects SET rowid=rowid+100',
    'PRAGMA application_id=937',
    'PRAGMA query_only=OFF',
    'ROLLBACK',
    "ATTACH DATABASE ':memory:' AS alternate",
])
def test_exposed_snapshot_connection_rejects_writes_and_transaction_escape(case,statement):
    c=case
    with c['gate'].capture(c['op']) as permit:
        with owned_database_snapshot(c['database'],snapshot_permit=permit) as snapshot:
            with pytest.raises(sqlite3.DatabaseError):snapshot.connection.execute(statement)
            capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit)


def test_committed_hidden_rowids_and_database_metadata_are_preserved(case):
    c=case
    with c['database'].transaction() as connection:
        connection.execute('UPDATE projects SET rowid=rowid+100')
        connection.execute('PRAGMA application_id=937')
        expected=[tuple(row) for row in connection.execute('SELECT rowid,id FROM projects ORDER BY rowid')]
    with c['gate'].capture(c['op']) as permit:
        with owned_database_snapshot(c['database'],snapshot_permit=permit) as snapshot:
            capture_database(c['database'],c['destination'],snapshot=snapshot,snapshot_permit=permit)
    with sqlite3.connect(c['destination']) as copied:
        assert copied.execute('PRAGMA application_id').fetchone()[0]==937
        assert copied.execute('SELECT rowid,id FROM projects ORDER BY rowid').fetchall()==expected
