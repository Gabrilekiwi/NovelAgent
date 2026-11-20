"""Shared commit service on actual v3 books, real SQLite and file transactions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest

from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_reviewed_chapter_commit import case, ready, PowerLoss
from workbench.book_ownership import BookOwnership, BookOwnershipError
from workbench.chapter_publication import ChapterPublicationService
from workbench.chapter_commits import ChapterCommitConflict, ChapterCommitIntegrityError


@pytest.fixture
def service_case(ready):
    machine = ready["root"].parent / "m"
    with BookOwnership(ready["project"].book_id, machine_store=machine).locked() as owner:
        with ready["db"].transaction() as connection:
            owner.enroll(root=ready["root"], database=ready["db"], project_id=ready["project"].id, connection=connection)
    service = ChapterPublicationService(preparations=ready["service"], machine_store=machine)
    return {**ready, "publication": service, "machine": machine}


def test_two_concurrent_commits_publish_one_logical_request(service_case):
    c = service_case
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(c["publication"].commit, **c["route"]) for _ in range(2)]
        first, second = [f.result(timeout=60) for f in futures]
    assert first == second and first["publication_verified"] and first["chapter"] == 2
    assert infer_next_chapter(c["root"]) == 3 and c["calls"] == [1]
    with c["db"].connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chapter_publication_index").fetchone()[0] == 1
    assert len(list((c["root"] / ".novelagent/runtime/reviewed/intents").glob("*.json"))) == 1
    assert len(list((c["root"] / ".novelagent/runtime/reviewed/receipts").glob("*.json"))) == 2
    with pytest.raises(ChapterCommitConflict, match="reused"):
        c["publication"].commit(**{**c["route"], "expected_draft_revision": 2})


@pytest.mark.parametrize("event,committed,state", [
    ("after_intent", False, "interrupted_before_stage"),
    ("after_prepare", False, "rolled_back"),
    ("after_file_commit_before_index", True, None),
    ("before_db_commit", True, None),
])
def test_lost_response_or_process_retries_original_request(service_case, event, committed, state):
    c = service_case
    def crash(name, payload):
        if name == event:
            raise PowerLoss()
    c["publication"].fault_injector = crash
    with pytest.raises(PowerLoss):
        c["publication"].commit(**c["route"])
    c["publication"].fault_injector = None
    original_prepare = c["service"].prepare
    def deny(**kwargs):
        pytest.fail("retry must identify the original request before mutable draft preparation")
    c["service"].prepare = deny
    result = c["publication"].commit(**c["route"])
    assert c["publication"].commit(**c["route"]) == result
    c["service"].prepare = original_prepare
    if committed:
        assert result["publication_verified"]
    else:
        assert result["committed"] is False and result["state"] == state and result["retry_requires_new_key"]
        with pytest.raises(ChapterCommitConflict, match="reused"):
            c["publication"].commit(**{**c["route"], "expected_review_revision": c["route"]["expected_review_revision"] + 1})
    assert infer_next_chapter(c["root"]) == (3 if committed else 2) and c["calls"] == [1]


@pytest.mark.parametrize("event,index,committed", [("after_apply_target", 0, False), ("after_commit_marker", None, True)])
def test_service_recovers_partial_state_before_reading_current_identity(service_case, event, index, committed):
    c = service_case
    def crash(name, position, path):
        if (name, position) == (event, index):
            raise PowerLoss()
    c["publication"].persistence_fault_injector = crash
    with pytest.raises(PowerLoss):
        c["publication"].commit(**c["route"])
    c["publication"].persistence_fault_injector = None
    result = c["publication"].commit(**c["route"])
    assert result.get("publication_verified", False) is committed
    assert infer_next_chapter(c["root"]) == (3 if committed else 2) and c["calls"] == [1]


def test_changed_author_draft_is_rejected_inside_commit_transaction(service_case):
    c = service_case
    c["drafts"].update(c["draft"].id, text=c["draft"].text + "新修订。", expected_revision=c["draft"].revision)
    before = (c["root"] / RELATIVE_PATH).read_bytes()
    with pytest.raises(ChapterCommitConflict, match="changed"):
        c["publication"].commit(**c["route"])
    assert (c["root"] / RELATIVE_PATH).read_bytes() == before and infer_next_chapter(c["root"]) == 2


def test_missing_machine_ownership_does_not_auto_enroll_or_publish(ready):
    service = ChapterPublicationService(preparations=ready["service"], machine_store=ready["root"].parent / "unowned")
    with pytest.raises(BookOwnershipError, match="not been enrolled"):
        service.commit(**ready["route"])
    assert infer_next_chapter(ready["root"]) == 2


def test_commit_preparation_reuses_one_connection_and_blocks_concurrent_database_writes(service_case):
    c = service_case
    observed = []
    original = c["service"].prepare
    def verify_connection(**kwargs):
        connection = kwargs["connection"]
        assert connection.in_transaction
        if not observed:
            with c["db"].connection() as competing:
                competing.execute("PRAGMA busy_timeout=20")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute("UPDATE chapter_candidate_drafts SET revision=revision+1 WHERE id=?", (c["draft"].id,))
        observed.append(connection)
        return original(**kwargs)
    c["service"].prepare = verify_connection
    assert c["publication"].commit(**c["route"])["publication_verified"]
    assert len(observed) >= 2 and all(conn is observed[0] for conn in observed)
    assert c["drafts"].get(c["draft"].id).revision == c["draft"].revision


def test_ownership_changes_during_request_prevent_publication(service_case):
    from core.engine.persistence import atomic_write_json
    from core.story_project.reviewed_state_document import digest
    c = service_case
    def change(event, payload):
        if event == "before_commit":
            owner = BookOwnership(c["project"].book_id, machine_store=c["machine"])
            value = json.loads(owner.record_path.read_text(encoding="utf-8"))
            value["generation"] += 1
            value["record_sha256"] = digest({k: v for k, v in value.items() if k != "record_sha256"})
            atomic_write_json(owner.record_path, value)
    c["publication"].fault_injector = change
    with pytest.raises(ChapterCommitConflict, match="ownership"):
        c["publication"].commit(**c["route"])
    assert infer_next_chapter(c["root"]) == 2
    c["publication"].fault_injector = None
    assert c["publication"].commit(**c["route"])["state"] == "rolled_back"


def test_source_changes_after_staging_are_rechecked_before_file_commit(service_case):
    from workbench.chapter_facts import ChapterFactReviewConflict
    c = service_case
    source = c["root"] / "追踪/上下文.md"
    def change(event, payload):
        if event == "before_commit":
            source.write_bytes(source.read_bytes() + b"\nmanual edit")
    before = (c["root"] / RELATIVE_PATH).read_bytes()
    c["publication"].fault_injector = change
    with pytest.raises(ChapterFactReviewConflict, match="trusted chapter sources changed"):
        c["publication"].commit(**c["route"])
    assert (c["root"] / RELATIVE_PATH).read_bytes() == before and infer_next_chapter(c["root"]) == 2
    c["publication"].fault_injector = None
    assert c["publication"].commit(**c["route"])["state"] == "rolled_back"
    assert source.read_bytes().endswith(b"manual edit")


def test_unresolved_provider_blocks_publication_without_touching_formal_state(service_case, monkeypatch):
    import workbench.chapter_publication as module
    c = service_case
    monkeypatch.setattr(module, "list_unresolved_provider_calls", lambda _: [{"call_id": "unresolved"}])
    before = (c["root"] / RELATIVE_PATH).read_bytes()
    with pytest.raises(ChapterCommitConflict, match="unresolved Provider"):
        c["publication"].commit(**c["route"])
    assert (c["root"] / RELATIVE_PATH).read_bytes() == before and c["calls"] == [1]


def test_corrupted_intent_cannot_be_treated_as_a_fresh_request(service_case):
    from workbench.chapter_commits import ChapterCommitIntegrityError
    c = service_case
    def crash(event, payload):
        if event == "after_intent":
            raise PowerLoss()
    c["publication"].fault_injector = crash
    with pytest.raises(PowerLoss):
        c["publication"].commit(**c["route"])
    c["publication"].fault_injector = None
    path = next((c["root"] / ".novelagent/runtime/reviewed/intents").glob("*.json"))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["ownership_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ChapterCommitIntegrityError, match="damaged"):
        c["publication"].commit(**c["route"])
    assert infer_next_chapter(c["root"]) == 2 and c["calls"] == [1]


def test_persisted_recovery_failure_blocks_repeated_and_new_requests(service_case):
    c = service_case
    def crash(event, payload):
        if event == "after_prepare":
            raise PowerLoss()
    c["publication"].fault_injector = crash
    with pytest.raises(PowerLoss):
        c["publication"].commit(**c["route"])
    c["publication"].fault_injector = None
    runtime = c["root"] / ".novelagent/runtime"
    tx_root = runtime / "reviewed/p"
    journal = next(path for path in (tx_root / "journals").iterdir() if path.name.startswith("commit_"))
    # Use a real staged transaction with damaged evidence, not a fabricated
    # recovery report. The first retry must durably quarantine the failure.
    damaged = journal / "candidate_result.json"
    damaged.write_bytes(b"{}")
    with pytest.raises(ChapterCommitIntegrityError, match="requires recovery"):
        c["publication"].commit(**c["route"])
    recovery_dir = tx_root / "registry/recovery_required"
    assert len(list(recovery_dir.glob("*.json"))) == 1
    retained = {path: path.read_bytes() for base in (journal, recovery_dir)
                for path in base.rglob("*") if path.is_file()}
    state_before = (c["root"] / RELATIVE_PATH).read_bytes()
    original_intents = {path.name for path in (runtime / "reviewed/intents").iterdir()}
    original_receipts = {path.name for path in (runtime / "reviewed/receipts").iterdir()}
    for key in (c["route"]["idempotency_key"], c["route"]["idempotency_key"], "new-after-recovery-failure"):
        with pytest.raises(ChapterCommitIntegrityError, match="requires recovery"):
            c["publication"].commit(**{**c["route"], "idempotency_key": key})
        assert {path: path.read_bytes() for base in (journal, recovery_dir)
                for path in base.rglob("*") if path.is_file()} == retained
        assert (c["root"] / RELATIVE_PATH).read_bytes() == state_before
        assert infer_next_chapter(c["root"]) == 2
    assert damaged.read_bytes() == b"{}" and c["calls"] == [1]
    assert {path.name for path in (runtime / "reviewed/intents").iterdir()} == original_intents
    assert {path.name for path in (runtime / "reviewed/receipts").iterdir()} == original_receipts
    with c["db"].connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chapter_publication_index").fetchone()[0] == 0


def test_malformed_persistent_recovery_entry_is_preserved_and_blocks_publication(service_case):
    c = service_case
    directory = c["root"] / ".novelagent/runtime/reviewed/p/registry/recovery_required"
    directory.mkdir(parents=True, exist_ok=True)
    damaged = directory / "unreadable-entry"
    damaged.write_bytes(b"not a registry record")
    with pytest.raises(ChapterCommitIntegrityError, match="requires recovery"):
        c["publication"].commit(**c["route"])
    assert damaged.read_bytes() == b"not a registry record"
    assert infer_next_chapter(c["root"]) == 2 and c["calls"] == [1]


def test_fresh_process_rebuilds_missing_commit_index_without_preparing_or_generating(service_case):
    c = service_case
    def crash(event, payload):
        if event == "after_file_commit_before_index":
            raise PowerLoss()
    c["publication"].fault_injector = crash
    with pytest.raises(PowerLoss):
        c["publication"].commit(**c["route"])
    request_file = c["root"].parent / "retry.json"
    request_file.write_text(json.dumps(c["route"]), encoding="utf-8")
    code = """import json,sys
from pathlib import Path
from workbench.database import WorkbenchDatabase
from workbench.reviewed_projects import ReviewedProjectRegistry
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_facts import ChapterFactReviewService
from workbench.chapter_commits import ChapterCommitPreparationService
from workbench.chapter_publication import ChapterPublicationService
db=WorkbenchDatabase(sys.argv[1]); root=Path(sys.argv[2])
projects=ReviewedProjectRegistry(db, allowed_workspaces=[root.parent]); drafts=CandidateDraftStore(db)
reviews=ChapterFactReviewService(database=db, projects=projects, candidate_drafts=drafts)
preparations=ChapterCommitPreparationService(database=db, projects=projects, candidate_drafts=drafts, fact_reviews=reviews)
def deny(**kwargs):
    raise AssertionError('published retry attempted mutable draft preparation')
preparations.prepare=deny
service=ChapterPublicationService(preparations=preparations, machine_store=Path(sys.argv[3]))
print(json.dumps(service.commit(**json.loads(Path(sys.argv[4]).read_text(encoding='utf-8'))),ensure_ascii=True))
"""
    child = subprocess.run([sys.executable, "-B", "-c", code, str(c["db"].path), str(c["root"]),
        str(c["machine"]), str(request_file)], capture_output=True, text=True, timeout=45, check=True)
    result = json.loads(child.stdout)
    assert result["publication_verified"] and result["chapter"] == 2
    with c["db"].connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chapter_publication_index").fetchone()[0] == 1
    assert c["calls"] == [1]
