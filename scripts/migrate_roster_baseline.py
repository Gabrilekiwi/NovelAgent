from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state.roster_baseline_migration import (  # noqa: E402
    RosterBaselineMigrationError,
    run_roster_baseline_migration,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or explicitly apply a hash-pinned aggregate roster "
            "baseline migration. Preview is the default and performs no writes."
        )
    )
    parser.add_argument("--story-project", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--upgrade-identity-binding-from",
        metavar="LEGACY_MANIFEST",
        help=(
            "Explicitly upgrade an already-applied legacy receipt whose manifest "
            "had no book_id. The legacy receipt is preserved and a separate "
            "identity receipt is created only with --apply."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration after all preview checks pass.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_roster_baseline_migration(
            story_project=args.story_project,
            manifest_path=args.manifest,
            apply=bool(args.apply),
            legacy_manifest_path=args.upgrade_identity_binding_from,
        )
    except (OSError, RosterBaselineMigrationError) as exc:
        payload = {
            "status": "error",
            "code": getattr(exc, "code", "migration_io_error"),
            "message": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
