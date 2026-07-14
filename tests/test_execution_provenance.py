from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import unittest
import uuid

from core.execution_provenance import (
    ExecutionProvenanceError,
    UnsafeProvenanceError,
    build_execution_provenance,
    canonical_provenance_hash,
    canonical_provenance_json_bytes,
    capture_execution_provenance,
    validate_execution_provenance,
)
from core.schema import validate_schema, validate_schema_keywords


class ExecutionProvenanceTest(unittest.TestCase):
    def _writable_temp_root(self):
        base = Path(".tmp")
        base.mkdir(exist_ok=True)
        root = base / f"execution_provenance_{uuid.uuid4().hex}"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def _build(self, **overrides: object):
        values: dict[str, object] = {
            "code_bundle_hash": "a" * 64,
            "code_file_count": 4,
            "git_commit": "b" * 40,
            "git_dirty": False,
            "prompt_hashes": {
                "prompts/chapter_prompt.md": "c" * 64,
                "prompts/repair_prompt.md": "d" * 64,
            },
            "schema_hashes": {
                "schemas/chapter_pipeline.schema.json": "e" * 64,
                "schemas/run_record.schema.json": "f" * 64,
            },
            "python_version": "3.12.4",
            "python_implementation": "CPython",
            "dependency_versions": {
                "anthropic": "0.40.0",
                "openai": "1.55.0",
            },
            "provider": "openai",
            "model": "gpt-test",
            "config": {
                "max_output_tokens": 4096,
                "temperature": 0.2,
            },
            "feature_flags": {
                "memory_v2": True,
                "review_gate": False,
            },
        }
        values.update(overrides)
        return build_execution_provenance(**values)  # type: ignore[arg-type]

    def test_record_is_versioned_schema_valid_and_hash_verified(self) -> None:
        provenance = self._build()
        record = provenance.to_dict()

        self.assertIs(record, validate_schema(record, "execution_provenance.schema.json"))
        self.assertEqual(record, validate_execution_provenance(record))
        self.assertEqual(record["provenance_hash"], canonical_provenance_hash(record))
        self.assertEqual(
            canonical_provenance_json_bytes(record),
            provenance.canonical_json_bytes(),
        )
        self.assertEqual("1.0", record["schema_version"])
        self.assertEqual("sha256", record["hash_algorithm"])
        self.assertEqual(64, len(record["provenance_hash"]))

    def test_schema_uses_only_supported_runtime_keywords(self) -> None:
        schema_path = Path("schemas/execution_provenance.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIs(schema, validate_schema_keywords(schema, schema_path.name))

    def test_canonical_json_and_hash_are_stable_across_mapping_order(self) -> None:
        first = self._build(
            prompt_hashes={
                "prompts/z.md": "1" * 64,
                "prompts/a.md": "2" * 64,
            },
            dependency_versions={"z-package": "2", "a-package": "1"},
            config={
                "zeta": {"second": 2, "first": 1},
                "alpha": [3, 2, 1],
            },
            feature_flags={"zeta": False, "alpha": True},
        )
        second = self._build(
            prompt_hashes={
                "prompts/a.md": "2" * 64,
                "prompts/z.md": "1" * 64,
            },
            dependency_versions={"a-package": "1", "z-package": "2"},
            config={
                "alpha": [3, 2, 1],
                "zeta": {"first": 1, "second": 2},
            },
            feature_flags={"alpha": True, "zeta": False},
        )

        self.assertEqual(first.canonical_json_bytes(), second.canonical_json_bytes())
        self.assertEqual(first.provenance_hash, second.provenance_hash)
        self.assertEqual(
            ["prompts/a.md", "prompts/z.md"],
            [item["path"] for item in first.to_dict()["assets"]["prompts"]],
        )

    def test_tampering_is_rejected_by_integrity_validation(self) -> None:
        record = self._build().to_dict()
        record["model"]["model"] = "changed-model"

        with self.assertRaisesRegex(ExecutionProvenanceError, "hash mismatch"):
            validate_execution_provenance(record)

    def test_sensitive_fields_credentials_and_environment_dumps_are_rejected(self) -> None:
        secret = "sk-test-super-secret-value"
        unsafe_configs = (
            {"api_key": secret},
            {"request": {"Authorization": f"Bearer {secret}"}},
            {"notes": f"Authorization: Bearer {secret}"},
            {"notes": "FIRST=value\nSECOND=value"},
            {"headers": {"user_agent": "NovelAgent"}},
        )

        for config in unsafe_configs:
            with self.subTest(config_keys=tuple(config)):
                with self.assertRaises(UnsafeProvenanceError) as caught:
                    self._build(config=config)
                self.assertNotIn(secret, str(caught.exception))

    def test_local_absolute_paths_are_rejected_in_public_fields(self) -> None:
        unsafe_paths = (
            r"C:\Users\alice\NovelAgent",
            r"\\server\private\NovelAgent",
            "/home/alice/NovelAgent",
            "file:///home/alice/NovelAgent",
            "~/NovelAgent",
        )

        for local_path in unsafe_paths:
            with self.subTest(local_path=local_path):
                with self.assertRaises(UnsafeProvenanceError):
                    self._build(config={"workspace": local_path})

    def test_capture_emits_only_logical_paths_and_git_commit_dirty_boolean(self) -> None:
        root = self._writable_temp_root()
        (root / "core").mkdir()
        (root / "prompts").mkdir()
        (root / "schemas").mkdir()
        (root / "core" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "prompts" / "chapter.md").write_text("Draft.", encoding="utf-8")
        (root / "schemas" / "chapter.json").write_text("{}\n", encoding="utf-8")

        provenance = capture_execution_provenance(
            root,
            provider="anthropic",
            model="claude-test",
            dependency_versions={"anthropic": "0.40.0"},
            git_commit="1" * 40,
            git_dirty=True,
            config={"max_output_tokens": 2048},
            feature_flags={"review_gate": True},
            code_files=[root / "core" / "worker.py"],
            prompt_files=[root / "prompts" / "chapter.md"],
            schema_files=[root / "schemas" / "chapter.json"],
        )
        record = provenance.to_dict()
        rendered = provenance.canonical_json_bytes().decode("utf-8")

        self.assertEqual(
            {"commit": "1" * 40, "dirty": True},
            record["code"]["git"],
        )
        self.assertEqual(
            [{
                "path": "prompts/chapter.md",
                "sha256": hashlib.sha256(b"Draft.").hexdigest(),
            }],
            record["assets"]["prompts"],
        )
        self.assertEqual(
            [{
                "path": "schemas/chapter.json",
                "sha256": hashlib.sha256(
                    (root / "schemas" / "chapter.json").read_bytes()
                ).hexdigest(),
            }],
            record["assets"]["schemas"],
        )
        self.assertNotIn(str(root), rendered)
        self.assertNotIn(str(root).replace("\\", "/"), rendered)
        self.assertNotIn("diff", record["code"]["git"])
        self.assertNotIn("status", record["code"]["git"])

    def test_code_bundle_hash_changes_with_code_bytes_not_file_order(self) -> None:
        root = self._writable_temp_root()
        (root / "core").mkdir()
        first_path = root / "core" / "a.py"
        second_path = root / "core" / "b.py"
        first_path.write_text("A = 1\n", encoding="utf-8")
        second_path.write_text("B = 2\n", encoding="utf-8")
        common = {
            "provider": "openai",
            "model": "gpt-test",
            "dependency_versions": {},
            "git_commit": "2" * 40,
            "git_dirty": False,
            "prompt_files": [],
            "schema_files": [],
        }

        first = capture_execution_provenance(
            root,
            code_files=[first_path, second_path],
            **common,
        )
        reordered = capture_execution_provenance(
            root,
            code_files=[second_path, first_path],
            **common,
        )
        second_path.write_text("B = 3\n", encoding="utf-8")
        changed = capture_execution_provenance(
            root,
            code_files=[first_path, second_path],
            **common,
        )

        self.assertEqual(first.code_bundle_hash, reordered.code_bundle_hash)
        self.assertNotEqual(first.code_bundle_hash, changed.code_bundle_hash)


if __name__ == "__main__":
    unittest.main()
