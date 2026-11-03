"""Actual confirmed-review/new-certificate upgrade admission, no public shortcut."""
from pathlib import Path

import pytest

from tests.test_legacy_registration import registration_case,contents
from tests.test_legacy_restore_identity import legacy
from tests.test_registration_upgrade_archive import tmp_path,old_package
from tests.test_author_baseline_archive import seed
from tests.test_backup_archive import capture
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import logical
from tests.test_backup_external_writers import child
from workbench.backup_certification import RecoveryCertification,CERTIFICATE
from workbench.book_ownership import BookOwnership,_record,WITNESS_PATH
from workbench.onboarding_upgrade import OnboardingUpgradeService,OnboardingUpgradeError
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest


@pytest.fixture
def upgrade_case(registration_case,request):
    c=registration_case;old=old_package(c);store,review=seed(c)
    mode=getattr(request,'param',None)
    if mode=='draft':
        from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
        review=store.revise(review['id'],project_id=c['project'].id,expected_revision=review['revision'],
            expected_sha256=review['record_sha256'],trusted_context=ChapterCandidateContextBuilder(c['root']).build(2),state=c['state'])
    if mode in {'queued','held'}:
        c['app'].state.maintenance.finish(c['op'])
        c['pending_job']=c['app'].state.jobs.create_job(project_id=c['project'].id,kind='generation_session',
            start_chapter=2,end_chapter=2,held=mode=='held')
        c['op']=c['app'].state.maintenance.begin()['operation_id']
    package=capture({'service':c['app'].state.backups,'project':c['project'],'op':c['op']})
    certifier=RecoveryCertification(c['app'].state.backups)
    preview=certifier.preview(c['project'].id,archive_id=package['archive_id'],operation_id=c['op'],temporary_root=c['scratch'])
    assert preview['can_certify'],preview['blockers']
    certificate=certifier.issue(c['project'].id,archive_id=package['archive_id'],operation_id=c['op'],
        expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    service=OnboardingUpgradeService(c['app'].state.backups,temporary_root=c['scratch'])
    request=dict(operation_id=c['op'],archive_id=package['archive_id'],baseline_id=review['id'],
        expected_revision=review['revision'],expected_sha256=review['record_sha256'])
    return {**c,'old_package':old,'review':review,'review_store':store,'new_package':package,
        'certificate':certificate,'upgrade':service,'request':request}


def preview(c):return c['upgrade'].preview(c['project'].id,**c['request'])


def commit(c,value,**overrides):
    body={**c['request'],'expected_preview_sha256':value['preview_sha256'],
        'acknowledged_policy_sha256':value['recovery_policy_sha256'],'confirmed':True,**overrides}
    return c['upgrade'].commit(c['project'].id,**body)


@pytest.mark.parametrize('registration_case',['1.0','2.0'],indirect=True)
def test_real_certified_upgrade_preserves_original_book_sql_review_and_registration(upgrade_case):
    c=upgrade_case;service=c['upgrade'];book=contents(c['root']);sql=logical(c['app'].state.database.path)
    archive=contents(c['old_package']);fresh=contents(c['app'].state.backups.archive_root/c['new_package']['archive_id'])
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine']);before=_record(owner.directory,owner.book_id)
    value=preview(c);assert preview(c)==value and contents(c['root'])==book
    assert logical(c['app'].state.database.path)==sql and not list(owner.directory.glob('oug-*.json'))
    assert value['can_upgrade'] and value['fact_counts']['characters']==len(c['state']['characters'])
    assert value['recovery_policy']['restore']['source_unavailable_write_activation_supported'] is False
    assert value['certificate_sha256']==c['certificate']['certificate_sha256']
    result=commit(c,value);assert result['committed'] and result['identity_version']=='3.0'
    verified=load_verified_reviewed_identity(c['root'])
    assert verified['identity']['book_id']==c['project'].book_id
    after=_record(owner.directory,owner.book_id)
    assert after['generation']==before['generation']+1 and after['registration_id']==before['registration_id']
    assert after['binding']['identity_sha256']==value['new_identity_sha256']
    actual=contents(c['root'])
    for name,sha in book.items():
        if name!='.novelagent/project.json':assert actual[name]==sha,name
    assert logical(c['app'].state.database.path)==sql
    assert c['review_store'].history(c['review']['id'],project_id=c['project'].id)[-1]==c['review']
    assert contents(c['old_package'])==archive
    assert contents(c['app'].state.backups.archive_root/c['new_package']['archive_id'])==fresh
    assert not result['automatic_provider_calls_allowed'] and result['fresh_chapter_preparation_required']
    intent=service._read_intent(owner,value['preview_sha256'])
    assert intent['intent_sha256']==result['intent_sha256'] and intent['preview']==value
    assert (c['root']/WITNESS_PATH).exists()
    completed_book=contents(c['root']);completed_controls=contents(owner.directory)
    assert service.completed_result(c['project'].id,preview_sha256=value['preview_sha256'])['result']==result
    # A lost response must return the exact original result, even after the
    # source archive certificate is unavailable. It grants no new write.
    cert_path=Path(c['new_package']['path'])/CERTIFICATE;cert_raw=cert_path.read_bytes();cert_path.unlink()
    try:
        assert commit(c,value)==result
        with pytest.raises(OnboardingUpgradeError,match='重复请求'):
            commit(c,value,acknowledged_policy_sha256='f'*64)
    finally:cert_path.write_bytes(cert_raw)
    assert contents(c['root'])==completed_book and contents(owner.directory)==completed_controls
    assert logical(c['app'].state.database.path)==sql
    # Existence alone is not completion; damaged original evidence cannot
    # trigger repair or fall through to a second upgrade.
    preparation=c['root']/'.novelagent/runtime/reviewed/upgrades'/(value['run_id']+'-preparation.json')
    raw=preparation.read_bytes();preparation.write_text('{}',encoding='utf-8')
    try:
        damaged=contents(c['root'])
        with pytest.raises((ValueError,RuntimeError)):commit(c,value)
        assert contents(c['root'])==damaged and contents(owner.directory)==completed_controls
    finally:preparation.write_bytes(raw)


def test_upgrade_requires_explicit_current_preview_and_recovery_policy_without_side_effects(upgrade_case):
    c=upgrade_case;value=preview(c);book=contents(c['root']);sql=logical(c['app'].state.database.path)
    for body in ({'confirmed':False},{'confirmed':'true'},{'acknowledged_policy_sha256':'f'*64},
                 {'expected_preview_sha256':'f'*64},{'expected_revision':1},
                 {'expected_sha256':'f'*64},{'expected_revision':True}):
        with pytest.raises((ValueError,RuntimeError)):commit(c,value,**body)
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    assert not list(owner.directory.glob('oug-*.json'))


@pytest.mark.parametrize('event,expected',[('after_apply_target','rolled_back'),('after_commit_marker','completed')])
@pytest.mark.parametrize('real_exit',[False,True],ids=['signal','process_exit'])
def test_explicit_original_upgrade_continuation_uses_commit_marker_and_query_is_read_only(upgrade_case,event,expected,real_exit):
    c=upgrade_case;value=preview(c);service=c['upgrade']
    assert service.operation(c['project'].id,preview_sha256=value['preview_sha256'])=={'state':'not_started','can_continue':False}
    class InterruptedUpgrade(BaseException):pass
    def fault(name,index,path):
        if name==event:raise InterruptedUpgrade('simulated interrupted upgrade')
    if real_exit:
        def stop_prepared(name):
            if name=='after_onboarding_prepare':raise InterruptedUpgrade('simulated interrupted upgrade')
        service.fault_injector=stop_prepared
    else:service.transaction_fault=fault
    with pytest.raises(InterruptedUpgrade,match='simulated interrupted'):commit(c,value)
    service.transaction_fault=None;service.fault_injector=None
    if real_exit:
        # Start from the exact journal prepared by the real product admission.
        # The independent process dies without Python context-manager cleanup.
        code='''import os,sys
from pathlib import Path
import workbench.book_ownership as b
from core.engine.persistence_v2 import PersistenceV2Transaction,load_persistence_manifest_v2,_root_map_from_manifest
b.default_machine_store=lambda:Path(sys.argv[2])
root=Path(sys.argv[1]);run=sys.argv[3];event=sys.argv[4]
manifest=load_persistence_manifest_v2(root/'journals'/run/'manifest.json')
def fault(name,index,path):
 if name==event:os._exit(92)
tx=PersistenceV2Transaction(transaction_root=root,run_id=run,book_id=manifest['immutable']['book_id'],
 root_map=_root_map_from_manifest(manifest),fault_injector=fault)
tx.commit()
raise AssertionError('requested process exit was not reached')
'''
        result=child(code,c['root']/'.novelagent/runtime/reviewed/p',c['machine'],value['run_id'],event)
        assert result.returncode==92,result.stdout+result.stderr
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    before=contents(c['root']);machine=contents(owner.directory);sql=logical(c['app'].state.database.path)
    state=service.operation(c['project'].id,preview_sha256=value['preview_sha256'])
    assert state['state']==('publication_pending' if expected=='completed' else 'rollback_pending')
    assert state['can_continue']
    assert contents(c['root'])==before and contents(owner.directory)==machine
    body=dict(operation_id=c['op'],preview_sha256=value['preview_sha256'],
        acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
    for overrides in ({'confirmed':False},{'operation_id':'f'*32},{'acknowledged_policy_sha256':'f'*64}):
        with pytest.raises((ValueError,RuntimeError)):
            service.continue_operation(c['project'].id,**{**body,**overrides})
    assert contents(c['root'])==before and contents(owner.directory)==machine
    outcome=service.continue_operation(c['project'].id,**body)
    assert outcome['state']==expected
    assert _record(owner.directory,owner.book_id)['state']=='active'
    assert _record(owner.directory,owner.book_id)['generation']==(2 if expected=='completed' else 1)
    assert logical(c['app'].state.database.path)==sql
    if expected=='completed':assert commit(c,value)==outcome['result']


def test_final_source_recheck_precedes_upgrade_intent_and_all_upgrade_writes(upgrade_case):
    c=upgrade_case;value=preview(c);identity=(c['root']/'.novelagent/project.json').read_bytes()
    def fault(event):
        assert event=='before_source_verification'
        (c['root']/'late-manual-note.txt').write_text('manual edit after preview',encoding='utf-8')
    c['upgrade'].fault_injector=fault
    with pytest.raises((ValueError,RuntimeError)):commit(c,value)
    assert (c['root']/'.novelagent/project.json').read_bytes()==identity
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    assert not list(owner.directory.glob('oug-*.json')) and _record(owner.directory,owner.book_id)['generation']==1


@pytest.mark.parametrize('event,state',[('after_onboarding_intent','intent_saved'),
    ('after_temporary_journal_created','preparation_interrupted'),
    ('before_journal_publish','preparation_interrupted'),('after_onboarding_prepare','prepared')])
def test_preparation_continuation_preserves_original_operation_and_unrelated_runs(upgrade_case,event,state):
    c=upgrade_case;service=c['upgrade'];value=preview(c)
    class InterruptedPreparation(BaseException):pass
    def product_fault(name):
        if name==event:raise InterruptedPreparation()
    def tx_fault(name,index,path):
        if name==event:raise InterruptedPreparation()
    service.fault_injector=product_fault;service.transaction_fault=tx_fault
    with pytest.raises(InterruptedPreparation):commit(c,value)
    service.fault_injector=None;service.transaction_fault=None
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    original=service._read_intent(owner,value['preview_sha256'])
    txroot=c['root']/'.novelagent/runtime/reviewed/p'
    # Before any root registry was created, an unknown staged transaction in
    # that root correctly forbids enrollment. Exercise other roots in this
    # case, and other runs in the same original registered root thereafter.
    unrelated=(txroot.parent/'other-journal/staging/unrelated-run' if event=='after_onboarding_intent'
        else txroot/'staging/unrelated-run')
    unrelated.mkdir(parents=True)
    (unrelated/'keep.txt').write_text('unrelated incomplete preparation',encoding='utf-8')
    before=contents(c['root']);controls=contents(owner.directory);sql=logical(c['app'].state.database.path)
    assert service.operation(c['project'].id,preview_sha256=value['preview_sha256'])['state']==state
    assert contents(c['root'])==before and contents(owner.directory)==controls
    result=service.continue_operation(c['project'].id,operation_id=c['op'],preview_sha256=value['preview_sha256'],
        acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
    assert result['state']=='completed' and result['result']['run_id']==value['run_id']
    assert service._read_intent(owner,value['preview_sha256'])==original
    assert (unrelated/'keep.txt').read_text('utf-8')=='unrelated incomplete preparation'
    if event=='after_temporary_journal_created':assert (txroot/'abandoned'/value['run_id']).is_dir()
    assert logical(c['app'].state.database.path)==sql


@pytest.mark.parametrize('event,place',[('after_temporary_journal_created','staging'),
    ('before_journal_publish','staging'),('after_onboarding_prepare','journals')])
def test_lost_preparation_directory_or_progress_is_never_recreated(upgrade_case,event,place):
    c=upgrade_case;service=c['upgrade'];value=preview(c)
    class LostPreparation(BaseException):pass
    def stop(name,*args):
        if name==event:raise LostPreparation()
    service.fault_injector=stop;service.transaction_fault=stop
    with pytest.raises(LostPreparation):commit(c,value)
    service.fault_injector=None;service.transaction_fault=None
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    target=c['root']/'.novelagent/runtime/reviewed/p'/place/value['run_id']
    progress=owner.directory/('oup-'+value['preview_sha256'][:16]+'.json')
    paths=[target,progress]
    if event!='after_temporary_journal_created':paths.append(target/'manifest.json')
    for index,path in enumerate(paths):
        retained=c['root'].parent/('retained-proof-'+str(index))
        workspace=Path(__file__).resolve().parents[1]/'.tmp'
        assert path.resolve().is_relative_to(workspace) and retained.resolve().is_relative_to(workspace)
        path.rename(retained)
        try:
            book=contents(c['root']);machine=contents(owner.directory);sql=logical(c['app'].state.database.path)
            with pytest.raises((ValueError,RuntimeError,OSError)):
                service.operation(c['project'].id,preview_sha256=value['preview_sha256'])
            with pytest.raises((ValueError,RuntimeError,OSError)):
                service.continue_operation(c['project'].id,operation_id=c['op'],preview_sha256=value['preview_sha256'],
                    acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
            assert not path.exists()
            assert contents(c['root'])==book and contents(owner.directory)==machine
            assert logical(c['app'].state.database.path)==sql
        finally:retained.rename(path)
    if event!='after_temporary_journal_created':
        manifest=target/'manifest.json';raw=manifest.read_bytes();manifest.write_text('{}',encoding='utf-8')
        try:
            book=contents(c['root']);machine=contents(owner.directory)
            with pytest.raises((ValueError,RuntimeError,OSError)):
                service.continue_operation(c['project'].id,operation_id=c['op'],preview_sha256=value['preview_sha256'],
                    acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
            assert contents(c['root'])==book and contents(owner.directory)==machine
        finally:manifest.write_bytes(raw)


@pytest.mark.parametrize('upgrade_case',['draft'],indirect=True)
def test_fresh_actual_certificate_cannot_replace_explicit_author_confirmation(upgrade_case):
    c=upgrade_case;book=contents(c['root']);sql=logical(c['app'].state.database.path)
    assert c['review']['status']=='draft'
    with pytest.raises(OnboardingUpgradeError,match='尚未确认'):preview(c)
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


@pytest.mark.parametrize('missing_registry',[False,True],ids=['unknown_staging','lost_root_registry'])
def test_unbound_or_lost_original_root_refuses_before_write_locks(upgrade_case,missing_registry):
    c=upgrade_case;service=c['upgrade'];value=preview(c)
    event='after_onboarding_prepare' if missing_registry else 'after_onboarding_intent'
    class InterruptedRoot(BaseException):pass
    def stop(name):
        if name==event:raise InterruptedRoot()
    service.fault_injector=stop
    with pytest.raises(InterruptedRoot):commit(c,value)
    service.fault_injector=None
    txroot=c['root']/'.novelagent/runtime/reviewed/p'
    registry=txroot/'root_registry.json'
    raw=registry.read_bytes() if missing_registry else None
    if missing_registry:registry.unlink()
    else:
        unknown=txroot/'staging/unknown-run';unknown.mkdir(parents=True)
        (unknown/'keep.txt').write_text('unbound transaction must remain',encoding='utf-8')
    try:
        owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
        book=contents(c['root']);machine=contents(owner.directory);sql=logical(c['app'].state.database.path)
        with pytest.raises(OnboardingUpgradeError,match='根登记'):
            service.continue_operation(c['project'].id,operation_id=c['op'],preview_sha256=value['preview_sha256'],
                acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
        assert contents(c['root'])==book and contents(owner.directory)==machine and logical(c['app'].state.database.path)==sql
        assert not registry.exists()
    finally:
        if raw is not None:registry.write_bytes(raw)


def test_missing_certificate_and_later_real_draft_sql_change_refuse_upgrade(upgrade_case):
    c=upgrade_case;value=preview(c);book=contents(c['root'])
    certificate=Path(c['new_package']['path'])/CERTIFICATE;raw=certificate.read_bytes();certificate.unlink()
    try:
        with pytest.raises((ValueError,RuntimeError)):commit(c,value)
    finally:certificate.write_bytes(raw)
    text='source author draft changed after certification'
    with c['app'].state.database.transaction() as connection:
        connection.execute('UPDATE drafts SET text=?,sha256=? WHERE id=?',(text,digest(text.encode('utf-8')),c['draft'].id))
    sql=logical(c['app'].state.database.path)
    with pytest.raises(OnboardingUpgradeError,match='备份已偏离'):commit(c,value)
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


@pytest.mark.parametrize('event,expected',[('before_journal_publish','preparation_interrupted'),
    ('after_commit_marker','publication_pending')])
def test_real_workbench_process_exit_and_cold_restart_explicitly_resume_original_upgrade(upgrade_case,event,expected):
    import json,subprocess,sys
    from fastapi.testclient import TestClient
    from workbench.app import create_app
    c=upgrade_case;database=c['app'].state.database.path
    request_path=c['root'].parent/'original-upgrade-request.json'
    result_path=c['root'].parent/'child-upgrade-preview.json'
    request_path.write_text(json.dumps(c['request']),encoding='utf-8')
    sql=logical(database);runtime=c['app'].state.backups.task_runtime_root
    c['client'].__exit__(None,None,None);c['client_closed']=True
    code='''import json,os,sys
from pathlib import Path
import anthropic,openai
from fastapi.testclient import TestClient
import workbench.book_ownership as b
from workbench.app import create_app
from workbench.jobs import JobSupervisor
from workbench.onboarding_upgrade import OnboardingUpgradeService
database,root,machine,runtime,scratch,request_path,result_path,project_id,event=sys.argv[1:]
def forbidden(*args,**kwargs):raise AssertionError('real Provider or worker forbidden')
anthropic.Anthropic=openai.OpenAI=forbidden
JobSupervisor._start_worker=forbidden
b.default_machine_store=lambda:Path(machine)
request=json.loads(Path(request_path).read_text('utf-8'))
app=create_app(database_path=database,start_worker=False,auto_import_active=False,
 allowed_workspaces=[str(Path(root).parent)],runtime_root=runtime,chapter_publication_machine_store=machine)
with TestClient(app):
 assert app.state.maintenance.status()['state']=='interrupted'
 app.state.maintenance.resume(request['operation_id'])
 def fault(name,index,path):
  if name==event:os._exit(92)
 service=OnboardingUpgradeService(app.state.backups,temporary_root=Path(scratch),transaction_fault=fault)
 value=service.preview(project_id,**request)
 Path(result_path).write_text(json.dumps(value),encoding='utf-8')
 service.commit(project_id,**request,expected_preview_sha256=value['preview_sha256'],
  acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
raise AssertionError('requested process exit not reached')
'''
    exited=subprocess.run([sys.executable,'-B','-c',code,str(database),str(c['root']),str(c['machine']),str(runtime),
        str(c['scratch']),str(request_path),str(result_path),c['project'].id,event],capture_output=True,timeout=180,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert exited.returncode==92,exited.stdout+exited.stderr
    value=json.loads(result_path.read_text('utf-8'))
    book=contents(c['root'])
    restarted=create_app(database_path=database,start_worker=False,auto_import_active=False,
        allowed_workspaces=[c['root'].parent],runtime_root=runtime,chapter_publication_machine_store=c['machine'])
    with TestClient(restarted):
        assert restarted.state.maintenance.status()['state']=='interrupted'
        service=OnboardingUpgradeService(restarted.state.backups,temporary_root=c['scratch'])
        state=service.operation(c['project'].id,preview_sha256=value['preview_sha256'])
        assert state['state']==expected
        assert contents(c['root'])==book and logical(database)==sql
        body=dict(operation_id=c['op'],preview_sha256=value['preview_sha256'],
            acknowledged_policy_sha256=value['recovery_policy_sha256'],confirmed=True)
        with pytest.raises((ValueError,RuntimeError)):service.continue_operation(c['project'].id,**body)
        assert contents(c['root'])==book and logical(database)==sql
        restarted.state.maintenance.resume(c['op'])
        outcome=service.continue_operation(c['project'].id,**body)
        assert outcome['state']=='completed' and outcome['result']['run_id']==value['run_id']
        assert logical(database)==sql


@pytest.mark.parametrize('upgrade_case',['queued','held'],indirect=True)
def test_nonterminal_legacy_jobs_remain_retained_and_cannot_be_carried_through_upgrade(upgrade_case):
    c=upgrade_case;book=contents(c['root']);sql=logical(c['app'].state.database.path)
    assert c['certificate']['recoverable'] and c['review']['status']=='confirmed'
    before=c['app'].state.jobs.get(c['pending_job'].id)
    with pytest.raises(OnboardingUpgradeError,match='未结束的旧任务'):preview(c)
    assert c['app'].state.jobs.get(c['pending_job'].id)==before
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql


def test_upgrade_holds_independent_state_and_sql_writer_fences_until_after_author_intent(upgrade_case):
    c=upgrade_case;value=preview(c);observed=[]
    code='''import sqlite3,sys
from pathlib import Path
from core.engine.persistence import persistence_state_lock,PersistenceLockError
root=Path(sys.argv[1]);database=sys.argv[2]
try:
 with persistence_state_lock((root,)):
  raise AssertionError('state writer entered')
except PersistenceLockError:pass
connection=sqlite3.connect(database,timeout=.1)
try:connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as exc:
 assert 'locked' in str(exc);print('BOTH_WRITERS_BLOCKED');sys.exit(17)
raise AssertionError('SQL writer entered')
'''
    def compete(event):
        if event!='after_onboarding_intent':return
        result=child(code,c['root'],c['app'].state.database.path)
        assert result.returncode==17,result.stderr
        assert b'BOTH_WRITERS_BLOCKED' in result.stdout;observed.append(True)
    c['upgrade'].fault_injector=compete
    assert commit(c,value)['committed'] and observed==[True]
