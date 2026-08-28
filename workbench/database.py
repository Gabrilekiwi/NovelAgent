from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
import sqlite3


SCHEMA_VERSION = 1


class WorkbenchDatabaseError(RuntimeError):
    """Raised when the local workbench database cannot be initialized."""


class WorkbenchDatabase:
    """Small SQLite boundary shared by the local workbench domains.

    A fresh connection is used for each unit of work.  This keeps the object
    safe to share between request and worker threads without relaxing
    SQLite's thread ownership checks.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)

    def initialize(self) -> None:
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
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an explicit commit-or-rollback boundary."""

        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=10.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise WorkbenchDatabaseError(
                f"could not open workbench database {self.path}: {exc}"
            ) from exc


__all__ = ["SCHEMA_VERSION", "WorkbenchDatabase", "WorkbenchDatabaseError"]
