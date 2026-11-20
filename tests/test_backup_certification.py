"""Actual registered archive certification, independent of writable activation."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case,contents
from tests.test_registration_upgrade_archive import old_package,tmp_path,copied,activate,read_twice
from tests.test_restored_identity_upgrade import restored_legacy
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import logical
from workbench.backup_certification import RecoveryCertification,verify_recovery_certificate,CERTIFICATE
from workbench.backup_archive import verify_archive
from workbench.backup_archive import BackupArchiveError
from core.story_project.reviewed_state_document import digest


pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows same-machine certification protocol')


@pytest.mark.parametrize('registration_case',['1.0','2.0'],indirect=True)
def test_actual_public_registration_archive_certifies_without_mutating_original_package(registration_case):
    c=registration_case;package=old_package(c)
    version=json.loads((c['root']/'.novelagent/project.json').read_bytes())['schema_version']
    certifier=RecoveryCertification(c['app'].state.backups)
    original=contents(package);book=contents(c['root']);sql=logical(c['app'].state.database.path)
    preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert preview['can_certify'],preview['blockers']
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    assert result['recoverable'] and not result['write_activation_allowed']
    assert not result['automatic_provider_calls_allowed']
    after=contents(package);after.pop(CERTIFICATE)
    assert after==original
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
    assert verify_archive(package)['recoverable'] is False
    with absent_source(c['root']):
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
    # Certification attests the package and supported protocol; only the actual
    # original handoff/usage/budget/jobs/execution/activation can grant writers.
    c['app'].state.maintenance.finish(c['op'])
    from workbench.restore_activation import RestoreActivation
    target=c['machine'].parent/'认证 后恢复';copied(c,package,target)
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    activate(c,service,package,intent);read_twice(c,target,version)
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


def test_certification_rejects_never_registered_source_and_stale_drafts(registration_case):
    c=registration_case;certifier=RecoveryCertification(c['app'].state.backups)
    value=certifier.preview(c['project'].id,archive_id=c['package'].name,
        operation_id=c['op'],temporary_root=c['scratch'])
    assert not value['can_certify'] and 'registered_active_source_required' in {b['code'] for b in value['blockers']}
    with pytest.raises(BackupArchiveError):
        certifier.issue(c['project'].id,archive_id=c['package'].name,operation_id=c['op'],
            expected_assessment_sha256=value['assessment_sha256'],temporary_root=c['scratch'])
    assert not (c['package']/CERTIFICATE).exists()
    package=old_package(c)
    value=certifier.preview(c['project'].id,archive_id=package.name,
        operation_id=c['op'],temporary_root=c['scratch'])
    assert value['can_certify'],value['blockers']
    c['app'].state.jobs.upsert_draft(project_id=c['project'].id,chapter=2,text='认证前作者又修改了草稿')
    with pytest.raises(BackupArchiveError,match='偏离预览'):
        certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
            expected_assessment_sha256=value['assessment_sha256'],temporary_root=c['scratch'])
    current=certifier.preview(c['project'].id,archive_id=package.name,
        operation_id=c['op'],temporary_root=c['scratch'])
    assert not current['can_certify'] and 'source_changed_since_archive' in {b['code'] for b in current['blockers']}
    assert not (package/CERTIFICATE).exists()


def test_certificate_is_immutable_rechecked_and_cannot_grant_activation(registration_case):
    c=registration_case;package=old_package(c);certifier=RecoveryCertification(c['app'].state.backups)
    preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    args=dict(archive_id=package.name,operation_id=c['op'],expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    raw=(package/'archive.zip').read_bytes()
    def change(phase,value):
        if phase=='before_certificate':(package/'archive.zip').write_bytes(b'broken synthetic archive')
    certifier.fault_injector=change
    try:
        with pytest.raises(BackupArchiveError):certifier.issue(c['project'].id,**args)
        assert not (package/CERTIFICATE).exists()
    finally:(package/'archive.zip').write_bytes(raw)
    def lost(phase,value):
        if phase=='after_certificate':raise RuntimeError('lost certificate response')
    certifier.fault_injector=lost
    with pytest.raises(RuntimeError,match='lost certificate response'):certifier.issue(c['project'].id,**args)
    path=package/CERTIFICATE;original=path.read_bytes();stat=path.stat()
    certifier.fault_injector=None
    result=certifier.issue(c['project'].id,**args)
    assert result['recoverable'] and not result['write_activation_allowed']
    assert path.read_bytes()==original and (path.stat().st_ino,path.stat().st_mtime_ns)==(stat.st_ino,stat.st_mtime_ns)
    for damage in ('write_permission','wrong_build','broken','oversized'):
        if damage=='oversized':path.write_bytes(b' '*(4*1024**2+1))
        elif damage=='broken':path.write_bytes(b'not JSON')
        else:
            value=json.loads(original)
            if damage=='write_permission':value['write_activation_allowed']=True
            else:value['verifier']['implementation_sha256']='e'*64
            value['certificate_sha256']=digest({k:v for k,v in value.items() if k!='certificate_sha256'})
            path.write_text(json.dumps(value),encoding='utf-8')
        try:
            with pytest.raises(BackupArchiveError):
                verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,temporary_root=c['scratch'])
            before=path.read_bytes()
            with pytest.raises(BackupArchiveError):certifier.issue(c['project'].id,**args)
            assert path.read_bytes()==before
        finally:path.write_bytes(original)


def test_existing_auxiliary_history_is_not_silently_certified(registration_case):
    from tests.test_registered_reviewed_upgrade import register
    from tests.test_backup_archive import capture
    from tests.test_backup_autonomy_settlement import seed
    c=registration_case;register(c);store,_=seed(c)
    unknown=store.root/'stage_receipts/unclassified';unknown.mkdir(parents=True)
    (unknown/'receipt.json').write_text('{}')
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    certifier=RecoveryCertification(c['app'].state.backups)
    value=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert not value['can_certify']
    assert {'auxiliary_history_not_certified','auxiliary_history_unverified'} & {item['code'] for item in value['blockers']}
    with pytest.raises(BackupArchiveError):
        certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
            expected_assessment_sha256=value['assessment_sha256'],temporary_root=c['scratch'])
    assert not (package/CERTIFICATE).exists()
    assert verify_archive(package)['recoverable'] is False


def test_settled_delivery_and_inactive_autonomy_history_certify_without_dropping_records(registration_case):
    from tests.test_registered_reviewed_upgrade import register
    from tests.test_backup_archive import capture
    from tests.test_backup_auxiliary_settlement import queue_case
    from tests.test_backup_autonomy_settlement import seed
    c=registration_case;register(c);queue_case(c);seed(c)
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    book=contents(c['root']);sql=logical(c['app'].state.database.path)
    certifier=RecoveryCertification(c['app'].state.backups)
    value=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert value['can_certify'],value['blockers']
    assert {item['kind'] for item in value['auxiliary_history']['histories']}=={'delivery','autonomy'}
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=value['assessment_sha256'],temporary_root=c['scratch'])
    assert result['recoverable'] and not result['write_activation_allowed']
    with absent_source(c['root']):
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


def test_actual_model_backed_stage_archive_certifies_with_raw_call_evidence(registration_case):
    from tests.test_registered_reviewed_upgrade import register
    from tests.test_backup_archive import capture
    from tests.test_backup_stage_history import append_stage
    c=registration_case;register(c);append_stage(c,kind='model')
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    book=contents(c['root']);sql=logical(c['app'].state.database.path)
    certifier=RecoveryCertification(c['app'].state.backups)
    value=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert value['can_certify'],value['blockers']
    assert value['auxiliary_history']['histories'][0]['stage_groups']
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=value['assessment_sha256'],temporary_root=c['scratch'])
    with absent_source(c['root']):
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


def test_actual_registered_upgrade_and_completed_chapter_history_certify_offline(registration_case,monkeypatch):
    from tests.test_registered_reviewed_upgrade import register,preparation
    from tests import test_reviewed_chapter_commit as chapter_fixture
    from tests.test_backup_completion_history import complete_chapter,forbid_live
    from tests.test_backup_archive import capture
    from core.engine.reviewed_upgrade import prepare_reviewed_upgrade_transaction
    from workbench.reviewed_projects import ReviewedProjectRegistry
    c=registration_case;register(c)
    # Reuse the real candidate/fact/quality preparation against this publicly
    # registered DB/book, replacing only the fixture's isolated enrollment.
    def registered_upgrade(case):
        upgrade=preparation(c)
        assert prepare_reviewed_upgrade_transaction(preparation=upgrade).commit()['committed']
        reviewed=ReviewedProjectRegistry(c['app'].state.database,allowed_workspaces=[c['root'].parent])
        return c['app'].state.database,reviewed,c['project'],upgrade
    with monkeypatch.context() as fixture_patch:
        fixture_patch.setattr(chapter_fixture,'registered_upgrade',registered_upgrade)
        ready=chapter_fixture.ready.__wrapped__(c)
    complete_chapter(ready)
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    book=contents(c['root']);sql=logical(c['app'].state.database.path)
    certifier=RecoveryCertification(c['app'].state.backups)
    preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert preview['can_certify'],preview['blockers']
    history=next(item for item in preview['auxiliary_history']['histories'] if item['kind']=='autonomy')
    assert history['completion_groups'][0]['receipts'] and history['outline_groups'] and history['stage_groups']
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    forbid_live(monkeypatch)
    with absent_source(c['root']):
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


def test_custom_preview_only_autonomy_history_cannot_hide_from_certification(registration_case):
    from tests.test_registered_reviewed_upgrade import register
    from tests.test_backup_archive import capture
    from core.autonomy.session import AutonomySessionStore
    from core.autonomy.plans import build_source_snapshot,compile_instruction_plan
    from tests.test_autonomy_plans import trusted_profiles,NOW
    import copy
    from core.autonomy.profiles import TrustedProfiles
    c=registration_case;register(c)
    payload=copy.deepcopy(trusted_profiles().payload);payload['story_projects'][0]['book_id']=c['project'].book_id
    profiles=TrustedProfiles.from_dict(payload)
    source=build_source_snapshot(book_id=c['project'].book_id,root_uuid=payload['story_projects'][0]['root_uuid'],authority_epoch=2,
        authority_head_event_hash='2'*64,canonical_next_chapter=2,source_digest='1'*64,captured_at=NOW)
    plan=compile_instruction_plan('write 2 chapters',trusted_profiles=profiles,source_snapshot=source,created_at=NOW)
    store=AutonomySessionStore(c['root']/'custom-preview',trusted_profiles=profiles,reconcile_on_open=False)
    store.save_preview(plan)
    assert not (store.root/'leases').exists() and not (store.root/'sessions').exists()
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    certifier=RecoveryCertification(c['app'].state.backups)
    value=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert value['can_certify'],value['blockers']
    histories=value['auxiliary_history']['histories']
    assert len(histories)==1 and histories[0]['root']=='custom-preview'
    assert histories[0]['plans'][0]['plan_hash']==plan['plan_hash']
    assert value['certificate']['auxiliary_history_sha256']==value['auxiliary_history']['verification_sha256']
    assert not (package/CERTIFICATE).exists()
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=value['assessment_sha256'],temporary_root=c['scratch'])
    with absent_source(c['root']):
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result


def test_actual_upgraded_source_certifies_reviewed_archive(registration_case):
    from tests.test_registered_reviewed_upgrade import preparation,register
    from tests.test_backup_archive import capture
    from core.engine.reviewed_upgrade import prepare_reviewed_upgrade_transaction
    c=registration_case;register(c)
    assert prepare_reviewed_upgrade_transaction(preparation=preparation(c)).commit()['committed']
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    certifier=RecoveryCertification(c['app'].state.backups)
    preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert preview['can_certify'],preview['blockers']
    assert preview['certificate']['identity_sha256']==digest((c['root']/'.novelagent/project.json').read_bytes())
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    assert result['recoverable'] and not result['write_activation_allowed']


def test_qualified_restored_source_can_certify_its_upgraded_archive(restored_legacy):
    from tests.test_registered_reviewed_upgrade import preparation
    from tests.test_backup_archive import capture
    from core.engine.reviewed_upgrade import prepare_reviewed_upgrade_transaction
    c=restored_legacy
    assert prepare_reviewed_upgrade_transaction(preparation=preparation(c)).commit()['committed']
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    assert verify_archive(package)['manifest']['preview']['scope']['machine_ownership']['record']['version']=='machine_book_ownership_v3'
    book=contents(c['root']);sql=logical(c['database']);qualification=(c['root'].parent/'write-qualification.json').read_bytes()
    certifier=RecoveryCertification(c['app'].state.backups)
    preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert preview['can_certify'],preview['blockers']
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    with absent_source(c['root']):
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result
    assert contents(c['root'])==book and logical(c['database'])==sql
    assert (c['root'].parent/'write-qualification.json').read_bytes()==qualification


@pytest.mark.parametrize('phase',['before_certificate','after_certificate'])
def test_actual_process_exit_recertifies_explicitly_without_rewriting_existing_receipt(registration_case,phase):
    c=registration_case;package=old_package(c);book=contents(c['root']);sql=logical(c['app'].state.database.path)
    c['client'].__exit__(None,None,None);c['client_closed']=True
    code=r'''
import os,sys
from pathlib import Path
from fastapi.testclient import TestClient
from workbench.app import create_app
from workbench.backup_certification import RecoveryCertification
def forbidden(*a,**k):raise AssertionError('unexpected Provider/worker/import')
import anthropic,openai
anthropic.Anthropic=forbidden;openai.OpenAI=forbidden
from workbench.jobs import JobSupervisor
JobSupervisor._start_worker=forbidden
from workbench import app as module
module._import_active_project=forbidden
database,machine,book,project,archive,operation,scratch,runtime,phase=sys.argv[1:]
app=create_app(database_path=Path(database),runtime_root=Path(runtime),
    start_worker=False,auto_import_active=False,allowed_workspaces=[Path(book).parent],
    chapter_publication_machine_store=Path(machine))
with TestClient(app) as client:
 client.get('/')
 response=client.post('/api/maintenance/resume',json={'operation_id':operation})
 assert response.status_code==200,response.text
 certifier=RecoveryCertification(app.state.backups)
 preview=certifier.preview(project,archive_id=archive,operation_id=operation,temporary_root=Path(scratch))
 assert preview['can_certify'],preview['blockers']
 def fault(event,value):
  if event==phase:os._exit(96)
 certifier.fault_injector=fault
 result=certifier.issue(project,archive_id=archive,operation_id=operation,
     expected_assessment_sha256=preview['assessment_sha256'],temporary_root=Path(scratch))
 assert result['recoverable'] and not result['write_activation_allowed']
 print('CERTIFIED',flush=True)
'''
    args=[sys.executable,'-B','-c',code,str(c['app'].state.database.path),str(c['machine']),str(c['root']),
        c['project'].id,package.name,c['op'],str(c['scratch']),str(c['app'].state.supervisor.runtime_root),phase]
    kwargs=dict(capture_output=True,timeout=180)
    if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
    child=subprocess.run(args,**kwargs)
    assert child.returncode==96,child.stdout+child.stderr
    path=package/CERTIFICATE
    assert path.exists()==(phase=='after_certificate')
    before=(path.read_bytes(),path.stat().st_ino,path.stat().st_mtime_ns) if path.exists() else None
    args[-1]='no_exit'
    resumed=subprocess.run(args,**kwargs)
    assert resumed.returncode==0,resumed.stdout+resumed.stderr
    assert b'CERTIFIED' in resumed.stdout
    if before:assert (path.read_bytes(),path.stat().st_ino,path.stat().st_mtime_ns)==before
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
