from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.review import run_review_regression_suite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic review regression fixtures.")
    parser.add_argument("--manifest", required=True, help="Path to review regression manifest JSON.")
    parser.add_argument("--out", default=None, help="Optional summary JSON output path.")
    parser.add_argument("--artifacts-dir", default=None, help="Optional directory for per-case review artifacts.")
    parser.add_argument("--json", action="store_true", help="Print only summary JSON to stdout.")
    args = parser.parse_args(argv)

    try:
        summary = run_review_regression_suite(
            manifest_path=args.manifest,
            artifacts_dir=args.artifacts_dir,
        )
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should provide a concise non-zero diagnostic.
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"Review regression failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        counts = summary["summary"]
        print(f"Review regression: {summary['status']}")
        print(f"Cases: {counts['passed']} passed, {counts['failed']} failed, {counts['case_count']} total")
        if args.out:
            print(f"Summary: {args.out}")
        if args.artifacts_dir:
            print(f"Artifacts: {args.artifacts_dir}")
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
