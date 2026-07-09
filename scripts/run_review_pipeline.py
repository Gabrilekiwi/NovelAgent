from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.review import run_review_pipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic chapter review pipeline.")
    parser.add_argument("--chapter", required=True, help="Path to chapter prose. Read only; never modified.")
    parser.add_argument("--snapshot", required=True, help="Path to runtime snapshot JSON.")
    parser.add_argument("--previous", default=None, help="Optional previous chapter path.")
    parser.add_argument("--rules", default=None, help="Optional narrative rule pack JSON path.")
    parser.add_argument("--default-rules", dest="default_rules", action="store_true", default=True, help="Use default rules.")
    parser.add_argument("--no-default-rules", dest="default_rules", action="store_false", help="Disable default rules.")
    parser.add_argument("--out-dir", default=None, help="Optional artifact output directory.")
    parser.add_argument("--no-repair-prompt", action="store_true", help="Skip repair prompt generation.")
    parser.add_argument("--no-human-report", action="store_true", help="Skip human review report generation.")
    parser.add_argument("--title", default=None, help="Optional human review report title.")
    parser.add_argument("--json", action="store_true", help="Print only summary JSON to stdout.")
    args = parser.parse_args(argv)

    try:
        if not args.default_rules and not args.rules:
            raise ValueError("missing rules: pass --rules or omit --no-default-rules")

        chapter_text = Path(args.chapter).read_text(encoding="utf-8")
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        previous_chapter_text = Path(args.previous).read_text(encoding="utf-8") if args.previous else None

        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous_chapter_text,
            rule_pack_path=args.rules,
            use_default_rules=args.default_rules,
            output_dir=args.out_dir,
            build_repair_prompt=not args.no_repair_prompt,
            build_human_report=not args.no_human_report,
            title=args.title,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise non-zero diagnostic.
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"Review pipeline failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Review pipeline: {summary['status']}")
        print(f"Decision: {summary['decision']['label']}")
        print(f"Quality score: {summary['scores']['quality_score']}")
        print(f"Rule score: {summary['scores']['rule_score']}")
        print(
            "Repair tasks: "
            f"{summary['tasks']['repair_task_count']} total, "
            f"{summary['tasks']['blocking_task_count']} blocking"
        )
        if args.out_dir:
            print(f"Artifacts: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
