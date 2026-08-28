from __future__ import annotations

from pathlib import Path
import argparse
import socket
import threading
import webbrowser

import uvicorn

from workbench.app import create_app


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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    repository = Path(__file__).resolve().parent
    workspaces = [Path(value).resolve(strict=True) for value in args.workspace] or [repository]
    port = _available_port(args.port)
    app = create_app(
        repo_root=repository,
        allowed_workspaces=workspaces,
        start_worker=True,
    )
    url = f"http://127.0.0.1:{port}/"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url, new=1)).start()
    if args.port and port != args.port:
        print(f"端口 {args.port} 已被占用，已自动改用 {port}。")
    print(f"NovelAgent 创作工作台：{url}")
    print("仅监听 127.0.0.1；关闭此窗口即可停止工作台。")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )


def _available_port(requested: int) -> int:
    if isinstance(requested, bool) or requested < 0 or requested > 65535:
        raise ValueError("port must be between 0 and 65535")
    if requested:
        last_candidate = min(65535, requested + 19)
        for candidate in range(requested, last_candidate + 1):
            if _port_can_bind(candidate):
                return candidate
    return _ephemeral_port()


def _port_can_bind(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
            handle.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


if __name__ == "__main__":
    main()
