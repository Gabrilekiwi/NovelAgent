"""Durable restrictions for a restored database before write activation.

The complete database is retained. This module grants no restore or write
qualification: it restricts an already prepared copy to one registered book.
"""
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3

from core.engine.persistence import atomic_create_json, PersistenceLockBusy
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive import _lineage as _archive_lineage
from workbench.backup_files import _ordinary


class RestoreScopeError(RuntimeError):
    code = 'restore_scope_invalid'


class RestoreScopeBlocked(RestoreScopeError):
    code = 'restore_scope_read_only'


class RestoreScopeBusy(RestoreScopeError):
    code = 'restore_scope_busy'


_SCOPE_FIELDS = {'schema_version', 'state', 'project_id', 'book_id', 'book_root', 'task_runtime_root',
    'database', 'database_identity', 'database_parent_binding', 'book_binding',
    'plan_sha256', 'archive_receipt_sha256', 'record_sha256'}
_LINEAGE_FIELDS = {'scope_generation', 'predecessor_scope', 'source_provenance_sha256'}


def validate_scope_document(value, *, depth=0):
    """Validate retained scope ancestry without opening any historical path."""
    try:
        if depth>16 or len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>1024**2:
            raise RestoreScopeError('恢复范围来源链超过支持大小。')
        if not isinstance(value,dict):raise RestoreScopeError('恢复范围必须是对象。')
        version=value.get('schema_version')
        fields=_SCOPE_FIELDS | (_LINEAGE_FIELDS if version=='restored_workbench_scope_v2' else set())
        if (set(value)!=fields or version not in {'restored_workbench_scope_v1','restored_workbench_scope_v2'}
                or value['state']!='restoring'
                or value['record_sha256']!=digest({k:v for k,v in value.items() if k!='record_sha256'})):
            raise RestoreScopeError('恢复范围格式、状态或摘要无效。')
        for key in ('plan_sha256','archive_receipt_sha256'):
            if not isinstance(value[key],str) or re.fullmatch('[0-9a-f]{64}',value[key]) is None:
                raise RestoreScopeError('恢复范围缺少计划或归档绑定。')
        if version=='restored_workbench_scope_v1':return 1
        previous=value['predecessor_scope']
        generation=validate_scope_document(previous,depth=depth+1)
        if (type(value['scope_generation']) is not int or value['scope_generation']!=generation+1
                or not isinstance(value['source_provenance_sha256'],str)
                or re.fullmatch('[0-9a-f]{64}',value['source_provenance_sha256']) is None
                or any(value[key]!=previous[key] for key in ('project_id','book_id'))
                or value['database']==previous['database'] or value['book_root']==previous['book_root']):
            raise RestoreScopeError('恢复范围代际或原目标绑定无效。')
        return value['scope_generation']
    except (KeyError,TypeError,ValueError) as exc:
        raise RestoreScopeError('恢复范围来源链无法核验。') from exc


def _lineage(path):
    return {name:list(identity) for name,identity in _archive_lineage(path).items()}


def _sidecar(path):
    return path.with_name(path.name + '.restore-scope.json')


def _physical(path):
    info = _ordinary(path)
    return {'device': info.st_dev, 'inode': info.st_ino}


def _connection(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def _stored(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workbench_restore_scope'").fetchone()
    if exists is None:
        return None
    rows = connection.execute('SELECT singleton,document_json FROM workbench_restore_scope LIMIT 2').fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or len(rows[0][1]) > 1024**2:
        raise RestoreScopeError('恢复范围登记缺失或不明确。')
    return json.loads(rows[0][1])


@dataclass(frozen=True)
class RestoreScope:
    document: dict
    activation: dict | None = None
    machine_store: object = None
    controller: dict | None = None

    @property
    def active(self):
        return self.activation is not None

    @property
    def read_only(self):
        return not self.active

    @property
    def project_id(self):
        return self.document['project_id']

    @property
    def book_root(self):
        return Path(self.document['book_root'])

    @property
    def task_runtime_root(self):
        if self.active: return Path(self.activation['fresh_task_runtime_root'])
        return Path(self.document['task_runtime_root'])

    def require_project(self, project_id):
        if project_id != self.project_id:
            raise RestoreScopeBlocked('此恢复副本只包含目标作品，其他登记作品不可访问。')

    def require_unchanged(self, path):
        current = load_restore_scope(path,machine_store=self.machine_store,allow_recovery_controller=self.controller is not None)
        if (current is None or current.document != self.document or current.activation != self.activation
                or current.controller != self.controller):
            raise RestoreScopeError('恢复范围或物理绑定发生变化，请停止当前实例。')

    def view(self):
        return {'state':'source_controller' if self.controller else 'active' if self.active else 'restoring',
            'read_only':self.read_only,'project_id':self.project_id,'controller':self.controller,
            'message':'原副本已交出写资格；当前只能继续原恢复会话、核对或打开目标副本。' if self.controller else
                '恢复副本已启用，只访问目标作品。' if self.active else '恢复副本尚未启用，当前只供核对资料。'}


def load_restore_scope(database_path, *, machine_store=None, allow_recovery_controller=False):
    """Probe before schema initialization, project loading or worker construction."""
    path = Path(database_path).absolute()
    marker = _sidecar(path)
    in_progress = os.path.lexists(path.parent / 'restore-intent.json')
    if not os.path.lexists(path):
        if os.path.lexists(marker) or in_progress:
            raise RestoreScopeError('恢复尚未完成，数据库不能作为普通工作台启动。')
        return None
    try:
        with closing(_connection(path)) as connection:
            value = _stored(connection)
            if value is None:
                if os.path.lexists(marker) or in_progress:
                    raise RestoreScopeError('恢复范围登记不完整，拒绝普通启动。')
                retained=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
                    "('workbench_restored_job_history','workbench_restored_job_generations')").fetchone()
                if retained is not None:
                    raise RestoreScopeError('数据库保留恢复任务历史，但恢复范围已丢失，拒绝普通启动。')
                from workbench.restore_qualification import require_no_lost_activation
                require_no_lost_activation(connection, path, machine_store=machine_store)
                return None
            if not os.path.lexists(marker) or _ordinary(marker).st_size > 1024**2:
                raise RestoreScopeError('恢复范围的独立凭据缺失。')
            external = json.loads(read_project_bytes(marker.parent, marker.name))
            validate_scope_document(value)
            if (external != value
                    or value['database'] != str(path) or value['database_identity'] != _physical(path)
                    or value['database_parent_binding'] != _lineage(path.parent)):
                raise RestoreScopeError('恢复范围、状态或数据库物理绑定无效。')
            for key in ('plan_sha256', 'archive_receipt_sha256'):
                if not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key]):
                    raise RestoreScopeError('恢复范围缺少计划或归档绑定。')
            root = Path(value['book_root'])
            runtime = Path(value['task_runtime_root'])
            if (not root.is_absolute() or '..' in root.parts or not runtime.is_absolute() or '..' in runtime.parts
                    or value['book_binding'] != _lineage(root)):
                raise RestoreScopeError('恢复作品的目录绑定无效。')
            row = connection.execute('SELECT book_id,root FROM projects WHERE id=?', (value['project_id'],)).fetchone()
            if row != (value['book_id'], str(root)):
                raise RestoreScopeError('恢复范围与作品登记不一致。')
            if value['schema_version']=='restored_workbench_scope_v2' or in_progress:
                from workbench.backup_restore_roots import _read
                from workbench.backup_restore_materialization import _validate_plan
                plan=_validate_plan(_read(path.parent,'restore-plan.json'))
                if (plan['plan_sha256']!=value['plan_sha256']
                        or plan['archive_receipt_sha256']!=value['archive_receipt_sha256']
                        or plan['project_id']!=value['project_id'] or plan['book_id']!=value['book_id']):
                    raise RestoreScopeError('恢复范围与原计划不同。')
                provenance=plan.get('source_restore_provenance')
                if (provenance is not None)!=(value['schema_version']=='restored_workbench_scope_v2'):
                    raise RestoreScopeError('恢复范围代际不能降级或补造来源。')
                if provenance is not None:
                    from workbench.backup_restore_provenance import validate_restore_provenance
                    intent=_read(path.parent,'database-binding.json')
                    validate_restore_provenance(provenance,root=Path(value['predecessor_scope']['book_root']),
                        book_id=value['book_id'])
                    if (intent.get('intent_sha256')!=digest({k:v for k,v in intent.items() if k!='intent_sha256'})
                            or intent.get('scope')!=value or intent.get('source_restore_provenance')!=provenance
                            or provenance['scope']!=value['predecessor_scope']
                            or provenance['provenance_sha256']!=value['source_provenance_sha256']):
                        raise RestoreScopeError('恢复新代际范围与原数据库意图不一致。')
            from workbench.restore_qualification import load_activation
            controller = None
            try:
                activation = load_activation(value,machine_store=machine_store)
            except PersistenceLockBusy:
                raise
            except (RestoreScopeError,OSError,ValueError,KeyError,RuntimeError):
                if not allow_recovery_controller:
                    raise
                from workbench.recovery_controller import load_recovery_controller
                controller = load_recovery_controller(value,machine_store=machine_store)
                if controller is None:
                    raise
                activation = None
            if allow_recovery_controller and activation is None and controller is None:
                from workbench.recovery_controller import load_recovery_controller
                controller = load_recovery_controller(value,machine_store=machine_store)
            return RestoreScope(value,activation,machine_store,controller)
    except RestoreScopeError:
        raise
    except PersistenceLockBusy as exc:
        raise RestoreScopeBusy('其他本机操作正在核对写资格，请稍后重试；当前未改变副本资格。') from exc
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
        raise RestoreScopeError('恢复范围无法核验，已停止启动或访问。') from exc


def _scope_document(database_path, *, project_id, book_id, book_root, task_runtime_root,
                    plan_sha256, archive_receipt_sha256, predecessor_scope=None, source_provenance_sha256=None):
    path = Path(database_path).absolute()
    root = Path(book_root).absolute()
    value = {'schema_version':'restored_workbench_scope_v1', 'state':'restoring',
             'project_id':project_id, 'book_id':book_id, 'book_root':str(root),
             'task_runtime_root':str(Path(task_runtime_root).absolute()), 'database':str(path),
             'database_identity':_physical(path), 'database_parent_binding':_lineage(path.parent),
             'book_binding':_lineage(root), 'plan_sha256':plan_sha256,
             'archive_receipt_sha256':archive_receipt_sha256}
    if predecessor_scope is not None:
        generation=validate_scope_document(predecessor_scope)
        value.update(schema_version='restored_workbench_scope_v2',scope_generation=generation+1,
            predecessor_scope=json.loads(json.dumps(predecessor_scope)),
            source_provenance_sha256=source_provenance_sha256)
    value['record_sha256'] = digest(value)
    validate_scope_document(value)
    return value


def _insert_scope(connection, value, *, source_provenance=None):
    if not connection.in_transaction:
        raise RestoreScopeError('恢复范围必须在独占事务中首次登记。')
    validate_scope_document(value)
    previous=_stored(connection)
    if previous is None:
        if source_provenance is not None or value['schema_version']!='restored_workbench_scope_v1':
            raise RestoreScopeError('恢复范围原代际缺失，不能补造来源。')
    else:
        if source_provenance is None or value['schema_version']!='restored_workbench_scope_v2':
            raise RestoreScopeError('恢复范围必须在独占事务中首次登记。')
        from workbench.backup_restore_provenance import validate_restore_provenance
        validate_restore_provenance(source_provenance,root=Path(previous['book_root']),book_id=previous['book_id'])
        if (source_provenance['scope']!=previous or value['predecessor_scope']!=previous
                or value['source_provenance_sha256']!=source_provenance['provenance_sha256']):
            raise RestoreScopeError('恢复范围原登记与归档来源证据不同。')
    row = connection.execute('SELECT book_id,root FROM projects WHERE id=?', (value['project_id'],)).fetchone()
    if row is None or tuple(row) != (value['book_id'], value['book_root']):
        raise RestoreScopeError('只能限制已完成目标重绑定的数据库副本。')
    if previous is None:
        connection.execute('CREATE TABLE workbench_restore_scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), document_json TEXT NOT NULL)')
        connection.execute('INSERT INTO workbench_restore_scope VALUES (1,?)', (json.dumps(value, ensure_ascii=False, sort_keys=True),))
    else:
        original=connection.execute('SELECT document_json FROM workbench_restore_scope WHERE singleton=1').fetchone()[0]
        cursor=connection.execute('UPDATE workbench_restore_scope SET document_json=? WHERE singleton=1 AND document_json=?',
            (json.dumps(value,ensure_ascii=False,sort_keys=True),original))
        if cursor.rowcount!=1:raise RestoreScopeError('恢复范围在换代前发生变化。')


def install_restore_restriction(database_path, *, project_id, book_id, book_root, task_runtime_root,
                                plan_sha256, archive_receipt_sha256):
    """Restrict a prepared copy; never activate it or rewrite original book data.

    The restore executor must publish restore-intent.json before copying its DB.
    A crash between the DB row and sidecar remains a blocked startup.
    """
    path = Path(database_path).absolute()
    value = _scope_document(path, project_id=project_id, book_id=book_id, book_root=book_root,
        task_runtime_root=task_runtime_root, plan_sha256=plan_sha256, archive_receipt_sha256=archive_receipt_sha256)
    if os.path.lexists(_sidecar(path)):
        raise RestoreScopeError('恢复范围已经存在，不能覆盖。')
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _insert_scope(connection, value)
        connection.commit()
    atomic_create_json(_sidecar(path), value)
    return load_restore_scope(path)


def install_restore_scope_api(app, database):
    """Check URL identities before a handler can read another book's files."""
    from starlette.responses import JSONResponse
    scope = database.restore_scope
    if scope is None:
        return
    entities = {'jobs':'jobs', 'drafts':'drafts', 'candidate-drafts':'chapter_candidate_drafts',
                'fact-reviews':'chapter_fact_reviews', 'outline-batches':'outline_batches',
                'cohorts':'cohorts'}

    @app.middleware('http')
    async def restrict_restore(request, call_next):
        if not request.url.path.startswith('/api/'):
            return await call_next(request)
        try:
            current = load_restore_scope(database.path,machine_store=scope.machine_store,allow_recovery_controller=True)
            if current is None or current.document != scope.document:
                raise RestoreScopeError('恢复范围或物理绑定发生变化，请停止当前实例。')
            request.state.restore_scope = current
            parts = request.url.path.strip('/').split('/')
            if current.controller is not None or scope.controller is not None:
                control = current.controller or scope.controller
                if current.controller is None:
                    # A controller restarted while revoked cannot refresh all
                    # constructed services in place after rollback. Keep the
                    # original controls until a normal startup observes it.
                    request.state.restore_scope = RestoreScope(current.document,None,current.machine_store,control)
                    request.state.restore_restart_required = True
                metadata = request.method=='GET' and parts in [['api',name] for name in
                    ('instance','bootstrap','maintenance','restore-scope','recovery-sessions')]
                session = (len(parts)>=3 and parts[:2]==['api','recovery-sessions']
                    and parts[2]==control['session_id'] and (
                        len(parts)==3 and request.method=='GET'
                        or request.method=='POST' and (len(parts)==4 and parts[3] in {'readback','open'}
                            or len(parts)==5 and parts[3]=='steps')))
                transition = request.method=='POST' and len(parts)==3 and parts[:2]==['api','maintenance'] and parts[2] in {'resume','finish'}
                if not (metadata or session or transition):
                    raise RestoreScopeBlocked('原副本已交出写资格，只能继续原恢复会话。')
                if parts==['api','restore-scope']:
                    return JSONResponse(request.state.restore_scope.view())
                return await call_next(request)
            scope.require_unchanged(database.path)
            if scope.read_only and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                raise RestoreScopeBlocked('恢复副本尚未启用，当前只供核对资料。')
            parts = request.url.path.strip('/').split('/')
            if parts == ['api', 'restore-scope']:
                return JSONResponse(current.view())
            if len(parts) >= 3 and parts[1] == 'projects':
                scope.require_project(parts[2])
            elif len(parts) >= 3 and parts[1] in entities:
                with database.connection() as connection:
                    row = connection.execute(f'SELECT project_id FROM {entities[parts[1]]} WHERE id=?', (parts[2],)).fetchone()
                if row is None:
                    raise RestoreScopeBlocked('此记录不在当前恢复作品中。')
                scope.require_project(row['project_id'])
            elif scope.active and (
                parts[:2]==['api','recovery-sessions']
                or
                parts==['api','backups'] and request.method=='GET'
                or len(parts)==3 and parts[:2]==['api','maintenance']
                    and parts[2] in {'begin','resume','finish'} and request.method=='POST'
                or len(parts)==4 and parts[:2]==['api','backups']
                    and parts[3]=='verify' and request.method=='POST'):
                # These controls operate only on this instance. Verification
                # additionally checks the archive's target in backup_api.
                pass
            elif parts not in [['api', name] for name in ('instance', 'bootstrap', 'projects', 'maintenance')]:
                raise RestoreScopeBlocked('恢复核对期间此入口不可用。')
            elif scope.active and request.method not in {'GET','HEAD','OPTIONS'} and parts==['api','projects']:
                raise RestoreScopeBlocked('此恢复实例只允许当前目标作品，不能新建或导入其他作品。')
            supplied = request.query_params.get('project_id')
            if supplied is not None:
                scope.require_project(supplied)
            return await call_next(request)
        except RestoreScopeError as exc:
            return JSONResponse({'error':{'code':exc.code, 'message':str(exc)}}, status_code=409)
