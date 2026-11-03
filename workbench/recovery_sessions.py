"""Durable orchestration of the existing single-book restore protocol.

Session state owns workflow progress, never write qualification. Every step
delegates to the original archive, physical-binding and machine-ledger checks.
There is no automatic execution on construction, discovery or restart.
"""
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import re
import uuid

from core.engine.persistence import atomic_create_json, atomic_write_json, persistence_state_lock
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive import BackupArchiveError, _lineage, verify_archive
from workbench.backup_files import _ordinary
from workbench.backup_restore_materialization import materialize_archived_restore, _validate_plan, _json_copy
from workbench.backup_restore_plan import plan_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.restore_activation import RestoreActivation
from workbench.restore_scope import load_restore_scope
from workbench.book_ownership import _machine_gate


class RecoverySessionError(BackupArchiveError):
    code = 'recovery_session_invalid'


_NEXT = {
    'materialize':('planned','materialized'),
    'bind_roots':('materialized','roots_bound'),
    'prepare_database':('roots_bound','readonly_ready'),
    'preview_handoff':('readonly_ready','handoff_ready'),
    'handoff':('handoff_ready','source_revoked'),
    'capture_usage':('source_revoked','usage_preserved'),
    'prepare_budget':('usage_preserved','budget_prepared'),
    'prepare_jobs':('budget_prepared','jobs_prepared'),
    'prepare_executions':('jobs_prepared','execution_prepared'),
    'activate':('execution_prepared','active'),
}
_CONFIRM = {'handoff','activate','rollback'}


def _signed(value, field):
    return {**value,field:digest({k:v for k,v in value.items() if k != field})}


def _read(path):
    if _ordinary(path).st_size > 32*1024**2:
        raise RecoverySessionError('恢复会话超过支持大小。')
    return json.loads(read_project_bytes(path.parent,path.name))


def load_session_record(controller, session_id, *, archive_root=None):
    """Read original session evidence without constructing a controller or writing."""
    controller = Path(controller).absolute()
    if not isinstance(session_id,str) or re.fullmatch('restore_[0-9a-f]{32}',session_id) is None:
        raise RecoverySessionError('恢复会话标识无效。')
    directory = controller/'recovery-sessions'/session_id
    intent = _read(directory/'intent.json')
    if (not isinstance(intent,dict) or intent.get('schema_version') != 'recovery_session_intent_v1'
            or intent != _signed(intent,'intent_sha256') or intent.get('session_id') != session_id
            or not isinstance(intent.get('operation_id'),str)
            or re.fullmatch('[0-9a-f]{32}',intent['operation_id']) is None
            or not isinstance(intent.get('archive_root'),str)
            or not Path(intent['archive_root']).is_absolute() or '..' in Path(intent['archive_root']).parts
            or archive_root is not None and intent['archive_root'] != str(archive_root)
            or intent.get('controller_binding') != _json_copy(_lineage(controller))
            or intent.get('session_binding') != _json_copy(_lineage(directory))):
        raise RecoverySessionError('恢复会话意图或原控制目录的物理绑定变化。')
    _validate_plan(intent['plan'])
    path = directory/'state.json'
    state = _read(path) if os.path.lexists(path) else RecoverySessionService._initial(intent)
    if (not isinstance(state,dict) or state.get('schema_version') != 'recovery_session_state_v1'
            or state != _signed(state,'state_sha256') or state.get('session_id') != session_id
            or state.get('intent_sha256') != intent['intent_sha256']
            or type(state.get('revision')) is not int or state['revision'] < 1
            or state.get('phase') not in {'planned','rolled_back','cancelled',*(v[1] for v in _NEXT.values())}
            or state.get('pending_action') not in {None,'rollback','cancel',*_NEXT}
            or not isinstance(state.get('receipts'),dict)):
        raise RecoverySessionError('恢复会话状态缺失、损坏或与原意图不一致。')
    return intent,state


class RecoverySessionService:
    def __init__(self, *, archive_root, maintenance, temporary_root=None,
                 machine_store=None, fault_injector=None, source_scope=None):
        self.archive_root = assert_safe_local_tree(Path(archive_root).absolute())
        self.maintenance = maintenance
        self.root = maintenance.lease.directory/'recovery-sessions'
        self.temporary_root = temporary_root
        self.machine_store = machine_store
        self.fault_injector = fault_injector
        self.source_scope = source_scope

    def _require_source_plan(self, plan):
        if self.source_scope is not None:
            proof = plan.get('source_restore_provenance')
            if (proof is None or proof['scope'] != self.source_scope.document
                    or plan['project_id'] != self.source_scope.project_id
                    or plan['book_id'] != self.source_scope.document['book_id']):
                raise RecoverySessionError('归档不属于此恢复实例的原目标作品和范围。')

    def _fault(self, phase, state):
        if self.fault_injector is not None:
            self.fault_injector(phase,state)

    def _package(self, archive_id):
        if not isinstance(archive_id,str) or re.fullmatch('backup_[0-9a-f]{32}',archive_id) is None:
            raise RecoverySessionError('归档标识无效。')
        return self.archive_root/archive_id

    def _directory(self, session_id):
        if not isinstance(session_id,str) or re.fullmatch('restore_[0-9a-f]{32}',session_id) is None:
            raise RecoverySessionError('恢复会话标识无效。')
        return self.root/session_id

    def _admitted(self, permit, operation_id):
        status = self.maintenance.status()
        if status['purpose'] != 'restore':
            raise RecoverySessionError('恢复会话需要恢复维护状态。')
        return self.maintenance.borrow_capture(permit,operation_id)

    def preview(self, archive_id, *, destination, operation_id, snapshot_permit):
        with self._admitted(snapshot_permit,operation_id):
            target = Path(destination)
            controller = self.maintenance.lease.directory
            if (not target.is_absolute() or '..' in target.parts
                    or target.is_relative_to(controller) or controller.is_relative_to(target)):
                raise RecoverySessionError('恢复副本必须位于工作台数据目录之外的新目录。')
            package = self._package(archive_id)
            verified = verify_archive(package)
            source = verified['manifest']['preview']
            plan = plan_archived_restore(package, project_id=source['project_id'],book_id=source['book_id'],
                destination=target, temporary_root=self.temporary_root)
            self._require_source_plan(plan)
            return _json_copy(plan)

    def create(self, archive_id, *, destination, operation_id, expected_plan_sha256,
               snapshot_permit, session_id=None):
        session_id = session_id or 'restore_'+uuid.uuid4().hex
        directory = self._directory(session_id)
        with self._admitted(snapshot_permit,operation_id), persistence_state_lock((directory/'state.json',)):
            if os.path.lexists(directory):
                intent,state = self._load(session_id)
                if (intent['archive_id'] != archive_id or intent['operation_id'] != operation_id
                        or intent['plan']['plan_sha256'] != expected_plan_sha256
                        or intent['plan']['root_plan']['container'] != str(Path(destination))):
                    raise RecoverySessionError('已有会话属于其他恢复请求，不能覆盖。')
                return self._public(intent,state)
            plan = self.preview(archive_id,destination=destination,operation_id=operation_id,
                snapshot_permit=snapshot_permit)
            if plan['plan_sha256'] != expected_plan_sha256:
                raise RecoverySessionError('归档、恢复目标或版本已偏离预览，请重新核对。')
            _validate_plan(plan)
            assert_safe_local_tree(self.root).mkdir(exist_ok=True)
            directory.mkdir()
            intent = _signed({'schema_version':'recovery_session_intent_v1','session_id':session_id,
                'archive_id':archive_id,'archive_root':str(self.archive_root),'operation_id':operation_id,
                'controller_binding':_json_copy(_lineage(self.maintenance.lease.directory)),
                'session_binding':_json_copy(_lineage(directory)), 'plan':plan},'intent_sha256')
            atomic_create_json(directory/'intent.json',intent)
            self._fault('after_session_intent',intent)
            state = self._initial(intent)
            atomic_create_json(directory/'state.json',state)
            return self._public(intent,state)

    @staticmethod
    def _initial(intent):
        return _signed({'schema_version':'recovery_session_state_v1','session_id':intent['session_id'],
            'intent_sha256':intent['intent_sha256'],'revision':1,'phase':'planned','pending_action':None,
            'pending_input_sha256':None,'last_action':None,'last_input_sha256':None,
            'last_resume_input_sha256':None,
            'receipts':{},'previous_sha256':None},'state_sha256')

    def _load(self, session_id):
        try:
            return self._load_record(session_id)
        except RecoverySessionError:
            raise
        except (OSError,ValueError,TypeError,KeyError,RuntimeError) as exc:
            raise RecoverySessionError('恢复会话无法核验，请保留原目录和错误现场。') from exc

    def _load_record(self, session_id):
        intent,state = load_session_record(self.maintenance.lease.directory,session_id,archive_root=self.archive_root)
        self._require_source_plan(intent['plan'])
        return intent,state

    def _write(self, intent, before, **changes):
        current_intent,current = self._load(intent['session_id'])
        if current_intent != intent or current != before:
            raise RecoverySessionError('恢复会话在更新前发生变化。')
        state = _signed({**before,**changes,'revision':before['revision']+1,
            'previous_sha256':before['state_sha256']},'state_sha256')
        path = self._directory(intent['session_id'])/'state.json'
        (atomic_write_json if os.path.lexists(path) else atomic_create_json)(path,state)
        if _read(path) != state:
            raise RecoverySessionError('恢复会话状态未通过写后核验。')
        return state

    def _public(self, intent, state):
        plan = intent['plan']
        usage = state['receipts'].get('capture_usage',{})
        if state['phase'] == 'active':
            scope = load_restore_scope(Path(plan['root_plan']['container'])/'workbench.sqlite',machine_store=self.machine_store)
            if scope is None or not scope.active or scope.document['plan_sha256'] != plan['plan_sha256']:
                raise RecoverySessionError('已记录的恢复启用资格当前无法核验。')
        return {'schema_version':'recovery_session_view_v1','session_id':intent['session_id'],
            'operation_id':intent['operation_id'],'archive_id':intent['archive_id'],
            'project_id':plan['project_id'],'book_id':plan['book_id'],'destination':plan['root_plan']['container'],
            'plan_sha256':plan['plan_sha256'],'state_sha256':state['state_sha256'],
            'revision':state['revision'],'phase':state['phase'],'pending_action':state['pending_action'],
            'next_action':None if state['pending_action'] is not None else next(
                (action for action,(phase,_) in _NEXT.items() if phase == state['phase']),None),
            'continuation_blockers':plan['continuation_blockers'],
            'usage_settled':usage.get('settled'),'usage_blockers':usage.get('blockers',[]),
            'activated':state['phase']=='active','recoverable':False,
            'automatic_provider_calls_allowed':False}

    def get(self, session_id):
        with persistence_state_lock((self._directory(session_id)/'state.json',)):
            return self._public(*self._load(session_id))

    def list(self):
        if not os.path.lexists(self.root):
            return []
        _lineage(self.root)
        result = []
        for directory in sorted(self.root.iterdir()):
            if re.fullmatch('restore_[0-9a-f]{32}',directory.name) is None:
                continue
            result.append(self.get(directory.name))
            if len(result)>1000:
                raise RecoverySessionError('恢复会话数量超过支持上限。')
        return result

    def require_finishable(self, operation_id):
        """Keep the original maintenance operation until its sessions terminate."""
        self.maintenance.require_quiescent(operation_id)
        if not os.path.lexists(self.root):
            return
        _lineage(self.root)
        for directory in self.root.iterdir():
            if re.fullmatch('restore_[0-9a-f]{32}',directory.name) is None:
                continue
            with persistence_state_lock((directory/'state.json',)):
                intent,state = self._load(directory.name)
                if intent['operation_id'] != operation_id:
                    continue
                if state['pending_action'] is not None or state['phase'] not in {'active','rolled_back','cancelled'}:
                    raise RecoverySessionError('恢复会话尚未结束。交接前可取消并保留副本；交接后请启用副本或恢复原作品写资格。')
                self._public(intent,state)

    def step(self, session_id, action, *, expected_state_sha256, operation_id,
             snapshot_permit, confirmed=False, resume=False):
        if action not in {*_NEXT,'rollback','cancel'} or type(resume) is not bool or type(confirmed) is not bool:
            raise RecoverySessionError('恢复步骤或显式继续选择无效。')
        if not isinstance(expected_state_sha256,str) or re.fullmatch('[0-9a-f]{64}',expected_state_sha256) is None:
            raise RecoverySessionError('请读取原恢复会话的有效状态凭据。')
        directory = self._directory(session_id)
        with self._admitted(snapshot_permit,operation_id), persistence_state_lock((directory/'state.json',)), ExitStack() as guards:
            intent,state = self._load(session_id)
            if intent['operation_id'] != operation_id:
                raise RecoverySessionError('恢复会话必须继续原维护操作。')
            if (state['pending_action'] is None and state['last_action'] == action
                    and expected_state_sha256 in {state['last_input_sha256'],state.get('last_resume_input_sha256')}):
                return self._public(intent,state)  # Lost response, already persisted original result.
            if state['state_sha256'] != expected_state_sha256:
                raise RecoverySessionError('恢复进度已变化，请刷新原会话。')
            if action in _CONFIRM and not confirmed:
                raise RecoverySessionError('写资格交接、启用或回退需要明确确认。')
            if action == 'activate' and state['pending_action'] is None:
                usage = state['receipts'].get('capture_usage',{})
                budget = state['receipts'].get('prepare_budget',{})
                if (intent['plan']['continuation_blockers'] or usage.get('settled') is not True
                        or usage.get('blockers') or budget.get('settled') is not True or budget.get('blockers')):
                    # This is only an early denial. The core still revalidates
                    # original financial evidence before granting qualification.
                    raise RecoverySessionError('消费证据、预算或继续条件尚未核实，不能开始启用；可以恢复原作品写资格。')
            if action == 'cancel':
                if state['phase'] not in {'planned','materialized','roots_bound','readonly_ready','handoff_ready'}:
                    raise RecoverySessionError('当前会话不能取消；交接开始后请启用副本或恢复原作品写资格。')
                preview = state['receipts'].get('preview_handoff')
                if preview is not None:
                    service = RestoreActivation(intent['plan']['book_id'],machine_store=self.machine_store)
                    guards.enter_context(_machine_gate(service.owner.book_id,service.owner.store))
                    if os.path.lexists(service._path(preview['handoff_id'])):
                        raise RecoverySessionError('原写资格交接已经开始，请恢复原作品写资格，不能取消交接记录。')
            if action == 'rollback':
                preview = state['receipts'].get('preview_handoff')
                service = RestoreActivation(intent['plan']['book_id'],machine_store=self.machine_store)
                if preview is None or not os.path.lexists(service._path(preview['handoff_id'])):
                    raise RecoverySessionError('原交接尚未开始，不需要恢复原作品资格。')
                if state['pending_action'] == 'activate':
                    guards.enter_context(_machine_gate(service.owner.book_id,service.owner.store))
                    if os.path.lexists(service._path(preview['handoff_id'],'activation')):
                        raise RecoverySessionError('启用结果尚未确认，请先明确继续原启用步骤，避免覆盖已生效资格。')
                    # No original core activation intent exists. Its rollback
                    # still proves the machine is restoring the original book.
            if state['pending_action'] is not None:
                if (action == 'cancel' and state['pending_action'] != 'cancel'
                        or action == 'rollback' and state['pending_action'] != 'rollback' and 'preview_handoff' in state['receipts']):
                    state = self._write(intent,state,pending_action=action,
                        pending_input_sha256=expected_state_sha256)
                    self._fault('after_session_pending',state)
                elif not resume or state['pending_action'] != action:
                    raise RecoverySessionError('已有中断步骤，请明确继续原步骤。')
            else:
                if resume:
                    raise RecoverySessionError('此步骤没有原中断意图，不能当作续接。')
                if action == 'rollback':
                    if not any(name in state['receipts'] for name in ('preview_handoff','handoff')) or state['phase'] in {'active','rolled_back','cancelled'}:
                        raise RecoverySessionError('当前没有可以回退的原交接。')
                elif action != 'cancel' and state['phase'] != _NEXT[action][0]:
                    raise RecoverySessionError('恢复步骤与当前阶段不匹配。')
                state = self._write(intent,state,pending_action=action,
                    pending_input_sha256=expected_state_sha256)
                self._fault('after_session_pending',state)
            result = self._execute(intent,state,action,resume)
            snapshot_permit.require()
            self._fault('after_session_operation',state)
            receipts = {**state['receipts'],action:_json_copy(result)}
            terminal = {'rollback':'rolled_back','cancel':'cancelled'}
            state = self._write(intent,state,phase=terminal[action] if action in terminal else _NEXT[action][1],
                pending_action=None,pending_input_sha256=None,last_action=action,
                last_input_sha256=state['pending_input_sha256'],
                last_resume_input_sha256=expected_state_sha256 if resume else None,receipts=receipts)
            self._fault('after_session_completion',state)
            return self._public(intent,state)

    def _execute(self, intent, state, action, resume):
        plan = intent['plan']
        package, target = self._package(intent['archive_id']), Path(plan['root_plan']['container'])
        kwargs = {'temporary_root':self.temporary_root}
        if action == 'cancel':
            return {'cancelled':True,'target_retained':os.path.lexists(target),'write_activation_allowed':False}
        if action == 'materialize':
            # Death before directory creation has no materialization intent yet.
            return materialize_archived_restore(package,plan=plan,
                resume=resume and os.path.lexists(target),**kwargs)
        if action == 'bind_roots':
            return bind_restored_roots(package,destination=target,**kwargs)
        if action == 'prepare_database':
            return prepare_restored_workbench(package,destination=target,**kwargs)
        service = RestoreActivation(plan['book_id'],machine_store=self.machine_store)
        if action == 'preview_handoff':
            return service.preview(package,destination=target,**kwargs)
        preview = state['receipts'].get('preview_handoff')
        if preview is None:
            raise RecoverySessionError('原交接预览丢失，不能补造资格。')
        handoff = preview['handoff_id']
        if action == 'handoff':
            if resume and os.path.lexists(service._path(handoff)):
                return service.resume(package,handoff_id=handoff,**kwargs)
            return service.begin(package,destination=target,expected_preview_sha256=preview['intent_sha256'],**kwargs)
        if action == 'rollback':
            original = Path(preview['source_record']['binding']['database']).parent
            lease = self.maintenance.lease if original == self.maintenance.lease.directory else None
            return service.rollback(handoff_id=handoff,source_lease=lease)
        if action == 'capture_usage':
            method = service.resume_usage if resume and os.path.lexists(service._path(handoff,'usage')) else service.capture
            return method(handoff_id=handoff)
        suffixes = {'prepare_budget':'budget','prepare_jobs':'jobs','prepare_executions':'executions','activate':'activation'}
        exists = os.path.lexists(service._path(handoff,suffixes[action]))
        if action == 'activate':
            return service.activate(package,handoff_id=handoff,resume=resume and exists,**kwargs)
        method = getattr(service,action.replace('prepare_','resume_') if resume and exists else action)
        return method(package,handoff_id=handoff,**kwargs)

    def readback(self, session_id, *, operation_id, snapshot_permit, chapter=None, draft_id=None):
        """Read only the bound target, including generic author drafts."""
        from workbench.instance import InstanceLease
        from workbench.database import WorkbenchDatabase
        from workbench.reviewed_project_views import ProjectDisplayRegistry
        from workbench.jobs import JobStore
        from core.story_project.paths import scan_prose_chapters
        from core.story_project.amendments import read_current_chapter_text
        directory = self._directory(session_id)
        with self._admitted(snapshot_permit,operation_id), persistence_state_lock((directory/'state.json',)):
            intent,state = self._load(session_id)
            if (intent['operation_id'] != operation_id or state['pending_action'] is not None
                    or state['phase'] in {'planned','materialized','roots_bound','rolled_back','cancelled'}):
                raise RecoverySessionError('请先完成原恢复步骤，再核对副本资料。')
            plan = intent['plan']; target = Path(plan['root_plan']['container'])
            with closing(InstanceLease(target).acquire()), persistence_state_lock((target/'book',)):
                database = WorkbenchDatabase(target/'workbench.sqlite',defer_initialization=True,machine_store=self.machine_store)
                scope = database.restore_scope
                if (scope is None or scope.project_id != plan['project_id'] or scope.document['book_id'] != plan['book_id']
                        or scope.document['plan_sha256'] != plan['plan_sha256']):
                    raise RecoverySessionError('恢复资料不属于原会话的目标作品。')
                record = ProjectDisplayRegistry(database,allowed_workspaces=[target/'book']).get(plan['project_id'])
                chapters = []
                for number in sorted(scan_prose_chapters(record.root)):
                    _text,_path,sha,source = read_current_chapter_text(record.root,number)
                    chapters.append({'chapter':number,'sha256':sha,'source':source})
                with database.connection() as connection:
                    drafts = [dict(row) for row in connection.execute(
                        'SELECT id,chapter,revision,state,sha256 FROM drafts WHERE project_id=? ORDER BY chapter,id LIMIT 1001',
                        (plan['project_id'],))]
                if len(drafts)>1000:
                    raise RecoverySessionError('作者草稿数量超过当前核对列表上限。')
                result = {**self._public(intent,state),'chapters':chapters,'author_drafts':drafts}
                if chapter is not None:
                    if type(chapter) is not int or chapter not in {item['chapter'] for item in chapters}:
                        raise RecoverySessionError('请求的正式章节不在当前恢复作品中。')
                    result['chapter_text'] = read_current_chapter_text(record.root,chapter)[0]
                if draft_id is not None:
                    if draft_id not in {item['id'] for item in drafts}:
                        raise RecoverySessionError('请求的作者草稿不在当前恢复作品中。')
                    result['author_draft'] = JobStore(database).get_draft(draft_id).to_dict()
                scope.require_unchanged(database.path)
            return result
