"""External archived job artifacts must locate the restored book themselves."""
import pytest

from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import bound_copy
from tests.test_chapter_budget_runtime import args
from tests.test_restore_budget_preparation import make_financial_source, financial
from tests.test_restore_executions import prepared, prepare
from tests.test_restore_handoff import restored
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.backup_files import inventory_tree
from workbench.restore_execution_policy import RestoredExecutionBlocked, require_restored_execution_paths, POLICY_FILE


@pytest.fixture(scope='module')
def full_source():
    extra = {}
    def before_archive(c):
        with c['db'].connection() as connection:
            job = connection.execute('SELECT id FROM jobs WHERE project_id=? ORDER BY id',(c['project'].id,)).fetchone()[0]
        artifact = c['runtime']/job
        artifact.mkdir(parents=True)
        (artifact/'evidence.txt').write_text('归档中的原任务产物',encoding='utf-8')
        extra['external_job_id'] = job
    return {**make_financial_source(before_archive=before_archive), **extra}


def test_external_archived_job_namespace_is_blocked_without_explicit_book_root(prepared):
    import json
    c = prepared; prepare(c)
    path = c['target']/'jobs'/c['external_job_id']/'model_calls'
    policy = json.loads((c['target']/POLICY_FILE).read_text(encoding='utf-8'))
    assert 'jobs/'+c['external_job_id'] in policy['blocked_paths']
    before = inventory_tree(c['target']/'jobs')
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'):
        require_restored_execution_paths(path)
    calls = []
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'):
        ModelCallRuntimeContext(ModelCallStore(path)).execute_attempt(
            **{**args(call_id='never-dispatch'),'operation':lambda:calls.append('external')})
    assert calls == [] and inventory_tree(c['target']/'jobs') == before
    marker = c['target']/POLICY_FILE; raw = marker.read_bytes(); marker.unlink()
    with pytest.raises(RestoredExecutionBlocked): require_restored_execution_paths(path)
    assert not marker.exists() and inventory_tree(c['target']/'jobs') == before
    marker.write_bytes(raw)
    identity = c['target']/'book/.novelagent/project.json'; original = identity.read_bytes(); identity.unlink()
    with pytest.raises(RestoredExecutionBlocked): require_restored_execution_paths(path)
    assert not identity.exists() and inventory_tree(c['target']/'jobs') == before
    identity.write_bytes(original)


def test_direct_new_provider_attempts_remain_blocked_before_restore_activation(prepared):
    c = prepared; prepare(c); calls = []
    roots = [c['target']/'book/.novelagent/runtime/runs/executions/execution_new/model_calls',
             c['target']/'next-jobs/job_new/model_calls']
    for path in roots:
        with pytest.raises(RestoredExecutionBlocked,match='尚未启用'):
            ModelCallRuntimeContext(ModelCallStore(path)).execute_attempt(
                **{**args(call_id='no-activation'),'operation':lambda:calls.append('new')})
        assert calls == [] and not path.exists()
