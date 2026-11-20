"""Actual process exits while one original restored confirmation is persisted."""
import copy,json,os,subprocess,sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_api import web
from tests.test_final_batch_restore import exercise_final_restore
from tests.test_reviewed_batch_driver import calls
from scripts.smoke_v1 import _missing_provider_env
from workbench.app import create_app
from workbench.book_ownership import BookOwnership
from workbench.restored_batch_continuation import RestoredBatchContinuationService
from workbench.restore_scope import load_restore_scope
from core.chapter_execution_budget import ChapterExecutionBudget
from core.story_project.reviewed_state_document import digest

def signed(value):
    value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
    return value

CHILD=r'''
import dataclasses,json,os,socket,sys
from pathlib import Path
from fastapi.testclient import TestClient
from core.config import get_config
from workbench.app import create_app
import workbench.reviewed_batches as batches
data=json.loads(sys.argv[1]);config=get_config();fields={f.name for f in dataclasses.fields(config)}
config=dataclasses.replace(config,**{k:v for k,v in data['selection'].items() if k in fields},openai_base_url='https://api.openai.com/v1')
batches.get_config=lambda:config
connect=socket.socket.connect
def local_only(sock,address):
    if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:return connect(sock,address)
    raise AssertionError('restored preparation child attempted external network')
socket.socket.connect=local_only
def fault(event):
    if event==data['event']:os._exit(data['exit_code'])
app=create_app(database_path=Path(data['target'])/'workbench.sqlite',start_worker=True,auto_import_active=True,
    chapter_publication_machine_store=Path(data['machine']))
with TestClient(app) as client:
    assert client.get('/reviewed-batches').status_code==200
    assert app.state.supervisor._thread is None
    app.state.reviewed_batches.restored.fault_injector=fault
    response=client.post(data['path']+'/restored-continuation',json=data['confirmation'])
    raise AssertionError('requested real exit not reached:'+str(response.status_code)+':'+response.text)
'''


def test_four_real_preparation_exits_resume_only_original_confirmation_without_dispatch(web,monkeypatch):
    c,_,_,_=web;captured=[]
    class DeferPreparation(Exception):pass
    prepare=RestoredBatchContinuationService.prepare
    def defer(self,**kwargs):
        if kwargs.get('author_confirmed') is True and kwargs.get('expected_preview_sha256')!='f'*64:
            captured.append(dict(kwargs));raise DeferPreparation()
        return prepare(self,**kwargs)
    # Close the real restored app before the first valid preparation writes.
    # A new child must own its own actual instance and book admission.
    with monkeypatch.context() as patch:
        patch.setattr(RestoredBatchContinuationService,'prepare',defer)
        with pytest.raises(DeferPreparation):exercise_final_restore(web,preparation_only=True)
    assert len(captured)==1
    request=captured[0];project=request.pop('project_id');batch=request.pop('batch_id')
    target=c['root'].parent/'restored';root=target/'book';runtime=root/'.novelagent/runtime'
    path='/api/projects/'+project+'/reviewed-batches/'+batch
    grant_path=runtime/'reviewed/batches'/(batch+'.json');control_path=runtime/'reviewed/batch_controls'/(batch+'.json')
    raw={grant_path:grant_path.read_bytes(),control_path:control_path.read_bytes()}
    grant=json.loads(raw[grant_path]);origin=json.loads(raw[control_path])
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    scope=load_restore_scope(target/'workbench.sqlite',machine_store=c['machine'])
    token=digest({'batch_id':batch,'qualification_sha256':scope.activation['qualification_sha256']})[:24]
    base=runtime/'reviewed/continuations'/token
    for prefix in ('bg-','bgr-','bc0-','bc-'):
        p=owner.directory/(prefix+batch+'.json');raw[p]=p.read_bytes()
    start=next(command for command in origin['commands'] if command['action']=='start')
    prefix={**origin,'commands':origin['commands'][:start['sequence']-1]}
    prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
    old={'action':'start','expected_control_sha256':prefix['record_sha256'],'author_confirmed':True}
    claims={n:ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=n).store.repair_cycle_claim() for n in (2,3,4)}
    charged={n:ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=n).store.report()['charged'] for n in (2,3,4)}
    env=os.environ.copy();env.update(_missing_provider_env());env.update(NOVELAGENT_SKIP_DOTENV='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    evidence=[];semantic=[];machine_intent=None
    for index,name in enumerate(('after_continuation_machine','after_continuation_book','after_continuation_control','after_continuation_ready')):
        data={'target':str(target),'machine':str(c['machine']),'path':path,'confirmation':request,
            'selection':grant['manifest']['model_selection'],'event':name,'exit_code':111+index}
        child=subprocess.run([sys.executable,'-X','utf8','-c',CHILD,json.dumps(data,ensure_ascii=False)],
            cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,encoding='utf-8',timeout=180,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        assert child.returncode==111+index,child.stdout+child.stderr
        intent_path=owner.directory/('brc-'+base.name+'.json')
        if machine_intent is None:machine_intent=intent_path.read_bytes()
        assert intent_path.read_bytes()==machine_intent
        assert (base/'intent.json').exists()==(index>=1)
        assert (owner.directory/('brcc-'+base.name+'.json')).exists()==(index>=2)
        assert (base/'ready.json').exists()==(index>=3)
        app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            assert client.get('/reviewed-batches').status_code==200
            assert app.state.supervisor._thread is None
            service=app.state.reviewed_batches
            service.tasks.driver_factory=lambda _:pytest.fail('preparation retry manufactured a driver')
            observed=client.get(path);assert observed.status_code==200,observed.text
            view=observed.json();assert not view['execution_live'] and view['explicit_resume_required']
            assert view['history_only'] is (index<3)
            rejected=client.post(path+'/restored-continuation',json={**request,'expected_preview_sha256':'0'*64})
            assert rejected.status_code==409,rejected.text
            rejected=client.post(path+'/restored-continuation',json={**request,'author_confirmed':False})
            assert rejected.status_code==409,rejected.text
            replay=client.post(path+'/commands',json=old,headers={'Idempotency-Key':'restore-source-start'})
            assert replay.status_code==(409 if index<3 else 200),replay.text
            if index==3:
                ready=(base/'ready.json').read_bytes()
                for _ in range(2):
                    again=client.post(path+'/restored-continuation',json=request)
                    assert again.status_code==200,again.text
                    assert again.json()['preparation']==json.loads(ready)
                assert (base/'ready.json').read_bytes()==ready
                assert json.loads((base/'control.json').read_bytes())==origin
                assert json.loads((owner.directory/('brcc-'+base.name+'.json')).read_bytes())==origin
                # Consistent hashes and matching copies are insufficient:
                # current qualification, consent and the original prefix
                # must each still be required.
                proof_paths=[base/'intent.json',intent_path,base/'ready.json',base/'control.json',owner.directory/('brcc-'+base.name+'.json')]
                originals={p:p.read_bytes() for p in proof_paths}
                for damage in ('rehashed_foreign_qualification','rehashed_missing_consent','valid_both_controls_rewound_before_origin'):
                    try:
                        if damage.startswith('rehashed_'):
                            altered=copy.deepcopy(json.loads(originals[base/'intent.json']))
                            if damage=='rehashed_foreign_qualification':
                                altered['preview']['qualification_sha256']='0'*64;signed(altered['preview'])
                            else:altered['author_confirmed']=False
                            signed(altered)
                            for p in (base/'intent.json',intent_path):p.write_text(json.dumps(altered),encoding='utf-8')
                            altered_ready=json.loads(ready);altered_ready['intent_sha256']=altered['record_sha256'];signed(altered_ready)
                            (base/'ready.json').write_text(json.dumps(altered_ready),encoding='utf-8')
                        else:
                            initial=(base/'anchor.json').read_bytes()
                            for p in (base/'control.json',owner.directory/('brcc-'+base.name+'.json')):p.write_bytes(initial)
                        rejected=client.get(path);assert rejected.status_code==409,(damage,rejected.text)
                        rejected=client.post(path+'/restored-continuation',json=request)
                        assert rejected.status_code==409,(damage,rejected.text)
                        assert service.tasks._active is None and not service.controls.handles
                        semantic.append({'damage':damage,'status':409})
                    finally:
                        for p,content in originals.items():p.write_bytes(content)
                assert client.get(path).status_code==200
            assert service.tasks._active is None and not service.controls.handles
        assert {p:p.read_bytes() for p in raw}==raw
        assert [calls({'root':root,'project':c['project']},n) for n in (2,3,4)]==[2,1,0]
        assert {n:ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=n).store.report()['charged'] for n in (2,3,4)}==charged
        assert {n:ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=n).store.repair_cycle_claim() for n in (2,3,4)}==claims
        evidence.append({'event':name,'actual_exit_code':child.returncode,'history_only':index<3,
            'changed_confirmation_status':409,'original_request_status':replay.status_code,'calls':[2,1,0]})
    assert len(semantic)==3
    (c['root'].parent/'t72-prepare-crash-evidence.json').write_text(json.dumps({'exits':evidence,'semantic_tamper':semantic,'original_preview_sha256':request['expected_preview_sha256']},indent=2),encoding='utf-8')
