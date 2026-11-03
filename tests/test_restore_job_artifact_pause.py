"""An actual imported first-boundary pause keeps its proved root absence."""
from contextlib import closing, ExitStack
import json
import copy
from pathlib import Path
import sqlite3
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy, logical
from tests.test_restore_activation import prepared, activate
from tests.test_restore_budget_preparation import make_financial_source, financial
from tests.test_restore_executions import prepared as jobs_prepared
from tests.test_restore_handoff import restored
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.app import create_app
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_files import inventory_tree
from workbench.restore_job_artifacts import RestoredJobArtifacts
from workbench.backup_restore_provenance import RestoreProvenanceError


@pytest.fixture(scope='module')
def full_source():
    def pause(c):
        with closing(sqlite3.connect(c['db'].path)) as connection:
            connection.row_factory=sqlite3.Row
            job=connection.execute('SELECT * FROM jobs WHERE project_id=?',(c['project'].id,)).fetchone()
            payload={'reason':'workbench_maintenance','completed_chapters':0,'chapter':job['next_chapter'],
                'automatic_resume':False,'operation_id':'a'*32}
            connection.execute('UPDATE jobs SET state=?,started_at=?,revision=revision+1 WHERE id=?',
                ('awaiting_input',job['created_at'],job['id']))
            connection.execute('INSERT INTO job_events(job_id,sequence,event_type,payload_json,created_at) VALUES (?,?,?,?,?)',
                (job['id'],2,'job.paused',json.dumps(payload),job['updated_at']))
            connection.commit()
    return make_financial_source(include_uncertain=False,before_archive=pause)


def test_actual_imported_first_pause_rearchives_offline_without_fabricating_a_job_root(prepared):
    c=prepared;activate(c);target=c['target'];before=inventory_tree(target/'book');sql=logical(target/'workbench.sqlite')
    calls=[]
    def forbidden(*args,**kwargs):calls.append(1);pytest.fail('No executor or Provider authorized')
    archive_root=Path.cwd()/'.tmp'/('jah-pause-'+uuid.uuid4().hex[:8]);archive_root.mkdir()
    app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    with absent_source(c['root']),TestClient(app):
        assert app.state.supervisor._thread is None
        gate=app.state.maintenance;operation=gate.begin()['operation_id']
        try:
            service=app.state.backups;service.archive_root=archive_root
            preview=service.preview(c['project'].id,operation_id=operation)
            assert preview['scope']['scope_checks_passed'],preview['scope']['blockers']
            assert 'jobs/'+c['job'].id not in preview['scope']['inventories']
            archive=service.capture(c['project'].id,operation_id=operation,expected_preview_sha256=preview['preview_sha256'])
        finally:gate.finish(operation)
    with ExitStack() as absent,pytest.MonkeyPatch.context() as offline:
        for root in (c['root'],c['runtime'],target,c['store']):
            absent.enter_context(absent_source(root,temporary_name=root.name+'-pause-offline-'+uuid.uuid4().hex[:8]))
        offline.setattr('workbench.restore_scope.load_restore_scope',forbidden)
        offline.setattr('workbench.book_ownership._record',forbidden)
        verified=verify_archived_target(archive['path'],project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=c['scratch'])
        assert verified['offline_data_verified'] and not verified['blockers']
        saved=verified['checks']['jobs']['jobs'][0]
        assert saved['first_boundary_pause'] and saved['artifact'] is None and saved['state']=='cancelled'
    assert not (target/'next-jobs'/c['job'].id).exists()
    assert inventory_tree(target/'book')==before and logical(target/'workbench.sqlite')==sql and not calls
    # A checksum-consistent inventory is not permission to add a new artifact
    # root to an imported job whose original copy had none.
    counterfeit=target/'next-jobs'/c['job'].id;counterfeit.mkdir(parents=True)
    (counterfeit/'stdout.json').write_text('{}')
    inventories=copy.deepcopy(preview['scope']['inventories'])
    inventories['jobs/'+c['job'].id]=inventory_tree(counterfeit)
    resolver=RestoredJobArtifacts(target/'book',c['project'].book_id,preview['scope']['restored_provenance'])
    with pytest.raises(RestoreProvenanceError):resolver.validate_trees(inventories)
