from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.delivery import (
    DeliveryConflictError,
    DeliveryError,
    DeliveryQueue,
    FileDeliveryAdapter,
    NotionDeliveryAdapter,
    SafeFileDeliveryAdapter,
    default_notion_property_map,
    delivery_outcome,
    delivery_outcome_from_legacy,
    load_delivery_attempt_receipt,
    notion_delivery_properties,
    validate_notion_delivery_schema,
)
from core.engine.persistence import atomic_write_json
from core.engine.safe_paths import RootBinding
from core.path_refs import path_ref_for


class SimulatedCrash(BaseException):
    pass


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingAdapter:
    def __init__(self, outcome=None, *, crash_after_mutation: bool = False) -> None:
        self.outcome = outcome or delivery_outcome("succeeded", code="ok", message="done")
        self.crash_after_mutation = crash_after_mutation
        self.calls = []

    def deliver(self, job, context):
        self.calls.append({"job": job, "query_only": context.query_only})
        if self.crash_after_mutation:
            context.mark_remote_mutation_started()
            raise SimulatedCrash("worker died after POST boundary")
        return self.outcome


class DeliveryQueueTest(unittest.TestCase):
    def _case(self, name: str) -> dict:
        root = Path.cwd() / ".tmp" / "test_delivery_queue" / f"{name}_{uuid.uuid4().hex}"
        runtime = root / "runtime"
        export = root / "export"
        runtime.mkdir(parents=True)
        export.mkdir()
        return {
            "root": root,
            "runtime": runtime,
            "export": export,
            "queue_root": runtime / "deliveries",
            "root_map": {"delivery_store": export, "runtime": runtime},
        }

    @staticmethod
    def _receipt_hash() -> str:
        return "a" * 64

    def _file_job(self, queue: DeliveryQueue, case: dict, *, job_id: str = "job-1", content: str = "chapter\n", policy=None):
        return queue.enqueue(
            job_id=job_id,
            book_id="book-1",
            run_id="run-1",
            publication_receipt_hash=self._receipt_hash(),
            target_type="file",
            target={
                "path_ref": path_ref_for(
                    case["export"] / f"{job_id}.md",
                    root_id="delivery_store",
                    root=case["export"],
                ).to_dict()
            },
            payload={"content": content, "encoding": "utf-8"},
            policy=policy,
        )

    def _notion_job(self, queue: DeliveryQueue, *, job_id: str = "notion-1", policy=None):
        return queue.enqueue(
            job_id=job_id,
            book_id="book-1",
            run_id="run-1",
            publication_receipt_hash=self._receipt_hash(),
            target_type="notion",
            target={"database_id": "db", "quarantine_seconds": 60},
            payload={"id": "memory-1", "type": "world_state", "name": "World", "data": {"level": 2}},
            policy=policy,
        )

    @staticmethod
    def _notion_schema() -> dict:
        return {
            "properties": {
                "Operation ID": {"type": "rich_text"},
                "Memory ID": {"type": "rich_text"},
                "Payload Hash": {"type": "rich_text"},
                "Type": {"type": "select"},
                "Name": {"type": "title"},
                "Data": {"type": "rich_text"},
            }
        }

    @staticmethod
    def _notion_page(job: dict, page_id: str = "page-1", *, payload_hash: str | None = None) -> dict:
        properties = notion_delivery_properties(job)
        if payload_hash is not None:
            properties["Payload Hash"] = {"rich_text": [{"text": {"content": payload_hash}}]}
        return {"id": page_id, "url": f"https://notion.test/{page_id}", "properties": properties}

    def test_file_delivery_defaults_required_and_writes_attempt_receipt_without_payload(self) -> None:
        case = self._case("file")
        queue = DeliveryQueue(case["queue_root"])
        enqueued = self._file_job(queue, case)

        result = queue.attempt(
            "job-1",
            worker_id="worker-1",
            adapter=FileDeliveryAdapter(root_map=case["root_map"]),
        )

        self.assertEqual("required", enqueued["policy"])
        self.assertEqual("succeeded", result["state"])
        self.assertEqual("chapter\n", (case["export"] / "job-1.md").read_text(encoding="utf-8"))
        receipt_path = next((case["queue_root"] / "attempts" / "job-1").glob("*.json"))
        receipt = load_delivery_attempt_receipt(receipt_path)
        serialized = json.dumps(receipt, ensure_ascii=False).lower()
        self.assertNotIn("chapter\\n", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertEqual(self._receipt_hash(), receipt["publication_receipt_hash"])

    def test_safe_file_delivery_rejects_mismatched_operator_root_uuid(self) -> None:
        case = self._case("safe-root-uuid")
        queue = DeliveryQueue(case["queue_root"])
        root_id = "external:required-export"
        expected_uuid = "11111111-1111-4111-8111-111111111111"
        wrong_uuid = "22222222-2222-4222-8222-222222222222"
        target_path = case["export"] / "chapter.md"
        queue.enqueue(
            job_id="safe-job",
            book_id="book-1",
            run_id="run-1",
            publication_receipt_hash=self._receipt_hash(),
            target_type="file",
            target={
                "path_ref": path_ref_for(
                    target_path,
                    root_id=root_id,
                    root=case["export"],
                    root_uuid=wrong_uuid,
                ).to_dict()
            },
            payload={"content": "chapter\n", "encoding": "utf-8"},
        )

        result = queue.attempt(
            "safe-job",
            worker_id="worker-1",
            adapter=SafeFileDeliveryAdapter(
                binding=RootBinding(
                    root_id=root_id,
                    root_uuid=expected_uuid,
                    path=case["export"],
                )
            ),
        )

        self.assertEqual("retryable_failed", result["state"])
        self.assertFalse(target_path.exists())
        receipt_path = next(
            (case["queue_root"] / "attempts" / "safe-job").glob("*.json")
        )
        receipt = load_delivery_attempt_receipt(receipt_path)
        self.assertIn("PathRef root UUID mismatch", receipt["outcome"]["message"])

    def test_enqueue_is_idempotent_and_payload_change_conflicts(self) -> None:
        case = self._case("enqueue")
        queue = DeliveryQueue(case["queue_root"])
        first = self._file_job(queue, case)
        second = self._file_job(queue, case)

        self.assertEqual(first, second)
        with self.assertRaises(DeliveryConflictError):
            self._file_job(queue, case, content="different")
        self.assertEqual("conflict", queue.load("job-1")["state"])

    def test_single_valid_lease_blocks_second_worker(self) -> None:
        case = self._case("lease")
        clock = FakeClock()
        queue = DeliveryQueue(case["queue_root"], clock=clock)
        self._file_job(queue, case)
        queue._claim("job-1", "worker-1")
        adapter = RecordingAdapter()

        job = queue.attempt("job-1", worker_id="worker-2", adapter=adapter)

        self.assertEqual("delivering", job["state"])
        self.assertEqual([], adapter.calls)
        self.assertEqual("worker-1", job["lease"]["worker_id"])

    def test_stale_posting_lease_becomes_query_only_uncertain(self) -> None:
        case = self._case("stale")
        clock = FakeClock()
        queue = DeliveryQueue(case["queue_root"], clock=clock, lease_seconds=10)
        self._file_job(queue, case)
        crashing = RecordingAdapter(crash_after_mutation=True)
        with self.assertRaises(SimulatedCrash):
            queue.attempt("job-1", worker_id="worker-1", adapter=crashing)
        clock.advance(11)
        query_adapter = RecordingAdapter(
            delivery_outcome("uncertain", code="still_absent", message="query only")
        )

        job = queue.attempt("job-1", worker_id="worker-2", adapter=query_adapter)

        self.assertEqual("uncertain", job["state"])
        self.assertTrue(query_adapter.calls[0]["query_only"])
        self.assertEqual(2, job["attempt_count"])

    def test_attempt_receipt_recovers_job_without_repeating_adapter(self) -> None:
        case = self._case("receipt_recover")

        def crash(event, _attempt_id, _path):
            if event == "after_attempt_receipt":
                raise SimulatedCrash("crash after durable attempt receipt")

        queue = DeliveryQueue(case["queue_root"], fault_injector=crash)
        self._file_job(queue, case)
        adapter = RecordingAdapter()
        with self.assertRaises(SimulatedCrash):
            queue.attempt("job-1", worker_id="worker-1", adapter=adapter)
        recovering = DeliveryQueue(case["queue_root"])
        never_called = RecordingAdapter()

        job = recovering.attempt("job-1", worker_id="worker-2", adapter=never_called)

        self.assertEqual("succeeded", job["state"])
        self.assertEqual([], never_called.calls)

    def test_notion_existing_page_query_paginates_and_skips_post(self) -> None:
        case = self._case("notion_existing")
        queue = DeliveryQueue(case["queue_root"])
        job = self._notion_job(queue)
        calls = []

        def transport(url, headers, body):
            del headers
            calls.append((url, dict(body)))
            if url.endswith("/query") and "start_cursor" not in body:
                return {"results": [], "has_more": True, "next_cursor": "next"}
            if url.endswith("/query"):
                return {"results": [self._notion_page(job)], "has_more": False}
            self.fail("POST must not run when paginated query finds the page")

        result = queue.attempt(
            job["job_id"],
            worker_id="worker-1",
            adapter=NotionDeliveryAdapter(
                database_id="db",
                api_key="secret",
                database_schema=self._notion_schema(),
                transport=transport,
            ),
        )

        self.assertEqual("succeeded", result["state"])
        self.assertEqual("next", calls[1][1]["start_cursor"])
        self.assertEqual(2, len(calls))

    def test_notion_duplicate_or_payload_mismatch_is_conflict(self) -> None:
        for pages, code in (("duplicate", "notion_duplicate_pages"), ("mismatch", "notion_payload_conflict")):
            with self.subTest(case=pages):
                case = self._case(pages)
                queue = DeliveryQueue(case["queue_root"])
                job = self._notion_job(queue)
                results = (
                    [self._notion_page(job, "p1"), self._notion_page(job, "p2")]
                    if pages == "duplicate"
                    else [self._notion_page(job, payload_hash="0" * 64)]
                )

                def transport(url, headers, body):
                    del url, headers, body
                    return {"results": results, "has_more": False}

                result = queue.attempt(
                    job["job_id"],
                    worker_id="worker-1",
                    adapter=NotionDeliveryAdapter(
                        database_id="db",
                        api_key="secret",
                        database_schema=self._notion_schema(),
                        transport=transport,
                    ),
                )

                self.assertEqual("conflict", result["state"])
                receipt = next((case["queue_root"] / "attempts" / job["job_id"]).glob("*.json"))
                self.assertEqual(code, load_delivery_attempt_receipt(receipt)["outcome"]["code"])

    def test_notion_post_timeout_becomes_uncertain_and_never_auto_reposts(self) -> None:
        case = self._case("notion_uncertain")
        clock = FakeClock()
        queue = DeliveryQueue(case["queue_root"], clock=clock)
        job = self._notion_job(queue)
        post_calls = 0

        def transport(url, headers, body):
            nonlocal post_calls
            del headers, body
            if url.endswith("/query"):
                return {"results": [], "has_more": False}
            post_calls += 1
            raise TimeoutError("response lost")

        adapter = NotionDeliveryAdapter(
            database_id="db",
            api_key="secret",
            database_schema=self._notion_schema(),
            transport=transport,
        )
        first = queue.attempt(job["job_id"], worker_id="worker-1", adapter=adapter)
        second = queue.attempt(job["job_id"], worker_id="worker-2", adapter=adapter)

        self.assertEqual("uncertain", first["state"])
        self.assertEqual("uncertain", second["state"])
        self.assertEqual(1, post_calls)

    def test_notion_delayed_readback_succeeds_on_query_only_reconcile(self) -> None:
        case = self._case("notion_delayed")
        queue = DeliveryQueue(case["queue_root"])
        job = self._notion_job(queue)
        query_count = 0

        def transport(url, headers, body):
            nonlocal query_count
            del headers, body
            if url.endswith("/query"):
                query_count += 1
                if query_count < 3:
                    return {"results": [], "has_more": False}
                return {"results": [self._notion_page(job)], "has_more": False}
            return {"id": "created-page"}

        adapter = NotionDeliveryAdapter(
            database_id="db",
            api_key="secret",
            database_schema=self._notion_schema(),
            transport=transport,
        )
        first = queue.attempt(job["job_id"], worker_id="worker-1", adapter=adapter)
        second = queue.attempt(job["job_id"], worker_id="worker-2", adapter=adapter)

        self.assertEqual("uncertain", first["state"])
        self.assertEqual("succeeded", second["state"])

    def test_confirmed_absent_requires_quarantine_and_second_query(self) -> None:
        case = self._case("confirmed_absent")
        clock = FakeClock()
        queue = DeliveryQueue(case["queue_root"], clock=clock)
        job = self._notion_job(queue)
        uncertain = RecordingAdapter(
            delivery_outcome(
                "uncertain",
                code="notion_absent_during_uncertain_reconcile",
                message="not visible after a complete query",
            )
        )
        queue.attempt(job["job_id"], worker_id="worker-1", adapter=uncertain)

        with self.assertRaisesRegex(DeliveryError, "quarantine"):
            queue.resolve_confirmed_absent(job["job_id"], worker_id="human", adapter=uncertain)
        clock.advance(61)
        resolved = queue.resolve_confirmed_absent(job["job_id"], worker_id="human", adapter=uncertain)

        self.assertEqual("pending", resolved["state"])
        self.assertIsNotNone(resolved["confirmed_absent_at"])
        self.assertTrue(uncertain.calls[-1]["query_only"])

    def test_confirmed_absent_does_not_reset_pending_after_query_failure(self) -> None:
        case = self._case("confirmed-absent-query-failed")
        clock = FakeClock()
        queue = DeliveryQueue(case["queue_root"], clock=clock)
        job = self._notion_job(queue)
        first = RecordingAdapter(delivery_outcome("uncertain", code="post_timeout", message="unknown"))
        queue.attempt(job["job_id"], worker_id="worker-1", adapter=first)
        clock.advance(61)
        query_failed = RecordingAdapter(
            delivery_outcome("uncertain", code="notion_query_failed", message="network unavailable")
        )

        with self.assertRaisesRegex(DeliveryError, "fully paginated query"):
            queue.resolve_confirmed_absent(
                job["job_id"],
                worker_id="human",
                adapter=query_failed,
            )

        self.assertEqual("uncertain", queue.load(job["job_id"])["state"])

    def test_required_notion_schema_preflight_fails_closed(self) -> None:
        case = self._case("schema")
        queue = DeliveryQueue(case["queue_root"])
        job = self._notion_job(queue)
        called = False

        def transport(*args):
            nonlocal called
            called = True
            return {}

        result = queue.attempt(
            job["job_id"],
            worker_id="worker-1",
            adapter=NotionDeliveryAdapter(
                database_id="db",
                api_key="secret",
                database_schema={"properties": {}},
                transport=transport,
            ),
        )

        self.assertEqual("permanent_failed", result["state"])
        self.assertFalse(called)
        with self.assertRaises(DeliveryError):
            validate_notion_delivery_schema({"properties": {}})

    def test_missing_notion_schema_is_retryable_without_remote_call(self) -> None:
        case = self._case("schema-unavailable")
        queue = DeliveryQueue(case["queue_root"])
        job = self._notion_job(queue)
        called = False

        def transport(*args):
            nonlocal called
            called = True
            return {}

        result = queue.attempt(
            job["job_id"],
            worker_id="worker-1",
            adapter=NotionDeliveryAdapter(
                database_id="db",
                api_key="secret",
                database_schema=None,
                transport=transport,
            ),
        )

        self.assertEqual("retryable_failed", result["state"])
        self.assertFalse(called)

    def test_notion_property_limit_fails_before_remote_mutation(self) -> None:
        case = self._case("property-limit")
        queue = DeliveryQueue(case["queue_root"])
        job = queue.enqueue(
            job_id="notion-long",
            book_id="book-1",
            run_id="run-1",
            publication_receipt_hash=self._receipt_hash(),
            target_type="notion",
            target={"database_id": "db"},
            payload={"id": "memory-1", "name": "World", "data": {"text": "x" * 2100}},
        )
        called = False

        def transport(*args):
            nonlocal called
            called = True
            return {}

        result = queue.attempt(
            job["job_id"],
            worker_id="worker-1",
            adapter=NotionDeliveryAdapter(
                database_id="db",
                api_key="secret",
                database_schema=self._notion_schema(),
                transport=transport,
            ),
        )

        self.assertEqual("permanent_failed", result["state"])
        self.assertFalse(called)

    def test_delivery_job_never_persists_target_credentials(self) -> None:
        case = self._case("target-secret")
        queue = DeliveryQueue(case["queue_root"])

        with self.assertRaisesRegex(DeliveryError, "credentials"):
            queue.enqueue(
                job_id="notion-secret",
                book_id="book-1",
                run_id="run-1",
                publication_receipt_hash=self._receipt_hash(),
                target_type="notion",
                target={"database_id": "db", "api_key": "must-not-persist"},
                payload={"id": "memory-1"},
            )

    def test_policy_and_legacy_outcome_mapping(self) -> None:
        case = self._case("policy")
        queue = DeliveryQueue(case["queue_root"])
        best_effort = self._file_job(queue, case, job_id="best", policy="best_effort")
        no_delivery = queue.enqueue(
            job_id="none",
            book_id="book-1",
            run_id="run-1",
            publication_receipt_hash=self._receipt_hash(),
            target_type="none",
            target={},
            payload={},
            policy="not_required",
        )

        self.assertEqual("best_effort", best_effort["policy"])
        self.assertEqual("not_required", no_delivery["state"])
        self.assertEqual(
            "uncertain",
            delivery_outcome_from_legacy(
                {"target": "notion", "written": 1, "verification": {"status": "readback_failed"}}
            )["state"],
        )

    def test_reconcile_summary_includes_terminal_required_failures(self) -> None:
        case = self._case("summary-terminal")
        queue = DeliveryQueue(case["queue_root"])
        failed = self._file_job(queue, case, job_id="failed")
        failed["state"] = "permanent_failed"
        atomic_write_json(queue.jobs_dir / "failed.json", failed)
        self._file_job(queue, case, job_id="pending")

        result = queue.reconcile(
            adapters={"file": FileDeliveryAdapter(root_map=case["root_map"])},
            worker_id="worker",
            run_id="run-1",
        )

        self.assertEqual(1, result["attempted"])
        self.assertFalse(result["required_succeeded"])


if __name__ == "__main__":
    unittest.main()
