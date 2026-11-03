"""Bind one server-owned execution to its immutable chapter budget.

The caller supplies the canonical execution store after verifying book ownership.
An execution lock spans recovery, reservation, dispatch and receipt settlement.
It is separate from the short ledger lock, and shared by every runtime using
this execution ID. No network request is made by this module.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re

from core.chapter_budget_lock import chapter_budget_lock
from core.chapter_budget_store import ChapterBudgetError, ChapterBudgetConflict
from core.engine.persistence import atomic_create_json
from core.engine.safe_paths import assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.reviewed_state_document import read_project_bytes


BINDING_FILE = 'b.json'


class ChapterModelCallBudget:
    def __init__(self, store, *, execution_id):
        if not isinstance(execution_id,str) or not re.fullmatch(r'execution_[A-Za-z0-9_-]{1,180}',execution_id):
            raise ChapterBudgetError('invalid budget execution identity')
        self.store=store
        self.execution_id=execution_id
        frozen=store.frozen_budget()
        self.binding=dict(schema_version='chapter_execution_budget_v1',book_id=frozen['book_id'],
            chapter=frozen['chapter'],budget_sha256=frozen['budget_sha256'],execution_id=execution_id,
            attempt_protocol='reserve_intent_dispatch_receipt_v1')
        self.binding['binding_sha256']=canonical_json_hash(self.binding,exclude_environment_fields=False)

    @contextmanager
    def locked(self, calls):
        # Hash keeps long execution identifiers out of Windows file paths.
        lock_name='e-'+hashlib.sha256(self.execution_id.encode('ascii')).hexdigest()[:32]
        with chapter_budget_lock(self.store.root/lock_name):
            self._bind(calls)
            yield

    def _bind(self, calls):
        root=assert_safe_local_tree(calls.root)
        frozen=self.store.frozen_budget()
        if frozen['budget_sha256']!=self.binding['budget_sha256']:
            raise ChapterBudgetConflict('execution frozen budget changed')
        if os.path.lexists(root/BINDING_FILE):
            try:actual=json.loads(read_project_bytes(root,BINDING_FILE))
            except (OSError,ValueError) as exc:
                raise ChapterBudgetError('execution budget binding is unreadable') from exc
            if actual!=self.binding:
                raise ChapterBudgetConflict('execution belongs to a different frozen chapter budget')
            return
        # Missing binding is only acceptable before the first durable attempt.
        # Existing unbudgeted calls cannot be adopted, even if already completed.
        if self.store.execution_attempts(self.execution_id):
            raise ChapterBudgetError('execution lost its original budget binding')
        for name in ('intents','receipts','responses'):
            directory=assert_safe_local_tree(root/name)
            if directory.exists() and any(directory.iterdir()):
                raise ChapterBudgetError('cannot attach an empty chapter budget to existing model calls')
        root.mkdir(parents=True,exist_ok=True)
        assert_safe_local_tree(root)
        atomic_create_json(root/BINDING_FILE,self.binding)

    def attempts(self):
        return self.store.execution_attempts(self.execution_id)

    def reserve(self, intent, **kwargs):
        return self.store.reserve(self.execution_id,intent,**kwargs)

    def mark_dispatched(self, intent):
        self.store.mark_dispatched(self.execution_id,intent)

    def settle(self, receipt):
        return self.store.settle(self.execution_id,receipt)
