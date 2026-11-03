from concurrent.futures import ThreadPoolExecutor
import json
import threading
import pytest

from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from tests.test_chapter_publication_api import api_case
from tests.test_chapter_publication_service import case, ready, service_case
from tests.test_reviewed_candidate_context import _fixed_provenance
from tests.test_reviewed_workbench_workflow import new_candidate
from tests.test_workbench_chapter_fact_extractions import FactExtractionExecutorFactory


@pytest.mark.parametrize('finish_during_probe', [False, True])
def test_running_candidate_is_not_misreported_as_recovery(api_case, monkeypatch, finish_during_probe):
    c = api_case
    started, release = threading.Event(), threading.Event()
    calls = []
    def generate(_):
        calls.append(1)
        started.set()
        assert release.wait(timeout=30)
        return c['text']
    c['app'].state.chapter_candidates.reviewed.executor_factory = lambda root: ReviewedChapterCandidateExecutor(
        story_project_root=root, generator=generate, execution_kind='deterministic', provenance_factory=_fixed_provenance)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(c['client'].post, c['path']+'/candidates',
            json={'confirmation':c['confirmation']}, headers={'Idempotency-Key':'live-candidate'})
        try:
            assert started.wait(timeout=20)
            if finish_during_probe:
                import workbench.chapter_candidates as module
                original_probe = module.execution_is_active
                def finish_then_probe(root, execution_id):
                    release.set()
                    future.result(timeout=30)
                    return original_probe(root, execution_id)
                monkeypatch.setattr(module, 'execution_is_active', finish_then_probe)
            response = c['client'].get(c['path']+'/candidates')
            assert response.status_code == 200, response.text
            items = response.json()['items']
            expected = 'completed' if finish_during_probe else 'running'
            finished_id = future.result(timeout=30).json()['candidate']['candidate_id'] if finish_during_probe else None
            live = [item for item in items if item['status']==expected and (finished_id is None or item['candidate_id']==finished_id)]
            assert len(live) == 1, items
            detail = c['client'].get(c['path']+'/candidates/'+live[0]['candidate_id'])
            assert detail.status_code == 200 and detail.json()['candidate']['status'] == expected
        finally:
            release.set()
        result = future.result(timeout=30)
    assert result.status_code == 200, result.text
    assert result.json()['candidate']['status'] == 'completed' and calls == [1]


@pytest.mark.parametrize('finish_during_probe', [False, True])
def test_running_fact_extraction_is_not_misreported_as_uncertain(api_case, monkeypatch, finish_during_probe):
    c = api_case
    draft = new_candidate(c)['draft']
    started, release = threading.Event(), threading.Event()
    raw = json.dumps({'schema_version':'1.0','facts':[f for f in c['example']['proposal']['facts'] if f['kind'] in {'event','counter'}]})
    base_factory = FactExtractionExecutorFactory(raw)
    def wait_during_provider():
        started.set()
        assert release.wait(timeout=30)
    base_factory.before_return = wait_during_provider
    c['app'].state.chapter_fact_extractions.reviewed.executor_factory = base_factory
    path = f"/api/candidate-drafts/{draft['id']}/fact-extractions"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(c['client'].post, path,
            json={'expected_draft_revision':draft['revision'],'expected_draft_sha256':draft['sha256'],'author_confirmed':True},
            headers={'Idempotency-Key':'live-facts'})
        try:
            assert started.wait(timeout=20)
            if finish_during_probe:
                import workbench.chapter_fact_extractions as module
                original_probe = module.execution_is_active
                def finish_then_probe(root, execution_id):
                    release.set()
                    future.result(timeout=30)
                    return original_probe(root, execution_id)
                monkeypatch.setattr(module, 'execution_is_active', finish_then_probe)
            response = c['client'].get(path)
            assert response.status_code == 200, response.text
            expected = 'completed' if finish_during_probe else 'running'
            live = [item for item in response.json()['items'] if item['status']==expected]
            assert len(live) == 1, response.text
        finally:
            release.set()
        result = future.result(timeout=30)
    assert result.status_code == 200, result.text
    assert result.json()['fact_extraction']['status'] == 'completed'
