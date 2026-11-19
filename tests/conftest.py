from __future__ import annotations

from pathlib import Path
import re
import shutil
from typing import Iterator
import uuid

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    # A fresh checkout (or an independent CI group) has no preceding test
    # that can be relied on to create the shared parent for short test paths.
    (session.config.rootpath / ".tmp").mkdir(exist_ok=True)


def _case_name(node_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", node_name).strip("-")
    return (normalized or "case")[:48]


@pytest.fixture
def tmp_path(
    request: pytest.FixtureRequest,
) -> Iterator[Path]:
    # Offline source-loss tests may move only disposable roots inside .tmp.
    # Python 3.12 can translate pytest's mode=0o700 temporary directories into
    # a private Windows DACL that managed child operations cannot traverse.
    # A unique directory under the workspace inherits the usable parent ACL.
    root = Path.cwd() / ".tmp" / "pytest-cases"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_case_name(request.node.name)}-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
