from pathlib import Path

from api.contracts import reported_model_usage
from tests.test_workbench_app import _client, _unlock, _create_project
from tests.test_workbench_model_usage import put
from tests.test_chapter_publication_api import api_case
from tests.test_chapter_publication_service import case,ready,service_case


def test_legacy_project_report_is_read_only_and_keeps_failed_attempts(tmp_path):
    client,workspace=_client(tmp_path)
    with client:
        _unlock(client)
        project=_create_project(client,workspace)
        root=Path(project['root'])
        put(root,'execution_legacy',usage=reported_model_usage({'input_tokens':0,'output_tokens':5}))
        path=f"/api/projects/{project['id']}/model-calls"
        first=client.get(path)
        assert first.status_code==200,first.text
        report=first.json()
        assert report['items'][0]['usage']['input_tokens']['value']==0
        assert report['summary']['unknown_cost_count']==1
        assert client.get(path).json()['items']==report['items']
        assert client.get('/api/projects/project_missing/model-calls').status_code==400


def test_reviewed_report_remains_readable_and_corruption_does_not_leak_partial_total(api_case):
    c=api_case
    store,intent,receipt=put(c['root'],'execution_observed',usage={'input_tokens':12})
    path=f"/api/projects/{c['project'].id}/model-calls"
    first=c['client'].get(path)
    assert first.status_code==200,first.text
    assert first.json()['summary']['attempt_count']==1
    assert first.json()['items'][0]['usage']['input_tokens']['source']=='legacy_receipt'
    store.receipt_path(receipt['attempt_id']).write_text('{}',encoding='utf-8')
    failure=c['client'].get(path)
    assert failure.status_code==409 and failure.json()['error']['code']=='model_usage_evidence_invalid'
    assert 'summary' not in failure.json() and str(c['root']) not in failure.text
