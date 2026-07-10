from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import main as cli
from core.engine.run_record import build_run_record
from core.runtime_paths import DEFAULT_CHAPTER_DIR, DEFAULT_RUN_DIR, DEFAULT_SNAPSHOT_PATH


class CliTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_cli" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self, case_dir: Path) -> Path:
        path = case_dir / "snapshot.json"
        path.write_text(
            json.dumps({"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )
        return path

    def test_format_run_summary_is_concise(self) -> None:
        summary = cli.format_run_summary(
            {
                "chapter": "A concise chapter.",
                "run": {
                    "id": "chapter_1_test",
                    "status": "committed",
                    "committed": True,
                    "chapter_index": 1,
                    "workflow": ["generate_chapter", "validate"],
                    "repair_attempts": 0,
                    "validation": {
                        "problem_codes": [],
                        "requested_focus": ["logic"],
                        "executed_checks": ["logic"],
                        "skipped_checks": ["continuity", "spatial"],
                    },
                },
                "validation": {"ok": True, "problems": []},
                "analysis": {
                    "summary": "A concise chapter.",
                    "events": [{"text": "A concise chapter."}],
                    "conflicts": ["danger"],
                    "world_changes": [],
                },
            }
        )

        self.assertIn("Chapter:", summary)
        self.assertIn("Run:", summary)
        self.assertIn("Validation:", summary)
        self.assertIn("workflow: generate_chapter -> validate", summary)
        self.assertIn("requested_focus: logic", summary)
        self.assertIn("executed_checks: logic", summary)
        self.assertIn("skipped_checks: continuity, spatial", summary)
        self.assertNotIn('"trace"', summary)
        self.assertNotIn('"snapshot_builder"', summary)

    def test_parse_args_defaults_to_local_runtime_paths(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--dry-run"]):
            args = cli.parse_args()

        self.assertEqual(str(DEFAULT_SNAPSHOT_PATH), args.snapshot)
        self.assertEqual(str(DEFAULT_RUN_DIR), args.run_dir)
        self.assertEqual(str(DEFAULT_CHAPTER_DIR), args.chapter_dir)
        self.assertFalse(args.no_proxy)
        self.assertFalse(args.check_memory_v2)
        self.assertEqual("data/memory_v2/default", args.memory_v2_out)
        self.assertIsNone(args.story_project)
        self.assertEqual("auto", args.chapter)
        self.assertFalse(args.story_project_compat_report)

    def test_parse_args_accepts_story_project_and_chapter(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--story-project", "auto", "--chapter", "21"]):
            args = cli.parse_args()

        self.assertEqual("auto", args.story_project)
        self.assertEqual("21", args.chapter)

    def test_parse_args_accepts_story_project_writeback_flags(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["main.py", "--story-project", "auto", "--story-project-writeback-dry-run", "--story-project-overwrite"],
        ):
            args = cli.parse_args()

        self.assertTrue(args.story_project_writeback_dry_run)
        self.assertTrue(args.story_project_overwrite)
        config = cli._story_project_writeback_config_from_args(args)
        self.assertEqual("dry_run", config.mode)
        self.assertTrue(config.overwrite)

    def test_story_project_compat_report_requires_story_project(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--story-project-compat-report"]):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "--story-project"):
            cli._validate_story_project_compat_report_args(args)

    def test_story_project_compat_report_outputs_without_executor(self) -> None:
        case_dir = self._case_dir("compat_report")
        story_project_root = case_dir / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (story_project_root / directory).mkdir(parents=True)
        (story_project_root / ".story-deployed").write_text("ok", encoding="utf-8")

        class FailingExecutor:
            def __init__(self, **kwargs):
                raise AssertionError("compat report must not construct AgentExecutor")

        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            ["main.py", "--story-project", str(story_project_root), "--story-project-compat-report", "--output-json"],
        ), patch.object(cli, "AgentExecutor", FailingExecutor), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["detected"])
        self.assertEqual("low", payload["confidence"])

    def test_review_auto_repair_requires_review_pipeline(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--review-auto-repair"]):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "--enable-review-pipeline"):
            cli._review_repair_config_from_args(args)

    def test_review_repair_dry_run_requires_auto_repair(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--enable-review-pipeline", "--review-repair-dry-run"]):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "--review-auto-repair"):
            cli._review_repair_config_from_args(args)

    def test_review_repair_max_attempts_accepts_one_to_three(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["main.py", "--enable-review-pipeline", "--review-auto-repair", "--review-repair-max-attempts", "3"],
        ):
            args = cli.parse_args()

        config = cli._review_repair_config_from_args(args)
        self.assertTrue(config.enabled)
        self.assertEqual(3, config.max_attempts)

    def test_review_repair_max_attempts_rejects_out_of_range(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["main.py", "--enable-review-pipeline", "--review-auto-repair", "--review-repair-max-attempts", "4"],
        ):
            with self.assertRaises(SystemExit):
                cli.parse_args()

    def test_story_project_real_writeback_conflicts_with_global_dry_run(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--dry-run", "--story-project", "auto", "--story-project-writeback"]):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "--story-project-writeback-dry-run"):
            cli._story_project_writeback_config_from_args(args)

    def test_story_project_writeback_preview_conflicts_with_persist_dry_run(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--story-project",
                "auto",
                "--story-project-writeback-dry-run",
                "--persist-dry-run",
            ],
        ):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "--persist-dry-run"):
            cli._story_project_writeback_config_from_args(args)

    def test_story_project_real_writeback_rejects_direct_notion_delivery(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--story-project",
                "auto",
                "--story-project-writeback",
                "--memory-writeback",
                "notion",
            ],
        ):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "Notion"):
            cli._story_project_writeback_config_from_args(args)

    def test_story_project_multistep_requires_real_writeback(self) -> None:
        for extra in ([], ["--story-project-writeback-dry-run"]):
            with self.subTest(extra=extra), patch.object(
                sys,
                "argv",
                ["main.py", "--story-project", "auto", "--steps", "2", *extra],
            ):
                args = cli.parse_args()
                config = cli._story_project_writeback_config_from_args(args)
                with self.assertRaisesRegex(ValueError, "--story-project-writeback"):
                    cli._validate_story_project_multistep_args(args, config)

    def test_story_project_multistep_rejects_persist_dry_run(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--story-project",
                "auto",
                "--steps",
                "2",
                "--story-project-writeback",
                "--persist-dry-run",
            ],
        ):
            args = cli.parse_args()
        config = cli._story_project_writeback_config_from_args(args)

        with self.assertRaisesRegex(ValueError, "--persist-dry-run"):
            cli._validate_story_project_multistep_args(args, config)

    def test_story_project_multistep_accepts_real_persisted_writeback(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["main.py", "--story-project", "auto", "--steps", "2", "--story-project-writeback"],
        ):
            args = cli.parse_args()
        config = cli._story_project_writeback_config_from_args(args)

        cli._validate_story_project_multistep_args(args, config)
        self.assertEqual("apply", config.mode)

    def test_story_project_writeback_requires_story_project(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--story-project-writeback"]):
            args = cli.parse_args()

        with self.assertRaisesRegex(ValueError, "--story-project"):
            cli._story_project_writeback_config_from_args(args)

    def test_reconcile_persistence_is_a_generation_free_command(self) -> None:
        case_dir = self._case_dir("reconcile_command")
        report = {
            "ok": True,
            "transaction_count": 0,
            "recovery_required": [],
            "transactions": [],
            "published_run_ids": [],
            "existing_run_ids": [],
            "publish_errors": [],
        }
        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--reconcile-persistence",
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
                "--output-json",
            ],
        ), patch.object(
            cli,
            "_reconcile_and_publish_persistence",
            return_value=report,
        ) as reconcile, patch.object(cli, "AgentExecutor") as executor, contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        reconcile.assert_called_once_with(
            str(case_dir / "runs"),
            chapter_dir=str(case_dir / "chapters"),
            state_paths=(Path(str(DEFAULT_SNAPSHOT_PATH)),),
        )
        executor.assert_not_called()
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_story_project_real_writeback_failure_exits_nonzero(self) -> None:
        context_loader = type("ContextLoader", (), {"story_project_root": Path("book")})()

        class FakeExecutor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run_once(self, *, persist: bool = True):
                return {
                    "chapter": "chapter",
                    "validation": {"ok": True, "problems": []},
                    "analysis": {},
                    "run": {
                        "id": "chapter_1_test",
                        "status": "committed",
                        "committed": True,
                        "chapter_index": 1,
                        "workflow": [],
                        "repair_attempts": 0,
                        "validation": {"problem_codes": []},
                        "story_project": {
                            "writeback": {
                                "attempted": True,
                                "applied": False,
                                "partial": False,
                                "dry_run": False,
                                "overwrite": False,
                                "targets": [],
                                "blocked_reasons": ["target_prose_exists"],
                                "errors": [],
                                "failed_targets": [],
                                "diff_summary": {},
                                "artifacts": {},
                            }
                        },
                    },
                    "committed": True,
                }

        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            ["main.py", "--story-project", "auto", "--story-project-writeback"],
        ), patch.object(
            cli,
            "build_generation_story_project_context_loader",
            return_value=context_loader,
        ), patch.object(
            cli,
            "AgentExecutor",
            FakeExecutor,
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(1, exit_context.exception.code)

    def test_parse_args_accepts_no_proxy(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--dry-run", "--no-proxy"]):
            args = cli.parse_args()

        self.assertTrue(args.no_proxy)

    def test_apply_notion_shortcuts(self) -> None:
        with patch.object(sys, "argv", ["main.py", "--notion-sync"]):
            args = cli.apply_notion_shortcuts(cli.parse_args())

        self.assertEqual("notion", args.memory_source)
        self.assertEqual("notion", args.memory_writeback)
        self.assertTrue(args.notion_readback)

    def test_format_loop_progress_event(self) -> None:
        line = cli.format_loop_progress_event(
            {
                "event": "step_end",
                "step": 1,
                "requested_steps": 3,
                "status": "committed",
                "committed": True,
                "duration_ms": 123,
                "run_id": "chapter_1_test",
            }
        )

        self.assertIn("step 1/3 committed", line)
        self.assertIn("duration_ms=123", line)

    def test_no_proxy_clears_proxy_environment(self) -> None:
        case_dir = self._case_dir("no_proxy")
        run_dir = case_dir / "runs"
        run_dir.mkdir()
        output = io.StringIO()

        env = {
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "ALL_PROXY": "socks5://127.0.0.1:7890",
            "http_proxy": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "all_proxy": "socks5://127.0.0.1:7890",
        }

        with patch.dict(os.environ, env, clear=False), patch.object(
            sys,
            "argv",
            ["main.py", "--no-proxy", "--report-runs", "--run-dir", str(run_dir)],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

            self.assertEqual(0, exit_context.exception.code)
            for name in env:
                self.assertNotIn(name, os.environ)

    def test_env_no_proxy_clears_proxy_environment(self) -> None:
        case_dir = self._case_dir("env_no_proxy")
        run_dir = case_dir / "runs"
        run_dir.mkdir()
        output = io.StringIO()

        env = {
            "NOVELAGENT_NO_PROXY": "1",
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "ALL_PROXY": "socks5://127.0.0.1:7890",
        }

        with patch.dict(os.environ, env, clear=False), patch.object(
            sys,
            "argv",
            ["main.py", "--report-runs", "--run-dir", str(run_dir)],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

            self.assertEqual(0, exit_context.exception.code)
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertNotIn("ALL_PROXY", os.environ)

    def test_format_run_summary_includes_failure_diagnostics(self) -> None:
        summary = cli.format_run_summary(
            {
                "chapter": "",
                "run": {
                    "id": "chapter_1_failed",
                    "status": "failed",
                    "committed": False,
                    "chapter_index": 1,
                    "workflow": ["generate_chapter"],
                    "repair_attempts": 0,
                    "validation": {"problem_codes": ["execution_error"]},
                    "error": {"type": "ModelCallError", "message": "OpenAI response did not include choices."},
                    "trace": [
                        {
                            "action": "generate_chapter",
                            "model_call": {
                                "provider": "openai",
                                "stage": "chapter_generation",
                                "model": "gpt-test",
                                "cause_type": None,
                                "message": "OpenAI response did not include choices.",
                            },
                        }
                    ],
                },
                "validation": {"ok": False, "problems": [{"code": "execution_error"}]},
                "analysis": {},
            }
        )

        self.assertIn("Error:", summary)
        self.assertIn("type: ModelCallError", summary)
        self.assertIn("Model Calls:", summary)
        self.assertIn("generate_chapter: openai chapter_generation model=gpt-test", summary)
        self.assertIn("problem_codes: execution_error", summary)
        self.assertNotIn('"trace"', summary)

    def test_format_run_summary_hides_recoverable_polish_error_message(self) -> None:
        summary = cli.format_run_summary(
            {
                "chapter": "The generated chapter remains usable.",
                "run": {
                    "id": "chapter_1_polish_failed",
                    "status": "committed",
                    "committed": True,
                    "chapter_index": 1,
                    "workflow": ["generate_chapter", "polish", "validate"],
                    "repair_attempts": 0,
                    "validation": {"problem_codes": []},
                    "trace": [
                        {
                            "action": "polish",
                            "status": "failed",
                            "plan_failure_policy": "continue_unpolished",
                            "model_call": {
                                "provider": "anthropic",
                                "stage": "claude_polish",
                                "model": "claude-test",
                                "cause_type": None,
                                "message": "Claude polish failed: invalid provider response",
                                "failure_category": "provider_error",
                                "retryable": False,
                            },
                        },
                        {"action": "validate", "status": "completed"},
                    ],
                },
                "validation": {"ok": True, "problems": []},
                "analysis": {},
            }
        )

        self.assertIn("Polish:", summary)
        self.assertIn("using unpolished generated chapter", summary)
        self.assertNotIn("Claude polish failed", summary)
        self.assertNotIn("invalid provider response", summary)

    def test_format_loop_failure_summary_is_concise(self) -> None:
        summary = cli.format_loop_failure_summary(
            {
                "session": {
                    "id": "loop_20260101T000000000000Z",
                    "completed_steps": 2,
                    "stopped_reason": "failed",
                    "committed_count": 1,
                    "rejected_count": 0,
                    "failed_count": 1,
                    "artifact": {"path": "data/runs/loop_sessions/loop_20260101T000000000000Z.json"},
                },
                "error": {
                    "type": "ValueError",
                    "message": "generation failed",
                },
            }
        )

        self.assertIn("Loop failed:", summary)
        self.assertIn("session: loop_20260101T000000000000Z", summary)
        self.assertIn("failed: 1", summary)
        self.assertIn("ValueError: generation failed", summary)
        self.assertIn("loop_artifact:", summary)
        self.assertNotIn('"session"', summary)

    def test_format_loop_failure_summary_includes_last_run_model_call(self) -> None:
        summary = cli.format_loop_failure_summary(
            {
                "session": {
                    "id": "loop_20260101T000000000000Z",
                    "completed_steps": 1,
                    "stopped_reason": "failed",
                    "committed_count": 0,
                    "rejected_count": 0,
                    "failed_count": 1,
                },
                "runs": [
                    {
                        "run": {
                            "id": "chapter_1_failed",
                            "status": "failed",
                            "error": {"type": "ModelCallError", "message": "Claude response did not include text content."},
                            "trace": [
                                {
                                    "action": "polish",
                                    "model_call": {
                                        "provider": "anthropic",
                                        "stage": "claude_polish",
                                        "model": "claude-test",
                                        "cause_type": None,
                                        "message": "Claude response did not include text content.",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "error": {
                    "type": "ModelCallError",
                    "message": "Claude response did not include text content.",
                },
            }
        )

        self.assertIn("last_run: chapter_1_failed (failed)", summary)
        self.assertIn("last_run_error: ModelCallError: Claude response did not include text content.", summary)
        self.assertIn("model_call_polish: anthropic claude_polish model=claude-test", summary)

    def test_format_preflight_summary_is_concise(self) -> None:
        summary = cli.format_preflight_summary(
            {
                "ok": True,
                "checks": [
                    {"name": "schema_assets", "ok": True, "details": {"count": 15, "paths": ["a", "b"]}},
                    {"name": "schema_consistency", "ok": True, "details": {"count": 7, "contracts": []}},
                    {
                        "name": "memory",
                        "ok": True,
                        "details": {
                            "requested_source": "file",
                            "source": "notion-export",
                            "status": "ready",
                            "item_count": 3,
                            "source_mapping_count": 3,
                        },
                    },
                    {
                        "name": "run_history",
                        "ok": True,
                        "details": {
                            "total": 1,
                            "loaded": 1,
                            "latest_run_id": "chapter_1_20260101T000000000000Z",
                            "latest_run_status": "rejected",
                            "latest_run_executed_checks": ["logic"],
                            "latest_run_skipped_checks": ["continuity", "spatial"],
                        },
                    },
                    {"name": "memory_writeback", "ok": True, "details": {"mode": "notion", "notion_readback": True}},
                    {
                        "name": "state_builder_audit",
                        "ok": True,
                        "details": {
                            "item_count": 3,
                            "applied_count": 2,
                            "skipped_count": 1,
                            "deduplicated_count": 0,
                            "applied_type_counts": [{"type": "location", "count": 1}, {"type": "world_state", "count": 1}],
                            "skipped_type_counts": [{"type": "character", "count": 1}],
                            "skipped_reason_counts": [{"reason_code": "missing_name", "count": 1}],
                            "skipped_severity_counts": [{"severity": "medium", "count": 1}],
                            "skipped_blocking_count": 0,
                            "applied_items": ["large"],
                        },
                    },
                    {"name": "planned_workflow", "ok": True, "details": ["generate_chapter", "validate"]},
                    {
                        "name": "oh_story_detection",
                        "ok": True,
                        "details": {
                            "detected": False,
                            "confidence": "none",
                            "summary": {"present_count": 4, "optional_missing_count": 6, "unsupported_count": 2},
                        },
                    },
                ],
            }
        )

        self.assertIn("Preflight: OK", summary)
        self.assertIn("Schemas: 15 files", summary)
        self.assertIn("Schema consistency: 7 contracts", summary)
        self.assertIn("Memory: notion-export status=ready items=3 mappings=3", summary)
        self.assertIn(
            "State builder: items=3 applied=2 skipped=1 deduplicated=0 "
            "applied_types=location=1,world_state=1 skipped_types=character=1 "
            "skipped_blocking=0 reasons=missing_name=1 severity=medium=1",
            summary,
        )
        self.assertIn(
            "Run history: runs=1/1 latest=chapter_1_20260101T000000000000Z "
            "status=rejected checks=logic skipped=continuity,spatial",
            summary,
        )
        self.assertIn("Memory writeback: notion readback=True", summary)
        self.assertIn("Workflow: generate_chapter -> validate", summary)
        self.assertIn("oh-story: detected=False confidence=none markers=4 unsupported=2", summary)
        self.assertNotIn("applied_items", summary)

    def test_check_prints_summary_by_default(self) -> None:
        case_dir = self._case_dir("summary")
        snapshot = self._snapshot(case_dir)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--check",
                "--dry-run",
                "--snapshot",
                str(snapshot),
                "--memory-source",
                "file",
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
            ],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        text = output.getvalue()
        self.assertIn("Preflight: OK", text)
        self.assertIn("Execution:", text)
        self.assertIn("persist=False", text)
        self.assertIn("Memory:", text)
        self.assertIn("State builder:", text)
        self.assertIn("Checks:", text)
        self.assertNotIn("Memory V2 compile:", text)
        self.assertNotIn('"checks"', text)
        self.assertNotIn("state_builder_audit", text)

    def test_check_memory_v2_prints_summary_when_requested(self) -> None:
        case_dir = self._case_dir("memory_v2")
        snapshot = self._snapshot(case_dir)
        output_dir = case_dir / "memory_v2_out"
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--check",
                "--dry-run",
                "--snapshot",
                str(snapshot),
                "--memory",
                "data/notion_memory.example.json",
                "--memory-source",
                "file",
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
                "--check-memory-v2",
                "--memory-v2-out",
                str(output_dir),
            ],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        text = output.getvalue()
        self.assertIn("Memory V2 compile: dry_run=True reset=True", text)
        self.assertFalse((output_dir / "canonical_memory.json").exists())

    def test_init_runtime_prints_summary(self) -> None:
        output = io.StringIO()
        init_result = {
            "runtime_dir": ".tmp/runtime",
            "snapshot_path": ".tmp/runtime/snapshot.json",
            "memory_path": ".tmp/runtime/notion_memory.json",
            "run_dir": ".tmp/runtime/runs",
            "chapter_dir": ".tmp/runtime/chapters",
            "copied": [{"name": "snapshot"}, {"name": "memory"}],
            "skipped": [],
        }

        with patch.object(sys, "argv", ["main.py", "--init-runtime"]), patch.object(
            cli,
            "init_runtime_state",
            return_value=init_result,
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        text = output.getvalue()
        self.assertIn("Runtime initialized:", text)
        self.assertIn(".tmp", text)
        self.assertIn("snapshot", text)
        self.assertIn("memory", text)

    def test_recover_latest_prints_summary(self) -> None:
        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--recover-latest",
                "--run-dir",
                "runs",
                "--chapter-dir",
                "chapters",
            ],
        ), patch.object(
            cli,
            "recover_latest_chapter_draft",
            return_value={
                "ok": True,
                "source_run_id": "chapter_2_failed",
                "source_status": "failed",
                "chapter_index": 2,
                "chars": 42,
                "artifact": {"path": "chapters/chapter_0002_recovered.md"},
            },
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        text = output.getvalue()
        self.assertIn("Recovered chapter draft:", text)
        self.assertIn("chapter_2_failed", text)
        self.assertIn("snapshot_updated: False", text)

    def test_check_json_prints_full_preflight_json(self) -> None:
        case_dir = self._case_dir("json")
        snapshot = self._snapshot(case_dir)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--check",
                "--check-json",
                "--dry-run",
                "--persist-dry-run",
                "--steps",
                "2",
                "--continue-on-rejection",
                "--snapshot",
                str(snapshot),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
            ],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        execution = [check for check in payload["checks"] if check["name"] == "execution_mode"][0]
        self.assertTrue(execution["details"]["persist"])
        self.assertEqual(2, execution["details"]["steps"])
        self.assertFalse(execution["details"]["stop_on_rejection"])
        self.assertIn("state_builder_audit", {check["name"] for check in payload["checks"]})

    def test_rejects_zero_steps_before_running(self) -> None:
        stderr = io.StringIO()

        with patch.object(sys, "argv", ["main.py", "--dry-run", "--steps", "0"]), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(2, exit_context.exception.code)
        self.assertIn("must be at least 1", stderr.getvalue())

    def test_run_prints_summary_by_default(self) -> None:
        case_dir = self._case_dir("run_summary")
        snapshot = self._snapshot(case_dir)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--dry-run",
                "--snapshot",
                str(snapshot),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
            ],
        ), contextlib.redirect_stdout(output):
            cli.main()

        text = output.getvalue()
        self.assertIn("Chapter:", text)
        self.assertIn("Run:", text)
        self.assertIn("Validation:", text)
        self.assertIn("requested_focus:", text)
        self.assertIn("executed_checks:", text)
        self.assertNotIn("--- run ---", text)
        self.assertNotIn('"trace"', text)

    def test_output_json_prints_full_result(self) -> None:
        case_dir = self._case_dir("output_json")
        snapshot = self._snapshot(case_dir)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--dry-run",
                "--output-json",
                "--snapshot",
                str(snapshot),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
            ],
        ), contextlib.redirect_stdout(output):
            cli.main()

        payload = json.loads(output.getvalue())
        self.assertIn("chapter", payload)
        self.assertIn("run", payload)
        self.assertIn("validation", payload)

    def test_loop_output_json_prints_full_loop_result(self) -> None:
        case_dir = self._case_dir("loop_output_json")
        snapshot = self._snapshot(case_dir)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--dry-run",
                "--persist-dry-run",
                "--steps",
                "2",
                "--output-json",
                "--snapshot",
                str(snapshot),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
            ],
        ), contextlib.redirect_stdout(output):
            cli.main()

        payload = json.loads(output.getvalue())
        self.assertIn("session", payload)
        self.assertIn("runs", payload)
        self.assertIn("last_result", payload)
        self.assertEqual(2, payload["completed_steps"])
        self.assertEqual(2, len(payload["runs"]))
        self.assertEqual("max_steps", payload["stopped_reason"])
        self.assertEqual(payload["runs"][-1]["run"]["id"], payload["last_result"]["run"]["id"])
        self.assertIn("artifact", payload["session"])

    def test_output_run_json_prints_run_record(self) -> None:
        case_dir = self._case_dir("output_run_json")
        snapshot = self._snapshot(case_dir)
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--dry-run",
                "--output-run-json",
                "--snapshot",
                str(snapshot),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
            ],
        ), contextlib.redirect_stdout(output):
            cli.main()

        payload = json.loads(output.getvalue())
        self.assertIn("id", payload)
        self.assertIn("trace", payload)
        self.assertNotIn("run", payload)

    def test_report_runs_exposes_state_builder_source_mapping_counts(self) -> None:
        case_dir = self._case_dir("report_runs")
        run_dir = case_dir / "runs"
        run_dir.mkdir()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        run = build_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 1},
            runtime_snapshot={"chapter_index": 1},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 1,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="The shelter faced danger as the team had to choose a costly rescue.",
            validation={"ok": True, "problems": []},
            analysis={
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team had to choose."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team had to choose.",
            },
            repair_attempts=0,
            committed=True,
            snapshot_audit={
                "source": "notion-export",
                "status": "ready",
                "item_count": 1,
                "applied_count": 1,
                "skipped_count": 0,
                "deduplicated_count": 0,
                "applied_type_counts": [{"type": "location", "count": 1}],
                "skipped_type_counts": [],
                "skipped_reason_counts": [],
                "skipped_severity_counts": [],
                "skipped_blocking_count": 0,
                "applied_items": [
                    {
                        "index": 0,
                        "type": "location",
                        "name": "Safehouse",
                        "id": "manual:location:safehouse",
                        "source_mapping": {
                            "index": 0,
                            "source": "notion-export",
                            "type": "location",
                            "name": "Safehouse",
                            "page_id": "page-safehouse",
                        },
                    }
                ],
                "skipped_items": [],
            },
        )
        (run_dir / f"{run['id']}.json").write_text(
            json.dumps({"run": run}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            ["main.py", "--report-runs", "--run-dir", str(run_dir), "--report-limit", "1"],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main()

        self.assertEqual(0, exit_context.exception.code)
        payload = json.loads(output.getvalue())
        state_builder = payload["runs"][0]["state_builder"]
        self.assertEqual(1, state_builder["applied_source_mapping_count"])
        self.assertEqual(0, state_builder["skipped_source_mapping_count"])


if __name__ == "__main__":
    unittest.main()
