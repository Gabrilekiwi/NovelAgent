"""Pure autonomy preflight, including missing heads that ordinary loads repair."""
import copy
import json

import pytest

from core.autonomy.common import canonical_hash
from core.autonomy.plans import build_source_snapshot,compile_instruction_plan
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.session import AutonomySessionStore
from tests.test_autonomy_plans import trusted_profiles,NOW
from tests.test_backup_autonomy_writers import autonomy_book
from tests.test_backup_auxiliary_settlement import check,patch_json
from tests.test_backup_external_writers import child


def seed(c, *, custom=False, state='abandoned'):
    payload=copy.deepcopy(trusted_profiles().payload)
    payload['story_projects'][0]['book_id']=c['project'].book_id
    profiles=TrustedProfiles.from_dict(payload)
    source=build_source_snapshot(book_id=c['project'].book_id,root_uuid='root-autonomy',authority_epoch=2,
        authority_head_event_hash='2'*64,canonical_next_chapter=11,source_digest='1'*64,captured_at=NOW)
    plan=compile_instruction_plan('write 2 chapters',trusted_profiles=profiles,source_snapshot=source,created_at=NOW)
    store=AutonomySessionStore(c['root']/('custom-autonomy' if custom else '.novelagent/runtime/autonomy'),trusted_profiles=profiles)
    started=store.execute_plan(plan,source_snapshot_loader=lambda:source,at=NOW)
    if state=='abandoned':store.abandon(started['session_id'],at='2026-07-14T00:01:00+00:00')
    elif state=='cancelled':store.cancel(started['session_id'],at='2026-07-14T00:01:00+00:00')
    return store,started


def forbid_recovery(monkeypatch):
    from core.autonomy import common,lease,session
    def reject(*args,**kwargs):pytest.fail('read-only inspector entered a recovery/write API')
    monkeypatch.setattr(session.AutonomySessionStore,'__init__',reject)
    monkeypatch.setattr(lease.BookLeaseStore,'load',reject)
    monkeypatch.setattr(common,'atomic_append_json',reject)
    monkeypatch.setattr(common,'atomic_replace_json',reject)


@pytest.mark.parametrize('state',['abandoned','cancelled','custom'])
def test_settled_sessions_and_released_leases_are_verified_without_opening_runtime_stores(autonomy_book,monkeypatch,state):
    c=autonomy_book;seed(c,custom=state=='custom',state='abandoned' if state=='custom' else state)
    forbid_recovery(monkeypatch)
    result=check(c)
    assert result['state']=='settled',result['blockers']
    control=next(item for item in result['stores'] if item['kind']=='autonomy')
    assert control['sessions']==1 and control['lease_heads']==1 and control['operations']==2


@pytest.mark.parametrize('damage',['active','missing_head','stale_head','missing_latest','bad_latest',
    'missing_result','missing_marker','missing_event','orphan_operation','history_gap','large_generation'])
def test_unsettled_or_damaged_autonomy_is_rejected_without_repairing_heads(autonomy_book,monkeypatch,damage):
    c=autonomy_book;store,started=seed(c,state='active' if damage=='active' else 'abandoned')
    current=next((store.root/'leases').rglob('current.json'))
    history=sorted((current.parent/'history').glob('*.json'))
    latest=store.root/'sessions/latest.json'
    if damage=='missing_head':current.unlink()
    elif damage=='stale_head':current.write_bytes(history[0].read_bytes())
    elif damage=='missing_latest':latest.unlink()
    elif damage=='bad_latest':patch_json(latest,lambda x:x.update(genesis_hash='b'*64))
    elif damage=='missing_result':next((store.root/'operations').rglob('result.json')).unlink()
    elif damage=='missing_marker':next((store.root/'operations').rglob('commit.marker')).unlink()
    elif damage=='missing_event':sorted((store.root/'sessions'/started['session_id']/'events').glob('*.json'))[-1].unlink()
    elif damage=='orphan_operation':(store.root/'operations/orphan').mkdir()
    elif damage=='history_gap':history[0].unlink()
    elif damage=='large_generation':
        value=json.loads(history[-1].read_bytes());value['generation']=10**50
        value['lease_hash']=canonical_hash(value,exclude_fields=('lease_hash',))
        history[-1].unlink()
        target=history[-1].parent/f"{value['generation']:06d}-{value['lease_hash'][:20]}.json"
        target.write_text(json.dumps(value))
    forbid_recovery(monkeypatch)
    result=check(c)
    assert result['state']=='unverified' and result['blockers']
    if damage=='active':
        assert {'autonomy_active_lease','autonomy_active_session'}<={item['code'] for item in result['blockers']}
    if damage=='missing_head':assert not current.exists()
    if damage=='missing_latest':assert not latest.exists()


def test_process_exit_after_durable_lease_history_does_not_trigger_head_reconstruction(autonomy_book,monkeypatch):
    c=autonomy_book;root=c['root']/'.novelagent/runtime/autonomy'
    code='''import os,sys
from pathlib import Path
import core.autonomy.lease as lease
original=lease.atomic_append_json
def append(path,payload):
 original(path,payload)
 os._exit(79)
lease.atomic_append_json=append
lease.BookLeaseStore(Path(sys.argv[1])).acquire(book_id=sys.argv[2],session_id='session-1',plan_id='plan-1',at='2026-07-14T00:00:00+00:00')
'''
    result=child(code,root,c['project'].book_id)
    assert result.returncode==79,result.stderr
    assert list((root/'leases').rglob('*.json')) and not list((root/'leases').rglob('current.json'))
    forbid_recovery(monkeypatch)
    result=check(c)
    assert 'autonomy_lease_head_requires_recovery' in {item['code'] for item in result['blockers']}
    assert not list((root/'leases').rglob('current.json'))
