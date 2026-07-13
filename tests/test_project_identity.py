from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
import uuid

from core.path_refs import PathRef, PathRefError, path_ref_for, resolve_path_ref, validate_path_ref
from core.engine.preflight import run_preflight
from core.runtime_paths import RuntimePaths
from core.story_project.identity import (
    ProjectIdentityError,
    ProjectIdentityMismatchError,
    assert_project_identity,
    create_ephemeral_project_identity,
    ensure_project_identity,
    ensure_project_identity_for_runtime,
    load_project_identity,
    project_identity_path,
)


class ProjectIdentityTest(unittest.TestCase):
    def _story_project(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_project_identity" / f"{name}_{uuid.uuid4().hex}" / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        return root

    @staticmethod
    def _now() -> datetime:
        return datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)

    def test_ephemeral_identity_does_not_create_project_file(self) -> None:
        root = self._story_project("preview")

        identity = create_ephemeral_project_identity(
            root,
            now=self._now,
            uuid_factory=lambda: uuid.UUID("00000000-0000-0000-0000-000000000001"),
        )

        self.assertTrue(identity.ephemeral)
        self.assertEqual("ephemeral:00000000-0000-0000-0000-000000000001", identity.book_id)
        self.assertFalse(project_identity_path(root).exists())

    def test_ensure_identity_atomically_creates_and_reuses_stable_book_id(self) -> None:
        root = self._story_project("persist")

        first = ensure_project_identity(
            root,
            now=self._now,
            uuid_factory=lambda: uuid.UUID("00000000-0000-0000-0000-000000000002"),
        )
        second = ensure_project_identity(
            root,
            now=self._now,
            uuid_factory=lambda: uuid.UUID("00000000-0000-0000-0000-000000000003"),
        )

        self.assertFalse(first.ephemeral)
        self.assertEqual("00000000-0000-0000-0000-000000000002", first.book_id)
        self.assertEqual(first, second)
        self.assertEqual(first, load_project_identity(root))
        self.assertEqual(first.to_dict(), json.loads(project_identity_path(root).read_text(encoding="utf-8")))

    def test_malformed_or_ephemeral_persisted_identity_fails_closed(self) -> None:
        root = self._story_project("malformed")
        path = project_identity_path(root)
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")

        with self.assertRaises(ProjectIdentityError):
            load_project_identity(root)

    def test_existing_unattributed_journal_blocks_new_identity_assignment(self) -> None:
        root = self._story_project("existing_journal")
        persistence_dir = root / ".novelagent" / "runtime" / "persistence"
        (persistence_dir / "legacy-run").mkdir(parents=True)

        with self.assertRaisesRegex(ProjectIdentityError, "identity_missing_for_existing_journal"):
            ensure_project_identity_for_runtime(root, persistence_dir=persistence_dir)

        self.assertFalse(project_identity_path(root).exists())

    def test_identity_mismatch_uses_stable_error_code(self) -> None:
        root = self._story_project("mismatch")
        identity = ensure_project_identity(root, now=self._now)

        with self.assertRaises(ProjectIdentityMismatchError) as raised:
            assert_project_identity(identity, "another-book", source="snapshot")

        self.assertEqual("story_project_state_identity_mismatch", raised.exception.code)
        assert_project_identity(identity, None, source="legacy-run", allow_missing_legacy=True)

    def test_story_project_runtime_paths_stay_inside_project(self) -> None:
        root = self._story_project("paths")

        paths = RuntimePaths.for_story_project(root)

        self.assertEqual(root / ".novelagent" / "runtime", paths.runtime_dir)
        self.assertEqual(paths.runtime_dir / "snapshot.json", paths.snapshot_path)
        self.assertEqual(paths.runtime_dir / "runs", paths.run_dir)
        self.assertEqual(paths.runtime_dir / "chapters", paths.chapter_dir)
        self.assertEqual(paths.runtime_dir / "reviews", paths.review_dir)
        self.assertEqual(paths.runtime_dir / "persistence", paths.persistence_dir)
        self.assertEqual(paths.runtime_dir / "deliveries", paths.delivery_dir)
        self.assertEqual(paths.runtime_dir / "memory", paths.memory_dir)
        self.assertFalse(paths.runtime_dir.exists())

    def test_preflight_reports_preview_identity_without_creating_it(self) -> None:
        root = self._story_project("preflight_preview")
        (root / "大纲" / "细纲_第001章.md").write_text(
            "# 第一章\n核心事件：测试\n## 剧情节拍\n- 测试\n结尾压力：继续",
            encoding="utf-8",
        )

        result = run_preflight(
            snapshot_path="data/snapshot.example.json",
            memory_path="data/notion_memory.example.json",
            run_dir=root / ".novelagent" / "runtime" / "runs",
            chapter_dir=root / ".novelagent" / "runtime" / "chapters",
            dry_run=True,
            story_project=root,
            chapter=1,
        )

        identity_check = [check for check in result["checks"] if check["name"] == "story_project_identity"][0]
        self.assertTrue(identity_check["ok"])
        self.assertEqual("ephemeral_preview", identity_check["details"]["status"])
        self.assertFalse(project_identity_path(root).exists())

    def test_preflight_fails_on_snapshot_book_identity_mismatch(self) -> None:
        root = self._story_project("preflight_mismatch")
        (root / "大纲" / "细纲_第001章.md").write_text(
            "# 第一章\n核心事件：测试\n## 剧情节拍\n- 测试\n结尾压力：继续",
            encoding="utf-8",
        )
        identity = ensure_project_identity(root, now=self._now)
        snapshot = json.loads(Path("data/snapshot.example.json").read_text(encoding="utf-8"))
        snapshot["book_id"] = "different-book"
        snapshot_path = root.parent / "snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

        result = run_preflight(
            snapshot_path=snapshot_path,
            memory_path="data/notion_memory.example.json",
            run_dir=root / ".novelagent" / "runtime" / "runs",
            chapter_dir=root / ".novelagent" / "runtime" / "chapters",
            dry_run=True,
            story_project=root,
            chapter=1,
        )

        self.assertFalse(result["ok"])
        check = [check for check in result["checks"] if check["name"] == "story_project_snapshot_identity"][0]
        self.assertFalse(check["ok"])
        self.assertEqual(identity.book_id, check["details"]["project_book_id"])
        self.assertEqual("story_project_state_identity_mismatch", check["error"])

    def test_path_ref_round_trip_and_root_escape_guards(self) -> None:
        root = self._story_project("path_ref")
        target = root / "正文" / "第001章.md"
        target.write_text("正文", encoding="utf-8")

        ref = path_ref_for(target, root_id="story_project", root=root)

        self.assertEqual("正文/第001章.md", ref.relative_path)
        self.assertEqual(target.resolve(), resolve_path_ref(ref, {"story_project": root}))
        self.assertEqual(ref, validate_path_ref(ref.to_dict()))

        for invalid in ("../outside.md", "/absolute.md", "C:drive-relative.md", "追踪//上下文.md", "./正文.md"):
            with self.subTest(invalid=invalid), self.assertRaises(PathRefError):
                validate_path_ref(PathRef(root_id="story_project", relative_path=invalid))

        with self.assertRaises(PathRefError):
            path_ref_for(root.parent / "outside.md", root_id="story_project", root=root)

    def test_path_ref_rejects_unknown_and_unc_roots(self) -> None:
        with self.assertRaises(PathRefError):
            validate_path_ref(PathRef(root_id="unknown", relative_path="file.json"))
        with self.assertRaises(PathRefError):
            resolve_path_ref(
                PathRef(root_id="runtime", relative_path="file.json"),
                {"runtime": r"\\server\share\runtime"},
            )

    def test_path_ref_rejects_existing_symlink_escape_when_supported(self) -> None:
        root = self._story_project("path_ref_symlink")
        outside = root.parent / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text("{}", encoding="utf-8")
        link = root / "linked-outside"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are not available in this Windows environment")

        with self.assertRaises(PathRefError):
            resolve_path_ref(
                PathRef(root_id="story_project", relative_path="linked-outside/secret.json"),
                {"story_project": root},
            )


if __name__ == "__main__":
    unittest.main()
