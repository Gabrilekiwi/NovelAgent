"""Actual v2 writes/recovery fenced by a synthetic current-machine ledger."""
import subprocess
import sys
import pytest

from core.engine.persistence_v2 import (
    PersistenceV2IntegrityError, gc_persistence_v2, reconcile_pending_persistence_v2,
)
from tests.test_book_persistence_ownership import legacy, suspend, inventory, Crash, child_cannot_take_ownership
from tests import test_persistence_v21 as v21_helpers
from workbench import book_ownership as m


@pytest.fixture
def v2(legacy):
    c=legacy
    runtime=c['base']/'v2-runtime'; runtime.mkdir()
    artifacts=c['base']/'v2-artifacts'; artifacts.mkdir()
    snapshot=runtime/'snapshot.json'; snapshot.write_bytes(b'{"chapter":1}\n')
    sample=dict(story=c['root'],runtime=runtime,artifacts=artifacts,snapshot=snapshot,
        transaction_root=runtime/'persistence',root_map=dict(story_project=c['root'],runtime=runtime,
        snapshot=runtime,chapter_artifacts=artifacts))
    return {**c,'sample':sample,'transaction_root':sample['transaction_root']}


def prepare(c, *, fault=None):
    return v21_helpers.PersistenceV21Test()._prepare(c['sample'],book_id=c['identity'].book_id,fault_injector=fault)


@pytest.mark.parametrize('entry',['prepare','commit','complete','reconcile'])
def test_revoked_v2_writer_leaves_all_book_and_runtime_files_unchanged(v2,entry):
    c=v2
    def crash(event,index,path):
        if event=='after_commit_marker':raise Crash()
    if entry!='prepare':t=prepare(c,fault=crash if entry=='complete' else None)
    if entry=='complete':
        with pytest.raises(Crash):t.commit()
    suspend(c);before=inventory(c)
    with pytest.raises(m.BookOwnershipError):
        if entry=='prepare':prepare(c)
        elif entry=='commit':t.commit()
        elif entry=='complete':t.complete_publication()
        else:reconcile_pending_persistence_v2(c['transaction_root'])
    assert inventory(c)==before


@pytest.mark.parametrize('boundary',['after_temporary_journal_created','before_journal_publish','after_journal_publish'])
def test_revoked_recovery_cannot_quarantine_publish_or_register_journals(v2,boundary):
    c=v2
    def crash(event,index,path):
        if event==boundary:raise Crash()
    with pytest.raises(Crash):prepare(c,fault=crash)
    suspend(c);before=inventory(c)
    with pytest.raises(m.BookOwnershipError):reconcile_pending_persistence_v2(c['transaction_root'])
    assert inventory(c)==before
    assert not (c['transaction_root']/'abandoned').exists()


def test_completed_history_verification_is_readonly_but_index_rebuild_requires_ownership(v2):
    c=v2;t=prepare(c);assert t.commit()['committed']
    suspend(c);before=inventory(c)
    assert t.complete_publication()['committed']
    assert inventory(c)==before
    (c['transaction_root']/'registry/completed/run-1.json').unlink()
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError):reconcile_pending_persistence_v2(c['transaction_root'])
    assert inventory(c)==before


def test_actual_gc_is_fenced_while_retention_preview_remains_readonly(v2):
    c=v2;t=prepare(c);assert t.commit()['committed']
    suspend(c);before=inventory(c)
    assert gc_persistence_v2(c['transaction_root'],completed_keep=0)['deleted']
    assert inventory(c)==before
    with pytest.raises(m.BookOwnershipError):
        gc_persistence_v2(c['transaction_root'],dry_run=False,completed_keep=0)
    assert inventory(c)==before


def test_external_recovery_with_missing_registry_still_checks_manifest_book(v2):
    c=v2;prepare(c)
    (c['transaction_root']/'root_registry.json').unlink()
    suspend(c);before=inventory(c)
    with pytest.raises(m.BookOwnershipError):reconcile_pending_persistence_v2(c['transaction_root'])
    assert inventory(c)==before


def test_registry_book_id_keeps_damaged_unidentified_journal_from_becoming_unowned(v2):
    c=v2;t=prepare(c)
    t.manifest_path.write_text('{}')
    (c['root']/'.novelagent/project.json').unlink()
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError,match='actual project root'):
        reconcile_pending_persistence_v2(c['transaction_root'])
    assert inventory(c)==before


def test_unattributable_external_staging_is_not_silently_quarantined(v2):
    c=v2;stage=c['transaction_root']/'staging/unknown'
    stage.mkdir(parents=True);(stage/'partial.bin').write_bytes(b'partial')
    before=inventory(c)
    with pytest.raises(PersistenceV2IntegrityError,match='association'):
        reconcile_pending_persistence_v2(c['transaction_root'])
    assert inventory(c)==before and stage.exists()


def test_active_v2_commit_holds_machine_gate_through_apply_and_publication(v2):
    c=v2;observed=[]
    def observe(event,index,path):
        if event in ('before_apply_target','before_publication_receipt'):
            child_cannot_take_ownership(c);observed.append(event)
    t=prepare(c,fault=observe)
    assert t.commit()['committed']
    assert observed==['before_apply_target','before_publication_receipt']


def test_active_recovery_holds_machine_gate_before_journal_publication(v2):
    c=v2
    def crash(event,index,path):
        if event=='before_journal_publish':raise Crash()
    with pytest.raises(Crash):prepare(c,fault=crash)
    observed=[]
    def observe(event,index,path):
        # Recovery runs actual durable writes; no Provider or fake lock is used.
        if not observed:
            child_cannot_take_ownership(c);observed.append(event)
    result=reconcile_pending_persistence_v2(c['transaction_root'],fault_injector=observe)
    assert result['ok'] and result['transactions'][0]['state']=='rolled_back' and observed


def test_fresh_process_after_real_staging_exit_cannot_recover_revoked_book(v2):
    c=v2
    script='''from pathlib import Path
import os,sys
from workbench import book_ownership as m
from core.engine.persistence_v2 import reconcile_pending_persistence_v2
from tests.test_persistence_v21 import PersistenceV21Test
base=Path(sys.argv[1]); book_id=sys.argv[2]
m.default_machine_store=lambda:base/'machine'
runtime=base/'v2-runtime'; artifacts=base/'v2-artifacts'; root=base/'book'
if sys.argv[3]=='stage':
    case=dict(story=root,runtime=runtime,artifacts=artifacts,snapshot=runtime/'snapshot.json',
        transaction_root=runtime/'persistence',root_map=dict(story_project=root,runtime=runtime,
        snapshot=runtime,chapter_artifacts=artifacts))
    def crash(event,index,path):
        if event=='before_journal_publish':os._exit(79)
    PersistenceV21Test()._prepare(case,book_id=book_id,fault_injector=crash)
else:
    try:reconcile_pending_persistence_v2(runtime/'persistence')
    except m.BookOwnershipError:sys.exit(80)
raise AssertionError('expected guarded process boundary was not reached')
'''
    args=[sys.executable,'-B','-c',script,str(c['base']),c['identity'].book_id]
    staged=subprocess.run([*args,'stage'],capture_output=True,text=True,timeout=25)
    assert staged.returncode==79,staged.stderr
    assert (c['transaction_root']/'staging/run-1/manifest.json').exists()
    suspend(c);before=inventory(c)
    denied=subprocess.run([*args,'recover'],capture_output=True,text=True,timeout=25)
    assert denied.returncode==80,denied.stderr
    assert inventory(c)==before
