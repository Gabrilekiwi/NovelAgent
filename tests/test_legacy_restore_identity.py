"""Legacy writer evidence and actual isolated restore, without public enrollment.

The seeded ownership is explicit synthetic test data. These tests cannot prove
normal enrollment, recoverable certification or a real Strict calibration.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest

from core.autonomy.common import atomic_append_json
from core.engine.persistence import atomic_create_json
from core.story_project.identity import LEGACY_AUTHORITY_PROJECTION
from tests.test_backup_archive import capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import logical
from tests.test_backup_restore_materialization import prepare
from tests.test_reviewed_upgrade import case as strict_case
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.book_ownership import BookOwnership, BookOwnershipError, OWNERSHIP_V2, WITNESS_PATH, _new_witness
from workbench.database import WorkbenchDatabase
from workbench.restore_activation import RestoreActivation
from workbench.restore_handoff import RestoreHandoffError, _signed
from workbench.restore_scope import load_restore_scope
from workbench.writer_identity import load_verified_writer_identity, WriterIdentityError


REPO=Path(__file__).resolve().parents[1]


@pytest.fixture
def legacy(tmp_path,monkeypatch):
    machine=tmp_path/'machine'
    monkeypatch.setattr('workbench.book_ownership.default_machine_store',lambda:machine)
    def forbidden(*args,**kwargs):
        pytest.fail('legacy identity/restore called a real Provider, worker or importer')
    import anthropic,openai
    from workbench.jobs import JobSupervisor
    actual_start_worker = JobSupervisor._start_worker
    monkeypatch.setattr(anthropic,'Anthropic',forbidden)
    monkeypatch.setattr(openai,'OpenAI',forbidden)
    monkeypatch.setattr('workbench.jobs.JobSupervisor._start_worker',forbidden)
    monkeypatch.setattr('workbench.app._import_active_project',forbidden)
    c=strict_case.__wrapped__()
    return {**c,'machine':machine,'forbidden':forbidden,'scratch':tmp_path/'scratch',
            'actual_start_worker':actual_start_worker}


@contextmanager
def actual_copy(c,*,version='1.0',seeded=True,before_capture=None):
    root=c['root']
    if version=='2.0':
        path=root/'.novelagent/project.json'
        value=json.loads(path.read_bytes())
        value.update(schema_version='2.0',root_hint='.',authority=LEGACY_AUTHORITY_PROJECTION)
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    app=create_app(database_path=root.parent/'workbench.db',start_worker=False,auto_import_active=False,
        allowed_workspaces=[root.parent],chapter_publication_machine_store=c['machine'])
    with TestClient(app) as client:
        project=app.state.projects.import_project(root)
        draft=app.state.jobs.upsert_draft(project_id=project.id,chapter=2,text='旧书作者保留的第二章草稿')
        owner=BookOwnership(project.book_id,machine_store=c['machine'])
        if seeded:
            with owner.locked(), app.state.database.transaction() as connection:
                binding=owner._binding(root=root,database=app.state.database,project_id=project.id,connection=connection)
                witness=_new_witness(project.book_id,uuid.uuid4().hex,binding)
                record=_signed({'version':OWNERSHIP_V2,'generation':1,'state':'active','binding':binding,
                    'registration_id':witness['registration_id'],'witness_sha256':witness['witness_sha256']})
                atomic_create_json(root/WITNESS_PATH,witness)
                atomic_create_json(owner.record_path,record)
        app.state.backups.machine_store=c['machine']
        op=app.state.maintenance.begin()['operation_id']
        if before_capture is not None:
            # Normal preview arms the known source locks before the user saves
            # an archive. Inject proof loss after that actual successful preview.
            app.state.backups.preview(project.id,operation_id=op)
            before_capture({'root':root,'owner':owner,'app':app,'project':project})
        package=Path(capture({'service':app.state.backups,'op':op,'project':project})['path'])
        app.state.maintenance.finish(op)
        target=REPO/'.tmp'/('lr-'+uuid.uuid4().hex[:8])
        plan=prepare({'project':project},package,target,c['scratch'])
        materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
        bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
        prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])
        yield {**c,'app':app,'client':client,'project':project,'draft':draft,'owner':owner,
            'package':package,'target':target,'plan':plan,'database':app.state.database,
            'service':RestoreActivation(project.book_id,machine_store=c['machine'])}


def deny(root):
    path=root/'restore-denied.json'
    with pytest.raises(BookOwnershipError):
        atomic_append_json(path,{'not_authorized':True})
    assert not path.exists()


@pytest.mark.parametrize('version',['1.0','2.0'])
def test_actual_legacy_handoff_budget_activation_and_restart_preserve_identity_and_draft(legacy,version):
    with actual_copy(legacy,version=version) as c:
        source_book=inventory_tree(c['root']); source_sql=logical(c['database'].path)
        book=inventory_tree(c['target']/'book'); old=load_verified_writer_identity(c['root'])
        assert load_verified_writer_identity(c['target']/'book')==old
        service=c['service']
        preview=service.preview(c['package'],destination=c['target'],temporary_root=c['scratch'])
        handoff=preview['handoff_id']
        result=service.begin(c['package'],destination=c['target'],expected_preview_sha256=preview['intent_sha256'],temporary_root=c['scratch'])
        assert result['source_revoked'] and not result['target_writable']
        deny(c['root']); deny(c['target']/'book')
        usage=service.capture(handoff_id=handoff)
        assert usage['settled'],usage
        for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
            method(c['package'],handoff_id=handoff,temporary_root=c['scratch'])
        assert service.activate(c['package'],handoff_id=handoff,temporary_root=c['scratch'])['target_writable']
        scope=load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['machine'])
        assert scope.active and not scope.read_only
        assert load_verified_writer_identity(c['target']/'book')==old
        assert inventory_tree(c['target']/'book')==book
        assert inventory_tree(c['root'])==source_book and logical(c['database'].path)==source_sql
        for _ in range(2):
            app=create_app(database_path=c['target']/'workbench.sqlite',start_worker=True,auto_import_active=True,
                chapter_publication_machine_store=c['machine'],chapter_candidate_executor_factory=c['forbidden'],
                chapter_fact_extraction_executor_factory=c['forbidden'],reviewed_chapter_candidate_executor_factory=c['forbidden'],
                reviewed_chapter_fact_extraction_executor_factory=c['forbidden'])
            with TestClient(app) as client:
                assert client.get('/api/restore-scope').json()['state']=='active'
                assert app.state.supervisor._thread is None
                draft=client.get('/api/drafts/'+c['draft'].id)
                assert draft.status_code==200,draft.text
                assert draft.json()['draft']['text']==c['draft'].text
                chapter_path='/api/projects/'+c['project'].id+'/chapters/'
                formal=client.get(chapter_path+'1')
                assert formal.status_code==200,formal.text
                assert formal.json()['has_prose'] and 'retained_author_draft' not in formal.json()
                second=client.get(chapter_path+'2')
                assert second.status_code==200,second.text
                retained=second.json()['retained_author_draft']
                assert retained=={name:getattr(c['draft'],name) for name in
                    ('id','project_id','chapter','revision','sha256','text','state')}
                assert not second.json()['has_prose']
                assert client.get(chapter_path+'2').json()==second.json()
                # A damaged author draft must be reported, without changing
                # the readable formal chapter or silently repairing the DB.
                before_sql=logical(app.state.database.path)
                try:
                    with app.state.database.transaction() as connection:
                        connection.execute('UPDATE drafts SET sha256=? WHERE id=?',('0'*64,c['draft'].id))
                    damaged_sql=logical(app.state.database.path)
                    failed=client.get(chapter_path+'2')
                    assert failed.status_code in {400,409},failed.text
                    assert '作者草稿' in failed.json()['error']['message']
                    assert client.get(chapter_path+'1').json()==formal.json()
                    assert logical(app.state.database.path)==damaged_sql
                finally:
                    with app.state.database.transaction() as connection:
                        connection.execute('UPDATE drafts SET sha256=? WHERE id=?',(c['draft'].sha256,c['draft'].id))
                assert logical(app.state.database.path)==before_sql
                assert client.get(chapter_path+'2').json()==second.json()
        deny(c['root'])


def test_actual_legacy_rollback_reactivates_only_original_without_identity_upgrade(legacy):
    with actual_copy(legacy) as c:
        service=c['service']; before=inventory_tree(c['root']); target=inventory_tree(c['target']/'book')
        preview=service.preview(c['package'],destination=c['target'],temporary_root=c['scratch'])
        service.begin(c['package'],destination=c['target'],expected_preview_sha256=preview['intent_sha256'],temporary_root=c['scratch'])
        first=service.rollback(handoff_id=preview['handoff_id'])
        assert first['rolled_back'] and service.rollback(handoff_id=preview['handoff_id'])==first
        with c['owner'].locked(),c['database'].transaction() as connection:
            assert c['owner'].require(root=c['root'],database=c['database'],project_id=c['project'].id,connection=connection)==first['source_record']
        assert inventory_tree(c['root'])==before and inventory_tree(c['target']/'book')==target
        assert not load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['machine']).active
        deny(c['target']/'book')


def test_unenrolled_legacy_archive_still_cannot_create_registration_or_handoff(legacy):
    with actual_copy(legacy,seeded=False) as c:
        before=inventory_tree(c['root']); target=inventory_tree(c['target']/'book')
        with pytest.raises(RestoreHandoffError,match='有效登记'):
            c['service'].preview(c['package'],destination=c['target'],temporary_root=c['scratch'])
        assert not c['owner'].record_path.exists() and not (c['root']/WITNESS_PATH).exists()
        assert not (c['target']/'book'/WITNESS_PATH).exists()
        assert inventory_tree(c['root'])==before and inventory_tree(c['target']/'book')==target


@pytest.mark.parametrize('damage',['profile','encoding','prose_alias','event_receipt','shadow','chapter_gap'])
def test_live_legacy_evidence_damage_is_refused_without_repair_or_registration(legacy,damage):
    root=legacy['root']
    if damage=='profile':
        path=root/'追踪/上下文.md'
        path.write_text(path.read_text(encoding='utf-8')+'\nNovelAgent:story_project_writeback\n',encoding='utf-8')
    elif damage=='encoding':
        (root/'追踪/上下文.md').write_bytes(b'\xff')
    elif damage=='prose_alias':
        (root/'正文/第001章_重复.md').write_text('另一个版本',encoding='utf-8')
    elif damage=='event_receipt':
        path=root/'.novelagent/authority/receipts/incomplete.json'
        path.parent.mkdir(parents=True);path.write_text('{}',encoding='utf-8')
    elif damage=='shadow':
        path=root/'.novelagent/project.json';value=json.loads(path.read_bytes())
        value['story_state_mode']='shadow';path.write_text(json.dumps(value),encoding='utf-8')
    else:
        (root/'正文/第003章.md').write_text('缺少第二章',encoding='utf-8')
    before=inventory_tree(root)
    with pytest.raises((ValueError,RuntimeError)):
        load_verified_writer_identity(root)
    assert inventory_tree(root)==before and not (root/WITNESS_PATH).exists()


def test_standard_reviewed_enrollment_does_not_enroll_legacy_by_compatibility(legacy):
    root=legacy['root'];database=WorkbenchDatabase(root.parent/'enrollment.db')
    from workbench.projects import ProjectRegistry
    project=ProjectRegistry(database,allowed_workspaces=[root.parent]).import_project(root)
    owner=BookOwnership(project.book_id,machine_store=legacy['machine'])
    before=inventory_tree(root)
    with owner.locked(),database.transaction() as connection,pytest.raises(ValueError):
        owner.enroll(root=root,database=database,project_id=project.id,connection=connection)
    assert not owner.record_path.exists() and not (root/WITNESS_PATH).exists()
    assert inventory_tree(root)==before


@pytest.mark.parametrize('damage',['missing_machine','missing_witness'])
def test_registered_legacy_missing_proof_cannot_be_archived_as_never_enrolled(legacy,damage):
    seen={}
    def corrupt(c):
        path=c['owner'].record_path if damage=='missing_machine' else c['root']/WITNESS_PATH
        assert path.resolve().is_relative_to(REPO/'.tmp')
        path.unlink()
        seen['book']=inventory_tree(c['root']);seen['c']=c
    with pytest.raises(BookOwnershipError):
        with actual_copy(legacy,before_capture=corrupt):
            pytest.fail('missing legacy registration proof was accepted')
    assert inventory_tree(legacy['root'])==seen['book']
    assert not seen['c']['app'].state.backups.archive_root.exists()


def test_source_change_during_legacy_cold_read_cannot_return_old_success(legacy,monkeypatch):
    from workbench import backup_archive_legacy_state
    original=backup_archive_legacy_state.inspect_legacy_state
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        path=legacy['root']/'追踪/上下文.md'
        path.write_text(path.read_text(encoding='utf-8')+'\n作者新增笔记\n',encoding='utf-8')
        return result
    monkeypatch.setattr(backup_archive_legacy_state,'inspect_legacy_state',changed)
    with pytest.raises(WriterIdentityError,match='来源变化'):
        load_verified_writer_identity(legacy['root'])
    assert not (legacy['root']/WITNESS_PATH).exists()


def test_unfinished_legacy_outline_keeps_readonly_copy_and_author_draft(legacy):
    outline=legacy['root']/'大纲/细纲_第002章.md'
    assert outline.resolve().is_relative_to(REPO/'.tmp')
    outline.unlink()
    with actual_copy(legacy,seeded=False) as c:
        assert 'legacy_next_outline_required' in {issue['code'] for issue in c['plan']['continuation_blockers']}
        assert load_verified_writer_identity(c['target']/'book')['identity']['schema_version']=='1.0'
        app=create_app(database_path=c['target']/'workbench.sqlite',start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'],chapter_candidate_executor_factory=c['forbidden'])
        with TestClient(app) as client:
            assert client.get('/api/restore-scope').json()['read_only']
            response=client.get('/api/drafts/'+c['draft'].id)
            assert response.status_code==200,response.text
            assert response.json()['draft']['text']==c['draft'].text
            assert app.state.supervisor._thread is None
