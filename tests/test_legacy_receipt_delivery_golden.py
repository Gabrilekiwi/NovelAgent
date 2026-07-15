from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.delivery import load_delivery_job
from core.engine.persistence_v2 import validate_publication_receipt


LEGACY_PUBLICATION_RECEIPT_BYTES = (
    '{"apply_targets":[],"artifact_bundle_digest":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
    '"artifacts":[],"book_id":"legacy-book-1","candidate_digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
    '"canonical_json_algorithm":"novelagent-canonical-json-v1","context_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"delivery_jobs":[],"final_run":{"kind":"final_run_record","path_ref":{"relative_path":"runs/legacy-run-1.json",'
    '"root_id":"runtime"},"sha256":"1111111111111111111111111111111111111111111111111111111111111111","size":2,"target_id":"final"},'
    '"generation_input_context_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    '"manifest":{"path_ref":{"relative_path":"persistence/journals/legacy-run-1/manifest.json","root_id":"runtime"},'
    '"sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},'
    '"marker":{"path_ref":{"relative_path":"persistence/markers/legacy-run-1.json","root_id":"runtime"},'
    '"sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},'
    '"published_at":"2026-01-01T00:00:00+00:00","receipt_hash":"c8bf82b3749d43c2989e689046d2248f0221d07b79e0467e4646ed60e0c29fbd",'
    '"receipt_id":"legacy-receipt-1","receipt_path_ref":{"relative_path":"receipts/legacy.json","root_id":"runtime"},'
    '"run_id":"legacy-run-1","schema_version":"2.0","story_project_source_revision_after":{"revision":1}}\n'
).encode("utf-8")

LEGACY_DELIVERY_JOB_BYTES = (
    '{"attempt_count":0,"book_id":"legacy-book-1","confirmed_absent_at":null,"created_at":"2026-01-01T00:00:00+00:00",'
    '"job_id":"legacy-job-1","last_attempt_receipt":null,"lease":null,'
    '"operation_id":"novelagent:61acfd2da78b12b0a55eccc1411a5f646e70e54b62d273eff61c817ba15bb3bc",'
    '"payload":{},"payload_hash":"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",'
    '"policy":"not_required","publication_receipt_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"run_id":"legacy-run-1","schema_version":"1.0","state":"not_required","target":{},"target_type":"none",'
    '"uncertain_since":null,"updated_at":"2026-01-01T00:00:00+00:00"}\n'
).encode("utf-8")


class LegacyReceiptDeliveryGoldenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path.cwd()
            / ".tmp"
            / "test_legacy_receipt_delivery_golden"
            / uuid.uuid4().hex
        )
        self.root.mkdir(parents=True)

    def test_publication_receipt_v20_validates_without_rewrite(self) -> None:
        path = self.root / "publication-receipt-v2.0.json"
        path.write_bytes(LEGACY_PUBLICATION_RECEIPT_BYTES)
        before = path.read_bytes()

        validated = validate_publication_receipt(
            json.loads(before.decode("utf-8"))
        )

        self.assertEqual("2.0", validated["schema_version"])
        self.assertEqual(
            "c8bf82b3749d43c2989e689046d2248f0221d07b79e0467e4646ed60e0c29fbd",
            validated["receipt_hash"],
        )
        self.assertEqual(
            "0b0100abda4e217640a506f2567d555acf7f8abf1e5ac1cc633bfe6c4807f03f",
            hashlib.sha256(before).hexdigest(),
        )
        self.assertEqual(before, path.read_bytes())

    def test_delivery_job_v10_loads_without_rewrite(self) -> None:
        path = self.root / "delivery-job-v1.0.json"
        path.write_bytes(LEGACY_DELIVERY_JOB_BYTES)
        before = path.read_bytes()

        loaded = load_delivery_job(path)

        self.assertEqual("1.0", loaded["schema_version"])
        self.assertEqual("not_required", loaded["state"])
        self.assertEqual(
            "f343640a76ad2b86d8be25c56676150666c7dc0cd8dc24e435e4d75a3b01e55f",
            hashlib.sha256(before).hexdigest(),
        )
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
