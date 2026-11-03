"""Real public legacy registration must survive its explicit format upgrade.

The Strict calibration and author baseline here are synthetic test inputs;
this does not authorize a product upgrade or certify the diagnostic archive.
"""
from pathlib import Path
import json
import subprocess
import sys
import uuid

import pytest

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.reviewed_upgrade import prepare_reviewed_upgrade, prepare_reviewed_upgrade_transaction
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import baseline_request, capture_sources, digest
from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case, preview, endpoint, body, contents
from tests.test_backup_restore_database import logical
from tests.test_backup_archive_database import private_temporary_directory
from workbench.book_ownership import BookOwnership, WITNESS_PATH
from workbench.book_ownership import BookOwnershipError, _record
from workbench.registered_identity_upgrade import resume_registered_identity_upgrade, _name
from workbench.backup_files import inventory_tree


@pytest.fixture
def tmp_path(tmp_path,request):
    # Keep the existing long test paths for upgrade/fence tests. The separate
    # restore flow uses a machine store of supported default-like length;
    # existing handoff filenames exceed MAX_PATH in the long pytest wrapper.
    if request.node.originalname != 'test_upgraded_source_new_archive_rolls_back_and_restores_again':
        return tmp_path
    value=Path(__file__).resolve().parents[1]/'.tmp'/('常规升级 恢复-'+uuid.uuid4().hex[:8])
    value.mkdir()
    return value


def preparation(c,run_id='registered-upgrade-1'):
    old = (c['root']/'.novelagent/project.json').read_bytes()
    confirmed = baseline_request(book_id=c['project'].book_id, chapter=1, state=c['state'],
        sources=capture_sources(c['root']), old_identity_sha256=digest(old), static_references=[])
    return prepare_reviewed_upgrade(trusted_context=ChapterCandidateContextBuilder(c['root']).build(2),
        baseline_state=c['state'], confirmed_baseline=confirmed, static_references=[],
        calibration_report=c['report'], run_id=run_id)


def register(c):
    value = preview(c)
    response = c['client'].post(endpoint(c), json=body(c,value))
    assert response.status_code == 200, response.text
    owner = BookOwnership(c['project'].book_id, machine_store=c['machine'])
    with owner.locked(), c['app'].state.database.transaction() as connection:
        record = owner.require(root=c['root'], database=c['app'].state.database,
            project_id=c['project'].id, connection=connection)
    return owner, record


@pytest.mark.parametrize('registration_case',['1.0','2.0'],indirect=True)
def test_actual_registered_upgrade_preserves_write_ownership_and_original_backup(registration_case):
    c = registration_case
    owner, before = register(c)
    witness = (c['root']/WITNESS_PATH).read_bytes()
    archive = contents(c['package'])
    sql = logical(c['app'].state.database.path)
    p = preparation(c)
    tx = prepare_reviewed_upgrade_transaction(preparation=p)
    assert tx.commit()['committed']
    assert load_verified_reviewed_identity(c['root'])['identity'] == p['new_identity']
    with owner.locked(), c['app'].state.database.transaction() as connection:
        after = owner.require(root=c['root'], database=c['app'].state.database,
            project_id=c['project'].id, connection=connection)
    assert after['binding']['identity_sha256'] == p['binding']['new_identity_sha256']
    assert after['generation'] == before['generation'] + 1
    assert after['registration_id'] == before['registration_id']
    assert (c['root']/WITNESS_PATH).read_bytes() == witness
    assert logical(c['app'].state.database.path) == sql
    assert contents(c['package']) == archive
    with owner.locked(), c['app'].state.database.transaction() as connection:
        assert owner.enroll(root=c['root'], database=c['app'].state.database,
            project_id=c['project'].id, connection=connection) == after


def controls(owner):
    return {p.name:p.read_bytes() for p in owner.directory.glob('*.json')
            if p.name != 'workbench-instance.json'}


@pytest.mark.parametrize('event,index,committed,fenced',[
    ('after_identity_upgrade_intent',None,False,False),
    ('after_identity_upgrade_fence',None,False,True),
    ('after_apply_target',1,False,True),
    ('after_commit_marker',None,True,True),
    ('after_identity_upgrade_done',None,True,True),
    ('after_identity_upgrade_active',None,True,False),
])
def test_real_process_exit_keeps_exact_upgrade_fenced_until_explicit_continuation(
        registration_case,event,index,committed,fenced):
    c=registration_case;owner,before=register(c)
    p=preparation(c);tx=prepare_reviewed_upgrade_transaction(preparation=p)
    witness=(c['root']/WITNESS_PATH).read_bytes();archive=contents(c['package'])
    database=logical(c['app'].state.database.path)
    code=r'''
import json,os,sys
from pathlib import Path
from core.engine.persistence_v2 import PersistenceV2Transaction,load_persistence_manifest_v2,_root_map_from_manifest
import workbench.book_ownership as b
root=Path(sys.argv[1]);b.default_machine_store=lambda:Path(sys.argv[2])
event=sys.argv[3];index=json.loads(sys.argv[4])
m=load_persistence_manifest_v2(root/'journals/registered-upgrade-1/manifest.json')
def fault(name,position,path):
    if name==event and position==index:os._exit(92)
t=PersistenceV2Transaction(transaction_root=root,run_id='registered-upgrade-1',
    book_id=m['immutable']['book_id'],root_map=_root_map_from_manifest(m),fault_injector=fault)
t.commit()
raise AssertionError('requested real process exit was not reached')
'''
    child=subprocess.run([sys.executable,'-X','utf8','-c',code,str(tx.transaction_root),str(c['machine']),
        event,json.dumps(index)],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=90)
    assert child.returncode==92,child.stdout+child.stderr
    book=inventory_tree(c['root']);machine=controls(owner)
    if fenced:
        assert _record(owner.directory,owner.book_id)['state']=='identity_upgrading'
        from core.autonomy.common import atomic_append_json
        from core.engine.persistence_v2 import reconcile_pending_persistence_v2
        with pytest.raises(BookOwnershipError):
            atomic_append_json(c['root']/'.novelagent/runtime/unrelated-write.json',{'forbidden':True})
        with pytest.raises(BookOwnershipError):
            reconcile_pending_persistence_v2(tx.transaction_root,expected_book_id=owner.book_id)
        assert controls(owner)==machine and inventory_tree(c['root'])==book
    if event=='after_commit_marker':
        intent=owner.directory/_name(p['binding']['new_identity_sha256'],'intent')
        raw=intent.read_bytes();intent.unlink();missing=controls(owner)
        with pytest.raises(BookOwnershipError):
            resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)
        assert controls(owner)==missing and inventory_tree(c['root'])==book
        intent.write_bytes(raw)
    result=resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)
    assert result['committed'] is committed
    with owner.locked(),c['app'].state.database.transaction() as connection:
        after=owner.require(root=c['root'],database=c['app'].state.database,
            project_id=c['project'].id,connection=connection)
    assert after['generation']==before['generation']+int(committed)
    assert after['registration_id']==before['registration_id']
    assert after['binding']['identity_sha256']==p['binding'][
        'new_identity_sha256' if committed else 'old_identity_sha256']
    assert logical(c['app'].state.database.path)==database
    assert (c['root']/WITNESS_PATH).read_bytes()==witness
    assert contents(c['package'])==archive


@pytest.mark.parametrize('missing',['intent','done'])
def test_active_upgrade_missing_original_control_is_not_repaired(registration_case,missing):
    c=registration_case;owner,_=register(c);p=preparation(c)
    tx=prepare_reviewed_upgrade_transaction(preparation=p);assert tx.commit()['committed']
    path=owner.directory/_name(p['binding']['new_identity_sha256'],missing)
    raw=path.read_bytes();path.unlink();machine=controls(owner);book=inventory_tree(c['root'])
    with owner.locked(),c['app'].state.database.transaction() as connection:
        with pytest.raises(BookOwnershipError):
            owner.require(root=c['root'],database=c['app'].state.database,
                project_id=c['project'].id,connection=connection)
    with pytest.raises(BookOwnershipError):
        resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)
    assert controls(owner)==machine and inventory_tree(c['root'])==book
    path.write_bytes(raw)
    assert resume_registered_identity_upgrade(tx.transaction_root,tx.run_id)['committed']


def test_upgraded_source_new_archive_rolls_back_and_restores_again(registration_case):
    from fastapi.testclient import TestClient
    from tests.test_backup_archive import capture
    from tests.test_backup_restore_materialization import prepare
    from workbench.backup_restore_materialization import materialize_archived_restore
    from workbench.backup_restore_roots import bind_restored_roots
    from workbench.backup_restore_database import prepare_restored_workbench
    from workbench.restore_activation import RestoreActivation
    from workbench.restore_scope import load_restore_scope
    from workbench.app import create_app
    c=registration_case;owner,_=register(c)
    tx=prepare_reviewed_upgrade_transaction(preparation=preparation(c));assert tx.commit()['committed']
    book=inventory_tree(c['root']);sql=logical(c['app'].state.database.path)
    original_archive=contents(c['package'])
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    c['app'].state.maintenance.finish(c['op'])
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    for attempt in range(2):
        target=Path(__file__).resolve().parents[1]/'.tmp'/('iur-'+uuid.uuid4().hex[:8])
        plan=prepare({'project':c['project']},package,target,c['scratch'])
        materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
        bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
        prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
        intent=service.preview(package,destination=target,temporary_root=c['scratch'])
        handoff=intent['handoff_id']
        assert service.begin(package,destination=target,expected_preview_sha256=intent['intent_sha256'],
            temporary_root=c['scratch'])['source_revoked']
        if attempt==0:
            assert service.rollback(handoff_id=handoff)['rolled_back'] is True
            with owner.locked(),c['app'].state.database.transaction() as connection:
                assert owner.require(root=c['root'],database=c['app'].state.database,
                    project_id=c['project'].id,connection=connection)['generation']==4
            continue
        assert service.capture(handoff_id=handoff)['settled']
        for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
            method(package,handoff_id=handoff,temporary_root=c['scratch'])
        assert service.activate(package,handoff_id=handoff,temporary_root=c['scratch'])['target_writable']
        assert load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        for _ in range(2):
            app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
                chapter_publication_machine_store=c['machine'])
            with TestClient(app) as client:
                assert app.state.supervisor._thread is None
                response=client.get('/api/projects/'+c['project'].id+'/chapters/2')
                assert response.status_code==200,response.text
                assert response.json()['retained_author_draft']['text']==c['draft'].text
    assert inventory_tree(c['root'])==book and logical(c['app'].state.database.path)==sql
    assert contents(c['package'])==original_archive
