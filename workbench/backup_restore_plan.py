"""Read-only plan for a new single-book restore container on this Windows host."""
import json
import os
from pathlib import Path

from core.engine.root_registry import validate_root_registry
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError, _lineage, _member, _file_hash
from workbench.backup_archive_verification import verify_archived_target, _implementation
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_archive_database import _database


def _target_layout(view, destination, package, *, registered_roots, _existing=False):
    target = Path(destination)
    if (not target.is_absolute() or ".." in target.parts or len(target.parts) < 2
            or len(target.drive) != 2 or target.drive[1:] != ":"):
        raise BackupArchiveError("恢复目标必须是本机盘符下的新目录。")
    _member("/".join(target.parts[1:]))
    scope = view.manifest["preview"]["scope"]
    sources = [Path(tree["source_root"]) for tree in scope["inventories"].values()]
    sources.append(Path(package).absolute())
    sources.extend(Path(path) for path in registered_roots)
    configured = scope.get("task_runtime_root")
    runtime = Path(configured) if isinstance(configured, str) else None
    if runtime is None or not runtime.is_absolute() or ".." in runtime.parts:
        raise BackupArchiveError("恢复计划缺少服务器原任务目录绑定。")
    sources.append(runtime)
    # Source paths are compared lexically, never opened. Inspect target parents
    # only after rejecting overlap, including a source that no longer exists.
    if any(target.is_relative_to(source) or source.is_relative_to(target) for source in sources):
        raise BackupArchiveError("恢复目标不能与归档或原作品范围重叠。")
    if os.path.lexists(target):
        if not _existing:
            raise BackupArchiveError("恢复目标必须是尚不存在的新目录。")
        _lineage(target)
    elif _existing:
        raise BackupArchiveError("待绑定的恢复容器不存在。")
    parent_binding = _lineage(target.parent)
    # Reject short-name aliases and noncanonical spelling before binding parents.
    if target.parent.resolve() != target.parent:
        raise BackupArchiveError("恢复目标父目录必须使用规范的物理路径。")
    book = target / "book"
    jobs = target / "jobs"
    for name in ('workbench.sqlite', 'restore-intent.json', 'restore-plan.json', 'restore-materialization.json',
                 'workbench.sqlite.restore-scope.json', '.instance-' + '0'*32 + '.tmp'):
        if len(str(target / name).encode('utf-16-le')) // 2 > 259:
            raise BackupArchiveError("恢复控制文件超出当前 Windows 路径长度支持范围。")
    original_book = Path(scope["inventories"]["story_project"]["source_root"])
    effective_runtime = book / runtime.relative_to(original_book) if runtime.is_relative_to(original_book) else jobs
    mappings = []
    planned = set()
    for key, tree in sorted(scope["inventories"].items()):
        if key == "story_project":
            root = book
        elif key.startswith("jobs/"):
            _member(key)
            root = jobs / key[5:]
        else:
            raise BackupArchiveError("恢复计划包含不受支持的外部根。")
        for relative in [".", *tree["directories"], *tree["files"]]:
            path = root if relative == "." else root / relative
            length = len(str(path).encode("utf-16-le")) // 2
            limit = 259 if relative in tree["files"] else 247
            if length > limit:
                raise BackupArchiveError("恢复成员超出当前 Windows 路径长度支持范围。")
            coordinate = str(path).casefold()
            if coordinate in planned:
                raise BackupArchiveError("恢复根映射产生重复目标。")
            planned.add(coordinate)
        mappings.append({"root":key, "source_root":tree["source_root"],
            "archive_path":view.manifest["roots"][key]["archive_path"],
            "inventory_sha256":tree["inventory_sha256"], "destination":str(root)})
    return {"container":str(target), "book_root":str(book),
            "database":str(target / "workbench.sqlite"), "task_runtime_root":str(effective_runtime),
            "parent_binding":parent_binding, "roots":mappings}


def plan_archived_restore(package, *, project_id, book_id, destination, temporary_root=None):
    """A plan cannot create a directory, register a book or authorize activation."""
    return _plan_archived_restore(package, project_id=project_id, book_id=book_id,
        destination=destination, temporary_root=temporary_root)


def _plan_archived_restore(package, *, project_id, book_id, destination, temporary_root=None, _existing=False):
    # Only the materialized-copy binder accepts an existing container. It must
    # independently prove the saved intent, completion and full copied bytes.
    data = verify_archived_target(package, project_id=project_id, book_id=book_id, temporary_root=temporary_root)
    with verified_archive_view(package) as view:
        if view.verified["receipt_sha256"] != data["archive_receipt_sha256"]:
            raise BackupArchiveError("恢复规划前归档完成凭据发生变化。")
        with _database(view, temporary_root=temporary_root) as connection:
            registered_roots = [row[0] for row in connection.execute("SELECT root FROM projects ORDER BY id LIMIT 100001")]
            if len(registered_roots) > 100000:
                raise BackupArchiveError("恢复目标登记范围超过支持上限。")
        layout = _target_layout(view, destination, package, registered_roots=registered_roots, _existing=_existing)
        archive_bytes = _file_hash(Path(package).absolute() / "archive.zip")
        tree = view.manifest["preview"]["scope"]["inventories"]["story_project"]
        source = Path(tree["source_root"])
        registries = []
        for name in sorted(tree["files"]):
            if Path(name).name != "root_registry.json":
                continue
            raw = view.read_root("story_project", name)
            value = validate_root_registry(json.loads(raw))
            roots = []
            for key, binding in sorted(value["roots"].items()):
                original = Path(binding["path"])
                if not original.is_absolute() or ".." in original.parts or not original.is_relative_to(source):
                    raise BackupArchiveError("恢复计划不能扩展未归档的 registry 根。")
                roots.append({"root_id":key, "root_uuid":binding["root_uuid"],
                    "source_path":binding["path"],
                    "destination":str(Path(layout["book_root"]) / original.relative_to(source)),
                    "historical_binding":binding})
            registries.append({"path":name, "sha256":digest(raw),
                "registry_id":value["registry_id"], "registry_digest":value["registry_digest"],
                "revision":value["revision"], "roots":roots})
        if _target_layout(view, destination, package, registered_roots=registered_roots, _existing=_existing) != layout:
            raise BackupArchiveError("恢复规划期间目标父目录变化。")
        if _implementation()["fingerprint_sha256"] != data["verifier"]["implementation_sha256"]:
            raise BackupArchiveError("恢复规划期间验证器磁盘版本变化。")
        result = {"schema_version":"target_restore_plan_v1",
            "archive_id":data["archive_id"], "archive_receipt_sha256":data["archive_receipt_sha256"],
            "archive_binding":{**data["archive_binding"], "archive":archive_bytes}, "verifier":data["verifier"],
            "data_verification_sha256":data["verification_sha256"],
            "project_id":project_id, "book_id":book_id,
            "identity_sha256":data["identity_sha256"],
            "root_plan":layout, "historical_registries":registries,
            "history_policy":{"preserve_original_bytes":True, "execute_old_path_guards":False,
                "new_physical_controls_required":True, "replay_uncommitted_plans":False},
            "database_policy":data["database_policy"],
            "execution_policy":data["execution_policy"],
            "ownership_requirement":data["ownership_requirement"],
            "blockers":data["blockers"], "continuation_blockers":data["continuation_blockers"],
            "offline_data_verified":data["offline_data_verified"],
            "plan_ready":data["offline_data_verified"],
            "recoverable":False, "write_activation_allowed":False,
            "restored":False, "other_project_isolation_enforced":False}
        provenance = view.manifest['preview']['scope'].get('restored_provenance')
        if provenance is not None:
            result['source_restore_provenance'] = provenance
        result["plan_sha256"] = digest(result)
    return result
