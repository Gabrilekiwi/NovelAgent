"""Owned reviewed chapter commit and original-request retry service.

Requests contain IDs/CAS/key only. Machine book -> Provider boundary -> SQLite
write transaction -> persistence locks is the fixed acquisition order.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from core.engine.chapter_commit_request import chapter_commit_request, prepare_commit_request_evidence
from core.engine.persistence import atomic_create_json
from core.engine.persistence_v2 import (
    PersistenceV2Error, _load_staged_content, load_persistence_manifest_v2, reconcile_pending_persistence_v2,
)
from core.engine.provider_safety import list_unresolved_provider_calls, provider_execution_lock
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.reviewed_chapter_commit import build_reviewed_chapter_transaction
from core.engine.root_registry import RootRegistryService
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.book_ownership import BookOwnership
from workbench.chapter_commit_index import ChapterCommitIndex
from workbench.chapter_commits import ChapterCommitConflict, ChapterCommitIntegrityError, ChapterCommitRecoveryRequired

INTENT_VERSION = "reviewed_commit_intent_v1"


class ChapterPublicationService:
    def __init__(self, *, preparations, machine_store=None, quality_policy="standard",
                 fault_injector=None, persistence_fault_injector=None):
        self.preparations = preparations
        self.database, self.projects = preparations.database, preparations.projects
        self.index = ChapterCommitIndex(database=self.database, projects=self.projects)
        self.machine_store, self.quality_policy = machine_store, quality_policy
        self.fault_injector, self.persistence_fault_injector = fault_injector, persistence_fault_injector

    def _registration(self, project_id, connection):
        row = connection.execute("SELECT id, book_id, root FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ChapterCommitConflict("commit project registration is missing")
        root = self.projects.validate_project_root(row["root"])
        return {"project_id": row["id"], "book_id": row["book_id"], "root": root}

    def _owner(self, book_id):
        return BookOwnership(book_id, machine_store=self.machine_store)

    def commit(self, *, project_id, chapter, review_id, expected_draft_revision, expected_review_revision,
               expected_draft_sha256, expected_facts_sha256, idempotency_key):
        route = dict(project_id=project_id, chapter=chapter, review_id=review_id,
            expected_draft_revision=expected_draft_revision, expected_review_revision=expected_review_revision,
            expected_draft_sha256=expected_draft_sha256, expected_facts_sha256=expected_facts_sha256,
            idempotency_key=idempotency_key)
        return self._request(route, prepare_new=True)

    def resolve(self, **route):
        """Resolve a lost response without starting a never-submitted request."""
        return self._request(route, prepare_new=False)

    def _request(self, route, *, prepare_new):
        project_id, chapter = route["project_id"], route["chapter"]
        with self._owned_project(project_id) as context:
            current, connection, owner, owner_args, ownership, root, runtime, roots, tx_root = context
            incoming = chapter_commit_request(book_id=current["book_id"], request=route)
            from workbench.restore_execution_policy import require_restored_execution_paths
            require_restored_execution_paths(runtime/'reviewed/intents'/f"{incoming['idempotency_scope_sha256']}.json")
            self._reconcile_owned_persistence(tx_root, book_id=current["book_id"])
            verified = load_verified_reviewed_identity(root)
            if owner.require(**owner_args) != ownership:
                raise ChapterCommitConflict("book ownership changed during recovery")
            index_args = dict(project_id=project_id, request=route, root_map=roots, connection=connection,
                baseline_document_sha256=verified["identity"]["reviewed_state"]["baseline_document_sha256"])
            published = self.index.find_published(**index_args)
            if published is not None:
                return published
            intents = assert_safe_local_tree(runtime / "reviewed/intents")
            intent_path = intents / f"{incoming['idempotency_scope_sha256']}.json"
            if intent_path.exists():
                return self._original_unpublished(intent_path, incoming=incoming, tx_root=tx_root)
            if not prepare_new:
                return {"state": "not_started", "committed": False, "publication_verified": False,
                    "retry_requires_new_key": False}
            preparation = self.preparations.prepare(**route, connection=connection)
            trusted = ReviewedChapterCandidateContextBuilder(root).build(chapter)
            run_id = f"commit_{incoming['idempotency_scope_sha256'][:32]}"
            tx, arguments = build_reviewed_chapter_transaction(trusted_context=trusted,
                chapter_preparation=preparation, request=incoming, run_id=run_id,
                quality_policy=self.quality_policy, fault_injector=self.persistence_fault_injector)
            if owner.require(**owner_args) != ownership:
                raise ChapterCommitConflict("book ownership changed during preparation")
            evidence = prepare_commit_request_evidence(preparation=preparation, request=incoming)
            intent = {"version": INTENT_VERSION, "run_id": run_id, "evidence": evidence,
                "ownership_sha256": ownership["record_sha256"]}
            intent["intent_sha256"] = digest(intent)
            intents.mkdir(parents=True, exist_ok=True)
            atomic_create_json(intent_path, intent)
            self._fault("after_intent", intent)
            tx.prepare(**arguments)
            self._fault("after_prepare", intent)
            self._fault("before_commit", intent)
            if (owner.require(**owner_args) != ownership
                    or self.preparations.prepare(**route, connection=connection) != preparation):
                raise ChapterCommitConflict("author request or ownership changed before publication")
            result = tx.commit()
            if not result["committed"]:
                self._reconcile_owned_persistence(tx_root, book_id=current["book_id"])
            self._fault("after_file_commit_before_index", intent)
            if owner.require(**owner_args) != ownership:
                raise ChapterCommitConflict("book ownership changed after file publication")
            published = self.index.find_published(**index_args)
            if published is None:
                return self._original_unpublished(intent_path, incoming=incoming, tx_root=tx_root)
            self._fault("before_db_commit", published)
            return published

    @contextmanager
    def _owned_project(self, project_id):
        # Registration must be readable before history: interrupted file apply
        # can require recovery before strict project lookup is possible.
        with self.database.connection() as connection:
            initial = self._registration(project_id, connection)
        # Concurrent identical requests must serialize on machine ownership
        # instead of failing the second waiter with InstanceAlreadyRunning.
        with self._owner(initial["book_id"]).locked(wait_timeout=60) as owner:
            root = initial["root"]
            runtime = assert_safe_local_tree(root / ".novelagent/runtime")
            with provider_execution_lock(runtime / "runs"):
                with self.database.transaction() as connection:
                    current = self._registration(project_id, connection)
                    if current != initial:
                        raise ChapterCommitConflict("project registration changed before the commit lock")
                    owner_args = dict(root=root, database=self.database, project_id=project_id, connection=connection)
                    ownership = owner.require(**owner_args)
                    if list_unresolved_provider_calls(runtime / "runs"):
                        raise ChapterCommitConflict("unresolved Provider call blocks chapter publication")
                    roots = {"story_project": root, "runtime": runtime}
                    tx_root = runtime / "reviewed/p"
                    self._require_persistence_roots(tx_root, roots)
                    yield current, connection, owner, owner_args, ownership, root, runtime, roots, tx_root
                    if owner.require(**owner_args) != ownership:
                        raise ChapterCommitConflict("book ownership changed during the operation")

    def preview(self, **route):
        """Read-only quality preview; pending transactions need explicit recovery."""
        with self._owned_project(route["project_id"]) as context:
            current, connection, owner, owner_args, ownership, root, runtime, roots, tx_root = context
            chapter_commit_request(book_id=current["book_id"], request=route)
            self._require_settled_persistence(tx_root)
            return self.preparations.prepare_quality(**route, quality_policy=self.quality_policy, connection=connection)

    def review_input(self, *, project_id, chapter, review_id):
        # Reading an already stored draft does not require a current Candidate:
        # the UI must be able to display the original request during recovery.
        with self._owned_project(project_id) as context:
            current, connection, *_ = context
            draft, review = self.preparations._read_pair(review_id, connection=connection)
            if (draft.project_id != project_id or review.project_id != project_id
                    or draft.chapter != chapter or review.chapter != chapter):
                raise ChapterCommitConflict("review does not belong to this project and chapter")
            return {"project_id": project_id, "book_id": current["book_id"], "chapter": chapter,
                "review_id": review.id, "review_state": review.state, "final_draft": draft.text,
                "expected_draft_revision": draft.revision, "expected_review_revision": review.revision,
                "expected_draft_sha256": draft.sha256, "expected_facts_sha256": review.facts_sha256,
                "publication_authorized": False}

    def recover(self, *, project_id):
        """Finish or roll back original transactions only; never prepare a chapter."""
        with self._owned_project(project_id) as context:
            current, connection, owner, owner_args, ownership, root, runtime, roots, tx_root = context
            self._reconcile_owned_persistence(tx_root, book_id=current["book_id"])
            verified = load_verified_reviewed_identity(root)
            publications = self.index.rebuild(project_id=project_id, root_map=roots,
                baseline_document_sha256=verified["identity"]["reviewed_state"]["baseline_document_sha256"],
                connection=connection)
            return {"recovery_verified": True, "publication_authorized": False,
                "next_chapter": verified["history"]["value"]["committed_through"] + 1,
                "publications": publications}

    @classmethod
    def _require_settled_persistence(cls, transaction_root):
        cls._require_no_recovery_required(transaction_root)
        pending = assert_safe_local_tree(transaction_root / "registry/pending")
        if pending.exists() and (not pending.is_dir() or any(pending.iterdir())):
            raise ChapterCommitRecoveryRequired("original chapter transaction requires recovery before preview")
        # A power loss can publish the journal before its pending entry. Preview
        # must not silently recover or overlook that interval.
        journals = assert_safe_local_tree(transaction_root / "journals")
        for journal in journals.iterdir() if journals.exists() else []:
            try:
                manifest = load_persistence_manifest_v2(assert_safe_local_tree(journal) / "manifest.json")
            except (OSError, ValueError, PersistenceV2Error) as exc:
                raise ChapterCommitRecoveryRequired("chapter journal cannot be verified; explicit recovery is required") from exc
            if manifest["state"] not in {"completed", "rolled_back"}:
                raise ChapterCommitRecoveryRequired("original chapter transaction requires recovery before preview")
        staging = assert_safe_local_tree(transaction_root / "staging")
        if staging.exists() and (not staging.is_dir() or any(staging.iterdir())):
            raise ChapterCommitRecoveryRequired("original chapter staging requires recovery before preview")

    def _original_unpublished(self, path, *, incoming, tx_root):
        value = json.loads(read_project_bytes(path.parent, path.name))
        if (set(value) != {"version", "run_id", "evidence", "ownership_sha256", "intent_sha256"}
                or value["version"] != INTENT_VERSION
                or value["intent_sha256"] != digest({k: v for k, v in value.items() if k != "intent_sha256"})):
            raise ChapterCommitIntegrityError("original commit intent is damaged")
        evidence = value["evidence"]
        if evidence != prepare_commit_request_evidence(preparation=evidence["preparation"], request=evidence["request"]):
            raise ChapterCommitIntegrityError("original commit intent evidence differs")
        if evidence["request"] != incoming:
            raise ChapterCommitConflict("idempotency key was reused for a different request")
        expected_run = f"commit_{incoming['idempotency_scope_sha256'][:32]}"
        if value["run_id"] != expected_run:
            raise ChapterCommitIntegrityError("original commit run binding differs")
        journal = assert_safe_local_tree(tx_root / "journals" / expected_run)
        state = "interrupted_before_stage"
        if (journal / "manifest.json").exists():
            manifest = load_persistence_manifest_v2(journal / "manifest.json")
            immutable = manifest["immutable"]
            targets = [t for t in immutable["targets"] if t["target_id"] == "reviewed_commit_request"]
            if (immutable["run_id"] != expected_run or immutable["book_id"] != incoming["book_id"]
                    or immutable["story_project_read_set"] != evidence["preparation"]["read_set"] or len(targets) != 1
                    or json.loads(_load_staged_content(journal, targets[0])) != evidence):
                raise ChapterCommitIntegrityError("original journal differs from its durable intent")
            if manifest["state"] != "rolled_back":
                raise ChapterCommitIntegrityError("unpublished original transaction is not safely rolled back")
            state = "rolled_back"
        # A retry reports this original failed/interrupted request. It never
        # silently creates another attempt after a lost response or power loss.
        return {"record_type": "reviewed_chapter_unpublished", "schema_version": "1.0", "book_id": incoming["book_id"],
            "run_id": expected_run, "request": incoming, "state": state, "committed": False,
            "publication_verified": False, "retry_requires_new_key": True}

    @staticmethod
    def _require_no_recovery_required(transaction_root):
        # Reconciliation reports only transactions processed by that call. An
        # already quarantined transaction is no longer pending, so a later
        # empty report cannot prove that the book is safe for another write.
        # Do not parse away malformed entries or touch retained repair evidence.
        directory = assert_safe_local_tree(transaction_root / "registry/recovery_required")
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ChapterCommitRecoveryRequired("original chapter transaction still requires recovery")

    def _reconcile_owned_persistence(self, transaction_root, *, book_id):
        # Both callers already hold machine ownership, Provider and DB locks.
        self._require_no_recovery_required(transaction_root)
        recovery = reconcile_pending_persistence_v2(transaction_root, expected_book_id=book_id)
        self._require_no_recovery_required(transaction_root)
        if not recovery["ok"]:
            raise ChapterCommitRecoveryRequired("original chapter transaction still requires recovery")
        # Generic discovery ignores unregistered invalid journals. This owned
        # workflow must retain them as blockers, including the publish->pending
        # crash interval, instead of treating an empty recovery report as proof.
        self._require_settled_persistence(transaction_root)

    @staticmethod
    def _require_persistence_roots(transaction_root, roots):
        registry = RootRegistryService(transaction_root).load()
        if (set(registry["roots"]) != set(roots)
                or any(Path(registry["roots"][name]["path"]).absolute() != path.absolute() for name, path in roots.items())):
            raise ChapterCommitIntegrityError("pending transaction roots differ from the owned registration")

    def _fault(self, event, payload):
        if self.fault_injector is not None:
            self.fault_injector(event, payload)
