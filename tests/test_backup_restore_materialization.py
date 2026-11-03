"""Actual archive extraction, unchanged history, and process-death boundaries."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION
from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive import capture, software_tree
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_executions import execution_archive
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed, reviewed_archive
from workbench import backup_restore_materialization as materialization
from workbench.backup_archive import _lineage, _file_hash
from workbench.backup_archive_view import verified_archive_view, ArchiveView
from workbench.backup_database import _logical_digest
from workbench.backup_files import inventory_tree
from workbench.backup_restore_plan import plan_archived_restore, _target_layout
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry
from workbench.restore_scope import RestoreScopeError


REPO = Path(__file__).resolve().parents[1]


def new_target():
    return REPO / '.tmp' / ('rm-' + uuid.uuid4().hex[:6])


def prepare(c, package, target, scratch):
    plan = plan_archived_restore(package, project_id=c['project'].id, book_id=c['project'].book_id,
        destination=target, temporary_root=scratch)
    assert plan['plan_ready'], plan['blockers']
    # Exercise the real persisted JSON round trip, including tuple/list bindings.
    return json.loads(json.dumps(plan))


def assert_original_bytes(package, plan, result):
    target = Path(plan['root_plan']['container'])
    with verified_archive_view(package) as view:
        assert _file_hash(target / 'workbench.sqlite') == view.manifest['members']['workbench.sqlite']
        with closing(sqlite3.connect((target / 'workbench.sqlite').as_uri() + '?mode=ro', uri=True)) as connection:
            assert _logical_digest(connection) == plan['archive_binding']['database_logical_sha256']
        for mapping in plan['root_plan']['roots']:
            tree = view.manifest['preview']['scope']['inventories'][mapping['root']]
            copied = inventory_tree(Path(mapping['destination']))
            assert sorted(copied['directories']) == sorted(tree['directories'])
            assert set(copied['files']) == set(tree['files']) and not copied['excluded']
            for name in tree['files']:
                assert (Path(mapping['destination']) / name).read_bytes() == view.read_root(mapping['root'], name)
    assert result['historical_bytes_preserved'] and result['state'] == 'materialized'
    assert not any(result[k] for k in ('database_rebound', 'new_physical_controls_installed',
        'read_only_app_ready', 'restored', 'recoverable', 'write_activation_allowed',
        'ownership_handoff_completed', 'start_worker', 'provider_calls_allowed'))
    assert result == json.loads((target / 'restore-materialization.json').read_bytes())
    assert result['receipt_sha256'] == digest({k:v for k,v in result.items() if k != 'receipt_sha256'})
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(target / 'workbench.sqlite')


@pytest.mark.parametrize('version', ['1.0', '2.0'])
def test_legacy_bytes_materialize_without_source_and_startup_stays_blocked(
        execution_archive, private_temporary_directory, version):
    c = execution_archive
    if version == '2.0':
        path = c['root'] / '.novelagent/project.json'
        identity = json.loads(path.read_bytes())
        identity.update(schema_version='2.0', root_hint='.', authority=LEGACY_AUTHORITY_PROJECTION)
        path.write_text(json.dumps(identity), encoding='utf-8')
    package = Path(capture(c)['path'])
    source_before, archive_before = inventory_tree(c['root']), inventory_tree(package)
    target = new_target()
    with absent_source(c['root']):
        plan = prepare(c, package, target, private_temporary_directory)
        result = materialization.materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
        assert_original_bytes(package, plan, result)
    assert inventory_tree(c['root']) == source_before and inventory_tree(package) == archive_before


def test_mixed_reviewed_history_and_full_database_survive_with_both_books_offline(
        reviewed_archive, private_temporary_directory):
    c = reviewed_archive
    other = ProjectRegistry(c['db'], allowed_workspaces=[c['root'].parent]).create_project(c['root'].parent/'other', name='other')
    package = Path(capture(c)['path'])
    before = inventory_tree(package)
    target = new_target()
    with ExitStack() as stack:
        stack.enter_context(absent_source(c['root']))
        stack.enter_context(absent_source(other.root, temporary_name='other-offline'))
        plan = prepare(c, package, target, private_temporary_directory)
        assert plan['historical_registries']
        result = materialization.materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
        assert_original_bytes(package, plan, result)
        with closing(sqlite3.connect((target/'workbench.sqlite').as_uri()+'?mode=ro', uri=True)) as connection:
            assert connection.execute('SELECT root FROM projects WHERE id=?', (other.id,)).fetchone() == (str(other.root),)
            assert connection.execute('SELECT root FROM projects WHERE id=?', (c['project'].id,)).fetchone() == (str(c['root']),)
    assert inventory_tree(package) == before


@pytest.mark.parametrize('boundary', ['partial_database', 'complete_database'])
def test_real_process_exit_leaves_intent_and_no_ordinary_startup(
        execution_archive, private_temporary_directory, boundary):
    c = execution_archive
    package = Path(capture(c)['path'])
    before = inventory_tree(package)
    target = new_target()
    plan_path = c['root'].parent / 'saved-restore-plan.json'
    with absent_source(c['root']):
        plan_path.write_text(json.dumps(prepare(c, package, target, private_temporary_directory)), encoding='utf-8')
        script = '''import json,os,sys
from pathlib import Path
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_archive_view import ArchiveView
plan=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
destination=Path(plan['root_plan']['database'])
original=ArchiveView.copy_member
def interrupted(self,name,output):
    target_copy=name == 'workbench.sqlite' and Path(output.name) == destination
    if target_copy and sys.argv[4] == 'partial_database':
        output.write(self.read_member(name)[:32]);output.flush();os.fsync(output.fileno());os._exit(73)
    result=original(self,name,output)
    if target_copy:
        output.flush();os.fsync(output.fileno());os._exit(73)
    return result
ArchiveView.copy_member=interrupted
materialize_archived_restore(sys.argv[1],plan=plan,temporary_root=sys.argv[3])
'''
        process = subprocess.run([sys.executable, '-B', '-c', script, str(package), str(plan_path),
            str(private_temporary_directory), boundary], cwd=REPO, capture_output=True, text=True, timeout=180)
        assert process.returncode == 73, process.stderr
    assert (target/'restore-intent.json').is_file() and not (target/'restore-materialization.json').exists()
    if boundary == 'partial_database':
        assert (target/'workbench.sqlite').stat().st_size == 32
    else:
        with verified_archive_view(package) as view:
            assert _file_hash(target/'workbench.sqlite') == view.manifest['members']['workbench.sqlite']
    before_restart = inventory_tree(target)
    probe = '''import sys
from workbench.database import WorkbenchDatabase
from workbench.restore_scope import RestoreScopeError
try: WorkbenchDatabase(sys.argv[1])
except RestoreScopeError: sys.exit(74)
sys.exit(75)
'''
    restarted = subprocess.run([sys.executable, '-B', '-c', probe, str(target/'workbench.sqlite')],
        cwd=REPO, capture_output=True, text=True, timeout=30)
    assert restarted.returncode == 74, restarted.stderr
    assert inventory_tree(target) == before_restart and inventory_tree(package) == before


@pytest.mark.parametrize('changed', ['parent', 'rehashed_plan'])
def test_changed_plan_binding_is_rejected_before_any_destination_write(
        execution_archive, private_temporary_directory, changed):
    c = execution_archive
    package = Path(capture(c)['path'])
    parent = new_target()
    parent.mkdir()
    target = parent / 'copy'
    plan = prepare(c, package, target, private_temporary_directory)
    if changed == 'parent':
        moved = parent.with_name(parent.name+'-old')
        assert parent.resolve().is_relative_to(REPO/'.tmp') and moved.resolve().is_relative_to(REPO/'.tmp')
        parent.rename(moved)
        parent.mkdir()
    else:
        plan['execution_policy']['start_worker'] = True
        plan['plan_sha256'] = digest({k:v for k,v in plan.items() if k != 'plan_sha256'})
    with pytest.raises(materialization.RestoreMaterializationError, match='偏离'):
        materialization.materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
    assert not target.exists()


def test_archive_completion_changed_during_copy_never_publishes_receipt(
        execution_archive, private_temporary_directory, monkeypatch):
    c = execution_archive
    package = Path(capture(c)['path'])
    target = new_target()
    plan = prepare(c, package, target, private_temporary_directory)
    original = materialization._NewContainer.member
    completion = package/'completion.json'
    original_bytes = completion.read_bytes()
    def changed(self, view, member, path):
        result = original(self, view, member, path)
        if member == 'workbench.sqlite':
            completion.write_bytes(b'{}')
        return result
    monkeypatch.setattr(materialization._NewContainer, 'member', changed)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            materialization.materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
    finally:
        completion.write_bytes(original_bytes)
    assert (target/'restore-intent.json').exists() and not (target/'restore-materialization.json').exists()
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(target/'workbench.sqlite')


@pytest.mark.parametrize('changed', ['content', 'added_file', 'added_secret', 'directory_replaced'])
def test_copied_tree_changes_cannot_pass_final_inventory(tmp_path, changed):
    target = tmp_path/'new'
    writer = materialization._NewContainer({'container':str(target), 'parent_binding':json.loads(json.dumps(_lineage(tmp_path)))})
    writer.json('restore-intent.json', {'state':'restoring'})
    writer.mkdir(target/'book')
    if changed == 'content':
        (target/'restore-intent.json').write_text('{}')
    elif changed == 'added_file':
        (target/'book/unexpected.txt').write_text('new')
    elif changed == 'added_secret':
        (target/'book/.env').write_text('synthetic fixture')
    else:
        old, moved = target/'book', target/'book-old'
        assert old.resolve().is_relative_to(tmp_path) and moved.resolve().is_relative_to(tmp_path)
        old.rename(moved)
        old.mkdir()
    with pytest.raises(materialization.RestoreMaterializationError):
        writer.verify()


def test_existing_destination_cannot_be_overwritten(tmp_path):
    target = tmp_path/'exists'
    target.mkdir()
    sentinel = target/'author.txt'
    sentinel.write_text('author work')
    with pytest.raises(materialization.RestoreMaterializationError):
        materialization._NewContainer({'container':str(target), 'parent_binding':json.loads(json.dumps(_lineage(tmp_path)))})
    assert sentinel.read_text() == 'author work'


def test_control_changed_between_publication_and_readback_is_rejected(tmp_path, monkeypatch):
    target = tmp_path/'new'
    writer = materialization._NewContainer({'container':str(target), 'parent_binding':json.loads(json.dumps(_lineage(tmp_path)))})
    original = materialization.atomic_create_json
    def replaced(path, value):
        original(path, value)
        path.write_text('{"state":"different"}')
    monkeypatch.setattr(materialization, 'atomic_create_json', replaced)
    with pytest.raises(materialization.RestoreMaterializationError, match='控制文件'):
        writer.json('restore-intent.json', {'state':'restoring'})
    assert not (target/'workbench.sqlite').exists()


def test_control_filename_path_budget_is_checked_before_extraction(execution_archive):
    c = execution_archive
    package = Path(capture(c)['path'])
    parent = REPO/'.tmp'
    # The container plus /book fits a Windows directory. The longer control
    # receipt filename must still be rejected before creating any directory.
    count = 240 - len(str(parent).encode('utf-16-le'))//2 - 1
    target = parent / ('x' * count)
    assert not target.exists()
    with verified_archive_view(package) as view, pytest.raises(RuntimeError, match='恢复控制文件'):
        _target_layout(view, target, package, registered_roots=[str(c['root'])])
    assert not target.exists()


@pytest.mark.parametrize('inside', [False, True])
def test_job_artifact_trees_are_copied_to_the_planned_runtime_without_old_roots(
        execution_archive, private_temporary_directory, inside):
    c = execution_archive
    jobs = c['app'].state.jobs
    jobs.create_job(project_id=c['project'].id, kind='generation_session', start_chapter=2, end_chapter=2)
    job = jobs.claim_next()
    assert job is not None
    jobs.pause_for_maintenance(job.id, chapter=2, operation_id='a'*32)
    runtime = c['root']/'.novelagent/runtime/jobs' if inside else c['service'].task_runtime_root
    c['service'].task_runtime_root = runtime
    artifact = runtime/job.id
    (artifact/'empty').mkdir(parents=True)
    (artifact/'evidence.txt').write_text('保留的任务资料', encoding='utf-8')
    package = Path(capture(c)['path'])
    target = new_target()
    before = inventory_tree(package)
    with ExitStack() as stack:
        stack.enter_context(absent_source(c['root']))
        if not inside:
            stack.enter_context(absent_source(runtime, temporary_name='jobs-offline'))
        plan = prepare(c, package, target, private_temporary_directory)
        result = materialization.materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
        assert_original_bytes(package, plan, result)
        restored = Path(plan['root_plan']['task_runtime_root'])/job.id
        assert (restored/'empty').is_dir()
        assert (restored/'evidence.txt').read_text(encoding='utf-8') == '保留的任务资料'
        assert restored.is_relative_to(target/'book' if inside else target/'jobs')
    assert inventory_tree(package) == before
