from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.delivery import DeliveryQueue
from core.quality_decision import build_quality_decision
from core.readiness import (
    ReadinessError,
    ReadinessService,
    assert_provider_consumes_readiness_context,
    derive_readiness_decision,
)


class ReadinessTest(unittest.TestCase):
    def _queue(self) -> DeliveryQueue:
        root = Path.cwd() / ".tmp" / "test_readiness" / uuid.uuid4().hex
        root.mkdir(parents=True)
        return DeliveryQueue(root)

    @staticmethod
    def _job(queue: DeliveryQueue, job_id: str, *, policy: str = "required") -> dict:
        job = queue.enqueue(
            job_id=job_id,
            book_id="book-1",
            run_id="run-1",
            publication_receipt_hash="a" * 64,
            target_type="file",
            target={"path_ref": {"root_id": "delivery_store", "relative_path": f"{job_id}.md"}},
            payload={"content": "x"},
            policy=policy,
        )
        return job

    @staticmethod
    def _preflight(
        digest: str,
        *,
        book_id: str = "book-1",
        next_chapter: int = 2,
        conflicts: list[str] | None = None,
    ) -> dict:
        blocking_conflicts = list(conflicts or [])
        checks = {
            "unique_next_chapter_outline": True,
            "current_story_project_sources": True,
            "project_identity_matches": True,
            "parser_qualified": True,
            "blocking_conflicts_absent": not blocking_conflicts,
        }
        return {
            "schema_version": "1.0",
            "valid": all(checks.values()) and not blocking_conflicts,
            "book_id": book_id,
            "next_chapter": next_chapter,
            "next_step_context_digest": digest,
            "checks": checks,
            "blocking_conflicts": blocking_conflicts,
        }

    @staticmethod
    def _quality_decision() -> dict:
        return build_quality_decision(
            policy="minimal",
            validation={
                "ok": True,
                "requested_focus": ["continuity"],
                "executed_checks": ["continuity"],
                "skipped_checks": [],
                "problems": [],
            },
        )

    def test_pure_decision_requires_only_required_jobs_and_stable_context(self) -> None:
        queue = self._queue()
        required = self._job(queue, "required")
        best_effort = self._job(queue, "best-effort", policy="best_effort")
        best_effort["state"] = "permanent_failed"
        digest = "b" * 64

        blocked = derive_readiness_decision(
            accepted=True,
            committed=True,
            book_id="book-1",
            run_id="run-1",
            expected_book_id="book-1",
            delivery_jobs=[required, best_effort],
            next_chapter=2,
            next_step_context_preflight=self._preflight(digest),
            current_context_digest=digest,
        )
        required["state"] = "succeeded"
        ready = derive_readiness_decision(
            accepted=True,
            committed=True,
            book_id="book-1",
            run_id="run-1",
            expected_book_id="book-1",
            delivery_jobs=[required, best_effort],
            next_chapter=2,
            next_step_context_preflight=self._preflight(digest),
            current_context_digest=digest,
        )

        self.assertFalse(blocked["ok"])
        self.assertIn("required_delivery_not_succeeded:pending", blocked["reasons"])
        self.assertTrue(ready["ok"])

    def test_decision_reports_receipt_identity_preflight_and_read_set_failures(self) -> None:
        digest = "b" * 64
        decision = derive_readiness_decision(
            accepted=False,
            committed=False,
            book_id="book-other",
            run_id="run-1",
            expected_book_id="book-1",
            delivery_jobs=[],
            next_chapter=2,
            next_step_context_preflight=self._preflight(
                digest,
                book_id="book-other",
                conflicts=["outline_conflict"],
            ),
            current_context_digest="c" * 64,
        )

        self.assertEqual(
            [
                "quality_not_accepted",
                "local_commit_not_verified",
                "project_identity_mismatch",
                "next_step_context_invalid",
                "next_step_context_drift",
            ],
            decision["reasons"],
        )

    def test_service_collects_receipt_and_queue_evidence(self) -> None:
        queue = self._queue()
        job = self._job(queue, "required")
        job["state"] = "succeeded"
        # Persist the state through the queue's job file for service collection.
        path = queue.jobs_dir / "required.json"
        from core.engine.persistence import atomic_write_json

        atomic_write_json(path, job)
        digest = "d" * 64
        service = ReadinessService(
            delivery_queue=queue,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with patch(
            "core.readiness.verify_publication_receipt",
            return_value={
                "valid": True,
                "committed": True,
                "book_id": "book-1",
                "run_id": "run-1",
                "receipt_hash": "a" * 64,
                "delivery_jobs": [
                    {
                        "id": "required",
                        "payload_hash": job["payload_hash"],
                        "policy": {"required": True, "target": "file"},
                    }
                ],
            },
        ):
            decision = service.evaluate(
                quality_decision=self._quality_decision(),
                publication_receipt={},
                root_map={},
                expected_book_id="book-1",
                next_chapter=2,
                next_step_context_preflight=self._preflight(digest),
                current_context_digest=digest,
            )

        self.assertTrue(decision["ok"])
        self.assertEqual("2026-01-01T00:00:00+00:00", decision["checked_at"])
        self.assertEqual("required", decision["evidence"]["required_delivery_jobs"][0]["job_id"])

    def test_receipt_delivery_binding_cannot_be_vacuously_ready(self) -> None:
        digest = "e" * 64
        decision = derive_readiness_decision(
            accepted=True,
            committed=True,
            book_id="book-1",
            run_id="run-1",
            expected_book_id="book-1",
            delivery_jobs=[],
            next_chapter=2,
            next_step_context_preflight=self._preflight(digest),
            current_context_digest=digest,
            receipt_evidence={
                "receipt_hash": "a" * 64,
                "delivery_jobs": [
                    {
                        "id": "required",
                        "payload_hash": "f" * 64,
                        "policy": {"required": True, "target": "file"},
                    }
                ],
            },
        )

        self.assertFalse(decision["ok"])
        self.assertIn("receipt_delivery_job_missing", decision["reasons"])

    def test_provider_must_consume_exact_readiness_digest(self) -> None:
        digest = "f" * 64
        decision = derive_readiness_decision(
            accepted=True,
            committed=True,
            book_id="book-1",
            run_id="run-1",
            expected_book_id="book-1",
            delivery_jobs=[],
            next_chapter=2,
            next_step_context_preflight=self._preflight(digest),
            current_context_digest=digest,
        )

        assert_provider_consumes_readiness_context(decision, actual_context_digest=digest)
        with self.assertRaisesRegex(ReadinessError, "drifted"):
            assert_provider_consumes_readiness_context(decision, actual_context_digest="0" * 64)


if __name__ == "__main__":
    unittest.main()
