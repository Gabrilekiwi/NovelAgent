"""Actual policy publication using the original request/transaction/index stack."""
import copy,os,re

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_commit_prepare import prepare_policy_reviewed_chapter
from core.engine.chapter_commit_request import chapter_commit_request
from core.engine.persistence import atomic_create_json
from core.engine.provider_safety import list_unresolved_provider_calls
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,validate_control,BatchControlStore
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.reviewed_chapter_transition import verify_quality_binding
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_store import read_verified_commit_requests
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.reviewed_policy_quality import ReviewedPolicyQualityService,validate_policy_quality_result
from workbench.restore_execution_policy import require_restored_execution_paths


def _step(batch,chapter,stage):return 'step_'+digest({'batch_id':batch,'chapter':chapter,'stage':stage})[:32]


def validate_publication_result(value):
    """Portable shape/binding validation; execution also checks the real receipt."""
    try:
        fields={'schema_version','batch_id','grant_sha256','quality_result_sha256','step_id',
            'execution_id','input_sha256','publication','result_sha256'}
        if (not isinstance(value,dict) or set(value)!=fields
                or value['schema_version']!='reviewed_policy_publication_result_v1'
                or not isinstance(value['batch_id'],str)
                or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None
                    for k in fields if k.endswith('_sha256'))):
            raise BatchControlError('原发布结果形状或哈希字段无效。')
        actual=value['publication'];chapter=actual['chapter']
        request_sha=actual['request']['client_request_sha256']
        if (type(chapter) is not int or chapter<1 or actual['publication_verified'] is not True
                or not isinstance(request_sha,str) or re.fullmatch('[0-9a-f]{64}',request_sha) is None
                or value['execution_id']!='execution_publish_'+request_sha[:32]
                or value['step_id']!=_step(value['batch_id'],chapter,'publication')
                or value['result_sha256']!=digest({k:v for k,v in value.items() if k!='result_sha256'})):
            raise BatchControlError('原发布结果步骤、执行或校验和无效。')
        return copy.deepcopy(value)
    except (KeyError,TypeError,ValueError) as exc:raise BatchControlError('原发布结果不完整。') from exc


def _quality(publication,context,grant,chapter):
    from workbench.reviewed_batch_controls import control_paths
    from workbench.reviewed_batches import ReviewedBatchService
    batch=grant['manifest']['batch_id'];original=_step(batch,chapter,'quality')
    _,machine,_=control_paths(context,batch)
    record,_=validate_control(BatchControlStore._read(machine),grant)
    repaired=any(c['action']=='begin_repair' and c['payload']['step_id']==original for c in record['commands'])
    step=_step(batch,chapter,'repair_quality' if repaired else 'quality')
    result=validate_policy_quality_result(ReviewedBatchService._read(context[5]/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')))
    if repaired:
        from workbench.reviewed_policy_repairs import require_original_repair
        _,claim=require_original_repair(None,context[5],grant,chapter,record,verify_sources=False)
        if result.get('repair_claim_sha256')!=claim['claim_sha256']:
            raise BatchControlError('最终修复质量不属于原已消耗周期。')
    if (result['grant_sha256']!=grant['grant_sha256'] or result['step_id']!=step
            or not any(c['action']=='finish_step' and c['payload']['step_id']==step
                and c['payload']['result_sha256']==result['result_sha256'] for c in record['commands'])):
        raise BatchControlError('发布缺少原质量阶段完整通过的完成证明。')
    verify_quality_binding(result['review']['quality'],result['review']['preparation'])
    if chapter==grant['manifest']['start_chapter'] and result['review']['preparation']['binding']['reviewed_source_sha256']!=grant['manifest']['initial_history_sha256']:
        raise BatchControlError('原批次首章质量不是授权时的原正式状态。')
    return result,record


def _route(quality):
    p=quality['review']['preparation'];b=p['binding']
    return dict(project_id=b['project_id'],chapter=b['chapter'],review_id=b['review_id'],
        expected_draft_revision=b['draft_revision'],expected_review_revision=b['review_revision'],
        expected_draft_sha256=b['draft_sha256'],expected_facts_sha256=b['facts_sha256'],idempotency_key='policy-'+b['review_id'])


def published_original(publication,context,grant,chapter,*,require_tip=True):
    """Actual receipt chain, never a flag permitting a second publication."""
    if type(chapter) is not int or chapter not in grant['manifest']['chapters']:
        raise BatchControlError('发布章节不属于原有限批次。')
    quality,_=_quality(publication,context,grant,chapter);p=quality['review']['preparation']
    incoming=chapter_commit_request(book_id=grant['manifest']['book_id'],request=_route(quality))
    root=context[5];runtime=context[6]
    records=read_verified_commit_requests(root=root,book_id=grant['manifest']['book_id'],
        root_map={'story_project':root,'runtime':runtime},baseline_document_sha256=grant['manifest']['baseline_document_sha256'])
    actual=next((r for r in records if r['request']['idempotency_scope_sha256']==incoming['idempotency_scope_sha256']),None)
    if actual is None:return None
    verified=load_verified_reviewed_identity(root)
    if (actual['request']!=incoming or actual['preparation_sha256']!=p['preparation_sha256']
            or actual['prose_sha256']!=p['binding']['final_prose_sha256'] or actual['chapter']!=chapter
            or require_tip and (verified['history']['value']['committed_through']!=chapter
                or actual['document_sha256']!=verified['history']['document_sha256'])):
        raise BatchControlError('实际正式提交不是本次原策略请求或当前权威章。')
    return actual


class ReviewedPolicyPublicationService:
    def __init__(self,controls,*,fault_injector=None):
        self.controls=controls;self.publication=controls.publication;self.fault_injector=fault_injector

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    def get(self,*,project_id,batch_id,chapter):
        context=self.controls._control_context(project_id)
        grant=self.controls.batches._stored(context,batch_id)['grant']
        if type(chapter) is not int or chapter not in grant['manifest']['chapters']:
            raise BatchControlError('原发布结果章号不属于批次。')
        step=_step(batch_id,chapter,'publication')
        saved=validate_publication_result(self.controls.batches._read(context[6]/'reviewed/batch_results'/(step+'.json')))
        if saved['grant_sha256']!=grant['grant_sha256'] or saved['step_id']!=step:
            raise BatchControlError('原发布结果与批次授权不同。')
        return dict(result=saved,execution_permit=False,publication_authorized=False)

    def recover_finish(self,*,project_id,batch_id,chapter):
        """Reconcile only the recorded finish, never create one from a result file."""
        saved=self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']
        def verify(context,grant,control,pending):
            quality,_=_quality(self.publication,context,grant,chapter)
            incoming=chapter_commit_request(book_id=grant['manifest']['book_id'],request=_route(quality))
            expected=digest(dict(grant_sha256=grant['grant_sha256'],quality_result_sha256=quality['result_sha256'],request=incoming))
            if (saved['input_sha256']!=expected or saved['quality_result_sha256']!=quality['result_sha256']
                    or saved['publication']!=published_original(self.publication,context,grant,chapter)
                    or pending['stage']!='publication' or pending['execution_id']!=saved['execution_id']
                    or pending['input_sha256']!=saved['input_sha256']
                    or self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']!=saved):
                raise BatchControlError('原发布完成命令与实际正式凭据不同。')
            return saved['result_sha256']
        final=self.controls._reconcile_original_finish(project_id=project_id,batch_id=batch_id,
            idempotency_key='finish-publication:'+str(chapter),result_sha256=saved['result_sha256'],step_id=saved['step_id'],verify_result=verify)
        return dict(result=saved,control=final,execution_permit=False,publication_authorized=False)

    def resolve(self,*,project_id,batch_id,chapter):
        """Explicit original-transaction recovery; no new chapter/handle/dispatch."""
        p=self.publication
        with p._owned_project(project_id) as context:
            grant=self.controls.batches._stored(context,batch_id)['grant'];quality,_=_quality(p,context,grant,chapter)
            p._reconcile_owned_persistence(context[8],book_id=grant['manifest']['book_id'])
            actual=published_original(p,context,grant,chapter,require_tip=False)
            if actual is not None:
                p.index.rebuild(project_id=project_id,root_map=context[7],
                    baseline_document_sha256=grant['manifest']['baseline_document_sha256'],connection=context[1])
                return dict(publication=actual,state='published',execution_permit=False,publication_authorized=False)
            incoming=chapter_commit_request(book_id=grant['manifest']['book_id'],request=_route(quality))
            path=context[6]/'reviewed/intents'/(incoming['idempotency_scope_sha256']+'.json')
            if os.path.lexists(path):
                return dict(publication=p._original_unpublished(path,incoming=incoming,tx_root=context[8]),
                    state='original_unpublished',execution_permit=False,publication_authorized=False)
            return dict(publication=None,state='not_started',execution_permit=False,publication_authorized=False)

    def publish(self,handle,*,expected_control_sha256):
        if self.controls.handles.get(getattr(handle,'batch_id',None)) is not handle or handle.service is not self.controls:
            raise BatchControlPaused('正式自动发布需要本次明确批次许可。')
        p=self.publication
        with p._owned_project(handle.project_id) as context:
            grant=self.controls.batches._require_current_execution(context,handle.batch_id)['grant']
            control,state=self.controls._store(context,handle.batch_id,grant).load()
            chapter=state['chapter'];root=context[5];runtime=context[6]
            if state['status']!='running' or state['run_epoch']!=handle.epoch or state['stage']!='publication':
                raise BatchControlPaused('原批次当前没有待发布步骤。')
            quality,_=_quality(p,context,grant,chapter);prepared=quality['review']['preparation'];route=_route(quality)
            existing=published_original(p,context,grant,chapter)
        incoming=chapter_commit_request(book_id=grant['manifest']['book_id'],request=route)
        input_value=dict(grant_sha256=grant['grant_sha256'],quality_result_sha256=quality['result_sha256'],request=incoming)
        source_sha=digest(input_value)
        def inputs():
            context=self.controls._control_context(handle.project_id)
            saved,record=_quality(p,context,grant,chapter)
            if saved!=quality:raise BatchControlError('原发布质量结果变化。')
            actual=published_original(p,context,grant,chapter)
            if actual is None:
                sources=ReviewedPolicyQualityService(self.controls,stage='repair_quality' if quality['schema_version']=='reviewed_policy_quality_result_v2' else 'quality')._inputs(root,grant,chapter,record)
                if digest({'grant_sha256':grant['grant_sha256'],'sources':sources})!=quality['input_sha256']:
                    raise BatchControlError('正式发布前完整正文和提取来源变化。')
            return source_sha
        execution='execution_publish_'+incoming['client_request_sha256'][:32]
        step=_step(handle.batch_id,chapter,'publication')
        path=runtime/'reviewed/batch_results'/(step+'.json')
        with self.controls.step(handle,stage='publication',execution_id=execution,input_sha256=source_sha,
                idempotency_key='claim-publication:'+str(chapter),expected_control_sha256=expected_control_sha256,
                verify_inputs=inputs,local_recovery=existing is not None) as permit:
            with permit._owned_project(p,handle.project_id) as context:
                actual=published_original(p,context,grant,chapter)
                if actual is None:
                    intent_path=runtime/'reviewed/intents'/(incoming['idempotency_scope_sha256']+'.json')
                    require_restored_execution_paths(intent_path)
                    if os.path.lexists(intent_path):
                        raise BatchControlError('原发布已中断，请明确找回原事务结果，不能重新准备。')
                    def prepare(**request):
                        if {k:v for k,v in request.items() if k!='connection'}!=route:
                            raise BatchControlError('自动发布准备请求与原质量凭据不同。')
                        draft,_=p.preparations.fact_reviews.verify_draft_source(prepared['binding']['draft_id'],connection=request['connection'])
                        trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter)
                        result=prepare_policy_reviewed_chapter(trusted_context=trusted,project_id=handle.project_id,
                            draft=draft.to_dict(),policy_review=quality['review']['policy_review'],idempotency_key=route['idempotency_key'])
                        if result!=prepared:raise BatchControlError('当前正式准备与原完整质量结果不同。')
                        return result
                    def before():
                        permit.require_scope(stage='publication',book_id=grant['manifest']['book_id'],chapter=chapter,
                            execution_id=execution,input_sha256=source_sha)
                        permit.active_interval.remaining_seconds()
                    original_fault=p.persistence_fault_injector
                    def fault(event,*args):
                        if event in {'before_apply_target','before_commit_marker'}:
                            permit.require_control();permit.active_interval.remaining_seconds()
                        if original_fault is not None:original_fault(event,*args)
                        if event in {'before_apply_target','before_commit_marker'}:
                            permit.require_control();permit.active_interval.remaining_seconds()
                    actual=p._publish_new(route,context=context,incoming=incoming,
                        index_args=dict(project_id=handle.project_id,request=route,root_map=context[7],connection=context[1],
                            baseline_document_sha256=grant['manifest']['baseline_document_sha256']),
                        intent_path=intent_path,before_effect=before,prepare=prepare,persistence_fault=fault)
                    if actual.get('publication_verified') is not True:raise BatchControlError('原事务没有已核验的正式发布结果。')
                verified=published_original(p,context,grant,chapter)
                if verified!=actual:raise BatchControlError('正式写入后的原请求凭证核对失败。')
                result=dict(schema_version='reviewed_policy_publication_result_v1',batch_id=handle.batch_id,
                    grant_sha256=grant['grant_sha256'],quality_result_sha256=quality['result_sha256'],
                    step_id=step,execution_id=execution,input_sha256=source_sha,publication=actual)
                result['result_sha256']=digest(result)
                validate_publication_result(result)
                require_restored_execution_paths(path)
                if os.path.lexists(path):
                    if self.controls.batches._read(path)!=result:raise BatchControlError('原发布阶段结果不同。')
                else:atomic_create_json(path,result)
        self._fault('after_publication_result_before_finish')
        def verify():
            context=self.controls._control_context(handle.project_id)
            if self.controls.batches._read(path)!=result or published_original(p,context,grant,chapter)!=actual:
                raise BatchControlError('完成发布步骤时实际原凭据变化。')
            return result['result_sha256']
        current=self.controls.get(project_id=handle.project_id,batch_id=handle.batch_id)
        final=self.controls.finish_step(handle,expected_control_sha256=current['control']['record_sha256'],
            idempotency_key='finish-publication:'+str(chapter),result_sha256=result['result_sha256'],verify_result=verify)
        return dict(result=result,control=final,execution_permit=False,publication_authorized=False)
