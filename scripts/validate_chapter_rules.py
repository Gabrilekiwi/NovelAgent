from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rules import RuleValidationError, validate_chapter_against_rules  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a chapter against a Narrative Rule Pack.")
    parser.add_argument("--chapter", required=True, help="Path to the generated chapter text.")
    parser.add_argument("--snapshot", required=True, help="Path to the runtime snapshot JSON.")
    parser.add_argument("--previous", default=None, help="Optional path to the previous chapter text.")
    parser.add_argument("--rules", default=None, help="Optional narrative rule pack JSON path.")
    parser.add_argument("--default-rules", action="store_true", help="Use rules/default_narrative_rule_pack.json.")
    parser.add_argument("--quality-report", default=None, help="Optional existing chapter_quality_report JSON path.")
    parser.add_argument(
        "--min-severity",
        choices=["low", "medium", "high", "critical"],
        default=None,
        help="Only validate rules at or above this severity.",
    )
    parser.add_argument("--category", action="append", default=None, help="Rule category to include. Repeatable.")
    parser.add_argument("--out", default=None, help="Optional output path for rule_validation_report.json.")
    parser.add_argument("--json", action="store_true", help="Print only the JSON report to stdout.")
    args = parser.parse_args(argv)

    try:
        if not args.rules and not args.default_rules:
            raise RuleValidationError("missing rule pack: pass --rules or --default-rules")

        chapter_text = Path(args.chapter).read_text(encoding="utf-8")
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        previous_chapter_text = (
            Path(args.previous).read_text(encoding="utf-8")
            if args.previous
            else None
        )
        quality_report = (
            json.loads(Path(args.quality_report).read_text(encoding="utf-8"))
            if args.quality_report
            else None
        )

        report = validate_chapter_against_rules(
            chapter_text=chapter_text,
            snapshot=snapshot,
            rule_pack_path=args.rules,
            use_default_rules=args.default_rules,
            previous_chapter_text=previous_chapter_text,
            quality_report=quality_report,
            min_severity=args.min_severity,
            categories=args.category,
        )

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise non-zero diagnostic.
        print(f"Rule validation failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Rule validation: {report['status']} score={report['score']}")
        summary = report["summary"]
        print(
            "Rules: "
            f"{summary['passed']} passed, "
            f"{summary['warnings']} warnings, "
            f"{summary['failed']} failed, "
            f"{summary['skipped']} skipped"
        )
        if report["violations"]:
            print("Violations:")
            for violation in report["violations"]:
                print(f"- {violation['rule_code']} [{violation['severity']}] {violation['status']}")
        if args.out:
            print(f"Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
