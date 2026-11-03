from datetime import datetime, timedelta, timezone
from decimal import localcontext
from types import SimpleNamespace as NS
import sys

import pytest

from api.claude_client import polish_chapter
from api.contracts import ModelCallError
from api.execution_deadline import RequestExecutionDeadline
from api.openai_client import chat_completion
from api.retry import CLAUDE_POLISH, MODEL_READ_GENERATION, RetryPolicy
from core.chapter_budget_contract import freeze_chapter_budget
from core.chapter_execution_time import ChapterExecutionClock, ChapterTimeError, ChapterTimeExceeded
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from tests.test_chapter_budget_runtime import setup, runtime, args, forbidden


class ManualClock:
    ns=0
    def advance(self,seconds):self.ns+=int(seconds*1e9)
    def monotonic_ns(self):return self.ns
    def seconds(self):return self.ns/1e9
    def wall(self):return datetime(2026,9,10,tzinfo=timezone.utc)+timedelta(seconds=self.seconds())


def execution_clock(root,manual,seconds='10'):
    budget=freeze_chapter_budget(book_id='book-test',chapter=2,max_provider_calls=10,
        max_input_tokens=10000,max_output_tokens=10000,max_active_seconds=seconds)
    return ChapterExecutionClock(root,budget,wall_clock=manual.wall,monotonic_ns=manual.monotonic_ns)


def configure(monkeypatch,provider,create,stream=None):
    config=NS(openai_api_key='synthetic',openai_model='fixture',openai_base_url=None,
        openai_timeout_seconds=500,openai_max_output_tokens=10,openai_stream=stream is not None,
        anthropic_api_key='synthetic',claude_model='fixture',claude_base_url=None,
        claude_timeout_seconds=500,claude_max_tokens=10,claude_stream=stream is not None,claude_user_agent=None)
    if provider=='openai':
        monkeypatch.setattr('api.openai_client.get_config',lambda:config)
        monkeypatch.setitem(sys.modules,'openai',NS(OpenAI=lambda **kwargs:NS(chat=NS(completions=NS(create=stream or create)))))
    else:
        monkeypatch.setattr('api.claude_client.get_config',lambda:config)
        messages=NS(create=create)
        if stream is not None:messages.stream=stream
        monkeypatch.setitem(sys.modules,'anthropic',NS(Anthropic=lambda **kwargs:NS(messages=messages)))
    return config


def sdk_response(provider):
    if provider=='openai':
        return NS(model='fixture',choices=[NS(message=NS(content='synthetic'),finish_reason='stop')],
            usage={'prompt_tokens':1,'completion_tokens':1})
    return NS(model='fixture',content=[NS(text='synthetic')],usage={'input_tokens':1,'output_tokens':1},stop_reason='end_turn')


def invoke(provider,context,manual,**policy_changes):
    values=dict(profile=MODEL_READ_GENERATION if provider=='openai' else CLAUDE_POLISH,
        max_attempts=3,base_delay_seconds=1,max_delay_seconds=1,jitter_ratio=0,
        deadline_seconds=100,clock=manual.seconds,sleep=manual.advance)
    values.update(policy_changes)
    kwargs=dict(model_call_runtime=context,retry_policy=RetryPolicy(**values),call_id='timed',input_tokens=1)
    if provider=='openai':return chat_completion([{'role':'user','content':'synthetic'}],**kwargs)
    return polish_chapter('synthetic',**kwargs)


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_sdk_timeout_uses_persistent_active_time_excluding_author_wait(tmp_path,monkeypatch,provider):
    manual=ManualClock();clock=execution_clock(tmp_path/'t',manual);clock.initialize();timeouts=[]
    def create(**kwargs):
        timeouts.append(kwargs['timeout']);manual.advance(2)
        return sdk_response(provider)
    configure(monkeypatch,provider,create)
    with clock.active('generation',awaiting_author_after=True) as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c1'),active_interval=interval)
        assert invoke(provider,r,manual)=='synthetic'
    manual.advance(100000)
    restarted=execution_clock(tmp_path/'t',manual)
    with restarted.active('facts',awaiting_author_after=True) as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c2'),active_interval=interval)
        assert invoke(provider,r,manual)=='synthetic'
    assert timeouts==pytest.approx([10,8]) and timeouts[0]<=10 and timeouts[1]<=8
    report=restarted.report()
    assert report['active_seconds']=='4'
    assert report['author_wait_seconds']=='100000'


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_no_sdk_request_after_active_time_exhausted(tmp_path,monkeypatch,provider):
    manual=ManualClock();clock=execution_clock(tmp_path/'t',manual);clock.initialize()
    configure(monkeypatch,provider,lambda **kwargs:pytest.fail('unexpected SDK request'))
    with clock.active('generation') as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c'),active_interval=interval)
        manual.advance(10)
        with pytest.raises(ModelCallError) as caught:invoke(provider,r,manual)
        assert caught.value.failure_category=='local_budget' and not caught.value.retryable
        assert not r.store.intents_dir.exists()
    assert clock.report()['active_seconds']=='10'


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_retry_timeout_shrinks_after_failed_connection_and_backoff(tmp_path,monkeypatch,provider):
    manual=ManualClock();clock=execution_clock(tmp_path/'t',manual);clock.initialize();timeouts=[]
    def create(**kwargs):
        timeouts.append(kwargs['timeout']);manual.advance(2)
        if len(timeouts)==1:
            import httpx
            raise httpx.ConnectError('synthetic connection failure')
        return sdk_response(provider)
    configure(monkeypatch,provider,create)
    with clock.active('generation') as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c'),active_interval=interval)
        invoke(provider,r,manual)
    assert timeouts==pytest.approx([10,7])
    assert clock.report()['active_seconds']=='5'
    assert r.store.load_receipt('timed-a1')['status']=='request_not_sent'
    assert r.store.load_receipt('timed-a2')['status']=='succeeded'


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_retry_cannot_wait_past_remaining_active_budget(tmp_path,monkeypatch,provider):
    manual=ManualClock();clock=execution_clock(tmp_path/'t',manual);clock.initialize();calls=[]
    def create(**kwargs):
        import httpx
        calls.append(1);manual.advance(9)
        raise httpx.ConnectError('synthetic')
    configure(monkeypatch,provider,create)
    with clock.active('generation') as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c'),active_interval=interval)
        with pytest.raises(ModelCallError) as caught:invoke(provider,r,manual)
        assert caught.value.retry_stop_reason=='run_budget_exhausted'
    assert len(calls)==1 and clock.report()['active_seconds']=='9'


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_overslept_backoff_is_checked_again_before_next_sdk_request(tmp_path,monkeypatch,provider):
    manual=ManualClock();clock=execution_clock(tmp_path/'t',manual);clock.initialize();calls=[]
    def create(**kwargs):
        import httpx
        calls.append(1);manual.advance(8)
        raise httpx.ConnectError('synthetic')
    configure(monkeypatch,provider,create)
    with clock.active('generation') as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c'),active_interval=interval)
        with pytest.raises(ModelCallError) as caught:invoke(provider,r,manual,sleep=lambda unused:manual.advance(3))
        assert caught.value.failure_category=='local_budget'
        assert not r.store.intent_path('timed-a2').exists()
    assert len(calls)==1 and clock.report()['active_seconds']=='11'


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_stream_total_time_is_checked_and_partial_content_is_preserved(tmp_path,monkeypatch,provider):
    manual=ManualClock();clock=execution_clock(tmp_path/'t',manual);clock.initialize();closes=[];requests=[]
    class Stream:
        def __enter__(self):return self
        def __exit__(self,*args):closes.append(1)
        def close(self):closes.append(1)
        @property
        def text_stream(self):return iter(self)
        def __iter__(self):
            for text in ['first','second','must not arrive']:
                manual.advance(6)
                if provider=='openai':yield NS(choices=[NS(delta=NS(content=text),finish_reason=None)])
                else:yield text
    def stream(**kwargs):requests.append(kwargs);return Stream()
    configure(monkeypatch,provider,lambda **kwargs:pytest.fail('unexpected non-stream request'),stream)
    with clock.active('generation') as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c'),active_interval=interval)
        with pytest.raises(ModelCallError) as caught:invoke(provider,r,manual)
        assert caught.value.failure_category=='provider_partial_response' and caught.value.partial_content_received
    assert len(requests)==1 and requests[0]['timeout']==pytest.approx(10)
    assert len(closes)==1 and clock.report()['active_seconds']=='12'
    receipt=r.store.load_receipt('timed-a1')
    assert receipt['status']=='partial_response_failed'
    assert (r.store.root/receipt['response_artifact_ref']).read_text()=='firstsecond'


def test_deadline_obeys_smaller_policy_and_sdk_limits_and_decimal_context(tmp_path):
    manual=ManualClock();clock=execution_clock(tmp_path,manual,'100');clock.initialize()
    with clock.active('generation') as interval:
        r=ModelCallRuntimeContext(ModelCallStore(tmp_path/'c'),active_interval=interval)
        policy=RetryPolicy(MODEL_READ_GENERATION,deadline_seconds=9.999,clock=manual.seconds)
        deadline=RequestExecutionDeadline(r,policy,configured_timeout=500)
        manual.advance(.001)
        with localcontext() as context:
            context.prec=2
            deadline.prepare()
        assert 9.997<deadline.timeout<=9.998
        smaller=RequestExecutionDeadline(r,policy,configured_timeout=2,extra_remaining=lambda:1.5)
        smaller.prepare()
        assert 1.499<smaller.timeout<=1.5


def test_time_rechecked_after_reservation_and_expired_attempt_is_released(tmp_path):
    s=setup(tmp_path);manual=ManualClock()
    clock=ChapterExecutionClock(tmp_path/'b',s.frozen_budget(),wall_clock=manual.wall,monotonic_ns=manual.monotonic_ns)
    clock.initialize()
    def fault(event,*unused):
        if event=='after_intent_before_dispatch':manual.advance(900)
    with clock.active('generation') as interval:
        r=runtime(tmp_path,active_interval=interval,fault_injector=fault)
        with pytest.raises(ChapterTimeExceeded):r.execute_attempt(**args(operation=forbidden))
    assert s.report()['charged_amount']=='0'
    assert r.store.load_receipt('call-a1')['status']=='budget_rejected'


def test_time_interval_from_another_frozen_budget_is_rejected(tmp_path):
    s=setup(tmp_path);manual=ManualClock();other=execution_clock(tmp_path/'other',manual);other.initialize()
    with other.active('generation') as interval:
        with pytest.raises(ChapterTimeError):runtime(tmp_path,active_interval=interval).execute_attempt(**args(operation=forbidden))
    assert s.report()['charged']['calls']==0
