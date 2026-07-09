from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.review.index import get_latest_review, list_recent_reviews  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect runtime review artifact index entries.")
    parser.add_argument("--review-output-dir", default=".tmp/runtime/reviews", help="Runtime review artifact root.")
    parser.add_argument("--list", action="store_true", help="List recent reviews instead of the latest review.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum entries to list.")
    parser.add_argument("--status", choices=["pass", "warning", "needs_revision", "blocked", "error", "unknown"], default=None)
    parser.add_argument("--gate-status", choices=["disabled", "pass", "fail", "error"], default=None)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    if args.list:
        entries = list_recent_reviews(
            review_output_dir=args.review_output_dir,
            limit=args.limit,
            status=args.status,
            gate_status=args.gate_status,
        )
        payload = {"ok": True, "count": len(entries), "entries": entries}
    else:
        payload = {"ok": True, "latest": get_latest_review(review_output_dir=args.review_output_dir)}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.list:
        if not payload["entries"]:
            print("No review entries found.")
        else:
            print("Recent reviews:")
            for index, entry in enumerate(payload["entries"], start=1):
                print(
                    f"{index}. {entry.get('run_id')} - {entry.get('review_status')} - "
                    f"gate={entry.get('gate_status')} - quality={entry.get('quality_score')} - rule={entry.get('rule_score')}"
                )
        return 0

    latest = payload["latest"]
    if not latest:
        print("No review entries found.")
    else:
        print(f"Latest review: {latest.get('run_id')} {latest.get('review_status')}")
        if latest.get("human_report_path"):
            print(f"Human report: {latest.get('human_report_path')}")
        if latest.get("summary_path"):
            print(f"Summary: {latest.get('summary_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
