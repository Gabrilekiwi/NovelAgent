"""Execution restrictions derived from an actual restored archive and budgets.

This is an additional denial gate. A policy never grants book ownership, permits
Provider calls, changes budget evidence, or certifies a writable restore.
"""
import json
import os
from pathlib import Path, PurePosixPath
import re

from workbench.backup_restore_roots import _read
from workbench.restore_handoff import RestoreHandoffError, _signed


POLICY_FILE = 'execution-policy.json'
FRESH_JOBS = 'next-jobs'
_RESTORE_MARKERS = ('restore-intent.json', POLICY_FILE, 'workbench.sqlite.restore-scope.json')


class RestoredExecutionBlocked(RestoreHandoffError):
    code = 'restored_execution_history_only'


def _job_namespaces(plan, jobs_intent, *, depth=0):
    """Exact imported IDs lose their old namespaces, including absent roots."""
    if depth>16:
        raise RestoredExecutionBlocked('历史任务命名空间代际超过支持范围。')
    if (jobs_intent!=_signed({k:v for k,v in jobs_intent.items() if k!='intent_sha256'},'intent_sha256')
            or jobs_intent['plan_sha256']!=plan['plan_sha256']
            or jobs_intent['archive_receipt_sha256']!=plan['archive_receipt_sha256']):
        raise RestoredExecutionBlocked('原任务意图与执行隔离的恢复计划不同。')
    container=Path(plan['root_plan']['container']);runtime=Path(plan['root_plan']['task_runtime_root'])
    if not runtime.is_relative_to(container) or '..' in runtime.parts:
        raise RestoredExecutionBlocked('原任务命名空间不属于恢复容器。')
    history=jobs_intent['history']
    if not isinstance(history,list) or len(history)>10000:
        raise RestoredExecutionBlocked('原任务命名空间清单无效。')
    blocked=set()
    for item in history:
        if item['original']['project_id']!=plan['project_id']:continue
        identity=item['id']
        if not isinstance(identity,str) or not re.fullmatch(r'job_[A-Za-z0-9_-]{1,180}',identity):
            raise RestoredExecutionBlocked('原任务命名空间标识无效。')
        blocked.add((runtime/identity).relative_to(container).as_posix())
    inherited=plan.get('source_restore_provenance')
    if inherited is not None:
        from workbench.backup_restore_provenance import validate_restore_provenance
        validate_restore_provenance(inherited,root=Path(inherited['source_root']),book_id=plan['book_id'],depth=depth)
        # Completed older policies omitted internal job directories. Their
        # original plans and job intents still prove each former namespace;
        # add denials for every generation without rewriting those policies.
        blocked.update(_job_namespaces(inherited['plan'],inherited['jobs_intent'],depth=depth+1))
        previous=inherited['execution_policy']['blocked_paths']
        if not isinstance(previous,list) or len(previous)>100000:
            raise RestoredExecutionBlocked('上一代执行隔离清单无效。')
        for name in previous:
            if not isinstance(name,str):raise RestoredExecutionBlocked('上一代执行坐标无效。')
            relative=PurePosixPath(name)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                    or not relative.parts or relative.parts[0] not in {'book','jobs'}):
                raise RestoredExecutionBlocked('上一代执行坐标不属于复制的历史范围。')
            blocked.add(name)
    return blocked


def execution_policy(copy_, value, budget_intent, jobs_receipt, *, jobs_intent):
    names = set(copy_.files) | copy_.directories | {
        'book/'+name for name in budget_intent['reconciliation']['files']}
    if jobs_intent['intent_sha256']!=jobs_receipt['jobs_intent_sha256']:
        raise RestoredExecutionBlocked('执行隔离与原任务准备回执不同。')
    blocked = _job_namespaces(copy_.plan,jobs_intent)
    runtime = 'book/.novelagent/runtime/'
    families = [runtime+'candidates/', runtime+'fact_extractions/', runtime+'runs/executions/']
    families += [runtime+'model_calls/'+family+'/' for family in
        ('workbench_planning','workbench_outline_batches','workbench_amendments')]
    for name in names:
        for family in families:
            if name.startswith(family):
                child = name[len(family):].split('/')[0]
                if child:
                    blocked.add(family+child)
                    # An intent may precede the creation of its call directory.
                    if family == families[0] and child.startswith('candidate_'):
                        blocked.add(runtime+'runs/executions/execution_candidate_'+child[10:][:32])
                    if family == families[1] and child.startswith('fact_extraction_'):
                        blocked.add(runtime+'runs/executions/execution_fx_'+child[16:][:24])
        if name.startswith(runtime+'reviewed/intents/') and name.endswith('.json'):
            blocked.add(name)
        if name.startswith(runtime+'reviewed/batches/') and name.endswith('.json'):
            blocked.add(name)
        if name.startswith(runtime+'reviewed/batch_controls/') and name.endswith('.json'):
            blocked.add(name)
    for mapping in copy_.layout['roots']:
        if mapping['root'].startswith('jobs/'):
            blocked.add(Path(mapping['destination']).relative_to(copy_.root).as_posix())
    # Actual recognized transaction manifests also cover noncanonical legacy
    # and v2 journal roots. Ordinary author files named manifest.json do not.
    for name in copy_.tree['files']:
        if PurePosixPath(name).name != 'manifest.json': continue
        data = json.loads(copy_.view.read_root('story_project', name))
        if not isinstance(data, dict): continue
        immutable = data.get('immutable')
        run = data.get('run_id') or (immutable.get('run_id') if isinstance(immutable,dict) else None)
        if not run or PurePosixPath(name).parent.name != run: continue
        parent = 'book/'+str(PurePosixPath(name).parent)
        blocked.add(parent)
        parts = PurePosixPath(parent)
        if parts.parent.name in {'journals','staging'}:
            for sibling in ('journals','staging'):
                blocked.add(str(parts.parent.parent/sibling/run))
    ordered = []
    for name in sorted(blocked, key=lambda x:(len(PurePosixPath(x).parts),x)):
        if not any(name == old or name.startswith(old+'/') for old in ordered): ordered.append(name)
    policy = _signed({'schema_version':'restored_execution_policy_v1',
        'handoff_id':value['handoff_id'], 'book_id':copy_.plan['book_id'],
        'project_id':copy_.plan['project_id'], 'container':str(copy_.root), 'book_root':str(copy_.book),
        'container_binding':copy_.binding, 'plan_sha256':copy_.plan['plan_sha256'],
        'archive_receipt_sha256':value['archive_receipt_sha256'],
        'budget_intent_sha256':budget_intent['intent_sha256'],
        'jobs_receipt_sha256':jobs_receipt['receipt_sha256'],
        'blocked_paths':sorted(ordered), 'fresh_task_runtime_root':str(copy_.root/FRESH_JOBS),
        'canonical_budget_history_preserved':True, 'write_activation_allowed':False}, 'policy_sha256')
    if len(json.dumps(policy,ensure_ascii=False).encode('utf-8')) > 8*1024**2:
        raise RestoreHandoffError('旧执行计划清单超过支持大小，不能截断。')
    return policy


def require_restored_execution_paths(*paths, require_active=False):
    """Reject imported execution namespaces; ordinary projects remain unchanged.

    This also accepts an explicit book root among paths for external journals.
    Both the copy and its independent machine preparation must agree. Runtime
    ownership and the read-only/active scope remain separate mandatory checks.
    """
    from workbench import book_ownership as ownership
    from workbench.restore_scope import load_restore_scope
    requested = [Path(path).absolute() for path in paths]
    books = ownership._output_books(requested)
    # Archived workbench jobs may live beside book/, with no project marker in
    # their ancestors. Locate their actual restricted container, never infer a
    # book from a bare execution ID or trust a path declared by the policy.
    containers = {parent for path in requested for parent in (path,*path.parents)
        if any(os.path.lexists(parent/name) for name in _RESTORE_MARKERS)}
    for container in containers:
        try:
            scope = load_restore_scope(container/'workbench.sqlite')
            if scope is None or scope.book_root != container/'book':
                raise ValueError('external execution has no verified restored book')
            actual = ownership._output_books([scope.book_root])
            book_id = scope.document['book_id']
            if actual.get(scope.book_root) != book_id:
                raise ValueError('external execution book identity differs')
            books[scope.book_root] = book_id
        except (OSError,RuntimeError,KeyError,TypeError,ValueError) as exc:
            raise RestoredExecutionBlocked('外部任务所属恢复容器或作品身份无法核验，禁止执行。') from exc
    for root, book_id in books.items():
        container = root.parent
        if not any(os.path.lexists(container/name) for name in _RESTORE_MARKERS):
            continue
        try:
            policy = _read(container, POLICY_FILE)
            if (policy.get('schema_version') != 'restored_execution_policy_v1'
                    or policy != _signed({k:v for k,v in policy.items() if k != 'policy_sha256'}, 'policy_sha256')
                    or policy['book_id'] != book_id or policy['book_root'] != str(root)
                    or policy['container'] != str(container)):
                raise ValueError('policy identity differs')
            scope = load_restore_scope(container/'workbench.sqlite')
            if (scope is None or scope.book_root != root or scope.document['plan_sha256'] != policy['plan_sha256']
                    or scope.document['archive_receipt_sha256'] != policy['archive_receipt_sha256']
                    or scope.document['database_parent_binding'] != policy['container_binding']):
                raise ValueError('restore scope differs')
            selected = {store for known,store in ownership._machine_scopes.get() if known == book_id}
            if len(selected) > 1: raise ValueError('conflicting machine stores')
            store = next(iter(selected)) if selected else ownership.default_machine_store()
            from workbench.restore_executions import execution_receipt
            directory = ownership.BookOwnership(book_id,machine_store=store).directory
            with ownership._machine_gate(book_id,store):
                prefix = 'handoff-'+policy['handoff_id']
                intent = _read(directory, prefix+'-executions.json')
                if intent != _signed({'schema_version':'restore_execution_preparation_v1','policy':policy}, 'intent_sha256'):
                    raise ValueError('machine execution intent differs')
                completed = _read(directory, prefix+'-executions-done.json')
                if completed != execution_receipt(intent): raise ValueError('machine execution receipt differs')
                jobs = _read(directory, prefix+'-jobs-done.json')
                if (jobs.get('receipt_sha256') != policy['jobs_receipt_sha256']
                        or jobs != _signed({k:v for k,v in jobs.items() if k != 'receipt_sha256'}, 'receipt_sha256')):
                    raise ValueError('job preparation differs')
                jobs_intent=_read(directory,prefix+'-jobs.json')
                if jobs_intent['intent_sha256']!=jobs['jobs_intent_sha256'] or jobs_intent['handoff_id']!=policy['handoff_id']:
                    raise ValueError('original job intent differs')
            from workbench.backup_restore_materialization import _validate_plan
            plan=_validate_plan(_read(container,'restore-plan.json'))
            if plan['plan_sha256']!=policy['plan_sha256'] or plan['archive_receipt_sha256']!=policy['archive_receipt_sha256']:
                raise ValueError('original execution plan differs')
            recorded = policy['blocked_paths']
            # Older completed policies remain unchanged. Their bound original
            # job intent proves the additional denials without rewriting any
            # qualification, row or receipt at startup.
            blocked = sorted(set(recorded)|_job_namespaces(plan,jobs_intent)) if isinstance(recorded,list) else recorded
            if not isinstance(blocked,list) or len(blocked)>100000: raise ValueError('invalid execution list')
            for name in blocked:
                relative = PurePosixPath(name)
                if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
                    raise ValueError('invalid execution coordinate')
                forbidden = container/relative
                for path in requested:
                    if path == forbidden or path.is_relative_to(forbidden):
                        raise RestoredExecutionBlocked('恢复前的执行计划只保留历史，请使用新的执行请求。')
            if require_active:
                if not scope.active:
                    raise RestoredExecutionBlocked('恢复副本尚未启用，不能创建或发送模型调用。')
                with ownership._machine_gate(book_id,store):
                    ownership._require_output_owner(root,book_id,ownership._record(directory,book_id))
        except RestoredExecutionBlocked:
            raise
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise RestoredExecutionBlocked('恢复执行隔离凭据缺失或变化，禁止执行。') from exc
