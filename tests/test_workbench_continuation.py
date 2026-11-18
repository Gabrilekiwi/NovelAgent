"""Real reviewed routes and editable persistence; all provider work is synthetic."""
import copy
import hashlib
import json

from api.contracts import ModelResponse, reported_model_usage
from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.model_call_runtime import current_model_call_runtime
from tests.test_chapter_budget_api import fresh_api, case
from tests.test_workbench_chapter_candidates import _fixed_provenance, _confirm_chapter, _generate_candidate, PROSE


class ContinuationProvider:
    def __init__(self, counter_path=None):
        self.calls=0
        self.counter_path=counter_path
    def __call__(self,root):
        def generate(pack):
            runtime=current_model_call_runtime()
            def operation():
                self.calls+=1
                if self.counter_path:
                    self.counter_path.write_text(str(self.calls),encoding='utf-8')
                if self.calls==1:raise TimeoutError('synthetic uncertain response')
                return ModelResponse(PROSE,actual_model='fixture',endpoint_type='official',
                    usage=reported_model_usage({'input_tokens':10,'output_tokens':30}))
            return runtime.execute_attempt(call_id=runtime.new_call_id(provider='fixture',stage='chapter_generation'),
                attempt_number=1,provider='fixture',model='fixture',endpoint_type='official',endpoint_fingerprint='a'*64,
                stage='chapter_generation',request={'input':pack},input_tokens=10,max_output_tokens=128,operation=operation).text
        return ReviewedChapterCandidateExecutor(story_project_root=root,generator=generate,
            execution_budget_factory=ChapterExecutionBudget,provenance_factory=_fixed_provenance)


def prepare(c):
    factory=ContinuationProvider()
    c['app'].state.chapter_candidates.reviewed.executor_factory=factory
    client=c['client'];project=c['project']
    response=client.post(c['path'],json=c['payload']);assert response.status_code==200,response.text
    confirmation=_confirm_chapter(client,project.id)
    response=_generate_candidate(client,project.id,confirmation,key='first')
    assert response.status_code==200,response.text
    source=response.json()['candidate'];assert source['status']=='provider_uncertain'
    path=f"/api/projects/{project.id}/chapters/2/candidates/{source['candidate_id']}"
    body=dict(expected_ledger_sha256=source['continuation']['expected_ledger_sha256'],
        expected_intent_hash=source['intent_hash'],expected_record_hash=source['record_hash'],
        expected_uncertainty_sha256=source['uncertainty']['sha256'],author_confirmed=True)
    return factory,source,path,body,confirmation


def test_permission_refresh_single_dispatch_edit_and_reload(fresh_api):
    c=fresh_api;f,source,path,body,confirmation=prepare(c);client=c['client']
    ledger=c['root']/'.novelagent/runtime/budgets/c2/ledger.json';before=ledger.read_bytes()
    response=client.post(path+'/continuation',json=body)
    assert response.status_code==200,response.text
    ready=response.json()['candidate']['continuation'];assert ready['state']=='ready'
    assert f.calls==1 and ledger.read_bytes()==before
    assert client.get(path).json()['candidate']['continuation']==ready
    assert client.post(path+'/continuation',json=body).status_code==200
    # Normal generation remains stopped; accepting the old outcome is not a general permission.
    denied=_generate_candidate(client,c['project'].id,confirmation,key='unapproved-new')
    assert denied.status_code in (200,409)
    assert f.calls==1
    payload=dict(author_confirmed=True,expected_grant_sha256=ready['grant_sha256'])
    response=client.post(path+'/continuation/run',json=payload)
    assert response.status_code==200,response.text
    result=response.json()['candidate'];assert result['status']=='completed' and result['draft']['text']==PROSE
    assert f.calls==2 and client.get(path).json()['candidate']['continuation']['state']=='used'
    repeat=client.post(path+'/continuation/run',json=payload)
    assert repeat.status_code==200,repeat.text
    assert repeat.json()['candidate']['candidate_id']==result['candidate_id'] and f.calls==2
    draft=result['draft'];edited=PROSE+'\n\n这是作者保存的局部修改。'
    saved=client.put(f"/api/candidate-drafts/{draft['id']}",json={'text':edited,'expected_revision':1})
    assert saved.status_code==200,saved.text
    stale=client.put(f"/api/candidate-drafts/{draft['id']}",json={'text':'stale','expected_revision':1})
    assert stale.status_code==409
    detail=path.rsplit('/',1)[0]+'/'+result['candidate_id']
    assert client.get(detail).json()['candidate']['draft']['text']==edited
    report=client.get(c['path']).json()['usage']
    assert report['charged']['calls']==2 and len(report['unresolved_attempts'])==1
    assert json.loads(ledger.read_bytes())['attempts'][0]==json.loads(before)['attempts'][0]
    assert str(c['root']) not in json.dumps(response.json())


def test_stale_unconfirmed_wrong_chapter_and_unowned_requests_never_authorize(fresh_api):
    c=fresh_api;f,source,path,body,_=prepare(c);client=c['client']
    for bad in ({**body,'author_confirmed':False},{**body,'expected_ledger_sha256':'f'*64}):
        assert client.post(path+'/continuation',json=bad).status_code==409
    assert client.post(path.replace('/chapters/2/','/chapters/3/')+'/continuation',json=body).status_code==409
    assert client.post(path+'/continuation/run',json={'author_confirmed':True,'expected_grant_sha256':'f'*64}).status_code==409
    c['app'].state.chapter_publication.machine_store=c['root'].parent/'unowned'
    assert client.post(path+'/continuation',json=body).status_code==409
    assert f.calls==1
    assert not (c['root']/'.novelagent/runtime/budgets/c2/continuation.json').exists()
