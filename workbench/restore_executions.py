"""Prepare durable old-execution fences and a fresh workbench task directory."""
from contextlib import closing, ExitStack
import os
import json
from pathlib import Path

from core.engine.persistence import atomic_create_json, persistence_state_lock, _fsync_directory, _windows_move_file
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_files import _ordinary
from workbench.backup_restore_database import _database_intent, _connect
from workbench.backup_restore_materialization import _validate_plan, _json_copy
from workbench.backup_restore_plan import _plan_archived_restore
from workbench.backup_restore_roots import _MaterializedCopy, _read
from workbench.instance import InstanceLease
from workbench.restore_budget_preparation import _BudgetOverlay, _CAPABILITY
from workbench.restore_execution_policy import execution_policy, POLICY_FILE, FRESH_JOBS
from workbench.restore_handoff import RestoreHandoffError, _signed
from workbench.restore_jobs import RestoreJobPreparation
from workbench.restore_scope import _lineage

POLICY_TEMP = '.execution-policy.tmp'


def _policy_bytes(policy):
    return (json.dumps(policy,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')


def execution_receipt(intent):
    policy = intent['policy']
    return _signed({'schema_version':'restore_executions_prepared_v1',
        'handoff_id':policy['handoff_id'], 'execution_intent_sha256':intent['intent_sha256'],
        'policy_sha256':policy['policy_sha256'], 'blocked_namespace_count':len(policy['blocked_paths']),
        'fresh_task_runtime_root':policy['fresh_task_runtime_root'],
        'fresh_task_runtime_binding':_lineage(Path(policy['fresh_task_runtime_root'])),
        'old_execution_namespaces_invalidated':True, 'new_task_directory_ready':True,
        'canonical_budget_history_preserved':True, 'write_activation_allowed':False,
        'handoff_completed':False}, 'receipt_sha256')


class _ExecutionOverlay(_BudgetOverlay):
    execution_intent = None
    require_complete = False

    def _additional_directories(self):
        if self.execution_intent is None: return set()
        path = self.service._path(self.value['handoff_id'], 'executions')
        return {FRESH_JOBS} if os.path.lexists(path) and os.path.lexists(Path(self.value['target'])/FRESH_JOBS) else set()

    def verify(self, copy_, replacements, extra):
        intent = self.execution_intent
        extra = set(extra)
        if intent is not None:
            path = self.service._path(self.value['handoff_id'], 'executions')
            if os.path.lexists(path):
                self.service._require_execution_intent(self.value, intent)
                target = copy_.root/POLICY_FILE
                if os.path.lexists(target):
                    if _read(copy_.root, POLICY_FILE) != intent['policy']:
                        raise RestoreHandoffError('恢复执行隔离策略变化。')
                    extra.add(POLICY_FILE)
                elif self.require_complete:
                    raise RestoreHandoffError('恢复执行隔离策略丢失。')
                fresh = copy_.root/FRESH_JOBS
                if os.path.lexists(fresh):
                    _ordinary(fresh)
                    if not fresh.is_dir() or any(fresh.iterdir()):
                        raise RestoreHandoffError('恢复后新任务目录必须独立且为空。')
                elif self.require_complete:
                    raise RestoreHandoffError('恢复后新任务目录丢失。')
                temporary = copy_.root/POLICY_TEMP
                if os.path.lexists(temporary):
                    if self.require_complete or _ordinary(temporary).st_size > len(_policy_bytes(intent['policy'])):
                        raise RestoreHandoffError('执行隔离暂存文件异常。')
                    extra.add(POLICY_TEMP)
        super().verify(copy_, replacements, extra)


class RestoreExecutionPreparation(RestoreJobPreparation):
    def _publish_policy(self, copy_, value, intent):
        self._require_execution_intent(value,intent)
        copy_.require_binding()
        target, temporary = copy_.root/POLICY_FILE, copy_.root/POLICY_TEMP
        if os.path.lexists(target):
            if _read(copy_.root,POLICY_FILE) != intent['policy'] or os.path.lexists(temporary):
                raise RestoreHandoffError('执行隔离策略或暂存状态变化。')
            return
        raw = _policy_bytes(intent['policy'])
        if os.path.lexists(temporary):
            if _ordinary(temporary).st_size > len(raw): raise RestoreHandoffError('执行隔离暂存文件超过原大小。')
            temporary.unlink()
        with temporary.open('xb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        self._fault('after_execution_policy_temp_fsync',value)
        self._require_execution_intent(value,intent)
        copy_.require_binding()
        if _read(copy_.root,POLICY_TEMP) != intent['policy']:
            raise RestoreHandoffError('执行隔离暂存文件内容变化。')
        if os.name == 'nt': _windows_move_file(temporary,target,replace_existing=False)
        else:
            os.link(temporary,target); temporary.unlink()
        _fsync_directory(copy_.root)
        self._fault('after_execution_policy',value)

    def _require_execution_intent(self, value, intent):
        self.owner._held()
        path = self._path(value['handoff_id'], 'executions')
        if self._pending(value['handoff_id']) != value or _read(path.parent,path.name) != intent:
            raise RestoreHandoffError('原交接或执行隔离意图变化。')
        jobs = _read(path.parent,self._path(value['handoff_id'],'jobs-done').name)
        if (jobs.get('receipt_sha256') != intent['policy']['jobs_receipt_sha256']
                or jobs != _signed({k:v for k,v in jobs.items() if k != 'receipt_sha256'}, 'receipt_sha256')):
            raise RestoreHandoffError('执行隔离前的任务准备凭据变化。')

    def _executions(self, package, *, handoff_id, temporary_root, resume):
        with self.owner.locked(), ExitStack() as budget_context:
            value = self._pending(handoff_id); root = Path(value['target'])
            budget, source = budget_context.enter_context(self._budget_context(package,value))
            budget_receipt = self._budget_receipt(value,budget)
            self._require_budget_intent(value,budget)
            if _read(self.owner.directory,self._path(handoff_id,'budget-done').name) != budget_receipt:
                raise RestoreHandoffError('执行隔离缺少原预算完成凭据。')
            plan = _validate_plan(_read(root,'restore-plan.json'))
            fresh = _plan_archived_restore(package,project_id=plan['project_id'],book_id=plan['book_id'],
                destination=root,temporary_root=temporary_root,_existing=True)
            if _json_copy(fresh) != plan or plan['plan_sha256'] != value['plan_sha256']:
                raise RestoreHandoffError('执行隔离归档或目标位置变化。')
            with closing(InstanceLease(root).acquire()), persistence_state_lock((root/'book',)):
                with verified_archive_view(package) as view:
                    overlay = _ExecutionOverlay(self,value,budget,source,_capability=_CAPABILITY)
                    try:
                        copy_ = _MaterializedCopy(root,plan,view,_usage_overlay=overlay)
                        base = _database_intent(copy_,temporary_root=temporary_root)
                        saved_jobs = _read(self.owner.directory,self._path(handoff_id,'jobs').name)
                        jobs = self._job_intent(value,budget_receipt,copy_,base,saved_jobs['created_at'],temporary_root)
                        self._require_job_intent(value,jobs)
                        jobs_receipt = self._jobs_receipt(value,jobs)
                        if _read(self.owner.directory,self._path(handoff_id,'jobs-done').name) != jobs_receipt:
                            raise RestoreHandoffError('执行隔离前旧任务尚未完成隔离。')
                        policy = execution_policy(copy_,value,budget,jobs_receipt,jobs_intent=jobs)
                        if {POLICY_FILE,POLICY_TEMP} & set(copy_.files) or any(name == FRESH_JOBS or name.startswith(FRESH_JOBS+'/')
                                for name in set(copy_.files)|copy_.directories):
                            raise RestoreHandoffError('执行隔离新位置与历史资料冲突。')
                        intent = _signed({'schema_version':'restore_execution_preparation_v1','policy':policy},'intent_sha256')
                        overlay.execution_intent = intent
                        path = self._path(handoff_id,'executions'); done = self._path(handoff_id,'executions-done')
                        exists, completed = os.path.lexists(path), os.path.lexists(done)
                        if exists and not resume and not completed:
                            raise RestoreHandoffError('执行隔离已开始，请显式续接。')
                        if not exists and (resume or completed):
                            raise RestoreHandoffError('原执行隔离意图丢失，不能补造。')
                        if exists: self._require_execution_intent(value,intent)
                        if completed and _read(done.parent,done.name) != execution_receipt(intent):
                            raise RestoreHandoffError('执行隔离完成凭据变化。')
                        overlay.require_complete = completed
                        database = root/'workbench.sqlite'
                        if any(os.path.lexists(database.with_name(database.name+suffix)) for suffix in ('-wal','-shm','-journal')):
                            raise RestoreHandoffError('执行隔离要求任务事务已经完全提交。')
                        with closing(_connect(database)) as connection:
                            self._verify_copy(copy_,connection,base,jobs,complete=True)
                            if not exists:
                                atomic_create_json(path,intent)
                                self._fault('after_execution_intent',value)
                            self._require_execution_intent(value,intent)
                            self._publish_policy(copy_,value,intent)
                            if not os.path.lexists(root/FRESH_JOBS):
                                (root/FRESH_JOBS).mkdir()
                                _fsync_directory(root)
                                self._fault('after_fresh_task_directory',value)
                            overlay.require_complete = True
                            self._fault('before_execution_completion',value)
                            self._verify_copy(copy_,connection,base,jobs,complete=True)
                            self._require_job_intent(value,jobs)
                            result = execution_receipt(intent)
                    finally:
                        overlay.live = False
                budget_context.close()
                self._require_execution_intent(value,intent)
                if not os.path.lexists(done):
                    atomic_create_json(done,result)
                    self._fault('after_execution_completion',value)
                elif _read(done.parent,done.name) != result:
                    raise RestoreHandoffError('执行隔离完成凭据不一致。')
            return result

    def prepare_executions(self, package, *, handoff_id, temporary_root=None):
        return self._executions(package,handoff_id=handoff_id,temporary_root=temporary_root,resume=False)

    def resume_executions(self, package, *, handoff_id, temporary_root=None):
        return self._executions(package,handoff_id=handoff_id,temporary_root=temporary_root,resume=True)
