from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from core.autonomy.arc import ArcPlanStore, build_run_arc_plan
from core.autonomy.operations import AutonomyOperationError
from core.autonomy.receipts import StageReceiptStore
from core.autonomy.session import AutonomySessionStore
from core.engine.root_registry import RootRegistryService, RootRemapBlockedError
from tests.test_autonomy_plans import (
    instruction_plan,
    source_snapshot,
    trusted_profiles,
    workspace_case,
)
from tests.test_autonomy_receipts import (
    append_successful_stage_chain,
    publication,
    stage_pair,
    verify_publication,
)


T0 = "2026-07-14T00:00:00+00:00"
T1 = "2026-07-14T00:01:00+00:00"


class SimulatedProcessCrash(BaseException):
    """Bypass ``except Exception`` exactly like abrupt process termination."""


def _fault_json_call(
    target: str,
    *,
    predicate,
    after_write: bool,
):
    module_name, attribute = target.rsplit(".", 1)
    module = importlib.import_module(module_name)
    original = getattr(module, attribute)
    fired = False

    def injected(*args, **kwargs):
        nonlocal fired
        should_crash = not fired and predicate(*args, **kwargs)
        if should_crash and not after_write:
            fired = True
            raise SimulatedProcessCrash(target)
        result = original(*args, **kwargs)
        if should_crash:
            fired = True
            raise SimulatedProcessCrash(target)
        return result

    return patch(target, side_effect=injected)


class AutonomyRecoveryTest(unittest.TestCase):
    def _execute_plan(self) -> dict:
        return instruction_plan(count=1)

    def test_execute_recovers_every_cross_file_publication_window(self) -> None:
        windows = (
            (
                "lease_history_before_current",
                "core.autonomy.lease.atomic_replace_json",
                lambda path, payload: Path(path).name == "current.json"
                and payload.get("status") == "active",
                False,
            ),
            (
                "arc_revision_before_head",
                "core.autonomy.arc.atomic_replace_json",
                lambda path, payload: Path(path).name == "head.json",
                False,
            ),
            (
                "genesis_before_plan",
                "core.autonomy.session.atomic_append_json",
                lambda path, payload: Path(path).name == "genesis.json",
                True,
            ),
            (
                "started_event_before_index",
                "core.autonomy.session.atomic_append_json",
                lambda path, payload: Path(path).parent.name == "events",
                True,
            ),
            (
                "plan_index_before_latest",
                "core.autonomy.session.atomic_append_json",
                lambda path, payload: Path(path).parent.name == "plan_sessions",
                True,
            ),
            (
                "latest_before_operation_result",
                "core.autonomy.session.atomic_replace_json",
                lambda path, payload: Path(path).name == "latest.json",
                True,
            ),
            (
                "operation_result_durable_before_return",
                "core.autonomy.operations.atomic_append_json",
                lambda path, payload: Path(path).name == "result.json"
                and payload.get("outcome") == "completed",
                True,
            ),
        )
        for name, target, predicate, after_write in windows:
            with self.subTest(window=name), workspace_case(f"recover_{name}") as temporary:
                root = Path(temporary)
                store = AutonomySessionStore(root, trusted_profiles=trusted_profiles())
                plan = self._execute_plan()
                with _fault_json_call(
                    target, predicate=predicate, after_write=after_write
                ):
                    with self.assertRaises(SimulatedProcessCrash):
                        store.execute_plan(
                            plan,
                            source_snapshot_loader=lambda: source_snapshot(),
                            at=T0,
                        )

                recovered = AutonomySessionStore(
                    root, trusted_profiles=trusted_profiles()
                )
                # An unverified lease-only attempt is rolled back on open; all
                # source-verified windows are rolled forward. Re-execution is
                # safe in both cases and must never append a second start.
                status = recovered.execute_plan(
                    plan,
                    source_snapshot_loader=lambda: source_snapshot(),
                    at=T1,
                )
                self.assertEqual("active", status["state"])
                self.assertEqual(1, status["event_count"])
                self.assertEqual(0, status["completed_count"])
                self.assertFalse(recovered.operations.pending())

    def test_source_verified_resume_rolls_forward_once_after_crash(self) -> None:
        with workspace_case("recover_resume") as temporary:
            root = Path(temporary)
            store = AutonomySessionStore(root, trusted_profiles=trusted_profiles())
            plan = self._execute_plan()
            started = store.execute_plan(
                plan, source_snapshot_loader=lambda: source_snapshot(), at=T0
            )
            store.cancel(started["session_id"], at=T0)
            original = store.operations.mark_source_verified

            def crash_after_marker(*args, **kwargs):
                original(*args, **kwargs)
                raise SimulatedProcessCrash("resume source marker")

            with patch.object(
                store.operations,
                "mark_source_verified",
                side_effect=crash_after_marker,
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    store.resume(
                        started["session_id"],
                        source_snapshot_loader=lambda: source_snapshot(),
                        at=T1,
                    )

            recovered = AutonomySessionStore(
                root, trusted_profiles=trusted_profiles()
            )
            status = recovered.status(started["session_id"], at=T1)
            self.assertEqual("active", status["state"])
            self.assertEqual(3, status["event_count"])
            replay = recovered.resume(
                started["session_id"],
                source_snapshot_loader=lambda: source_snapshot(),
                at=T1,
            )
            self.assertEqual(3, replay["event_count"])
            self.assertFalse(recovered.operations.pending())

    def test_tampered_recovery_marker_fails_closed_on_open(self) -> None:
        with workspace_case("recover_tampered_marker") as temporary:
            root = Path(temporary)
            store = AutonomySessionStore(root, trusted_profiles=trusted_profiles())
            original = store.operations.mark_source_verified

            def crash_after_marker(*args, **kwargs):
                original(*args, **kwargs)
                raise SimulatedProcessCrash("execute source marker")

            with patch.object(
                store.operations,
                "mark_source_verified",
                side_effect=crash_after_marker,
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    store.execute_plan(
                        self._execute_plan(),
                        source_snapshot_loader=lambda: source_snapshot(),
                        at=T0,
                    )
            marker_path = next((root / "operations").glob("*/source-verified.json"))
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["source_snapshot_hash"] = "f" * 64
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaisesRegex(
                AutonomyOperationError,
                "autonomy_operation_verification_hash_mismatch",
            ):
                AutonomySessionStore(root, trusted_profiles=trusted_profiles())

    def test_completed_event_and_interrupted_lease_release_recover_once(self) -> None:
        with workspace_case("recover_complete") as temporary:
            root = Path(temporary)
            store = AutonomySessionStore(
                root,
                trusted_profiles=trusted_profiles(),
                publication_verifier=verify_publication,
            )
            plan = self._execute_plan()
            started = store.execute_plan(
                plan, source_snapshot_loader=lambda: source_snapshot(), at=T0
            )
            final_stage = append_successful_stage_chain(
                store.stage_receipts,
                lease_hash=started["lease_hash"],
                chapter=11,
                session_id=started["session_id"],
                plan_id=plan["plan_id"],
            )
            arc = store.arc_plans.load(started["arc_plan_id"])
            store.completion_ledger(started["session_id"]).append(
                final_stage_receipt=final_stage,
                publication_receipt=publication(11),
                planned_target_hash=arc["targets"][0]["target_hash"],
                chapter_body_hash="6" * 64,
                source_snapshot_after=source_snapshot(chapter=12, digest="a" * 64),
                created_at=T1,
            )
            with _fault_json_call(
                "core.autonomy.lease.atomic_replace_json",
                predicate=lambda path, payload: Path(path).name == "current.json"
                and payload.get("status") == "released",
                after_write=False,
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    store.complete(started["session_id"], at=T1)

            recovered = AutonomySessionStore(
                root,
                trusted_profiles=trusted_profiles(),
                publication_verifier=verify_publication,
            )
            status = recovered.status(started["session_id"], at=T1)
            self.assertEqual("completed", status["state"])
            self.assertFalse(status["lease_held"])
            self.assertEqual(2, status["event_count"])
            replay = recovered.complete(started["session_id"], at=T1)
            self.assertEqual(2, replay["event_count"])
            self.assertEqual(1, replay["completed_count"])
            self.assertFalse(recovered.operations.pending())

    def test_arc_orphan_revision_rolls_forward_without_duplicate_fulfillment(self) -> None:
        with workspace_case("recover_arc_revision") as temporary:
            store = ArcPlanStore(Path(temporary))
            plan = self._execute_plan()
            arc = store.create(
                build_run_arc_plan(plan, session_id="session-recovery", created_at=T0)
            )
            fulfilled = dict(arc["targets"][0]["planned"])
            with _fault_json_call(
                "core.autonomy.arc.atomic_replace_json",
                predicate=lambda path, payload: Path(path).name == "head.json"
                and payload.get("revision") == 2,
                after_write=False,
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    store.record_fulfillment(
                        arc["arc_plan_id"],
                        chapter_index=11,
                        fulfilled=fulfilled,
                        completion_receipt_hash="c" * 64,
                        expected_arc_plan_hash=arc["arc_plan_hash"],
                        recorded_at=T1,
                    )
            recovered = store.load(arc["arc_plan_id"])
            self.assertEqual(2, recovered["revision"])
            replay = store.record_fulfillment(
                arc["arc_plan_id"],
                chapter_index=11,
                fulfilled=fulfilled,
                completion_receipt_hash="c" * 64,
                expected_arc_plan_hash=arc["arc_plan_hash"],
                recorded_at=T1,
            )
            self.assertEqual(2, replay["revision"])
            revisions = list(
                (Path(temporary) / "arc_plans" / arc["arc_plan_id"] / "revisions").glob(
                    "*.json"
                )
            )
            self.assertEqual(2, len(revisions))

    def test_orphan_stage_fence_is_reused_without_duplicate_receipt(self) -> None:
        with workspace_case("recover_stage_fence") as temporary:
            root = Path(temporary)
            store = StageReceiptStore(root)
            plan = self._execute_plan()
            lease = store.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at=T0,
            )
            authorization, receipt = stage_pair(
                chapter=11, stage="outline", plan_id=plan["plan_id"]
            )
            with _fault_json_call(
                "core.autonomy.receipts.atomic_append_json",
                predicate=lambda path, payload: Path(path).parent.name == "fences",
                after_write=True,
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    store.append(
                        receipt,
                        authorization=authorization,
                        expected_lease_hash=lease["lease_hash"],
                        at=T0,
                    )
            replay = store.append(
                receipt,
                authorization=authorization,
                expected_lease_hash=lease["lease_hash"],
                at=T1,
            )
            self.assertEqual(receipt, replay)
            self.assertEqual(1, len(store.load_chain("session-receipts", 11)))

    def test_root_remap_scanner_blocks_active_and_allows_terminal_session(self) -> None:
        with workspace_case("recover_remap_scanner") as temporary:
            base = Path(temporary)
            runtime = base / "runtime"
            story = base / "story"
            moved = base / "story-moved"
            runtime.mkdir()
            story.mkdir()
            moved.mkdir()
            service = RootRegistryService(runtime / "persistence")
            registry = service.ensure({"runtime": runtime, "story_project": story})
            store = AutonomySessionStore(
                runtime / "autonomy", trusted_profiles=trusted_profiles()
            )
            started = store.execute_plan(
                self._execute_plan(),
                source_snapshot_loader=lambda: source_snapshot(),
                at=T0,
            )
            with self.assertRaises(RootRemapBlockedError):
                service.remap(
                    {"story_project": moved},
                    expected_revision=registry["revision"],
                    expected_registry_digest=registry["registry_digest"],
                )
            store.cancel(started["session_id"], at=T1)
            remapped = service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )
            self.assertEqual(str(moved.absolute()), remapped["roots"]["story_project"]["path"])

    def test_root_remap_blocks_terminal_session_with_pending_operation(self) -> None:
        with workspace_case("recover_remap_pending_operation") as temporary:
            base = Path(temporary)
            runtime = base / "runtime"
            story = base / "story"
            moved = base / "story-moved"
            runtime.mkdir()
            story.mkdir()
            moved.mkdir()
            service = RootRegistryService(runtime / "persistence")
            registry = service.ensure({"runtime": runtime, "story_project": story})
            store = AutonomySessionStore(
                runtime / "autonomy", trusted_profiles=trusted_profiles()
            )
            started = store.execute_plan(
                self._execute_plan(),
                source_snapshot_loader=lambda: source_snapshot(),
                at=T0,
            )
            with patch.object(
                store.operations,
                "finish",
                side_effect=SimulatedProcessCrash("terminal operation result"),
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    store.cancel(started["session_id"], at=T1)
            # The event and released lease are terminal, but the immutable
            # intent has no result marker yet and must fence root remapping.
            with self.assertRaises(RootRemapBlockedError):
                service.remap(
                    {"story_project": moved},
                    expected_revision=registry["revision"],
                    expected_registry_digest=registry["registry_digest"],
                )
            store.reconcile_orphans(at=T1)
            remapped = service.remap(
                {"story_project": moved},
                expected_revision=registry["revision"],
                expected_registry_digest=registry["registry_digest"],
            )
            self.assertEqual(str(moved.absolute()), remapped["roots"]["story_project"]["path"])


if __name__ == "__main__":
    unittest.main()
