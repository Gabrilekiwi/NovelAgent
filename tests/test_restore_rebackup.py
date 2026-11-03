"""Rearchive an activated, subsequently edited/published synthetic restore."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy
from tests.test_restore_activation import full_source, prepared
from tests.test_restore_activation_chapter import publish_restored_next_chapter
from tests.test_restore_budget_preparation import financial
from tests.test_restore_executions import prepared as jobs_prepared
from tests.test_restore_handoff import restored
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.app import create_app
from workbench.backup_archive_history import verify_archived_persistence_history
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_files import inventory_tree
from workbench.backup_history import verify_persistence_history
from workbench.backup_restore_plan import plan_archived_restore
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_provenance import (HistoricalCoordinates, RestoreProvenanceError,
    validate_restore_provenance, validate_restored_database)


@pytest.fixture(scope='module')
def rebackup_case(full_source):
    scratch=full_source['root'].parent/'rebackup-scratch'; scratch.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        c=bound_copy.__wrapped__(full_source,scratch)
        c=restored.__wrapped__(c,patch)
        c=pending.__wrapped__(c); c=financial.__wrapped__(c)
        c=budget_prepared.__wrapped__(c); c=jobs_prepared.__wrapped__(c)
        c=prepared.__wrapped__(c)
        publish_restored_next_chapter(c)
        root=c['target']/'book'; calls=[]
        def forbidden(*args,**kwargs):
            calls.append(1); raise AssertionError('No executor or real Provider is authorized')
        archives=Path.cwd()/'.tmp'/('rb-'+uuid.uuid4().hex[:10]); archives.mkdir()
        app=create_app(database_path=c['target']/'workbench.sqlite',start_worker=False,auto_import_active=False,
            chapter_publication_machine_store=c['store'],
            reviewed_chapter_candidate_executor_factory=forbidden,
            reviewed_chapter_fact_extraction_executor_factory=forbidden)
        with TestClient(app):
            app.state.jobs.upsert_draft(project_id=c['project'].id,chapter=4,text='恢复后作者保留的第四章草稿')
            (root/'作者工作笔记.txt').write_text('恢复后的新笔记',encoding='utf-8')
            before=inventory_tree(root)
            gate=app.state.maintenance; operation=gate.begin()['operation_id']
            try:
                service=app.state.backups; service.archive_root=archives
                preview=service.preview(c['project'].id,operation_id=operation)
                assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
                result=service.capture(c['project'].id,operation_id=operation,
                    expected_preview_sha256=preview['preview_sha256'])
                assert inventory_tree(root)==before and not calls
            finally: gate.finish(operation)
        yield {**c,'rebackup':Path(result['path']),'preview':preview,'book_before':before,'calls':calls}
        assert inventory_tree(root)==before and not calls and list(scratch.iterdir())==[]


def test_rebackup_after_next_chapter_and_author_edits_verifies_with_all_live_sources_absent(rebackup_case,monkeypatch):
    c=rebackup_case; package=c['rebackup']; before=inventory_tree(package)
    def forbidden(*args,**kwargs): pytest.fail('offline rebackup verification opened live evidence')
    monkeypatch.setattr('workbench.restore_scope.load_restore_scope',forbidden)
    monkeypatch.setattr('workbench.book_ownership._record',forbidden)
    with ExitStack() as paths:
        paths.enter_context(absent_source(c['root'],temporary_name='original-offline'))
        paths.enter_context(absent_source(c['target'],temporary_name='restored-offline'))
        paths.enter_context(absent_source(c['store'],temporary_name='machine-offline'))
        first=verify_archived_target(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])
        second=verify_archived_target(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])
        assert first==second and first['offline_data_verified'] and not first['blockers']
        assert first['checks']['reviewed_state']['history']['value']['committed_through']==3
        assert first['checks']['usage']['model_and_budgets_verified']
        assert first['checks']['database']['restored_provenance']['original_job_count']==len(
            c['preview']['scope']['restored_provenance']['jobs_intent']['history'])
        assert not first['recoverable'] and not first['execution_policy']['write_activation_allowed']
        destination=Path.cwd()/'.tmp'/('rr-'+uuid.uuid4().hex[:10])
        plan=plan_archived_restore(package,project_id=c['project'].id,book_id=c['project'].book_id,
            destination=destination,temporary_root=c['scratch'])
        assert plan['plan_ready'] and plan['source_restore_provenance']==c['preview']['scope']['restored_provenance']
        assert not destination.exists()
        materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
        bound=bind_restored_roots(package,destination=destination,temporary_root=c['scratch'])
        assert bound['new_v2_physical_registries_bound'] and not bound['write_activation_allowed']
        assert (destination/'book/作者工作笔记.txt').read_text(encoding='utf-8')=='恢复后的新笔记'
    assert inventory_tree(package)==before


@pytest.mark.parametrize('missing',['transactions','journals','registration'])
def test_original_history_cannot_disappear_from_rebackup_inventory(rebackup_case,missing):
    c=rebackup_case; tree=copy.deepcopy(c['preview']['scope']['inventories']['story_project'])
    if missing=='registration':
        name=next(name for name in tree['files'] if '/registry/completed/' in name)
        del tree['files'][name]
    else:
        name=next(name for name in tree['files'] if '/'+missing+'/' in name and name.endswith('/manifest.json'))
        journal=str(Path(name).parent).replace('\\','/')
        tree['files']={name:record for name,record in tree['files'].items() if not name.startswith(journal+'/')}
    with pytest.raises(RestoreProvenanceError):
        HistoricalCoordinates(c['target']/'book',tree,c['project'].book_id,
            c['preview']['scope']['restored_provenance'])


def test_new_record_cannot_borrow_ancestral_coordinates(rebackup_case):
    c=rebackup_case; scope=c['preview']['scope']; tree=copy.deepcopy(scope['inventories']['story_project'])
    provenance=scope['restored_provenance']
    coordinates=HistoricalCoordinates(c['target']/'book',tree,c['project'].book_id,provenance)
    new='new-unbound-manifest.json'; tree['files'][new]={'size':2,'sha256':digest(b'{}')}
    with pytest.raises(RestoreProvenanceError):
        coordinates.path(c['root']/'note.txt',evidence=new)
    with pytest.raises(RestoreProvenanceError):
        coordinates.path(c['root']/'..'/'outside',evidence=new)
    with pytest.raises(ValueError):
        verify_persistence_history(c['target']/'book',scope['inventories']['story_project'],
            book_id=c['project'].book_id)  # Missing provenance never infers old roots.


@pytest.mark.parametrize('damaged',['scope','copy_receipt','activation_intent','job_intent','active_owner'])
def test_missing_or_changed_provenance_relationships_reject_rebackup(rebackup_case,damaged):
    c=rebackup_case; scope=c['preview']['scope']; value=copy.deepcopy(scope['restored_provenance'])
    if damaged=='scope':value['scope']['book_id']='another-book'
    elif damaged=='copy_receipt':value['materialization']['copied']['files'].pop(next(iter(value['materialization']['copied']['files'])))
    elif damaged=='activation_intent':value['activation_intent']['qualification']['project_id']='another-project'
    elif damaged=='job_intent':value['jobs_intent']['history'].clear()
    else:value['active_record']['state']='restoring'
    value['provenance_sha256']=digest({k:v for k,v in value.items() if k!='provenance_sha256'})
    with pytest.raises(RestoreProvenanceError):
        validate_restore_provenance(value,root=c['target']/'book',book_id=c['project'].book_id,
            ownership=scope['machine_ownership']['record'])


@pytest.mark.parametrize('damaged',['scope_row','original_job_row','execution_contract'])
def test_restore_tables_require_exact_scope_and_preserved_invalidated_jobs(rebackup_case,damaged):
    c=rebackup_case; connection=sqlite3.connect(':memory:'); connection.row_factory=sqlite3.Row
    source=sqlite3.connect((c['target']/'workbench.sqlite').as_uri()+'?mode=ro',uri=True)
    try:
        source.backup(connection)
        if damaged=='scope_row':connection.execute('DELETE FROM workbench_restore_scope')
        elif damaged=='original_job_row':connection.execute('DELETE FROM workbench_restored_job_history WHERE job_id=?',(c['job'].id,))
        else:connection.execute('UPDATE jobs SET execution_contract=? WHERE id=?',('workbench_execution_v1',c['job'].id))
        with pytest.raises(RestoreProvenanceError):
            validate_restored_database(connection,c['preview']['scope']['restored_provenance'])
    finally:
        source.close(); connection.close()
