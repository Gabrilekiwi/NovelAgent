"""A qualified restore must expose its target-book backup workflow over HTTP."""
from pathlib import Path
import hashlib
import json
import shutil
import uuid
import zipfile

from fastapi.testclient import TestClient

from tests.test_restore_job_artifacts import full_source, artifact_case
from tests.test_restore_job_artifacts import (prepared,bound_copy,restored,pending,financial,
    budget_prepared,jobs_prepared,private_temporary_directory)
from tests.test_backup_restore_database import logical
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.backup_archive import verify_archive, _json
from core.story_project.reviewed_state_document import digest


def test_active_restore_can_maintain_preview_capture_verify_and_finish_only_target(artifact_case):
    c=artifact_case;target=c['target'];database=target/'workbench.sqlite';calls=[]
    before=inventory_tree(target/'book');jobs=inventory_tree(target/'jobs'/c['job'].id);sql=logical(database)
    def forbidden(*args,**kwargs):calls.append(1);raise AssertionError('No Provider authorized')
    app=create_app(database_path=database,start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    archives=Path.cwd()/'.tmp'/('active-http-backup-'+uuid.uuid4().hex[:8]);archives.mkdir()
    app.state.backups.archive_root=archives
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        response=client.post('/api/maintenance/begin',json={})
        assert response.status_code==200,response.json()
        operation=response.json()['operation_id']
        try:
            assert client.get('/api/backups').status_code==200
            assert client.post('/api/projects/'+c['other'].id+'/backups/preview',json={'operation_id':operation}).status_code==409
            response=client.post('/api/projects/'+c['project'].id+'/backups/preview',json={'operation_id':operation})
            assert response.status_code==200,response.json()
            preview=response.json();assert preview['scope_checks_passed'] and not preview['blockers']
            assert next(item for item in preview['roots'] if item['name']=='jobs/'+c['job'].id)['source_root']==str(target/'jobs'/c['job'].id)
            response=client.post('/api/projects/'+c['project'].id+'/backups',json={'operation_id':operation,
                'expected_preview_sha256':preview['preview_sha256'],'archive_purpose':'diagnostic'})
            assert response.status_code==200,response.json()
            saved=response.json();assert saved['receipt']['recoverable'] is False
            response=client.post('/api/backups/'+saved['archive_id']+'/verify',json={'operation_id':operation})
            assert response.status_code==200 and response.json()['archive_verified']
        finally:
            finished=client.post('/api/maintenance/finish',json={'operation_id':operation})
            assert finished.status_code==200 and finished.json()['state']=='idle'
        assert app.state.supervisor._thread is None
    assert inventory_tree(target/'book')==before and inventory_tree(target/'jobs'/c['job'].id)==jobs
    assert logical(database)==sql and not calls


def test_readonly_restore_keeps_maintenance_and_archive_writes_blocked(prepared):
    c=prepared;app=create_app(database_path=c['target']/'workbench.sqlite',start_worker=False,
        auto_import_active=False,chapter_publication_machine_store=c['store'])
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        assert client.get('/api/restore-scope').json()['read_only']
        for path in ('/api/maintenance/begin','/api/maintenance/resume','/api/maintenance/finish',
            '/api/projects/'+c['project'].id+'/backups/preview','/api/backups/backup_'+'a'*32+'/verify'):
            response=client.post(path,json={})
            assert response.status_code==409 and response.json()['error']['code']=='restore_scope_read_only'
        assert app.state.maintenance.status()['state']=='idle'


def test_active_restore_archive_verify_rejects_other_book_in_actual_rehashed_package(artifact_case):
    c=artifact_case;archives=Path.cwd()/'.tmp'/('active-other-backup-'+uuid.uuid4().hex[:8]);archives.mkdir()
    # Use the original ordinary archive, whose scope has no restore lineage.
    # The generic integrity verifier does not certify its claimed book identity;
    # the active instance must still reject a different declared target.
    package=archives/c['package'].name;shutil.copytree(c['package'],package)
    with zipfile.ZipFile(package/'archive.zip') as zipped:payload={name:zipped.read(name) for name in zipped.namelist()}
    manifest=json.loads(payload['manifest.json']);preview=manifest['preview']
    preview.update(project_id=c['other'].id,book_id=c['other'].book_id)
    preview['scope'].update(project_id=c['other'].id,book_id=c['other'].book_id)
    preview['scope']['scope_sha256']=digest({key:value for key,value in preview['scope'].items() if key!='scope_sha256'})
    preview['preview_sha256']=digest({key:value for key,value in preview.items() if key!='preview_sha256'})
    manifest['manifest_sha256']=digest({key:value for key,value in manifest.items() if key!='manifest_sha256'})
    payload['manifest.json']=_json(manifest)
    with zipfile.ZipFile(package/'archive.zip','w') as zipped:
        for name,raw in payload.items():zipped.writestr(name,raw)
    (package/'manifest.json').write_bytes(_json(manifest))
    receipt=json.loads((package/'completion.json').read_bytes());raw=(package/'archive.zip').read_bytes()
    receipt.update(archive={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},
        manifest_sha256=manifest['manifest_sha256'],preview_sha256=preview['preview_sha256'])
    receipt['receipt_sha256']=digest({key:value for key,value in receipt.items() if key!='receipt_sha256'})
    (package/'completion.json').write_bytes(_json(receipt))
    assert verify_archive(package)['archive_verified']
    before=inventory_tree(package)
    app=create_app(database_path=c['target']/'workbench.sqlite',start_worker=False,auto_import_active=False,
        chapter_publication_machine_store=c['store']);app.state.backups.archive_root=archives
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        response=client.post('/api/maintenance/begin',json={});assert response.status_code==200
        operation=response.json()['operation_id']
        try:
            response=client.post('/api/backups/'+package.name+'/verify',json={'operation_id':operation})
            assert response.status_code==409 and response.json()['error']['code']=='backup_archive_invalid'
            assert '当前恢复作品' in response.json()['error']['message']
        finally:assert client.post('/api/maintenance/finish',json={'operation_id':operation}).status_code==200
    assert inventory_tree(package)==before
