"""Real file publication -> disposable DB index, using isolated synthetic books."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.engine.chapter_commit_request import (
    ChapterCommitRequestError, chapter_commit_request, prepare_commit_request_evidence,
)
from core.engine.persistence_v2 import (
    PersistenceV2Target, PersistenceV2Transaction, bind_final_run_record_receipt,
    reconcile_pending_persistence_v2, verify_publication_receipt,
)
from core.engine.reviewed_upgrade import json_bytes
from core.path_refs import path_ref_for
from core.story_project.paths import canonical_prose_path
from core.story_project.read_set import declared_read_set_writes
from core.story_project.reviewed_state_document import (
    RELATIVE_PATH, ReviewedStateDocumentError, decode_document, digest, prepare_chapter_document,
)
from core.story_project.reviewed_state_store import source_revision_after
from tests.test_workbench_chapter_commits import case
from workbench.chapter_commit_index import ChapterCommitIndex
from workbench.chapter_commits import ChapterCommitConflict, ChapterCommitIntegrityError


pytestmark = pytest.mark.parametrize("case", ["formal"], indirect=True)


class PowerLoss(BaseException):
    pass


def publish_request(case, *, change=None, fault=None):
    """Transaction fixture, not the pending product quality/commit entrypoint."""
    prepared = case["service"].prepare(**case["request"])
    formal = case["formal_case"]
    root, runtime, book_id = formal["root"], formal["runtime"], formal["book_id"]
    run_id = "chapter-3"
    request = chapter_commit_request(book_id=book_id, request=case["request"])
    evidence = prepare_commit_request_evidence(preparation=prepared, request=request)
    document = prepare_chapter_document(document=formal["document"], book_id=book_id,
                                         preparation=prepared, run_id=run_id)
    value = decode_document(document, book_id=book_id)
    prose = prepared["final_prose"].encode("utf-8")
    def ref(relative, root_id):
        return path_ref_for(formal["roots"][root_id] / relative, root_id=root_id, root=formal["roots"][root_id])
    prose_path = canonical_prose_path(root, 3)
    apply = [
        PersistenceV2Target(target_id="reviewed_state", kind="tracking", path_ref=ref(RELATIVE_PATH, "story_project"),
            content=document, expected_before_exists=True, expected_before_sha256=digest(formal["document"])),
        PersistenceV2Target(target_id="reviewed_chapter", kind="story_project_update",
            path_ref=ref(prose_path.relative_to(root).as_posix(), "story_project"), content=prose,
            expected_before_exists=False),
    ]
    head = value["head"]
    receipt_ref = ref(head["receipt_relative_path"], "runtime")
    final = bind_final_run_record_receipt({"record_type": "reviewed_legacy_commit", "id": run_id,
        "book_id": book_id, "kind": "chapter", "revision": value["revision"], "committed": True,
        "request_sha256": prepared["request_sha256"], "client_request_sha256": request["client_request_sha256"],
        "idempotency_scope_sha256": request["idempotency_scope_sha256"],
        "request_evidence_sha256": evidence["evidence_sha256"]},
        receipt_id=head["receipt_id"], receipt_path_ref=receipt_ref)
    options = {"evidence": evidence, "request_path": f"reviewed/requests/{run_id}.json", "final": final,
               "context_digest": prepared["binding"]["context_sha256"], "include_request": True}
    if change:
        change(options)
    artifacts = [PersistenceV2Target(target_id=target_id, kind="review_artifact", phase="publication",
        path_ref=ref(relative, "runtime"), content=content) for target_id, relative, content in (
            ("reviewed_state_archive", f"reviewed/states/{run_id}.md", document),
            ("reviewed_chapter_archive", f"reviewed/chapters/{run_id}.md", prose))]
    if options["include_request"]:
        artifacts.append(PersistenceV2Target(target_id="reviewed_commit_request", kind="review_artifact", phase="publication",
            path_ref=ref(options["request_path"], "runtime"), content=json_bytes(options["evidence"])))
    declared = declared_read_set_writes(prepared["read_set"], [(root / t.path_ref.relative_path,
        digest(t.content_bytes()), len(t.content_bytes())) for t in apply])
    tx = PersistenceV2Transaction(transaction_root=runtime / "reviewed/p", run_id=run_id, book_id=book_id,
        root_map=formal["roots"], story_project_read_set=prepared["read_set"], read_set_declared_writes=declared,
        fault_injector=fault)
    tx.prepare(apply_targets=apply, artifacts=artifacts, final_run_record=options["final"],
        final_run_path_ref=ref(f"reviewed/runs/{run_id}.json", "runtime"), receipt_id=head["receipt_id"],
        receipt_path_ref=receipt_ref, context_digest=options["context_digest"],
        generation_input_context_digest=prepared["binding"]["context_sha256"],
        story_project_source_revision_after=source_revision_after(document, book_id=book_id),
        candidate_result={"request_evidence_sha256": evidence["evidence_sha256"]})
    case["publication_preparation"] = prepared
    return tx.commit()


def index(case):
    return ChapterCommitIndex(database=case["database"], projects=case["projects"])


def args(case):
    return {"project_id": case["project"].id, "root_map": case["formal_case"]["roots"],
            "baseline_document_sha256": digest(case["formal_case"]["document"])}


def rows(case):
    with case["database"].connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM chapter_publication_index")]


def test_real_receipt_rebuild_and_retry_after_cursor_and_draft_advance(case):
    store = index(case)
    assert store.find_published(**args(case), request=case["request"]) is None
    assert publish_request(case)["committed"]
    assert rows(case) == []  # Simulate crash after file commit but before SQLite update.
    draft = case["draft"]
    changed = case["drafts"].update(draft.id, text=draft.text + "作者事后改稿。", expected_revision=draft.revision)
    first = store.find_published(**args(case), request=case["request"])
    assert first["publication_verified"] and first["chapter"] == 3
    assert first["request"]["request"]["expected_draft_revision"] == draft.revision
    assert case["drafts"].get(draft.id) == changed
    assert store.find_published(**args(case), request=case["request"]) == first
    assert len(rows(case)) == 1 and len(case["calls"]) == 1
    assert "cp3-commit" not in (case["formal_case"]["runtime"] / "reviewed/requests/chapter-3.json").read_text(encoding="utf-8")


def test_persisted_author_review_and_formal_context_feed_quality_and_request_without_publication(case):
    from core.engine.reviewed_chapter_quality import prepare_reviewed_chapter_quality
    from scripts.experiments.run_d2_b0_compatibility import inventory
    before = inventory(case["root"])
    prepared = case["service"].prepare(**case["request"])
    quality = prepare_reviewed_chapter_quality(trusted_context=case["builder"](case["root"]).build(3),
                                              chapter_preparation=prepared)
    evidence = prepare_commit_request_evidence(preparation=prepared,
        request=chapter_commit_request(book_id=case["project"].book_id, request=case["request"]))
    assert quality["binding"]["chapter_preparation_sha256"] == evidence["preparation"]["preparation_sha256"]
    assert quality["binding"]["confirmation_sha256"] == case["confirmed"]["confirmation_sha256"]
    assert quality["pending_gates"] == ["candidate_scene_and_blueprint_evidence"]
    assert quality["publication_authorized"] is False and prepared["publication_authorized"] is False
    assert inventory(case["root"]) == before and len(case["calls"]) == 1


@pytest.mark.parametrize("field,value", [("chapter", 4), ("expected_draft_revision", 2),
    ("expected_review_revision", 99), ("expected_draft_sha256", "0" * 64), ("expected_facts_sha256", "1" * 64),
    ("review_id", "another-review")])
def test_same_key_with_another_request_conflicts_without_repreparing(case, field, value):
    publish_request(case)
    store = index(case)
    with pytest.raises(ChapterCommitConflict, match="reused"):
        store.find_published(**args(case), request={**case["request"], field: value})
    assert len(case["calls"]) == 1


def test_index_corruption_and_loss_are_repaired_but_drafts_remain_db_native(case):
    publish_request(case)
    store = index(case)
    expected = store.rebuild(**args(case))
    original_draft = case["drafts"].get(case["draft"].id)
    with case["database"].transaction() as connection:
        connection.execute("UPDATE chapter_publication_index SET publication_json = '{}', receipt_hash = 'bad'")
    assert store.rebuild(**args(case)) == expected
    assert json.loads(rows(case)[0]["publication_json"]) == expected[0]
    with case["database"].transaction() as connection:
        connection.execute("DELETE FROM chapter_publication_index")
    assert store.find_published(**args(case), request=case["request"]) == expected[0]
    assert case["drafts"].get(original_draft.id) == original_draft and len(case["calls"]) == 1


def test_cold_process_rebuild_uses_archive_without_candidate_or_draft_loading(case):
    publish_request(case)
    store = index(case)
    # A fresh process starts with an empty index. No model factory is installed.
    code = """import json,sys
from pathlib import Path
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry
from workbench.chapter_commit_index import ChapterCommitIndex
db=WorkbenchDatabase(sys.argv[1]); root=Path(sys.argv[2]); projects=ProjectRegistry(db,allowed_workspaces=[root.parent])
index=ChapterCommitIndex(database=db,projects=projects)
print(json.dumps(index.rebuild(project_id=sys.argv[3],root_map={'story_project':root,'runtime':root/'.novelagent/runtime'},baseline_document_sha256=sys.argv[4]),ensure_ascii=True))
"""
    result = subprocess.run([sys.executable, "-B", "-c", code, str(case["database"].path), str(case["root"]),
        case["project"].id, args(case)["baseline_document_sha256"]], cwd=Path.cwd(), capture_output=True,
        text=True, timeout=35, check=True)
    assert json.loads(result.stdout)[0]["chapter"] == 3
    assert len(rows(case)) == 1 and len(case["calls"]) == 1


@pytest.mark.parametrize("relative", ["reviewed/requests/chapter-3.json", "reviewed/receipts/chapter-3.json"])
def test_broken_disk_history_is_not_hidden_by_a_previous_database_success(case, relative):
    publish_request(case)
    store = index(case)
    store.rebuild(**args(case))
    before = rows(case)
    (case["formal_case"]["runtime"] / relative).write_bytes(b"{}")
    with pytest.raises((ValueError, RuntimeError)):
        store.find_published(**args(case), request=case["request"])
    assert rows(case) == before


@pytest.mark.parametrize("mutation", ["missing", "empty", "wrong_path", "wrong_context", "wrong_final"])
def test_valid_generic_receipt_does_not_prove_request_binding(case, mutation):
    def change(options):
        if mutation == "missing":
            options["include_request"] = False
        elif mutation == "empty":
            options["evidence"] = {}
        elif mutation == "wrong_path":
            options["request_path"] = "reviewed/requests/other-run.json"
        elif mutation == "wrong_context":
            options["context_digest"] = "f" * 64
        else:
            options["final"]["client_request_sha256"] = "e" * 64
    publish_request(case, change=change)
    formal = case["formal_case"]
    assert verify_publication_receipt(formal["runtime"] / "reviewed/receipts/chapter-3.json", root_map=formal["roots"])["valid"]
    store = index(case)
    with pytest.raises((ChapterCommitRequestError, ReviewedStateDocumentError)):
        store.rebuild(**args(case))
    assert rows(case) == []


def test_request_revision_mismatch_cannot_be_hidden_by_rehashing_evidence(case):
    p = case["service"].prepare(**case["request"])
    request = chapter_commit_request(book_id=case["project"].book_id,
                                    request={**case["request"], "expected_draft_revision": 10})
    with pytest.raises(ChapterCommitRequestError, match="differs"):
        prepare_commit_request_evidence(preparation=p, request=request)


@pytest.mark.parametrize("point", ["after_apply_target", "after_commit_marker"])
def test_recovery_boundary_controls_index_without_regeneration(case, point):
    store = index(case)
    def fault(event, position, path):
        if event == point and (point != "after_apply_target" or position == 1):
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        publish_request(case, fault=fault)
    formal = case["formal_case"]
    with pytest.raises((ValueError, RuntimeError)):
        store.find_published(**args(case), request=case["request"])
    assert rows(case) == []
    result = reconcile_pending_persistence_v2(formal["runtime"] / "reviewed/p", expected_book_id=formal["book_id"])
    assert result["ok"]
    found = store.find_published(**args(case), request=case["request"])
    if point == "after_commit_marker":
        assert found["run_id"] == "chapter-3" and found["publication_verified"]
    else:
        assert found is None and (case["root"] / RELATIVE_PATH).read_bytes() == formal["document"]
        assert not canonical_prose_path(case["root"], 3).exists()
    assert len(case["calls"]) == 1


def test_external_database_transaction_owns_rebuild_commit(case):
    publish_request(case)
    store = index(case)
    with pytest.raises(PowerLoss):
        with case["database"].transaction() as connection:
            assert store.rebuild(**args(case), connection=connection)
            raise PowerLoss()
    assert rows(case) == []
    assert store.rebuild(**args(case))


@pytest.mark.parametrize("kind", ["autocommit", "another_database"])
def test_external_connection_cannot_partially_replace_index(case, kind):
    from workbench.database import WorkbenchDatabase
    store = index(case)
    db = case["database"]
    if kind == "another_database":
        db = WorkbenchDatabase(case["root"].parent / "other.db")
        db.initialize()
    manager = db.transaction() if kind == "another_database" else db.connection()
    with manager as connection:
        with pytest.raises(ChapterCommitIntegrityError, match="transaction"):
            store.rebuild(**args(case), connection=connection)
    assert rows(case) == []


def test_cross_registration_or_root_cannot_use_book_publication(case):
    publish_request(case)
    store = index(case)
    with pytest.raises(ChapterCommitConflict, match="another project"):
        store.find_published(**args(case), request={**case["request"], "project_id": "another"})
    roots = {**case["formal_case"]["roots"], "story_project": case["root"].parent}
    with pytest.raises(ChapterCommitIntegrityError, match="roots"):
        store.rebuild(**{**args(case), "root_map": roots})
