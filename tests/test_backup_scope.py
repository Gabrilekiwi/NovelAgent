"""Preflight selects actual target evidence and never certifies an archive."""
from contextlib import closing
import json

import pytest

from tests.test_workbench_jobs import _job_store
from tests.test_reviewed_chapter_commit import case,ready
from tests.test_model_usage_budget import seed
from tests.test_workbench_model_usage import put
from workbench.backup_database import owned_database_snapshot
from workbench.backup_files import inventory_tree
from workbench.backup_scope import inspect_backup_scope,BackupScopeError
from workbench.instance import InstanceLease
from workbench.jobs import JobStore
from workbench.maintenance import maintenance_for
from workbench.projects import ProjectRegistry


@pytest.fixture
def legacy(tmp_path):
    _,jobs,project_id=_job_store(tmp_path)
    projects=ProjectRegistry(jobs.database,allowed_workspaces=[tmp_path/'books'])
    project=projects.get(project_id)
    return dict(jobs=jobs,db=jobs.database,projects=projects,project=project,root=project.root,runtime=tmp_path/'jobs')


def inspect(c):
    with closing(InstanceLease(c['db'].path.parent).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with gate.capture(op) as permit:
            with owned_database_snapshot(c['db'],snapshot_permit=permit) as snapshot:
                result=inspect_backup_scope(c['projects'],c['project'].id,
                    connection=snapshot.connection,task_runtime_root=c['runtime'])
        gate.finish(op)
        return result


def codes(result):return {item['code'] for item in result['blockers']}


def test_scope_keeps_target_job_files_and_full_database_policy_without_other_book_files(legacy):
    c=legacy;root=c['root']
    other=c['projects'].create_project(root.parent/'另一部书',name='另一部书')
    other_job=c['jobs'].create_job(project_id=other.id,kind='generation_session',start_chapter=1,end_chapter=1)
    job=c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=1)
    artifact=c['runtime']/job.id/'chapter_0001';artifact.mkdir(parents=True)
    source=artifact/'draft.json';source.write_text('{"text":"作者草稿"}',encoding='utf-8')
    c['jobs'].upsert_draft(project_id=c['project'].id,chapter=1,text='作者改过的文本',source_path=str(source))
    other_artifact=c['runtime']/other_job.id;other_artifact.mkdir();(other_artifact/'private.txt').write_text('unrelated')
    (root/'.env').write_text('FAKE_KEY=synthetic',encoding='utf-8')
    before=inventory_tree(root)
    result=inspect(c)
    assert result['scope_checks_passed'],result['blockers']
    assert not result['archive_certified'] and result['database_scope']=='whole_database'
    assert result['restore_requires_other_projects_disabled'] and not result['other_project_files_included']
    assert set(result['inventories'])=={'story_project','jobs/'+job.id}
    assert result['references'][0]['sha256']==result['inventories']['jobs/'+job.id]['files']['chapter_0001/draft.json']['sha256']
    assert '.env' not in result['inventories']['story_project']['files']
    assert inventory_tree(root)==before


@pytest.mark.parametrize('kind',['unknown','missing','secret'])
def test_missing_excluded_or_unapproved_external_reference_prevents_recovery_claim(legacy,kind):
    c=legacy
    source={'unknown':c['root'].parent/'external.txt','missing':c['root']/'absent.txt','secret':c['root']/'.env'}[kind]
    if kind!='missing':source.write_text('synthetic',encoding='utf-8')
    c['jobs'].upsert_draft(project_id=c['project'].id,chapter=1,text='保存草稿',source_path=str(source))
    result=inspect(c)
    assert not result['scope_checks_passed']
    assert ('unknown_external_file_reference' if kind=='unknown' else 'required_file_missing_or_excluded') in codes(result)


def test_claimed_job_with_missing_external_artifacts_is_not_a_settled_backup(legacy):
    c=legacy;c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=2)
    job=c['jobs'].claim_next()
    c['jobs'].mark_running(job.id,chapter=1,command=['python','main.py'])
    result=inspect(c)
    assert {'job_not_settled','job_artifact_root_missing'}<=codes(result)


def test_maintenance_before_first_chapter_does_not_invent_missing_artifacts(legacy):
    c=legacy;c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=2)
    job=c['jobs'].claim_next()
    c['jobs'].pause_for_maintenance(job.id,chapter=1,operation_id='a'*32)
    result=inspect(c)
    assert result['scope_checks_passed'],result['blockers']
    assert set(result['inventories'])=={'story_project'}


@pytest.mark.parametrize('damage',['missing','malformed'])
def test_missing_job_history_is_not_proof_that_an_artifact_was_never_needed(legacy,damage):
    c=legacy;c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=2)
    job=c['jobs'].claim_next()
    c['jobs'].pause_for_maintenance(job.id,chapter=1,operation_id='a'*32)
    with c['db'].transaction() as connection:
        if damage=='missing':connection.execute('DELETE FROM job_events WHERE job_id=?',(job.id,))
        else:connection.execute("UPDATE job_events SET payload_json='[]' WHERE job_id=? AND event_type='job.paused'",(job.id,))
    assert 'job_artifact_root_missing' in codes(inspect(c))


def test_reviewed_history_and_candidate_runtime_reference_are_verified(ready):
    c={**ready,'runtime':ready['root'].parent/'jobs'};JobStore(c['db'])
    before=inventory_tree(c['root'])
    result=inspect(c)
    assert result['scope_checks_passed'],result['blockers']
    references=[r for r in result['references'] if r['source'].startswith('chapter_candidate_drafts/')]
    assert len(references)==1 and references[0]['path'].startswith('.novelagent/runtime/candidates/')
    assert {'check':'reviewed_history','state':'verified'} in result['checks']
    assert inventory_tree(c['root'])==before


@pytest.mark.parametrize('damage',['history','candidate','pending'])
def test_broken_reviewed_or_execution_evidence_is_diagnostic_only(ready,damage):
    c={**ready,'runtime':ready['root'].parent/'jobs'};JobStore(c['db'])
    if damage=='history':(c['root']/'.novelagent/runtime/reviewed/p/journals/upgrade-1/manifest.json').write_text('{}')
    elif damage=='candidate':(c['root']/'.novelagent/runtime'/c['draft'].source_artifact_ref).write_text('changed',encoding='utf-8')
    else:(c['root']/'.novelagent/runtime/candidates'/c['draft'].candidate_id/'record.json').unlink()
    result=inspect(c)
    assert not result['scope_checks_passed']
    expected={'history':'identity_or_history_unverified','candidate':'execution_evidence_unverified','pending':'execution_not_settled'}
    assert expected[damage] in codes(result)


def test_unfinished_budget_reservation_is_preserved_without_reconciliation(legacy):
    c=legacy
    seed(c['root'],book_id=c['project'].book_id,fault='after_chapter_reservation_before_intent')
    before=inventory_tree(c['root']);result=inspect(c)
    assert 'chapter_budget_unsettled' in codes(result) and not result['scope_checks_passed']
    assert inventory_tree(c['root'])==before


def test_historical_unbudgeted_response_file_must_still_be_available(legacy):
    c=legacy;put(c['root'],'execution_legacy')
    result=inspect(c)
    assert not result['scope_checks_passed']
    assert codes(result)&{'model_response_unverified','model_or_budget_evidence_unverified'}


def test_preflight_rejects_another_database_transaction(legacy,tmp_path):
    from workbench.database import WorkbenchDatabase
    other=WorkbenchDatabase(tmp_path/'other.db');other.initialize()
    with other.transaction() as connection,pytest.raises(BackupScopeError,match='登记数据库'):
        inspect_backup_scope(legacy['projects'],legacy['project'].id,connection=connection,task_runtime_root=legacy['runtime'])
