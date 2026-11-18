"""Actual owned publication services and durable chapter clocks; no Provider."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.chapter_budget_store import ChapterBudgetError
from core.chapter_execution_budget import ChapterExecutionBudget,current_chapter_operation
from core.chapter_execution_time import ChapterExecutionClock,ChapterTimeExceeded,ChapterTimeUnknown
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_chapter_execution_deadline import ManualClock
from tests.test_chapter_publication_service import case,ready,service_case


def metered(c,monkeypatch):
    manual=ManualClock()
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    budget.time_clock=ChapterExecutionClock(budget.root,budget.store.frozen_budget(),
        wall_clock=manual.wall,monotonic_ns=manual.monotonic_ns)
    def factory(root,*,book_id,chapter):
        assert root==c['root'] and book_id==c['project'].book_id and chapter==2
        return budget
    monkeypatch.setattr(c['publication'],'execution_budget_factory',factory)
    return manual,budget


def test_explicit_quality_actions_share_active_time_and_exclude_author_wait(service_case,monkeypatch):
    c=service_case;manual,budget=metered(c,monkeypatch)
    original=c['service'].prepare_quality
    def counted(**kwargs):
        action=current_chapter_operation()
        assert action and not action.provider_dispatch_allowed and not action.local_only
        assert action.budget.root==budget.root
        manual.advance(3)
        return original(**kwargs)
    monkeypatch.setattr(c['service'],'prepare_quality',counted)
    before=(c['root']/RELATIVE_PATH).read_bytes()
    first=c['publication'].preview(**c['route'])
    assert first['quality']['quality_coverage_complete'] and first['execution_budget']['time']['active_seconds']=='3'
    manual.advance(10000)
    second=c['publication'].preview(**{**c['route'],'idempotency_key':'different-preview-key'})
    assert second['execution_budget']['time']['active_seconds']=='6'
    assert second['execution_budget']['time']['author_wait_seconds']=='10000'
    assert budget.time_clock.report()['interval_count']==2
    assert budget.store.report()['charged']['calls']==0
    assert (c['root']/RELATIVE_PATH).read_bytes()==before
    assert not (c['root']/'.novelagent/runtime/reviewed/intents').exists()
    assert current_chapter_operation() is None


def test_quality_exhaustion_is_measured_and_blocks_new_keys_and_publication(service_case,monkeypatch):
    c=service_case;manual,budget=metered(c,monkeypatch)
    original=c['service'].prepare_quality
    def slow(**kwargs):manual.advance(901);return original(**kwargs)
    monkeypatch.setattr(c['service'],'prepare_quality',slow)
    before=(c['root']/RELATIVE_PATH).read_bytes()
    with pytest.raises(ChapterTimeExceeded):c['publication'].preview(**c['route'])
    assert budget.time_clock.report()['active_seconds']=='901'
    for operation in (c['publication'].preview,c['publication'].commit):
        with pytest.raises(ChapterTimeExceeded):operation(**{**c['route'],'idempotency_key':'after-time-exhaustion'})
    assert budget.time_clock.report()['interval_count']==1
    assert (c['root']/RELATIVE_PATH).read_bytes()==before and infer_next_chapter(c['root'])==2
    assert not (c['root']/'.novelagent/runtime/reviewed/intents').exists()


def test_local_quality_cannot_borrow_an_unbound_runtime_to_dispatch(service_case,monkeypatch):
    c=service_case;manual,budget=metered(c,monkeypatch)
    runtime=ModelCallRuntimeContext(ModelCallStore(c['root'].parent/'forbidden-calls'))
    def faulty_check(**kwargs):
        manual.advance(2)
        return runtime.execute_attempt(call_id='hidden',attempt_number=1,provider='openai',model='fixture',
            stage='unexpected_quality_model',endpoint_type='official',request={'text':'fixture'},
            max_output_tokens=1,input_tokens=1,operation=lambda:pytest.fail('must not dispatch'))
    monkeypatch.setattr(c['service'],'prepare_quality',faulty_check)
    with pytest.raises(ChapterBudgetError):c['publication'].preview(**c['route'])
    assert budget.time_clock.report()['active_seconds']=='2'
    assert not runtime.store.intents_dir.exists() and budget.store.report()['items']==[]


def test_publication_recheck_and_finalization_are_in_one_chapter_interval(service_case,monkeypatch):
    import workbench.chapter_publication as module
    c=service_case;manual,budget=metered(c,monkeypatch)
    prepare=c['service'].prepare;build=module.build_reviewed_chapter_transaction
    def counted(**kwargs):
        assert current_chapter_operation() and not current_chapter_operation().provider_dispatch_allowed
        manual.advance(2);return prepare(**kwargs)
    def quality(**kwargs):manual.advance(4);return build(**kwargs)
    def finish(event,payload):
        if event=='after_file_commit_before_index':manual.advance(5)
    monkeypatch.setattr(c['service'],'prepare',counted)
    monkeypatch.setattr(module,'build_reviewed_chapter_transaction',quality)
    c['publication'].fault_injector=finish
    result=c['publication'].commit(**c['route'])
    assert result['publication_verified'] and budget.time_clock.report()['active_seconds']=='13'
    before=(budget.root/'time.json').read_bytes()
    assert c['publication'].commit(**c['route'])==result
    assert c['publication'].resolve(**c['route'])==result
    assert (budget.root/'time.json').read_bytes()==before


@pytest.mark.parametrize('event,committed',[('before_commit',False),('after_file_commit_before_index',True)])
def test_time_exhaustion_around_publication_preserves_original_recovery(service_case,monkeypatch,event,committed):
    c=service_case;manual,budget=metered(c,monkeypatch)
    def delay(name,payload):
        if name==event:manual.advance(901)
    c['publication'].fault_injector=delay
    if committed:
        result=c['publication'].commit(**c['route'])
        assert result['publication_verified']
    else:
        with pytest.raises(ChapterTimeExceeded):c['publication'].commit(**c['route'])
    assert budget.time_clock.report()['active_seconds']=='901'
    c['publication'].fault_injector=None
    before=(budget.root/'time.json').read_bytes()
    result=c['publication'].resolve(**c['route'])
    assert result['publication_verified'] is committed
    if not committed:assert result['state']=='rolled_back' and result['retry_requires_new_key']
    assert (budget.root/'time.json').read_bytes()==before
    assert infer_next_chapter(c['root'])==(3 if committed else 2)


def test_read_input_and_never_started_status_do_not_initialize_or_change_clock(service_case,monkeypatch):
    c=service_case;manual,budget=metered(c,monkeypatch)
    before=(budget.root/'time.json').read_bytes()
    view=c['publication'].review_input(project_id=c['project'].id,chapter=2,review_id=c['review']['id'])
    assert view['final_draft']==c['draft'].text
    assert c['publication'].resolve(**c['route'])['state']=='not_started'
    assert (budget.root/'time.json').read_bytes()==before
    (budget.root/'time.json').unlink()
    assert c['publication'].resolve(**c['route'])['state']=='not_started'
    with pytest.raises((ChapterBudgetError,RuntimeError)):c['publication'].preview(**c['route'])
    assert not (budget.root/'time.json').exists()


def crash_child(state_path,event):
    from workbench.app import create_app
    state=json.loads(Path(state_path).read_text(encoding='utf-8'))
    app=create_app(database_path=Path(state['database']),allowed_workspaces=[Path(state['root']).parent],
        runtime_root=Path(state['root']).parent/'jobs',start_worker=False,auto_import_active=False,
        chapter_publication_machine_store=Path(state['machine']))
    service=app.state.chapter_publication
    if event=='quality':
        service.preparations.prepare_quality=lambda **kwargs:os._exit(73)
        service.preview(**state['route'])
    else:
        def fault(name,payload):
            if name=='after_file_commit_before_index':os._exit(73)
        service.fault_injector=fault
        service.commit(**state['route'])
    raise AssertionError('process-death boundary was not reached')


@pytest.mark.parametrize('event',['quality','published'])
def test_actual_process_death_retains_unknown_clock_and_original_result(service_case,event):
    c=service_case
    state=c['root'].parent/'quality-child.json'
    state.write_text(json.dumps(dict(root=str(c['root']),database=str(c['db'].path),
        machine=str(c['machine']),route=c['route'])),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',
        'import sys; from tests.test_chapter_quality_budget import crash_child; crash_child(sys.argv[1],sys.argv[2])',
        str(state),event],capture_output=True,text=True,timeout=60)
    assert child.returncode==73,child.stderr
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    before=(budget.root/'time.json').read_bytes()
    assert budget.time_clock.report()['active_status']=='unknown'
    if event=='quality':
        with pytest.raises(ChapterTimeUnknown):c['publication'].preview(**c['route'])
        assert infer_next_chapter(c['root'])==2
    else:
        result=c['publication'].resolve(**c['route'])
        assert result['publication_verified'] and infer_next_chapter(c['root'])==3
        assert c['publication'].commit(**c['route'])==result
    assert (budget.root/'time.json').read_bytes()==before
