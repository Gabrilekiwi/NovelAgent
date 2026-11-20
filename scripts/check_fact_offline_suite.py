"""Report deterministic checks and unresolved policy cases separately."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.quality.fact_offline_suite import run_fact_offline_suite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-local-sources", action="store_true")
    parser.add_argument("--require-policy-complete", action="store_true")
    args = parser.parse_args()
    report = run_fact_offline_suite(ROOT / "tests/fixtures/chapter_fact_offline_v1", repository=ROOT,
                                   verify_local_sources=args.verify_local_sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("case_count", "verified", "regressions", "policy_gaps", "paid_provider_calls")}, ensure_ascii=False))
    failed = report["regressions"] or any(not s["source_matches"] or not s["snapshot_matches"] for s in report["local_sources"])
    return 1 if failed or (args.require_policy_complete and report["policy_gaps"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
