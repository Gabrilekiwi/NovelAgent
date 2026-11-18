"""Public original-stage recovery and actual workbench process death/restart."""
import json,os,socket,subprocess,sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_api import web,grant,command,_connect
from tests.test_reviewed_batch_driver import driver,calls
from tests.test_reviewed_batch_tasks import joined
from tests.test_reviewed_stage_recovery import ORDER,crash,run_stage,counters
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from workbench.app import create_app
from workbench.reviewed_batch_controls import control_paths
from workbench.reviewed_batches import _selection


def test_public_all_eight_actual_claim_exits_reconcile_original_bytes_without_dispatch(web):
    c,app,client,service=web;c['route']['end_chapter']=2
    path,view=grant(c,client);batch=path.rsplit('/',1)[1]
    controls=service.controls
    original,handle=controls.command(project_id=c['project'].id,batch_id=batch,action='start',
        idempotency_key='matrix-explicit-start',expected_control_sha256=view['control']['control']['record_sha256'],author_confirmed=True)
    for stage in ORDER:
        previous=counters(c)
        cold=crash(c,batch,original,stage,'claim_step')
        assert not service._live(c['project'].id,batch)
        # This HTTP service is still cold for execution: it must not resurrect
        # the old handle merely because an original child command was written.
        controls.handles.clear()
        before=client.get(path);assert before.status_code==200,before.text
        value=before.json();assert value['recovery']['action']=='claim_step' and value['recovery']['stage']==stage
        assert value['explicit_resume_required'] and not value['execution_live']
        _,machine,book=control_paths(controls._control_context(c['project'].id),batch)
        machine_bytes=machine.read_bytes();prefix_bytes=book.read_bytes();assert machine_bytes!=prefix_bytes
        body={'action':'claim_step','chapter':2,'stage':stage,'author_confirmed':True}
        assert client.post(path+'/recover-stage',json={**body,'chapter':True}).status_code==409
        assert client.post(path+'/recover-stage',json={**body,'author_confirmed':False}).status_code==409
        assert machine.read_bytes()==machine_bytes and book.read_bytes()==prefix_bytes
        result=client.post(path+'/recover-stage',json=body);assert result.status_code==200,result.text
        value=result.json();assert not value['execution_live'] and value['explicit_resume_required']
        assert machine.read_bytes()==book.read_bytes()==machine_bytes and not controls.handles and counters(c)==previous
        original,handle=controls.command(project_id=c['project'].id,batch_id=batch,action='resume',
            idempotency_key='matrix-explicit-'+stage,expected_control_sha256=value['control']['control']['record_sha256'],author_confirmed=True)
        original=run_stage(c,controls,handle,original,stage)
        with (c['root'].parent/'public-claim-observations.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'stage':stage,'machine_sha256':value['control']['control']['record_sha256'],'counters':previous})+'\n');stream.flush();os.fsync(stream.fileno())
        print('public original claim recovered',stage,flush=True)
    assert original['progress']['status']=='completed' and counters(c)==('2','2')
    assert calls(c,2)==4 and load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2


@pytest.mark.filterwarnings('error::pytest.PytestUnhandledThreadExceptionWarning')
def test_actual_workbench_background_exit_cold_startup_never_dispatches_until_new_public_resume(batch_case,monkeypatch):
    c=batch_case
    spec=dict(root=str(c['root']),database=str(c['db'].path),machine=str(c['machine']),book_id=c['project'].book_id,
        project_id=c['project'].id,config={k:v for k,v in _selection().items() if k.startswith(('openai_','provider_'))},
        body={k:c['route'][k] for k in ('end_chapter','configuration')})
    spec['config']['openai_base_url']=c['config'].openai_base_url
    file=c['root'].parent/'actual-workbench-exit.fixture.json';file.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(file)],capture_output=True,text=True,timeout=360)
    assert child.returncode==99,child.stderr
    saved=json.loads(file.with_suffix('.started.json').read_text(encoding='utf-8'))
    def local_only(sock,address):
        if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:return _connect(sock,address)
        pytest.fail('cold workbench attempted external network')
    monkeypatch.setattr(socket.socket,'connect',local_only)
    app=create_app(database_path=c['db'].path,allowed_workspaces=[c['root'].parent],runtime_root=c['root'].parent/'jobs',
        start_worker=True,auto_import_active=False,chapter_publication_machine_store=c['machine'])
    service=app.state.reviewed_batches;path=saved['path']
    service.tasks.driver_factory=lambda controls:pytest.fail('cold startup or old request attempted dispatch')
    gc=c['root'].parent/'driver-generation.txt';ec=c['root'].parent/'driver-extraction.txt'
    with TestClient(app) as client:
        client.get('/reviewed-batches')
        value=client.get(path);assert value.status_code==200,value.text
        current=value.json();assert current['control']['progress']['stage']=='extraction'
        assert current['task_state']=='interrupted' and current['explicit_resume_required'] and not current['execution_live']
        assert not service.controls.handles and gc.read_text()=='1' and not ec.exists() and calls(c,2)==1 and calls(c,3)==0
        duplicate=client.post(path+'/commands',json=saved['request'],headers={'Idempotency-Key':'actual-workbench-start'})
        assert duplicate.status_code==200,duplicate.text
        assert not duplicate.json()['execution_live'] and not service.controls.handles and gc.read_text()=='1' and not ec.exists()
        def pause(event,step):
            if step['stage']=='readback':
                current=service.controls.get(project_id=c['project'].id,batch_id=saved['batch_id'])
                service.controls.command(project_id=c['project'].id,batch_id=saved['batch_id'],action='pause',
                    idempotency_key='cold-after-chapter',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
        service.tasks.driver_factory=lambda controls:driver(c,controls,fault=pause)[0]
        command(client,path,current,'resume','actual-workbench-new-resume');joined(service.tasks)
        final=client.get(path);assert final.status_code==200,final.text
        state=final.json()['control']['progress'];assert state['chapter']==3 and state['status']=='paused'
        assert gc.read_text()==ec.read_text()=='1' and calls(c,2)==2 and calls(c,3)==0
        assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2
        assert app.state.supervisor._thread.is_alive(),'legacy worker died while reviewed SQLite transaction was active'


CHILD=r'''import json,os,socket,sys,time
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_batch_driver import driver
from workbench.app import create_app
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
connect=socket.socket.connect
cold_service(spec)
def local_only(sock,address):
    if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:return connect(sock,address)
    raise AssertionError('workbench child attempted external network')
socket.socket.connect=local_only
app=create_app(database_path=spec['database'],allowed_workspaces=[Path(spec['root']).parent],
    runtime_root=Path(spec['root']).parent/'jobs',start_worker=False,auto_import_active=False,chapter_publication_machine_store=spec['machine'])
c={'root':Path(spec['root']),'project':SimpleNamespace(id=spec['project_id'],book_id=spec['book_id'])}
fixture=Path(sys.argv[1]).with_suffix('.started.json')
def stop(event,step):
    if step['stage']=='generation':os._exit(99)
app.state.reviewed_batches.tasks.driver_factory=lambda controls:driver(c,controls,fault=stop)[0]
with TestClient(app) as client:
    client.get('/reviewed-batches')
    base='/api/projects/'+spec['project_id']+'/reviewed-batches';headers={'Idempotency-Key':'actual-workbench-grant'}
    response=client.post(base+'/preview',json=spec['body'],headers=headers);assert response.status_code==200,response.text
    manifest=response.json()['manifest'];body={**spec['body'],'expected_manifest_sha256':manifest['manifest_sha256'],'author_confirmed':True}
    response=client.post(base,json=body,headers=headers);assert response.status_code==200,response.text
    batch=manifest['batch_id'];path=base+'/'+batch
    request={'action':'start','expected_control_sha256':response.json()['control']['control']['record_sha256'],'author_confirmed':True}
    with fixture.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps({'batch_id':batch,'path':path,'request':request}));stream.flush();os.fsync(stream.fileno())
    response=client.post(path+'/commands',json=request,headers={'Idempotency-Key':'actual-workbench-start'});assert response.status_code==200,response.text
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:time.sleep(.1)
    raise AssertionError('actual workbench generation exit not reached')
'''
