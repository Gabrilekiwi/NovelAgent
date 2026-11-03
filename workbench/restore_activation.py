"""Crash-resumable activation of an actually verified single-book restore."""
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path

from core.engine.persistence import atomic_create_json, atomic_write_json, persistence_state_lock, _fsync_directory, _windows_move_file
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_files import _ordinary
from workbench.backup_restore_database import _database_intent, _connect
from workbench.backup_restore_materialization import _validate_plan, _json_copy
from workbench.backup_restore_plan import _plan_archived_restore
from workbench.backup_restore_roots import _MaterializedCopy, _read
from workbench.book_ownership import OWNERSHIP_V3, _record
from workbench.instance import InstanceLease
from workbench.restore_budget_preparation import _CAPABILITY
from workbench.restore_execution_policy import execution_policy
from workbench.restore_executions import RestoreExecutionPreparation, _ExecutionOverlay, execution_receipt
from workbench.restore_handoff import RestoreHandoffError, _signed
from workbench.restore_qualification import ACTIVATION_FILE, ACTIVATION_TEMP, activation_receipt, archived_recovery_history
from workbench.restore_scope import load_restore_scope


def _bytes(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')


class _ActivationOverlay(_ExecutionOverlay):
    activation_intent = None

    def verify(self,copy_,replacements,extra):
        extra=set(extra)
        path=self.service._path(self.value['handoff_id'],'activation')
        intent=self.activation_intent
        if intent is not None and os.path.lexists(path):
            self.service._require_activation_intent(self.value,intent)
            if os.path.lexists(copy_.root/ACTIVATION_FILE):
                if _read(copy_.root,ACTIVATION_FILE)!=intent['qualification']:
                    raise RestoreHandoffError('本地写入资格与原启用意图不同。')
                extra.add(ACTIVATION_FILE)
            if os.path.lexists(copy_.root/ACTIVATION_TEMP):
                if _ordinary(copy_.root/ACTIVATION_TEMP).st_size>len(_bytes(intent['qualification'])):
                    raise RestoreHandoffError('写入资格暂存文件超出原大小。')
                extra.add(ACTIVATION_TEMP)
        super().verify(copy_,replacements,extra)


class RestoreActivation(RestoreExecutionPreparation):
    _allow_pending_active=False

    def _pending(self,handoff_id):
        if not self._allow_pending_active: return super()._pending(handoff_id)
        value=self._load(handoff_id)
        current=_record(self.owner.directory,self.owner.book_id)
        if current==value['restoring_record']: return super()._pending(handoff_id)
        path=self._path(handoff_id,'activation')
        if os.path.lexists(self._path(handoff_id,'rollback')) or not os.path.lexists(path):
            raise RestoreHandoffError('启用交接已经变化或原意图丢失。')
        intent=_read(path.parent,path.name)
        if (current is None or intent!=_signed({k:v for k,v in intent.items() if k!='intent_sha256'},'intent_sha256')
                or current['version']!=OWNERSHIP_V3
                or intent['qualification']['handoff_id']!=handoff_id):
            raise RestoreHandoffError('当前机器记录不属于原启用操作。')
        if current!=intent['active_record']:
            scope=load_restore_scope(Path(value['target'])/'workbench.sqlite',machine_store=self.owner.store)
            if scope is None or not scope.active or scope.activation!=intent['qualification']:
                raise RestoreHandoffError('当前机器代际没有完成原恢复资格的显式续接。')
        return value

    def _activation_intent(self,value,copy_,base,jobs,budget_receipt,execution_done):
        qualification=_signed({'schema_version':'restored_write_qualification_v1',
            'handoff_id':value['handoff_id'],'handoff_intent_sha256':value['intent_sha256'],
            'book_id':self.owner.book_id,'project_id':copy_.plan['project_id'],
            'book_root':str(copy_.book),'database':str(copy_.root/'workbench.sqlite'),
            'scope_sha256':base['scope']['record_sha256'],
            'plan_sha256':copy_.plan['plan_sha256'],'archive_receipt_sha256':value['archive_receipt_sha256'],
            'initial_database_logical_sha256':jobs['after_logical_sha256'],
            'budget_receipt_sha256':budget_receipt['receipt_sha256'],
            'jobs_receipt_sha256':self._jobs_receipt(value,jobs)['receipt_sha256'],
            'execution_receipt_sha256':execution_done['receipt_sha256'],
            'execution_policy_sha256':execution_done['policy_sha256'],
            'fresh_task_runtime_root':execution_done['fresh_task_runtime_root'],
            'fresh_task_runtime_binding':execution_done['fresh_task_runtime_binding'],
            'archived_recovery_history':archived_recovery_history(copy_),
            'automatic_provider_calls_allowed':False},'qualification_sha256')
        active=_signed({**value['restoring_record'],'version':OWNERSHIP_V3,
            'generation':value['restoring_record']['generation']+1,'state':'active',
            'restore_qualification_sha256':qualification['qualification_sha256']})
        return _signed({'schema_version':'restored_write_activation_intent_v1',
            'qualification':qualification,'active_record':active},'intent_sha256')

    def _require_activation_intent(self,value,intent):
        self.owner._held()
        path=self._path(value['handoff_id'],'activation')
        if self._pending(value['handoff_id'])!=value or _read(path.parent,path.name)!=intent:
            raise RestoreHandoffError('恢复启用的机器记录或原意图变化。')

    def _publish_qualification(self,value,intent):
        root=Path(value['target']); target=root/ACTIVATION_FILE; temporary=root/ACTIVATION_TEMP
        self._require_activation_intent(value,intent)
        if os.path.lexists(target):
            if _read(root,ACTIVATION_FILE)!=intent['qualification'] or os.path.lexists(temporary):
                raise RestoreHandoffError('原本地写入资格或暂存状态变化。')
            return
        raw=_bytes(intent['qualification'])
        if os.path.lexists(temporary):
            if _ordinary(temporary).st_size>len(raw): raise RestoreHandoffError('写入资格暂存大小变化。')
            temporary.unlink()
        with temporary.open('xb') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        self._fault('after_activation_temp_fsync',value)
        self._require_activation_intent(value,intent)
        if _read(root,ACTIVATION_TEMP)!=intent['qualification']:
            raise RestoreHandoffError('写入资格暂存内容变化。')
        if os.name=='nt': _windows_move_file(temporary,target,replace_existing=False)
        else:
            os.link(temporary,target);temporary.unlink()
        _fsync_directory(root)
        self._fault('after_activation_qualification',value)

    def activate(self,package,*,handoff_id,temporary_root=None,resume=False):
        with self.owner.locked():
            self._allow_pending_active=True
            try: return self._activate(package,handoff_id=handoff_id,temporary_root=temporary_root,resume=resume)
            finally: self._allow_pending_active=False

    def _activate(self,package,*,handoff_id,temporary_root,resume):
        value=self._pending(handoff_id); root=Path(value['target'])
        done=self._path(handoff_id,'activation-done'); path=self._path(handoff_id,'activation')
        if os.path.lexists(done):
            intent=_read(path.parent,path.name); self._require_activation_intent(value,intent)
            scope=load_restore_scope(root/'workbench.sqlite',machine_store=self.owner.store)
            result=activation_receipt(intent)
            if scope is None or not scope.active or _read(done.parent,done.name)!=result:
                raise RestoreHandoffError('已启用的恢复资格无法核验。')
            return result
        exists=os.path.lexists(path)
        if exists and not resume: raise RestoreHandoffError('恢复启用已开始，请显式续接。')
        if resume and not exists: raise RestoreHandoffError('原启用意图丢失，不能补造。')
        with ExitStack() as evidence:
            budget,source=evidence.enter_context(self._budget_context(package,value))
            budget_receipt=self._budget_receipt(value,budget)
            if not budget_receipt['settled'] or budget_receipt['blockers']:
                raise RestoreHandoffError('原消费或预算尚未核实结算，禁止启用恢复副本。')
            plan=_validate_plan(_read(root,'restore-plan.json'))
            fresh=_plan_archived_restore(package,project_id=plan['project_id'],book_id=plan['book_id'],
                destination=root,temporary_root=temporary_root,_existing=True)
            if _json_copy(fresh)!=plan or plan['plan_sha256']!=value['plan_sha256'] or plan['continuation_blockers']:
                raise RestoreHandoffError('恢复版本/归档/位置或下一章条件不满足。')
            # The machine gate blocks supported writers throughout activation;
            # new direct Provider calls also require completed activation. Keep
            # archived lock files readable for whole-copy verification on Windows.
            with closing(InstanceLease(root).acquire()),persistence_state_lock((root/'book',)):
                view=evidence.enter_context(verified_archive_view(package))
                overlay=_ActivationOverlay(self,value,budget,source,_capability=_CAPABILITY)
                try:
                    copy_=_MaterializedCopy(root,plan,view,_usage_overlay=overlay)
                    base=_database_intent(copy_,temporary_root=temporary_root)
                    old_jobs=_read(self.owner.directory,self._path(handoff_id,'jobs').name)
                    jobs=self._job_intent(value,budget_receipt,copy_,base,old_jobs['created_at'],temporary_root)
                    self._require_job_intent(value,jobs)
                    jobs_receipt=self._jobs_receipt(value,jobs)
                    if _read(self.owner.directory,self._path(handoff_id,'jobs-done').name)!=jobs_receipt:
                        raise RestoreHandoffError('原任务尚未完成隔离。')
                    policy=execution_policy(copy_,value,budget,jobs_receipt,jobs_intent=jobs)
                    execution_intent=_signed({'schema_version':'restore_execution_preparation_v1','policy':policy},'intent_sha256')
                    self._require_execution_intent(value,execution_intent)
                    execution_done=execution_receipt(execution_intent)
                    if _read(self.owner.directory,self._path(handoff_id,'executions-done').name)!=execution_done:
                        raise RestoreHandoffError('原执行目录隔离尚未完成。')
                    overlay.execution_intent=execution_intent;overlay.require_complete=True
                    intent=self._activation_intent(value,copy_,base,jobs,budget_receipt,execution_done)
                    overlay.activation_intent=intent
                    if exists:self._require_activation_intent(value,intent)
                    database=root/'workbench.sqlite'
                    if any(os.path.lexists(database.with_name(database.name+suffix)) for suffix in ('-wal','-shm','-journal')):
                        raise RestoreHandoffError('启用前数据库必须完整关闭。')
                    with closing(_connect(database)) as connection:
                        connection.execute('BEGIN IMMEDIATE')
                        self._verify_copy(copy_,connection,base,jobs,complete=True)
                        if not exists:
                            atomic_create_json(path,intent)
                            self._fault('after_activation_intent',value)
                        self._publish_qualification(value,intent)
                        self._fault('before_activation_machine',value)
                        self._verify_copy(copy_,connection,base,jobs,complete=True)
                        self._require_job_intent(value,jobs)
                        # All archive/source views must pass their final checks
                        # before any machine active record or completion receipt.
                        evidence.close()
                        self._require_activation_intent(value,intent)
                        if _record(self.owner.directory,self.owner.book_id)==value['restoring_record']:
                            atomic_write_json(self.owner.record_path,intent['active_record'])
                            self._fault('after_activation_machine',value)
                        self._require_activation_intent(value,intent)
                        result=activation_receipt(intent)
                        atomic_create_json(done,result)
                        self._fault('after_activation_completion',value)
                        connection.rollback()  # No SQL mutation is part of activation.
                finally: overlay.live=False
                scope=load_restore_scope(database,machine_store=self.owner.store)
                if scope is None or not scope.active: raise RestoreHandoffError('恢复完成后未取得活动资格。')
                return result
