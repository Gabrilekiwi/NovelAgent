from __future__ import annotations

import sqlite3
import unittest
import uuid
from pathlib import Path

from workbench.database import SCHEMA_VERSION, WorkbenchDatabase


class WorkbenchDatabaseTest(unittest.TestCase):
    def _database(self, name: str) -> WorkbenchDatabase:
        root = Path.cwd() / ".tmp" / "test_workbench_database" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        return WorkbenchDatabase(root / "workbench.db")

    def test_initialize_enables_wal_foreign_keys_and_schema(self) -> None:
        database = self._database("initialize")
        database.initialize()

        with database.connection() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(SCHEMA_VERSION, connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertIn("projects", tables)
        self.assertIn("outline_batches", tables)

    def test_transaction_is_extensible_and_rolls_back_on_error(self) -> None:
        database = self._database("rollback")
        database.initialize()
        with database.transaction() as connection:
            connection.execute("CREATE TABLE jobs_for_test(id TEXT PRIMARY KEY)")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with database.transaction() as connection:
                connection.execute("INSERT INTO jobs_for_test(id) VALUES ('job-1')")
                raise RuntimeError("stop")

        with database.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM jobs_for_test").fetchone()[0]
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
