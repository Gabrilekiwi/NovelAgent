import json
import threading

import pytest

from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for, MaintenanceBlocked, MaintenanceError


@pytest.fixture
def owned(tmp_path):
    lease=InstanceLease(tmp_path/'data').acquire()
    try:yield lease,maintenance_for(lease)
    finally:lease.close()


def test_request_blocks_new_writes_and_drains_existing_before_snapshot(owned):
    lease,gate=owned
    assert maintenance_for(lease) is gate
    assert gate.status()['state']=='idle' and not gate.root.exists()
    one,two=gate.admit('http_write'),gate.admit('worker')
    state=gate.begin();op=state['operation_id']
    assert state['state']=='draining' and state['active_writes']==2
    with pytest.raises(MaintenanceBlocked):gate.admit('http_write')
    with pytest.raises(MaintenanceBlocked):gate.require_quiescent(op)
    one.close();one.close()
    with pytest.raises(MaintenanceBlocked):gate.finish(op)
    two.close()
    assert gate.require_quiescent(op)['state']=='quiescent'
    assert gate.finish(op)['state']=='idle'
    with gate.admit('worker'):assert gate.status()['active_writes']==1
    assert gate.status()['active_writes']==0


def test_interruption_survives_new_os_lease_and_never_auto_releases(owned):
    lease,gate=owned
    op=gate.begin()['operation_id']
    lease.close()
    new_lease=InstanceLease(lease.directory).acquire()
    try:
        restarted=maintenance_for(new_lease)
        before=(gate.root/'control.json').read_bytes()
        assert restarted.status()['state']=='interrupted'
        with pytest.raises(MaintenanceBlocked):restarted.admit('worker')
        with pytest.raises(MaintenanceError):restarted.finish(op)
        with pytest.raises(MaintenanceError):restarted.require_quiescent(op)
        assert (gate.root/'control.json').read_bytes()==before
        assert restarted.resume(op)['generation']==2
        assert restarted.require_quiescent(op)['state']=='quiescent'
        restarted.finish(op)
    finally:new_lease.close()


@pytest.mark.parametrize('damage',['missing','hash','invalid_shape'])
def test_lost_or_corrupt_control_cannot_be_treated_as_idle(owned,damage):
    _,gate=owned
    op=gate.begin()['operation_id'];path=gate.root/'control.json'
    if damage=='missing':path.unlink()
    elif damage=='invalid_shape':path.write_text('[]',encoding='utf-8')
    else:
        value=json.loads(path.read_text(encoding='utf-8'));value['phase']='released'
        path.write_text(json.dumps(value),encoding='utf-8')
    for operation in (gate.status,gate.begin,lambda:gate.admit('http_write'),lambda:gate.resume(op)):
        with pytest.raises(MaintenanceError):operation()


def test_wrong_operation_and_second_begin_preserve_original_request(owned):
    _,gate=owned
    op=gate.begin()['operation_id'];before=(gate.root/'control.json').read_bytes()
    with pytest.raises(MaintenanceBlocked):gate.begin()
    for action in (gate.finish,gate.resume,gate.require_quiescent):
        with pytest.raises(MaintenanceError):action('0'*32)
    assert (gate.root/'control.json').read_bytes()==before
    gate.finish(op)


def test_request_races_with_real_writer_without_cancelling_it(owned):
    _,gate=owned
    started=threading.Event();finish=threading.Event();outcomes=[]
    def writer():
        with gate.admit('local_writer'):
            started.set()
            if not finish.wait(5):raise RuntimeError('test writer did not receive completion')
            outcomes.append('saved')
    thread=threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(5)
        state=gate.begin();op=state['operation_id']
        assert state['state']=='draining' and outcomes==[]
        with pytest.raises(MaintenanceBlocked):gate.admit('worker')
    finally:
        finish.set();thread.join(5)
    assert not thread.is_alive() and outcomes==['saved']
    assert gate.require_quiescent(op)['active_writes']==0


def test_exception_releases_only_its_admission_and_closed_handle_cannot_reenter(owned):
    _,gate=owned
    permit=gate.admit('http_write')
    with pytest.raises(ValueError):
        with permit:raise ValueError('synthetic failure')
    assert gate.status()['active_writes']==0
    with pytest.raises(MaintenanceError):
        with permit:pytest.fail('closed admission reused')


def test_lost_instance_ownership_cannot_request_or_certify_maintenance(owned):
    lease,gate=owned
    op=gate.begin()['operation_id'];lease.close()
    for action in (gate.status,lambda:gate.require_quiescent(op),lambda:gate.resume(op),lambda:gate.finish(op)):
        with pytest.raises(RuntimeError):action()
