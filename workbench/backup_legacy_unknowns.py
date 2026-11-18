"""Verify retained unknown fees for already resolved, pre-ledger executions.

A recovery/reset marker ends an execution, not its bill. Only that original
legacy contract is recognized. Candidate, extraction and bound-budget attempts
remain subject to their own stricter settlement checks.
"""
from pathlib import PurePosixPath
from datetime import datetime

from core.engine.locked_chapter_state import validate_locked_chapter_resolution

RUNS='.novelagent/runtime/runs'


class RetainedLegacyUnknowns:
    def __init__(self,evidence,book_id):
        self.evidence=evidence;self.book_id=book_id;self.records=[];self.retained=[]
        directory=RUNS+'/locked_chapter_resolutions'
        for name in evidence.entries(directory):
            if not PurePosixPath(name).name.startswith('resolution_') or not name.endswith('.json'):
                continue
            record=validate_locked_chapter_resolution(evidence.read(name))
            if record['book_id']!=book_id or PurePosixPath(name).stem!=record['id']:
                raise ValueError('旧调用处理记录的作品或文件名不同。')
            self.records.append((name,record))

    def recognize(self,execution,attempt):
        if (not execution.startswith('execution_') or execution.startswith(('execution_candidate_','execution_fx_'))):
            return None
        base=RUNS+'/executions/'+execution+'/model_calls'
        if base+'/b.json' in self.evidence.files:
            return None
        intent_path=base+'/intents/'+attempt+'.json'
        intent=self.evidence.read(intent_path)
        if intent.get('attempt_id')!=attempt:return None
        for name,record in self.records:
            if execution not in record['resolved_execution_ids'] or attempt not in record['resolved_attempt_ids']:
                continue
            source=RUNS+'/'+record['source_run_id']+'.json'
            if source in self.evidence.files:
                run=self.evidence.read(source).get('run',{})
                story=run.get('story_project',{})
                if (run.get('id')!=record['source_run_id'] or run.get('chapter_index')!=record['chapter_index']
                        or (run.get('book_id') or story.get('book_id'))!=self.book_id
                        or run.get('execution_evidence',{}).get('execution_id')!=execution):
                    continue
            elif not (record['action']=='reset' and record['source_run_id']=='unbound_'+execution
                    and record['reason'] in {'operator_requested_reset','provider_call_uncertain_without_run'}):
                continue
            if datetime.fromisoformat(record['created_at']) < datetime.fromisoformat(intent['created_at']):
                continue
            item={'execution_id':execution,'attempt_id':attempt,'intent_file_sha256':self.evidence.tree['files'][intent_path]['sha256'],
                'resolution_record':name,'resolution_hash':record['resolution_hash'],
                'resolution_file_sha256':self.evidence.tree['files'][name]['sha256'],
                'execution_status':'previously_resolved','fee_status':'unknown','receipt_created':False}
            if item not in self.retained:self.retained.append(item)
            return item
        return None
