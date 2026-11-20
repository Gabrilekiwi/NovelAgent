"""Explicit author takeover and receipt-backed continuation of an original batch."""
import copy,json,os,re

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.persistence import atomic_create_json,persistence_state_lock
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,BatchControlStore,validate_control
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest,read_project_bytes
from core.story_project.reviewed_state_store import read_verified_commit_requests
from workbench.restore_execution_policy import require_restored_execution_paths


def takeover_path(context,batch,chapter):
    return context[6]/'reviewed/batch_takeovers'/('takeover_'+digest({'batch_id':batch,'chapter':chapter})[:32]+'.json')


def read_takeover(context,grant,chapter):
    from workbench.reviewed_batches import ReviewedBatchService,ReviewedBatchError
    try:value=ReviewedBatchService._read(takeover_path(context,grant['manifest']['batch_id'],chapter))
    except ReviewedBatchError as exc:raise BatchControlError('原作者接管记录缺失或不可读。') from exc
    fields={'schema_version','batch_id','grant_sha256','chapter','control_sha256','original_progress',
        'generation_result_sha256','candidate_intent_hash','draft_at_takeover','context_before','author_confirmed','takeover_sha256'}
    if (not isinstance(value,dict) or set(value)!=fields or value['schema_version']!='reviewed_batch_author_takeover_v1'
            or value['author_confirmed'] is not True or type(value['chapter']) is not int or value['chapter']!=chapter
            or value['batch_id']!=grant['manifest']['batch_id'] or value['grant_sha256']!=grant['grant_sha256']
            or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None for k in fields if k.endswith('_sha256'))
            or not isinstance(value['candidate_intent_hash'],str) or re.fullmatch('[0-9a-f]{64}',value['candidate_intent_hash']) is None
            or value['takeover_sha256']!=digest({k:v for k,v in value.items() if k!='takeover_sha256'})):
        raise BatchControlError('原作者接管记录与批次授权或原稿不同。')
    return value


def published_author_original(publication,context,grant,chapter,*,require_tip=True,route=None):
    """Read the actual author receipt and its immutable request preparation."""
    try:return _published_author_original(publication,context,grant,chapter,require_tip=require_tip,route=route)
    except BatchControlError:raise
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise BatchControlError('原作者提交凭证、正式正文或状态损坏，不能继续批次。') from exc


def _published_author_original(publication,context,grant,chapter,*,require_tip=True,route=None):
    from workbench.reviewed_batch_controls import control_paths
    snapshot=read_takeover(context,grant,chapter)
    _,machine,_=control_paths(context,grant['manifest']['batch_id'])
    control,_=validate_control(BatchControlStore._read(machine),grant)
    if not any(c['action']=='author_takeover' and c['payload']['takeover_sha256']==snapshot['takeover_sha256'] for c in control['commands']):
        raise BatchControlError('原作者接管缺少机器控制命令。')
    records=read_verified_commit_requests(root=context[5],book_id=grant['manifest']['book_id'],
        root_map={'story_project':context[5],'runtime':context[6]},baseline_document_sha256=grant['manifest']['baseline_document_sha256'])
    actual=next((r for r in records if r['chapter']==chapter),None)
    if actual is None:raise BatchControlError('作者尚未真实正式提交接管章节。')
    evidence=json.loads(read_project_bytes(context[6],'reviewed/requests/'+actual['run_id']+'.json'))
    prepared=evidence['preparation'];b=prepared['binding'];draft=snapshot['draft_at_takeover']
    if (b['acceptance_kind']!='author' or not b.get('confirmation_sha256')
            or b['draft_id']!=draft['id'] or b['candidate_id']!=draft['candidate_id']
            or b['candidate_intent_hash']!=snapshot['candidate_intent_hash']
            or b['reviewed_source_sha256']!=snapshot['context_before']['source_sha256']
            or prepared['preparation_sha256']!=actual['preparation_sha256'] or evidence['request']!=actual['request']):
        raise BatchControlError('正式提交不是原接管稿件的真实作者确认。')
    if route is not None:
        from core.engine.chapter_commit_request import chapter_commit_request
        if chapter_commit_request(book_id=grant['manifest']['book_id'],request=route)!=actual['request']:
            raise BatchControlError('找回的原作者提交请求与这次ID/CAS不同。')
    history=load_verified_reviewed_identity(context[5])['history']
    if require_tip and (history['value']['committed_through']!=chapter or history['document_sha256']!=actual['document_sha256']):
        raise BatchControlError('原作者提交不是当前正式章节。')
    return actual


class ReviewedBatchTakeoverService:
    def __init__(self,controls,*,fault_injector=None):
        self.controls=controls;self.publication=controls.publication;self.fault_injector=fault_injector

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    def _replay(self,context,grant,*,action,key,expected_control_sha256,verify):
        """Reconcile only an original command and its exact previous CAS."""
        from workbench.reviewed_batch_controls import control_paths
        c=self.controls;batch=grant['manifest']['batch_id'];store=c._store(context,batch,grant)
        _,machine,book=control_paths(context,batch)
        with persistence_state_lock((machine,book)):
            record,_=validate_control(store._read(machine),grant)
            prior=next((v for v in record['commands'] if v['key_sha256']==digest(key.encode('utf-8'))),None)
            if prior is None:return None
            prefix={**record,'commands':record['commands'][:prior['sequence']-1]}
            prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
            if prior['action']!=action or expected_control_sha256!=prefix['record_sha256']:
                raise BatchControlError('续办请求不是同一原接管命令或前序版本。')
            def before():
                if c.batches._stored(context,batch)['grant']!=grant:
                    raise BatchControlError('原接管命令授权变化。')
                verify(prior,prefix)
            before()
            store.apply(action=action,payload=prior['payload'],key=key,expected_sha256=expected_control_sha256,
                before_write=before,fault_injector=self.fault_injector)
        # An old response must not revoke a later explicit resume in this service.
        return c._view(context,batch,grant)

    def get(self,*,project_id,batch_id,chapter):
        """Read original snapshot/control only; never continue or repair copies."""
        from workbench.reviewed_batch_controls import control_paths
        c=self.controls;context=c._control_context(project_id);grant=c.batches._stored(context,batch_id)['grant']
        if type(chapter) is not int or chapter not in grant['manifest']['chapters']:
            raise BatchControlError('原接管章号不属于批次。')
        snapshot=read_takeover(context,grant,chapter)
        _,machine,book=control_paths(context,batch_id)
        record,_=validate_control(BatchControlStore._read(machine),grant)
        mirror,_=validate_control(BatchControlStore._read(book),grant)
        prior=next((v for v in record['commands'] if v['key_sha256']==digest(('author-takeover:'+str(chapter)).encode('utf-8'))),None)
        if prior is not None and (prior['action']!='author_takeover' or prior['payload']!={'takeover_sha256':snapshot['takeover_sha256']}):
            raise BatchControlError('原接管命令与保存稿件不一致。')
        return dict(takeover=snapshot,original_control_recorded=prior is not None,
            control_reconciliation_required=record!=mirror,execution_permit=False,publication_authorized=False)

    def takeover(self,*,project_id,batch_id,draft_id,expected_control_sha256,author_confirmed):
        from workbench.reviewed_batch_controls import control_paths
        from workbench.reviewed_policy_readback import _next
        if author_confirmed is not True:raise BatchControlError('请明确确认接管原批次稿件。')
        c=self.controls;p=self.publication
        with p._owned_project(project_id) as context:
            grant=c.batches._stored(context,batch_id)['grant']
            requested=p.preparations.drafts.get(draft_id,connection=context[1])
            if requested.project_id!=project_id or requested.chapter not in grant['manifest']['chapters']:
                raise BatchControlError('原接管稿不属于批次项目。')
            chapter=requested.chapter
            def verify_original(prior,prefix):
                original=read_takeover(context,grant,chapter)
                _,previous=validate_control(prefix,grant)
                if (prior['payload']!={'takeover_sha256':original['takeover_sha256']}
                        or original['draft_at_takeover']['id']!=draft_id or original['control_sha256']!=prefix['record_sha256']
                        or original['original_progress']!=previous or previous['chapter']!=chapter or previous['status']!='paused'):
                    raise BatchControlError('原接管请求或保存前进度不一致。')
            replay=self._replay(context,grant,action='author_takeover',key='author-takeover:'+str(chapter),
                expected_control_sha256=expected_control_sha256,verify=verify_original)
            if replay is not None:
                return dict(takeover=read_takeover(context,grant,chapter),control=replay,execution_permit=False,publication_authorized=False)
            grant=c.batches._require_current_execution(context,batch_id)['grant'];store=c._store(context,batch_id,grant)
            record,state=store.load();chapter=state['chapter']
            if record['record_sha256']!=expected_control_sha256 or state['status']!='paused':
                raise BatchControlPaused('请先暂停原批次并核对当前控制版本后接管。')
            if load_verified_reviewed_identity(context[5])['history']['value']['committed_through']!=chapter-1:
                raise BatchControlError('该章节已有正式提交，需找回原结果后回读。')
            draft,intent=p.preparations.fact_reviews.verify_draft_source(draft_id,connection=context[1])
            auth=intent.get('policy_authorization',{})
            if (draft.project_id!=project_id or draft.chapter!=chapter or auth.get('batch_id')!=batch_id
                    or auth.get('grant_sha256')!=grant['grant_sha256']):
                raise BatchControlError('接管稿不属于原批次实际生成结果。')
            generation=None
            for stage in ('generation','repair_generation'):
                step='step_'+digest({'batch_id':batch_id,'chapter':chapter,'stage':stage})[:32]
                path=context[6]/'reviewed/batch_results'/(step+'.json')
                if os.path.lexists(path):
                    value=c.batches._read(path)
                    if value.get('draft_id')==draft_id:generation=value
            if (generation is None or generation['grant_sha256']!=grant['grant_sha256']
                    or generation['candidate_intent_hash']!=intent['intent_hash']
                    or generation['result_sha256']!=digest({k:v for k,v in generation.items() if k!='result_sha256'})):
                raise BatchControlError('原接管稿缺少完整生成凭据。')
            budget=ChapterExecutionBudget(context[5],book_id=grant['manifest']['book_id'],chapter=chapter)
            with budget.operation('execution_takeover_'+digest({'batch_id':batch_id,'chapter':chapter})[:32],allow_provider_dispatch=False) as action:
                value=dict(schema_version='reviewed_batch_author_takeover_v1',batch_id=batch_id,grant_sha256=grant['grant_sha256'],
                    chapter=chapter,control_sha256=record['record_sha256'],original_progress=copy.deepcopy(state),
                    generation_result_sha256=generation['result_sha256'],candidate_intent_hash=intent['intent_hash'],
                    draft_at_takeover=draft.to_dict(),context_before=_next(context[5],chapter),author_confirmed=True)
                value['takeover_sha256']=digest(value);path=takeover_path(context,batch_id,chapter)
                require_restored_execution_paths(path)
                if os.path.lexists(path):
                    if c.batches._read(path)!=value:raise BatchControlError('原接管记录不能被另一稿件或控制版本覆盖。')
                else:atomic_create_json(path,value)
                self._fault('after_takeover_snapshot')
                _,machine,book=control_paths(context,batch_id)
                def before():
                    c.batches._require_frozen(context,batch_id)
                    if p.preparations.drafts.get(draft_id,connection=context[1])!=draft or c.batches._read(path)!=value:
                        raise BatchControlError('保存接管命令前原稿变化。')
                    action.interval.remaining_seconds()
                with persistence_state_lock((machine,book)):
                    store.apply(action='author_takeover',payload={'takeover_sha256':value['takeover_sha256']},key='author-takeover:'+str(chapter),
                        expected_sha256=expected_control_sha256,before_write=before,fault_injector=self.fault_injector)
            c.handles.pop(batch_id,None)
            return dict(takeover=value,control=c._view(context,batch_id,grant),execution_permit=False,publication_authorized=False)

    def complete(self,*,project_id,batch_id,route,expected_control_sha256,author_confirmed):
        from workbench.reviewed_batch_controls import control_paths
        from workbench.reviewed_policy_publication import _step
        from workbench.reviewed_policy_readback import _next,validate_readback,ReviewedPolicyReadbackService
        if author_confirmed is not True:raise BatchControlError('请明确确认找回作者提交并完成本章回读。')
        if not isinstance(route,dict):raise BatchControlError('原作者提交请求必须是ID/CAS对象。')
        c=self.controls;p=self.publication
        with p._owned_project(project_id) as context:
            grant=c.batches._stored(context,batch_id)['grant'];chapter=route.get('chapter')
            if type(chapter) is not int or chapter not in grant['manifest']['chapters'] or route.get('project_id')!=project_id:
                raise BatchControlError('原作者提交请求不属于批次。')
            snapshot=read_takeover(context,grant,chapter)
            path=context[6]/'reviewed/batch_results'/(_step(batch_id,chapter,'readback')+'.json')
            def verify_original(prior,prefix):
                original=validate_readback(c.batches._read(path));_,previous=validate_control(prefix,grant)
                actual=published_author_original(p,context,grant,chapter,require_tip=False,route=route)
                if (original['schema_version']!='reviewed_author_readback_v1' or original['publication']!=actual
                        or original['grant_sha256']!=grant['grant_sha256'] or original['takeover_sha256']!=snapshot['takeover_sha256']
                        or prior['payload']!=dict(takeover_sha256=snapshot['takeover_sha256'],readback_result_sha256=original['result_sha256'])
                        or previous['chapter']!=chapter or previous['status']!='paused' or previous['stage']!='author_review'):
                    raise BatchControlError('原作者完成命令与实际提交和回读不同。')
            replay=self._replay(context,grant,action='finish_author_chapter',key='finish-author:'+str(chapter),
                expected_control_sha256=expected_control_sha256,verify=verify_original)
            if replay is not None:
                return dict(result=validate_readback(c.batches._read(path)),control=replay,execution_permit=False,publication_authorized=False)
            grant=c.batches._require_frozen(context,batch_id)['grant'];store=c._store(context,batch_id,grant)
            control,state=store.load()
            actual=published_author_original(p,context,grant,chapter,route=route)
            if state['stage']!='author_review' or state['chapter']!=chapter or state['status']!='paused':
                raise BatchControlPaused('原批次当前没有待完成的作者接管章节。')
            if control['record_sha256']!=expected_control_sha256:raise BatchControlError('原作者接管控制版本变化。')
            budget=ChapterExecutionBudget(context[5],book_id=grant['manifest']['book_id'],chapter=chapter)
            ReviewedPolicyReadbackService._require_available_time(context[5],grant['manifest']['book_id'],chapter)
            with budget.operation('execution_author_readback_'+digest({'batch_id':batch_id,'chapter':chapter})[:32],allow_provider_dispatch=False) as action:
                history=load_verified_reviewed_identity(context[5])['history']
                result=dict(schema_version='reviewed_author_readback_v1',batch_id=batch_id,grant_sha256=grant['grant_sha256'],
                    chapter=chapter,step_id=_step(batch_id,chapter,'readback'),
                    execution_id='execution_readback_'+digest({'batch_id':batch_id,'chapter':chapter})[:32],
                    takeover_sha256=snapshot['takeover_sha256'],publication=actual,document_sha256=actual['document_sha256'],
                    prose_sha256=actual['prose_sha256'],state_sha256=digest(history['value']['current_state']),
                    publication_result_sha256=digest({'takeover_sha256':snapshot['takeover_sha256'],'publication':actual}),
                    next_context=_next(context[5],chapter+1) if chapter<grant['manifest']['end_chapter'] else None)
                result['input_sha256']=digest(result);result['result_sha256']=digest(result);validate_readback(result)
                path=context[6]/'reviewed/batch_results'/(result['step_id']+'.json');require_restored_execution_paths(path)
                if os.path.lexists(path):
                    if c.batches._read(path)!=result:raise BatchControlError('原作者回读结果变化，不能覆盖。')
                else:atomic_create_json(path,result)
                self._fault('after_author_readback')
                _,machine,book=control_paths(context,batch_id)
                def before():
                    c.batches._require_frozen(context,batch_id)
                    if (published_author_original(p,context,grant,chapter,route=route)!=actual or c.batches._read(path)!=result
                            or result['next_context']!=(_next(context[5],chapter+1) if chapter<grant['manifest']['end_chapter'] else None)):
                        raise BatchControlError('完成作者章节前正式正文、状态或下一章上下文变化。')
                    action.interval.remaining_seconds()
                with persistence_state_lock((machine,book)):
                    store.apply(action='finish_author_chapter',payload=dict(takeover_sha256=snapshot['takeover_sha256'],
                        readback_result_sha256=result['result_sha256']),key='finish-author:'+str(chapter),
                        expected_sha256=expected_control_sha256,before_write=before,fault_injector=self.fault_injector)
            c.handles.pop(batch_id,None)
            return dict(result=result,control=c._view(context,batch_id,grant),execution_permit=False,publication_authorized=False)
