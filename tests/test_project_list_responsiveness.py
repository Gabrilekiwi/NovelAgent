import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_workbench_app import _client, _unlock


def test_slow_project_scan_does_not_block_bootstrap(tmp_path):
    client, _ = _client(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    def slow_scan(self):
        entered.set()
        assert release.wait(10)
        return ()
    with client:
        _unlock(client)
        with patch('workbench.app.ProjectDisplayRegistry.list_projects', slow_scan):
            with ThreadPoolExecutor(max_workers=2) as pool:
                listing = pool.submit(client.get, '/api/projects')
                try:
                    assert entered.wait(5)
                    bootstrap = pool.submit(client.get, '/api/bootstrap')
                    assert bootstrap.result(timeout=3).status_code == 200
                finally:
                    release.set()
                assert listing.result(timeout=5).json() == {'items': []}
