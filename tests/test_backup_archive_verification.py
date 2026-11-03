"""Whole-package composition must not turn partial evidence into a restore permit."""
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.test_backup_archive import capture, software_tree
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory, replace_database
from tests.test_backup_archive_executions import execution_archive, forbid_live
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed, reviewed_archive
from workbench import backup_archive_verification as verification
from workbench.backup_files import inventory_tree
from workbench.jobs import JobStore
from workbench.projects import ProjectRegistry


def inspect(c, package, scratch):
    return verification.verify_archived_target(package, project_id=c['project'].id,
        book_id=c['project'].book_id, temporary_root=scratch)


def test_actual_mixed_reviewed_book_and_other_project_policy_without_sources(
        reviewed_archive, private_temporary_directory, monkeypatch):
    c = reviewed_archive
    other = ProjectRegistry(c['db'], allowed_workspaces=[c['root'].parent]).create_project(
        c['root'].parent / 'other', name='other')
    jobs = JobStore(c['db'])
    other_job = jobs.create_job(project_id=other.id, kind='generation_session', start_chapter=1, end_chapter=1)
    package = Path(capture(c)['path'])
    before = inventory_tree(package)
    source_before = inventory_tree(c['root'])
    def forbidden(*args, **kwargs):
        pytest.fail('archive composition entered a live writer or scheduler')
    monkeypatch.setattr(JobStore, 'claim_next', forbidden)
    from workbench.book_ownership import BookOwnership
    monkeypatch.setattr(BookOwnership, '__init__', forbidden)
    with ExitStack() as stack:
        stack.enter_context(absent_source(c['root']))
        stack.enter_context(absent_source(other.root, temporary_name='other-offline'))
        first = inspect(c, package, private_temporary_directory)
        second = inspect(c, package, private_temporary_directory)
    assert first == second
    assert first['offline_data_verified'], first['blockers']
    assert len(first['checks']) == 9
    assert first['database_policy']['disable_other_project_ids'] == [other.id]
    assert first['database_policy']['disable_other_job_ids'] == [other_job.id]
    assert not first['database_policy']['enforced_in_restored_app']
    assert not first['restore_plan_ready'] and not first['recoverable'] and not first['write_activation_allowed']
    assert first['archive_binding']['archive_purpose'] == 'diagnostic'
    assert first['verifier']['implementation_sha256'] != first['archive_binding']['source_software_sha256']
    assert not first['verifier']['loaded_modules_match_proven']
    assert not first['execution_policy']['provider_calls_allowed']
    assert inventory_tree(package) == before and inventory_tree(c['root']) == source_before


def test_author_edit_is_preserved_as_a_continuation_gate(reviewed_archive, private_temporary_directory):
    c = reviewed_archive
    (c['root'] / '设定/离线来源.md').write_text('# 作者修订\n应保留并等待重新审核。', encoding='utf-8')
    package = Path(capture(c)['path'])
    with absent_source(c['root']):
        result = inspect(c, package, private_temporary_directory)
    assert result['offline_data_verified'], result['blockers']
    assert result['continuation_blockers'] == [{'check':'sources',
        'code':'reviewed_manual_source_changed', 'reference':'追踪/已审核事实.md'}]
    assert result['checks']['sources']['author_review_required']
    assert not result['execution_policy']['replay_prepared_plans']


@pytest.mark.parametrize('damage', ['unknown_table', 'running_job'])
def test_rehashed_actual_database_cannot_hide_unsupported_or_unsettled_state(
        reviewed_archive, private_temporary_directory, damage):
    c = reviewed_archive
    jobs = JobStore(c['db'])
    job = jobs.create_job(project_id=c['project'].id, kind='generation_session', start_chapter=3, end_chapter=3)
    c['package'] = Path(capture(c)['path'])
    def change(connection):
        if damage == 'unknown_table':
            connection.execute('CREATE TABLE future_execution (id TEXT)')
        else:
            connection.execute("UPDATE jobs SET state='running' WHERE id=?", (job.id,))
    replace_database(c, change)
    with absent_source(c['root']):
        result = inspect(c, c['package'], private_temporary_directory)
    assert not result['offline_data_verified']
    expected = 'unclassified_database_table' if damage == 'unknown_table' else 'job_not_settled'
    assert expected in {issue['code'] for issue in result['blockers']}


def test_components_from_another_completion_are_not_combined(
        reviewed_archive, private_temporary_directory, monkeypatch):
    c = reviewed_archive
    package = Path(capture(c)['path'])
    original = verification.inspect_archived_auxiliary
    def wrong_completion(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, 'archive_receipt_sha256':'0'*64}
    monkeypatch.setattr(verification, 'inspect_archived_auxiliary', wrong_completion)
    result = inspect(c, package, private_temporary_directory)
    assert not result['offline_data_verified']
    assert {'check':'auxiliary', 'code':'component_evidence_unverified', 'reference':'story_project'} in result['blockers']


@pytest.mark.parametrize('damage', [False, True])
def test_actual_legacy_executions_and_semantic_qualification_are_checked(
        execution_archive, private_temporary_directory, monkeypatch, damage):
    c = execution_archive
    if damage:
        (c['fact_dir'] / 'proposal.json').write_text('{}', encoding='utf-8')
    package = Path(capture(c)['path'])
    forbid_live(monkeypatch)
    with absent_source(c['root']):
        result = inspect(c, package, private_temporary_directory)
    assert result['offline_data_verified'] == (not damage), result['blockers']
    assert not result['restore_plan_ready']
    codes = {issue['code'] for issue in result['blockers']}
    assert result['checks']['legacy_state']['legacy_state_verified']
    assert result['checks']['legacy_state']['ready_for_next_chapter']
    assert result['checks']['executions']['executions_verified'] == (not damage)
    if damage:
        assert 'execution_evidence_unverified' in codes
        assert any(issue['check'] == 'source_scope' for issue in result['blockers'])
