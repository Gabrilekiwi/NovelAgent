from __future__ import annotations

import copy
import unittest

from core.memory_v2 import MemoryPatchValidationError, create_memory_patch, validate_memory_patch


class MemoryV2PatchTest(unittest.TestCase):
    def test_create_empty_memory_patch(self) -> None:
        patch = create_memory_patch(patch_id="patch_import_v1_default")

        self.assertEqual("2.1", patch["schema_version"])
        self.assertEqual("patch_import_v1_default", patch["patch_id"])
        self.assertEqual("local_memory", patch["source"]["kind"])
        self.assertEqual([], patch["operations"])

    def test_memory_patch_matches_schema(self) -> None:
        patch = create_memory_patch(
            patch_id="patch-1",
            source_path="data/notion_memory.example.json",
            operations=[
                {
                    "op": "upsert_character",
                    "id": "char_lin_xue",
                    "value": {"name": "Lin Xue", "data": {}},
                }
            ],
        )

        self.assertIs(patch, validate_memory_patch(patch))

    def test_rejects_missing_required_field(self) -> None:
        patch = create_memory_patch(patch_id="patch-1")
        patch.pop("patch_id")

        with self.assertRaisesRegex(MemoryPatchValidationError, "patch_id is required"):
            validate_memory_patch(patch)

    def test_rejects_missing_source(self) -> None:
        patch = create_memory_patch(patch_id="patch-1")
        patch.pop("source")

        with self.assertRaisesRegex(MemoryPatchValidationError, "source is required"):
            validate_memory_patch(patch)

    def test_rejects_non_array_operations(self) -> None:
        patch = create_memory_patch(patch_id="patch-1")
        patch["operations"] = {"op": "update_world"}

        with self.assertRaisesRegex(MemoryPatchValidationError, "operations must be array"):
            validate_memory_patch(patch)

    def test_rejects_operation_without_op(self) -> None:
        patch = create_memory_patch(patch_id="patch-1")
        patch["operations"].append({"id": "char_lin_xue"})

        with self.assertRaisesRegex(MemoryPatchValidationError, "op is required"):
            validate_memory_patch(patch)

    def test_accepts_chinese_ids_and_values(self) -> None:
        patch = create_memory_patch(
            patch_id="patch-cn",
            operations=[
                {
                    "op": "upsert_character",
                    "id": "char_林雪",
                    "value": {"name": "林雪", "data": {"role": "地铁A线乘务员"}},
                }
            ],
        )

        self.assertEqual(patch, validate_memory_patch(copy.deepcopy(patch)))


if __name__ == "__main__":
    unittest.main()
