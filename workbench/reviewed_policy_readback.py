"""Formal post-commit readback, and exact next-chapter parent admission."""
import copy,os,re

from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterTimeUnknown,ChapterTimeExceeded
from core.engine.persistence import atomic_create_json
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,BatchControlStore,validate_control
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.paths import canonical_prose_path
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest,read_project_bytes,RELATIVE_PATH
from workbench.reviewed_policy_publication import _step,published_original,_quality,validate_publication_result
from workbench.restore_execution_policy import require_restored_execution_paths


def _next(root,chapter):
    trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter)
    state=trusted.story_project_context['reviewed_state']
    return dict(chapter=chapter,context_sha256=trusted.context_sha256,
        read_set_sha256=digest(trusted.story_project_context['read_set']),
        authoritative_state_sha256=digest(state['authoritative_state']),source_sha256=state['source_sha256'],
        committed_through=state['committed_through'])


def validate_readback(value):
    try:
        fields={'schema_version','batch_id','grant_sha256','chapter','step_id','execution_id','input_sha256',
            'publication_result_sha256','publication','state_sha256','prose_sha256','document_sha256','next_context','result_sha256'}
        author=isinstance(value,dict) and value.get('schema_version')=='reviewed_author_readback_v1'
        if author:fields.add('takeover_sha256')
        if (not isinstance(value,dict) or set(value)!=fields or value['schema_version'] not in {'reviewed_policy_readback_v1','reviewed_author_readback_v1'}
                or type(value['chapter']) is not int or value['chapter']<1
                or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None
                    for k in fields if k.endswith('_sha256'))
                or value['result_sha256']!=digest({k:v for k,v in value.items() if k!='result_sha256'})
                or value['step_id']!=_step(value['batch_id'],value['chapter'],'readback')
                or value['execution_id']!='execution_readback_'+digest({'batch_id':value['batch_id'],'chapter':value['chapter']})[:32]
                or value['publication']['chapter']!=value['chapter'] or value['publication']['publication_verified'] is not True
                or value['publication']['document_sha256']!=value['document_sha256']
                or value['publication']['prose_sha256']!=value['prose_sha256']):
            raise BatchControlError('原正式 readback 凭据形状、绑定或哈希无效。')
        n=value['next_context']
        if n is not None and (set(n)!={'chapter','context_sha256','read_set_sha256','authoritative_state_sha256','source_sha256','committed_through'}
                or type(n['chapter']) is not int or n['chapter']!=value['chapter']+1 or type(n['committed_through']) is not int
                or n['committed_through']!=value['chapter'] or n['source_sha256']!=value['document_sha256']
                or n['authoritative_state_sha256']!=value['state_sha256']
                or any(not isinstance(n[k],str) or re.fullmatch('[0-9a-f]{64}',n[k]) is None for k in n if k.endswith('_sha256'))):
            raise BatchControlError('原 readback 的后续章上下文绑定无效。')
        return copy.deepcopy(value)
    except (KeyError,TypeError,ValueError) as exc:raise BatchControlError('原正式 readback 凭据不完整。') from exc


def require_readback_parent(publication,context,grant,chapter):
    from workbench.reviewed_batch_controls import control_paths
    from workbench.reviewed_batches import ReviewedBatchService
    m=grant['manifest'];root=context[5]
    if type(chapter) is not int or chapter not in m['chapters'] or chapter<=m['start_chapter']:
        raise BatchControlError('后续章不属于原批次范围。')
    # Reconcile an already recorded command even if its book mirror was torn.
    # Dispatch/step admission still loads and requires identical copies.
    _,machine,_=control_paths(context,m['batch_id'])
    control,_=validate_control(BatchControlStore._read(machine),grant)
    parent=None
    for previous in range(m['start_chapter'],chapter):
        step=_step(m['batch_id'],previous,'readback')
        saved=validate_readback(ReviewedBatchService._read(root/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')))
        if saved['schema_version']=='reviewed_author_readback_v1':
            from workbench.reviewed_batch_takeovers import published_author_original
            actual=published_author_original(publication,context,grant,previous,require_tip=previous==chapter-1)
            completed=any(c['action']=='finish_author_chapter' and c['payload']['takeover_sha256']==saved['takeover_sha256']
                and c['payload']['readback_result_sha256']==saved['result_sha256'] for c in control['commands'])
        else:
            actual=published_original(publication,context,grant,previous,require_tip=previous==chapter-1)
            completed=any(c['action']=='finish_step' and c['payload']['step_id']==step
                and c['payload']['result_sha256']==saved['result_sha256'] for c in control['commands'])
        if (saved['grant_sha256']!=grant['grant_sha256'] or saved['step_id']!=step or saved['publication']!=actual
                or not completed):
            raise BatchControlError('后续章缺少本批次原正式提交和 readback 完成证明。')
        ReviewedPolicyReadbackService._require_available_time(root,m['book_id'],previous)
        parent=saved
    if parent['next_context']!=_next(root,chapter):
        from workbench.restored_batch_continuation import require_restored_parent_bridge
        require_restored_parent_bridge(context,grant,parent,_next(root,chapter))
    return parent


class ReviewedPolicyReadbackService:
    def __init__(self,controls,*,fault_injector=None):
        self.controls=controls;self.publication=controls.publication;self.fault_injector=fault_injector

    def get(self,*,project_id,batch_id,chapter):
        context=self.controls._control_context(project_id)
        grant=self.controls.batches._stored(context,batch_id)['grant']
        if type(chapter) is not int or chapter not in grant['manifest']['chapters']:
            raise BatchControlError('原 readback 结果章号不属于批次。')
        step=_step(batch_id,chapter,'readback')
        saved=validate_readback(self.controls.batches._read(context[6]/'reviewed/batch_results'/(step+'.json')))
        if saved['grant_sha256']!=grant['grant_sha256'] or saved['step_id']!=step:
            raise BatchControlError('原 readback 结果与批次授权不同。')
        return dict(result=saved,execution_permit=False,publication_authorized=False)

    def recover_finish(self,*,project_id,batch_id,chapter):
        saved=self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']
        def verify(context,grant,control,pending):
            original=self._inputs(context,grant,chapter)
            if (saved['input_sha256']!=digest({'grant_sha256':grant['grant_sha256'],'actual_readback':original})
                    or any(saved[k]!=v for k,v in original.items())
                    or pending['stage']!='readback' or pending['execution_id']!=saved['execution_id']
                    or pending['input_sha256']!=saved['input_sha256']
                    or self.get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']!=saved):
                raise BatchControlError('原 readback 完成命令与当前实际正式输入不同。')
            self._require_available_time(context[5],grant['manifest']['book_id'],chapter)
            return saved['result_sha256']
        final=self.controls._reconcile_original_finish(project_id=project_id,batch_id=batch_id,
            idempotency_key='finish-readback:'+str(chapter),result_sha256=saved['result_sha256'],step_id=saved['step_id'],verify_result=verify)
        return dict(result=saved,control=final,execution_permit=False,publication_authorized=False)

    @staticmethod
    def _require_available_time(root,book_id,chapter):
        latest=ChapterExecutionBudget(root,book_id=book_id,chapter=chapter).time_clock.report()
        if latest['active_status']=='unknown':raise ChapterTimeUnknown('完成 readback 前原章主动时间未知。')
        if latest['active_limit_reached']:raise ChapterTimeExceeded('完成 readback 前原章主动时间已超限。')

    def _inputs(self,context,grant,chapter):
        p=self.publication;batch=grant['manifest']['batch_id'];root=context[5]
        self.controls.batches._require_frozen(context,batch)
        quality,control=_quality(p,context,grant,chapter)
        published=published_original(p,context,grant,chapter)
        if published is None:raise BatchControlError('原章尚无真实正式提交，不能 readback。')
        step=_step(batch,chapter,'publication')
        result=validate_publication_result(self.controls.batches._read(root/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')))
        if (result['schema_version']!='reviewed_policy_publication_result_v1' or result['step_id']!=step
                or result['grant_sha256']!=grant['grant_sha256'] or result['publication']!=published
                or result['quality_result_sha256']!=quality['result_sha256']
                or result['result_sha256']!=digest({k:v for k,v in result.items() if k!='result_sha256'})
                or not any(c['action']=='finish_step' and c['payload']['step_id']==step
                    and c['payload']['result_sha256']==result['result_sha256'] for c in control['commands'])):
            raise BatchControlError('原发布步骤完成证明与真实正式提交不同。')
        state=load_verified_reviewed_identity(root)['history']
        prose=read_project_bytes(root,canonical_prose_path(root,chapter).relative_to(root).as_posix())
        if digest(prose)!=published['prose_sha256'] or state['document_sha256']!=published['document_sha256']:
            raise BatchControlError('正式正文或当前正式状态与原提交凭证不同。')
        return dict(publication_result_sha256=result['result_sha256'],publication=published,
            document_sha256=state['document_sha256'],prose_sha256=digest(prose),state_sha256=digest(state['value']['current_state']),
            next_context=_next(root,chapter+1) if chapter<grant['manifest']['end_chapter'] else None)

    def readback(self,handle,*,expected_control_sha256):
        if self.controls.handles.get(getattr(handle,'batch_id',None)) is not handle or handle.service is not self.controls:
            raise BatchControlPaused('正式 readback 需要本次明确批次执行许可。')
        with self.publication._owned_project(handle.project_id) as context:
            grant=self.controls.batches._require_current_execution(context,handle.batch_id)['grant']
            _,state=self.controls._store(context,handle.batch_id,grant).load();chapter=state['chapter'];root=context[5]
            if state['status']!='running' or state['run_epoch']!=handle.epoch or state['stage']!='readback':
                raise BatchControlPaused('原批次当前没有待 readback 步骤。')
            original=self._inputs(context,grant,chapter)
        source=digest({'grant_sha256':grant['grant_sha256'],'actual_readback':original})
        def inputs():return digest({'grant_sha256':grant['grant_sha256'],
            'actual_readback':self._inputs(self.controls._control_context(handle.project_id),grant,chapter)})
        step=_step(handle.batch_id,chapter,'readback');execution='execution_readback_'+digest({'batch_id':handle.batch_id,'chapter':chapter})[:32]
        path=root/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')
        with self.controls.step(handle,stage='readback',execution_id=execution,input_sha256=source,
                idempotency_key='claim-readback:'+str(chapter),expected_control_sha256=expected_control_sha256,
                verify_inputs=inputs,local_recovery=True) as permit:
            permit.require_control()
            result=dict(schema_version='reviewed_policy_readback_v1',batch_id=handle.batch_id,grant_sha256=grant['grant_sha256'],
                chapter=chapter,step_id=step,execution_id=execution,input_sha256=source,**original)
            result['result_sha256']=digest(result);validate_readback(result)
            require_restored_execution_paths(path)
            if os.path.lexists(path):
                if self.controls.batches._read(path)!=result:raise BatchControlError('原 readback 结果不同，不能覆盖。')
            else:atomic_create_json(path,result)
        if self.fault_injector is not None:self.fault_injector('after_readback_before_finish')
        def verify():
            if self.controls.batches._read(path)!=result or inputs()!=source:
                raise BatchControlError('完成 readback 前实际正文、状态或原凭证变化。')
            return result['result_sha256']
        current=self.controls.get(project_id=handle.project_id,batch_id=handle.batch_id)
        time=ChapterExecutionBudget(root,book_id=grant['manifest']['book_id'],chapter=chapter).time_clock.report()
        if time['active_status']=='unknown' or time['active_limit_reached']:
            final=self.controls.pause_step(handle,expected_control_sha256=current['control']['record_sha256'],
                idempotency_key='pause-readback:'+str(chapter)+':'+handle.epoch,verify_result=verify)
        else:
            def verify_available():
                value=verify()
                self._require_available_time(root,grant['manifest']['book_id'],chapter)
                return value
            final=self.controls.finish_step(handle,expected_control_sha256=current['control']['record_sha256'],
                idempotency_key='finish-readback:'+str(chapter),result_sha256=result['result_sha256'],verify_result=verify_available)
        return dict(result=result,control=final,execution_permit=False,publication_authorized=False)
