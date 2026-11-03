"""Real maintenance/source/archive boundaries for normal onboarding previews."""
from pathlib import Path
import asyncio
import threading

import httpx
import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_backup_api import configured, begin, preview, capture_body
from tests.test_backup_archive import software_tree, capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_executions import execution_archive
from tests.test_workbench_app import _unlock, _create_project
from tests.test_restore_scope import prepared_scope, open_app, logical
from workbench.backup_database import _logical_digest
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership, WITNESS_PATH
from workbench.maintenance import MaintenanceBlocked


def inspect(client, project, op, archive_id=''):
    return client.post(f"/api/projects/{project['id']}/onboarding/preview",
        json={'operation_id': op, 'archive_id': archive_id})


def saved(client, project, op):
    result = client.post(f"/api/projects/{project['id']}/backups", json=capture_body(preview(client, project, op)))
    assert result.status_code == 200, result.text
    return result.json()


def data(client, project):
    with client.app.state.database.connection() as connection:
        logical = _logical_digest(connection)
    return inventory_tree(project['root']), logical


def test_preview_reports_missing_conditions_without_granting_or_mutating(tmp_path, software_tree, monkeypatch):
    client, workspace = configured(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('onboarding preview entered enrollment or generation')
    monkeypatch.setattr(BookOwnership, 'enroll', forbidden)
    with client:
        _unlock(client); project = _create_project(client, workspace); op = begin(client)
        preview(client, project, op)  # Arm the existing snapshot locks before comparing.
        before = data(client, project)
        result = inspect(client, project, op)
        assert result.status_code == 200, result.text
        value = result.json()
        assert {'supported_identity', 'matching_backup', 'recoverable_backup', 'onboarding_available',
            'author_baseline_review'} <= set(value['blockers'])
        assert 'settled_source' not in value['blockers']
        assert value['archive'] is None
        assert not any(value[k] for k in ('can_upgrade', 'can_enroll', 'publication_authorized'))
        assert value['preview_sha256'] == digest({k:v for k,v in value.items() if k != 'preview_sha256'})
        assert data(client, project) == before
        assert not Path(project['root'], WITNESS_PATH).exists()
        assert not client.app.state.backups.archive_root.exists()
        assert client.get('/api/maintenance').json()['state'] == 'quiescent'


def test_completed_matching_diagnostic_gets_offline_checks_but_no_upgrade(tmp_path, software_tree):
    client, workspace = configured(tmp_path)
    with client:
        _unlock(client); project = _create_project(client, workspace); op = begin(client)
        package = saved(client, project, op); before = data(client, project)
        first = inspect(client, project, op, package['archive_id'])
        assert first.status_code == 200, first.text
        value = first.json(); archived = value['archive']
        assert archived['source_matches'] and archived['differences'] == []
        assert archived['receipt_sha256'] == package['receipt']['receipt_sha256']
        # A Shadow fixture cannot become Strict merely because its ZIP is intact.
        assert archived['offline_data_verified'] is False
        assert 'unsupported_identity_execution_contract' in {x['code'] for x in archived['offline_blockers']}
        assert not value['can_upgrade'] and not archived['recoverable']
        assert inspect(client, project, op, package['archive_id']).json() == value
        assert data(client, project) == before


def test_actual_strict_candidate_and_fact_archive_passes_data_checks_without_activation(execution_archive):
    c = execution_archive; package = capture(c)
    service = c['app'].state.onboarding
    before = inventory_tree(c['root'])
    result = service.preview(c['project'].id, operation_id=c['op'], archive_id=package['archive_id'])
    assert result['archive']['source_matches']
    assert result['archive']['offline_data_verified'], result['archive']['offline_blockers']
    assert 'supported_identity' not in result['blockers']
    assert not result['can_upgrade'] and not result['can_enroll']
    assert inventory_tree(c['root']) == before


def test_unchanged_backup_can_be_rechecked_in_later_maintenance(tmp_path, software_tree):
    client, workspace = configured(tmp_path)
    with client:
        _unlock(client); project = _create_project(client, workspace); op = begin(client)
        package = saved(client, project, op)
        first = inspect(client, project, op, package['archive_id']).json()
        gate = client.app.state.maintenance
        gate.finish(op); new_op = begin(client)
        again = inspect(client, project, new_op, package['archive_id'])
        assert again.status_code == 200, again.text
        assert again.json()['archive'] == first['archive']
        assert again.json()['operation_id'] != first['operation_id']


def test_source_mutation_during_offline_checks_invalidates_the_entire_response(tmp_path, software_tree, monkeypatch):
    from workbench import onboarding
    client, workspace = configured(tmp_path)
    with client:
        _unlock(client); project = _create_project(client, workspace); op = begin(client)
        package = saved(client, project, op)
        original = onboarding.verify_archived_target
        def changed(*args, **kwargs):
            evidence = original(*args, **kwargs)
            Path(project['root'], 'concurrent-note.txt').write_text('independent editor')
            return evidence
        monkeypatch.setattr(onboarding, 'verify_archived_target', changed)
        result = inspect(client, project, op, package['archive_id'])
        assert result.status_code == 409, result.text
        assert 'archive' not in result.json()
        assert client.app.state.maintenance.status()['state'] == 'quiescent'
        assert not Path(project['root'], WITNESS_PATH).exists()


@pytest.mark.parametrize('change', ['book', 'database', 'other_project', 'config', 'software', 'external_job'])
def test_stale_backup_is_reported_and_not_reinterpreted_as_current(tmp_path, software_tree, change):
    client, workspace = configured(tmp_path)
    with client:
        _unlock(client); project = _create_project(client, workspace)
        other = _create_project(client, workspace, name='other')
        jobs = client.app.state.jobs
        job = jobs.create_job(project_id=project['id'], kind='generation_session', start_chapter=1, end_chapter=1)
        external = client.app.state.backups.task_runtime_root / job.id
        external.mkdir(parents=True); (external/'retained.txt').write_text('old')
        op = begin(client); package = saved(client, project, op)
        if change == 'book': Path(project['root'], 'note.txt').write_text('new')
        elif change == 'database': jobs.upsert_draft(project_id=project['id'], chapter=1, text='author edit')
        elif change == 'other_project': jobs.upsert_draft(project_id=other['id'], chapter=1, text='other author edit')
        elif change == 'config':
            service = client.app.state.backups
            service.runtime_config = {**service.runtime_config, 'worker_enabled': True}
        elif change == 'software': (software_tree/'run_workbench.py').write_text('# changed build')
        else: (external/'retained.txt').write_text('changed external evidence')
        before = data(client, project)
        result = inspect(client, project, op, package['archive_id'])
        if change == 'software':
            # A running backup service is pinned to its startup source build;
            # it must refuse before presenting a new-build onboarding preview.
            assert result.status_code == 409, result.text
            assert data(client, project) == before
            return
        assert result.status_code == 200, result.text
        value = result.json()
        assert 'matching_backup' in value['blockers'] and value['archive']['differences']
        assert value['archive']['offline_data_verified'] is None and not value['can_upgrade']
        assert data(client, project) == before


@pytest.mark.parametrize('damage', ['other_book', 'corrupt_zip', 'missing_receipt'])
def test_wrong_or_damaged_archive_refused_without_source_changes(tmp_path, software_tree, damage):
    client, workspace = configured(tmp_path)
    with client:
        _unlock(client); project = _create_project(client, workspace)
        other = _create_project(client, workspace, name='other'); op = begin(client)
        package = saved(client, other if damage == 'other_book' else project, op)
        preview(client, project, op)
        if damage == 'corrupt_zip': Path(package['path'], 'archive.zip').write_bytes(b'invalid')
        elif damage == 'missing_receipt': Path(package['path'], 'completion.json').unlink()
        before = data(client, project)
        result = inspect(client, project, op, package['archive_id'])
        assert result.status_code == 409, result.text
        assert data(client, project) == before


def test_unsettled_job_remains_untouched_and_visible(tmp_path, software_tree):
    client, workspace = configured(tmp_path)
    with client:
        _unlock(client); project = _create_project(client, workspace)
        job = client.app.state.jobs.create_job(project_id=project['id'], kind='generation_session', start_chapter=1, end_chapter=1)
        with client.app.state.database.transaction() as connection:
            connection.execute("UPDATE jobs SET state='needs_recovery' WHERE id=?", (job.id,))
        op = begin(client); preview(client, project, op); before = data(client, project)
        result = inspect(client, project, op)
        assert result.status_code == 200, result.text
        assert {'code': 'job_not_settled', 'reference': job.id} in result.json()['source_blockers']
        assert 'settled_source' in result.json()['blockers']
        assert data(client, project) == before


def test_preview_retains_security_exact_payload_and_live_maintenance_boundaries(tmp_path):
    client, workspace = configured(tmp_path)
    with client:
        url = '/api/projects/absent/onboarding/preview'
        assert client.post(url, json={}).status_code == 403
        _unlock(client)
        assert client.post(url, json={}, headers={'origin':'https://external.invalid'}).status_code == 403
        assert client.post(url, json={}, headers={'host':'untrusted.invalid'}).status_code == 400
        project = _create_project(client, workspace)
        assert inspect(client, project, 'a'*32).status_code == 409
        gate = client.app.state.maintenance
        with gate.admit('local_writer'):
            op = begin(client)
            assert inspect(client, project, op).status_code == 423
        assert inspect(client, project, 'b'*32).status_code == 409
        assert inspect(client, project, op, '../escape').status_code == 409
        url = f"/api/projects/{project['id']}/onboarding/preview"
        assert client.post(url, json={'operation_id':op, 'archive_id':'', 'can_upgrade':True}).status_code == 409
        assert not client.app.state.backups.archive_root.exists()


def test_restored_read_only_app_rejects_onboarding_before_entering_source(prepared_scope, monkeypatch):
    from fastapi.testclient import TestClient
    c = prepared_scope; before = logical(c['database']); book = inventory_tree(c['root'])
    app = open_app(c)
    def forbidden(*args, **kwargs):
        pytest.fail('read-only restored app entered onboarding capture')
    monkeypatch.setattr(app.state.onboarding, 'preview', forbidden)
    with TestClient(app) as client:
        _unlock(client)
        response = inspect(client, {'id':c['target'].id}, 'a'*32)
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'restore_scope_read_only'
    assert logical(c['database']) == before and inventory_tree(c['root']) == book


def test_cancelled_preview_retains_capture_until_real_verification_finishes(tmp_path, software_tree, monkeypatch):
    from workbench import onboarding
    client, workspace = configured(tmp_path)
    app = client.app; gate = app.state.maintenance
    _unlock(client); project = _create_project(client, workspace); op = begin(client)
    package = saved(client, project, op)
    original = onboarding.verify_archived_target
    started, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        started.set()
        if not release.wait(30): raise AssertionError('verification was not released')
        return original(*args, **kwargs)
    monkeypatch.setattr(onboarding, 'verify_archived_target', delayed)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                headers={'x-novelagent-token':app.state.session_token}) as request:
            pending = asyncio.create_task(request.post(f"/api/projects/{project['id']}/onboarding/preview",
                json={'operation_id':op, 'archive_id':package['archive_id']}))
            try:
                assert await asyncio.to_thread(started.wait, 10)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError): await pending
                assert gate.status()['state'] == 'capturing'
                with pytest.raises(MaintenanceBlocked): gate.finish(op)
                assert inspect(client, project, op).status_code == 423
            finally:
                release.set()
                await asyncio.wait_for(gate.drain_async(), 30)
            assert gate.status()['state'] == 'quiescent'
    try: asyncio.run(exercise())
    finally: release.set(); app.state.instance_lease.close()
