"""Public routes retain the real source, archive, maintenance and auth gates."""
from pathlib import Path

import os
import pytest

from tests.platform_requirements import require_windows_recovery
from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case,contents
from tests.test_registration_upgrade_archive import old_package,tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import logical
from tests.test_backup_api import configured,begin
from tests.test_workbench_app import _unlock,_create_project
from workbench.backup_certification import CERTIFICATE


def url(c,archive,suffix=''):
    return f"/api/projects/{c['project'].id}/backups/{archive}/certification"+suffix


def test_public_actual_certificate_confirmation_stale_preview_and_offline_reverification(registration_case):
    c=registration_case;package=old_package(c);client=c['client'];route=url(c,package.name)
    body={'operation_id':c['op']};original=contents(package);book=contents(c['root']);sql=logical(c['app'].state.database.path)
    value=client.post(route+'/preview',json=body)
    assert value.status_code==200,value.text
    preview=value.json()
    if os.name!='nt':
        assert not preview['can_certify']
        assert 'unsupported_certification_host' in {b['code'] for b in preview['blockers']}
        refused=client.post(route,json={**body,'expected_assessment_sha256':preview['assessment_sha256'],'confirmed':True})
        assert refused.status_code==409 and not (package/CERTIFICATE).exists()
        assert contents(package)==original and contents(c['root'])==book
        return
    assert preview['can_certify'],preview['blockers']
    assert not preview['write_activation_allowed'] and not preview['automatic_provider_calls_allowed']
    assert contents(package)==original and contents(c['root'])==book
    issue={**body,'expected_assessment_sha256':preview['assessment_sha256']}
    assert client.post(route,json=issue).status_code==409
    assert client.post(route,json={**issue,'confirmed':'true'}).status_code==409
    assert client.post(route,json={**issue,'confirmed':True,'can_certify':True}).status_code==409
    assert client.post(route,json={**issue,'confirmed':True,'expected_assessment_sha256':'f'*64}).status_code==409
    assert not (package/CERTIFICATE).exists()
    result=client.post(route,json={**issue,'confirmed':True})
    assert result.status_code==200,result.text
    assert result.json()['recoverable'] and not result.json()['write_activation_allowed']
    assert not result.json()['policy']['restore']['source_unavailable_write_activation_supported']
    assert {k:v for k,v in contents(package).items() if k!=CERTIFICATE}==original
    stat=(package/CERTIFICATE).stat()
    replay=client.post(route,json={**issue,'confirmed':True})
    assert replay.status_code==200 and replay.json()==result.json()
    assert (package/CERTIFICATE).stat().st_ino==stat.st_ino and (package/CERTIFICATE).stat().st_mtime_ns==stat.st_mtime_ns
    with absent_source(c['root']):
        checked=client.post(route+'/verify',json=body)
        assert checked.status_code==200 and checked.json()==result.json(),checked.text
    assert client.post(url(c,package.name).replace(c['project'].id,'absent')+'/verify',json=body).status_code==409
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
    assert client.get('/api/maintenance').json()['state']=='quiescent'


@pytest.mark.parametrize('suffix',['/preview','','/verify'])
def test_public_certification_keeps_session_origin_intent_and_maintenance_guards(tmp_path,suffix):
    client,workspace=configured(tmp_path);archive='backup_'+'a'*32
    with client:
        route=f'/api/projects/absent/backups/{archive}/certification'+suffix
        assert client.post(route,json={}).status_code==403
        _unlock(client)
        assert client.post(route,json={},headers={'origin':'https://external.invalid'}).status_code==403
        assert client.post(route,json={},headers={'host':'untrusted.invalid'}).status_code==400
        payload={'operation_id':'a'*32}
        if not suffix:payload.update(expected_assessment_sha256='b'*64,confirmed=True)
        assert client.post(route,json=payload).status_code==409
        op=begin(client);payload['operation_id']=op
        assert client.post(route,json={**payload,'archive_root':str(tmp_path/'elsewhere')}).status_code==409
        assert client.post(route.replace(archive,'invalid'),json=payload).status_code==409
        assert not client.app.state.backups.archive_root.exists()


def test_source_change_after_public_preview_refuses_issuance_without_sidecar(registration_case):
    require_windows_recovery()
    c=registration_case;package=old_package(c);route=url(c,package.name)
    response=c['client'].post(route+'/preview',json={'operation_id':c['op']})
    assert response.status_code==200 and response.json()['can_certify'],response.text
    (c['root']/'changed.txt').write_text('author changed after confirmation preview')
    book=contents(c['root'])
    refused=c['client'].post(route,json={'operation_id':c['op'],'confirmed':True,
        'expected_assessment_sha256':response.json()['assessment_sha256']})
    assert refused.status_code==409,refused.text
    assert not (package/CERTIFICATE).exists() and contents(c['root'])==book
