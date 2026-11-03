from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable, Iterator
import sqlite3
import threading


SCHEMA_VERSION = 1


class WorkbenchDatabaseError(RuntimeError):
    """Raised when the local workbench database cannot be initialized."""


class WorkbenchDatabaseInitializationPending(WorkbenchDatabaseError):
    code = "workbench_database_initialization_pending"


class WorkbenchDatabase:
    """Small SQLite boundary shared by the local workbench domains.

    A fresh connection is used for each unit of work.  This keeps the object
    safe to share between request and worker threads without relaxing
    SQLite's thread ownership checks.
    """

    def __init__(self, path: str | Path, *, defer_initialization: bool = False, machine_store=None,
                 allow_recovery_controller: bool = False) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        from workbench.restore_scope import load_restore_scope
        self.restore_scope = load_restore_scope(self.path,machine_store=machine_store,
            allow_recovery_controller=allow_recovery_controller)
        self._initialization_pending = defer_initialization
        self._initializers: dict[Callable[[], None], None] = {}
        self._initialization_lock = threading.RLock()
        self._initializing_thread: int | None = None

    @property
    def initialization_pending(self) -> bool:
        return self._initialization_pending

    def initialize_component(self, operation: Callable[[], None]) -> None:
        """Register schema work while the app is assembled without write admission."""
        if self.restore_scope is not None:
            return  # A prepared restore must already contain the matching schema.
        with self._initialization_lock:
            if self._initialization_pending:
                self._initializers.setdefault(operation, None)
            else:
                operation()

    def complete_initialization(self) -> None:
        """Called only under write admission; failed components remain pending."""
        with self._initialization_lock:
            if not self._initialization_pending:
                return
            self._initializing_thread = threading.get_ident()
            try:
                while self._initializers:
                    operation = next(iter(self._initializers))
                    operation()
                    del self._initializers[operation]
                self._initialization_pending = False
            finally:
                self._initializing_thread = None

    def _read_only_pending(self) -> bool:
        return (self._initialization_pending
                and self._initializing_thread != threading.get_ident())

    def require_project_access(self, project_id: str) -> None:
        if self.restore_scope is not None:
            self.restore_scope.require_project(project_id)

    def require_write_access(self) -> None:
        if self.restore_scope is not None and self.restore_scope.read_only:
            from workbench.restore_scope import RestoreScopeBlocked
            raise RestoreScopeBlocked('恢复副本尚未启用，当前只供核对资料。')

    def initialize(self) -> None:
        self.initialize_component(self._initialize)

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS workbench_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS projects (
                        id TEXT PRIMARY KEY,
                        book_id TEXT NOT NULL UNIQUE,
                        root TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS outline_batches (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        start_chapter INTEGER NOT NULL CHECK (start_chapter >= 1),
                        end_chapter INTEGER NOT NULL CHECK (end_chapter >= start_chapter),
                        status TEXT NOT NULL CHECK (status IN ('draft', 'approved')),
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        proposal_json TEXT NOT NULL,
                        proposal_sha256 TEXT NOT NULL,
                        source_fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        approved_at TEXT,
                        approval_manifest_path TEXT,
                        approval_receipt_sha256 TEXT,
                        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_outline_batches_project
                    ON outline_batches(project_id, created_at DESC);
                    """
                )
                row = connection.execute(
                    "SELECT version FROM workbench_schema WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO workbench_schema(singleton, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                elif int(row["version"]) != SCHEMA_VERSION:
                    raise WorkbenchDatabaseError(
                        "unsupported workbench database schema version: "
                        f"{row['version']!r}"
                    )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except (OSError, sqlite3.Error) as exc:
            raise WorkbenchDatabaseError(
                f"could not initialize workbench database {self.path}: {exc}"
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection without starting a transaction."""

        connection = self._open()
        try:
            yield connection
        except sqlite3.Error as exc:
            if self._read_only_pending():
                raise WorkbenchDatabaseInitializationPending(
                    "维护期间数据库保持只读；此数据需要先完成数据库初始化。"
                ) from exc
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an explicit commit-or-rollback boundary."""

        self.require_write_access()
        from workbench.restore_qualification import restored_database_write_lock
        gate=restored_database_write_lock(self.restore_scope) if self.restore_scope is not None else nullcontext()
        with gate:
            if immediate and self._read_only_pending():
                raise WorkbenchDatabaseInitializationPending("维护期间尚未执行数据库初始化，不能写入。")
            connection = self._open()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                if self.restore_scope is not None: self.restore_scope.require_unchanged(self.path)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _open(self) -> sqlite3.Connection:
        connection = None
        if self.restore_scope is not None:
            self.restore_scope.require_unchanged(self.path)
            if self.restore_scope.controller is not None:
                from workbench.restore_scope import RestoreScopeBlocked
                raise RestoreScopeBlocked('原副本已交出写资格，不能读取普通写作入口。')
        readonly = self._read_only_pending() or (self.restore_scope is not None and self.restore_scope.read_only)
        try:
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro" if readonly else self.path,
                uri=readonly,
                timeout=10.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            if readonly:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            if readonly:
                raise WorkbenchDatabaseInitializationPending(
                    "维护期间数据库尚不可读；初始化将在维护结束后执行。"
                ) from exc
            raise WorkbenchDatabaseError(
                f"could not open workbench database {self.path}: {exc}"
            ) from exc


__all__ = ["SCHEMA_VERSION", "WorkbenchDatabase", "WorkbenchDatabaseError",
           "WorkbenchDatabaseInitializationPending"]
