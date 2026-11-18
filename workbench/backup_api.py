"""Local maintenance-only archive endpoints with owned worker lifetimes."""
import re

from fastapi import APIRouter,Body
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from core.engine.persistence import PersistenceLockError
from core.engine.safe_paths import assert_safe_local_tree
from workbench.backup_archive import BackupArchiveService,BackupArchiveError,verify_archive
from workbench.backup_database import BackupDatabaseError
from workbench.backup_files import BackupFilesError,_ordinary
from workbench.backup_scope import BackupScopeError
from workbench.jobs import CURRENT_JOB_EXECUTION_CONTRACT
from workbench.maintenance import MaintenanceError
from workbench.onboarding import OnboardingPreviewService
from workbench.legacy_registration import LegacyRegistration
from workbench.backup_certification_api import install_certification_routes
from workbench.author_baseline_api import install_author_baseline_routes
from workbench.onboarding_upgrade_api import install_upgrade_routes
from workbench.legacy_memory_revision_api import install_legacy_memory_revision_routes


class BackupControlRoute(APIRoute):
    """Endpoints explicitly acquire snapshot admission, including read checks."""


def _payload(value,fields):
    if set(value)!=set(fields) or any(not isinstance(value[k],str) for k in fields):
        raise BackupArchiveError('归档请求字段无效。')
    if re.fullmatch('[0-9a-f]{32}',value['operation_id']) is None:
        raise BackupArchiveError('必须提供当前维护操作标识。')


def _preview_summary(value):
    scope=value['scope']
    return {**{k:value[k] for k in ('project_id','book_id','operation_id','preview_sha256',
            'archive_purpose','recoverable','certification_pending','archive_root','runtime_config')},
        'scope_checks_passed':scope['scope_checks_passed'],'blockers':scope['blockers'],
        'total_source_bytes':scope['total_source_bytes'],'database_bytes':scope['database_bytes'],
        'roots':[{'name':key,'source_root':tree['source_root'],'files':len(tree['files']),
            'directories':len(tree['directories']),'total_bytes':tree['total_bytes'],
            'excluded':tree['excluded']} for key,tree in scope['inventories'].items()],
        'software_fingerprint_sha256':value['software']['fingerprint_sha256'],
        'loaded_modules_match_proven':value['software']['loaded_modules_match_proven']}


def install_backup_api(app,*,projects,maintenance,task_runtime_root,worker_enabled,auto_import_active,machine_store=None):
    service=BackupArchiveService(projects=projects,maintenance=maintenance,
        task_runtime_root=task_runtime_root,archive_root=maintenance.lease.directory/'backups',
        runtime_config={'workbench_version':app.version,'job_execution_contract':CURRENT_JOB_EXECUTION_CONTRACT,
            'worker_enabled':bool(worker_enabled),'auto_import_active':bool(auto_import_active)},
        machine_store=machine_store)
    app.state.backups=service
    onboarding=OnboardingPreviewService(service)
    app.state.onboarding=onboarding
    registration=LegacyRegistration(service)
    app.state.legacy_registration=registration
    router=APIRouter(route_class=BackupControlRoute)

    async def archive_error(request,exc):
        return JSONResponse({'error':{'code':getattr(exc,'code','backup_source_busy'),
            'message':str(exc)}},status_code=423 if isinstance(exc,PersistenceLockError) else 409)
    for kind in (BackupArchiveError,BackupDatabaseError,BackupFilesError,BackupScopeError,PersistenceLockError):
        app.add_exception_handler(kind,archive_error)

    async def execute(payload,operation):
        status=maintenance.status()
        if status['purpose']!='backup':raise MaintenanceError('此归档操作需要备份维护状态。')
        return await maintenance.run_capture_async(payload['operation_id'],operation)

    @router.post('/api/projects/{project_id}/backups/preview')
    async def preview(project_id:str,payload:dict=Body(...)):
        _payload(payload,('operation_id',))
        return await execute(payload,lambda permit:_preview_summary(service.preview(project_id,
            operation_id=payload['operation_id'],snapshot_permit=permit)))

    @router.post('/api/projects/{project_id}/onboarding/preview')
    async def onboarding_preview(project_id:str,payload:dict=Body(...)):
        _payload(payload,('operation_id','archive_id'))
        return await execute(payload,lambda permit:onboarding.preview(project_id,
            operation_id=payload['operation_id'],archive_id=payload['archive_id'],snapshot_permit=permit))

    @router.post('/api/projects/{project_id}/onboarding/registration/preview')
    async def registration_preview(project_id:str,payload:dict=Body(...)):
        _payload(payload,('operation_id','archive_id'))
        return await execute(payload,lambda permit:registration.preview(project_id,
            operation_id=payload['operation_id'],archive_id=payload['archive_id'],snapshot_permit=permit))

    @router.post('/api/projects/{project_id}/onboarding/registration')
    async def register_legacy(project_id:str,payload:dict=Body(...)):
        if payload.get('confirmed') is not True:
            raise BackupArchiveError('请明确确认当前旧书的首次登记或原意图续接。')
        _payload({k:v for k,v in payload.items() if k!='confirmed'},
            ('operation_id','archive_id','expected_preview_sha256'))
        return await execute(payload,lambda permit:registration.register(project_id,
            operation_id=payload['operation_id'],archive_id=payload['archive_id'],
            expected_preview_sha256=payload['expected_preview_sha256'],confirmed=True,snapshot_permit=permit))

    @router.post('/api/projects/{project_id}/backups')
    async def capture(project_id:str,payload:dict=Body(...)):
        _payload(payload,('operation_id','expected_preview_sha256','archive_purpose'))
        if payload['archive_purpose']!='diagnostic':
            raise BackupArchiveError('当前仅提供诊断归档，可恢复认证尚未完成。')
        return await execute(payload,lambda permit:service.capture(project_id,
            operation_id=payload['operation_id'],expected_preview_sha256=payload['expected_preview_sha256'],
            snapshot_permit=permit))

    @router.post('/api/backups/{archive_id}/verify')
    async def verify(archive_id:str,payload:dict=Body(...)):
        _payload(payload,('operation_id',))
        if re.fullmatch('backup_[0-9a-f]{32}',archive_id) is None:
            raise BackupArchiveError('归档标识无效。')
        def work(permit):
            permit.require()
            verified=verify_archive(service.archive_root/archive_id)
            scope=projects.database.restore_scope
            if scope is not None:
                preview=verified['manifest']['preview']
                if preview['project_id']!=scope.project_id or preview['book_id']!=scope.document['book_id']:
                    raise BackupArchiveError('归档不属于当前恢复作品。')
            return {k:v for k,v in verified.items() if k!='manifest'}
        return await execute(payload,work)

    @router.get('/api/backups')
    async def list_archives():
        # Discovery only: a completion filename is not proof that a ZIP is valid.
        root=assert_safe_local_tree(service.archive_root)
        if not root.exists():return {'archives':[]}
        entries=[]
        for item in sorted(root.iterdir(),key=lambda p:p.name):
            if re.fullmatch('backup_[0-9a-f]{32}',item.name) is None:continue
            _ordinary(item)
            if item.is_dir():entries.append({'archive_id':item.name,'verification':'required'})
            if len(entries)>1000:raise BackupArchiveError('归档数量超过当前列表上限，请整理归档目录。')
        return {'archives':entries}

    install_certification_routes(router,app,service,projects,execute,_payload)
    install_author_baseline_routes(router,app,service,projects,execute,_payload)
    install_upgrade_routes(router,app,service,execute,_payload)
    install_legacy_memory_revision_routes(router,app,service,execute,_payload)
    app.include_router(router)
