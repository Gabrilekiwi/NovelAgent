"""Archive version evidence must remain bound to its running service session."""
from pathlib import Path
import hashlib
import json
import zipfile

import pytest

from tests.test_backup_archive import archive_case, software_tree, legacy, capture
from workbench.backup_archive import BackupArchiveError, verify_archive
from workbench.backup_files import inventory_tree
from core.story_project.reviewed_state_document import digest


@pytest.mark.parametrize('change',['code','lock','new','missing'])
def test_changed_software_before_first_preview_requires_restart(archive_case,change):
    c=archive_case
    before=inventory_tree(c['root'])
    if change=='code':(c['software_tree']/'run_workbench.py').write_text('# replaced after service construction')
    elif change=='lock':(c['software_tree']/'requirements.lock').write_text('# replaced dependency lock')
    elif change=='new':(c['software_tree']/'main.py').write_text('# new implementation file')
    else:(c['software_tree']/'run_workbench.py').unlink()
    with pytest.raises(BackupArchiveError,match='重启'):
        c['service'].preview(c['project'].id,operation_id=c['op'])
    assert not c['service'].archive_root.exists()
    assert inventory_tree(c['root'])==before


@pytest.mark.parametrize('phase',['after_zip','before_completion'])
def test_late_source_replacement_cannot_publish_completion(archive_case,phase):
    c=archive_case
    def change(event,path):
        if event==phase:(c['software_tree']/'run_workbench.py').write_text('# changed after source locks released')
    with pytest.raises(BackupArchiveError,match='重启'):
        capture(c,fault=change)
    assert not list(c['service'].archive_root.rglob('completion.json'))


def rewrite_manifest(package,manifest):
    """Rehash the entire synthetic package, so failure is a semantic check."""
    from workbench.backup_archive import _json
    preview=manifest['preview']
    preview['preview_sha256']=digest({k:v for k,v in preview.items() if k!='preview_sha256'})
    manifest['manifest_sha256']=digest({k:v for k,v in manifest.items() if k!='manifest_sha256'})
    with zipfile.ZipFile(package/'archive.zip') as archive:
        payload={name:archive.read(name) for name in archive.namelist()}
    payload['manifest.json']=_json(manifest)
    with zipfile.ZipFile(package/'archive.zip','w') as archive:
        for name,raw in payload.items():archive.writestr(name,raw)
    (package/'manifest.json').write_bytes(_json(manifest))
    receipt=json.loads((package/'completion.json').read_bytes())
    raw=(package/'archive.zip').read_bytes()
    receipt.update(archive={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},
        manifest_sha256=manifest['manifest_sha256'],preview_sha256=preview['preview_sha256'])
    receipt['receipt_sha256']=digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})
    (package/'completion.json').write_bytes(_json(receipt))


@pytest.mark.parametrize('change',['another_version','memory_claim'])
def test_rehashed_package_rejects_incompatible_session_evidence(archive_case,change):
    package=Path(capture(archive_case)['path'])
    manifest=verify_archive(package)['manifest'];session=manifest['preview']['source_session']
    if change=='another_version':session['startup_fingerprint_sha256']='f'*64
    else:session['loaded_modules_match_proven']=True
    session['evidence_sha256']=digest({k:v for k,v in session.items() if k!='evidence_sha256'})
    rewrite_manifest(package,manifest)
    with pytest.raises(BackupArchiveError):verify_archive(package)


def test_older_diagnostic_package_without_session_still_verifies(archive_case):
    package=Path(capture(archive_case)['path'])
    manifest=verify_archive(package)['manifest']
    session=manifest['preview'].pop('source_session')
    assert session['instance_id']==archive_case['gate'].lease.instance_id
    assert session['startup_fingerprint_sha256']==manifest['preview']['software']['fingerprint_sha256']
    assert session['loaded_modules_match_proven'] is False
    rewrite_manifest(package,manifest)
    before=inventory_tree(package)
    assert verify_archive(package)['recoverable'] is False
    assert inventory_tree(package)==before


def test_http_repreview_refuses_updated_source_until_actual_app_restart(tmp_path,software_tree):
    from fastapi.testclient import TestClient
    from tests.test_backup_api import configured,begin,preview,capture_body
    from tests.test_workbench_app import _unlock,_create_project
    from workbench.app import create_app
    client,workspace=configured(tmp_path)
    with client:
        _unlock(client);project=_create_project(client,workspace);op=begin(client)
        original=preview(client,project,op)
        (software_tree/'run_workbench.py').write_text('# installation changed while app was running')
        url='/api/projects/'+project['id']+'/backups'
        for response in (client.post(url+'/preview',json={'operation_id':op}),
                client.post(url,json=capture_body(original))):
            assert response.status_code==409,response.text
            assert '重启' in response.json()['error']['message']
        assert not client.app.state.backups.archive_root.exists()
    app=create_app(database_path=tmp_path/'workbench.db',runtime_root=tmp_path/'job-runtime',
        allowed_workspaces=[workspace],start_worker=False,auto_import_active=False,
        chapter_publication_machine_store=tmp_path/'machine')
    with TestClient(app) as restarted:
        _unlock(restarted)
        assert app.state.maintenance.status()['state']=='interrupted'
        assert restarted.post('/api/maintenance/resume',json={'operation_id':op}).status_code==200
        updated=preview(restarted,project,op)
        assert updated['software_fingerprint_sha256']!=original['software_fingerprint_sha256']
        result=restarted.post(url,json=capture_body(updated))
        assert result.status_code==200,result.text
        verified=verify_archive(Path(result.json()['path']))
        assert verified['manifest']['preview']['source_session']['instance_id']==app.state.instance_lease.instance_id
        assert verified['recoverable'] is False
