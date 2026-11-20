"""Activate a real restored book with settled retained spending and no auto calls."""
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from core.autonomy.common import atomic_append_json
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy, logical
from tests.test_restore_budget_preparation import make_financial_source, financial
from tests.test_restore_executions import prepared as jobs_prepared, prepare as prepare_executions
from tests.test_restore_handoff import restored, deny, ledger
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.book_ownership import OWNERSHIP_V3, BookOwnershipError
from workbench.database import WorkbenchDatabase
from workbench.restore_activation import RestoreActivation
from workbench.restore_execution_policy import RestoredExecutionBlocked
from workbench.restore_scope import load_restore_scope
from workbench.restore_handoff import RestoreHandoffError
from workbench.restore_qualification import ACTIVATION_FILE, ACTIVATION_TEMP


@pytest.fixture(scope='module')
def full_source():
    return make_financial_source(include_uncertain=False)


@pytest.fixture
def prepared(jobs_prepared):
    c=jobs_prepared; execution_done=prepare_executions(c)
    return {**c,'activation_service':RestoreActivation(c['project'].book_id,machine_store=c['store']),
        'execution_done':execution_done}


def activate(c,*,resume=False,service=None):
    return (service or c['activation_service']).activate(c['package'],handoff_id=c['id'],
        temporary_root=c['scratch'],resume=resume)


def test_actual_copy_activates_only_target_and_restarts_without_auto_calls(prepared):
    c=prepared; root=c['target']/'book'; path=c['target']/'workbench.sqlite'
    book=inventory_tree(root); sql=logical(path); source=inventory_tree(c['root'])
    with absent_source(c['root']):
        result=activate(c)
        assert result['target_writable'] and not result['automatic_provider_calls_allowed']
        record=ledger(c)
        assert record['version']==OWNERSHIP_V3 and record['state']=='active'
        assert record['generation']==c['value']['restoring_record']['generation']+1
        scope=load_restore_scope(path,machine_store=c['store'])
        assert scope.active and scope.task_runtime_root==c['target']/'next-jobs'
        assert inventory_tree(root)==book and logical(path)==sql
        calls=[]
        def forbidden(*args,**kwargs): calls.append(1);pytest.fail('restart invoked an executor')
        for _ in range(2):
            app=create_app(database_path=path,start_worker=True,auto_import_active=True,
                chapter_publication_machine_store=c['store'],reviewed_chapter_candidate_executor_factory=forbidden,
                reviewed_chapter_fact_extraction_executor_factory=forbidden)
            with TestClient(app) as client:
                state=client.get('/api/restore-scope')
                assert state.status_code==200 and state.json()['state']=='active' and not state.json()['read_only']
                assert app.state.supervisor.runtime_root==scope.task_runtime_root and app.state.supervisor._thread is None
                assert client.get('/api/projects/'+c['other'].id).status_code==409
                assert client.get('/api/jobs/'+c['other_job'].id).status_code==409
                assert [p['id'] for p in client.get('/api/projects').json()['items']]==[c['project'].id]
        assert not calls
        # Exercise actual ordinary file and database writers after activation.
        atomic_append_json(root/'.novelagent/runtime/activated-probe.json',{'target_only':True})
        db=WorkbenchDatabase(path,machine_store=c['store'])
        with db.transaction() as connection:
            connection.execute('UPDATE drafts SET text=? WHERE id=?',('恢复后作者修改',c['draft'].id))
        assert activate(c,resume=True)==result  # Later valid author changes are not reset to the archive.
    assert inventory_tree(c['root'])==source
    deny(c['root'])
    with pytest.raises((BookOwnershipError,RestoredExecutionBlocked,RuntimeError,ValueError)):
        c['handoff'].rollback(handoff_id=c['id'])


@pytest.mark.parametrize('phase', ['after_activation_intent', 'after_activation_temp_fsync',
    'after_activation_qualification', 'after_activation_machine', 'after_activation_completion'])
def test_actual_process_crash_resumes_activation_without_source(prepared, phase):
    c = prepared; root = c['target']/'book'; database = c['target']/'workbench.sqlite'
    before = inventory_tree(root); sql = logical(database)
    code = '''import os,sys
from workbench.restore_activation import RestoreActivation
def fault(phase,value):
    if phase==sys.argv[6]: os._exit(89)
RestoreActivation(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).activate(
    sys.argv[3],handoff_id=sys.argv[4],temporary_root=sys.argv[5])
'''
    child = subprocess.run([sys.executable, '-B', '-c', code, c['project'].book_id,
        str(c['store']), str(c['package']), c['id'], str(c['scratch']), phase],
        capture_output=True, text=True, encoding='utf-8', timeout=180,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert child.returncode == 89, child.stdout+child.stderr
    intent_path = c['activation_service']._path(c['id'], 'activation')
    intent = intent_path.read_bytes()
    if phase != 'after_activation_completion':
        with pytest.raises(RestoreHandoffError, match='显式续接'): activate(c)
        scope = load_restore_scope(database, machine_store=c['store'])
        assert scope.read_only
        probe = root/'.novelagent/runtime/unfinished-activation.json'
        with pytest.raises((RuntimeError, ValueError, OSError)):
            atomic_append_json(probe, {'must_not_write':True})
        assert not probe.exists()
        from core.model_calls import ModelCallStore
        from core.model_call_runtime import ModelCallRuntimeContext
        from tests.test_chapter_budget_runtime import args
        call_root = root/'.novelagent/runtime/runs/executions/execution_before_activation/model_calls'
        runtime = ModelCallRuntimeContext(ModelCallStore(call_root)); calls = []
        with pytest.raises((RuntimeError, ValueError, OSError)):
            runtime.execute_attempt(**{**args(call_id='not-authorized'), 'operation':lambda:calls.append(1)})
        assert calls == [] and not call_root.exists()
    with absent_source(c['root']):
        result = activate(c, resume=True)
    assert result['target_writable'] and not result['automatic_provider_calls_allowed']
    assert intent_path.read_bytes() == intent and not (c['target']/ACTIVATION_TEMP).exists()
    assert load_restore_scope(database, machine_store=c['store']).active
    assert inventory_tree(root) == before and logical(database) == sql


def test_completed_activation_proof_loss_revokes_existing_writers(prepared):
    c = prepared; completed = activate(c); root = c['target']/'book'; path = c['target']/'workbench.sqlite'
    database = WorkbenchDatabase(path, machine_store=c['store'])
    service = c['activation_service']
    proofs = [c['target']/ACTIVATION_FILE, c['target']/'execution-policy.json',
        path.with_name(path.name+'.restore-scope.json')]
    proofs += [service._path(c['id'], suffix) for suffix in
        ('activation', 'activation-done', 'executions', 'executions-done', 'jobs-done', 'budget-done')]
    before = inventory_tree(root); sql = logical(path)
    for proof in proofs:
        raw = proof.read_bytes(); proof.unlink()
        try:
            probe = root/'.novelagent/runtime/missing-proof-write.json'
            with pytest.raises((RuntimeError, ValueError, OSError)):
                atomic_append_json(probe, {'must_not_write':True})
            assert not probe.exists()
            with pytest.raises((RuntimeError, ValueError, OSError)):
                with database.transaction() as connection:
                    connection.execute('UPDATE drafts SET text=? WHERE id=?', ('forbidden', c['draft'].id))
            if proof == service._path(c['id'], 'activation-done'):
                # Lost final acknowledgement is indistinguishable from the real
                # after-machine crash. Only explicit resume with the original
                # intent and a complete, still-pristine copy can finish it again.
                assert activate(c, resume=True) == completed
                assert proof.read_bytes() == raw
            else:
                with pytest.raises((RuntimeError, ValueError, OSError)): activate(c, resume=True)
                assert not proof.exists()
        finally:
            proof.write_bytes(raw)
        assert load_restore_scope(path, machine_store=c['store']).active
    assert inventory_tree(root) == before and logical(path) == sql
    # Once legitimate author work exists, a missing completion cannot be rebuilt
    # by silently resetting the book or skipping the full preparation proof.
    author_file = root/'.novelagent/runtime/author-after-activation.json'
    atomic_append_json(author_file, {'keep_author_work':True})
    changed = inventory_tree(root)
    done = service._path(c['id'], 'activation-done'); done.unlink()
    with pytest.raises((RuntimeError, ValueError, OSError)): activate(c, resume=True)
    assert not done.exists() and inventory_tree(root) == changed and logical(path) == sql


def test_lost_activation_intent_before_qualification_cannot_publish(prepared):
    c = prepared
    def fault(phase, value):
        if phase == 'after_activation_temp_fsync': service._path(c['id'], 'activation').unlink()
    service = RestoreActivation(c['project'].book_id, machine_store=c['store'], fault_injector=fault)
    with pytest.raises((RuntimeError, ValueError, OSError)): activate(c, service=service)
    assert not (c['target']/ACTIVATION_FILE).exists()
    assert not service._path(c['id'], 'activation-done').exists()
    assert ledger(c) == c['value']['restoring_record']
    with pytest.raises(RestoreHandoffError, match='意图丢失'): activate(c, resume=True)
