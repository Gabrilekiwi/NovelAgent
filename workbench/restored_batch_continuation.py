"""Explicit restored-batch preparation; imported grants never become permits.

The original grant, control, task records and budget ledgers remain immutable.
A confirmed preparation creates a separate control namespace under the actual
restored qualification. Dispatch integration must independently admit that
namespace; neither construction nor preparation starts a worker.
"""
import copy,json,os,re
from pathlib import Path

from core.batch_budget_contract import project_batch_usage
from core.batch_budget_store import BatchBudgetRegistry
from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.persistence import atomic_create_json,persistence_state_lock
from core.engine.reviewed_batch_control import BatchControlError,initial_control,validate_control
from core.engine.safe_paths import assert_safe_local_tree,SafePathError
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.backup_version import fingerprint_software
from workbench.reviewed_batch_controls import control_ready
from workbench.reviewed_batches import _selection,_outlines
from workbench.restore_scope import load_restore_scope
from workbench.restore_handoff import validate_handoff_intent


def _signed(value):return {**value,'record_sha256':digest(value)}


def _original_grant(context,batch):
    read=RestoredBatchContinuationService._read
    if not isinstance(batch,str) or re.fullmatch('batch_[0-9a-f]{32}',batch) is None:
        raise BatchControlError('原恢复批次编号无效。')
    grant=read(context[6],'reviewed/batches/'+batch+'.json')
    if not isinstance(grant,dict):raise BatchControlError('恢复副本的原批次授权无效。')
    m=grant.get('manifest')
    if (not isinstance(m,dict) or set(grant)!={'schema_version','manifest','grant_source','author_confirmed','automatic_review_source','grant_sha256'}
            or grant['schema_version']!='reviewed_batch_author_grant_v1' or grant['grant_source']!='author'
            or grant['author_confirmed'] is not True or grant['automatic_review_source']!='policy'
            or grant['grant_sha256']!=digest({k:v for k,v in grant.items() if k!='grant_sha256'})
            or m.get('manifest_sha256')!=digest({k:v for k,v in m.items() if k!='manifest_sha256'})
            or m.get('batch_id')!=batch or m.get('project_id')!=context[0]['project_id'] or m.get('book_id')!=context[0]['book_id']):
        raise BatchControlError('恢复副本的原批次授权形状、身份或摘要不同。')
    machine=context[2].directory
    if (read(machine,'bg-'+batch+'.json')!=grant
            or read(machine,'bgr-'+batch+'.json')!=control_ready(grant)
            or read(machine,'bc0-'+batch+'.json')!=initial_control(grant)):
        raise BatchControlError('原机器批次授权、初始或准备凭证不同。')
    budget,_=BatchBudgetRegistry(context[6]/'budgets',book_id=m['book_id']).load(batch)
    if budget!=m['budget']:raise BatchControlError('恢复批次丢失原冻结预算。')
    return grant


def _qualified_handoff(context,qualification,depth=0):
    if depth>16:raise BatchControlError('恢复批次来源链超过支持深度。')
    value=RestoredBatchContinuationService._read(context[2].directory,
        'handoff-'+qualification['handoff_id']+'-intent.json')
    validate_handoff_intent(value,context[0]['book_id'],depth=depth)
    if (value['intent_sha256']!=qualification['handoff_intent_sha256']
            or value['restoring_record']['binding']['root']!=qualification['book_root']
            or value['restoring_record']['binding']['database']!=qualification['database']):
        raise BatchControlError('恢复批次交接不属于同一来源资格。')
    return value


def _source_origin(context,grant,qualification,depth=0):
    read=RestoredBatchContinuationService._read;m=grant['manifest'];batch=m['batch_id']
    handoff=_qualified_handoff(context,qualification,depth);owner=handoff['source_record']
    root=Path(owner['binding']['root'])
    if m['source_root']==str(root) and m['ownership_sha256']==owner['record_sha256']:
        relative='reviewed/batch_controls/'+batch+'.json'
        origin=read(context[6],relative);machine=context[2].directory/('bc-'+batch+'.json')
    else:
        from workbench.backup_restore_provenance import validate_restore_provenance,RestoreProvenanceError
        proof=handoff.get('source_restore_provenance')
        if not isinstance(proof,dict):raise BatchControlError('原批次缺少已核验的上一代恢复来源。')
        try:validate_restore_provenance(proof,root=root,book_id=m['book_id'],ownership=owner,depth=depth+1)
        except RestoreProvenanceError as exc:raise BatchControlError('上一代恢复来源证明无效。') from exc
        prior=_qualified_continuation(context,grant,proof['qualification'],owner,depth+1)
        if prior['book_control']!=prior['machine_control']:
            raise BatchControlError('上一代恢复批次控制尚未完整保存。')
        origin=prior['book_control'];relative='reviewed/continuations/'+prior['token']+'/control.json'
        machine=context[2].directory/('brcc-'+prior['token']+'.json')
    validate_control(origin,grant)
    return origin,dict(root=root,relative=relative,machine=machine,handoff=handoff)


def _qualified_continuation(context,grant,qualification,ownership,depth=0):
    if depth>16:raise BatchControlError('恢复批次续接链超过支持深度。')
    read=RestoredBatchContinuationService._read;m=grant['manifest'];batch=m['batch_id']
    token=digest({'batch_id':batch,'qualification_sha256':qualification['qualification_sha256']})[:24]
    base=assert_safe_local_tree(context[6]/'reviewed/continuations'/token);machine=context[2].directory
    intent=read(base,'intent.json')
    if not isinstance(intent,dict):raise BatchControlError('恢复批次原确认无效。')
    preview=intent.get('preview')
    if not isinstance(preview,dict):raise BatchControlError('恢复批次原确认预览无效。')
    if (set(intent)!={'schema_version','preview','author_confirmed','record_sha256'}
            or intent['schema_version']!='restored_batch_continuation_intent_v1' or intent['author_confirmed'] is not True
            or intent!=_signed({k:v for k,v in intent.items() if k!='record_sha256'})
            or preview!=_signed({k:v for k,v in preview.items() if k!='record_sha256'})
            or preview.get('schema_version')!='restored_batch_continuation_preview_v1'
            or preview.get('batch_id')!=batch or preview.get('project_id')!=m['project_id']
            or preview.get('book_id')!=m['book_id'] or preview.get('can_prepare') is not True
            or preview.get('blockers')!=[] or preview.get('execution_permit') is not False
            or preview.get('qualification_sha256')!=qualification['qualification_sha256']
            or preview.get('ownership_sha256')!=ownership['record_sha256']
            or preview.get('handoff_intent_sha256')!=qualification['handoff_intent_sha256']
            or read(machine,'brc-'+token+'.json')!=intent):
        raise BatchControlError('恢复批次缺少同一原确认及来源机器资格。')
    origin,source=_source_origin(context,grant,qualification,depth)
    if (read(base,'grant.json')!=grant or grant['grant_sha256']!=preview.get('grant_sha256')
            or read(base,'control-origin.json')!=origin or read(source['machine'].parent,source['machine'].name)!=origin
            or origin['record_sha256']!=preview.get('original_control_sha256')
            or read(base,'anchor.json')!=initial_control(grant)):
        raise BatchControlError('恢复批次的原授权、原控制或独立机器证明已变化。')
    _,original_state=validate_control(origin,grant)
    if original_state!=preview.get('original_progress'):raise BatchControlError('恢复续接原位置不同。')
    controls=[]
    for path in (base/'control.json',machine/('brcc-'+token+'.json')):
        current,_=validate_control(read(path.parent,path.name),grant)
        if current['commands'][:len(origin['commands'])]!=origin['commands']:
            raise BatchControlError('恢复控制链不能回退或替换原批次历史。')
        controls.append(current)
    ready=_signed(dict(schema_version='restored_batch_continuation_ready_v1',intent_sha256=intent['record_sha256'],
        original_control_sha256=origin['record_sha256'],qualification_sha256=qualification['qualification_sha256'],execution_permit=False))
    if read(base,'ready.json')!=ready or read(base,'grant-ready.json')!=control_ready(grant):
        raise BatchControlError('恢复批次明确准备尚未完整完成。')
    return dict(grant=grant,preview=preview,origin=origin,base=base,token=token,
        book_control=controls[0],machine_control=controls[1],source=source)


def continuation_location(context,batch_id):
    """Only a moved imported grant selects a qualified, separate namespace."""
    if re.fullmatch('batch_[0-9a-f]{32}',str(batch_id)) is None:raise BatchControlError('批次编号无效。')
    if context[4].get('version')!='machine_book_ownership_v3':return None
    scope=load_restore_scope(context[4]['binding']['database'],machine_store=context[2].store)
    if scope is None or not scope.active or scope.book_root!=context[5]:return None
    path=context[6]/'reviewed/batches'/(batch_id+'.json')
    if not os.path.lexists(path):return None
    grant=RestoredBatchContinuationService._read(path.parent,path.name)
    if grant.get('manifest',{}).get('source_root')==str(context[5]):return None
    token=digest({'batch_id':batch_id,'qualification_sha256':scope.activation['qualification_sha256']})[:24]
    return scope,assert_safe_local_tree(context[6]/'reviewed/continuations'/token),token


def load_continuation(context,batch_id):
    selected=continuation_location(context,batch_id)
    if selected is None:return None
    scope,_,_=selected
    grant=_original_grant(context,batch_id)
    saved=_qualified_continuation(context,grant,scope.activation,context[4])
    return {**saved,'scope':scope}


def require_restored_parent_bridge(context,grant,parent,current):
    saved=load_continuation(context,grant['manifest']['batch_id'])
    if saved is None:raise BatchControlError('下一章上下文不同，且无明确恢复续接。')
    preview=saved['preview'];bridge=preview.get('parent_bridge')
    if (not isinstance(bridge,dict) or bridge.get('readback_sha256')!=parent['result_sha256']
            or bridge.get('original_next_context')!=parent['next_context']
            or preview['current_context']!=current
            or preview['original_progress']['chapter']!=current['chapter']
            or any(parent['next_context'][k]!=current[k] for k in
                ('chapter','authoritative_state_sha256','source_sha256','committed_through'))):
        raise BatchControlError('当前权威状态不能通过原回读与明确恢复续接连接。')


def restored_author_review_source(database,project,draft,intent):
    """Effective author-review source; the original Candidate Intent is untouched."""
    scope=database.restore_scope
    if scope is None or not scope.active:return None
    old_read_set=intent.get('source',{}).get('story_project_read_set',{})
    if old_read_set.get('root_identity',{}).get('resolved_path')==str(project.root):return None
    from workbench.book_ownership import BookOwnership,_record,_require_witness,_file_identity
    from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
    from workbench.reviewed_policy_readback import _next
    authorization=intent.get('policy_authorization',{});batch_id=authorization.get('batch_id')
    if not isinstance(batch_id,str):return None
    owner=BookOwnership(project.book_id,machine_store=scope.machine_store);record=_record(owner.directory,project.book_id)
    _require_witness(project.root,project.book_id,record)
    if (record is None or record['state']!='active' or record['binding']['root']!=str(project.root)
            or record['binding']['database']!=str(database.path) or record['binding']['project_id']!=project.id
            or record['binding']['database_file_identity']!=_file_identity(database.path)):
        raise BatchControlError('恢复作者审核不属于当前原作品写资格。')
    context=({'project_id':project.id,'book_id':project.book_id,'root':project.root},None,owner,{},record,
        project.root,project.root/'.novelagent/runtime',{},project.root/'.novelagent/runtime/reviewed/p')
    selected=continuation_location(context,batch_id)
    if selected is None:return None
    saved=load_continuation(context,batch_id);preview=saved['preview'];m=saved['grant']['manifest'];bridge=preview['parent_bridge']
    if (draft.chapter!=preview['original_progress']['chapter'] or intent['book_id']!=project.book_id
            or authorization.get('grant_sha256')!=saved['grant']['grant_sha256']
            or intent['source']['context_sha256']!=(bridge['original_next_context']['context_sha256'] if bridge else m['initial_context_sha256'])
            or _next(project.root,draft.chapter)!=preview['current_context']):
        raise BatchControlError('原候选稿不能通过已确认恢复上下文继续作者审核。')
    # Validate the exact original read set against its retained source before
    # exposing a separate current source for new author decisions.
    ReviewedChapterCandidateContextBuilder(m['source_root']).verify_binding(chapter_index=draft.chapter,
        source=intent['source'],authority=intent['authority'],phase='prepare')
    step='step_'+digest({'batch_id':batch_id,'chapter':draft.chapter,'stage':'repair_generation' if
        authorization['schema_version']=='reviewed_policy_generation_v2' else 'generation'})[:32]
    generation=RestoredBatchContinuationService._read(context[6],'reviewed/batch_results/'+step+'.json')
    if (generation['candidate_id']!=draft.candidate_id or generation['candidate_intent_hash']!=intent['intent_hash']
            or generation['draft_id']!=draft.id or generation['grant_sha256']!=saved['grant']['grant_sha256']
            or generation['result_sha256']!=digest({k:v for k,v in generation.items() if k!='result_sha256'})):
        raise BatchControlError('恢复作者稿缺少本次原生成结果。')
    trusted=ReviewedChapterCandidateContextBuilder(project.root).build(draft.chapter)
    source=copy.deepcopy(intent['source']);source.update(context_sha256=trusted.context_sha256,
        story_project_read_set=trusted.story_project_context['read_set'],authority_projection_sha256=trusted.authority_projection_sha256)
    return source


def restored_outlines(root,manifest):
    """Only the two declared blueprint coordinates may change physical root."""
    current=_outlines(root,manifest['chapters']);expected=copy.deepcopy(manifest['outlines']);mappings=[]
    source=Path(manifest['source_root'])
    for item in expected:
        relative=item['relative_path'] if 'relative_path' in item else item['path']
        for field in ('outline_path','source_path'):
            before=Path(item['blueprint'][field])
            if not before.is_absolute() or not before.is_relative_to(source) or '..' in before.parts:
                raise BatchControlError('原章纲坐标越过作者批次来源。')
            name=before.relative_to(source)
            if name.as_posix()!=relative:raise BatchControlError('原章纲引用与冻结成员不一致。')
            after=root/name;item['blueprint'][field]=str(after)
            mappings.append(dict(chapter=item['chapter'],field=field,relative_path=name.as_posix(),before=str(before),after=str(after)))
    return current==expected,mappings


class RestoredBatchContinuationService:
    def __init__(self,publication,*,fault_injector=None):
        self.publication=publication;self.fault_injector=fault_injector

    def _scope(self,project_id):
        p=self.publication
        scope=load_restore_scope(p.database.path,machine_store=p.machine_store)
        if scope is None or not scope.active or scope.project_id!=project_id:
            raise BatchControlError('此入口仅用于已真实启用的原目标恢复副本。')
        return scope

    @staticmethod
    def _read(root,name):
        try:return json.loads(read_project_bytes(root,name))
        except (OSError,ValueError,TypeError,SafePathError) as exc:raise BatchControlError('恢复续接所需原记录缺失或不可读。') from exc

    def _original(self,context,batch_id,scope):
        if context[5]!=scope.book_root or context[4]['restore_qualification_sha256']!=scope.activation['qualification_sha256']:
            raise BatchControlError('原恢复批次不属于当前活动写入资格。')
        grant=_original_grant(context,batch_id)
        record,source=_source_origin(context,grant,scope.activation)
        record,state=validate_control(record,grant)
        budget,snapshots=BatchBudgetRegistry(context[6]/'budgets',book_id=grant['manifest']['book_id']).load(batch_id)
        return grant,record,state,project_batch_usage(budget,snapshots),source['handoff'],source

    def _paths(self,context,batch_id,scope):
        token=digest({'batch_id':batch_id,'qualification_sha256':scope.activation['qualification_sha256']})[:24]
        base=assert_safe_local_tree(context[6]/'reviewed/continuations'/token)
        return base,context[2].directory/('brc-'+token+'.json')

    def history(self,*,project_id,batch_id):
        scope=self._scope(project_id)
        with self.publication._owned_project(project_id) as context:
            grant,record,state,usage,_,_=self._original(context,batch_id,scope)
            base,machine=self._paths(context,batch_id,scope)
            return dict(grant=grant,control=dict(control=record,progress=state,execution_permit=False,explicit_resume_required=True),
                usage=usage,task=None,task_state='restored_history',initialized=True,drafts=[],recovery=None,
                history_only=True,continuation_required=True,continuation_preparation_exists=os.path.lexists(machine) or os.path.lexists(base/'intent.json'),
                execution_live=False,execution_permit=False,publication_authorized=False,explicit_resume_required=True)

    def _preview(self,context,batch_id,scope):
        from core.quality.reviewed_disposition import reviewed_quality_action_policy
        from core.quality_decision import resolve_quality_policy
        from core.story_project.reviewed_state_document import capture_sources
        from workbench.reviewed_policy_readback import _next
        grant,record,state,usage,handoff,origin_source=self._original(context,batch_id,scope);m=grant['manifest']
        blockers=[]
        latest=self._read(origin_source['machine'].parent,origin_source['machine'].name)
        if latest!=record:blockers.append('original_control_advanced_or_torn')
        source=origin_source['root']
        if self._read(source/'.novelagent/runtime','reviewed/batches/'+batch_id+'.json')!=grant:
            blockers.append('original_source_grant_changed')
        if self._read(source/'.novelagent/runtime',origin_source['relative'])!=record:
            blockers.append('original_source_control_changed')
        if state['status']!='paused' or state['pending_step'] is not None or state['stage']=='author_review':
            blockers.append('original_step_requires_recovery')
        history=load_verified_reviewed_identity(context[5])['history']
        if history['value']['committed_through']!=state['chapter']-1:blockers.append('formal_position_differs')
        if usage['unresolved_attempts'] or usage['exceeded']:blockers.append('original_spending_not_available')
        clocks=[]
        for chapter in m['chapters']:
            budget=ChapterExecutionBudget(context[5],book_id=m['book_id'],chapter=chapter)
            time=budget.time_clock.report()
            if not time['active_time_complete'] or time['active_limit_reached']:blockers.append('original_time_not_available:'+str(chapter))
            clocks.append(dict(chapter=chapter,clock={k:time[k] for k in ('binding','active_seconds','interval_count','active_status','active_time_complete','active_limit_reached')},
                repair_cycle=budget.store.repair_cycle_claim()))
        outlines_match,outline_mappings=restored_outlines(context[5],m)
        if capture_sources(context[5])!=m['manual_sources'] or not outlines_match:
            blockers.append('frozen_author_inputs_changed')
        if (_selection()!=m['model_selection'] or resolve_quality_policy('standard').to_dict()!=m['quality_policy']
                or reviewed_quality_action_policy()!=m['action_policy']):blockers.append('frozen_model_or_policy_changed')
        if fingerprint_software()['fingerprint_sha256']!=m['software_fingerprint_sha256']:blockers.append('software_changed')
        current_context=_next(context[5],state['chapter']);parent_bridge=None
        if state['chapter']>m['start_chapter']:
            from workbench.reviewed_policy_readback import validate_readback
            step='step_'+digest({'batch_id':batch_id,'chapter':state['chapter']-1,'stage':'readback'})[:32]
            parent=validate_readback(self._read(context[6],'reviewed/batch_results/'+step+'.json'))
            if parent['schema_version']=='reviewed_author_readback_v1':
                completed=any(c['action']=='finish_author_chapter'
                    and c['payload'].get('takeover_sha256')==parent['takeover_sha256']
                    and c['payload'].get('readback_result_sha256')==parent['result_sha256'] for c in record['commands'])
            else:
                completed=any(c['action']=='finish_step' and c['payload'].get('step_id')==step
                    and c['payload'].get('result_sha256')==parent['result_sha256'] for c in record['commands'])
            if not completed or parent['grant_sha256']!=grant['grant_sha256'] or parent['next_context']!=_next(source,state['chapter']):
                blockers.append('original_parent_readback_not_available')
            if parent['next_context'] is None or any(parent['next_context'][k]!=current_context[k] for k in
                    ('chapter','authoritative_state_sha256','source_sha256','committed_through')):
                blockers.append('restored_authoritative_context_differs')
            parent_bridge=dict(readback_sha256=parent['result_sha256'],original_next_context=parent['next_context'])
        value=dict(schema_version='restored_batch_continuation_preview_v1',batch_id=batch_id,
            project_id=m['project_id'],book_id=m['book_id'],grant_sha256=grant['grant_sha256'],
            qualification_sha256=scope.activation['qualification_sha256'],ownership_sha256=context[4]['record_sha256'],
            handoff_intent_sha256=handoff['intent_sha256'],original_control_sha256=record['record_sha256'],
            original_progress=copy.deepcopy(state),history_sha256=history['document_sha256'],
            current_context=current_context,parent_bridge=parent_bridge,outline_coordinate_mappings=outline_mappings,budget_usage=usage,chapter_evidence=clocks,
            can_prepare=not blockers,blockers=blockers,execution_permit=False,explicit_resume_required=True)
        return _signed(value),grant,record

    def preview(self,*,project_id,batch_id):
        scope=self._scope(project_id)
        with self.publication._owned_project(project_id) as context:return self._preview(context,batch_id,scope)[0]

    def prepare(self,*,project_id,batch_id,expected_preview_sha256,author_confirmed):
        if author_confirmed is not True:raise BatchControlError('请明确确认原恢复批次与当前写入位置。')
        scope=self._scope(project_id)
        with self.publication._owned_project(project_id) as context:
            selected=continuation_location(context,batch_id)
            if selected and os.path.lexists(selected[1]/'ready.json'):
                saved=load_continuation(context,batch_id)
                if expected_preview_sha256!=saved['preview']['record_sha256']:raise BatchControlError('原续接确认摘要不同。')
                return dict(preparation=self._read(saved['base'],'ready.json'),execution_permit=False,publication_authorized=False,explicit_resume_required=True)
            preview,grant,record=self._preview(context,batch_id,scope)
            if not preview['can_prepare'] or expected_preview_sha256!=preview['record_sha256']:
                raise BatchControlError('恢复批次来源或预览已变化，请核对原记录后重新确认。')
            base,machine=self._paths(context,batch_id,scope)
            value=_signed(dict(schema_version='restored_batch_continuation_intent_v1',preview=preview,author_confirmed=True))
            from workbench.restore_execution_policy import require_restored_execution_paths
            files={'intent.json':value,'grant.json':grant,'anchor.json':initial_control(grant),'control.json':record,
                'control-origin.json':record,'grant-ready.json':control_ready(grant)}
            require_restored_execution_paths(*(base/name for name in files))
            with persistence_state_lock((machine,base/'intent.json')):
                def create(path,content):
                    if os.path.lexists(path):
                        if self._read(path.parent,path.name)!=content:raise BatchControlError('原恢复续接准备记录不能被覆盖。')
                    else:atomic_create_json(path,content)
                create(machine,value)
                if self.fault_injector is not None:self.fault_injector('after_continuation_machine')
                for name,content in files.items():create(base/name,content)
                if self.fault_injector is not None:self.fault_injector('after_continuation_book')
                create(context[2].directory/('brcc-'+base.name+'.json'),record)
                if self.fault_injector is not None:self.fault_injector('after_continuation_control')
                if context[2].require(**context[3])!=context[4]:raise BatchControlError('恢复续接期间当前所有权变化。')
                receipt=_signed(dict(schema_version='restored_batch_continuation_ready_v1',intent_sha256=value['record_sha256'],
                    original_control_sha256=record['record_sha256'],qualification_sha256=scope.activation['qualification_sha256'],
                    execution_permit=False))
                create(base/'ready.json',receipt)
                if self.fault_injector is not None:self.fault_injector('after_continuation_ready')
            return dict(preparation=receipt,execution_permit=False,publication_authorized=False,explicit_resume_required=True)
