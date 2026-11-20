"""Service-owned time scopes, inside the reviewed book mutation boundary."""
from contextlib import nullcontext


def chapter_service_operation(executor,project,chapter,execution_id,*,completed=False,resume=False):
    select=getattr(executor,'_select_execution_budget',None)
    budget=select(project.book_id,chapter) if select is not None else None
    if budget is None:return nullcontext()
    report=budget.time_clock.report()
    local_only=completed or resume and (report['active_status']=='unknown' or report['active_limit_reached'])
    return budget.operation(execution_id,local_only=local_only)
