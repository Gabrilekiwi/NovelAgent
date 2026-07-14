from __future__ import annotations

import multiprocessing
import time
import unittest
import uuid
from pathlib import Path

from core.autonomy.lease import BookLeaseError, BookLeaseStore


AT = "2026-07-14T00:00:00+00:00"


def _compete_for_outline_provider(
    root: str,
    session_id: str,
    start_path: str,
) -> None:
    """Windows-spawn-safe worker used by the real process race below."""

    root_path = Path(root)
    ready = root_path / "ready" / f"{session_id}.marker"
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not Path(start_path).exists():
        if time.monotonic() >= deadline:
            result = "start_timeout"
            break
        time.sleep(0.01)
    else:
        result = ""
    if result:
        result_path = root_path / "results" / f"{session_id}.txt"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(result, encoding="utf-8")
        return
    try:
        BookLeaseStore(root_path).acquire(
            book_id="book-process-race",
            session_id=session_id,
            plan_id=f"plan-{session_id}",
            ttl_seconds=120,
            at=AT,
        )
    except BookLeaseError as exc:
        result = f"blocked:{exc.code}"
    else:
        # This marker represents the first outline-provider boundary.  A
        # process may reach it only after the durable lease is acquired.
        marker = root_path / "provider-boundary" / f"{session_id}.marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("authorized\n", encoding="utf-8")
        result = "provider_entered"
    result_path = root_path / "results" / f"{session_id}.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(result, encoding="utf-8")


class AutonomyMultiprocessLeaseTest(unittest.TestCase):
    def test_two_spawned_processes_allow_exactly_one_outline_provider_entry(self) -> None:
        root = (
            Path.cwd()
            / ".tmp"
            / "test_autonomy_multiprocess"
            / uuid.uuid4().hex
        )
        root.mkdir(parents=True)
        context = multiprocessing.get_context("spawn")
        start_path = root / "start.marker"
        session_ids = ("session-one", "session-two")
        processes = [
            context.Process(
                target=_compete_for_outline_provider,
                args=(str(root), session_id, str(start_path)),
            )
            for session_id in session_ids
        ]
        for process in processes:
            process.start()
        deadline = time.monotonic() + 10
        while len(list((root / "ready").glob("*.marker"))) != len(processes):
            if time.monotonic() >= deadline:
                self.fail("lease contenders did not reach the file barrier")
            time.sleep(0.01)
        start_path.write_text("start\n", encoding="utf-8")
        for process in processes:
            process.join(timeout=20)
            self.assertFalse(process.is_alive(), "lease contender did not terminate")
            self.assertEqual(0, process.exitcode)

        results = [
            (session_id, (root / "results" / f"{session_id}.txt").read_text(encoding="utf-8"))
            for session_id in session_ids
        ]
        states = sorted(state for _, state in results)
        self.assertEqual(1, states.count("provider_entered"), results)
        self.assertEqual(1, sum(state.startswith("blocked:book_lease_held") for state in states), results)
        self.assertEqual(
            1,
            len(list((root / "provider-boundary").glob("*.marker"))),
            results,
        )


if __name__ == "__main__":
    unittest.main()
