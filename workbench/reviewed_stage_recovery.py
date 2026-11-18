"""Typed original stage recovery, with no dispatch or live-permit restoration."""
import copy,re

from core.engine.reviewed_batch_control import BatchControlError
from core.story_project.reviewed_state_document import digest


STAGES=('generation','extraction','quality','publication','readback',
    'repair_generation','repair_extraction','repair_quality')


def _verified(call):
    from core.engine.chapter_candidate import ChapterCandidateError
    from core.engine.chapter_fact_extraction import ChapterFactExtractionError
    from workbench.reviewed_batches import ReviewedBatchError
    try:return call()
    except BatchControlError:raise
    except (ChapterCandidateError,ChapterFactExtractionError,ReviewedBatchError,OSError,ValueError,KeyError,TypeError) as exc:
        raise BatchControlError('原阶段完整输入或模型凭证不可核验，不能续办控制记录。') from exc


def read_stage_result(controls,project_id,batch_id,chapter,stage):
    from workbench.reviewed_batches import ReviewedBatchError
    context=controls._control_context(project_id);grant=controls.batches._stored(context,batch_id)['grant']
    if type(chapter) is not int or chapter not in grant['manifest']['chapters'] or stage not in {'generation','repair_generation','extraction','repair_extraction'}:
        raise BatchControlError('原生成或提取结果不属于有限批次阶段。')
    step='step_'+digest({'batch_id':batch_id,'chapter':chapter,'stage':stage})[:32]
    try:value=controls.batches._read(context[6]/'reviewed/batch_results'/(step+'.json'))
    except ReviewedBatchError as exc:raise BatchControlError('原阶段结果缺失或不可读。') from exc
    generation=stage.endswith('generation');repair=stage.startswith('repair_')
    fields={'schema_version','batch_id','grant_sha256','step_id','execution_id','input_sha256',
        'draft_id','draft_revision','draft_sha256','result_sha256'}
    if generation:
        fields|={'candidate_id','candidate_intent_hash','candidate_record_hash'}
        if repair:fields|={'repair_claim_sha256','original_quality_result_sha256','original_draft_sha256'}
    else:
        fields|={'generation_result_sha256','extraction_id','extraction_intent_hash','extraction_record_hash','proposal_sha256'}
        if repair:fields.add('repair_claim_sha256')
    if (not isinstance(value,dict) or set(value)!=fields or value['schema_version']!='reviewed_policy_'+stage+'_result_v1'
            or value['batch_id']!=batch_id or value['grant_sha256']!=grant['grant_sha256'] or value['step_id']!=step
            or type(value['draft_revision']) is not int or value['draft_revision']<1
            or not isinstance(value['draft_id'],str) or re.fullmatch('candidate_draft_[0-9a-f]{32}',value['draft_id']) is None
            or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None
                for k in fields if k.endswith(('_sha256','_hash')))
            or value['result_sha256']!=digest({k:v for k,v in value.items() if k!='result_sha256'})):
        raise BatchControlError('原阶段结果形状、授权、步骤或哈希不一致。')
    identity='candidate_id' if generation else 'extraction_id'
    pattern='candidate_[0-9a-f]{40}' if generation else 'fact_extraction_[0-9a-f]{40}'
    execution='execution_candidate_[0-9a-f]{32}' if generation else 'execution_fx_[0-9a-f]{24}'
    if (not isinstance(value[identity],str) or re.fullmatch(pattern,value[identity]) is None
            or not isinstance(value['execution_id'],str) or re.fullmatch(execution,value['execution_id']) is None):
        raise BatchControlError('原阶段Candidate、提取或执行编号无效。')
    return copy.deepcopy(value)


class ReviewedBatchStageRecoveryService:
    def __init__(self,controls):self.controls=controls

    def get(self,*,project_id,batch_id):
        from core.engine.reviewed_batch_control import validate_control
        from workbench.reviewed_batch_controls import control_paths
        c=self.controls;context=c._control_context(project_id);grant=c.batches._stored(context,batch_id)['grant']
        _,machine,book=control_paths(context,batch_id);store=c._store(context,batch_id,grant)
        record,state=validate_control(store._read(machine),grant);mirror,_=validate_control(store._read(book),grant)
        recovery=None
        if record!=mirror:
            if not record['commands']:raise BatchControlError('原控制副本无可核验的原命令。')
            command=record['commands'][-1];prefix={**record,'commands':record['commands'][:-1]}
            prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
            _,previous=validate_control(prefix,grant)
            if mirror!=prefix:raise BatchControlError('控制副本不是同一原命令的前后版本。')
            recovery=dict(action=command['action'],chapter=previous['chapter'],stage=previous['stage'],
                expected_control_sha256=prefix['record_sha256'],command_sha256=command['command_sha256'])
        return dict(control=record,progress=state,control_reconciliation_required=record!=mirror,
            original_recovery=recovery,explicit_resume_required=state['status'] not in {'stopped','completed'},
            execution_permit=False,publication_authorized=False)

    def _service(self,stage):
        from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService
        from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
        from workbench.reviewed_policy_quality import ReviewedPolicyQualityService
        from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService
        from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService
        if stage not in STAGES:raise BatchControlError('不支持该原批次恢复阶段。')
        if stage.endswith('generation'):return ReviewedPolicyGenerationService(self.controls,stage=stage)
        if stage.endswith('extraction'):return ReviewedPolicyExtractionService(self.controls,stage=stage)
        if stage.endswith('quality'):return ReviewedPolicyQualityService(self.controls,stage=stage)
        return ReviewedPolicyPublicationService(self.controls) if stage=='publication' else ReviewedPolicyReadbackService(self.controls)

    def recover_finish(self,*,project_id,batch_id,chapter,stage):
        return _verified(lambda:self._service(stage).recover_finish(project_id=project_id,batch_id=batch_id,chapter=chapter))

    def recover_claim(self,*,project_id,batch_id,chapter,stage):
        service=self._service(stage)
        def verify(context,grant,prefix,previous):
            if stage.endswith('generation'):
                from core.engine.reviewed_policy_generation import generation_input_sha256
                plan=service._prepare_plan(context,grant,prefix,chapter)
                return plan['execution_id'],generation_input_sha256(plan['prepared'])
            if stage.endswith('extraction'):
                from core.engine.reviewed_policy_extraction import extraction_input_sha256
                plan=service._prepare_plan(context,grant,prefix,chapter)
                return plan['execution_id'],extraction_input_sha256(plan['prepared'])
            if stage.endswith('quality'):
                sources=service._inputs(context[5],grant,chapter,prefix)
                execution=('execution_repair_quality_' if stage=='repair_quality' else 'execution_quality_')+digest({'batch_id':batch_id,'chapter':chapter})[:32]
                return execution,digest({'grant_sha256':grant['grant_sha256'],'sources':sources})
            if stage=='publication':
                from workbench.reviewed_policy_publication import _quality,_route,published_original
                from workbench.reviewed_policy_quality import ReviewedPolicyQualityService
                from core.engine.chapter_commit_request import chapter_commit_request
                quality,_=_quality(self.controls.publication,context,grant,chapter)
                if published_original(self.controls.publication,context,grant,chapter) is None:
                    source=ReviewedPolicyQualityService(self.controls,stage='repair_quality' if
                        quality['schema_version']=='reviewed_policy_quality_result_v2' else 'quality')._inputs(context[5],grant,chapter,prefix)
                    if quality['input_sha256']!=digest({'grant_sha256':grant['grant_sha256'],'sources':source}):
                        raise BatchControlError('原正式发布领取的完整正文、提取或质量来源变化。')
                incoming=chapter_commit_request(book_id=grant['manifest']['book_id'],request=_route(quality))
                return 'execution_publish_'+incoming['client_request_sha256'][:32],digest(dict(
                    grant_sha256=grant['grant_sha256'],quality_result_sha256=quality['result_sha256'],request=incoming))
            actual=service._inputs(context,grant,chapter)
            return 'execution_readback_'+digest({'batch_id':batch_id,'chapter':chapter})[:32],digest(
                {'grant_sha256':grant['grant_sha256'],'actual_readback':actual})
        final=_verified(lambda:self.controls._reconcile_original_claim(project_id=project_id,batch_id=batch_id,
            chapter=chapter,stage=stage,verify_inputs=verify))
        return dict(control=final,execution_permit=False,publication_authorized=False)
