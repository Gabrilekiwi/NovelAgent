"""Durable enrollment proof and actual process crashes on upgraded test books."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from core.autonomy.common import atomic_append_json
from core.engine.persistence import atomic_write_json
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_candidate_context import registered_upgrade
from tests.test_reviewed_upgrade import case
from workbench import book_ownership as m
from workbench.database import WorkbenchDatabase


@pytest.fixture
def enrollment(case, monkeypatch):
    db, projects, project, upgrade = registered_upgrade(case)
    root = case['root']
    store = root.parent/'machine-witness'
    monkeypatch.setattr(m, 'default_machine_store', lambda:store)
    owner = m.BookOwnership(project.book_id, machine_store=store)
    return dict(db=db, projects=projects, project=project, root=root, store=store, owner=owner)


def enroll(c, owner=None):
    owner = owner or c['owner']
    with owner.locked(), c['db'].transaction() as connection:
        return owner.enroll(root=c['root'], database=c['db'], project_id=c['project'].id, connection=connection)


def witness(c):
    return c['root']/m.WITNESS_PATH


def deny_output(c):
    target=c['root']/'must-not-create/new.json'
    with pytest.raises(m.BookOwnershipError):
        atomic_append_json(target, {'forbidden': True})
    assert not target.parent.exists()


def test_enrollment_records_book_proof_and_is_byte_idempotent(enrollment):
    c=enrollment;record=enroll(c)
    proof=json.loads(witness(c).read_text(encoding='utf-8'))
    assert record['version']==m.OWNERSHIP_V2 and record['state']=='active'
    assert record['registration_id']==proof['registration_id']
    assert record['witness_sha256']==proof['witness_sha256']
    before=(witness(c).read_bytes(),c['owner'].record_path.read_bytes())
    assert enroll(c)==record
    assert before==(witness(c).read_bytes(),c['owner'].record_path.read_bytes())
    atomic_append_json(c['root']/'allowed.json', {'qualified':True})


@pytest.mark.parametrize('loss', ['record', 'machine-directory'])
def test_missing_machine_record_cannot_reenroll_or_allow_ordinary_output(enrollment,loss):
    c=enrollment;enroll(c);before=witness(c).read_bytes()
    if loss=='record':
        c['owner'].record_path.unlink()
    else:
        destination=c['store'].with_name('lost-machine-directory')
        assert c['store'].is_relative_to(Path.cwd()/'.tmp') and destination.is_relative_to(Path.cwd()/'.tmp')
        c['store'].rename(destination)
    deny_output(c)
    with pytest.raises(m.BookOwnershipError,match='previously registered'):
        enroll(c)
    assert not c['owner'].record_path.exists() and witness(c).read_bytes()==before


@pytest.mark.parametrize('damage', ['missing','corrupt','other-registration','identity-missing'])
def test_missing_or_different_book_witness_cannot_be_repaired_by_active_enrollment(enrollment,damage):
    c=enrollment;enroll(c);before=c['owner'].record_path.read_bytes()
    if damage=='missing':witness(c).unlink()
    elif damage=='identity-missing':
        (c['root']/'.novelagent/project.json').unlink()
    elif damage=='corrupt':witness(c).write_text('{}',encoding='utf-8')
    else:
        value=json.loads(witness(c).read_text(encoding='utf-8'))
        value['registration_id']='a'*32
        value['witness_sha256']=digest({k:v for k,v in value.items() if k!='witness_sha256'})
        atomic_write_json(witness(c),value)
    deny_output(c)
    if damage!='identity-missing':
        with pytest.raises(m.BookOwnershipError):enroll(c)
    assert c['owner'].record_path.read_bytes()==before


@pytest.mark.parametrize('phase', ['after_enrollment_intent','after_enrollment_witness','after_enrollment_active'])
def test_actual_process_crash_resumes_original_enrollment_without_new_registration(enrollment,phase):
    c=enrollment
    script='''import os,sys
from pathlib import Path
from workbench.database import WorkbenchDatabase
from workbench.book_ownership import BookOwnership
def crash(phase,record):
    if phase==sys.argv[5]:os._exit(82)
db=WorkbenchDatabase(sys.argv[2])
owner=BookOwnership(sys.argv[3],machine_store=Path(sys.argv[4]),fault_injector=crash)
with owner.locked(),db.transaction() as connection:
    owner.enroll(root=Path(sys.argv[1]),database=db,project_id=sys.argv[6],connection=connection)
'''
    result=subprocess.run([sys.executable,'-B','-c',script,str(c['root']),str(c['db'].path),
                           c['project'].book_id,str(c['store']),phase,c['project'].id],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==82,result.stderr
    pending=json.loads(c['owner'].record_path.read_text(encoding='utf-8'))
    assert pending['state']==('active' if phase=='after_enrollment_active' else 'enrolling')
    if phase!='after_enrollment_active':deny_output(c)
    active=enroll(c)
    assert active['state']=='active' and active['registration_id']==pending['registration_id']
    assert active['binding']==pending['binding'] and active['generation']==pending['generation']
    assert enroll(c)==active
    atomic_append_json(c['root']/'after-restart.json', {'resumed':True})


def test_pending_enrollment_cannot_resume_in_another_database(enrollment):
    c=enrollment
    def crash(phase,record):
        if phase=='after_enrollment_witness':raise RuntimeError('synthetic interruption')
    owner=m.BookOwnership(c['project'].book_id,machine_store=c['store'],fault_injector=crash)
    with pytest.raises(RuntimeError,match='synthetic'):enroll(c,owner)
    other=WorkbenchDatabase(c['root'].parent/'other.db')
    with c['db'].connection() as source,other.connection() as target:source.backup(target)
    before=c['owner'].record_path.read_bytes()
    with pytest.raises(m.BookOwnershipError,match='physical ownership'):
        enroll({**c,'db':other})
    assert c['owner'].record_path.read_bytes()==before
    assert enroll(c)['state']=='active'


def test_copied_witness_cannot_enroll_on_an_empty_machine_store(enrollment,monkeypatch):
    c=enrollment;enroll(c)
    target=c['root'].parent/'copied-book'
    shutil.copytree(c['root'],target)
    empty=c['root'].parent/'empty-machine'
    monkeypatch.setattr(m,'default_machine_store',lambda:empty)
    deny_output({**c,'root':target})
    assert not empty.exists()


def test_explicit_old_active_record_migration_preserves_binding_and_rejects_lost_proof(enrollment):
    c=enrollment
    with c['owner'].locked(), c['db'].transaction() as connection:
        binding=c['owner']._binding(root=c['root'],database=c['db'],project_id=c['project'].id,connection=connection)
        old=dict(version='machine_book_ownership_v1',generation=4,state='active',binding=binding)
        old['record_sha256']=digest(old)
        atomic_write_json(c['owner'].record_path,old)
    active=enroll(c)
    assert active['version']==m.OWNERSHIP_V2 and active['generation']==4 and active['binding']==binding
    assert witness(c).exists()
    c['owner'].record_path.unlink()
    with pytest.raises(m.BookOwnershipError,match='previously registered'):enroll(c)
