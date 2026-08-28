from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import main as cli
from core.cli.progress import (
    PROGRESS_JSONL_SCHEMA_VERSION,
    ProgressJSONLError,
    ProgressJSONLWriter,
    build_progress_jsonl_record,
    validate_progress_jsonl_path,
)


class ProgressJSONLTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = (
            Path.cwd()
            / ".tmp"
            / "test_cli_progress"
            / f"{name}_{uuid.uuid4().hex}"
        )
        case_dir.mkdir(parents=True)
        return case_dir.resolve()

    def test_parser_accepts_progress_jsonl_path(self) -> None:
        path = str(self._case_dir("parser") / "events.jsonl")
        with patch.object(
            cli.sys,
            "argv",
            ["main.py", "--dry-run", "--progress-jsonl", path],
        ):
            args = cli.parse_args()

        self.assertEqual(path, args.progress_jsonl)

    def test_record_has_stable_envelope_and_omits_error_details(self) -> None:
        record = build_progress_jsonl_record(
            {
                "event": "step_failed",
                "step": 2,
                "requested_steps": 10,
                "chapter_index": 20,
                "run_id": "chapter_20_test",
                "error_type": "ProviderError",
                "message": "request failed with api_key=secret-value",
                "api_key": "secret-value",
            },
            sequence=4,
            timestamp="2026-08-03T12:00:00+00:00",
        )

        self.assertEqual(PROGRESS_JSONL_SCHEMA_VERSION, record["schema_version"])
        self.assertEqual(4, record["sequence"])
        self.assertEqual("2026-08-03T12:00:00+00:00", record["timestamp"])
        self.assertEqual("step_failed", record["event"])
        self.assertEqual("chapter", record["stage"])
        self.assertEqual(20, record["chapter_index"])
        self.assertEqual("chapter_20_test", record["run_id"])
        self.assertEqual(
            {
                "step": 2,
                "requested_steps": 10,
                "error_type": "ProviderError",
            },
            record["payload"],
        )
        self.assertNotIn("secret-value", json.dumps(record))

    def test_writer_flushes_and_continues_append_only_sequence(self) -> None:
        path = self._case_dir("append") / "events.jsonl"
        writer = ProgressJSONLWriter(path)
        writer({"event": "loop_start", "requested_steps": 2})

        first = path.read_text(encoding="utf-8")
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(1, json.loads(first)["sequence"])
        writer.close()

        with ProgressJSONLWriter(path) as resumed:
            resumed(
                {
                    "event": "step_end",
                    "step": 1,
                    "requested_steps": 2,
                    "chapter_index": 20,
                    "run_id": "chapter_20_test",
                    "committed": True,
                    "status": "committed",
                    "duration_ms": 12,
                }
            )

        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

        self.assertEqual([1, 2], [record["sequence"] for record in records])
        self.assertEqual("chapter_20_test", records[1]["run_id"])
        self.assertEqual(20, records[1]["chapter_index"])

    def test_path_validation_rejects_unsafe_or_non_contract_targets(self) -> None:
        with self.assertRaisesRegex(ProgressJSONLError, "absolute"):
            validate_progress_jsonl_path("events.jsonl")

        root = self._case_dir("unsafe")
        with self.assertRaisesRegex(ProgressJSONLError, "already exist"):
            validate_progress_jsonl_path(root / "missing" / "events.jsonl")
        with self.assertRaisesRegex(ProgressJSONLError, "normal file"):
            validate_progress_jsonl_path(root)

        invalid = root / "existing.txt"
        invalid.write_text("unrelated content\n", encoding="utf-8")
        with self.assertRaisesRegex(ProgressJSONLError, "invalid JSON"):
            ProgressJSONLWriter(invalid)

        incomplete = root / "incomplete.jsonl"
        incomplete.write_text(
            '{"schema_version":1,"sequence":1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ProgressJSONLError, "incomplete"):
            ProgressJSONLWriter(incomplete)

    def test_jsonl_observer_is_independent_from_human_progress(self) -> None:
        path = self._case_dir("no_human") / "events.jsonl"
        args = SimpleNamespace(
            no_progress=True,
            output_json=False,
            output_run_json=False,
            steps=2,
            progress_jsonl=str(path),
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            observer = cli._loop_progress_observer(args)
            self.assertIsNotNone(observer)
            observer({"event": "loop_start", "requested_steps": 2})
            observer.close()

        record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual("", stderr.getvalue())
        self.assertEqual("loop_start", record["event"])

    def test_single_step_uses_same_progress_contract_without_console_output(self) -> None:
        class Executor:
            def run_once(self, *, persist: bool):
                self.persist = persist
                return {
                    "committed": True,
                    "run": {
                        "id": "chapter_7_test",
                        "chapter_index": 7,
                        "status": "committed",
                    },
                }

        path = self._case_dir("single") / "events.jsonl"
        args = SimpleNamespace(
            no_progress=False,
            output_json=False,
            output_run_json=False,
            steps=1,
            progress_jsonl=str(path),
        )
        stderr = io.StringIO()
        observer = cli._loop_progress_observer(args)
        self.assertIsNotNone(observer)
        try:
            with contextlib.redirect_stderr(stderr):
                result = cli._run_once_with_progress(
                    Executor(), persist=True, observer=observer
                )
        finally:
            observer.close()

        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

        self.assertTrue(result["committed"])
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            ["loop_start", "step_start", "step_end", "loop_end"],
            [record["event"] for record in records],
        )
        self.assertEqual("chapter_7_test", records[2]["run_id"])


if __name__ == "__main__":
    unittest.main()
