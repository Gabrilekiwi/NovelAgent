"""Real legacy writers and subprocess locks, using only synthetic books/ledgers.

Legacy enrollment is deliberately fixture setup here: the supported onboarding
and restore handoff are separate unfinished product work.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

from core.engine.executor import AgentExecutor
from core.engine.persistence import (
    LocalPersistenceTransaction, PersistenceTarget, atomic_write_json,
    complete_persistence_transaction, reconcile_persistence_transaction,
)
from core.story_project.identity import ensure_project_identity
from core.story_project.reviewed_state_document import digest
from workbench import book_ownership as m


@pytest.fixture
def legacy(monkeypatch):
    base = Path.cwd()/'.tmp/lwo'/uuid.uuid4().hex[:12]
    root = base/'book'
    root.mkdir(parents=True)
    identity = ensure_project_identity(root)
    database = base/'workbench.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE fixture (value TEXT)')
    store = base/'machine'
    monkeypatch.setattr(m, 'default_machine_store', lambda: store)
    owner = m.BookOwnership(identity.book_id, machine_store=store)
    binding = dict(book_id=identity.book_id, root=str(root), root_lineage=m._lineage(root),
        database=str(database), database_parent_lineage=m._lineage(base),
        database_file_identity=m._file_identity(database), project_id='synthetic-registration',
        identity_sha256=digest((root/'.novelagent/project.json').read_bytes()))
    record = dict(version='machine_book_ownership_v1', generation=1, state='active', binding=binding)
    record['record_sha256'] = digest(record)
    with owner.locked():
        atomic_write_json(owner.record_path, record)
    target = root/'chapter.txt'
    target.write_text('before', encoding='utf-8')
    snapshot = root/'snapshot.json'
    snapshot.write_text(json.dumps(dict(chapter_index=2, world_state=dict(infection_level='medium', locations={}),
        characters={}, timeline=[])), encoding='utf-8')
    return locals()


def suspend(c, state='restoring'):
    record = {**c['record'], 'state':state}
    record['record_sha256'] = digest({k:v for k,v in record.items() if k != 'record_sha256'})
    with c['owner'].locked():
        atomic_write_json(c['owner'].record_path, record)


def tx(c, *, fault=None, root=None, book_id=True):
    return LocalPersistenceTransaction(run_dir=c['base']/'external-runs', run_id='chapter-new',
        allowed_roots=[root or c['root']], book_id=c['identity'].book_id if book_id else None,
        fault_injector=fault)


def inventory(c):
    return {str(p.relative_to(c['base'])):p.read_bytes() for p in c['base'].rglob('*') if p.is_file()}


@pytest.mark.parametrize('entry', ['prepare', 'commit', 'complete_method', 'complete_function', 'reconcile'])
def test_inactive_legacy_book_blocks_each_direct_mutation_with_external_journal(legacy, entry):
    c=legacy; t=tx(c)
    if entry != 'prepare':
        t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    if entry.startswith('complete'):
        assert t.commit().committed
    suspend(c)
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError, match='no longer active'):
        if entry == 'prepare': t.prepare([PersistenceTarget('prose', c['target'], 'after')])
        elif entry == 'commit': t.commit()
        elif entry == 'complete_method': t.complete_publication()
        elif entry == 'complete_function': complete_persistence_transaction(t.journal_dir)
        else: reconcile_persistence_transaction(t.journal_dir)
    assert inventory(c)==before


class Crash(BaseException):
    pass


def test_reconcile_cannot_rollback_formal_bytes_after_ownership_revocation(legacy):
    c=legacy
    def crash(event, index, path):
        if event=='after_target_replace': raise Crash()
    t=tx(c, fault=crash)
    t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    with pytest.raises(Crash): t.commit()
    assert c['target'].read_text()=='after'
    suspend(c); before=inventory(c)
    with pytest.raises(m.BookOwnershipError): reconcile_persistence_transaction(t.journal_dir)
    assert inventory(c)==before


@pytest.mark.parametrize('terminal', ['completed', 'rolled_back'])
def test_terminal_history_remains_readable_without_repair_after_revocation(legacy, terminal):
    c=legacy; t=tx(c)
    t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    if terminal=='completed':
        assert t.commit().committed
        assert t.complete_publication().state=='completed'
    else:
        assert reconcile_persistence_transaction(t.journal_dir).state=='rolled_back'
    suspend(c); before=inventory(c)
    assert reconcile_persistence_transaction(t.journal_dir).state==terminal
    if terminal=='completed':
        assert complete_persistence_transaction(t.journal_dir).state=='completed'
        assert t.complete_publication().state=='completed'
    assert inventory(c)==before


def test_external_runtime_cannot_use_enrolled_id_without_actual_project(legacy):
    c=legacy; external=c['base']/'outside'; external.mkdir()
    t=tx(c, root=external)
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError, match='actual project root'):
        t.prepare([PersistenceTarget('external', external/'new.json', '{}')])
    assert inventory(c)==before


def test_wrong_declared_book_id_does_not_mask_actual_target_ownership(legacy):
    c=legacy; t=tx(c); t.book_id='another-book'
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError, match='differs'):
        t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    assert inventory(c)==before


def test_target_path_fences_legacy_transaction_even_without_declared_book_id(legacy):
    c=legacy; t=tx(c, book_id=False); suspend(c)
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError):
        t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    assert inventory(c)==before


def child_cannot_take_ownership(c):
    code='''from pathlib import Path
import sys
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceAlreadyRunning
try:
    with BookOwnership(sys.argv[1],machine_store=Path(sys.argv[2])).locked():
        raise AssertionError('writer did not retain machine gate')
except InstanceAlreadyRunning:
    sys.exit(81)
'''
    result=subprocess.run([sys.executable,'-B','-c',code,c['identity'].book_id,str(c['store'])],
        capture_output=True,text=True,timeout=20)
    assert result.returncode==81, result.stderr


def test_active_direct_commit_holds_machine_gate_across_actual_target_replace(legacy):
    c=legacy; observed=[]
    def observe(event, index, path):
        if event=='before_target_replace':
            child_cannot_take_ownership(c); observed.append(path)
    t=tx(c, fault=observe)
    t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    assert t.commit().committed
    assert t.complete_publication().state=='completed'
    assert observed==[c['target']] and c['target'].read_text()=='after'


def executor(c, **kwargs):
    return AgentExecutor(snapshot_path=c['snapshot'], memory_path=c['root']/'missing-memory.json',
        run_dir=c['base']/'executor-runs', persistence_dir=c['base']/'executor-transactions',
        chapter_dir=c['base']/'executor-chapters', **kwargs)


@pytest.mark.parametrize('entry', ['run', 'candidate', 'direct', 'loop'])
def test_executor_rejects_before_provenance_provider_and_identity_preparation(legacy, entry):
    c=legacy; calls=[]
    e=executor(c, dry_run=entry!='candidate', generator=lambda p: calls.append(p) or 'must not run')
    suspend(c); before=inventory(c)
    with pytest.raises(m.BookOwnershipError):
        if entry=='direct': e._invoke_persist_once(snapshot_override=None,previous_result=None,chapter_hint=None)
        elif entry=='loop': e.run_loop(steps=2)
        else: e.run_once(persist=entry!='candidate')
    assert not calls and inventory(c)==before
    assert e._execution_scope_depth==0


def test_configured_story_root_guards_executor_when_all_outputs_are_external(legacy):
    c=legacy; e=executor(c, dry_run=True)
    e.snapshot_path=c['base']/'external-snapshot.json'
    e.snapshot_path.write_bytes(c['snapshot'].read_bytes())
    e.story_project_context={'story_project_root':str(c['root'])}
    suspend(c); before=inventory(c)
    with pytest.raises(m.BookOwnershipError): e.run_once()
    assert inventory(c)==before


def test_removed_project_identity_does_not_turn_declared_enrolled_transaction_into_unowned_work(legacy):
    c=legacy; t=tx(c)
    (c['root']/'.novelagent/project.json').unlink()
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError, match='actual project root'):
        t.prepare([PersistenceTarget('prose', c['target'], 'after')])
    assert inventory(c)==before


def test_readonly_dry_run_needs_no_write_ownership_and_changes_no_files(legacy):
    c=legacy; suspend(c); before=inventory(c)
    result=executor(c, dry_run=True).run_once(persist=False)
    assert result['validation']['ok'] and inventory(c)==before


def test_active_executor_persists_under_machine_lock_without_reenrollment(legacy):
    c=legacy; e=executor(c, dry_run=True)
    before_record=c['owner'].record_path.read_bytes()
    original=e._begin_execution_evidence
    def observe(*,persist):
        child_cannot_take_ownership(c)
        return original(persist=persist)
    e._begin_execution_evidence=observe
    result=e.run_once(persist=True)
    assert result['committed']
    assert json.loads(c['snapshot'].read_text())['chapter_index']==3
    assert c['owner'].record_path.read_bytes()==before_record
