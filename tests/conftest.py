from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Iterator
import uuid

import pytest


def _case_name(node_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", node_name).strip("-")
    return (normalized or "case")[:48]


@pytest.fixture
def tmp_path(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    if os.name != "nt":
        yield tmp_path_factory.mktemp(_case_name(request.node.name), numbered=True)
        return

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
