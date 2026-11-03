"""Reuse an immutable qualified-source archive through the real HTTP protocol."""
from fastapi.testclient import TestClient

from tests.test_recovery_restored_controller import (
    tmp_path, original, case, restored_client, begin_second, unchanged,
)
from tests.test_recovery_sessions import start, advance, step
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.restore_scope import load_restore_scope


def test_qualified_source_reuses_pre_rollback_archive_restarts_controller_and_activates(case):
    c = case
    archived = inventory_tree(c['rebackup'])
    qualification = (c['source']/'write-qualification.json').read_bytes()
    with restored_client(c) as (app, client):
        first = advance(client, begin_second(c, client), 'source_revoked')
        _, state = app.state.recovery_sessions._load(first['session_id'])
        first_handoff = state['receipts']['preview_handoff']
        assert first_handoff['schema_version'] == 'machine_restore_handoff_v2'
        response = step(client, first, action='rollback')
        assert response.status_code == 200, response.text
        assert response.json()['phase'] == 'rolled_back'
        assert client.post('/api/maintenance/finish', json={'operation_id':first['operation_id']}).status_code == 200

    target = c['target'].parent/'回退后 再恢复副本'
    with restored_client(c) as (app, client):
        session = start(client, {**c, 'package':c['rebackup']}, target)
        session = advance(client, session, 'source_revoked')
        _, state = app.state.recovery_sessions._load(session['session_id'])
        handoff = state['receipts']['preview_handoff']
        assert handoff['schema_version'] == 'machine_restore_handoff_v4'
        archived_proof = handoff['archived_source_restore_provenance']
        current_proof = handoff['source_restore_provenance']
        assert archived_proof == first_handoff['source_restore_provenance']
        assert archived_proof['active_record'] == first_handoff['source_record']
        assert current_proof['active_record'] == handoff['source_record']
        assert handoff['source_record']['generation'] == first_handoff['source_record']['generation']+2
        assert current_proof['qualification'] == archived_proof['qualification']
        assert len(current_proof['qualification_reinstatements']) == 1
        assert current_proof['qualification_reinstatements'][0]['intent']['handoff_intent'] == first_handoff
        assert client.get('/api/restore-scope').json()['controller']['session_id'] == session['session_id']

    with restored_client(c) as (app, client):
        assert app.state.restore_scope.controller['session_id'] == session['session_id']
        assert client.get('/api/maintenance').json()['state'] == 'interrupted'
        assert client.post('/api/maintenance/resume', json={'operation_id':session['operation_id']}).status_code == 200
        session = advance(client, session, 'active')
        assert client.post('/api/maintenance/finish', json={'operation_id':session['operation_id']}).status_code == 200
        assert client.get('/api/projects').status_code == 409

    for _ in range(2):
        app = create_app(database_path=target/'workbench.sqlite', start_worker=True,
            auto_import_active=True, chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            assert app.state.supervisor._thread is None
            assert client.get('/api/restore-scope').json()['state'] == 'active'
            response = client.get('/api/projects/'+c['project'].id+'/chapters/2')
            assert response.status_code == 200, response.text
            assert response.json()['has_prose']
            response = client.get('/api/projects/'+c['project'].id+'/chapters/'+str(c['draft'].chapter))
            assert response.status_code == 200, response.text
            assert response.json()['retained_author_draft']['text'] == c['draft'].text
    assert load_restore_scope(target/'workbench.sqlite', machine_store=c['machine']).document['scope_generation'] == 2
    assert (c['source']/'write-qualification.json').read_bytes() == qualification
    assert inventory_tree(c['rebackup']) == archived
    unchanged(c)
