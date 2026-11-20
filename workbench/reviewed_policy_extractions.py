"""Full policy extraction from actual original generation evidence, never author import."""
import os

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_fact_context import build_chapter_fact_context
from core.engine.chapter_fact_extraction import (ChapterFactExtractionExecutor,_extraction_id_from_digest,
    _execution_id,_load_single_successful_response)
from core.engine.persistence import atomic_create_json
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.reviewed_policy_extraction import (extraction_input_sha256,policy_extraction_authorization,
    require_policy_extraction_scope)
from core.story_project.reviewed_state_document import digest
from workbench.restore_execution_policy import require_restored_execution_paths


class ReviewedPolicyExtractionService:
    def __init__(self,controls,*,executor_factory=None,fault_injector=None,stage='extraction'):
        if stage not in {'extraction','repair_extraction'}:raise ValueError('invalid policy extraction stage')
        self.stage=stage
        self.controls=controls;self.publication=controls.publication;self.fault_injector=fault_injector
        self.executor_factory=executor_factory or (lambda root:ChapterFactExtractionExecutor(
            story_project_root=root,execution_budget_factory=ChapterExecutionBudget))

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    def _inputs(self,root,grant,generation,control,*,allow_edited_draft=False):
        m=grant['manifest'];chapter=generation['chapter']
        receipt=generation['receipt'];store=ChapterCandidateStore(root);loaded=store.load(receipt['candidate_id'])
        intent,record=loaded['intent'],loaded['record']
        draft,verified=self.publication.preparations.fact_reviews.verify_draft_source(receipt['draft_id'])
        trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter)
        repair=receipt.get('schema_version')=='reviewed_policy_repair_generation_result_v1'
        if repair:
            from workbench.reviewed_policy_repairs import require_original_repair
            saved,claim=require_original_repair(self.controls,root,grant,chapter,control,allow_edited_draft=allow_edited_draft)
            expected={'repair_claim_sha256':claim['claim_sha256'],'original_quality_result_sha256':saved['result_sha256'],
                'original_draft_sha256':saved['review']['policy_review']['source_draft_sha256']}
            if any(receipt.get(k)!=v or intent.get('policy_authorization',{}).get(k)!=v for k,v in expected.items()):
                raise BatchControlError('修复稿生成凭据与原消耗资格不同。')
        if (receipt['schema_version'] not in {'reviewed_policy_generation_result_v1','reviewed_policy_repair_generation_result_v1'} or receipt['batch_id']!=m['batch_id']
                or receipt['grant_sha256']!=grant['grant_sha256'] or receipt['result_sha256']!=digest({k:v for k,v in receipt.items() if k!='result_sha256'})
                or not any(c['action']=='finish_step' and c['payload']['step_id']==receipt['step_id']
                    and c['payload']['result_sha256']==receipt['result_sha256'] for c in control['commands'])
                or not isinstance(record,dict) or record['status']!='completed' or verified!=intent
                or intent.get('policy_authorization',{}).get('grant_sha256')!=grant['grant_sha256']
                or intent['intent_hash']!=receipt['candidate_intent_hash'] or record['record_hash']!=receipt['candidate_record_hash']
                or intent['execution']['execution_id']!=receipt['execution_id']
                or intent['source']['context_sha256']!=trusted.context_sha256
                or draft.project_id!=m['project_id'] or draft.chapter!=chapter or draft.candidate_id!=receipt['candidate_id']
                or (not allow_edited_draft and (draft.revision!=receipt['draft_revision'] or draft.sha256!=receipt['draft_sha256']
                    or draft.text!=store.load_prose(receipt['candidate_id'])))
                or (allow_edited_draft and (draft.revision<receipt['draft_revision']
                    or draft.source_sha256!=record['prose']['sha256'] or draft.source_artifact_ref!=record['prose']['artifact_ref']
                    or record['prose']['sha256']!=receipt['draft_sha256'] or draft.sha256!=digest(draft.text.encode('utf-8'))))):
            raise BatchControlError('原生成阶段凭据、完整正文或作者稿发生变化，请转人工处理。')
        text=store.load_prose(receipt['candidate_id']) if allow_edited_draft else draft.text
        return dict(book_id=m['book_id'],chapter_index=chapter,
            candidate_binding={'candidate_id':draft.candidate_id,'intent_hash':intent['intent_hash']},
            draft_binding={'draft_id':draft.id,'revision':receipt['draft_revision'] if allow_edited_draft else draft.revision,
                'sha256':receipt['draft_sha256'] if allow_edited_draft else draft.sha256},
            context_sha256=trusted.context_sha256,authority_binding=intent['authority'],chapter_text=text,
            authority_view=build_chapter_fact_context(trusted,text).to_dict())

    def _plan(self,handle):
        if self.controls.handles.get(getattr(handle,'batch_id',None)) is not handle or handle.service is not self.controls:
            raise BatchControlPaused('自动事实提取需要本服务原批次明确执行许可。')
        with self.publication._owned_project(handle.project_id) as context:
            grant=self.controls.batches._require_current_execution(context,handle.batch_id)['grant']
            control,state=self.controls._store(context,handle.batch_id,grant).load()
            if state['status']!='running' or state['run_epoch']!=handle.epoch or state['stage']!=self.stage:
                raise BatchControlPaused('原批次当前没有待提取步骤。')
            return self._prepare_plan(context,grant,control,state['chapter'])

    def _prepare_plan(self,context,grant,control,chapter,*,allow_edited_draft=False):
        root=context[5];batch=grant['manifest']['batch_id']
        generation_stage='repair_generation' if self.stage=='repair_extraction' else 'generation'
        generation_step='step_'+digest({'batch_id':batch,'chapter':chapter,'stage':generation_stage})[:32]
        generation_path=root/'.novelagent/runtime/reviewed/batch_results'/(generation_step+'.json')
        generation={'chapter':chapter,'receipt':self.controls.batches._read(generation_path)}
        if generation['receipt']['step_id']!=generation_step:raise BatchControlError('提取来源不属于原生成步骤。')
        request=self._inputs(root,grant,generation,control,allow_edited_draft=allow_edited_draft)
        key=('policy-repair-extraction:' if self.stage=='repair_extraction' else 'policy-extraction:')+batch+':'+str(chapter)
        request['idempotency_key']=key
        executor=self.executor_factory(root);prepared=executor.prepare_policy_request(**request)
        extraction_id=_extraction_id_from_digest(book_id=grant['manifest']['book_id'],chapter_index=chapter,
            idempotency_key_sha256=digest(key.encode('utf-8')))
        return dict(root=root,grant=grant,generation=generation,generation_path=generation_path,control=control,
            request=request,prepared=prepared,chapter=chapter,executor=executor,extraction_id=extraction_id,
            execution_id=_execution_id(extraction_id))

    def get(self,*,project_id,batch_id,chapter):
        from workbench.reviewed_stage_recovery import read_stage_result
        return dict(result=read_stage_result(self.controls,project_id,batch_id,chapter,self.stage),execution_permit=False)

    def recover_finish(self,*,project_id,batch_id,chapter):
        saved=self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']
        def verify(context,grant,control,pending):
            plan=self._prepare_plan(context,grant,control,chapter,allow_edited_draft=True)
            auth=policy_extraction_authorization(grant=grant,prepared=plan['prepared'],
                generation_result_sha256=plan['generation']['receipt']['result_sha256'],
                binding={'step_id':saved['step_id'],'execution_id':plan['execution_id']},
                repair_claim_sha256=plan['generation']['receipt'].get('repair_claim_sha256'))
            def inputs():
                request=self._inputs(plan['root'],grant,plan['generation'],control,allow_edited_draft=True)
                return extraction_input_sha256(plan['executor'].prepare_policy_request(**request,idempotency_key=plan['request']['idempotency_key']))
            if (pending['stage']!=self.stage or pending['execution_id']!=plan['execution_id']
                    or saved['extraction_id']!=plan['extraction_id'] or saved['execution_id']!=plan['execution_id']
                    or pending['input_sha256']!=extraction_input_sha256(plan['prepared'])
                    or saved['input_sha256']!=pending['input_sha256']
                    or self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']!=saved):
                raise BatchControlError('原提取完成命令与完整正文、输入或实际结果不同。')
            return self._verify(plan,saved,auth,inputs)
        final=self.controls._reconcile_original_finish(project_id=project_id,batch_id=batch_id,
            idempotency_key='finish-'+self.stage+':'+str(chapter),result_sha256=saved['result_sha256'],step_id=saved['step_id'],verify_result=verify)
        return dict(result=saved,control=final,execution_permit=False,publication_authorized=False)

    def extract(self,handle,*,expected_control_sha256):
        plan=self._plan(handle);executor=plan['executor'];chapter=plan['chapter'];grant=plan['grant']
        source_sha=extraction_input_sha256(plan['prepared'])
        def inputs():
            if self.controls.batches._read(plan['generation_path'])!=plan['generation']['receipt']:
                raise BatchControlError('原生成结果凭据已变化。')
            request=self._inputs(plan['root'],grant,plan['generation'],plan['control'])
            return extraction_input_sha256(executor.prepare_policy_request(**request,idempotency_key=plan['request']['idempotency_key']))
        step_id='step_'+digest({'batch_id':handle.batch_id,'chapter':chapter,'stage':self.stage})[:32]
        authorization=policy_extraction_authorization(grant=grant,prepared=plan['prepared'],
            generation_result_sha256=plan['generation']['receipt']['result_sha256'],
            binding={'step_id':step_id,'execution_id':plan['execution_id']},
            repair_claim_sha256=plan['generation']['receipt'].get('repair_claim_sha256'))
        local_recovery=False
        if executor.store.has_intent(plan['extraction_id']):
            intent=executor.store.load_intent(plan['extraction_id'])
            if intent.get('policy_authorization')!=authorization:raise BatchControlError('原提取意图不属于本次完整输入。')
            if executor.store.has_record(plan['extraction_id']):
                record=executor.store.load_record(plan['extraction_id'])
                if record['status']!='completed':raise BatchControlError('原提取未成功，不能重新创建同一步骤。')
                local_recovery=True
            else:
                # Only actual validated succeeded model evidence qualifies for
                # dispatch-forbidden local parsing of an interrupted result.
                calls=executor.store.model_call_store(intent)
                if calls.receipts_dir.exists() and any(calls.receipts_dir.glob('*.json')):
                    _load_single_successful_response(calls);local_recovery=True
        with self.controls.step(handle,stage=self.stage,execution_id=plan['execution_id'],input_sha256=source_sha,
                idempotency_key='claim-'+self.stage+':'+str(chapter),expected_control_sha256=expected_control_sha256,
                verify_inputs=inputs,local_recovery=local_recovery):
            result=executor.execute_policy(policy_authorization=authorization,**plan['request'])
            if result['status']!='completed':raise BatchControlError('原完整事实提取尚未完成。')
            require_policy_extraction_scope(authorization,prepared=plan['prepared'])
            proposal=executor.store.load_proposal(plan['extraction_id'])
            receipt=dict(schema_version='reviewed_policy_repair_extraction_result_v1' if self.stage=='repair_extraction' else 'reviewed_policy_extraction_result_v1',batch_id=handle.batch_id,
                grant_sha256=grant['grant_sha256'],generation_result_sha256=plan['generation']['receipt']['result_sha256'],
                step_id=step_id,execution_id=plan['execution_id'],input_sha256=source_sha,
                extraction_id=plan['extraction_id'],extraction_intent_hash=result['intent_hash'],
                extraction_record_hash=result['record_hash'],proposal_sha256=proposal['proposal_hash'],
                draft_id=plan['request']['draft_binding']['draft_id'],draft_revision=plan['request']['draft_binding']['revision'],
                draft_sha256=plan['request']['draft_binding']['sha256'])
            if self.stage=='repair_extraction':receipt['repair_claim_sha256']=authorization['repair_claim_sha256']
            receipt['result_sha256']=digest(receipt)
            path=plan['root']/'.novelagent/runtime/reviewed/batch_results'/(step_id+'.json')
            require_restored_execution_paths(path)
            self._verify(plan,receipt,authorization,inputs)
            if os.path.lexists(path):
                if self.controls.batches._read(path)!=receipt:raise BatchControlError('原完整提取结果凭据不同。')
            else:atomic_create_json(path,receipt)
        self._fault('after_extraction_result_before_finish')
        def verify():
            if self.controls.batches._read(path)!=receipt:raise BatchControlError('原提取结果凭据变化。')
            return self._verify(plan,receipt,authorization,inputs)
        current=self.controls.get(project_id=handle.project_id,batch_id=handle.batch_id)
        final=self.controls.finish_step(handle,expected_control_sha256=current['control']['record_sha256'],
            idempotency_key='finish-'+self.stage+':'+str(chapter),result_sha256=receipt['result_sha256'],verify_result=verify)
        return {'extraction':receipt,'control':final,'request_source':'policy','author_confirmed':False,'fact_review':None}

    def _verify(self,plan,receipt,authorization,inputs):
        store=plan['executor'].store;loaded=store.load(plan['extraction_id']);intent,record=loaded['intent'],loaded['record']
        proposal=store.load_proposal(plan['extraction_id'])
        if (not isinstance(record,dict) or record['status']!='completed' or intent.get('policy_authorization')!=authorization
                or intent['execution']['execution_id']!=receipt['execution_id']
                or intent['intent_hash']!=receipt['extraction_intent_hash'] or record['record_hash']!=receipt['extraction_record_hash']
                or proposal['proposal_hash']!=receipt['proposal_sha256'] or store.load_source(plan['extraction_id'])!=plan['request']['chapter_text']
                or inputs()!=receipt['input_sha256']):
            raise BatchControlError('原完整提取、模型响应、正文或当前来源核对失败。')
        return receipt['result_sha256']
