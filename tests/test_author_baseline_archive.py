"""Actual approval-before-backup order and independent DB history verification."""
from contextlib import closing
from pathlib import Path
import copy
import json
import sqlite3

import pytest

from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case,contents
from tests.test_registration_upgrade_archive import old_package,tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive import capture
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_materialization import prepare
from tests.test_backup_restore_database import logical
from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.story_project.reviewed_state_document import digest
from workbench.author_baselines import AuthorBaselineStore,CATEGORIES,build_baseline_draft
from workbench.backup_archive_database import inspect_archived_database,ArchivedDatabaseError
from workbench.backup_certification import RecoveryCertification,verify_recovery_certificate,CERTIFICATE
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.database import WorkbenchDatabase


def seed(c):
    store=AuthorBaselineStore(c['app'].state.database);context=ChapterCandidateContextBuilder(c['root']).build(2)
    draft=build_baseline_draft(project_id=c['project'].id,trusted_context=context,state=c['state'],calibration_report=c['report'])
    saved=store.create(draft,trusted_context=context)
    confirmed=store.revise(saved['id'],project_id=c['project'].id,expected_revision=saved['revision'],
        expected_sha256=saved['record_sha256'],trusted_context=context,confirmed_categories=CATEGORIES,author_confirmed=True)
    return store,confirmed


def test_author_confirmed_history_requires_new_backup_and_survives_actual_restore_read_only(registration_case,tmp_path):
    c=registration_case;old=old_package(c);original_book=contents(c['root'])
    store,confirmed=seed(c);history=store.history(confirmed['id'],project_id=c['project'].id)
    certifier=RecoveryCertification(c['app'].state.backups)
    stale=certifier.preview(c['project'].id,archive_id=old.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert not stale['can_certify'] and 'source_changed_since_archive' in {b['code'] for b in stale['blockers']}
    assert not (old/CERTIFICATE).exists() and contents(c['root'])==original_book
    package=Path(capture({'service':c['app'].state.backups,'project':c['project'],'op':c['op']})['path'])
    sql=logical(c['app'].state.database.path)
    preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=c['op'],temporary_root=c['scratch'])
    assert preview['can_certify'],preview['blockers']
    result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=c['op'],
        expected_assessment_sha256=preview['assessment_sha256'],temporary_root=c['scratch'])
    with absent_source(c['root']):
        db=inspect_archived_database(package,project_id=c['project'].id,book_id=c['project'].book_id,temporary_root=c['scratch'])
        assert db['target_tables']['author_baseline_revisions']['count']==2
        assert 'author_baseline_revisions' not in db['unclassified_tables']
        assert verify_recovery_certificate(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])==result
    destination=tmp_path/'审核记录 恢复副本'
    plan=prepare({'project':c['project']},package,destination,c['scratch'])
    materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
    bind_restored_roots(package,destination=destination,temporary_root=c['scratch'])
    prepare_restored_workbench(package,destination=destination,temporary_root=c['scratch'])
    restored=WorkbenchDatabase(destination/'workbench.sqlite',machine_store=c['machine'])
    rows=AuthorBaselineStore(restored).history(confirmed['id'],project_id=c['project'].id)
    assert rows==history and restored.restore_scope.read_only
    assert rows[-1]['binding']['source_root']==str(c['root'])
    assert rows[-1]['author_confirmed'] and rows[-1]['status']=='confirmed'
    assert contents(c['root'])==original_book and logical(c['app'].state.database.path)==sql


@pytest.mark.parametrize('damage',['hash','gap','confirmed_content','registration_scope','row_scope'])
def test_actual_archived_review_history_damage_is_rejected_without_opening_original_sources(registration_case,damage):
    c=registration_case;old_package(c);store,confirmed=seed(c)
    other=c['app'].state.projects.create_project(c['root'].parent/'other-book',name='other') if damage=='row_scope' else None
    with c['app'].state.database.transaction() as connection:
        if damage=='gap':connection.execute('DELETE FROM author_baseline_revisions WHERE id=? AND revision=1',(confirmed['id'],))
        else:
            value=copy.deepcopy(confirmed)
            if damage=='hash':value['record_sha256']='f'*64
            elif damage=='confirmed_content':next(iter(value['state']['characters'].values()))['current_goal']='unreviewed data'
            else:value['project_id']=other.id if other is not None else 'different-project'
            if damage!='hash':value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
            connection.execute('UPDATE author_baseline_revisions SET payload_json=?,record_sha256=? WHERE id=? AND revision=2',
                (json.dumps(value),value['record_sha256'],confirmed['id']))
            if other is not None:
                connection.execute('UPDATE author_baseline_revisions SET project_id=? WHERE id=? AND revision=2',
                    (other.id,confirmed['id']))
    package=Path(capture({'service':c['app'].state.backups,'project':c['project'],'op':c['op']})['path'])
    original=contents(package);sql=logical(c['app'].state.database.path)
    with absent_source(c['root']),pytest.raises(ArchivedDatabaseError):
        inspect_archived_database(package,project_id=c['project'].id,book_id=c['project'].book_id,temporary_root=c['scratch'])
    assert contents(package)==original and logical(c['app'].state.database.path)==sql
    assert not (package/CERTIFICATE).exists()
