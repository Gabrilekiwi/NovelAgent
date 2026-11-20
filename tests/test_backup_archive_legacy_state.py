"""Offline legacy facts use the same pure parser and activation decision."""
import json
from pathlib import Path

import pytest

from core.story_project.activation import evaluate_story_state_activation
from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION, load_project_identity
from core.story_project import semantic_parser
from tests.test_backup_archive import capture, software_tree
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_executions import execution_archive
from tests.test_backup_archive_history import absent_source
from workbench.backup_archive_legacy_state import inspect_archived_legacy_state
from workbench.backup_files import inventory_tree


def inspect(c, package):
    return inspect_archived_legacy_state(package, project_id=c['project'].id, book_id=c['project'].book_id)


@pytest.mark.parametrize('version', ['1.0', '2.0'])
def test_offline_legacy_semantics_equal_live_with_no_source_or_store(execution_archive, monkeypatch, version):
    c = execution_archive
    path = c['root'] / '.novelagent/project.json'
    if version == '2.0':
        value = json.loads(path.read_bytes())
        value.update(schema_version='2.0', root_hint='.', authority=LEGACY_AUTHORITY_PROJECTION)
        path.write_text(json.dumps(value), encoding='utf-8')
    identity = load_project_identity(c['root'])
    expected = semantic_parser.parse_story_project_semantic_state(c['root'], 2, project_identity=identity)
    activation = evaluate_story_state_activation(identity, expected)
    package = Path(capture(c)['path'])
    before = inventory_tree(package)
    source_before = inventory_tree(c['root'])
    def forbidden(*args, **kwargs):
        pytest.fail('offline parser tried to read the source directory')
    for method in ('_load_sources', 'load_text', 'resolve_outline', 'resolve_prose', 'project_identity_for_operation'):
        monkeypatch.setattr(semantic_parser, method, forbidden)
    with absent_source(c['root']):
        first = inspect(c, package)
        second = inspect(c, package)
    assert first == second
    assert first['legacy_state_verified'] and first['ready_for_next_chapter']
    assert first['semantic_state'] == expected and first['activation'] == activation
    assert not first['recoverable'] and not first['write_activation_allowed']
    assert inventory_tree(package) == before and inventory_tree(c['root']) == source_before


@pytest.mark.parametrize('damage', ['prose_alias', 'outline_alias', 'encoding'])
def test_ambiguous_or_unreadable_legacy_sources_block_evidence(execution_archive, damage):
    c = execution_archive
    if damage == 'prose_alias':
        (c['root'] / '正文/第001章_重复.md').write_text('另一个版本', encoding='utf-8')
    elif damage == 'outline_alias':
        (c['root'] / '大纲/细纲_第002章_重复.md').write_text('另一个章纲', encoding='utf-8')
    else:
        (c['root'] / '追踪/上下文.md').write_bytes(b'\xff')
    package = Path(capture(c)['path'])
    with absent_source(c['root']):
        result = inspect(c, package)
    assert not result['legacy_state_verified']
    code = {'prose_alias':'legacy_prose_alias', 'outline_alias':'legacy_outline_alias',
            'encoding':'legacy_semantic_evidence_unverified'}[damage]
    assert code in {b['code'] for b in result['blockers']}


@pytest.mark.parametrize('change', ['unfinished_outline', 'changed_profile'])
def test_retained_author_work_requires_fresh_preparation(execution_archive, change):
    c = execution_archive
    if change == 'unfinished_outline':
        outline = c['root'] / '大纲/细纲_第002章.md'
        assert outline.resolve().is_relative_to(Path('.tmp').resolve())
        outline.unlink()
        expected = 'legacy_next_outline_required'
    else:
        path = c['root'] / '追踪/上下文.md'
        path.write_text(path.read_text(encoding='utf-8') + '\nNovelAgent:story_project_writeback\n', encoding='utf-8')
        expected = 'strict_profile_version_mismatch'
    package = Path(capture(c)['path'])
    with absent_source(c['root']):
        result = inspect(c, package)
    assert result['legacy_state_verified'], result['blockers']
    assert not result['ready_for_next_chapter']
    assert expected in {b['code'] for b in result['continuation_blockers']}


def test_interrupted_event_authority_cannot_qualify_as_legacy(execution_archive):
    c = execution_archive
    receipts = c['root'] / '.novelagent/authority/receipts'
    receipts.mkdir(parents=True)
    (receipts / 'incomplete.json').write_text('{}', encoding='utf-8')
    package = Path(capture(c)['path'])
    with absent_source(c['root']), pytest.raises(RuntimeError, match='事件权威'):
        inspect(c, package)
