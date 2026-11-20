"""Inventory-bound settlement inspection never reconciles or resends work."""
from contextlib import closing
import json

import pytest

from core.delivery import DeliveryQueue, FileDeliveryAdapter
from tests.test_backup_delivery_writers import enqueue
from tests.test_backup_external_writers import child
from tests.test_backup_scope import legacy, inspect, codes
from workbench.backup_auxiliary import inspect_auxiliary_settlement
from workbench.backup_files import inventory_tree
from workbench.backup_source import locked_backup_source
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for
from tests.test_backup_archive import archive_case,software_tree,capture
from workbench.backup_archive import verify_archive


def queue_case(c, *, custom=False, delivered=True):
    root=c['root'];queue=DeliveryQueue(root/('custom-queue' if custom else '.novelagent/runtime/deliveries'))
    enqueue(c,queue)
    if delivered:queue.attempt('delivery-1',worker_id='test',adapter=FileDeliveryAdapter(root_map={'story_project':root}))
    return queue


def check(c, **kwargs):
    before=inventory_tree(c['root'])
    result=inspect_auxiliary_settlement(c['root'],before,c['project'].book_id,**kwargs)
    assert inventory_tree(c['root'])==before
    return result


def patch_json(path, change):
    value=json.loads(path.read_bytes());change(value);path.write_text(json.dumps(value))


@pytest.mark.parametrize('state',['pending','succeeded','custom'])
def test_settled_delivery_control_evidence_is_read_only_and_keeps_pending_work(legacy,state):
    c=legacy;queue=queue_case(c,custom=state=='custom',delivered=state!='pending')
    result=check(c)
    assert result['state']=='settled' and not result['blockers']
    assert result['stores'][0]['jobs']==1
    assert not result['complete_history_certified']
    assert queue.load('delivery-1')['state']==('pending' if state=='pending' else 'succeeded')


@pytest.mark.parametrize('damage',['missing_receipt','corrupt_receipt','wrong_latest_hash','counter',
    'orphan_receipt','job_book','job_filename','receipt_scope','receipt_state','empty_success','changed_after_inventory'])
def test_delivery_damage_is_never_treated_as_settled_or_repaired(legacy,damage):
    c=legacy;queue=queue_case(c);job_path=queue.jobs_dir/'delivery-1.json'
    receipt=next(queue.attempts_dir.rglob('*.json'))
    if damage=='missing_receipt':receipt.unlink()
    elif damage=='corrupt_receipt':receipt.write_text('{broken')
    elif damage=='wrong_latest_hash':patch_json(job_path,lambda x:x['last_attempt_receipt'].update(attempt_receipt_hash='b'*64))
    elif damage=='counter':patch_json(job_path,lambda x:x.update(attempt_count=10**50))
    elif damage=='orphan_receipt':job_path.unlink()
    elif damage=='job_book':
        # Scope validation fails even when each independent record is valid.
        from core.delivery import delivery_operation_id
        def edit(x):
            x['book_id']='other-book'
            x['operation_id']=delivery_operation_id(book_id=x['book_id'],run_id=x['run_id'],job_id=x['job_id'],payload_hash=x['payload_hash'])
        patch_json(job_path,edit)
    elif damage=='job_filename':job_path.rename(queue.jobs_dir/'other-job.json')
    elif damage=='receipt_scope':
        from core.memory_v2.canonical import canonical_json_hash
        def edit(x):
            x['publication_receipt_hash']='b'*64
            x['attempt_receipt_hash']=canonical_json_hash(x,exclude_fields=('attempt_receipt_hash',),exclude_environment_fields=False)
        patch_json(receipt,edit)
    elif damage=='receipt_state':patch_json(job_path,lambda x:x.update(state='permanent_failed'))
    elif damage=='empty_success':
        receipt.unlink();patch_json(job_path,lambda x:x.update(attempt_count=0,last_attempt_receipt=None))
    else:
        before=inventory_tree(c['root'])
        patch_json(job_path,lambda x:x.update(state='permanent_failed'))
        result=inspect_auxiliary_settlement(c['root'],before,c['project'].book_id)
        assert result['blockers'];return
    result=check(c)
    assert result['state']=='unverified' and result['blockers']


def test_delivery_evidence_read_limit_fails_closed(legacy):
    c=legacy;queue_case(c)
    assert check(c,max_files=1)['blockers']


def test_malformed_auxiliary_documents_consume_the_read_budget(legacy):
    c=legacy;queue=queue_case(c,delivered=False)
    (queue.jobs_dir/'delivery-1.json').write_text('{broken')
    custom=DeliveryQueue(c['root']/'custom-queue');enqueue(c,custom)
    (custom.jobs_dir/'delivery-1.json').write_text('{broken')
    result=check(c,max_files=1)
    assert result['files_read']==1 and len(result['blockers'])==2


@pytest.mark.parametrize('kind',['delivery','autonomy'])
def test_unsettled_auxiliary_state_is_preserved_in_a_diagnostic_archive(archive_case,kind):
    c=archive_case
    if kind=='delivery':
        queue=queue_case(c,delivered=False)
        queue._claim('delivery-1','stopped-worker')
        target=queue.jobs_dir/'delivery-1.json'
        expected='delivery_not_settled'
    else:
        from core.autonomy.lease import BookLeaseStore
        store=BookLeaseStore(c['root']/'.novelagent/runtime/autonomy')
        store.acquire(book_id=c['project'].book_id,session_id='stopped-session',plan_id='plan-1',at='2026-07-14T00:00:00+00:00')
        target=store._current_path(c['project'].book_id)
        expected='autonomy_active_lease'
    before=target.read_bytes()
    result=capture(c)
    verified=verify_archive(result['path'])
    assert verified['archive_verified'] and not verified['recoverable']
    scope=verified['manifest']['preview']['scope']
    assert expected in codes(scope) and not scope['scope_checks_passed']
    assert target.read_bytes()==before


@pytest.mark.parametrize('boundary',['remote_mutation_started','after_attempt_receipt'])
def test_process_death_preserves_unsettled_delivery_during_actual_backup_preflight(legacy,boundary):
    c=legacy;root=c['root'];base=root.parent.parent;queue=queue_case(c,delivered=False)
    code='''import os,sys
from core.delivery import DeliveryQueue,delivery_outcome
def fault(event,*args):
 if event==sys.argv[2]:os._exit(73)
class Adapter:
 def deliver(self,job,context):
  context.mark_remote_mutation_started()
  return delivery_outcome('succeeded',code='synthetic',message='No remote call')
DeliveryQueue(sys.argv[1],fault_injector=fault).attempt('delivery-1',worker_id='crash',adapter=Adapter())
'''
    result=child(code,queue.root,boundary)
    assert result.returncode==73,result.stderr
    job_before=(queue.jobs_dir/'delivery-1.json').read_bytes()
    assert queue.load('delivery-1')['state']=='delivering'
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            assert not scope['scope_checks_passed']
            assert 'delivery_not_settled' in codes(scope)
            assert inventory_tree(root,locked_readers=readers)==scope['inventories']['story_project']
        gate.finish(op)
    assert (queue.jobs_dir/'delivery-1.json').read_bytes()==job_before
    assert len(list(queue.attempts_dir.rglob('*.json')))==(1 if boundary=='after_attempt_receipt' else 0)
