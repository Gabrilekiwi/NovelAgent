from __future__ import annotations

from pathlib import Path
import argparse
import os
import sys
import socket
import threading
import webbrowser

import uvicorn

from workbench.app import create_app, _local_app_root
from workbench.instance import InstanceAlreadyRunning, InstanceLease, existing_instance_url
from workbench.backup_archive import _lineage
from workbench.backup_files import _ordinary
from workbench.restore_scope import RestoreScopeError, load_restore_scope


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 NovelAgent Windows 本地创作工作台")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765；0 表示自动选择")
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="允许导入或创建作品的父目录；可重复传入",
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    directories = parser.add_mutually_exclusive_group()
    directories.add_argument("--data-dir", type=Path, help="工作台数据目录；默认使用本机应用数据目录")
    directories.add_argument("--restore-dir", type=Path, help="打开已核验的恢复副本目录；不会自动执行任务")
    parser.add_argument("--no-active-import", action="store_true", help="不自动登记当前活动作品")
    return parser


def _launch_database(directory: Path, *, require_restore: bool = False) -> tuple[Path, bool]:
    """Choose the original restored database before creating any startup files.

    A partial, unqualified or ambiguous restore must never open a new empty
    workbench.db. create_app rechecks qualification while holding the lease.
    """
    restored = directory / "workbench.sqlite"
    restore_evidence = any(os.path.lexists(directory / name) for name in (
        "restore-intent.json", "restore-plan.json", "workbench.sqlite",
        "workbench.sqlite.restore-scope.json",
    ))
    if not require_restore and not restore_evidence:
        return directory / "workbench.db", False
    _lineage(directory)
    if os.path.lexists(directory / "workbench.db"):
        raise RestoreScopeError("恢复目录同时包含普通数据库，无法确定要打开的工作台，请核对目录。")
    # Opening even a read-only WAL database may create/change shared-memory
    # files. Missing evidence must be rejected before SQLite is opened.
    marker = restored.with_name(restored.name + ".restore-scope.json")
    if not os.path.lexists(restored) or not os.path.lexists(marker):
        raise RestoreScopeError("恢复数据库或独立范围凭据缺失，不能启动此副本。")
    _ordinary(restored)
    _ordinary(marker)
    scope = load_restore_scope(restored,allow_recovery_controller=True)
    if scope is None:
        raise RestoreScopeError("恢复副本尚未完成范围核验，不能作为普通工作台启动。")
    return restored, True


def main() -> int:
    args = build_parser().parse_args()
    repository = Path(__file__).resolve().parent
    workspaces = [Path(value).resolve(strict=True) for value in args.workspace] or [repository]
    directory = Path(args.restore_dir or args.data_dir or _local_app_root()).expanduser().absolute()
    try:
        database_path, restored = _launch_database(directory, require_restore=args.restore_dir is not None)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"工作台未启动：{exc}", file=sys.stderr)
        return 2
    try:
        lease = InstanceLease(directory).acquire()
    except InstanceAlreadyRunning:
        url = existing_instance_url(directory)
        if url:
            print(f"工作台已在运行：{url}", flush=True)
            if not args.no_browser:
                webbrowser.open(url, new=1)
            return 0
        print("工作台或旧执行子进程仍占用此数据目录，当前无法确认可访问的工作台地址。"
              "请等待执行结束或检查原进程；本次未接管或恢复任务。", file=sys.stderr)
        return 2
    listener = None
    timer = None
    app = None
    try:
        listener = _bound_listener(args.port)
        port = listener.getsockname()[1]
        app = create_app(
            repo_root=repository, allowed_workspaces=workspaces, start_worker=not restored,
            database_path=database_path, runtime_root=directory / "jobs",
            instance_lease=lease, auto_import_active=not restored and not args.no_active_import,
        )
        url = f"http://127.0.0.1:{port}/"
        lease.publish(url)
        if not args.no_browser:
            timer = threading.Timer(0.8, lambda: webbrowser.open(url, new=1))
            timer.daemon = True
            timer.start()
        if args.port and port != args.port:
            print(f"端口 {args.port} 已被占用，已自动改用 {port}。")
        print(f"NovelAgent 创作工作台：{url}", flush=True)
        print("仅监听 127.0.0.1；关闭此窗口即可停止工作台。", flush=True)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                             log_level="info", access_log=False))
        server.run(sockets=[listener])
        return 0 if server.started else 1
    finally:
        if timer:
            timer.cancel()
        if listener:
            listener.close()
        if app is not None:
            app.state.supervisor.shutdown()
        lease.close()


def _bound_listener(requested: int) -> socket.socket:
    if isinstance(requested, bool) or not 0 <= requested <= 65535:
        raise ValueError("port must be between 0 and 65535")
    candidates = list(range(requested, min(65535, requested + 19) + 1)) if requested else []
    for port in [*candidates, 0]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            return listener
        except OSError:
            listener.close()
            if port == 0:
                raise
    raise RuntimeError("No available local port")


if __name__ == "__main__":
    raise SystemExit(main())
