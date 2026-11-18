"""Actual policy generation under original batch controls, without author cards."""
import os

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_candidate import _candidate_id,_execution_id
from core.engine.persistence import atomic_create_json
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,validate_control
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_policy_generation import (generation_input_sha256,policy_generation_authorization,
    require_policy_generation_scope)
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.restore_execution_policy import require_restored_execution_paths


class ReviewedPolicyGenerationService:
    def __init__(self,controls,*,executor_factory=None,fault_injector=None,stage='generation'):
        if stage not in {'generation','repair_generation'}:raise ValueError('invalid policy generation stage')
        self.stage=stage
        self.controls=controls;self.publication=controls.publication;self.fault_injector=fault_injector
        self.executor_factory=executor_factory or (lambda root:ReviewedChapterCandidateExecutor(
            story_project_root=root,execution_budget_factory=ChapterExecutionBudget))

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    def _plan(self,handle):
        if self.controls.handles.get(getattr(handle,'batch_id',None)) is not handle or handle.service is not self.controls:
            raise BatchControlPaused('自动生成需要本服务原批次明确执行许可。')
        with self.publication._owned_project(handle.project_id) as context:
            grant=self.controls.batches._require_current_execution(context,handle.batch_id)['grant']
            record,state=self.controls._store(context,handle.batch_id,grant).load()
            if state['status']!='running' or state['run_epoch']!=handle.epoch or state['stage']!=self.stage:
                raise BatchControlPaused('原批次当前没有待生成步骤。')
            return self._prepare_plan(context,grant,record,state['chapter'])

    def _prepare_plan(self,context,grant,record,chapter,*,allow_edited_draft=False):
        """Pure original inputs, shared by execution and dispatch-free recovery."""
        outline=next(o for o in grant['manifest']['outlines'] if o['chapter']==chapter);blueprint=outline['blueprint']
        chapter_intent={'goal':blueprint['core_event'],'validation_focus':['continuity','spatial','logic'],'notes':[]}
        repair=None
        if self.stage=='repair_generation':
            from workbench.reviewed_policy_repairs import repair_generation_inputs
            chapter_intent,repair=repair_generation_inputs(self.controls,context[5],grant,chapter,record,allow_edited_draft=allow_edited_draft)
        key=('policy-repair:' if repair is not None else 'policy-generation:')+grant['manifest']['batch_id']+':'+str(chapter)
        candidate_id=_candidate_id(book_id=grant['manifest']['book_id'],chapter_index=chapter,idempotency_key=key)
        executor=self.executor_factory(context[5]);prepared=executor.prepare_policy_request(chapter_index=chapter,chapter_intent=chapter_intent)
        return dict(root=context[5],grant=grant,chapter=chapter,chapter_intent=chapter_intent,key=key,
            candidate_id=candidate_id,execution_id=_execution_id(candidate_id),executor=executor,prepared=prepared,repair=repair)

    def get(self,*,project_id,batch_id,chapter):
        from workbench.reviewed_stage_recovery import read_stage_result
        return dict(result=read_stage_result(self.controls,project_id,batch_id,chapter,self.stage),execution_permit=False)

    def recover_finish(self,*,project_id,batch_id,chapter):
        saved=self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']
        def verify(context,grant,control,pending):
            plan=self._prepare_plan(context,grant,control,chapter,allow_edited_draft=True)
            auth=policy_generation_authorization(grant=grant,prepared=plan['prepared'],
                binding={'step_id':saved['step_id'],'execution_id':plan['execution_id']},repair=plan['repair'])
            if (pending['stage']!=self.stage or pending['execution_id']!=plan['execution_id']
                    or pending['input_sha256']!=generation_input_sha256(plan['prepared'])
                    or saved['input_sha256']!=pending['input_sha256'] or saved['execution_id']!=plan['execution_id']
                    or saved['candidate_id']!=plan['candidate_id']
                    or self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']!=saved):
                raise BatchControlError('原生成完成命令与完整原输入、Candidate或结果不同。')
            return self._verify(plan,saved,auth,preserve_author_edit=True)
        final=self.controls._reconcile_original_finish(project_id=project_id,batch_id=batch_id,
            idempotency_key='finish-'+self.stage+':'+str(chapter),result_sha256=saved['result_sha256'],step_id=saved['step_id'],verify_result=verify)
        return dict(result=saved,control=final,execution_permit=False,publication_authorized=False)

    def generate(self,handle,*,expected_control_sha256):
        plan=self._plan(handle);executor=plan['executor'];chapter=plan['chapter'];grant=plan['grant']
        source_sha=generation_input_sha256(plan['prepared'])
        def inputs():
            if plan['repair'] is not None:
                from workbench.reviewed_policy_repairs import repair_generation_inputs
                context=self.controls._control_context(handle.project_id)
                store=self.controls._store(context,handle.batch_id,grant)
                record,_=validate_control(store._read(store.machine_path),grant)
                if repair_generation_inputs(self.controls,plan['root'],grant,chapter,record)!=(plan['chapter_intent'],plan['repair']):
                    raise BatchControlError('原修复输入或资格发生变化。')
            return generation_input_sha256(executor.prepare_policy_request(chapter_index=chapter,chapter_intent=plan['chapter_intent']))
        with self.controls.step(handle,stage=self.stage,execution_id=plan['execution_id'],input_sha256=source_sha,
                idempotency_key='claim-'+self.stage+':'+str(chapter),expected_control_sha256=expected_control_sha256,verify_inputs=inputs) as permit:
            authorization=policy_generation_authorization(grant=grant,prepared=plan['prepared'],binding=permit.binding,repair=plan['repair'])
            result=executor.execute_policy(chapter_index=chapter,chapter_intent=plan['chapter_intent'],
                policy_authorization=authorization,idempotency_key=plan['key'])
            if result['status']!='completed':raise BatchControlError('原自动生成尚未产生可继续的完整 Candidate。')
            require_policy_generation_scope(authorization,prepared=plan['prepared'])
            self._fault('after_candidate_before_draft')
            prose=executor.store.load_prose(plan['candidate_id'])
            draft=self.publication.preparations.drafts.create_from_candidate(project_id=handle.project_id,chapter=chapter,
                candidate_id=plan['candidate_id'],source_sha256=result['prose']['sha256'],
                source_artifact_ref=result['prose']['artifact_ref'],text=prose)
            receipt=dict(schema_version='reviewed_policy_repair_generation_result_v1' if plan['repair'] else 'reviewed_policy_generation_result_v1',batch_id=handle.batch_id,
                grant_sha256=grant['grant_sha256'],step_id=permit.binding['step_id'],execution_id=plan['execution_id'],
                input_sha256=source_sha,candidate_id=plan['candidate_id'],candidate_intent_hash=result['intent_hash'],
                candidate_record_hash=result['record_hash'],draft_id=draft.id,draft_revision=1,draft_sha256=result['prose']['sha256'])
            if plan['repair'] is not None:receipt.update(plan['repair'])
            receipt['result_sha256']=digest(receipt)
            path=plan['root']/'.novelagent/runtime/reviewed/batch_results'/(permit.binding['step_id']+'.json')
            require_restored_execution_paths(path)
            self._verify(plan,receipt,authorization)
            if os.path.lexists(path):
                if self.controls.batches._read(path)!=receipt:raise BatchControlError('原自动生成结果凭据不同。')
            else:atomic_create_json(path,receipt)
        self._fault('after_generation_result_before_finish')
        def verify():
            if self.controls.batches._read(path)!=receipt:raise BatchControlError('原生成结果凭据变化。')
            if inputs()!=source_sha:raise BatchControlError('完成生成时原输入变化。')
            return self._verify(plan,receipt,authorization)
        current=self.controls.get(project_id=handle.project_id,batch_id=handle.batch_id)
        final=self.controls.finish_step(handle,expected_control_sha256=current['control']['record_sha256'],
            idempotency_key='finish-'+self.stage+':'+str(chapter),result_sha256=receipt['result_sha256'],verify_result=verify)
        return {'generation':receipt,'draft':draft.to_dict(),'control':final,'request_source':'policy','author_confirmed':False}

    def _verify(self,plan,receipt,authorization,*,preserve_author_edit=False):
        store=plan['executor'].store;loaded=store.load(plan['candidate_id']);intent=loaded['intent'];record=loaded['record']
        draft=self.publication.preparations.drafts.get(receipt['draft_id'])
        if (not isinstance(record,dict) or record['status']!='completed' or intent.get('policy_authorization')!=authorization
                or intent['execution']['execution_id']!=receipt['execution_id']
                or intent['intent_hash']!=receipt['candidate_intent_hash'] or record['record_hash']!=receipt['candidate_record_hash']
                or record['prose']['sha256']!=receipt['draft_sha256'] or draft.candidate_id!=receipt['candidate_id']
                or draft.project_id!=plan['grant']['manifest']['project_id'] or draft.chapter!=plan['chapter']
                or (not preserve_author_edit and (draft.revision!=receipt['draft_revision'] or draft.sha256!=receipt['draft_sha256']
                    or draft.text!=store.load_prose(plan['candidate_id'])))
                or (preserve_author_edit and (draft.revision<receipt['draft_revision']
                    or draft.source_sha256!=record['prose']['sha256'] or draft.source_artifact_ref!=record['prose']['artifact_ref']
                    or draft.sha256!=digest(draft.text.encode('utf-8'))))
                or generation_input_sha256(plan['executor'].prepare_policy_request(
                    chapter_index=plan['chapter'],chapter_intent=plan['chapter_intent']))!=receipt['input_sha256']):
            raise BatchControlError('原完整 Candidate、模型凭据、输入或作者稿已变化，请转人工处理。')
        return receipt['result_sha256']
