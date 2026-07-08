from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.memory_v2 import create_empty_canonical_memory, load_canonical_memory, save_canonical_memory
from core.memory_v2.storage import _atomic_replace, _tmp_path_for


class MemoryV2StorageTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_v2_storage" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_save_and_load_canonical_memory(self) -> None:
        memory = create_empty_canonical_memory(book_id="book-1", title="测试书", language="zh-CN")
        path = self._case_dir("roundtrip") / "canonical_memory.json"

        with patch("core.memory_v2.storage._atomic_replace", side_effect=self._copy_replace):
            saved = save_canonical_memory(path, memory)
        loaded = load_canonical_memory(path)

        self.assertEqual(memory, saved)
        self.assertEqual(memory, loaded)
        self.assertTrue(path.exists())

    def test_save_uses_same_directory_tmp_then_replace(self) -> None:
        path = self._case_dir("atomic") / "canonical_memory.json"
        old_memory = create_empty_canonical_memory(book_id="old", title="Old")
        new_memory = create_empty_canonical_memory(book_id="new", title="New")

        with patch("core.memory_v2.storage._atomic_replace", side_effect=self._copy_replace) as replace_mock:
            save_canonical_memory(path, old_memory)
            save_canonical_memory(path, new_memory)

        self.assertEqual("new", json.loads(path.read_text(encoding="utf-8"))["book_id"])
        self.assertEqual(_tmp_path_for(path), replace_mock.call_args_list[-1].args[0])
        self.assertEqual(path, replace_mock.call_args_list[-1].args[1])

    def test_save_creates_parent_directory(self) -> None:
        path = self._case_dir("parents") / "nested" / "canonical_memory.json"

        with patch("core.memory_v2.storage._atomic_replace", side_effect=self._copy_replace):
            save_canonical_memory(path, create_empty_canonical_memory())

        self.assertTrue(path.exists())

    def test_tmp_path_sits_next_to_target(self) -> None:
        path = Path("data") / "canonical_memory.json"

        self.assertEqual(Path("data") / "canonical_memory.json.tmp", _tmp_path_for(path))

    def test_atomic_replace_uses_path_replace(self) -> None:
        tmp_path = Path("canonical_memory.json.tmp")
        target_path = Path("canonical_memory.json")

        with patch.object(Path, "replace") as replace_mock:
            _atomic_replace(tmp_path, target_path)

        replace_mock.assert_called_once_with(target_path)

    def _copy_replace(self, tmp_path: Path, target_path: Path) -> None:
        target_path.write_text(tmp_path.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
