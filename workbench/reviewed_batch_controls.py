"""Explicit author controls and revocable process-local step permits."""
import os,uuid,threading
from contextlib import contextmanager

from core.engine.persistence import atomic_create_json,atomic_write_json,persistence_state_lock
from core.engine.provider_safety import provider_execution_lock,list_unresolved_provider_calls
from core.engine.reviewed_batch_control import (BatchControlStore,BatchControlError,BatchControlPaused,
    initial_control,validate_control,policy_step_scope)
from core.engine.safe_paths import assert_safe_local_tree
from core.chapter_execution_budget import ChapterExecutionBudget
from core.batch_budget_store import BatchBudgetRegistry
from core.story_project.reviewed_state_document import digest
from workbench.book_ownership import BookOwnership,_record,_require_witness,_file_identity
from workbench.chapter_budgets import ChapterBudgetService
from workbench.reviewed_batches import ReviewedBatchService,_selection


def control_paths(context,batch_id):
    from workbench.restored_batch_continuation import continuation_location
    selected=continuation_location(context,batch_id)
    if selected:
        _,base,token=selected
        return base/'anchor.json',context[2].directory/('brcc-'+token+'.json'),base/'control.json'
    machine=context[2].directory;runtime=context[6]
    return machine/('bc0-'+batch_id+'.json'),machine/('bc-'+batch_id+'.json'),runtime/'reviewed/batch_controls'/(batch_id+'.json')


def control_ready(grant):
    m=grant['manifest']
    old=dict(schema_version='reviewed_batch_grant_ready_v1',batch_id=m['batch_id'],
        grant_sha256=grant['grant_sha256'],ownership_sha256=m['ownership_sha256'])
    old['receipt_sha256']=digest(old)
    result={**old,'schema_version':'reviewed_batch_grant_ready_v2','previous_ready_sha256':old['receipt_sha256'],
        'control_initial_sha256':initial_control(grant)['record_sha256']}
    result['receipt_sha256']=digest({k:v for k,v in result.items() if k!='receipt_sha256'})
    return result


def validate_control_anchor(context,batch_id,grant):
    anchor,machine,book=control_paths(context,batch_id)
    initial=initial_control(grant)
    if BatchControlStore._read(anchor)!=initial:raise BatchControlError('原批次控制初始凭据不同。')
    # A torn command may be explicitly resumed by its original key/CAS. Grant
    # authentication remains readable, while every dispatch requires load()
    # to prove both copies are the same completed revision.
    for path in (machine,book):validate_control(BatchControlStore._read(path),grant)


class _RunHandle:
    def __init__(self,service,project_id,batch_id,epoch):
        self.service=service;self.project_id=project_id;self.batch_id=batch_id;self.epoch=epoch


class ReviewedBatchControlService:
    def __init__(self,publication,*,fault_injector=None):
        self.publication=publication;self.batches=ReviewedBatchService(publication)
        self.epoch=uuid.uuid4().hex;self.handles={};self.fault_injector=fault_injector
        self._driver_lock=threading.Lock()

    def _store(self,context,batch_id,grant):
        _,machine,book=control_paths(context,batch_id)
        return BatchControlStore(machine,book,grant)

    def initialize(self,*,project_id,batch_id,author_confirmed):
        if author_confirmed is not True:raise BatchControlError('请明确初始化原批次执行控制。')
        with self.publication._owned_project(project_id) as context:
            result=self.batches._require_current_start(context,batch_id);grant=result['grant']
            _,_,ready=self.batches._paths(context,batch_id)
            if self.batches._read(ready)==control_ready(grant):return self._view(context,batch_id,grant)
            anchor,machine,book=control_paths(context,batch_id)
            def before():
                if context[2].require(**context[3])!=context[4]:raise BatchControlError('批次所有权变化。')
                registry=BatchBudgetRegistry(context[6]/'budgets',book_id=grant['manifest']['book_id'])
                _,snapshots=registry.load(batch_id)
                for chapter,snapshot in snapshots.items():
                    ChapterBudgetService._require_no_chapter_work(context[5],grant['manifest']['book_id'],chapter)
                    budget=ChapterExecutionBudget(context[5],book_id=grant['manifest']['book_id'],chapter=chapter)
                    if snapshot['attempts'] or budget.time_clock.report()['interval_count'] or snapshot['report'].get('repair_cycle'):
                        raise BatchControlError('不能给已执行章节重建初始批次控制。')
            before()
            initial=initial_control(grant)
            if os.path.lexists(anchor):
                if self.batches._read(anchor)!=initial:raise BatchControlError('原批次控制初始记录不同。')
            else:atomic_create_json(anchor,initial)
            store=self._store(context,batch_id,grant)
            with persistence_state_lock((machine,book)):
                store.initialize(before_write=before,fault_injector=self.fault_injector)
                before();atomic_write_json(ready,control_ready(grant))
                if self.fault_injector is not None:self.fault_injector('after_control_ready')
            return self._view(context,batch_id,grant)

    def _view(self,context,batch_id,grant):
        _,_,ready=self.batches._paths(context,batch_id)
        if self.batches._read(ready)!=control_ready(grant):raise BatchControlError('原批次尚未明确初始化执行控制。')
        validate_control_anchor(context,batch_id,grant)
        record,state=self._store(context,batch_id,grant).load()
        live=self.handles.get(batch_id)
        live=live is not None and live.epoch==state['run_epoch'] and state['status']=='running'
        return {'control':record,'progress':state,'explicit_resume_required':state['status'] not in {'stopped','completed'} and not live,
            'execution_permit':False,'provider_calls_started':False}

    def get(self,*,project_id,batch_id):
        context=self._control_context(project_id)
        return self._view(context,batch_id,self.batches._stored(context,batch_id)['grant'])

    def command(self,*,project_id,batch_id,action,idempotency_key,expected_control_sha256,author_confirmed):
        if author_confirmed is not True or action not in {'start','resume','pause','stop'}:
            raise BatchControlError('请明确选择开始、继续、暂停或停止原批次。')
        if action in {'pause','stop'}:
            return self._signal(project_id,batch_id,action,idempotency_key,expected_control_sha256)
        with self.publication._owned_project(project_id) as context:
            grant=self.batches._require_current_execution(context,batch_id)['grant']
            validate_control_anchor(context,batch_id,grant)
            _,_,ready=self.batches._paths(context,batch_id)
            if self.batches._read(ready)!=control_ready(grant):raise BatchControlError('执行控制尚未完成初始化。')
            store=self._store(context,batch_id,grant);_,machine,book=control_paths(context,batch_id)
            def before():
                if context[2].require(**context[3])!=context[4] or self.batches._require_current_execution(context,batch_id)['grant']!=grant:
                    raise BatchControlError('开始执行前原授权或输入发生变化。')
            with persistence_state_lock((machine,book)):
                # Cold retry completes the original command bytes. It must not
                # replace that command's old epoch with this service's epoch.
                # Reconciliation itself never manufactures a new live handle.
                recorded,_=validate_control(store._read(machine),grant)
                prior=next((c for c in recorded['commands'] if c['key_sha256']==digest(idempotency_key.encode('utf-8'))),None)
                payload=prior['payload'] if prior is not None and prior['action']==action else {'run_epoch':self.epoch}
                record,state=store.apply(action=action,payload=payload,key=idempotency_key,
                    expected_sha256=expected_control_sha256,before_write=before,fault_injector=self.fault_injector)
            if state['status']!='running' or state['run_epoch']!=self.epoch:
                return self._view(context,batch_id,grant),None
            handle=self.handles.get(batch_id)
            if handle is None:
                handle=_RunHandle(self,project_id,batch_id,self.epoch);self.handles[batch_id]=handle
            return self._view(context,batch_id,grant),handle

    def _control_context(self,project_id):
        # Pause/stop only reduce authority. They use a short separate control
        # lock, so an in-flight Provider under the book lock does not delay a
        # stop until another physical retry has already begun. No grant/start
        # or new side effect can be created through this read-only admission.
        p=self.publication
        with p.database.connection() as connection:current=p._registration(project_id,connection)
        owner=BookOwnership(current['book_id'],machine_store=p.machine_store)
        record=_record(owner.directory,current['book_id'])
        _require_witness(current['root'],current['book_id'],record)
        if (record is None or record['state']!='active' or record['binding']['project_id']!=project_id
                or record['binding']['root']!=str(current['root']) or record['binding']['database']!=str(p.database.path)
                or record['binding']['database_file_identity']!=_file_identity(p.database.path)):
            raise BatchControlError('暂停/停止请求不属于原活跃作品。')
        runtime=assert_safe_local_tree(current['root']/'.novelagent/runtime')
        context=(current,None,owner,{},record,current['root'],runtime,{},runtime/'reviewed/p')
        return context

    def _signal(self,project_id,batch_id,action,key,expected):
        context=self._control_context(project_id);current=context[0];owner=context[2];record=context[4]
        grant=self.batches._stored(context,batch_id)['grant']
        validate_control_anchor(context,batch_id,grant)
        _,_,ready=self.batches._paths(context,batch_id)
        if self.batches._read(ready)!=control_ready(grant):raise BatchControlError('原执行控制未初始化。')
        _,machine,book=control_paths(context,batch_id);store=self._store(context,batch_id,grant)
        def before():
            if _record(owner.directory,current['book_id'])!=record:raise BatchControlError('停止期间原所有权改变。')
        with persistence_state_lock((machine,book)):
            _,state=store.apply(action=action,payload={},key=key,expected_sha256=expected,before_write=before,fault_injector=self.fault_injector)
        if state['status']!='running' or state['run_epoch']!=self.epoch:self.handles.pop(batch_id,None)
        return self._view(context,batch_id,grant)

    @contextmanager
    def step(self,handle,*,stage,execution_id,input_sha256,idempotency_key,expected_control_sha256,verify_inputs,local_recovery=False):
        if (not isinstance(handle,_RunHandle) or handle.service is not self or handle.epoch!=self.epoch
                or self.handles.get(handle.batch_id) is not handle or not callable(verify_inputs)):
            raise BatchControlPaused('本次服务实例没有有效的明确执行许可。')
        if type(local_recovery) is not bool:raise BatchControlError('本地恢复模式必须明确。')
        p=self.publication
        with p.database.connection() as connection:initial=p._registration(handle.project_id,connection)
        # Keep original book ownership through the stage, but release Provider
        # and SQL locks before the actual executor acquires its own boundaries.
        with p._owner(initial['book_id']).locked(wait_timeout=60) as owner:
            root=initial['root'];runtime=assert_safe_local_tree(root/'.novelagent/runtime')
            with provider_execution_lock(runtime/'runs'):
                with p.database.transaction() as connection:
                    current=p._registration(handle.project_id,connection)
                    if current!=initial:raise BatchControlError('原批次作品注册变化。')
                    args=dict(root=root,database=p.database,project_id=handle.project_id,connection=connection)
                    ownership=owner.require(**args)
                    context=(current,connection,owner,args,ownership,root,runtime,{},runtime/'reviewed/p')
                    p._require_settled_persistence(context[8])
                    grant=self.batches._require_current_execution(context,handle.batch_id)['grant']
                    store=self._store(context,handle.batch_id,grant);_,machine,book=control_paths(context,handle.batch_id)
                    def check_control():
                        _,state=store.load()
                        pending=state['pending_step']
                        if (self.handles.get(handle.batch_id) is not handle or state['status']!='running'
                                or state['run_epoch']!=self.epoch or pending is None or pending['execution_id']!=execution_id
                                or pending['stage']!=stage or pending['input_sha256']!=input_sha256
                                or _selection()!=grant['manifest']['model_selection']):
                            raise BatchControlPaused('原步骤已暂停、来源变化或不属于当前许可。')
                        machine_grant,book_grant,_=self.batches._paths(context,handle.batch_id)
                        if self.batches._read(machine_grant)!=grant or self.batches._read(book_grant)!=grant:
                            raise BatchControlError('原作者批次授权发生变化。')
                    def check():
                        check_control()
                        if verify_inputs()!=input_sha256:raise BatchControlPaused('原步骤完整输入发生变化。')
                    with persistence_state_lock((machine,book)):
                        record,state=store.load()
                        if state['status']!='running' or state['run_epoch']!=self.epoch or state['stage']!=stage:
                            raise BatchControlPaused('本次步骤与原批次进度不同。')
                        if verify_inputs()!=input_sha256:raise BatchControlError('原步骤输入摘要变化。')
                        if state['pending_step'] is None:
                            step_id='step_'+digest({'batch_id':handle.batch_id,'chapter':state['chapter'],'stage':stage})[:32]
                            record,state=store.apply(action='claim_step',payload={'run_epoch':self.epoch,'step_id':step_id,
                                'execution_id':execution_id,'input_sha256':input_sha256},key=idempotency_key,
                                expected_sha256=expected_control_sha256,before_write=lambda:None,fault_injector=self.fault_injector)
                        elif record['record_sha256']!=expected_control_sha256:
                            raise BatchControlError('续办步骤的控制版本变化。')
                        check()
                        binding={**state['pending_step'],'book_id':current['book_id'],'batch_id':handle.batch_id,
                            'grant_sha256':grant['grant_sha256'],'control_sha256':record['record_sha256']}
                        if stage.startswith('repair_'):
                            original='step_'+digest({'batch_id':handle.batch_id,'chapter':state['chapter'],'stage':'quality'})[:32]
                            repair=next((c for c in record['commands'] if c['action']=='begin_repair' and c['payload']['step_id']==original),None)
                            if repair is None:raise BatchControlError('修复步骤缺少原周期开始命令。')
                            binding['repair_claim_sha256']=repair['payload']['claim_sha256']
            budget=ChapterExecutionBudget(root,book_id=initial['book_id'],chapter=binding['chapter'])
            with budget.operation(execution_id,local_only=local_recovery,
                    allow_provider_dispatch=not local_recovery and stage in {'generation','extraction','repair_generation','repair_extraction'}) as action:
                with policy_step_scope(binding=binding,model=grant['manifest']['model_selection'],check=check) as permit:
                    permit.active_interval=action.interval
                    permit._control_check=check_control
                    @contextmanager
                    def owned_project(publication,project_id):
                        # The original machine lease is deliberately exclusive,
                        # including within this process. Reuse that exact held
                        # owner only while its opaque policy scope is active;
                        # obtain fresh Provider/SQL boundaries and revalidate.
                        permit.require_scope(**{k:binding[k] for k in ('stage','book_id','chapter','execution_id','input_sha256')})
                        if publication is not p or project_id!=handle.project_id:
                            raise BatchControlError('本地步骤不能借用其他作品或服务的所有权。')
                        with provider_execution_lock(runtime/'runs'):
                            with p.database.transaction() as current_connection:
                                current=p._registration(project_id,current_connection)
                                args=dict(root=root,database=p.database,project_id=project_id,connection=current_connection)
                                if current!=initial or owner.require(**args)!=ownership:
                                    raise BatchControlError('本地步骤的原所有权或注册发生变化。')
                                if list_unresolved_provider_calls(runtime/'runs'):
                                    raise BatchControlError('原作品仍有未知模型调用，本地阶段不能发布。')
                                p._require_settled_persistence(runtime/'reviewed/p')
                                yield (current,current_connection,owner,args,ownership,root,runtime,{'story_project':root,'runtime':runtime},runtime/'reviewed/p')
                                if owner.require(**args)!=ownership:raise BatchControlError('本地步骤结束时原所有权变化。')
                                permit.require_scope(**{k:binding[k] for k in ('stage','book_id','chapter','execution_id','input_sha256')})
                    permit._owned_project=owned_project
                    yield permit

    def finish_step(self,handle,*,expected_control_sha256,idempotency_key,result_sha256,verify_result):
        if (not isinstance(handle,_RunHandle) or self.handles.get(handle.batch_id) is not handle
                or handle.service is not self or not callable(verify_result)):
            raise BatchControlPaused('完成步骤需要本次服务的原执行许可。')
        with self.publication._owned_project(handle.project_id) as context:
            grant=self.batches._stored(context,handle.batch_id)['grant'];store=self._store(context,handle.batch_id,grant)
            _,machine,book=control_paths(context,handle.batch_id)
            with persistence_state_lock((machine,book)):
                record,state=store.load()
                old=next((c for c in record['commands'] if c['key_sha256']==digest(idempotency_key.encode('utf-8'))),None)
                if old is not None:
                    if old['action']!='finish_step' or old['payload']['result_sha256']!=result_sha256:
                        raise BatchControlError('原完成步骤命令与这次结果不同。')
                    payload=old['payload']
                else:
                    if state['pending_step'] is None:raise BatchControlError('没有可完成的原步骤。')
                    payload={'run_epoch':handle.epoch,'step_id':state['pending_step']['step_id'],'result_sha256':result_sha256}
                def before():
                    if verify_result()!=result_sha256:raise BatchControlError('原步骤实际结果凭据未通过核对。')
                store.apply(action='finish_step',payload=payload,key=idempotency_key,expected_sha256=expected_control_sha256,
                    before_write=before,fault_injector=self.fault_injector)
            return self._view(context,handle.batch_id,grant)

    def pause_step(self,handle,*,expected_control_sha256,idempotency_key,verify_result):
        """Server-owned adverse disposition, without impersonating author consent."""
        if (not isinstance(handle,_RunHandle) or handle.service is not self or self.handles.get(handle.batch_id) is not handle
                or not callable(verify_result)):
            raise BatchControlPaused('策略暂停需要本服务的原步骤许可。')
        with self.publication._owned_project(handle.project_id) as context:
            grant=self.batches._stored(context,handle.batch_id)['grant'];store=self._store(context,handle.batch_id,grant)
            _,machine,book=control_paths(context,handle.batch_id)
            with persistence_state_lock((machine,book)):
                _,state=store.load()
                if state['status']!='running' or state['run_epoch']!=handle.epoch or state['pending_step'] is None:
                    raise BatchControlPaused('原步骤已暂停或属于其他运行。')
                store.apply(action='pause',payload={},key=idempotency_key,expected_sha256=expected_control_sha256,
                    before_write=verify_result,fault_injector=self.fault_injector)
            self.handles.pop(handle.batch_id,None)
            return self._view(context,handle.batch_id,grant)

    def _reconcile_original_finish(self,*,project_id,batch_id,idempotency_key,result_sha256,step_id,verify_result):
        """Complete only an already-recorded original finish; never issue a handle.

        The typed stage service authenticates its actual result under this
        owned boundary. It cannot use this entrypoint to append a new finish.
        """
        if not callable(verify_result):raise BatchControlError('原结果核验器缺失。')
        with self.publication._owned_project(project_id) as context:
            grant=self.batches._require_current_execution(context,batch_id)['grant']
            store=self._store(context,batch_id,grant);_,machine,book=control_paths(context,batch_id)
            with persistence_state_lock((machine,book)):
                recorded,_=validate_control(store._read(machine),grant)
                key_sha=digest(idempotency_key.encode('utf-8'))
                prior=next((c for c in recorded['commands'] if c['key_sha256']==key_sha),None)
                if (prior is None or prior['action']!='finish_step' or prior['payload']['step_id']!=step_id
                        or prior['payload']['result_sha256']!=result_sha256):
                    raise BatchControlError('没有已落盘的同一原完成命令，不能从结果文件创建新进度。')
                prefix={**recorded,'commands':recorded['commands'][:prior['sequence']-1]}
                prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
                _,previous=validate_control(prefix,grant)
                if previous['pending_step'] is None or previous['pending_step']['step_id']!=step_id:
                    raise BatchControlError('原完成命令缺少同一待办步骤。')
                was_torn=store._read(book)!=recorded
                def before():
                    if verify_result(context,grant,recorded,previous['pending_step'])!=result_sha256:
                        raise BatchControlError('原完成结果的实际凭据核对失败。')
                before()
                store.apply(action='finish_step',payload=prior['payload'],key=idempotency_key,
                    expected_sha256=prefix['record_sha256'],before_write=before,fault_injector=self.fault_injector)
            if was_torn:self.handles.pop(batch_id,None)
            return self._view(context,batch_id,grant)

    def _reconcile_original_claim(self,*,project_id,batch_id,chapter,stage,verify_inputs):
        """Repair an original claim mirror, never claim a new step or handle."""
        if not callable(verify_inputs):raise BatchControlError('原步骤输入核验器缺失。')
        with self.publication._owned_project(project_id) as context:
            grant=self.batches._require_current_execution(context,batch_id)['grant']
            if type(chapter) is not int or chapter not in grant['manifest']['chapters']:
                raise BatchControlError('原步骤章号不属于批次。')
            store=self._store(context,batch_id,grant);_,machine,book=control_paths(context,batch_id)
            with persistence_state_lock((machine,book)):
                recorded,_=validate_control(store._read(machine),grant)
                key='claim-'+stage+':'+str(chapter)
                prior=next((c for c in recorded['commands'] if c['key_sha256']==digest(key.encode('utf-8'))),None)
                if prior is None or prior['action']!='claim_step':
                    raise BatchControlError('没有已落盘的同一原领取命令，不能创建新的步骤。')
                prefix={**recorded,'commands':recorded['commands'][:prior['sequence']-1]}
                prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
                _,previous=validate_control(prefix,grant)
                if (previous['status']!='running' or previous['chapter']!=chapter or previous['stage']!=stage
                        or previous['pending_step'] is not None):
                    raise BatchControlError('原领取命令没有同一章号、阶段和前序运行。')
                was_torn=store._read(book)!=recorded
                def before():
                    execution,source=verify_inputs(context,grant,prefix,previous)
                    expected=dict(run_epoch=previous['run_epoch'],step_id='step_'+digest(
                        {'batch_id':batch_id,'chapter':chapter,'stage':stage})[:32],execution_id=execution,input_sha256=source)
                    if prior['payload']!=expected:
                        raise BatchControlError('原步骤领取命令与实际完整输入或确定执行不同。')
                before()
                store.apply(action='claim_step',payload=prior['payload'],key=key,expected_sha256=prefix['record_sha256'],
                    before_write=before,fault_injector=self.fault_injector)
            if was_torn:self.handles.pop(batch_id,None)
            return self._view(context,batch_id,grant)
