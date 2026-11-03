"""Bind one restored registration and enable only the verified read-only app.

The source DB remains in the archive. An exact SQL delta, including the durable
scope row, is predicted from that DB and committed atomically to the copy.
Historical JSON, job states, drafts, budgets and all other projects are retained.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from core.engine.persistence import atomic_create_json
from workbench.backup_archive import BackupArchiveError, _file_hash, _lineage, _member
from workbench.backup_archive_database import _database
from workbench.backup_archive_verification import _implementation
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_database import _logical_digest
from workbench.backup_files import _ordinary
from workbench.backup_restore_materialization import _json_copy, _validate_plan
from workbench.backup_restore_plan import _plan_archived_restore
from workbench.backup_restore_roots import (_MaterializedCopy, _read, _signed, _verify_bound_copy,
    _require_registry_bindings)
from workbench.instance import InstanceLease
from workbench.restore_scope import _scope_document, _insert_scope, _sidecar, load_restore_scope


class RestoreDatabaseBindingError(BackupArchiveError):
    code = 'restore_database_binding_invalid'


def _connect(path):
    connection = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA trusted_schema=OFF')
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


def _require_plain_schema(connection):
    # Predictions must not silently run user-defined triggers/views or virtual
    # tables. Such a schema is outside the supported workbench write contract.
    for row in connection.execute('SELECT type,sql FROM sqlite_master'):
        if row['type'] in {'view','trigger'} or (row['sql'] or '').lstrip().upper().startswith('CREATE VIRTUAL TABLE'):
            raise RestoreDatabaseBindingError('恢复数据库含不受支持的触发器、视图或虚拟表。')


def _mapped_file(value, copy_, *, relative_allowed=False):
    if not isinstance(value, str) or not value:
        raise RestoreDatabaseBindingError('数据库文件索引不是明确路径。')
    path = Path(value)
    if '..' in path.parts:
        raise RestoreDatabaseBindingError('数据库文件索引包含父目录跳转。')
    if not path.is_absolute():
        if not relative_allowed or path.drive:
            raise RestoreDatabaseBindingError('数据库文件索引缺少原根。')
        _member(path.as_posix())
        if path.as_posix() not in copy_.tree['files']:
            raise RestoreDatabaseBindingError('相对文件索引不在原作品清单中。')
        return value
    for mapping in copy_.layout['roots']:
        source = Path(mapping['source_root'])
        if path.is_relative_to(source):
            name = path.relative_to(source).as_posix()
            tree = copy_.view.manifest['preview']['scope']['inventories'][mapping['root']]
            if name not in tree['files']:
                raise RestoreDatabaseBindingError('数据库文件索引指向未复制的资料。')
            return str(Path(mapping['destination'])/name)
    raise RestoreDatabaseBindingError('数据库文件索引越过归档根。')


def _changes(connection, copy_):
    _require_plain_schema(connection)
    project = copy_.plan['project_id']
    row = connection.execute('SELECT root,book_id FROM projects WHERE id=?', (project,)).fetchone()
    if row is None or tuple(row) != (str(copy_.source), copy_.plan['book_id']):
        raise RestoreDatabaseBindingError('原数据库与待恢复作品登记不一致。')
    changes = [{'table':'projects','column':'root','id':project,'before':str(copy_.source),'after':str(copy_.book)}]
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column, relative in (('drafts','source_path',False),
            ('outline_batches','approval_manifest_path',False), ('amendments','content_path',True)):
        if table not in tables:
            continue
        for row in connection.execute(f'SELECT id,{column} FROM {table} WHERE project_id=? ORDER BY id', (project,)):
            if row[column] in (None, ''):
                continue
            after = _mapped_file(row[column], copy_, relative_allowed=relative)
            if after != row[column]:
                changes.append({'table':table,'column':column,'id':row['id'],'before':row[column],'after':after})
    return changes


def _apply_changes(connection, changes, scope, *, source_provenance=None):
    if not connection.in_transaction:
        raise RestoreDatabaseBindingError('数据库重绑定必须持有写事务。')
    allowed = {('projects','root'),('drafts','source_path'),('outline_batches','approval_manifest_path'),('amendments','content_path')}
    for change in changes:
        table, column = change['table'], change['column']
        if (table,column) not in allowed:
            raise RestoreDatabaseBindingError('恢复请求超出可变索引范围。')
        cursor = connection.execute(f'UPDATE {table} SET {column}=? WHERE id=? AND {column}=?',
            (change['after'],change['id'],change['before']))
        if cursor.rowcount != 1:
            raise RestoreDatabaseBindingError('待重定位的数据库索引已变化。')
    _insert_scope(connection, scope, source_provenance=source_provenance)
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RestoreDatabaseBindingError('恢复重绑定破坏了数据库关系。')


@contextmanager
def _prediction_database(source, temporary_root):
    temporary = tempfile.NamedTemporaryFile(prefix='na-restore-',suffix='.sqlite',dir=temporary_root,delete=False)
    path = Path(temporary.name)
    info = os.fstat(temporary.fileno())
    physical = (info.st_dev,info.st_ino)
    temporary.close()
    try:
        with closing(_connect(path)) as connection:
            source.backup(connection)
            yield connection
    finally:
        info = _ordinary(path)
        if (info.st_dev,info.st_ino) != physical:
            raise RestoreDatabaseBindingError('预测用临时数据库被替换，保留现场。')
        path.unlink()


def _database_intent(copy_, *, temporary_root):
    plan = copy_.plan
    provenance=plan.get('source_restore_provenance')
    scope = _scope_document(copy_.root/'workbench.sqlite', project_id=plan['project_id'], book_id=plan['book_id'],
        book_root=copy_.book, task_runtime_root=copy_.layout['task_runtime_root'],
        plan_sha256=plan['plan_sha256'], archive_receipt_sha256=plan['archive_receipt_sha256'],
        predecessor_scope=provenance['scope'] if provenance else None,
        source_provenance_sha256=provenance['provenance_sha256'] if provenance else None)
    with _database(copy_.view, temporary_root=temporary_root) as source:
        changes = _changes(source, copy_)
        with _prediction_database(source, temporary_root) as predicted:
            if _logical_digest(predicted) != plan['archive_binding']['database_logical_sha256']:
                raise RestoreDatabaseBindingError('预测数据库没有保留完整原始逻辑状态。')
            predicted.execute('BEGIN IMMEDIATE')
            if provenance is None:_apply_changes(predicted, changes, scope)
            else:_apply_changes(predicted,changes,scope,source_provenance=provenance)
            after = _logical_digest(predicted)
            predicted.rollback()
    root_receipt = _read(copy_.root, 'root-binding-done.json')
    value={'schema_version':'restore_database_binding_intent_v1',
        'plan_sha256':plan['plan_sha256'], 'archive_receipt_sha256':plan['archive_receipt_sha256'],
        'root_binding_receipt_sha256':root_receipt['receipt_sha256'],
        'before_logical_sha256':plan['archive_binding']['database_logical_sha256'],
        'after_logical_sha256':after, 'changes':changes, 'scope':scope,
        'write_activation_allowed':False}
    if provenance is not None:value['source_restore_provenance']=provenance
    return _signed(value,'intent_sha256')


def _apply_intent_changes(connection, intent):
    provenance=intent.get('source_restore_provenance')
    if provenance is None:_apply_changes(connection,intent['changes'],intent['scope'])
    else:_apply_changes(connection,intent['changes'],intent['scope'],source_provenance=provenance)


def _state(connection, intent):
    _require_plain_schema(connection)
    if [tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]:
        raise RestoreDatabaseBindingError('待绑定数据库完整性检查失败。')
    current = _logical_digest(connection)
    if current not in {intent['before_logical_sha256'],intent['after_logical_sha256']}:
        raise RestoreDatabaseBindingError('实际数据库既非原快照，也非本次限定的重绑定结果。')
    return current


def _binding_receipt(intent, plan):
    """Reconstruct the immutable receipt for the original root/DB binding step."""
    value={'schema_version':'restore_database_binding_receipt_v1',
        'intent_sha256':intent['intent_sha256'], 'plan_sha256':plan['plan_sha256'],
        'scope_record_sha256':intent['scope']['record_sha256'],
        'database_logical_sha256':intent['after_logical_sha256'],
        'mutable_index_changes':intent['changes'], 'historical_records_rewritten':False,
        'database_rebound':True, 'read_only_app_ready':True,
        'worker_enabled':False, 'auto_import_enabled':False, 'provider_calls_allowed':False,
        'ownership_handoff_completed':False, 'write_activation_allowed':False, 'restored':False,
        'recoverable':False}
    if intent['scope']['schema_version']=='restored_workbench_scope_v2':
        value.update(scope_generation=intent['scope']['scope_generation'],
            predecessor_scope_sha256=intent['scope']['predecessor_scope']['record_sha256'],
            predecessor_scope_preserved=True,
            source_provenance_sha256=intent['scope']['source_provenance_sha256'])
    return _signed(value,'receipt_sha256')


def prepare_restored_workbench(package, *, destination, temporary_root=None, _lease=None, _verify_only=False,
        _usage_overlay=None):
    """Explicit continuation after bind_restored_roots; grants read-only UI only."""
    root = Path(destination).absolute()
    _lineage(root)
    if _usage_overlay is not None:
        from workbench.restore_budget_preparation import _BudgetOverlay
        if type(_usage_overlay) is not _BudgetOverlay or not _verify_only or _lease is None:
            raise RestoreDatabaseBindingError('预算变更只能在原交接持锁的只核验路径使用。')
    if _verify_only:
        for name in ('database-binding.json', 'database-binding-done.json', 'workbench.sqlite.restore-scope.json'):
            _read(root, name)
        if any(os.path.lexists(root/('workbench.sqlite'+suffix)) for suffix in ('-journal','-wal','-shm')):
            raise RestoreDatabaseBindingError('交接核验只接受已完成且关闭的独立恢复数据库。')
    saved = _validate_plan(_read(root, 'restore-plan.json'))
    if str(root) != saved['root_plan']['container']:
        raise RestoreDatabaseBindingError('恢复计划属于其他容器。')
    fresh = _plan_archived_restore(package, project_id=saved['project_id'], book_id=saved['book_id'],
        destination=root, temporary_root=temporary_root, _existing=True)
    if _json_copy(fresh) != saved:
        raise RestoreDatabaseBindingError('原归档、实现版本或恢复位置已变化。')
    if _lease is not None:
        if not isinstance(_lease, InstanceLease) or _lease.directory != root:
            raise RestoreDatabaseBindingError('恢复核验的实例锁不属于当前容器。')
        _lease.require_held()
    lease = _lease if _lease is not None else InstanceLease(root).acquire()
    try:
        with verified_archive_view(package) as view:
            if (view.verified['receipt_sha256'] != saved['archive_receipt_sha256']
                    or _file_hash(Path(package).absolute()/'archive.zip') != saved['archive_binding']['archive']):
                raise RestoreDatabaseBindingError('数据库绑定前归档变化。')
            copy_ = _MaterializedCopy(root, saved, view, _usage_overlay=_usage_overlay)
            intent = _database_intent(copy_, temporary_root=temporary_root)
            path = root/'database-binding.json'
            existed = os.path.lexists(path)
            if _verify_only and not existed:
                raise RestoreDatabaseBindingError('交接期间数据库绑定意图丢失。')
            if existed and _read(root, path.name) != intent:
                raise RestoreDatabaseBindingError('数据库重绑定意图或物理资格变化。')
            database = root/'workbench.sqlite'
            marker = _sidecar(database)
            extra = {'database-binding.json','database-binding-done.json',marker.name}
            if os.path.lexists(marker) and _read(root, marker.name) != intent['scope']:
                raise RestoreDatabaseBindingError('已有只读范围凭据与本次重绑定不一致。')
            # Explicit resume may let SQLite roll back a hot journal left by
            # this operation. Ordinary read-only app startup cannot do that.
            if not existed and _file_hash(database) != view.manifest['members']['workbench.sqlite']:
                raise RestoreDatabaseBindingError('首次数据库重绑定的副本已经偏离原字节。')
            journal = database.with_name(database.name+'-journal')
            if os.path.lexists(journal):
                # Before SQLite can recover a journal, reject links and other
                # nonordinary files. Only our durable intent permits recovery.
                if not existed:
                    raise RestoreDatabaseBindingError('首次重绑定不能带入未登记的数据库日志。')
                _ordinary(journal)
            if any(os.path.lexists(database.with_name(database.name+suffix)) for suffix in ('-wal','-shm')):
                raise RestoreDatabaseBindingError('恢复重绑定只接受独立数据库及本次回滚日志。')
            with closing(_connect(database)) as connection:
                current = _state(connection, intent)
                if _verify_only and current != intent['after_logical_sha256']:
                    raise RestoreDatabaseBindingError('交接不能代替未完成的数据库重绑定。')
                pre_write_extra = extra if existed else set()
                if existed and current == intent['before_logical_sha256']:
                    # A crash before SQLite's first flush can leave a non-hot
                    # journal that a read does not remove. The exact original
                    # SQL state is proved above; our commit must remove it.
                    pre_write_extra = {*pre_write_extra, journal.name}
                root_receipt = _verify_bound_copy(copy_, database_record=_file_hash(database),
                    extra_files=pre_write_extra)
                if root_receipt['receipt_sha256'] != intent['root_binding_receipt_sha256']:
                    raise RestoreDatabaseBindingError('数据库意图未绑定当前已核验的物理根。')
                if not existed:
                    atomic_create_json(path, intent)
                if _read(root, path.name) != intent:
                    raise RestoreDatabaseBindingError('数据库意图发布后变化。')
                if current == intent['before_logical_sha256']:
                    connection.execute('BEGIN IMMEDIATE')
                    try:
                        if _state(connection, intent) != current:
                            raise RestoreDatabaseBindingError('取得写事务前数据库变化。')
                        _apply_intent_changes(connection,intent)
                        if _state(connection, intent) != intent['after_logical_sha256']:
                            raise RestoreDatabaseBindingError('实际重绑定结果偏离限定的 SQL 变更。')
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                if _state(connection, intent) != intent['after_logical_sha256']:
                    raise RestoreDatabaseBindingError('数据库尚未完成目标重绑定。')
            if not os.path.lexists(marker):
                if _verify_only:
                    raise RestoreDatabaseBindingError('交接期间恢复只读凭据丢失。')
                atomic_create_json(marker, intent['scope'])
            if _read(root, marker.name) != intent['scope']:
                raise RestoreDatabaseBindingError('只读范围凭据发布后变化。')
            loaded_scope = load_restore_scope(database)
            if loaded_scope is None or loaded_scope.document != intent['scope']:
                raise RestoreDatabaseBindingError('恢复工作台未建立一致的只读限制。')
            database_record = _file_hash(database)
            _verify_bound_copy(copy_, database_record=database_record, extra_files=extra)
            root_intent = _read(root, 'root-binding.json')
        if _implementation()['fingerprint_sha256'] != saved['verifier']['implementation_sha256']:
            raise RestoreDatabaseBindingError('数据库重绑定期间实现版本变化。')
        result = _binding_receipt(intent, saved)
        done = root/'database-binding-done.json'
        if os.path.lexists(done):
            if _read(root, done.name) != result:
                raise RestoreDatabaseBindingError('数据库重绑定完成凭据不一致。')
        else:
            if _verify_only:
                raise RestoreDatabaseBindingError('交接期间数据库完成凭据丢失。')
            atomic_create_json(done, result)
        replacements = {(Path('book')/item['path']).as_posix():item['after'] for item in root_intent['registries']}
        replacements['workbench.sqlite'] = database_record
        copy_.verify(replacements, {'root-binding.json','root-binding-done.json', *extra})
        _require_registry_bindings(root_intent)
        if (_read(root, path.name) != intent or _read(root, done.name) != result
                or load_restore_scope(database).document != intent['scope']):
            raise RestoreDatabaseBindingError('数据库恢复控制资料在发布后变化。')
        return result
    finally:
        if _lease is None:
            lease.close()
