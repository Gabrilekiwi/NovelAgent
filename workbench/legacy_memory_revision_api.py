"""Explicit review of old memory edits, under ordinary maintenance admission."""
from fastapi import Body
from workbench.backup_archive import BackupArchiveError
from workbench.legacy_memory_revision_service import LegacyMemoryRevisionService


def install_legacy_memory_revision_routes(router,app,backups,execute,validate_operation):
    service=LegacyMemoryRevisionService(backups)
    app.state.legacy_memory_revisions=service
    prefix='/api/projects/{project_id}/memory-revisions'
    fields={'operation_id','from_journal','to_journal','reason','reviewer'}

    def payload(value,confirm=False):
        if set(value)-{'supersedes'}!=fields|({'expected_sha256','confirmed'} if confirm else set()):
            raise BackupArchiveError('记忆修订请求字段无效。')
        validate_operation({'operation_id':value.get('operation_id')},('operation_id',))
        if (not all(isinstance(value[k],str) for k in ('from_journal','reason','reviewer'))
                or value['to_journal'] is not None and not isinstance(value['to_journal'],str)):
            raise BackupArchiveError('记忆修订来源或说明无效。')

    def checked(operation):
        try:return operation()
        except (OSError,ValueError) as exc:
            raise BackupArchiveError('记忆修订未保存：'+str(exc)) from exc

    @router.post(prefix+'/preview')
    async def preview(project_id:str,value:dict=Body(...)):
        payload(value)
        return await execute(value,lambda permit:checked(lambda:service.preview(project_id,**value,snapshot_permit=permit)))

    @router.post(prefix+'/confirm')
    async def confirm(project_id:str,value:dict=Body(...)):
        payload(value,True)
        return await execute(value,lambda permit:checked(lambda:service.confirm(project_id,**value,snapshot_permit=permit)))
