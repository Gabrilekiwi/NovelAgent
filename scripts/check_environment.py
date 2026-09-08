"""Check the pinned environment without importing the app or loading credentials."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            versions.update(locked_versions(path.parent / line[3:].strip()))
            continue
        requirement, _, marker = line.partition(";")
        if marker:
            if marker.strip() != 'sys_platform == "win32"':
                raise ValueError(f"Unsupported lock marker: {marker}")
            if sys.platform != "win32":
                continue
        name, version = requirement.strip().split("==", 1)
        versions[name] = version
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="Also check test dependencies")
    args = parser.parse_args()
    problems: list[str] = []
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        problems.append("This locked baseline requires CPython 3.12.")
    if sys.prefix == sys.base_prefix:
        problems.append("Use the project virtual environment, not global Python.")
    lock = ROOT / ("requirements-dev.lock" if args.dev else "requirements.lock")
    expected = locked_versions(lock)
    for name, wanted in expected.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        if actual != wanted:
            problems.append(f"{name}: expected {wanted}, found {actual}")
    print(json.dumps({"ok": not problems, "python": sys.version.split()[0],
                      "executable": sys.executable, "lock": lock.name,
                      "checked_packages": len(expected), "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
