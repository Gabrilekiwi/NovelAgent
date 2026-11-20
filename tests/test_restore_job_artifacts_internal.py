"""Actual project-internal job artifacts survive qualified repeated restores."""
from contextlib import ExitStack,contextmanager
import json
from pathlib import Path
import shutil
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy, logical
from tests.test_restore_job_artifacts import (make_job_source,prepared,restored,pending,financial,
    budget_prepared,jobs_prepared,scope)
from workbench.app import create_app
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_files import inventory_tree
from workbench.backup_restore_plan import plan_archived_restore
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.restore_activation import RestoreActivation
from workbench.restore_handoff import RestoreHandoff
from workbench.restore_usage import RestoreUsage
from workbench.restore_budget_preparation import RestoreBudgetPreparation
from workbench.restore_jobs import RestoreJobPreparation
from workbench.restore_executions import RestoreExecutionPreparation
from workbench.restore_scope import load_restore_scope
from workbench.restore_execution_policy import require_restored_execution_paths, RestoredExecutionBlocked

REPO=Path(__file__).resolve().parents[1]


def forbidden(*args,**kwargs):pytest.fail('No executor or Provider authorized')


@contextmanager
def isolated_artifact(root,*,missing=False):
    # Store both original and damaged trees outside the book. Otherwise the
    # diagnostic copy itself would alter the full-book backup inventory.
    checked=(REPO/'.tmp').resolve();root=Path(root)
    saved=checked/('internal-job-intact-'+uuid.uuid4().hex)
    retained=checked/('internal-job-damaged-'+uuid.uuid4().hex)
    assert root.resolve().is_relative_to(checked) and not root.is_symlink()
    assert saved.parent==checked and retained.parent==checked and not saved.exists() and not retained.exists()
    root.rename(saved)
    try:
        if not missing:shutil.copytree(saved,root)
        yield root
    finally:
        if root.exists():
            assert root.resolve().is_relative_to(checked)
            root.rename(retained)
        saved.rename(root)


def archive(c,target):
    app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    output=REPO/'.tmp'/('jai-archive-'+uuid.uuid4().hex[:8]);output.mkdir()
    with TestClient(app):
        assert app.state.supervisor._thread is None
        gate=app.state.maintenance;operation=gate.begin()['operation_id']
        try:
            service=app.state.backups;service.archive_root=output
            preview=service.preview(c['project'].id,operation_id=operation)
            (REPO/'.tmp/t72-internal-job-last-preview.json').write_text(json.dumps({'target':str(target),
                'machine_store':str(c['store']),'job_id':c['job'].id,'book_id':c['project'].book_id,
                'project_id':c['project'].id,'blockers':preview['scope']['blockers']},ensure_ascii=False,indent=2),encoding='utf-8')
            assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
            assert 'jobs/'+c['job'].id not in preview['scope']['inventories']
            result=service.capture(c['project'].id,operation_id=operation,expected_preview_sha256=preview['preview_sha256'])
        finally:gate.finish(operation)
    return Path(result['path']),preview


@pytest.fixture(scope='module')
def internal_case():
    source=make_job_source(job_runtime_relative='.novelagent/runtime/jobs')
    scratch=source['root'].parent/'internal-job-scratch';scratch.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        c=bound_copy.__wrapped__(source,scratch);c=restored.__wrapped__(c,patch)
        c=pending.__wrapped__(c);c=financial.__wrapped__(c);c=budget_prepared.__wrapped__(c)
        c=jobs_prepared.__wrapped__(c);c=prepared.__wrapped__(c)
        c['activation_service'].activate(c['package'],handoff_id=c['id'],temporary_root=scratch)
        relative=c['job_artifact'].relative_to(c['root']);artifact=c['target']/'book'/relative
        assert artifact.is_dir() and (artifact/'stdout.json').read_bytes()==c['job_stdout'].read_bytes()
        assert (artifact/'正文.txt').read_bytes()==c['job_output'].read_bytes()
        assert all(mapping['root']=='story_project' for mapping in c['plan']['root_plan']['roots'])
        with absent_source(c['root']):package,preview=archive(c,c['target'])
        yield {**c,'internal_relative':relative,'internal_artifact':artifact,'rebackup':package,'preview':preview}
        assert not list(scratch.iterdir())


def test_first_internal_artifacts_rearchive_offline_without_external_root(internal_case):
    c=internal_case;book=inventory_tree(c['target']/'book');sql=logical(c['target']/'workbench.sqlite')
    with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
        for root in (c['root'],c['target'],c['store']):
            absent.enter_context(absent_source(root,temporary_name=root.name+'-internal-offline-'+uuid.uuid4().hex[:8]))
        offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
        offline.setattr('workbench.book_ownership._record',forbidden)
        verified=verify_archived_target(c['rebackup'],project_id=c['project'].id,book_id=c['project'].book_id,temporary_root=c['scratch'])
        assert verified['offline_data_verified'] and not verified['blockers']
        artifact=verified['checks']['jobs']['jobs'][0]['artifact']
        assert artifact=={'root':'story_project','path':c['internal_relative'].as_posix()}
    assert inventory_tree(c['target']/'book')==book and logical(c['target']/'workbench.sqlite')==sql


def test_first_internal_imported_execution_namespace_is_blocked(internal_case):
    c=internal_case
    for path in (c['internal_artifact'],c['internal_artifact']/'stdout.json'):
        with pytest.raises(RestoredExecutionBlocked):require_restored_execution_paths(path,require_active=True)
    require_restored_execution_paths(c['target']/'next-jobs'/'job_new',require_active=True)


def make_second_internal_case(c):
    book_id=c['project'].book_id;project_id=c['project'].id
    package=c['rebackup'];target=REPO/'.tmp'/('jai-second-'+uuid.uuid4().hex[:8])
    source_book=inventory_tree(c['target']/'book');source_sql=logical(c['target']/'workbench.sqlite')
    with absent_source(c['root']),absent_source(c['target'],temporary_name=c['target'].name+'-copy-offline'):
        plan=plan_archived_restore(package,project_id=project_id,book_id=book_id,destination=target,temporary_root=c['scratch'])
        assert plan['plan_ready'] and not Path(plan['root_plan']['task_runtime_root']).is_relative_to(target/'book')
        materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
        bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
        prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
    handoff=RestoreHandoff(book_id,machine_store=c['store']);preview=handoff.preview(package,destination=target,temporary_root=c['scratch'])
    value=handoff.begin(package,destination=target,temporary_root=c['scratch'],expected_preview_sha256=preview['intent_sha256'])
    hid=value['handoff_id'];usage=RestoreUsage(book_id,machine_store=c['store']).capture(handoff_id=hid);assert usage['settled']
    RestoreBudgetPreparation(book_id,machine_store=c['store']).prepare_budget(package,handoff_id=hid,temporary_root=c['scratch'])
    RestoreJobPreparation(book_id,machine_store=c['store']).prepare_jobs(package,handoff_id=hid,temporary_root=c['scratch'])
    RestoreExecutionPreparation(book_id,machine_store=c['store']).prepare_executions(package,handoff_id=hid,temporary_root=c['scratch'])
    activation=RestoreActivation(book_id,machine_store=c['store'])
    assert activation.activate(package,handoff_id=hid,temporary_root=c['scratch'])['target_writable']
    assert load_restore_scope(target/'workbench.sqlite',machine_store=c['store']).document['scope_generation']==2
    artifact=target/'book'/c['internal_relative'];assert artifact.is_dir()
    assert (artifact/'stdout.json').read_bytes()==c['job_stdout'].read_bytes()
    return {**c,'previous':c,'target':target,'package':package,'plan':plan,'id':hid,
        'activation_service':activation,'internal_artifact':artifact,
        'source_book_before':source_book,'source_sql_before':source_sql}


@pytest.fixture(scope='module')
def second_case(internal_case):return make_second_internal_case(internal_case)


def test_actual_second_internal_restore_rearchives_with_original_coordinates_and_no_ancestors(second_case):
    c=second_case;target=c['target'];artifact=c['internal_artifact']
    book_id=c['project'].book_id;project_id=c['project'].id
    before=inventory_tree(target/'book');sql=logical(target/'workbench.sqlite')
    new_package,new_preview=archive(c,target)
    proof=new_preview['scope']['restored_provenance']
    assert proof['jobs_intent']['event_history']==proof['plan']['source_restore_provenance']['jobs_intent']['event_history']
    for path in (artifact,artifact/'stdout.json'):
        with pytest.raises(RestoredExecutionBlocked):require_restored_execution_paths(path,require_active=True)
    require_restored_execution_paths(target/'next-jobs'/'job_new',require_active=True)
    with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
        for root in (c['root'],c['previous']['target'],target,c['store']):
            absent.enter_context(absent_source(root,temporary_name=root.name+'-internal-all-offline-'+uuid.uuid4().hex[:8]))
        offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
        offline.setattr('workbench.book_ownership._record',forbidden)
        verified=verify_archived_target(new_package,project_id=project_id,book_id=book_id,temporary_root=c['scratch'])
        assert verified['offline_data_verified'] and not verified['blockers']
        assert verified['checks']['jobs']['jobs'][0]['artifact']=={'root':'story_project','path':c['internal_relative'].as_posix()}
    previous=c['previous']['target']
    assert inventory_tree(previous/'book')==c['source_book_before'] and logical(previous/'workbench.sqlite')==c['source_sql_before']
    assert inventory_tree(target/'book')==before and logical(target/'workbench.sqlite')==sql
    (REPO/'.tmp/t72-internal-job-browser-case.json').write_text(json.dumps({'target':str(target),
        'machine_store':str(c['store']),'job_id':c['job'].id,'project_id':project_id,'book_id':book_id,
        'internal_relative':c['internal_relative'].as_posix(),'archive':str(new_package),'scope_generation':2},indent=2),encoding='utf-8')


@pytest.mark.parametrize('damage',['missing-root','missing-file','changed-file','extra-file'])
def test_second_internal_artifact_damage_blocks_live_backup(second_case,damage):
    c=second_case;artifact=c['internal_artifact']
    before=inventory_tree(c['target']/'book');sql=logical(c['target']/'workbench.sqlite')
    if damage=='missing-root':
        with isolated_artifact(artifact,missing=True):result=scope(c)
    else:
        with isolated_artifact(artifact):
            if damage=='missing-file':(artifact/'正文.txt').unlink()
            elif damage=='changed-file':(artifact/'正文.txt').write_text('改变旧任务输出',encoding='utf-8')
            else:(artifact/'extra.txt').write_text('未经原复制清单证明的新产物',encoding='utf-8')
            result=scope(c)
    assert not result['scope_checks_passed']
    assert 'historical_job_artifacts_unverified' in {item['code'] for item in result['blockers']}
    assert inventory_tree(c['target']/'book')==before and logical(c['target']/'workbench.sqlite')==sql


@pytest.mark.parametrize('lost',['restore-plan.json','machine-jobs-intent'])
def test_missing_bound_namespace_proof_blocks_new_execution_without_repair(second_case,lost):
    c=second_case
    if lost=='restore-plan.json':path=c['target']/lost
    else:
        from workbench.book_ownership import BookOwnership
        path=BookOwnership(c['project'].book_id,machine_store=c['store']).directory/('handoff-'+c['id']+'-jobs.json')
    assert path.resolve().is_relative_to((REPO/'.tmp').resolve())
    raw=path.read_bytes();before=inventory_tree(c['target']/'book');sql=logical(c['target']/'workbench.sqlite')
    hidden=path.with_name(path.name+'-absent-'+uuid.uuid4().hex[:8]);path.rename(hidden)
    try:
        with pytest.raises(RestoredExecutionBlocked):
            require_restored_execution_paths(c['target']/'next-jobs/job_new',require_active=True)
        assert not path.exists()
    finally:hidden.rename(path)
    assert path.read_bytes()==raw and inventory_tree(c['target']/'book')==before and logical(c['target']/'workbench.sqlite')==sql


def test_second_generation_publishes_formal_next_chapter_and_rearchives_offline(second_case):
    from tests.test_restore_activation_chapter import publish_restored_next_chapter
    from core.story_project.paths import resolve_prose
    from core.chapter_budget_store import ChapterBudgetStore
    c=second_case;artifact_before=inventory_tree(c['internal_artifact'])
    previous_book=inventory_tree(c['previous']['target']/'book');previous_sql=logical(c['previous']['target']/'workbench.sqlite')
    chapter2=resolve_prose(c['target']/'book',2).path.read_bytes()
    publish_restored_next_chapter(c)
    assert resolve_prose(c['target']/'book',2).path.read_bytes()==chapter2
    chapter3=resolve_prose(c['target']/'book',3).path
    assert chapter3.is_file()
    assert inventory_tree(c['internal_artifact'])==artifact_before
    assert inventory_tree(c['previous']['target']/'book')==previous_book and logical(c['previous']['target']/'workbench.sqlite')==previous_sql
    package,preview=archive(c,c['target'])
    before=inventory_tree(c['target']/'book');sql=logical(c['target']/'workbench.sqlite')
    with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
        for root in (c['root'],c['previous']['target'],c['target'],c['store']):
            absent.enter_context(absent_source(root,temporary_name=root.name+'-published-offline-'+uuid.uuid4().hex[:8]))
        offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
        offline.setattr('workbench.book_ownership._record',forbidden)
        verified=verify_archived_target(package,project_id=c['project'].id,book_id=c['project'].book_id,temporary_root=c['scratch'])
        assert verified['offline_data_verified'] and not verified['blockers']
    assert inventory_tree(c['target']/'book')==before and logical(c['target']/'workbench.sqlite')==sql
    budget=ChapterBudgetStore(c['target']/'book/.novelagent/runtime/budgets/c3',book_id=c['project'].book_id,chapter=3)
    assert budget.report()['charged_amount']=='0.000219' and len(budget.usage_snapshot()['attempts'])==3
    import hashlib
    (REPO/'.tmp/t72-internal-job-browser-case.json').write_text(json.dumps({'target':str(c['target']),
        'machine_store':str(c['store']),'job_id':c['job'].id,'project_id':c['project'].id,'book_id':c['project'].book_id,
        'internal_relative':c['internal_relative'].as_posix(),'archive':str(package),'scope_generation':2,
        'formal_next_chapter':3,'chapter3_sha256':hashlib.sha256(chapter3.read_bytes()).hexdigest(),
        'charged_amount':'0.000219','attempt_count':3},indent=2),encoding='utf-8')
