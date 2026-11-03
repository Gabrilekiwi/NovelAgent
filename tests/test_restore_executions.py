"""Actual archive history remains readable but cannot execute after restore."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy, logical
from tests.test_chapter_budget_runtime import args
from tests.test_restore_budget_preparation import make_financial_source, financial
from tests.test_restore_handoff import restored, ledger, deny
from tests.test_restore_jobs import prepared as budget_prepared, prepare as prepare_jobs
from tests.test_restore_usage import pending
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.backup_files import inventory_tree
from workbench.restore_execution_policy import POLICY_FILE, FRESH_JOBS, RestoredExecutionBlocked, require_restored_execution_paths
from workbench.restore_executions import RestoreExecutionPreparation
from workbench.restore_handoff import RestoreHandoffError


@pytest.fixture(scope='module')
def full_source():
    from tests import test_chapter_fact_extraction as fx
    extra = {}
    def before_archive(c):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr('workbench.book_ownership.default_machine_store',lambda:c['machine'])
            patch.setattr(fx,'BOOK_ID',c['project'].book_id)
            executor = fx._executor(c['root'],lambda *args:fx._raw_empty(),execution_kind='deterministic')
            record = fx._execute(executor)
            assert record['status'] == 'completed'
            extra['extraction_id'] = record['extraction_id']
    return {**make_financial_source(before_archive=before_archive), **extra}


@pytest.fixture
def prepared(budget_prepared):
    c = budget_prepared; job_receipt = prepare_jobs(c)
    service = RestoreExecutionPreparation(c['project'].book_id,machine_store=c['store'])
    return {**c,'jobs_receipt':job_receipt,'execution_service':service}


def prepare(c, *, resume=False, service=None):
    service = service or c['execution_service']
    method = service.resume_executions if resume else service.prepare_executions
    return method(c['package'],handoff_id=c['id'],temporary_root=c['scratch'])


def test_actual_old_candidate_fact_and_call_namespaces_cannot_run(prepared):
    from tests import test_chapter_fact_extraction as fx
    c = prepared; root = c['target']/'book'; database = c['target']/'workbench.sqlite'
    source = inventory_tree(c['root']); before = inventory_tree(root); sql = logical(database)
    candidate_id = c['record']['candidate_id']; extraction_id = c['extraction_id']
    candidate = ChapterCandidateStore(root).load_record(candidate_id)
    extraction = ChapterFactExtractionStore(root).load_record(extraction_id)
    result = prepare(c)
    assert result['old_execution_namespaces_invalidated'] and result['new_task_directory_ready']
    assert not result['write_activation_allowed'] and not result['handoff_completed']
    assert list((c['target']/FRESH_JOBS).iterdir()) == []
    policy = json.loads((c['target']/POLICY_FILE).read_text(encoding='utf-8'))
    assert 'book/.novelagent/runtime/runs/executions/execution_shared' in policy['blocked_paths']
    calls = []
    executor = ReviewedChapterCandidateExecutor(story_project_root=root,
        generator=lambda _:calls.append('candidate'),execution_kind='deterministic',provenance_factory=_fixed_provenance)
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'): executor.resume(candidate_id)
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'): executor.cancel(candidate_id)
    extraction_executor = fx._executor(root,lambda *args:calls.append('fact'),execution_kind='deterministic')
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'): extraction_executor.resume(extraction_id)
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'): extraction_executor.cancel(extraction_id)
    runtime = ModelCallRuntimeContext(ModelCallStore(root/'.novelagent/runtime/runs/executions/execution_shared/model_calls'))
    with pytest.raises(RestoredExecutionBlocked,match='只保留历史'):
        runtime.execute_attempt(**{**args(call_id='must-not-repeat'),'operation':lambda:calls.append('call')})
    assert calls == []
    assert ChapterCandidateStore(root).load_record(candidate_id) == candidate
    assert ChapterFactExtractionStore(root).load_record(extraction_id) == extraction
    assert any('/transactions/pre-upgrade' in name for name in policy['blocked_paths'])
    for name in policy['blocked_paths']:
        require_root = root if not (c['target']/name).is_relative_to(root) else c['target']/name
        with pytest.raises(RestoredExecutionBlocked): require_restored_execution_paths(require_root,c['target']/name)
    new_execution = root/'.novelagent/runtime/runs/executions/execution_new_after_restore'
    require_restored_execution_paths(new_execution)
    assert not new_execution.exists()  # The denial gate itself grants/creates nothing.
    with absent_source(c['root']):
        assert prepare(c,resume=True) == result and prepare(c) == result
    assert inventory_tree(root) == before and inventory_tree(c['root']) == source
    assert logical(database) == sql and ledger(c) == c['value']['restoring_record']
    deny(root); deny(c['root'])


@pytest.mark.parametrize('phase',['after_execution_intent','after_execution_policy_temp_fsync',
    'after_execution_policy','after_fresh_task_directory','after_execution_completion'])
def test_real_process_crash_resumes_execution_preparation_without_source(prepared,phase):
    c = prepared; before = inventory_tree(c['target']/'book'); sql = logical(c['target']/'workbench.sqlite')
    script = '''import os,sys
from workbench.restore_executions import RestoreExecutionPreparation
def fault(phase,value):
    if phase==sys.argv[6]: os._exit(88)
RestoreExecutionPreparation(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).prepare_executions(
    sys.argv[3],handoff_id=sys.argv[4],temporary_root=sys.argv[5])
'''
    child = subprocess.run([sys.executable,'-B','-c',script,c['project'].book_id,str(c['store']),str(c['package']),
        c['id'],str(c['scratch']),phase],cwd=Path.cwd(),capture_output=True,text=True,encoding='utf-8',timeout=180)
    assert child.returncode == 88, child.stdout+child.stderr
    intent = c['execution_service']._path(c['id'],'executions').read_bytes()
    if phase != 'after_execution_completion':
        with pytest.raises(RestoreHandoffError,match='显式续接'): prepare(c)
        with pytest.raises(RestoredExecutionBlocked):
            require_restored_execution_paths(c['target']/'book/.novelagent/runtime/runs/executions/execution_new')
    with absent_source(c['root']): result = prepare(c,resume=True)
    assert result['old_execution_namespaces_invalidated'] and not result['write_activation_allowed']
    assert c['execution_service']._path(c['id'],'executions').read_bytes() == intent
    assert not (c['target']/'.execution-policy.tmp').exists()
    assert inventory_tree(c['target']/'book') == before and logical(c['target']/'workbench.sqlite') == sql


def test_missing_policy_or_changed_fresh_directory_cannot_be_repaired(prepared):
    c = prepared; prepare(c); policy = c['target']/POLICY_FILE; raw = policy.read_bytes()
    policy.unlink()
    with pytest.raises((OSError,RuntimeError,ValueError)): prepare(c,resume=True)
    assert not policy.exists()
    with pytest.raises(RestoredExecutionBlocked): require_restored_execution_paths(c['target']/'book')
    policy.write_bytes(raw)
    fresh = c['target']/FRESH_JOBS; fresh.rename(c['target']/'displaced-next-jobs'); fresh.mkdir()
    with pytest.raises((OSError,RuntimeError,ValueError)): prepare(c,resume=True)
    with pytest.raises(RestoredExecutionBlocked): require_restored_execution_paths(c['target']/'book')


@pytest.mark.parametrize('suffix',['executions','jobs-done'])
def test_lost_intent_before_policy_publication_does_not_publish(prepared,suffix):
    c = prepared
    def fault(phase,value):
        if phase=='after_execution_policy_temp_fsync':
            path = service._path(c['id'],suffix)
            value = {} if suffix=='executions' else json.loads(path.read_text(encoding='utf-8'))
            if suffix=='jobs-done': value['original_job_count'] += 1  # Retain the old digest to test full receipt validation.
            path.write_text(json.dumps(value),encoding='utf-8')
    service = RestoreExecutionPreparation(c['project'].book_id,machine_store=c['store'],fault_injector=fault)
    with pytest.raises(RestoreHandoffError): prepare(c,service=service)
    assert not (c['target']/POLICY_FILE).exists()
    assert not service._path(c['id'],'executions-done').exists()
    assert ledger(c) == c['value']['restoring_record']
