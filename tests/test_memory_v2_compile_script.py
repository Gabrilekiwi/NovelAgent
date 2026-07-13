from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.memory_v2 import (
    MemoryCompileError,
    compile_memory_v2,
    load_canonical_memory,
    load_memory_event_batches,
    validate_canonical_memory,
    validate_memory_event,
)
from core.schema import validate_schema
from core.state.snapshot import validate_snapshot


class MemoryV2CompileScriptTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_memory_v2_compile_script" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _compile(self, output_dir: Path, **kwargs) -> dict:
        return compile_memory_v2(
            memory_path=Path("data/notion_memory.example.json"),
            output_dir=output_dir,
            **kwargs,
        )

    def test_initial_compile_writes_all_outputs(self) -> None:
        output_dir = self._case_dir("initial")

        self._compile(output_dir)

        for name in (
            "canonical_memory.json",
            "memory_events",
            "memory_patch.json",
            "snapshot_preview.json",
            "memory_compile_report.json",
        ):
            self.assertTrue((output_dir / name).exists(), name)

    def test_compile_report_matches_schema(self) -> None:
        output_dir = self._case_dir("report")

        report = self._compile(output_dir)

        self.assertIs(report, validate_schema(report, "memory_compile_report.schema.json"))
        self.assertEqual("ok", report["status"])
        self.assertGreater(report["patch"]["operation_count"], 0)
        self.assertGreater(report["events"]["event_count"], 0)
        self.assertEqual(report["canonical_memory"]["revision"], report["events"]["last_revision"])

    def test_canonical_memory_output_loads_and_validates(self) -> None:
        output_dir = self._case_dir("canonical")

        self._compile(output_dir)
        canonical = load_canonical_memory(output_dir / "canonical_memory.json")

        self.assertIs(canonical, validate_canonical_memory(canonical))
        self.assertTrue(canonical["world"])
        self.assertTrue(canonical["characters"])
        self.assertTrue(canonical["locations"])
        self.assertTrue(canonical["current_state"])

    def test_events_output_loads_and_validates(self) -> None:
        output_dir = self._case_dir("events")
        report = self._compile(output_dir)

        batches = load_memory_event_batches(output_dir / "memory_events")
        events = [event for batch in batches for event in batch["events"]]

        self.assertEqual(report["events"]["event_count"], len(events))
        self.assertTrue(all(validate_memory_event(event) is event for event in events))

    def test_snapshot_preview_validates(self) -> None:
        output_dir = self._case_dir("snapshot")

        self._compile(output_dir)
        snapshot = json.loads((output_dir / "snapshot_preview.json").read_text(encoding="utf-8"))

        self.assertIs(snapshot, validate_snapshot(snapshot))
        self.assertEqual("medium", snapshot["world_state"]["infection_level"])
        self.assertTrue(snapshot["story_state"]["open_threads"])
        self.assertTrue(snapshot["spatial_state"]["spaces"])
        self.assertTrue(snapshot["characters"])
        self.assertTrue(snapshot["locations"])

    def test_dry_run_does_not_write_outputs(self) -> None:
        output_dir = self._case_dir("dry_run") / "out"

        report = self._compile(output_dir, dry_run=True)

        self.assertTrue(report["dry_run"])
        for name in (
            "canonical_memory.json",
            "memory_events",
            "memory_patch.json",
            "snapshot_preview.json",
            "memory_compile_report.json",
        ):
            self.assertFalse((output_dir / name).exists(), name)

    def test_incremental_compile_advances_revision_and_appends_events(self) -> None:
        output_dir = self._case_dir("incremental")

        first = self._compile(output_dir)
        first_event_count = len(load_memory_event_batches(output_dir / "memory_events"))
        second = self._compile(output_dir)
        second_event_count = len(load_memory_event_batches(output_dir / "memory_events"))

        self.assertEqual(first["canonical_memory"]["revision"], second["canonical_memory"]["revision"])
        self.assertEqual("no_op", second["patch"]["apply_status"])
        self.assertEqual(first_event_count, second_event_count)

    def test_changed_source_creates_new_source_sync_transaction(self) -> None:
        output_dir = self._case_dir("changed_source")
        source_path = output_dir / "source.json"
        payload = json.loads(Path("data/notion_memory.example.json").read_text(encoding="utf-8"))
        source_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        first = compile_memory_v2(memory_path=source_path, output_dir=output_dir / "out")
        if isinstance(payload, dict) and isinstance(payload.get("pages"), list):
            payload["pages"].append(
                {"properties": {"Type": "world_state", "Data": {"weather": "rain"}}}
            )
        else:
            self.fail("memory example must contain a pages list")
        source_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        second = compile_memory_v2(memory_path=source_path, output_dir=output_dir / "out")

        self.assertNotEqual(first["patch"]["patch_id"], second["patch"]["patch_id"])
        self.assertEqual("applied", second["patch"]["apply_status"])
        self.assertGreater(second["canonical_memory"]["revision"], first["canonical_memory"]["revision"])

    def test_reset_starts_from_empty_canonical_memory(self) -> None:
        output_dir = self._case_dir("reset")
        self._compile(output_dir)

        with self.assertRaisesRegex(MemoryCompileError, "immutable"):
            self._compile(output_dir, reset=True)

    def test_cli_can_compile(self) -> None:
        output_dir = self._case_dir("cli")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/compile_memory_v2.py",
                "--memory",
                "data/notion_memory.example.json",
                "--out",
                str(output_dir),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((output_dir / "canonical_memory.json").exists())
        self.assertTrue((output_dir / "memory_compile_report.json").exists())

    def test_cli_json_outputs_report_only(self) -> None:
        output_dir = self._case_dir("cli_json")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/compile_memory_v2.py",
                "--memory",
                "data/notion_memory.example.json",
                "--out",
                str(output_dir),
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("ok", report["status"])


if __name__ == "__main__":
    unittest.main()
