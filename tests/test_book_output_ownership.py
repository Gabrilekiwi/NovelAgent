"""Actual auxiliary writes share the machine ownership transition boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from core.autonomy.common import atomic_append_json
from core.engine.persistence import atomic_write_json, PersistenceLockError
from core.engine.project_output import project_output_lock
from core.story_project.reviewed_state_document import digest
from tests.test_book_ownership import case, owned
from workbench import book_ownership as ownership


@pytest.fixture
def bound(owned, monkeypatch):
    c=owned
    monkeypatch.setattr(ownership, 'default_machine_store', lambda:c['machine'])
    return {**c, 'root':c['case']['root'], 'output':c['case']['root']/'auxiliary/new.json'}


def test_active_book_allows_actual_auxiliary_publication_without_reenrollment(bound):
    c=bound;before=c['owner'].record_path.read_bytes()
    atomic_append_json(c['output'], {'synthetic':True})
    assert json.loads(c['output'].read_text())=={'synthetic':True}
    assert c['owner'].record_path.read_bytes()==before


def test_unenrolled_legacy_writer_does_not_create_an_ownership_record(case, monkeypatch):
    store=case['root'].parent/'unused-machine'
    monkeypatch.setattr(ownership, 'default_machine_store', lambda:store)
    path=case['root']/'auxiliary.json'
    atomic_append_json(path, {'old_flow':True})
    assert path.is_file() and not store.exists()


def test_first_enrollment_cannot_race_an_unenrolled_legacy_output(case, monkeypatch):
    store=case['root'].parent/'first-machine'
    monkeypatch.setattr(ownership, 'default_machine_store', lambda:store)
    code='''from pathlib import Path
import sys
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceAlreadyRunning
try:
    with BookOwnership(sys.argv[1],machine_store=Path(sys.argv[2])).locked():
        raise AssertionError('first machine enrollment raced an active legacy writer')
except InstanceAlreadyRunning:
    sys.exit(81)
'''
    with project_output_lock(case['root']/'auxiliary.json'):
        result=subprocess.run([sys.executable,'-B','-c',code,case['old'].book_id,str(store)],
            capture_output=True,text=True,timeout=20)
        assert result.returncode==81,result.stderr
    assert not store.exists()


@pytest.mark.parametrize('state',['restoring','suspended'])
def test_inactive_ownership_rejects_actual_auxiliary_write_before_directory_creation(bound,state):
    c=bound;value={**c['record'],'state':state}
    value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
    with c['owner'].locked():atomic_write_json(c['owner'].record_path,value)
    before=c['owner'].record_path.read_bytes()
    with pytest.raises(ownership.BookOwnershipError,match='no longer active'):
        atomic_append_json(c['output'], {'must_not_write':True})
    assert not c['output'].parent.exists() and c['owner'].record_path.read_bytes()==before


@pytest.mark.parametrize('damage',['record','identity'])
def test_corrupt_record_cannot_be_treated_as_an_unenrolled_book(bound, damage):
    c=bound
    target=c['owner'].record_path if damage=='record' else c['root']/'.novelagent/project.json'
    target.write_text('{}')
    with pytest.raises(ownership.BookOwnershipError):
        atomic_append_json(c['output'], {'must_not_write':True})
    assert not c['output'].parent.exists()


def test_same_identity_in_copied_book_does_not_inherit_auxiliary_write_qualification(bound):
    c=bound;copy=c['root'].parent/'copy'
    assert copy.is_relative_to(Path.cwd()/'.tmp')
    shutil.copytree(c['root'],copy)
    with pytest.raises(ownership.BookOwnershipError,match='no longer active'):
        atomic_append_json(copy/'auxiliary/new.json', {'must_not_write':True})
    assert not (copy/'auxiliary').exists() and not c['output'].parent.exists()


def test_identical_bytes_in_a_replaced_registered_database_revoke_auxiliary_writes(bound):
    c=bound;database=c['db'].path;old=database.with_name('original-database.sqlite')
    assert database.is_relative_to(Path.cwd()/'.tmp') and old.is_relative_to(Path.cwd()/'.tmp')
    database.rename(old)
    shutil.copyfile(old,database)
    with pytest.raises(ownership.BookOwnershipError,match='no longer active'):
        atomic_append_json(c['output'], {'must_not_write':True})
    assert not c['output'].parent.exists()


def test_same_thread_owned_publication_borrows_the_real_machine_gate(bound):
    c=bound
    with c['owner'].locked():
        with project_output_lock(c['output']):
            atomic_append_json(c['output'], {'nested':True})
    assert json.loads(c['output'].read_text())=={'nested':True}


def test_copied_context_in_another_thread_cannot_borrow_machine_lock(bound):
    c=bound
    with c['owner'].locked(), ThreadPoolExecutor(max_workers=1) as pool:
        context=copy_context()
        future=pool.submit(context.run, lambda: _direct_output(c['output']))
        with pytest.raises(PersistenceLockError):future.result(timeout=10)
    assert not c['output'].parent.exists()


def _direct_output(path):
    with project_output_lock(path):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('unexpected')


@pytest.mark.parametrize('holder',['auxiliary','ownership'])
def test_real_process_serializes_ownership_changes_and_auxiliary_writes_both_directions(bound,holder):
    c=bound
    script='''from pathlib import Path
import sys
from core.engine.project_output import project_output_lock
from core.engine.persistence import PersistenceLockError
from workbench.instance import InstanceAlreadyRunning
from workbench import book_ownership as m
m.default_machine_store=lambda:Path(sys.argv[2])
try:
    context=(m.BookOwnership(sys.argv[1],machine_store=Path(sys.argv[2])).locked()
        if sys.argv[4]=='auxiliary' else project_output_lock(Path(sys.argv[3])))
    with context:
        raise AssertionError('second process acquired a machine boundary already held by the first')
except PersistenceLockError:
    if sys.argv[4]!='ownership':raise
    sys.exit(81)
except InstanceAlreadyRunning:
    if sys.argv[4]!='auxiliary':raise
    sys.exit(81)
'''
    context=project_output_lock(c['output']) if holder=='auxiliary' else c['owner'].locked()
    with context:
        result=subprocess.run([sys.executable,'-B','-c',script,c['project'].book_id,str(c['machine']),str(c['output']),holder],
            capture_output=True,text=True,timeout=20)
        assert result.returncode==81,result.stderr
    assert not c['output'].parent.exists()
    assert json.loads(c['owner'].record_path.read_text(encoding='utf-8'))==c['record']
