"""Maintenance-owned recovery-session routes; none execute at startup."""
import re
import sqlite3
import asyncio
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.routing import APIRoute

from workbench.recovery_sessions import RecoverySessionService, RecoverySessionError
from workbench.maintenance import MaintenanceError
from workbench.recovery_target import RecoveryTargetLauncher
from workbench.restore_scope import load_restore_scope


class RecoveryControlRoute(APIRoute):
    """Handlers own snapshot admission instead of ordinary writer admission."""


def install_recovery_api(app, *, projects, maintenance, backups, machine_store=None,
                         repository=None, python_executable=None):
    service = RecoverySessionService(archive_root=backups.archive_root,maintenance=maintenance,machine_store=machine_store,
        source_scope=projects.database.restore_scope)
    app.state.recovery_sessions = service
    launcher = RecoveryTargetLauncher(repository=repository or Path(__file__).resolve().parents[1],
        python_executable=python_executable, machine_store=machine_store)
    app.state.recovery_target_launcher = launcher
    router = APIRouter(route_class=RecoveryControlRoute)

    def controller(session_id=None, value=None, *, new=False):
        original = projects.database.restore_scope
        if original is None:
            return None
        scope = load_restore_scope(projects.database.path,machine_store=machine_store,allow_recovery_controller=True)
        if scope is None or scope.document != original.document:
            raise RecoverySessionError('原恢复控制实例范围变化。')
        bound = scope.controller or original.controller
        if bound is not None:
            if (new or session_id is not None and session_id != bound['session_id']
                    or value is not None and value.get('operation_id') != bound['operation_id']):
                raise RecoverySessionError('原副本只能继续原会话和原维护操作。')
            return bound
        if not scope.active:
            raise RecoverySessionError('此恢复副本尚未启用，不能开始再次恢复。')
        return None

    def payload(value, fields, optional=()):
        if not isinstance(value,dict) or not set(fields) <= set(value) or not set(value) <= set(fields)|set(optional):
            raise RecoverySessionError('恢复请求字段无效。')
        if (not isinstance(value.get('operation_id'),str)
                or re.fullmatch('[0-9a-f]{32}',value['operation_id']) is None):
            raise RecoverySessionError('请继续当前恢复维护操作。')
        for name in set(value)-{'confirmed','resume','chapter'}:
            if not isinstance(value[name],str):
                raise RecoverySessionError('恢复请求字段类型无效。')

    async def execute(value, work):
        try:
            return await maintenance.run_capture_async(value['operation_id'],work)
        except (RecoverySessionError,MaintenanceError):
            raise
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
            raise RecoverySessionError(str(exc)) from exc

    @router.get('/api/recovery-sessions')
    async def sessions():
        bound = controller()
        return {'sessions':[service.get(bound['session_id'])] if bound else service.list()}

    @router.post('/api/recovery-sessions/preview')
    async def preview(value:dict=Body(...)):
        payload(value,('operation_id','archive_id','destination'))
        controller(value=value,new=True)
        def work(permit):
            plan = service.preview(value['archive_id'],destination=value['destination'],
                operation_id=value['operation_id'],snapshot_permit=permit)
            return {key:plan[key] for key in ('archive_id','project_id','book_id','plan_sha256',
                'offline_data_verified','plan_ready','continuation_blockers','recoverable')} | {
                    'destination':plan['root_plan']['container'],
                    'source_root':next(item['source_root'] for item in plan['root_plan']['roots'] if item['root']=='story_project')}
        return await execute(value,work)

    @router.post('/api/recovery-sessions')
    async def create(value:dict=Body(...)):
        payload(value,('operation_id','archive_id','destination','expected_plan_sha256','session_id'))
        controller(value=value,new=True)
        return await execute(value,lambda permit:service.create(value['archive_id'],destination=value['destination'],
            operation_id=value['operation_id'],expected_plan_sha256=value['expected_plan_sha256'],
            session_id=value['session_id'],snapshot_permit=permit))

    @router.get('/api/recovery-sessions/{session_id}')
    async def session(session_id:str):
        controller(session_id)
        return service.get(session_id)

    @router.post('/api/recovery-sessions/{session_id}/steps/{action}')
    async def step(session_id:str,action:str,value:dict=Body(...)):
        payload(value,('operation_id','expected_state_sha256','confirmed','resume'))
        controller(session_id,value)
        return await execute(value,lambda permit:service.step(session_id,action,
            operation_id=value['operation_id'],expected_state_sha256=value['expected_state_sha256'],
            confirmed=value['confirmed'],resume=value['resume'],snapshot_permit=permit))

    @router.post('/api/recovery-sessions/{session_id}/readback')
    async def readback(session_id:str,value:dict=Body(...)):
        payload(value,('operation_id',),('chapter','draft_id'))
        controller(session_id,value)
        return await execute(value,lambda permit:service.readback(session_id,operation_id=value['operation_id'],
            snapshot_permit=permit,chapter=value.get('chapter'),draft_id=value.get('draft_id')))

    @router.post('/api/recovery-sessions/{session_id}/open')
    async def open_target(session_id:str,value:dict=Body(...)):
        payload(value,('operation_id','expected_state_sha256'))
        controller(session_id,value)
        def work(_permit=None):
            current = service.get(session_id)
            if (current['operation_id'] != value['operation_id']
                    or current['state_sha256'] != value['expected_state_sha256']):
                raise RecoverySessionError('恢复会话已变化，请读取原会话的最新进度再打开。')
            result = launcher.open(current)
            if service.get(session_id) != current:
                raise RecoverySessionError('恢复会话在打开前发生变化，请核对最新进度。')
            return result
        if maintenance.status()['state'] != 'idle':
            return await execute(value,work)
        async def launch():
            return await asyncio.to_thread(work)
        return await maintenance.run_async('local_writer',launch)

    app.include_router(router)
