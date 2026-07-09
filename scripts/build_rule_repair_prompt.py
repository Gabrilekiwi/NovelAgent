from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rules import build_rule_repair_prompt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a rule-aware repair prompt from a rule repair plan.")
    parser.add_argument("--chapter", required=True, help="Path to the original chapter text.")
    parser.add_argument("--snapshot", required=True, help="Path to the runtime snapshot JSON.")
    parser.add_argument("--rule-repair-plan", required=True, help="Path to PR10 rule_repair_plan JSON.")
    parser.add_argument("--previous", default=None, help="Optional previous chapter text path.")
    parser.add_argument("--narrative-rules", default=None, help="Optional narrative rules Markdown path.")
    parser.add_argument("--max-tasks", type=int, default=None, help="Maximum repair tasks to include in the prompt.")
    parser.add_argument("--blocking-only", action="store_true", help="Only include blocking repair tasks.")
    parser.add_argument("--out", default=None, help="Optional output path for repair prompt Markdown.")
    parser.add_argument("--metadata-out", default=None, help="Optional output path for prompt metadata JSON.")
    parser.add_argument("--json", action="store_true", help="Print only metadata JSON to stdout.")
    parser.add_argument("--print", dest="print_prompt", action="store_true", help="Print prompt Markdown to stdout.")
    args = parser.parse_args(argv)

    try:
        chapter_text = Path(args.chapter).read_text(encoding="utf-8")
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        rule_repair_plan = json.loads(Path(args.rule_repair_plan).read_text(encoding="utf-8"))
        previous_chapter_text = Path(args.previous).read_text(encoding="utf-8") if args.previous else None
        narrative_rules = Path(args.narrative_rules).read_text(encoding="utf-8") if args.narrative_rules else None
        result = build_rule_repair_prompt(
            chapter_text=chapter_text,
            snapshot=snapshot,
            rule_repair_plan=rule_repair_plan,
            previous_chapter_text=previous_chapter_text,
            narrative_rules=narrative_rules,
            max_tasks=args.max_tasks,
            include_non_blocking=not args.blocking_only,
        )
        prompt = result["prompt"]
        metadata = result["metadata"]

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(prompt, encoding="utf-8")
        if args.metadata_out:
            metadata_path = Path(args.metadata_out)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise non-zero diagnostic.
        print(f"Rule repair prompt failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    elif args.print_prompt:
        print(prompt, end="")
    else:
        print("Rule repair prompt: ok")
        print(f"Chars: {metadata['chars']}")
        print(
            "Tasks: "
            f"{metadata['prompt']['task_count']} total, "
            f"{metadata['prompt']['blocking_task_count']} blocking"
        )
        if args.out:
            print(f"Prompt: {args.out}")
        if args.metadata_out:
            print(f"Metadata: {args.metadata_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
