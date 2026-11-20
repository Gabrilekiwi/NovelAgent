"""User-visible one-shot Candidate recovery using the original chapter budget."""
import copy

from core.chapter_budget_continuation import load as load_permit
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetError, ChapterBudgetConflict
from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_candidate import _candidate_id, _execution_id
from core.engine.chapter_candidate_resolution import load_candidate_provider_resolution
from core.batch_budget_store import BatchBudgetRegistry


def operation_key(source_candidate_id):
    return 'workbench-continuation-' + source_candidate_id


def confirmation_from_intent(intent):
    author = intent.get('author_confirmation')
    if not author:
        raise ChapterBudgetConflict('自动批次候选不支持此逐章续试入口。')
    return dict(schema_version=author['schema_version'], record_type='chapter_intent_confirmation',
        book_id=intent['book_id'], chapter_index=intent['chapter_index'], confirmed=True,
        card_sha256=author['card_sha256'], context_sha256=author['context_sha256'],
        chapter_intent=copy.deepcopy(intent['chapter_intent']), confirmation_sha256=author['confirmation_sha256'])


def view(root, intent):
    """Read-only, with no initialization, settlement, or Provider dispatch."""
    store = ChapterBudgetStore(root / f".novelagent/runtime/budgets/c{intent['chapter_index']}",
        book_id=intent['book_id'], chapter=intent['chapter_index'])
    if not (store.root/'budget.json').exists():
        return None
    try:
        budget, ledger = store._load()
        report = store._report(budget, ledger)
        permit = load_permit(store, budget)
        source_execution = intent['execution']['execution_id']
        candidate = _candidate_id(book_id=intent['book_id'], chapter_index=intent['chapter_index'],
            idempotency_key=operation_key(intent['candidate_id']))
        target = _execution_id(candidate)
        result = dict(state='unavailable', message='当前候选不符合受控续试条件。',
            charged=report['charged'], limits={k:budget[k] for k in ('max_provider_calls','max_input_tokens','max_output_tokens')},
            unresolved_count=len(report['unresolved_attempts']), expected_ledger_sha256=ledger['ledger_sha256'],
            grant_sha256=None, target_candidate_id=None)
        if permit is not None:
            if permit['source_execution_id'] != source_execution:
                return {**result, 'message':'本章的一次续试已绑定其他候选。'}
            if permit['target_execution_id'] != target:
                return {**result, 'state':'managed_elsewhere', 'message':'本章续试已由已有入口处理，请查看最新候选；不能重复授权。'}
            rows = [r for r in ledger['attempts'] if r['execution_id']==target]
            state = 'used' if rows else 'ready'
            if not rows and ledger['ledger_sha256'] != permit['source_ledger_sha256']:
                state='blocked'
            return {**result,'state':state,'grant_sha256':permit['grant_sha256'],
                'target_candidate_id':candidate,
                'message':{'ready':'一次续试许可已保存，尚未发送。旧请求的预算占用和未知费用继续保留。',
                    'used':'一次续试已使用，不能再次发送。请查看该次候选结果。',
                    'blocked':'原预算记录发生变化，续试已停止；请核查记录。'}[state]}
        unknown=report['unresolved_attempts']
        if (len(unknown)!=1 or unknown[0][0]!=source_execution or not intent.get('author_confirmation')
                or ledger.get('repair_cycle') or BatchBudgetRegistry(store.root.parent,book_id=store.book_id).for_chapter(store.chapter,ledger) is not None):
            return result
        row=next(r for r in ledger['attempts'] if r['execution_id']==source_execution and r['plan']['intent']['attempt_id']==unknown[0][1])
        if not row['dispatch_claimed'] or row['receipt'] is not None or row['local_rejection']:
            return result
        return {**result,'state':'available',
            'message':'上一笔结果及费用未知。可保留原占用，在本章剩余额度内允许一次相同章意图的续试；不会自动发送。'}
    except (ChapterBudgetError, ValueError, OSError):
        return dict(state='blocked',message='预算或续试记录无法核验，已停止新增调用。',
            grant_sha256=None,target_candidate_id=None,unresolved_count=None)


def executor_with_budget(service, root):
    executor=service.executor_factory(root)
    if executor.execution_budget is None and executor.execution_budget_factory is None:
        executor.execution_budget_factory=ChapterExecutionBudget
    return executor


def authorize(service, project, intent, *, expected_ledger_sha256, expected_intent_hash,
              expected_record_hash, expected_uncertainty_sha256, author_confirmed):
    if author_confirmed is not True:
        raise ChapterBudgetConflict('请明确确认保留旧占用并允许一次续试。')
    status=view(project.root,intent)
    if not status or status['state'] not in {'available','ready','used'}:
        raise ChapterBudgetConflict('当前记录不允许创建续试许可。')
    confirmation=confirmation_from_intent(intent)
    service.chapter_cards.verify_confirmation(project.id,intent['chapter_index'],confirmation)
    executor=executor_with_budget(service,project.root)
    scope=executor._select_execution_budget(project.book_id,intent['chapter_index'])
    budget,ledger=scope.store._load()
    permit=load_permit(scope.store,budget)
    expected = permit['source_ledger_sha256'] if permit else ledger['ledger_sha256']
    if expected_ledger_sha256 != expected:
        raise ChapterBudgetConflict('预算记录已变化，请刷新后重新确认。')
    executor.abandon_provider_uncertainty(intent['candidate_id'],expected_intent_hash=expected_intent_hash,
        expected_record_hash=expected_record_hash,expected_uncertainty_sha256=expected_uncertainty_sha256,
        author_confirmed=True)
    return executor.authorize_budgeted_continuation(source_candidate_id=intent['candidate_id'],
        chapter_index=intent['chapter_index'],confirmation=confirmation,
        idempotency_key=operation_key(intent['candidate_id']),expected_ledger_sha256=expected_ledger_sha256,
        author_confirmed=True)


def run(service, project, intent, *, expected_grant_sha256, author_confirmed):
    if author_confirmed is not True:
        raise ChapterBudgetConflict('请明确确认开始这一次续试。')
    status=view(project.root,intent)
    if not status or status['state'] not in {'ready','used'} or not expected_grant_sha256 or status['grant_sha256']!=expected_grant_sha256:
        raise ChapterBudgetConflict('一次续试许可无效或已变化，请刷新页面。')
    resolution=load_candidate_provider_resolution(project.root/'.novelagent/runtime/runs',intent['candidate_id'])
    executor=executor_with_budget(service,project.root)
    scope=executor._select_execution_budget(project.book_id,intent['chapter_index'])
    permit=load_permit(scope.store,scope.store.frozen_budget())
    if resolution is None or resolution['resolution_hash']!=permit['source_resolution_hash']:
        raise ChapterBudgetConflict('原风险确认与续试许可不一致。')
    from workbench.chapter_budget_scope import chapter_service_operation
    from core.engine.chapter_candidate import ChapterCandidateStore
    target=status['target_candidate_id']; key=operation_key(intent['candidate_id'])
    confirmation=confirmation_from_intent(intent)
    with chapter_service_operation(executor,project,intent['chapter_index'],_execution_id(target),
            completed=ChapterCandidateStore(project.root).has_record(target)):
        return service._generate_in_operation(project,intent['chapter_index'],confirmation,key,executor)
