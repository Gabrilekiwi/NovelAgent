"""Archive-bound restore lineage for evidence reads, never write authority.

Physical source bindings are checked by the live archive boundary. Offline
readers check the same recorded chain without opening any ancestral path.
Only unchanged, originally copied evidence may use an ancestral coordinate.
"""
import json
from pathlib import Path, PurePosixPath

from core.story_project.reviewed_state_document import digest


class RestoreProvenanceError(ValueError):
    code = 'backup_restore_provenance_invalid'


def _checked(value, schema, field):
    if (not isinstance(value, dict) or value.get('schema_version') != schema
            or value.get(field) != digest({k:v for k,v in value.items() if k != field})):
        raise RestoreProvenanceError('恢复来源证据的格式或摘要不同。')
    return value


def _same(actual, expected):
    if actual != expected:
        raise RestoreProvenanceError('恢复来源、范围或前后凭据不一致。')


def validate_restore_provenance(value, *, root, book_id, ownership=None, depth=0):
    """Check recorded relationships only; no machine/source path is opened."""
    try:
        if depth > 16 or len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 32*1024**2:
            raise RestoreProvenanceError('恢复来源链超过本次支持大小。')
        _checked(value, 'restored_backup_provenance_v1', 'provenance_sha256')
        root = Path(root)
        if not root.is_absolute() or '..' in root.parts:
            raise RestoreProvenanceError('恢复来源坐标无效。')
        _same(value['source_root'], str(root)); _same(value['book_id'], book_id)
        q = _checked(value['qualification'], 'restored_write_qualification_v1', 'qualification_sha256')
        active = value['active_record']
        from workbench.book_ownership import OWNERSHIP_V3
        if (active.get('version') != OWNERSHIP_V3 or active.get('state') != 'active'
                or active.get('record_sha256') != digest({k:v for k,v in active.items() if k!='record_sha256'})):
            raise RestoreProvenanceError('恢复来源不是已启用的机器资格。')
        _same(active['binding']['book_id'], book_id)
        _same(active['restore_qualification_sha256'], q['qualification_sha256'])
        if ownership is not None: _same(active, ownership)
        _same(active['binding']['root'], str(root))
        _same(active['binding']['database'], str(root.parent/'workbench.sqlite'))
        _same(q['book_root'], str(root)); _same(q['database'], active['binding']['database'])
        _same(q['book_id'], book_id); _same(q['project_id'], value['project_id'])
        intent = _checked(value['activation_intent'], 'restored_write_activation_intent_v1', 'intent_sha256')
        _same(intent['qualification'], q)
        from workbench.restore_reinstatement import validate_reinstatements
        _same(validate_reinstatements(intent['active_record'],q,
            value.get('qualification_reinstatements',[]),depth=depth),active)
        from workbench.restore_qualification import activation_receipt
        _same(value['activation_done'], activation_receipt(intent))

        from workbench.restore_scope import validate_scope_document, RestoreScopeError
        scope = value['scope']
        try: validate_scope_document(scope)
        except RestoreScopeError as exc:
            raise RestoreProvenanceError('恢复来源范围证据无效。') from exc
        _same(scope['record_sha256'], q['scope_sha256'])
        for key, expected in {'book_root':str(root), 'database':q['database'],
                'book_id':book_id, 'project_id':q['project_id'], 'state':'restoring'}.items():
            _same(scope[key], expected)
        from workbench.backup_restore_materialization import _validate_plan
        plan = _validate_plan(value['plan'])
        _same(plan['plan_sha256'], q['plan_sha256'])
        _same(plan['root_plan']['book_root'], str(root))
        _same(plan['root_plan']['database'], q['database'])
        _same(plan['book_id'], book_id); _same(plan['project_id'], q['project_id'])
        _same(plan['archive_receipt_sha256'], q['archive_receipt_sha256'])
        _same(scope['plan_sha256'], plan['plan_sha256'])
        _same(scope['archive_receipt_sha256'], plan['archive_receipt_sha256'])
        materialized = _checked(value['materialization'], 'restore_materialization_receipt_v1', 'receipt_sha256')
        _same(materialized['copied_sha256'], digest(materialized['copied']))
        _same(materialized['plan_sha256'], plan['plan_sha256'])
        _same(materialized['archive_receipt_sha256'], plan['archive_receipt_sha256'])
        _same(materialized['container'], str(root.parent))
        _same(materialized['book_id'], book_id); _same(materialized['project_id'], q['project_id'])
        roots = _checked(value['root_intent'], 'restore_root_binding_intent_v1', 'intent_sha256')
        root_done = _checked(value['root_done'], 'restore_root_binding_receipt_v1', 'receipt_sha256')
        _same(roots['plan_sha256'], plan['plan_sha256'])
        _same(roots['archive_receipt_sha256'], plan['archive_receipt_sha256'])
        _same(roots['materialization_receipt_sha256'], materialized['receipt_sha256'])
        _same(root_done['intent_sha256'], roots['intent_sha256'])
        _same(root_done['plan_sha256'], plan['plan_sha256'])
        _same(root_done['materialization_receipt_sha256'], materialized['receipt_sha256'])
        database = _checked(value['database_intent'], 'restore_database_binding_intent_v1', 'intent_sha256')
        _same(database['scope'], scope)
        _same(database['root_binding_receipt_sha256'], root_done['receipt_sha256'])
        _same(database['plan_sha256'], plan['plan_sha256'])
        _same(database['archive_receipt_sha256'], plan['archive_receipt_sha256'])
        inherited=plan.get('source_restore_provenance')
        if scope['schema_version']=='restored_workbench_scope_v2':
            if inherited is None:raise RestoreProvenanceError('新范围缺少原代际归档证据。')
            _same(scope['predecessor_scope'],inherited['scope'])
            _same(scope['source_provenance_sha256'],inherited['provenance_sha256'])
            _same(database.get('source_restore_provenance'),inherited)
        elif inherited is not None or database.get('source_restore_provenance') is not None:
            raise RestoreProvenanceError('旧范围不能声称已继承另一代恢复证据。')
        from workbench.backup_restore_database import _binding_receipt
        _same(value['database_done'], _binding_receipt(database, plan))
        from workbench.restore_job_history import validate_job_intent, RestoreJobHistoryError
        jobs = value['jobs_intent']
        try: generation = validate_job_intent(jobs, scope, inherited)
        except RestoreJobHistoryError as exc: raise RestoreProvenanceError(str(exc)) from exc
        _same(jobs['handoff_id'], q['handoff_id'])
        _same(jobs['base_database_intent_sha256'], database['intent_sha256'])
        _same(jobs['before_logical_sha256'], database['after_logical_sha256'])
        _same(jobs['after_logical_sha256'], q['initial_database_logical_sha256'])
        _same(jobs['plan_sha256'], plan['plan_sha256'])
        _same(jobs['archive_receipt_sha256'], plan['archive_receipt_sha256'])
        _same(jobs['invalidated_contract'], 'restored_job_history_v1')
        jobs_schema = 'restore_jobs_prepared_v1' if generation == 1 else 'restore_jobs_prepared_v2'
        for key, schema, field in [('jobs_done',jobs_schema,'jobs_receipt_sha256'),
                ('budget_done','restore_budget_prepared_v1','budget_receipt_sha256'),
                ('execution_done','restore_executions_prepared_v1','execution_receipt_sha256')]:
            done = _checked(value[key], schema, 'receipt_sha256')
            _same(done['receipt_sha256'], q[field]); _same(done['handoff_id'], q['handoff_id'])
        _same(value['jobs_done']['jobs_intent_sha256'], jobs['intent_sha256'])
        if 'event_history' in jobs:
            _same(value['jobs_done']['original_event_count'],len(jobs['event_history']))
            _same(value['jobs_done']['original_events_preserved'],True)
            _same(value['jobs_done']['event_history_sha256'],jobs['event_history_sha256'])
        if generation > 1:
            _same(value['jobs_done']['scope_generation'], generation)
            _same(value['jobs_done']['source_provenance_sha256'], inherited['provenance_sha256'])
            _same(value['jobs_done']['prior_generations_preserved'], True)
        _same(jobs['budget_receipt_sha256'], q['budget_receipt_sha256'])
        executions = _checked(value['execution_intent'], 'restore_execution_preparation_v1', 'intent_sha256')
        policy = _checked(executions['policy'], 'restored_execution_policy_v1', 'policy_sha256')
        _same(policy, value['execution_policy'])
        _same(policy['policy_sha256'], q['execution_policy_sha256'])
        _same(policy['book_root'], str(root)); _same(policy['handoff_id'], q['handoff_id'])
        _same(value['execution_done']['execution_intent_sha256'], executions['intent_sha256'])
        _same(value['execution_done']['policy_sha256'], policy['policy_sha256'])
        _same(policy['fresh_task_runtime_root'], q['fresh_task_runtime_root'])
        _same(value['execution_done']['fresh_task_runtime_binding'], q['fresh_task_runtime_binding'])
        mappings = [m for m in plan['root_plan']['roots'] if m['root']=='story_project']
        if len(mappings) != 1: raise RestoreProvenanceError('恢复来源作品映射不唯一。')
        previous = Path(mappings[0]['source_root'])
        if not previous.is_absolute() or '..' in previous.parts or previous==root:
            raise RestoreProvenanceError('原作品历史坐标无效。')
        _same(mappings[0]['destination'], str(root))
        inherited = plan.get('source_restore_provenance')
        if inherited is not None:
            validate_restore_provenance(inherited, root=previous, book_id=book_id, depth=depth+1)
        return previous
    except (KeyError, TypeError, AttributeError) as exc:
        raise RestoreProvenanceError('恢复来源的必要证据缺失。') from exc


def capture_restore_provenance(database, ownership, *, machine_store=None):
    """Called under the owner/book/SQLite snapshot fences and rechecked at end."""
    from workbench.book_ownership import OWNERSHIP_V3
    if ownership is None or ownership['version'] != OWNERSHIP_V3: return None
    from workbench.restore_scope import load_restore_scope
    from workbench.restore_qualification import machine_store_for
    from workbench.backup_restore_roots import _read
    scope = load_restore_scope(database.path, machine_store=machine_store)
    if scope is None or not scope.active:
        raise RestoreProvenanceError('恢复来源尚未启用。')
    container = database.path.parent
    book_id = scope.document['book_id']
    store = machine_store_for(book_id, machine_store)/digest(book_id.encode('utf-8'))
    prefix = 'handoff-'+scope.activation['handoff_id']+'-'
    value = {'schema_version':'restored_backup_provenance_v1','source_root':str(scope.book_root),
        'book_id':book_id,'project_id':scope.project_id,'qualification':scope.activation,
        'active_record':ownership,'scope':scope.document}
    local = {'plan':'restore-plan.json','materialization':'restore-materialization.json',
        'root_intent':'root-binding.json','root_done':'root-binding-done.json',
        'database_intent':'database-binding.json','database_done':'database-binding-done.json',
        'execution_policy':'execution-policy.json'}
    machine = {'activation_intent':'activation','activation_done':'activation-done',
        'jobs_intent':'jobs','jobs_done':'jobs-done','budget_done':'budget-done',
        'execution_intent':'executions','execution_done':'executions-done'}
    for key, name in local.items(): value[key] = _read(container, name)
    for key, suffix in machine.items(): value[key] = _read(store, prefix+suffix+'.json')
    from workbench.restore_reinstatement import load_reinstatements
    renewals=load_reinstatements(store,value['activation_intent']['active_record'],scope.activation,ownership)
    if renewals: value['qualification_reinstatements']=renewals
    value['provenance_sha256'] = digest(value)
    validate_restore_provenance(value, root=scope.book_root, book_id=book_id, ownership=ownership)
    return value


class HistoricalCoordinates:
    def __init__(self, root, tree, book_id, provenance=None):
        self.root = Path(root); self.tree = tree; self.lineages = []
        current = self.root
        while provenance is not None:
            previous = validate_restore_provenance(provenance, root=current, book_id=book_id)
            copied = provenance['materialization']['copied']['files']
            # Originally terminal journals and registrations remain immutable,
            # even if a complete directory disappears from the current tree.
            controls = {'.novelagent/runtime/runs','.novelagent/runtime/persistence',
                '.novelagent/runtime/reviewed/p','.novelagent/runtime/ea'}
            for name in copied:
                if not name.startswith('book/'): continue
                path=PurePosixPath(name[5:])
                if path.name in {'.persistence.lock','root_registry.json'}:
                    controls.add(str(path.parent))
                if path.parts[:2]==('.novelagent','runtime'):
                    for index, part in enumerate(path.parts[2:],2):
                        if part in {'transactions','journals','registry'}:
                            controls.add('/'.join(path.parts[:index]))
            for name, expected in copied.items():
                if not name.startswith('book/'): continue
                relative = name[5:]
                historical = any(relative.startswith(control+'/transactions/')
                    or relative.startswith(control+'/journals/')
                    or relative.startswith(control+'/registry/completed/')
                    or relative.startswith(control+'/registry/rolled_back/')
                    or PurePosixPath(relative).parent==PurePosixPath(control)
                        and PurePosixPath(relative).suffix=='.json'
                        and PurePosixPath(relative).name!='root_registry.json' for control in controls)
                if historical:
                    actual = tree['files'].get(relative)
                    if actual is None or {k:actual[k] for k in ('size','sha256')} != expected:
                        raise RestoreProvenanceError('原复制事务或终态登记缺失或变化。')
            self.lineages.append((previous, copied))
            current = previous
            provenance = provenance['plan'].get('source_restore_provenance')

    def path(self, value, *, evidence):
        """Translate a historical path only for an unchanged copied record."""
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise RestoreProvenanceError('历史引用坐标无效。')
        if path.is_relative_to(self.root): return path
        actual = self.tree['files'].get(evidence)
        for previous, copied in self.lineages:
            expected = copied.get('book/'+evidence)
            if (path.is_relative_to(previous) and actual is not None and expected is not None
                    and {k:actual[k] for k in ('size','sha256')} == expected):
                return self.root/path.relative_to(previous)
        raise RestoreProvenanceError('历史引用没有未变化的原复制证据。')


def validate_restored_database(connection, provenance):
    """Classify restore tables only after checking their complete original rows."""
    scope = provenance['scope']; intent = provenance['jobs_intent']
    columns = {'workbench_restore_scope':['singleton','document_json']}
    for table, expected in columns.items():
        if [row[1] for row in connection.execute('PRAGMA table_info('+table+')')] != expected:
            raise RestoreProvenanceError('恢复来源数据库表缺失或格式不同。')
    rows = [tuple(row) for row in connection.execute('SELECT singleton,document_json FROM workbench_restore_scope LIMIT 2')]
    if len(rows)!=1 or rows[0][0]!=1 or json.loads(rows[0][1])!=scope:
        raise RestoreProvenanceError('恢复来源数据库范围与原凭据不同。')
    from workbench.restore_job_history import (validate_retained_tables, invalidated_row,
        validate_retained_events,FIRST_TABLE, GENERATION_TABLE, RestoreJobHistoryError)
    try:
        tables = validate_retained_tables(connection, provenance)
        validate_retained_events(connection,intent)
    except RestoreJobHistoryError as exc: raise RestoreProvenanceError(str(exc)) from exc
    for item in intent['history']:
        current = connection.execute('SELECT * FROM jobs WHERE id=?',(item['id'],)).fetchone()
        if current is None or dict(current)!=invalidated_row(item['original'], intent):
            raise RestoreProvenanceError('原任务重新取得执行契约或偏离失效记录。')
    generation = scope.get('scope_generation', 1)
    history = tables[FIRST_TABLE] if generation == 1 else [row for row in tables[GENERATION_TABLE]
        if row['scope_generation'] == generation]
    return {'classified_tables':sorted([*columns, *tables]),'original_job_count':len(history),
        'original_job_rows_sha256':digest(history),'scope_record_sha256':scope['record_sha256']}
