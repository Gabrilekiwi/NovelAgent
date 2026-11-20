"""Original chapter ledgers, real runtime, crash recovery and offline evidence."""
import copy,json,subprocess,sys,uuid
from decimal import localcontext
from pathlib import Path

import pytest

from core.batch_budget_contract import freeze_batch_budget,project_batch_usage
from core.batch_budget_store import BatchBudgetRegistry
from core.chapter_budget_store import ChapterBudgetStore,ChapterBudgetError,ChapterBudgetConflict,ChapterBudgetExceeded,ChapterBudgetUncertain
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_model_budget import ChapterModelCallBudget
from core.model_calls import ModelCallStore
from core.model_call_runtime import ModelCallRuntimeContext
from core.story_project.reviewed_state_document import digest
from tests.test_chapter_budget_store import budget
from tests.test_chapter_budget_runtime import args,response
from tests.test_restore_budget_merge import MemoryEvidence,payload,replace_document
from tests.test_reviewed_quality_disposition import isolated_report,PARAGRAPH
from core.quality.reviewed_repair_cycle import build_repair_cycle_claim
from workbench.model_usage import read_project_model_usage
from workbench.backup_archive_usage import _usage
from workbench.restore_budget_merge import reconcile_usage_evidence
from workbench.restore_handoff import RestoreHandoffError


@pytest.fixture
def tmp_path():
    # Match existing archive/workflow fixtures' supported Windows path budget.
    # The first long-default-path failure remains in t53-batch-first.log/XML;
    # these tests do not certify the separate Windows long-path release gate.
    root=Path.cwd()/'.tmp'/('b53-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    return root


def contract(**changes):
    values=dict(batch_id='batch_'+'1'*32,chapter_budgets=[budget(chapter=c) for c in (2,3,4)],
        amount_limit='0.002',max_provider_calls=10,max_input_tokens=10000,max_output_tokens=10000)
    values.update(changes)
    return freeze_batch_budget(**values)


def setup(root,batch=None):
    registry=BatchBudgetRegistry(root/'.novelagent/runtime/budgets',book_id='book-test')
    registry.freeze(contract() if batch is None else batch,before_write=lambda:None)
    (root/'.novelagent/project.json').write_text(json.dumps({'book_id':'book-test'}),encoding='utf-8')
    return registry


def runtime(root,chapter,execution,**kwargs):
    store=ChapterBudgetStore(root/f'.novelagent/runtime/budgets/c{chapter}',book_id='book-test',chapter=chapter)
    calls=ModelCallStore(root/'.novelagent/runtime/runs/executions'/execution/'model_calls')
    return ModelCallRuntimeContext(calls,chapter_budget=ChapterModelCallBudget(store,execution_id=execution),**kwargs)


def forbidden(*args,**kwargs):pytest.fail('no new Provider operation or write preflight allowed')


def report(registry,batch_id=None):
    return project_batch_usage(*registry.load(batch_id or 'batch_'+'1'*32))


def test_generation_repair_and_fact_share_original_batch_money_and_calls_across_restart(tmp_path):
    registry=setup(tmp_path,contract(amount_limit='0.00071'));effects=[]
    for chapter,stage in ((2,'generation'),(2,'repair'),(3,'fact_extraction')):
        execute=runtime(tmp_path,chapter,'execution_'+stage)
        assert execute.execute_attempt(**args(stage=stage,operation=lambda:effects.append(stage) or response()))=='synthetic result'
        assert execute.execute_attempt(**args(stage=stage,operation=forbidden))=='synthetic result'
    with pytest.raises(ChapterBudgetExceeded,match='batch_amount_limit'):
        runtime(tmp_path,4,'execution_next').execute_attempt(**args(operation=forbidden))
    assert effects==['generation','repair','fact_extraction']
    result=report(registry)
    assert result['charged_amount']=='0.00015' and result['charged']['calls']==3 and not result['unresolved_attempts']
    assert read_project_model_usage(tmp_path,book_id='book-test')['batch_budgets']==[result]
    assert registry.freeze(contract(amount_limit='0.00071'),before_write=forbidden)==contract(amount_limit='0.00071')


@pytest.mark.parametrize('cap,value,reason',[('max_provider_calls',1,'batch_max_provider_calls'),
    ('max_input_tokens',109,'batch_max_input_tokens'),('max_output_tokens',219,'batch_max_output_tokens'),
    ('amount_limit','0.00064','batch_amount_limit')])
def test_next_physical_attempt_is_rejected_before_dispatch_at_batch_cap(tmp_path,cap,value,reason):
    registry=setup(tmp_path,contract(**{cap:value}))
    runtime(tmp_path,2,'execution_first').execute_attempt(**args())
    attempt=runtime(tmp_path,3,'execution_second')
    with pytest.raises(ChapterBudgetExceeded,match=reason):attempt.execute_attempt(**args(operation=forbidden))
    ledger=attempt.chapter_budget.attempts()
    assert ledger[0]['local_rejection']==reason and ledger[0]['receipt']['status']=='budget_rejected'
    assert ledger[0]['dispatch_claimed'] is False
    assert report(registry)['charged']['calls']==1
    with pytest.raises(ChapterBudgetExceeded):attempt.execute_attempt(**args(operation=forbidden))


def test_unknown_physical_call_blocks_another_chapter_and_keeps_reservation(tmp_path):
    registry=setup(tmp_path)
    class Stop(BaseException):pass
    def crash(event,*unused):
        if event=='after_dispatch_claim_before_provider':raise Stop()
    with pytest.raises(Stop):runtime(tmp_path,2,'execution_unknown',fault_injector=crash).execute_attempt(**args(operation=forbidden))
    before=payload({'root':tmp_path})
    with pytest.raises(ChapterBudgetUncertain):runtime(tmp_path,3,'execution_other').execute_attempt(**args(operation=forbidden))
    result=report(registry)
    assert result['charged_amount']=='0.0006' and result['unresolved_attempts'][0]['chapter']==2
    # Original evidence remains; the unrelated chapter did not reserve an attempt.
    assert runtime(tmp_path,3,'execution_other').chapter_budget.attempts()==[]
    assert all((tmp_path/name).read_bytes()==raw for name,raw in before.items())


@pytest.mark.parametrize('damage',['manifest','ready','binding'])
def test_plain_chapter_store_cannot_escape_missing_batch_evidence(tmp_path,damage):
    registry=setup(tmp_path)
    if damage in ('manifest','ready'):
        (registry.root/'batches'/((('r' if damage=='ready' else '')+'batch_'+'1'*32)+'.json')).unlink()
    else:
        path=registry.root/'c2/ledger.json';value=json.loads(path.read_text(encoding='utf-8'))
        value.pop('batch_budget');value.pop('repair_cycle');value['schema_version']='chapter_budget_ledger_v2'
        value['ledger_sha256']=digest({k:v for k,v in value.items() if k!='ledger_sha256'})
        path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises((ChapterBudgetError,ValueError)):runtime(tmp_path,2,'execution_escape').execute_attempt(**args(operation=forbidden))


@pytest.mark.parametrize('kind',['unpriced','mixed_currency','mixed_book','duplicate','gap','wrong_type'])
def test_batch_freeze_rejects_unsupported_financial_scope_before_publishing(tmp_path,kind):
    chapters=[budget(chapter=c) for c in (2,3,4)]
    if kind=='unpriced':chapters[1]=budget(None,chapter=3)
    elif kind=='mixed_currency':chapters[1]=budget(chapter=3,currency='EUR',pricing=[{**chapters[1]['pricing'][0],'currency':'EUR'}])
    elif kind=='mixed_book':chapters[1]=budget(chapter=3,book_id='other')
    elif kind=='duplicate':chapters[1]=chapters[0]
    elif kind=='gap':chapters[1]=budget(chapter=5)
    with pytest.raises(ValueError):contract(chapter_budgets=chapters,**({'max_provider_calls':True} if kind=='wrong_type' else {}))
    assert not (tmp_path/'.novelagent').exists()


def test_existing_chapter_spending_cannot_be_adopted_and_does_not_leave_batch_intent(tmp_path):
    b=budget();ChapterBudgetFreezeRegistry(tmp_path,book_id='book-test').freeze(b,before_write=lambda:None)
    runtime(tmp_path,2,'execution_before_batch').execute_attempt(**args())
    registry=BatchBudgetRegistry(tmp_path/'.novelagent/runtime/budgets',book_id='book-test')
    with pytest.raises(ChapterBudgetConflict):registry.freeze(contract(),before_write=forbidden)
    assert not (registry.root/'batches').exists()


def test_one_chapter_cannot_bind_a_second_batch_and_frozen_caps_cannot_change(tmp_path):
    registry=setup(tmp_path)
    for changes in ({'batch_id':'batch_'+'2'*32},{'amount_limit':'0.003'}):
        with pytest.raises(ChapterBudgetConflict):registry.freeze(contract(**changes),before_write=forbidden)


def test_aggregate_decimal_sum_is_independent_of_callers_precision(tmp_path):
    registry=setup(tmp_path)
    for chapter in (2,3,4):runtime(tmp_path,chapter,'execution_c'+str(chapter)).execute_attempt(**args())
    with localcontext() as context:
        context.prec=2
        assert report(registry)['charged_amount']=='0.00015'


def test_repair_cycle_consumption_retains_the_original_batch_binding(tmp_path):
    registry=setup(tmp_path);store=runtime(tmp_path,2,'execution_probe').chapter_budget.store
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH)
    p['binding']['book_id']='book-test'
    p['preparation_sha256']=digest({k:v for k,v in p.items() if k!='preparation_sha256'})
    q['binding'].update(book_id='book-test',chapter_preparation_sha256=p['preparation_sha256'])
    q['quality_sha256']=digest({k:v for k,v in q.items() if k!='quality_sha256'})
    original=build_repair_cycle_claim(preparation=p,quality=q,budget=store.frozen_budget(),
        batch_id=contract()['batch_id'],batch_authorization_sha256='a'*64)
    store.claim_repair_cycle(original,before_claim=lambda:None)
    runtime(tmp_path,2,original['execution_id']).execute_attempt(**args(stage='repair'))
    _,ledger=store._load()
    assert ledger['schema_version']=='chapter_budget_ledger_v4' and ledger['repair_cycle']==original
    assert report(registry)['charged']['calls']==1
    assert registry.freeze(contract(),before_write=forbidden)==contract()


def test_repair_claim_from_another_batch_cannot_use_bound_chapter(tmp_path):
    setup(tmp_path);store=runtime(tmp_path,2,'execution_probe').chapter_budget.store
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH)
    p['binding']['book_id']='book-test';p['preparation_sha256']=digest({k:v for k,v in p.items() if k!='preparation_sha256'})
    q['binding'].update(book_id='book-test',chapter_preparation_sha256=p['preparation_sha256'])
    q['quality_sha256']=digest({k:v for k,v in q.items() if k!='quality_sha256'})
    wrong=build_repair_cycle_claim(preparation=p,quality=q,budget=store.frozen_budget(),
        batch_id='batch_'+'2'*32,batch_authorization_sha256='b'*64)
    before=(store.root/'ledger.json').read_bytes()
    with pytest.raises(ChapterBudgetConflict):store.claim_repair_cycle(wrong,before_claim=forbidden)
    assert (store.root/'ledger.json').read_bytes()==before


CHILD=r'''
import json,os,sys
from pathlib import Path
from core.batch_budget_store import BatchBudgetRegistry
def crash(event):
    if event==sys.argv[3]:os._exit(75)
BatchBudgetRegistry(Path(sys.argv[1]),book_id='book-test').freeze(
    json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')),before_write=lambda:None,fault_injector=crash)
'''


@pytest.mark.parametrize('phase',['after_batch_intent','after_chapter_budget','after_chapter_batch_binding','after_batch_ready'])
def test_real_freeze_process_death_resumes_exact_batch_without_spending(tmp_path,phase):
    batch=contract();path=tmp_path/'batch.fixture.json';path.write_text(json.dumps(batch),encoding='utf-8')
    root=tmp_path/'.novelagent/runtime/budgets'
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(root),str(path),phase],capture_output=True,text=True,timeout=30)
    assert child.returncode==75,child.stderr
    registry=BatchBudgetRegistry(root,book_id='book-test')
    manifest=(root/'batches'/(batch['batch_id']+'.json')).read_bytes()
    if phase!='after_batch_ready':
        with pytest.raises(ChapterBudgetError):registry.load(batch['batch_id'])
    assert registry.freeze(batch,before_write=lambda:None)==batch
    assert (root/'batches'/(batch['batch_id']+'.json')).read_bytes()==manifest
    assert report(registry)['charged_amount']=='0' and report(registry)['charged']['calls']==0


def test_actual_ledger_archive_projection_keeps_batch_totals_and_restore_binding(tmp_path):
    registry=setup(tmp_path);before=payload({'root':tmp_path})
    runtime(tmp_path,2,'execution_generate').execute_attempt(**args())
    runtime(tmp_path,3,'execution_extract').execute_attempt(**args())
    after=payload({'root':tmp_path});evidence=MemoryEvidence(tmp_path,after)
    offline=_usage(evidence,'book-test',lambda *unused:None)
    assert offline['batch_budgets']==[report(registry)]
    result=reconcile_usage_evidence(MemoryEvidence(tmp_path,before),evidence,book_id='book-test')
    assert result['usage']['batch_budgets'][0]['charged_amount']=='0.0001'
    # Removing all batch files and validly downgrading every empty old ledger
    # can form another valid standalone snapshot, but cannot undo this archive.
    stripped=copy.deepcopy(before)
    for name in list(stripped):
        if '/batches/' in name:del stripped[name]
        elif name.endswith('/ledger.json'):
            value=json.loads(stripped[name]);value.pop('batch_budget');value.pop('repair_cycle')
            value['schema_version']='chapter_budget_ledger_v2';replace_document(stripped,name,value,'ledger_sha256')
    assert 'batch_budgets' not in _usage(MemoryEvidence(tmp_path,stripped),'book-test',lambda *unused:None)
    with pytest.raises(RestoreHandoffError):reconcile_usage_evidence(MemoryEvidence(tmp_path,before),MemoryEvidence(tmp_path,stripped),book_id='book-test')
