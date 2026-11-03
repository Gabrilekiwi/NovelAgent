"""Actual repeated restores retain every full job row and never resume imports."""
from contextlib import closing, ExitStack
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import logical
from tests.test_restore_handoff_generation import (full_source, rebackup_case, second_copy,
    private_temporary_directory, next_handoff, begin)
from tests.test_restore_scope_generation import contents
from workbench.app import create_app
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_files import inventory_tree
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_plan import plan_archived_restore
from workbench.backup_restore_provenance import validate_restored_database
from workbench.backup_restore_roots import bind_restored_roots
from workbench.database import WorkbenchDatabase
from workbench.jobs import JobError, _require_job_execution_contract
from workbench.restore_budget_preparation import RestoreBudgetPreparation
from workbench.restore_activation import RestoreActivation
from workbench.restore_executions import RestoreExecutionPreparation
from workbench.restore_handoff import RestoreHandoff, RestoreHandoffError
from workbench.restore_job_history import (FIRST_TABLE, GENERATION_TABLE, INVALIDATED_CONTRACT,
    invalidated_row, validate_retained_tables, RestoreJobHistoryError)
from workbench.restore_jobs import RestoreJobPreparation
from workbench.restore_scope import load_restore_scope, RestoreScopeError
from workbench.restore_usage import RestoreUsage

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(next_handoff):
    c = next_handoff; begin(c); handoff_id = c['value2']['handoff_id']
    usage = RestoreUsage(c['project'].book_id, machine_store=c['store']).capture(handoff_id=handoff_id)
    assert usage['settled'] and usage['usage']['summary']['attempt_count'] == 3
    budget = RestoreBudgetPreparation(c['project'].book_id, machine_store=c['store']).prepare_budget(
        c['package'], handoff_id=handoff_id, temporary_root=c['scratch'])
    return {**c, 'second_id':handoff_id, 'second_budget':budget,
        'jobs_service':RestoreJobPreparation(c['project'].book_id, machine_store=c['store'])}


def prepare(c, *, resume=False, service=None):
    service = service or c['jobs_service']
    method = service.resume_jobs if resume else service.prepare_jobs
    return method(c['package'], handoff_id=c['second_id'], temporary_root=c['scratch'])


def test_actual_second_jobs_preserve_complete_first_history_and_read_only_startup(prepared):
    c = prepared; database = c['target']/'workbench.sqlite'
    before = contents(database); book = inventory_tree(c['target']/'book')
    source_book = inventory_tree(c['source_root']); source_sql = logical(c['source_database'])
    result = prepare(c)
    intent = json.loads(c['jobs_service']._path(c['second_id'], 'jobs').read_bytes())
    assert intent['schema_version'] == 'restore_job_preparation_v2'
    assert result['schema_version'] == 'restore_jobs_prepared_v2'
    assert result['scope_generation'] == 2 and result['prior_generations_preserved']
    assert intent['source_provenance_sha256'] == c['plan']['source_restore_provenance']['provenance_sha256']
    expected = copy.deepcopy(before)
    expected['jobs'] = [invalidated_row(row, intent) for row in before['jobs']]
    expected[GENERATION_TABLE] = [{'scope_generation':2, 'job_id':item['id'],
        'handoff_id':c['second_id'], 'original_row_json':json.dumps(item['original'],ensure_ascii=False,
            sort_keys=True,separators=(',',':')), 'row_sha256':item['original_sha256']} for item in intent['history']]
    # Compare all tables, not only the newly created history rows.
    actual = contents(database)
    for table in expected:
        assert sorted(actual[table], key=lambda row:json.dumps(row,sort_keys=True)) == sorted(
            expected[table], key=lambda row:json.dumps(row,sort_keys=True)), table
    assert set(actual) == set(expected)
    assert actual[FIRST_TABLE] == before[FIRST_TABLE]
    for row in actual['jobs']:
        with pytest.raises(JobError, match='契约不兼容'): _require_job_execution_contract(row)
    saved = database.read_bytes()
    with absent_source(c['source_target'],temporary_name=c['source_target'].name+'-jobs-offline'), absent_source(c['root']):
        assert prepare(c,resume=True) == result and prepare(c) == result
        calls = []
        def forbidden(*args, **kwargs): calls.append(1); pytest.fail('Imported job executed')
        for _ in range(2):
            app = create_app(database_path=database,start_worker=True,auto_import_active=True,
                chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
                chapter_fact_extraction_executor_factory=forbidden,
                reviewed_chapter_candidate_executor_factory=forbidden,
                reviewed_chapter_fact_extraction_executor_factory=forbidden)
            with TestClient(app) as client:
                assert client.get('/api/restore-scope').json()['read_only']
                response = client.get('/api/jobs/'+c['job'].id)
                assert response.status_code == 200 and not response.json()['job']['execution_compatible']
                assert client.get('/api/jobs/'+c['other_job'].id).status_code == 409
                assert not app.state.jobs.resume_ready(c['job'].id)
                assert app.state.jobs.claim_next() is None and app.state.supervisor._thread is None
        assert not calls
    assert database.read_bytes() == saved and inventory_tree(c['target']/'book') == book
    assert inventory_tree(c['source_root']) == source_book and logical(c['source_database']) == source_sql
    assert load_restore_scope(database,machine_store=c['store']).read_only
    (REPO/'.tmp/t72-job-generation-case.json').write_text(json.dumps({
        'target':str(c['target']), 'machine_store':str(c['store']), 'book_id':c['project'].book_id,
        'project_id':c['project'].id, 'job_id':c['job'].id, 'other_job_id':c['other_job'].id,
        'handoff_id':c['second_id']},ensure_ascii=False,indent=2),encoding='utf-8')


@pytest.mark.parametrize('phase', ['after_jobs_intent','before_jobs_commit_flushed',
    'after_jobs_commit','before_jobs_completion','after_jobs_completion'])
def test_actual_second_jobs_process_death_requires_original_intent_and_preserves_all_history(prepared, phase):
    c = prepared; database = c['target']/'workbench.sqlite'
    before = contents(database); book = inventory_tree(c['target']/'book'); original_bytes = database.read_bytes()
    code = '''import os,sys
from pathlib import Path
from workbench import restore_jobs as m
target=Path(sys.argv[7])/'workbench.sqlite'
if sys.argv[6]=='before_jobs_commit_flushed':
    original=m._apply_job_intent
    def apply(connection,intent):
        if Path(connection.execute('PRAGMA database_list').fetchone()[2])==target:
            connection.execute('PRAGMA cache_size=1')
        return original(connection,intent)
    m._apply_job_intent=apply
def fault(phase,value):
    wanted='before_jobs_commit' if sys.argv[6]=='before_jobs_commit_flushed' else sys.argv[6]
    if phase==wanted:os._exit(93)
m.RestoreJobPreparation(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).prepare_jobs(
    sys.argv[3],handoff_id=sys.argv[4],temporary_root=sys.argv[5])
'''
    child = subprocess.run([sys.executable,'-B','-c',code,c['project'].book_id,str(c['store']),
        str(c['package']),c['second_id'],str(c['scratch']),phase,str(c['target'])],cwd=REPO,
        capture_output=True,text=True,encoding='utf-8',timeout=240,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert child.returncode == 93, child.stdout+child.stderr
    intent_path = c['jobs_service']._path(c['second_id'],'jobs'); intent = intent_path.read_bytes()
    if phase == 'before_jobs_commit_flushed':
        assert database.read_bytes() != original_bytes and Path(str(database)+'-journal').exists()
    if phase != 'after_jobs_completion':
        with pytest.raises(RestoreHandoffError,match='显式续接'): prepare(c)
    with absent_source(c['source_target'],temporary_name=c['source_target'].name+'-crash-offline'), absent_source(c['root']):
        result = prepare(c,resume=True)
    assert intent_path.read_bytes() == intent
    assert logical(database) == result['database_logical_sha256']
    assert contents(database)[FIRST_TABLE] == before[FIRST_TABLE]
    assert not Path(str(database)+'-journal').exists() and inventory_tree(c['target']/'book') == book


@pytest.mark.parametrize('damage', ['first_history','new_history','event','intent','done'])
def test_second_jobs_changed_evidence_is_rejected_without_healing(prepared, damage):
    c = prepared; prepare(c); database = c['target']/'workbench.sqlite'
    if damage in {'first_history','new_history','event'}:
        with closing(sqlite3.connect(database)) as connection:
            if damage == 'event': connection.execute("UPDATE job_events SET payload_json='{}'")
            else:
                table = FIRST_TABLE if damage == 'first_history' else GENERATION_TABLE
                connection.execute('UPDATE '+table+" SET original_row_json='{}'")
            connection.commit()
    else:
        suffix = 'jobs' if damage == 'intent' else 'jobs-done'
        path = c['jobs_service']._path(c['second_id'],suffix)
        if damage == 'intent': path.unlink()
        else: path.write_text('{}',encoding='utf-8')
    before = database.read_bytes(); book = inventory_tree(c['target']/'book')
    with pytest.raises((RuntimeError,ValueError,OSError)): prepare(c,resume=True)
    assert database.read_bytes() == before and inventory_tree(c['target']/'book') == book


def test_second_jobs_original_intent_loss_before_commit_rolls_back_generation_only(prepared):
    c = prepared; database = c['target']/'workbench.sqlite'; before = logical(database)
    def fault(phase,value):
        if phase == 'before_jobs_commit': service._path(c['second_id'],'jobs').unlink()
    service = RestoreJobPreparation(c['project'].book_id,machine_store=c['store'],fault_injector=fault)
    with pytest.raises((RuntimeError,ValueError,OSError)): prepare(c,service=service)
    assert logical(database) == before and GENERATION_TABLE not in contents(database)
    assert not service._path(c['second_id'],'jobs-done').exists()


def test_new_history_table_alone_prevents_scope_loss_from_becoming_ordinary_database(prepared):
    c = prepared; prepare(c); database = c['target']/'workbench.sqlite'
    for name in ('restore-intent.json','restore-plan.json','write-qualification.json','workbench.sqlite.restore-scope.json'):
        path = c['target']/name
        if path.exists(): path.unlink()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('DROP TABLE workbench_restore_scope')
        connection.execute('DROP TABLE '+FIRST_TABLE); connection.commit()
    isolated_store = c['target']/'empty-machine'; isolated_store.mkdir()
    with pytest.raises(RestoreScopeError): WorkbenchDatabase(database,machine_store=isolated_store)


def test_actual_second_activation_rearchive_and_third_jobs_keep_every_generation_offline(prepared,monkeypatch):
    c = prepared; jobs = prepare(c); database = c['target']/'workbench.sqlite'
    executions = RestoreExecutionPreparation(c['project'].book_id,machine_store=c['store']).prepare_executions(
        c['package'],handoff_id=c['second_id'],temporary_root=c['scratch'])
    activation = RestoreActivation(c['project'].book_id,machine_store=c['store'])
    result = activation.activate(c['package'],handoff_id=c['second_id'],temporary_root=c['scratch'])
    assert result['target_writable'] and load_restore_scope(database,machine_store=c['store']).active
    assert activation.activate(c['package'],handoff_id=c['second_id'],temporary_root=c['scratch'],resume=True) == result
    calls = []
    def forbidden(*args, **kwargs): calls.append(1); pytest.fail('Archive or startup invoked an executor')
    archives = REPO/'.tmp'/('jg-archive-'+uuid.uuid4().hex[:8]); archives.mkdir()
    app = create_app(database_path=database,start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    book = inventory_tree(c['target']/'book'); prior = contents(database)
    with TestClient(app) as client:
        assert client.get('/api/restore-scope').json()['state'] == 'active'
        assert app.state.supervisor._thread is None
        gate = app.state.maintenance; operation = gate.begin()['operation_id']
        try:
            service = app.state.backups; service.archive_root = archives
            preview = service.preview(c['project'].id,operation_id=operation)
            assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
            proof = preview['scope']['restored_provenance']
            assert proof['jobs_intent']['schema_version'] == 'restore_job_preparation_v2'
            assert proof['jobs_done'] == jobs
            archive = service.capture(c['project'].id,operation_id=operation,
                expected_preview_sha256=preview['preview_sha256'])
        finally: gate.finish(operation)
    assert inventory_tree(c['target']/'book') == book and not calls
    package = Path(archive['path']); target = REPO/'.tmp'/('jg-third-'+uuid.uuid4().hex[:8])
    with ExitStack() as absent:
        for path in (c['root'],c['source_target'],c['target'],c['store']):
            absent.enter_context(absent_source(path,temporary_name=path.name+'-third-offline'))
        with pytest.MonkeyPatch.context() as offline:
            offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
            offline.setattr('workbench.book_ownership._record',forbidden)
            verified = verify_archived_target(package,project_id=c['project'].id,book_id=c['project'].book_id,
                temporary_root=c['scratch'])
            assert verified['offline_data_verified'] and not verified['blockers']
            assert verified['checks']['database']['restored_provenance']['original_job_count'] == len(prior['jobs'])
            plan = plan_archived_restore(package,project_id=c['project'].id,book_id=c['project'].book_id,
                destination=target,temporary_root=c['scratch'])
            assert plan['plan_ready']
            materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
            bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
        # Local target startup must probe its independent machine guard. The
        # ancestral sources and machine directory remain physically absent.
        prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
    handoff = RestoreHandoff(c['project'].book_id,machine_store=c['store'])
    preview = handoff.preview(package,destination=target,temporary_root=c['scratch'])
    value = handoff.begin(package,destination=target,temporary_root=c['scratch'],
        expected_preview_sha256=preview['intent_sha256'])
    handoff_id = value['handoff_id']
    RestoreUsage(c['project'].book_id,machine_store=c['store']).capture(handoff_id=handoff_id)
    RestoreBudgetPreparation(c['project'].book_id,machine_store=c['store']).prepare_budget(
        package,handoff_id=handoff_id,temporary_root=c['scratch'])
    third = RestoreJobPreparation(c['project'].book_id,machine_store=c['store']).prepare_jobs(
        package,handoff_id=handoff_id,temporary_root=c['scratch'])
    assert third['scope_generation'] == 3 and third['prior_generations_preserved']
    current = contents(target/'workbench.sqlite')
    assert current[FIRST_TABLE] == prior[FIRST_TABLE]
    assert [row for row in current[GENERATION_TABLE] if row['scope_generation'] == 2] == prior[GENERATION_TABLE]
    assert len([row for row in current[GENERATION_TABLE] if row['scope_generation'] == 3]) == len(prior['jobs'])
    assert current['job_events'] == prior['job_events'] and not calls
    assert load_restore_scope(target/'workbench.sqlite',machine_store=c['store']).read_only


