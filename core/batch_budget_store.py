"""Batch reservations use the original chapter ledgers, not a second total.

Financial scope only. A ready budget never authenticates a batch execution
grant, policy review, or permission to dispatch a Provider.
"""
import json,os,re
from pathlib import Path

from core.batch_budget_contract import (validate_batch_budget,chapter_batch_binding,batch_ready,project_batch_usage)
from core.chapter_budget_lock import chapter_budget_lock
from core.chapter_budget_store import ChapterBudgetStore,ChapterBudgetError,ChapterBudgetConflict
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_execution_time import ChapterExecutionClock
from core.engine.persistence import atomic_create_json
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import read_project_bytes


def coordinator_lock(budgets_root):
    return chapter_budget_lock(Path(budgets_root)/'aggregate-lock')


def read_batch_manifests(root,*,book_id,read,entries,require_ready=True):
    """Read immutable manifest/ready pairs using live or archived bytes."""
    names=list(entries(Path(root)/'batches'))
    if len(names)>128:raise ChapterBudgetError('batch manifest count exceeds supported size')
    manifests={};ready={}
    for name in names:
        match=re.fullmatch(r'(r?)(batch_[0-9a-f]{32})\.json',name)
        if match is None:raise ChapterBudgetError('unexpected batch budget evidence member')
        value=read(Path(root)/'batches',name)
        if match[1]:ready[match[2]]=value
        else:
            batch=validate_batch_budget(value,book_id=book_id)
            if batch['batch_id']!=match[2]:raise ChapterBudgetConflict('batch manifest filename differs')
            manifests[match[2]]=batch
    if set(ready)-set(manifests):raise ChapterBudgetError('batch ready receipt lost its original manifest')
    result=[]
    claimed=set()
    for identity,batch in sorted(manifests.items()):
        chapters={b['chapter'] for b in batch['chapter_budgets']}
        if claimed & chapters:raise ChapterBudgetConflict('one chapter cannot belong to multiple batch budgets')
        claimed|=chapters
        completed=identity in ready
        if completed and ready[identity]!=batch_ready(batch):raise ChapterBudgetConflict('batch ready receipt differs')
        if require_ready and not completed:raise ChapterBudgetError('batch budget initialization is incomplete')
        result.append((batch,completed))
    return result


def inspect_batch_budgets(root,*,book_id,snapshots,read,entries):
    batches=read_batch_manifests(root,book_id=book_id,read=read,entries=entries)
    members={};reports=[]
    for batch,_ in batches:
        for budget in batch['chapter_budgets']:
            chapter=budget['chapter'];members[chapter]=chapter_batch_binding(batch,chapter)
            if (chapter not in snapshots or snapshots[chapter]['budget']!=budget
                    or snapshots[chapter].get('batch_budget')!=members[chapter]):
                raise ChapterBudgetError('batch chapter lost its original budget or ledger binding')
        reports.append(project_batch_usage(batch,snapshots))
    for chapter,snapshot in snapshots.items():
        if snapshot.get('batch_budget') is not None and snapshot['batch_budget']!=members.get(chapter):
            raise ChapterBudgetError('chapter lost or changed its original batch manifest')
    return reports


class BatchBudgetRegistry:
    def __init__(self,budgets_root,*,book_id):
        self.root=assert_safe_local_tree(Path(budgets_root));self.book_id=book_id

    def _read(self,root,name):
        try:return json.loads(read_project_bytes(root,name))
        except (OSError,ValueError) as exc:raise ChapterBudgetError('batch evidence is missing or unreadable') from exc

    def _entries(self,path):
        path=assert_safe_local_tree(path)
        if not os.path.lexists(path):return []
        if not path.is_dir():raise ChapterBudgetError('batch namespace is not a directory')
        names=[p.name for p in path.iterdir()]
        if len(names)>128:raise ChapterBudgetError('batch evidence count exceeds supported size')
        return names

    def records(self,*,require_ready=True):
        return read_batch_manifests(self.root,book_id=self.book_id,read=self._read,
            entries=self._entries,require_ready=require_ready)

    def _snapshot(self,chapter):
        # Require the actual original chapter freeze, including its clock.
        registry=ChapterBudgetFreezeRegistry(self.root.parent.parent.parent,book_id=self.book_id)
        if registry.root!=self.root:raise ChapterBudgetError('batch requires the canonical book budget root')
        budget=registry.load(chapter)
        snapshot=ChapterBudgetStore(self.root/f'c{chapter}',book_id=self.book_id,chapter=chapter).usage_snapshot()
        if snapshot['budget']!=budget:raise ChapterBudgetConflict('batch frozen chapter changed')
        return snapshot

    def load(self,batch_id):
        records=self.records()
        batch=next((b for b,_ in records if b['batch_id']==batch_id),None)
        if batch is None:raise ChapterBudgetError('original batch budget is missing')
        snapshots={b['chapter']:self._snapshot(b['chapter']) for b in batch['chapter_budgets']}
        for budget in batch['chapter_budgets']:
            if snapshots[budget['chapter']].get('batch_budget')!=chapter_batch_binding(batch,budget['chapter']):
                raise ChapterBudgetError('batch ledger binding is missing or different')
        return batch,snapshots

    def for_chapter(self,chapter,value):
        records=self.records()
        batch=next((b for b,_ in records if any(c['chapter']==chapter for c in b['chapter_budgets'])),None)
        binding=value.get('batch_budget')
        if batch is None:
            if binding is not None:raise ChapterBudgetError('chapter lost its batch budget manifest')
            return None
        if binding!=chapter_batch_binding(batch,chapter):
            raise ChapterBudgetError('chapter cannot opt out of its original batch budget')
        return self.load(batch['batch_id'])

    def freeze(self,batch,*,before_write,fault_injector=None):
        """Explicit first publication/resume under service book/Provider locks.

        before_write must verify real owner, source/selection and authorization.
        No method here creates an execution grant or automatically starts work.
        """
        batch=validate_batch_budget(batch,book_id=self.book_id)
        if not callable(before_write):raise ChapterBudgetError('batch owner/source preflight is required')
        def fault(event):
            if fault_injector is not None:fault_injector(event)
        with coordinator_lock(self.root):
            existing=self.records(require_ready=False)
            old=next(((b,r) for b,r in existing if b['batch_id']==batch['batch_id']),None)
            if old and old[0]!=batch:raise ChapterBudgetConflict('batch budget is already frozen differently')
            selected={b['chapter'] for b in batch['chapter_budgets']}
            if any(selected & {c['chapter'] for c in b['chapter_budgets']} for b,_ in existing if b['batch_id']!=batch['batch_id']):
                raise ChapterBudgetConflict('chapter is already assigned to another batch')
            if old and old[1]:return self.load(batch['batch_id'])[0]
            if old is None and len(existing)>=64:raise ChapterBudgetError('batch count exceeds supported size')
            registry=ChapterBudgetFreezeRegistry(self.root.parent.parent.parent,book_id=self.book_id)
            if registry.root!=self.root:raise ChapterBudgetError('batch requires canonical book budget root')
            # Reject an already-used or differently frozen chapter before
            # publishing a new batch intent. The coordinator excludes new
            # reservations while this selection is checked and bound.
            for budget in batch['chapter_budgets']:
                status=registry.status(budget['chapter'])
                if status['budget'] is not None and status['budget']!=budget:
                    raise ChapterBudgetConflict('batch differs from an original chapter budget')
                if status['state']=='ready':
                    store=ChapterBudgetStore(self.root/f"c{budget['chapter']}",book_id=self.book_id,chapter=budget['chapter'])
                    _,value=store._load()
                    if value['attempts'] or value.get('repair_cycle') is not None or ChapterExecutionClock(store.root,budget).report()['interval_count']:
                        raise ChapterBudgetConflict('cannot adopt prior execution into a new batch')
                    if value.get('batch_budget') is not None and value['batch_budget']!=chapter_batch_binding(batch,budget['chapter']):
                        raise ChapterBudgetConflict('chapter belongs to another batch')
            before_write()
            if old is None:atomic_create_json(self.root/'batches'/(batch['batch_id']+'.json'),batch)
            fault('after_batch_intent')
            stores=[]
            for budget in batch['chapter_budgets']:
                registry.freeze(budget,before_write=before_write)
                store=ChapterBudgetStore(self.root/f"c{budget['chapter']}",book_id=self.book_id,chapter=budget['chapter'])
                _,value=store._load()
                if value['attempts'] or value.get('repair_cycle') is not None or ChapterExecutionClock(store.root,budget).report()['interval_count']:
                    raise ChapterBudgetConflict('cannot adopt prior chapter execution into a new batch')
                binding=value.get('batch_budget')
                if binding is not None and binding!=chapter_batch_binding(batch,budget['chapter']):
                    raise ChapterBudgetConflict('chapter has a different original batch binding')
                stores.append(store)
                fault('after_chapter_budget')
            for store in stores:
                with store._locked():
                    budget,value=store._load()
                    if value['attempts'] or value.get('repair_cycle') is not None:
                        raise ChapterBudgetConflict('chapter execution changed before batch binding')
                    expected=chapter_batch_binding(batch,budget['chapter'])
                    if value.get('batch_budget') is not None and value['batch_budget']!=expected:
                        raise ChapterBudgetConflict('chapter batch binding changed')
                    before_write()
                    if value.get('batch_budget') is None:
                        value.update(schema_version='chapter_budget_ledger_v4',repair_cycle=None,batch_budget=expected)
                        store._save(value)
                    fault('after_chapter_batch_binding')
            before_write()
            atomic_create_json(self.root/'batches'/('r'+batch['batch_id']+'.json'),batch_ready(batch))
            fault('after_batch_ready')
            return self.load(batch['batch_id'])[0]
