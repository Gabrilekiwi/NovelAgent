from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest
import uuid
from pathlib import Path

from core.engine.persistence_v2 import (
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
)
from core.engine.persistence import persistence_run_lock
from core.engine.root_registry import RootRegistryError, RootRegistryService, RootRemapBlockedError
from core.memory_v2.canonical import canonical_json_hash
from core.path_refs import path_ref_for
from core.story_project.authority_persistence import (
    EventAuthorityPersistenceBarrierError,
    event_authority_write_operation,
    reconcile_event_authority_persistence,
)
from core.story_project.identity import ensure_project_identity, load_project_identity


class SimulatedPowerLoss(BaseException):
    pass


class EventAuthorityPersistenceBarrierTest(unittest.TestCase):
    def _case(self, name: str) -> dict:
        base = (
            Path.cwd()
            / ".tmp"
            / "event-authority-barrier"
            / f"{name[:18]}-{uuid.uuid4().hex[:10]}"
        )
        story = base / "story"
        runtime_a = base / "writer-a"
        runtime_b = base / "writer-b"
        for path in (story, runtime_a, runtime_b):
            path.mkdir(parents=True)
        identity = ensure_project_identity(story, book_id=f"book-{uuid.uuid4().hex[:16]}")
        shared = story / "shared-authority.json"
        shared.write_bytes(b'{"head":0}\n')
        return {
            "base": base,
            "story": story,
            "book_id": identity.book_id,
            "runtime_a": runtime_a,
            "runtime_b": runtime_b,
            "shared": shared,
        }

    @staticmethod
    def _sha(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _transaction(
        self,
        case: dict,
        *,
        runtime: Path,
        run_id: str,
        before: str,
        after: str,
        fault_injector=None,
    ) -> tuple[PersistenceV2Transaction, dict]:
        transaction_root = runtime / "tx"
        root_map = {
            "story_project": case["story"],
            "runtime": runtime,
        }
        receipt_ref = path_ref_for(
            runtime / "receipts" / f"{run_id}.json",
            root_id="runtime",
            root=runtime,
        )
        final_ref = path_ref_for(
            runtime / "completed" / f"{run_id}.json",
            root_id="runtime",
            root=runtime,
        )
        final = bind_final_run_record_receipt(
            {"id": run_id, "committed": True},
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
        )
        transaction = PersistenceV2Transaction(
            transaction_root=transaction_root,
            run_id=run_id,
            book_id=case["book_id"],
            root_map=root_map,
            fault_injector=fault_injector,
        )
        prepare = {
            "apply_targets": [
                PersistenceV2Target(
                    target_id="authority-state",
                    kind="canonical_projection",
                    path_ref=path_ref_for(
                        case["shared"],
                        root_id="story_project",
                        root=case["story"],
                    ),
                    content=after,
                    expected_before_exists=True,
                    expected_before_sha256=self._sha(before),
                )
            ],
            "artifacts": [],
            "final_run_record": final,
            "final_run_path_ref": final_ref,
            "receipt_id": f"receipt-{run_id}",
            "receipt_path_ref": receipt_ref,
            "context_digest": self._sha(f"context:{run_id}"),
            "generation_input_context_digest": self._sha(f"input:{run_id}"),
            "story_project_source_revision_after": {
                "revision": int(after.split(":")[1].split("}")[0]),
                "digest": self._sha(after),
            },
            "candidate_result": {"run": {"id": run_id}},
            "delivery_jobs": [],
        }
        return transaction, prepare

    def test_post_marker_recovery_precedes_every_other_writer_kind(self) -> None:
        for descendant_kind in ("chapter", "migration", "history_revision"):
            with self.subTest(descendant_kind=descendant_kind):
                case = self._case(f"post-{descendant_kind}")

                def crash(point: str, _index: int | None, _path: Path | None) -> None:
                    if point == "after_commit_marker":
                        raise SimulatedPowerLoss(point)

                first, first_prepare = self._transaction(
                    case,
                    runtime=case["runtime_a"],
                    run_id="writer-a",
                    before='{"head":0}\n',
                    after='{"head":1}\n',
                    fault_injector=crash,
                )
                with self.assertRaises(SimulatedPowerLoss):
                    with event_authority_write_operation(
                        case["story"],
                        expected_book_id=case["book_id"],
                        writer_kind="migration",
                    ) as operation:
                        operation.prepare_transaction(first, **first_prepare)
                        operation.commit_transaction(first)

                self.assertEqual('{"head":1}\n', case["shared"].read_text(encoding="utf-8"))
                self.assertFalse(
                    (case["runtime_a"] / "receipts" / "writer-a.json").exists()
                )

                with event_authority_write_operation(
                    case["story"],
                    expected_book_id=case["book_id"],
                    writer_kind=descendant_kind,
                ) as operation:
                    recovered = [
                        item
                        for item in operation.recovery["transactions"]
                        if item.get("run_id") == "writer-a"
                    ]
                    self.assertEqual(["completed"], [item["state"] for item in recovered])
                    self.assertTrue(
                        (case["runtime_a"] / "receipts" / "writer-a.json").is_file()
                    )
                    second, second_prepare = self._transaction(
                        case,
                        runtime=(
                            case["runtime_a"]
                            if descendant_kind == "migration"
                            else case["runtime_b"]
                        ),
                        run_id=f"writer-b-{descendant_kind}",
                        before='{"head":1}\n',
                        after='{"head":2}\n',
                    )
                    operation.prepare_transaction(second, **second_prepare)
                    committed = operation.commit_transaction(second)
                    self.assertTrue(committed["committed"])

                self.assertEqual('{"head":2}\n', case["shared"].read_text(encoding="utf-8"))
                self.assertFalse(
                    any(
                        (case["story"] / ".novelagent" / "runtime" / "ea" / "r" / "p").glob(
                            "*.json"
                        )
                    )
                )

    def test_pre_marker_crash_is_rolled_back_before_descendant_prepare(self) -> None:
        case = self._case("pre-marker")

        def crash(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "before_commit_marker":
                raise SimulatedPowerLoss(point)

        first, first_prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="pre-marker-a",
            before='{"head":0}\n',
            after='{"head":1}\n',
            fault_injector=crash,
        )
        with self.assertRaises(SimulatedPowerLoss):
            with event_authority_write_operation(
                case["story"],
                expected_book_id=case["book_id"],
                writer_kind="chapter",
            ) as operation:
                operation.prepare_transaction(first, **first_prepare)
                operation.commit_transaction(first)

        self.assertEqual('{"head":1}\n', case["shared"].read_text(encoding="utf-8"))
        with event_authority_write_operation(
            case["story"],
            expected_book_id=case["book_id"],
            writer_kind="history_revision",
        ) as operation:
            recovered = next(
                item
                for item in operation.recovery["transactions"]
                if item.get("run_id") == "pre-marker-a"
            )
            self.assertEqual("rolled_back", recovered["state"])
            self.assertEqual('{"head":0}\n', case["shared"].read_text(encoding="utf-8"))

            second, second_prepare = self._transaction(
                case,
                runtime=case["runtime_b"],
                run_id="pre-marker-b",
                before='{"head":0}\n',
                after='{"head":2}\n',
            )
            operation.prepare_transaction(second, **second_prepare)
            self.assertTrue(operation.commit_transaction(second)["committed"])

    def test_pending_entry_uses_uuid_bound_pathref_and_wrong_uuid_fails_closed(self) -> None:
        case = self._case("wrong-uuid")

        def crash(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise SimulatedPowerLoss(point)

        first, first_prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="uuid-a",
            before='{"head":0}\n',
            after='{"head":1}\n',
            fault_injector=crash,
        )
        with self.assertRaises(SimulatedPowerLoss):
            with event_authority_write_operation(
                case["story"],
                expected_book_id=case["book_id"],
                writer_kind="chapter",
            ) as operation:
                operation.prepare_transaction(first, **first_prepare)
                operation.commit_transaction(first)

        pending = next(
            (case["story"] / ".novelagent" / "runtime" / "ea" / "r" / "p").glob(
                "*.json"
            )
        )
        entry = json.loads(pending.read_text(encoding="utf-8"))
        self.assertNotIn(str(case["runtime_a"]), pending.read_text(encoding="utf-8"))
        self.assertIn("root_uuid", entry["transaction_root_ref"])
        entry["transaction_root_ref"]["root_uuid"] = "00000000-0000-4000-8000-000000000000"
        entry["entry_hash"] = canonical_json_hash(
            entry, exclude_fields=("entry_hash",)
        )
        pending.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            EventAuthorityPersistenceBarrierError,
            "event_authority_global_recovery_required",
        ):
            with event_authority_write_operation(
                case["story"],
                expected_book_id=case["book_id"],
                writer_kind="migration",
            ):
                self.fail("wrong UUID must block every descendant writer")
        self.assertEqual('{"head":1}\n', case["shared"].read_text(encoding="utf-8"))

    def test_normal_chapter_commit_does_not_reenter_windows_state_lock(self) -> None:
        case = self._case("normal-chapter")
        # First-use registry creation must not run the remap-only active-session
        # scan; an autonomy chapter naturally starts after its session exists.
        (
            case["story"]
            / ".novelagent"
            / "runtime"
            / "autonomy"
            / "sessions"
            / "active-before-first-ea-registry"
        ).mkdir(parents=True)
        transaction, prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="normal-chapter",
            before='{"head":0}\n',
            after='{"head":1}\n',
        )
        with event_authority_write_operation(
            case["story"],
            expected_book_id=case["book_id"],
            writer_kind="chapter",
        ) as operation:
            operation.prepare_transaction(transaction, **prepare)
            result = operation.commit_transaction(transaction)
        self.assertEqual("completed", result["state"])
        self.assertTrue(result["committed"])

    def test_non_history_commit_reacquires_dependency_fence(self) -> None:
        case = self._case("chapter-dependency")
        transaction, prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="chapter-dependency",
            before='{"head":0}\n',
            after='{"head":1}\n',
        )
        dependency_root = (
            case["story"]
            / ".novelagent"
            / "runtime"
            / ".root-remap-fence"
        )
        acquired = threading.Event()
        release = threading.Event()

        def hold_dependency_fence() -> None:
            with persistence_run_lock(dependency_root):
                acquired.set()
                release.wait(timeout=10.0)

        with event_authority_write_operation(
            case["story"],
            expected_book_id=case["book_id"],
            writer_kind="chapter",
        ) as operation:
            operation.prepare_transaction(transaction, **prepare)
            holder = threading.Thread(target=hold_dependency_fence, daemon=True)
            holder.start()
            self.assertTrue(acquired.wait(timeout=2.0))
            with self.assertRaisesRegex(
                EventAuthorityPersistenceBarrierError,
                "event_authority_dependency_busy",
            ):
                operation.commit_transaction(transaction)
            release.set()
            holder.join(timeout=5.0)
            self.assertFalse(holder.is_alive())
            result = operation.commit_transaction(transaction)

        self.assertEqual("completed", result["state"])

    def test_pending_entry_blocks_remap_and_ensure_then_recovers_original_root(self) -> None:
        case = self._case("remap-blocked")

        def crash(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise SimulatedPowerLoss(point)

        transaction, prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="remap-blocked",
            before='{"head":0}\n',
            after='{"head":1}\n',
            fault_injector=crash,
        )
        with self.assertRaises(SimulatedPowerLoss):
            with event_authority_write_operation(
                case["story"],
                expected_book_id=case["book_id"],
                writer_kind="chapter",
            ) as operation:
                operation.prepare_transaction(transaction, **prepare)
                operation.commit_transaction(transaction)

        home = case["story"] / ".novelagent" / "runtime" / "ea"
        service = RootRegistryService(home)
        registry = service.load()
        registry_before = service.registry_path.read_bytes()
        pending_path = next((home / "r" / "p").glob("*.json"))
        entry = json.loads(pending_path.read_text(encoding="utf-8"))
        entry_text = pending_path.read_text(encoding="utf-8")
        # root_registry.json is the sole physical-path control-plane exception;
        # the pending recovery record itself contains only a UUID-bound PathRef.
        self.assertNotIn(str(case["runtime_a"]), entry_text)
        self.assertEqual(
            str(case["runtime_a"].absolute()),
            registry["roots"]["external:event-authority-chapter"]["path"],
        )
        self.assertEqual(registry["revision"], entry["root_registry_revision"])
        self.assertEqual(registry["registry_digest"], entry["root_registry_digest"])

        moved_parent = case["base"] / "moved-writer"
        moved_parent.mkdir()
        with self.assertRaises(RootRemapBlockedError):
            service.remap(
                {"external:event-authority-chapter": moved_parent},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )
        self.assertEqual(registry_before, service.registry_path.read_bytes())

        with self.assertRaises(RootRemapBlockedError):
            service.ensure(
                {
                    "story_project": case["story"],
                    "external:event-authority-history_revision": moved_parent,
                },
                require_runtime=False,
            )
        self.assertEqual(registry_before, service.registry_path.read_bytes())

        with event_authority_write_operation(
            case["story"],
            expected_book_id=case["book_id"],
            writer_kind="migration",
        ) as operation:
            recovered = next(
                item
                for item in operation.recovery["transactions"]
                if item.get("run_id") == "remap-blocked"
            )
            self.assertEqual("completed", recovered["state"])
        self.assertTrue(
            (case["runtime_a"] / "receipts" / "remap-blocked.json").is_file()
        )

    def test_registry_revision_drift_is_recovery_required_never_abandoned(self) -> None:
        case = self._case("registry-drift")

        def crash(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise SimulatedPowerLoss(point)

        transaction, prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="registry-drift",
            before='{"head":0}\n',
            after='{"head":1}\n',
            fault_injector=crash,
        )
        with self.assertRaises(SimulatedPowerLoss):
            with event_authority_write_operation(
                case["story"],
                expected_book_id=case["book_id"],
                writer_kind="chapter",
            ) as operation:
                operation.prepare_transaction(transaction, **prepare)
                operation.commit_transaction(transaction)

        home = case["story"] / ".novelagent" / "runtime" / "ea"
        service = RootRegistryService(home)
        registry = service.load()
        moved_parent = case["base"] / "bypassed-remap"
        moved_parent.mkdir()
        binding = registry["roots"]["external:event-authority-chapter"]
        binding["path"] = str(moved_parent.absolute())
        binding["path_identity_sha256"] = hashlib.sha256(
            os.path.normcase(str(moved_parent.absolute())).encode("utf-8")
        ).hexdigest()
        registry["revision"] += 1
        registry["registry_digest"] = canonical_json_hash(
            registry, exclude_fields=("registry_digest",)
        )
        service.registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        recovery = reconcile_event_authority_persistence(
            case["story"], expected_book_id=case["book_id"]
        )
        self.assertFalse(recovery["ok"])
        self.assertEqual(
            ["recovery_required"],
            [item["state"] for item in recovery["transactions"]],
        )
        self.assertEqual(1, len(list((home / "r" / "p").glob("*.json"))))
        self.assertEqual([], list((home / "r" / "a").glob("*.json")))
        self.assertFalse(
            (moved_parent / "receipts" / "registry-drift.json").exists()
        )

    def test_idle_ea_single_registry_remap_is_rejected_without_id_drift(self) -> None:
        case = self._case("idle-remap")
        first, first_prepare = self._transaction(
            case,
            runtime=case["runtime_a"],
            run_id="idle-remap-a",
            before='{"head":0}\n',
            after='{"head":1}\n',
        )
        with event_authority_write_operation(
            case["story"],
            expected_book_id=case["book_id"],
            writer_kind="chapter",
        ) as operation:
            operation.prepare_transaction(first, **first_prepare)
            self.assertTrue(operation.commit_transaction(first)["committed"])

        moved_story = case["base"] / "story-moved"
        case["story"].rename(moved_story)
        case["story"] = moved_story
        case["shared"] = moved_story / "shared-authority.json"
        home = moved_story / ".novelagent" / "runtime" / "ea"
        service = RootRegistryService(home)
        before = service.load()
        root_ids = set(before["roots"])
        root_uuids = {
            root_id: binding["root_uuid"]
            for root_id, binding in before["roots"].items()
        }

        with self.assertRaisesRegex(RootRegistryError, "project-level"):
            service.remap(
                {
                    "story_project": moved_story,
                    "external:event-authority-chapter": case["runtime_b"],
                },
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )
        remapped = service.load()
        self.assertEqual(before["registry_id"], remapped["registry_id"])
        self.assertEqual(before["revision"], remapped["revision"])
        self.assertEqual(root_ids, set(remapped["roots"]))
        self.assertEqual(
            root_uuids,
            {
                root_id: binding["root_uuid"]
                for root_id, binding in remapped["roots"].items()
            },
        )

        after = service.load()
        self.assertEqual(remapped["registry_digest"], after["registry_digest"])
        self.assertEqual(root_ids, set(after["roots"]))
        self.assertEqual('{"head":1}\n', case["shared"].read_text(encoding="utf-8"))

    def test_real_agent_executor_recovers_cross_root_marked_transaction(self) -> None:
        from tests.test_executor_event_v2 import EventAuthorityExecutorV2E2ETest

        fixture = EventAuthorityExecutorV2E2ETest()._event_case()
        runtime = fixture["case"] / "cross-root-pending"
        runtime.mkdir()
        shared = fixture["book"] / ".novelagent" / "synthetic-authority.json"
        shared.write_bytes(b'{"head":0}\n')
        case = {
            "story": fixture["book"],
            "book_id": load_project_identity(fixture["book"]).book_id,
            "runtime_a": runtime,
            "shared": shared,
        }

        def crash(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise SimulatedPowerLoss(point)

        pending, pending_prepare = self._transaction(
            case,
            runtime=runtime,
            run_id="before-agent-executor",
            before='{"head":0}\n',
            after='{"head":1}\n',
            fault_injector=crash,
        )
        with self.assertRaises(SimulatedPowerLoss):
            with event_authority_write_operation(
                fixture["book"],
                expected_book_id=case["book_id"],
                writer_kind="migration",
            ) as operation:
                operation.prepare_transaction(pending, **pending_prepare)
                operation.commit_transaction(pending)

        result = fixture["executor"].run_once(persist=True)
        self.assertTrue(result["committed"])
        self.assertEqual('{"head":1}\n', shared.read_text(encoding="utf-8"))
        self.assertTrue(
            (runtime / "receipts" / "before-agent-executor.json").is_file()
        )
        self.assertEqual(
            [],
            list(
                (
                    fixture["book"]
                    / ".novelagent"
                    / "runtime"
                    / "ea"
                    / "r"
                    / "p"
                ).glob("*.json")
            ),
        )


if __name__ == "__main__":
    unittest.main()
