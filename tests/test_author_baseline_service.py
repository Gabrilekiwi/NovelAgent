"""Real registered-book mutation boundary; no borrowed backup read snapshot."""
import copy
import json

import pytest

from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case,contents
from tests.test_registration_upgrade_archive import old_package,tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import logical
from tests.test_backup_archive_history import absent_source
from tests.test_backup_external_writers import child
from workbench.author_baseline_service import AuthorBaselineReviewService
from workbench.author_baselines import CATEGORIES,AuthorBaselineError
from workbench.book_ownership import BookOwnership,_record,WITNESS_PATH
from core.story_project.reviewed_state_document import digest
from workbench.maintenance import MaintenanceError


@pytest.fixture
def service_case(registration_case):
    c=registration_case;old_package(c)
    c['reviews']=AuthorBaselineReviewService(c['app'].state.backups)
    return c


def preview(c,**kwargs):
    return c['reviews'].preview(c['project'].id,operation_id=c['op'],state=c['state'],calibration_report=c['report'],**kwargs)


def create(c,expected=None,**kwargs):
    expected=expected or preview(c)
    return c['reviews'].create(c['project'].id,operation_id=c['op'],state=c['state'],calibration_report=c['report'],
        expected_preview_sha256=expected['preview_sha256'],**kwargs)


def test_actual_service_previews_mutates_and_confirms_under_one_real_sql_transaction(service_case,monkeypatch):
    c=service_case;service=c['reviews'];book=contents(c['root'])
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    control={k:v for k,v in contents(owner.directory).items() if k!='workbench-instance.json'}
    def forbidden(*args,**kwargs):pytest.fail('review write entered the unchanged-SQL backup snapshot')
    monkeypatch.setattr(c['app'].state.backups,'_source',forbidden)
    sql=logical(c['app'].state.database.path)
    first=preview(c);assert preview(c)==first
    assert logical(c['app'].state.database.path)==sql
    saved=create(c,first);record=saved['record']
    assert record['revision']==1 and not record['author_confirmed'] and saved['fresh_backup_required']
    assert not saved['can_upgrade'] and not saved['publication_authorized']
    checked=service.inspect(c['project'].id,record['id'],operation_id=c['op'])
    assert checked['source_current'] and checked['can_confirm']
    confirmed=service.revise(c['project'].id,record['id'],operation_id=c['op'],expected_revision=1,
        expected_sha256=record['record_sha256'],author_confirmed=True,confirmed_categories=CATEGORIES)['record']
    assert confirmed['status']=='confirmed' and confirmed['revision']==2
    edited=service.revise(c['project'].id,record['id'],operation_id=c['op'],expected_revision=2,
        expected_sha256=confirmed['record_sha256'],state=c['state'])['record']
    assert edited['status']=='draft' and not edited['author_confirmed']
    assert service.history(c['project'].id,record['id'])['items']==[record,confirmed,edited]
    assert contents(c['root'])==book
    assert {k:v for k,v in contents(owner.directory).items() if k!='workbench-instance.json'}==control
    assert c['app'].state.maintenance.status()['state']=='quiescent'


@pytest.mark.parametrize('damage',['missing_control','inactive','missing_witness','wrong_operation','not_maintenance'])
def test_service_refuses_missing_inactive_or_wrong_maintenance_without_repair(service_case,damage):
    c=service_case;expected=preview(c)
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine']);missing=None
    if damage=='missing_control':missing=owner.directory/'ownership.json';missing.unlink()
    elif damage=='missing_witness':missing=c['root']/WITNESS_PATH;missing.unlink()
    elif damage=='inactive':
        value=_record(owner.directory,c['project'].book_id);value['state']='inactive'
        value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
        (owner.directory/'ownership.json').write_text(json.dumps(value),encoding='utf-8')
    elif damage=='wrong_operation':c['op']='f'*32
    else:c['app'].state.maintenance.finish(c['op'])
    book=contents(c['root']);control=contents(owner.directory);sql=logical(c['app'].state.database.path)
    with pytest.raises((ValueError,MaintenanceError)):create(c,expected)
    assert contents(c['root'])==book and contents(owner.directory)==control
    assert logical(c['app'].state.database.path)==sql
    if missing is not None:assert not missing.exists()


def test_unregistered_book_cannot_be_implicitly_enrolled_by_review_preview(registration_case):
    c=registration_case;c['reviews']=AuthorBaselineReviewService(c['app'].state.backups)
    sql=logical(c['app'].state.database.path);book=contents(c['root'])
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine']);control=contents(owner.directory)
    with pytest.raises(AuthorBaselineError):preview(c)
    assert logical(c['app'].state.database.path)==sql and contents(c['root'])==book
    assert contents(owner.directory)==control and _record(owner.directory,c['project'].book_id) is None


@pytest.mark.parametrize('change',['source','state','preview_hash'])
def test_actual_preview_is_bound_to_source_and_submitted_review_content(service_case,change):
    c=service_case;expected=preview(c)
    if change=='source':(c['root']/'设定/late-note.md').write_text('new author source',encoding='utf-8')
    elif change=='state':
        c['state']=copy.deepcopy(c['state']);next(iter(c['state']['characters'].values()))['current_goal']='different goal'
    else:expected={**expected,'preview_sha256':'f'*64}
    book=contents(c['root']);sql=logical(c['app'].state.database.path)
    with pytest.raises(ValueError):create(c,expected)
    assert logical(c['app'].state.database.path)==sql and contents(c['root'])==book


@pytest.mark.parametrize('failure',['source_after_insert','exception_after_insert'])
def test_final_boundary_failure_rolls_back_the_whole_review_sql_write(service_case,failure):
    c=service_case;expected=preview(c);sql=logical(c['app'].state.database.path)
    def fail(event,root):
        assert event=='before_review_commit'
        if failure=='source_after_insert':(root/'设定/during-save.md').write_text('late source edit',encoding='utf-8')
        else:raise RuntimeError('synthetic final check failure')
    c['reviews'].fault_injector=fail
    with pytest.raises((ValueError,RuntimeError)):create(c,expected)
    assert logical(c['app'].state.database.path)==sql
    with c['app'].state.database.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM author_baseline_revisions').fetchone()[0]==0


def test_borrowed_maintenance_permit_is_reused_without_closing_its_owner(service_case):
    c=service_case;gate=c['app'].state.maintenance
    with gate.capture(c['op']) as permit:
        expected=preview(c,snapshot_permit=permit)
        result=create(c,expected,snapshot_permit=permit)
        permit.require()
        assert gate.status()['state']=='capturing' and result['record']['revision']==1
    assert gate.status()['state']=='quiescent'


def test_source_loss_keeps_history_readable_without_revalidating_or_rebinding_it(service_case):
    c=service_case;saved=create(c)['record'];sql=logical(c['app'].state.database.path)
    with absent_source(c['root']):
        result=c['reviews'].history(c['project'].id,saved['id'])
        assert result['items']==[saved] and not result['source_checked'] and not result['can_upgrade']
    assert logical(c['app'].state.database.path)==sql


def test_independent_state_writer_cannot_enter_during_the_review_commit_boundary(service_case):
    c=service_case;expected=preview(c);observed=[]
    command='''import sys
from pathlib import Path
from core.engine.persistence import persistence_state_lock,PersistenceLockError
root=Path(sys.argv[1])
try:
 with persistence_state_lock((root,)):
  (root/'unexpected-writer.txt').write_text('must not happen')
except PersistenceLockError:
 print('REVIEW_STATE_LOCK_BLOCKED');sys.exit(17)
'''
    def compete(event,root):
        result=child(command,root);assert result.returncode==17,result.stderr
        assert b'REVIEW_STATE_LOCK_BLOCKED' in result.stdout;observed.append(True)
    c['reviews'].fault_injector=compete
    saved=create(c,expected)
    assert observed==[True] and saved['record']['revision']==1
    assert not (c['root']/'unexpected-writer.txt').exists()
