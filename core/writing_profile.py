"""Per-workbench writing model, outside installed code, manuscripts and backups."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


class ProfileError(ValueError):
    pass


PROFILE_ENV = 'NOVELAGENT_WRITING_PROFILE'
_reader = ContextVar('writing_profile_reader', default=None)


def profile_path(data_dir: Path) -> Path:
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.config')
    scope = os.path.normcase(str(data_dir.resolve())).encode('utf-8')
    return base / 'NovelAgentSettings' / (hashlib.sha256(scope).hexdigest() + '.profile')


def _windows_protect(data: bytes, *, decrypt: bool) -> bytes:
    import ctypes as c
    from ctypes import wintypes as w

    class Blob(c.Structure):
        _fields_ = [('size', w.DWORD), ('ptr', c.c_void_p)]

    lib = c.WinDLL('crypt32', use_last_error=True)
    func = lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    ptr = c.POINTER(Blob)
    func.argtypes = [ptr, c.c_void_p, ptr, c.c_void_p, c.c_void_p, w.DWORD, ptr]
    func.restype = w.BOOL
    buf = c.create_string_buffer(data)
    source, output = Blob(len(data), c.cast(buf, c.c_void_p)), Blob()
    if not func(c.byref(source), None, None, None, None, 1, c.byref(output)):
        raise ProfileError('当前系统账户无法保护或读取模型设置。')
    try:
        return c.string_at(output.ptr, output.size)
    finally:
        free = c.WinDLL('kernel32').LocalFree
        free.argtypes = [c.c_void_p]; free.restype = c.c_void_p
        free(output.ptr)


def read_profile(path: Path) -> dict | None:
    try:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        if os.name == 'nt':
            raw = _windows_protect(raw, decrypt=True)
        elif path.stat().st_mode & 0o077:
            raise ProfileError('模型设置文件的访问权限不正确。')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'api_key', 'model', 'base_url'} or not all(isinstance(v, str) for v in value.values()) or not value['api_key'] or not value['model']:
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ProfileError('无法读取本机模型设置，请重新填写密钥并保存。') from None


def save_profile(path: Path, value: dict) -> None:
    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if os.name == 'nt':
        raw = _windows_protect(raw, decrypt=False)
    try:
        # Windows inherits the user profile ACL; the contents are protected by DPAPI.
        path.parent.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
        tmp = path.parent / ("." + uuid4().hex + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(raw); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    except OSError:
        raise ProfileError('无法保存本机模型设置，请检查当前账户的写入权限。') from None


@contextmanager
def use_profile(path: Path):
    # An in-flight chapter keeps one consistent configuration even if another tab saves.
    token = _reader.set(lru_cache(maxsize=1)(lambda: read_profile(path)))
    try:
        yield
    finally:
        _reader.reset(token)


def current_profile():
    reader = _reader.get()
    if reader is not None:
        return reader()
    path = os.environ.get(PROFILE_ENV)
    return read_profile(Path(path)) if path else None
