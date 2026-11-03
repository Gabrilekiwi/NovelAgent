"""Actual archive-only references, including target external job artifacts."""
from contextlib import ExitStack
import json
from pathlib import Path

import pytest

from core.engine.root_registry import RootRegistryService
from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_database import private_temporary_directory,replace_database
from tests.test_backup_archive_history import absent_source
from workbench.backup_archive_references import inspect_archived_references
from workbench.backup_files import inventory_tree


def inspect(c,package,scratch):
    return inspect_archived_references(package,project_id=c['project'].id,book_id=c['project'].book_id,
                                       temporary_root=scratch)


def seed_job(c,value=None):
    job=c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=1)
    if value is not None:
        with c['db'].transaction() as connection:
            connection.execute('UPDATE jobs SET result_json=? WHERE id=?',(json.dumps(value),job.id))
    return job


def test_source_book_and_external_job_root_can_be_absent_while_graph_is_checked(archive_case,private_temporary_directory):
    c=archive_case;job=seed_job(c);artifact=c['runtime']/job.id/'draft.json'
    artifact.parent.mkdir(parents=True);artifact.write_text('{"text":"saved author draft"}')
    c['jobs'].upsert_draft(project_id=c['project'].id,chapter=1,text='author text',source_path=str(artifact))
    other=c['projects'].create_project(c['root'].parent/'other',name='other')
    other_job=c['jobs'].create_job(project_id=other.id,kind='generation_session',start_chapter=1,end_chapter=1)
    with c['db'].transaction() as connection:
        connection.execute('UPDATE jobs SET result_json=? WHERE id=?',
            (json.dumps({'outbox':{'path':str(artifact),'format':'json','sha256':digest(artifact.read_bytes())}}),job.id))
        connection.execute('UPDATE jobs SET result_json=? WHERE id=?',
            (json.dumps({'path':'D:/unrelated-other-book-content'}),other_job.id))
    package=Path(capture(c)['path']);before=inventory_tree(package)
    with ExitStack() as stack:
        stack.enter_context(absent_source(c['root']))
        stack.enter_context(absent_source(c['runtime'],temporary_name='offline-jobs'))
        stack.enter_context(absent_source(other.root,temporary_name='o'))
        first=inspect(c,package,private_temporary_directory);second=inspect(c,package,private_temporary_directory)
    assert first==second and first['references_verified']
    assert any(r['root']=='jobs/'+job.id and r['path']=='draft.json' for r in first['references'])
    assert first['metadata_files']==[{'root':'jobs/'+job.id,'path':'draft.json'}]
    assert not first['recoverable'] and not first['restore_plan_ready']
    assert inventory_tree(package)==before


@pytest.mark.parametrize('location',['result','config','event','stdout','run'])
def test_nested_external_reference_is_blocked_without_following_original_path(archive_case,private_temporary_directory,location):
    c=archive_case;job=seed_job(c)
    payload={'outbox':{'deep':[{'path':str(c['root'].parent/'unarchived.txt')}]}}
    if location in {'result','config'}:
        with c['db'].transaction() as connection:
            connection.execute('UPDATE jobs SET '+location+'_json=? WHERE id=?',(json.dumps(payload),job.id))
    elif location=='event':
        with c['db'].transaction() as connection:
            connection.execute('''INSERT INTO job_events(job_id,sequence,event_type,payload_json,created_at)
                VALUES (?,999,'artifact.test',?,'now')''',(job.id,json.dumps(payload)))
    else:
        path=(c['runtime']/job.id/'chapter_0001/stdout.json' if location=='stdout'
              else c['root']/'.novelagent/runtime/runs/reference-test.json')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(payload))
    package=Path(capture(c)['path']);before=inventory_tree(package)
    with absent_source(c['root']):result=inspect(c,package,private_temporary_directory)
    assert not result['references_verified']
    assert 'unknown_external_file_reference' in {b['code'] for b in result['blockers']}
    assert inventory_tree(package)==before


@pytest.mark.parametrize('damage',['missing','excluded','traversal','wrong_hash','unknown_semantics','malformed'])
def test_invalid_reference_is_not_hidden_by_valid_archive_bytes(archive_case,private_temporary_directory,damage):
    c=archive_case;target=c['root']/'artifact.txt';target.write_text('saved')
    payload={'path':str(target),'format':'text','sha256':digest(target.read_bytes())}
    expected={'missing':'required_file_missing_or_excluded','excluded':'required_file_missing_or_excluded',
        'traversal':'invalid_file_reference','wrong_hash':'referenced_artifact_digest_mismatch',
        'unknown_semantics':'unclassified_metadata_reference','malformed':'run_metadata_unreadable'}[damage]
    if damage=='missing':payload['path']=str(c['root']/'absent.txt')
    elif damage=='excluded':
        (c['root']/'.env').write_text('SYNTHETIC=secret');payload={'path':str(c['root']/'.env')}
    elif damage=='traversal':payload['path']=str(c['root']/'nested/../artifact.txt')
    elif damage=='wrong_hash':payload['sha256']='0'*64
    elif damage=='unknown_semantics':payload={'future_payload_ref':'unclassified'}
    else:
        bad=c['root']/'bad.json';bad.write_text('{invalid');payload={'path':str(bad),'format':'json'}
    seed_job(c,{'nested':[payload]})
    package=Path(capture(c)['path'])
    result=inspect(c,package,private_temporary_directory)
    assert expected in {b['code'] for b in result['blockers']} and not result['references_verified']


def test_followed_json_cycle_is_bounded_and_preserved(archive_case,private_temporary_directory):
    c=archive_case;first=c['root']/'first.json';second=c['root']/'second.json'
    first.write_text(json.dumps({'next':{'path':str(second),'format':'json'}}))
    second.write_text(json.dumps({'next':{'path':str(first),'format':'json'}}))
    seed_job(c,{'path':str(first),'format':'json'})
    package=Path(capture(c)['path'])
    result=inspect(c,package,private_temporary_directory)
    assert result['references_verified'] and len(result['metadata_files'])==2


@pytest.mark.parametrize('known',[True,False])
def test_logical_root_uuid_uses_archived_registry_only(archive_case,private_temporary_directory,known):
    c=archive_case;runtime=c['root']/'.novelagent/runtime';runtime.mkdir(parents=True,exist_ok=True)
    registry=RootRegistryService(runtime/'custom-persistence').ensure({'runtime':runtime})
    (runtime/'artifact.txt').write_text('kept')
    value={'root_id':'runtime','relative_path':'artifact.txt',
           'root_uuid':registry['roots']['runtime']['root_uuid'] if known else '00000000-0000-4000-8000-000000000000'}
    seed_job(c,{'result_ref':value})
    package=Path(capture(c)['path'])
    with absent_source(c['root']):result=inspect(c,package,private_temporary_directory)
    assert result['references_verified']==known
    if not known:assert 'unresolved_logical_reference_root' in {b['code'] for b in result['blockers']}


def test_reference_graph_rechecks_sqlite_instead_of_trusting_preview_list(archive_case,private_temporary_directory):
    c=archive_case;job=seed_job(c)
    c['package']=Path(capture(c)['path'])
    assert not c['service'].preview(c['project'].id,operation_id=c['op'])['scope']['blockers']
    replace_database(c,lambda connection:connection.execute('UPDATE jobs SET result_json=? WHERE id=?',
        (json.dumps({'path':str(c['root'].parent/'missing-external.txt')}),job.id)))
    result=inspect(c,c['package'],private_temporary_directory)
    assert not result['references_verified'] and result['blockers'][0]['code']=='unknown_external_file_reference'
