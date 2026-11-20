"""Job state is re-read from the actual offline database, with no scheduler."""
from contextlib import ExitStack
import json
from pathlib import Path

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory,replace_database
from tests.test_backup_archive_history import absent_source
from workbench.backup_archive_jobs import inspect_archived_jobs
from workbench.backup_files import inventory_tree


def job(c):return c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=2)


def inspect(c,package,scratch):return inspect_archived_jobs(package,project_id=c['project'].id,
    book_id=c['project'].book_id,temporary_root=scratch)


def test_queued_work_and_unbacked_other_project_are_retained_without_scheduling(archive_case,private_temporary_directory,monkeypatch):
    c=archive_case;target=job(c);other=c['projects'].create_project(c['root'].parent/'other',name='other')
    c['jobs'].create_job(project_id=other.id,kind='generation_session',start_chapter=1,end_chapter=1)
    package=Path(capture(c)['path']);before=inventory_tree(package)
    def deny(*args,**kwargs):pytest.fail('offline job check entered scheduler')
    monkeypatch.setattr(type(c['jobs']),'claim_next',deny)
    with ExitStack() as stack:
        stack.enter_context(absent_source(c['root']));stack.enter_context(absent_source(other.root,temporary_name='other-offline'))
        first=inspect(c,package,private_temporary_directory);second=inspect(c,package,private_temporary_directory)
    assert first==second and first['jobs_verified'] and [j['id'] for j in first['jobs']]==[target.id]
    assert first['jobs'][0]['requires_revalidation_before_resume'] and not first['start_worker']
    assert not first['provider_calls_allowed'] and inventory_tree(package)==before


@pytest.mark.parametrize('inside',[True,False])
def test_configured_job_artifacts_resolve_only_to_the_archived_inventory(archive_case,private_temporary_directory,inside):
    c=archive_case;job(c);target=c['jobs'].claim_next()
    c['jobs'].pause_for_maintenance(target.id,chapter=1,operation_id='a'*32)
    runtime=c['root']/'.novelagent/runtime/jobs' if inside else c['runtime']
    c['service'].task_runtime_root=runtime
    artifact=runtime/target.id;artifact.mkdir(parents=True);(artifact/'evidence.txt').write_text('saved')
    package=Path(capture(c)['path'])
    with absent_source(c['root']):result=inspect(c,package,private_temporary_directory)
    assert result['jobs_verified'],result['blockers']
    assert result['jobs'][0]['artifact']['root']==('story_project' if inside else 'jobs/'+target.id)


@pytest.mark.parametrize('state',['starting','running','pause_requested','needs_recovery'])
def test_unsettled_archived_job_cannot_be_treated_as_resumable(archive_case,private_temporary_directory,state):
    c=archive_case;target=job(c)
    with c['db'].transaction() as connection:connection.execute('UPDATE jobs SET state=? WHERE id=?',(state,target.id))
    package=Path(capture(c)['path']);result=inspect(c,package,private_temporary_directory)
    assert not result['jobs_verified'] and 'job_not_settled' in {b['code'] for b in result['blockers']}


@pytest.mark.parametrize('damage',[None,'missing','boolean_count','wrong_chapter'])
def test_first_boundary_pause_is_the_only_proof_of_expected_missing_artifacts(archive_case,private_temporary_directory,damage):
    c=archive_case;job(c);target=c['jobs'].claim_next()
    c['jobs'].pause_for_maintenance(target.id,chapter=1,operation_id='a'*32)
    with c['db'].transaction() as connection:
        if damage=='missing':connection.execute('DELETE FROM job_events WHERE job_id=?',(target.id,))
        elif damage:
            row=connection.execute("SELECT sequence,payload_json FROM job_events WHERE job_id=? AND event_type='job.paused'",(target.id,)).fetchone()
            value=json.loads(row['payload_json']);value['completed_chapters' if damage=='boolean_count' else 'chapter']=False if damage=='boolean_count' else 99
            connection.execute('UPDATE job_events SET payload_json=? WHERE job_id=? AND sequence=?',(json.dumps(value),target.id,row['sequence']))
    package=Path(capture(c)['path']);result=inspect(c,package,private_temporary_directory)
    assert result['jobs_verified']==(damage is None)
    if damage:assert 'job_artifact_root_missing' in {b['code'] for b in result['blockers']}
    else:assert result['jobs'][0]['first_boundary_pause']


def test_stale_preflight_does_not_hide_changed_actual_sqlite_job_state(archive_case,private_temporary_directory):
    c=archive_case;target=job(c);c['package']=Path(capture(c)['path'])
    replace_database(c,lambda connection:connection.execute("UPDATE jobs SET state='running' WHERE id=?",(target.id,)))
    result=inspect(c,c['package'],private_temporary_directory)
    assert not result['jobs_verified'] and result['blockers']==[{'code':'job_not_settled','reference':target.id}]
