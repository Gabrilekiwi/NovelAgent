from __future__ import annotations

import os
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

    def test_missing_parent_is_actionable_and_retry_preserves_project_information(self) -> None:
        workspace, registry = self._case("missing_parent")
        parent = workspace / "尚未创建的 保存目录"
        target = parent / "中文 新书"
        details = {"name": "中文 新书", "genre": "科幻", "synopsis": "保留这份简介。"}

        with self.assertRaisesRegex(ProjectRegistryError, "保存父目录不存在.*已有文件夹"):
            registry.create_project(target, **details)

        self.assertFalse(parent.exists())
        self.assertEqual((), registry.list_projects())
        self.assertEqual([], list(workspace.glob(".novelagent-create-*")))
        parent.mkdir()
        created = registry.create_project(target, **details)
        self.assertEqual(target.resolve(), created.root)
        self.assertEqual(details["name"], created.name)
        self.assertEqual(details["genre"], created.metadata["genre"])
        self.assertEqual(details["synopsis"], created.metadata["synopsis"])
        self.assertEqual(created, registry.get(created.id))

    def test_file_parent_is_rejected_without_modifying_the_file(self) -> None:
        workspace, registry = self._case("file_parent")
        parent = workspace / "保存位置.txt"
        parent.write_text("保留原文件", encoding="utf-8")

        with self.assertRaisesRegex(ProjectRegistryError, "保存父.*文件夹"):
            registry.create_project(parent / "新书", name="新书")

        self.assertEqual("保留原文件", parent.read_text(encoding="utf-8"))
        self.assertEqual((), registry.list_projects())

    def test_invalid_save_paths_raise_registry_errors_before_creating_files(self) -> None:
        workspace, registry = self._case("invalid_paths")
        for target in ("relative/new-book", str(workspace / "bad\0name")):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ProjectRegistryError, "保存路径"):
                    registry.create_project(target, name="新书")

        self.assertEqual([], list(workspace.iterdir()))
        self.assertEqual((), registry.list_projects())

    @unittest.skipUnless(os.name == "nt", "Windows directory naming rules")
    def test_windows_invalid_directory_names_fail_before_staging(self) -> None:
        workspace, registry = self._case("windows_invalid_names")
        for name in ("bad?name", "bad<name", "NUL", "CON.txt", "trailing.", "trailing "):
            for target in (workspace / name, workspace / name / "新书"):
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ProjectRegistryError, "Windows 不支持的目录名"):
                        registry.create_project(target, name="新书")

        self.assertEqual([], list(workspace.iterdir()))
        self.assertEqual((), registry.list_projects())

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
        with self.assertRaisesRegex(ProjectRegistryError, "outside"):
            registry.create_project(workspace / ".." / "outside" / "new-book", name="不能越界")
        self.assertFalse((outside / "new-book").exists())

        with self.assertRaisesRegex(ProjectRegistryError, "secrets"):
            registry.create_project(
                workspace / "secret",
                name="secret",
                metadata={"provider": {"openai-api-key": "do-not-store"}},
            )
        self.assertFalse((workspace / "secret").exists())


if __name__ == "__main__":
    unittest.main()
