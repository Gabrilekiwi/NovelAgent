"""Real v1 journals: overlapping chapters, rollback and corrupt evidence."""
import json

import pytest

from core.engine.persistence import LocalPersistenceTransaction,PersistenceTarget
from workbench.backup_history import BackupHistoryError,verify_legacy_history
from workbench.backup_files import inventory_tree
from tests.test_backup_scope import legacy,inspect,codes


def commit(c,run,changes,*,rollback=False,candidate=False):
    def fail(event,*_):
        if rollback and event=='before_commit_marker':raise RuntimeError('synthetic failure')
    transaction=LocalPersistenceTransaction(run_dir=c['root']/'.novelagent/runtime/runs',run_id=run,
        allowed_roots=[c['root']],book_id=c['project'].book_id,fault_injector=fail)
    transaction.prepare([PersistenceTarget(kind='synthetic',path=c['root']/name,content=text)
        for name,text in changes.items()],candidate_result={'run':{'id':run},'committed':True} if candidate else None)
    result=transaction.commit()
    if not rollback:transaction.complete_publication()
    return transaction.journal_dir


def verify(c,**kwargs):return verify_legacy_history(c['root'],inventory_tree(c['root']),book_id=c['project'].book_id,**kwargs)


@pytest.fixture
def history(legacy):
    c=legacy;(c['root']/'state.txt').write_text('baseline')
    first=commit(c,'z-first',{'state.txt':'chapter 1','notes.txt':'retained'},candidate=True)
    second=commit(c,'a-second',{'state.txt':'chapter 2','notes.txt':'retained'})
    return {**c,'first':first,'second':second}


def test_overlapping_commits_keep_old_staged_files_and_prove_current_tip_read_only(history):
    c=history;before=inventory_tree(c['root']);result=verify(c)
    assert result['state']=='verified' and len(result['transactions'])==2
    assert [name.split('/')[-1] for name in result['consistent_reverse_order']]==['a-second','z-first']
    assert result['baseline_targets']['notes.txt'] is None
    assert inventory_tree(c['root'])==before
    scope=inspect(c);assert scope['scope_checks_passed'],scope['blockers']
    assert any(check['check']=='legacy_v1_history' and len(check['evidence']['transactions'])==2 for check in scope['checks'])
    assert not scope['archive_certified']


def test_repeated_states_noop_targets_and_rolled_back_transaction_are_supported(history):
    c=history
    commit(c,'rollback',{'state.txt':'failed content','absent.txt':'must stay absent'},rollback=True)
    commit(c,'repeat',{'state.txt':'chapter 1'})
    commit(c,'last',{'state.txt':'chapter 3','notes.txt':'retained'})
    result=verify(c)
    assert len(result['transactions'])==5
    assert not (c['root']/'absent.txt').exists()
    assert (c['root']/'state.txt').read_text()=='chapter 3'


@pytest.mark.parametrize('damage',['staged','backup','candidate','marker','current','missing_manifest','orphan','external','pending','malformed'])
def test_incomplete_or_corrupt_history_cannot_be_certified(history,damage):
    c=history;journal=c['first'];path=journal/'manifest.json'
    if damage in {'staged','backup','candidate','marker'}:
        target={'staged':'staged/000.bin','backup':'backups/000.bin','candidate':'candidate_result.json','marker':'commit.marker'}[damage]
        (journal/target).write_text('{}')
    elif damage=='current':(c['root']/'state.txt').write_text('unaccounted change')
    elif damage=='missing_manifest':path.unlink()
    elif damage=='orphan':(journal.parent/'orphan').mkdir()
    else:
        manifest=json.loads(path.read_bytes())
        if damage=='external':manifest['targets'][0]['path']=str(c['root'].parent/'unapproved.txt')
        elif damage=='pending':manifest['state']='commit_marked'
        else:manifest['targets'][0]['path']=None
        path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises((BackupHistoryError,ValueError)):verify(c)
    assert 'identity_or_history_unverified' in codes(inspect(c))


def test_locally_valid_before_image_cannot_break_cross_transaction_chain(history):
    c=history;path=c['second']/'manifest.json';manifest=json.loads(path.read_bytes())
    from core.story_project.reviewed_state_document import digest
    raw=b'not the first committed state';(c['second']/'backups/000.bin').write_bytes(raw)
    manifest['targets'][0]['before_sha256']=digest(raw);manifest['targets'][0]['before_size']=len(raw)
    path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(BackupHistoryError,match='无法衔接'):verify(c)


def test_search_limit_is_an_explicit_failure_not_partial_success(history):
    with pytest.raises(BackupHistoryError,match='计算上限'):verify(history,max_search=1)


@pytest.mark.parametrize('damage',['none','missing_journal','wrong_run','pending'])
def test_final_run_reference_requires_its_settled_matching_journal(history,damage):
    c=history;journal=c['second'];run='a-second'
    value={'run':{'id':run},'persistence':{'run_id':run,'state':'completed','committed':True,
        'partial':False,'journal_path':str(journal)}}
    if damage=='missing_journal':value['persistence']['journal_path']=str(journal.parent/'lost-journal')
    elif damage=='wrong_run':value['run']['id']='other'
    elif damage=='pending':value['persistence']['state']='commit_marked'
    (journal.parent.parent/(run+'.json')).write_text(json.dumps(value),encoding='utf-8')
    if damage=='none':
        result=verify(c);assert len(result['final_run_records'])==1
    else:
        with pytest.raises(BackupHistoryError):verify(c)
