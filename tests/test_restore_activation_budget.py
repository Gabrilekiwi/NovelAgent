"""An actual unresolved source call remains an activation blocker."""
import pytest

from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import bound_copy
from tests.test_restore_activation import prepared, activate
from tests.test_restore_budget_preparation import full_source, financial
from tests.test_restore_executions import prepared as jobs_prepared
from tests.test_restore_handoff import restored, ledger, deny
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.backup_files import inventory_tree
from workbench.model_usage import read_project_model_usage
from workbench.restore_handoff import RestoreHandoffError


def test_actual_unsettled_source_call_cannot_activate_or_erase_occupancy(prepared):
    c = prepared; root = c['target']/'book'; before = inventory_tree(root)
    usage = read_project_model_usage(root, book_id=c['project'].book_id)
    assert usage['chapter_budgets'][0]['charged_amount'] == '0.000769'
    with pytest.raises(RestoreHandoffError, match='尚未核实结算'): activate(c)
    assert not c['activation_service']._path(c['id'], 'activation').exists()
    assert not c['activation_service']._path(c['id'], 'activation-done').exists()
    assert ledger(c) == c['value']['restoring_record'] and inventory_tree(root) == before
    assert read_project_model_usage(root, book_id=c['project'].book_id)['chapter_budgets'] == usage['chapter_budgets']
    deny(root)
