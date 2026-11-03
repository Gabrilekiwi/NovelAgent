import json
from pathlib import Path
import subprocess
import sys

from core.engine.execution_activity import execution_activity, execution_is_active


def test_probe_does_not_create_state_and_stale_file_is_not_liveness(tmp_path):
    root = tmp_path / 'runs'
    assert not execution_is_active(root, 'execution_candidate_test')
    assert not root.exists()
    with execution_activity(root, 'execution_candidate_test'):
        assert execution_is_active(root, 'execution_candidate_test')
        assert not execution_is_active(root, 'execution_candidate_other')
    assert not execution_is_active(root, 'execution_candidate_test')
    assert list(root.rglob('.persistence.lock'))


def test_process_exit_releases_exact_execution_lease(tmp_path):
    root = tmp_path / 'runs'
    script = '''import sys
from core.engine.execution_activity import execution_activity
with execution_activity(sys.argv[1], 'execution_fx_test'):
    print('held', flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', script, str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'held'
        assert execution_is_active(root, 'execution_fx_test')
        assert not execution_is_active(root, 'execution_fx_other')
        child.kill()
        child.wait(timeout=10)
        assert not execution_is_active(root, 'execution_fx_test')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close(); child.stdout.close(); child.stderr.close()
