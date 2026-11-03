"""Explicit, recoverable first publication of an empty chapter budget.

The service must hold book ownership and the Provider lock while invoking
freeze(), and revalidate the author's source selection in before_write. Reads
never initialize. The ready receipt is published last; execution must require
it, so partial initialization cannot contain legitimate spending or intervals.
"""
import json
import os
from pathlib import Path

from core.chapter_budget_contract import validate_chapter_budget
from core.chapter_budget_lock import chapter_budget_lock
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetError, ChapterBudgetConflict
from core.chapter_execution_time import ChapterExecutionClock
from core.engine.persistence import atomic_create_json
from core.engine.safe_paths import SafePathError, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.reviewed_state_document import read_project_bytes


def _hashed(value,field):
    return {**value,field:canonical_json_hash(value,exclude_environment_fields=False)}


class ChapterBudgetFreezeRegistry:
    def __init__(self,story_project_root,*,book_id):
        self.root=assert_safe_local_tree(Path(story_project_root)/'.novelagent/runtime/budgets')
        self.book_id=book_id

    def _names(self,chapter):
        if type(chapter) is not int or not 1<=chapter<=10**15:
            raise ChapterBudgetError('invalid chapter budget identity')
        return f'i{chapter}.json',f'r{chapter}.json',f'c{chapter}'

    def _read(self,name):
        try:return json.loads(read_project_bytes(self.root,name))
        except (OSError,ValueError,SafePathError) as exc:
            raise ChapterBudgetError('chapter budget initialization evidence is missing or invalid') from exc

    def _intent(self,budget):
        return _hashed(dict(schema_version='chapter_budget_freeze_intent_v1',budget=budget),'intent_sha256')

    def _ready(self,intent):
        budget=intent['budget']
        return _hashed(dict(schema_version='chapter_budget_ready_v1',book_id=budget['book_id'],
            chapter=budget['chapter'],budget_sha256=budget['budget_sha256'],
            intent_sha256=intent['intent_sha256']),'ready_sha256')

    def load(self,chapter):
        """Load a complete initialization; absence/corruption is not a new budget."""
        intent_name,ready_name,directory=self._names(chapter)
        intent=self._read(intent_name)
        if not isinstance(intent,dict) or 'budget' not in intent:
            raise ChapterBudgetError('invalid chapter budget freeze intent')
        budget=validate_chapter_budget(intent['budget'],expected_book_id=self.book_id,expected_chapter=chapter)
        if intent!=self._intent(budget) or self._read(ready_name)!=self._ready(intent):
            raise ChapterBudgetConflict('chapter initialization binding differs')
        store=ChapterBudgetStore(self.root/directory,book_id=self.book_id,chapter=chapter)
        if store.frozen_budget()!=budget:
            raise ChapterBudgetConflict('chapter budget differs from original initialization')
        # Only validate: an open interval remains unknown and is not closed here.
        ChapterExecutionClock(store.root,budget).report()
        return budget

    def status(self,chapter):
        intent_name,ready_name,directory=self._names(chapter)
        if os.path.lexists(self.root/ready_name):
            return {'state':'ready','budget':self.load(chapter)}
        if os.path.lexists(self.root/intent_name):
            intent=self._read(intent_name)
            if not isinstance(intent,dict) or 'budget' not in intent:
                raise ChapterBudgetError('invalid chapter budget initialization')
            budget=validate_chapter_budget(intent['budget'],expected_book_id=self.book_id,expected_chapter=chapter)
            if intent!=self._intent(budget):raise ChapterBudgetConflict('initialization intent hash differs')
            return {'state':'initializing','budget':budget}
        if os.path.lexists(self.root/directory):
            raise ChapterBudgetError('chapter budget directory has lost its initialization intent')
        return {'state':'not_configured','budget':None}

    def freeze(self,budget,*,before_write,fault_injector=None):
        """First freeze or explicit retry of that exact unfinished freeze.

        before_write must reject existing unbudgeted chapter work and recheck
        current registered book/chapter/source under the caller's ownership.
        """
        if not callable(before_write):raise ChapterBudgetError('budget source/ownership preflight is required')
        budget=validate_chapter_budget(budget,expected_book_id=self.book_id)
        intent_name,ready_name,directory=self._names(budget['chapter'])
        intent=self._intent(budget)
        def fault(event):
            if fault_injector is not None:fault_injector(event)
        with chapter_budget_lock(self.root/'freeze-lock'):
            state=self.status(budget['chapter'])
            if state['state']!='not_configured' and state['budget']!=budget:
                raise ChapterBudgetConflict('chapter budget is already frozen or being initialized')
            if state['state']=='ready':return state['budget']
            before_write()
            if state['state']=='not_configured':
                atomic_create_json(self.root/intent_name,intent)
                fault('after_freeze_intent')
            binding={key:budget[key] for key in ('book_id','chapter','budget_sha256')}
            records={
                'budget.json':budget,
                'ledger.json':_hashed(dict(schema_version='chapter_budget_ledger_v2',
                    budget_sha256=budget['budget_sha256'],attempts=[]),'ledger_sha256'),
                'time.json':_hashed(dict(schema_version='chapter_execution_time_v1',
                    binding=binding,intervals=[]),'record_sha256'),
            }
            target=assert_safe_local_tree(self.root/directory)
            if target.exists() and any(path.name not in records for path in target.iterdir()):
                raise ChapterBudgetError('unfinished initialization contains unexpected execution evidence')
            # Validate every existing file before creating a missing one. A
            # non-empty ledger/time record can never be reset by this path.
            for name,expected in records.items():
                if os.path.lexists(target/name) and self._read(f'{directory}/{name}')!=expected:
                    raise ChapterBudgetConflict('unfinished budget has changed or already contains execution')
            for name,value in records.items():
                before_write()
                if not os.path.lexists(target/name):atomic_create_json(target/name,value)
                fault('after_'+name.removesuffix('.json'))
            before_write()
            atomic_create_json(self.root/ready_name,self._ready(intent))
            fault('after_ready')
            return self.load(budget['chapter'])
