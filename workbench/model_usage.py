"""Read-only physical-attempt history for an already registered local project."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from core.engine.execution_activity import execution_is_active
from core.engine.safe_paths import assert_safe_local_tree
from core.model_calls import ModelCallIntegrityError
from core.model_call_usage import project_model_attempt, summarize_model_attempts
from core.story_project.reviewed_state_document import read_project_bytes
from workbench.model_usage_budget import ProjectBudgetUsage


MAX_ATTEMPTS = 10000
_EXECUTION = re.compile(r'execution_[a-zA-Z0-9_-]{1,180}')


def read_project_model_usage(root, *, book_id=None):
    """Attribute fees to verified evidence; keep reservations out of expenses."""
    root = Path(root)
    budgets=ProjectBudgetUsage(root,book_id=book_id,max_attempts=MAX_ATTEMPTS)
    executions = assert_safe_local_tree(root / '.novelagent/runtime/runs/executions')
    rows = []
    stores = []
    if executions.exists():
        for execution in sorted(executions.iterdir(), key=lambda path:path.name):
            if not execution.name.startswith('execution_'):
                continue
            if not _EXECUTION.fullmatch(execution.name):
                raise ModelCallIntegrityError('invalid execution directory in model-call evidence')
            store = assert_safe_local_tree(execution / 'model_calls')
            if not store.exists():
                continue
            observed = execution_is_active(executions.parent, execution.name) if execution.name.startswith(('execution_candidate_','execution_fx_')) else None
            stores.append((execution.name,store,observed))
    # The original planning and amendment services use per-operation stores,
    # outside chapter execution directories. Include them without a broad scan.
    for family in ('workbench_outline_batches','workbench_amendments'):
        parent = assert_safe_local_tree(root / '.novelagent/runtime/model_calls' / family)
        if parent.exists():
            for store in sorted(parent.iterdir(),key=lambda path:path.name):
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,191}',store.name):
                    raise ModelCallIntegrityError('invalid model-call operation directory')
                stores.append((family+':'+store.name,assert_safe_local_tree(store),None))
    # Include original reservations even before their first physical Intent,
    # so deleting a bound execution cannot silently remove its cost evidence.
    known={identity for identity,_,_ in stores}
    for identity in sorted(budgets.executions.keys()-known):
        stores.append((identity,assert_safe_local_tree(executions/identity/'model_calls'),None))
    for identity,store,observed in stores:
        binding=budgets.bind(identity,store)
        intents = assert_safe_local_tree(store / 'intents')
        receipts = assert_safe_local_tree(store / 'receipts')
        names = set()
        for directory in (intents,receipts):
            if directory.exists():
                for path in directory.iterdir():
                    if path.suffix == '.json':names.add(path.stem)
        if binding is not None:names.update(binding['attempts'])
        if len(rows)+len(names)>MAX_ATTEMPTS:
            raise ModelCallIntegrityError('model-call history exceeds the supported report size; no partial total was produced')
        for name in sorted(names):
            def read(directory):
                path = directory / (name + '.json')
                return json.loads(read_project_bytes(root,path.relative_to(root).as_posix()))
            if not (intents/(name+'.json')).exists() and binding is not None and name in binding['attempts']:
                original,_=binding['attempts'][name]
                if not original['dispatch_claimed'] and original['receipt'] is None and not (receipts/(name+'.json')).exists():
                    continue  # Durable reservation before Intent publication.
            intent = read(intents)
            if intent.get('attempt_id') != name:
                raise ModelCallIntegrityError('model-call filename does not match its attempt identity')
            receipt = read(receipts) if (receipts / (name+'.json')).exists() else None
            item=project_model_attempt(identity,intent,receipt,active=observed)
            rows.append(budgets.project(binding,store,name,intent,receipt,item))
    rows.sort(key=lambda item:(item['created_at'],item['execution_id'],item['attempt_id']),reverse=True)
    return {'schema_version':'model_usage_report_v1','scope':'project','observed_at':datetime.now(timezone.utc).isoformat(),
            'items':rows,'summary':summarize_model_attempts(rows),'chapter_budgets':budgets.reports()}
