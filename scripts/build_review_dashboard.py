from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.review.dashboard import build_review_dashboard_from_index  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a static NovelAgent review dashboard.")
    parser.add_argument("--review-output-dir", default=".tmp/runtime/reviews", help="Runtime review artifact root.")
    parser.add_argument("--out", default=None, help="Output HTML path. Defaults to <review-output-dir>/dashboard.html.")
    parser.add_argument("--title", default="NovelAgent Review Dashboard", help="Dashboard title.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    result = build_review_dashboard_from_index(
        review_output_dir=args.review_output_dir,
        output_path=args.out,
        title=args.title,
    )
    payload = {"ok": True, "dashboard": result["metadata"]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        metadata = result["metadata"]
        print("Review dashboard generated:")
        print(f"- output: {metadata.get('output_path')}")
        print(f"- entries: {metadata.get('entry_count')}")
        print(f"- latest_run_id: {metadata.get('latest_run_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
