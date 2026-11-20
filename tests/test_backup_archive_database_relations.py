"""Preserved batch and reviewed relations cannot cross restored book scope."""
from contextlib import closing
from pathlib import Path

import pytest

from tests.test_backup_archive_database import (saved, archive_case, legacy, software_tree,
    private_temporary_directory, replace_database, inspect)
from tests.test_backup_archive import capture
from tests.test_backup_mixed_history import mixed, case, transaction
from workbench.backup_archive import BackupArchiveService
from workbench.backup_archive_database import ArchivedDatabaseError
from workbench.book_ownership import BookOwnership
from workbench.cohorts import CohortService
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for
from workbench.projects import ProjectRegistry


@pytest.mark.parametrize('binding',['target','other','missing'])
def test_batch_position_and_evaluation_are_preserved_with_matching_job_only(saved,binding):
    c=saved
    CohortService(database=c['db'],projects=c['projects'],jobs=c['jobs'],repo_root=Path.cwd())
    job={'target':c['job'].id,'other':c['other_job'].id,'missing':'job_missing'}[binding]
    with c['db'].transaction() as connection:
        connection.execute('''INSERT INTO cohorts
            (id,project_id,start_chapter,end_chapter,status,weekly_budget,baseline_json,session_job_id,
             revision,created_at,updated_at) VALUES ('cohort_retained',?,1,2,'active',10,'{}',?,1,'now','now')''',
             (c['project'].id,job))
        connection.execute('''INSERT INTO chapter_evaluations
            (cohort_id,chapter,scores_json,edit_minutes,actual_cost,severe_defects_json,notes,revision,created_at,updated_at)
            VALUES ('cohort_retained',1,'{}',5,0,'[]','author evaluation',1,'now','now')''')
    c['package']=Path(capture(c)['path'])
    if binding=='target':
        result=inspect(c)
        assert result['target_tables']['cohorts']['count']==1
        assert result['target_tables']['chapter_evaluations']['rows'][0]['key']=={'cohort_id':'cohort_retained','chapter':1}
        assert result['target_jobs'][0]['next_chapter']==1
        assert not result['restore_plan_ready']
    else:
        with pytest.raises(ArchivedDatabaseError,match='批次任务'):inspect(c)


@pytest.mark.parametrize('cross_project',[False,True])
def test_actual_reviewed_drafts_and_fact_reviews_remain_bound_to_target(mixed,software_tree,cross_project,private_temporary_directory):
    c=mixed;assert transaction(c).commit()['committed']
    c['temporary_root']=private_temporary_directory
    base=c['root'].parent;machine=base/'m'
    other=ProjectRegistry(c['db'],allowed_workspaces=[base]).create_project(base/'other',name='other')
    with BookOwnership(c['project'].book_id,machine_store=machine).locked() as owner:
        with c['db'].transaction() as connection:
            owner.enroll(root=c['root'],database=c['db'],project_id=c['project'].id,connection=connection)
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        service=BackupArchiveService(projects=c['projects'],maintenance=gate,task_runtime_root=c['runtime'],
            archive_root=base/'archives',machine_store=machine,runtime_config={'workbench_version':'0.1.0-beta',
            'job_execution_contract':'workbench_execution_v1','worker_enabled':False,'auto_import_active':False})
        c['package']=Path(capture({**c,'service':service,'op':op})['path'])
        gate.finish(op)
    if cross_project:
        replace_database(c,lambda conn:conn.execute('UPDATE chapter_candidate_drafts SET project_id=?',(other.id,)))
        with pytest.raises(ArchivedDatabaseError,match='事实审核'):inspect(c)
    else:
        result=inspect(c)
        assert result['target_tables']['chapter_candidate_drafts']['count']==1
        assert result['target_tables']['chapter_fact_reviews']['count']==1
        assert result['target_tables']['chapter_fact_review_edits']['count']>=1
        assert result['restore_policy']['disable_other_project_ids']==[other.id]
