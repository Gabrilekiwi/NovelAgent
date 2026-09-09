"""Verify current task isolation using a copied database actually written by B0."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from workbench.database import WorkbenchDatabase
from workbench.jobs import JobError, JobStore


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(probes: Path) -> dict:
    probes = probes.resolve(strict=True)
    assert probes.is_relative_to(REPO / ".tmp/stage-b/d2-b0-probes")
    report = json.loads((probes / "report.json").read_text(encoding="utf-8"))
    assert report["b0_source_unchanged"] is True
    case = probes / "control-workbench_schedule"
    source = case / "workbench.db"
    original_hash = digest(source)
    original_job = json.loads((case / "stdout.txt").read_text(encoding="utf-8"))
    assert "execution_contract" not in original_job
    directory = REPO / ".tmp/stage-b/d2-task-upgrade" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    target = directory / "workbench.db"
    # All B0 connections are closed. Refuse a partial file-only WAL copy.
    assert not Path(str(source) + "-wal").exists()
    shutil.copy2(source, target)
    database = WorkbenchDatabase(target)
    store = JobStore(database)
    loaded = store.get(original_job["id"])
    assert loaded.execution_contract is None
    assert all(loaded.to_dict()[key] == value for key, value in original_job.items())
    assert store.claim_next() is None
    isolated = store.get(loaded.id)
    assert isolated.state == "needs_recovery" and not store.resume_ready(loaded.id)
    for action in (lambda: store.resume(loaded.id, confirmed=True), lambda: store.release_held(loaded.id)):
        try:
            action()
        except JobError as exc:
            assert "执行契约不兼容" in str(exc)
        else:
            raise AssertionError("B0 task became executable")
    assert isolated.config == loaded.config and isolated.result == loaded.result and isolated.error == loaded.error
    with sqlite3.connect(target) as connection:
        original_events = list(connection.execute("SELECT event_type FROM job_events WHERE job_id=? ORDER BY id", (loaded.id,)))
    assert original_events[0] == ("job.queued",)
    assert original_events[-1] == ("job.execution_contract_incompatible",)
    assert digest(source) == original_hash
    result = {"directory": str(directory), "b0_report": str(probes / "report.json"),
              "source_database_sha256": original_hash, "source_database_unchanged": True,
              "original_job": original_job, "isolated_job": isolated.to_dict(),
              "original_config_result_error_preserved": True,
              "automatic_execution": False, "provider_calls": 0, "real_book_writes": 0}
    (directory / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b0-probes", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().b0_probes), ensure_ascii=False, indent=2))
