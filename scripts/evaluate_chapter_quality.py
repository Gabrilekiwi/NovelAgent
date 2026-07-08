from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.quality import evaluate_chapter_quality  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate generated chapter quality with local heuristic checks.")
    parser.add_argument("--chapter", required=True, help="Path to the generated chapter text.")
    parser.add_argument("--snapshot", required=True, help="Path to the runtime snapshot JSON.")
    parser.add_argument("--previous", default=None, help="Optional path to the previous chapter text.")
    parser.add_argument("--language", default=None, help="Target language. Defaults to snapshot.project_profile.language.")
    parser.add_argument("--out", default=None, help="Optional output path for chapter_quality_report.json.")
    parser.add_argument("--json", action="store_true", help="Print only the JSON report to stdout.")
    args = parser.parse_args(argv)

    try:
        chapter_text = Path(args.chapter).read_text(encoding="utf-8")
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        previous_chapter_text = (
            Path(args.previous).read_text(encoding="utf-8")
            if args.previous
            else None
        )
        report = evaluate_chapter_quality(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous_chapter_text,
            language=args.language,
        )
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"Chapter quality evaluation failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Chapter quality: {report['status']} score={report['score']}")
        summary = report["summary"]
        print(
            "Checks: "
            f"{summary['passed']} passed, "
            f"{summary['warnings']} warnings, "
            f"{summary['failed']} failed, "
            f"{summary['skipped']} skipped"
        )
        if args.out:
            print(f"Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
