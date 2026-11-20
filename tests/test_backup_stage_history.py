"""Archived actual stage append protocol is checked with all live stores forbidden."""
from pathlib import Path
import json

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_backup_autonomy_settlement import forbid_recovery
from tests.test_backup_archive_usage import forbid_runtime
from tests.test_model_usage_budget import seed as model_seed
from core.autonomy.receipts import StageReceiptStore
from core.autonomy.common import canonical_hash
from core.stage_control import build_stage_authorization,build_stage_receipt
from core.engine.recovery_protocol import build_marker_envelope
from workbench.backup_auxiliary_history import inspect_archived_auxiliary_history
from workbench.backup_files import inventory_tree


NOW='2026-07-14T00:00:00+00:00'


def append_stage(c,*,kind='deterministic',status='succeeded',chain=False,chapter_mismatch=False,missing_model=False):
    store=StageReceiptStore(c['root']/'.novelagent/runtime/autonomy')
    lease=store.leases.acquire(book_id=c['project'].book_id,session_id='stage-session',plan_id='stage-plan',at=NOW)
    model=None
    if kind=='model' and status=='succeeded':
        if missing_model:model='5'*64
        else:
            _,calls,_=model_seed(c['root'],book_id=c['project'].book_id,chapter=3 if chapter_mismatch else 2)
            model=json.loads(next(calls.receipts_dir.iterdir()).read_bytes())['receipt_hash']
    previous=None
    for stage in (('outline','scene_plan') if chain else ('outline',)):
        auth=build_stage_authorization(stage=stage,book_id=c['project'].book_id,session_id='stage-session',plan_id='stage-plan',
            chapter_index=2,authority_epoch=2,authority_head_event_hash='2'*64,input_digest='3'*64,
            previous_stage_receipt_hash=previous,provider_profile='synthetic',max_output_tokens=100,
            execution_kind=kind,issued_at=NOW)
        receipt=build_stage_receipt(auth,status=status,output_digest='4'*64 if status=='succeeded' else None,
            model_call_receipt_hash=model,execution_evidence_hash='e'*64 if kind=='deterministic' and status=='succeeded' else None,
            created_at=NOW)
        store.append(receipt,authorization=auth,expected_lease_hash=lease['lease_hash'],at=NOW)
        previous=receipt['receipt_hash']
    store.leases.release(book_id=c['project'].book_id,session_id='stage-session',plan_id='stage-plan',
        expected_lease_hash=lease['lease_hash'],at='2026-07-14T00:01:00+00:00')
    return store


def inspect(c,package):
    return inspect_archived_auxiliary_history(package,project_id=c['project'].id,book_id=c['project'].book_id)


@pytest.mark.parametrize('scenario',['deterministic','chain','model','failed','budget_rejected'])
def test_real_stage_markers_authorizations_fences_and_call_receipts_verify_offline(archive_case,scenario,monkeypatch):
    c=archive_case
    store=append_stage(c,kind='model' if scenario=='model' else 'deterministic',
        status=scenario if scenario in {'failed','budget_rejected'} else 'succeeded',chain=scenario=='chain')
    package=Path(capture(c)['path']);before=inventory_tree(package);book=inventory_tree(c['root'])
    forbid_recovery(monkeypatch);forbid_runtime(monkeypatch)
    def forbidden(*args,**kwargs):pytest.fail('offline stage verifier opened a live StageReceiptStore')
    monkeypatch.setattr(StageReceiptStore,'__init__',forbidden)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['complete_history_certified'],first['blockers']
    assert len(first['histories'][0]['stage_groups'][0]['receipts'])==(2 if scenario=='chain' else 1)
    assert not first['write_activation_allowed'] and not first['recoverable']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==book


@pytest.mark.parametrize('damage',['missing_final','missing_marker','missing_staged','missing_authorization',
    'fence_scope','marker_scope','missing_model','model_chapter','provider_uncertain'])
def test_incomplete_or_cross_bound_stages_are_not_repaired(archive_case,damage,monkeypatch):
    c=archive_case
    store=append_stage(c,kind='model' if damage in {'missing_model','model_chapter','provider_uncertain'} else 'deterministic',
        missing_model=damage=='missing_model',chapter_mismatch=damage=='model_chapter',
        status='provider_call_uncertain' if damage=='provider_uncertain' else 'succeeded')
    stages=store.root/'stage_receipts';missing=None
    if damage=='missing_final':missing=next(stages.rglob('0001-*.json'))
    elif damage=='missing_marker':missing=next(stages.rglob('commit_markers/*.json'))
    elif damage=='missing_staged':missing=next(stages.rglob('staged/*.json'))
    elif damage=='missing_authorization':missing=next(stages.rglob('authorizations/*.json'))
    elif damage=='fence_scope':
        path=next(stages.rglob('fences/*.json'));value=json.loads(path.read_bytes());value['book_id']='another-book'
        value['fence_hash']=canonical_hash(value,exclude_fields=('fence_hash',));path.write_text(json.dumps(value))
    elif damage=='marker_scope':
        path=next(stages.rglob('commit_markers/*.json'));old=json.loads(path.read_bytes());metadata={**old['metadata'],'sequence':9}
        value=build_marker_envelope(transaction_id=old['transaction_id'],intent_hash=old['intent_hash'],
            evidence_kind=old['evidence_kind'],evidence_hash=old['evidence_hash'],metadata=metadata)
        path.write_text(json.dumps(value))
    if missing is not None:missing.unlink()
    package=Path(capture(c)['path']);book=inventory_tree(c['root']);forbid_recovery(monkeypatch);forbid_runtime(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'] and result['blockers']
    assert inventory_tree(c['root'])==book
    if missing is not None:assert not missing.exists()
