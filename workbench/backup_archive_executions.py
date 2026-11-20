"""Offline candidate/fact validation reuses the normal content-binding rules."""
import copy
from types import SimpleNamespace

from core.engine import chapter_candidate as candidate,chapter_fact_extraction as facts
from core.model_calls import (PROVIDER_CALL_UNCERTAIN,validate_model_call_intent,validate_model_call_receipt,
    _verify_receipt_link,model_response_artifact_hash)
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError,_member
from workbench.backup_archive_usage import _StoryEvidence
from workbench.backup_archive_view import verified_archive_view


class _Calls:
    def __init__(self,evidence,root):
        self.source=evidence;self.root=root;self.intents={};self.receipts={};self.responses=[]
        for directory,output,validator in (('intents',self.intents,validate_model_call_intent),
                                           ('receipts',self.receipts,validate_model_call_receipt)):
            for path in evidence.child_paths(root/directory):
                if path.suffix!='.json':continue
                value=validator(evidence.document(path.parent,path.name))
                if value['attempt_id']!=path.stem:raise BackupArchiveError('归档模型凭据文件名与身份不同。')
                output[path.stem]=value
        if len(self.intents)+len(self.receipts)>20000:raise BackupArchiveError('归档执行调用数量超过支持范围。')
        for name,receipt in self.receipts.items():
            _verify_receipt_link(self.load_intent(name),receipt)
            ref=receipt['response_artifact_ref']
            if not ref:raise BackupArchiveError('归档执行回执缺少响应。')
            raw=evidence.raw(root,_member(ref))
            if model_response_artifact_hash(raw)!=receipt['response_artifact_hash']:
                raise BackupArchiveError('归档执行响应摘要不同。')
            if receipt['status']=='succeeded':self.responses.append((receipt,raw.decode('utf-8')))

    def load_intent(self,name):
        if name not in self.intents:raise BackupArchiveError('归档执行缺少模型调用意图。')
        return self.intents[name]

    def load_receipt(self,name):
        if name not in self.receipts:raise BackupArchiveError('归档执行缺少模型调用回执。')
        return self.receipts[name]

    def evidence(self):
        unresolved=[]
        for name,intent in self.intents.items():
            if name in self.receipts:continue
            unresolved.append({'status':PROVIDER_CALL_UNCERTAIN,**{key:copy.deepcopy(intent[key]) for key in
                ('call_id','attempt_id','intent_hash','request_digest','provider','model','stage','budget_reservation','created_at')}})
        unresolved.sort(key=lambda item:(item['created_at'],item['call_id'],item['attempt_id']))
        return {'intent_hashes':[item['intent_hash'] for item in self.intents.values()],
            'receipt_hashes':[item['receipt_hash'] for item in self.receipts.values()],
            'receipt_statuses':[item['status'] for item in self.receipts.values()],
            'unresolved_calls':unresolved,'selected_response':None}


class _ArchivedStore:
    def __init__(self,evidence,namespace):
        self.evidence=evidence;self.story_project_root=evidence.root
        runtime=evidence.root/'.novelagent/runtime'
        self.paths=SimpleNamespace(runtime_dir=runtime,run_dir=runtime/'runs')
        self.root=runtime/namespace;self.calls={}

    def _read_json(self,path,label):return self.evidence.document(path.parent,path.name)

    def _read_text(self,path):
        # Match Path.read_text's universal-newline semantics used by live records.
        return self.evidence.raw(path.parent,path.name).decode('utf-8').replace('\r\n','\n').replace('\r','\n')

    def _runtime_artifact_path(self,ref):return self.paths.runtime_dir/_member(ref)
    def _run_artifact_path(self,ref):return self.paths.run_dir/_member(ref)
    def _call_evidence(self,store):return store.evidence()

    def model_call_store(self,intent):
        execution=intent['execution'];ref=execution['model_calls_ref']
        if ref!='executions/'+execution['execution_id']+'/model_calls':
            raise BackupArchiveError('归档执行模型目录引用不符合约定。')
        root=self._run_artifact_path(ref)
        if root not in self.calls:self.calls[root]=_Calls(self.evidence,root)
        return self.calls[root]


class _Candidates(_ArchivedStore,candidate.ChapterCandidateStore):
    def __init__(self,evidence):super().__init__(evidence,'candidates')
    def candidate_dir(self,identity):return self.root/candidate._validate_candidate_id(identity)
    def has_record(self,identity):return self.evidence.exists(self.record_path(identity))
    def _selected_response(self,store,prose):
        return candidate._selected_response_binding(store,prose,_responses=store.responses)


class _Facts(_ArchivedStore,facts.ChapterFactExtractionStore):
    def __init__(self,evidence):super().__init__(evidence,'fact_extractions')
    def extraction_dir(self,identity):return self.root/facts._validate_extraction_id(identity)
    def has_record(self,identity):return self.evidence.exists(self.record_path(identity))
    def _selected_response(self,store,selected):
        return facts._load_selected_response(store,selected,_responses=store.responses)


def inspect_archived_executions(package,*,project_id,book_id):
    with verified_archive_view(package) as view:
        preview=view.manifest['preview'];evidence=_StoryEvidence(view)
        identity=evidence.document(evidence.root,'.novelagent/project.json')
        if preview['project_id']!=project_id or preview['book_id']!=book_id or identity.get('book_id')!=book_id:
            raise BackupArchiveError('归档执行目标与作品身份不同。')
        records=[];blockers=[];count=0
        for store in (_Candidates(evidence),_Facts(evidence)):
            try:
                for path in evidence.child_paths(store.root):
                    count+=1
                    if count>10000:raise BackupArchiveError('归档执行数量超过支持范围。')
                    label=store.root.name+'/'+path.name
                    record=store.load(path.name)
                    if record['intent']['book_id']!=book_id:
                        blockers.append({'code':'execution_book_mismatch','reference':label})
                    status=record['record']['status'] if record['record'] is not None else 'running'
                    if status in {'running','provider_uncertain','needs_recovery'}:
                        blockers.append({'code':'execution_not_settled','reference':label})
                    records.append({'reference':label,'state':status,'content_sha256':digest(record),
                        'intent_hash':record['intent']['intent_hash'],
                        'record_hash':record['record']['record_hash'] if record['record'] is not None else None})
            except (OSError,ValueError,RuntimeError):
                blockers.append({'code':'execution_evidence_unverified','reference':store.root.name})
        result={'schema_version':'archived_execution_settlement_v1','archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],'project_id':project_id,'book_id':book_id,
            'executions':records,'blockers':blockers,'executions_verified':not blockers,
            'coverage':'candidate_and_fact_intents_artifacts_provenance_calls_records_and_selected_responses',
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
    return result
