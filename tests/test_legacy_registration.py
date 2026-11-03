"""Normal archive-bound legacy registration, using actual Strict book evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.test_backup_archive import capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import logical
from tests.test_backup_restore_materialization import prepare
from tests.test_legacy_restore_identity import legacy
from tests.test_workbench_app import _unlock
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.book_ownership import BookOwnership, BookOwnershipError, WITNESS_PATH
from workbench.legacy_registration import ADMISSION
from workbench.restore_activation import RestoreActivation
from workbench.restore_scope import load_restore_scope
from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION


@pytest.fixture
def registration_case(legacy, request):
    c = legacy
    if getattr(request, 'param', '1.0') == '2.0':
        path = c['root']/'.novelagent/project.json'
        identity = json.loads(path.read_bytes())
        identity.update(schema_version='2.0', root_hint='.', authority=LEGACY_AUTHORITY_PROJECTION)
        path.write_text(json.dumps(identity, ensure_ascii=False), encoding='utf-8')
    app = create_app(database_path=c['root'].parent/'workbench.db', start_worker=False,
        auto_import_active=False, allowed_workspaces=[c['root'].parent],
        chapter_publication_machine_store=c['machine'])
    client = TestClient(app)
    client.__enter__()
    _unlock(client)
    project = app.state.projects.import_project(c['root'])
    draft = app.state.jobs.upsert_draft(project_id=project.id, chapter=2, text='登记前作者草稿')
    op = app.state.maintenance.begin()['operation_id']
    package = capture({'service':app.state.backups, 'op':op, 'project':project})
    value = {**c,'app':app,'client':client,'project':project,'draft':draft,'op':op,
             'package':Path(package['path']),'archive_id':package['archive_id']}
    try:
        yield value
    finally:
        if not value.get('client_closed'):
            client.__exit__(None,None,None)


def endpoint(c, preview=False):
    return '/api/projects/'+c['project'].id+'/onboarding/registration'+('/preview' if preview else '')


def preview(c):
    result=c['client'].post(endpoint(c,True),json={'operation_id':c['op'],'archive_id':c['archive_id']})
    assert result.status_code==200,result.text
    return result.json()


def body(c,value):
    return {'operation_id':c['op'],'archive_id':c['archive_id'],
            'expected_preview_sha256':value['preview_sha256'],'confirmed':True}


def contents(root):
    return {name:item['sha256'] for name,item in inventory_tree(root)['files'].items()}


@pytest.mark.parametrize('registration_case', ['1.0', '2.0'], indirect=True)
def test_public_first_registration_preserves_legacy_identity_draft_and_diagnostic_archive(registration_case):
    c=registration_case
    before=contents(c['root']); database=logical(c['app'].state.database.path)
    original_package=contents(c['package'])
    value=preview(c)
    assert value['state']=='ready' and value['can_register']
    assert not (c['root']/WITNESS_PATH).exists()
    result=c['client'].post(endpoint(c),json=body(c,value))
    assert result.status_code==200,result.text
    registered=result.json()
    assert registered['state']=='active' and registered['generation']==1
    assert not registered['changes_format'] and not registered['archive_recoverable']
    assert registered['requires_new_backup'] and not registered['automatic_provider_calls_allowed']
    after=contents(c['root']);after.pop(WITNESS_PATH)
    assert after==before and logical(c['app'].state.database.path)==database
    assert contents(c['package'])==original_package
    assert c['client'].get('/api/drafts/'+c['draft'].id).json()['draft']['text']==c['draft'].text
    assert c['client'].post(endpoint(c),json=body(c,value)).json()==registered
    assert preview(c)['state']=='registered'
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    with owner.locked(),c['app'].state.database.transaction() as connection:
        assert owner.require(root=c['root'],database=c['app'].state.database,project_id=c['project'].id,
            connection=connection)['registration_id']==registered['registration_id']
    # The next real source snapshot must contain the actual registration.
    saved=c['app'].state.backups.preview(c['project'].id,operation_id=c['op'])
    assert saved['scope']['machine_ownership']['state']=='verified'
    assert not saved['recoverable']
    # Follow actual enrollment with a new archive and the real writable restore
    # protocol. No seeded ownership or archive recoverability claim is used.
    registered_book=inventory_tree(c['root'])
    package=Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])
    c['app'].state.maintenance.finish(c['op'])
    target=Path(__file__).resolve().parents[1]/'.tmp'/('lrg-'+uuid.uuid4().hex[:8])
    plan=prepare({'project':c['project']},package,target,c['scratch'])
    materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
    bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
    prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    handoff=intent['handoff_id']
    assert service.begin(package,destination=target,expected_preview_sha256=intent['intent_sha256'],
        temporary_root=c['scratch'])['source_revoked']
    assert service.capture(handoff_id=handoff)['settled']
    for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
        method(package,handoff_id=handoff,temporary_root=c['scratch'])
    assert service.activate(package,handoff_id=handoff,temporary_root=c['scratch'])['target_writable']
    assert load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
    for _ in range(2):
        restored=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'])
        with TestClient(restored) as client:
            assert restored.state.supervisor._thread is None
            detail=client.get('/api/projects/'+c['project'].id+'/chapters/2')
            assert detail.status_code==200,detail.text
            assert detail.json()['retained_author_draft']['text']==c['draft'].text
            assert not detail.json()['has_prose']
    assert inventory_tree(c['root'])==registered_book and logical(c['app'].state.database.path)==database
    assert contents(c['package'])==original_package


@pytest.mark.parametrize('damage',['missing_confirmation','extra_field','old_preview','source_changed','wrong_archive'])
def test_registration_rejects_changed_or_unconfirmed_request_without_creating_controls(registration_case,damage):
    c=registration_case;value=preview(c);request=body(c,value)
    if damage=='missing_confirmation':request['confirmed']=False
    elif damage=='extra_field':request['override']=True
    elif damage=='old_preview':request['expected_preview_sha256']='0'*64
    elif damage=='source_changed':(c['root']/'author-note.txt').write_text('新作者内容',encoding='utf-8')
    else:request['archive_id']='backup_'+'0'*32
    before=contents(c['root']);database=logical(c['app'].state.database.path)
    result=c['client'].post(endpoint(c),json=request)
    assert result.status_code in {409,423},result.text
    assert contents(c['root'])==before and logical(c['app'].state.database.path)==database
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    assert not owner.record_path.exists() and not (c['root']/WITNESS_PATH).exists()
    assert not (owner.directory/ADMISSION).exists()


@pytest.mark.parametrize('damage',['machine','witness','admission'])
def test_completed_registration_proof_loss_is_not_treated_as_first_registration(registration_case,damage):
    c=registration_case;value=preview(c)
    assert c['client'].post(endpoint(c),json=body(c,value)).status_code==200
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    if damage=='machine':owner.record_path.unlink()
    elif damage=='witness':(c['root']/WITNESS_PATH).unlink()
    else:(owner.directory/ADMISSION).write_text('{broken',encoding='utf-8')
    before=contents(c['root']);database=logical(c['app'].state.database.path)
    result=c['client'].post(endpoint(c,True),json={'operation_id':c['op'],'archive_id':c['archive_id']})
    assert result.status_code==409,result.text
    assert contents(c['root'])==before and logical(c['app'].state.database.path)==database


def test_new_operation_and_unrelated_book_cannot_reuse_registration_preview(registration_case):
    c=registration_case;value=preview(c)
    c['app'].state.maintenance.finish(c['op'])
    old_op=c['op'];c['op']=c['app'].state.maintenance.begin()['operation_id']
    request=body(c,value);request['operation_id']=old_op
    assert c['client'].post(endpoint(c),json=request).status_code in {409,423}
    request['operation_id']=c['op']
    assert c['client'].post(endpoint(c),json=request).status_code==409
    value=preview(c)
    assert c['client'].post(endpoint(c),json=body(c,value)).status_code==200


@pytest.mark.parametrize('phase',['after_legacy_admission','after_enrollment_intent','after_enrollment_witness','after_enrollment_active'])
def test_actual_process_exit_resumes_original_registration_without_changing_book_format(registration_case,phase):
    c=registration_case
    original=contents(c['root']);database=logical(c['app'].state.database.path)
    c['client'].__exit__(None,None,None)
    c['client_closed']=True
    code='''import json,os,sys
from pathlib import Path
from fastapi.testclient import TestClient
from workbench.app import create_app
def forbidden(*a,**k):raise AssertionError('unexpected Provider, worker or import')
import anthropic,openai
anthropic.Anthropic=forbidden;openai.OpenAI=forbidden
from workbench.jobs import JobSupervisor
JobSupervisor._start_worker=forbidden
from workbench import app as module
module._import_active_project=forbidden
database,machine,project,archive,operation,phase=sys.argv[1:]
app=create_app(database_path=Path(database),start_worker=False,auto_import_active=False,
    chapter_publication_machine_store=Path(machine))
with TestClient(app) as client:
 client.get('/')
 assert app.state.database.initialization_pending
 if app.state.maintenance.status()['state']=='interrupted':
  response=client.post('/api/maintenance/resume',json={'operation_id':operation})
  assert response.status_code==200,response.text
 def fault(event,value):
  if event==phase:os._exit(87)
 app.state.legacy_registration.fault_injector=fault
 endpoint='/api/projects/'+project+'/onboarding/registration'
 response=client.post(endpoint+'/preview',json={'operation_id':operation,'archive_id':archive})
 assert response.status_code==200,response.text
 value=response.json()
 response=client.post(endpoint,json={'operation_id':operation,'archive_id':archive,
     'expected_preview_sha256':value['preview_sha256'],'confirmed':True})
 assert response.status_code==200,response.text
 assert app.state.database.initialization_pending and not app.state.startup_recovery.complete
 print(json.dumps(response.json()),flush=True)
'''
    args=[sys.executable,'-B','-c',code,str(c['app'].state.database.path),str(c['machine']),c['project'].id,
          c['archive_id'],c['op'],phase]
    result=subprocess.run(args,capture_output=True,timeout=60,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    assert result.returncode==87,result.stderr.decode('utf-8',errors='replace')
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    pending=json.loads(owner.record_path.read_bytes()) if owner.record_path.exists() else None
    args[-1]='no_injected_exit'
    resumed=subprocess.run(args,capture_output=True,timeout=60,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    assert resumed.returncode==0,resumed.stderr.decode('utf-8',errors='replace')
    record=json.loads(owner.record_path.read_bytes())
    assert record['state']=='active' and record['generation']==1
    if pending:assert record['registration_id']==pending['registration_id']
    after=contents(c['root']);after.pop(WITNESS_PATH)
    assert after==original and logical(c['app'].state.database.path)==database
