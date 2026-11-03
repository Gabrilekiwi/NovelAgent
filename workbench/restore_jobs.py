"""Invalidate imported workbench jobs with a proved, atomic whole-DB delta.

Original rows are retained as exact JSON snapshots; config, results, errors and
events are not rewritten. This step never grants ownership or resumes jobs.
Candidate/fact/publication plans and fresh execution roots are separate gates.
"""
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from core.engine.persistence import atomic_create_json, persistence_state_lock
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import _file_hash
from workbench.backup_archive_database import _database
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_database import _logical_digest
from workbench.backup_files import _ordinary
from workbench.backup_restore_database import (_connect, _database_intent, _prediction_database,
    _apply_changes, _apply_intent_changes, _require_plain_schema, _binding_receipt)
from workbench.backup_restore_materialization import _validate_plan, _json_copy
from workbench.backup_restore_plan import _plan_archived_restore
from workbench.backup_restore_roots import _MaterializedCopy, _read, _verify_bound_copy
from workbench.instance import InstanceLease
from workbench.restore_budget_preparation import RestoreBudgetPreparation, _BudgetOverlay, _CAPABILITY
from workbench.restore_handoff import RestoreHandoffError, _signed
from workbench.restore_scope import _sidecar, _stored, load_restore_scope
from workbench.restore_job_history import (GENERATION_TABLE, RestoreJobHistoryError,
    validate_retained_tables, validate_job_intent)


INVALIDATED_CONTRACT = 'restored_job_history_v1'
_TERMINAL = {'completed','cancelled','failed'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False)


def _job_history(connection):
    rows = [dict(row) for row in connection.execute('SELECT * FROM jobs ORDER BY id LIMIT 10001')]
    if len(rows) > 10000 or len(_json(rows).encode('utf-8')) > 16*1024**2:
        raise RestoreHandoffError('恢复任务历史超过本次支持大小，不截断任务记录。')
    return [{'id':row['id'], 'original':row, 'original_sha256':digest(row)} for row in rows]


def _event_history(connection):
    rows=[dict(row) for row in connection.execute('SELECT * FROM job_events ORDER BY id LIMIT 100001')]
    if len(rows)>100000 or len(_json(rows).encode('utf-8'))>16*1024**2:
        raise RestoreHandoffError('恢复任务事件超过支持大小，不截断原事件。')
    return rows


def _apply_jobs(connection, history, handoff_id, created_at):
    if not connection.in_transaction:
        raise RestoreHandoffError('任务失效必须在独占数据库事务中执行。')
    connection.execute('CREATE TABLE workbench_restored_job_history '
        '(job_id TEXT PRIMARY KEY, handoff_id TEXT NOT NULL, original_row_json TEXT NOT NULL, row_sha256 TEXT NOT NULL)')
    for item in history:
        old = item['original']
        connection.execute('INSERT INTO workbench_restored_job_history VALUES (?,?,?,?)',
            (item['id'], handoff_id, _json(old), item['original_sha256']))
        # Terminal history keeps its state. Every imported job loses execution
        # compatibility; nonterminal jobs cannot occupy the new active slot.
        state = old['state'] if old['state'] in _TERMINAL else 'cancelled'
        cursor = connection.execute('UPDATE jobs SET execution_contract=?,state=?,updated_at=?,revision=revision+1 '
            'WHERE id=? AND execution_contract IS ? AND state=? AND revision=?',
            (INVALIDATED_CONTRACT, state, created_at, item['id'], old['execution_contract'], old['state'], old['revision']))
        if cursor.rowcount != 1:
            raise RestoreHandoffError('失效前原任务发生变化。')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RestoreHandoffError('任务失效破坏了数据库关系。')


def _apply_job_intent(connection, intent):
    if intent['schema_version'] == 'restore_job_preparation_v1':
        return _apply_jobs(connection, intent['history'], intent['handoff_id'], intent['created_at'])
    if intent['schema_version'] != 'restore_job_preparation_v2' or not connection.in_transaction:
        raise RestoreHandoffError('新代际任务失效必须属于明确意图和独占事务。')
    connection.execute('CREATE TABLE IF NOT EXISTS '+GENERATION_TABLE+' '
        '(scope_generation INTEGER NOT NULL, job_id TEXT NOT NULL, handoff_id TEXT NOT NULL, '
        'original_row_json TEXT NOT NULL, row_sha256 TEXT NOT NULL, PRIMARY KEY(scope_generation,job_id))')
    for item in intent['history']:
        old = item['original']
        connection.execute('INSERT INTO '+GENERATION_TABLE+' VALUES (?,?,?,?,?)',
            (intent['scope_generation'], item['id'], intent['handoff_id'], _json(old), item['original_sha256']))
        state = old['state'] if old['state'] in _TERMINAL else 'cancelled'
        cursor = connection.execute('UPDATE jobs SET execution_contract=?,state=?,updated_at=?,revision=revision+1 '
            'WHERE id=? AND execution_contract IS ? AND state=? AND revision=?',
            (INVALIDATED_CONTRACT, state, intent['created_at'], item['id'], old['execution_contract'], old['state'], old['revision']))
        if cursor.rowcount != 1:
            raise RestoreHandoffError('新代际失效前原任务发生变化。')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RestoreHandoffError('新代际任务失效破坏了数据库关系。')


class RestoreJobPreparation(RestoreBudgetPreparation):
    def _job_intent(self, value, budget_receipt, copy_, base, created_at, temporary_root):
        try:
            instant = datetime.fromisoformat(created_at)
            if instant.utcoffset() != timezone.utc.utcoffset(instant): raise ValueError('UTC required')
        except (TypeError, ValueError) as exc:
            raise RestoreHandoffError('任务准备时间必须是明确的 UTC 时间。') from exc
        with _database(copy_.view, temporary_root=temporary_root) as original:
            with _prediction_database(original, temporary_root) as predicted:
                predicted.execute('BEGIN IMMEDIATE')
                _apply_intent_changes(predicted,base)
                if _logical_digest(predicted) != base['after_logical_sha256']:
                    raise RestoreHandoffError('原数据库绑定预测不同。')
                history = _job_history(predicted)
                events = _event_history(predicted)
                inherited = base.get('source_restore_provenance')
                lineage = {}
                schema = 'restore_job_preparation_v1'
                if inherited is not None:
                    try: validate_retained_tables(predicted, inherited)
                    except RestoreJobHistoryError as exc: raise RestoreHandoffError(str(exc)) from exc
                    schema = 'restore_job_preparation_v2'
                    lineage = {'scope_generation':base['scope']['scope_generation'],
                        'source_provenance_sha256':inherited['provenance_sha256']}
                elif predicted.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (GENERATION_TABLE,)).fetchone():
                    raise RestoreHandoffError('首次恢复不能采用未绑定的旧任务代际表。')
                changes = {'schema_version':schema, 'history':history, 'handoff_id':value['handoff_id'],
                    'created_at':created_at, **lineage}
                _apply_job_intent(predicted, changes)
                after = _logical_digest(predicted)
                predicted.rollback()
        intent = _signed({'schema_version':schema, **self._binding_fields(value), **lineage,
            'plan_sha256':value['plan_sha256'],'archive_receipt_sha256':value['archive_receipt_sha256'],
            'budget_receipt_sha256':budget_receipt['receipt_sha256'], 'created_at':created_at,
            'base_database_intent_sha256':base['intent_sha256'],
            'before_logical_sha256':base['after_logical_sha256'], 'after_logical_sha256':after,
            'history':history, 'event_history':events,'event_history_sha256':digest(events),
            'invalidated_contract':INVALIDATED_CONTRACT}, 'intent_sha256')
        try: validate_job_intent(intent, base['scope'], inherited)
        except RestoreJobHistoryError as exc: raise RestoreHandoffError(str(exc)) from exc
        return intent

    def _require_job_intent(self, value, intent):
        self.owner._held()
        path = self._path(value['handoff_id'], 'jobs')
        if self._pending(value['handoff_id']) != value or _read(path.parent, path.name) != intent:
            raise RestoreHandoffError('原交接或任务准备意图发生变化。')
        budget_path = self._path(value['handoff_id'], 'budget')
        budget = _read(budget_path.parent, budget_path.name)
        receipt = self._budget_receipt(value, budget)
        if (budget != _signed({k:v for k,v in budget.items() if k != 'intent_sha256'}, 'intent_sha256')
                or receipt['receipt_sha256'] != intent['budget_receipt_sha256']
                or _read(path.parent, self._path(value['handoff_id'], 'budget-done').name) != receipt):
            raise RestoreHandoffError('任务准备所依赖的预算完成凭据变化。')

    def _verify_copy(self, copy_, connection, base, intent, *, complete):
        _require_plain_schema(connection)
        if [tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]:
            raise RestoreHandoffError('恢复任务数据库完整性检查失败。')
        current = _logical_digest(connection)
        allowed = {intent['after_logical_sha256']} if complete else {
            intent['before_logical_sha256'], intent['after_logical_sha256']}
        if current not in allowed:
            raise RestoreHandoffError('实际数据库超出原绑定及限定的任务失效变更。')
        database = copy_.root/'workbench.sqlite'; marker = _sidecar(database)
        if (_read(copy_.root, 'database-binding.json') != base
                or _read(copy_.root, 'database-binding-done.json') != _binding_receipt(base, copy_.plan)
                or _stored(connection) != base['scope'] or _read(copy_.root, marker.name) != base['scope']):
            raise RestoreHandoffError('任务准备不能改写原数据库绑定或只读范围凭据。')
        extras = {'database-binding.json','database-binding-done.json',marker.name}
        journal = database.with_name(database.name+'-journal')
        if os.path.lexists(journal):
            _ordinary(journal)
            if complete: raise RestoreHandoffError('已完成任务准备仍存在数据库日志。')
            extras.add(journal.name)
        # The whole current SQL digest was proved above. File verification still
        # checks every archived byte plus the completed budget preparation.
        _verify_bound_copy(copy_, database_record=_file_hash(database), extra_files=extras)
        return current

    def _jobs_receipt(self, value, intent):
        lineage = {}
        schema = 'restore_jobs_prepared_v1'
        if intent['schema_version'] == 'restore_job_preparation_v2':
            schema = 'restore_jobs_prepared_v2'
            lineage = {'scope_generation':intent['scope_generation'],
                'source_provenance_sha256':intent['source_provenance_sha256'], 'prior_generations_preserved':True}
        if 'event_history' in intent:
            lineage.update(original_event_count=len(intent['event_history']),original_events_preserved=True,
                event_history_sha256=intent['event_history_sha256'])
        return _signed({'schema_version':schema, **self._binding_fields(value), **lineage,
            'jobs_intent_sha256':intent['intent_sha256'], 'database_logical_sha256':intent['after_logical_sha256'],
            'original_job_count':len(intent['history']), 'original_rows_preserved':True,
            'workbench_jobs_invalidated':True, 'prepared_candidate_plans_invalidated':False,
            'fresh_execution_roots_ready':False, 'write_activation_allowed':False,
            'handoff_completed':False}, 'receipt_sha256')

    def _jobs(self, package, *, handoff_id, temporary_root, resume):
        with self.owner.locked():
            value = self._pending(handoff_id); root = Path(value['target'])
            with ExitStack() as budget_context:
                budget_intent, source = budget_context.enter_context(self._budget_context(package, value))
                budget_receipt = self._budget_receipt(value, budget_intent)
                if (_read(self.owner.directory, self._path(handoff_id, 'budget').name) != budget_intent
                        or _read(self.owner.directory, self._path(handoff_id, 'budget-done').name) != budget_receipt):
                    raise RestoreHandoffError('任务失效前必须完成原预算准备。')
                saved = _validate_plan(_read(root, 'restore-plan.json'))
                fresh = _plan_archived_restore(package, project_id=saved['project_id'], book_id=saved['book_id'],
                    destination=root, temporary_root=temporary_root, _existing=True)
                if _json_copy(fresh) != saved or saved['plan_sha256'] != value['plan_sha256']:
                    raise RestoreHandoffError('任务准备的归档、实现版本或恢复位置变化。')
                with closing(InstanceLease(root).acquire()), persistence_state_lock((root/'book',)):
                    with verified_archive_view(package) as view:
                        overlay = _BudgetOverlay(self, value, budget_intent, source, _capability=_CAPABILITY)
                        try:
                            copy_ = _MaterializedCopy(root, saved, view, _usage_overlay=overlay)
                            base = _database_intent(copy_, temporary_root=temporary_root)
                            path = self._path(handoff_id, 'jobs'); done = self._path(handoff_id, 'jobs-done')
                            exists = os.path.lexists(path)
                            if exists and not resume and not os.path.lexists(done):
                                raise RestoreHandoffError('原任务准备已开始，请显式续接。')
                            if not exists and (resume or os.path.lexists(done)):
                                raise RestoreHandoffError('任务准备意图丢失，不能补造。')
                            created = _read(path.parent, path.name).get('created_at') if exists else datetime.now(timezone.utc).isoformat()
                            intent = self._job_intent(value, budget_receipt, copy_, base, created, temporary_root)
                            if exists and _read(path.parent, path.name) != intent:
                                raise RestoreHandoffError('原任务准备意图无法从归档重建。')
                            result = self._jobs_receipt(value, intent)
                            completed = os.path.lexists(done)
                            if completed and _read(done.parent, done.name) != result:
                                raise RestoreHandoffError('任务准备完成凭据变化。')
                            database = root/'workbench.sqlite'
                            if any(os.path.lexists(database.with_name(database.name+suffix)) for suffix in ('-wal','-shm')):
                                raise RestoreHandoffError('任务准备只接受已关闭的独立数据库。')
                            journal = database.with_name(database.name+'-journal')
                            if os.path.lexists(journal):
                                _ordinary(journal)
                                if not exists or completed: raise RestoreHandoffError('未登记或已完成任务准备出现数据库日志。')
                            # Only a verified existing intent permits SQLite to
                            # roll back its hot journal during explicit resume.
                            with closing(_connect(database)) as connection:
                                state = self._verify_copy(copy_, connection, base, intent, complete=completed)
                                if not exists:
                                    if state != intent['before_logical_sha256']:
                                        raise RestoreHandoffError('首次任务准备不属于原数据库状态。')
                                    atomic_create_json(path, intent)
                                    self._fault('after_jobs_intent', value)
                                self._require_job_intent(value, intent)
                                if state == intent['before_logical_sha256']:
                                    connection.execute('BEGIN IMMEDIATE')
                                    try:
                                        if _logical_digest(connection) != state:
                                            raise RestoreHandoffError('取得任务写事务前数据库变化。')
                                        _apply_job_intent(connection, intent)
                                        self._fault('before_jobs_commit', value)
                                        self._require_job_intent(value, intent)
                                        if _logical_digest(connection) != intent['after_logical_sha256']:
                                            raise RestoreHandoffError('任务失效结果偏离限定的完整 SQL 变更。')
                                        connection.commit()
                                        self._fault('after_jobs_commit', value)
                                    except BaseException:
                                        connection.rollback()
                                        raise
                                self._verify_copy(copy_, connection, base, intent, complete=True)
                            scope = load_restore_scope(database)
                            if scope is None or scope.document != base['scope']:
                                raise RestoreHandoffError('任务准备后丢失了原只读范围。')
                            self._require_job_intent(value, intent)
                        finally:
                            overlay.live = False
                    self._fault('before_jobs_completion', value)
                    # Recheck the saved source evidence and outer archive view
                    # before publishing completion, with the target lease held.
                    budget_context.close()
                    if not os.path.lexists(done):
                        self._require_job_intent(value, intent)
                        atomic_create_json(done, result)
                        self._fault('after_jobs_completion', value)
                    elif _read(done.parent, done.name) != result:
                        raise RestoreHandoffError('任务准备完成凭据不一致。')
            return result

    def prepare_jobs(self, package, *, handoff_id, temporary_root=None):
        return self._jobs(package, handoff_id=handoff_id, temporary_root=temporary_root, resume=False)

    def resume_jobs(self, package, *, handoff_id, temporary_root=None):
        return self._jobs(package, handoff_id=handoff_id, temporary_root=temporary_root, resume=True)
