import json
from pathlib import Path

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_history import absent_source
from tests.test_workbench_model_usage import put
from core.memory_v2.canonical import canonical_json_hash
from workbench.backup_scope import inspect_backup_scope
from tests.test_backup_scope import inspect,codes
from workbench.backup_archive_usage import inspect_archived_usage
from workbench.model_usage import read_project_model_usage


def seed_resolution(c,damage):
    execution='execution_old' if damage!='candidate' else 'execution_candidate_old'
    store,intent,_=put(c['root'],execution,receipt=False)
    marker={'schema_version':'1.2','id':'resolution_20261118T000000Z_test','created_at':'2026-11-18T00:00:00+00:00',
        'book_id':c['project'].book_id,'chapter_index':1,'action':'reset','source_run_id':'unbound_'+execution,
        'resolved_execution_ids':[execution],'resolved_attempt_ids':[intent['attempt_id']],
        'discarded_run_ids':[],'expected_scene_count':1,'complete_draft':None,'scenes':[],
        'reason':'provider_call_uncertain_without_run'}
    if damage=='wrong_book':marker['book_id']='another-book'
    if damage=='wrong_attempt':marker['resolved_attempt_ids']=['different-a1']
    if damage=='wrong_execution':marker['resolved_execution_ids']=['execution_other']
    if damage=='missing_source':marker['source_run_id']='ordinary_missing_run'
    if damage=='early':marker['created_at']='2026-01-01T00:00:00+00:00'
    marker['resolution_hash']=canonical_json_hash(marker,exclude_fields=('resolution_hash',),exclude_environment_fields=False)
    if damage=='hash':marker['resolution_hash']='a'*64
    directory=c['root']/'.novelagent/runtime/runs/locked_chapter_resolutions';directory.mkdir(parents=True)
    if damage!='absent':(directory/(marker['id']+'.json')).write_text(json.dumps(marker),encoding='utf-8')
    return store


@pytest.mark.parametrize('damage',['none','absent','wrong_book','wrong_attempt','wrong_execution','missing_source','early','hash','candidate'])
def test_only_original_matching_resolution_preserves_unknown_without_fee_settlement(legacy,damage):
    c=legacy;store=seed_resolution(c,damage)
    before=read_project_model_usage(c['root'],book_id=c['project'].book_id)
    result=inspect(c)
    if damage=='none':
        assert result['scope_checks_passed'],result['blockers']
        record=next(x for x in result['checks'] if x['check']=='previously_resolved_legacy_unknown_fees')['evidence'][0]
        assert record['fee_status']=='unknown' and record['receipt_created'] is False
    else:assert not result['scope_checks_passed']
    after=read_project_model_usage(c['root'],book_id=c['project'].book_id)
    assert before['items']==after['items'] and before['summary']==after['summary']
    assert after['items'][0]['receipt_hash'] is None
    assert after['items'][0]['reservation']['reserved_output_tokens']==500


def test_archive_verifies_resolution_independently_and_keeps_unknown_cost(archive_case):
    c=archive_case;seed_resolution(c,'none')
    before=read_project_model_usage(c['root'],book_id=c['project'].book_id)
    package=Path(capture(c)['path'])
    with absent_source(c['root']):
        result=inspect_archived_usage(package,project_id=c['project'].id,book_id=c['project'].book_id)
    assert result['model_and_budgets_verified'],result['blockers']
    # Offline activity is known to be false; the live legacy reader leaves it
    # unobserved. Their display status can differ, but accounting must not.
    for key in ('cost','usage','reservation','intent_hash','receipt_hash'):
        assert result['usage']['items'][0][key]==before['items'][0][key]
    assert result['usage']['summary']['costs_by_currency']==before['summary']['costs_by_currency']
    assert result['usage']['retained_unknown_calls'][0]['fee_status']=='unknown'
    assert result['recoverable'] is False
