"""Actual legacy restoration followed by its qualified format upgrade.

Only calibration/baseline inputs are synthetic; registration, restore,
ownership, activation, upgrade journals and renewal files are real protocols.
"""
from pathlib import Path
import json
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case, contents
from tests.test_registered_reviewed_upgrade import register, preparation
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive import capture
from tests.test_backup_restore_materialization import prepare
from tests.test_backup_restore_database import logical
from workbench.backup_files import inventory_tree
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.restore_activation import RestoreActivation
from workbench.restore_scope import load_restore_scope
from workbench.book_ownership import BookOwnership, WITNESS_PATH, _record
from workbench.app import create_app
from core.engine.reviewed_upgrade import prepare_reviewed_upgrade_transaction


@pytest.fixture
def tmp_path():
    root=Path(__file__).resolve().parents[1]/'.tmp'/('恢复升级 '+uuid.uuid4().hex[:8])
    root.mkdir()
    return root


@pytest.fixture
def restored_legacy(registration_case,tmp_path):
    c=registration_case;register(c)
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    c['app'].state.maintenance.finish(c['op'])
    source_book=inventory_tree(c['root']);source_sql=logical(c['app'].state.database.path)
    archive=contents(package);target=tmp_path/'恢复 来源'
    plan=prepare({'project':c['project']},package,target,c['scratch'])
    materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
    bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
    prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    handoff=intent['handoff_id']
    service.begin(package,destination=target,expected_preview_sha256=intent['intent_sha256'],temporary_root=c['scratch'])
    assert service.capture(handoff_id=handoff)['settled']
    for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
        method(package,handoff_id=handoff,temporary_root=c['scratch'])
    assert service.activate(package,handoff_id=handoff,temporary_root=c['scratch'])['target_writable']
    app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'])
    client=TestClient(app);client.__enter__()
    value={**c,'original':c,'root':target/'book','database':target/'workbench.sqlite',
        'app':app,'client':client,'package':package}
    try:
        assert app.state.supervisor._thread is None
        operation=app.state.maintenance.begin(purpose='backup')['operation_id']
        value['op']=operation
        yield value
    finally:
        if not value.get('client_closed'):client.__exit__(None,None,None)
    assert inventory_tree(c['root'])==source_book
    assert logical(c['app'].state.database.path)==source_sql
    assert contents(package)==archive


def close_source(c):
    c['app'].state.maintenance.finish(c['op'])
    c['client'].__exit__(None,None,None);c['client_closed']=True


def restart_source(c):
    close_source(c)
    for _ in range(2):
        app=create_app(database_path=c['database'],start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            assert app.state.supervisor._thread is None
            response=client.get('/api/projects/'+c['project'].id+'/chapters/2')
            assert response.status_code==200,response.text
            assert response.json()['retained_author_draft']['text']==c['draft'].text


@pytest.mark.parametrize('registration_case',['1.0','2.0'],indirect=True)
def test_actual_restored_legacy_upgrade_keeps_original_qualification_and_restarts(restored_legacy):
    c=restored_legacy;owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    before=load_restore_scope(c['database'],machine_store=c['machine'])
    assert before.active
    qualification=(c['root'].parent/'write-qualification.json').read_bytes()
    witness=(c['root']/WITNESS_PATH).read_bytes();sql=logical(c['database'])
    p=preparation(c);tx=prepare_reviewed_upgrade_transaction(preparation=p)
    assert tx.commit()['committed']
    with owner.locked(),c['app'].state.database.transaction() as connection:
        record=owner.require(root=c['root'],database=c['app'].state.database,
            project_id=c['project'].id,connection=connection)
    assert record['version']=='machine_book_ownership_v3'
    assert record['binding']['identity_sha256']==p['binding']['new_identity_sha256']
    assert record['restore_qualification_sha256']==before.activation['qualification_sha256']
    assert load_restore_scope(c['database'],machine_store=c['machine']).active
    assert (c['root'].parent/'write-qualification.json').read_bytes()==qualification
    assert (c['root']/WITNESS_PATH).read_bytes()==witness and logical(c['database'])==sql
    restart_source(c)


def machine_controls(c):
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    return {p.name:p.read_bytes() for p in owner.directory.glob('*.json') if p.name!='workbench-instance.json'}


def child_exit(c,tx,event,index=None,*,resume=False):
    code=r'''
import json,os,sys
from pathlib import Path
from core.engine.persistence_v2 import PersistenceV2Transaction,load_persistence_manifest_v2,_root_map_from_manifest
from workbench.registered_identity_upgrade import resume_registered_identity_upgrade
import workbench.book_ownership as b
root=Path(sys.argv[1]);run=sys.argv[2];b.default_machine_store=lambda:Path(sys.argv[3])
event=sys.argv[4];index=json.loads(sys.argv[5])
def fault(name,position,path):
    if name==event and position==index:os._exit(94)
if sys.argv[6]=='resume':resume_registered_identity_upgrade(root,run,fault_injector=fault)
else:
 m=load_persistence_manifest_v2(root/'journals'/run/'manifest.json')
 PersistenceV2Transaction(transaction_root=root,run_id=run,book_id=m['immutable']['book_id'],
     root_map=_root_map_from_manifest(m),fault_injector=fault).commit()
raise AssertionError('requested real exit not reached')
'''
    child=subprocess.run([sys.executable,'-X','utf8','-c',code,str(tx.transaction_root),tx.run_id,
        str(c['machine']),event,json.dumps(index),'resume' if resume else 'commit'],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=120)
    assert child.returncode==94,child.stdout+child.stderr


def test_qualified_upgrade_six_real_exits_explicit_recovery_and_missing_proofs(restored_legacy):
    from core.autonomy.common import atomic_append_json
    from core.engine.persistence_v2 import reconcile_pending_persistence_v2
    from workbench.registered_identity_upgrade import resume_registered_identity_upgrade,_name
    from workbench.restore_reinstatement import reinstatement_name
    c=restored_legacy;owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    qualification=(c['root'].parent/'write-qualification.json').read_bytes()
    witness=(c['root']/WITNESS_PATH).read_bytes();sql=logical(c['database'])
    for attempt,(event,index) in enumerate([('after_identity_upgrade_fence',None),('after_apply_target',1)]):
        tx=prepare_reviewed_upgrade_transaction(preparation=preparation(c,'qualified-pre-'+str(attempt)))
        child_exit(c,tx,event,index)
        book=inventory_tree(c['root']);controls=machine_controls(c)
        with pytest.raises((ValueError,RuntimeError)):
            atomic_append_json(c['root']/'.novelagent/runtime/forbidden-write.json',{'forbidden':True})
        with pytest.raises((ValueError,RuntimeError)):
            reconcile_pending_persistence_v2(tx.transaction_root,expected_book_id=owner.book_id)
        assert inventory_tree(c['root'])==book and machine_controls(c)==controls
        assert not resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)['committed']
        assert load_restore_scope(c['database'],machine_store=c['machine']).active
    p=preparation(c,'qualified-committed');tx=prepare_reviewed_upgrade_transaction(preparation=p)
    child_exit(c,tx,'after_commit_marker')
    for event in ('after_qualified_upgrade_intent','after_qualified_upgrade_done','after_identity_upgrade_active'):
        child_exit(c,tx,event,resume=True)
    assert resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)['committed']
    record=_record(owner.directory,owner.book_id)
    assert record['generation']==4 and record['state']=='active'
    original_activation=json.loads(qualification)['handoff_id']
    receipt=c['root']/'.novelagent/runtime/reviewed/receipts'/('qualified-committed.json')
    paths=[owner.directory/_name(p['binding']['new_identity_sha256'],suffix) for suffix in ('intent','done')]
    paths += [owner.directory/reinstatement_name(record,done=completed) for completed in (False,True)]
    paths += [c['root'].parent/'write-qualification.json',owner.directory/('handoff-'+original_activation+'-activation-done.json'),receipt]
    for path in paths:
        raw=path.read_bytes();path.unlink();controls=machine_controls(c);book=contents(c['root'])
        try:
            try:
                disabled=load_restore_scope(c['database'],machine_store=c['machine'])
            except (ValueError,RuntimeError,OSError):
                disabled=None
            assert disabled is None or not disabled.active
            with pytest.raises((ValueError,RuntimeError,OSError)):
                atomic_append_json(c['root']/'.novelagent/runtime/forbidden-proof-loss.json',{'forbidden':True})
            with pytest.raises((ValueError,RuntimeError,OSError)):
                resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)
            assert machine_controls(c)==controls and contents(c['root'])==book and not path.exists()
        finally:
            path.write_bytes(raw)
        assert load_restore_scope(c['database'],machine_store=c['machine']).active
    assert logical(c['database'])==sql
    assert (c['root'].parent/'write-qualification.json').read_bytes()==qualification
    assert (c['root']/WITNESS_PATH).read_bytes()==witness
    restart_source(c)


def test_qualified_pre_upgrade_archive_restores_old_format_after_actual_upgrade(restored_legacy,tmp_path):
    c=restored_legacy
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    archive=contents(package)
    p=preparation(c);tx=prepare_reviewed_upgrade_transaction(preparation=p);assert tx.commit()['committed']
    close_source(c)
    book=inventory_tree(c['root']);sql=logical(c['database']);target=tmp_path/'退回旧 格式'
    plan=prepare({'project':c['project']},package,target,c['scratch'])
    materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
    bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
    prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    assert intent['schema_version']=='machine_restore_handoff_v4'
    assert intent['archived_source_restore_provenance']['active_record']['generation']==3
    assert intent['source_record']['generation']==4
    handoff=intent['handoff_id']
    service.begin(package,destination=target,expected_preview_sha256=intent['intent_sha256'],temporary_root=c['scratch'])
    assert service.capture(handoff_id=handoff)['settled']
    for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
        method(package,handoff_id=handoff,temporary_root=c['scratch'])
    assert service.activate(package,handoff_id=handoff,temporary_root=c['scratch'])['target_writable']
    assert json.loads((target/'book/.novelagent/project.json').read_bytes())['schema_version']=='1.0'
    for _ in range(2):
        app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            assert app.state.supervisor._thread is None
            response=client.get('/api/projects/'+c['project'].id+'/chapters/2')
            assert response.status_code==200,response.text
            assert response.json()['retained_author_draft']['text']==c['draft'].text
    assert inventory_tree(c['root'])==book and logical(c['database'])==sql and contents(package)==archive


def test_upgraded_qualified_archive_keeps_mixed_upgrade_rollback_chain_and_restores(restored_legacy,tmp_path):
    c=restored_legacy
    p=preparation(c);tx=prepare_reviewed_upgrade_transaction(preparation=p);assert tx.commit()['committed']
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    archive=contents(package);close_source(c)
    book=inventory_tree(c['root']);sql=logical(c['database'])
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    for attempt in range(2):
        target=tmp_path/('新版恢复 '+str(attempt))
        plan=prepare({'project':c['project']},package,target,c['scratch'])
        materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
        bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
        prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
        intent=service.preview(package,destination=target,temporary_root=c['scratch'])
        proof=intent['source_restore_provenance']
        assert len(proof['qualification_reinstatements'])==attempt+1
        handoff=intent['handoff_id']
        service.begin(package,destination=target,expected_preview_sha256=intent['intent_sha256'],temporary_root=c['scratch'])
        if attempt==0:
            assert service.rollback(handoff_id=handoff)['rolled_back']
            assert _record(owner.directory,owner.book_id)['generation']==6
            assert load_restore_scope(c['database'],machine_store=c['machine']).active
            continue
        assert intent['schema_version']=='machine_restore_handoff_v4'
        assert service.capture(handoff_id=handoff)['settled']
        for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
            method(package,handoff_id=handoff,temporary_root=c['scratch'])
        assert service.activate(package,handoff_id=handoff,temporary_root=c['scratch'])['target_writable']
        assert json.loads((target/'book/.novelagent/project.json').read_bytes())['schema_version']=='3.0'
        for _ in range(2):
            app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
                chapter_publication_machine_store=c['machine'])
            with TestClient(app) as client:
                assert app.state.supervisor._thread is None
                response=client.get('/api/projects/'+c['project'].id+'/chapters/2')
                assert response.status_code==200,response.text
                assert response.json()['retained_author_draft']['text']==c['draft'].text
    assert inventory_tree(c['root'])==book and logical(c['database'])==sql and contents(package)==archive
