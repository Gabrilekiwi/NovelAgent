from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock
import uuid

from core.autonomy.lease import BookLeaseStore
from core.engine.persistence_backends import (
    LegacyV1PersistenceBackend,
    PersistenceBackendError,
    PersistenceV2Backend,
    select_persistence_backend,
)
from core.engine.persistence import PersistenceLockError
from core.engine.persistence_coordinator import PersistenceCoordinator
from core.engine.persistence_v2 import (
    PersistenceV2IntegrityError,
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
    reconcile_pending_persistence_v2,
    validate_persistence_manifest_v2,
    verify_publication_receipt,
)
from core.engine.root_registry import (
    RootRegistryCasError,
    RootRegistryError,
    RootRegistryService,
    RootRemapBlockedError,
)
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, SafePathError
from core.memory_v2.canonical import canonical_json_hash
from core.path_refs import path_ref_for
from core.story_project.authority import (
    activate_event_authority,
    prepare_event_authority_advance,
    project_identity_sha256,
)
from core.story_project.identity import ensure_project_identity, load_project_identity
from core.story_project.read_set import (
    StoryProjectSourceDriftError,
    capture_story_project_read_set,
    declared_read_set_writes,
)


class SimulatedPowerLoss(BaseException):
    pass


class PersistenceV21Test(unittest.TestCase):
    def _case(self, name: str) -> dict:
        base = Path.cwd() / ".tmp" / "test_persistence_v21" / f"{name}_{uuid.uuid4().hex}"
        story = base / "story"
        runtime = base / "runtime"
        artifacts = base / "artifacts"
        for path in (story, runtime, artifacts):
            path.mkdir(parents=True)
        snapshot = runtime / "snapshot.json"
        snapshot.write_bytes(b'{"chapter":1}\n')
        return {
            "base": base,
            "story": story,
            "runtime": runtime,
            "artifacts": artifacts,
            "snapshot": snapshot,
            "transaction_root": runtime / "persistence",
            "root_map": {
                "story_project": story,
                "runtime": runtime,
                "snapshot": runtime,
                "chapter_artifacts": artifacts,
            },
        }

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _prepare(
        self,
        case: dict,
        *,
        run_id: str = "run-1",
        book_id: str = "book-1",
        fault_injector=None,
        apply_target: PersistenceV2Target | None = None,
        read_set: dict | None = None,
        declared_writes: list[dict] | None = None,
        receipt_filename: str | None = None,
        story_project_source_revision_after: dict | None = None,
    ) -> PersistenceV2Transaction:
        receipt_ref = path_ref_for(
            case["runtime"] / "receipts" / (receipt_filename or f"{run_id}.json"),
            root_id="runtime",
            root=case["runtime"],
        )
        final_ref = path_ref_for(
            case["runtime"] / "runs" / f"{run_id}.json",
            root_id="runtime",
            root=case["runtime"],
        )
        final = bind_final_run_record_receipt(
            {"id": run_id, "status": "committed", "committed": True},
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
        )
        target = apply_target or PersistenceV2Target(
            target_id="snapshot",
            kind="snapshot",
            path_ref=path_ref_for(
                case["snapshot"], root_id="snapshot", root=case["runtime"]
            ),
            content='{"chapter":2}\n',
            expected_before_exists=True,
            expected_before_sha256=self._sha('{"chapter":1}\n'),
        )
        transaction = PersistenceV2Transaction(
            transaction_root=case["transaction_root"],
            run_id=run_id,
            book_id=book_id,
            root_map=case["root_map"],
            fault_injector=fault_injector,
            story_project_read_set=read_set,
            read_set_declared_writes=declared_writes or [],
        )
        transaction.prepare(
            apply_targets=[target],
            artifacts=[
                PersistenceV2Target(
                    target_id="artifact",
                    kind="chapter_artifact",
                    path_ref=path_ref_for(
                        case["artifacts"] / f"{run_id}.md",
                        root_id="chapter_artifacts",
                        root=case["artifacts"],
                    ),
                    content="chapter\n",
                    phase="publication",
                )
            ],
            final_run_record=final,
            final_run_path_ref=final_ref,
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
            context_digest=self._sha("context"),
            generation_input_context_digest=self._sha("generation"),
            story_project_source_revision_after=(
                story_project_source_revision_after or {"revision": 2}
            ),
            candidate_result={"run": {"id": run_id}},
        )
        return transaction

    def test_reconcile_rejects_valid_receipt_from_another_pending_manifest(self) -> None:
        case = self._case("foreign_receipt")
        first = self._prepare(case, run_id="run-a", receipt_filename="shared.json")
        self.assertTrue(first.commit()["committed"])

        second_path = case["runtime"] / "second.json"
        second_path.write_text("one", encoding="utf-8")
        second_target = PersistenceV2Target(
            target_id="second",
            kind="snapshot",
            path_ref=path_ref_for(
                second_path, root_id="snapshot", root=case["runtime"]
            ),
            content="two",
            expected_before_exists=True,
            expected_before_sha256=self._sha("one"),
        )
        self._prepare(
            case,
            run_id="run-b",
            apply_target=second_target,
            receipt_filename="shared.json",
        )

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        result = next(item for item in report["transactions"] if item["run_id"] == "run-b")
        self.assertEqual("recovery_required", result["state"])
        self.assertFalse(result["committed"])
        self.assertEqual("one", second_path.read_text(encoding="utf-8"))
        self.assertFalse(
            (case["transaction_root"] / "registry" / "completed" / "run-b.json").exists()
        )

    def test_discovered_completed_journal_reverifies_published_files(self) -> None:
        case = self._case("completed_discovery_verification")
        transaction = self._prepare(case)
        self.assertTrue(transaction.commit()["committed"])
        completed = (
            case["transaction_root"] / "registry" / "completed" / "run-1.json"
        )
        completed.unlink()
        (case["artifacts"] / "run-1.md").write_text("corrupt", encoding="utf-8")

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertFalse(report["ok"])
        self.assertEqual("recovery_required", report["transactions"][0]["state"])
        self.assertFalse(report["transactions"][0]["committed"])
        self.assertTrue(
            (
                case["transaction_root"]
                / "registry"
                / "recovery_required"
                / "run-1.json"
            ).is_file()
        )

    def test_crash_after_journal_publish_before_pending_is_discovered(self) -> None:
        case = self._case("journal_before_pending")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_journal_publish":
                raise SimulatedPowerLoss("power loss")

        with self.assertRaises(SimulatedPowerLoss):
            self._prepare(case, fault_injector=crash)
        journal = case["transaction_root"] / "journals" / "run-1"
        pending = case["transaction_root"] / "registry" / "pending" / "run-1.json"
        self.assertTrue(journal.is_dir())
        self.assertFalse(pending.exists())

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertTrue(report["ok"])
        self.assertEqual("rolled_back", report["transactions"][0]["state"])
        self.assertEqual('{"chapter":1}\n', case["snapshot"].read_text(encoding="utf-8"))

    def test_incomplete_temporary_journal_is_quarantined_without_pending(self) -> None:
        case = self._case("temporary_partial")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_temporary_journal_created":
                raise SimulatedPowerLoss("power loss")

        with self.assertRaises(SimulatedPowerLoss):
            self._prepare(case, fault_injector=crash)
        staging = case["transaction_root"] / "staging" / "run-1"
        self.assertTrue(staging.is_dir())
        self.assertFalse((case["transaction_root"] / "journals" / "run-1").exists())

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertTrue(report["ok"])
        self.assertEqual("abandoned_prepare", report["transactions"][0]["errors"][0]["code"])
        self.assertFalse(staging.exists())
        self.assertTrue(any((case["transaction_root"] / "abandoned").iterdir()))

    def test_complete_temporary_journal_is_published_then_reconciled(self) -> None:
        case = self._case("temporary_complete")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_journal_publish":
                raise SimulatedPowerLoss("power loss")

        with self.assertRaises(SimulatedPowerLoss):
            self._prepare(case, fault_injector=crash)
        staging = case["transaction_root"] / "staging" / "run-1"
        self.assertTrue((staging / "manifest.json").is_file())
        self.assertFalse((case["transaction_root"] / "journals" / "run-1").exists())

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertTrue(report["ok"])
        self.assertEqual("rolled_back", report["transactions"][0]["state"])
        self.assertTrue((case["transaction_root"] / "journals" / "run-1").is_dir())
        self.assertFalse(staging.exists())

    def test_crash_after_pending_registration_is_recoverable(self) -> None:
        case = self._case("pending_complete")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_pending_registry":
                raise SimulatedPowerLoss("power loss")

        with self.assertRaises(SimulatedPowerLoss):
            self._prepare(case, fault_injector=crash)
        self.assertTrue(
            (case["transaction_root"] / "registry" / "pending" / "run-1.json").is_file()
        )
        report = reconcile_pending_persistence_v2(case["transaction_root"])
        self.assertTrue(report["ok"])
        self.assertEqual("rolled_back", report["transactions"][0]["state"])

    def test_reconcile_lock_busy_does_not_scan_or_mutate_inflight_prepare(self) -> None:
        case = self._case("reconcile_prepare_lock")
        entered = threading.Event()
        release = threading.Event()
        prepared: list[object] = []
        reports: list[dict] = []
        reconcile_errors: list[Exception] = []

        def pause(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_temporary_journal_created":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release prepare")

        def do_prepare() -> None:
            try:
                prepared.append(self._prepare(case, fault_injector=pause))
            except Exception as exc:  # pragma: no cover - assertion reports it.
                prepared.append(exc)

        def do_reconcile() -> None:
            try:
                reports.append(reconcile_pending_persistence_v2(case["transaction_root"]))
            except Exception as exc:  # expected while prepare owns the lock.
                reconcile_errors.append(exc)

        prepare_thread = threading.Thread(target=do_prepare)
        prepare_thread.start()
        self.assertTrue(entered.wait(5))
        reconcile_thread = threading.Thread(target=do_reconcile)
        reconcile_thread.start()
        reconcile_thread.join(5)
        self.assertFalse(reconcile_thread.is_alive())
        self.assertEqual(1, len(reconcile_errors))
        self.assertIsInstance(reconcile_errors[0], PersistenceLockError)
        self.assertTrue((case["transaction_root"] / "staging" / "run-1").is_dir())
        self.assertFalse((case["transaction_root"] / "abandoned").exists())
        self.assertFalse(
            (case["transaction_root"] / "registry" / "recovery_required").exists()
        )
        release.set()
        prepare_thread.join(5)

        self.assertEqual(1, len(prepared))
        self.assertIsInstance(prepared[0], PersistenceV2Transaction)
        self.assertEqual([], reports)
        reports.append(reconcile_pending_persistence_v2(case["transaction_root"]))
        errors = [
            error.get("code")
            for result in reports[0]["transactions"]
            for error in result.get("errors", [])
        ]
        self.assertNotIn("abandoned_prepare", errors)

    def _story_read_set_case(
        self, name: str, *, activate_before_prose: bool = False
    ) -> tuple[dict, dict, Path, Path]:
        case = self._case(name)
        story = case["story"]
        for directory in ("设定", "大纲", "正文", "追踪"):
            (story / directory).mkdir()
        (story / "大纲" / "细纲_第002章.md").write_text("# 第二章", encoding="utf-8")
        tracking = story / "追踪" / "上下文.md"
        settings = story / "设定" / "地点.md"
        tracking.write_text("# 上下文\n旧", encoding="utf-8")
        settings.write_text("# 地点\n旧", encoding="utf-8")
        identity = ensure_project_identity(story)
        if activate_before_prose:
            identity = activate_event_authority(
                story,
                expected_identity_sha256=project_identity_sha256(story),
                head_event_hash="a" * 64,
            )
        (story / "正文" / "第001章_一.md").write_text("第一章", encoding="utf-8")
        read_set = capture_story_project_read_set(story, 2, project_identity=identity)
        return case, read_set, tracking, settings

    def _story_target_and_declared(
        self, case: dict, read_set: dict, tracking: Path
    ) -> tuple[PersistenceV2Target, list[dict]]:
        content = "# 上下文\n新"
        target = PersistenceV2Target(
            target_id="tracking",
            kind="tracking",
            path_ref=path_ref_for(
                tracking, root_id="story_project", root=case["story"]
            ),
            content=content,
            expected_before_exists=True,
            expected_before_sha256=hashlib.sha256(tracking.read_bytes()).hexdigest(),
        )
        raw = content.encode("utf-8")
        declared = declared_read_set_writes(
            read_set, [(tracking, hashlib.sha256(raw).hexdigest(), len(raw))]
        )
        return target, declared

    def test_full_read_set_is_verified_at_prepare(self) -> None:
        case, read_set, tracking, settings = self._story_read_set_case("read_prepare")
        target, declared = self._story_target_and_declared(case, read_set, tracking)
        settings.write_text("concurrent", encoding="utf-8")

        with self.assertRaises(StoryProjectSourceDriftError):
            self._prepare(
                case,
                book_id=read_set["book_id"],
                apply_target=target,
                read_set=read_set,
                declared_writes=declared,
            )
        self.assertFalse((case["transaction_root"] / "staging" / "run-1").exists())
        self.assertFalse((case["transaction_root"] / "journals" / "run-1").exists())

    def test_read_set_is_bound_to_transaction_root_and_exact_story_targets(self) -> None:
        first, foreign_read_set, _foreign_tracking, _foreign_settings = (
            self._story_read_set_case("read_foreign")
        )
        case, local_read_set, tracking, _settings = self._story_read_set_case(
            "read_local"
        )
        target, local_declared = self._story_target_and_declared(
            case, local_read_set, tracking
        )

        with self.assertRaisesRegex(
            Exception, "read-set root identity|exactly match"
        ):
            self._prepare(
                case,
                book_id=foreign_read_set["book_id"],
                apply_target=target,
                read_set=foreign_read_set,
                declared_writes=[],
            )

        with self.assertRaisesRegex(Exception, "complete StoryProject read-set"):
            self._prepare(case, run_id="missing-read-set", apply_target=target)

        wrong = copy.deepcopy(local_declared)
        wrong[0]["after_sha256"] = "f" * 64
        with self.assertRaisesRegex(Exception, "does not bind target bytes"):
            self._prepare(
                case,
                run_id="wrong-declaration",
                book_id=local_read_set["book_id"],
                apply_target=target,
                read_set=local_read_set,
                declared_writes=wrong,
            )

        duplicate = [*local_declared, copy.deepcopy(local_declared[0])]
        with self.assertRaisesRegex(Exception, "duplicate_declared|duplicate declared"):
            self._prepare(
                case,
                run_id="duplicate-declaration",
                book_id=local_read_set["book_id"],
                apply_target=target,
                read_set=local_read_set,
                declared_writes=duplicate,
            )

    def test_full_read_set_is_verified_at_pre_apply(self) -> None:
        case, read_set, tracking, settings = self._story_read_set_case("read_pre_apply")
        target, declared = self._story_target_and_declared(case, read_set, tracking)
        transaction = self._prepare(
            case,
            book_id=read_set["book_id"],
            apply_target=target,
            read_set=read_set,
            declared_writes=declared,
        )
        settings.write_text("concurrent", encoding="utf-8")

        result = transaction.commit()

        self.assertEqual("rolled_back", result["state"])
        self.assertEqual("# 上下文\n旧", tracking.read_text(encoding="utf-8"))

    def test_event_authority_source_write_requires_identity_head_transition(self) -> None:
        case, read_set, tracking, _settings = self._story_read_set_case(
            "event_source_without_identity", activate_before_prose=True
        )
        activated = load_project_identity(case["story"])
        target, declared = self._story_target_and_declared(case, read_set, tracking)
        case["root_map"]["snapshot"] = case["story"]

        with self.assertRaisesRegex(
            PersistenceV2IntegrityError, "require an atomic ProjectIdentity head transition"
        ):
            self._prepare(
                case,
                run_id="event-source-without-identity",
                book_id=activated.book_id,
                apply_target=target,
                read_set=read_set,
                declared_writes=declared,
                story_project_source_revision_after={"forged": "opaque"},
            )

        alias_target = PersistenceV2Target(
            target_id="tracking-alias",
            kind="tracking",
            path_ref=path_ref_for(
                tracking, root_id="snapshot", root=case["story"]
            ),
            content=target.content,
            expected_before_exists=True,
            expected_before_sha256=target.expected_before_sha256,
        )
        with self.assertRaisesRegex(
            PersistenceV2IntegrityError, "require an atomic ProjectIdentity head transition"
        ):
            self._prepare(
                case,
                run_id="event-source-through-alias",
                book_id=activated.book_id,
                apply_target=alias_target,
                read_set=read_set,
                declared_writes=declared,
                story_project_source_revision_after={"forged": "opaque"},
            )

    def test_full_read_set_is_verified_at_pre_marker(self) -> None:
        case, read_set, tracking, settings = self._story_read_set_case("read_pre_marker")
        target, declared = self._story_target_and_declared(case, read_set, tracking)

        def mutate(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_commit_marker":
                settings.write_text("concurrent", encoding="utf-8")

        transaction = self._prepare(
            case,
            book_id=read_set["book_id"],
            apply_target=target,
            read_set=read_set,
            declared_writes=declared,
            fault_injector=mutate,
        )
        result = transaction.commit()

        self.assertEqual("rolled_back", result["state"])
        self.assertEqual("# 上下文\n旧", tracking.read_text(encoding="utf-8"))

    def test_project_identity_head_advance_uses_before_and_after_read_set_cas(self) -> None:
        case, read_set, _tracking, _settings = self._story_read_set_case(
            "identity_advance", activate_before_prose=True
        )
        activated = load_project_identity(case["story"])
        advanced = prepare_event_authority_advance(
            activated,
            expected_authority_epoch=activated.authority["authority_epoch"],
            expected_head_event_hash=activated.authority["head_event_hash"],
            new_head_event_hash="b" * 64,
        )
        identity_path = case["story"] / ".novelagent" / "project.json"
        before = identity_path.read_bytes()
        after = (
            json.dumps(advanced.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        target = PersistenceV2Target(
            target_id="project-identity",
            kind="project_identity",
            path_ref=path_ref_for(
                identity_path, root_id="story_project", root=case["story"]
            ),
            content=after,
            expected_before_exists=True,
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
        )
        declaration = {
            "relative_path": ".novelagent/project.json",
            "role": "project_identity",
            "action": "replace",
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "after_size": len(after),
            "book_id": activated.book_id,
            "expected_authority_epoch": activated.authority["authority_epoch"],
            "expected_head_event_hash": activated.authority["head_event_hash"],
            "after_authority_epoch": advanced.authority["authority_epoch"],
            "after_head_event_hash": advanced.authority["head_event_hash"],
        }
        registry = RootRegistryService(case["transaction_root"]).ensure(case["root_map"])
        source_revision_after = {
            "schema_version": "1.0",
            "book_id": activated.book_id,
            "root_uuid": registry["roots"]["story_project"]["root_uuid"],
            "identity_sha256": declaration["after_sha256"],
            "authority_epoch": declaration["after_authority_epoch"],
            "head_event_hash": declaration["after_head_event_hash"],
        }

        bad_source_revision = dict(source_revision_after)
        bad_source_revision["head_event_hash"] = "c" * 64
        with self.assertRaises(PersistenceV2IntegrityError):
            self._prepare(
                case,
                run_id="identity-head-mismatch",
                book_id=activated.book_id,
                apply_target=target,
                read_set=read_set,
                declared_writes=[declaration],
                story_project_source_revision_after=bad_source_revision,
            )

        transaction = self._prepare(
            case,
            run_id="identity-head-advance",
            book_id=activated.book_id,
            apply_target=target,
            read_set=read_set,
            declared_writes=[declaration],
            story_project_source_revision_after=source_revision_after,
        )
        result = transaction.commit()

        self.assertTrue(result["committed"], result)
        self.assertEqual(
            "b" * 64,
            load_project_identity(case["story"]).authority["head_event_hash"],
        )

    def test_parent_directory_swap_is_detected_before_replace(self) -> None:
        case = self._case("toctou_parent")
        parent = case["story"] / "state"
        parent.mkdir()
        target_path = parent / "value.md"
        target_path.write_text("before", encoding="utf-8")
        target = PersistenceV2Target(
            target_id="state",
            kind="state",
            path_ref=path_ref_for(
                target_path, root_id="story_project", root=case["story"]
            ),
            content="after",
            expected_before_exists=True,
            expected_before_sha256=self._sha("before"),
        )

        def swap(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_apply_target":
                parent.rename(case["story"] / "state-original")
                parent.mkdir()
                (parent / "value.md").write_text("before", encoding="utf-8")

        transaction = self._prepare(case, apply_target=target, fault_injector=swap)
        result = transaction.commit()

        self.assertEqual("recovery_required", result["state"])
        self.assertEqual("before", target_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "before",
            (case["story"] / "state-original" / "value.md").read_text(encoding="utf-8"),
        )

    def test_reparse_parent_is_rejected_during_prepare(self) -> None:
        case = self._case("reparse")
        parent = case["story"] / "state"
        parent.mkdir()
        target_path = parent / "value.md"
        target_path.write_text("before", encoding="utf-8")
        target = PersistenceV2Target(
            target_id="state",
            kind="state",
            path_ref=path_ref_for(
                target_path, root_id="story_project", root=case["story"]
            ),
            content="after",
        )
        real_lstat = os.lstat

        class ReparseStat:
            def __init__(self, wrapped) -> None:
                self._wrapped = wrapped
                self.st_file_attributes = (
                    getattr(wrapped, "st_file_attributes", 0) | FILE_ATTRIBUTE_REPARSE_POINT
                )

            def __getattr__(self, name: str):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            result = real_lstat(path)
            if Path(path).absolute() == parent.absolute():
                return ReparseStat(result)
            return result

        with mock.patch("core.engine.safe_paths.os.lstat", side_effect=fake_lstat):
            with self.assertRaises(SafePathError):
                self._prepare(case, apply_target=target)

    def test_v20_manifest_remains_valid(self) -> None:
        case = self._case("historical")
        transaction = self._prepare(case)
        current = json.loads(transaction.manifest_path.read_text(encoding="utf-8"))
        legacy = copy.deepcopy(current)
        legacy["schema_version"] = "2.0"
        immutable = legacy["immutable"]
        immutable.pop("root_registry")
        immutable.pop("story_project_read_set")
        immutable.pop("read_set_declared_writes")
        for binding in immutable["root_map"].values():
            binding.pop("root_uuid")
        for name in ("manifest_path_ref", "marker_path_ref"):
            immutable[name].pop("root_uuid")
        immutable["publication_receipt"]["path_ref"].pop("root_uuid")
        for target in immutable["targets"]:
            target["path_ref"].pop("root_uuid")
            target.pop("path_guard")
        final = next(item for item in immutable["targets"] if item["kind"] == "final_run_record")
        immutable["final_run"] = {
            "target_id": final["target_id"],
            "kind": final["kind"],
            "path_ref": copy.deepcopy(final["path_ref"]),
            "sha256": final["after_sha256"],
            "size": final["after_size"],
        }
        artifacts = [
            {
                "target_id": item["target_id"],
                "kind": item["kind"],
                "path_ref": copy.deepcopy(item["path_ref"]),
                "sha256": item["after_sha256"],
                "size": item["after_size"],
            }
            for item in immutable["targets"]
            if item["phase"] == "publication" and item["kind"] != "final_run_record"
        ]
        apply = [
            {
                "target_id": item["target_id"],
                "kind": item["kind"],
                "path_ref": copy.deepcopy(item["path_ref"]),
                "sha256": item["after_sha256"],
                "size": item["after_size"],
            }
            for item in immutable["targets"]
            if item["phase"] == "apply"
        ]
        immutable["artifact_bundle_digest"] = canonical_json_hash(
            sorted(artifacts, key=lambda item: item["target_id"])
        )
        immutable["apply_target_bundle_digest"] = canonical_json_hash(
            sorted(apply, key=lambda item: item["target_id"])
        )
        legacy["manifest_digest"] = canonical_json_hash(immutable)

        validated = validate_persistence_manifest_v2(legacy)

        self.assertEqual("2.0", validated["schema_version"])

    def test_completed_receipt_verifies_after_explicit_root_remap(self) -> None:
        case = self._case("completed_remap")
        transaction = self._prepare(case)
        self.assertTrue(transaction.commit()["committed"])
        moved_artifacts = case["base"] / "artifacts-moved"
        case["artifacts"].rename(moved_artifacts)
        service = RootRegistryService(case["transaction_root"])
        before = service.load()
        service.remap(
            {"chapter_artifacts": moved_artifacts},
            expected_revision=before["revision"],
            expected_registry_digest=before["registry_digest"],
        )
        current_roots = dict(case["root_map"])
        current_roots["chapter_artifacts"] = moved_artifacts

        verification = verify_publication_receipt(
            case["runtime"] / "receipts" / "run-1.json",
            root_map=current_roots,
        )

        self.assertTrue(verification["valid"], verification["errors"])


class RootRegistryAndBackendTest(unittest.TestCase):
    def _roots(self, name: str) -> tuple[Path, dict[str, Path]]:
        base = Path.cwd() / ".tmp" / "test_root_registry" / f"{name}_{uuid.uuid4().hex}"
        runtime = base / "runtime"
        story = base / "story"
        runtime.mkdir(parents=True)
        story.mkdir()
        return runtime / "persistence", {"runtime": runtime, "story_project": story}

    def test_remap_is_pure_cas_and_preserves_logical_uuid(self) -> None:
        transaction_root, roots = self._roots("cas")
        service = RootRegistryService(transaction_root)
        external = transaction_root.parent.parent / "external"
        external.mkdir()
        before = service.ensure({**roots, "external": external})
        moved = transaction_root.parent.parent / "external-moved"
        moved.mkdir()

        after = service.remap(
            {"external": moved},
            expected_revision=before["revision"],
            expected_registry_digest=before["registry_digest"],
        )

        self.assertEqual(
            before["roots"]["external"]["root_uuid"],
            after["roots"]["external"]["root_uuid"],
        )
        self.assertEqual(str(moved.absolute()), after["roots"]["external"]["path"])
        self.assertTrue(external.is_dir())
        with self.assertRaises(RootRegistryCasError):
            service.remap(
                {"external": external},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )

    def test_runtime_control_plane_remap_is_rejected(self) -> None:
        transaction_root, roots = self._roots("runtime_remap")
        service = RootRegistryService(transaction_root)
        before = service.ensure(roots)
        moved_runtime = transaction_root.parent.parent / "runtime-moved"
        moved_runtime.mkdir()

        with self.assertRaisesRegex(RootRegistryError, "single registry"):
            service.remap(
                {"runtime": moved_runtime},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )

        self.assertEqual(before, service.load())

    def test_remap_is_blocked_by_pending_or_active_session(self) -> None:
        transaction_root, roots = self._roots("blocked")
        service = RootRegistryService(transaction_root)
        registry = service.ensure(roots)
        moved = transaction_root.parent.parent / "story-moved"
        moved.mkdir()
        pending = transaction_root / "registry" / "pending" / "run-1.json"
        pending.parent.mkdir(parents=True)
        pending.write_text("{}", encoding="utf-8")
        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )
        pending.unlink()
        recovery = transaction_root / "registry" / "recovery_required" / "run-1.json"
        recovery.parent.mkdir(parents=True, exist_ok=True)
        recovery.write_text("{}", encoding="utf-8")
        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )
        recovery.unlink()
        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
                active_sessions=["session-1"],
            )

        invalid_session = transaction_root.parent / "autonomy" / "sessions" / "session-broken"
        invalid_session.mkdir(parents=True)
        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )

    def test_remap_fails_closed_for_expired_lease_with_broken_history(self) -> None:
        transaction_root, roots = self._roots("broken_lease_history")
        service = RootRegistryService(transaction_root)
        registry = service.ensure(roots)
        moved = transaction_root.parent.parent / "story-moved"
        moved.mkdir()
        lease_store = BookLeaseStore(transaction_root.parent / "autonomy")
        lease = lease_store.acquire(
            book_id="book-broken",
            session_id="session-broken",
            plan_id="plan-broken",
            ttl_seconds=1,
            at="2000-01-01T00:00:00+00:00",
        )
        history = lease_store._history_dir(lease["book_id"])
        next(history.glob("*.json")).unlink()

        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )

    def test_remap_fails_closed_for_orphan_lease_history(self) -> None:
        transaction_root, roots = self._roots("orphan_lease_history")
        service = RootRegistryService(transaction_root)
        registry = service.ensure(roots)
        moved = transaction_root.parent.parent / "story-moved"
        moved.mkdir()
        lease_store = BookLeaseStore(transaction_root.parent / "autonomy")
        lease = lease_store.acquire(
            book_id="book-orphan",
            session_id="session-orphan",
            plan_id="plan-orphan",
            ttl_seconds=1,
            at="2000-01-01T00:00:00+00:00",
        )
        lease_store._current_path(lease["book_id"]).unlink()

        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )

    def test_backend_selection_is_explicit_and_never_falls_back(self) -> None:
        transaction_root, roots = self._roots("backend")
        with self.assertRaises(PersistenceBackendError):
            select_persistence_backend(
                "unknown",
                run_dir=transaction_root.parent,
                persistence_dir=transaction_root,
                allowed_roots=roots.values(),
            )
        self.assertIsInstance(
            select_persistence_backend(
                "v1",
                run_dir=transaction_root.parent,
                persistence_dir=transaction_root,
                allowed_roots=roots.values(),
            ),
            LegacyV1PersistenceBackend,
        )
        self.assertIsInstance(
            select_persistence_backend(
                "v2",
                run_dir=transaction_root.parent,
                persistence_dir=transaction_root,
                root_map=roots,
            ),
            PersistenceV2Backend,
        )

        class FailingBackend:
            backend_id = "v2"

            def reconcile(self, *, expected_book_id=None):
                raise RuntimeError("v2 failed")

            def create_transaction(self, **kwargs):
                raise AssertionError("unused")

        coordinator = PersistenceCoordinator(
            run_dir=transaction_root.parent,
            persistence_dir=transaction_root,
            backend=FailingBackend(),
        )
        with self.assertRaisesRegex(RuntimeError, "v2 failed"):
            coordinator.assert_ready()


if __name__ == "__main__":
    unittest.main()
