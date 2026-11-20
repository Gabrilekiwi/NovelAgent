"""Offline business proof must exceed a valid file-transaction chain."""
from contextlib import closing
from pathlib import Path

import pytest

from core.engine.persistence import LocalPersistenceTransaction, PersistenceTarget
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_backup_mixed_history import mixed,case,transaction
from tests.test_backup_archive import software_tree,capture
from tests.test_backup_archive_history import absent_source
from workbench.backup_archive import BackupArchiveService
from workbench.backup_archive_history import verify_archived_persistence_history
from workbench.backup_archive_reviewed import verify_archived_reviewed_state
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


@pytest.fixture
def reviewed_archive(mixed,software_tree,monkeypatch):
    c=mixed;assert transaction(c).commit()['committed']
    base=c['root'].parent;machine=base/'m'
    monkeypatch.setattr('workbench.book_ownership.default_machine_store',lambda:machine)
    with BookOwnership(c['project'].book_id,machine_store=machine).locked() as owner:
        with c['db'].transaction() as connection:
            owner.enroll(root=c['root'],database=c['db'],project_id=c['project'].id,connection=connection)
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        service=BackupArchiveService(projects=c['projects'],maintenance=gate,task_runtime_root=c['runtime'],
            archive_root=base/'archives',machine_store=machine,runtime_config={'workbench_version':'0.1.0-beta',
            'job_execution_contract':'workbench_execution_v1','worker_enabled':False,'auto_import_active':False})
        yield {**c,'service':service,'op':op}
        gate.finish(op)


def verify(c,package):
    return verify_archived_reviewed_state(package,project_id=c['project'].id,book_id=c['project'].book_id)


def test_actual_reviewed_state_matches_live_verifier_after_source_disappears(reviewed_archive,monkeypatch):
    c=reviewed_archive;expected=load_verified_reviewed_identity(c['root'])
    package=Path(capture(c)['path']);before=inventory_tree(package);source_before=inventory_tree(c['root'])
    def forbidden(*args,**kwargs):raise AssertionError('live reviewed evidence used offline')
    import core.story_project.reviewed_state_store as state_store
    import core.story_project.reviewed_identity as identity
    for module in (state_store,identity):monkeypatch.setattr(module,'read_project_bytes',forbidden)
    monkeypatch.setattr(state_store,'verify_publication_receipt',forbidden)
    monkeypatch.setattr(state_store,'assert_safe_local_tree',forbidden)
    monkeypatch.setattr(state_store,'resolve_prose',forbidden)
    with absent_source(c['root']):
        first=verify(c,package);second=verify(c,package)
    assert first==second
    assert first['history']==expected['history'] and first['identity']==expected['identity']
    assert first['reviewed_state_verified'] and len(first['retained_receipts'])==2
    assert not first['recoverable'] and not first['restore_plan_ready'] and not first['write_activation_allowed']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==source_before


@pytest.mark.parametrize('damage',['current_state','quality','baseline_prose','extra_state','prose_alias'])
def test_valid_persistence_chain_still_rejects_invalid_reviewed_evidence(reviewed_archive,damage):
    c=reviewed_archive;root=c['root']
    if damage=='current_state':
        path=root/RELATIVE_PATH;content=b'corrupt reviewed document'
    elif damage=='quality':
        path=next((root/'.novelagent/runtime/reviewed/quality').glob('*.json'));content=b'{}'
    elif damage=='baseline_prose':
        path=next((root/'正文').glob('第001*.md'));content=b'altered historical chapter'
    elif damage=='extra_state':
        path=root/'.novelagent/runtime/reviewed/states/unclaimed.md';content=b'unclaimed'
    else:
        path=root/'正文/第002章_重复.md';content=b'conflicting prose alias'
    # A completed generic file transaction explains every byte change. A valid
    # reverse file graph alone must not certify the reviewed business history.
    tx=LocalPersistenceTransaction(run_dir=root/'.novelagent/runtime/runs',run_id='after-reviewed',
        allowed_roots=[root],book_id=c['project'].book_id)
    tx.prepare([PersistenceTarget(kind='synthetic',path=path,content=content)],
               candidate_result={'run':{'id':'after-reviewed'}})
    tx.commit();tx.complete_publication()
    package=Path(capture(c)['path'])
    assert verify_archived_persistence_history(package,project_id=c['project'].id,
                                               book_id=c['project'].book_id)['history_verified']
    before=inventory_tree(package);source_before=inventory_tree(root)
    with absent_source(root),pytest.raises((ValueError,RuntimeError)):
        verify(c,package)
    assert inventory_tree(package)==before and inventory_tree(root)==source_before


def test_offline_reviewed_target_is_explicit(reviewed_archive):
    c=reviewed_archive;package=Path(capture(c)['path'])
    with pytest.raises((ValueError,RuntimeError),match='目标'):
        verify_archived_reviewed_state(package,project_id='different',book_id=c['project'].book_id)
