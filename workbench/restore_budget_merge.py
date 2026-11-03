"""Select a proved descendant of archived spending; never add two totals."""
import json
from pathlib import PurePosixPath
import re

from core.story_project.reviewed_state_document import digest
from workbench.backup_archive_usage import _usage
from workbench.restore_handoff import RestoreHandoffError


_BUDGETS = '.novelagent/runtime/budgets'
_EXECUTIONS = '.novelagent/runtime/runs/executions'
_AUXILIARY = '.novelagent/runtime/model_calls'
_LEDGER = re.compile(r'\.novelagent/runtime/budgets/c[1-9][0-9]*/ledger\.json')
_TIME = re.compile(r'\.novelagent/runtime/budgets/c[1-9][0-9]*/(?:local-recovery/)?time\.json')


def transferable(name):
    if name.startswith(_BUDGETS+'/') or name.startswith(_AUXILIARY+'/'):
        return True
    if not name.startswith(_EXECUTIONS+'/'):
        return False
    parts = name[len(_EXECUTIONS)+1:].split('/')
    # Post-archive execution plans/publication artifacts are NOT imported.
    # Only their immutable model-call evidence supports retained spending.
    return len(parts) >= 3 and parts[1] == 'model_calls'


def _ledger_extends(old, new, reference):
    if old['budget_sha256'] != new['budget_sha256'] or len(old['attempts']) > len(new['attempts']):
        raise RestoreHandoffError('恢复预算丢失原冻结条件或调用：'+reference)
    for before, after in zip(old['attempts'], new['attempts']):
        if (before['execution_id'] != after['execution_id'] or before['plan'] != after['plan']
                or before['local_rejection'] != after['local_rejection']
                or before['dispatch_claimed'] and not after['dispatch_claimed']
                or before['receipt'] is not None and before['receipt'] != after['receipt']):
            raise RestoreHandoffError('恢复预算中的原调用身份、发送标记或结算被改写：'+reference)


def _time_extends(old, new, reference):
    if old['binding'] != new['binding'] or len(old['intervals']) > len(new['intervals']):
        raise RestoreHandoffError('恢复预算丢失已记录的执行时间：'+reference)
    for before, after in zip(old['intervals'], new['intervals']):
        if before['ended_at'] is not None:
            if before != after:
                raise RestoreHandoffError('已闭合执行时间不能在恢复时改写：'+reference)
        elif (any(before[key] != after[key] for key in ('interval_id','operation_id','started_at'))
                or after['ended_at'] is None and before != after):
            raise RestoreHandoffError('未知执行时间只能由同一区间的实际闭合记录解释：'+reference)


def reconcile_usage_evidence(archived, current, *, book_id):
    """Both inputs expose checked raw bytes, not caller-supplied summaries.

    The source is required to contain every earlier immutable call and frozen
    budget. Allowed ledger/time progression is checked by identity. A missing
    source row is a conflict, even when the source's overall total is higher.
    """
    for evidence in (archived, current):
        identity = evidence.document(evidence.root, '.novelagent/project.json')
        if identity.get('book_id') != book_id:
            raise RestoreHandoffError('恢复预算的作品身份不同。')
    # Validate both complete projections before comparing mutable documents.
    # Archive blockers can be explained by later evidence; current uncertainty
    # remains a continuation blocker, never an invitation to settle locally.
    _usage(archived, book_id, lambda code, reference:None)
    blockers = []
    def block(code, reference):
        item = {'code':code, 'reference':reference}
        if item not in blockers: blockers.append(item)
    usage = json.loads(json.dumps(_usage(current, book_id, block), ensure_ascii=False))
    before_files = {name for name in archived.files if transferable(name)}
    after_files = {name for name in current.files if transferable(name)}
    if not before_files.issubset(after_files):
        raise RestoreHandoffError('原消费凭据中缺少归档已有文件，不能当成零消费。')
    changed = []
    for name in sorted(before_files):
        before = archived.raw(archived.root, name); after = current.raw(current.root, name)
        if before == after: continue
        if _LEDGER.fullmatch(name):
            _ledger_extends(archived.document(archived.root, name), current.document(current.root, name), name)
        elif _TIME.fullmatch(name):
            _time_extends(archived.document(archived.root, name), current.document(current.root, name), name)
        else:
            raise RestoreHandoffError('归档已有的冻结条件或不可变调用凭据变化：'+name)
        changed.append(name)
    files = {name:{key:current.tree['files'][name][key] for key in ('size','sha256')}
        for name in sorted(after_files)}
    directories = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != '.'}
    result = {'schema_version':'restore_budget_reconciliation_v1', 'book_id':book_id,
        'files':files, 'directories':sorted(directories), 'changed_mutable_files':changed,
        'new_files':sorted(after_files-before_files), 'usage':usage, 'blockers':blockers,
        'budget_evidence_reconciled':True, 'settled':not blockers, 'write_activation_allowed':False}
    result['reconciliation_sha256'] = digest(result)
    return result
