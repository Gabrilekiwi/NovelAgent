"""Actual budget ledger, crash, competing claim and archived-descendant checks."""
import copy,json,os,subprocess,sys
from pathlib import Path

import pytest

from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore,ChapterBudgetError,ChapterBudgetConflict,ChapterBudgetUncertain
from core.quality.reviewed_repair_cycle import build_repair_cycle_claim,validate_repair_cycle_claim,ReviewedRepairCycleError
from core.story_project.reviewed_state_document import digest
from tests.test_chapter_budget_store import budget,reserve,settle,intent,receipt
from tests.test_reviewed_quality_disposition import isolated_report,PARAGRAPH
from tests.test_restore_budget_merge import MemoryEvidence,payload,replace_document
from workbench.backup_archive_usage import _usage
from workbench.restore_budget_merge import reconcile_usage_evidence
from workbench.restore_handoff import RestoreHandoffError


BOOK='synthetic-book'
def setup(root):
    frozen=budget(book_id=BOOK)
    registry=ChapterBudgetFreezeRegistry(root,book_id=BOOK)
    registry.freeze(frozen,before_write=lambda:None)
    store=ChapterBudgetStore(registry.root/'c2',book_id=BOOK,chapter=2)
    identity=root/'.novelagent/project.json'
    identity.write_text(json.dumps({'book_id':BOOK}),encoding='utf-8')
    return store


def claim(store,*,batch='1',text=None,pending=False):
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH if text is None else text,pending=pending)
    return build_repair_cycle_claim(preparation=p,quality=q,budget=store.frozen_budget(),
        batch_id='batch_'+batch*32,batch_authorization_sha256=batch*64)


def forbidden():pytest.fail('replay/read must not invoke a new action or preflight')


def test_original_ledger_consumes_once_and_old_initialization_preserves_claim(tmp_path):
    store=setup(tmp_path);before=store.root.joinpath('ledger.json').read_bytes()
    assert store.repair_cycle_claim() is None and store.root.joinpath('ledger.json').read_bytes()==before
    original=claim(store);calls=[]
    assert store.claim_repair_cycle(original,before_claim=lambda:calls.append('checked'))==original
    assert calls==['checked'] and original['execution_authorized'] is False
    after=store.root.joinpath('ledger.json').read_bytes()
    restarted=ChapterBudgetStore(store.root,book_id=BOOK,chapter=2)
    restarted.initialize(store.frozen_budget())
    assert restarted.claim_repair_cycle(original,before_claim=forbidden)==original
    assert restarted.repair_cycle_claim()==original and restarted.report()['repair_cycle']==original
    assert store.root.joinpath('ledger.json').read_bytes()==after
    with pytest.raises(ChapterBudgetConflict):restarted.claim_repair_cycle(claim(store,batch='2'),before_claim=forbidden)


def test_original_spending_protocol_preserves_consumption_through_reserve_dispatch_and_settle(tmp_path):
    store=setup(tmp_path);original=claim(store)
    store.claim_repair_cycle(original,before_claim=lambda:None)
    source=intent(stage='repair');reserve(store,execution=original['execution_id'],source=source)
    assert store.repair_cycle_claim()==original
    settle(store,original['execution_id'],receipt(source))
    assert store.report()['charged']['calls']==1 and store.repair_cycle_claim()==original


def test_stale_source_preflight_or_uncertain_call_cannot_consume_new_cycle(tmp_path):
    store=setup(tmp_path);original=claim(store);before=store.root.joinpath('ledger.json').read_bytes()
    def stale():raise ChapterBudgetConflict('synthetic source changed')
    with pytest.raises(ChapterBudgetConflict):store.claim_repair_cycle(original,before_claim=stale)
    assert store.root.joinpath('ledger.json').read_bytes()==before
    reserve(store)
    with pytest.raises(ChapterBudgetUncertain):store.claim_repair_cycle(original,before_claim=forbidden)
    assert store.repair_cycle_claim() is None


@pytest.mark.parametrize('kind',['clean','semantic','missing_check'])
def test_claim_builder_never_turns_a_pause_or_clean_result_into_repair_eligibility(tmp_path,kind):
    store=setup(tmp_path)
    text=PARAGRAPH if kind=='clean' else PARAGRAPH+'\n'+PARAGRAPH.replace('手电光','灯光') if kind=='semantic' else None
    with pytest.raises(ReviewedRepairCycleError):claim(store,text=text,pending=kind=='missing_check')
    assert store.repair_cycle_claim() is None


@pytest.mark.parametrize('field,value',[('chapter',True),('repair_cycles_consumed',True),
    ('execution_authorized',0),('budget_sha256','f'*64),('batch_id','../../unsafe')])
def test_claim_fields_cannot_be_forged_by_rehashing(tmp_path,field,value):
    store=setup(tmp_path);value_claim=claim(store);value_claim[field]=value
    value_claim['claim_sha256']=digest({k:v for k,v in value_claim.items() if k!='claim_sha256'})
    with pytest.raises(ReviewedRepairCycleError):validate_repair_cycle_claim(value_claim,budget=store.frozen_budget())
    assert store.repair_cycle_claim() is None


@pytest.mark.parametrize('damage',['delete_claim','null_claim','boolean_count','remove_ledger'])
def test_missing_or_damaged_consumption_is_not_recreated_as_empty(tmp_path,damage):
    store=setup(tmp_path);original=claim(store);store.claim_repair_cycle(original,before_claim=lambda:None)
    path=store.root/'ledger.json'
    if damage=='remove_ledger':path.unlink()
    else:
        value=json.loads(path.read_text(encoding='utf-8'))
        if damage=='delete_claim':del value['repair_cycle']
        elif damage=='null_claim':value['repair_cycle']=None
        else:value['repair_cycle']['repair_cycles_consumed']=True
        value['ledger_sha256']=digest({k:v for k,v in value.items() if k!='ledger_sha256'})
        path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ChapterBudgetError):store.repair_cycle_claim()
    with pytest.raises(ChapterBudgetError):store.claim_repair_cycle(original,before_claim=forbidden)


CHILD=r'''
import json,os,sys
from pathlib import Path
from core.chapter_budget_store import ChapterBudgetStore,ChapterBudgetConflict
from core.engine.persistence import PersistenceLockError
store=ChapterBudgetStore(Path(sys.argv[1]),book_id='synthetic-book',chapter=2)
claim=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
def fault(event):
    if sys.argv[3]=='crash':os._exit(74)
try:store.claim_repair_cycle(claim,before_claim=lambda:None,fault_injector=fault)
except (ChapterBudgetConflict,PersistenceLockError):sys.exit(2)
'''


def child_args(store,original,mode):
    path=store.root/(original['claim_id']+'.fixture.json');path.write_text(json.dumps(original),encoding='utf-8')
    return [sys.executable,'-B','-c',CHILD,str(store.root),str(path),mode]


def test_real_process_death_after_atomic_claim_does_not_issue_a_second_cycle(tmp_path):
    store=setup(tmp_path);original=claim(store)
    child=subprocess.run(child_args(store,original,'crash'),capture_output=True,text=True,timeout=30)
    assert child.returncode==74,child.stderr
    restarted=ChapterBudgetStore(store.root,book_id=BOOK,chapter=2)
    assert restarted.repair_cycle_claim()==original
    assert restarted.claim_repair_cycle(original,before_claim=forbidden)==original
    with pytest.raises(ChapterBudgetConflict):restarted.claim_repair_cycle(claim(store,batch='2'),before_claim=forbidden)


def test_two_real_processes_cannot_claim_different_cycles_for_one_chapter(tmp_path):
    store=setup(tmp_path);first=claim(store);second=claim(store,batch='2')
    children=[subprocess.Popen(child_args(store,c,'normal'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for c in (first,second)]
    outputs=[c.communicate(timeout=30) for c in children]
    assert sorted(c.returncode for c in children)==[0,2],outputs
    assert store.repair_cycle_claim() in (first,second)


def test_archive_projection_and_descendant_keep_consumed_cycle_without_new_spending(tmp_path):
    store=setup(tmp_path);before=payload({'root':tmp_path})
    original=claim(store);store.claim_repair_cycle(original,before_claim=lambda:None)
    after=payload({'root':tmp_path})
    archived=MemoryEvidence(tmp_path,before);current=MemoryEvidence(tmp_path,after)
    result=reconcile_usage_evidence(archived,current,book_id=BOOK)
    assert result['usage']['chapter_budgets'][0]['repair_cycle']==original
    assert result['usage']['summary']['attempt_count']==0
    # Both snapshots are individually valid; the older empty ledger cannot
    # replace a later consumed one, even though their monetary totals match.
    assert _usage(archived,BOOK,lambda *args:None)['summary']['attempt_count']==0
    with pytest.raises(RestoreHandoffError):reconcile_usage_evidence(current,archived,book_id=BOOK)


def test_valid_alternate_batch_claim_is_not_a_descendant_of_original_claim(tmp_path):
    store=setup(tmp_path);first=claim(store);second=claim(store,batch='2')
    store.claim_repair_cycle(first,before_claim=lambda:None)
    before=payload({'root':tmp_path});after=copy.deepcopy(before)
    name='.novelagent/runtime/budgets/c2/ledger.json';ledger=json.loads(after[name]);ledger['repair_cycle']=second
    replace_document(after,name,ledger,'ledger_sha256')
    current=MemoryEvidence(tmp_path,after)
    assert _usage(current,BOOK,lambda *args:None)['chapter_budgets'][0]['repair_cycle']==second
    with pytest.raises(RestoreHandoffError):reconcile_usage_evidence(MemoryEvidence(tmp_path,before),current,book_id=BOOK)
