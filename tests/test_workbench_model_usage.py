import json
from pathlib import Path

import pytest

from core.model_calls import ModelCallStore, ModelCallIntegrityError
from core.model_call_usage import USAGE_SOURCE_KEY
from tests.test_model_call_usage import evidence
from workbench.model_usage import read_project_model_usage


def put(root,execution,*,usage=None,receipt=True):
    store=ModelCallStore(root / '.novelagent/runtime/runs/executions' / execution / 'model_calls')
    intent,result=evidence(usage)
    store.record_intent(intent)
    if receipt:store.record_receipt(result)
    return store,intent,result


def test_empty_read_does_not_create_directories(tmp_path):
    before=list(tmp_path.iterdir())
    report=read_project_model_usage(tmp_path)
    assert report['summary']['attempt_count']==0 and report['items']==[]
    assert list(tmp_path.iterdir())==before


def test_actual_stores_replay_and_cross_execution_ids_are_counted_once_each(tmp_path):
    store,intent,receipt=put(tmp_path,'execution_one',usage={USAGE_SOURCE_KEY:'provider_reported','input_tokens':4,'output_tokens':2})
    put(tmp_path,'execution_two',usage={'input_tokens':9},receipt=False)
    before={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    store.record_receipt(receipt)
    first=read_project_model_usage(tmp_path)
    second=read_project_model_usage(tmp_path)
    assert first['items']==second['items'] and first['summary']==second['summary']
    assert first['summary']['attempt_count']==2
    assert first['summary']['status_counts']=={'awaiting_receipt':1,'succeeded':1}
    assert first['summary']['unknown_cost_count']==2
    assert before=={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('damage',['orphan','hash','filename'])
def test_incomplete_or_tampered_evidence_never_returns_partial_total(tmp_path,damage):
    store,intent,receipt=put(tmp_path,'execution_one',usage={'input_tokens':5})
    if damage=='orphan':store.intent_path(intent['attempt_id']).unlink()
    elif damage=='hash':
        receipt['usage']['input_tokens']=2
        store.receipt_path(intent['attempt_id']).write_text(json.dumps(receipt),encoding='utf-8')
    else:store.intent_path(intent['attempt_id']).rename(store.intents_dir/'wrong-name.json')
    with pytest.raises((ModelCallIntegrityError,OSError,RuntimeError)):read_project_model_usage(tmp_path)


def test_report_size_limit_is_explicit_and_does_not_truncate_costs(tmp_path,monkeypatch):
    put(tmp_path,'execution_one')
    put(tmp_path,'execution_two')
    monkeypatch.setattr('workbench.model_usage.MAX_ATTEMPTS',1)
    with pytest.raises(ModelCallIntegrityError,match='partial total'):read_project_model_usage(tmp_path)


def test_planning_amendments_and_chapter_attempts_are_all_included(tmp_path):
    _,intent,receipt=put(tmp_path,'execution_one')
    for family in ('workbench_outline_batches','workbench_amendments'):
        store=ModelCallStore(tmp_path/'.novelagent/runtime/model_calls'/family/'operation_a')
        store.record_intent(intent);store.record_receipt(receipt)
    report=read_project_model_usage(tmp_path)
    assert report['summary']['attempt_count']==3
    assert {row['execution_id'] for row in report['items']}=={'execution_one',
        'workbench_outline_batches:operation_a','workbench_amendments:operation_a'}
