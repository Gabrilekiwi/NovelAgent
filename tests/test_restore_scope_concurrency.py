"""Actual restored qualification and budget reads serialize within a process."""
import concurrent.futures
from pathlib import Path
import threading

from fastapi.testclient import TestClient

from tests.test_backup_restore_database import logical
from tests.test_restore_job_artifacts_internal import second_case,internal_case,forbidden
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.restore_scope import load_restore_scope


def evidence(c):
    directory=BookOwnership(c['project'].book_id,machine_store=c['store']).directory
    return (inventory_tree(c['target']/'book'),logical(c['target']/'workbench.sqlite'),
        {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file() and p.suffix=='.json'})


def test_four_concurrent_actual_activation_reads_keep_qualification_unchanged(second_case):
    c=second_case;before=evidence(c);barrier=threading.Barrier(4)
    def read(_):
        barrier.wait()
        scope=load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['store'])
        assert scope.active and scope.document['scope_generation']==2
        return scope.activation
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(read,range(4)))
    assert all(item==results[0] for item in results) and evidence(c)==before


def test_four_concurrent_http_budget_reads_return_success_without_calls(second_case):
    c=second_case;before=evidence(c);barrier=threading.Barrier(4)
    app=create_app(database_path=c['target']/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    with TestClient(app) as client:
        assert client.get('/').status_code==200 and app.state.supervisor._thread is None
        def read(_):
            barrier.wait()
            result=client.get('/api/projects/'+c['project'].id+'/chapters/3/budget')
            assert result.status_code==200,result.text
            assert result.json()['book_id']==c['project'].book_id
            return result.json()['state']
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(read,range(4)))
        assert results==[results[0]]*4
    assert evidence(c)==before
