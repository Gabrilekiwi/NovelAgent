"""Disposable SQLite index of verified chapter publications.

The filesystem transaction/receipt remains authoritative. This store has no
method to declare success from a caller-supplied dict or an unverified DB row.
The enclosing service must own the book writer lock and reconcile pending
transactions before calling it. Draft data is never reconstructed or changed.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.engine.chapter_commit_request import chapter_commit_request
from core.story_project.reviewed_state_store import read_verified_commit_requests
from workbench.chapter_commits import ChapterCommitConflict, ChapterCommitIntegrityError
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry


class ChapterCommitIndex:
    def __init__(self, *, database: WorkbenchDatabase, projects: ProjectRegistry):
        if projects.database.path != database.path:
            raise ChapterCommitIntegrityError("commit index must use the project database")
        self.database, self.projects = database, projects
        database.initialize()
        database.initialize_component(self._initialize)

    def _initialize(self):
        with self.database.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS chapter_publication_index (
                book_id TEXT NOT NULL,
                idempotency_scope_sha256 TEXT NOT NULL,
                chapter INTEGER NOT NULL CHECK (chapter >= 1),
                run_id TEXT NOT NULL,
                client_request_sha256 TEXT NOT NULL,
                receipt_hash TEXT NOT NULL,
                publication_json TEXT NOT NULL,
                PRIMARY KEY (book_id, idempotency_scope_sha256),
                UNIQUE (book_id, chapter),
                UNIQUE (book_id, run_id)
            )""")

    def rebuild(self, *, project_id: str, root_map: dict, baseline_document_sha256: str,
                connection=None) -> list[dict]:
        """Atomically replace one book's index only after full disk verification.

        Optional connection is the service's BEGIN IMMEDIATE transaction, used
        while the book lock is held. This method does not commit that owner.
        """
        if connection is None:
            with self.database.transaction() as owned:
                return self.rebuild(project_id=project_id, root_map=root_map,
                    baseline_document_sha256=baseline_document_sha256, connection=owned)
        self._require_transaction(connection)
        # Registration is read through the same transaction as index updates;
        # concurrent remapping cannot change book/root between verification/write.
        row = connection.execute("SELECT book_id, root FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ChapterCommitConflict("commit project registration is missing")
        root = Path(row["root"])
        if Path(root_map["story_project"]).absolute() != root.absolute():
            raise ChapterCommitIntegrityError("commit index roots differ from the registered project")
        records = read_verified_commit_requests(root=root, book_id=row["book_id"], root_map=root_map,
                                               baseline_document_sha256=baseline_document_sha256)
        # The table is a cache: a corrupt/missing row is replaced from receipts.
        # A corrupt/missing receipt throws above and cannot be hidden by the DB.
        connection.execute("DELETE FROM chapter_publication_index WHERE book_id = ?", (row["book_id"],))
        connection.executemany("""INSERT INTO chapter_publication_index
            (book_id, idempotency_scope_sha256, chapter, run_id, client_request_sha256, receipt_hash, publication_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", [
                (record["book_id"], record["request"]["idempotency_scope_sha256"], record["chapter"], record["run_id"],
                 record["request"]["client_request_sha256"], record["receipt_hash"],
                 json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)) for record in records])
        return records

    def _require_transaction(self, connection):
        databases = {row[1]: row[2] for row in connection.execute("PRAGMA database_list")}
        if (not connection.in_transaction or not databases.get("main")
                or Path(databases["main"]).resolve() != self.database.path):
            raise ChapterCommitIntegrityError("commit index requires a transaction on its own database")

    def find_published(self, *, project_id: str, request: dict, root_map: dict,
                       baseline_document_sha256: str, connection=None) -> dict | None:
        """Look up a retry before re-preparing a now-historical chapter.

        Returning None does not authorize a new commit; the service still has
        to inspect persisted pending/rolled-back intents, locks and live inputs.
        """
        if connection is None:
            with self.database.transaction() as owned:
                return self.find_published(project_id=project_id, request=request, root_map=root_map,
                    baseline_document_sha256=baseline_document_sha256, connection=owned)
        self._require_transaction(connection)
        if not isinstance(request, dict) or request.get("project_id") != project_id:
            raise ChapterCommitConflict("retry belongs to another project registration")
        row = connection.execute("SELECT book_id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ChapterCommitConflict("commit project registration is missing")
        incoming = chapter_commit_request(book_id=row["book_id"], request=request)
        records = self.rebuild(project_id=project_id, root_map=root_map,
            baseline_document_sha256=baseline_document_sha256, connection=connection)
        for record in records:
            old = record["request"]
            if old["idempotency_scope_sha256"] == incoming["idempotency_scope_sha256"]:
                if old != incoming:
                    raise ChapterCommitConflict("idempotency key was reused for a different request")
                return record
        return None
