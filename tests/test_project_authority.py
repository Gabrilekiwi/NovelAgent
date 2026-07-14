from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from core.story_project.authority import (
    AUTHORITY_MODE_EVENT,
    AUTHORITY_MODE_LEGACY,
    AuthorityCASMismatchError,
    AuthorityError,
    AuthorityWriterContractError,
    activate_event_authority,
    assert_authority_writer,
    authority_receipt_path,
    build_authority_genesis_receipt,
    project_identity_sha256,
)
from core.story_project.identity import (
    LEGACY_AUTHORITY_PROJECTION,
    ProjectIdentityError,
    ensure_project_identity,
    load_project_identity,
    project_identity_path,
    validate_project_identity,
)


class ProjectAuthorityTest(unittest.TestCase):
    def _story_project(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_project_authority" / f"{name}_{uuid.uuid4().hex}" / "book"
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _now() -> datetime:
        return datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc)

    def test_v1_golden_bytes_load_with_memory_only_legacy_projection(self) -> None:
        root = self._story_project("golden_v1")
        path = project_identity_path(root)
        path.parent.mkdir(parents=True)
        golden = (
            b'\xef\xbb\xbf{\r\n'
            b'  "book_id": "legacy-book",\r\n'
            b'  "root_hint": "D:\\\\Legacy\\\\Book",\r\n'
            b'  "created_at": "2026-01-01T00:00:00+00:00",\r\n'
            b'  "ephemeral": false,\r\n'
            b'  "activation": null,\r\n'
            b'  "story_state_mode": "shadow",\r\n'
            b'  "schema_version": "1.0"\r\n'
            b'}\r\n'
        )
        path.write_bytes(golden)

        identity = load_project_identity(root)
        ensured = ensure_project_identity(root, now=self._now)

        self.assertIsNotNone(identity)
        self.assertEqual(identity, ensured)
        self.assertEqual(LEGACY_AUTHORITY_PROJECTION, identity.authority)
        self.assertNotIn("authority", identity.to_dict())
        self.assertEqual(golden, path.read_bytes())
        self.assertFalse((root / ".novelagent" / "authority").exists())

    def test_unknown_future_identity_schema_fails_closed(self) -> None:
        payload = {
            "schema_version": "3.0",
            "book_id": "future",
            "created_at": "2026-07-14T00:00:00+00:00",
            "root_hint": ".",
            "story_state_mode": "shadow",
            "activation": None,
            "ephemeral": False,
        }

        with self.assertRaises(ProjectIdentityError):
            validate_project_identity(payload)

    def test_empty_state_genesis_activates_without_shadow_history(self) -> None:
        root = self._story_project("genesis")
        original = ensure_project_identity(root, now=self._now)
        before_sha = project_identity_sha256(root)

        activated = activate_event_authority(
            root,
            expected_identity_sha256=before_sha,
            canonical_state_sha256="a" * 64,
            now=self._now,
        )

        authority = activated.authority
        self.assertEqual("2.0", activated.schema_version)
        self.assertEqual(".", activated.root_hint)
        self.assertEqual(original.book_id, activated.book_id)
        self.assertEqual(AUTHORITY_MODE_EVENT, authority["mode"])
        self.assertEqual(1, authority["authority_epoch"])
        activation = authority["activation_receipt"]
        self.assertEqual(before_sha, activation["expected_identity_sha256"])
        self.assertEqual(authority["head_event_hash"], activation["head_event_hash"])
        self.assertIsNotNone(activation["genesis_receipt_sha256"])
        self.assertTrue(
            authority_receipt_path(root, activation["receipt_sha256"]).is_file()
        )
        self.assertTrue(
            authority_receipt_path(root, activation["genesis_receipt_sha256"]).is_file()
        )
        self.assertEqual(activated, load_project_identity(root))

    def test_activation_cas_mismatch_preserves_original_identity_bytes(self) -> None:
        root = self._story_project("cas")
        ensure_project_identity(root, now=self._now)
        before = project_identity_path(root).read_bytes()

        with self.assertRaises(AuthorityCASMismatchError):
            activate_event_authority(
                root,
                expected_identity_sha256="0" * 64,
                canonical_state_sha256="a" * 64,
                now=self._now,
            )

        self.assertEqual(before, project_identity_path(root).read_bytes())
        receipt_dir = root / ".novelagent" / "authority" / "receipts"
        self.assertFalse(receipt_dir.exists())

    def test_receipt_publication_failure_leaves_v2_fail_closed_not_legacy(self) -> None:
        root = self._story_project("receipt_failure")
        ensure_project_identity(root, now=self._now)

        with patch(
            "core.story_project.authority.atomic_create_json",
            side_effect=OSError("injected receipt failure"),
        ), self.assertRaisesRegex(AuthorityError, "receipt_publish_failed"):
            activate_event_authority(
                root,
                expected_identity_sha256=project_identity_sha256(root),
                canonical_state_sha256="9" * 64,
                now=self._now,
            )

        raw = json.loads(project_identity_path(root).read_text(encoding="utf-8"))
        identity = validate_project_identity(raw)
        self.assertEqual("2.0", identity.schema_version)
        self.assertEqual(AUTHORITY_MODE_EVENT, identity.authority["mode"])
        with self.assertRaisesRegex(AuthorityWriterContractError, "legacy_writer_forbidden"):
            assert_authority_writer(
                identity,
                writer_mode=AUTHORITY_MODE_LEGACY,
                writer_contract=1,
            )
        with self.assertRaises(AuthorityError):
            load_project_identity(root)

    def test_activation_and_persisted_receipt_tampering_fail_closed(self) -> None:
        root = self._story_project("tamper")
        ensure_project_identity(root, now=self._now)
        activated = activate_event_authority(
            root,
            expected_identity_sha256=project_identity_sha256(root),
            head_event_hash="b" * 64,
            now=self._now,
        )
        activation = activated.authority["activation_receipt"]
        receipt_path = authority_receipt_path(root, activation["receipt_sha256"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["head_event_hash"] = "c" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        with self.assertRaisesRegex(AuthorityError, "hash_mismatch"):
            load_project_identity(root)

        identity_payload = activated.to_dict()
        identity_payload["authority"]["activation_receipt"]["authority_epoch"] = 2
        with self.assertRaises(AuthorityError):
            validate_project_identity(identity_payload)

    def test_epoch_head_and_writer_contract_are_fail_closed(self) -> None:
        root = self._story_project("writer")
        ensure_project_identity(root, now=self._now)

        with self.assertRaises(AuthorityWriterContractError):
            activate_event_authority(
                root,
                expected_identity_sha256=project_identity_sha256(root),
                head_event_hash="d" * 64,
                minimum_writer_contract=2,
                writer_contract=1,
                now=self._now,
            )

        activated = activate_event_authority(
            root,
            expected_identity_sha256=project_identity_sha256(root),
            head_event_hash="d" * 64,
            minimum_writer_contract=2,
            writer_contract=2,
            now=self._now,
        )
        assert_authority_writer(
            activated,
            writer_mode=AUTHORITY_MODE_EVENT,
            writer_contract=2,
            expected_authority_epoch=1,
            expected_head_event_hash="d" * 64,
        )
        with self.assertRaises(AuthorityWriterContractError):
            assert_authority_writer(
                activated,
                writer_mode=AUTHORITY_MODE_EVENT,
                writer_contract=1,
            )
        with self.assertRaisesRegex(
            AuthorityWriterContractError,
            "legacy_writer_forbidden_after_event_activation",
        ):
            assert_authority_writer(
                activated,
                writer_mode=AUTHORITY_MODE_LEGACY,
                writer_contract=2,
            )
        with self.assertRaises(AuthorityCASMismatchError):
            assert_authority_writer(
                activated,
                writer_mode=AUTHORITY_MODE_EVENT,
                writer_contract=2,
                expected_authority_epoch=2,
            )
        with self.assertRaises(AuthorityCASMismatchError):
            assert_authority_writer(
                activated,
                writer_mode=AUTHORITY_MODE_EVENT,
                writer_contract=2,
                expected_head_event_hash="e" * 64,
            )

    def test_first_event_authority_receipt_irreversibly_blocks_legacy_activation(self) -> None:
        root = self._story_project("no_downgrade")
        ensure_project_identity(root, now=self._now)
        activated = activate_event_authority(
            root,
            expected_identity_sha256=project_identity_sha256(root),
            head_event_hash="f" * 64,
            now=self._now,
        )
        before = project_identity_path(root).read_bytes()

        with self.assertRaisesRegex(
            AuthorityWriterContractError,
            "legacy_writer_forbidden_after_event_activation",
        ):
            activate_event_authority(
                root,
                expected_identity_sha256=hashlib.sha256(before).hexdigest(),
                head_event_hash="f" * 64,
                now=self._now,
            )

        self.assertEqual(activated, load_project_identity(root))
        self.assertEqual(before, project_identity_path(root).read_bytes())

    def test_v2_rejects_epoch_head_receipt_mismatch_and_unsafe_public_fields(self) -> None:
        root = self._story_project("mismatch")
        ensure_project_identity(root, now=self._now)
        activated = activate_event_authority(
            root,
            expected_identity_sha256=project_identity_sha256(root),
            head_event_hash="1" * 64,
            now=self._now,
        )

        for field, value in (
            ("authority_epoch", 2),
            ("minimum_writer_contract", 2),
        ):
            with self.subTest(field=field):
                payload = activated.to_dict()
                payload["authority"][field] = value
                with self.assertRaises(AuthorityError):
                    validate_project_identity(payload)

        # The current head is allowed to advance while the immutable
        # activation receipt continues to identify the initial event head.
        advanced = activated.to_dict()
        advanced["authority"]["head_event_hash"] = "2" * 64
        self.assertEqual(
            "2" * 64,
            validate_project_identity(advanced).authority["head_event_hash"],
        )

        unsafe = replace(activated, root_hint=str(root.resolve()))
        with self.assertRaises(ProjectIdentityError):
            validate_project_identity(unsafe.to_dict())
        with self.assertRaises(AuthorityError):
            build_authority_genesis_receipt(
                book_id=r"C:\private\book",
                canonical_state_sha256="3" * 64,
                now=self._now,
            )


if __name__ == "__main__":
    unittest.main()
