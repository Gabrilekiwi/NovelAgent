"""Typed final quality stage over actual batch generation/extraction receipts."""
import copy,os,re

from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterTimeUnknown,ChapterTimeExceeded
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.engine.persistence import atomic_create_json
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.engine.reviewed_policy_review import verify_policy_preparation
from core.quality.reviewed_disposition import project_reviewed_quality_disposition
from core.story_project.reviewed_chapter_transition import verify_quality_binding
from core.story_project.reviewed_state_document import digest
from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
from workbench.reviewed_policy_reviews import ReviewedPolicyReviewService
from workbench.restore_execution_policy import require_restored_execution_paths


def _stable_quality(value):
    """Only the producer's actual evaluation timestamp is nondeterministic."""
    result=copy.deepcopy(value);result.pop('quality_sha256')
    result['final_artifact_integrity'].pop('timestamp')
    return result


def validate_policy_quality_result(value):
    try:
        fields={'schema_version','batch_id','grant_sha256','step_id','execution_id','input_sha256',
            'extraction_result_sha256','review','result_sha256'}
        repair=isinstance(value,dict) and value.get('schema_version')=='reviewed_policy_quality_result_v2'
        if repair:fields.add('repair_claim_sha256')
        if (not isinstance(value,dict) or set(value)!=fields or value['schema_version'] not in {'reviewed_policy_quality_result_v1','reviewed_policy_quality_result_v2'}
                or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None
                    for k in fields if k.endswith('_sha256'))
                or value['result_sha256']!=digest({k:v for k,v in value.items() if k!='result_sha256'})):
            raise BatchControlError('原批次质量结果凭据无效。')
        r=value['review'];p=r['preparation'];q=r['quality'];policy=verify_policy_preparation(p)
        if (set(r)!={'policy_review','preparation','quality','quality_disposition','review_source','publication_authorized','execution_permit'}
                or policy!=r['policy_review'] or r['review_source']!='policy' or r['publication_authorized'] is not False
                or r['execution_permit'] is not False or p['preparation_sha256']!=digest({k:v for k,v in p.items() if k!='preparation_sha256'})
                or q['quality_sha256']!=digest({k:v for k,v in q.items() if k!='quality_sha256'})
                or policy['batch_grant']['grant_sha256']!=value['grant_sha256']
                or policy['batch_grant']['manifest']['batch_id']!=value['batch_id']
                or value['step_id']!='step_'+digest({'batch_id':value['batch_id'],'chapter':p['binding']['chapter'],'stage':'repair_quality' if repair else 'quality'})[:32]
                or value['execution_id']!=('execution_repair_quality_' if repair else 'execution_quality_')+digest({'batch_id':value['batch_id'],'chapter':p['binding']['chapter']})[:32]):
            raise BatchControlError('原质量报告、准备或策略来源不同。')
        disposition=project_reviewed_quality_disposition(preparation=p,quality=q,
            policy_snapshot=policy['batch_grant']['manifest']['action_policy'])
        if disposition!=r['quality_disposition']:raise BatchControlError('原质量处置与完整报告不同。')
        if disposition['automatic_action']=='continue':verify_quality_binding(q,p)
        return copy.deepcopy(value)
    except (KeyError,TypeError,ValueError) as exc:
        raise BatchControlError('原批次质量准备、报告或处置核对失败。') from exc


class ReviewedPolicyQualityService:
    def __init__(self,controls,*,fault_injector=None,stage='quality',allow_repair=False):
        if stage not in {'quality','repair_quality'}:raise ValueError('invalid policy quality stage')
        self.stage=stage;self.allow_repair=allow_repair
        self.controls=controls;self.publication=controls.publication;self.fault_injector=fault_injector
        self.reviews=ReviewedPolicyReviewService(self.publication)

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    def get(self,*,project_id,batch_id,chapter):
        """Read-only, no constructor/GET restores a live execution permit."""
        context=self.controls._control_context(project_id)
        grant=self.controls.batches._stored(context,batch_id)['grant']
        if type(chapter) is not int or chapter not in grant['manifest']['chapters']:raise BatchControlError('质量结果章号不属于原批次。')
        step='step_'+digest({'batch_id':batch_id,'chapter':chapter,'stage':self.stage})[:32]
        result=validate_policy_quality_result(self.controls.batches._read(context[5]/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')))
        if result['grant_sha256']!=grant['grant_sha256'] or result['step_id']!=step:raise BatchControlError('质量结果与原批次授权或章号不同。')
        return dict(result=result,execution_permit=False,publication_authorized=False)

    def recover_finish(self,*,project_id,batch_id,chapter):
        """Explicit local completion of an original torn finish, no new permission."""
        saved=self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']
        def verify(context,grant,control,pending):
            sources=self._inputs(context[5],grant,chapter,control,allow_edited_draft=True)
            if (saved['input_sha256']!=digest({'grant_sha256':grant['grant_sha256'],'sources':sources})
                    or saved['extraction_result_sha256']!=sources['extraction']['result_sha256']
                    or pending['stage']!=self.stage or pending['execution_id']!=saved['execution_id']
                    or pending['input_sha256']!=saved['input_sha256']
                    or self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']!=saved):
                raise BatchControlError('原质量完成命令与当前完整证据不同。')
            verify_quality_binding(saved['review']['quality'],saved['review']['preparation'])
            self._require_available_time(context[5],grant['manifest']['book_id'],chapter)
            return saved['result_sha256']
        final=self.controls._reconcile_original_finish(project_id=project_id,batch_id=batch_id,
            idempotency_key='finish-'+self.stage+':'+str(chapter),result_sha256=saved['result_sha256'],step_id=saved['step_id'],verify_result=verify)
        return dict(result=saved,control=final,execution_permit=False,publication_authorized=False)

    @staticmethod
    def _require_available_time(root,book_id,chapter):
        time=ChapterExecutionBudget(root,book_id=book_id,chapter=chapter).time_clock.report()
        if time['active_status']=='unknown':raise ChapterTimeUnknown('原质量完成步骤仍有未知主动时间，不能推进发布。')
        if time['active_limit_reached']:raise ChapterTimeExceeded('完成质量步骤前原章主动时间已超限。')

    def _inputs(self,root,grant,chapter,control,*,allow_edited_draft=False):
        batch=grant['manifest']['batch_id'];read=self.controls.batches._read
        def prior(stage,schema):
            step='step_'+digest({'batch_id':batch,'chapter':chapter,'stage':stage})[:32]
            result=read(root/'.novelagent/runtime/reviewed/batch_results'/(step+'.json'))
            if (result.get('schema_version')!=schema or result['step_id']!=step or result['grant_sha256']!=grant['grant_sha256']
                    or result['batch_id']!=batch or result['result_sha256']!=digest({k:v for k,v in result.items() if k!='result_sha256'})
                    or not any(c['action']=='finish_step' and c['payload']['step_id']==step and c['payload']['result_sha256']==result['result_sha256']
                        for c in control['commands'])):
                raise BatchControlError('质量检查缺少原生成或完整提取的阶段完成证明。')
            return result
        repair=self.stage=='repair_quality'
        generation=prior('repair_generation' if repair else 'generation',
            'reviewed_policy_repair_generation_result_v1' if repair else 'reviewed_policy_generation_result_v1')
        extraction=prior('repair_extraction' if repair else 'extraction',
            'reviewed_policy_repair_extraction_result_v1' if repair else 'reviewed_policy_extraction_result_v1')
        request=ReviewedPolicyExtractionService(self.controls)._inputs(root,grant,{'chapter':chapter,'receipt':generation},control,
            allow_edited_draft=allow_edited_draft)
        store=ChapterFactExtractionStore(root);loaded=store.load(extraction['extraction_id']);i=loaded['intent'];r=loaded['record']
        proposal=store.load_proposal(extraction['extraction_id']);a=i.get('policy_authorization',{})
        if repair:
            from workbench.reviewed_policy_repairs import require_original_repair
            _,claim=require_original_repair(self.controls,root,grant,chapter,control,allow_edited_draft=allow_edited_draft)
            if extraction.get('repair_claim_sha256')!=claim['claim_sha256'] or a.get('repair_claim_sha256')!=claim['claim_sha256']:
                raise BatchControlError('修复后完整提取不属于原修复周期。')
        if (not isinstance(r,dict) or r['status']!='completed' or i['intent_hash']!=extraction['extraction_intent_hash']
                or r['record_hash']!=extraction['extraction_record_hash'] or proposal['proposal_hash']!=extraction['proposal_sha256']
                or i['execution']['execution_id']!=extraction['execution_id'] or a.get('grant_sha256')!=grant['grant_sha256']
                or a.get('manifest_sha256')!=grant['manifest']['manifest_sha256']
                or a.get('generation_result_sha256')!=generation['result_sha256']
                or a.get('input_sha256')!=extraction['input_sha256'] or extraction['generation_result_sha256']!=generation['result_sha256']
                or i['draft']['draft_id']!=request['draft_binding']['draft_id']
                or i['draft']['revision']!=request['draft_binding']['revision'] or i['draft']['sha256']!=request['draft_binding']['sha256']
                or i['context']['context_sha256']!=request['context_sha256'] or i['authority']!=request['authority_binding']
                or store.load_source(extraction['extraction_id'])!=request['chapter_text']):
            raise BatchControlError('质量检查的原完整提取、正文或权威来源不同。')
        return dict(generation=generation,extraction=extraction,request=request)

    def check(self,handle,*,expected_control_sha256):
        if self.controls.handles.get(getattr(handle,'batch_id',None)) is not handle or handle.service is not self.controls:
            raise BatchControlPaused('批次质量检查需要本次明确执行许可。')
        with self.publication._owned_project(handle.project_id) as context:
            grant=self.controls.batches._require_current_execution(context,handle.batch_id)['grant']
            control,state=self.controls._store(context,handle.batch_id,grant).load();root=context[5];chapter=state['chapter']
            if state['status']!='running' or state['run_epoch']!=handle.epoch or state['stage']!=self.stage:
                raise BatchControlPaused('原批次当前没有待质量检查步骤。')
            original=self._inputs(root,grant,chapter,control)
        input_sha=digest({'grant_sha256':grant['grant_sha256'],'sources':original})
        def inputs():return digest({'grant_sha256':grant['grant_sha256'],'sources':self._inputs(root,grant,chapter,control)})
        step='step_'+digest({'batch_id':handle.batch_id,'chapter':chapter,'stage':self.stage})[:32]
        execution=('execution_repair_quality_' if self.stage=='repair_quality' else 'execution_quality_')+digest({'batch_id':handle.batch_id,'chapter':chapter})[:32]
        path=root/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')
        exists=os.path.lexists(path)
        with self.controls.step(handle,stage=self.stage,execution_id=execution,input_sha256=input_sha,
                idempotency_key='claim-'+self.stage+':'+str(chapter),expected_control_sha256=expected_control_sha256,verify_inputs=inputs,
                local_recovery=exists) as permit:
            permit.require_scope(stage=self.stage,book_id=grant['manifest']['book_id'],chapter=chapter,execution_id=execution,input_sha256=input_sha)
            e=original['extraction'];request=original['request']
            fresh=self.reviews.prepare(project_id=handle.project_id,batch_id=handle.batch_id,draft_id=e['draft_id'],
                extraction_id=e['extraction_id'],expected_draft_revision=request['draft_binding']['revision'],
                expected_draft_sha256=request['draft_binding']['sha256'],expected_extraction_record_hash=e['extraction_record_hash'])
            permit.require_scope(stage=self.stage,book_id=grant['manifest']['book_id'],chapter=chapter,execution_id=execution,input_sha256=input_sha)
            if exists:
                result=validate_policy_quality_result(self.controls.batches._read(path));saved=result['review']
                if (saved['policy_review']!=fresh['policy_review'] or saved['preparation']!=fresh['preparation']
                        or _stable_quality(saved['quality'])!=_stable_quality(fresh['quality'])):
                    raise BatchControlError('当前完整质量重检与原保存报告不同，不能覆盖原结果。')
            else:
                result=dict(schema_version='reviewed_policy_quality_result_v1',batch_id=handle.batch_id,grant_sha256=grant['grant_sha256'],
                    step_id=step,execution_id=execution,input_sha256=input_sha,extraction_result_sha256=e['result_sha256'],review=fresh)
                if self.stage=='repair_quality':result.update(schema_version='reviewed_policy_quality_result_v2',repair_claim_sha256=e['repair_claim_sha256'])
                result['result_sha256']=digest(result);validate_policy_quality_result(result)
                require_restored_execution_paths(path);atomic_create_json(path,result)
            def verify():
                if (self.controls.batches._read(path)!=result or result['input_sha256']!=inputs()
                        or result['extraction_result_sha256']!=e['result_sha256'] or result['grant_sha256']!=grant['grant_sha256']
                        or result['step_id']!=step or result['execution_id']!=execution):
                    raise BatchControlError('原质量结果、完整输入或提取凭据变化。')
                validate_policy_quality_result(result)
                return result['result_sha256']
            verify()
            self._fault('after_quality_result_inside_scope')
        self._fault('after_quality_result_before_finish')
        current=self.controls.get(project_id=handle.project_id,batch_id=handle.batch_id)
        budget=ChapterExecutionBudget(root,book_id=grant['manifest']['book_id'],chapter=chapter)
        time=budget.time_clock.report()
        action=result['review']['quality_disposition']['automatic_action']
        if action=='continue' and time['active_status']!='unknown' and not time['active_limit_reached']:
            def verify_continue():
                value=verify()
                self._require_available_time(root,grant['manifest']['book_id'],chapter)
                return value
            final=self.controls.finish_step(handle,expected_control_sha256=current['control']['record_sha256'],
                idempotency_key='finish-'+self.stage+':'+str(chapter),result_sha256=result['result_sha256'],verify_result=verify_continue)
        elif (action=='repair_once' and self.allow_repair and self.stage=='quality'
                and time['active_status']!='unknown' and not time['active_limit_reached']):
            from workbench.reviewed_policy_repairs import ReviewedPolicyRepairService
            verify()
            final=ReviewedPolicyRepairService(self.controls).begin(handle,expected_control_sha256=current['control']['record_sha256'])
        else:
            final=self.controls.pause_step(handle,expected_control_sha256=current['control']['record_sha256'],
                idempotency_key='pause-'+self.stage+':'+str(chapter)+':'+handle.epoch,verify_result=verify)
        return dict(result=result,control=final,execution_permit=False,publication_authorized=False,
            repair_execution_authorized=False,automatic_action=action,
            time_blocked=time['active_status']=='unknown' or time['active_limit_reached'])
