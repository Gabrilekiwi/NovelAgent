"""Exercise standalone review commands against the actual snapshot lock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
import json

import pytest

from core.engine.persistence import persistence_run_lock, persistence_state_lock, PersistenceLockError
from core.story_project.identity import ensure_project_identity
from core.review.dashboard import build_review_dashboard_from_index
from tests.test_backup_external_writers import child
from tests.test_backup_scope import legacy
from workbench.backup_files import inventory_tree
from workbench.backup_source import locked_backup_source
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


REVIEW_COMMAND = '''import sys
from pathlib import Path
from core.engine.persistence import PersistenceLockError
from core.review.dashboard import build_review_dashboard_from_index
from core.review.index import update_review_index,build_review_index_entry
root=Path(sys.argv[1]);command=sys.argv[2]
review=root/'.novelagent/runtime/reviews'
try:
 if command=='main':
  import main
  sys.argv=['main.py','--story-project',str(root),'--review-dashboard','--output-json']
  main.main()
 elif command=='script':
  from scripts.build_review_dashboard import main
  main(['--review-output-dir',str(review),'--json'])
 elif command=='index':
  update_review_index(review_output_dir=review,
   entry=build_review_index_entry(run_id='chapter_1_test',review_pipeline={'status':'warning'}))
 else:
  output=root/'nested/new-dashboard.html' if command=='custom' else None
  build_review_dashboard_from_index(review_output_dir=review,output_path=output)
except PersistenceLockError:
 print('BACKUP_LOCK_BLOCKED',flush=True);sys.exit(17)
'''


@pytest.mark.parametrize('command', ['default', 'custom', 'main', 'script', 'index', 'overwrite'])
def test_review_writers_refuse_before_changing_source_and_succeed_after_maintenance(legacy, command):
    c = legacy
    root = c['root']; base = root.parent.parent
    review = root / '.novelagent/runtime/reviews'
    if command == 'overwrite':
        review.mkdir(parents=True)
        (review / 'dashboard.html').write_text('previous dashboard')
    with closing(InstanceLease(base).acquire()) as lease:
        gate = maintenance_for(lease); operation_id = gate.begin()['operation_id']
        with locked_backup_source(c['projects'], c['project'].id, maintenance=gate,
                operation_id=operation_id, task_runtime_root=c['runtime'], machine_store=base/'machine') as (_,scope,readers,_):
            before = scope['inventories']['story_project']
            result = child(REVIEW_COMMAND, root, command)
            assert result.returncode == 17 and b'BACKUP_LOCK_BLOCKED' in result.stdout, result.stderr
            assert inventory_tree(root, locked_readers=readers) == before
        gate.finish(operation_id)
        result = child(REVIEW_COMMAND, root, command)
        assert result.returncode == 0, result.stderr
    if command == 'index':
        assert json.loads((review/'review_index.json').read_bytes())['summary']['entry_count'] == 1
    else:
        output = root/'nested/new-dashboard.html' if command == 'custom' else review/'dashboard.html'
        assert 'NovelAgent Review Dashboard' in output.read_text(encoding='utf-8')


COMPETE = '''import sys
from core.engine.persistence import persistence_run_lock,PersistenceLockError
try:
 with persistence_run_lock(sys.argv[1],state_paths=[sys.argv[2]]):pass
except PersistenceLockError:sys.exit(17)
'''


def test_review_writer_borrows_generation_lock_without_releasing_outer_owner(tmp_path):
    root = tmp_path/'book'
    (root/'.novelagent').mkdir(parents=True)
    ensure_project_identity(root)
    review = root/'.novelagent/runtime/reviews'
    with persistence_run_lock(tmp_path/'runs', state_paths=[root]):
        result = build_review_dashboard_from_index(review_output_dir=review)
        assert (review/'dashboard.html').read_text(encoding='utf-8') == result['html']
        assert child(COMPETE,tmp_path/'other-runs',root).returncode == 17
    assert child(COMPETE,tmp_path/'other-runs',root).returncode == 0


def test_copied_context_in_another_thread_cannot_borrow_active_state_lock(tmp_path):
    target = tmp_path/'state'
    def compete():
        with persistence_state_lock([target]):
            pytest.fail('another thread borrowed the owner lock')
    with persistence_run_lock(tmp_path/'runs',state_paths=[target]):
        copied = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(PersistenceLockError):
                pool.submit(copied.run, compete).result(timeout=10)


def test_stale_context_reacquires_state_lock_after_owner_exits(tmp_path):
    target = tmp_path/'state'
    with persistence_run_lock(tmp_path/'runs',state_paths=[target]):
        copied = copy_context()
    def reacquire():
        with persistence_state_lock([target]):
            assert child(COMPETE,tmp_path/'other-runs',target).returncode == 17
    copied.run(reacquire)
    assert child(COMPETE,tmp_path/'other-runs',target).returncode == 0
