"""Offline SQLite proof must come from actual bytes, not manifest assertions."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from tests.test_backup_archive import archive_case, legacy, software_tree, capture
from tests.test_backup_archive_history import absent_source
from workbench.backup_archive import verify_archive, _json
from workbench.backup_archive_database import inspect_archived_database, ArchivedDatabaseError, _database
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_database import _logical_digest
from workbench.backup_files import inventory_tree
from core.story_project.reviewed_state_document import digest


@pytest.fixture(autouse=True)
def private_temporary_directory(tmp_path):
    scratch=tmp_path/'scratch';scratch.mkdir()
    yield scratch
    assert list(scratch.iterdir()) == []


@pytest.fixture
def saved(archive_case,private_temporary_directory):
    c = archive_case
    c['temporary_root']=private_temporary_directory
    c['draft'] = c['jobs'].upsert_draft(project_id=c['project'].id, chapter=1, text='作者保留的中文草稿')
    c['job'] = c['jobs'].create_job(project_id=c['project'].id, kind='generation_session', start_chapter=1, end_chapter=2)
    c['other'] = c['projects'].create_project(c['root'].parent/'另一作品',name='另一作品')
    c['other_job'] = c['jobs'].create_job(project_id=c['other'].id, kind='generation_session', start_chapter=1, end_chapter=2)
    c['package'] = Path(capture(c)['path'])
    return c


def inspect(c):
    return inspect_archived_database(c['package'], project_id=c['project'].id, book_id=c['project'].book_id,
                                     temporary_root=c['temporary_root'])


def replace_database(c, mutation, *, bind_logical=True, header_wal=False):
    """Build an internally checksum-consistent package with altered SQL data."""
    package = c['package']
    with zipfile.ZipFile(package/'archive.zip') as archive:
        payload = {name:archive.read(name) for name in archive.namelist()}
    temporary = package/'mutation.sqlite'; temporary.write_bytes(payload['workbench.sqlite'])
    with closing(sqlite3.connect(temporary)) as connection:
        mutation(connection); connection.commit()
        logical = _logical_digest(connection)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    raw = temporary.read_bytes()
    if header_wal:
        raw = raw[:18]+b'\x02\x02'+raw[20:]
    manifest = json.loads(payload['manifest.json'])
    meta = {'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    manifest['members']['workbench.sqlite'] = meta
    manifest['database'].update(meta)
    if bind_logical:
        manifest['database'].update(logical_sha256=logical,user_version=version)
        manifest['preview']['database'].update(logical_sha256=logical,user_version=version)
    preview = manifest['preview']
    preview['preview_sha256'] = digest({k:v for k,v in preview.items() if k!='preview_sha256'})
    manifest['manifest_sha256'] = digest({k:v for k,v in manifest.items() if k!='manifest_sha256'})
    payload.update({'workbench.sqlite':raw,'manifest.json':_json(manifest)})
    with zipfile.ZipFile(package/'archive.zip','w') as archive:
        for name,data in payload.items():archive.writestr(name,data)
    (package/'manifest.json').write_bytes(_json(manifest))
    receipt = json.loads((package/'completion.json').read_bytes())
    raw_zip = (package/'archive.zip').read_bytes()
    receipt.update(archive={'size':len(raw_zip),'sha256':hashlib.sha256(raw_zip).hexdigest()},
                   manifest_sha256=manifest['manifest_sha256'],preview_sha256=preview['preview_sha256'])
    receipt['receipt_sha256'] = digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})
    (package/'completion.json').write_bytes(_json(receipt))
    assert verify_archive(package)['archive_verified']


def test_offline_database_keeps_drafts_and_requires_other_projects_disabled_without_source_access(saved, monkeypatch):
    c = saved; before = inventory_tree(c['package'])
    connect = sqlite3.connect
    opened = []
    def private_only(path,*args,**kwargs):
        assert 'na-db-' in str(path) and 'mode=ro' in str(path) and 'immutable=1' in str(path)
        opened.append(str(path)); return connect(path,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',private_only)
    from workbench.database import WorkbenchDatabase
    monkeypatch.setattr(WorkbenchDatabase,'__init__',lambda *_a,**_k:pytest.fail('app database constructed offline'))
    with absent_source(c['root']), absent_source(c['other'].root,temporary_name='o'):
        first = inspect(c); second = inspect(c)
    assert first == second and first['database_verified']
    assert first['target_tables']['drafts']['count'] == 1
    assert first['target_tables']['drafts']['rows'][0]['key']['id'] == c['draft'].id
    assert first['target_jobs'][0]['id'] == c['job'].id
    policy = first['restore_policy']
    assert policy['disable_other_project_ids'] == [c['other'].id]
    assert policy['disable_other_job_ids'] == [c['other_job'].id]
    assert policy['target_jobs_require_explicit_revalidation'] == [c['job'].id]
    assert not any(policy[k] for k in ('start_worker','auto_import','provider_calls_allowed','write_activation_allowed','enforced_in_restored_app'))
    assert not first['recoverable'] and not first['restore_plan_ready']
    assert len(opened) == 2 and inventory_tree(c['package']) == before


@pytest.mark.parametrize('damage',['draft_text','root','book','foreign_key','schema','user_version','missing_table','logical_digest','wal_header'])
def test_valid_zip_rejected_for_invalid_actual_sqlite_state(saved, damage):
    mutations = {
        'draft_text':"UPDATE drafts SET text='changed'",
        'root':"UPDATE projects SET root='D:\\not-the-archived-root' WHERE id=?",
        'book':"UPDATE projects SET book_id='different-book' WHERE id=?",
        'foreign_key':"UPDATE drafts SET project_id='lost-project'",
        'schema':"UPDATE workbench_schema SET version=999",
        'user_version':"PRAGMA user_version=999",
        'missing_table':"DROP TABLE drafts",
        'logical_digest':"UPDATE projects SET name='changed' WHERE id=?",
        'wal_header':'SELECT 1',
    }
    sql = mutations[damage]
    replace_database(saved,lambda conn:conn.execute(sql,(saved['project'].id,) if '?' in sql else ()),
                     bind_logical=damage!='logical_digest',header_wal=damage=='wal_header')
    before = inventory_tree(saved['package'])
    reason={'draft_text':'草稿','root':'登记','book':'登记','foreign_key':'外键','schema':'schema',
            'user_version':'版本','missing_table':'必要表','logical_digest':'逻辑状态','wal_header':'独立'}[damage]
    with pytest.raises(ArchivedDatabaseError,match=reason):inspect(saved)
    assert inventory_tree(saved['package']) == before


@pytest.mark.parametrize('statement',["UPDATE projects SET name='changed'",'CREATE TABLE forbidden (id)',
                                    "ATTACH DATABASE ':memory:' AS attached",'PRAGMA user_version=9'])
def test_offline_connection_rejects_writes_and_attachment(saved, statement):
    before = inventory_tree(saved['package'])
    with verified_archive_view(saved['package']) as view, _database(view,temporary_root=saved['temporary_root']) as connection:
        with pytest.raises(sqlite3.DatabaseError):connection.execute(statement)
    assert inventory_tree(saved['package']) == before


def test_archive_changes_after_reads_cannot_return_success(saved):
    with pytest.raises((ValueError,RuntimeError)):
        with verified_archive_view(saved['package']) as view:
            assert view.read_root('story_project','.novelagent/project.json')
            (saved['package']/'completion.json').write_bytes(b'{}')


def test_archive_reader_cannot_outlive_verified_context(saved):
    with verified_archive_view(saved['package']) as view:
        assert view.read_root('story_project','.novelagent/project.json')
    with pytest.raises((ValueError,RuntimeError)):
        view.read_root('story_project','.novelagent/project.json')
