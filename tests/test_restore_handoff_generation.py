"""Revoke a real activated restore and explicitly reinstate its qualification."""
from contextlib import closing, ExitStack
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from core.autonomy.common import atomic_append_json
from core.engine.persistence import atomic_write_json
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import logical
from tests.test_restore_scope_generation import (full_source,rebackup_case,second_copy,private_temporary_directory)
from workbench.app import create_app
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_files import inventory_tree
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.backup_restore_provenance import capture_restore_provenance, validate_restore_provenance
from workbench.book_ownership import BookOwnership, OWNERSHIP_V2, OWNERSHIP_V3
from workbench.database import WorkbenchDatabase
from workbench.instance import InstanceLease, InstanceAlreadyRunning
from workbench.restore_activation import RestoreActivation
from workbench.restore_handoff import RestoreHandoff, RestoreHandoffError, _signed
from workbench.restore_reinstatement import reinstatement_name
from workbench.restore_scope import load_restore_scope, RestoreScopeError
from workbench.restore_usage import RestoreUsage

REPO=Path(__file__).resolve().parents[1]


@pytest.fixture
def next_handoff(second_copy,monkeypatch):
    c=second_copy
    store=c['target'].parent/(c['target'].name+'-machine')
    shutil.copytree(c['store'],store)
    monkeypatch.setattr('workbench.book_ownership.default_machine_store',lambda:store)
    prepare_restored_workbench(c['package'],destination=c['target'],temporary_root=c['scratch'])
    owner=BookOwnership(c['project'].book_id,machine_store=store)
    handoff=RestoreHandoff(c['project'].book_id,machine_store=store)
    original=record(owner)
    assert original['version']==OWNERSHIP_V3
    value=handoff.preview(c['package'],destination=c['target'],temporary_root=c['scratch'])
    return {**c,'store':store,'owner':owner,'handoff':handoff,'original':original,'value2':value,
        'source_database':c['source_target']/'workbench.sqlite','source_root':c['source_target']/'book'}


def record(owner):
    return json.loads(owner.record_path.read_text(encoding='utf-8'))


def begin(c,service=None):
    return (service or c['handoff']).begin(c['package'],destination=c['target'],
        expected_preview_sha256=c['value2']['intent_sha256'],temporary_root=c['scratch'])


def deny(root):
    path=root/'.novelagent/runtime/second-handoff-denied.json'
    with pytest.raises((RuntimeError,ValueError,OSError)):
        atomic_append_json(path,{'must_not_write':True})
    assert not path.exists()


def rollback(c,service=None):
    return (service or c['handoff']).rollback(handoff_id=c['value2']['handoff_id'])


def test_actual_second_handoff_preserves_activation_and_rollback_restores_write_with_new_generation(next_handoff):
    c=next_handoff; value=c['value2']; source=inventory_tree(c['source_root']); target=inventory_tree(c['target']/'book')
    source_sql=logical(c['source_database']); target_sql=logical(c['target']/'workbench.sqlite')
    qualification=(c['source_target']/'write-qualification.json').read_bytes()
    assert value['schema_version']=='machine_restore_handoff_v2'
    assert value['source_restore_provenance']==c['plan']['source_restore_provenance']
    assert value['restoring_record']['version']==OWNERSHIP_V2
    assert 'restore_qualification_sha256' not in value['restoring_record']
    assert record(c['owner'])==c['original']
    begin(c)
    deny(c['source_root']); deny(c['target']/'book')
    with pytest.raises(RestoreScopeError): WorkbenchDatabase(c['source_database'],machine_store=c['store'])
    assert load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['store']).read_only
    assert c['handoff'].resume(c['package'],handoff_id=value['handoff_id'],temporary_root=c['scratch'])['source_revoked']
    result=rollback(c)
    assert result['source_record']['version']==OWNERSHIP_V3
    assert result['source_record']['generation']==c['original']['generation']+2
    assert result['source_record']['restore_qualification_sha256']==c['original']['restore_qualification_sha256']
    assert rollback(c)==result
    scope=load_restore_scope(c['source_database'],machine_store=c['store'])
    assert scope.active and scope.activation['qualification_sha256']==c['original']['restore_qualification_sha256']
    probe=c['source_root']/'.novelagent/runtime/rollback-generation-probe.json'
    try:
        atomic_append_json(probe,{'source_writable':True})
        assert json.loads(probe.read_text(encoding='utf-8'))=={'source_writable':True}
    finally:
        if probe.exists(): probe.unlink()
    deny(c['target']/'book')
    with pytest.raises(RestoreHandoffError):
        c['handoff'].resume(c['package'],handoff_id=value['handoff_id'],temporary_root=c['scratch'])
    # The original activation remains idempotent after an explicit renewal.
    assert RestoreActivation(c['project'].book_id,machine_store=c['store']).activate(
        c['package'],handoff_id=c['id'],temporary_root=c['scratch'],resume=True)['target_writable']
    calls=[]
    def forbidden(*args,**kwargs):calls.append(1);pytest.fail('rollback startup invoked executor')
    for _ in range(2):
        app=create_app(database_path=c['source_database'],start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['store'],reviewed_chapter_candidate_executor_factory=forbidden,
            reviewed_chapter_fact_extraction_executor_factory=forbidden)
        with TestClient(app) as client:
            assert client.get('/api/restore-scope').json()['state']=='active'
            assert client.get('/api/projects/'+c['project'].id+'/chapters/3').status_code==200
            assert app.state.supervisor._thread is None
    assert not calls and (c['source_target']/'write-qualification.json').read_bytes()==qualification
    assert inventory_tree(c['source_root'])==source and inventory_tree(c['target']/'book')==target
    assert logical(c['source_database'])==source_sql and logical(c['target']/'workbench.sqlite')==target_sql
    (REPO/'.tmp/t72-second-handoff-case.json').write_text(json.dumps({
        'source_target':str(c['source_target']),'target':str(c['target']),'machine':str(c['store']),
        'project_id':c['project'].id,'book_id':c['project'].book_id,'handoff_id':value['handoff_id'],
        'source_record':result['source_record'],'real_provider_calls':0},indent=2),encoding='utf-8')


@pytest.mark.parametrize('phase',['after_handoff_intent','after_source_revoked','after_revocation_receipt'])
def test_real_second_handoff_crash_resumes_original_revocation(next_handoff,phase):
    c=next_handoff; value=c['value2']
    code='''import os,sys
from workbench.restore_handoff import RestoreHandoff
def fault(phase,value):
 if phase==sys.argv[6]:os._exit(91)
RestoreHandoff(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).begin(
 sys.argv[3],destination=sys.argv[4],expected_preview_sha256=sys.argv[5],temporary_root=sys.argv[7])
'''
    child=subprocess.run([sys.executable,'-B','-c',code,c['project'].book_id,str(c['store']),
        str(c['package']),str(c['target']),value['intent_sha256'],phase,str(c['scratch'])],
        capture_output=True,timeout=180,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert child.returncode==91,child.stderr
    if phase=='after_handoff_intent': assert load_restore_scope(c['source_database'],machine_store=c['store']).active
    else: deny(c['source_root'])
    c['handoff'].resume(c['package'],handoff_id=value['handoff_id'],temporary_root=c['scratch'])
    deny(c['source_root']); deny(c['target']/'book')
    assert rollback(c)['rolled_back']


@pytest.mark.parametrize('phase',['after_rollback_intent','after_reinstatement_intent',
    'after_source_reactivated','after_reinstatement_completion'])
def test_real_second_rollback_crash_requires_completed_reinstatement(next_handoff,phase):
    c=next_handoff; begin(c)
    code='''import os,sys
from workbench.restore_handoff import RestoreHandoff
def fault(phase,value):
 if phase==sys.argv[4]:os._exit(92)
RestoreHandoff(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).rollback(handoff_id=sys.argv[3])
'''
    child=subprocess.run([sys.executable,'-B','-c',code,c['project'].book_id,str(c['store']),
        c['value2']['handoff_id'],phase],capture_output=True,timeout=180,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert child.returncode==92,child.stderr
    if phase!='after_reinstatement_completion':
        deny(c['source_root'])
        with pytest.raises(RestoreScopeError): WorkbenchDatabase(c['source_database'],machine_store=c['store'])
    else: assert load_restore_scope(c['source_database'],machine_store=c['store']).active
    assert rollback(c)['rolled_back']
    assert load_restore_scope(c['source_database'],machine_store=c['store']).active
    deny(c['target']/'book')


def test_second_handoff_source_absent_revokes_but_cannot_fabricate_rollback(next_handoff):
    c=next_handoff
    with absent_source(c['source_target']):
        begin(c)
        with pytest.raises((OSError,ValueError,RuntimeError)): rollback(c)
        assert record(c['owner'])==c['value2']['restoring_record']
        assert not c['source_target'].exists()
        assert not c['handoff']._path(c['value2']['handoff_id'],'rollback').exists()
    assert rollback(c)['rolled_back']


def test_new_machine_record_after_rollback_intent_is_never_overwritten(next_handoff):
    c=next_handoff; begin(c)
    newer=_signed({**c['value2']['restoring_record'],'generation':c['original']['generation']+20})
    def change(phase,value):
        if phase=='after_rollback_intent': atomic_write_json(c['owner'].record_path,newer)
    service=RestoreHandoff(c['project'].book_id,machine_store=c['store'],fault_injector=change)
    with pytest.raises(RestoreHandoffError): rollback(c,service)
    assert record(c['owner'])==newer
    deny(c['source_root']); deny(c['target']/'book')


@pytest.mark.parametrize('damage',['renewal_intent','renewal_done','original_rollback','original_qualification'])
def test_renewed_record_without_complete_original_evidence_refuses_write(next_handoff,damage):
    c=next_handoff; begin(c); result=rollback(c); current=result['source_record']
    paths={'renewal_intent':c['owner'].directory/reinstatement_name(current),
        'renewal_done':c['owner'].directory/reinstatement_name(current,done=True),
        'original_rollback':c['handoff']._path(c['value2']['handoff_id'],'rollback'),
        'original_qualification':c['source_target']/'write-qualification.json'}
    path=paths[damage]; raw=path.read_bytes(); path.unlink()
    try:
        deny(c['source_root'])
        with pytest.raises(RestoreScopeError): WorkbenchDatabase(c['source_database'],machine_store=c['store'])
        assert not path.exists()
    finally: path.write_bytes(raw)
    assert load_restore_scope(c['source_database'],machine_store=c['store']).active


def test_revoked_restored_source_usage_is_preserved_without_granting_normal_startup(next_handoff):
    c=next_handoff; source=inventory_tree(c['source_root']); sql=logical(c['source_database']); begin(c)
    usage=RestoreUsage(c['project'].book_id,machine_store=c['store'])
    result=usage.capture(handoff_id=c['value2']['handoff_id'])
    assert result['post_archive_usage_verified'] and result['settled']
    assert result['usage']['summary']['attempt_count']==3
    with absent_source(c['source_target']):
        assert usage.read_usage(handoff_id=c['value2']['handoff_id'])==result
    deny(c['source_root']); deny(c['target']/'book')
    assert inventory_tree(c['source_root'])==source and logical(c['source_database'])==sql
    assert rollback(c)['rolled_back']


def test_renewal_provenance_rearchives_and_verifies_offline_without_original_machine(next_handoff,monkeypatch):
    c=next_handoff; begin(c); rollback(c)
    db=WorkbenchDatabase(c['source_database'],machine_store=c['store'])
    proof=capture_restore_provenance(db,record(c['owner']),machine_store=c['store'])
    assert len(proof['qualification_reinstatements'])==1
    validate_restore_provenance(proof,root=c['source_root'],book_id=c['project'].book_id)
    archives=REPO/'.tmp'/('renewed-archive-'+uuid.uuid4().hex[:8]);archives.mkdir()
    def forbidden(*args,**kwargs):pytest.fail('archive called executor or opened live evidence offline')
    app=create_app(database_path=c['source_database'],start_worker=False,auto_import_active=False,
        chapter_publication_machine_store=c['store'],reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    with TestClient(app):
        gate=app.state.maintenance; operation=gate.begin()['operation_id']
        try:
            app.state.backups.archive_root=archives
            preview=app.state.backups.preview(c['project'].id,operation_id=operation)
            assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
            result=app.state.backups.capture(c['project'].id,operation_id=operation,
                expected_preview_sha256=preview['preview_sha256'])
        finally:gate.finish(operation)
    with ExitStack() as absent:
        absent.enter_context(absent_source(c['source_target'],temporary_name=c['source_target'].name+'-renewed-offline'))
        absent.enter_context(absent_source(c['root'],temporary_name=c['root'].name+'-renewed-offline'))
        absent.enter_context(absent_source(c['store'],temporary_name=c['store'].name+'-renewed-offline'))
        with monkeypatch.context() as patch:
            patch.setattr('workbench.book_ownership._record',forbidden)
            patch.setattr('workbench.restore_scope.load_restore_scope',forbidden)
            verified=verify_archived_target(Path(result['path']),project_id=c['project'].id,
                book_id=c['project'].book_id,temporary_root=c['scratch'])
            assert verified['offline_data_verified'] and not verified['blockers']


def test_open_original_restore_blocks_rollback_without_decision(next_handoff):
    c=next_handoff; begin(c)
    with closing(InstanceLease(c['source_target']).acquire()):
        with pytest.raises(InstanceAlreadyRunning):rollback(c)
    assert not c['handoff']._path(c['value2']['handoff_id'],'rollback').exists()
    assert record(c['owner'])==c['value2']['restoring_record']


@pytest.mark.parametrize('remaining_evidence',['job_history','machine_handoff_only'])
def test_revoked_restore_losing_all_local_scope_markers_never_becomes_ordinary_database(next_handoff,remaining_evidence):
    c=next_handoff; begin(c)
    local_names=['restore-intent.json','execution-policy.json','write-qualification.json',
        'workbench.sqlite.restore-scope.json']
    paths=[c['source_target']/name for name in local_names]
    if remaining_evidence=='machine_handoff_only':
        paths += [c['owner'].directory/('handoff-'+c['id']+'-'+suffix+'.json')
            for suffix in ('activation','activation-done')]
    original={path:path.read_bytes() for path in paths}
    original_database=c['source_database'].read_bytes()
    try:
        for path in paths:path.unlink()
        with closing(sqlite3.connect(c['source_database'])) as connection:
            connection.execute('DROP TABLE workbench_restore_scope')
            if remaining_evidence=='machine_handoff_only':
                connection.execute('DROP TABLE workbench_restored_job_history')
            connection.commit()
        with pytest.raises(RestoreScopeError):WorkbenchDatabase(c['source_database'],machine_store=c['store'])
        deny(c['source_root'])
        # The original ordinary V2 source has different root/DB coordinates.
        assert WorkbenchDatabase(c['db'].path,machine_store=c['store']).restore_scope is None
        assert all(not path.exists() for path in paths)
    finally:
        c['source_database'].write_bytes(original_database)
        for path,raw in original.items():path.write_bytes(raw)
    assert rollback(c)['rolled_back']
