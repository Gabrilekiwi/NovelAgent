"""Machine-scope ownership on actual upgraded books, without Provider calls."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from core.engine.persistence import atomic_write_json
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_candidate_context import registered_upgrade
from tests.test_reviewed_upgrade import case
from workbench.book_ownership import BookOwnership, BookOwnershipError
from workbench.database import WorkbenchDatabase


@pytest.fixture
def owned(case):
    db, projects, project, upgrade = registered_upgrade(case)
    machine = case["root"].parent / "machine"
    owner = BookOwnership(project.book_id, machine_store=machine)
    with owner.locked():
        with db.transaction() as connection:
            args = dict(root=case["root"], database=db, project_id=project.id, connection=connection)
            record = owner.enroll(**args)
            assert owner.require(**args) == owner.enroll(**args) == record
    return locals()


def test_ownership_is_shared_across_database_instances_and_does_not_auto_rebind(owned):
    c = owned
    other = WorkbenchDatabase(c["case"]["root"].parent / "other.db")
    with c["db"].connection() as source:
        with other.connection() as target:
            source.backup(target)
    with c["owner"].locked():
        with other.transaction() as connection:
            with pytest.raises(BookOwnershipError, match="physical ownership"):
                c["owner"].enroll(root=c["case"]["root"], database=other, project_id=c["project"].id, connection=connection)
    assert json.loads(c["owner"].record_path.read_text(encoding="utf-8")) == c["record"]


def test_directory_replacement_at_the_same_path_invalidates_ownership(owned):
    c = owned
    root = c["case"]["root"]
    previous = root.with_name("old-root")
    assert root.is_relative_to(Path.cwd() / ".tmp") and previous.is_relative_to(Path.cwd() / ".tmp")
    root.rename(previous)
    shutil.copytree(previous, root)
    with c["owner"].locked():
        with c["db"].transaction() as connection:
            with pytest.raises(BookOwnershipError, match="physical ownership"):
                c["owner"].require(root=root, database=c["db"], project_id=c["project"].id, connection=connection)


def test_suspended_ownership_cannot_be_silently_reactivated(owned):
    c = owned
    value = {**c["record"], "state": "suspended"}
    value["record_sha256"] = digest({k: v for k, v in value.items() if k != "record_sha256"})
    atomic_write_json(c["owner"].record_path, value)
    with c["owner"].locked():
        with c["db"].transaction() as connection:
            with pytest.raises(BookOwnershipError):
                c["owner"].enroll(root=c["case"]["root"], database=c["db"], project_id=c["project"].id, connection=connection)
    assert json.loads(c["owner"].record_path.read_text(encoding="utf-8")) == value


def test_machine_book_os_lock_blocks_a_second_process(owned):
    c = owned
    code = """import sys
from pathlib import Path
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceAlreadyRunning
try:
    with BookOwnership(sys.argv[1], machine_store=Path(sys.argv[2])).locked():
        raise AssertionError('second process acquired an owned book')
except InstanceAlreadyRunning:
    print('blocked')
"""
    with c["owner"].locked():
        child = subprocess.run([sys.executable, "-B", "-c", code, c["project"].book_id, str(c["machine"])],
            capture_output=True, text=True, timeout=20, check=True)
        assert child.stdout.strip() == "blocked"


def test_corrupt_ownership_record_never_authorizes_writing(owned):
    c = owned
    value = {**c["record"], "generation": 2}
    atomic_write_json(c["owner"].record_path, value)
    with c["owner"].locked():
        with c["db"].transaction() as connection:
            with pytest.raises(BookOwnershipError):
                c["owner"].require(root=c["case"]["root"], database=c["db"], project_id=c["project"].id, connection=connection)
