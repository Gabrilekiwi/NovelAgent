from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import core.memory_v2.storage as storage_module
from core.memory_v2 import (
    create_empty_canonical_memory,
    load_canonical_memory,
    save_canonical_memory,
    validate_canonical_memory,
)
from core.memory_v2.storage import _tmp_path_for


class MemoryV2StorageTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_v2_storage" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_save_and_load_canonical_memory(self) -> None:
        memory = create_empty_canonical_memory(book_id="book-1", title="Test Book", language="zh-CN")
        path = self._case_dir("roundtrip") / "canonical_memory.json"

        saved = save_canonical_memory(path, memory)
        loaded = load_canonical_memory(path)

        self.assertEqual(memory, saved)
        self.assertEqual(memory, loaded)
        self.assertTrue(path.exists())

    def test_save_prefers_atomic_replace(self) -> None:
        path = self._case_dir("atomic") / "canonical_memory.json"
        memory = create_empty_canonical_memory(book_id="atomic", title="Atomic")

        def fake_replace(tmp_path: Path, target_path: Path) -> None:
            target_path.write_text(tmp_path.read_text(encoding="utf-8"), encoding="utf-8")

        with patch.object(storage_module, "_atomic_replace", side_effect=fake_replace) as replace:
            save_canonical_memory(path, memory)

        loaded = load_canonical_memory(path)

        replace.assert_called_once()
        self.assertTrue(path.exists())
        self.assertEqual("atomic", json.loads(path.read_text(encoding="utf-8"))["book_id"])
        self.assertIs(loaded, validate_canonical_memory(loaded))

    def test_save_falls_back_to_direct_write_on_windows_permission_denied(self) -> None:
        path = self._case_dir("fallback") / "canonical_memory.json"
        memory = create_empty_canonical_memory(book_id="fallback", title="Fallback")
        error = PermissionError(13, "Access is denied")
        error.winerror = 5

        with patch.object(storage_module, "_atomic_replace", side_effect=error) as replace:
            save_canonical_memory(path, memory)

        replace.assert_called_once()
        self.assertTrue(path.exists())
        self.assertEqual("fallback", json.loads(path.read_text(encoding="utf-8"))["book_id"])

    def test_save_reraises_non_permission_replace_errors(self) -> None:
        path = self._case_dir("reraises") / "canonical_memory.json"
        memory = create_empty_canonical_memory(book_id="reraises", title="Reraises")

        with patch.object(storage_module, "_atomic_replace", side_effect=PermissionError(1, "Different failure")):
            with self.assertRaises(PermissionError):
                save_canonical_memory(path, memory)

    def test_save_creates_parent_directory(self) -> None:
        path = self._case_dir("parents") / "nested" / "canonical_memory.json"

        save_canonical_memory(path, create_empty_canonical_memory())

        self.assertTrue(path.exists())

    def test_tmp_path_sits_next_to_target(self) -> None:
        path = Path("data") / "canonical_memory.json"

        self.assertEqual(Path("data") / "canonical_memory.json.tmp", _tmp_path_for(path))


if __name__ == "__main__":
    unittest.main()
