"""Observe copy/restore barriers without changing any production guards."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.engine.persistence_v2 import PersistenceV2Target, PersistenceV2Transaction, bind_final_run_record_receipt, verify_publication_receipt
from core.engine.persistence import LocalPersistenceTransaction, PersistenceTarget, load_persistence_candidate
from core.engine.project_root_remap import remap_story_project_roots
from core.engine.root_registry import RootRegistryError, RootRegistryService
from core.engine.safe_paths import PathGuardMismatchError, RootBinding, SafePathResolver
from core.path_refs import path_ref_for
from core.story_project.identity import load_project_identity
from core.story_project.read_set import capture_story_project_read_set, declared_read_set_writes
from scripts.experiments.run_d2_b0_compatibility import digest, fixture, inventory, save
from workbench.database import WorkbenchDatabase
from workbench.jobs import JobStore
from workbench.projects import ProjectRegistry


def root_map(book: Path) -> dict[str, Path]:
    runtime = book / ".novelagent/runtime"
    return {"story_project": book, "runtime": runtime, "snapshot": runtime,
            "chapter_artifacts": runtime / "chapters"}


def run() -> dict:
    directory = REPO / ".tmp/stage-b/d3-baseline" / uuid.uuid4().hex
    source = directory / "source/book"
    fixture(source, "control")
    identity = json.loads((source / ".novelagent/project.json").read_text(encoding="utf-8"))
    roots = root_map(source)
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    snapshot = roots["runtime"] / "snapshot.json"
    save(snapshot, {"book_id": identity["book_id"], "chapter_index": 2})
    transaction_root = roots["runtime"] / "persistence"
    run_id = "d3-baseline-chapter-2"
    receipt_path = roots["runtime"] / "receipts" / f"{run_id}.json"
    receipt_ref = path_ref_for(receipt_path, root_id="runtime", root=roots["runtime"])
    final_ref = path_ref_for(roots["runtime"] / "runs" / f"{run_id}.json", root_id="runtime", root=roots["runtime"])
    final = bind_final_run_record_receipt(
        {"id": run_id, "book_id": identity["book_id"], "chapter_index": 2, "status": "committed", "committed": True},
        receipt_id="d3-baseline-receipt", receipt_path_ref=receipt_ref)
    prose = "林澈把信标交给周岚，两人决定守住旧城站的闸门。\n"
    prose_path = source / "正文/第002章_D3隔离提交.md"
    read_set = capture_story_project_read_set(source, 2, project_identity=load_project_identity(source))
    declared = declared_read_set_writes(read_set, [(prose_path, digest(prose.encode()), len(prose.encode()))])
    transaction = PersistenceV2Transaction(transaction_root=transaction_root, run_id=run_id,
                                         book_id=identity["book_id"], root_map=roots,
                                         story_project_read_set=read_set, read_set_declared_writes=declared)
    manifest = transaction.prepare(
        apply_targets=[
            PersistenceV2Target(target_id="snapshot", kind="snapshot",
                path_ref=path_ref_for(snapshot, root_id="snapshot", root=roots["runtime"]),
                content=json.dumps({"book_id":identity["book_id"],"chapter_index":3})+"\n",
                expected_before_exists=True, expected_before_sha256=digest(snapshot.read_bytes())),
            PersistenceV2Target(target_id="formal_prose", kind="story_project_prose",
                path_ref=path_ref_for(prose_path, root_id="story_project", root=source),
                content=prose, expected_before_exists=False),
        ],
        artifacts=[PersistenceV2Target(target_id="chapter", kind="chapter_artifact", phase="publication",
                path_ref=path_ref_for(roots["chapter_artifacts"] / "chapter_2.md", root_id="chapter_artifacts", root=roots["chapter_artifacts"]),
                content=prose)],
        final_run_record=final, final_run_path_ref=final_ref, receipt_id="d3-baseline-receipt",
        receipt_path_ref=receipt_ref, context_digest=digest(b"synthetic-context"),
        generation_input_context_digest=digest(b"synthetic-generation"),
        story_project_source_revision_after={"chapter":2}, candidate_result={"run":{"id":run_id}})
    committed = transaction.commit()
    assert committed["committed"] is True
    source_verification = verify_publication_receipt(receipt_path, root_map=roots)
    assert source_verification["valid"] is True
    # Representative old absolute-path journal, separate from the v2.1 chapter.
    # Do not pass its copied journal into reconciliation: it still addresses the source.
    legacy = LocalPersistenceTransaction(run_dir=roots["runtime"] / "legacy-runs", run_id="d3-v1-history",
        allowed_roots=[source], book_id=identity["book_id"])
    legacy.prepare([PersistenceTarget("legacy_artifact", roots["runtime"] / "legacy-output.md", "旧事务保留材料。\n")],
                   candidate_result={"run":{"id":"d3-v1-history","status":"committed"}})
    legacy.commit()
    legacy_result = legacy.complete_publication()
    assert legacy_result.committed is True

    database = WorkbenchDatabase(directory / "source/workbench.db")
    registry = ProjectRegistry(database, allowed_workspaces=[directory])
    project = registry.import_project(source, name="D3 隔离恢复基线")
    jobs = JobStore(database)
    draft = jobs.upsert_draft(project_id=project.id, chapter=3, text="作者草稿：周岚发现闸门背后的新线索。")
    before = inventory(source)
    restored = directory / "restored/book"
    restored.parent.mkdir()
    shutil.copytree(source, restored)
    assert inventory(restored) == before
    copied_database = directory / "restored/workbench.db"
    with sqlite3.connect(database.path) as origin, sqlite3.connect(copied_database) as target:
        origin.backup(target)
    copied_jobs = JobStore(WorkbenchDatabase(copied_database))
    assert copied_jobs.get_draft(draft.id).to_dict(include_text=True) == draft.to_dict(include_text=True)
    # Stored registry still points to the original book: do not schedule via it.
    with sqlite3.connect(copied_database) as connection:
        retained_root = connection.execute("SELECT root FROM projects WHERE id=?", (project.id,)).fetchone()[0]
    assert retained_root == str(source)
    copied_legacy = restored / Path(legacy_result.journal_path).relative_to(source)
    assert load_persistence_candidate(copied_legacy)["run"]["id"] == "d3-v1-history"
    legacy_manifest = json.loads((copied_legacy / "manifest.json").read_text(encoding="utf-8"))
    old_absolute_targets = [item["path"] for item in legacy_manifest["targets"]]
    assert all(Path(value).is_relative_to(source) for value in old_absolute_targets)
    restored_roots = root_map(restored)
    before_registry = RootRegistryService(transaction_root).load()
    copied_receipt = restored / receipt_path.relative_to(source)
    copied_verification = verify_publication_receipt(copied_receipt, root_map=restored_roots)
    assert copied_verification["valid"] is False
    requested = {key:{"path":str(restored_roots[key]),"root_uuid":binding["root_uuid"]}
                 for key,binding in before_registry["roots"].items()}
    try:
        remap_story_project_roots(new_story_project=restored,
            control_plane=restored_roots["runtime"] / "persistence", requested=requested,
            expected_revision=before_registry["revision"], expected_registry_digest=before_registry["registry_digest"])
    except RootRegistryError as exc:
        remap_error = str(exc)
        assert "old StoryProject path still exists" in remap_error
    else:
        raise AssertionError("copy was accepted as rename")
    resolver = SafePathResolver({key:RootBinding(key,before_registry["roots"][key]["root_uuid"],value)
                                 for key,value in restored_roots.items()})
    old_target = manifest["immutable"]["targets"][0]
    try:
        resolver.resolve(old_target["path_ref"], expected_guard=old_target["path_guard"])
    except PathGuardMismatchError as exc:
        guard_error = str(exc)
    else:
        raise AssertionError("copied directory retained the old preparation permit")
    assert inventory(source) == inventory(restored) == before
    report = {"directory":str(directory), "status":"baseline_observed_candidate_pending",
              "source_book":str(source), "restored_book":str(restored), "root_map":{k:str(v) for k,v in roots.items()},
              "book_id":identity["book_id"], "source_receipt":source_verification,
              "copied_receipt":copied_verification,"remap_error":remap_error,"old_prepare_guard_error":guard_error,
              "source_and_copy_unchanged":True,"book_files":before,"draft":draft.to_dict(include_text=True),
              "copied_database_registered_root":retained_root,"copy_enabled_for_writing":False,
              "formal_transaction_exercised":"v2.1", "agent_executor_exercised":False,
              "legacy_v1_absolute_targets":old_absolute_targets,
              "legacy_v1_candidate_bytes_readable":True,"legacy_v1_reconcile_not_called_on_copy":True,
              "legacy_v1_restore_binding_pending":True,"ownership_handoff_pending":True,
              "provider_calls":0,"real_book_writes":0}
    save(directory / "report.json", report)
    return {key:value for key,value in report.items() if key not in {"book_files","draft","root_map"}}


if __name__ == "__main__":
    print(json.dumps(run(),ensure_ascii=False,indent=2))
