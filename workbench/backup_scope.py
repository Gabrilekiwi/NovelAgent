"""Read-only target-book scope selection; never follow an unknown external root.

This is a preflight input to a locked archive operation, not an archive receipt.
Unavailable or unverified recovery evidence remains an explicit blocker.
"""
import json
import os
from pathlib import Path
import re

from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_execution_time import ChapterExecutionClock
from core.engine.root_registry import load_root_registry
from core.engine.safe_paths import assert_safe_local_tree
from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.story_project.identity import load_project_identity
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest,read_project_bytes
from core.model_calls import model_response_artifact_hash,validate_model_call_receipt
from workbench.backup_files import inventory_tree
from workbench.backup_history import verify_persistence_history
from workbench.backup_references import inspect_metadata_references
from workbench.backup_auxiliary import inspect_auxiliary_settlement
from workbench.backup_job_state import first_boundary_pause
from workbench.model_usage import read_project_model_usage


class BackupScopeError(RuntimeError):
    code='backup_scope_invalid'


def inspect_backup_scope(projects,project_id,*,connection,task_runtime_root,locked_readers=None,restored_provenance=None):
    """Use only a registered book and server-configured job artifact root."""
    if not connection.in_transaction:raise BackupScopeError('备份预检需要一致数据库事务。')
    attached={item[1]:item[2] for item in connection.execute('PRAGMA database_list')}
    if set(attached)-{'main','temp'} or Path(attached.get('main','')).resolve()!=projects.database.path:
        raise BackupScopeError('备份预检连接不属于目标登记数据库。')
    row=connection.execute('SELECT id,book_id,root FROM projects WHERE id=?',(project_id,)).fetchone()
    if row is None:raise BackupScopeError('备份目标未登记。')
    root=projects.validate_project_root(assert_safe_local_tree(Path(row['root'])))
    runtime=assert_safe_local_tree(Path(task_runtime_root).absolute())
    blockers=[];inventories={};references=[];checks=[]
    def block(code,reference=None):
        value={'code':code}
        if reference is not None:value['reference']=reference
        if value not in blockers:blockers.append(value)
    def inventory(key,path):
        try:inventories[key]=inventory_tree(path,locked_readers=locked_readers)
        except (OSError,ValueError,RuntimeError):block('required_root_unreadable',key)
    inventory('story_project',root)
    tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'jobs','drafts'}<=tables:block('required_database_tables_missing')
    jobs=list(connection.execute('SELECT * FROM jobs WHERE project_id=? ORDER BY id',(project_id,))) if 'jobs' in tables else []
    historical=None
    if restored_provenance is not None:
        from workbench.backup_restore_provenance import validate_restored_database
        from workbench.restore_job_artifacts import RestoredJobArtifacts
        try:
            evidence=validate_restored_database(connection,restored_provenance)
            historical=RestoredJobArtifacts(root,row['book_id'],restored_provenance)
            checks.append({'check':'restored_database','state':'verified','evidence':evidence})
        except (ValueError,KeyError,TypeError):block('restored_database_provenance_unverified')
    for job in jobs:
        identity=job['id']
        if not isinstance(identity,str) or not re.fullmatch(r'job_[A-Za-z0-9_-]{1,180}',identity):
            block('invalid_job_identity');continue
        if job['state'] in {'starting','running','pause_requested','needs_recovery'}:
            block('job_not_settled',identity)
        imported=historical is not None and identity in historical.imported
        retained=historical.target(identity) if imported else None
        artifact=retained if retained is not None else runtime/identity
        if imported and retained is None and os.path.lexists(artifact):
            block('historical_job_artifact_root_collision',identity)
        if retained is not None and artifact!=runtime/identity and os.path.lexists(runtime/identity):
            block('historical_job_artifact_root_collision',identity)
        if os.path.lexists(artifact):
            if not artifact.is_relative_to(root):inventory('jobs/'+identity,artifact)
        elif job['started_at'] is not None:
            # A claim sets started_at before any child or artifact exists. Only
            # the explicit first-boundary pause proves this expected absence;
            # absence of chapter.started alone is not positive evidence.
            event=connection.execute("SELECT event_type,payload_json FROM job_events WHERE job_id=? ORDER BY sequence DESC LIMIT 1",(identity,)).fetchone() if 'job_events' in tables else None
            first_pause=historical.first_pause(identity) if imported else first_boundary_pause(job,event)
            if not first_pause:block('job_artifact_root_missing',identity)
    if historical is not None:
        try:historical.validate_trees(inventories)
        except (ValueError,KeyError,TypeError):block('historical_job_artifacts_unverified')
    for table,field,unsettled in (
        ('planning_operations','state',{'running','needs_recovery'}),
        ('amendments','status',{'validating','needs_recovery'})):
        if table in tables:
            for operation in connection.execute(f'SELECT id,{field} FROM {table} WHERE project_id=?',(project_id,)):
                if operation[field] in unsettled:block('provider_operation_not_settled',table+'/'+operation['id'])

    pending_files=set()
    def required_path(value,label,*,relative_to_book=False,kind='file',expected_sha256=None,follow_json=False,evidence=None):
        if not isinstance(value,str) or not value:
            block('invalid_file_reference',label);return
        candidate=Path(value)
        if '..' in candidate.parts:
            block('invalid_file_reference',label);return
        if relative_to_book and not candidate.is_absolute():candidate=root/candidate
        if not candidate.is_absolute():
            block('unresolved_file_reference',label);return
        candidate=Path(os.path.abspath(candidate))
        if historical is not None:
            try:candidate=historical.path(candidate,evidence=evidence,inventories=inventories)
            except ValueError:block('historical_reference_unverified',label);return
        for key,tree in inventories.items():
            base=Path(tree['source_root'])
            if candidate.is_relative_to(base):
                relative=candidate.relative_to(base).as_posix()
                if kind=='directory':
                    if relative=='.' or relative in tree['directories']:
                        references.append({'source':label,'root':key,'path':relative,'kind':'directory'})
                    else:block('required_directory_missing_or_excluded',label)
                elif relative in tree['files']:
                    if expected_sha256 is not None and expected_sha256!=tree['files'][relative]['sha256']:
                        block('referenced_artifact_digest_mismatch',label);return
                    references.append({'source':label,'root':key,'path':relative,
                        'sha256':tree['files'][relative]['sha256']})
                    if follow_json:pending_files.add((key,relative))
                else:block('required_file_missing_or_excluded',label)
                return
        block('unknown_external_file_reference',label)

    # These fields are artifact references, not arbitrary prose or user notes.
    for table,field,relative in (('drafts','source_path',False),
        ('chapter_candidate_drafts','source_artifact_ref',True),('amendments','content_path',True),
        ('outline_batches','approval_manifest_path',False)):
        if table not in tables:continue
        for item in connection.execute(f'SELECT id,{field} FROM {table} WHERE project_id=?',(project_id,)):
            if item[field]:
                value=item[field]
                if table=='chapter_candidate_drafts':value=str(root/'.novelagent/runtime'/value)
                required_path(value,table+'/'+item['id']+'/'+field,relative_to_book=relative)
    pending_metadata=[]
    for job in jobs:
        for field in ('config_json','result_json'):
            if not job[field]:continue
            try:
                if len(job[field])>32*1024**2:raise ValueError('job metadata too large')
                value=json.loads(job[field])
            except (TypeError,ValueError):block('invalid_job_artifact_references',job['id']);continue
            if not isinstance(value,dict):block('invalid_job_artifact_references',job['id']);continue
            pending_metadata.append((value,job['id']+'/'+field,('job',job['id'],field,job[field])))

        if 'job_events' in tables:
            for event in connection.execute('SELECT * FROM job_events WHERE job_id=? ORDER BY sequence',(job['id'],)):
                label=job['id']+'/events/'+str(event['sequence'])
                try:
                    if len(event['payload_json'])>32*1024**2:raise ValueError('event metadata too large')
                    value=json.loads(event['payload_json'])
                except (TypeError,ValueError):block('invalid_job_event_references',label);continue
                if not isinstance(value,dict):block('invalid_job_event_references',label);continue
                pending_metadata.append((value,label,('event',dict(event))))

    tree=inventories.get('story_project')
    root_bindings={}
    if tree:
        auxiliary=inspect_auxiliary_settlement(root,tree,row['book_id'])
        checks.append({k:v for k,v in auxiliary.items() if k!='blockers'})
        for issue in auxiliary['blockers']:block(issue['code'],issue['reference'])
        for relative in tree['files']:
            if not relative.endswith('/root_registry.json'):continue
            try:
                registry=load_root_registry(root/relative)
                for key,binding in registry['roots'].items():
                    path=Path(binding['path']).absolute()
                    # Never authorize new roots by trusting a stored path.
                    if not path.is_relative_to(root):block('unknown_external_registry_root',relative+':'+key)
                    elif path!=root and path.relative_to(root).as_posix() not in tree['directories']:
                        block('required_registry_root_missing',relative+':'+key)
                    else:
                        root_bindings.setdefault((key,binding['root_uuid']),set()).add(str(path))
                        root_bindings.setdefault((key,None),set()).add(str(path))
            except (OSError,ValueError,RuntimeError):block('invalid_root_registry',relative)
        for value,label,evidence in pending_metadata:
            def require_metadata(value,label,**options):return required_path(value,label,evidence=evidence,**options)
            inspect_metadata_references(value,source=label,book_root=root,require_path=require_metadata,
                block=block,root_bindings=root_bindings)
        # Final run records and captured child stdout carry nested publication,
        # outbox and artifact references absent from the compact job columns.
        for key,source_tree in inventories.items():
            for name,record in source_tree['files'].items():
                path=Path(name)
                selected=(key=='story_project' and path.suffix=='.json' and path.parent.as_posix()=='.novelagent/runtime/runs'
                    or key.startswith('jobs/') and path.name=='stdout.json')
                if not selected:continue
                pending_files.add((key,name))
        visited_metadata=set();metadata_bytes=0
        while pending_files:
            key,name=min(pending_files);pending_files.remove((key,name))
            if (key,name) in visited_metadata:continue
            visited_metadata.add((key,name));source_tree=inventories[key];record=source_tree['files'][name]
            metadata_bytes+=record['size']
            if len(visited_metadata)>10000 or metadata_bytes>256*1024**2:
                block('metadata_graph_limits_exceeded');break
            label=key+'/'+name
            try:
                if record['size']>32*1024**2:raise ValueError('run metadata too large')
                raw=read_project_bytes(Path(source_tree['source_root']),name)
                if len(raw)!=record['size'] or digest(raw)!=record['sha256']:raise ValueError('run metadata changed')
                value=json.loads(raw)
                if not isinstance(value,(dict,list)):raise ValueError('machine metadata must be structured')
            except (OSError,ValueError,RuntimeError):block('run_metadata_unreadable',label);continue
            def require_file(value,label,**options):return required_path(value,label,evidence=('file',key,name),**options)
            inspect_metadata_references(value,source=label,book_root=root,require_path=require_file,
                block=block,root_bindings=root_bindings)
    try:
        identity=json.loads(read_project_bytes(root,'.novelagent/project.json'))
        if not isinstance(identity,dict):raise BackupScopeError('invalid project identity')
        if identity.get('schema_version')=='3.0':
            verified=load_verified_reviewed_identity(root)
            if verified['identity']['book_id']!=row['book_id']:raise BackupScopeError('book identity mismatch')
            from workbench.chapter_publication import ChapterPublicationService
            # Same read-only settled-journal check used by publication preview.
            ChapterPublicationService._require_settled_persistence(root/'.novelagent/runtime/reviewed/p')
            checks.append({'check':'reviewed_history','state':'verified'})
        else:
            loaded=load_project_identity(root)
            if loaded is None or loaded.book_id!=row['book_id']:raise BackupScopeError('book identity mismatch')
            checks.append({'check':'legacy_identity','state':'verified'})
        if tree:
            history=verify_persistence_history(root,tree,book_id=row['book_id'],provenance=restored_provenance)
            checks.append({'check':'persistence_history' if history['schema_version']=='mixed_backup_history_v1' else 'legacy_v1_history',
                'state':'verified','evidence':history})
    except (OSError,ValueError,RuntimeError):block('identity_or_history_unverified')
    for store_type in (ChapterCandidateStore,ChapterFactExtractionStore):
        try:
            store=store_type(root)
            for entry in sorted(store.root.iterdir()) if store.root.exists() else []:
                record=store.load(entry.name)
                if record['intent']['book_id']!=row['book_id']:
                    block('execution_book_mismatch',store.root.name+'/'+entry.name)
                if record['record'] is None or record['record']['status'] in {'running','provider_uncertain','needs_recovery'}:
                    block('execution_not_settled',store.root.name+'/'+entry.name)
        except (OSError,ValueError,RuntimeError):block('execution_evidence_unverified',store_type.__name__)
    try:
        usage=read_project_model_usage(root,book_id=row['book_id'])
        if any(item['receipt_hash'] is None for item in usage['items']):block('model_call_without_receipt')
        for item in usage['items']:
            if item['receipt_hash'] is None:continue
            execution=item['execution_id']
            if execution.startswith('execution_'):
                store=root/'.novelagent/runtime/runs/executions'/execution/'model_calls'
            else:
                family,operation=execution.split(':',1)
                store=root/'.novelagent/runtime/model_calls'/family/operation
            receipt=validate_model_call_receipt(json.loads(read_project_bytes(store,'receipts/'+item['attempt_id']+'.json')))
            ref=receipt['response_artifact_ref']
            if (receipt['receipt_hash']!=item['receipt_hash'] or not ref
                    or model_response_artifact_hash(read_project_bytes(store,ref))!=receipt['response_artifact_hash']):
                block('model_response_unverified',execution+'/'+item['attempt_id'])
        registry=ChapterBudgetFreezeRegistry(root,book_id=row['book_id'])
        for budget_report in usage['chapter_budgets']:
            if budget_report['unresolved_attempts']:block('chapter_budget_unsettled',str(budget_report['chapter']))
            frozen=registry.load(budget_report['chapter'])
            time=ChapterExecutionClock(registry.root/f"c{budget_report['chapter']}",frozen).report()
            if not time['active_time_complete']:block('chapter_active_time_unsettled',str(budget_report['chapter']))
        checks.append({'check':'model_usage_and_budgets','state':'verified'})
    except (OSError,ValueError,RuntimeError):block('model_or_budget_evidence_unverified')
    db_bytes=connection.execute('PRAGMA page_count').fetchone()[0]*connection.execute('PRAGMA page_size').fetchone()[0]
    value=dict(schema_version='target_backup_scope_v1',project_id=row['id'],book_id=row['book_id'],
        source_root=str(root),task_runtime_root=str(runtime),inventories=inventories,references=references,checks=checks,blockers=blockers,
        scope_checks_passed=not blockers,archive_certified=False,database_scope='whole_database',
        other_project_files_included=False,restore_requires_other_projects_disabled=True,
        total_source_bytes=db_bytes+sum(v['total_bytes'] for v in inventories.values()),database_bytes=db_bytes)
    value['scope_sha256']=digest(value)
    return value
