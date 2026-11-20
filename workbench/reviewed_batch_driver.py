"""Finite sequential dispatch over the original explicit batch run handle.

Construction and status reads never start work. Every stage retains its owned
grant, budget, revocation, receipt and original persistence boundaries.
"""
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService
from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
from workbench.reviewed_policy_quality import ReviewedPolicyQualityService
from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService
from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService


_STAGES=('generation','extraction','quality','publication','readback')
_NEXT={'generation':'extraction','extraction':'quality','quality':'publication','publication':'readback',
    'repair_generation':'repair_extraction','repair_extraction':'repair_quality','repair_quality':'publication'}


def _run_command(view):
    return next((c['command_sha256'] for c in reversed(view['control']['commands'])
        if c['action'] in {'start','resume'}),None)


class ReviewedBatchDriver:
    def __init__(self,controls,*,generation_executor_factory=None,extraction_executor_factory=None,
                 repair_executor_factory=None,fault_injector=None):
        self.controls=controls
        self.generation=ReviewedPolicyGenerationService(controls,executor_factory=generation_executor_factory)
        self.extraction=ReviewedPolicyExtractionService(controls,executor_factory=extraction_executor_factory)
        self.quality=ReviewedPolicyQualityService(controls,allow_repair=True)
        self.repair_generation=ReviewedPolicyGenerationService(controls,stage='repair_generation',
            executor_factory=repair_executor_factory if repair_executor_factory is not None else generation_executor_factory)
        self.repair_extraction=ReviewedPolicyExtractionService(controls,stage='repair_extraction',executor_factory=extraction_executor_factory)
        self.repair_quality=ReviewedPolicyQualityService(controls,stage='repair_quality')
        self.publication=ReviewedPolicyPublicationService(controls)
        self.readback=ReviewedPolicyReadbackService(controls)
        self.fault_injector=fault_injector

    def get(self,*,project_id,batch_id):
        return self.controls.get(project_id=project_id,batch_id=batch_id)

    def _require_handle(self,handle):
        if (self.controls.handles.get(getattr(handle,'batch_id',None)) is not handle
                or getattr(handle,'service',None) is not self.controls):
            raise BatchControlPaused('连续执行需要本次明确开始或继续的原批次许可。')

    def _revoke_after_error(self,handle,run_command):
        # Only reduce this run's authority. Never pause a newer author resume.
        revoke=True
        try:
            view=self.get(project_id=handle.project_id,batch_id=handle.batch_id)
            if _run_command(view)!=run_command:
                revoke=False
                return
            if view['progress']['status']=='running' and view['progress']['run_epoch']==handle.epoch:
                self.controls._signal(handle.project_id,handle.batch_id,'pause',
                    'driver-error:'+handle.epoch+':'+view['control']['record_sha256'],view['control']['record_sha256'])
        except Exception:
            # A torn or damaged log must remain available for explicit recovery.
            # The original exception is retained; no retry or new call follows.
            pass
        finally:
            if revoke and self.controls.handles.get(handle.batch_id) is handle:self.controls.handles.pop(handle.batch_id)

    def run(self,handle,*,expected_control_sha256):
        self._require_handle(handle)
        if not self.controls._driver_lock.acquire(blocking=False):
            raise BatchControlPaused('本服务已有批次执行中，不能重复启动调度。')
        steps=[];admitted=False;run_command=None
        try:
            view=self.get(project_id=handle.project_id,batch_id=handle.batch_id)
            if view['control']['record_sha256']!=expected_control_sha256:
                raise BatchControlError('连续执行的原控制版本已变化。')
            if view['progress']['status']!='running' or view['progress']['run_epoch']!=handle.epoch:
                return self._result(view,steps)
            run_command=_run_command(view)
            admitted=True
            with self.controls.publication._owned_project(handle.project_id) as context:
                grant=self.controls.batches._require_current_execution(context,handle.batch_id)['grant']
            manifest=grant['manifest']
            operations={'generation':self.generation.generate,'extraction':self.extraction.extract,
                'quality':self.quality.check,'publication':self.publication.publish,'readback':self.readback.readback,
                'repair_generation':self.repair_generation.generate,'repair_extraction':self.repair_extraction.extract,
                'repair_quality':self.repair_quality.check}
            for _ in range(len(manifest['chapters'])*len(operations)):
                self._require_handle(handle)
                view=self.get(project_id=handle.project_id,batch_id=handle.batch_id);before=view['progress']
                if before['status']!='running' or before['run_epoch']!=handle.epoch:
                    return self._result(view,steps)
                if _run_command(view)!=run_command:
                    raise BatchControlPaused('原批次已收到新的明确继续命令，旧调度停止。')
                chapter,stage=before['chapter'],before['stage']
                if chapter not in manifest['chapters'] or stage not in operations:
                    raise BatchControlError('原批次当前章号或阶段不在有限执行范围。')
                operations[stage](handle,expected_control_sha256=view['control']['record_sha256'])
                current=self.get(project_id=handle.project_id,batch_id=handle.batch_id);after=current['progress']
                steps.append(dict(chapter=chapter,stage=stage,status=after['status'],
                    control_sha256=current['control']['record_sha256']))
                if self.fault_injector is not None:self.fault_injector('after_stage',steps[-1])
                # Re-read after observers/signals; no cached result grants work.
                current=self.get(project_id=handle.project_id,batch_id=handle.batch_id);after=current['progress']
                if after['status']!='running':
                    if after['status']=='completed' and (stage!='readback' or chapter!=manifest['end_chapter']):
                        raise BatchControlError('批次完成状态与原有限末章 readback 不同。')
                    return self._result(current,steps)
                expected=(chapter+1,'generation') if stage=='readback' else (chapter,_NEXT[stage])
                if stage=='quality' and after['stage']=='repair_generation':expected=(chapter,'repair_generation')
                if (after['run_epoch']!=handle.epoch or _run_command(current)!=run_command
                        or (after['chapter'],after['stage'])!=expected
                        or after['pending_step'] is not None):
                    raise BatchControlPaused('阶段执行后原进度或明确运行许可发生变化。')
            raise BatchControlError('有限批次超过原阶段数量，不能继续循环。')
        except Exception:
            if admitted:self._revoke_after_error(handle,run_command)
            raise
        finally:self.controls._driver_lock.release()

    @staticmethod
    def _result(view,steps):
        return dict(control=view,steps=steps,execution_permit=False,publication_authorized=False,
            explicit_resume_required=view['explicit_resume_required'])
