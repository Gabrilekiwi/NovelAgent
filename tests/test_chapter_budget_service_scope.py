"""Actual reviewed service actions must include work outside the executor."""
import pytest

from core.chapter_execution_budget import ChapterExecutionBudget,current_chapter_operation
from core.chapter_execution_time import ChapterExecutionClock
from core.model_call_runtime import current_model_call_runtime
from tests.test_chapter_budget_api import fresh_api
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_candidate_context import author_confirmation
from tests.test_chapter_execution_deadline import ManualClock
from tests.test_chapter_budget_executors import model_request,response,fact_provider,short_root,scope,executor


def setup(c,monkeypatch,*,limit='900',preparation=2):
    c['payload']['configuration']['max_active_seconds']=limit
    assert c['client'].post(c['path'],json=c['payload']).status_code==200
    manual=ManualClock()
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    budget.time_clock=ChapterExecutionClock(budget.root,budget.store.frozen_budget(),wall_clock=manual.wall,monotonic_ns=manual.monotonic_ns)
    service=c['app'].state.chapter_candidates.reviewed
    factory=service.executor_factory
    captured=[]
    def counted_factory(root):
        executor=factory(root)
        executor.execution_budget_factory=lambda *args,**kwargs:budget
        capture=executor._capture_provenance
        def provenance():
            if executor._captured_provenance is None:manual.advance(4)
            return capture()
        executor._capture_provenance=provenance
        return executor
    monkeypatch.setattr(service,'executor_factory',counted_factory)
    verify=service.chapter_cards.verify_confirmation
    def verify_with_time(*args,**kwargs):
        assert current_chapter_operation() is not None
        manual.advance(preparation)
        return verify(*args,**kwargs)
    monkeypatch.setattr(service.chapter_cards,'verify_confirmation',verify_with_time)
    publish=service._public_candidate
    def finish(*args,**kwargs):
        assert current_chapter_operation() is not None
        manual.advance(5)
        return publish(*args,**kwargs)
    monkeypatch.setattr(service,'_public_candidate',finish)
    prose='林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。'
    def generate(text,**kwargs):
        runtime=current_model_call_runtime();captured.append(runtime)
        def operation():manual.advance(3);return response(prose)
        return model_request(runtime,{'input':text},runtime.new_call_id(provider='fixture',stage='generation'),
            'generation',operation).text
    monkeypatch.setattr('core.engine.chapter_candidate.generate_chapter',generate)
    confirmation=author_confirmation(c['app'].state.chapter_publication.projects,c['project'])
    def run():
        return c['client'].post(c['path'].removesuffix('/budget')+'/candidates',json={'confirmation':confirmation},
            headers={'Idempotency-Key':'service-clock'})
    return manual,budget,run,captured


def test_candidate_preparation_provenance_generation_and_finalize_count_once(fresh_api,monkeypatch):
    manual,budget,run,captured=setup(fresh_api,monkeypatch)
    result=run();assert result.status_code==200,result.text
    assert result.json()['candidate']['status']=='completed'
    report=budget.time_clock.report()
    assert report['active_seconds']=='14' and report['interval_count']==1
    assert len(captured)==1 and captured[0].active_interval.closed
    assert current_chapter_operation() is None


def test_preparation_exhaustion_stops_before_provider_without_losing_local_time(fresh_api,monkeypatch):
    manual,budget,run,captured=setup(fresh_api,monkeypatch,limit='1',preparation=2)
    result=run();assert result.status_code==200,result.text
    assert result.json()['candidate']['status']=='failed'
    assert budget.store.report()['charged']['calls']==0
    assert budget.time_clock.report()['active_seconds']=='11'
    assert not budget.store.report()['items']


def test_completed_request_replay_after_limit_uses_only_local_service_time(fresh_api,monkeypatch):
    manual,budget,run,captured=setup(fresh_api,monkeypatch,limit='15')
    first=run();assert first.status_code==200,first.text
    with budget.time_clock.active('consume-final-second'):manual.advance(1)
    before=budget.store.report()
    replay=run();assert replay.status_code==200,replay.text
    assert replay.json()['candidate']['candidate_id']==first.json()['candidate']['candidate_id']
    assert len(captured)==1 and budget.store.report()==before
    report=budget.time_clock.report()
    assert report['active_seconds']=='22' and report['local_recovery_active_seconds']=='7'


def test_fact_context_preparation_and_finalization_share_one_interval(fresh_api,monkeypatch):
    c=fresh_api;manual,budget,run,captured=setup(c,monkeypatch)
    generated=run();assert generated.status_code==200,generated.text
    draft=generated.json()['candidate']['draft']
    service=c['app'].state.chapter_fact_extractions.reviewed
    factory=service.executor_factory;calls=[]
    def facts_factory(root):
        executor=factory(root);executor.execution_budget_factory=lambda *args,**kwargs:budget
        executor.provider=fact_provider(calls)
        return executor
    monkeypatch.setattr(service,'executor_factory',facts_factory)
    prepare=service._prepare_current_draft
    def timed_prepare(*args,**kwargs):
        assert current_chapter_operation() is not None
        manual.advance(6);return prepare(*args,**kwargs)
    monkeypatch.setattr(service,'_prepare_current_draft',timed_prepare)
    finalize=service._finalize
    def timed_finalize(*args,**kwargs):
        assert current_chapter_operation() is not None
        manual.advance(7);return finalize(*args,**kwargs)
    monkeypatch.setattr(service,'_finalize',timed_finalize)
    manual.advance(100000)
    result=c['client'].post(f"/api/candidate-drafts/{draft['id']}/fact-extractions",json={
        'expected_draft_revision':draft['revision'],'expected_draft_sha256':draft['sha256'],
        'author_confirmed':True},headers={'Idempotency-Key':'timed-facts'})
    assert result.status_code==200,result.text
    assert result.json()['fact_extraction']['status']=='completed'
    assert calls==[1]
    report=budget.time_clock.report()
    assert report['active_seconds']=='27' and report['interval_count']==2
    assert report['author_wait_seconds']=='100000'


def test_outer_scope_cannot_be_borrowed_by_another_execution(short_root):
    from core.chapter_budget_store import ChapterBudgetConflict
    from core.model_call_runtime import ModelCallRuntimeContext
    from tests.test_chapter_fact_extraction import _execute
    budget=scope(short_root)
    first=executor(short_root,budget,fact_provider([]));record=_execute(first)
    intent=first.store.load_intent(record['extraction_id'])
    runtime=ModelCallRuntimeContext(first.store.model_call_store(intent))
    before=budget.store.report()
    with budget.operation('another-execution'):
        with pytest.raises(ChapterBudgetConflict):
            with budget.activate(runtime,intent):pytest.fail('wrong operation scope')
    assert budget.store.report()==before
    assert current_chapter_operation() is None


def test_scope_exception_closes_handle_and_prevents_nested_operations(short_root):
    from core.chapter_budget_store import ChapterBudgetConflict
    budget=scope(short_root)
    with pytest.raises(ValueError):
        with budget.operation('outer') as action:
            with pytest.raises(ChapterBudgetConflict):
                with budget.operation('inner'):pytest.fail('nested timer')
            raise ValueError('preparation failed')
    assert action.closed and action.interval.closed
    assert current_chapter_operation() is None
    assert budget.time_clock.report()['interval_count']==1


@pytest.mark.parametrize('local_only',[True,False])
def test_service_operation_cannot_create_an_unbudgeted_runtime_attempt(short_root,local_only):
    from core.chapter_budget_store import ChapterBudgetError
    from core.model_call_runtime import ModelCallRuntimeContext
    from core.model_calls import ModelCallStore
    budget=scope(short_root);runtime=ModelCallRuntimeContext(ModelCallStore(short_root/'other'))
    with budget.operation('service-operation',local_only=local_only):
        with pytest.raises(ChapterBudgetError):
            model_request(runtime,{},'forbidden','facts',lambda:pytest.fail('no Provider'))
    assert not runtime.store.intents_dir.exists()
