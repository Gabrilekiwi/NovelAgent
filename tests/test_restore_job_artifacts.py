"""Rearchive actual imported job directories and their immutable references."""
from contextlib import closing, ExitStack, contextmanager
import copy
import json
from pathlib import Path
import sqlite3
import shutil
import uuid

from fastapi.testclient import TestClient
import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy, logical
from tests.test_restore_activation import prepared, activate
from tests.test_restore_budget_preparation import make_financial_source, financial
from tests.test_restore_executions import prepared as jobs_prepared
from tests.test_restore_handoff import restored
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.app import create_app
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_archive_jobs import inspect_archived_jobs
from workbench.backup_archive_references import inspect_archived_references
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_database import _database
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_scope import inspect_backup_scope
from workbench.backup_files import inventory_tree
from workbench.restore_job_artifacts import RestoredJobArtifacts
from workbench.backup_restore_provenance import validate_restored_database, RestoreProvenanceError
from workbench.backup_restore_plan import plan_archived_restore
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.restore_handoff import RestoreHandoff
from workbench.restore_usage import RestoreUsage
from workbench.restore_budget_preparation import RestoreBudgetPreparation
from workbench.restore_jobs import RestoreJobPreparation
from workbench.restore_executions import RestoreExecutionPreparation
from workbench.restore_activation import RestoreActivation
from workbench.restore_scope import load_restore_scope
from tests.test_backup_archive_database import replace_database

REPO=Path(__file__).resolve().parents[1]


def make_job_source(*, job_runtime_relative=None):
    retained={}
    def historical_job(c):
        # A completed pre-migration task fixture with real files. No executor
        # or Provider is run while constructing these historical records.
        with closing(sqlite3.connect(c['db'].path)) as connection:
            connection.row_factory=sqlite3.Row
            row=dict(connection.execute('SELECT * FROM jobs WHERE project_id=? ORDER BY id',
                (c['project'].id,)).fetchone())
            artifact=c['runtime']/row['id'];artifact.mkdir(parents=True)
            text='合成历史任务保留的原输出\n';output=artifact/'正文.txt';output.write_text(text,encoding='utf-8')
            metadata={'outputs':[{'format':'text','path':str(output),'sha256':digest(output.read_bytes())}]}
            stdout=artifact/'stdout.json';stdout.write_text(json.dumps(metadata,ensure_ascii=False,sort_keys=True),encoding='utf-8')
            result={'format':'json','path':str(stdout),'sha256':digest(stdout.read_bytes())}
            connection.execute('UPDATE jobs SET state=?,started_at=?,finished_at=?,config_json=?,result_json=?,revision=revision+1 WHERE id=?',
                ('completed',row['created_at'],row['updated_at'],json.dumps({'runtime_root':str(artifact)}),
                    json.dumps(result),row['id']))
            sequence=connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM job_events WHERE job_id=?',(row['id'],)).fetchone()[0]
            connection.execute('INSERT INTO job_events(job_id,sequence,event_type,payload_json,created_at) VALUES (?,?,?,?,?)',
                (row['id'],sequence,'job.completed',json.dumps({'result':result}),row['updated_at']))
            connection.commit()
        retained.update(job_artifact=artifact,job_output=output,job_stdout=stdout)
    return {**make_financial_source(include_uncertain=False,before_archive=historical_job,
        job_runtime_relative=job_runtime_relative),**retained}


@pytest.fixture(scope='module')
def full_source():return make_job_source()


@pytest.fixture(scope='module')
def artifact_case(full_source):
    scratch=full_source['root'].parent/'job-artifact-scratch';scratch.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        c=bound_copy.__wrapped__(full_source,scratch)
        c=restored.__wrapped__(c,patch);c=pending.__wrapped__(c);c=financial.__wrapped__(c)
        c=budget_prepared.__wrapped__(c);c=jobs_prepared.__wrapped__(c);c=prepared.__wrapped__(c)
        activate(c)
        _capture_first(c)
        yield c
        assert list(scratch.iterdir())==[]


def _capture_first(c):
    database=c['target']/'workbench.sqlite';root=c['target']/'book'
    mapping=next(m for m in c['plan']['root_plan']['roots'] if m['root']=='jobs/'+c['job'].id)
    copied=Path(mapping['destination'])
    assert copied.is_dir() and (copied/'正文.txt').read_bytes()==c['job_output'].read_bytes()
    before=inventory_tree(root);job_before=inventory_tree(copied);sql=logical(database);calls=[]
    def forbidden(*args,**kwargs):calls.append(1);pytest.fail('No Provider or executor authorized')
    app=create_app(database_path=database,start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    archives=REPO/'.tmp'/('jah-archive-'+uuid.uuid4().hex[:8]);archives.mkdir()
    with absent_source(c['root']),absent_source(c['runtime'],temporary_name=c['runtime'].name+'-job-offline'):
        with TestClient(app):
            gate=app.state.maintenance;operation=gate.begin()['operation_id']
            try:
                service=app.state.backups;service.archive_root=archives
                preview=service.preview(c['project'].id,operation_id=operation)
                (REPO/'.tmp/t72-job-artifact-initial-blockers.json').write_text(json.dumps({
                    'blockers':preview['scope']['blockers'],'target':str(c['target']),
                    'machine_store':str(c['store']),'job_id':c['job'].id},ensure_ascii=False,indent=2),encoding='utf-8')
                assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
                assert preview['scope']['inventories']['jobs/'+c['job'].id]['source_root']==str(copied)
                archive=service.capture(c['project'].id,operation_id=operation,
                    expected_preview_sha256=preview['preview_sha256'])
                c.update(rebackup=Path(archive['path']),preview=preview)
            finally:gate.finish(operation)
        with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
            absent.enter_context(absent_source(c['target'],temporary_name=c['target'].name+'-offline'))
            absent.enter_context(absent_source(c['store'],temporary_name=c['store'].name+'-machine-offline'))
            offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
            offline.setattr('workbench.book_ownership._record',forbidden)
            verified=verify_archived_target(archive['path'],project_id=c['project'].id,book_id=c['project'].book_id,
                temporary_root=c['scratch'])
            assert verified['offline_data_verified'] and not verified['blockers']
            assert verified['checks']['jobs']['jobs_verified'] and verified['checks']['references']['references_verified']
            c['verified']=verified
    assert inventory_tree(root)==before and inventory_tree(copied)==job_before and logical(database)==sql and not calls


def test_started_imported_job_artifacts_rearchive_offline_without_ancestral_roots(artifact_case):
    assert artifact_case['verified']['offline_data_verified']
    assert not artifact_case['verified']['blockers']


def scope(c):
    database=c['target']/'workbench.sqlite'
    app=create_app(database_path=database,start_worker=False,auto_import_active=False,
        chapter_publication_machine_store=c['store'])
    with TestClient(app):
        gate=app.state.maintenance;operation=gate.begin()['operation_id']
        try:return app.state.backups.preview(c['project'].id,operation_id=operation)['scope']
        finally:gate.finish(operation)


@contextmanager
def damaged_copy(root):
    # Preserve the real fixture by renaming it; only the independent copy is
    # damaged. Keep that copy for diagnostics instead of deleting any tree.
    root=Path(root);checked=(REPO/'.tmp').resolve();assert root.resolve().is_relative_to(checked)
    offline=root.name+'-intact-'+uuid.uuid4().hex[:8]
    with absent_source(root,temporary_name=offline):
        shutil.copytree(root.parent/offline,root)
        try:yield root
        finally:
            retained=root.parent/(root.name+'-damaged-'+uuid.uuid4().hex[:8])
            assert root.resolve().is_relative_to(checked) and retained.parent==root.parent
            root.rename(retained)


@pytest.mark.parametrize('damage',['missing-file','changed-file','extra-file','missing-root','changed-stdout'])
def test_live_preflight_rejects_actual_historical_artifact_damage(artifact_case,damage):
    c=artifact_case;key='jobs/'+c['job'].id
    root=Path(c['preview']['scope']['inventories'][key]['source_root']);before=inventory_tree(root)
    sql=logical(c['target']/'workbench.sqlite')
    if damage=='missing-root':
        with absent_source(root,temporary_name=root.name+'-missing-'+uuid.uuid4().hex[:8]):result=scope(c)
    else:
        with damaged_copy(root):
            if damage=='missing-file':(root/'正文.txt').unlink()
            elif damage=='changed-file':(root/'正文.txt').write_text('changed',encoding='utf-8')
            elif damage=='extra-file':(root/'extra.txt').write_text('new counterfeit artifact')
            else:(root/'stdout.json').write_text('{}')
            result=scope(c)
    assert not result['scope_checks_passed']
    assert 'historical_job_artifacts_unverified' in {item['code'] for item in result['blockers']}
    assert inventory_tree(root)==before and logical(c['target']/'workbench.sqlite')==sql


def test_counterfeit_artifact_in_fresh_namespace_cannot_replace_historical_copy(artifact_case):
    c=artifact_case;old=c['target']/'jobs'/c['job'].id;new=c['target']/'next-jobs'/c['job'].id
    assert not new.exists();new.parent.mkdir(exist_ok=True);shutil.copytree(old,new)
    try:
        result=scope(c)
        assert not result['scope_checks_passed']
        assert 'historical_job_artifact_root_collision' in {item['code'] for item in result['blockers']}
        assert result['inventories']['jobs/'+c['job'].id]['source_root']==str(old)
    finally:
        retained=c['target'].parent/('counterfeit-'+uuid.uuid4().hex[:8])
        assert new.resolve().is_relative_to((REPO/'.tmp').resolve()) and retained.parent==c['target'].parent
        new.rename(retained)


@pytest.mark.parametrize('damage',['config','result','event-payload','event-delete','event-append'])
def test_offline_check_rejects_altered_actual_db_even_with_rehashed_archive(artifact_case,tmp_path,damage):
    c=artifact_case;package=tmp_path/c['rebackup'].name;shutil.copytree(c['rebackup'],package)
    def mutate(connection):
        if damage in {'config','result'}:
            field='config_json' if damage=='config' else 'result_json'
            connection.execute('UPDATE jobs SET '+field+'=? WHERE id=?',('{}',c['job'].id))
        elif damage=='event-payload':connection.execute("UPDATE job_events SET payload_json='{}' WHERE job_id=? AND sequence=2",(c['job'].id,))
        elif damage=='event-delete':connection.execute('DELETE FROM job_events WHERE job_id=? AND sequence=2',(c['job'].id,))
        else:connection.execute('INSERT INTO job_events(job_id,sequence,event_type,payload_json,created_at) VALUES (?,?,?,?,?)',
            (c['job'].id,3,'job.completed','{}','changed'))
    replace_database({**c,'package':package},mutate)
    for inspect in (inspect_archived_jobs,inspect_archived_references):
        with pytest.raises(BackupArchiveError,match='历史'):
            inspect(package,project_id=c['project'].id,book_id=c['project'].book_id,temporary_root=c['scratch'])


def test_only_bound_original_metadata_can_use_an_ancestral_coordinate(artifact_case):
    c=artifact_case;scope_=c['preview']['scope'];proof=scope_['restored_provenance'];trees=scope_['inventories']
    resolver=RestoredJobArtifacts(c['target']/'book',c['project'].book_id,proof)
    source=c['job_output'];destination=c['target']/'jobs'/c['job'].id/'正文.txt'
    original=next(item['original'] for item in proof['jobs_intent']['history'] if item['id']==c['job'].id)
    row=('job',c['job'].id,'result_json',original['result_json'])
    assert resolver.path(source,evidence=row,inventories=trees)==destination
    assert resolver.path(source,evidence=('file','jobs/'+c['job'].id,'stdout.json'),inventories=trees)==destination
    event=next(row for row in proof['jobs_intent']['event_history'] if row['job_id']==c['job'].id and row['sequence']==2)
    assert resolver.path(source,evidence=('event',event),inventories=trees)==destination
    for evidence in (None,('job','job-new','result_json',original['result_json']),
        ('job',c['job'].id,'result_json','newly-authored'),('event',{**event,'payload_json':'{}'}),
        ('file','story_project','new.json')):
        assert resolver.path(source,evidence=evidence,inventories=trees)==source
    changed=copy.deepcopy(trees);changed['jobs/'+c['job'].id]['files']['stdout.json']['sha256']='changed'
    assert resolver.path(source,evidence=('file','jobs/'+c['job'].id,'stdout.json'),inventories=changed)==source


def test_new_authored_run_cannot_borrow_historical_paths_in_live_preflight(artifact_case):
    c=artifact_case;run=c['target']/'book/.novelagent/runtime/runs'/('new-'+uuid.uuid4().hex[:8]+'.json')
    run.write_text(json.dumps({'outputs':[{'format':'text','path':str(c['job_output']),
        'sha256':digest(c['job_output'].read_bytes())}]}),encoding='utf-8')
    try:
        result=scope(c)
        assert not result['scope_checks_passed']
        assert any(item['code']=='unknown_external_file_reference' and run.name in item['reference'] for item in result['blockers'])
    finally:run.unlink()


def test_actual_second_activation_rearchive_retains_jobs_and_events_with_all_roots_absent(artifact_case):
    c=artifact_case;book_id=c['project'].book_id;project_id=c['project'].id
    package=c['rebackup'];target=REPO/'.tmp'/('jah-second-'+uuid.uuid4().hex[:8]);calls=[]
    def forbidden(*args,**kwargs):calls.append(1);pytest.fail('No executor or Provider authorized')
    first_book=inventory_tree(c['target']/'book');first_job=inventory_tree(c['target']/'jobs'/c['job'].id)
    first_sql=logical(c['target']/'workbench.sqlite')
    with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
        for root in (c['root'],c['runtime'],c['target'],c['store']):
            absent.enter_context(absent_source(root,temporary_name=root.name+'-second-'+uuid.uuid4().hex[:8]))
        offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
        offline.setattr('workbench.book_ownership._record',forbidden)
        plan=plan_archived_restore(package,project_id=project_id,book_id=book_id,
            destination=target,temporary_root=c['scratch'])
        assert plan['plan_ready']
        materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
        bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
    prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
    handoff=RestoreHandoff(book_id,machine_store=c['store'])
    preview=handoff.preview(package,destination=target,temporary_root=c['scratch'])
    value=handoff.begin(package,destination=target,temporary_root=c['scratch'],expected_preview_sha256=preview['intent_sha256'])
    hid=value['handoff_id'];usage=RestoreUsage(book_id,machine_store=c['store']).capture(handoff_id=hid)
    assert usage['settled']
    RestoreBudgetPreparation(book_id,machine_store=c['store']).prepare_budget(package,handoff_id=hid,temporary_root=c['scratch'])
    jobs=RestoreJobPreparation(book_id,machine_store=c['store']).prepare_jobs(package,handoff_id=hid,temporary_root=c['scratch'])
    assert jobs['scope_generation']==2 and jobs['original_events_preserved']
    RestoreExecutionPreparation(book_id,machine_store=c['store']).prepare_executions(package,handoff_id=hid,temporary_root=c['scratch'])
    activation=RestoreActivation(book_id,machine_store=c['store']).activate(package,handoff_id=hid,temporary_root=c['scratch'])
    assert activation['target_writable'] and load_restore_scope(target/'workbench.sqlite',machine_store=c['store']).active
    imported=target/'jobs'/c['job'].id
    assert (imported/'stdout.json').read_bytes()==c['job_stdout'].read_bytes()
    assert (imported/'正文.txt').read_bytes()==c['job_output'].read_bytes()
    second_book=inventory_tree(target/'book');second_job=inventory_tree(imported);second_sql=logical(target/'workbench.sqlite')
    archives=REPO/'.tmp'/('jah-again-'+uuid.uuid4().hex[:8]);archives.mkdir()
    app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    with TestClient(app):
        assert app.state.supervisor._thread is None
        gate=app.state.maintenance;operation=gate.begin()['operation_id']
        try:
            service=app.state.backups;service.archive_root=archives
            preview=service.preview(project_id,operation_id=operation)
            assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
            proof=preview['scope']['restored_provenance'];old=proof['plan']['source_restore_provenance']
            assert proof['jobs_intent']['event_history']==old['jobs_intent']['event_history']
            assert preview['scope']['inventories']['jobs/'+c['job'].id]['source_root']==str(imported)
            archive=service.capture(project_id,operation_id=operation,expected_preview_sha256=preview['preview_sha256'])
        finally:gate.finish(operation)
    with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
        for root in (c['root'],c['runtime'],c['target'],target,c['store']):
            absent.enter_context(absent_source(root,temporary_name=root.name+'-all-offline-'+uuid.uuid4().hex[:8]))
        offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
        offline.setattr('workbench.book_ownership._record',forbidden)
        verified=verify_archived_target(archive['path'],project_id=project_id,book_id=book_id,temporary_root=c['scratch'])
        assert verified['offline_data_verified'] and not verified['blockers']
        assert verified['checks']['references']['references_verified'] and verified['checks']['jobs']['jobs_verified']
    assert inventory_tree(c['target']/'book')==first_book and inventory_tree(c['target']/'jobs'/c['job'].id)==first_job
    assert logical(c['target']/'workbench.sqlite')==first_sql
    assert inventory_tree(target/'book')==second_book and inventory_tree(imported)==second_job
    assert logical(target/'workbench.sqlite')==second_sql and not calls
    (REPO/'.tmp/t72-job-artifact-browser-case.json').write_text(json.dumps({'target':str(target),
        'machine_store':str(c['store']),'job_id':c['job'].id,'project_id':project_id,'book_id':book_id,
        'archive':archive['path'],'scope_generation':2},ensure_ascii=False,indent=2),encoding='utf-8')
