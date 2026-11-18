"""Explicit background dispatch tied to original durable batch commands.

Task files describe work; they never grant permission. Construction/status do
not spawn threads, and a previously recorded command is never launched again.
"""
import copy,os,re,threading

from core.engine.persistence import atomic_create_json
from core.engine.reviewed_batch_control import BatchControlError
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest
from workbench.reviewed_batch_driver import ReviewedBatchDriver,_run_command
from workbench.restore_execution_policy import require_restored_execution_paths


def _signed(value):
    return {**value,'record_sha256':digest(value)}


class ReviewedBatchTaskService:
    def __init__(self,controls,*,instance_lease,maintenance,driver_factory=None,fault_injector=None):
        self.controls=controls;self.lease=instance_lease;self.maintenance=maintenance
        if maintenance.lease is not instance_lease:raise ValueError('任务与维护必须属于同一工作台实例。')
        self.driver_factory=driver_factory or ReviewedBatchDriver
        self.fault_injector=fault_injector
        self._mutex=threading.RLock();self._active=None;self._closing=False

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    def _paths(self,project_id,batch_id,command):
        context=self.controls._control_context(project_id)
        grant=self.controls.batches._stored(context,batch_id)['grant']
        base=assert_safe_local_tree(context[6]/'reviewed/batch_tasks')
        return context,grant,base/(command['command_sha256']+'.json'),base/(command['command_sha256']+'.result.json')

    @staticmethod
    def _ticket(grant,command):
        return _signed(dict(schema_version='reviewed_batch_task_v1',project_id=grant['manifest']['project_id'],
            book_id=grant['manifest']['book_id'],batch_id=grant['manifest']['batch_id'],grant_sha256=grant['grant_sha256'],
            run_command=copy.deepcopy(command),restart_behavior='explicit_resume_required'))

    def _read_task(self,project_id,batch_id,command,record):
        _,grant,path,result_path=self._paths(project_id,batch_id,command)
        expected=self._ticket(grant,command)
        if not os.path.lexists(path):
            if os.path.lexists(result_path):raise BatchControlError('后台任务缺少原启动记录。')
            return None
        actual=self.controls.batches._read(path)
        if actual!=expected or digest(actual)!=digest(expected):raise BatchControlError('后台任务与原批次运行命令不同。')
        outcome=None
        if os.path.lexists(result_path):
            outcome=self.controls.batches._read(result_path)
            if (not isinstance(outcome,dict) or set(outcome)!={'schema_version','task_sha256','control_sha256','control_sequence','outcome','error_code','record_sha256'}
                    or outcome['schema_version']!='reviewed_batch_task_result_v1' or outcome['task_sha256']!=actual['record_sha256']
                    or type(outcome['control_sequence']) is not int or not command['sequence']<=outcome['control_sequence']<=len(record['commands'])
                    or not isinstance(outcome['control_sha256'],str) or re.fullmatch('[0-9a-f]{64}',outcome['control_sha256']) is None
                    or not isinstance(outcome['outcome'],str) or outcome['outcome'] not in {'returned','failed'}
                    or not (outcome['error_code'] is None or isinstance(outcome['error_code'],str)
                        and outcome['error_code'] in {'batch_execution_interrupted','batch_execution_failed'})
                    or (outcome['outcome']=='returned')!=(outcome['error_code'] is None)
                    or outcome!=_signed({k:v for k,v in outcome.items() if k!='record_sha256'})):
                raise BatchControlError('后台任务结果记录不完整或已变化。')
            prefix={**record,'commands':record['commands'][:outcome['control_sequence']]}
            if outcome['control_sha256']!=digest({k:v for k,v in prefix.items() if k!='record_sha256'}):
                raise BatchControlError('后台任务结果不属于原批次控制历史。')
        return dict(ticket=actual,result=outcome)

    def get(self,*,project_id,batch_id):
        # No book execution lock: status and stop remain available during a call.
        with self._mutex:
            view=self.controls.get(project_id=project_id,batch_id=batch_id)
            command=next((c for c in reversed(view['control']['commands']) if c['action'] in {'start','resume'}),None)
            task=self._read_task(project_id,batch_id,command,view['control']) if command is not None else None
            active=self._active
            live=bool(active is not None and active['project_id']==project_id and active['batch_id']==batch_id
                and command is not None and active['command_sha256']==command['command_sha256'])
            state=('running' if live else 'finished' if task and task['result'] else 'interrupted' if task else 'not_started')
            return dict(control=view,task=task,task_state=state,execution_live=live,
                explicit_resume_required=view['progress']['status'] not in {'stopped','completed'} and not live,
                execution_permit=False,publication_authorized=False)

    def submit(self,*,project_id,batch_id,action,idempotency_key,expected_control_sha256,author_confirmed):
        if (author_confirmed is not True or action not in {'start','resume'}
                or not isinstance(idempotency_key,str) or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,191}',idempotency_key) is None):
            raise BatchControlError('请明确开始或继续，并保留本次请求编号。')
        with self._mutex:
            self.lease.require_held()
            if self._closing:raise BatchControlError('工作台正在关闭，不能开始新任务。')
            view=self.controls.get(project_id=project_id,batch_id=batch_id)
            prior=next((c for c in view['control']['commands'] if c['key_sha256']==digest(idempotency_key.encode('utf-8'))),None)
            if prior is not None:
                prefix={**view['control'],'commands':view['control']['commands'][:prior['sequence']-1]}
                prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
                if prior['action']!=action or prefix['record_sha256']!=expected_control_sha256:
                    raise BatchControlError('原运行请求的动作或前序版本不同。')
                # Even an interrupted ticket (or absent ticket) requires a new
                # explicit resume. Repeating the request only reads its result.
                return self.get(project_id=project_id,batch_id=batch_id)
            if self._active is not None:raise BatchControlError('本工作台已有自动批次在执行，请先等待或暂停。')
            permit=self.maintenance.admit('worker');handle=None;driver=None;command=None
            try:
                view,handle=self.controls.command(project_id=project_id,batch_id=batch_id,action=action,
                    idempotency_key=idempotency_key,expected_control_sha256=expected_control_sha256,author_confirmed=True)
                if handle is None:raise BatchControlError('原运行命令没有授予本次工作台执行许可，请重新读取。')
                command=next(c for c in reversed(view['control']['commands']) if c['action'] in {'start','resume'})
                driver=self.driver_factory(self.controls)
                with self.controls.publication._owned_project(project_id) as context:
                    grant=self.controls.batches._stored(context,batch_id)['grant']
                    path=assert_safe_local_tree(context[6]/'reviewed/batch_tasks')/(command['command_sha256']+'.json')
                    require_restored_execution_paths(path)
                    atomic_create_json(path,self._ticket(grant,command))
                self._fault('after_task_saved')
                active=dict(project_id=project_id,batch_id=batch_id,command_sha256=command['command_sha256'],handle=handle)
                thread=threading.Thread(target=self._work,args=(active,driver,view['control']['record_sha256'],permit),
                    name='reviewed-batch-'+command['command_sha256'][:12],daemon=False)
                active['thread']=thread;self._active=active
                # Capture the accepted response while the command/ticket are
                # still quiescent. Once started, the worker may own the restore
                # qualification gate; a status read then must not turn an
                # already accepted start into an apparent rejected command.
                response=self.get(project_id=project_id,batch_id=batch_id)
                thread.start()
            except BaseException:
                self._active=None
                if handle is not None:
                    (driver or ReviewedBatchDriver(self.controls))._revoke_after_error(handle,command['command_sha256'] if command else _run_command(view))
                permit.close()
                raise
            return response

    def _work(self,active,driver,expected,permit):
        failure=None
        try:
            self.lease.require_held()
            driver.run(active['handle'],expected_control_sha256=expected)
        except BaseException as exc:
            failure=exc
            try:driver._revoke_after_error(active['handle'],active['command_sha256'])
            except BaseException:pass
        try:
            self._fault('before_task_result')
            with self.controls.publication._owned_project(active['project_id']) as context:
                grant=self.controls.batches._stored(context,active['batch_id'])['grant']
                record,_=self.controls._store(context,active['batch_id'],grant).load()
                command=next(c for c in record['commands'] if c['command_sha256']==active['command_sha256'])
                ticket=self._ticket(grant,command)
                base=assert_safe_local_tree(context[6]/'reviewed/batch_tasks')
                if self.controls.batches._read(base/(active['command_sha256']+'.json'))!=ticket:
                    raise BatchControlError('后台任务原记录已变化，不能写入完成状态。')
                result=_signed(dict(schema_version='reviewed_batch_task_result_v1',task_sha256=ticket['record_sha256'],
                    control_sha256=record['record_sha256'],control_sequence=len(record['commands']),outcome='failed' if failure is not None else 'returned',
                    error_code=('batch_execution_interrupted' if isinstance(failure,BatchControlError) else 'batch_execution_failed') if failure is not None else None))
                path=base/(active['command_sha256']+'.result.json');require_restored_execution_paths(path)
                atomic_create_json(path,result)
        except BaseException:
            # A torn/damaged domain log cannot be labelled completed. Preserve
            # the ticket; status reports interrupted and explicit recovery.
            pass
        finally:
            with self._mutex:
                self._drop_original_handle(active)
                permit.close()
                if self._active is active:self._active=None

    def _drop_original_handle(self,active):
        if self.controls.handles.get(active['batch_id']) is not active['handle']:return
        try:
            view=self.controls.get(project_id=active['project_id'],batch_id=active['batch_id'])
            if _run_command(view)!=active['command_sha256']:return
        except Exception:pass
        self.controls.handles.pop(active['batch_id'],None)

    def close(self):
        """Revoke future dispatch, then retain instance ownership until exit."""
        with self._mutex:
            self._closing=True;active=self._active
            if active is not None:self._drop_original_handle(active)
        if active is not None:active['thread'].join()
