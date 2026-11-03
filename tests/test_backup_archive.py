"""Completed diagnostic ZIPs, stale previews and interruption evidence."""
from contextlib import closing,nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from tests.test_backup_scope import legacy
from tests.test_chapter_publication_service import case,ready,service_case
from workbench.backup_archive import BackupArchiveService,BackupArchiveError,verify_archive
from workbench.backup_files import inventory_tree
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for,MaintenanceBlocked
from core.story_project.reviewed_state_document import digest


@pytest.fixture
def software_tree(tmp_path,monkeypatch):
    from workbench import backup_version
    root=tmp_path/'software';root.mkdir()
    (root/'run_workbench.py').write_text('# synthetic version A')
    (root/'requirements.lock').write_text('# synthetic dependency lock')
    monkeypatch.setattr(backup_version,'SOURCE_ROOT',root)
    return root


@pytest.fixture
def archive_case(legacy,software_tree):
    c=legacy;base=c['root'].parent.parent
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        config={'workbench_version':'0.1.0-beta','job_execution_contract':'workbench_execution_v1',
                'worker_enabled':False,'auto_import_active':False}
        service=BackupArchiveService(projects=c['projects'],maintenance=gate,
            task_runtime_root=c['runtime'],archive_root=base/'备份 归档',runtime_config=config,machine_store=base/'machine')
        yield {**c,'gate':gate,'op':op,'service':service,'software_tree':software_tree}


def capture(c,**kwargs):
    preview=c['service'].preview(c['project'].id,operation_id=c['op'])
    return c['service'].capture(c['project'].id,operation_id=c['op'],expected_preview_sha256=preview['preview_sha256'],**kwargs)


def test_preview_bound_archive_contains_target_files_drafts_whole_db_and_explicit_diagnostic_purpose(archive_case):
    c=archive_case;root=c['root']
    draft=c['jobs'].upsert_draft(project_id=c['project'].id,chapter=1,text='尚未发布的作者草稿')
    other=c['projects'].create_project(root.parent/'另一部书',name='另一部书')
    (root/'.env').write_text('FAKE_SECRET=never-copy',encoding='utf-8')
    (root/'空目录').mkdir()
    result=capture(c);package=Path(result['path'])
    before=inventory_tree(package)
    verified=verify_archive(package)
    assert verified['archive_verified'] and not verified['recoverable'] and verified['archive_purpose']=='diagnostic'
    assert inventory_tree(package)==before
    manifest=verified['manifest'];preview=manifest['preview']
    assert preview['scope']['restore_requires_other_projects_disabled']
    assert preview['runtime_config']['restore_provider_credentials']=='reconfigure'
    assert not preview['runtime_config']['restore_start_worker']
    assert preview['scope']['scope_checks_passed'] and preview['certification_pending']
    with zipfile.ZipFile(package/'archive.zip') as zipped:
        assert 'roots/r0/空目录/' in zipped.namelist()
        assert not any(name.endswith('/.env') for name in zipped.namelist())
        assert all(b'FAKE_SECRET=never-copy' not in zipped.read(name) for name in zipped.namelist())
        copied=package/'inspect.sqlite';copied.write_bytes(zipped.read('workbench.sqlite'))
    with closing(sqlite3.connect(copied)) as connection:
        assert {row[0] for row in connection.execute('SELECT id FROM projects')}=={c['project'].id,other.id}
        assert connection.execute('SELECT text FROM drafts').fetchone()[0]==draft.text
    assert c['gate'].status()['state']=='quiescent'
    with pytest.raises(MaintenanceBlocked):c['gate'].admit('http_write')


@pytest.mark.parametrize('change',['file','database','config','software'])
def test_changed_preview_rejected_before_output_creation(archive_case,change):
    c=archive_case;service=c['service']
    preview=service.preview(c['project'].id,operation_id=c['op'])
    if change=='file':(c['root']/'changed.txt').write_text('new')
    elif change=='database':c['jobs'].upsert_draft(project_id=c['project'].id,chapter=1,text='new draft')
    elif change=='config':service.runtime_config={**service.runtime_config,'workbench_version':'changed'}
    else:(c['software_tree']/'run_workbench.py').write_text('# synthetic version B')
    with pytest.raises(BackupArchiveError,match='偏离预览'):
        service.capture(c['project'].id,operation_id=c['op'],expected_preview_sha256=preview['preview_sha256'])
    assert not service.archive_root.exists()


@pytest.mark.parametrize('event',['after_database','after_root','before_source_release','after_zip','before_completion'])
def test_interruptions_leave_no_completion_receipt(archive_case,event):
    c=archive_case
    class Stop(BaseException):pass
    def fail(name,path):
        if name==event:raise Stop()
    with pytest.raises(Stop):capture(c,fault=fail)
    packages=list(c['service'].archive_root.iterdir());assert len(packages)==1
    assert not (packages[0]/'completion.json').exists()
    with pytest.raises(BackupArchiveError):verify_archive(packages[0])
    assert c['gate'].status()['state']=='quiescent'


@pytest.mark.parametrize('damage',['zip','receipt','manifest','duplicate','path_escape'])
def test_corrupt_or_unsafe_archive_never_passes_read_only_verification(archive_case,damage):
    package=Path(capture(archive_case)['path'])
    if damage=='zip':(package/'archive.zip').write_bytes(b'invalid zip')
    elif damage=='receipt':(package/'completion.json').write_text('{}')
    elif damage=='manifest':(package/'manifest.json').write_text('{}')
    else:
        with pytest.warns(UserWarning,match='Duplicate name') if damage=='duplicate' else nullcontext():
            with zipfile.ZipFile(package/'archive.zip','a') as zipped:
                zipped.writestr('manifest.json' if damage=='duplicate' else '../escape.txt',b'bad')
    with pytest.raises(BackupArchiveError):verify_archive(package)
    assert not (package.parent/'escape.txt').exists()


def test_replaced_package_directory_cannot_receive_completion(archive_case):
    c=archive_case
    def replace(event,path):
        if event!='after_zip':return
        moved=path.with_name(path.name+'-original')
        assert moved.parent==path.parent==c['service'].archive_root
        path.rename(moved);path.mkdir()
    with pytest.raises(BackupArchiveError,match='被替换'):capture(c,fault=replace)
    assert not list(c['service'].archive_root.rglob('completion.json'))


def test_unknown_reference_can_be_preserved_as_diagnostic_without_recoverability(archive_case):
    c=archive_case
    c['jobs'].upsert_draft(project_id=c['project'].id,chapter=1,text='source absent',source_path=str(c['root'].parent/'outside.txt'))
    result=verify_archive(capture(c)['path'])
    assert not result['recoverable']
    assert {'code':'unknown_external_file_reference','reference':next(r['reference'] for r in result['manifest']['preview']['scope']['blockers'])} in result['manifest']['preview']['scope']['blockers']


def test_runtime_configuration_rejects_secret_or_unknown_fields(archive_case):
    c=archive_case
    with pytest.raises(BackupArchiveError,match='非秘密'):
        BackupArchiveService(projects=c['projects'],maintenance=c['gate'],task_runtime_root=c['runtime'],
            archive_root=c['root'].parent/'archive',runtime_config={'api_key':'synthetic'})


def test_real_reviewed_book_and_drafts_are_packed_without_claiming_restore_readiness(service_case,software_tree):
    from workbench.jobs import JobStore
    c=service_case;JobStore(c['db']);base=c['root'].parent
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        service=BackupArchiveService(projects=c['projects'],maintenance=gate,task_runtime_root=base/'jobs',
            archive_root=base/'archives',machine_store=c['machine'],runtime_config={'workbench_version':'0.1.0-beta',
                'job_execution_contract':'workbench_execution_v1','worker_enabled':False,'auto_import_active':False})
        preview=service.preview(c['project'].id,operation_id=op)
        result=service.capture(c['project'].id,operation_id=op,expected_preview_sha256=preview['preview_sha256'])
        verified=verify_archive(result['path'])
        assert verified['manifest']['preview']['scope']['scope_checks_passed']
        assert not verified['recoverable']
        ownership=verified['manifest']['preview']['scope']['machine_ownership']
        assert ownership['state']=='verified' and ownership['restore_handoff_required']
        assert ownership['record']['binding']['book_id']==c['project'].book_id
        assert ownership['record']['generation']==1
        with zipfile.ZipFile(Path(result['path'])/'archive.zip') as zipped:
            assert zipped.read('roots/r0/.novelagent/runtime/'+c['draft'].source_artifact_ref)
            assert 'roots/r0/.novelagent/runtime/reviewed/p/journals/upgrade-1/manifest.json' in zipped.namelist()


def test_real_process_exit_after_zip_keeps_incomplete_package_and_interrupted_maintenance(tmp_path):
    code='''from pathlib import Path
import os,sys
from tests.test_workbench_jobs import _job_store
from workbench.projects import ProjectRegistry
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for
from workbench.backup_archive import BackupArchiveService
base=Path(sys.argv[1]);_,store,project_id=_job_store(base)
projects=ProjectRegistry(store.database,allowed_workspaces=[base/'books'])
lease=InstanceLease(base).acquire();gate=maintenance_for(lease);op=gate.begin()['operation_id']
service=BackupArchiveService(projects=projects,maintenance=gate,task_runtime_root=base/'jobs',archive_root=base/'archives',
 machine_store=base/'machine',runtime_config={'workbench_version':'0.1.0-beta','job_execution_contract':'workbench_execution_v1','worker_enabled':False,'auto_import_active':False})
preview=service.preview(project_id,operation_id=op)
def crash(event,path):
 if event=='before_completion':
  print('ZIP_WRITTEN',flush=True);os._exit(23)
service.capture(project_id,operation_id=op,expected_preview_sha256=preview['preview_sha256'],fault=crash)
'''
    result=subprocess.run([sys.executable,'-B','-c',code,str(tmp_path)],capture_output=True,timeout=15,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert result.returncode==23 and b'ZIP_WRITTEN' in result.stdout,result.stderr
    packages=list((tmp_path/'archives').iterdir());assert len(packages)==1
    assert (packages[0]/'archive.zip').is_file() and not (packages[0]/'completion.json').exists()
    with pytest.raises(BackupArchiveError):verify_archive(packages[0])
    with closing(InstanceLease(tmp_path).acquire()) as lease:
        assert maintenance_for(lease).status()['state']=='interrupted'


@pytest.mark.parametrize('target',['manifest','software'])
def test_final_metadata_or_software_change_never_publishes_completion(archive_case,target):
    c=archive_case
    def damage(event,path):
        if target=='manifest' and event=='before_completion':(path/'manifest.json').write_text('{}')
        if target=='software' and event=='before_source_release':(c['software_tree']/'run_workbench.py').write_text('# changed')
    with pytest.raises(BackupArchiveError,match='发生变化'):capture(c,fault=damage)
    assert not list(c['service'].archive_root.rglob('completion.json'))


def test_configuration_cannot_be_mutated_to_export_unknown_secret_fields(archive_case):
    service=archive_case['service'];view=service.runtime_config
    view['api_key']='FAKE_SECRET'
    assert 'api_key' not in service.runtime_config
    with pytest.raises(BackupArchiveError,match='非秘密'):service.runtime_config=view
    assert 'api_key' not in service.preview(archive_case['project'].id,operation_id=archive_case['op'])['runtime_config']


def test_self_consistent_zip_and_receipt_cannot_omit_a_previewed_file(archive_case):
    c=archive_case;(c['root']/'author-notes.txt').write_text('keep this')
    package=Path(capture(c)['path'])
    manifest=json.loads((package/'manifest.json').read_bytes())
    manifest['members'].pop('roots/r0/author-notes.txt')
    manifest['manifest_sha256']=digest({k:v for k,v in manifest.items() if k!='manifest_sha256'})
    from workbench.backup_archive import _json
    with zipfile.ZipFile(package/'archive.zip') as archive:
        payload={name:archive.read(name) for name in archive.namelist() if name!='roots/r0/author-notes.txt'}
    payload['manifest.json']=_json(manifest)
    with zipfile.ZipFile(package/'archive.zip','w') as archive:
        for name,content in payload.items():archive.writestr(name,content)
    (package/'manifest.json').write_bytes(_json(manifest))
    receipt=json.loads((package/'completion.json').read_bytes());raw=(package/'archive.zip').read_bytes()
    receipt['archive']={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    receipt['manifest_sha256']=manifest['manifest_sha256']
    receipt['receipt_sha256']=digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})
    (package/'completion.json').write_bytes(_json(receipt))
    with pytest.raises(BackupArchiveError,match='来源范围'):verify_archive(package)


@pytest.mark.parametrize('name',['roots/r0/CON.txt','roots/r0/trailing.','roots/r0/file /notes.txt','roots/r0/a?.txt'])
def test_archive_paths_reject_windows_aliases_and_invalid_names(name):
    from workbench.backup_archive import _member
    with pytest.raises(BackupArchiveError,match='Windows'):_member(name)


def test_software_fingerprint_tracks_code_and_locks_without_exporting_contents(software_tree):
    from workbench.backup_version import fingerprint_software
    (software_tree/'.env').write_text('FAKE_SECRET')
    before=fingerprint_software()
    assert set(before['files'])=={'run_workbench.py','requirements.lock'}
    assert 'FAKE_SECRET' not in json.dumps(before)
    assert not before['loaded_modules_match_proven']
    (software_tree/'requirements.lock').write_text('# version changed')
    assert fingerprint_software()['fingerprint_sha256']!=before['fingerprint_sha256']


def test_archive_contains_overlapping_legacy_history_proof(archive_case):
    from tests.test_backup_history import commit
    c=archive_case
    commit(c,'chapter-first',{'state.txt':'first'})
    commit(c,'chapter-second',{'state.txt':'second'})
    package=Path(capture(c)['path']);result=verify_archive(package)
    checks=result['manifest']['preview']['scope']['checks']
    history=next(check['evidence'] for check in checks if check['check']=='legacy_v1_history')
    assert len(history['transactions'])==2
    assert history['consistent_reverse_order'][0].endswith('/chapter-second')
    assert not result['recoverable']
    with zipfile.ZipFile(package/'archive.zip') as archive:
        assert archive.read('roots/r0/state.txt')==b'second'
        assert archive.read('roots/r0/.novelagent/runtime/runs/transactions/chapter-first/staged/000.bin')==b'first'
