from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.review import build_human_review_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a human-readable Markdown review report from audit artifacts.")
    parser.add_argument("--chapter", default=None, help="Optional chapter text path. Read only; never modified.")
    parser.add_argument("--quality-report", required=True, help="Path to chapter_quality_report JSON.")
    parser.add_argument("--rule-validation-report", required=True, help="Path to rule_validation_report JSON.")
    parser.add_argument("--rule-repair-plan", required=True, help="Path to rule_repair_plan JSON.")
    parser.add_argument("--repair-prompt-metadata", default=None, help="Optional rule_repair_prompt_metadata JSON path.")
    parser.add_argument("--title", default=None, help="Optional Markdown report title.")
    parser.add_argument("--out", default=None, help="Optional Markdown output path.")
    parser.add_argument("--metadata-out", default=None, help="Optional metadata JSON output path.")
    parser.add_argument("--json", action="store_true", help="Print only metadata JSON to stdout.")
    parser.add_argument("--print", dest="print_report", action="store_true", help="Print Markdown report to stdout.")
    args = parser.parse_args(argv)

    try:
        chapter_text = Path(args.chapter).read_text(encoding="utf-8") if args.chapter else None
        quality_report = json.loads(Path(args.quality_report).read_text(encoding="utf-8"))
        rule_validation_report = json.loads(Path(args.rule_validation_report).read_text(encoding="utf-8"))
        rule_repair_plan = json.loads(Path(args.rule_repair_plan).read_text(encoding="utf-8"))
        repair_prompt_metadata = (
            json.loads(Path(args.repair_prompt_metadata).read_text(encoding="utf-8"))
            if args.repair_prompt_metadata
            else None
        )
        result = build_human_review_report(
            chapter_text=chapter_text,
            chapter_quality_report=quality_report,
            rule_validation_report=rule_validation_report,
            rule_repair_plan=rule_repair_plan,
            rule_repair_prompt_metadata=repair_prompt_metadata,
            title=args.title,
        )
        markdown = result["markdown"]
        metadata = result["metadata"]

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(markdown, encoding="utf-8")
        if args.metadata_out:
            metadata_path = Path(args.metadata_out)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise non-zero diagnostic.
        print(f"Human review report failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    elif args.print_report:
        print(markdown, end="")
    else:
        print(f"Human review report: {metadata['decision']['decision']}")
        print(f"Chars: {metadata['chars']}")
        print(f"Decision: {metadata['decision']['label']}")
        if args.out:
            print(f"Report: {args.out}")
        if args.metadata_out:
            print(f"Metadata: {args.metadata_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
