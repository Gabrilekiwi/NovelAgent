"""Create/update only this repository's .venv using the checked-in dependency lock."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-only", action="store_true", help="Omit pytest and development tools")
    args = parser.parse_args()
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        print("Use CPython 3.12, e.g. py -3.12 scripts/setup_environment.py", file=sys.stderr)
        return 1
    target = ROOT / ".venv"
    python = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
    # Reject a misplaced/global interpreter or another Python minor version before pip runs.
    subprocess.run([str(python), "-c",
                    "import pathlib,sys; "
                    "assert sys.implementation.name == 'cpython', 'Expected CPython'; "
                    "assert sys.version_info[:2] == (3,12), 'Existing .venv must use Python 3.12'; "
                    "assert sys.prefix != sys.base_prefix, 'Expected virtual environment'; "
                    "assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve(), 'Unexpected .venv location'",
                    str(target)], check=True)
    # A cancelled Windows install may leave python.exe but no pip. Repair only
    # this verified project venv before attempting to install its locked packages.
    if subprocess.run([str(python), "-c", "import pip"], capture_output=True).returncode:
        subprocess.run([str(python), "-m", "ensurepip", "--upgrade", "--default-pip"], check=True)
    lock = "requirements.lock" if args.runtime_only else "requirements-dev.lock"
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-input", "-r", str(ROOT / lock)], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    command = [str(python), "-B", str(ROOT / "scripts/check_environment.py")]
    if not args.runtime_only:
        command.append("--dev")
    subprocess.run(command, check=True)
    print("Project environment ready. No model calls or workbench startup performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
