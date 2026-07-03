from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state.snapshot_tools import inspect_snapshot_text, load_normalized_snapshot, write_normalized_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect and normalize a NovelAgent snapshot as UTF-8 JSON.")
    parser.add_argument("--snapshot", required=True, help="Snapshot JSON path.")
    parser.add_argument(
        "--write-normalized",
        action="store_true",
        help="Rewrite the snapshot as normalized UTF-8 JSON after validation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    snapshot = load_normalized_snapshot(args.snapshot)
    report = inspect_snapshot_text(snapshot)
    report["snapshot"] = str(Path(args.snapshot))
    if args.write_normalized:
        write_normalized_snapshot(snapshot, args.snapshot)
        report["written"] = True
    else:
        report["written"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
