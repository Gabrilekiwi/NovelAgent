from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import uuid

import pytest

from api.contracts import ModelResponse, reported_model_usage
from api.retry import MODEL_READ_GENERATION, RetryPolicy
from core.chapter_budget_contract import freeze_chapter_budget
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetConflict
from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterExecutionClock, ChapterTimeError
from core.engine.chapter_fact_context import build_chapter_fact_context
from core.engine.chapter_fact_extraction import (ChapterFactExtractionExecutor,
    ChapterFactExtractionExecutionError, _durable_openai_fact_provider)
from core.model_call_runtime import current_model_call_runtime
from tests import test_chapter_candidate as candidate_fixture
from tests.test_chapter_execution_deadline import ManualClock
from tests.test_chapter_fact_extraction import (BOOK_ID,_execute,_provenance,_raw_empty)


@pytest.fixture
def short_root():
    base=Path.cwd()/'.tmp/eb';root=base/uuid.uuid4().hex[:12];root.mkdir(parents=True)
    yield root
    assert root.resolve().is_relative_to(base.resolve())
    shutil.rmtree(root)


def scope(root,*,book_id=BOOK_ID,chapter=27,calls=10,manual=None):
    manual=manual or ManualClock()
    frozen=freeze_chapter_budget(book_id=book_id,chapter=chapter,max_provider_calls=calls,
        max_input_tokens=100000,max_output_tokens=100000,max_active_seconds='900')
    directory=root/f'.novelagent/runtime/budgets/c{chapter}'
    ChapterBudgetFreezeRegistry(root,book_id=book_id).freeze(frozen,before_write=lambda:None)
    timer=ChapterExecutionClock(directory,frozen,wall_clock=manual.wall,monotonic_ns=manual.monotonic_ns)
    timer.initialize()
    return ChapterExecutionBudget(root,book_id=book_id,chapter=chapter,time_clock=timer)


class Crash(BaseException):pass


class InjectedScope(ChapterExecutionBudget):
    fault=None
    saved_runtime=None
    @contextmanager
    def activate(self,runtime,intent):
        with super().activate(runtime,intent):
            self.saved_runtime=runtime
            runtime.fault_injector=self.fault
            yield runtime


def inject(source,fault):
    result=InjectedScope(source.story_project_root,book_id=source.store.book_id,
        chapter=source.store.chapter,time_clock=source.time_clock)
    result.fault=fault
    return result


def model_request(runtime,request,call_id,stage,operation,attempt=1):
    return runtime.execute_attempt(call_id=call_id,attempt_number=attempt,provider='fixture',model='fixture',
        endpoint_type='official',endpoint_fingerprint='a'*64,stage=stage,request=request,
        input_tokens=10,max_output_tokens=128,operation=operation)


def response(text):
    return ModelResponse(text,actual_model='fixture',endpoint_type='official',
        usage=reported_model_usage({'input_tokens':10,'output_tokens':20}))


def fact_provider(calls):
    def provider(messages,call_id,evidence_root):
        runtime=current_model_call_runtime()
        assert runtime.store.root==evidence_root and runtime.active_interval is not None
        attempt=0
        def operation():calls.append(1);return response(_raw_empty())
        def invoke():
            nonlocal attempt
            attempt+=1
            return model_request(runtime,{'messages':messages},call_id,'facts',operation,attempt)
        return RetryPolicy(MODEL_READ_GENERATION,max_attempts=2,base_delay_seconds=0,max_delay_seconds=0).execute(invoke).value.text
    return provider


def executor(root,budget,provider):
    return ChapterFactExtractionExecutor(story_project_root=root,execution_budget=budget,
        provider=provider,provenance_factory=_provenance)


@pytest.mark.parametrize('event',['after_chapter_reservation_before_intent','after_intent_before_dispatch'])
def test_fact_executor_recovers_unsent_before_unresolved_gate(short_root,event):
    budget=scope(short_root);calls=[]
    failing=inject(budget,lambda point,*unused:(_ for _ in ()).throw(Crash()) if point==event else None)
    first=executor(short_root,failing,fact_provider(calls))
    with pytest.raises(Crash):_execute(first)
    extraction=next(first.store.root.iterdir()).name
    assert not calls
    recovered=executor(short_root,budget,fact_provider(calls)).resume(extraction)
    assert recovered['status']=='completed' and len(calls)==1
    model_store=first.store.model_call_store(first.store.load_intent(extraction))
    assert model_store.load_receipt('fact-a1')['status']=='request_not_sent'
    assert model_store.load_receipt('fact-a2')['status']=='succeeded'
    assert budget.store.report()['charged']['calls']==1


def test_fact_successful_response_reuse_reconciles_budget_without_provider(short_root):
    budget=scope(short_root);calls=[]
    failing=inject(budget,lambda point,*unused:(_ for _ in ()).throw(Crash()) if point=='after_receipt_before_chapter_settlement' else None)
    first=executor(short_root,failing,fact_provider(calls))
    with pytest.raises(Crash):_execute(first)
    extraction=next(first.store.root.iterdir()).name
    assert budget.store.report()['unresolved_attempts'] and len(calls)==1
    def forbidden(*args):pytest.fail('completed response must be reused locally')
    recovered=executor(short_root,budget,forbidden).resume(extraction)
    assert recovered['status']=='completed' and len(calls)==1
    assert not budget.store.report()['unresolved_attempts']


def test_default_fact_provider_reuses_bound_runtime(short_root,monkeypatch):
    budget=scope(short_root);calls=[]
    def chat(messages,**kwargs):
        active=current_model_call_runtime()
        assert kwargs['model_call_runtime'] is active and active.chapter_budget is not None
        assert active.active_interval is not None
        calls.append(1)
        return model_request(active,{'messages':messages},kwargs['call_id'],'facts',lambda:response(_raw_empty()))
    monkeypatch.setattr('core.engine.chapter_fact_extraction.chat_completion',chat)
    record=_execute(executor(short_root,budget,_durable_openai_fact_provider))
    assert record['status']=='completed' and calls==[1]


def test_runtime_cannot_escape_closed_execution_time_scope(short_root):
    budget=inject(scope(short_root),None);calls=[]
    _execute(executor(short_root,budget,fact_provider(calls)))
    active=budget.saved_runtime
    assert active.active_interval.closed
    with pytest.raises(ChapterTimeError):
        model_request(active,{},'escaped','facts',lambda:pytest.fail('closed scope cannot spend'))
    assert calls==[1]


def test_scope_refuses_missing_time_evidence_without_initializing(short_root):
    budget=scope(short_root)
    (budget.root/'time.json').unlink()
    with pytest.raises(ChapterTimeError):_execute(executor(short_root,budget,fact_provider([])))
    assert not (budget.root/'time.json').exists()
    assert budget.store.report()['charged']['calls']==0


def test_wrong_chapter_scope_rejects_fact_intent(short_root):
    budget=scope(short_root,chapter=28)
    with pytest.raises(ChapterBudgetConflict):_execute(executor(short_root,budget,fact_provider([])))
    assert budget.store.report()['charged']['calls']==0


def test_real_candidate_and_fact_executors_share_chapter_budget():
    # Reuse the existing real synthetic Strict setup and real context builders.
    fixture=candidate_fixture.ChapterCandidateTest();fixture.setUp()
    manual=ManualClock();budget=scope(fixture.book,book_id=fixture.identity.book_id,chapter=2,calls=1,manual=manual)
    prose='林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。'
    calls=[]
    def generator(input_pack):
        runtime=current_model_call_runtime()
        call_id=runtime.new_call_id(provider='fixture',stage='generation')
        def operation():calls.append('generation');manual.advance(2);return response(prose)
        return model_request(runtime,{'input':input_pack},call_id,'generation',operation).text
    candidate=fixture._execute(fixture._executor(generator=generator,execution_budget=budget),key='shared-budget')
    assert candidate['status']=='completed'
    original=fixture._executor().store.load_intent(candidate['candidate_id'])
    assert budget.time_clock.report()['active_seconds']=='2'
    manual.advance(100000)
    restarted=ChapterExecutionBudget(fixture.book,book_id=fixture.identity.book_id,chapter=2,time_clock=budget.time_clock)
    fact=executor(fixture.book,restarted,fact_provider(calls))
    trusted=fixture._executor().context_builder.build(2)
    with pytest.raises(ChapterFactExtractionExecutionError) as caught:
        fact.execute(book_id=fixture.identity.book_id,chapter_index=2,
            candidate_binding={'candidate_id':candidate['candidate_id'],'intent_hash':candidate['intent_hash']},
            draft_binding={'draft_id':'candidate_draft_'+'d'*32,'revision':1,'sha256':hashlib.sha256(prose.encode()).hexdigest()},
            context_sha256=original['source']['context_sha256'],authority_binding=original['authority'],
            chapter_text=prose,authority_view=build_chapter_fact_context(trusted,prose).to_dict(),
            idempotency_key='shared-facts',author_confirmed=True)
    assert caught.value.record['status']=='failed'
    assert calls==['generation'] and budget.store.report()['charged']['calls']==1
    assert budget.time_clock.report()['active_seconds']=='2'
    assert budget.time_clock.report()['author_wait_seconds']=='100000'
