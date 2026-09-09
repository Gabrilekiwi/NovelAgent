"""Run the actual archived B0 entrypoints against isolated format candidates.

No B0 source edits, patched guards, Provider calls, or real books. The archive
manifest is checked before and after execution. Evidence includes every book file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
B0 = "090d0aa191f7aae96ea1c979b6725d7f87d811ae"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture(root: Path, variant: str) -> None:
    shutil.copytree(REPO / "tests/fixtures/story_project_semantics/cases/synthetic_standard/book", root)
    (root / ".novelagent").mkdir()
    identity = {
        "schema_version": "3.0" if variant == "identity_v3" else "2.0",
        "book_id": "synthetic-d2-" + root.parent.name,
        "created_at": "2026-09-09T00:00:00Z", "root_hint": ".",
        "story_state_mode": "strict", "ephemeral": False,
        "activation": {"parser_version": "shadow-1.0", "semantic_schema_version": "1.0",
                       "layout_profile_version": "canonical-zh-1",
                       "calibration_report_sha256": "a" * 64,
                       "activated_at": "2026-09-09T00:00:00Z"},
        "authority": {"mode": "legacy_markdown_v1", "authority_epoch": 0,
                      "head_event_hash": None, "activation_receipt": None,
                      "minimum_writer_contract": 1 if variant == "control" else 2},
    }
    # Synthetic metadata tests the format boundary, not real Strict qualification.
    save(root / ".novelagent/project.json", identity)


def run(archive: Path) -> dict:
    archive = archive.resolve(strict=True)
    code = (archive / "code").resolve(strict=True)
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == B0
    assert digest((archive / "b0.zip").read_bytes()) == manifest["archive_sha256"]
    original = inventory(code)
    # Support the original extraction manifest's list/dict representation below.
    listed = manifest["files"]
    expected = listed if isinstance(listed, dict) else {p["path"]: p["sha256"] for p in listed}
    assert original == expected, "B0 extracted source differs from its manifest"
    directory = REPO / ".tmp/stage-b/d2-b0-probes" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    commands = {
        "init_runtime": ["--init-runtime"],
        "generation": ["--dry-run", "--persist-dry-run",
                       "--memory-source", "file", "--memory-writeback", "none"],
        "reconcile": ["--reconcile-persistence"],
        "recover_latest": ["--recover-latest"],
        "recover_locked": ["--recover-locked-chapter"],
        "workbench_schedule": [],
    }
    results = []
    for variant in ("control", "minimum_only", "identity_v3"):
        for entrypoint, flags in commands.items():
            case = directory / f"{variant}-{entrypoint}"
            root = case / "book"
            fixture(root, variant)
            before = inventory(root)
            command = [sys.executable, "-B", str(code / "main.py"), "--story-project", str(root), *flags]
            if entrypoint == "workbench_schedule":
                # External harness only: import and call archived public APIs.
                # No monkeypatches and no supervisor/process/Provider creation.
                command = [sys.executable, "-B", "-c", "\n".join([
                    "import json, sys", "from pathlib import Path",
                    "from workbench.database import WorkbenchDatabase",
                    "from workbench.projects import ProjectRegistry",
                    "from workbench.jobs import JobStore",
                    "import core.story_project.identity as identity_module",
                    "assert Path(identity_module.__file__).resolve().is_relative_to(Path.cwd())",
                    "root = Path(sys.argv[1])",
                    "database = WorkbenchDatabase(root.parent / 'workbench.db')",
                    "registry = ProjectRegistry(database, allowed_workspaces=[root.parent])",
                    "project = registry.import_project(root)",
                    "store = JobStore(database)",
                    "job = store.create_job(project_id=project.id, kind='generation_session', start_chapter=2, end_chapter=2)",
                    "print(json.dumps(job.to_dict()))",
                ]), str(root)]
            process = subprocess.run(command, cwd=code, env=environment,
                                     capture_output=True, text=True, encoding="utf-8", timeout=45)
            after = inventory(root)
            (case / "stdout.txt").write_text(process.stdout, encoding="utf-8")
            (case / "stderr.txt").write_text(process.stderr, encoding="utf-8")
            result = {"variant": variant, "entrypoint": entrypoint, "command": command,
                      "returncode": process.returncode, "before": before, "after": after,
                      "changed": sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p)),
                      "schema_refusal": "$.schema_version must be one of ['1.0', '2.0']" in process.stderr,
                      "stderr_tail": process.stderr[-1800:]}
            save(case / "result.json", result)
            results.append(result)
    assert inventory(code) == original, "B0 code tree changed during execution"
    assert all(row["schema_refusal"] and not row["changed"] and row["returncode"] != 0
               for row in results if row["variant"] == "identity_v3")
    assert any(row["changed"] and row["returncode"] == 0 for row in results
               if row["variant"] == "minimum_only")
    report = {"b0_commit": B0, "archive": str(archive), "directory": str(directory),
              "b0_source_unchanged": True, "provider_calls": 0, "real_book_writes": 0,
              "cases": results}
    save(directory / "report.json", report)
    return {"directory": str(directory), "cases": [
        {key: row[key] for key in ("variant", "entrypoint", "returncode", "changed", "schema_refusal")}
        for row in results]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().archive), ensure_ascii=False, indent=2))
