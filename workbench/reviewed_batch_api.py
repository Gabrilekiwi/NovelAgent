"""Reviewed finite batches: explicit commands, live tasks and read-only progress."""
import asyncio,os,re

from fastapi import Body,Header,HTTPException
from fastapi.responses import JSONResponse

from core.engine.reviewed_batch_control import BatchControlError
from core.engine.safe_paths import assert_safe_local_tree
from core.batch_budget_store import BatchBudgetRegistry
from core.batch_budget_contract import project_batch_usage
from core.chapter_execution_budget import ChapterExecutionBudget
from core.story_project.reviewed_state_document import digest
from workbench.chapter_budgets import ChapterBudgetService
from workbench.reviewed_batches import ReviewedBatchError
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_ready
from workbench.reviewed_batch_driver import ReviewedBatchDriver
from workbench.reviewed_batch_tasks import ReviewedBatchTaskService
from workbench.reviewed_batch_takeovers import ReviewedBatchTakeoverService,takeover_path
from workbench.reviewed_policy_repairs import ReviewedPolicyRepairService
from workbench.reviewed_stage_recovery import ReviewedBatchStageRecoveryService


def _fields(payload,fields):
    if set(payload)!=set(fields):raise HTTPException(422,'本次参数不完整或包含不支持的字段。')


class ReviewedBatchWorkbenchService:
    def __init__(self,publication,*,instance_lease,maintenance,generation_executor_factory=None,extraction_executor_factory=None):
        from workbench.restored_batch_continuation import RestoredBatchContinuationService
        self.restored=RestoredBatchContinuationService(publication)
        self.controls=ReviewedBatchControlService(publication)
        self.recovery=ReviewedBatchStageRecoveryService(self.controls)
        self.takeovers=ReviewedBatchTakeoverService(self.controls)
        self.tasks=ReviewedBatchTaskService(self.controls,instance_lease=instance_lease,maintenance=maintenance,
            driver_factory=lambda controls:ReviewedBatchDriver(controls,generation_executor_factory=generation_executor_factory,
                extraction_executor_factory=extraction_executor_factory))

    def _live(self,project_id,batch_id):
        with self.tasks._mutex:
            active=self.tasks._active
            return bool(active and active['project_id']==project_id and active['batch_id']==batch_id)

    def list(self,project_id):
        from workbench.restored_batch_continuation import continuation_location
        c=self.controls;context=c._control_context(project_id)
        folder=assert_safe_local_tree(context[6]/'reviewed/batches')
        paths=list(folder.iterdir()) if folder.exists() else []
        if len(paths)>256:raise BatchControlError('批次记录超过当前列表范围，请保留记录后处理。')
        items=[]
        for path in sorted(paths,key=lambda p:p.name):
            if re.fullmatch('batch_[0-9a-f]{32}\\.json',path.name) is None:
                raise BatchControlError('原批次目录有无法识别的记录。')
            # A restored book can also receive new grants at its current
            # root. Only a moved imported grant uses the historical chain.
            imported=continuation_location(context,path.stem) is not None
            grant=(self.restored.history(project_id=project_id,batch_id=path.stem)['grant'] if imported
                else c.batches._stored(context,path.stem)['grant']);m=grant['manifest']
            items.append(dict(batch_id=m['batch_id'],start_chapter=m['start_chapter'],end_chapter=m['end_chapter']))
        return dict(items=items,monetary_option=ChapterBudgetService.monetary_option(),execution_permit=False)

    def status(self,project_id,batch_id):
        scope=self.controls.publication.database.restore_scope
        if scope is not None and scope.active:
            context=self.controls._control_context(project_id)
            from workbench.restored_batch_continuation import continuation_location,load_continuation
            selected=continuation_location(context,batch_id)
            if selected and not os.path.lexists(selected[1]/'ready.json'):
                return self.restored.history(project_id=project_id,batch_id=batch_id)
            if selected:load_continuation(context,batch_id)
        c=self.controls;context=c._control_context(project_id);grant=c.batches._stored(context,batch_id)['grant']
        _,_,ready=c.batches._paths(context,batch_id)
        if c.batches._read(ready)!=control_ready(grant):
            return dict(grant=grant,initialized=False,control=None,task=None,task_state='not_started',
                execution_live=False,explicit_resume_required=True,execution_permit=False,recovery=None,drafts=[])
        try:result=self.tasks.get(project_id=project_id,batch_id=batch_id);result['recovery']=None
        except BatchControlError:
            recovery=self.recovery.get(project_id=project_id,batch_id=batch_id)
            if not recovery['control_reconciliation_required']:raise
            live=self._live(project_id,batch_id)
            result=dict(control={k:recovery[k] for k in ('control','progress','execution_permit','explicit_resume_required')},
                task=None,task_state='recovery_required',execution_live=live,explicit_resume_required=not live,
                execution_permit=False,recovery=recovery['original_recovery'])
        budget,snapshots=BatchBudgetRegistry(context[6]/'budgets',book_id=grant['manifest']['book_id']).load(batch_id)
        result.update(grant=grant,initialized=True,drafts=[],usage=project_batch_usage(budget,snapshots),quality=None,chapter_time=None,
            history_only=False,continuation_required=False)
        # Draft selection is based on actual saved generation receipts, never a
        # client-supplied candidate/quality success flag.
        state=result['control']['progress'];chapter=result['recovery']['chapter'] if result['recovery'] else state['chapter']
        if chapter in grant['manifest']['chapters'] and not result['execution_live']:
            result['chapter_time']=ChapterExecutionBudget(context[5],book_id=grant['manifest']['book_id'],chapter=chapter).time_clock.report()
            for stage in ('generation','repair_generation'):
                step='step_'+digest(dict(batch_id=batch_id,chapter=chapter,stage=stage))[:32]
                path=context[6]/'reviewed/batch_results'/(step+'.json')
                if not os.path.lexists(path):continue
                saved=self.recovery._service(stage).get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']
                draft=c.publication.preparations.drafts.get(saved['draft_id'])
                if draft.project_id!=project_id or draft.chapter!=chapter or draft.candidate_id!=saved['candidate_id']:
                    raise BatchControlError('原批次稿件与真实生成记录不同。')
                result['drafts'].append(dict(stage=stage,draft_id=draft.id,candidate_id=draft.candidate_id,
                    revision=draft.revision,chapter=chapter))
            for stage in ('quality','repair_quality'):
                step='step_'+digest(dict(batch_id=batch_id,chapter=chapter,stage=stage))[:32]
                if os.path.lexists(context[6]/'reviewed/batch_results'/(step+'.json')):
                    result['quality']=self.recovery._service(stage).get(project_id=project_id,batch_id=batch_id,chapter=chapter)['result']['review']['quality_disposition']
            path=takeover_path(context,batch_id,chapter)
            if os.path.lexists(path):
                takeover=self.takeovers.get(project_id=project_id,batch_id=batch_id,chapter=chapter)
                result['takeover']=takeover
                draft_id=takeover['takeover']['draft_at_takeover']['id']
                result['author_reviews']=[dict(id=r['id'],state=r['state'],revision=r['revision'])
                    for r in c.publication.preparations.fact_reviews.list_for_draft(draft_id)]
        return result

    def recover_command(self,project_id,batch_id,*,action,idempotency_key,expected_control_sha256,author_confirmed):
        if author_confirmed is not True or action not in {'start','resume','pause','stop'}:
            raise BatchControlError('请明确续办原控制命令。')
        with self.tasks._mutex:
            if self.tasks._active is not None:raise BatchControlError('请等待原后台执行结束后恢复控制记录。')
            observed=self.recovery.get(project_id=project_id,batch_id=batch_id);original=observed['original_recovery']
            if (original is None or original['action']!=action or original['expected_control_sha256']!=expected_control_sha256
                    or not isinstance(idempotency_key,str)
                    or observed['control']['commands'][-1]['key_sha256']!=digest(idempotency_key.encode('utf-8'))):
                raise BatchControlError('请使用中断前保存的同一原请求恢复，不能创建新命令。')
            value=self.controls.command(project_id=project_id,batch_id=batch_id,action=action,idempotency_key=idempotency_key,
                expected_control_sha256=expected_control_sha256,author_confirmed=True)
            if isinstance(value,tuple):
                _,handle=value
                if handle is not None and self.controls.handles.get(batch_id) is handle:self.controls.handles.pop(batch_id,None)
            return self.status(project_id,batch_id)


def install_reviewed_batch_routes(app,publication,*,instance_lease,maintenance,generation_executor_factory=None,extraction_executor_factory=None):
    service=ReviewedBatchWorkbenchService(publication,instance_lease=instance_lease,maintenance=maintenance,
        generation_executor_factory=generation_executor_factory,extraction_executor_factory=extraction_executor_factory)
    app.state.reviewed_batches=service

    async def error(request,exc):
        return JSONResponse({'error':{'code':getattr(exc,'code','reviewed_batch_invalid'),'message':str(exc)}},status_code=409)
    for kind in (ReviewedBatchError,BatchControlError):app.add_exception_handler(kind,error)

    @app.get('/api/projects/{project_id}/reviewed-batches')
    async def listing(project_id:str):return await asyncio.to_thread(service.list,project_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/preview')
    async def preview(project_id:str,payload:dict=Body(...),key:str=Header(...,alias='Idempotency-Key')):
        _fields(payload,{'end_chapter','configuration'})
        return await asyncio.to_thread(service.controls.batches.preview,project_id=project_id,idempotency_key=key,**payload)

    @app.post('/api/projects/{project_id}/reviewed-batches')
    async def authorize(project_id:str,payload:dict=Body(...),key:str=Header(...,alias='Idempotency-Key')):
        _fields(payload,{'end_chapter','configuration','expected_manifest_sha256','author_confirmed'})
        saved=await asyncio.to_thread(service.controls.batches.authorize,project_id=project_id,idempotency_key=key,**payload)
        batch_id=saved['grant']['manifest']['batch_id']
        await asyncio.to_thread(service.controls.initialize,project_id=project_id,batch_id=batch_id,author_confirmed=payload['author_confirmed'])
        return await asyncio.to_thread(service.status,project_id,batch_id)

    @app.get('/api/projects/{project_id}/reviewed-batches/{batch_id}')
    async def status(project_id:str,batch_id:str):return await asyncio.to_thread(service.status,project_id,batch_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/initialize')
    async def initialize(project_id:str,batch_id:str,payload:dict=Body(...)):
        _fields(payload,{'author_confirmed'})
        await asyncio.to_thread(service.controls.initialize,project_id=project_id,batch_id=batch_id,**payload)
        return await asyncio.to_thread(service.status,project_id,batch_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/restored-continuation/preview')
    async def restored_preview(project_id:str,batch_id:str,payload:dict=Body(...)):
        _fields(payload,set())
        return await asyncio.to_thread(service.restored.preview,project_id=project_id,batch_id=batch_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/restored-continuation')
    async def restored_prepare(project_id:str,batch_id:str,payload:dict=Body(...)):
        _fields(payload,{'expected_preview_sha256','author_confirmed'})
        return await asyncio.to_thread(service.restored.prepare,project_id=project_id,batch_id=batch_id,**payload)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/commands')
    async def command(project_id:str,batch_id:str,payload:dict=Body(...),key:str=Header(...,alias='Idempotency-Key')):
        _fields(payload,{'action','expected_control_sha256','author_confirmed'})
        action=payload['action']
        if action in {'start','resume'}:
            return await asyncio.to_thread(service.tasks.submit,project_id=project_id,batch_id=batch_id,idempotency_key=key,**payload)
        if action not in {'pause','stop'}:raise HTTPException(422,'不支持的批次动作。')
        await asyncio.to_thread(service.controls.command,project_id=project_id,batch_id=batch_id,idempotency_key=key,**payload)
        return await asyncio.to_thread(service.status,project_id,batch_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/recover-command')
    async def recover_command(project_id:str,batch_id:str,payload:dict=Body(...),key:str=Header(...,alias='Idempotency-Key')):
        _fields(payload,{'action','expected_control_sha256','author_confirmed'})
        return await asyncio.to_thread(service.recover_command,project_id,batch_id,idempotency_key=key,**payload)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/recover-stage')
    async def recover_stage(project_id:str,batch_id:str,payload:dict=Body(...)):
        _fields(payload,{'action','chapter','stage','author_confirmed'})
        if payload['author_confirmed'] is not True or service._live(project_id,batch_id):
            raise BatchControlError('请等待后台执行结束并明确恢复原步骤。')
        observed=await asyncio.to_thread(service.recovery.get,project_id=project_id,batch_id=batch_id)
        original=observed['original_recovery']
        if original is None or any(payload[k]!=original[k] or type(payload[k]) is not type(original[k]) for k in ('action','chapter','stage')):
            raise BatchControlError('待恢复步骤已变化，请重新读取原记录。')
        action=payload['action']
        if action=='begin_repair':
            operation=ReviewedPolicyRepairService(service.controls).recover_begin
            args=dict(project_id=project_id,batch_id=batch_id,chapter=payload['chapter'])
        elif action=='author_takeover':
            saved=await asyncio.to_thread(service.takeovers.get,project_id=project_id,batch_id=batch_id,chapter=payload['chapter'])
            operation=service.takeovers.takeover
            args=dict(project_id=project_id,batch_id=batch_id,draft_id=saved['takeover']['draft_at_takeover']['id'],
                expected_control_sha256=original['expected_control_sha256'],author_confirmed=True)
        elif action in {'claim_step','finish_step'}:
            operation=service.recovery.recover_claim if action=='claim_step' else service.recovery.recover_finish
            args=dict(project_id=project_id,batch_id=batch_id,chapter=payload['chapter'],stage=payload['stage'])
        else:raise BatchControlError('请使用原控制请求或作者接管入口完成该恢复。')
        await asyncio.to_thread(operation,**args)
        return await asyncio.to_thread(service.status,project_id,batch_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/takeover')
    async def takeover(project_id:str,batch_id:str,payload:dict=Body(...)):
        _fields(payload,{'draft_id','expected_control_sha256','author_confirmed'})
        if service._live(project_id,batch_id):raise BatchControlError('请先暂停并等待原执行退出，再接管稿件。')
        await asyncio.to_thread(service.takeovers.takeover,project_id=project_id,batch_id=batch_id,**payload)
        return await asyncio.to_thread(service.status,project_id,batch_id)

    @app.post('/api/projects/{project_id}/reviewed-batches/{batch_id}/complete-author')
    async def complete_author(project_id:str,batch_id:str,payload:dict=Body(...)):
        _fields(payload,{'chapter','request','expected_control_sha256','author_confirmed'})
        request=payload['request']
        if not isinstance(request,dict) or set(request)!={'key','body'} or not isinstance(request['body'],dict):
            raise HTTPException(422,'请提供原发布页面保存的请求。')
        _fields(request['body'],{'review_id','expected_draft_revision','expected_review_revision','expected_draft_sha256','expected_facts_sha256'})
        route=dict(project_id=project_id,chapter=payload['chapter'],idempotency_key=request['key'],**request['body'])
        await asyncio.to_thread(service.takeovers.complete,project_id=project_id,batch_id=batch_id,route=route,
            expected_control_sha256=payload['expected_control_sha256'],author_confirmed=payload['author_confirmed'])
        return await asyncio.to_thread(service.status,project_id,batch_id)
    return service
