"""Actual restore protocol through durable HTTP sessions; no Provider or live book."""
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.platform_requirements import require_windows_recovery
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_restore_database import logical
from tests.test_restore_budget_preparation import make_financial_source
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.restore_scope import load_restore_scope


@pytest.fixture(scope='module')
def source():
    return make_financial_source(include_uncertain=False)


def controller(c, directory):
    def forbidden(*args,**kwargs):
        pytest.fail('recovery session invoked an executor')
    app = create_app(database_path=directory/'workbench.db',runtime_root=directory/'jobs',
        allowed_workspaces=[c['root'].parent],start_worker=False,auto_import_active=False,
        chapter_publication_machine_store=c['machine'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    app.state.backups.archive_root = c['package'].parent
    app.state.recovery_sessions.archive_root = c['package'].parent
    return app


def start(client, c, target):
    require_windows_recovery()
    client.get('/')
    gate = client.post('/api/maintenance/begin',json={'purpose':'restore'})
    assert gate.status_code == 200,gate.text
    operation = gate.json()['operation_id']
    data = dict(operation_id=operation,archive_id=c['package'].name,destination=str(target))
    preview = client.post('/api/recovery-sessions/preview',json=data)
    assert preview.status_code == 200,preview.text
    created = client.post('/api/recovery-sessions',json={**data,'expected_plan_sha256':preview.json()['plan_sha256'],
        'session_id':'restore_'+uuid.uuid4().hex})
    assert created.status_code == 200,created.text
    return created.json()


def step(client, session, *, action=None, resume=False, confirmed=True):
    action = action or session['next_action']
    response = client.post('/api/recovery-sessions/'+session['session_id']+'/steps/'+action,
        json={'operation_id':session['operation_id'],'expected_state_sha256':session['state_sha256'],
            'confirmed':confirmed,'resume':resume})
    return response


def advance(client, session, stop):
    while session['phase'] != stop:
        response = step(client,session)
        assert response.status_code == 200,response.text
        session = response.json()
    return session


def test_actual_http_session_restarts_after_revocation_keeps_drafts_budget_and_activates(source,tmp_path):
    c = source; directory=tmp_path/'controller';target=tmp_path/'恢复 副本'
    before_book,before_sql = inventory_tree(c['root']),logical(c['db'].path)
    app = controller(c,directory)
    with TestClient(app) as client:
        session = advance(client,start(client,c,target),'readonly_ready')
        read = client.post('/api/recovery-sessions/'+session['session_id']+'/readback',
            json={'operation_id':session['operation_id'],'draft_id':c['draft'].id,'chapter':2})
        assert read.status_code == 200,read.text
        assert read.json()['author_draft']['text'] == c['draft'].text
        assert [item['chapter'] for item in read.json()['chapters']] == [1,2]
        assert c['other_draft'].id not in [item['id'] for item in read.json()['author_drafts']]
        assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        session = advance(client,session,'handoff_ready')
        before = session
        assert step(client,session,confirmed=False).status_code == 409
        assert app.state.recovery_sessions.get(session['session_id']) == before
        def interrupted(phase,state):
            if phase == 'after_session_operation' and state.get('pending_action') == 'handoff':
                raise RuntimeError('synthetic lost response after actual revocation')
        app.state.recovery_sessions.fault_injector = interrupted
        response = step(client,session)
        assert response.status_code == 409,response.text
        session = app.state.recovery_sessions.get(session['session_id'])
        assert session['phase'] == 'handoff_ready' and session['pending_action'] == 'handoff'
        owner = BookOwnership(c['project'].book_id,machine_store=c['machine'])
        assert json.loads(owner.record_path.read_text(encoding='utf-8'))['state'] == 'restoring'
        assert step(client,session,action='handoff',resume=False).status_code == 409
    # New application/lease, original durable maintenance and session only.
    app = controller(c,directory)
    with TestClient(app) as client:
        client.get('/')
        assert client.get('/api/maintenance').json()['state'] == 'interrupted'
        assert client.get('/api/recovery-sessions/'+session['session_id']).json() == session
        assert json.loads(owner.record_path.read_text(encoding='utf-8'))['state'] == 'restoring'
        assert client.post('/api/maintenance/resume',json={'operation_id':session['operation_id']}).status_code == 200
        response = step(client,session,action='handoff',resume=True)
        assert response.status_code == 200,response.text
        session = advance(client,response.json(),'execution_prepared')
        assert session['usage_settled'] is True and not session['usage_blockers']
        assert step(client,session,confirmed=False).status_code == 409
        response = step(client,session)
        assert response.status_code == 200,response.text
        session = response.json()
        assert session['activated'] and not session['automatic_provider_calls_allowed']
        scope = load_restore_scope(target/'workbench.sqlite',machine_store=c['machine'])
        assert scope.active and scope.task_runtime_root == target/'next-jobs'
        read = client.post('/api/recovery-sessions/'+session['session_id']+'/readback',
            json={'operation_id':session['operation_id'],'draft_id':c['draft'].id})
        assert read.status_code == 200,read.text
        assert read.json()['author_draft']['text'] == c['draft'].text
        assert app.state.supervisor._thread is None
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code == 200
    assert inventory_tree(c['root']) == before_book and logical(c['db'].path) == before_sql


@pytest.mark.parametrize('scenario', ['skip','stale','wrong_operation','wrong_type','extra_field'])
def test_http_session_rejects_unbound_steps_before_copy(source,tmp_path,scenario):
    c=source;directory=tmp_path/'controller';target=tmp_path/'copy'
    app=controller(c,directory)
    with TestClient(app) as client:
        session=start(client,c,target)
        body={'operation_id':session['operation_id'],'expected_state_sha256':session['state_sha256'],
            'confirmed':True,'resume':False}
        action='materialize'
        if scenario=='skip':action='activate'
        elif scenario=='stale':body['expected_state_sha256']='0'*64
        elif scenario=='wrong_operation':body['operation_id']='0'*32
        elif scenario=='wrong_type':body['confirmed']='true'
        else:body['write_activation_allowed']=True
        response=client.post('/api/recovery-sessions/'+session['session_id']+'/steps/'+action,json=body)
        assert response.status_code in {409,423},response.text
        assert not target.exists()
        assert app.state.recovery_sessions.get(session['session_id'])==session


def test_lost_create_response_is_idempotent_and_missing_state_does_not_auto_execute(source,tmp_path):
    c=source;app=controller(c,tmp_path/'controller');target=tmp_path/'copy'
    with TestClient(app) as client:
        session=start(client,c,target)
        service=app.state.recovery_sessions
        intent=json.loads((service._directory(session['session_id'])/'intent.json').read_text(encoding='utf-8'))
        state=service._directory(session['session_id'])/'state.json'
        state.unlink()
        assert client.get('/api/recovery-sessions/'+session['session_id']).json()==session
        retry=client.post('/api/recovery-sessions',json={'operation_id':session['operation_id'],
            'archive_id':intent['archive_id'],'destination':str(target),'session_id':session['session_id'],
            'expected_plan_sha256':session['plan_sha256']})
        assert retry.status_code==200 and retry.json()==session
        assert not state.exists() and not target.exists()
        assert client.get('/api/recovery-sessions/restore_'+'0'*32).status_code==409
