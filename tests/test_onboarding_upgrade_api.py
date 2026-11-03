"""Real HTTP upgrade admission, original result query and explicit continuation."""
import pytest

from tests.test_onboarding_upgrade import (upgrade_case,registration_case,legacy,tmp_path,
    private_temporary_directory,preview,commit)
from tests.test_legacy_registration import contents
from tests.test_backup_restore_database import logical
from workbench.book_ownership import BookOwnership


def endpoint(c):return '/api/projects/'+c['project'].id+'/onboarding/upgrade'


def test_public_certified_upgrade_strict_confirmation_query_replay_and_bad_proof(upgrade_case):
    c=upgrade_case;client=c['client'];service=c['app'].state.onboarding_upgrade
    service.temporary_root=c['scratch'];base=endpoint(c)
    book=contents(c['root']);sql=logical(c['app'].state.database.path)
    value=client.post(base+'/preview',json=c['request'])
    assert value.status_code==200,value.text
    value=value.json()
    assert client.get(base+'/operations/'+value['preview_sha256']).json()['state']=='not_started'
    body={**c['request'],'expected_preview_sha256':value['preview_sha256'],
        'acknowledged_policy_sha256':value['recovery_policy_sha256'],'confirmed':True}
    for invalid in ({'confirmed':False},{'confirmed':'true'},{'expected_revision':True},
                    {'state':{}},{'can_upgrade':True},{'publication_authorized':True}):
        refused=client.post(base+'/commit',json={**body,**invalid})
        assert refused.status_code==409,refused.text
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
    response=client.post(base+'/commit',json=body)
    assert response.status_code==200,response.text
    result=response.json();assert result['committed'] and not result['publication_authorized']
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    completed=contents(c['root']);machine=contents(owner.directory)
    for _ in range(2):
        found=client.get(base+'/operations/'+value['preview_sha256'])
        assert found.status_code==200,found.text
        assert found.json()['state']=='completed' and found.json()['result']==result
    assert client.post(base+'/commit',json=body).json()==result
    assert contents(c['root'])==completed and contents(owner.directory)==machine
    assert logical(c['app'].state.database.path)==sql
    manifest=c['root']/'.novelagent/runtime/reviewed/p/journals'/value['run_id']/'manifest.json'
    raw=manifest.read_bytes();manifest.write_text('{}',encoding='utf-8')
    try:
        damaged=contents(c['root'])
        assert client.get(base+'/operations/'+value['preview_sha256']).status_code==409
        assert contents(c['root'])==damaged and contents(owner.directory)==machine
    finally:manifest.write_bytes(raw)


def test_public_original_preparation_continue_is_explicit_and_get_never_advances(upgrade_case):
    c=upgrade_case;client=c['client'];service=c['app'].state.onboarding_upgrade
    service.temporary_root=c['scratch'];c['upgrade']=service;value=preview(c);base=endpoint(c)
    class Interrupted(BaseException):pass
    def stop(name):
        if name=='after_onboarding_prepare':raise Interrupted()
    service.fault_injector=stop
    with pytest.raises(Interrupted):commit(c,value)
    service.fault_injector=None
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    before=contents(c['root']);machine=contents(owner.directory);sql=logical(c['app'].state.database.path)
    for _ in range(2):
        found=client.get(base+'/operations/'+value['preview_sha256'])
        assert found.status_code==200 and found.json()['state']=='prepared'
    body={'operation_id':c['op'],'preview_sha256':value['preview_sha256'],
        'acknowledged_policy_sha256':value['recovery_policy_sha256'],'confirmed':True}
    assert client.post(base+'/continue',json={**body,'confirmed':False}).status_code==409
    assert contents(c['root'])==before and contents(owner.directory)==machine
    result=client.post(base+'/continue',json=body)
    assert result.status_code==200,result.text
    assert result.json()['state']=='completed'
    assert client.get(base+'/operations/'+value['preview_sha256']).json()==result.json()
    assert logical(c['app'].state.database.path)==sql
