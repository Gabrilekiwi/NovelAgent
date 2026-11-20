"""Unknown outcomes never become receipts or refunds when an author continues."""
import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.chapter_budget_continuation import authorize, FILENAME
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetConflict, ChapterBudgetExceeded, ChapterBudgetUncertain
from tests.test_chapter_budget_store import store, budget, intent, reserve, receipt, settle


def pending(tmp_path, config=None):
    s = store(tmp_path, config)
    original = intent()
    reserve(s, source=original)
    s.mark_dispatched('execution_gen', original)
    ledger = json.loads((tmp_path/'ledger.json').read_text())
    args = dict(target_execution_id='execution_once', source_execution_id='execution_gen',
        source_resolution_hash='d'*64, expected_ledger_sha256=ledger['ledger_sha256'], author_confirmed=True,
        candidate_input_sha256='e'*64, candidate_confirmation_sha256='f'*64)
    return s, ledger, args


def test_one_permit_preserves_unknown_reservation_and_is_consumed_across_restart(tmp_path):
    s, original, args = pending(tmp_path)
    before = (tmp_path/'ledger.json').read_bytes()
    grant = authorize(s, **args)
    assert (tmp_path/'ledger.json').read_bytes() == before
    assert authorize(s, **args) == grant
    restarted = ChapterBudgetStore(tmp_path, book_id='book-test', chapter=2)
    reserve(restarted, 'execution_once')
    assert restarted.report()['charged']['calls'] == 2
    assert restarted.report()['charged_amount'] == '0.0012'
    assert json.loads((tmp_path/'ledger.json').read_text())['attempts'][0] == original['attempts'][0]
    # The same reservation is idempotent, never an additional physical dispatch.
    reserve(restarted, 'execution_once')
    restarted.mark_dispatched('execution_once', intent())
    with pytest.raises(ChapterBudgetConflict):
        restarted.mark_dispatched('execution_once', intent())
    settle(restarted, 'execution_once', receipt(intent()))
    assert restarted.report()['charged_amount'] == '0.00065'
    assert len(restarted.report()['unresolved_attempts']) == 1
    with pytest.raises(ChapterBudgetUncertain):
        reserve(restarted, 'execution_third')
    with pytest.raises(ChapterBudgetConflict):
        authorize(restarted, **{**args, 'target_execution_id':'execution_third'})
    assert restarted.execution_attempts('execution_gen')[0]['receipt'] is None


@pytest.mark.parametrize('field,value', [('author_confirmed',False), ('expected_ledger_sha256','0'*64),
    ('source_execution_id','execution_other'), ('target_execution_id','execution_gen')])
def test_approval_rejects_wrong_or_stale_source(tmp_path, field, value):
    s, _, args = pending(tmp_path)
    before = (tmp_path/'ledger.json').read_bytes()
    with pytest.raises(ChapterBudgetConflict):
        authorize(s, **{**args, field:value})
    assert (tmp_path/'ledger.json').read_bytes() == before
    assert not (tmp_path/FILENAME).exists()


@pytest.mark.parametrize('mutation', ['execution','model','stage','endpoint','input','output'])
def test_permit_cannot_authorize_a_different_request_or_larger_reservation(tmp_path, mutation):
    s, _, args = pending(tmp_path)
    authorize(s, **args)
    source = intent(); changes = {}; target = 'execution_once'
    if mutation == 'execution': target = 'execution_other'
    if mutation in ('model','stage'):
        from core.model_calls import build_model_call_intent
        source = build_model_call_intent(call_id='other', attempt_id='other-a1', provider='openai',
            model='other' if mutation=='model' else 'fixture', stage='other' if mutation=='stage' else 'generation',
            request={'messages':['test']}, created_at=source['created_at'], budget_reservation=source['budget_reservation'])
    if mutation == 'endpoint': changes['endpoint_type']='openai_compatible'
    if mutation in ('input','output'):
        from core.model_calls import build_model_call_intent
        inputs=101 if mutation=='input' else 100; outputs=201 if mutation=='output' else 200
        source=build_model_call_intent(call_id='other',attempt_id='other-a1',provider='openai',model='fixture',
            stage='generation',request={'messages':['test']},created_at=source['created_at'],
            budget_reservation={'reserved_input_tokens':inputs,'reserved_output_tokens':outputs,'reserved_total_tokens':inputs+outputs})
        if mutation=='input':
            changes['input_bound']={'max_billable_input_tokens':101,'basis':'verified_token_count',
                'request_digest':source['request_digest'],'evidence_sha256':'b'*64}
    with pytest.raises((ChapterBudgetUncertain, ValueError)):
        reserve(s, target, source, **changes)
    assert len(s.execution_attempts('execution_once')) == 0


def test_total_budget_still_rejects_and_consumes_permit(tmp_path):
    s, _, args = pending(tmp_path, budget('0.0007'))
    authorize(s, **args)
    with pytest.raises(ChapterBudgetExceeded):reserve(s,'execution_once')
    assert s.report()['charged_amount']=='0.0006'
    assert s.execution_attempts('execution_gen')[0]['receipt'] is None
    with pytest.raises(ChapterBudgetExceeded):reserve(s,'execution_once')


def test_late_settlement_invalidates_the_original_permit(tmp_path):
    s, _, args=pending(tmp_path);authorize(s, **args)
    settle(s,'execution_gen',receipt(intent()))
    with pytest.raises(ChapterBudgetUncertain):reserve(s,'execution_once')


def test_existing_batch_scope_cannot_receive_a_continuation(tmp_path,monkeypatch):
    from core.batch_budget_store import BatchBudgetRegistry
    s, _, args=pending(tmp_path)
    monkeypatch.setattr(BatchBudgetRegistry,'for_chapter',lambda *args: object())
    with pytest.raises(ChapterBudgetConflict,match='batch'):authorize(s, **args)
    assert not (tmp_path/FILENAME).exists()


@pytest.mark.parametrize('change',['missing','corrupt','settled'])
def test_dispatch_rechecks_permit_after_reservation(tmp_path,change):
    s, _, args=pending(tmp_path);authorize(s, **args);reserve(s,'execution_once')
    if change=='missing': (tmp_path/FILENAME).unlink()
    elif change=='corrupt': (tmp_path/FILENAME).write_text('{}')
    else: settle(s,'execution_gen',receipt(intent()))
    with pytest.raises(ChapterBudgetConflict):s.mark_dispatched('execution_once',intent())
    assert not s.execution_attempts('execution_once')[0]['dispatch_claimed']


def test_missing_corrupt_or_moved_permit_never_unlocks_budget(tmp_path):
    s, _, args = pending(tmp_path/'original')
    with pytest.raises(ChapterBudgetUncertain):reserve(s,'execution_once')
    authorize(s, **args)
    copy_root=tmp_path/'copy';shutil.copytree(s.root,copy_root)
    copied=ChapterBudgetStore(copy_root,book_id='book-test',chapter=2)
    with pytest.raises(ChapterBudgetConflict):reserve(copied,'execution_once')
    value=json.loads((s.root/FILENAME).read_text());value['limits']['max_output_tokens']+=1
    (s.root/FILENAME).write_text(json.dumps(value))
    with pytest.raises(ChapterBudgetConflict):reserve(s,'execution_once')


def test_racing_same_reservation_is_one_ledger_row(tmp_path):
    s, _, args = pending(tmp_path);authorize(s, **args)
    def attempt(_):
        from core.engine.persistence import PersistenceLockError
        try:return reserve(ChapterBudgetStore(tmp_path,book_id='book-test',chapter=2),'execution_once')
        except PersistenceLockError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert any(results)
    assert len(s.execution_attempts('execution_once'))==1
    assert s.report()['charged']['calls']==2


def test_real_candidate_continuation_retains_old_evidence_and_reuses_context():
    from tests.test_chapter_candidate import ChapterCandidateTest
    from tests.test_chapter_budget_executors import scope, model_request, response
    from core.model_call_runtime import current_model_call_runtime
    from core.engine.chapter_candidate import ChapterCandidateExecutionError, ChapterCandidateConflictError
    from core.engine.chapter_candidate_resolution import candidate_provider_uncertainty_sha256
    fixture=ChapterCandidateTest();fixture.setUp()
    bound=scope(fixture.book,book_id=fixture.identity.book_id,chapter=2)
    calls=[]
    prose='林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。'
    def generator(pack):
        runtime=current_model_call_runtime();call=runtime.new_call_id(provider='fixture',stage='generation')
        def operation():
            calls.append(pack)
            if len(calls)==1:raise TimeoutError('unknown remote outcome')
            return response(prose)
        return model_request(runtime,{'input':pack},call,'generation',operation).text
    executor=fixture._executor(generator=generator,execution_budget=bound)
    confirmation=fixture._confirmation()
    with pytest.raises(ChapterCandidateExecutionError) as failure:
        executor.execute(chapter_index=2,confirmation=confirmation,idempotency_key='first')
    cid=failure.value.candidate_id;source=executor.store.load_intent(cid);record=executor.store.load_record(cid)
    old_row=copy.deepcopy(bound.store.execution_attempts(source['execution']['execution_id'])[0])
    ledger=json.loads((bound.root/'ledger.json').read_text())
    args=dict(source_candidate_id=cid,chapter_index=2,confirmation=confirmation,idempotency_key='once',
        expected_ledger_sha256=ledger['ledger_sha256'],author_confirmed=True)
    with pytest.raises(ChapterCandidateConflictError):
        executor.authorize_budgeted_continuation(**args)
    executor.abandon_provider_uncertainty(cid,expected_intent_hash=source['intent_hash'],
        expected_record_hash=record['record_hash'],expected_uncertainty_sha256=candidate_provider_uncertainty_sha256(
            source['execution']['execution_id'],record['evidence']['unresolved_calls']),author_confirmed=True)
    executor.authorize_budgeted_continuation(**args)
    altered=fixture._confirmation(intent={**fixture.intent,'goal':'An unrelated changed goal'})
    with pytest.raises(ChapterBudgetConflict):
        executor.execute(chapter_index=2,confirmation=altered,idempotency_key='once')
    assert len(calls)==1
    result=executor.execute(chapter_index=2,confirmation=confirmation,idempotency_key='once')
    assert result['status']=='completed' and executor.store.load_prose(result['candidate_id'])==prose
    assert executor.execute(chapter_index=2,confirmation=confirmation,idempotency_key='once')==result
    assert len(calls)==2 and calls[0]==calls[1]
    assert bound.store.execution_attempts(source['execution']['execution_id'])[0]==old_row
    assert bound.store.report()['charged']['calls']==2 and len(bound.store.report()['unresolved_attempts'])==1
