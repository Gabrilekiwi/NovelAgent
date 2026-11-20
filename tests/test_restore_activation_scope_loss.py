"""An activated database must not fall back to ordinary mode when local scope is lost."""
from contextlib import closing
import sqlite3
import shutil

import pytest

from core.autonomy.common import atomic_append_json
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import bound_copy
from tests.test_restore_activation import full_source, prepared, activate
from tests.test_restore_budget_preparation import financial
from tests.test_restore_executions import prepared as jobs_prepared
from tests.test_restore_handoff import restored
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from workbench.database import WorkbenchDatabase
from workbench.restore_scope import RestoreScopeError


def test_missing_all_local_scope_markers_cannot_reopen_as_ordinary_database(prepared):
    c = prepared; activate(c); root = c['target']/'book'; database = c['target']/'workbench.sqlite'
    for name in ('restore-intent.json','execution-policy.json','write-qualification.json',
                 'workbench.sqlite.restore-scope.json'):
        (c['target']/name).unlink()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('DROP TABLE workbench_restore_scope'); connection.commit()
    probe = root/'.novelagent/runtime/no-scope-write.json'
    with pytest.raises((RuntimeError,ValueError,OSError)):
        atomic_append_json(probe, {'must_not_write':True})
    assert not probe.exists()
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(database, machine_store=c['store'])
    shadow = c['target']/'shadow.sqlite'
    shutil.copyfile(database, shadow)
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(shadow, machine_store=c['store'])
    # The original multi-book database has a different root registration and
    # remains readable; its old book still cannot acquire write ownership.
    assert WorkbenchDatabase(c['db'].path, machine_store=c['store']).restore_scope is None
