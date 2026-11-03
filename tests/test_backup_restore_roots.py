"""Live cold reads after proof-bound relocation, with immutable history intact."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from core.engine.root_registry import RootRegistryService, validate_root_registry
from core.engine.safe_paths import PathGuardMismatchError
from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION, load_project_identity
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive import capture, software_tree
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_executions import execution_archive
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed, reviewed_archive
from tests.test_backup_restore_materialization import prepare, new_target
from workbench import backup_restore_roots as binding
from workbench.backup_archive import _file_hash
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_files import inventory_tree
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.database import WorkbenchDatabase
from workbench.restore_scope import RestoreScopeError


REPO = Path(__file__).resolve().parents[1]


def _files(root):
    return {name:value['sha256'] for name,value in inventory_tree(root)['files'].items()}


@pytest.fixture
def materialized(reviewed_archive, private_temporary_directory):
    c = reviewed_archive
    package, target = Path(capture(c)['path']), new_target()
    plan = prepare(c, package, target, private_temporary_directory)
    materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
    return {**c, 'package':package, 'target':target, 'plan':plan, 'scratch':private_temporary_directory}


def run(c):
    return binding.bind_restored_roots(c['package'], destination=c['target'], temporary_root=c['scratch'])


def test_mixed_history_becomes_live_readable_without_weakening_old_guards(materialized):
    c = materialized
    book = c['target']/'book'
    expected = load_verified_reviewed_identity(c['root'])
    before, source_before = _files(book), inventory_tree(c['root'])
    db_before = _file_hash(c['target']/'workbench.sqlite')
    with absent_source(c['root']):
        with pytest.raises((ValueError, RuntimeError)):
            load_verified_reviewed_identity(book)
        result = run(c)
        assert load_verified_reviewed_identity(book) == expected
    intent = json.loads((c['target']/'root-binding.json').read_bytes())
    paths = {item['path'] for item in intent['registries']}
    after = _files(book)
    assert {name for name in before if before[name] != after.get(name)} == paths
    assert set(before) == set(after)
    assert result['registry_count'] == len(paths) > 0
    assert result['verified']['transaction_count'] == 3
    assert not any(result[key] for key in ('database_rebound','read_only_app_ready','ownership_handoff_completed',
        'restored','write_activation_allowed','recoverable','old_guards_replay_allowed'))
    checked = False
    for item in intent['registries']:
        original_bytes = base64.b64decode(item['original_base64'])
        assert digest(original_bytes) == before[item['path']]
        original = validate_root_registry(json.loads(original_bytes))
        updated = validate_root_registry(json.loads((book/item['path']).read_bytes()))
        assert updated['registry_id'] == original['registry_id']
        assert updated['revision'] == original['revision']+1
        assert {k:v['root_uuid'] for k,v in updated['roots'].items()} == {k:v['root_uuid'] for k,v in original['roots'].items()}
        control = (book/item['path']).parent
        resolver = RootRegistryService(control).resolver(updated)
        for path in (control/'journals').glob('*/manifest.json'):
            manifest = json.loads(path.read_bytes())
            for target in manifest['immutable']['targets']:
                if target.get('path_guard'):
                    with pytest.raises(PathGuardMismatchError):
                        resolver.resolve(target['path_ref'], expected_guard=target['path_guard'])
                    checked = True
    assert checked
    assert _file_hash(c['target']/'workbench.sqlite') == db_before
    assert inventory_tree(c['root']) == source_before
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(c['target']/'workbench.sqlite')


def test_fresh_process_rechecks_same_root_binding_without_rewriting_book(materialized):
    c = materialized
    with absent_source(c['root']):
        first = run(c)
        before = inventory_tree(c['target']/'book')
        output = c['target'].parent/(c['target'].name+'-probe.json')
        script = '''import json,sys
from pathlib import Path
from workbench.backup_restore_roots import bind_restored_roots
result=bind_restored_roots(sys.argv[1],destination=sys.argv[2],temporary_root=sys.argv[3])
Path(sys.argv[4]).write_text(json.dumps(result),encoding='utf-8')
'''
        process = subprocess.run([sys.executable,'-B','-c',script,str(c['package']),str(c['target']),str(c['scratch']),str(output)],
            cwd=REPO, capture_output=True, text=True, timeout=180)
        assert process.returncode == 0, process.stderr
        assert json.loads(output.read_text(encoding='utf-8')) == first
        assert inventory_tree(c['target']/'book') == before


def test_real_exit_after_registry_write_can_resume_without_replaying_old_plans(materialized):
    c = materialized
    before = _files(c['target']/'book')
    script = '''import os,sys
from workbench import backup_restore_roots as m
original=m.atomic_write_json
def interrupted(path,value):
    original(path,value)
    if path.name=='root_registry.json':os._exit(76)
m.atomic_write_json=interrupted
m.bind_restored_roots(sys.argv[1],destination=sys.argv[2],temporary_root=sys.argv[3])
'''
    with absent_source(c['root']):
        process = subprocess.run([sys.executable,'-B','-c',script,str(c['package']),str(c['target']),str(c['scratch'])],
            cwd=REPO,capture_output=True,text=True,timeout=180)
        assert process.returncode == 76, process.stderr
        assert (c['target']/'root-binding.json').is_file()
        assert not (c['target']/'root-binding-done.json').exists()
        with pytest.raises(RestoreScopeError):
            WorkbenchDatabase(c['target']/'workbench.sqlite')
        completed = run(c)
        assert completed['new_v2_physical_registries_bound'] and not completed['old_guards_replay_allowed']
    after = _files(c['target']/'book')
    changed = {item['path'] for item in json.loads((c['target']/'root-binding.json').read_bytes())['registries']}
    assert set(before) == set(after)
    assert all(after[name] == value for name,value in before.items() if name not in changed)


@pytest.mark.parametrize('damage',['book','database','materialization_receipt','registry'])
def test_modified_materialized_copy_is_rejected_before_binding(materialized, damage):
    c = materialized
    if damage == 'book':
        path = c['target']/'book/.novelagent/project.json'
    elif damage == 'database':
        path = c['target']/'workbench.sqlite'
    elif damage == 'materialization_receipt':
        path = c['target']/'restore-materialization.json'
    else:
        path = c['target']/'book'/c['plan']['historical_registries'][0]['path']
    path.write_bytes(b'{}')
    before = _files(c['target']/'book')
    with pytest.raises((ValueError, RuntimeError)):
        run(c)
    assert not (c['target']/'root-binding.json').exists()
    assert _files(c['target']/'book') == before


def test_same_bytes_in_replaced_book_directory_cannot_resume_binding(materialized, monkeypatch):
    c = materialized
    original = binding.atomic_write_json
    def interrupted(path, value):
        original(path, value)
        raise RuntimeError('synthetic interruption')
    with monkeypatch.context() as patch:
        patch.setattr(binding, 'atomic_write_json', interrupted)
        with pytest.raises(RuntimeError, match='synthetic interruption'):
            run(c)
    book = c['target']/'book'
    moved = c['target'].parent/(c['target'].name+'-old-book')
    assert book.resolve().is_relative_to(REPO/'.tmp') and moved.resolve().is_relative_to(REPO/'.tmp')
    before = _files(book)
    book.rename(moved)
    shutil.copytree(moved, book)
    assert _files(book) == before
    intent_before = (c['target']/'root-binding.json').read_bytes()
    with pytest.raises(binding.RestoreRootBindingError, match='物理根'):
        run(c)
    assert (c['target']/'root-binding.json').read_bytes() == intent_before
    assert not (c['target']/'root-binding-done.json').exists()


def test_rehashed_binding_intent_cannot_grant_write_activation(materialized):
    c = materialized
    run(c)
    path = c['target']/'root-binding.json'
    intent = json.loads(path.read_bytes())
    intent['write_activation_allowed'] = True
    intent['intent_sha256'] = digest({k:v for k,v in intent.items() if k != 'intent_sha256'})
    path.write_text(json.dumps(intent),encoding='utf-8')
    with pytest.raises(binding.RestoreRootBindingError, match='意图'):
        run(c)
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(c['target']/'workbench.sqlite')


def test_unreadable_directory_is_not_silently_omitted_from_copy_verification(materialized, monkeypatch):
    c = materialized
    with verified_archive_view(c['package']) as view:
        copy_ = binding._MaterializedCopy(c['target'], c['plan'], view)
        original = binding.os.walk
        def unreadable(*args, **kwargs):
            if kwargs.get('onerror') is not None:
                kwargs['onerror'](PermissionError('synthetic unreadable directory'))
            yield from original(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(binding.os, 'walk', unreadable)
            with pytest.raises(PermissionError, match='synthetic unreadable directory'):
                copy_.verify({}, set())


@pytest.mark.parametrize('version',['1.0','2.0'])
def test_legacy_identity_cold_reads_preserve_absolute_history_as_read_only_evidence(
        execution_archive, private_temporary_directory, version):
    c = execution_archive
    if version == '2.0':
        path = c['root']/'.novelagent/project.json'
        identity = json.loads(path.read_bytes())
        identity.update(schema_version='2.0', root_hint='.', authority=LEGACY_AUTHORITY_PROJECTION)
        path.write_text(json.dumps(identity),encoding='utf-8')
    expected = load_project_identity(c['root']).to_dict()
    package, target = Path(capture(c)['path']), new_target()
    with absent_source(c['root']):
        plan = prepare(c, package, target, private_temporary_directory)
        materialize_archived_restore(package, plan=plan, temporary_root=private_temporary_directory)
        before = _files(target/'book')
        result = binding.bind_restored_roots(package,destination=target,temporary_root=private_temporary_directory)
        assert load_project_identity(target/'book').to_dict() == expected
        assert result['registry_count'] == 0 and not result['write_activation_allowed']
        assert _files(target/'book') == before
