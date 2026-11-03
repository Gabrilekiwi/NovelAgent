"""Public author-confirmed upgrade joined to actual certified writable restore."""
import json
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.test_onboarding_upgrade import (upgrade_case,registration_case,legacy,tmp_path,
    private_temporary_directory)
from tests.test_legacy_registration import contents
from tests.test_backup_restore_database import logical
from tests.test_registration_upgrade_archive import copied,activate,read_twice
from workbench.app import create_app
from workbench.backup_archive import verify_archive
from workbench.backup_certification import verify_recovery_certificate
from workbench.restore_activation import RestoreActivation


def post(client,path,body):
    response=client.post(path,json=body)
    assert response.status_code==200,response.text
    return response.json()


@pytest.mark.parametrize('archive_position',['before','after'])
def test_public_upgrade_certified_archive_restores_author_review_and_draft_without_rewriting_source(upgrade_case,archive_position):
    c=upgrade_case;client=c['client'];project=c['project'];base='/api/projects/'+project.id
    client.app.state.onboarding_upgrade.temporary_root=c['scratch']
    original_package=Path(c['new_package']['path']);original_archive=contents(original_package)
    old_version=json.loads((c['root']/'.novelagent/project.json').read_bytes())['schema_version']
    value=post(client,base+'/onboarding/upgrade/preview',c['request'])
    result=post(client,base+'/onboarding/upgrade/commit',{**c['request'],
        'expected_preview_sha256':value['preview_sha256'],'acknowledged_policy_sha256':value['recovery_policy_sha256'],
        'confirmed':True})
    assert result['committed'] and result['registration_generation']==2
    package=original_package;version=old_version
    if archive_position=='after':
        body={'operation_id':c['op']}
        preview=post(client,base+'/backups/preview',body)
        saved=post(client,base+'/backups',{**body,'expected_preview_sha256':preview['preview_sha256'],
            'archive_purpose':'diagnostic'})
        package=Path(saved['path']);version='3.0'
        path=base+'/backups/'+package.name+'/certification'
        assessment=post(client,path+'/preview',body)
        assert assessment['can_certify'],assessment['blockers']
        certified=post(client,path,{**body,'expected_assessment_sha256':assessment['assessment_sha256'],'confirmed':True})
        assert certified['recoverable'] and not certified['write_activation_allowed']
    certificate=verify_recovery_certificate(package,project_id=project.id,book_id=project.book_id,temporary_root=c['scratch'])
    assert certificate['recoverable'] and not certificate['write_activation_allowed']
    assert not certificate['policy']['restore']['source_unavailable_write_activation_supported']
    assert verify_archive(package)['recoverable'] is False
    archive=contents(package);source=contents(c['root']);sql=logical(c['app'].state.database.path)
    c['app'].state.maintenance.finish(c['op'])
    target=Path(__file__).resolve().parents[1]/'.tmp'/('复 原-'+uuid.uuid4().hex[:6])
    copied(c,package,target)
    service=RestoreActivation(project.book_id,machine_store=c['machine'])
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    activate(c,service,package,intent)
    read_twice(c,target,version)
    restored=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'])
    with TestClient(restored) as restored_client:
        assert restored.state.supervisor._thread is None
        history=restored_client.get(base+'/onboarding/baselines/'+c['review']['id'])
        assert history.status_code==200,history.text
        assert history.json()['record']==c['review']
        assert history.json()['source_checked'] is False and history.json()['can_upgrade'] is False
    assert contents(c['root'])==source and logical(c['app'].state.database.path)==sql
    assert contents(package)==archive and contents(original_package)==original_archive
    # The source lost present writer ownership through handoff, but its
    # immutable completed product result remains a historical read.
    found=client.get(base+'/onboarding/upgrade/operations/'+value['preview_sha256'])
    assert found.status_code==200,found.text
    assert found.json()['result']==result
