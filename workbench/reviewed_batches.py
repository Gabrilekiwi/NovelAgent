"""Owned author grants for finite reviewed batches; no startup execution.

Only this service derives manifests from the real book, configuration and
policies. The machine copy is an independent original-consent witness; a
caller-supplied hash or copied book grant is never treated as that witness.
Execution/step receipts and policy review integration are added separately.
"""
import copy,json,os,re
from pathlib import Path

from api.budget_endpoint import endpoint_fingerprint
from core.config import get_config
from core.batch_budget_contract import freeze_batch_budget
from core.batch_budget_store import BatchBudgetRegistry
from core.chapter_budget_contract import freeze_chapter_budget,select_pricing
from core.engine.persistence import atomic_create_json
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder,verify_preparation_sources
from core.engine.safe_paths import assert_safe_local_tree,SafePathError
from core.quality.reviewed_disposition import reviewed_quality_action_policy
from core.quality_decision import resolve_quality_policy
from core.story_project.coverage import blueprint_to_dict,validate_generation_blueprint_contract
from core.story_project.mapper import _build_chapter_blueprint
from core.story_project.paths import resolve_outline,infer_next_chapter
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import capture_sources,digest,read_project_bytes
from workbench.backup_version import fingerprint_software
from workbench.chapter_budgets import ChapterBudgetService


class ReviewedBatchError(ValueError):
    code='reviewed_batch_invalid'


_CHAPTER_FIELDS={'max_provider_calls','max_input_tokens','max_output_tokens','max_active_seconds',
    'amount_limit','currency','pricing'}
_BATCH_FIELDS={'amount_limit','max_provider_calls','max_input_tokens','max_output_tokens'}


def _key(book_id,key):
    if not isinstance(key,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,191}',key) is None:
        raise ReviewedBatchError('批次请求编号无效。')
    return 'batch_'+digest({'book_id':book_id,'key':key})[:32]


def _selection():
    """Effective model/config fields only; never persist API keys or URLs."""
    config=get_config();base=config.openai_base_url or 'https://api.openai.com/v1'
    fields=('openai_model','openai_max_output_tokens','openai_timeout_seconds','openai_stream',
        'provider_max_attempts','provider_retry_base_delay_seconds','provider_retry_max_delay_seconds',
        'provider_retry_jitter_ratio','provider_retry_deadline_seconds')
    value={k:getattr(config,k) for k in fields}
    value.update(provider='openai',endpoint_type='official' if base.rstrip('/')=='https://api.openai.com/v1' else 'compatible',
        endpoint_fingerprint=endpoint_fingerprint('openai',base))
    return value


def _outlines(root,chapters):
    result=[];size=0
    for chapter in chapters:
        resolution=resolve_outline(root,chapter)
        if resolution.path is None:raise ReviewedBatchError('批次每章必须有唯一且完整的章纲。')
        relative=resolution.path.relative_to(root).as_posix()
        raw=read_project_bytes(root,relative);size+=len(raw)
        if len(raw)>256*1024 or size>4*1024**2:raise ReviewedBatchError('批次章纲超过支持范围。')
        text=raw.decode('utf-8-sig')
        blueprint=blueprint_to_dict(_build_chapter_blueprint(chapter_index=chapter,outline_path=resolution.path,outline_text=text))
        validate_generation_blueprint_contract(blueprint)
        result.append({'chapter':chapter,'relative_path':relative,'source_sha256':digest(raw),'text':text,'blueprint':blueprint})
    return result


class ReviewedBatchService:
    def __init__(self,publication,*,fault_injector=None):
        self.publication=publication;self.database=publication.database;self.projects=publication.projects
        self.fault_injector=fault_injector

    def _fault(self,event):
        if self.fault_injector is not None:self.fault_injector(event)

    @staticmethod
    def _paths(context,batch_id):
        if not isinstance(batch_id,str) or re.fullmatch(r'batch_[0-9a-f]{32}',batch_id) is None:
            raise ReviewedBatchError('批次编号无效。')
        owner=context[2];runtime=context[6]
        from workbench.restored_batch_continuation import continuation_location
        selected=continuation_location(context,batch_id)
        if selected:
            _,base,_=selected
            return owner.directory/('bg-'+batch_id+'.json'),base/'grant.json',base/'grant-ready.json'
        return (owner.directory/('bg-'+batch_id+'.json'),
            assert_safe_local_tree(runtime/'reviewed/batches')/(batch_id+'.json'),
            owner.directory/('bgr-'+batch_id+'.json'))

    @staticmethod
    def _read(path):
        try:return json.loads(read_project_bytes(path.parent,path.name))
        except (OSError,ValueError,SafePathError) as exc:raise ReviewedBatchError('原批次授权凭据缺失或不可读，不能重新创建空授权。') from exc

    def _manifest(self,context,*,end_chapter,configuration,idempotency_key):
        current,connection,owner,owner_args,ownership,root,runtime,roots,tx_root=context
        self.publication._require_settled_persistence(tx_root)
        verified=load_verified_reviewed_identity(root);history=verified['history']
        start=history['value']['committed_through']+1
        if type(end_chapter) is not int or not start<=end_chapter<start+64 or infer_next_chapter(root)!=start:
            raise ReviewedBatchError('批次只能从当前待写章开始，最多连续 64 章。')
        if (not isinstance(configuration,dict) or set(configuration)!={'chapter_limits','batch_limits'}
                or not isinstance(configuration['chapter_limits'],dict) or set(configuration['chapter_limits'])!=_CHAPTER_FIELDS
                or not isinstance(configuration['batch_limits'],dict) or set(configuration['batch_limits'])!=_BATCH_FIELDS):
            raise ReviewedBatchError('请提供完整的单章和批次预算配置。')
        chapters=list(range(start,end_chapter+1));batch_id=_key(current['book_id'],idempotency_key)
        trusted=ReviewedChapterCandidateContextBuilder(root).build(start)
        static=capture_sources(root);outlines=_outlines(root,chapters);model=_selection()
        budgets=[freeze_chapter_budget(book_id=current['book_id'],chapter=c,**configuration['chapter_limits']) for c in chapters]
        batch=freeze_batch_budget(batch_id=batch_id,chapter_budgets=budgets,**configuration['batch_limits'])
        for budget in budgets:
            # Every initial execution uses the currently configured exact model
            # and endpoint; repair/extraction use the same frozen selection.
            select_pricing(budget,provider=model['provider'],model=model['openai_model'],
                endpoint_type=model['endpoint_type'],endpoint_fingerprint=model['endpoint_fingerprint'])
        result={'schema_version':'reviewed_batch_manifest_v1','batch_id':batch_id,
            'project_id':current['project_id'],'book_id':current['book_id'],'source_root':str(root),
            'start_chapter':start,'end_chapter':end_chapter,'chapters':chapters,
            'ownership_sha256':ownership['record_sha256'],
            'baseline_document_sha256':verified['identity']['reviewed_state']['baseline_document_sha256'],
            'initial_history_sha256':history['document_sha256'],'initial_context_sha256':trusted.context_sha256,
            'initial_read_set':copy.deepcopy(trusted.story_project_context['read_set']),
            'manual_sources':static,'outlines':outlines,'model_selection':model,
            'quality_policy':resolve_quality_policy('standard').to_dict(),
            'action_policy':reviewed_quality_action_policy(),'budget':batch,
            'software_fingerprint_sha256':fingerprint_software()['fingerprint_sha256'],
            'review_source':'policy','maximum_repair_cycles_per_chapter':1,
            'restart_behavior':'explicit_resume_required','author_draft_policy':'preserve_original',
            'formal_publication_requires':'passing_full_reextract_recheck_then_transaction_readback'}
        result['manifest_sha256']=digest(result)
        verify_preparation_sources(trusted.story_project_context,phase='prepare')
        if (owner.require(**owner_args)!=ownership or capture_sources(root)!=static
                or _outlines(root,chapters)!=outlines or _selection()!=model):
            raise ReviewedBatchError('批次准备期间作品、模型或章纲变化，请重新预览。')
        return result

    def preview(self,*,project_id,end_chapter,configuration,idempotency_key):
        with self.publication._owned_project(project_id) as context:
            manifest=self._manifest(context,end_chapter=end_chapter,configuration=configuration,idempotency_key=idempotency_key)
            return {'manifest':manifest,'author_confirmation_required':True,'provider_calls_started':False}

    def authorize(self,*,project_id,end_chapter,configuration,idempotency_key,expected_manifest_sha256,author_confirmed):
        if author_confirmed is not True:raise ReviewedBatchError('请明确确认本次有限批次的章纲、模型、策略和费用上限。')
        with self.publication._owned_project(project_id) as context:
            manifest=self._manifest(context,end_chapter=end_chapter,configuration=configuration,idempotency_key=idempotency_key)
            if manifest['manifest_sha256']!=expected_manifest_sha256:raise ReviewedBatchError('批次输入已变化，请重新预览后确认。')
            machine,book,ready=self._paths(context,manifest['batch_id'])
            from workbench.restore_execution_policy import require_restored_execution_paths
            require_restored_execution_paths(book)
            grant={'schema_version':'reviewed_batch_author_grant_v1','manifest':manifest,
                'grant_source':'author','author_confirmed':True,'automatic_review_source':'policy'}
            grant['grant_sha256']=digest(grant)
            # A completed grant can only be read back, never reconstructed.
            # Missing book/machine evidence after ready is loss, not an
            # unfinished first publication that an ordinary retry may repair.
            if os.path.lexists(ready):
                result=self._stored(context,manifest['batch_id'])
                if result['grant']!=grant:raise ReviewedBatchError('原批次授权与本次确认不同。')
                return result
            # Recover only this exact author's original intent. A book-only
            # copied grant cannot manufacture its missing machine consent.
            if os.path.lexists(machine):
                if self._read(machine)!=grant:raise ReviewedBatchError('同一批次请求已经绑定其他授权。')
            elif os.path.lexists(book) or os.path.lexists(ready):
                raise ReviewedBatchError('原机器授权缺失，不能从作品副本重建。')
            else:
                for chapter in manifest['chapters']:
                    ChapterBudgetService._require_no_chapter_work(context[5],manifest['book_id'],chapter)
                machine.parent.mkdir(parents=True,exist_ok=True)
                atomic_create_json(machine,grant)
                self._fault('after_machine_grant')
            def preflight():
                now=self._manifest(context,end_chapter=end_chapter,configuration=configuration,idempotency_key=idempotency_key)
                if now!=manifest or self._read(machine)!=grant:raise ReviewedBatchError('原批次授权或输入已经变化。')
                for chapter in manifest['chapters']:
                    ChapterBudgetService._require_no_chapter_work(context[5],manifest['book_id'],chapter)
            registry=BatchBudgetRegistry(context[6]/'budgets',book_id=manifest['book_id'])
            registry.freeze(manifest['budget'],before_write=preflight)
            self._fault('after_batch_budget')
            preflight()
            if os.path.lexists(book):
                if self._read(book)!=grant:raise ReviewedBatchError('作品中的原授权记录不同。')
            else:atomic_create_json(book,grant)
            self._fault('after_book_grant')
            receipt={'schema_version':'reviewed_batch_grant_ready_v1','batch_id':manifest['batch_id'],
                'grant_sha256':grant['grant_sha256'],'ownership_sha256':manifest['ownership_sha256']}
            receipt['receipt_sha256']=digest(receipt)
            if os.path.lexists(ready):
                if self._read(ready)!=receipt:raise ReviewedBatchError('原批次授权完成凭据不同。')
            else:atomic_create_json(ready,receipt)
            self._fault('after_grant_ready')
            return self._stored(context,manifest['batch_id'])

    def _stored(self,context,batch_id):
        from workbench.restored_batch_continuation import load_continuation
        restored=load_continuation(context,batch_id)
        if restored:
            from workbench.reviewed_batch_controls import validate_control_anchor
            validate_control_anchor(context,batch_id,restored['grant'])
            return dict(grant=restored['grant'],state='restored_continuation_paused',explicit_resume_required=True,
                provider_calls_started=False,execution_permit=False)
        machine,book,ready=self._paths(context,batch_id)
        grant=self._read(machine);manifest=grant.get('manifest') if isinstance(grant,dict) else None
        if (not isinstance(manifest,dict) or set(grant)!={'schema_version','manifest','grant_source','author_confirmed','automatic_review_source','grant_sha256'}
                or grant['schema_version']!='reviewed_batch_author_grant_v1' or grant['grant_source']!='author'
                or grant['author_confirmed'] is not True or grant['automatic_review_source']!='policy'
                or grant['grant_sha256']!=digest({k:v for k,v in grant.items() if k!='grant_sha256'})
                or manifest['manifest_sha256']!=digest({k:v for k,v in manifest.items() if k!='manifest_sha256'})
                or manifest['batch_id']!=batch_id or manifest['project_id']!=context[0]['project_id']
                or manifest['book_id']!=context[0]['book_id'] or manifest['source_root']!=str(context[5])
                or manifest['ownership_sha256']!=context[4]['record_sha256'] or self._read(book)!=grant):
            raise ReviewedBatchError('原作者批次授权、作品身份或所有权绑定不同。')
        expected={'schema_version':'reviewed_batch_grant_ready_v1','batch_id':batch_id,
            'grant_sha256':grant['grant_sha256'],'ownership_sha256':manifest['ownership_sha256']}
        expected['receipt_sha256']=digest(expected)
        actual_ready=self._read(ready)
        if actual_ready!=expected:
            from workbench.reviewed_batch_controls import control_ready,validate_control_anchor
            if actual_ready!=control_ready(grant):raise ReviewedBatchError('批次授权尚未完整保存，请显式续办原请求。')
            validate_control_anchor(context,batch_id,grant)
        batch,_=BatchBudgetRegistry(context[6]/'budgets',book_id=manifest['book_id']).load(batch_id)
        if batch!=manifest['budget']:raise ReviewedBatchError('原批次授权与预算不同。')
        return {'grant':grant,'state':'authorized_paused','explicit_resume_required':True,
            'provider_calls_started':False,'execution_permit':False}

    def get(self,*,project_id,batch_id):
        """No reconciliation, authorization creation or Provider calls on read."""
        with self.publication._owned_project(project_id) as context:return self._stored(context,batch_id)

    def require_current_start(self,*,project_id,batch_id):
        """Validate a stored initial grant for a future explicit start boundary.

        This is not a resume token and does not start work. Later chapter
        execution must additionally prove its preceding formal commit/readback.
        """
        with self.publication._owned_project(project_id) as context:
            return self._require_current_start(context,batch_id)

    def _require_current_start(self,context,batch_id):
        result=self._stored(context,batch_id);manifest=result['grant']['manifest']
        from workbench.restore_execution_policy import require_restored_execution_paths
        require_restored_execution_paths(self._paths(context,batch_id)[1])
        trusted=ReviewedChapterCandidateContextBuilder(context[5]).build(manifest['start_chapter'])
        if (trusted.context_sha256!=manifest['initial_context_sha256']
                or capture_sources(context[5])!=manifest['manual_sources']
                or _outlines(context[5],manifest['chapters'])!=manifest['outlines']
                or _selection()!=manifest['model_selection']
                or resolve_quality_policy('standard').to_dict()!=manifest['quality_policy']
                or reviewed_quality_action_policy()!=manifest['action_policy']
                or fingerprint_software()['fingerprint_sha256']!=manifest['software_fingerprint_sha256']):
            raise ReviewedBatchError('原批次输入、模型或策略变化，当前不能开始。')
        return result

    def _require_frozen(self,context,batch_id):
        """Current frozen author inputs, independent of an already committed tip."""
        result=self._stored(context,batch_id);m=result['grant']['manifest']
        from workbench.restored_batch_continuation import continuation_location,restored_outlines
        moved=continuation_location(context,batch_id) is not None
        from workbench.restore_execution_policy import require_restored_execution_paths
        require_restored_execution_paths(self._paths(context,batch_id)[1])
        if (capture_sources(context[5])!=m['manual_sources'] or
                (not restored_outlines(context[5],m)[0] if moved else _outlines(context[5],m['chapters'])!=m['outlines'])
                or _selection()!=m['model_selection'] or resolve_quality_policy('standard').to_dict()!=m['quality_policy']
                or reviewed_quality_action_policy()!=m['action_policy']
                or fingerprint_software()['fingerprint_sha256']!=m['software_fingerprint_sha256']):
            raise ReviewedBatchError('原批次冻结来源、模型、策略或软件发生变化。')
        return result

    def _require_current_execution(self,context,batch_id):
        result=self._stored(context,batch_id)
        from workbench.reviewed_batch_controls import control_paths
        from core.engine.reviewed_batch_control import BatchControlStore,validate_control
        _,machine,book=control_paths(context,batch_id)
        # Exact original command reconciliation may observe a torn mirror.
        # Actual execution/store.apply still requires both copies to agree.
        _,state=validate_control(BatchControlStore._read(machine),result['grant'])
        if state['stage']=='author_review' or state['status']=='completed':
            self._require_frozen(context,batch_id)
            return result
        if state['stage'] in {'publication','readback'}:
            from workbench.reviewed_policy_publication import published_original
            published=published_original(self.publication,context,result['grant'],state['chapter'])
            if published is not None:
                self._require_frozen(context,batch_id)
                return result
        if state['chapter']!=result['grant']['manifest']['start_chapter']:
            self._require_frozen(context,batch_id)
            from workbench.reviewed_policy_readback import require_readback_parent
            require_readback_parent(self.publication,context,result['grant'],state['chapter'])
            return result
        return self._require_current_start(context,batch_id)
