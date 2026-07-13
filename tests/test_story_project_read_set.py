from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.engine.persistence import LocalPersistenceTransaction, PersistenceTarget
from core.story_project.identity import ensure_project_identity
from core.story_project.read_set import (
    StoryProjectSourceDriftError,
    capture_story_project_read_set,
    declared_read_set_writes,
    verify_story_project_read_set,
)


class StoryProjectReadSetTest(unittest.TestCase):
    def _case(self, name: str) -> tuple[Path, Path]:
        case = Path(".tmp") / "test_story_project_read_set" / f"{name}_{uuid.uuid4().hex}"
        root = case / "book"
        runtime = case / "runtime"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        (runtime / "runs").mkdir(parents=True)
        (runtime / "transactions").mkdir()
        (root / "大纲" / "细纲_第002章.md").write_text("# 第二章\n核心事件：继续", encoding="utf-8")
        (root / "正文" / "第001章_一.md").write_text("第一章正文", encoding="utf-8")
        (root / "追踪" / "上下文.md").write_text("# 上下文\n- 当前位置：站台", encoding="utf-8")
        (root / "设定" / "地点.md").write_text("# 地点\n- 闸门只能从内侧打开", encoding="utf-8")
        ensure_project_identity(root)
        return root, runtime

    def _capture(self, root: Path) -> dict:
        identity = ensure_project_identity(root)
        return capture_story_project_read_set(root, 2, project_identity=identity)

    def _transaction(
        self,
        *,
        root: Path,
        runtime: Path,
        run_id: str,
        read_set: dict,
        targets: list[PersistenceTarget],
        fault_injector=None,
    ) -> LocalPersistenceTransaction:
        declared = declared_read_set_writes(
            read_set,
            (
                (
                    target.path,
                    hashlib.sha256(target.content_bytes()).hexdigest(),
                    len(target.content_bytes()),
                )
                for target in targets
            ),
        )
        return LocalPersistenceTransaction(
            run_dir=runtime / "runs",
            run_id=run_id,
            allowed_roots=[root, runtime],
            transactions_dir=runtime / "transactions",
            story_project_read_set=read_set,
            read_set_declared_writes=declared,
            fault_injector=fault_injector,
        )

    def test_capture_records_full_hashes_candidates_membership_identity_and_digest(self) -> None:
        root, _runtime = self._case("capture")

        read_set = self._capture(root)

        self.assertEqual(2, read_set["chapter_index"])
        self.assertEqual("story_project", read_set["root_identity"]["root_id"])
        self.assertEqual(64, len(read_set["identity_revision"]))
        self.assertEqual(64, len(read_set["context_digest"]))
        self.assertEqual(64, len(read_set["membership_fingerprint"]))
        self.assertEqual(
            ["大纲/细纲_第002章.md"],
            read_set["candidate_fingerprints"]["outline"]["members"],
        )
        roles = {item["role"] for item in read_set["entries"]}
        self.assertEqual({"outline", "previous_prose", "追踪", "设定"}, roles)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in read_set["entries"]))
        self.assertTrue(verify_story_project_read_set(read_set, phase="prepare")["ok"])

    def test_content_membership_candidate_and_identity_drift_are_blocking(self) -> None:
        mutations = {
            "content": lambda root: (root / "设定" / "地点.md").write_text("外部修改", encoding="utf-8"),
            "membership": lambda root: (root / "追踪" / "新文件.md").write_text("新增", encoding="utf-8"),
            "candidate": lambda root: (root / "大纲" / "细纲_第2章_冲突.md").write_text("冲突", encoding="utf-8"),
            "identity": lambda root: (root / ".novelagent" / "project.json").write_text("{}", encoding="utf-8"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                root, _runtime = self._case(name)
                read_set = self._capture(root)
                mutate(root)
                with self.assertRaises(StoryProjectSourceDriftError) as raised:
                    verify_story_project_read_set(read_set, phase="prepare")
                self.assertEqual("story_project_source_drift", raised.exception.code)

    def test_prepare_detects_drift_before_creating_journal(self) -> None:
        root, runtime = self._case("prepare_drift")
        read_set = self._capture(root)
        target = PersistenceTarget("tracking", root / "追踪" / "上下文.md", "updated")
        transaction = self._transaction(
            root=root,
            runtime=runtime,
            run_id="prepare-drift",
            read_set=read_set,
            targets=[target],
        )
        (root / "设定" / "地点.md").write_text("changed", encoding="utf-8")

        with self.assertRaises(StoryProjectSourceDriftError):
            transaction.prepare([target])

        self.assertFalse(transaction.journal_dir.exists())

    def test_declared_replacement_and_create_are_valid_expected_post_membership(self) -> None:
        root, runtime = self._case("legal_writes")
        read_set = self._capture(root)
        targets = [
            PersistenceTarget("tracking", root / "追踪" / "上下文.md", "updated tracking"),
            PersistenceTarget("prose", root / "正文" / "第002章_二.md", "new chapter"),
            PersistenceTarget("snapshot", runtime / "snapshot.json", "{}"),
        ]
        transaction = self._transaction(
            root=root,
            runtime=runtime,
            run_id="legal-writes",
            read_set=read_set,
            targets=targets,
        )

        transaction.prepare(targets)
        result = transaction.commit()

        self.assertTrue(result.committed)
        self.assertEqual("commit_marked", result.state)
        manifest = json.loads(transaction.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(read_set["context_digest"], manifest["story_project_read_set"]["context_digest"])
        self.assertEqual(2, len(manifest["read_set_declared_writes"]))

    def test_source_drift_before_replace_rolls_back_without_provider_retry(self) -> None:
        root, runtime = self._case("replace_drift")
        read_set = self._capture(root)
        tracking = root / "追踪" / "上下文.md"
        before = tracking.read_bytes()
        settings = root / "设定" / "地点.md"

        def inject(event: str, index: int | None, _path: Path | None) -> None:
            if event == "before_target_replace" and index == 0:
                settings.write_text("用户并发修改", encoding="utf-8")

        targets = [PersistenceTarget("tracking", tracking, "managed update")]
        transaction = self._transaction(
            root=root,
            runtime=runtime,
            run_id="replace-drift",
            read_set=read_set,
            targets=targets,
            fault_injector=inject,
        )
        transaction.prepare(targets)

        result = transaction.commit()

        self.assertEqual("rolled_back", result.state)
        self.assertEqual(before, tracking.read_bytes())
        self.assertEqual("用户并发修改", settings.read_text(encoding="utf-8"))
        self.assertFalse(transaction.commit_marker_path.exists())

    def test_unlisted_source_drift_before_marker_rolls_back_applied_targets(self) -> None:
        root, runtime = self._case("marker_drift")
        read_set = self._capture(root)
        tracking = root / "追踪" / "上下文.md"
        before = tracking.read_bytes()

        def inject(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_commit_marker":
                (root / "追踪" / "外部新增.md").write_text("race", encoding="utf-8")

        targets = [PersistenceTarget("tracking", tracking, "managed update")]
        transaction = self._transaction(
            root=root,
            runtime=runtime,
            run_id="marker-drift",
            read_set=read_set,
            targets=targets,
            fault_injector=inject,
        )
        transaction.prepare(targets)

        result = transaction.commit()

        self.assertEqual("rolled_back", result.state)
        self.assertEqual(before, tracking.read_bytes())
        self.assertTrue((root / "追踪" / "外部新增.md").exists())
        self.assertFalse(transaction.commit_marker_path.exists())

    def test_user_edit_of_applied_target_before_marker_requires_recovery_and_is_preserved(self) -> None:
        root, runtime = self._case("target_after_drift")
        read_set = self._capture(root)
        tracking = root / "追踪" / "上下文.md"

        def inject(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_commit_marker":
                tracking.write_text("用户在 apply 后编辑", encoding="utf-8")

        targets = [PersistenceTarget("tracking", tracking, "managed update")]
        transaction = self._transaction(
            root=root,
            runtime=runtime,
            run_id="target-after-drift",
            read_set=read_set,
            targets=targets,
            fault_injector=inject,
        )
        transaction.prepare(targets)

        result = transaction.commit()

        self.assertEqual("recovery_required", result.state)
        self.assertTrue(result.partial)
        self.assertEqual("用户在 apply 后编辑", tracking.read_text(encoding="utf-8"))
        self.assertFalse(transaction.commit_marker_path.exists())


if __name__ == "__main__":
    unittest.main()
