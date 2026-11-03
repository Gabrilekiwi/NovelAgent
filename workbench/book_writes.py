"""Machine-first workbench writes bound to the actual SQLite registration."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading

from core.engine.persistence import PersistenceLockError
from core.story_project.reviewed_state_document import digest
from workbench import book_ownership as ownership
from workbench.projects import ProjectRegistryError

_TABLES = frozenset({'outline_batches', 'amendments', 'planning_operations'})


@contextmanager
def read_registration(database):
    # Normal WorkbenchDatabase.connection may switch journal mode. Routing a
    # write request must remain a read before machine qualification is known.
    # mode=ro deliberately sees committed WAL frames. immutable=1 would skip
    # those frames and can route a request using an obsolete registration.
    # SQLite may maintain its WAL/SHM coordination files even on a read.
    if database.restore_scope is not None:
        database.restore_scope.require_unchanged(database.path)
    ownership._file_identity(database.path)
    connection = sqlite3.connect(database.path.as_uri()+'?mode=ro', uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        yield connection
    finally:
        connection.close()


def _row(connection, project_id):
    row = connection.execute('SELECT id,book_id,root FROM projects WHERE id=?', (str(project_id),)).fetchone()
    if row is None:
        raise ProjectRegistryError(f'unknown workbench project: {project_id}')
    return dict(row)


def _record_project(connection, table, record_id):
    if table not in _TABLES:
        raise ValueError('unsupported workbench write record')
    row = connection.execute(f'SELECT project_id FROM {table} WHERE id=?', (str(record_id),)).fetchone()
    if row is None:
        raise ProjectRegistryError(f'unknown {table} record: {record_id}')
    return str(row['project_id'])


@dataclass
class RegisteredBookWrite:
    database: object
    project: object
    registration: dict
    database_identity: dict
    thread_id: int
    record: tuple | None = None
    active: bool = True

    def require(self, connection=None):
        if not self.active or self.thread_id != threading.get_ident():
            raise ownership.BookOwnershipError('registered write scope is no longer held by this thread')
        if ownership._file_identity(self.database.path) != self.database_identity:
            raise ownership.BookOwnershipError('workbench database physical identity changed')
        # Reacquisition validates the underlying OS handle, including nested
        # operations. The context variable itself is never a write permit.
        with ownership.book_output_write_lock(self.project.root, book_id=self.project.book_id):
            stores = {store for known,store in ownership._machine_scopes.get() if known==self.project.book_id}
            if len(stores)!=1:
                raise ownership.BookOwnershipError('machine write scope is ambiguous')
            store=next(iter(stores))
            record=ownership._record(store/digest(self.project.book_id.encode('utf-8')),self.project.book_id)
            if record is not None:
                binding=record['binding']
                if (binding.get('database')!=str(self.database.path)
                        or binding.get('project_id')!=self.project.id):
                    raise ownership.BookOwnershipError('当前工作台不是这本作品的活动登记，请在持有写入资格的副本中操作。')
            if connection is None:
                with read_registration(self.database) as current:
                    self._require_row(current)
            else:
                databases={r[1]:r[2] for r in connection.execute('PRAGMA database_list')}
                if Path(databases.get('main','')).resolve()!=self.database.path:
                    raise ownership.BookOwnershipError('write connection is not the qualified workbench database')
                self._require_row(connection)

    def _require_row(self, connection):
        if _row(connection,self.project.id)!=self.registration:
            raise ownership.BookOwnershipError('workbench project registration changed while writing')
        if self.record is not None and _record_project(connection,*self.record)!=self.project.id:
            raise ownership.BookOwnershipError('workbench record moved to another project')

    @contextmanager
    def transaction(self):
        self.require()
        with self.database.transaction() as connection:
            self.require(connection)
            yield connection
            self.require(connection)


@contextmanager
def registered_book_write(projects, project_id, *, database=None, record=None):
    database=database or projects.database
    if database.path!=projects.database.path:
        raise ownership.BookOwnershipError('service and project registry use different databases')
    database.require_write_access()
    database.require_project_access(project_id)
    physical=ownership._file_identity(database.path)
    with read_registration(database) as connection:
        initial=_row(connection,project_id)
    with ownership.book_output_write_lock(Path(initial['root']),book_id=initial['book_id']):
        with read_registration(database) as connection:
            if _row(connection,project_id)!=initial:
                raise ownership.BookOwnershipError('workbench registration changed while acquiring ownership')
            project=projects.get(project_id,connection=connection)
        scope=RegisteredBookWrite(database,project,initial,physical,threading.get_ident(),record)
        try:
            scope.require()
            yield scope
            scope.require()
        finally:
            scope.active=False


@contextmanager
def registered_record_write(projects, table, record_id, *, database=None):
    database=database or projects.database
    database.require_write_access()
    with read_registration(database) as connection:
        project_id=_record_project(connection,table,record_id)
    with registered_book_write(projects,project_id,database=database,record=(table,str(record_id))) as scope:
        yield scope


def recover_interrupted_operations(projects, database, *, table, error_json, timestamp):
    if table=='planning_operations':column,prior='state','running'
    elif table=='amendments':column,prior='status','validating'
    else:raise ValueError('unsupported startup recovery table')
    with read_registration(database) as connection:
        rows=connection.execute(f'SELECT id,project_id FROM {table} WHERE {column}=?', (prior,)).fetchall()
    recovered=0;blocked=[]
    for row in rows:
        try:
            with registered_record_write(projects,table,row['id'],database=database) as write:
                with write.transaction() as connection:
                    cursor=connection.execute(
                        f"UPDATE {table} SET {column}='needs_recovery',error_json=?,updated_at=? WHERE id=? AND {column}=?",
                        (error_json,timestamp,row['id'],prior))
                    changed=cursor.rowcount
            recovered+=changed
        except (ownership.BookOwnershipError,PersistenceLockError,ProjectRegistryError) as exc:
            blocked.append({'project_id':row['project_id'],'id':row['id'],'code':getattr(exc,'code',type(exc).__name__)})
    return recovered,blocked
