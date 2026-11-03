"""Guard every registered write route, including the real endpoint lifetime."""
import asyncio

from fastapi import APIRouter, Body, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from workbench.maintenance import MaintenanceBlocked, MaintenanceError


class MaintenanceRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def guarded(request):
            if request.method not in {'POST','PUT','PATCH','DELETE'}:
                return await handler(request)
            async def operation():
                await asyncio.to_thread(request.app.state.startup_recovery.run)
                return await handler(request)
            return await request.app.state.maintenance.run_async('http_write',operation)
        return guarded


class MaintenanceControlRoute(APIRoute):
    """Only the three coordinator transitions bypass normal write admission.

    Their synchronous transition runs without an await; FastAPI cannot cancel
    it halfway through a persisted gate update. Startup work after release uses
    the normal detached-task admission and shutdown drain.
    """


def install_maintenance_status(app):
    @app.exception_handler(MaintenanceError)
    async def maintenance_error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':str(exc)}},
            status_code=423 if isinstance(exc,MaintenanceBlocked) else 409)

    @app.get('/api/maintenance')
    async def maintenance_status():
        return app.state.maintenance.status()

    control=APIRouter(route_class=MaintenanceControlRoute)

    @control.post('/api/maintenance/{action}')
    async def maintenance_control(action:str,request:Request,payload:dict=Body(...)):
        gate=app.state.maintenance
        scope=getattr(request.state,'restore_scope',None)
        if scope is not None and scope.controller is not None:
            if action=='begin' or payload.get('operation_id')!=scope.controller['operation_id']:
                raise MaintenanceError('原副本只能继续或结束原恢复维护操作。')
        if action=='begin':
            if set(payload) not in (set(),{'purpose'}):raise MaintenanceError('进入维护参数无效。')
            return gate.begin(purpose=payload.get('purpose','backup'))
        if action not in {'resume','finish'} or set(payload)!={'operation_id'}:
            raise MaintenanceError('维护操作及其标识无效。')
        if action=='resume':return gate.resume(payload['operation_id'])
        recovery=getattr(app.state,'recovery_sessions',None)
        if recovery is not None and gate.status()['purpose']=='restore':
            recovery.require_finishable(payload['operation_id'])
        gate.finish(payload['operation_id'])
        async def startup():await asyncio.to_thread(app.state.startup_recovery.run)
        await gate.run_async('local_writer',startup)
        return gate.status()

    app.include_router(control)
