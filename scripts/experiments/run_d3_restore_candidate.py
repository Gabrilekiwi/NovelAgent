"""Run D3 candidate 1 against real local executor and fresh isolated processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.engine.safe_paths import PathGuardMismatchError, RootBinding, SafePathResolver
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from scripts.experiments.d3_restore_candidate import MachineBook, RestoreRejected, read, tree, rebind_v2_completed_baseline
from scripts.experiments.run_d2_b0_compatibility import fixture, save
from workbench.database import WorkbenchDatabase
from workbench.instance import InstanceAlreadyRunning
from workbench.jobs import JobStore
from workbench.projects import ProjectRegistry


def fresh(command: list[str], output: Path, *, expected: int = 0) -> dict:
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    process = subprocess.run([sys.executable, "-B", *command], cwd=REPO, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=40)
    record = {"returncode":process.returncode, "command":command,
              "stdout":process.stdout, "stderr":process.stderr}
    save(output, record)
    assert process.returncode == expected, record
    return record


def setup(directory: Path) -> dict:
    directory.mkdir(parents=True)
    source = directory / "source/book"
    fixture(source, "control")
    identity_path = source / ".novelagent/project.json"
    identity = read(identity_path)
    identity["book_id"] = "synthetic-d3-" + uuid.uuid4().hex
    save(identity_path, identity)
    for chapter, title in ((2,"Second"),(3,"Third")):
        canonical_outline_path(source, chapter).write_text(
            f"# {title}\n\ncore_event: danger forces a costly route choice\n\n## required_beats\n"
            "- danger forces the route choice\n- open conflict over the serum\n\n"
            "ending_pressure: the locked door starts a countdown\n", encoding="utf-8")
    runtime = source / ".novelagent/runtime"
    (runtime / "runs").mkdir(parents=True)
    save(runtime / "snapshot.json", {"book_id":identity["book_id"],"chapter_index":2,
        "characters":{},"world_state":{"infection_level":"medium","locations":{}},
        "timeline":[],"project_profile":{"language":"en"}})
    database = WorkbenchDatabase(directory / "source/workbench.db")
    registry = ProjectRegistry(database, allowed_workspaces=[directory])
    project = registry.import_project(source)
    jobs = JobStore(database)
    draft = jobs.upsert_draft(project_id=project.id, chapter=3, text="作者草稿：周岚发现闸门背后的新线索。")
    # An unrelated registry is retained by full DB backup, but never permitted.
    other = registry.create_project(directory / "source/other-book", name="Other isolated book")
    machine = directory / "machine"
    machine.mkdir()
    owner = MachineBook(machine, identity["book_id"])
    with owner.locked():
        owner.claim_source(source, database.path, project.id)
    (directory / "restored").mkdir()
    config = {"directory":str(directory), "book":str(source), "database":str(database.path),
        "machine_store":str(machine), "project_id":project.id, "book_id":identity["book_id"],
        "generation":1, "draft":draft.to_dict(include_text=True), "other_project_id":other.id}
    save(directory / "source-config.json", config)
    run_executor(config, directory / "source-executor.json")
    return config


def run_executor(config: dict, output: Path, *, expected: int = 0, rejection: str | None = None) -> dict:
    record = fresh(["scripts/experiments/d3_executor_probe.py", "--book",config["book"],
        "--machine-store",config["machine_store"],"--database",config["database"],
        "--project-id",config["project_id"],"--generation",str(config["generation"]),
        "--output",str(output)], output.with_suffix(".process.json"), expected=expected)
    if expected == 0:
        value = read(output)
        assert value["result"]["committed"] is True
        assert value["contexts"][0]["semantic_audit"]["effective_mode"] == "strict"
        return value
    assert rejection and rejection in record["stderr"], record
    return record


def expect_reject(function) -> str:
    try:
        function()
    except (RestoreRejected, InstanceAlreadyRunning, FileExistsError) as exc:
        return f"{type(exc).__name__}: {exc}"
    raise AssertionError("operation unexpectedly accepted")


def worker(config: dict, *, ready: Path, release: Path) -> None:
    owner = MachineBook(Path(config["machine_store"]), config["book_id"])
    with owner.locked():
        owner.permit(Path(config["book"]), Path(config["database"]), config["project_id"], generation=config["generation"])
        ready.write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.monotonic() + 20
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if not release.exists():
            raise TimeoutError("D3 hold process release deadline")


def positive(directory: Path) -> dict:
    config = setup(directory)
    source, source_db = Path(config["book"]), Path(config["database"])
    restored, restored_db = directory / "restored/book", directory / "restored/workbench.db"
    owner = MachineBook(Path(config["machine_store"]), config["book_id"])
    with owner.locked():
        proof = owner.prepare(source, source_db, config["project_id"], generation=1)
    before = tree(source)
    # A live writer in another process must prevent the handoff before copying.
    ready, release = directory / "held.ready", directory / "held.release"
    command = [sys.executable,"-B",str(Path(__file__).resolve()),"--hold",str(directory / "source-config.json"),
               "--ready",str(ready),"--release",str(release)]
    process = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 12
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists(), "writer process failed to acquire shared lease"
        def blocked_handoff():
            with owner.locked():
                owner.restore(proof, restored, restored_db)
        active_refusal = expect_reject(blocked_handoff)
        assert not restored.exists()
        # A second real execution process is denied by the same machine scope.
        run_executor(config, directory / "concurrent-source.json", expected=1, rejection="InstanceAlreadyRunning")
    finally:
        release.write_text("release", encoding="utf-8")
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise
    assert process.returncode == 0, stderr.decode(errors="replace")
    with owner.locked():
        receipt = owner.restore(proof, restored, restored_db)
        duplicate_refusal = expect_reject(lambda: owner.restore(proof, restored, restored_db))
    assert tree(source) == before
    restored_config = {**config,"book":str(restored),"database":str(restored_db),"generation":2}
    save(directory / "restored-config.json", restored_config)
    # Negative tests use the actual child execution entrypoint, not just permit().
    source_rejection = run_executor(config, directory / "source-rejected.json", expected=1, rejection="writer generation is inactive or stale")
    stale_rejection = run_executor({**restored_config,"generation":1}, directory / "stale-rejected.json", expected=1, rejection="writer generation is inactive or stale")
    foreign_rejection = run_executor({**restored_config,"project_id":config["other_project_id"]}, directory / "other-rejected.json", expected=1, rejection="registration no longer owns book writing")
    second_db = directory / "restored/second-workbench.db"
    with sqlite3.connect(restored_db) as origin, sqlite3.connect(second_db) as target:
        origin.backup(target)
    cross_data_rejection = run_executor({**restored_config,"database":str(second_db)}, directory / "cross-data-rejected.json", expected=1, rejection="registration no longer owns book writing")
    copied_draft = JobStore(WorkbenchDatabase(restored_db)).get_draft(config["draft"]["id"]).to_dict(include_text=True)
    assert copied_draft == config["draft"]
    before_execution = tree(restored)
    result = run_executor(restored_config, directory / "restored-executor.json")
    assert result["result"]["run"]["chapter_index"] == 3
    assert read(restored / ".novelagent/runtime/snapshot.json")["chapter_index"] == 4
    previous = result["contexts"][0]["previous_prose"]["text"].strip()
    assert previous == canonical_prose_path(source, 2, "Second").read_text(encoding="utf-8").strip()
    assert canonical_prose_path(restored, 3, "Third").is_file()
    assert not canonical_prose_path(source, 3, "Third").exists()
    assert tree(source) == before
    after = tree(restored)
    historic_files = {p:h for p,h in before_execution["files"].items() if p.startswith(".novelagent/runtime/runs/")}
    assert historic_files and all(after["files"].get(p) == h for p,h in historic_files.items())
    assert len(list((Path(receipt["run_dir"]) / "transactions").glob("*/commit.marker"))) == 1
    # Replacing an activated execution directory with byte-identical content
    # must revoke its physical permit, even though historic file hashes match.
    execution_dir = Path(receipt["run_dir"])
    displaced = execution_dir.with_name("displaced-r2")
    assert execution_dir.is_relative_to(directory) and displaced.is_relative_to(directory)
    execution_dir.rename(displaced)
    shutil.copytree(displaced,execution_dir)
    replacement_rejection = run_executor(restored_config,directory / "replaced-execution-rejected.json",
        expected=1,rejection="physical directory identity changed")
    return {"case":"positive", "directory":str(directory), "passed":True,
        "source_committed_chapter":2,"restored_committed_chapter":3,"restored_cursor":4,
        "history_file_count_unchanged":len(historic_files),"source_unchanged":True,"draft_all_fields_preserved":True,
        "active_writer_rejected":active_refusal,"duplicate_activation_rejected":duplicate_refusal,
        "source_old_generation_rejected":source_rejection["returncode"],
        "restored_old_generation_rejected":stale_rejection["returncode"],
        "other_registration_rejected":foreign_rejection["returncode"],
        "different_data_directory_rejected":cross_data_rejection["returncode"],
        "replaced_execution_directory_rejected":replacement_rejection["returncode"], "receipt":receipt}


def negative(directory: Path, kind: str) -> dict:
    config = setup(directory)
    owner = MachineBook(Path(config["machine_store"]), config["book_id"])
    source, database = Path(config["book"]), Path(config["database"])
    destination, copied_db = directory / "restored/book", directory / "restored/workbench.db"
    with owner.locked():
        proof = owner.prepare(source, database, config["project_id"], generation=1)
        if kind == "source_database_changed":
            JobStore(WorkbenchDatabase(database)).create_job(project_id=config["project_id"], kind="generation_session", start_chapter=3,end_chapter=3)
        elif kind == "proof_tampered":
            proof["source_tree"]["files"]["正文/imagined.md"] = "a"*64
        elif kind == "pending_transaction":
            manifest_path = next((source / ".novelagent/runtime/runs/transactions").glob("*/manifest.json"))
            manifest = read(manifest_path)
            manifest["state"] = "prepared"
            save(manifest_path,manifest)
            error = expect_reject(lambda: owner.prepare(source,database,config["project_id"],generation=1))
            return {"case":kind,"passed":True,"error":error}

        def fault(stage):
            if kind == "interrupted" and stage == "suspended":
                raise RestoreRejected("injected interruption after durable suspension")
            if kind == "corrupt_book" and stage == "copied":
                canonical_prose_path(destination,2,"Second").write_text("corrupt",encoding="utf-8")
            if kind == "corrupt_database" and stage == "copied":
                with sqlite3.connect(copied_db) as connection:
                    connection.execute("UPDATE drafts SET text='corrupt'")
            if kind == "late_history_change" and stage == "before_activation":
                canonical_prose_path(destination,2,"Second").write_text("late corrupt",encoding="utf-8")
            if kind == "late_database_change" and stage == "before_activation":
                with sqlite3.connect(copied_db) as connection:
                    connection.execute("UPDATE drafts SET text='late corrupt'")
            if kind == "replaced_directory" and stage == "before_activation":
                # Native Python move within this explicitly isolated case only.
                displaced = directory / "restored/displaced-book"
                assert destination.is_relative_to(directory) and displaced.is_relative_to(directory)
                destination.rename(displaced)
                shutil.copytree(displaced,destination)

        error = expect_reject(lambda: owner.restore(proof,destination,copied_db,fault=fault))
        record = read(owner.record_path)
        if kind in {"source_database_changed","proof_tampered"}:
            assert record["state"] == "active" and record["generation"] == 1
            assert not destination.exists()
        else:
            assert record["state"] == "restoring"
            expect_reject(lambda: owner.permit(source,database,config["project_id"],generation=1))
    return {"case":kind,"passed":True,"error":error,"owner_state":record["state"]}


def run() -> dict:
    directory = REPO / ".tmp/stage-b/d3-candidate-1" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    cases = [positive(directory / "positive")]
    save(directory / "partial-report.json", {"cases":cases,"status":"in_progress"})
    for kind in ("source_database_changed","proof_tampered","pending_transaction","interrupted",
                 "corrupt_book","corrupt_database","late_history_change","late_database_change","replaced_directory"):
        cases.append(negative(directory / kind,kind))
        save(directory / "partial-report.json", {"cases":cases,"status":"in_progress"})
    # v2 historical verification is a separate model, never an EA migration.
    from scripts.experiments.run_d3_restore_baseline import run as baseline
    observation = baseline()
    baseline_source, baseline_copy = Path(observation["source_book"]), Path(observation["restored_book"])
    before = tree(baseline_source)
    same_root_error = expect_reject(lambda: rebind_v2_completed_baseline(baseline_source,baseline_source))
    assert tree(baseline_source) == before
    binding = rebind_v2_completed_baseline(baseline_source,baseline_copy)
    assert tree(baseline_source) == before
    # A new history binding does not grant an old prepare path guard.
    manifest = read(next((baseline_copy / ".novelagent/runtime/persistence/journals").glob("*/manifest.json")))
    old = manifest["immutable"]["targets"][0]
    registry = read(baseline_copy / ".novelagent/runtime/persistence/root_registry.json")
    resolver = SafePathResolver({key:RootBinding(key,value["root_uuid"],Path(value["path"])) for key,value in registry["roots"].items()})
    try:
        resolver.resolve(old["path_ref"],expected_guard=old["path_guard"])
    except PathGuardMismatchError as exc:
        guard_rejection = str(exc)
    else:
        raise AssertionError("old physical preparation revived after history rebind")
    report = {"status":"candidate_1_passed_finite_experiment", "directory":str(directory),"cases":cases,
        "v2_baseline_directory":observation["directory"],"v2_history":binding,
        "same_source_target_rejected":same_root_error,"old_v2_prepare_rejected":guard_rejection,
        "paid_provider_calls":0,"real_book_writes":0,"active_processes":[],
        "production_entrypoints_enabled":False,
        "limitations":["Explicit experimental shared machine store and guarded execution entrypoint; ordinary product integration remains T7.",
            "Exactly one completed legacy chapter; multiple overlapping history targets and unresolved task recovery remain later work.",
            "Source is present and idle; interruption suspends both writers and requires explicit recovery, not automatic reactivation.",
            "Local deterministic generation, minimal policy and synthetic Strict identity; no real model quality or real book qualification claim."]}
    save(directory / "report.json", report)
    return {"directory":str(directory),"status":report["status"],"cases":len(cases)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold",type=Path)
    parser.add_argument("--ready",type=Path)
    parser.add_argument("--release",type=Path)
    args = parser.parse_args()
    if args.hold:
        worker(read(args.hold),ready=args.ready,release=args.release)
    else:
        print(json.dumps(run(),ensure_ascii=False,indent=2))
