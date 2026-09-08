from __future__ import annotations

from pathlib import Path
import argparse
import sys
import socket
import threading
import webbrowser

import uvicorn

from workbench.app import create_app, _local_app_root
from workbench.instance import InstanceAlreadyRunning, InstanceLease, existing_instance_url


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
    parser.add_argument("--data-dir", type=Path, help="工作台数据目录；默认使用本机应用数据目录")
    parser.add_argument("--no-active-import", action="store_true", help="不自动登记当前活动作品")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repository = Path(__file__).resolve().parent
    workspaces = [Path(value).resolve(strict=True) for value in args.workspace] or [repository]
    directory = Path(args.data_dir or _local_app_root()).expanduser().resolve()
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
            repo_root=repository, allowed_workspaces=workspaces, start_worker=True,
            database_path=directory / "workbench.db", runtime_root=directory / "jobs",
            instance_lease=lease, auto_import_active=not args.no_active_import,
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
