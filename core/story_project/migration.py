from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable
import uuid

from core.runtime_paths import RuntimePaths
from core.schema import validate_schema
from core.story_project.identity import ProjectIdentity, ensure_project_identity, load_project_identity


MIGRATION_SCHEMA_VERSION = "1.0"


class StoryProjectRuntimeMigrationError(ValueError):
    pass


def inspect_story_project_runtime_migration(
    *,
    source_runtime: str | Path,
    story_project_root: str | Path,
    target_runtime: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(source_runtime).resolve()
    story_root = Path(story_project_root).resolve()
    target = Path(target_runtime).resolve() if target_runtime is not None else RuntimePaths.for_story_project(story_root).runtime_dir
    problems: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []

    if not source.is_dir():
        problems.append({"code": "source_runtime_missing", "message": f"Source runtime is not a directory: {source}"})
    if target.exists():
        problems.append({"code": "target_runtime_exists", "message": f"Target runtime already exists: {target}"})
    if _contains(source, target) or _contains(target, source):
        problems.append({"code": "runtime_paths_overlap", "message": "Source and target runtime paths overlap"})

    run_dir = source / "runs"
    if source.is_dir():
        run_paths = sorted(run_dir.glob("chapter_*.json")) if run_dir.is_dir() else sorted(source.glob("chapter_*.json"))
        if not run_paths:
            problems.append({"code": "migration_run_evidence_missing", "message": "No historical run records prove StoryProject ownership"})
        for path in run_paths:
            record = _inspect_run_record(path, story_root)
            records.append(record)
            if record["status"] != "matching":
                problems.append(
                    {
                        "code": f"migration_run_{record['status']}",
                        "message": f"{path} does not prove ownership by {story_root}",
                    }
                )

    discovered_book_ids = sorted(
        {
            str(record["book_id"])
            for record in records
            if record.get("status") == "matching" and record.get("book_id")
        }
    )
    if len(discovered_book_ids) > 1:
        problems.append(
            {
                "code": "migration_book_id_conflict",
                "message": "Historical run records contain multiple book_id values",
            }
        )

    existing_identity = load_project_identity(story_root)
    if existing_identity is not None and discovered_book_ids and discovered_book_ids != [existing_identity.book_id]:
        problems.append(
            {
                "code": "story_project_state_identity_mismatch",
                "message": "Historical runtime book_id does not match project.json",
            }
        )

    return {
        "ok": not problems,
        "copy_allowed": not problems,
        "source_runtime": str(source),
        "target_runtime": str(target),
        "story_project_root": str(story_root),
        "target_exists": target.exists(),
        "records": records,
        "discovered_book_ids": discovered_book_ids,
        "problems": problems,
    }


def migrate_story_project_runtime(
    *,
    source_runtime: str | Path,
    story_project_root: str | Path,
    now: Callable[[], datetime] | None = None,
    migration_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> dict[str, Any]:
    story_root = Path(story_project_root).resolve()
    target = RuntimePaths.for_story_project(story_root).runtime_dir
    inspection = inspect_story_project_runtime_migration(
        source_runtime=source_runtime,
        story_project_root=story_root,
        target_runtime=target,
    )
    if not inspection["copy_allowed"]:
        codes = ", ".join(problem["code"] for problem in inspection["problems"])
        raise StoryProjectRuntimeMigrationError(f"StoryProject runtime migration blocked: {codes}")

    discovered_book_ids = inspection["discovered_book_ids"]
    identity = ensure_project_identity(
        story_root,
        book_id=discovered_book_ids[0] if discovered_book_ids else None,
    )
    migration_id = f"migration-{migration_id_factory()}"
    source = Path(inspection["source_runtime"])
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{migration_id}.staging"
    stage.mkdir()
    try:
        _copy_runtime_contents(source, stage, identity=identity)
        files = _file_inventory(stage)
        manifest = _migration_manifest(
            migration_id=migration_id,
            identity=identity,
            story_root=story_root,
            source=source,
            target=target,
            files=files,
            now=now,
        )
        manifest_dir = stage / "migrations"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{migration_id}.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(stage, target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "ok": True,
        "identity": identity.to_dict(),
        "inspection": inspection,
        "manifest": manifest,
        "manifest_path": str(target / "migrations" / f"{migration_id}.json"),
    }


def _inspect_run_record(path: Path, story_root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return {"path": str(path), "status": "malformed", "book_id": None, "error": str(exc)}
    run = payload.get("run") if isinstance(payload, dict) and isinstance(payload.get("run"), dict) else payload
    story = run.get("story_project") if isinstance(run, dict) and isinstance(run.get("story_project"), dict) else None
    root_value = story.get("root") if story else None
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        return {"path": str(path), "status": "unattributed", "book_id": None, "root": root_value}
    recorded_root = Path(root_value).resolve()
    status = "matching" if _same_path(recorded_root, story_root) else "mismatched"
    book_id = story.get("book_id") if story and isinstance(story.get("book_id"), str) else None
    return {
        "path": str(path),
        "status": status,
        "book_id": book_id,
        "root": str(recorded_root),
    }


def _copy_runtime_contents(source: Path, stage: Path, *, identity: ProjectIdentity) -> None:
    for child in source.iterdir():
        if child.name == "runs" and child.is_dir():
            shutil.copytree(child, stage / "runs", ignore=shutil.ignore_patterns("transactions"))
            legacy_transactions = child / "transactions"
            if legacy_transactions.is_dir():
                shutil.copytree(legacy_transactions, stage / "persistence")
            continue
        if child.name == "memory" and child.is_dir():
            shutil.copytree(child, stage / "memory")
            continue
        if child.name in {"notion_memory.json", "memory_outbox.jsonl"} and child.is_file():
            memory_dir = stage / "memory"
            memory_dir.mkdir(exist_ok=True)
            shutil.copy2(child, memory_dir / child.name)
            continue
        destination = stage / child.name
        if child.is_dir():
            shutil.copytree(child, destination)
        elif child.is_file():
            shutil.copy2(child, destination)
    _bind_migrated_state(stage, identity=identity)
    _bind_migrated_journals(stage / "persistence", book_id=identity.book_id)


def _bind_migrated_state(runtime_dir: Path, *, identity: ProjectIdentity) -> None:
    snapshot_path = runtime_dir / "snapshot.json"
    if snapshot_path.is_file():
        snapshot = _load_json_object(snapshot_path)
        _assert_migrated_book_id(snapshot.get("book_id"), identity.book_id, snapshot_path)
        snapshot["book_id"] = identity.book_id
        _write_json_object(snapshot_path, snapshot)

    run_dir = runtime_dir / "runs"
    if run_dir.is_dir():
        for run_path in sorted(run_dir.glob("chapter_*.json")):
            envelope = _load_json_object(run_path)
            run = envelope.get("run") if isinstance(envelope.get("run"), dict) else envelope
            story = run.get("story_project") if isinstance(run.get("story_project"), dict) else None
            if story is None:
                continue
            _assert_migrated_book_id(story.get("book_id"), identity.book_id, run_path)
            story["book_id"] = identity.book_id
            story["project_identity"] = identity.to_dict()
            _write_json_object(run_path, envelope)
        loop_dir = run_dir / "loop_sessions"
        if loop_dir.is_dir():
            for loop_path in sorted(loop_dir.glob("loop_*.json")):
                session = _load_json_object(loop_path)
                _assert_migrated_book_id(session.get("book_id"), identity.book_id, loop_path)
                session["book_id"] = identity.book_id
                _write_json_object(loop_path, session)


def _assert_migrated_book_id(actual: Any, expected: str, path: Path) -> None:
    if actual not in {None, expected}:
        raise StoryProjectRuntimeMigrationError(
            f"Migration artifact book_id mismatch: {path}: {actual!r} != {expected!r}"
        )


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise StoryProjectRuntimeMigrationError(f"Migration JSON artifact is not an object: {path}")
    return value


def _write_json_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _bind_migrated_journals(persistence_dir: Path, *, book_id: str) -> None:
    if not persistence_dir.is_dir():
        return
    for manifest_path in sorted(persistence_dir.glob("*/manifest.json")):
        payload = _load_json_object(manifest_path)
        existing = payload.get("book_id")
        if existing not in {None, book_id}:
            raise StoryProjectRuntimeMigrationError(
                f"Migration journal book_id mismatch: {manifest_path}: {existing!r} != {book_id!r}"
            )
        payload["book_id"] = book_id
        _write_json_object(manifest_path, payload)


def _file_inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "relative_path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file())
    ]


def _migration_manifest(
    *,
    migration_id: str,
    identity: ProjectIdentity,
    story_root: Path,
    source: Path,
    target: Path,
    files: list[dict[str, Any]],
    now: Callable[[], datetime] | None,
) -> dict[str, Any]:
    value = now() if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    manifest = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration_id": migration_id,
        "created_at": value.astimezone(timezone.utc).isoformat(),
        "book_id": identity.book_id,
        "story_project_root": str(story_root),
        "source_runtime": str(source),
        "target_runtime": str(target),
        "source_deleted": False,
        "files": files,
    }
    return validate_schema(manifest, "story_project_runtime_migration.schema.json")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = [
    "MIGRATION_SCHEMA_VERSION",
    "StoryProjectRuntimeMigrationError",
    "inspect_story_project_runtime_migration",
    "migrate_story_project_runtime",
]
