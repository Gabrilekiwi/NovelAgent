"""Whole SQLite snapshot under caller-held maintenance/book/Provider locks."""
import hashlib
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat

from core.engine.safe_paths import assert_safe_local_tree, FILE_ATTRIBUTE_REPARSE_POINT


class BackupDatabaseError(RuntimeError):
    code='backup_database_invalid'


def _identity(path):
    assert_safe_local_tree(path.parent)
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
            or getattr(info,'st_file_attributes',0)&FILE_ATTRIBUTE_REPARSE_POINT):
        raise BackupDatabaseError('数据库文件类型或物理绑定不安全。')
    return (info.st_dev,info.st_ino)


def _logical_digest(connection):
    # Stable across backup page layout/WAL checkpoint differences. Retain every
    # table and index, including non-target registrations and SQLite sequences.
    value=hashlib.sha256()
    for statement in connection.iterdump():
        encoded=statement.encode('utf-8')
        value.update(len(encoded).to_bytes(8,'big'))
        value.update(encoded)
    # iterdump omits header metadata and hidden rowids. They matter to a whole
    # database snapshot even when application tables do not usually use them.
    for pragma in ('user_version','application_id'):
        value.update(f'{pragma}:{connection.execute("PRAGMA "+pragma).fetchone()[0]}'.encode())
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted='"'+name.replace('"','""')+'"'
        columns={row[1].casefold() for row in connection.execute('PRAGMA table_info('+quoted+')')}
        alias=next((key for key in ('rowid','_rowid_','oid') if key not in columns),None)
        if alias is None:continue
        try:rows=connection.execute(f'SELECT {alias}, * FROM {quoted} ORDER BY {alias}')
        except sqlite3.OperationalError as exc:
            if 'no such column' in str(exc):continue  # WITHOUT ROWID table
            raise
        value.update(quoted.encode())
        for row in rows:
            encoded=repr(tuple(row)).encode('utf-8')
            value.update(len(encoded).to_bytes(8,'big'));value.update(encoded)
    return value.hexdigest()


class _OwnedDatabaseSnapshot:
    def __init__(self,source,source_identity,writer,reader,permit):
        self.source=source
        self.source_identity=source_identity
        self._writer=writer
        self.connection=reader
        self._permit=permit
        self._active=True

    def require(self,database,permit):
        permit.require()
        if (not self._active or permit is not self._permit or Path(database.path)!=self.source
                or not self._writer.in_transaction or not self.connection.in_transaction):
            raise BackupDatabaseError('快照事务已结束或不属于当前维护操作。')
        if _identity(self.source)!=self.source_identity:
            raise BackupDatabaseError('快照源数据库物理文件发生替换。')

    def fingerprint(self,database,permit):
        self.require(database,permit)
        return {'logical_sha256':_logical_digest(self.connection),
                'file_identity':list(self.source_identity),
                'user_version':self.connection.execute('PRAGMA user_version').fetchone()[0]}


def _read_authorizer(action,arg1,arg2,db,trigger):
    if action in {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}:
        return sqlite3.SQLITE_OK
    if action==sqlite3.SQLITE_PRAGMA:
        if arg1.lower() in {'table_info','table_xinfo','index_list','index_info','index_xinfo','foreign_key_list'}:
            return sqlite3.SQLITE_OK
        if arg2 is None and arg1.lower() in {
            'user_version','application_id','page_count','page_size','database_list',
            'integrity_check','foreign_key_check','schema_version','encoding',
        }:return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@contextmanager
def owned_database_snapshot(database,*,snapshot_permit):
    """Own a fresh writer fence and expose only its committed read snapshot.

    Acquire book/Provider locks first. Keep this context across preflight,
    file copying and database capture. Never accept a caller's transaction:
    SQLite dumps cannot prove the absence of every uncommitted state change.
    """
    snapshot_permit.require()
    source=Path(database.path)
    source_identity=_identity(source)
    with closing(sqlite3.connect(source.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=1)) as writer:
        try:
            # No schema initialization, journal-mode conversion or application
            # writes may occur on this connection before or after this fence.
            writer.execute('BEGIN IMMEDIATE')
            writer.set_authorizer(_read_authorizer)
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=1)) as reader:
                reader.row_factory=sqlite3.Row
                reader.execute('PRAGMA query_only=ON')
                reader.execute('BEGIN')
                reader.set_authorizer(_read_authorizer)
                snapshot=_OwnedDatabaseSnapshot(source,source_identity,writer,reader,snapshot_permit)
                try:
                    snapshot.require(database,snapshot_permit)
                    # Establish the read view while the writer fence is held.
                    reader.execute('SELECT count(*) FROM sqlite_master').fetchone()
                    yield snapshot
                    snapshot.require(database,snapshot_permit)
                finally:snapshot._active=False
        finally:
            writer.set_authorizer(None)
            writer.rollback()


def capture_database(database, destination, *, snapshot, snapshot_permit, progress=None):
    """Copy the complete DB from an owned snapshot into a new standalone file.

    Failure leaves a non-certified partial destination for diagnosis. The
    enclosing context retains the writer fence for the entire archive capture.
    """
    if not isinstance(snapshot,_OwnedDatabaseSnapshot):
        raise BackupDatabaseError('整库快照需要组件持有的新建事务。')
    snapshot.require(database,snapshot_permit)
    source=snapshot.source
    source_identity=snapshot.source_identity
    reader=snapshot.connection
    expected=_logical_digest(reader)
    source_version=reader.execute('PRAGMA user_version').fetchone()[0]
    target=Path(destination).absolute()
    if target==source or os.path.lexists(target):
        raise BackupDatabaseError('快照目标必须是新的独立文件。')
    assert_safe_local_tree(target.parent)
    if not target.parent.is_dir():raise BackupDatabaseError('快照暂存目录不存在。')
    if reader.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        raise BackupDatabaseError('源数据库未通过完整性检查。')
    if reader.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise BackupDatabaseError('源数据库存在外键缺失。')
    snapshot.require(database,snapshot_permit)
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(fd)
    target_identity=_identity(target)
    copied=sqlite3.connect(target,isolation_level=None)
    try:
        def checked_progress(status,remaining,total):
            snapshot.require(database,snapshot_permit)
            if _identity(source)!=source_identity or _identity(target)!=target_identity:
                raise BackupDatabaseError('复制期间数据库物理文件发生替换。')
            if progress is not None:progress(status,remaining,total)
        reader.backup(copied,pages=128,progress=checked_progress,sleep=0.01)
        if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise BackupDatabaseError('数据库副本未通过完整性检查。')
        if copied.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise BackupDatabaseError('数据库副本存在外键缺失。')
        if copied.execute('PRAGMA user_version').fetchone()[0]!=source_version or _logical_digest(copied)!=expected:
            raise BackupDatabaseError('数据库副本与锁定的完整逻辑快照不同。')
        # Close into a self-contained main file; do not ship a live WAL.
        copied.execute('PRAGMA journal_mode=DELETE')
    finally:copied.close()
    snapshot.require(database,snapshot_permit)
    if _identity(source)!=source_identity or _identity(target)!=target_identity or _logical_digest(reader)!=expected:
        raise BackupDatabaseError('复制结束前数据库来源发生变化。')
    with target.open('rb') as stream:
        checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(schema_version='workbench_database_snapshot_v1',logical_sha256=expected,
        sha256=checksum,size=target.stat().st_size,user_version=source_version,
        scope='whole_database',other_projects_default_disabled=True)
