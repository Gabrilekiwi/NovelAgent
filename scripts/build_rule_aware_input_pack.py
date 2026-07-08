from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rules import (  # noqa: E402
    build_rule_aware_input_pack,
    count_generation_rules_for_input_pack,
    load_default_narrative_rule_pack,
    load_narrative_rule_pack,
)
from core.state.input_pack import build_input_pack  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a plain or rule-aware NovelAgent input pack.")
    parser.add_argument("--snapshot", required=True, help="Path to a runtime snapshot JSON file.")
    parser.add_argument("--rules", default=None, help="Optional narrative rule pack JSON path.")
    parser.add_argument("--default-rules", action="store_true", help="Use rules/default_narrative_rule_pack.json.")
    parser.add_argument("--min-severity", default="high", choices=["low", "medium", "high", "critical"])
    parser.add_argument("--category", action="append", dest="categories", default=None, help="Rule category filter. Can be repeated.")
    parser.add_argument("--max-rules", type=int, default=None, help="Maximum number of rules to inject.")
    parser.add_argument("--out", default=None, help="Optional output path for the input pack markdown.")
    parser.add_argument("--json", action="store_true", help="Print only a JSON summary to stdout.")
    parser.add_argument("--print", action="store_true", dest="print_pack", help="Print the input pack markdown to stdout.")
    args = parser.parse_args(argv)

    try:
        snapshot_path = Path(args.snapshot)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        rule_pack = _load_rule_pack(args.rules, args.default_rules)
        if rule_pack is None:
            input_pack = build_input_pack(snapshot)
            rules_injected = 0
            rule_pack_id = None
        else:
            input_pack = build_rule_aware_input_pack(
                snapshot,
                rule_pack=rule_pack,
                min_severity=args.min_severity,
                categories=args.categories,
                max_rules=args.max_rules,
            )
            rules_injected = count_generation_rules_for_input_pack(
                rule_pack,
                min_severity=args.min_severity,
                categories=args.categories,
                max_rules=args.max_rules,
            )
            rule_pack_id = rule_pack.get("rule_pack_id")

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(input_pack, encoding="utf-8")
        else:
            out_path = None

        summary = {
            "status": "ok",
            "snapshot_path": str(snapshot_path),
            "rule_pack_id": rule_pack_id,
            "rules_injected": rules_injected,
            "min_severity": args.min_severity,
            "categories": args.categories or [],
            "output_path": str(out_path) if out_path is not None else None,
            "chars": len(input_pack),
        }
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"Rule-aware input pack failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    elif args.print_pack:
        print(input_pack)
    else:
        print("Rule-aware input pack: ok")
        print(f"Rules injected: {summary['rules_injected']}")
        print(f"Rule pack: {summary['rule_pack_id']}")
        if args.out:
            print(f"Output: {args.out}")
    return 0


def _load_rule_pack(path: str | None, use_default: bool) -> dict | None:
    if path:
        return load_narrative_rule_pack(path)
    if use_default:
        return load_default_narrative_rule_pack()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
