from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rules import build_rule_repair_plan  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a rule-aware repair plan from a rule validation report.")
    parser.add_argument("--rule-validation-report", required=True, help="Path to PR9 rule_validation_report JSON.")
    parser.add_argument("--chapter", default=None, help="Optional chapter text path. Read only; never modified.")
    parser.add_argument("--snapshot", default=None, help="Optional snapshot JSON path. Read only; never modified.")
    parser.add_argument("--max-tasks", type=int, default=None, help="Maximum repair task count.")
    parser.add_argument("--fail-only", action="store_true", help="Only create tasks for failed rules.")
    parser.add_argument("--out", default=None, help="Optional output path for rule_repair_plan.json.")
    parser.add_argument("--json", action="store_true", help="Print only the JSON plan to stdout.")
    args = parser.parse_args(argv)

    try:
        rule_validation_report = json.loads(Path(args.rule_validation_report).read_text(encoding="utf-8"))
        chapter_text = Path(args.chapter).read_text(encoding="utf-8") if args.chapter else None
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8")) if args.snapshot else None
        plan = build_rule_repair_plan(
            rule_validation_report=rule_validation_report,
            chapter_text=chapter_text,
            snapshot=snapshot,
            max_tasks=args.max_tasks,
            include_warnings=not args.fail_only,
        )
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise non-zero diagnostic.
        print(f"Rule repair plan failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    else:
        summary = plan["summary"]
        print(f"Rule repair plan: {plan['status']}")
        print(
            "Tasks: "
            f"{summary['task_count']} total, "
            f"{summary['blocking_task_count']} blocking, "
            f"{summary['human_review_task_count']} human review"
        )
        for task in plan["tasks"]:
            print(
                f"- {task['task_id']} {task['rule_code']} "
                f"[{task['severity']}] {task['repair_type']} blocking={str(task['blocking']).lower()}"
            )
        if args.out:
            print(f"Plan: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
