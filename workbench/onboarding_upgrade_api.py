"""Explicit certified upgrade controls using the existing capture lifetime."""
from fastapi import Body
from fastapi.responses import JSONResponse

from core.engine.persistence_v2 import PersistenceV2Error
from core.engine.root_registry import RootRegistryError
from workbench.book_ownership import BookOwnershipError
from workbench.onboarding_upgrade import OnboardingUpgradeService,OnboardingUpgradeError


def install_upgrade_routes(router,app,backups,execute,validate_operation):
    service=OnboardingUpgradeService(backups)
    app.state.onboarding_upgrade=service
    prefix='/api/projects/{project_id}/onboarding/upgrade'
    request_fields=('operation_id','archive_id','baseline_id','expected_revision','expected_sha256')

    async def error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':str(exc)}},status_code=409)
    app.add_exception_handler(OnboardingUpgradeError,error)

    def payload(value,fields):
        if set(value)!=set(fields):raise OnboardingUpgradeError('升级请求字段无效，请重新核对原操作。')
        validate_operation({'operation_id':value.get('operation_id')},('operation_id',))
        if 'expected_revision' in value and type(value['expected_revision']) is not int:
            raise OnboardingUpgradeError('作者确认修订无效。')
        if 'confirmed' in value and value['confirmed'] is not True:
            raise OnboardingUpgradeError('请明确确认升级差异和恢复前提。')

    def checked(operation):
        try:return operation()
        except OnboardingUpgradeError:raise
        except (OSError,ValueError,BookOwnershipError,PersistenceV2Error,RootRegistryError) as exc:
            raise OnboardingUpgradeError('原升级依据或事务证明无法核验，请保留现场并重新核对。') from exc

    @router.get(prefix+'/operations/{preview_sha256}')
    def operation(project_id:str,preview_sha256:str):
        return checked(lambda:service.operation(project_id,preview_sha256=preview_sha256))

    @router.post(prefix+'/preview')
    async def preview(project_id:str,value:dict=Body(...)):
        payload(value,request_fields)
        return await execute(value,lambda permit:checked(lambda:service.preview(project_id,
            **value,snapshot_permit=permit)))

    @router.post(prefix+'/commit')
    async def commit(project_id:str,value:dict=Body(...)):
        payload(value,(*request_fields,'expected_preview_sha256','acknowledged_policy_sha256','confirmed'))
        return await execute(value,lambda permit:checked(lambda:service.commit(project_id,
            **value,snapshot_permit=permit)))

    @router.post(prefix+'/continue')
    async def continue_original(project_id:str,value:dict=Body(...)):
        payload(value,('operation_id','preview_sha256','acknowledged_policy_sha256','confirmed'))
        return await execute(value,lambda permit:checked(lambda:service.continue_operation(project_id,
            **value,snapshot_permit=permit)))
