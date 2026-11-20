"""Whole delivery attempts compete with a real held backup in other processes."""
from contextlib import closing
import json
import subprocess
import sys
import time

import pytest

from core.delivery import DeliveryQueue, FileDeliveryAdapter, SafeFileDeliveryAdapter
from core.engine.persistence import PersistenceLockError
from core.engine.safe_paths import RootBinding
from tests.test_backup_external_writers import child
from tests.test_backup_scope import legacy
from workbench.backup_files import inventory_tree
from workbench.backup_source import locked_backup_source
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


def enqueue(c, queue, *, job_id='delivery-1', root_uuid=None):
    ref={'root_id':'story_project','relative_path':'exports/chapter.txt'}
    if root_uuid is not None:ref['root_uuid']=root_uuid
    return queue.enqueue(job_id=job_id,book_id=c['project'].book_id,run_id='run-1',
        publication_receipt_hash='a'*64,target_type='file',target={'path_ref':ref},
        payload={'content':'synthetic chapter','encoding':'utf-8'})


COMMAND = '''import socket,sys
from pathlib import Path
from unittest.mock import patch
from core.delivery import DeliveryQueue,FileDeliveryAdapter
from core.engine.persistence import PersistenceLockError
root,queue_root,command,book_id=sys.argv[1:]
queue=DeliveryQueue(queue_root)
try:
 with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
  if command=='enqueue':
   queue.enqueue(job_id='new-job',book_id=book_id,run_id='run-1',publication_receipt_hash='a'*64,
    target_type='none',target={},payload={})
  elif command=='cli':
   import main
   sys.argv=['main.py','--story-project',root,'--reconcile-deliveries','--output-json']
   main.main()
  else:queue.attempt('delivery-1',worker_id='test',adapter=FileDeliveryAdapter(root_map={'story_project':root}))
except PersistenceLockError:
 print('BACKUP_LOCK_BLOCKED',flush=True);sys.exit(17)
'''


@pytest.mark.parametrize('command', ['enqueue','attempt','cli','custom'])
def test_queue_mutations_block_before_directories_claims_or_adapter_calls(legacy,command):
    c=legacy;root=c['root'];base=root.parent.parent
    queue=DeliveryQueue(root/('custom/queue' if command=='custom' else '.novelagent/runtime/deliveries'))
    if command!='enqueue':enqueue(c,queue)
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            before=scope['inventories']['story_project']
            result=child(COMMAND,root,queue.root,command,c['project'].book_id)
            assert result.returncode==17 and b'BACKUP_LOCK_BLOCKED' in result.stdout,result.stderr
            assert inventory_tree(root,locked_readers=readers)==before
        gate.finish(op)
        result=child(COMMAND,root,queue.root,command,c['project'].book_id)
        assert result.returncode==0,result.stderr
    if command=='enqueue':assert queue.load('new-job')['state']=='not_required'
    else:
        assert queue.load('delivery-1')['state']=='succeeded'
        assert (root/'exports/chapter.txt').read_text()=='synthetic chapter'


@pytest.mark.parametrize('kind',['legacy','uuid'])
def test_file_adapter_from_external_queue_cannot_write_into_book_snapshot(legacy,kind):
    c=legacy;root=c['root'];base=root.parent.parent;queue=DeliveryQueue(base/'external-queue')
    root_uuid='11111111-1111-4111-8111-111111111111'
    job=enqueue(c,queue,root_uuid=root_uuid if kind=='uuid' else None)
    code='''import json,sys
from core.delivery import DeliveryQueue,FileDeliveryAdapter,SafeFileDeliveryAdapter
from core.engine.persistence import PersistenceLockError
from core.engine.safe_paths import RootBinding
from pathlib import Path
root,queue,kind,uuid=sys.argv[1:]
adapter=(SafeFileDeliveryAdapter(binding=RootBinding('story_project',uuid,Path(root)))
 if kind=='uuid' else FileDeliveryAdapter(root_map={'story_project':root}))
try:adapter.deliver(DeliveryQueue(queue).load('delivery-1'),None)
except PersistenceLockError:sys.exit(17)
'''
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            result=child(code,root,queue.root,kind,root_uuid)
            assert result.returncode==17,result.stderr
            assert inventory_tree(root,locked_readers=readers)==scope['inventories']['story_project']
        gate.finish(op)
    adapter=(SafeFileDeliveryAdapter(binding=RootBinding('story_project',root_uuid,root))
        if kind=='uuid' else FileDeliveryAdapter(root_map={'story_project':root}))
    assert adapter.deliver(job,None)['state']=='succeeded'


@pytest.mark.parametrize('boundary',['adapter','before_attempt_receipt','after_attempt_receipt'])
def test_active_attempt_keeps_backup_out_through_receipt_and_finalization(legacy,boundary):
    c=legacy;root=c['root'];base=root.parent.parent
    queue=DeliveryQueue(root/'.novelagent/runtime/deliveries');enqueue(c,queue)
    ready=base/'delivery-ready';release=base/'delivery-release'
    code='''import sys,time
from pathlib import Path
from core.delivery import DeliveryQueue,delivery_outcome
root,ready,release=map(Path,sys.argv[1:4]);boundary=sys.argv[4]
def pause():
 ready.write_text('paused')
 deadline=time.monotonic()+20
 while not release.exists():
  if time.monotonic()>deadline:raise RuntimeError('test release timeout')
  time.sleep(.02)
def fault(event,*args):
 if event==boundary:pause()
class Adapter:
 def deliver(self,job,context):
  context.mark_remote_mutation_started()
  if boundary=='adapter':pause()
  return delivery_outcome('succeeded',code='fake',message='No remote call')
queue=DeliveryQueue(root,fault_injector=fault)
assert queue.attempt('delivery-1',worker_id='fake',adapter=Adapter())['state']=='succeeded'
'''
    process=subprocess.Popen([sys.executable,'-B','-c',code,*map(str,(queue.root,ready,release)),boundary],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert ready.exists(),'delivery did not reach its checkpoint'
        before=inventory_tree(root)
        with closing(InstanceLease(base).acquire()) as lease:
            gate=maintenance_for(lease);op=gate.begin()['operation_id']
            with pytest.raises(PersistenceLockError):
                with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                        task_runtime_root=c['runtime'],machine_store=base/'machine'):
                    pytest.fail('backup entered an active delivery attempt')
            assert gate.status()['state']=='quiescent'
            assert inventory_tree(root)==before
            gate.finish(op)
    finally:
        release.write_text('continue')
        try:stdout,stderr=process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate(timeout=5);raise
    assert process.returncode==0,stderr.decode('utf-8',errors='replace')
    job=queue.load('delivery-1')
    assert job['state']=='succeeded' and job['attempt_count']==1 and job['lease'] is None
    assert len(list(queue.attempts_dir.rglob('*.json')))==1
