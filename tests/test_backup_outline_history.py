"""Actual outline publication histories stay verifiable without live stores."""
from pathlib import Path
import json

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_backup_autonomy_settlement import seed,forbid_recovery
from tests.test_backup_archive_usage import forbid_runtime
from core.autonomy.outline import OutlineCheckpointStore,build_outline_checkpoint
from core.autonomy.receipts import StageReceiptStore
from core.autonomy.common import canonical_hash
from core.stage_control import build_stage_authorization,build_stage_receipt
from workbench.backup_auxiliary_history import inspect_archived_auxiliary_history
from workbench.backup_files import inventory_tree


NOW='2026-07-14T00:00:00+00:00'


def seed_outline(c,*,replaced=False,custom=False,linked=False,session=False,mismatched_stage=False):
    root=c['root']/('custom-autonomy' if custom else '.novelagent/runtime/autonomy')
    sid='outline-session';pid='outline-plan';aid='outline-arc';chapter=2;target='3'*64
    if session:
        store,started=seed(c,custom=custom)
        sid=started['session_id'];pid=started['plan_id'];aid=started['arc_plan_id'];chapter=11
        target=store.arc_plans.load(aid)['targets'][0]['target_hash']
    store=OutlineCheckpointStore(root)
    def value(epoch):
        return build_outline_checkpoint(book_id=c['project'].book_id,session_id=sid,plan_id=pid,arc_plan_id=aid,
            chapter_index=chapter,planned_target_hash=target,source_snapshot_hash=str(epoch)*64,authority_epoch=epoch,
            authority_head_event_hash='2'*64,outline_input_digest=str(epoch+2)*64,provider_profile='synthetic',
            execution_kind='deterministic',outline_text=f'# Synthetic outline {epoch}\n',
            canonical_relative_path=f'outlines/chapter-{chapter}.md',canonical_before_sha256=None,created_at=NOW)
    first=store.create(value(1),invalidated_at=NOW)
    if linked:
        stages=StageReceiptStore(root)
        lease=stages.leases.acquire(book_id=c['project'].book_id,session_id=sid,plan_id=pid,at=NOW)
        auth=build_stage_authorization(stage='outline',book_id=c['project'].book_id,session_id=sid,plan_id=pid,
            chapter_index=chapter,authority_epoch=1,authority_head_event_hash='2'*64,input_digest=first['outline_input_digest'],
            previous_stage_receipt_hash=None,provider_profile='synthetic',max_output_tokens=100,
            execution_kind='deterministic',issued_at=NOW)
        receipt=build_stage_receipt(auth,status='succeeded',output_digest='f'*64 if mismatched_stage else first['outline_hash'],
            model_call_receipt_hash=None,execution_evidence_hash='e'*64,created_at=NOW)
        stages.append(receipt,authorization=auth,expected_lease_hash=lease['lease_hash'],at=NOW)
        stages.leases.release(book_id=c['project'].book_id,session_id=sid,plan_id=pid,
            expected_lease_hash=lease['lease_hash'],at=NOW)
    last=store.create(value(2),invalidated_at=NOW) if replaced else first
    return root,first,last


def inspect(c,package):
    return inspect_archived_auxiliary_history(package,project_id=c['project'].id,book_id=c['project'].book_id)


@pytest.mark.parametrize('layout',['single','replaced','custom','linked_older_stage','session','legacy_markerless'])
def test_real_outline_revisions_markers_invalidations_and_head_verify_offline(archive_case,layout,monkeypatch):
    c=archive_case
    root,first,last=seed_outline(c,replaced=layout in {'replaced','linked_older_stage'},
        custom=layout=='custom',linked=layout=='linked_older_stage',session=layout=='session')
    if layout=='legacy_markerless':
        # This explicit old-format fixture has no marker protocol declaration;
        # current marker-backed records still require all original artifacts.
        folder=root/'outline_checkpoints'/first['session_id']/f"chapter-{first['chapter_index']:06d}"
        first.pop('recovery_protocol');first['checkpoint_hash']=canonical_hash(first,exclude_fields=('checkpoint_hash',));last=first
        (folder/'revisions'/f"{first['checkpoint_id']}.json").write_text(json.dumps(first))
        (folder/'latest.json').write_text(json.dumps({'schema_version':'1.0',
            'checkpoint_id':first['checkpoint_id'],'checkpoint_hash':first['checkpoint_hash']}))
        for namespace in ('staged','commit_markers'):
            (folder/namespace/f"{first['checkpoint_id']}.json").unlink();(folder/namespace).rmdir()
    package=Path(capture(c)['path']);book=inventory_tree(c['root']);archive=inventory_tree(package)
    forbid_recovery(monkeypatch);forbid_runtime(monkeypatch)
    def forbidden(*args,**kwargs):pytest.fail('offline outline verification opened a live store')
    monkeypatch.setattr(OutlineCheckpointStore,'__init__',forbidden)
    with absent_source(c['root']):result=inspect(c,package);again=inspect(c,package)
    assert result==again and result['complete_history_certified'],result['blockers']
    group=next(item for item in result['histories'] if item['kind']=='autonomy')['outline_groups'][0]
    assert group['head']==last['checkpoint_hash']
    assert group['checkpoints']==([first['checkpoint_hash']] if first==last else [first['checkpoint_hash'],last['checkpoint_hash']])
    assert inventory_tree(c['root'])==book and inventory_tree(package)==archive


@pytest.mark.parametrize('damage',['missing_head','stale_head','missing_marker','missing_staged','missing_revision',
    'missing_invalidation','changed_invalidation','wrong_book','wrong_arc_target','unknown_record','stage_output'])
def test_incomplete_outline_history_refuses_without_reconstruction(archive_case,damage,monkeypatch):
    c=archive_case;root,first,last=seed_outline(c,replaced=True,session=damage!='stage_output',
        linked=damage=='stage_output',mismatched_stage=damage=='stage_output')
    folder=root/'outline_checkpoints'/last['session_id']/f"chapter-{last['chapter_index']:06d}"
    missing=None
    if damage=='missing_head':missing=folder/'latest.json'
    elif damage=='missing_marker':missing=folder/'commit_markers'/f"{last['checkpoint_id']}.json"
    elif damage=='missing_staged':missing=folder/'staged'/f"{last['checkpoint_id']}.json"
    elif damage=='missing_revision':missing=folder/'revisions'/f"{first['checkpoint_id']}.json"
    elif damage=='missing_invalidation':missing=next((folder/'invalidations').iterdir())
    elif damage=='stale_head':
        (folder/'latest.json').write_text(json.dumps({'schema_version':'1.0','checkpoint_id':first['checkpoint_id'],
            'checkpoint_hash':first['checkpoint_hash']}))
    elif damage=='changed_invalidation':
        name=next((folder/'invalidations').iterdir());value=json.loads(name.read_bytes());value['recorded_at']='2026-07-14T00:01:00+00:00'
        value['invalidation_hash']=canonical_hash(value,exclude_fields=('invalidation_hash',));name.unlink()
        (folder/'invalidations'/f"{value['invalidation_hash']}.json").write_text(json.dumps(value))
    elif damage in {'wrong_book','wrong_arc_target'}:
        name=folder/'revisions'/f"{last['checkpoint_id']}.json";value=json.loads(name.read_bytes())
        value['book_id' if damage=='wrong_book' else 'planned_target_hash']='other-book' if damage=='wrong_book' else 'f'*64
        value['checkpoint_hash']=canonical_hash(value,exclude_fields=('checkpoint_hash',));name.write_text(json.dumps(value))
    elif damage=='unknown_record':(folder/'unknown-control.json').write_text('{}')
    if missing is not None:missing.unlink()
    package=Path(capture(c)['path']);book=inventory_tree(c['root']);forbid_recovery(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'] and result['blockers']
    assert inventory_tree(c['root'])==book
    if missing is not None:assert not missing.exists()
