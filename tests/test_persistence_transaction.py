from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.persistence import (
    LocalPersistenceTransaction,
    PersistencePreparationError,
    PersistenceLockError,
    PersistenceTarget,
    load_persistence_candidate,
    complete_persistence_transaction,
    reconcile_persistence,
    reconcile_persistence_transaction,
    persistence_run_lock,
)


class SimulatedCrash(BaseException):
    pass


class PersistenceTransactionTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_persistence_transaction" / f"{name}_{uuid.uuid4().hex}"
        (root / "runtime" / "runs").mkdir(parents=True)
        (root / "story").mkdir()
        return root

    def _transaction(self, root: Path, run_id: str, *, fault_injector=None) -> LocalPersistenceTransaction:
        return LocalPersistenceTransaction(
            run_dir=root / "runtime" / "runs",
            run_id=run_id,
            allowed_roots=[root / "story", root / "runtime"],
            fault_injector=fault_injector,
        )

    def test_commit_writes_targets_candidate_manifest_and_marker(self) -> None:
        root = self._case_dir("commit")
        prose = root / "story" / "chapter.md"
        snapshot = root / "runtime" / "snapshot.json"
        snapshot.write_text('{"chapter_index": 2}\n', encoding="utf-8")
        transaction = self._transaction(root, "chapter-2")

        prepared = transaction.prepare(
            [
                PersistenceTarget("prose", prose, "new prose\n"),
                PersistenceTarget("snapshot", snapshot, '{"chapter_index": 3}\n'),
            ],
            candidate_result={"run": {"id": "chapter-2", "status": "committed"}},
        )

        self.assertEqual("prepared", prepared["state"])
        self.assertFalse(prose.exists())
        self.assertEqual('{"chapter_index": 2}\n', snapshot.read_text(encoding="utf-8"))
        committed = transaction.commit()
        self.assertEqual("commit_marked", committed.state)
        result = transaction.complete_publication()
        self.assertEqual("completed", result.state)
        self.assertTrue(result.committed)
        self.assertFalse(result.partial)
        self.assertEqual("new prose\n", prose.read_text(encoding="utf-8"))
        self.assertEqual('{"chapter_index": 3}\n', snapshot.read_text(encoding="utf-8"))
        self.assertTrue(Path(result.commit_marker).exists())
        self.assertEqual(
            {"run": {"id": "chapter-2", "status": "committed"}},
            load_persistence_candidate(result.journal_path),
        )
        manifest = json.loads((Path(result.journal_path) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("completed", manifest["state"])
        self.assertTrue(all(target["status"] == "verified" for target in manifest["targets"]))

    def test_run_directory_lock_rejects_concurrent_owner(self) -> None:
        root = self._case_dir("run_lock")
        run_dir = root / "runtime" / "runs"

        with persistence_run_lock(run_dir):
            with self.assertRaises(PersistenceLockError):
                with persistence_run_lock(run_dir):
                    self.fail("nested owner must not acquire the persistence lock")

    def test_state_identity_lock_conflicts_across_different_run_directories(self) -> None:
        root = self._case_dir("state_identity_lock")
        shared_snapshot = root / "runtime" / "shared-snapshot.json"
        first_run_dir = root / "runtime" / "runs-a"
        second_run_dir = root / "runtime" / "runs-b"

        with persistence_run_lock(first_run_dir, state_paths=[shared_snapshot]):
            with self.assertRaises(PersistenceLockError):
                with persistence_run_lock(second_run_dir, state_paths=[shared_snapshot]):
                    self.fail("shared state must not be locked through a second run directory")

    def test_commit_failure_rolls_back_existing_empty_and_new_targets(self) -> None:
        root = self._case_dir("rollback")
        existing_empty = root / "story" / "tracking.md"
        existing_empty.write_bytes(b"")
        new_target = root / "story" / "chapter.md"

        def fail_second(event: str, index: int | None, _path: Path | None) -> None:
            if event == "before_target_replace" and index == 1:
                raise OSError("simulated target lock")

        transaction = self._transaction(root, "rollback-empty", fault_injector=fail_second)
        transaction.prepare(
            [
                PersistenceTarget("tracking", existing_empty, "tracking update\n"),
                PersistenceTarget("prose", new_target, "chapter\n"),
            ]
        )

        result = transaction.commit()

        self.assertEqual("rolled_back", result.state)
        self.assertFalse(result.committed)
        self.assertFalse(result.partial)
        self.assertTrue(existing_empty.exists())
        self.assertEqual(b"", existing_empty.read_bytes())
        self.assertFalse(new_target.exists())
        self.assertTrue(any(error["code"] == "commit_failed" for error in result.errors))
        self.assertTrue(all(target["status"] == "rolled_back" for target in result.targets))

    def test_reconcile_rolls_back_crash_after_replace_before_manifest_update(self) -> None:
        root = self._case_dir("crash_before_marker")
        first = root / "story" / "first.md"
        second = root / "story" / "second.md"
        first.write_text("before\n", encoding="utf-8")

        def crash(event: str, index: int | None, _path: Path | None) -> None:
            if event == "after_target_replace" and index == 0:
                raise SimulatedCrash("power loss")

        transaction = self._transaction(root, "crash-before-marker", fault_injector=crash)
        transaction.prepare(
            [
                PersistenceTarget("first", first, "after\n"),
                PersistenceTarget("second", second, "created\n"),
            ]
        )

        with self.assertRaises(SimulatedCrash):
            transaction.commit()
        self.assertEqual("after\n", first.read_text(encoding="utf-8"))

        result = reconcile_persistence_transaction(transaction.journal_dir)

        self.assertEqual("rolled_back", result.state)
        self.assertFalse(result.partial)
        self.assertEqual("before\n", first.read_text(encoding="utf-8"))
        self.assertFalse(second.exists())

    def test_every_target_replace_failure_point_restores_all_before_images(self) -> None:
        for event in ("before_target_replace", "after_target_replace"):
            for failed_index in range(3):
                with self.subTest(event=event, failed_index=failed_index):
                    root = self._case_dir(f"replace_{event}_{failed_index}")
                    paths = [root / "story" / f"target-{index}.md" for index in range(3)]
                    paths[0].write_text("original zero\n", encoding="utf-8")
                    paths[1].write_bytes(b"")

                    def fail(selected_event: str, index: int | None, _path: Path | None) -> None:
                        if selected_event == event and index == failed_index:
                            raise OSError(f"failure at {event}:{failed_index}")

                    transaction = self._transaction(
                        root,
                        f"replace-{event}-{failed_index}",
                        fault_injector=fail,
                    )
                    transaction.prepare(
                        [
                            PersistenceTarget("zero", paths[0], "changed zero\n"),
                            PersistenceTarget("one", paths[1], "changed one\n"),
                            PersistenceTarget("two", paths[2], "created two\n"),
                        ]
                    )

                    result = transaction.commit()

                    self.assertEqual("rolled_back", result.state)
                    self.assertFalse(result.partial)
                    self.assertEqual("original zero\n", paths[0].read_text(encoding="utf-8"))
                    self.assertEqual(b"", paths[1].read_bytes())
                    self.assertFalse(paths[2].exists())

    def test_reconcile_recovers_crash_after_marker_then_publication_completes(self) -> None:
        root = self._case_dir("crash_after_marker")
        target = root / "story" / "chapter.md"

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_commit_marker":
                raise SimulatedCrash("power loss after durable commit")

        transaction = self._transaction(root, "crash-after-marker", fault_injector=crash)
        transaction.prepare(
            [PersistenceTarget("prose", target, "committed chapter\n")],
            candidate_result={"run": {"id": "crash-after-marker"}},
        )

        with self.assertRaises(SimulatedCrash):
            transaction.commit()
        self.assertTrue(transaction.commit_marker_path.exists())

        result = reconcile_persistence_transaction(transaction.journal_dir)

        self.assertEqual("commit_marked", result.state)
        self.assertTrue(result.committed)
        self.assertEqual("committed chapter\n", target.read_text(encoding="utf-8"))
        self.assertEqual({"run": {"id": "crash-after-marker"}}, load_persistence_candidate(result.journal_path))
        self.assertEqual("completed", complete_persistence_transaction(result.journal_path).state)

    def test_expected_before_version_rejects_stale_render_without_journal(self) -> None:
        root = self._case_dir("expected_before")
        target = root / "story" / "tracking.md"
        target.write_text("fresh external edit\n", encoding="utf-8")
        transaction = self._transaction(root, "expected-before")

        with self.assertRaises(PersistencePreparationError):
            transaction.prepare(
                [
                    PersistenceTarget(
                        "tracking",
                        target,
                        "stale rendered content\n",
                        expected_before_exists=True,
                        expected_before_sha256="0" * 64,
                    )
                ]
            )

        self.assertEqual("fresh external edit\n", target.read_text(encoding="utf-8"))
        self.assertFalse(transaction.journal_dir.exists())

    def test_candidate_hash_tamper_is_rejected(self) -> None:
        root = self._case_dir("candidate_tamper")
        target = root / "story" / "chapter.md"
        transaction = self._transaction(root, "candidate-tamper")
        transaction.prepare(
            [PersistenceTarget("prose", target, "chapter\n")],
            candidate_result={"run": {"id": "candidate-tamper"}},
        )
        transaction.candidate_path.write_text('{"run":{"id":"other"}}\n', encoding="utf-8")

        with self.assertRaisesRegex(Exception, "hash mismatch"):
            load_persistence_candidate(transaction.journal_dir)

    def test_commit_marker_collision_never_clobbers_marker_or_claims_commit(self) -> None:
        root = self._case_dir("marker_collision")
        target = root / "story" / "chapter.md"
        target.write_text("before\n", encoding="utf-8")
        transaction = self._transaction(root, "marker-collision")
        transaction.prepare([PersistenceTarget("prose", target, "after\n")])
        transaction.commit_marker_path.write_text("external marker", encoding="utf-8")

        result = transaction.commit()

        self.assertEqual("rolled_back", result.state)
        self.assertFalse(result.committed)
        self.assertEqual("before\n", target.read_text(encoding="utf-8"))
        self.assertEqual("external marker", transaction.commit_marker_path.read_text(encoding="utf-8"))
        reconciled = reconcile_persistence_transaction(transaction.journal_dir)
        self.assertEqual("recovery_required", reconciled.state)
        self.assertFalse(reconciled.committed)
        self.assertTrue(any(error["code"] == "commit_marker_invalid" for error in reconciled.errors))

    def test_external_change_after_prepare_is_not_overwritten(self) -> None:
        root = self._case_dir("cas")
        target = root / "story" / "chapter.md"
        target.write_text("original\n", encoding="utf-8")
        transaction = self._transaction(root, "cas-mismatch")
        transaction.prepare([PersistenceTarget("prose", target, "candidate\n")])
        target.write_text("external edit\n", encoding="utf-8")

        result = transaction.commit()

        self.assertEqual("rolled_back", result.state)
        self.assertFalse(result.committed)
        self.assertFalse(result.partial)
        self.assertEqual("external edit\n", target.read_text(encoding="utf-8"))
        self.assertEqual("rolled_back", result.targets[0]["status"])
        self.assertEqual("external_change_preserved", result.targets[0]["error"])

    def test_completed_transaction_is_not_revalidated_after_later_edits(self) -> None:
        root = self._case_dir("committed_drift")
        target = root / "story" / "chapter.md"
        target.write_text("before\n", encoding="utf-8")
        transaction = self._transaction(root, "committed-drift")
        transaction.prepare([PersistenceTarget("prose", target, "after\n")])
        self.assertEqual("commit_marked", transaction.commit().state)
        self.assertEqual("completed", transaction.complete_publication().state)
        target.write_text("external after commit\n", encoding="utf-8")

        result = reconcile_persistence_transaction(transaction.journal_dir)

        self.assertEqual("completed", result.state)
        self.assertTrue(result.committed)
        self.assertFalse(result.partial)
        self.assertEqual("external after commit\n", target.read_text(encoding="utf-8"))

    def test_preflight_rejects_escape_and_duplicate_without_creating_journal(self) -> None:
        root = self._case_dir("preflight")
        outside = root / "outside.md"
        transaction = self._transaction(root, "escape")
        with self.assertRaises(PersistencePreparationError):
            transaction.prepare([PersistenceTarget("outside", outside, "bad")])
        self.assertFalse(transaction.journal_dir.exists())

        target = root / "story" / "same.md"
        duplicate = self._transaction(root, "duplicate")
        with self.assertRaises(PersistencePreparationError):
            duplicate.prepare(
                [
                    PersistenceTarget("one", target, "one"),
                    PersistenceTarget("two", target, "two"),
                ]
            )
        self.assertFalse(duplicate.journal_dir.exists())

    def test_reconcile_all_preserves_safe_pre_apply_conflicts(self) -> None:
        root = self._case_dir("reconcile_all")
        safe_target = root / "story" / "safe.md"
        safe = self._transaction(root, "safe")
        safe.prepare([PersistenceTarget("safe", safe_target, "value")])

        drift_target = root / "story" / "drift.md"
        drift_target.write_text("before", encoding="utf-8")
        drift = self._transaction(root, "drift")
        drift.prepare([PersistenceTarget("drift", drift_target, "after")])
        drift_target.write_text("external", encoding="utf-8")

        report = reconcile_persistence(run_dir=root / "runtime" / "runs")

        self.assertTrue(report["ok"])
        self.assertEqual(2, report["transaction_count"])
        self.assertEqual([], report["recovery_required"])
        states = {item["run_id"]: item["state"] for item in report["transactions"]}
        self.assertEqual({"drift": "rolled_back", "safe": "rolled_back"}, states)
        self.assertFalse(safe_target.exists())
        self.assertEqual("external", drift_target.read_text(encoding="utf-8"))

    def test_reconcile_rejects_journal_from_another_book_before_mutation(self) -> None:
        root = self._case_dir("book_identity")
        target = root / "story" / "chapter.md"
        transaction = LocalPersistenceTransaction(
            run_dir=root / "runtime" / "runs",
            run_id="book-a-run",
            allowed_roots=[root / "story", root / "runtime"],
            book_id="book-a",
        )
        transaction.prepare([PersistenceTarget("prose", target, "candidate")])

        report = reconcile_persistence(
            run_dir=root / "runtime" / "runs",
            expected_book_id="book-b",
        )

        self.assertFalse(report["ok"])
        self.assertEqual(["book-a-run"], report["recovery_required"])
        self.assertFalse(target.exists())
        manifest = json.loads(transaction.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("book-a", manifest["book_id"])
        self.assertEqual("prepared", manifest["state"])


if __name__ == "__main__":
    unittest.main()
