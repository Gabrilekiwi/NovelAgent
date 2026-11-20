"""Inspect actual archived job state and configured artifact-root membership."""
import json
from pathlib import Path
import re

from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_database import _database
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_job_state import first_boundary_pause
from workbench.jobs import JOB_STATES


def inspect_archived_jobs(package,*,project_id,book_id,temporary_root=None):
    with verified_archive_view(package) as view,_database(view,temporary_root=temporary_root) as connection:
        preview=view.manifest['preview'];scope=preview['scope'];inventories=scope['inventories']
        root=Path(inventories['story_project']['source_root'])
        row=connection.execute('SELECT book_id,root FROM projects WHERE id=?',(project_id,)).fetchone()
        identity=json.loads(view.read_root('story_project','.novelagent/project.json'))
        if (preview['project_id']!=project_id or preview['book_id']!=book_id or row is None
                or row['book_id']!=book_id or Path(row['root'])!=root or identity.get('book_id')!=book_id):
            raise BackupArchiveError('归档任务目标与数据库或作品身份不同。')
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        jobs=list(connection.execute('SELECT * FROM jobs WHERE project_id=? ORDER BY id LIMIT 10001',(project_id,)))
        if len(jobs)>10000:raise BackupArchiveError('归档目标任务数量超过支持范围。')
        blockers=[];states=[];operations=[]
        historical=None
        if scope.get('restored_provenance') is not None:
            from workbench.backup_restore_provenance import validate_restored_database
            from workbench.restore_job_artifacts import RestoredJobArtifacts
            try:
                validate_restored_database(connection,scope['restored_provenance'])
                historical=RestoredJobArtifacts(root,book_id,scope['restored_provenance'])
                historical.validate_trees(inventories)
            except (ValueError,KeyError,TypeError) as exc:
                raise BackupArchiveError('归档历史任务来源或产物无效。') from exc
        def block(code,reference):blockers.append({'code':code,'reference':reference})
        configured=scope.get('task_runtime_root');runtime=Path(configured) if isinstance(configured,str) else None
        if jobs and (runtime is None or not runtime.is_absolute() or '..' in runtime.parts):
            block('job_runtime_root_binding_unavailable','task_runtime_root');runtime=None
        for job in jobs:
            identity=job['id']
            if not isinstance(identity,str) or not re.fullmatch(r'job_[A-Za-z0-9_-]{1,180}',identity):
                block('invalid_job_identity','jobs');continue
            if job['state'] not in JOB_STATES:block('unknown_job_state',identity)
            elif job['state'] in {'starting','running','pause_requested','needs_recovery'}:block('job_not_settled',identity)
            artifact=None;first_pause=False
            if runtime is not None:
                imported=historical is not None and identity in historical.imported
                retained=historical.target(identity) if imported else None
                target=retained if retained is not None else runtime/identity
                if target.is_relative_to(root):
                    name=target.relative_to(root).as_posix()
                    if name in inventories['story_project']['directories']:artifact={'root':'story_project','path':name}
                else:
                    key='jobs/'+identity;tree=inventories.get(key)
                    if tree is not None:
                        if Path(tree['source_root'])!=target:block('job_artifact_root_binding_mismatch',identity)
                        else:artifact={'root':key,'path':'.'}
                if artifact is None and job['started_at'] is not None:
                    event=connection.execute('SELECT event_type,payload_json FROM job_events WHERE job_id=? ORDER BY sequence DESC LIMIT 1',(identity,)).fetchone() if 'job_events' in tables else None
                    first_pause=historical.first_pause(identity) if imported else first_boundary_pause(job,event)
                    if not first_pause:block('job_artifact_root_missing',identity)
            states.append({'id':identity,'state':job['state'],'start_chapter':job['start_chapter'],
                'end_chapter':job['end_chapter'],'next_chapter':job['next_chapter'],'artifact':artifact,
                'first_boundary_pause':first_pause,'requires_revalidation_before_resume':True})
        for table,field,unsettled in (('planning_operations','state',{'running','needs_recovery'}),
                                    ('amendments','status',{'validating','needs_recovery'})):
            if table not in tables:continue
            rows=list(connection.execute(f'SELECT id,{field} FROM {table} WHERE project_id=? ORDER BY id LIMIT 10001',(project_id,)))
            if len(rows)>10000:raise BackupArchiveError('归档操作数量超过支持范围。')
            for operation in rows:
                label=table+'/'+operation['id']
                if operation[field] in unsettled:block('provider_operation_not_settled',label)
                operations.append({'reference':label,'state':operation[field]})
        result={'schema_version':'archived_job_settlement_v1','archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],'database_logical_sha256':view.manifest['database']['logical_sha256'],
            'project_id':project_id,'book_id':book_id,'jobs':states,'operations':operations,'blockers':blockers,
            'jobs_verified':not blockers,'start_worker':False,'provider_calls_allowed':False,
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
    return result
