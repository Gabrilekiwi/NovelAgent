from __future__ import annotations

import threading
import unittest
from pathlib import Path

from core.autonomy.lease import BookLeaseError, BookLeaseStore
from core.engine.persistence import PersistenceLockError, persistence_run_lock
from tests.test_autonomy_plans import workspace_case


T0 = "2026-07-14T00:00:00+00:00"
T1 = "2026-07-14T00:01:00+00:00"
T2 = "2026-07-14T00:02:00+00:00"


class BookLeaseStoreTest(unittest.TestCase):
    def test_single_writer_renew_cas_release_and_takeover(self) -> None:
        with workspace_case("lease") as temporary:
            store = BookLeaseStore(Path(temporary))
            first = store.acquire(
                book_id="book-lease",
                session_id="session-one",
                plan_id="plan-one",
                ttl_seconds=120,
                at=T0,
            )
            replay = store.acquire(
                book_id="book-lease",
                session_id="session-one",
                plan_id="plan-one",
                ttl_seconds=120,
                at=T1,
            )
            self.assertEqual(first, replay)
            with self.assertRaisesRegex(BookLeaseError, "book_lease_held"):
                store.acquire(
                    book_id="book-lease",
                    session_id="session-two",
                    plan_id="plan-two",
                    ttl_seconds=120,
                    at=T1,
                )
            with self.assertRaisesRegex(BookLeaseError, "book_lease_cas_failed"):
                store.renew(
                    book_id="book-lease",
                    session_id="session-one",
                    plan_id="plan-one",
                    expected_lease_hash="f" * 64,
                    at=T1,
                )
            renewed = store.renew(
                book_id="book-lease",
                session_id="session-one",
                plan_id="plan-one",
                expected_lease_hash=first["lease_hash"],
                ttl_seconds=120,
                at=T1,
            )
            released = store.release(
                book_id="book-lease",
                session_id="session-one",
                plan_id="plan-one",
                expected_lease_hash=renewed["lease_hash"],
                at=T2,
            )
            self.assertEqual("released", released["status"])
            takeover = store.acquire(
                book_id="book-lease",
                session_id="session-two",
                plan_id="plan-two",
                ttl_seconds=120,
                at=T2,
            )
            self.assertEqual("session-two", takeover["session_id"])

    def test_concurrent_acquire_has_exactly_one_winner(self) -> None:
        with workspace_case("lease_race") as temporary:
            root = Path(temporary)
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            outcome_lock = threading.Lock()

            def acquire(session: str) -> None:
                barrier.wait()
                try:
                    BookLeaseStore(root).acquire(
                        book_id="book-race",
                        session_id=session,
                        plan_id=f"plan-{session}",
                        ttl_seconds=120,
                        at=T0,
                    )
                    result = "won"
                except BookLeaseError:
                    result = "lost"
                with outcome_lock:
                    outcomes.append(result)

            threads = [
                threading.Thread(target=acquire, args=("one",)),
                threading.Thread(target=acquire, args=("two",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(["lost", "won"], sorted(outcomes))

    def test_direct_lease_mutation_holds_runtime_remap_fence(self) -> None:
        with workspace_case("lease_remap_fence") as temporary:
            runtime = Path(temporary)
            store = BookLeaseStore(runtime / "autonomy")
            publish_entered = threading.Event()
            allow_publish = threading.Event()
            outcome: list[object] = []
            original_publish = store._publish

            def slow_publish(lease: dict) -> None:
                publish_entered.set()
                if not allow_publish.wait(timeout=5):
                    raise RuntimeError("test publish was not released")
                original_publish(lease)

            store._publish = slow_publish  # type: ignore[method-assign]

            def acquire() -> None:
                try:
                    outcome.append(
                        store.acquire(
                            book_id="book-fenced",
                            session_id="session-fenced",
                            plan_id="plan-fenced",
                            ttl_seconds=120,
                            at=T0,
                        )
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome.append(exc)

            worker = threading.Thread(target=acquire)
            worker.start()
            self.assertTrue(publish_entered.wait(timeout=5))
            with self.assertRaises(PersistenceLockError):
                with persistence_run_lock(runtime / ".root-remap-fence"):
                    pass
            allow_publish.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(outcome))
            self.assertIsInstance(outcome[0], dict)


if __name__ == "__main__":
    unittest.main()
