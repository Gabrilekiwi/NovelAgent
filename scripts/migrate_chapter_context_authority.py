from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.engine.persistence import PersistenceError  # noqa: E402
from core.state.chapter_context_authority_migration import (  # noqa: E402
    ChapterContextAuthorityMigrationError,
    run_chapter_context_authority_migration,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or explicitly apply a hash-pinned legacy/shadow "
            "authoritative-state upsert migration. Preview is the default "
            "and performs no writes."
        )
    )
    parser.add_argument("--story-project", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply after rechecking ProjectIdentity, snapshot CAS, exact "
            "evidence, conflicts, backup, and receipt bindings."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_chapter_context_authority_migration(
            story_project=args.story_project,
            manifest_path=args.manifest,
            apply=bool(args.apply),
        )
    except (
        ChapterContextAuthorityMigrationError,
        PersistenceError,
        OSError,
    ) as exc:
        payload = {
            "status": "error",
            "code": getattr(exc, "code", "migration_io_error"),
            "message": str(exc),
        }
        print(
            json.dumps(payload, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
