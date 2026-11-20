"""Original failed evidence and once-only ledger claim for policy repair."""
import json

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.persistence import persistence_state_lock
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,validate_control
from core.quality.reviewed_repair_cycle import build_repair_cycle_claim
from core.story_project.reviewed_state_document import digest


def repair_step(batch,chapter,stage):
    return 'step_'+digest({'batch_id':batch,'chapter':chapter,'stage':stage})[:32]


def original_repair_inputs(controls,root,grant,chapter,control,*,verify_sources=True,allow_edited_draft=False):
    from workbench.reviewed_policy_quality import ReviewedPolicyQualityService,validate_policy_quality_result
    from workbench.reviewed_batches import ReviewedBatchService,ReviewedBatchError
    batch=grant['manifest']['batch_id'];step=repair_step(batch,chapter,'quality')
    try:saved=validate_policy_quality_result(ReviewedBatchService._read(root/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')))
    except ReviewedBatchError as exc:raise BatchControlError('原失败质量结果缺失或损坏，不能重新授权修复。') from exc
    if (saved['step_id']!=step or saved['grant_sha256']!=grant['grant_sha256']
            or saved['review']['quality_disposition']['automatic_action']!='repair_once'
            or saved['schema_version']!='reviewed_policy_quality_result_v1'):
        raise BatchControlError('原章没有本批次首次完整检查的具体单次修复资格。')
    if verify_sources:
        try:sources=ReviewedPolicyQualityService(controls)._inputs(root,grant,chapter,control,allow_edited_draft=allow_edited_draft)
        except ReviewedBatchError as exc:raise BatchControlError('原完整生成或提取结果缺失或损坏，不能修复。') from exc
        if saved['input_sha256']!=digest({'grant_sha256':grant['grant_sha256'],'sources':sources}):
            raise BatchControlError('修复前原完整正文、提取或质量来源变化，请转人工。')
    budget=ChapterExecutionBudget(root,book_id=grant['manifest']['book_id'],chapter=chapter)
    claim=build_repair_cycle_claim(preparation=saved['review']['preparation'],quality=saved['review']['quality'],
        budget=budget.store.frozen_budget(),batch_id=batch,batch_authorization_sha256=grant['grant_sha256'],
        policy_snapshot=grant['manifest']['action_policy'])
    return saved,claim


def require_original_repair(controls,root,grant,chapter,control,*,verify_sources=True,allow_edited_draft=False):
    saved,claim=original_repair_inputs(controls,root,grant,chapter,control,verify_sources=verify_sources,allow_edited_draft=allow_edited_draft)
    budget=ChapterExecutionBudget(root,book_id=grant['manifest']['book_id'],chapter=chapter)
    original=repair_step(grant['manifest']['batch_id'],chapter,'quality')
    commands=[c for c in control['commands'] if c['action']=='begin_repair' and c['payload']['step_id']==original]
    if (budget.store.repair_cycle_claim()!=claim or len(commands)!=1
            or commands[0]['payload']['claim_sha256']!=claim['claim_sha256']
            or commands[0]['payload']['quality_result_sha256']!=saved['result_sha256']):
        raise BatchControlError('修复缺少原账本已消耗的一次资格或同一开始命令。')
    return saved,claim


def repair_generation_inputs(controls,root,grant,chapter,control,*,allow_edited_draft=False):
    saved,claim=require_original_repair(controls,root,grant,chapter,control,allow_edited_draft=allow_edited_draft)
    original=saved['review']['policy_review']['source_text']
    intent={'goal':next(o for o in grant['manifest']['outlines'] if o['chapter']==chapter)['blueprint']['core_event'],
        'validation_focus':['continuity','spatial','logic'],
        'notes':['在保留人物、事实和章意图的基础上，只修复下列已有原文证据的问题。输出完整修复正文。',
            json.dumps({'original_prose':original,'quality_disposition':saved['review']['quality_disposition']},ensure_ascii=False,sort_keys=True)]}
    evidence=dict(repair_claim_sha256=claim['claim_sha256'],original_quality_result_sha256=saved['result_sha256'],
        original_draft_sha256=saved['review']['policy_review']['source_draft_sha256'])
    return intent,evidence


class ReviewedPolicyRepairService:
    def __init__(self,controls,*,fault_injector=None):
        self.controls=controls;self.fault_injector=fault_injector

    def recover_begin(self,*,project_id,batch_id,chapter):
        """Finish an original recorded begin mirror, preserving unknown time.

        A consumed claim without a recorded command cannot invent progress.
        This local reconciliation never creates a handle or dispatches work.
        """
        from workbench.reviewed_batch_controls import control_paths
        c=self.controls
        with c.publication._owned_project(project_id) as context:
            grant=c.batches._require_current_execution(context,batch_id)['grant']
            if type(chapter) is not int or chapter not in grant['manifest']['chapters']:
                raise BatchControlError('原修复恢复章号不属于批次。')
            store=c._store(context,batch_id,grant);_,machine,book=control_paths(context,batch_id)
            with persistence_state_lock((machine,book)):
                record,_=validate_control(store._read(machine),grant)
                key='begin-repair:'+str(chapter)
                prior=next((v for v in record['commands'] if v['key_sha256']==digest(key.encode('utf-8'))),None)
                if prior is None or prior['action']!='begin_repair':
                    raise BatchControlError('只有消耗记录而没有原开始命令，不能从结果或资格创建新修复进度。')
                prefix={**record,'commands':record['commands'][:prior['sequence']-1]}
                prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
                _,previous=validate_control(prefix,grant);pending=previous['pending_step']
                def before():
                    if c.batches._require_current_execution(context,batch_id)['grant']!=grant:
                        raise BatchControlError('续办原开始命令前批次授权变化。')
                    saved,claim=require_original_repair(c,context[5],grant,chapter,record)
                    if (previous['chapter']!=chapter or previous['stage']!='quality' or previous['status']!='running'
                            or pending is None or pending['step_id']!=saved['step_id']
                            or pending['input_sha256']!=saved['input_sha256'] or pending['execution_id']!=saved['execution_id']
                            or prior['payload']!=dict(run_epoch=previous['run_epoch'],step_id=saved['step_id'],
                                quality_result_sha256=saved['result_sha256'],claim_sha256=claim['claim_sha256'])):
                        raise BatchControlError('原开始命令缺少同一首次失败步骤、报告或消耗资格。')
                before()
                store.apply(action='begin_repair',payload=prior['payload'],key=key,expected_sha256=prefix['record_sha256'],
                    before_write=before,fault_injector=self.fault_injector)
            c.handles.pop(batch_id,None)
            return dict(control=c._view(context,batch_id,grant),execution_permit=False,
                repair_execution_authorized=False,publication_authorized=False)

    def begin(self,handle,*,expected_control_sha256):
        from workbench.reviewed_batch_controls import control_paths
        from workbench.reviewed_policy_quality import ReviewedPolicyQualityService
        c=self.controls
        if c.handles.get(getattr(handle,'batch_id',None)) is not handle or getattr(handle,'service',None) is not c:
            raise BatchControlPaused('修复需要本次原批次明确执行许可。')
        with c.publication._owned_project(handle.project_id) as context:
            grant=c.batches._require_current_execution(context,handle.batch_id)['grant']
            store=c._store(context,handle.batch_id,grant);record,state=store.load();chapter=state['chapter'];root=context[5]
            pending=state['pending_step']
            if (record['record_sha256']!=expected_control_sha256 or state['status']!='running'
                    or state['run_epoch']!=handle.epoch or state['stage']!='quality' or pending is None):
                raise BatchControlPaused('原批次没有本次待处理的首次质量步骤。')
            saved,claim=original_repair_inputs(c,root,grant,chapter,record)
            if pending['step_id']!=saved['step_id'] or pending['input_sha256']!=saved['input_sha256'] or pending['execution_id']!=saved['execution_id']:
                raise BatchControlError('原失败报告与待处理质量步骤不同。')
            budget=ChapterExecutionBudget(root,book_id=grant['manifest']['book_id'],chapter=chapter)
            def before():
                if c.handles.get(handle.batch_id) is not handle or c.batches._require_current_execution(context,handle.batch_id)['grant']!=grant:
                    raise BatchControlPaused('消耗修复资格前原许可变化。')
                current,progress=store.load()
                if current!=record or progress!=state or original_repair_inputs(c,root,grant,chapter,current)!=(saved,claim):
                    raise BatchControlError('开始修复前原失败报告、输入或进度变化。')
                ReviewedPolicyQualityService._require_available_time(root,grant['manifest']['book_id'],chapter)
            before()
            with budget.operation('execution_repair_claim_'+claim['claim_sha256'][:32],allow_provider_dispatch=False) as action:
                budget.store.claim_repair_cycle(claim,before_claim=before,fault_injector=self.fault_injector)
                if self.fault_injector is not None:self.fault_injector('after_claim_before_repair_control')
                _,machine,book=control_paths(context,handle.batch_id)
                def write_before():
                    if (c.handles.get(handle.batch_id) is not handle
                            or c.batches._require_current_execution(context,handle.batch_id)['grant']!=grant
                            or original_repair_inputs(c,root,grant,chapter,record)!=(saved,claim)):
                        raise BatchControlError('原周期开始双副本写入前来源或许可变化。')
                    action.interval.remaining_seconds()
                with persistence_state_lock((machine,book)):
                    store.apply(action='begin_repair',payload=dict(run_epoch=handle.epoch,step_id=saved['step_id'],
                        quality_result_sha256=saved['result_sha256'],claim_sha256=claim['claim_sha256']),
                        key='begin-repair:'+str(chapter),expected_sha256=expected_control_sha256,
                        before_write=write_before,fault_injector=self.fault_injector)
            return c._view(context,handle.batch_id,grant)
