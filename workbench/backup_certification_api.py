"""Explicit maintenance-owned access to actual archive certification."""
from fastapi import Body

from workbench.backup_archive import BackupArchiveError
from workbench.backup_certification import RecoveryCertification,verify_recovery_certificate


def install_certification_routes(router,app,backups,projects,execute,validate_payload):
    certifier=RecoveryCertification(backups)
    app.state.recovery_certification=certifier
    prefix='/api/projects/{project_id}/backups/{archive_id}/certification'

    @router.post(prefix+'/preview')
    async def preview(project_id:str,archive_id:str,payload:dict=Body(...)):
        validate_payload(payload,('operation_id',))
        return await execute(payload,lambda permit:certifier.preview(project_id,archive_id=archive_id,
            operation_id=payload['operation_id'],snapshot_permit=permit))

    @router.post(prefix)
    async def issue(project_id:str,archive_id:str,payload:dict=Body(...)):
        if payload.get('confirmed') is not True:
            raise BackupArchiveError('请核对认证前提后明确确认签发。')
        validate_payload({k:v for k,v in payload.items() if k!='confirmed'},
            ('operation_id','expected_assessment_sha256'))
        return await execute(payload,lambda permit:certifier.issue(project_id,archive_id=archive_id,
            operation_id=payload['operation_id'],expected_assessment_sha256=payload['expected_assessment_sha256'],
            snapshot_permit=permit))

    @router.post(prefix+'/verify')
    async def verify(project_id:str,archive_id:str,payload:dict=Body(...)):
        validate_payload(payload,('operation_id',))
        package=certifier._package(archive_id)
        def work(permit):
            permit.require()
            # Resolve the expected book from the actual DB, without opening the
            # original book: offline certificate verification survives its loss.
            projects.database.require_project_access(project_id)
            with projects.database.connection() as connection:
                row=connection.execute('SELECT book_id FROM projects WHERE id=?',(project_id,)).fetchone()
            if row is None:raise BackupArchiveError('当前作品登记不存在。')
            result=verify_recovery_certificate(package,project_id=project_id,book_id=row['book_id'])
            permit.require()
            return result
        return await execute(payload,work)
