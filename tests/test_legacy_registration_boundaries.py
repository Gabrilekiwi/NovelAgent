"""Public registration must retain local auth and maintenance/restore fences."""
from fastapi.testclient import TestClient
import pytest

from tests.test_backup_api import configured, begin
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_restore_scope import prepared_scope, open_app, logical
from tests.test_workbench_app import _unlock
from workbench.backup_files import inventory_tree


@pytest.mark.parametrize('suffix', ['', '/preview'])
def test_registration_routes_reject_before_service_without_local_session_or_quiescence(tmp_path, monkeypatch, suffix):
    client, _ = configured(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('registration service ran before request admission')
    service=client.app.state.legacy_registration
    monkeypatch.setattr(service, 'register', forbidden)
    monkeypatch.setattr(service, 'preview', forbidden)
    url='/api/projects/absent/onboarding/registration'+suffix
    with client:
        assert client.post(url,json={}).status_code==403
        _unlock(client)
        assert client.post(url,json={},headers={'origin':'https://external.invalid'}).status_code==403
        assert client.post(url,json={},headers={'host':'untrusted.invalid'}).status_code==400
        payload={'operation_id':'a'*32,'archive_id':'backup_'+'b'*32}
        if not suffix:payload.update(expected_preview_sha256='c'*64,confirmed=True)
        assert client.post(url,json=payload).status_code==409
        gate=client.app.state.maintenance
        with gate.admit('local_writer'):
            op=begin(client);payload['operation_id']=op
            assert client.post(url,json=payload).status_code==423
        payload['operation_id']='d'*32
        assert client.post(url,json=payload).status_code==409
        payload['operation_id']=op;payload['override']=True
        assert client.post(url,json=payload).status_code==409
        assert not client.app.state.backups.archive_root.exists()


def test_read_only_restore_rejects_both_registration_routes_before_service(prepared_scope,monkeypatch):
    c=prepared_scope;before=logical(c['database']);book=inventory_tree(c['root'])
    app=open_app(c)
    def forbidden(*args,**kwargs):
        pytest.fail('read-only restore entered first legacy registration')
    monkeypatch.setattr(app.state.legacy_registration,'register',forbidden)
    monkeypatch.setattr(app.state.legacy_registration,'preview',forbidden)
    with TestClient(app) as client:
        _unlock(client)
        for suffix in ('','/preview'):
            result=client.post('/api/projects/'+c['target'].id+'/onboarding/registration'+suffix,
                json={'operation_id':'a'*32,'archive_id':'backup_'+'b'*32,
                    'expected_preview_sha256':'c'*64,'confirmed':True})
            assert result.status_code==409,result.text
            assert result.json()['error']['code']=='restore_scope_read_only'
    assert logical(c['database'])==before and inventory_tree(c['root'])==book
