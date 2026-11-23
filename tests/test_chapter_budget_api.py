import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_candidate_context import registered_upgrade
from workbench.app import create_app
from workbench.book_ownership import BookOwnership


@pytest.fixture
def fresh_api(case):
    db,projects,project,_=registered_upgrade(case)
    root=case['root'];machine=root.parent/'m'
    with BookOwnership(project.book_id,machine_store=machine).locked() as owner:
        with db.transaction() as connection:
            owner.enroll(root=root,database=db,project_id=project.id,connection=connection)
    app=create_app(database_path=db.path,allowed_workspaces=[root.parent],runtime_root=root.parent/'jobs',
        start_worker=False,auto_import_active=False,chapter_publication_machine_store=machine)
    configuration=dict(max_provider_calls=3,max_input_tokens=100000,max_output_tokens=50000,
        max_active_seconds='900',amount_limit=None,currency=None,pricing=[])
    payload=dict(configuration=configuration,author_confirmed=True,
        expected_context_sha256=ReviewedChapterCandidateContextBuilder(root).build(2).context_sha256)
    with TestClient(app) as client:
        client.get('/')
        yield dict(root=root,db=db,project=project,app=app,client=client,payload=payload,
            path=f'/api/projects/{project.id}/chapters/2/budget')


def test_actual_api_freeze_reload_and_changed_configuration_refusal(fresh_api):
    c=fresh_api;before=c['client'].get(c['path'])
    assert before.status_code==200 and before.json()['state']=='not_configured'
    directory=c['root']/'.novelagent/runtime/budgets'
    assert not directory.exists()
    first=c['client'].post(c['path'],json=c['payload'])
    assert first.status_code==200,first.text
    assert first.json()['state']=='ready' and first.json()['usage']['charged']['calls']==0
    assert first.json()['time']['active_seconds']=='0'
    assert c['client'].post(c['path'],json=c['payload']).json()==first.json()
    assert c['client'].get(c['path']).json()==first.json()
    changed=copy.deepcopy(c['payload']);changed['configuration']['max_provider_calls']=5
    response=c['client'].post(c['path'],json=changed)
    assert response.status_code==409 and response.json()['error']['code']=='chapter_budget_conflict'
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2).store.frozen_budget()['max_provider_calls']==3


def test_api_rejects_source_change_and_missing_author_confirmation(fresh_api):
    c=fresh_api
    bad={**c['payload'],'expected_context_sha256':'f'*64}
    assert c['client'].post(c['path'],json=bad).status_code==409
    assert c['client'].get(c['path']).json()['state']=='not_configured'
    for value in (False,1,'true'):
        assert c['client'].post(c['path'],json={**c['payload'],'author_confirmed':value}).status_code==422
    c['client'].cookies.clear()
    assert c['client'].post(c['path'],json=c['payload']).status_code==403


def test_api_preserves_damaged_time_instead_of_reinitializing(fresh_api):
    c=fresh_api
    assert c['client'].post(c['path'],json=c['payload']).status_code==200
    target=c['root']/'.novelagent/runtime/budgets/c2/time.json';target.unlink()
    assert c['client'].get(c['path']).status_code==409
    assert c['client'].post(c['path'],json=c['payload']).status_code==409
    assert not target.exists()


def test_api_refuses_missing_machine_book_ownership(fresh_api):
    c=fresh_api;c['app'].state.chapter_publication.machine_store=c['root'].parent/'unowned'
    assert c['client'].get(c['path']).status_code==200
    response=c['client'].post(c['path'],json=c['payload'])
    assert response.status_code==409 and response.json()['error']['code']=='book_ownership_invalid'
    assert not (c['root']/'.novelagent/runtime/budgets/i2.json').exists()


def test_api_cannot_freeze_a_different_chapter_or_client_book(fresh_api):
    c=fresh_api
    bad=copy.deepcopy(c['payload']);bad['configuration']['book_id']='forged'
    assert c['client'].post(c['path'],json=bad).status_code==409
    assert c['client'].post(c['path'].replace('/2/','/1/'),json=c['payload']).status_code==409


def test_actual_api_explicit_retry_finishes_original_initialization(fresh_api):
    c=fresh_api;service=c['app'].state.chapter_budgets
    class Crash(BaseException):pass
    def fault(event):
        if event=='after_ledger':raise Crash()
    service.fault_injector=fault
    with pytest.raises(Crash):service.freeze(c['project'].id,2,**c['payload'])
    view=c['client'].get(c['path'])
    assert view.status_code==200 and view.json()['state']=='initializing'
    assert not (c['root']/'.novelagent/runtime/budgets/c2/time.json').exists()
    service.fault_injector=None
    result=c['client'].post(c['path'],json=c['payload'])
    assert result.status_code==200 and result.json()['state']=='ready'


def test_api_cannot_start_empty_budget_over_existing_candidate(fresh_api):
    from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
    from tests.test_reviewed_candidate_context import _fixed_provenance,author_confirmation
    c=fresh_api;projects=c['app'].state.chapter_publication.projects
    candidate=ReviewedChapterCandidateExecutor(story_project_root=c['root'],
        generator=lambda unused:'林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。',
        execution_kind='deterministic',provenance_factory=_fixed_provenance)
    candidate.execute(chapter_index=2,confirmation=author_confirmation(projects,c['project']),idempotency_key='old-candidate')
    response=c['client'].post(c['path'],json=c['payload'])
    assert response.status_code==409 and response.json()['error']['code']=='chapter_budget_conflict'
    assert not (c['root']/'.novelagent/runtime/budgets/i2.json').exists()


def test_default_workbench_provider_chain_requires_budget_and_shares_it_with_facts(fresh_api,monkeypatch):
    from types import SimpleNamespace as NS
    from api.openai_client import chat_completion
    from tests.test_chapter_execution_deadline import configure
    from tests.test_reviewed_candidate_context import author_confirmation
    c=fresh_api;calls=[]
    prose='林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。'
    def create(**kwargs):
        calls.append(kwargs)
        return NS(model='fixture',choices=[NS(message=NS(content=prose),finish_reason='stop')],
            usage={'prompt_tokens':12,'completion_tokens':24,'total_tokens':36})
    config=configure(monkeypatch,'openai',create)
    for key,value in dict(provider_max_attempts=2,provider_retry_base_delay_seconds=0,
        provider_retry_max_delay_seconds=0,provider_retry_jitter_ratio=0,
        provider_retry_deadline_seconds=900,openai_max_retries=0).items():setattr(config,key,value)
    monkeypatch.setattr('core.engine.chapter_candidate.generate_chapter',lambda text,**unused:
        chat_completion([{'role':'user','content':text}],max_tokens=128,stage='generation'))
    confirmation=author_confirmation(c['app'].state.chapter_publication.projects,c['project'])
    route=c['path'].removesuffix('/budget')+'/candidates'
    headers={'Idempotency-Key':'default-budget-chain'}
    missing=c['client'].post(route,json={'confirmation':confirmation},headers=headers)
    assert missing.status_code==409,missing.text
    assert not calls
    c['payload']['configuration']['max_provider_calls']=1
    frozen=c['client'].post(c['path'],json=c['payload'])
    assert frozen.status_code==200,frozen.text
    generated=c['client'].post(route,json={'confirmation':confirmation},headers=headers)
    assert generated.status_code==200,generated.text
    candidate=generated.json()['candidate']
    assert candidate['status']=='completed' and len(calls)==1
    assert calls[0]['timeout']>0
    draft=candidate['draft']
    extracted=c['client'].post(f"/api/candidate-drafts/{draft['id']}/fact-extractions",json={
        'expected_draft_revision':draft['revision'],'expected_draft_sha256':draft['sha256'],
        'author_confirmed':True},headers={'Idempotency-Key':'default-budget-facts'})
    assert extracted.status_code==200,extracted.text
    assert extracted.json()['fact_extraction']['status']=='failed' and len(calls)==1
    view=c['client'].get(c['path']).json()
    assert view['usage']['charged']['calls']==1 and not view['usage']['unresolved_attempts']
    assert len(view['usage']['items'])==2 and view['usage']['items'][1]['accounting_state']=='released'


@pytest.mark.parametrize('actual_tier',['default',None])
def test_public_money_freeze_default_generation_fact_gate_and_fee_report(fresh_api,monkeypatch,actual_tier):
    from types import SimpleNamespace as NS
    from api.openai_client import chat_completion
    from core.model_calls import canonical_model_request_digest
    from tests.test_chapter_execution_deadline import configure
    from tests.test_reviewed_candidate_context import author_confirmation
    from tests.test_reviewed_monetary_admission import MODEL,price,usage
    c=fresh_api;calls=[]
    prose='林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。'
    def create(**kwargs):
        calls.append(kwargs)
        ledger=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2).store
        row=ledger.usage_snapshot()['attempts'][0]
        assert row['dispatch_claimed'] and row['receipt'] is None
        payload={k:v for k,v in kwargs.items() if k!='timeout'}
        assert row['plan']['intent']['request_digest']==canonical_model_request_digest(payload)
        assert kwargs['max_completion_tokens']==128 and 'max_tokens' not in kwargs
        assert kwargs['service_tier']=='default' and kwargs['timeout']>0
        return NS(model=MODEL,service_tier=actual_tier,usage=usage(),
            choices=[NS(message=NS(content=prose),finish_reason='stop')])
    config=configure(monkeypatch,'openai',create)
    for key,value in dict(openai_model=MODEL,provider_max_attempts=2,provider_retry_base_delay_seconds=0,
        provider_retry_max_delay_seconds=0,provider_retry_jitter_ratio=0,
        provider_retry_deadline_seconds=900,openai_max_retries=0).items():setattr(config,key,value)
    monkeypatch.setenv('OPENAI_MODEL',MODEL)
    monkeypatch.setattr('core.engine.chapter_candidate.generate_chapter',lambda text,**unused:
        chat_completion([{'role':'user','content':text}],max_tokens=128,stage='generation'))
    c['payload']['configuration'].update(amount_limit='0.40045',currency='USD',pricing=[price()],
        max_input_tokens=2000000)
    if actual_tier=='default':
        before=c['client'].get(c['path'])
        assert before.json()['monetary_option']['available'] is True
        monkeypatch.setenv('OPENAI_MODEL','changed-alias')
        rejected=c['client'].post(c['path'],json=c['payload'])
        assert rejected.status_code==409,rejected.text
        assert '模型或端点已变化' in rejected.json()['error']['message']
        assert not (c['root']/'.novelagent/runtime/budgets/i2.json').exists()
        monkeypatch.setenv('OPENAI_MODEL',MODEL)
    frozen=c['client'].post(c['path'],json=c['payload'])
    assert frozen.status_code==200,frozen.text
    confirmation=author_confirmation(c['app'].state.chapter_publication.projects,c['project'])
    route=c['path'].removesuffix('/budget')+'/candidates'
    headers={'Idempotency-Key':'monetary-generation'}
    generated=c['client'].post(route,json={'confirmation':confirmation},headers=headers)
    assert generated.status_code==200,generated.text
    candidate=generated.json()['candidate']
    assert candidate['status']=='completed' and len(calls)==1
    assert c['client'].post(route,json={'confirmation':confirmation},headers=headers).status_code==200
    assert len(calls)==1
    view=c['client'].get(c['path'])
    assert view.status_code==200,view.text
    account=view.json()['usage']['items'][0]
    expected='settled' if actual_tier=='default' else 'held_unknown'
    assert account['accounting_state']==expected
    assert view.json()['usage']['charged_amount']==('0.000104' if actual_tier=='default' else '0.400256')
    fees=c['client'].get(f"/api/projects/{c['project'].id}/model-calls")
    assert fees.status_code==200,fees.text
    assert fees.json()['items'][0]['budget_accounting']['accounting_state']==expected
    assert fees.json()['items'][0]['cost']['classification']==('estimated' if actual_tier=='default' else 'unknown')
    draft=candidate['draft']
    extracted=c['client'].post(f"/api/candidate-drafts/{draft['id']}/fact-extractions",json={
        'expected_draft_revision':draft['revision'],'expected_draft_sha256':draft['sha256'],
        'author_confirmed':True},headers={'Idempotency-Key':'monetary-facts'})
    assert extracted.status_code==200,extracted.text
    assert extracted.json()['fact_extraction']['status']=='failed',extracted.text
    assert len(calls)==1
    after=c['client'].get(c['path']).json()
    assert after['usage']['items'][0]==account


def test_relay_suggestion_inherits_only_settings_and_rechecks_endpoint(fresh_api,monkeypatch):
    c=fresh_api; client=c['client']; path=c['path']; service=c['app'].state.chapter_budgets
    profile=dict(provider='openai',model='fixture',endpoint_fingerprint='a'*64)
    monkeypatch.setattr(service,'monetary_option',lambda:dict(available=False,estimate_identity=profile.copy()))
    first=client.get(path).json()['suggestion']
    assert first['source_chapter'] is None and first['target']=={'amount':'1','currency':'USD'}
    assert first['limits']['max_provider_calls']==3
    rates=dict(**profile,input_price_per_million='2',output_price_per_million='10')
    payload=copy.deepcopy(c['payload']); payload['configuration']['spending_target']=dict(amount='0.65',currency='USD',estimate_rates=rates)
    profile['endpoint_fingerprint']='b'*64
    assert client.post(path,json=payload).status_code==409
    profile['endpoint_fingerprint']='a'*64
    saved=client.post(path,json=payload); assert saved.status_code==200,saved.text
    next_path=path.replace('/2/','/3/'); following=client.get(next_path).json()
    assert following['state']=='not_configured' and following['usage'] is None
    assert following['suggestion']['source_chapter']==2 and following['suggestion']['estimate_rates']==rates
    assert following['suggestion']['target']=={'amount':'0.65','currency':'USD'}
    assert following['suggestion']['limits']['max_output_tokens']==payload['configuration']['max_output_tokens']
    profile['model']='changed-model'
    changed=client.get(next_path).json()['suggestion'];assert changed['tariff_changed'] and 'estimate_rates' not in changed
    assert client.get(path).json()['budget']==saved.json()['budget']
