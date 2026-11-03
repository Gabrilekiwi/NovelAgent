"""Exact-intent author review API; this does not install an upgrade endpoint."""
import re
import json

from fastapi import Body
from fastapi.responses import JSONResponse

from workbench.author_baseline_service import AuthorBaselineReviewService
from workbench.author_baselines import AuthorBaselineError


def install_author_baseline_routes(router,app,backups,projects,execute,validate_operation):
    service=AuthorBaselineReviewService(backups)
    app.state.author_baseline_reviews=service
    prefix='/api/projects/{project_id}/onboarding/baselines'
    async def error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':str(exc)}},status_code=409)
    app.add_exception_handler(AuthorBaselineError,error)

    def payload(value,fields):
        if set(value)!=set(fields):raise AuthorBaselineError('基线审核请求字段无效。')
        validate_operation({'operation_id':value.get('operation_id')},('operation_id',))
        # Browser JSON numbers lose the distinction between 1.0 and 1. The
        # original signed report must survive as text until Python parses it.
        if isinstance(value.get('calibration_report'),str):
            raw=value['calibration_report']
            if len(raw.encode('utf-8'))>2*1024**2:raise AuthorBaselineError('原校准报告超过支持大小。')
            try:value['calibration_report']=json.loads(raw)
            except ValueError as exc:raise AuthorBaselineError('原校准报告不是有效的 JSON 文件。') from exc
        for key in ('state','calibration_report'):
            if key in value and not isinstance(value[key],dict):raise AuthorBaselineError('基线内容和校准报告必须是完整记录。')
        for key in ('static_references','confirmed_categories'):
            if key in value and not isinstance(value[key],list):raise AuthorBaselineError('摘录和逐类确认必须是明确的列表。')
        if 'relative_path' in value and not isinstance(value['relative_path'],str):raise AuthorBaselineError('请选择原资料。')
        if 'expected_revision' in value and type(value['expected_revision']) is not int:raise AuthorBaselineError('审核版本无效。')
        for key in ('expected_sha256','expected_preview_sha256'):
            if key in value and (not isinstance(value[key],str) or re.fullmatch('[0-9a-f]{64}',value[key]) is None):
                raise AuthorBaselineError('审核预览或版本摘要无效。')

    @router.get(prefix)
    def listing(project_id:str,after:str=''):
        return service.list(project_id,after=after)

    @router.get(prefix+'/{identifier}')
    def history(project_id:str,identifier:str,revision:int|None=None):
        value=service.history(project_id,identifier)
        if not value['items']:raise AuthorBaselineError('作者基线不存在。')
        records=value['items']
        if revision is not None and not 1<=revision<=len(records):raise AuthorBaselineError('所选审核版本不存在。')
        record=records[-1] if revision is None else records[revision-1]
        return {'record':record,'calibration_report_json':json.dumps(record['calibration_report'],ensure_ascii=False,allow_nan=False,sort_keys=True),
            'head_revision':records[-1]['revision'],'head_sha256':records[-1]['record_sha256'],
            'revisions':[{k:r[k] for k in ('revision','status','record_sha256','created_at')} for r in records],
            'source_checked':False,'publication_authorized':False,'can_upgrade':False}

    @router.post(prefix+'/proposal')
    async def proposal(project_id:str,value:dict=Body(...)):
        payload(value,('operation_id',))
        return await execute(value,lambda permit:service.proposal(project_id,operation_id=value['operation_id'],snapshot_permit=permit))

    @router.post(prefix+'/source')
    async def source(project_id:str,value:dict=Body(...)):
        payload(value,('operation_id','relative_path'))
        return await execute(value,lambda permit:service.source_text(project_id,operation_id=value['operation_id'],
            relative_path=value['relative_path'],snapshot_permit=permit))

    @router.post(prefix+'/preview')
    async def preview(project_id:str,value:dict=Body(...)):
        payload(value,('operation_id','state','calibration_report','static_references'))
        return await execute(value,lambda permit:service.preview(project_id,operation_id=value['operation_id'],
            state=value['state'],calibration_report=value['calibration_report'],static_references=value['static_references'],snapshot_permit=permit))

    @router.post(prefix)
    async def create(project_id:str,value:dict=Body(...)):
        payload(value,('operation_id','state','calibration_report','static_references','expected_preview_sha256'))
        return await execute(value,lambda permit:service.create(project_id,operation_id=value['operation_id'],
            state=value['state'],calibration_report=value['calibration_report'],static_references=value['static_references'],
            expected_preview_sha256=value['expected_preview_sha256'],snapshot_permit=permit))

    @router.post(prefix+'/{identifier}/edit')
    async def edit(project_id:str,identifier:str,value:dict=Body(...)):
        payload(value,('operation_id','state','expected_revision','expected_sha256'))
        return await execute(value,lambda permit:service.revise(project_id,identifier,operation_id=value['operation_id'],
            state=value['state'],expected_revision=value['expected_revision'],expected_sha256=value['expected_sha256'],snapshot_permit=permit))

    @router.post(prefix+'/{identifier}/confirm')
    async def confirm(project_id:str,identifier:str,value:dict=Body(...)):
        payload(value,('operation_id','expected_revision','expected_sha256','confirmed_categories','author_confirmed'))
        if value['author_confirmed'] is not True:raise AuthorBaselineError('请逐类核对后明确确认基线。')
        return await execute(value,lambda permit:service.revise(project_id,identifier,operation_id=value['operation_id'],
            expected_revision=value['expected_revision'],expected_sha256=value['expected_sha256'],
            confirmed_categories=value['confirmed_categories'],author_confirmed=True,snapshot_permit=permit))

    @router.post(prefix+'/{identifier}/inspect')
    async def inspect(project_id:str,identifier:str,value:dict=Body(...)):
        payload(value,('operation_id',))
        return await execute(value,lambda permit:service.inspect(project_id,identifier,
            operation_id=value['operation_id'],snapshot_permit=permit))
