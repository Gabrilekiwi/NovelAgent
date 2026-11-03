"""Durable admission barrier for a single OS-owned workbench instance.

Request maintenance before acquiring book/Provider/database snapshot locks.
Already admitted operations drain normally; new writes are rejected. The caller
must hold each admission until its actual work (including a child/thread) ends.
An interrupted request survives restart and requires explicit local resumption.
This barrier alone does not certify a backup or cover an unregistered writer.
"""
from contextlib import AbstractContextManager,contextmanager
import asyncio
from datetime import datetime, timezone
import json
import os
import re
import threading
import uuid
import weakref

from core.engine.persistence import atomic_create_json, atomic_write_json
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest, read_project_bytes


class MaintenanceError(RuntimeError):
    code='workbench_maintenance_invalid'


class MaintenanceBlocked(MaintenanceError):
    code='workbench_maintenance_active'


class DeferredStartup:
    """Run local startup recovery once, after write admission becomes possible."""
    def __init__(self,operation):
        self.operation=operation
        self.mutex=threading.RLock()
        self.complete=False

    def run(self):
        with self.mutex:
            if not self.complete:
                self.operation()
                self.complete=True


_guard=threading.RLock()
_coordinators=weakref.WeakValueDictionary()
_FIELDS={'version','operation_id','purpose','phase','holder_instance_id','generation',
         'created_at','updated_at','record_sha256'}


def maintenance_for(instance_lease):
    """All request/worker paths sharing one lease must share their admission set."""
    with _guard:
        coordinator=_coordinators.get(instance_lease)
        if coordinator is None:
            coordinator=_MaintenanceCoordinator(instance_lease)
            _coordinators[instance_lease]=coordinator
        return coordinator


class _Admission(AbstractContextManager):
    def __init__(self, coordinator, token):
        self.coordinator,self.token=coordinator,token

    def close(self):
        with self.coordinator.mutex:
            if self.token is not None:
                self.coordinator.active.pop(self.token)
                self.token=None

    def __enter__(self):
        if self.token is None:raise MaintenanceError('已结束的写操作不能复用。')
        self.coordinator.lease.require_held()
        return self

    def __exit__(self,*unused):
        self.close()


class _SnapshotAdmission(_Admission):
    def __init__(self,coordinator,token,operation_id):
        super().__init__(coordinator,token)
        self.operation_id=operation_id

    def require(self):
        with self.coordinator.mutex:
            if self.token is None or self.coordinator.active.get(self.token)!='snapshot':
                raise MaintenanceError('快照许可已结束。')
            self.coordinator._require_operation(self.operation_id)


class _MaintenanceCoordinator:
    def __init__(self,lease):
        lease.require_held()
        self.lease=lease
        self.root=assert_safe_local_tree(lease.directory/'maintenance')
        self.mutex=threading.RLock()
        self.active={}
        self.pending_tasks=set()
        self.seen=False

    def _read(self):
        self.lease.require_held()
        assert_safe_local_tree(self.root)
        if not os.path.lexists(self.root/'control.json'):
            if self.seen:raise MaintenanceError('维护记录丢失，不能推断已退出维护。')
            return None
        self.seen=True
        try:value=json.loads(read_project_bytes(self.root,'control.json'))
        except (OSError,ValueError) as exc:raise MaintenanceError('维护记录不可读。') from exc
        if (not isinstance(value,dict) or set(value)!=_FIELDS
                or value['version']!='workbench_maintenance_v1'
                or value['purpose'] not in {'backup','restore'}
                or value['phase'] not in {'requested','released'}
                or type(value['generation']) is not int or value['generation']<1
                or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{32}',value[k]) is None
                       for k in ('operation_id','holder_instance_id'))
                or any(not isinstance(value[k],str) or not value[k] for k in ('created_at','updated_at'))
                or value['record_sha256']!=digest({k:v for k,v in value.items() if k!='record_sha256'})):
            raise MaintenanceError('维护记录格式或摘要不一致。')
        return value

    def _write(self,value,*,create=False):
        self.lease.require_held()
        assert_safe_local_tree(self.root)
        record={k:v for k,v in value.items() if k!='record_sha256'}
        record['record_sha256']=digest(record)
        (atomic_create_json if create else atomic_write_json)(self.root/'control.json',record)
        self.seen=True

    def _status(self,record):
        state='idle'
        if record and record['phase']=='requested':
            state=('interrupted' if record['holder_instance_id']!=self.lease.instance_id
                   else 'capturing' if 'snapshot' in self.active.values()
                   else 'draining' if self.active else 'quiescent')
        return dict(state=state,operation_id=record['operation_id'] if record else None,
            purpose=record['purpose'] if record else None,active_writes=len(self.active),
            active_kinds=sorted(self.active.values()),generation=record['generation'] if record else None)

    def status(self):
        with self.mutex:return self._status(self._read())

    def admit(self,kind):
        if kind not in {'http_write','worker','local_writer'}:
            raise MaintenanceError('未知写操作类型。')
        with self.mutex:
            record=self._read()
            if record and record['phase']=='requested':
                raise MaintenanceBlocked('工作台正在维护，暂停新任务和编辑；已开始的操作仍需完成。')
            token=uuid.uuid4().hex
            self.active[token]=kind
            return _Admission(self,token)

    async def run_async(self,kind,operation):
        """A disconnected HTTP caller must not release a live to_thread write."""
        permit=self.admit(kind)
        return await self._run_owned_async(permit,operation)

    async def run_capture_async(self,operation_id,operation):
        """Own a snapshot before dispatch, through the actual worker completion."""
        permit=self.capture(operation_id)
        async def work():return await asyncio.to_thread(operation,permit)
        return await self._run_owned_async(permit,work)

    async def _run_owned_async(self,permit,operation):
        async def work():
            try:return await operation()
            finally:permit.close()
        task=asyncio.create_task(work())
        self.pending_tasks.add(task)
        def completed(value):
            self.pending_tasks.discard(value)
            # Retrieve a detached failure without suppressing errors for a
            # caller still awaiting this task.
            if not value.cancelled():value.exception()
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    @contextmanager
    def borrow_capture(self,permit,operation_id):
        """The source may borrow a live API-owned capture without releasing it."""
        if (not isinstance(permit,_SnapshotAdmission) or permit.coordinator is not self
                or permit.operation_id!=operation_id):
            raise MaintenanceError('快照许可不属于本次维护。')
        permit.require()
        try:yield permit
        finally:permit.require()

    async def drain_async(self):
        """Graceful shutdown keeps OS ownership until detached writes finish."""
        while self.pending_tasks:
            await asyncio.shield(asyncio.gather(*tuple(self.pending_tasks),return_exceptions=True))

    def require_no_active(self):
        with self.mutex:
            self.lease.require_held()
            if self.active:raise MaintenanceBlocked('写操作尚未结束，必须保留实例所有权。')

    def begin(self,*,purpose='backup'):
        if purpose not in {'backup','restore'}:raise MaintenanceError('未知维护用途。')
        with self.mutex:
            previous=self._read()
            if previous and previous['phase']=='requested':
                raise MaintenanceBlocked('已有维护操作待完成，请先核对原操作。')
            now=datetime.now(timezone.utc).isoformat()
            value=dict(version='workbench_maintenance_v1',operation_id=uuid.uuid4().hex,
                purpose=purpose,phase='requested',holder_instance_id=self.lease.instance_id,
                generation=1,created_at=now,updated_at=now)
            self._write(value,create=previous is None)
            return self._status(value)

    def _require_operation(self,operation_id,*,current=True):
        record=self._read()
        if (not record or record['phase']!='requested' or record['operation_id']!=operation_id
                or current and record['holder_instance_id']!=self.lease.instance_id):
            raise MaintenanceError('维护操作或当前实例不匹配。')
        return record

    def require_quiescent(self,operation_id):
        with self.mutex:
            record=self._require_operation(operation_id)
            if self.active:raise MaintenanceBlocked('已有写操作尚未结束，不能取得一致快照。')
            return self._status(record)

    def capture(self,operation_id):
        """Atomically enter capture; finish/resume cannot release maintenance."""
        with self.mutex:
            self.require_quiescent(operation_id)
            token=uuid.uuid4().hex
            self.active[token]='snapshot'
            return _SnapshotAdmission(self,token,operation_id)

    def resume(self,operation_id):
        """Explicitly adopt interrupted local maintenance after OS ownership."""
        with self.mutex:
            record=self._require_operation(operation_id,current=False)
            if self.active:raise MaintenanceBlocked('当前仍有写操作，不能接管维护。')
            if record['holder_instance_id']!=self.lease.instance_id:
                record.update(holder_instance_id=self.lease.instance_id,generation=record['generation']+1,
                    updated_at=datetime.now(timezone.utc).isoformat())
                self._write(record)
            return self._status(record)

    def finish(self,operation_id):
        with self.mutex:
            record=self._require_operation(operation_id)
            if self.active:raise MaintenanceBlocked('已有写操作尚未结束，不能退出维护。')
            record.update(phase='released',updated_at=datetime.now(timezone.utc).isoformat())
            self._write(record)
            return self._status(record)
