"""Actual target launch and admission failures over a completed recovery."""
import json
from pathlib import Path
import subprocess
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_restore_database import logical
from tests.test_recovery_sessions import controller, start, advance
from tests.test_restore_budget_preparation import make_financial_source
from workbench.backup_files import inventory_tree


CHILD = r'''import json,socket,sys,threading
from pathlib import Path
from types import SimpleNamespace
# The support script is inside a private fixture directory, not the repository.
ROOT=Path(sys.argv[1]);machine=Path(sys.argv[2]);stop=Path(sys.argv[3])
sys.path.insert(0,str(ROOT));sys.argv=['run_workbench.py',*sys.argv[4:]]
calls=[]
def forbidden(*args,**kwargs):
    calls.append('unexpected automatic operation');raise AssertionError('No Provider authorized')
sys.modules['openai']=SimpleNamespace(OpenAI=forbidden)
sys.modules['anthropic']=SimpleNamespace(Anthropic=forbidden)
original=socket.socket.connect
def connect(self,address):
    if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:
        return original(self,address)
    raise AssertionError('Only loopback allowed')
socket.socket.connect=connect
from workbench import book_ownership
book_ownership.default_machine_store=lambda:machine
import run_workbench
from workbench.jobs import JobSupervisor
JobSupervisor._start_worker=forbidden
import workbench.app as app_module
app_module._import_active_project=forbidden
Original=run_workbench.uvicorn.Server
owned=[]
class Server(Original):
    def __init__(self,config):
        super().__init__(config);owned.append(config.app)
        def watch():
            while not self.should_exit:
                if stop.exists():self.should_exit=True;return
                threading.Event().wait(.1)
        threading.Thread(target=watch,daemon=True).start()
run_workbench.uvicorn.Server=Server
result=run_workbench.main()
assert result==0 and len(owned)==1 and owned[0].state.restore_scope.active
assert owned[0].state.supervisor._thread is None and not calls
stop.with_suffix('.after.json').write_text(json.dumps({'exit_code':result,'worker_absent':True,'calls':calls}),encoding='utf-8')
raise SystemExit(result)
'''


def guarded_child_factory(c, stop, processes):
    """Use the real CLI with a private machine store and forbidden Providers."""
    script = c['root'].parent/'target-launch-child.py'
    script.write_text(CHILD, encoding='utf-8')
    repository = Path(__file__).resolve().parents[1]
    def spawn(command, **options):
        actual = [*command[:2], str(script), str(repository), str(c['machine']), str(stop), *command[3:]]
        process = subprocess.Popen(actual, **options)
        processes.append(process)
        return process
    return spawn


@pytest.fixture(scope='module')
def opened():
    c = make_financial_source(include_uncertain=False)
    before_book, before_sql = inventory_tree(c['root']), logical(c['db'].path)
    directory = c['root'].parent/'打开 控制台'
    target = c['root'].parent/'恢复 作品'
    stop = c['root'].parent/'opened-target-stop'
    processes = []
    app = controller(c, directory)
    app.state.recovery_target_launcher.process_factory = guarded_child_factory(c, stop, processes)
    client = TestClient(app)
    client.__enter__()
    context = dict(c=c, app=app, client=client, session=None, directory=directory,
        target=target, processes=processes, stop=stop)
    try:
        session = advance(client, start(client, c, target), 'active')
        assert client.post('/api/maintenance/finish', json={'operation_id':session['operation_id']}).status_code == 200
        context['session'] = session
        try:
            yield context
        finally:
            stop.touch()
            for process in processes:
                try:
                    result = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.terminate();process.wait(timeout=10)
                    pytest.fail('original target did not stop gracefully')
                assert result == 0
            if processes:
                assert json.loads(stop.with_suffix('.after.json').read_text(encoding='utf-8')) == {'exit_code':0,'worker_absent':True,'calls':[]}
    finally:
        context['client'].__exit__(None,None,None)
    assert inventory_tree(c['root']) == before_book and logical(c['db'].path) == before_sql


def body(c):
    return {'operation_id':c['session']['operation_id'], 'expected_state_sha256':c['session']['state_sha256']}


def endpoint(c):
    return '/api/recovery-sessions/'+c['session']['session_id']+'/open'


def test_actual_open_reuses_target_and_preserves_source_and_target_data(opened):
    c = opened
    response = c['client'].post(endpoint(c), json=body(c))
    assert response.status_code == 200,response.text
    value = response.json()
    address = urlsplit(value['url'])
    assert address.scheme == 'http' and address.hostname == '127.0.0.1' and address.port
    assert address.fragment == '/projects/'+c['session']['project_id']+'/overview'
    assert value['automatic_provider_calls_allowed'] is False
    assert len(c['processes']) == 1 and c['processes'][0].poll() is None
    scope = c['app'].state.recovery_sessions.get(c['session']['session_id'])
    assert scope == c['session']
    from workbench.restore_scope import load_restore_scope
    binding = load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['c']['machine'])
    before_book, before_sql = inventory_tree(binding.book_root), logical(c['target']/'workbench.sqlite')
    with httpx.Client(base_url=value['url'].split('#')[0],trust_env=False,timeout=10) as target_client:
        assert target_client.get('/api/bootstrap').json()['restore_scope']['state'] == 'active'
        assert [item['id'] for item in target_client.get('/api/projects').json()['items']] == [c['session']['project_id']]
        draft = target_client.get('/api/drafts/'+c['c']['draft'].id)
        assert draft.status_code == 200 and draft.json()['draft']['text'] == c['c']['draft'].text
        chapter = target_client.get('/api/projects/'+c['session']['project_id']+'/chapters/3')
        assert chapter.status_code == 200,chapter.text
        retained = chapter.json()['retained_author_draft']
        assert retained['id'] == c['c']['draft'].id and retained['text'] == c['c']['draft'].text
        assert retained['sha256'] == c['c']['draft'].sha256 and retained['revision'] == c['c']['draft'].revision
        assert chapter.json()['current_prose'] is None and not chapter.json()['has_prose']
        formal = target_client.get('/api/projects/'+c['session']['project_id']+'/chapters/2')
        assert formal.status_code == 200 and 'retained_author_draft' not in formal.json()
        assert target_client.get('/api/projects/'+c['c']['other'].id).status_code == 409
    repeat = c['client'].post(endpoint(c),json=body(c))
    assert repeat.status_code == 200 and repeat.json() == value
    assert len(c['processes']) == 1
    assert inventory_tree(binding.book_root) == before_book and logical(c['target']/'workbench.sqlite') == before_sql
    assert not (c['target']/'workbench.db').exists()


def _live_target_url(c):
    response = c['client'].post(endpoint(c), json=body(c))
    assert response.status_code == 200, response.text
    return response.json()['url'].split('#')[0]


def test_short_cross_process_qualification_read_waits_for_current_record(opened):
    from workbench.book_ownership import _machine_gate
    c = opened
    url = _live_target_url(c)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with _machine_gate(c['session']['book_id'], c['c']['machine']):
            future = pool.submit(httpx.get, url+'api/maintenance', trust_env=False, timeout=10)
            time.sleep(.5)
            assert not future.done(), 'qualification read must wait for the other process gate'
        response = future.result(timeout=10)
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'idle'
    assert len(c['processes']) == 1


def test_qualification_read_timeout_reports_busy_without_releasing_owner(opened):
    from workbench.book_ownership import _machine_gate
    c = opened
    url = _live_target_url(c)
    instance = httpx.get(url+'api/instance', trust_env=False, timeout=10).json()
    with _machine_gate(c['session']['book_id'], c['c']['machine']):
        started = time.monotonic()
        response = httpx.get(url+'api/maintenance', trust_env=False, timeout=10)
        assert 1.5 <= time.monotonic()-started < 6
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'restore_scope_busy'
        assert c['processes'][0].poll() is None
    assert httpx.get(url+'api/maintenance', trust_env=False, timeout=10).json()['state'] == 'idle'
    assert httpx.get(url+'api/instance', trust_env=False, timeout=10).json() == instance
    assert len(c['processes']) == 1


def test_waiting_qualification_read_rejects_changed_receipt_after_lock_release(opened):
    from workbench.book_ownership import _machine_gate
    from workbench.restore_qualification import ACTIVATION_FILE
    c = opened
    url = _live_target_url(c)
    receipt = c['target']/ACTIVATION_FILE
    original = receipt.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with _machine_gate(c['session']['book_id'], c['c']['machine']):
                future = pool.submit(httpx.get, url+'api/maintenance', trust_env=False, timeout=10)
                time.sleep(.5)
                assert not future.done()
                receipt.write_bytes(b'{}')
            response = future.result(timeout=10)
            assert response.status_code == 409, response.text
            assert response.json()['error']['code'] == 'restore_scope_invalid'
        finally:
            receipt.write_bytes(original)
    assert httpx.get(url+'api/maintenance', trust_env=False, timeout=10).json()['state'] == 'idle'


@pytest.mark.parametrize('scenario',['wrong_operation','stale','destination','url','grant'])
def test_open_rejects_unbound_input_without_spawning(opened, scenario):
    c=opened;value=body(c);before=len(c['processes'])
    if scenario=='wrong_operation':value['operation_id']='0'*32
    elif scenario=='stale':value['expected_state_sha256']='0'*64
    elif scenario=='destination':value['destination']=str(c['c']['root'])
    elif scenario=='url':value['url']='https://example.invalid/'
    else:value['write_activation_allowed']=True
    response=c['client'].post(endpoint(c),json=value)
    assert response.status_code==409,response.text
    assert len(c['processes'])==before
    assert c['app'].state.recovery_sessions.get(c['session']['session_id'])==c['session']


def test_open_timeout_observes_original_child_without_respawning(opened, monkeypatch):
    c=opened
    assert c['client'].post(endpoint(c),json=body(c)).status_code==200
    original=c['processes'][0];before=len(c['processes'])
    monkeypatch.setattr('workbench.recovery_target.existing_instance_url',lambda *args,**kwargs:None)
    monkeypatch.setattr(c['app'].state.recovery_target_launcher,'startup_timeout',.01)
    assert c['client'].post(endpoint(c),json=body(c)).status_code==409
    assert c['client'].post(endpoint(c),json=body(c)).status_code==409
    assert len(c['processes'])==before and original.poll() is None


def test_open_requires_live_qualification_before_reusing_server(opened):
    c=opened;path=c['target']/'write-qualification.json';original=path.read_bytes()
    before=len(c['processes'])
    try:
        path.write_text('{}',encoding='utf-8')
        response=c['client'].post(endpoint(c),json=body(c))
        assert response.status_code==409,response.text
        assert len(c['processes'])==before
    finally:path.write_bytes(original)


def test_planned_session_cannot_open_or_spawn(opened):
    c=opened;client=c['client'];before=len(c['processes'])
    session=start(client,c['c'],c['c']['root'].parent/'尚未 恢复')
    response=client.post('/api/recovery-sessions/'+session['session_id']+'/open',json={
        'operation_id':session['operation_id'],'expected_state_sha256':session['state_sha256']})
    assert response.status_code==409,response.text
    assert len(c['processes'])==before
    cancelled=client.post('/api/recovery-sessions/'+session['session_id']+'/steps/cancel',json={
        'operation_id':session['operation_id'],'expected_state_sha256':session['state_sha256'],
        'confirmed':False,'resume':False})
    assert cancelled.status_code==200,cancelled.text
    assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200


def test_controller_restart_reuses_live_target_without_launching_again(opened):
    c=opened
    first=c['client'].post(endpoint(c),json=body(c))
    assert first.status_code==200
    c['client'].__exit__(None,None,None)
    app=controller(c['c'],c['directory'])
    def forbidden(*args,**kwargs):
        pytest.fail('controller restart launched a duplicate target process')
    app.state.recovery_target_launcher.process_factory=forbidden
    c['app']=app;c['client']=TestClient(app);c['client'].__enter__()
    c['client'].get('/')
    assert c['client'].get('/api/recovery-sessions/'+c['session']['session_id']).json()==c['session']
    resumed=c['client'].post(endpoint(c),json=body(c))
    assert resumed.status_code==200 and resumed.json()==first.json()
    assert len(c['processes'])==1 and c['processes'][0].poll() is None


def test_retained_author_draft_hash_damage_is_rejected_without_changing_formal_prose(opened):
    c=opened
    value=c['client'].post(endpoint(c),json=body(c)).json()
    database=c['target']/'workbench.sqlite'
    draft=c['c']['draft']
    before=logical(database)
    with httpx.Client(base_url=value['url'].split('#')[0],trust_env=False,timeout=10) as client:
        formal_path='/api/projects/'+c['session']['project_id']+'/chapters/2'
        formal=client.get(formal_path).json()
        try:
            with sqlite3.connect(database) as connection:
                connection.execute('UPDATE drafts SET sha256=? WHERE id=?',('0'*64,draft.id))
            failed=client.get('/api/projects/'+c['session']['project_id']+'/chapters/3')
            assert failed.status_code in {400,409},failed.text
            assert '作者草稿' in failed.json()['error']['message']
            assert client.get(formal_path).json()==formal
        finally:
            with sqlite3.connect(database) as connection:
                connection.execute('UPDATE drafts SET sha256=? WHERE id=?',(draft.sha256,draft.id))
    assert logical(database)==before
