"""OS-backed ownership shared with engine children, never inferred from a PID file."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid


class InstanceAlreadyRunning(RuntimeError):
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        super().__init__("此数据目录仍由工作台或执行子进程占用，不能启动第二个写入者。")


class InstanceLease:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.instance_id = uuid.uuid4().hex
        self._fd: int | None = None
        self._mutex = threading.RLock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> InstanceLease:
        with self._mutex:
            if self.held:
                raise RuntimeError("Instance lease already acquired")
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / "workbench.lock"
            if os.name == "nt":
                self._fd = _windows_open_exclusive(path)
            else:
                import fcntl
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(fd)
                    raise InstanceAlreadyRunning(self.directory) from exc
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            try:
                self.publish(None)
            except BaseException:
                self.close()
                raise
            return self

    def publish(self, url: str | None) -> None:
        with self._mutex:
            self.require_held()
            target = self.directory / "workbench-instance.json"
            temporary = self.directory / f".instance-{self.instance_id}.tmp"
            data = {"instance_id": self.instance_id, "pid": os.getpid(), "url": url}
            try:
                temporary.write_text(json.dumps(data), encoding="utf-8")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

    def require_held(self) -> None:
        if not self.held:
            raise RuntimeError("Workbench instance ownership has been released")

    def close(self) -> None:
        with self._mutex:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            # Never unlink the lock or explicitly unlock flock: an inherited
            # handle may still protect a running engine after the parent exits.

    def spawn(self, factory: Callable[..., Any], command: list[str], **kwargs: Any) -> Any:
        with self._mutex:
            self.require_held()
            assert self._fd is not None
            if os.name == "nt":
                import msvcrt
                handle = msvcrt.get_osfhandle(self._fd)
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {"handle_list": [handle]}
                os.set_handle_inheritable(handle, True)
                try:
                    return factory(command, close_fds=True, startupinfo=startup,
                                   creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
                finally:
                    os.set_handle_inheritable(handle, False)
            return factory(command, close_fds=True, pass_fds=(self._fd,), **kwargs)


def _windows_open_exclusive(path: Path) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # READ|WRITE access, sharing READ only; the restriction survives in each
    # inherited child handle until its process closes that handle/exits.
    handle = create(str(path), 0xC0000000, 1, None, 4, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {32, 33}:
            raise InstanceAlreadyRunning(path.parent)
        raise ctypes.WinError(error)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    os.set_inheritable(fd, False)
    return fd


def existing_instance_url(directory: Path, *, timeout: float = 5.0) -> str | None:
    """Only reuse an address whose live HTTP identity matches the owner's record."""
    deadline = time.monotonic() + timeout
    while True:
        connection = None
        try:
            data = json.loads((directory / "workbench-instance.json").read_text(encoding="utf-8"))
            address = urlsplit(data.get("url") or "")
            if (isinstance(data.get("instance_id"), str) and len(data["instance_id"]) == 32
                    and address.scheme == "http" and address.hostname == "127.0.0.1"
                    and address.port and not address.username and not address.password
                    and address.path in {"", "/"} and not address.query and not address.fragment):
                connection = http.client.HTTPConnection("127.0.0.1", address.port, timeout=0.3)
                connection.request("GET", "/api/instance")
                response = connection.getresponse()
                value = json.loads(response.read(4096))
                if response.status == 200 and value.get("instance_id") == data.get("instance_id"):
                    return f"http://127.0.0.1:{address.port}/"
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)
