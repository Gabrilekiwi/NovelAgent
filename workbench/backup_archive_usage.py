"""Recheck archived call/budget evidence without initializing or settling stores."""
from datetime import datetime,timezone
from decimal import Decimal
import json
from pathlib import Path
import re

from core.chapter_budget_contract import validate_chapter_budget
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_execution_time import ChapterExecutionClock,ChapterTimeError
from core.model_calls import ModelCallIntegrityError,model_response_artifact_hash
from core.model_call_usage import project_model_attempt,summarize_model_attempts
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_auxiliary import Evidence
from workbench.model_usage_budget import ProjectBudgetUsage


class _StoryEvidence(Evidence):
    def __init__(self,view):
        self.view=view;tree=view.manifest['preview']['scope']['inventories']['story_project']
        super().__init__(tree['source_root'],tree,_read_bytes=self.raw)

    def relative(self,path):
        path=Path(path)
        if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(self.root):
            raise BackupArchiveError('归档用量读取越过作品根。')
        return path.relative_to(self.root).as_posix()

    def raw(self,root,name):
        return self.view.read_root('story_project',self.relative(Path(root)/name))

    def exists(self,path):
        name=self.relative(path)
        return name in self.files or name in self.directories

    def document(self,root,name):
        return self.read(self.relative(Path(root)/name))

    def child_paths(self,path):
        relative=self.relative(path)
        if relative in self.files:raise ModelCallIntegrityError('archived evidence namespace is not a directory')
        return [self.root/name for name in self.entries(relative)]


class _BudgetStore(ChapterBudgetStore):
    def __init__(self,evidence,root,book_id,chapter):
        self.evidence=evidence;self.root=root;self.book_id=book_id;self.chapter=chapter

    def _read(self,name):return self.evidence.document(self.root,name)


class _Clock(ChapterExecutionClock):
    def __init__(self,evidence,root,budget,*,recovery=False):
        self.evidence=evidence;self.root=root;self.budget=validate_chapter_budget(budget)
        self.binding={key:self.budget[key] for key in ('book_id','chapter','budget_sha256')}
        self.limit=Decimal(self.budget['max_active_seconds']);self._is_local_recovery=recovery
        # Wall-clock projections are not part of the offline result. Only measured
        # intervals, their completeness and frozen active limits are retained.
        self.wall_clock=lambda:datetime(2000,1,1,tzinfo=timezone.utc)

    def _load(self):return self._validate_record(self.evidence.document(self.root,'time.json'))
    def _active(self):return False
    def _recovery_clock(self):return _Clock(self.evidence,self.root/'local-recovery',self.budget,recovery=True)

    def _has_recovery(self):
        if self._is_local_recovery:return False
        if not any(self.evidence.exists(self.root/name) for name in ('local-recovery.json','local-recovery')):return False
        if self.evidence.document(self.root,'local-recovery.json')!=self._recovery_marker():
            raise ChapterTimeError('archived local recovery binding differs')
        self._recovery_clock()._load()
        return True


class _Budgets(ProjectBudgetUsage):
    def __init__(self,evidence,book_id,block):
        self.root=evidence.root;self.book_id=book_id;self.chapters={};self.executions={};self.times=[]
        self._read_bytes=evidence.raw;self._exists=evidence.exists
        parent=self.root/'.novelagent/runtime/budgets';chapters=set();count=0
        for path in evidence.child_paths(parent):
            match=re.fullmatch(r'(?:[ir]([1-9][0-9]*)\.json|c([1-9][0-9]*))',path.name)
            if match:chapters.add(int(match.group(1) or match.group(2)))
        if len(chapters)>10000:raise ModelCallIntegrityError('archived budget count exceeds supported size')
        for chapter in sorted(chapters):
            intent=evidence.document(parent,f'i{chapter}.json')
            budget=validate_chapter_budget(intent.get('budget'),expected_book_id=book_id,expected_chapter=chapter)
            # These builders are pure; no FreezeRegistry constructor or load API.
            expected=ChapterBudgetFreezeRegistry._intent(None,budget)
            if intent!=expected or evidence.document(parent,f'r{chapter}.json')!=ChapterBudgetFreezeRegistry._ready(None,intent):
                raise ModelCallIntegrityError('archived budget initialization binding differs or is incomplete')
            snapshot=_BudgetStore(evidence,parent/f'c{chapter}',book_id,chapter).usage_snapshot()
            if snapshot['budget']!=budget:raise ModelCallIntegrityError('archived frozen budget differs from initialization')
            count+=len(snapshot['attempts'])
            if count>10000:raise ModelCallIntegrityError('archived budget attempts exceed supported size')
            self.chapters[chapter]=snapshot
            if snapshot['report']['unresolved_attempts']:block('chapter_budget_unsettled',str(chapter))
            time=_Clock(evidence,parent/f'c{chapter}',budget).report()
            self.times.append({key:time[key] for key in ('binding','interval_count','active_seconds','active_time_complete',
                'local_recovery_active_seconds','local_recovery_status','active_limit_seconds','active_limit_reached')})
            if not time['active_time_complete']:block('chapter_active_time_unsettled',str(chapter))
            for original,account in zip(snapshot['attempts'],snapshot['report']['items']):
                execution=original['execution_id'];existing=self.executions.setdefault(execution,{'chapter':chapter,'attempts':{}})
                if existing['chapter']!=chapter:raise ModelCallIntegrityError('execution appears in different archived budgets')
                existing['attempts'][original['plan']['intent']['attempt_id']]=(original,account)


def _usage(evidence,book_id,block):
    budgets=_Budgets(evidence,book_id,block);stores={};rows=[]
    executions=evidence.root/'.novelagent/runtime/runs/executions'
    for execution in evidence.child_paths(executions):
        if not execution.name.startswith('execution_'):continue
        if not re.fullmatch(r'execution_[a-zA-Z0-9_-]{1,180}',execution.name):
            raise ModelCallIntegrityError('invalid archived execution identity')
        store=execution/'model_calls'
        if evidence.exists(store):stores[execution.name]=store
    for family in ('workbench_outline_batches','workbench_amendments'):
        parent=evidence.root/'.novelagent/runtime/model_calls'/family
        for store in evidence.child_paths(parent):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,191}',store.name):
                raise ModelCallIntegrityError('invalid archived model-call operation identity')
            stores[family+':'+store.name]=store
    for execution in budgets.executions:stores.setdefault(execution,executions/execution/'model_calls')
    count=0
    for execution,store in sorted(stores.items()):
        if evidence.relative(store) not in evidence.directories:
            raise ModelCallIntegrityError('archived model-call store is missing or is not a directory')
        binding=budgets.bind(execution,store);names=set()
        for directory in ('intents','receipts'):
            names.update(path.stem for path in evidence.child_paths(store/directory) if path.suffix=='.json')
        if binding is not None:names.update(binding['attempts'])
        count+=len(names)
        if count>10000:raise ModelCallIntegrityError('archived model attempts exceed supported size')
        for name in sorted(names):
            label=execution+'/'+name
            if not evidence.exists(store/'intents'/(name+'.json')) and binding is not None and name in binding['attempts']:
                original,_=binding['attempts'][name]
                if not original['dispatch_claimed'] and original['receipt'] is None and not evidence.exists(store/'receipts'/(name+'.json')):
                    block('chapter_budget_unsettled',label);continue
            intent=evidence.document(store,'intents/'+name+'.json')
            if intent.get('attempt_id')!=name:raise ModelCallIntegrityError('archived model-call filename differs from identity')
            receipt=evidence.document(store,'receipts/'+name+'.json') if evidence.exists(store/'receipts'/(name+'.json')) else None
            item=project_model_attempt(execution,intent,receipt,active=False)
            if receipt is None:block('model_call_without_receipt',label)
            else:
                ref=receipt['response_artifact_ref']
                if not ref or model_response_artifact_hash(evidence.raw(store,ref))!=receipt['response_artifact_hash']:
                    raise ModelCallIntegrityError('archived model response is missing or differs')
            rows.append(budgets.project(binding,store,name,intent,receipt,item))
    rows.sort(key=lambda item:(item['created_at'],item['execution_id'],item['attempt_id']),reverse=True)
    return {'items':rows,'summary':summarize_model_attempts(rows),'chapter_budgets':budgets.reports(),'chapter_times':budgets.times}


def inspect_archived_usage(package,*,project_id,book_id):
    with verified_archive_view(package) as view:
        preview=view.manifest['preview'];evidence=_StoryEvidence(view)
        identity=evidence.document(evidence.root,'.novelagent/project.json')
        if preview['project_id']!=project_id or preview['book_id']!=book_id or identity.get('book_id')!=book_id:
            raise BackupArchiveError('归档用量目标与作品身份不同。')
        blockers=[]
        def block(code,reference):
            item={'code':code,'reference':reference}
            if item not in blockers:blockers.append(item)
        usage=None
        try:usage=_usage(evidence,book_id,block)
        except (OSError,ValueError,RuntimeError):block('model_or_budget_evidence_unverified','story_project')
        result={'schema_version':'archived_model_budget_usage_v1','archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],'project_id':project_id,'book_id':book_id,
            'usage':usage,'blockers':blockers,'model_and_budgets_verified':not blockers,
            'coverage':'chapter_and_workbench_model_attempts_frozen_budgets_and_active_intervals',
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
    return result
