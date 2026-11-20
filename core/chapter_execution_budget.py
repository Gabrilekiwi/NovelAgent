"""Shared chapter budget scope for Candidate and fact execution services.

The Workbench must verify book ownership before constructing/using this scope.
Construction and execution only load an existing freeze, never initialize or
replace it. First-time configuration belongs to the explicit budget service.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetConflict, ChapterBudgetError
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_execution_time import ChapterExecutionClock
from core.chapter_model_budget import ChapterModelCallBudget
from core.engine.safe_paths import assert_safe_local_tree

_CHAPTER_OPERATION=ContextVar('chapter_budget_operation',default=None)


@dataclass
class ChapterBudgetOperation:
    budget: object
    execution_id: str
    interval: object
    local_only: bool
    provider_dispatch_allowed: bool = True
    closed: bool = False

    def require_target(self,budget,execution_id):
        if (self.closed or self.interval.closed or self.budget.root!=budget.root
                or self.budget.time_clock.binding!=budget.time_clock.binding
                or self.execution_id!=execution_id):
            raise ChapterBudgetConflict('active chapter operation differs from the requested execution')


def current_chapter_operation():
    return _CHAPTER_OPERATION.get()


class ChapterExecutionBudget:
    def __init__(self,story_project_root,*,book_id,chapter,time_clock=None):
        if type(chapter) is not int or chapter<1:
            raise ChapterBudgetConflict('invalid chapter budget scope')
        self.story_project_root=assert_safe_local_tree(Path(story_project_root))
        self.run_root=assert_safe_local_tree(self.story_project_root/'.novelagent/runtime/runs')
        self.root=assert_safe_local_tree(self.story_project_root/f'.novelagent/runtime/budgets/c{chapter}')
        self.store=ChapterBudgetStore(self.root,book_id=book_id,chapter=chapter)
        frozen=ChapterBudgetFreezeRegistry(self.story_project_root,book_id=book_id).load(chapter)
        self.time_clock=time_clock or ChapterExecutionClock(self.root,frozen)
        if self.time_clock.root!=self.root or self.time_clock.budget!=frozen:
            raise ChapterBudgetConflict('chapter time and amount stores have different bindings')

    @contextmanager
    def operation(self,execution_id,*,local_only=False,allow_provider_dispatch=True):
        """One service action, including preparation and final local publication.

        The caller holds machine book ownership. A completed replay or explicit
        resume may select local_only; execution must then use saved responses.
        """
        if _CHAPTER_OPERATION.get() is not None:
            raise ChapterBudgetConflict('nested chapter service operations are forbidden')
        if type(allow_provider_dispatch) is not bool:
            raise ChapterBudgetConflict('chapter Provider dispatch policy must be explicit')
        manager=self.time_clock.local_recovery(execution_id) if local_only else self.time_clock.active(execution_id,awaiting_author_after=True)
        with manager as interval:
            action=ChapterBudgetOperation(self,execution_id,interval,local_only,allow_provider_dispatch)
            token=_CHAPTER_OPERATION.set(action)
            try:yield action
            finally:
                action.closed=True
                _CHAPTER_OPERATION.reset(token)

    @contextmanager
    def activate(self,runtime,intent):
        frozen=ChapterBudgetFreezeRegistry(self.story_project_root,book_id=self.store.book_id).load(self.store.chapter)
        if intent['book_id']!=frozen['book_id'] or intent['chapter_index']!=frozen['chapter']:
            raise ChapterBudgetConflict('execution intent belongs to a different chapter budget')
        execution=intent['execution']['execution_id']
        budget=ChapterModelCallBudget(self.store,execution_id=execution)
        relative=f'executions/{execution}/model_calls'
        if (intent['execution']['model_calls_ref']!=relative
                or runtime.store.root!=assert_safe_local_tree(self.run_root/relative)):
            raise ChapterBudgetConflict('model evidence directory differs from the original execution')
        runtime.chapter_budget=budget
        action=_CHAPTER_OPERATION.get()
        if action is not None:action.require_target(self,execution)
        # Receipt settlement and proven not-sent recovery are local operations.
        # Do them before the active clock or global unresolved-call gate blocks.
        runtime.reconcile_chapter_evidence()
        if action is not None:
            runtime.active_interval=action.interval
            if action.local_only:
                self._require_local_response(runtime,budget)
            yield runtime
            return
        time_status=self.time_clock.report()
        if runtime.local_recovery_requested and (time_status['active_status']=='unknown' or time_status['active_limit_reached']):
            # This explicit resume may finish local artifacts, but may not enter
            # generator/Provider code or reuse a runtime for a new request.
            self._require_local_response(runtime,budget)
            # The unknown original interval remains byte-for-byte unchanged.
            # Completing a local artifact does not reopen the chapter budget.
            with self.time_clock.local_recovery(execution) as local_interval:
                runtime.active_interval=local_interval
                yield runtime
            return
        with self.time_clock.active(execution,awaiting_author_after=True) as interval:
            runtime.active_interval=interval
            # Retain the closed handle afterwards so an escaped runtime cannot
            # silently opt out of the active-time gate after this operation.
            yield runtime

    @staticmethod
    def _require_local_response(runtime,budget):
        runtime.dispatch_forbidden=True
        rows=budget.attempts()
        if any(row['receipt'] is None or row['receipt']['status'] not in {'succeeded','request_not_sent'} for row in rows):
            raise ChapterBudgetError('local recovery requires fully explained original model receipts')
        successful=[row['receipt'] for row in rows if row['receipt']['status']=='succeeded']
        if len(successful)!=1:
            raise ChapterBudgetError('local recovery requires exactly one verified successful response')
        runtime.local_recovery_response=runtime._load_response_from_receipt(successful[0])
