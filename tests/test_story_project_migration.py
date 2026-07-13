from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
import uuid

from core.runtime_paths import RuntimePaths
from core.schema import validate_schema
from core.story_project.identity import load_project_identity, project_identity_path
from core.story_project.migration import (
    StoryProjectRuntimeMigrationError,
    inspect_story_project_runtime_migration,
    migrate_story_project_runtime,
)


class StoryProjectRuntimeMigrationTest(unittest.TestCase):
    def _case(self, name: str) -> tuple[Path, Path]:
        case = Path.cwd() / ".tmp" / "test_story_project_migration" / f"{name}_{uuid.uuid4().hex}"
        root = case / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        source = case / "old-runtime"
        (source / "runs").mkdir(parents=True)
        (source / "chapters").mkdir()
        return root, source

    @staticmethod
    def _write_run(source: Path, root: Path, *, book_id: str | None = None, recorded_root: Path | None = None) -> Path:
        story_project = {
            "enabled": True,
            "root": str((recorded_root or root).resolve()),
        }
        if book_id is not None:
            story_project["book_id"] = book_id
        path = source / "runs" / "chapter_1_legacy.json"
        path.write_text(
            json.dumps(
                {
                    "run": {
                        "id": "chapter_1_legacy",
                        "story_project": story_project,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_inspection_is_read_only_and_requires_proven_ownership(self) -> None:
        root, source = self._case("inspect")
        self._write_run(source, root)
        (source / "snapshot.json").write_text("{}", encoding="utf-8")

        inspection = inspect_story_project_runtime_migration(
            source_runtime=source,
            story_project_root=root,
        )

        self.assertTrue(inspection["ok"])
        self.assertTrue(inspection["copy_allowed"])
        self.assertEqual("matching", inspection["records"][0]["status"])
        self.assertFalse(project_identity_path(root).exists())
        self.assertFalse(RuntimePaths.for_story_project(root).runtime_dir.exists())

    def test_migration_copies_without_deleting_source_and_adopts_historical_book_id(self) -> None:
        root, source = self._case("migrate")
        historical_book_id = "00000000-0000-0000-0000-000000000099"
        run_path = self._write_run(source, root, book_id=historical_book_id)
        chapter_path = source / "chapters" / "chapter_1.md"
        chapter_path.write_text("历史正文", encoding="utf-8")
        legacy_journal = source / "runs" / "transactions" / "pending-run"
        legacy_journal.mkdir(parents=True)
        (legacy_journal / "manifest.json").write_text(
            json.dumps({"run_id": "pending-run", "book_id": None}),
            encoding="utf-8",
        )

        result = migrate_story_project_runtime(
            source_runtime=source,
            story_project_root=root,
            now=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
            migration_id_factory=lambda: uuid.UUID("00000000-0000-0000-0000-000000000100"),
        )

        target = RuntimePaths.for_story_project(root).runtime_dir
        self.assertTrue(result["ok"])
        self.assertTrue(run_path.exists())
        self.assertTrue(chapter_path.exists())
        self.assertTrue((target / "runs" / run_path.name).is_file())
        migrated_run = json.loads((target / "runs" / run_path.name).read_text(encoding="utf-8"))
        self.assertEqual(historical_book_id, migrated_run["run"]["story_project"]["book_id"])
        self.assertEqual(
            historical_book_id,
            migrated_run["run"]["story_project"]["project_identity"]["book_id"],
        )
        self.assertEqual("历史正文", (target / "chapters" / chapter_path.name).read_text(encoding="utf-8"))
        identity = load_project_identity(root)
        self.assertEqual(historical_book_id, identity.book_id)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertIs(manifest, validate_schema(manifest, "story_project_runtime_migration.schema.json"))
        self.assertFalse(manifest["source_deleted"])
        self.assertEqual(historical_book_id, manifest["book_id"])
        self.assertEqual(3, len(manifest["files"]))
        migrated_journal = json.loads(
            (target / "persistence" / "pending-run" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(historical_book_id, migrated_journal["book_id"])
        self.assertFalse((target / "runs" / "transactions").exists())

        with self.assertRaises(StoryProjectRuntimeMigrationError):
            migrate_story_project_runtime(source_runtime=source, story_project_root=root)

    def test_mismatched_or_unattributed_runs_block_migration(self) -> None:
        root, source = self._case("mismatch")
        other = root.parent / "other-book"
        other.mkdir()
        self._write_run(source, root, recorded_root=other)

        inspection = inspect_story_project_runtime_migration(
            source_runtime=source,
            story_project_root=root,
        )

        self.assertFalse(inspection["copy_allowed"])
        self.assertIn("migration_run_mismatched", {problem["code"] for problem in inspection["problems"]})
        with self.assertRaises(StoryProjectRuntimeMigrationError):
            migrate_story_project_runtime(source_runtime=source, story_project_root=root)


if __name__ == "__main__":
    unittest.main()
