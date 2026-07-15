from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import core.autonomy.operations as operations_module
import core.autonomy.session as session_module

from core.autonomy.lease import BookLeaseError
from core.autonomy.operations import AutonomyOperationError
from core.autonomy.plans import AutonomyPlanError, compile_instruction_plan
from core.autonomy.session import AutonomySessionError, AutonomySessionStore
from core.engine.persistence import PersistenceLockError, persistence_run_lock
from tests.test_autonomy_receipts import (
    append_successful_stage_chain,
    publication,
    verify_publication,
)
from tests.test_autonomy_plans import (
    instruction_plan,
    source_snapshot,
    trusted_profiles,
    workspace_case,
)


T0 = "2026-07-14T00:00:00+00:00"
T1 = "2026-07-14T00:01:00+00:00"
T2 = "2026-07-14T00:02:00+00:00"
T3 = "2026-07-14T00:03:00+00:00"


class AutonomySessionStoreTest(unittest.TestCase):
    def _plan(self) -> dict:
        return compile_instruction_plan(
            "连续写 3章",
            trusted_profiles=trusted_profiles(),
            source_snapshot=source_snapshot(),
            created_at=T0,
        )

    def test_execute_is_idempotent_and_commands_form_event_chain(self) -> None:
        with workspace_case("session_commands") as temporary:
            store = AutonomySessionStore(
                Path(temporary), trusted_profiles=trusted_profiles()
            )
            plan = self._plan()
            started = store.execute_plan(
                plan,
                source_snapshot_loader=lambda: source_snapshot(),
                lease_ttl_seconds=300,
                at=T0,
            )
            self.assertEqual("active", started["state"])
            self.assertTrue(started["lease_held"])
            self.assertEqual(0, started["completed_count"])
            store.assert_outline_provider_allowed(started["session_id"], at=T1)
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_session_incomplete"
            ):
                store.complete(started["session_id"], at=T1)

            replay = store.execute_plan(
                plan,
                source_snapshot_loader=lambda: source_snapshot(),
                at=T1,
            )
            self.assertEqual(started["session_id"], replay["session_id"])
            self.assertEqual(1, replay["event_count"])

            recovery_store = AutonomySessionStore(Path(temporary))
            inspectable = recovery_store.status(started["session_id"], at=T1)
            self.assertFalse(inspectable["trusted_profiles_current"])

            cancelled = recovery_store.cancel(
                started["session_id"], reason="operator pause", at=T1
            )
            self.assertEqual("cancelled", cancelled["state"])
            self.assertFalse(cancelled["lease_held"])
            with self.assertRaisesRegex(
                AutonomySessionError, "outline_session_not_active"
            ):
                store.assert_outline_provider_allowed(started["session_id"], at=T1)

            resumed = store.resume(
                started["session_id"],
                source_snapshot_loader=lambda: source_snapshot(),
                lease_ttl_seconds=300,
                at=T2,
            )
            self.assertEqual("active", resumed["state"])
            self.assertTrue(resumed["lease_held"])
            self.assertEqual(3, resumed["event_count"])

            abandoned = store.abandon(
                started["session_id"], reason="operator ended plan", at=T3
            )
            self.assertEqual("abandoned", abandoned["state"])
            with self.assertRaisesRegex(AutonomySessionError, "autonomy_session_terminal"):
                store.resume(
                    started["session_id"],
                    source_snapshot_loader=lambda: source_snapshot(),
                    at=T3,
                )

    def test_terminal_side_effect_without_marker_cannot_be_rolled_back(self) -> None:
        class AbruptCrash(BaseException):
            pass

        for fault_point in ("event", "result"):
            with self.subTest(fault_point=fault_point), workspace_case(
                f"terminal_marker_loss_{fault_point}"
            ) as temporary:
                root = Path(temporary)
                store = AutonomySessionStore(
                    root, trusted_profiles=trusted_profiles()
                )
                started = store.execute_plan(
                    self._plan(),
                    source_snapshot_loader=lambda: source_snapshot(),
                    at=T0,
                )

                if fault_point == "event":
                    original = session_module.atomic_append_json

                    def injected(path, payload):
                        result = original(path, payload)
                        if payload.get("event_type") == "cancelled":
                            raise AbruptCrash("after terminal event")
                        return result

                    target = "core.autonomy.session.atomic_append_json"
                else:
                    original = operations_module.atomic_append_json

                    def injected(path, payload):
                        if Path(path).name == "result.json":
                            raise AbruptCrash("before terminal result")
                        return original(path, payload)

                    target = "core.autonomy.operations.atomic_append_json"

                with patch(target, side_effect=injected), self.assertRaises(AbruptCrash):
                    store.cancel(started["session_id"], at=T1)

                intent = next(
                    item
                    for item in store.operations.pending()
                    if item["operation_type"] == "cancel"
                )
                marker = root / "operations" / intent["operation_id"] / "commit.marker"
                self.assertTrue(marker.is_file())
                marker.unlink()
                with self.assertRaisesRegex(
                    AutonomyOperationError,
                    "autonomy_operation_side_effect_without_marker",
                ):
                    store.reconcile_orphans(at=T1)
                self.assertEqual(
                    "cancelled", store._load_events(started["session_id"])[-1]["event_type"]
                )

    def test_source_drift_blocks_execute_and_resume(self) -> None:
        with workspace_case("session_source_drift") as temporary:
            store = AutonomySessionStore(
                Path(temporary), trusted_profiles=trusted_profiles()
            )
            plan = self._plan()

            def stale_after_lease() -> dict:
                lease = store.leases.load("book-autonomy")
                self.assertEqual("active", lease["status"])
                self.assertEqual(plan["plan_id"], lease["plan_id"])
                return source_snapshot(digest="9" * 64)

            with self.assertRaisesRegex(AutonomyPlanError, "instruction_source_snapshot_stale"):
                store.execute_plan(
                    plan,
                    source_snapshot_loader=stale_after_lease,
                    at=T0,
                )
            self.assertEqual("released", store.leases.load("book-autonomy")["status"])
            started = store.execute_plan(
                plan, source_snapshot_loader=lambda: source_snapshot(), at=T0
            )
            store.cancel(started["session_id"], at=T1)
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_session_source_snapshot_stale"
            ):
                store.resume(
                    started["session_id"],
                    source_snapshot_loader=lambda: source_snapshot(digest="8" * 64),
                    at=T2,
                )
            self.assertFalse(store.status(started["session_id"], at=T2)["lease_held"])

    def test_session_transition_holds_remap_fence_and_residual_lease_blocks(self) -> None:
        with workspace_case("session_remap_fence") as temporary:
            runtime = Path(temporary)
            root = runtime / "autonomy"
            store = AutonomySessionStore(root, trusted_profiles=trusted_profiles())
            plan = self._plan()
            loader_entered = threading.Event()
            allow_loader = threading.Event()
            outcome: list[object] = []

            def fenced_loader() -> dict:
                loader_entered.set()
                if not allow_loader.wait(timeout=5):
                    raise RuntimeError("test loader was not released")
                return source_snapshot()

            def start_session() -> None:
                try:
                    outcome.append(
                        store.execute_plan(
                            plan, source_snapshot_loader=fenced_loader, at=T0
                        )
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome.append(exc)

            worker = threading.Thread(target=start_session)
            worker.start()
            self.assertTrue(loader_entered.wait(timeout=5))
            with self.assertRaises(PersistenceLockError):
                with persistence_run_lock(runtime / ".root-remap-fence"):
                    pass
            allow_loader.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(outcome))
            self.assertIsInstance(outcome[0], dict)
            started = outcome[0]
            assert isinstance(started, dict)
            self.assertTrue(started["root_remap_blocked"])

            forced = BookLeaseError("forced_release_failure", "simulated failure")
            with patch.object(store.leases, "_release_fenced", side_effect=forced):
                with self.assertRaisesRegex(
                    BookLeaseError, "forced_release_failure"
                ):
                    store.cancel(started["session_id"], at=T1)
            residual = store.status(started["session_id"], at=T1)
            self.assertEqual("cancelled", residual["state"])
            self.assertTrue(residual["lease_held"])
            self.assertTrue(residual["root_remap_blocked"])
            retried = store.cancel(started["session_id"], at=T1)
            self.assertFalse(retried["lease_held"])
            self.assertFalse(retried["root_remap_blocked"])

    def test_tampered_event_chain_is_detected(self) -> None:
        with workspace_case("session_tamper") as temporary:
            root = Path(temporary)
            store = AutonomySessionStore(root, trusted_profiles=trusted_profiles())
            started = store.execute_plan(
                self._plan(), source_snapshot_loader=lambda: source_snapshot(), at=T0
            )
            event_path = next((root / "sessions").rglob("events/*.json"))
            payload = json.loads(event_path.read_text(encoding="utf-8"))
            payload["event_type"] = "abandoned"
            event_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_session_event_hash_mismatch"
            ):
                store.status(started["session_id"], at=T1)

    def test_missing_latest_pointer_rebuilds_from_verified_session_chain(self) -> None:
        with workspace_case("session_latest_rebuild") as temporary:
            root = Path(temporary)
            store = AutonomySessionStore(root, trusted_profiles=trusted_profiles())
            started = store.execute_plan(
                self._plan(), source_snapshot_loader=lambda: source_snapshot(), at=T0
            )
            latest = root / "sessions" / "latest.json"
            latest.unlink()

            rebuilt = store.status(None, at=T1)
            self.assertEqual(started["session_id"], rebuilt["session_id"])
            self.assertTrue(latest.is_file())
            pointer = json.loads(latest.read_text(encoding="utf-8"))
            self.assertEqual(started["session_id"], pointer["session_id"])

            latest.unlink()
            index_path = next((root / "plan_sessions").glob("*.json"))
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["genesis_hash"] = "0" * 64
            index_path.write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_latest_session_invalid"
            ):
                store.status(None, at=T1)

    def test_resume_accepts_only_source_evolution_proven_by_completion_chain(self) -> None:
        with workspace_case("session_completion_resume") as temporary:
            store = AutonomySessionStore(
                Path(temporary),
                trusted_profiles=trusted_profiles(),
                publication_verifier=verify_publication,
            )
            plan = self._plan()
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
            after = source_snapshot(chapter=12, digest="a" * 64)
            store.completion_ledger(started["session_id"]).append(
                final_stage_receipt=final_stage,
                publication_receipt=publication(11),
                planned_target_hash=arc["targets"][0]["target_hash"],
                chapter_body_hash="6" * 64,
                source_snapshot_after=after,
                created_at=T1,
            )
            store.cancel(started["session_id"], at=T1)
            resumed = store.resume(
                started["session_id"], source_snapshot_loader=lambda: after, at=T2
            )
            self.assertEqual(1, resumed["completed_count"])
            self.assertEqual(12, resumed["canonical_next_chapter"])

            replay = store.execute_plan(
                plan, source_snapshot_loader=lambda: after, at=T2
            )
            self.assertEqual(started["session_id"], replay["session_id"])
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_session_source_snapshot_stale"
            ):
                store.execute_plan(
                    plan, source_snapshot_loader=lambda: source_snapshot(), at=T2
                )

    def test_required_delivery_blocks_later_stages_and_completion(self) -> None:
        with workspace_case("session_delivery_blocked") as temporary:
            store = AutonomySessionStore(
                Path(temporary),
                trusted_profiles=trusted_profiles(),
                publication_verifier=verify_publication,
            )
            plan = instruction_plan(count=1)
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
                status="local_committed_delivery_blocked",
                created_at=T1,
            )
            blocked = store.status(started["session_id"], at=T1)
            self.assertTrue(blocked["delivery_blocked"])
            self.assertFalse(blocked["finalization_required"])
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_session_delivery_blocked"
            ):
                store.assert_stage_provider_allowed(
                    started["session_id"], stage="outline", at=T1
                )
            with self.assertRaisesRegex(
                AutonomySessionError, "autonomy_session_delivery_blocked"
            ):
                store.complete(started["session_id"], at=T1)

    def test_verified_target_count_requires_explicit_finalization(self) -> None:
        with workspace_case("session_finalization") as temporary:
            store = AutonomySessionStore(
                Path(temporary),
                trusted_profiles=trusted_profiles(),
                publication_verifier=verify_publication,
            )
            plan = instruction_plan(count=1)
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
            ready = store.status(started["session_id"], at=T1)
            self.assertTrue(ready["finalization_required"])
            completed = store.complete(started["session_id"], at=T1)
            self.assertEqual("completed", completed["state"])
            self.assertFalse(completed["lease_held"])


if __name__ == "__main__":
    unittest.main()
