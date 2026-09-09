"""D3 candidate 1, explicitly isolated. Not a production restore API.

The caller supplies a shared *experimental machine store*, not a workbench data
directory. T7 must install one fixed machine scope and integrate every writer.
Old journals stay byte-identical; only new execution directories may reconcile.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import uuid

from core.engine.persistence import atomic_create_json, atomic_write_json, load_persistence_candidate, _load_manifest, _validate_commit_marker
from core.engine.root_registry import directory_identity
from core.engine.safe_paths import assert_safe_local_tree, FILE_ATTRIBUTE_REPARSE_POINT
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.identity import load_project_identity
from workbench.instance import InstanceLease

REPO = Path(__file__).resolve().parents[2]


class RestoreRejected(ValueError):
    pass


def isolated(path: Path) -> Path:
    # Inspect links BEFORE resolve(), which would hide junction/symlink traversal.
    path = Path(path).absolute()
    if ".." in path.parts:
        raise RestoreRejected("parent traversal forbidden")
    assert_safe_local_tree(path.parent)
    if os.path.lexists(path):
        info = os.lstat(path)
        if (stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0) & FILE_ATTRIBUTE_REPARSE_POINT
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
            raise RestoreRejected("unsafe path component")
    if not path.is_relative_to(REPO / ".tmp/stage-b"):
        raise RestoreRejected("D3 accepts only isolated stage-b paths")
    return path


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict:
    isolated(root)
    files, directories = {}, []
    for item in sorted(root.rglob("*")):
        isolated(item)
        relative = item.relative_to(root).as_posix()
        if item.is_dir():
            directories.append(relative)
        elif item.is_file():
            if item.stat().st_nlink != 1:
                raise RestoreRejected("hard-linked backup member")
            files[relative] = sha(item)
        else:
            raise RestoreRejected("unsupported backup member")
    return {"files":files, "directories":directories}


def lineage(root: Path) -> dict:
    root = isolated(root)
    return {str(p):directory_identity(p) for p in (*reversed(root.parents), root)}


def check_lineage(expected: dict) -> None:
    if any(directory_identity(Path(p)) != value for p,value in expected.items()):
        raise RestoreRejected("physical directory identity changed")


def registration(database: Path, project_id: str) -> dict:
    database = isolated(database)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT book_id,root FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise RestoreRejected("registration missing")
    return {"database":str(database), "project_id":project_id, "book_id":row[0], "root":row[1]}


def database_digest(database: Path) -> str:
    with sqlite3.connect(f"{isolated(database).as_uri()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RestoreRejected("SQLite integrity check failed")
        return canonical_json_hash(list(connection.iterdump()))


def verify_v1_history(root: Path, original: Path) -> list[dict]:
    """Map absolute historic targets for read-only verification, never execution.

    Candidate 1 deliberately accepts only one completed chapter transaction.
    Multiple overlapping historic targets need the T7 latest-target verifier.
    """
    manifests = sorted((root / ".novelagent/runtime/runs/transactions").glob("*/manifest.json"))
    if len(manifests) != 1:
        raise RestoreRejected("D3 candidate requires exactly one completed v1 chapter")
    result = []
    for path in manifests:
        manifest = _load_manifest(path.parent)
        if manifest["state"] != "completed":
            raise RestoreRejected("pending historic transaction")
        candidate = load_persistence_candidate(path.parent)
        if candidate is None or not candidate.get("committed"):
            raise RestoreRejected("historic chapter candidate is not committed")
        error = _validate_commit_marker(path.parent / "commit.marker", manifest["run_id"], manifest.get("candidate_sha256"))
        if error:
            raise RestoreRejected(str(error))
        for target in manifest["targets"]:
            old = Path(target["path"])
            try:
                relative = old.relative_to(original)
            except ValueError as exc:
                raise RestoreRejected("historic transaction addresses an external root") from exc
            mapped = isolated(root / relative)
            staged = isolated(path.parent / target["staged_path"])
            if not staged.is_relative_to(path.parent) or sha(staged) != target["after_sha256"]:
                raise RestoreRejected("historic staged bytes mismatch")
            if sha(mapped) != target["after_sha256"]:
                raise RestoreRejected("historic committed target mismatch")
        result.append({"run_id":manifest["run_id"], "manifest_sha256":sha(path),
                       "chapter":candidate["run"]["chapter_index"]})
    return result


class MachineBook:
    def __init__(self, machine_store: Path, book_id: str):
        if not book_id.startswith("synthetic-"):
            raise RestoreRejected("synthetic identity required")
        self.machine_store = isolated(machine_store)
        self.book_id = book_id
        self.directory = self.machine_store / hashlib.sha256(book_id.encode()).hexdigest()
        self.directory.mkdir(exist_ok=True)
        self.record_path = self.directory / "ownership.json"
        self.lease = None

    @contextmanager
    def locked(self):
        if self.lease is not None:
            raise RestoreRejected("nested machine lock")
        lease = InstanceLease(self.directory).acquire()
        self.lease = lease
        try:
            yield self
        finally:
            self.lease = None
            lease.close()

    def _held(self):
        if self.lease is None:
            raise RestoreRejected("machine book lock required")
        self.lease.require_held()

    def claim_source(self, source: Path, database: Path, project_id: str):
        self._held()
        source = isolated(source)
        identity = load_project_identity(source)
        reg = registration(database, project_id)
        if identity is None or identity.book_id != self.book_id or reg["book_id"] != self.book_id or Path(reg["root"]) != source:
            raise RestoreRejected("source identity/registration mismatch")
        atomic_create_json(self.record_path, {"book_id":self.book_id, "generation":1, "state":"active",
            "active_root":str(source), "registration":reg, "lineage":lineage(source),
            "run_dir":str(source / ".novelagent/runtime/runs")})

    def permit(self, book: Path, database: Path, project_id: str, *, generation: int) -> dict:
        self._held()
        record = read(self.record_path)
        book = isolated(book)
        if record["state"] != "active" or record["generation"] != generation:
            raise RestoreRejected("writer generation is inactive or stale")
        if record["active_root"] != str(book) or record["registration"] != registration(database, project_id):
            raise RestoreRejected("registration no longer owns book writing")
        identity = load_project_identity(book)
        if identity is None or identity.book_id != self.book_id:
            raise RestoreRejected("active identity mismatch")
        check_lineage(record["lineage"])
        if record.get("execution_lineage"):
            check_lineage(record["execution_lineage"])
        if record.get("receipt_path"):
            receipt = read(isolated(Path(record["receipt_path"])))
            if canonical_json_hash(receipt) != record["receipt_digest"]:
                raise RestoreRejected("activation receipt changed")
        return record

    def prepare(self, source: Path, database: Path, project_id: str, *, generation: int) -> dict:
        record = self.permit(source, database, project_id, generation=generation)
        history = verify_v1_history(source, source)
        # The finite experiment starts from an idle database. General task
        # quarantine and snapshot maintenance are the later product work.
        with sqlite3.connect(database) as connection:
            active = connection.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('completed','cancelled','failed')").fetchone()[0]
        if active:
            raise RestoreRejected("source database has unresolved jobs")
        proof = {"format":"d3-restore-proof-1", "book_id":self.book_id,
            "generation":generation, "source_root":str(source), "registration":record["registration"],
            "source_tree":tree(source), "source_lineage":lineage(source), "history":history,
            "source_database_digest":database_digest(database),
            "nonce":uuid.uuid4().hex}
        proof_path = self.directory / f"proof-{proof['nonce']}.json"
        atomic_create_json(proof_path, proof)
        # Source remains active until an explicit activation. The prepared
        # digest is anchored outside both source and restored data directories.
        atomic_write_json(self.record_path, {**record, "prepared_proof":canonical_json_hash(proof)})
        return proof

    def restore(self, proof: dict, destination: Path, database: Path, *, fault=None) -> dict:
        self._held()
        record = read(self.record_path)
        if record["state"] != "active" or record["generation"] != proof["generation"] or record.get("prepared_proof") != canonical_json_hash(proof):
            raise RestoreRejected("unanchored, stale or already consumed restore proof")
        source = isolated(Path(proof["source_root"]))
        source_db = Path(proof["registration"]["database"])
        self.permit(source, source_db, proof["registration"]["project_id"], generation=proof["generation"])
        check_lineage(proof["source_lineage"])
        if tree(source) != proof["source_tree"]:
            raise RestoreRejected("source changed after proof")
        if database_digest(source_db) != proof["source_database_digest"]:
            raise RestoreRejected("source database changed after proof")
        destination = isolated(destination)
        database = isolated(database)
        if (destination.is_relative_to(source) or source.is_relative_to(destination)
                or database.is_relative_to(source) or database.is_relative_to(destination)):
            raise RestoreRejected("restore locations overlap source or book content")
        if destination.exists() or database.exists():
            raise RestoreRejected("restore destination must be new and empty")
        destination_parent = lineage(destination.parent)
        database_parent = lineage(database.parent)
        # From this durable point any interruption blocks BOTH registrations.
        # Automatic rollback is intentionally absent: no ambiguous dual owner.
        intent = {**record, "state":"restoring", "destination":str(destination), "destination_database":str(database)}
        atomic_write_json(self.record_path, intent)
        if fault:
            fault("suspended")
        shutil.copytree(source, destination)
        with sqlite3.connect(source_db) as origin, sqlite3.connect(database) as copied:
            origin.backup(copied)
        sqlite_snapshot_sha256 = sha(database)
        if fault:
            fault("copied")
        check_lineage(destination_parent)
        check_lineage(database_parent)
        if tree(source) != proof["source_tree"] or tree(destination) != proof["source_tree"]:
            raise RestoreRejected("snapshot changed or copy corrupt")
        if database_digest(database) != proof["source_database_digest"] or database_digest(source_db) != proof["source_database_digest"]:
            raise RestoreRejected("database snapshot changed or copy corrupt")
        verify_v1_history(destination, source)
        identity = load_project_identity(destination)
        if identity is None or identity.book_id != self.book_id:
            raise RestoreRejected("restored identity mismatch")
        project_id = proof["registration"]["project_id"]
        if registration(database, project_id)["root"] != str(source):
            raise RestoreRejected("copied database registration mismatch")
        # Preserve all draft fields and other registrations. They have no
        # permit from the shared machine record and cannot be executed here.
        with sqlite3.connect(database) as copied:
            copied.execute("UPDATE projects SET root=? WHERE id=?", (str(destination), project_id))
        # Short generation namespace: existing Windows persistence MoveFileExW
        # fails on long non-prefixed paths. Keep the nonce in the receipt.
        run_dir = destination / ".novelagent/runtime" / f"r{proof['generation']+1}"
        run_dir.mkdir()
        activated_tree = tree(destination)
        activated_database = database_digest(database)
        receipt = {"format":"d3-restore-activation-1", "book_id":self.book_id,
            "proof_digest":canonical_json_hash(proof), "source_root":str(source),
            "destination":str(destination), "sqlite_snapshot_sha256":sqlite_snapshot_sha256,
            "rebound_database_sha256":sha(database), "lineage":lineage(destination),
            "execution_lineage":lineage(run_dir), "database_digest":activated_database,
            "old_generation":proof["generation"], "generation":proof["generation"]+1,
            "history":proof["history"], "run_dir":str(run_dir),
            "old_execution_plans":"invalidated", "created_at":datetime.now(timezone.utc).isoformat()}
        receipt_path = self.directory / f"activation-{proof['nonce']}.json"
        atomic_create_json(receipt_path, receipt)
        if fault:
            fault("before_activation")
        check_lineage(destination_parent)
        check_lineage(database_parent)
        check_lineage(receipt["lineage"])
        check_lineage(receipt["execution_lineage"])
        if tree(destination) != activated_tree or database_digest(database) != activated_database:
            raise RestoreRejected("restored data changed before activation")
        if tree(source) != proof["source_tree"] or database_digest(source_db) != proof["source_database_digest"]:
            raise RestoreRejected("source changed before activation")
        active = {"book_id":self.book_id, "generation":receipt["generation"], "state":"active",
            "active_root":str(destination), "registration":registration(database, project_id),
            "lineage":receipt["lineage"], "run_dir":str(run_dir), "receipt_path":str(receipt_path),
            "execution_lineage":receipt["execution_lineage"],
            "receipt_digest":canonical_json_hash(receipt)}
        atomic_write_json(self.record_path, active)
        return receipt


def rebind_v2_completed_baseline(source: Path, destination: Path) -> dict:
    """Separate finite v2 history probe; no pending journal or write permit.

    Call only on the isolated D3 baseline while no worker exists. This is an
    explicit proof-checked control-plane mutation, never ordinary ensure/remap.
    """
    from core.engine.persistence_v2 import verify_publication_receipt
    from core.engine.root_registry import RootRegistryService, _arm_main_project_fence, _path_digest, _registry_digest, validate_root_registry
    from scripts.experiments.run_d3_restore_baseline import root_map
    import copy

    source, destination = isolated(source), isolated(destination)
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise RestoreRejected("v2 source and destination overlap")
    original = tree(source)
    if tree(destination) != original:
        raise RestoreRejected("v2 baseline copy differs from source")
    source_roots, roots = root_map(source), root_map(destination)
    control = roots["runtime"] / "persistence"
    manifests = list((control / "journals").glob("*/manifest.json"))
    if len(manifests) != 1 or list((control / "registry/pending").glob("*")):
        raise RestoreRejected("v2 baseline must have exactly one completed journal")
    receipt_relative = ".novelagent/runtime/receipts/d3-baseline-chapter-2.json"
    before_verification = verify_publication_receipt(source / receipt_relative, root_map=source_roots)
    if not before_verification["valid"]:
        raise RestoreRejected("source v2 receipt invalid")
    registry = RootRegistryService(control).load()
    if set(registry["roots"]) != set(roots):
        raise RestoreRejected("unexpected v2 physical roots")
    updated = copy.deepcopy(registry)
    for key, binding in updated["roots"].items():
        if Path(binding["path"]) != source_roots[key]:
            raise RestoreRejected("v2 source root differs from baseline proof")
        binding.update(path=str(roots[key]), path_identity_sha256=_path_digest(roots[key]),
                       directory_identity=directory_identity(roots[key]))
    updated["roots"]["runtime"]["project_remap_fence"] = _arm_main_project_fence(control, roots)
    updated["revision"] += 1
    updated["registry_digest"] = _registry_digest(updated)
    proof = {"format":"d3-v2-history-binding-1", "source_root":str(source), "destination":str(destination),
        "source_tree_digest":canonical_json_hash(original), "old_registry":registry,
        "new_registry":updated, "destination_lineage":lineage(destination)}
    # Retain the proof outside the book to keep the allowed byte changes exact.
    atomic_create_json(destination.parent / "v2-history-binding.json", proof)
    atomic_write_json(control / "root_registry.json", validate_root_registry(updated))
    verification = verify_publication_receipt(destination / receipt_relative, root_map=roots)
    if not verification["valid"]:
        raise RestoreRejected("new v2 history binding failed verification")
    after = tree(destination)
    changed = [p for p,h in original["files"].items() if after["files"].get(p) != h]
    if changed != [".novelagent/runtime/persistence/root_registry.json"] or set(after["files"]) != set(original["files"]):
        raise RestoreRejected("history bytes unexpectedly changed")
    return {"verification":verification, "changed_files":changed, "proof_digest":canonical_json_hash(proof)}
