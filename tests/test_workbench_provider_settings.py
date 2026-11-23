from core.config import get_config
from core.writing_profile import PROFILE_ENV, profile_path, use_profile
from tests.test_new_book_journey import open_desk


def test_credential_destination_persistence_and_workbench_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'user'))
    monkeypatch.setenv('NOVELAGENT_SKIP_DOTENV','1')
    monkeypatch.setenv('OPENAI_API_KEY','legacy-fixture')
    monkeypatch.setenv('OPENAI_BASE_URL','https://legacy.invalid/v1')
    path = profile_path(tmp_path/'desk/data')
    public = '/api/settings/writing-model'
    form = dict(model='fixture-model', base_url='https://relay.invalid/v1', api_key='')
    with open_desk(tmp_path/'desk') as client:
        state = client.get(public).json()
        assert state['source']=='environment' and 'legacy-fixture' not in str(state)
        assert client.put(public,json=form).status_code==422
        for url in ('https://user:secret@host.invalid/v1','http://relay.invalid/v1','https://relay.invalid/v1?key=bad'):
            assert client.put(public,json={**form,'base_url':url,'api_key':'test-only'}).status_code==422
        result = client.put(public,json={**form,'api_key':'private-fixture-value'})
        assert result.is_success, result.text
        assert result.json()==dict(model='fixture-model',base_url=form['base_url'],has_key=True,source='workbench')
        assert client.get('/api/bootstrap').json()['provider']['model']=='fixture-model'
        assert client.put(public,json={**form,'model':'new-model'}).is_success
        assert client.put(public,json={**form,'base_url':''}).status_code==422
    with open_desk(tmp_path/'desk') as reopened:
        assert reopened.get(public).json()['model']=='new-model'
        assert 'private-fixture-value' not in reopened.get(public).text
    with open_desk(tmp_path/'other') as separate:
        assert separate.get(public).json()['source']=='environment'
    monkeypatch.setenv(PROFILE_ENV, str(path))
    cfg = get_config()
    assert (cfg.openai_api_key,cfg.openai_base_url,cfg.openai_model)==('private-fixture-value',form['base_url'],'new-model')
    import os
    if os.name=='nt':
        assert b'private-fixture-value' not in path.read_bytes()
    else:
        assert path.stat().st_mode & 0o077 == 0


def test_active_request_keeps_old_model_and_endpoint(tmp_path, monkeypatch):
    from core.writing_profile import save_profile
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'user'))
    path=profile_path(tmp_path/'desk')
    first=dict(api_key='first-test',base_url='https://first.invalid/v1',model='one')
    second=dict(api_key='second-test',base_url='https://second.invalid/v1',model='two')
    save_profile(path,first)
    with use_profile(path):
        assert get_config().openai_model=='one'
        save_profile(path,second)
        cfg=get_config(); assert (cfg.openai_api_key,cfg.openai_base_url,cfg.openai_model)==tuple(first.values())
    with use_profile(path):
        assert get_config().openai_model=='two'
