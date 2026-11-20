"""Real application, original grants, background dispatch and author continuation."""
import threading,socket

import pytest
from fastapi.testclient import TestClient

from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_driver import driver,calls
from tests.test_reviewed_batch_takeovers import author_commit
from tests.test_reviewed_batch_tasks import joined
from workbench.app import create_app
_connect=socket.socket.connect


@pytest.fixture
def web(batch_case,monkeypatch):
    c=batch_case
    def local_only(sock,address):
        if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:return _connect(sock,address)
        pytest.fail('public batch test attempted external network')
    monkeypatch.setattr(socket.socket,'connect',local_only)
    app=create_app(database_path=c['db'].path,allowed_workspaces=[c['root'].parent],runtime_root=c['root'].parent/'jobs',
        start_worker=False,auto_import_active=False,chapter_publication_machine_store=c['machine'])
    service=app.state.reviewed_batches
    with TestClient(app) as client:
        page=client.get('/reviewed-batches');assert page.status_code==200
        assert client.get('/static/reviewed-batches.js').status_code==200
        yield c,app,client,service


def grant(c,client):
    base=f"/api/projects/{c['project'].id}/reviewed-batches"
    body={k:c['route'][k] for k in ('end_chapter','configuration')};headers={'Idempotency-Key':'web-grant'}
    preview=client.post(base+'/preview',json=body,headers=headers);assert preview.status_code==200,preview.text
    body.update(expected_manifest_sha256=preview.json()['manifest']['manifest_sha256'],author_confirmed=True)
    result=client.post(base,json=body,headers=headers);assert result.status_code==200,result.text
    value=result.json();assert value['initialized'] and not value['execution_live']
    assert value['control']['progress']['status']=='paused' and calls(c,2)==0
    return base+'/'+value['grant']['manifest']['batch_id'],value


def command(client,path,view,action,key):
    result=client.post(path+'/commands',json={'action':action,'expected_control_sha256':view['control']['control']['record_sha256'],
        'author_confirmed':True},headers={'Idempotency-Key':key})
    assert result.status_code==200,result.text
    return result.json()


def test_public_batch_background_pause_author_commit_and_explicit_next_chapter(web):
    c,app,client,service=web;path,view=grant(c,client)
    entered=threading.Event();release=threading.Event()
    def observed(event,step):
        assert step['stage']=='generation';entered.set();assert release.wait(300)
    service.tasks.driver_factory=lambda controls:driver(c,controls,fault=observed)[0]
    try:
        command(client,path,view,'start','web-start');assert entered.wait(300)
        live=client.get(path);assert live.status_code==200,live.text
        assert live.json()['execution_live']
        command(client,path,live.json(),'pause','web-pause');release.set();joined(service.tasks)
        paused=client.get(path);assert paused.status_code==200,paused.text
        view=paused.json();assert view['control']['progress']['stage']=='extraction' and view['drafts']
        draft_id=view['drafts'][0]['draft_id']
        takeover=client.post(path+'/takeover',json={'draft_id':draft_id,'expected_control_sha256':view['control']['control']['record_sha256'],'author_confirmed':True})
        assert takeover.status_code==200,takeover.text
        view=takeover.json();assert view['control']['progress']['stage']=='author_review'
        assert client.post(path+'/commands',json={'action':'resume','expected_control_sha256':view['control']['control']['record_sha256'],
            'author_confirmed':True},headers={'Idempotency-Key':'bypass-author'}).status_code==409
        drafts=c['publication'].preparations.drafts;draft=drafts.get(draft_id)
        route,published=author_commit(c,draft);assert published['publication_verified']
        original={'key':route['idempotency_key'],'body':{k:v for k,v in route.items() if k not in {'project_id','chapter','idempotency_key'}}}
        completed=client.post(path+'/complete-author',json={'chapter':2,'request':original,
            'expected_control_sha256':view['control']['control']['record_sha256'],'author_confirmed':True})
        assert completed.status_code==200,completed.text
        view=completed.json();assert view['control']['progress']['chapter']==3 and view['control']['progress']['status']=='paused'
        assert not view['execution_live'] and calls(c,3)==0
        service.tasks.driver_factory=lambda controls:driver(c,controls)[0]
        command(client,path,view,'resume','web-explicit-after-author');joined(service.tasks)
        final=client.get(path);assert final.status_code==200,final.text
        assert final.json()['control']['progress']['status']=='completed' and calls(c,3)==2
        assert drafts.get(draft.id)==draft
        assert client.get('/api/projects/'+c['project'].id+'/reviewed-batches').json()['items'][0]['batch_id']==path.rsplit('/',1)[1]
    finally:release.set();service.tasks.close()


def test_public_torn_original_control_recovery_never_launches_and_rejects_changed_requests(web):
    c,app,client,service=web;path,view=grant(c,client);batch=path.rsplit('/',1)[1]
    class Crash(BaseException):pass
    def fault(event):
        if event=='after_control_machine':raise Crash()
    service.controls.fault_injector=fault
    body={'action':'start','expected_control_sha256':view['control']['control']['record_sha256'],'author_confirmed':True}
    with pytest.raises(Crash):service.controls.command(project_id=c['project'].id,batch_id=batch,idempotency_key='original-web-start',**body)
    service.controls.fault_injector=None
    read=client.get(path);assert read.status_code==200,read.text
    assert read.json()['recovery']['action']=='start' and not read.json()['execution_live']
    assert client.post(path+'/recover-command',json=body,headers={'Idempotency-Key':'wrong-key'}).status_code==409
    recovered=client.post(path+'/recover-command',json=body,headers={'Idempotency-Key':'original-web-start'})
    assert recovered.status_code==200,recovered.text
    value=recovered.json();assert value['explicit_resume_required'] and not service.controls.handles and calls(c,2)==0
    duplicate=client.post(path+'/commands',json=body,headers={'Idempotency-Key':'original-web-start'})
    assert duplicate.status_code==200 and not duplicate.json()['execution_live'] and calls(c,2)==0
    assert client.post(path+'/commands',json={**body,'caller_quality_passed':True},headers={'Idempotency-Key':'extra-fields'}).status_code==422
    assert client.post(path+'/recover-stage',json={'action':'claim_step','chapter':2,'stage':'generation','author_confirmed':True}).status_code==409
    stopped=command(client,path,value,'stop','web-stop');assert stopped['control']['progress']['status']=='stopped' and calls(c,2)==0
