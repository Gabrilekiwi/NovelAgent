"""Attribute usage to original frozen chapter prices without settling anything."""
import json
import os
from pathlib import Path
import re

from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_model_budget import BINDING_FILE
from core.engine.safe_paths import assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import ModelCallIntegrityError, model_response_artifact_hash
from core.story_project.reviewed_state_document import read_project_bytes


class ProjectBudgetUsage:
    def __init__(self, root, *, book_id, max_attempts):
        self.root=Path(root)
        self.book_id=book_id
        self._read_bytes=read_project_bytes
        self._exists=os.path.lexists
        self.chapters={}
        self.executions={}
        registry=ChapterBudgetFreezeRegistry(root,book_id=book_id)
        if not registry.root.exists():return
        chapters=set()
        for path in registry.root.iterdir():
            match=re.fullmatch(r'(?:[ir]([1-9][0-9]*)\.json|c([1-9][0-9]*))',path.name)
            if match:chapters.add(int(match.group(1) or match.group(2)))
        if chapters and not book_id:
            raise ModelCallIntegrityError('registered book identity is required for budget attribution')
        count=0
        for chapter in sorted(chapters):
            state=registry.status(chapter)
            if state['state']=='initializing':
                ledger=registry.root/f'c{chapter}/ledger.json'
                if os.path.lexists(ledger):
                    value=json.loads(read_project_bytes(registry.root,ledger.relative_to(registry.root).as_posix()))
                    if not isinstance(value,dict) or value.get('attempts')!=[]:
                        raise ModelCallIntegrityError('unfinished budget initialization contains spending evidence')
                continue
            if state['state']!='ready':continue
            store=ChapterBudgetStore(registry.root/f'c{chapter}',book_id=book_id,chapter=chapter)
            snapshot=store.usage_snapshot()
            if snapshot['budget']!=state['budget']:
                raise ModelCallIntegrityError('frozen budget changed while reading usage')
            count+=len(snapshot['attempts'])
            if count>max_attempts:
                raise ModelCallIntegrityError('budget history exceeds report size; no partial total was produced')
            self.chapters[chapter]=snapshot
            for original,account in zip(snapshot['attempts'],snapshot['report']['items']):
                execution=original['execution_id']
                existing=self.executions.setdefault(execution,{'chapter':chapter,'attempts':{}})
                if existing['chapter']!=chapter:
                    raise ModelCallIntegrityError('execution appears in different chapter budgets')
                existing['attempts'][original['plan']['intent']['attempt_id']]=(original,account)

    def bind(self, execution_id, store):
        """Verify a physical store's existing binding, including lost b.json."""
        existing=self.executions.get(execution_id)
        path=store/BINDING_FILE
        if not self._exists(path):
            if existing:
                raise ModelCallIntegrityError('model-call store lost its original budget binding')
            return None
        value=json.loads(self._read_bytes(store,BINDING_FILE))
        chapter=value.get('chapter') if isinstance(value,dict) else None
        if type(chapter) is not int or chapter not in self.chapters:
            raise ModelCallIntegrityError('execution binding has no ready chapter budget')
        budget=self.chapters[chapter]['budget']
        expected=dict(schema_version='chapter_execution_budget_v1',book_id=self.book_id,
            chapter=chapter,budget_sha256=budget['budget_sha256'],execution_id=execution_id,
            attempt_protocol='reserve_intent_dispatch_receipt_v1')
        expected['binding_sha256']=canonical_json_hash(expected,exclude_environment_fields=False)
        if value!=expected or existing and existing['chapter']!=chapter:
            raise ModelCallIntegrityError('execution belongs to a different frozen chapter budget')
        return existing or {'chapter':chapter,'attempts':{}}

    def project(self, binding, store, name, intent, receipt, item):
        if binding is None:return item
        pair=binding['attempts'].get(name)
        if pair is None or pair[0]['plan']['intent']!=intent:
            raise ModelCallIntegrityError('physical attempt has no matching original reservation')
        original,account=pair
        # A receipt may have been written after this snapshot was taken. Keep
        # that reservation pending; a read must never perform reconciliation.
        if original['receipt'] is not None and original['receipt']!=receipt:
            raise ModelCallIntegrityError('settled budget receipt differs from physical evidence')
        if receipt is not None:
            ref=receipt['response_artifact_ref']
            if not ref or model_response_artifact_hash(self._read_bytes(store,ref))!=receipt['response_artifact_hash']:
                raise ModelCallIntegrityError('budget usage response artifact is missing or differs')
        frozen=self.chapters[binding['chapter']]['budget']
        item['budget_accounting']={**account,'chapter':binding['chapter'],
            'budget_sha256':frozen['budget_sha256'],'currency':frozen['currency']}
        if (account['accounting_state']=='settled' and account['cost_classification']=='estimated'
                and item['cost']['classification']!='actual'):
            calculation={**account['calculation'],'model':receipt['actual_model'],
                'endpoint_type':receipt['endpoint_type'],
                'input_token_semantics':original['plan']['pricing']['input_token_semantics']}
            item['cost']=dict(classification='estimated',amount=calculation['amount'],
                currency=calculation['currency'],source='frozen_chapter_pricing',
                source_id=frozen['budget_sha256'],reason=None,calculation=calculation)
        return item

    def reports(self):
        return [snapshot['report'] for snapshot in self.chapters.values()]
