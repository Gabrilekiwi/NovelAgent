"""A persistent restore session must keep a usable completion or abort path."""
import json

from fastapi.testclient import TestClient
import pytest

from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_restore_database import logical
from tests.test_recovery_sessions import controller, start, step, advance
from tests.test_restore_budget_preparation import make_financial_source
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.restore_scope import load_restore_scope


@pytest.mark.parametrize('phase',['planned','readonly_ready','handoff_ready'])
def test_cancel_before_handoff_preserves_copy_and_source_and_allows_original_maintenance_to_finish(tmp_path,phase):
    c=make_financial_source(include_uncertain=False)
    before_book,before_sql=inventory_tree(c['root']),logical(c['db'].path)
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    before_machine=owner.record_path.read_bytes()
    target=tmp_path/'copy';app=controller(c,tmp_path/'controller')
    with TestClient(app) as client:
        session=advance(client,start(client,c,target),phase)
        retained=inventory_tree(target) if target.exists() else None
        response=client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']})
        assert response.status_code==409,response.text
        assert client.get('/api/maintenance').json()['state']=='quiescent'
        assert app.state.recovery_sessions.get(session['session_id'])==session
        cancelled=step(client,session,action='cancel')
        assert cancelled.status_code==200,cancelled.text
        assert cancelled.json()['phase']=='cancelled' and cancelled.json()['next_action'] is None
        assert not cancelled.json()['activated']
        assert step(client,session,action='cancel').json()==cancelled.json()
        if retained is None:assert not target.exists()
        else:assert inventory_tree(target)==retained
        assert owner.record_path.read_bytes()==before_machine
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200
    assert inventory_tree(c['root'])==before_book and logical(c['db'].path)==before_sql


def test_cancel_durable_pending_copy_does_not_reexecute_or_delete_target(tmp_path):
    c=make_financial_source(include_uncertain=False)
    target=tmp_path/'copy';app=controller(c,tmp_path/'controller')
    with TestClient(app) as client:
        session=start(client,c,target)
        def lost(event,state):
            if event=='after_session_operation' and state.get('pending_action')=='materialize':
                raise RuntimeError('lost materialization response')
        app.state.recovery_sessions.fault_injector=lost
        assert step(client,session).status_code==409
        pending=app.state.recovery_sessions.get(session['session_id'])
        assert pending['pending_action']=='materialize'
        before=inventory_tree(target)
        app.state.recovery_sessions.fault_injector=None
        response=step(client,pending,action='cancel')
        assert response.status_code==200,response.text
        assert response.json()['phase']=='cancelled' and response.json()['pending_action'] is None
        assert inventory_tree(target)==before
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200


def test_finish_or_cancel_after_actual_revocation_cannot_discard_original_resume_control(tmp_path):
    c=make_financial_source(include_uncertain=False)
    target=tmp_path/'copy';app=controller(c,tmp_path/'controller')
    with TestClient(app) as client:
        session=advance(client,start(client,c,target),'source_revoked')
        owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
        before=owner.record_path.read_bytes()
        assert json.loads(before)['state']=='restoring'
        assert step(client,session,action='cancel').status_code==409
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==409
        assert app.state.recovery_sessions.get(session['session_id'])==session
        assert owner.record_path.read_bytes()==before
        rollback=step(client,session,action='rollback')
        assert rollback.status_code==200,rollback.text
        assert rollback.json()['phase']=='rolled_back'
        assert json.loads(owner.record_path.read_text(encoding='utf-8'))['state']=='active'
        assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200


def test_legacy_pending_activation_without_core_intent_can_explicitly_rollback_but_never_activate(tmp_path):
    c=make_financial_source(include_uncertain=True)
    target=tmp_path/'copy';app=controller(c,tmp_path/'controller')
    with TestClient(app) as client:
        session=advance(client,start(client,c,target),'execution_prepared')
        service=app.state.recovery_sessions
        intent,state=service._load(session['session_id'])
        # Persist exactly the state emitted by the previous orchestrator after
        # a preflight rejection, without manufacturing any core activation.
        pending_state=service._write(intent,state,pending_action='activate',pending_input_sha256=state['state_sha256'])
        pending=service._public(intent,pending_state)
        assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        response=step(client,pending,action='rollback')
        assert response.status_code==200,response.text
        assert response.json()['phase']=='rolled_back' and response.json()['pending_action'] is None
        owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
        assert json.loads(owner.record_path.read_text(encoding='utf-8'))['state']=='active'
        assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200
