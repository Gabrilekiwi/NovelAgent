from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_v2 import compile_memory_v2  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile v1 memory into Memory System V2 artifacts.")
    parser.add_argument("--memory", required=True, help="Path to a v1 memory JSON file.")
    parser.add_argument("--out", required=True, help="Output directory for Memory V2 artifacts.")
    parser.add_argument("--book-id", default="default")
    parser.add_argument("--title", default="Untitled")
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print only the JSON compile report to stdout.")
    args = parser.parse_args(argv)

    try:
        report = compile_memory_v2(
            memory_path=args.memory,
            output_dir=args.out,
            book_id=args.book_id,
            title=args.title,
            language=args.language,
            reset=args.reset,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"Memory V2 compile failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("Memory V2 compile: ok")
        print(f"Canonical: {report['outputs']['canonical_memory']}")
        print(
            "Revision: "
            f"{report['canonical_memory']['previous_revision']} -> {report['canonical_memory']['revision']}"
        )
        print(f"Events: {report['events']['event_count']}")
        print(f"Snapshot preview: {report['outputs']['snapshot_preview']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
