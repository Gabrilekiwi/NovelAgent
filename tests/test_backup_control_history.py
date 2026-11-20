"""Complete retained controls are validated without opening live stores."""
from pathlib import Path
from datetime import datetime,timedelta,timezone
import json

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_backup_auxiliary_settlement import queue_case,patch_json
from tests.test_backup_autonomy_settlement import seed,forbid_recovery
from core.delivery import DeliveryQueue,FileDeliveryAdapter,delivery_outcome
from core.autonomy.common import canonical_hash
from workbench.backup_auxiliary_history import inspect_archived_auxiliary_history
from workbench.backup_archive_auxiliary import inspect_archived_auxiliary
from workbench.backup_files import inventory_tree


def inspect(c,package):
    return inspect_archived_auxiliary_history(package,project_id=c['project'].id,book_id=c['project'].book_id)


class Outcome:
    def __init__(self,state,code='synthetic',mutate=False):self.state,self.code,self.mutate=state,code,mutate
    def deliver(self,job,context):
        if self.mutate:context.mark_remote_mutation_started()
        return delivery_outcome(self.state,code=self.code,message='isolated deterministic adapter')


def retried_queue(c):
    queue=queue_case(c,delivered=False)
    queue.attempt('delivery-1',worker_id='test',adapter=Outcome('retryable_failed'))
    queue.attempt('delivery-1',worker_id='test',adapter=FileDeliveryAdapter(root_map={'story_project':c['root']}))
    return queue


@pytest.mark.parametrize('layout',['pending','succeeded','custom','retried','none','confirmed_absent'])
def test_complete_delivery_jobs_attempts_and_manual_resolution_are_verified_offline(archive_case,layout,monkeypatch):
    c=archive_case
    if layout=='retried':queue=retried_queue(c)
    elif layout=='none':
        queue=DeliveryQueue(c['root']/'.novelagent/runtime/deliveries')
        queue.enqueue(job_id='unused',book_id=c['project'].book_id,run_id='run',publication_receipt_hash='a'*64,
            target_type='none',target={},payload={})
    elif layout=='confirmed_absent':
        now=[datetime(2026,7,14,tzinfo=timezone.utc)]
        queue=DeliveryQueue(c['root']/'.novelagent/runtime/deliveries',clock=lambda:now[0])
        queue.enqueue(job_id='notion',book_id=c['project'].book_id,run_id='run',publication_receipt_hash='a'*64,
            target_type='notion',target={'database_id':'synthetic','quarantine_seconds':1},payload={'id':'synthetic-memory'})
        queue.attempt('notion',worker_id='test',adapter=Outcome('uncertain',mutate=True))
        now[0]+=timedelta(seconds=2)
        queue.resolve_confirmed_absent('notion',worker_id='test',
            adapter=Outcome('uncertain',code='notion_absent_during_uncertain_reconcile'))
        queue.attempt('notion',worker_id='test',adapter=Outcome('succeeded',mutate=True))
    else:queue=queue_case(c,custom=layout=='custom',delivered=layout!='pending')
    package=Path(capture(c)['path']);before=inventory_tree(package);book=inventory_tree(c['root'])
    def forbidden(*args,**kwargs):pytest.fail('offline delivery verification opened or sent a live queue')
    monkeypatch.setattr(DeliveryQueue,'__init__',forbidden);monkeypatch.setattr(DeliveryQueue,'attempt',forbidden)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['complete_history_certified'],first['blockers']
    assert first['histories'][0]['kind']=='delivery'
    assert first['histories'][0]['record_hashes'] and not first['histories'][0]['automatic_delivery_allowed']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==book


@pytest.mark.parametrize('damage',['impossible_transition','counter','bad_target','unknown_file','unknown_directory','missing_attempt'])
def test_delivery_history_damage_refuses_without_repair(archive_case,damage):
    from core.memory_v2.canonical import canonical_json_hash
    c=archive_case;queue=retried_queue(c)
    receipt=sorted(queue.attempts_dir.rglob('*.json'))[0]
    if damage in {'impossible_transition','counter'}:
        value=json.loads(receipt.read_bytes())
        if damage=='impossible_transition':value['outcome']['state']='permanent_failed'
        else:value['diagnostics']['attempt_number']=99
        value['attempt_receipt_hash']=canonical_json_hash(value,exclude_fields=('attempt_receipt_hash',),exclude_environment_fields=False)
        receipt.write_text(json.dumps(value))
    elif damage=='bad_target':patch_json(queue.jobs_dir/'delivery-1.json',lambda value:value['target']['path_ref'].update(relative_path='../outside'))
    elif damage=='unknown_file':(queue.root/'lost-control.json').write_text('{}')
    elif damage=='unknown_directory':(queue.root/'unclassified-control').mkdir()
    else:receipt.unlink()
    package=Path(capture(c)['path']);book=inventory_tree(c['root'])
    if damage in {'impossible_transition','counter'}:
        assert inspect_archived_auxiliary(package,project_id=c['project'].id,book_id=c['project'].book_id)['auxiliary_settled']
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'] and result['blockers']
    assert inventory_tree(c['root'])==book


@pytest.mark.parametrize('layout',['abandoned','cancelled','custom','revised_arc'])
def test_complete_inactive_sessions_arcs_and_indexes_are_verified_offline(archive_case,layout,monkeypatch):
    c=archive_case;store,started=seed(c,custom=layout=='custom',state='cancelled' if layout=='cancelled' else 'abandoned')
    if layout=='revised_arc':
        genesis=json.loads((store.root/'sessions'/started['session_id']/'genesis.json').read_bytes())
        arc=store.arc_plans.load(genesis['arc_plan_id']);planned=dict(arc['targets'][0]['planned']);planned['mainline']='revised synthetic goal'
        store.arc_plans.adjust_uncommitted(genesis['arc_plan_id'],chapter_index=11,planned=planned,
            reason='author planning revision',expected_arc_plan_hash=arc['arc_plan_hash'],committed_chapters=set())
    package=Path(capture(c)['path']);before=inventory_tree(package);book=inventory_tree(c['root'])
    forbid_recovery(monkeypatch)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['complete_history_certified'],first['blockers']
    history=first['histories'][0]
    assert history['kind']=='autonomy' and history['sessions']==1 and history['operations']==2
    assert history['lease_heads']==1 and list(history['arcs'].values())==[2 if layout=='revised_arc' else 1]
    assert not history['automatic_resume_allowed']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==book


@pytest.mark.parametrize('damage',['missing_arc_head','wrong_index','arc_book','revision_gap','unknown_stage','unknown_file'])
def test_autonomy_history_missing_or_unclassified_evidence_is_not_repaired(archive_case,damage,monkeypatch):
    c=archive_case;store,started=seed(c)
    head=next((store.root/'arc_plans').rglob('head.json'))
    if damage=='missing_arc_head':head.unlink()
    elif damage=='wrong_index':patch_json(next((store.root/'plan_sessions').iterdir()),lambda value:value.update(genesis_hash='b'*64))
    elif damage=='arc_book':
        revision=next((head.parent/'revisions').iterdir());value=json.loads(revision.read_bytes());value['book_id']='another-book'
        value['arc_plan_hash']=canonical_hash(value,exclude_fields=('arc_plan_hash',));revision.write_text(json.dumps(value))
        patch_json(head,lambda current:current.update(arc_plan_hash=value['arc_plan_hash']))
    elif damage=='revision_gap':next((head.parent/'revisions').iterdir()).rename(head.parent/'revisions/000002.json')
    elif damage=='unknown_stage':
        folder=store.root/'stage_receipts/unclassified';folder.mkdir(parents=True);(folder/'receipt.json').write_text('{}')
    else:(store.root/'unclassified-control.json').write_text('{}')
    package=Path(capture(c)['path']);book=inventory_tree(c['root']);forbid_recovery(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'] and result['blockers']
    assert inventory_tree(c['root'])==book
    if damage=='missing_arc_head':assert not head.exists()
