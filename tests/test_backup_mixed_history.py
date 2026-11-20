"""An actual legacy transaction, upgrade and typed chapter in one backup graph."""
import json

import pytest

from core.engine.persistence import LocalPersistenceTransaction,PersistenceTarget
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_chapter_commit import case,ready,transaction
from tests.test_backup_scope import inspect,codes
from workbench.backup_files import inventory_tree
from workbench.backup_history import verify_legacy_history,verify_persistence_history,BackupHistoryError
from workbench.jobs import JobStore


@pytest.fixture
def mixed(case):
    root=case['root'];identity=root/'.novelagent/project.json'
    (root/'.novelagent/runtime').mkdir(parents=True,exist_ok=True)
    old=LocalPersistenceTransaction(run_dir=root/'.novelagent/runtime/runs',run_id='pre-upgrade',
        allowed_roots=[root],book_id=case['old'].book_id)
    old.prepare([PersistenceTarget(kind='identity',path=identity,content=identity.read_bytes()),
        PersistenceTarget(kind='runtime_note',path=root/'.novelagent/runtime/retained.txt',content='old evidence')],
        candidate_result={'run':{'id':'pre-upgrade'}})
    old.commit();old.complete_publication()
    c=ready.__wrapped__(case)
    JobStore(c['db'])
    c.update(old_journal=old.journal_dir,runtime=root.parent/'jobs')
    return c


def verify(c):return verify_persistence_history(c['root'],inventory_tree(c['root']),book_id=c['project'].book_id)


@pytest.mark.parametrize('commit_chapter',[False,True])
def test_joint_graph_bridges_old_identity_through_real_upgrade_and_chapter_read_only(mixed,commit_chapter):
    c=mixed
    if commit_chapter:assert transaction(c).commit()['committed']
    before=inventory_tree(c['root'])
    with pytest.raises(BackupHistoryError,match='无法衔接'):
        verify_legacy_history(c['root'],before,book_id=c['project'].book_id)
    result=verify(c)
    assert result['schema_version']=='mixed_backup_history_v1'
    assert len(result['transactions'])==(3 if commit_chapter else 2)
    assert result['consistent_reverse_order'][-1].endswith('/pre-upgrade')
    assert result['baseline_targets']['.novelagent/project.json']==digest(c['case']['old_bytes'])
    scope=inspect(c)
    assert scope['scope_checks_passed'],scope['blockers']
    assert any(item['check']=='persistence_history' for item in scope['checks'])
    assert inventory_tree(c['root'])==before


@pytest.mark.parametrize('damage',['old_staged','old_marker','old_manifest','old_orphan','v2_staged',
    'v2_before','v2_candidate','v2_marker','v2_receipt','v2_orphan','v2_registry','v2_pending',
    'v2_missing_registry','v2_wrong_registry','v2_pending_registry','v2_staging'])
def test_retained_old_or_new_evidence_damage_blocks_scope_without_repair(mixed,damage):
    c=mixed;root=c['root'];old=c['old_journal']
    newer=root/'.novelagent/runtime/reviewed/p/journals/upgrade-1'
    manifest=json.loads((newer/'manifest.json').read_bytes())
    target=next(item for item in manifest['immutable']['targets'] if item['target_id']=='reviewed_identity')
    if damage=='old_staged':(old/'staged/000.bin').write_bytes(b'changed')
    elif damage=='old_marker':(old/'commit.marker').write_text('{}')
    elif damage=='old_manifest':(old/'manifest.json').unlink()
    elif damage=='old_orphan':(old.parent/'lost').mkdir()
    elif damage=='v2_staged':(newer/target['staged_relative_path']).write_bytes(b'changed')
    elif damage=='v2_before':(newer/target['backup_relative_path']).write_bytes(b'changed')
    elif damage=='v2_candidate':(newer/'candidate_result.json').write_bytes(b'{}')
    elif damage=='v2_marker':(newer/'commit.marker').write_text('{}')
    elif damage=='v2_receipt':(root/'.novelagent/runtime/reviewed/receipts/upgrade-1.json').write_text('{}')
    elif damage=='v2_orphan':(newer.parent/'lost').mkdir()
    elif damage=='v2_registry':(newer.parents[1]/'root_registry.json').write_text('{}')
    elif damage=='v2_missing_registry':(newer.parents[1]/'registry/completed/upgrade-1.json').unlink()
    elif damage=='v2_wrong_registry':
        path=newer.parents[1]/'registry/completed/upgrade-1.json';entry=json.loads(path.read_bytes())
        entry['receipt']['receipt_hash']='0'*64;path.write_text(json.dumps(entry),encoding='utf-8')
    elif damage=='v2_pending_registry':(newer.parents[1]/'registry/pending/ghost.json').write_text('{}')
    elif damage=='v2_staging':(newer.parents[1]/'staging/ghost').mkdir()
    else:
        manifest['state']='commit_marked'
        (newer/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    before=inventory_tree(root)
    with pytest.raises((ValueError,RuntimeError)):verify(c)
    assert 'identity_or_history_unverified' in codes(inspect(c))
    assert inventory_tree(root)==before


def test_real_rolled_back_v2_chapter_keeps_before_chain_and_requires_failure_receipt(mixed):
    c=mixed
    def fail(event,*_):
        if event=='before_commit_marker':raise RuntimeError('synthetic rollback')
    tx=transaction(c,fault_injector=fail)
    assert not tx.commit()['committed']
    before=inventory_tree(c['root']);result=verify(c)
    assert len(result['transactions'])==3 and any(item['state']=='rolled_back' for item in result['transactions'])
    assert inventory_tree(c['root'])==before
    (tx.journal_dir/'failure_receipt.json').write_text('{}')
    with pytest.raises(ValueError):verify(c)
