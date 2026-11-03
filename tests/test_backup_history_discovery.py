"""Discover managed histories without treating author folders as journals."""
import json

import pytest

from core.engine.persistence import LocalPersistenceTransaction, PersistenceTarget
from core.engine.root_registry import RootRegistryService
from core.schema import validate_schema
import tests.test_persistence_v2 as v2_helpers
from workbench.backup_files import inventory_tree
from workbench.backup_history import verify_persistence_history


BOOK_ID = 'book-1'


def verify(root):
    return verify_persistence_history(root, inventory_tree(root), book_id=BOOK_ID)


@pytest.fixture
def book(tmp_path):
    root = tmp_path / 'book'
    root.mkdir()
    (root / 'author-note.txt').write_text('A normal author document.', encoding='utf-8')
    return root


@pytest.mark.parametrize('namespace', ['journals', 'transactions'])
def test_ordinary_author_folders_are_not_persistence_journals(book, namespace):
    directory = book / 'author-notes' / namespace / '2026'
    directory.mkdir(parents=True)
    (directory / 'note.txt').write_text('Ordinary notes, not a transaction.', encoding='utf-8')
    before = inventory_tree(book)
    result = verify(book)
    assert result['state'] == 'verified' and result['transactions'] == []
    assert inventory_tree(book) == before


@pytest.mark.parametrize('state', ['completed', 'rolled_back'])
@pytest.mark.parametrize('discovery', ['canonical', 'lock', 'root_registry', 'schema_only'])
def test_orphan_terminal_registry_is_discovered_without_journal_or_root_registry(book, state, discovery):
    control = (book / '.novelagent/runtime/persistence' if discovery == 'canonical'
               else book / 'custom-author-tool' / 'control')
    directory = control / 'registry' / state
    directory.mkdir(parents=True)
    if discovery == 'lock':
        (control / '.persistence.lock').write_bytes(b'')
    elif discovery == 'root_registry':
        runtime = book / 'custom-runtime'
        runtime.mkdir()
        RootRegistryService(control).ensure({'runtime': runtime})
    entry = {
        'schema_version': '2.1', 'book_id': BOOK_ID, 'run_id': 'lost',
        'state': state, 'journal_relative_path': 'journals/lost',
        'manifest_digest': '1' * 64, 'registered_at': '2026-09-12T00:00:00+00:00',
        'receipt': None, 'error_count': 0,
    }
    validate_schema(entry, 'persistence_registry_entry.schema.json')
    # Only schema_only relies on content recognition. The other modes must
    # discover their control root even when its sole registry entry is damaged.
    stored = entry if discovery == 'schema_only' else {'damaged': True}
    (directory / 'lost.json').write_text(json.dumps(stored), encoding='utf-8')
    before = inventory_tree(book)
    with pytest.raises((ValueError, RuntimeError)):
        verify(book)
    assert inventory_tree(book) == before


@pytest.mark.parametrize('keep_lock', [True, False])
def test_real_custom_v1_history_is_discovered_by_lock_or_manifest_schema(book, keep_lock):
    control = book / 'author-tool' / 'legacy-history'
    control.mkdir(parents=True)
    tx = LocalPersistenceTransaction(run_dir=control, run_id='custom-v1',
                                     allowed_roots=[book], book_id=BOOK_ID)
    tx.prepare([PersistenceTarget(kind='synthetic', path=book / 'state.txt', content='committed')],
               candidate_result={'run': {'id': 'custom-v1'}})
    tx.commit()
    tx.complete_publication()
    lock = control / '.persistence.lock'
    if not keep_lock:
        lock.unlink(missing_ok=True)
    before = inventory_tree(book)
    result = verify(book)
    assert result['state'] == 'verified' and len(result['transactions']) == 1
    assert result['transactions'][0]['journal'] == 'author-tool/legacy-history/transactions/custom-v1'
    assert inventory_tree(book) == before


def test_real_custom_v2_control_and_root_bindings_are_retained(book):
    runtime = book / 'custom-runtime'
    chapters = book / 'chapter-artifacts'
    delivery = book / 'delivery'
    for directory in (runtime, chapters, delivery):
        directory.mkdir()
    snapshot = runtime / 'snapshot.json'
    snapshot.write_bytes(b'{"chapter":1}\n')
    case = {
        'base': book, 'story': book, 'runtime': runtime, 'chapters': chapters,
        'delivery': delivery, 'snapshot': snapshot,
        'transaction_root': runtime / 'custom-control',
        'root_map': {'story_project': book, 'runtime': runtime, 'snapshot': runtime,
                     'chapter_artifacts': chapters, 'delivery_store': delivery},
    }
    helper = v2_helpers.PersistenceV2Test()
    tx, _ = helper._prepared(case, run_id='custom-v2', delivery_jobs=[])
    assert tx.commit()['committed']
    before = inventory_tree(book)
    result = verify(book)
    assert result['state'] == 'verified' and len(result['transactions']) == 1
    assert result['transactions'][0]['journal'] == 'custom-runtime/custom-control/journals/custom-v2'
    assert result['transactions'][0]['schema_version'] == '2.1'
    assert inventory_tree(book) == before


@pytest.mark.parametrize('damage', ['registry_file', 'unknown_state', 'unknown_file', 'state_file'])
def test_damaged_managed_registry_namespace_is_not_empty_history(book, damage):
    control = book / '.novelagent/runtime/persistence'
    control.mkdir(parents=True)
    registry = control / 'registry'
    if damage == 'registry_file':
        registry.write_text('damaged', encoding='utf-8')
    else:
        registry.mkdir()
        if damage == 'unknown_state':
            (registry / 'lost-state').mkdir()
        else:
            (registry / ('completed' if damage == 'state_file' else 'note.txt')).write_text('{}')
    before = inventory_tree(book)
    with pytest.raises((ValueError, RuntimeError)):
        verify(book)
    assert inventory_tree(book) == before
