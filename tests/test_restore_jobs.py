"""Actual restored DB job invalidation; source history and budgets survive."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy, logical, rows, run
from tests.test_restore_budget_preparation import make_financial_source, financial, prepare as prepare_budget
from tests.test_restore_handoff import restored, deny, ledger
from tests.test_restore_usage import pending
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.jobs import JobError, _require_job_execution_contract
from workbench.restore_handoff import RestoreHandoffError
from workbench.restore_jobs import RestoreJobPreparation, INVALIDATED_CONTRACT


@pytest.fixture(scope='module')
def full_source():
    def add_history(c):
        with closing(sqlite3.connect(c['db'].path)) as connection:
            connection.row_factory = sqlite3.Row
            original = dict(connection.execute('SELECT * FROM jobs WHERE project_id=?', (c['project'].id,)).fetchone())
            for state in ('completed', 'cancelled', 'failed'):
                row = {**original, 'id':'job_history_'+state, 'state':state,
                    'config_json':json.dumps({'author_note':'保留原配置'}, ensure_ascii=False),
                    'result_json':json.dumps({'history':'原结果'}, ensure_ascii=False),
                    'error_json':json.dumps({'message':'原错误'}, ensure_ascii=False),
                    'finished_at':original['updated_at']}
                connection.execute('INSERT INTO jobs ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', list(row.values()))
                connection.execute('INSERT INTO job_events(job_id,sequence,event_type,payload_json,created_at) VALUES (?,?,?,?,?)',
                    (row['id'], 1, 'historical_fixture', '{"note":"原事件"}', original['updated_at']))
            connection.commit()
    return make_financial_source(before_archive=add_history)


@pytest.fixture
def prepared(financial):
    c = financial; budget = prepare_budget(c)
    return {**c, 'budget_receipt':budget,
        'job_service':RestoreJobPreparation(c['project'].book_id, machine_store=c['store'])}


def prepare(c, *, resume=False, service=None):
    service = service or c['job_service']
    method = service.resume_jobs if resume else service.prepare_jobs
    return method(c['package'], handoff_id=c['id'], temporary_root=c['scratch'])


def history_rows(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute('SELECT * FROM workbench_restored_job_history ORDER BY job_id')]


def test_actual_jobs_preserve_original_rows_events_and_read_only_app(prepared):
    c = prepared; database = c['target']/'workbench.sqlite'
    old = rows(database, 'jobs')
    preserved = {table:rows(database, table) for table in ('projects','job_events','drafts')}
    book = inventory_tree(c['target']/'book'); source = inventory_tree(c['root']); source_sql = logical(c['db'].path)
    receipt = prepare(c)
    assert receipt['original_job_count'] == 5 and receipt['workbench_jobs_invalidated']
    assert receipt['original_rows_preserved'] and not receipt['prepared_candidate_plans_invalidated']
    assert not receipt['write_activation_allowed'] and not receipt['fresh_execution_roots_ready']
    audit = history_rows(database)
    assert [json.loads(row['original_row_json']) for row in audit] == old
    assert [row['row_sha256'] for row in audit] == [digest(row) for row in old]
    assert all(row['handoff_id'] == c['id'] for row in audit)
    actual = rows(database, 'jobs')
    intent = json.loads(c['job_service']._path(c['id'], 'jobs').read_text(encoding='utf-8'))
    for before, after in zip(old, actual):
        expected = {**before, 'execution_contract':INVALIDATED_CONTRACT,
            'state':before['state'] if before['state'] in {'completed','cancelled','failed'} else 'cancelled',
            'revision':before['revision']+1, 'updated_at':intent['created_at']}
        assert after == expected
        with pytest.raises(JobError, match='契约不兼容'): _require_job_execution_contract(after)
    assert {table:rows(database, table) for table in preserved} == preserved
    assert logical(database) == receipt['database_logical_sha256']
    db_bytes = database.read_bytes()
    with absent_source(c['root']):
        assert prepare(c, resume=True) == receipt
        assert prepare(c) == receipt
        def forbidden(*args, **kwargs): pytest.fail('restored history attempted Provider execution')
        app = create_app(database_path=database, start_worker=True, auto_import_active=True,
            chapter_publication_machine_store=c['store'],
            chapter_candidate_executor_factory=forbidden, chapter_fact_extraction_executor_factory=forbidden,
            reviewed_chapter_candidate_executor_factory=forbidden, reviewed_chapter_fact_extraction_executor_factory=forbidden)
        with TestClient(app) as client:
            assert client.get('/api/restore-scope').json()['read_only']
            job = client.get('/api/jobs/'+c['job'].id)
            assert job.status_code == 200, job.text
            assert job.json()['job']['execution_compatible'] is False
            assert not app.state.jobs.resume_ready(c['job'].id)
            assert app.state.jobs.claim_next() is None and app.state.supervisor._thread is None
            assert client.get('/api/jobs/'+c['other_job'].id).status_code == 409
    assert database.read_bytes() == db_bytes
    assert inventory_tree(c['target']/'book') == book and inventory_tree(c['root']) == source
    assert logical(c['db'].path) == source_sql and ledger(c) == c['value']['restoring_record']
    deny(c['root']); deny(c['target']/'book')


@pytest.mark.parametrize('phase', ['after_jobs_intent','before_jobs_commit','before_jobs_commit_flushed',
    'after_jobs_commit','after_jobs_completion'])
def test_real_process_crash_resumes_exact_job_invalidation_without_source(prepared, phase):
    c = prepared; database = c['target']/'workbench.sqlite'
    old = rows(database, 'jobs'); before = database.read_bytes(); book = inventory_tree(c['target']/'book')
    script = '''import os,sys
from pathlib import Path
from workbench import restore_jobs as m
target=Path(sys.argv[7])/'workbench.sqlite'
if sys.argv[6]=='before_jobs_commit_flushed':
    original=m._apply_jobs
    def spilling(connection,*args):
        actual=Path(connection.execute('PRAGMA database_list').fetchone()[2])==target
        if actual: connection.execute('PRAGMA cache_size=1')
        return original(connection,*args)
    m._apply_jobs=spilling
def fault(phase,value):
    wanted='before_jobs_commit' if sys.argv[6]=='before_jobs_commit_flushed' else sys.argv[6]
    if phase==wanted: os._exit(87)
m.RestoreJobPreparation(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).prepare_jobs(
    sys.argv[3],handoff_id=sys.argv[4],temporary_root=sys.argv[5])
'''
    child = subprocess.run([sys.executable,'-B','-c',script,c['project'].book_id,str(c['store']),str(c['package']),
        c['id'],str(c['scratch']),phase,str(c['target'])], cwd=Path.cwd(), capture_output=True,
        text=True, encoding='utf-8', timeout=180)
    assert child.returncode == 87, child.stdout+child.stderr
    if phase == 'before_jobs_commit_flushed':
        assert database.read_bytes() != before
        assert database.with_name(database.name+'-journal').exists()
    intent = c['job_service']._path(c['id'], 'jobs').read_bytes()
    if phase != 'after_jobs_completion':
        with pytest.raises(RestoreHandoffError, match='显式续接'): prepare(c)
    with absent_source(c['root']): receipt = prepare(c, resume=True)
    assert receipt['workbench_jobs_invalidated'] and not receipt['write_activation_allowed']
    assert logical(database) == receipt['database_logical_sha256']
    assert [json.loads(row['original_row_json']) for row in history_rows(database)] == old
    assert c['job_service']._path(c['id'], 'jobs').read_bytes() == intent
    assert not database.with_name(database.name+'-journal').exists()
    assert inventory_tree(c['target']/'book') == book and ledger(c) == c['value']['restoring_record']


def test_extra_database_changes_and_missing_receipts_are_never_healed(prepared):
    c = prepared; result = prepare(c); database = c['target']/'workbench.sqlite'
    original = database.read_bytes()
    for statement in ("UPDATE drafts SET text='changed'", "UPDATE projects SET name='changed'",
                      "UPDATE workbench_restored_job_history SET original_row_json='{}'"):
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(statement); connection.commit()
        changed = database.read_bytes()
        with pytest.raises(RestoreHandoffError, match='限定的任务失效变更'): prepare(c, resume=True)
        assert database.read_bytes() == changed
        database.write_bytes(original)
    for suffix in ('jobs','budget','budget-done'):
        path = c['job_service']._path(c['id'], suffix); raw = path.read_bytes(); path.unlink()
        with pytest.raises((OSError, RuntimeError, ValueError)): prepare(c, resume=True)
        assert not path.exists() and database.read_bytes() == original
        path.write_bytes(raw)
    assert prepare(c, resume=True) == result
    with pytest.raises((OSError, RuntimeError, ValueError)): run(c)
    with pytest.raises((OSError, RuntimeError, ValueError)): prepare_budget(c, resume=True)
    assert database.read_bytes() == original


@pytest.mark.parametrize('suffix', ['jobs','budget-done'])
def test_changed_preparation_proof_before_commit_rolls_back_whole_database(prepared, suffix):
    c = prepared; database = c['target']/'workbench.sqlite'; before = logical(database)
    def fault(phase, value):
        if phase == 'before_jobs_commit': service._path(c['id'], suffix).write_text('{}', encoding='utf-8')
    service = RestoreJobPreparation(c['project'].book_id, machine_store=c['store'], fault_injector=fault)
    with pytest.raises(RestoreHandoffError): prepare(c, service=service)
    assert logical(database) == before and not service._path(c['id'], 'jobs-done').exists()
    assert ledger(c) == c['value']['restoring_record']


def test_rollback_invalidates_prepared_jobs_and_keeps_copy_read_only(prepared):
    c = prepared; prepare(c); before = logical(c['target']/'workbench.sqlite')
    c['handoff'].rollback(handoff_id=c['id'])
    with pytest.raises(RestoreHandoffError): prepare(c, resume=True)
    assert logical(c['target']/'workbench.sqlite') == before
    deny(c['target']/'book')


def test_source_proof_lost_after_commit_cannot_publish_completion(prepared):
    c = prepared
    def fault(phase, value):
        if phase == 'before_jobs_completion':
            service._path(c['id'], 'usage-done').write_text('{}', encoding='utf-8')
    service = RestoreJobPreparation(c['project'].book_id, machine_store=c['store'], fault_injector=fault)
    with pytest.raises(RestoreHandoffError, match='用量保存完成凭据'):
        prepare(c, service=service)
    assert not service._path(c['id'], 'jobs-done').exists()
    assert len(history_rows(c['target']/'workbench.sqlite')) == 5
    with pytest.raises(RestoreHandoffError): prepare(c, resume=True)
    deny(c['target']/'book')
