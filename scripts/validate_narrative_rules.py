from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rules import (  # noqa: E402
    DEFAULT_NARRATIVE_RULE_PACK_PATH,
    get_enabled_rules,
    load_narrative_rule_pack,
    render_narrative_contract,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and render Narrative Rule Pack files.")
    parser.add_argument("--rules", default=str(DEFAULT_NARRATIVE_RULE_PACK_PATH), help="Path to a narrative rule pack JSON file.")
    parser.add_argument("--render", action="store_true", help="Print the rendered narrative contract Markdown.")
    parser.add_argument("--out", default=None, help="Optional output path for the rendered narrative contract.")
    parser.add_argument("--json", action="store_true", help="Print only a JSON validation summary to stdout.")
    args = parser.parse_args(argv)

    try:
        rule_pack = load_narrative_rule_pack(args.rules)
        enabled_rules = get_enabled_rules(rule_pack)
        summary = _summary(rule_pack, enabled_rules)
        rendered = render_narrative_contract(rule_pack) if args.render or args.out else None
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered or render_narrative_contract(rule_pack), encoding="utf-8")
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"Narrative rules failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    elif args.render:
        print(rendered, end="")
    else:
        categories = ", ".join(f"{name}={count}" for name, count in summary["categories"].items())
        print("Narrative rules: ok")
        print(f"Rule pack: {summary['rule_pack_id']}")
        print(f"Enabled rules: {summary['enabled_rule_count']}")
        print(f"Categories: {categories}")
        if args.out:
            print(f"Rendered contract: {args.out}")
    return 0


def _summary(rule_pack: dict, enabled_rules: list[dict]) -> dict:
    categories = Counter(str(rule.get("category")) for rule in enabled_rules)
    return {
        "status": "ok",
        "rule_pack_id": rule_pack["rule_pack_id"],
        "rule_count": len(rule_pack["rules"]),
        "enabled_rule_count": len(enabled_rules),
        "categories": {name: categories[name] for name in sorted(categories)},
    }


if __name__ == "__main__":
    raise SystemExit(main())
