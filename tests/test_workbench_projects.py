from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.story_project.identity import load_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry, ProjectRegistryError


class WorkbenchProjectRegistryTest(unittest.TestCase):
    def _case(self, name: str) -> tuple[Path, ProjectRegistry]:
        root = Path.cwd() / ".tmp" / "test_workbench_projects" / f"{name}_{uuid.uuid4().hex}"
        workspace = root / "books"
        workspace.mkdir(parents=True)
        registry = ProjectRegistry(
            WorkbenchDatabase(root / "workbench.db"),
            allowed_workspaces=[workspace],
        )
        return workspace, registry

    def test_create_project_atomically_builds_minimal_story_project(self) -> None:
        workspace, registry = self._case("create")
        target = workspace / "新书"

        record = registry.create_project(
            target,
            name="新书",
            genre="科幻",
            synopsis="一场漫长迁徙。",
            language="zh-CN",
        )

        self.assertEqual(target.resolve(), record.root)
        self.assertEqual(1, record.next_chapter)
        self.assertEqual("科幻", record.metadata["genre"])
        self.assertTrue(all((target / name).is_dir() for name in CORE_DIRECTORY_NAMES))
        identity = load_project_identity(target)
        self.assertIsNotNone(identity)
        self.assertEqual(record.book_id, identity.book_id)
        self.assertEqual(str(target.resolve()), identity.root_hint)
        self.assertEqual(record, registry.get(record.id))

    def test_create_accepts_existing_empty_target_but_refuses_nonempty_target(self) -> None:
        workspace, registry = self._case("target_rules")
        empty = workspace / "empty"
        empty.mkdir()
        created = registry.create_project(empty, name="空目录新书")
        self.assertTrue(created.root.is_dir())

        occupied = workspace / "occupied"
        occupied.mkdir()
        marker = occupied / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ProjectRegistryError, "not empty"):
            registry.create_project(occupied, name="不能覆盖")
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    def test_create_rolls_back_staging_and_preserves_original_empty_target(self) -> None:
        workspace, registry = self._case("rollback")
        target = workspace / "empty"
        target.mkdir()

        with patch("workbench.projects.atomic_create_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                registry.create_project(target, name="失败项目")

        self.assertTrue(target.is_dir())
        self.assertEqual([], list(target.iterdir()))
        self.assertEqual([], list(workspace.glob(".novelagent-create-*")))

    def test_import_creates_stable_identity_and_infers_next_chapter(self) -> None:
        workspace, registry = self._case("import")
        root = workspace / "legacy"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        (root / "正文" / "第001章_开始.md").write_text("第一章", encoding="utf-8")
        (root / "正文" / "第002章_继续.md").write_text("第二章", encoding="utf-8")

        first = registry.import_project(root, name="旧书", metadata={"source": "local"})
        second = registry.import_project(root, name="旧书改名", metadata={"source": "local"})

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.book_id, second.book_id)
        self.assertEqual(3, second.next_chapter)
        self.assertEqual("旧书改名", second.name)
        self.assertFalse(load_project_identity(root).ephemeral)

    def test_registry_rejects_outside_paths_and_secret_metadata(self) -> None:
        workspace, registry = self._case("security")
        outside = workspace.parent / "outside"
        outside.mkdir()
        for directory in CORE_DIRECTORY_NAMES:
            (outside / directory).mkdir()
        with self.assertRaisesRegex(ProjectRegistryError, "outside"):
            registry.import_project(outside)

        with self.assertRaisesRegex(ProjectRegistryError, "secrets"):
            registry.create_project(
                workspace / "secret",
                name="secret",
                metadata={"provider": {"openai-api-key": "do-not-store"}},
            )
        self.assertFalse((workspace / "secret").exists())


if __name__ == "__main__":
    unittest.main()
