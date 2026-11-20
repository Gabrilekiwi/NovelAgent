"""Current author changes survive backup and remain explicit continuation gates."""
from pathlib import Path

import pytest

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.story_project import reviewed_state_document as document
from tests.test_backup_archive import software_tree,capture
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_reviewed import reviewed_archive,mixed
from tests.test_reviewed_upgrade import case as upgrade_case
from workbench.backup_archive_sources import inspect_archived_sources
from workbench.backup_files import inventory_tree


@pytest.fixture
def case():
    c=upgrade_case.__wrapped__();root=c['root']
    path=root/'设定/离线来源.md';path.write_text('# 合成来源\n作者的初始设定。',encoding='utf-8')
    raw=(root/'追踪/上下文.md').read_bytes()
    ref={'relative_path':'追踪/上下文.md','sha256':document.digest(raw),'text':raw.decode('utf-8').splitlines()[0]}
    c['inputs']['trusted_context']=ChapterCandidateContextBuilder(root).build(2)
    c['inputs']['static_references']=[ref]
    c['inputs']['confirmed_baseline']=document.baseline_request(book_id=c['old'].book_id,chapter=1,state=c['state'],
        sources=document.capture_sources(root),old_identity_sha256=document.digest(c['old_bytes']),static_references=[ref])
    return c


def inspect(c,package):return inspect_archived_sources(package,project_id=c['project'].id,book_id=c['project'].book_id)


def test_reviewed_sources_and_static_quotes_match_without_live_directory(reviewed_archive,monkeypatch):
    c=reviewed_archive;expected=document.capture_sources(c['root']);package=Path(capture(c)['path'])
    before=inventory_tree(package);source_before=inventory_tree(c['root'])
    def deny(*args,**kwargs):pytest.fail('offline source inspection accessed live source')
    monkeypatch.setattr(document,'capture_sources',deny);monkeypatch.setattr(document,'read_project_bytes',deny)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['source_snapshot_verified'] and first['current_sources_match']
    assert first['current_sources']==expected and not first['continuation_blockers']
    assert first['static_references'][0]['matches_reviewed_reference']
    assert not first['restore_plan_ready'] and not first['write_activation_allowed']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==source_before


@pytest.mark.parametrize('change',['added','edited','deleted'])
def test_author_edits_remain_preserved_and_require_explicit_review(reviewed_archive,change):
    c=reviewed_archive;path=c['root']/'设定/离线来源.md'
    if change=='added':(c['root']/'设定/新增来源.md').write_text('新的作者设定。',encoding='utf-8')
    elif change=='edited':path.write_text('作者修订。',encoding='utf-8')
    else:path.unlink()
    raw=(c['root']/document.RELATIVE_PATH).read_bytes()
    expected=document.describe_source_conflict(root=c['root'],document=raw,book_id=c['project'].book_id)
    package=Path(capture(c)['path']);before=inventory_tree(package)
    with absent_source(c['root']):result=inspect(c,package)
    assert result['source_snapshot_verified'] and not result['current_sources_match']
    assert result['source_conflict']==expected and result['author_review_required']
    assert result['continuation_blockers']==[{'code':'reviewed_manual_source_changed','reference':document.RELATIVE_PATH}]
    assert not result['write_activation_allowed'] and inventory_tree(package)==before


@pytest.mark.parametrize('damage',['changed_quote','missing_quote','invalid_utf8'])
def test_static_quote_and_unreadable_source_are_not_silently_accepted(reviewed_archive,damage):
    c=reviewed_archive;path=c['root']/'追踪/上下文.md'
    if damage=='changed_quote':path.write_text('作者替换了被引用的上下文。',encoding='utf-8')
    elif damage=='missing_quote':path.unlink()
    else:(c['root']/'设定/离线来源.md').write_bytes(b'bad-utf8-\xff')
    package=Path(capture(c)['path']);before=inventory_tree(package)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['current_sources_match'] and not result['write_activation_allowed']
    if damage=='changed_quote':
        assert result['source_snapshot_verified'] and result['author_review_required']
        assert 'reviewed_static_reference_changed' in {item['code'] for item in result['continuation_blockers']}
    else:
        assert not result['source_snapshot_verified']
        assert result['blockers']==[{'code':'reviewed_current_source_evidence_unverified','reference':'story_project'}]
    assert inventory_tree(package)==before
