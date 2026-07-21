from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.story_project.paths import resolve_story_project_root, scan_prose_chapters  # noqa: E402


INITIAL_STATE_NAMES = {
    "context": "追踪/上下文.md",
    "foreshadowing": "追踪/伏笔.md",
    "timeline": "追踪/时间线.md",
    "character_state": "追踪/角色状态.md",
    "snapshot": "snapshot.json",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def text_stats(data: bytes) -> dict[str, int]:
    text = data.decode("utf-8-sig")
    return {
        "bytes": len(data),
        "text_chars": len(text),
        "non_whitespace_chars": sum(not char.isspace() for char in text),
    }


def committed_runs(run_dir: Path) -> tuple[dict[int, dict[str, Any]], Counter[str]]:
    by_chapter: dict[int, dict[str, Any]] = {}
    statuses: Counter[str] = Counter()
    for path in sorted(run_dir.glob("chapter_*.json")):
        payload = read_json(path)
        run = payload.get("run")
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "unknown")
        statuses[status] += 1
        if status != "committed":
            continue
        chapter = run.get("chapter_index")
        if not isinstance(chapter, int):
            raise ValueError(f"Committed run has no integer chapter_index: {path}")
        if chapter in by_chapter:
            raise ValueError(f"Multiple committed runs found for chapter {chapter}")
        by_chapter[chapter] = {
            "path": path,
            "payload": payload,
            "run_id": str(run.get("id") or path.stem),
            "models": sorted(collect_model_names(payload)),
        }
    return by_chapter, statuses


def collect_model_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"model", "model_name"} and isinstance(item, str) and item.strip():
                names.add(item.strip())
            names.update(collect_model_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(collect_model_names(item))
    return names


def prose_target(transaction_manifest: dict[str, Any]) -> dict[str, Any]:
    targets = transaction_manifest.get("targets")
    if not isinstance(targets, list):
        raise ValueError("Persistence manifest has no targets list")
    matches = [item for item in targets if isinstance(item, dict) and item.get("kind") == "prose"]
    if len(matches) != 1:
        raise ValueError(f"Expected one prose target, found {len(matches)}")
    return matches[0]


def copy_tree(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)


def merge_chapters(title: str, chapters: Iterable[tuple[Path, bytes]], output: Path) -> None:
    parts = [
        f"# 《{title}》旧稿封存版",
        "",
        "> 本文件由正式正文按章节号合并生成；各章原始字节与哈希见 baseline_manifest.json。",
        "",
    ]
    for path, data in chapters:
        heading = path.stem.replace("_", " ", 1)
        body = data.decode("utf-8-sig").strip("\ufeff\r\n")
        parts.extend([f"## {heading}", "", body, ""])
    output.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def git_metadata(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    status = run("status", "--short")
    return {
        "head": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(status),
        "status_sha256": sha256_bytes((status or "").encode("utf-8")),
    }


def zip_story_project(story_root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(story_root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                relative = path.relative_to(story_root)
                archive.write(path, (Path(story_root.name) / relative).as_posix())


def export_archive(story_root: Path, output_root: Path) -> dict[str, Any]:
    if output_root.exists():
        raise FileExistsError(f"Archive output already exists: {output_root}")
    output_root.mkdir(parents=True)

    project_path = story_root / ".novelagent" / "project.json"
    runtime_root = story_root / ".novelagent" / "runtime"
    run_dir = runtime_root / "runs"
    persistence_root = runtime_root / "persistence"
    project = read_json(project_path)

    scanned = scan_prose_chapters(story_root)
    if not scanned:
        raise ValueError("StoryProject has no prose chapters")
    expected = list(range(1, max(scanned) + 1))
    if sorted(scanned) != expected:
        raise ValueError(f"Prose chapters are not continuous: {sorted(scanned)}")
    for chapter, candidates in scanned.items():
        if len(candidates) != 1:
            raise ValueError(f"Chapter {chapter} has {len(candidates)} prose candidates")

    runs, run_statuses = committed_runs(run_dir)
    if sorted(runs) != expected:
        raise ValueError(
            f"Committed run chapters do not match prose chapters: prose={expected}, runs={sorted(runs)}"
        )

    current_dir = output_root / "chapters_current"
    committed_dir = output_root / "chapters_historical_committed"
    evidence_runs_dir = output_root / "evidence" / "committed_runs"
    evidence_transactions_dir = output_root / "evidence" / "committed_transactions"
    current_dir.mkdir(parents=True)
    committed_dir.mkdir(parents=True)
    evidence_runs_dir.mkdir(parents=True)
    evidence_transactions_dir.mkdir(parents=True)

    current_merge_inputs: list[tuple[Path, bytes]] = []
    committed_merge_inputs: list[tuple[Path, bytes]] = []
    chapters_manifest: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []

    for chapter in expected:
        current_path = scanned[chapter][0]
        current_data = current_path.read_bytes()
        shutil.copy2(current_path, current_dir / current_path.name)

        run = runs[chapter]
        run_id = run["run_id"]
        shutil.copy2(run["path"], evidence_runs_dir / run["path"].name)

        transaction_dir = persistence_root / run_id
        transaction_manifest_path = transaction_dir / "manifest.json"
        transaction_manifest = read_json(transaction_manifest_path)
        target = prose_target(transaction_manifest)
        staged_relative = target.get("staged_path")
        if not isinstance(staged_relative, str):
            raise ValueError(f"Committed prose target has no staged_path: {transaction_manifest_path}")
        committed_source = transaction_dir / staged_relative
        committed_data = committed_source.read_bytes()
        committed_hash = sha256_bytes(committed_data)
        declared_hash = target.get("after_sha256")
        if committed_hash != declared_hash:
            raise ValueError(f"Staged prose hash mismatch: {committed_source}")

        committed_output = committed_dir / current_path.name
        committed_output.write_bytes(committed_data)
        transaction_evidence_dir = evidence_transactions_dir / run_id
        transaction_evidence_dir.mkdir()
        shutil.copy2(transaction_manifest_path, transaction_evidence_dir / "manifest.json")
        marker = transaction_dir / "commit.marker"
        if marker.is_file():
            shutil.copy2(marker, transaction_evidence_dir / "commit.marker")

        current_hash = sha256_bytes(current_data)
        matches = current_hash == committed_hash
        item = {
            "chapter": chapter,
            "current_relative_path": current_path.relative_to(story_root).as_posix(),
            "exported_current_path": (current_dir / current_path.name).relative_to(output_root).as_posix(),
            "exported_committed_path": committed_output.relative_to(output_root).as_posix(),
            "current_sha256": current_hash,
            "committed_sha256": committed_hash,
            "matches_committed": matches,
            "current_stats": text_stats(current_data),
            "committed_stats": text_stats(committed_data),
            "run_id": run_id,
            "models": run["models"],
        }
        chapters_manifest.append(item)
        if not matches:
            drift.append(
                {
                    "chapter": chapter,
                    "current_sha256": current_hash,
                    "committed_sha256": committed_hash,
                    "current_path": item["exported_current_path"],
                    "committed_path": item["exported_committed_path"],
                }
            )
        current_merge_inputs.append((current_path, current_data))
        committed_merge_inputs.append((current_path, committed_data))

    merged_current = output_root / "旧稿合订本_当前正式版_第001-010章.md"
    merged_committed = output_root / "旧稿合订本_历史提交版_第001-010章.md"
    merge_chapters(story_root.name, current_merge_inputs, merged_current)
    merge_chapters(story_root.name, committed_merge_inputs, merged_committed)

    sources_dir = output_root / "story_sources_current"
    sources_dir.mkdir()
    for name in ("设定", "大纲", "追踪"):
        copy_tree(story_root / name, sources_dir / name)
    shutil.copy2(project_path, sources_dir / "project.json")

    chapter_one_run_id = runs[1]["run_id"]
    first_transaction_dir = persistence_root / chapter_one_run_id
    first_manifest = read_json(first_transaction_dir / "manifest.json")
    initial_state_dir = output_root / "initial_state_before_chapter_001"
    initial_state_dir.mkdir()
    initial_mapping: list[dict[str, Any]] = []
    for target in first_manifest.get("targets", []):
        if not isinstance(target, dict) or target.get("kind") not in INITIAL_STATE_NAMES:
            continue
        backup_relative = target.get("backup_path")
        if not isinstance(backup_relative, str):
            raise ValueError(f"Initial-state target has no backup: {target.get('kind')}")
        source = first_transaction_dir / backup_relative
        destination = initial_state_dir / INITIAL_STATE_NAMES[str(target["kind"])]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        initial_mapping.append(
            {
                "kind": target["kind"],
                "source_backup": backup_relative,
                "exported_path": destination.relative_to(output_root).as_posix(),
                "sha256": sha256_file(destination),
            }
        )
    shutil.copy2(first_transaction_dir / "manifest.json", initial_state_dir / "source_manifest.json")
    write_json(initial_state_dir / "mapping.json", initial_mapping)

    full_zip = output_root / f"{story_root.name}_完整StoryProject封存.zip"
    zip_story_project(story_root, full_zip)

    current_totals = Counter()
    committed_totals = Counter()
    for chapter in chapters_manifest:
        current_totals.update(chapter["current_stats"])
        committed_totals.update(chapter["committed_stats"])

    manifest = {
        "schema_version": 1,
        "archive_kind": "novelagent_old_story_baseline",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_story_project": str(story_root.resolve()),
        "book_name": story_root.name,
        "book_id": project.get("book_id"),
        "story_state_mode": project.get("story_state_mode"),
        "chapter_count": len(expected),
        "current_official_totals": dict(current_totals),
        "historical_committed_totals": dict(committed_totals),
        "run_status_counts": dict(sorted(run_statuses.items())),
        "committed_models": sorted({model for item in chapters_manifest for model in item["models"]}),
        "chapters": chapters_manifest,
        "drift": drift,
        "initial_state": {
            "source_run_id": chapter_one_run_id,
            "files": initial_mapping,
        },
        "git": git_metadata(ROOT),
        "artifacts": {
            "merged_current": {
                "path": merged_current.relative_to(output_root).as_posix(),
                "sha256": sha256_file(merged_current),
            },
            "merged_historical_committed": {
                "path": merged_committed.relative_to(output_root).as_posix(),
                "sha256": sha256_file(merged_committed),
            },
            "full_story_project_zip": {
                "path": full_zip.relative_to(output_root).as_posix(),
                "bytes": full_zip.stat().st_size,
                "sha256": sha256_file(full_zip),
            },
        },
    }
    manifest_path = output_root / "baseline_manifest.json"
    write_json(manifest_path, manifest)

    readme = output_root / "README.md"
    readme.write_text(
        "\n".join(
            [
                f"# 《{story_root.name}》旧稿封存",
                "",
                f"- 正式正文：{len(expected)} 章",
                f"- 当前正式版字符数：{current_totals['text_chars']}",
                f"- 历史运行：{dict(sorted(run_statuses.items()))}",
                f"- 正文漂移章节：{', '.join(str(item['chapter']) for item in drift) or '无'}",
                "",
                "`chapters_current/` 和“当前正式版”合订本是内容比较的默认旧稿。",
                "`chapters_historical_committed/` 保留运行事务实际提交的字节，用于过程审计。",
                "完整 StoryProject（包括运行记录、失败稿、拒绝稿、评审与持久化证据）位于 ZIP 封存包。",
                "`initial_state_before_chapter_001/` 是后续隔离重写可使用的第 1 章前状态。",
                "所有文件哈希见 `SHA256SUMS.txt`，结构化元数据见 `baseline_manifest.json`。",
                "",
            ]
        ),
        encoding="utf-8",
    )

    checksum_lines: list[str] = []
    for path in sorted(output_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksum_lines.append(f"{sha256_file(path)}  {path.relative_to(output_root).as_posix()}")
    (output_root / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export and seal a NovelAgent StoryProject baseline.")
    parser.add_argument(
        "--story-project",
        default="auto",
        help="StoryProject root, or auto to resolve .active-book.",
    )
    parser.add_argument("--out", required=True, help="New output directory; it must not already exist.")
    args = parser.parse_args(argv)

    resolution = resolve_story_project_root(args.story_project, workspace_root=ROOT)
    if resolution.error or resolution.root is None:
        print(f"StoryProject resolution failed: {resolution.error}", file=sys.stderr)
        return 1
    try:
        manifest = export_archive(resolution.root, Path(args.out))
    except Exception as exc:
        print(f"Story archive export failed: {exc}", file=sys.stderr)
        return 1

    print(f"Story archive: {Path(args.out).resolve()}")
    print(f"Chapters: {manifest['chapter_count']}")
    print(f"Current chars: {manifest['current_official_totals']['text_chars']}")
    print(f"Drift chapters: {[item['chapter'] for item in manifest['drift']]}")
    print(f"ZIP SHA-256: {manifest['artifacts']['full_story_project_zip']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
