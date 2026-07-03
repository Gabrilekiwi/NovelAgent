from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.contracts import ModelCallError
from core.runtime_paths import init_runtime_state
from core.schema import SchemaValidationError, validate_schema
from scripts.provider_smoke import (
    _build_diagnostics,
    _normalize_limits,
    _provider_failure_result,
    _required_check_summary,
    _retry_provider_check,
    _smoke_claude,
    _smoke_notion,
    _smoke_openai,
    _work_dir,
)


class ProviderSmokeScriptTest(unittest.TestCase):
    def test_provider_smoke_can_skip_missing_config_for_local_diagnostics(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_provider_smoke" / uuid.uuid4().hex
        env = dict(os.environ)
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_BASE_URL",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_USER_AGENT",
            "CLAUDE_MODEL",
            "ANTHROPIC_MODEL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOVELAGENT_NOTION_DATABASE_ID",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            env[name] = ""

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/provider_smoke.py",
                "--allow-missing",
                "--ignore-dotenv",
                "--work-dir",
                str(work_dir),
            ],
            cwd=Path.cwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue((work_dir / "provider_smoke_report.json").exists())
        self.assertEqual(["openai", "claude", "notion"], report["request"]["providers"])
        self.assertFalse(report["request"]["notion_write"])
        self.assertTrue(report["request"]["allow_missing"])
        self.assertTrue(report["request"]["ignore_dotenv"])
        self.assertFalse(report["request"]["no_proxy"])
        self.assertEqual(1, report["limits"]["steps"])
        self.assertEqual(800, report["limits"]["max_output_tokens"])
        self.assertEqual(0, report["limits"]["openai_max_retries"])
        self.assertEqual(1, report["limits"]["openai_scene_limit"])
        self.assertEqual(0, report["limits"]["retries"])
        self.assertEqual(1.0, report["limits"]["retry_delay_seconds"])
        self.assertIsNone(report["limits"]["openai_model"])
        self.assertIsNone(report["limits"]["openai_base_url"])
        self.assertIsNone(report["limits"]["claude_model"])
        self.assertEqual("missing", report["limits"]["claude_base_url"])
        self.assertEqual("missing", report["limits"]["claude_user_agent"])
        self.assertIsNone(report["limits"]["claude_max_tokens"])
        self.assertFalse(report["limits"]["require_all_checks"])
        self.assertEqual("missing", report["config_status"]["openai"]["api_key"])
        self.assertEqual("gpt-4.1-mini", report["config_status"]["openai"]["model"])
        self.assertEqual("sdk_default", report["config_status"]["openai"]["base_url"])
        self.assertEqual(0, report["config_status"]["openai"]["max_retries"])
        self.assertFalse(report["config_status"]["openai"]["configured"])
        self.assertEqual("missing", report["config_status"]["claude"]["api_key"])
        self.assertEqual("sdk_default", report["config_status"]["claude"]["base_url"])
        self.assertEqual("missing", report["config_status"]["claude"]["user_agent"])
        self.assertEqual("missing", report["config_status"]["claude"]["model_status"])
        self.assertFalse(report["config_status"]["claude"]["configured"])
        self.assertEqual("missing", report["config_status"]["notion"]["api_key"])
        self.assertEqual("missing", report["config_status"]["notion"]["database_id"])
        self.assertFalse(report["config_status"]["notion"]["configured"])
        self.assertEqual("missing", report["config_status"]["proxy"]["http_proxy"])
        self.assertEqual("missing", report["config_status"]["proxy"]["https_proxy"])
        self.assertEqual("missing", report["config_status"]["proxy"]["all_proxy"])
        self.assertEqual([], report["config_status"]["proxy"]["proxy_endpoints"])
        self.assertEqual("skipped", report["providers"]["openai"]["status"])
        self.assertEqual("skipped", report["providers"]["claude"]["status"])
        self.assertEqual("skipped", report["providers"]["notion"]["status"])
        self.assertFalse(report["required_checks_ok"])
        self.assertEqual("incomplete", report["diagnostics"]["status"])
        self.assertEqual(
            [
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_MODEL",
                "CLAUDE_MODEL",
                "NOTION_API_KEY",
                "NOTION_DATABASE_ID",
                "NOVELAGENT_NOTION_DATABASE_ID",
                "OPENAI_API_KEY",
            ],
            report["diagnostics"]["missing_config"],
        )
        self.assertEqual(
            [
                ("openai", ("OPENAI_API_KEY",)),
                ("claude", ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
                ("claude", ("ANTHROPIC_MODEL", "CLAUDE_MODEL")),
                ("notion", ("NOTION_API_KEY",)),
                ("notion", ("NOTION_DATABASE_ID", "NOVELAGENT_NOTION_DATABASE_ID")),
            ],
            [
                (group["provider"], tuple(group["any_of"]))
                for group in report["diagnostics"]["missing_config_groups"]
            ],
        )
        self.assertEqual(
            [
                "openai_api_key",
                "anthropic_api_key",
                "claude_model",
                "notion_api_key",
                "notion_database_id",
            ],
            [group["requirement"] for group in report["diagnostics"]["missing_config_groups"]],
        )
        self.assertEqual(6, len(report["diagnostics"]["skipped_checks"]))
        self.assertEqual([], report["diagnostics"]["failed_checks"])
        self.assertEqual([], report["diagnostics"]["unrequested_checks"])
        self.assertEqual(6, len(report["required_checks"]))
        self.assertEqual(
            {
                ("openai", "director", "skipped"),
                ("openai", "chapter_generation", "skipped"),
                ("claude", "polish", "skipped"),
                ("notion", "read", "skipped"),
                ("notion", "writeback", "skipped"),
                ("notion", "readback", "skipped"),
            },
            {(item["provider"], item["check"], item["status"]) for item in report["required_checks"]},
        )
        self.assertIs(report, validate_schema(report, "provider_smoke_report.schema.json"))

    def test_provider_smoke_no_proxy_clears_proxy_environment_for_report(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_provider_smoke" / uuid.uuid4().hex
        env = dict(os.environ)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOVELAGENT_NOTION_DATABASE_ID",
        ):
            env[name] = ""
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["ALL_PROXY"] = "http://127.0.0.1:9"

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/provider_smoke.py",
                "--allow-missing",
                "--ignore-dotenv",
                "--no-proxy",
                "--work-dir",
                str(work_dir),
            ],
            cwd=Path.cwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["request"]["no_proxy"])
        self.assertEqual("missing", report["config_status"]["proxy"]["http_proxy"])
        self.assertEqual("missing", report["config_status"]["proxy"]["https_proxy"])
        self.assertEqual("missing", report["config_status"]["proxy"]["all_proxy"])
        self.assertEqual([], report["config_status"]["proxy"]["proxy_endpoints"])
        self.assertIs(report, validate_schema(report, "provider_smoke_report.schema.json"))

    def test_provider_smoke_require_all_checks_fails_when_required_checks_do_not_pass(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_provider_smoke" / uuid.uuid4().hex
        env = dict(os.environ)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOVELAGENT_NOTION_DATABASE_ID",
        ):
            env[name] = ""

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/provider_smoke.py",
                "--allow-missing",
                "--ignore-dotenv",
                "--require-all-checks",
                "--work-dir",
                str(work_dir),
            ],
            cwd=Path.cwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        self.assertFalse(report["required_checks_ok"])
        self.assertTrue(report["limits"]["require_all_checks"])
        self.assertEqual("incomplete", report["diagnostics"]["status"])

    def test_provider_smoke_missing_config_fails_without_allow_missing(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_provider_smoke" / uuid.uuid4().hex
        env = dict(os.environ)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOVELAGENT_NOTION_DATABASE_ID",
        ):
            env[name] = ""

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/provider_smoke.py",
                "--ignore-dotenv",
                "--work-dir",
                str(work_dir),
            ],
            cwd=Path.cwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["request"]["allow_missing"])
        self.assertEqual("failed", report["diagnostics"]["status"])
        self.assertIn("missing_config_groups", report["providers"]["notion"])
        self.assertEqual(
            ["notion_database_id"],
            [
                group["requirement"]
                for group in report["providers"]["notion"]["missing_config_groups"]
                if group["any_of"] == ["NOTION_DATABASE_ID", "NOVELAGENT_NOTION_DATABASE_ID"]
            ],
        )
        self.assertIs(report, validate_schema(report, "provider_smoke_report.schema.json"))

    def test_provider_smoke_records_requested_provider_subset_and_notion_write_intent(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_provider_smoke" / uuid.uuid4().hex
        env = dict(os.environ)
        env["NOTION_API_KEY"] = ""
        env["NOTION_DATABASE_ID"] = ""
        env["NOVELAGENT_NOTION_DATABASE_ID"] = ""

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/provider_smoke.py",
                "--providers",
                "notion",
                "--notion-write",
                "--allow-missing",
                "--ignore-dotenv",
                "--work-dir",
                str(work_dir),
            ],
            cwd=Path.cwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(["notion"], report["request"]["providers"])
        self.assertTrue(report["request"]["notion_write"])
        self.assertEqual("not_requested", next(item for item in report["required_checks"] if item["provider"] == "openai")["status"])
        self.assertIs(report, validate_schema(report, "provider_smoke_report.schema.json"))

    def test_provider_smoke_default_work_dir_uses_ignored_runtime_tree(self) -> None:
        with patch("scripts.provider_smoke._timestamp", return_value="20260102T030405000000Z"):
            path = _work_dir(None)

        self.assertEqual(Path.cwd() / ".tmp" / "runtime" / "provider_smoke" / "20260102T030405000000Z", path)

    def test_provider_smoke_normalizes_effective_limits_before_reporting(self) -> None:
        args = SimpleNamespace(
            steps=99,
            max_input_chars=0,
            max_output_tokens=-10,
            openai_max_retries=-1,
            openai_scene_limit=0,
            request_timeout=0,
            retries=-1,
            retry_delay_seconds=-2.5,
            openai_model="gpt-test",
            no_openai_base_url=True,
            openai_base_url=None,
            claude_model="claude-test",
            claude_base_url="https://claude.example.test",
            claude_user_agent="claude-cli/1.0 test",
            claude_max_tokens=0,
            require_all_checks=True,
        )

        limits = _normalize_limits(args)

        self.assertEqual(1, limits["steps"])
        self.assertEqual(1, limits["max_input_chars"])
        self.assertEqual(1, limits["max_output_tokens"])
        self.assertEqual(0, limits["openai_max_retries"])
        self.assertEqual(1, limits["openai_scene_limit"])
        self.assertEqual(1, limits["request_timeout"])
        self.assertEqual(0, limits["retries"])
        self.assertEqual(0.0, limits["retry_delay_seconds"])
        self.assertEqual("sdk_default", limits["openai_base_url"])
        self.assertEqual("set", limits["claude_base_url"])
        self.assertEqual("set", limits["claude_user_agent"])
        self.assertEqual(1, limits["claude_max_tokens"])
        self.assertTrue(limits["require_all_checks"])

    def test_provider_smoke_preserves_model_call_diagnostics(self) -> None:
        result = _provider_failure_result(
            ModelCallError(
                "OpenAI chat completion failed: Request timed out.",
                provider="openai",
                stage="director_decision",
                model="gpt-test",
                cause=TimeoutError("timeout"),
            )
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual("ModelCallError", result["error_type"])
        self.assertEqual("timeout", result["failure_category"])
        self.assertTrue(result["retryable"])
        self.assertEqual(
            {
                "provider": "openai",
                "stage": "director_decision",
                "model": "gpt-test",
                "cause_type": "TimeoutError",
                "message": "OpenAI chat completion failed: Request timed out.",
                "failure_category": "timeout",
                "retryable": True,
            },
            result["model_call"],
        )

    def test_provider_smoke_classifies_transient_provider_errors(self) -> None:
        result = _provider_failure_result(
            ModelCallError(
                "OpenAI chat completion failed: Error code: 503 - Service temporarily unavailable.",
                provider="openai",
                stage="chapter_generation",
                model="gpt-test",
                cause=RuntimeError("InternalServerError"),
            )
        )

        self.assertEqual("transient_provider_error", result["failure_category"])
        self.assertTrue(result["retryable"])

    def test_provider_smoke_builds_failure_diagnostics_from_required_checks(self) -> None:
        diagnostics = _build_diagnostics(
            {
                "ok": False,
                "required_checks_ok": False,
                "required_checks": [
                    {
                        "provider": "openai",
                        "check": "director",
                        "ok": False,
                        "status": "failed",
                        "requested": True,
                        "attempts": 2,
                        "error_type": "ModelCallError",
                        "message": "OpenAI chat completion failed: Connection error.",
                        "failure_category": "connection",
                        "retryable": True,
                    },
                    {
                        "provider": "notion",
                        "check": "read",
                        "ok": False,
                        "status": "skipped",
                        "requested": True,
                        "reason": "NOTION_DATABASE_ID or NOVELAGENT_NOTION_DATABASE_ID is required.",
                    },
                ],
            }
        )

        self.assertEqual("failed", diagnostics["status"])
        self.assertEqual(["NOTION_DATABASE_ID", "NOVELAGENT_NOTION_DATABASE_ID"], diagnostics["missing_config"])
        self.assertEqual([], diagnostics["missing_config_groups"])
        self.assertEqual("director", diagnostics["failed_checks"][0]["check"])
        self.assertEqual(2, diagnostics["failed_checks"][0]["attempts"])
        self.assertEqual("connection", diagnostics["failed_checks"][0]["failure_category"])
        self.assertTrue(diagnostics["failed_checks"][0]["retryable"])
        self.assertEqual("read", diagnostics["skipped_checks"][0]["check"])
        self.assertEqual([], diagnostics["unrequested_checks"])

    def test_provider_smoke_diagnostics_separate_unrequested_checks(self) -> None:
        diagnostics = _build_diagnostics(
            {
                "ok": False,
                "required_checks_ok": False,
                "required_checks": [
                    {
                        "provider": "openai",
                        "check": "director",
                        "ok": False,
                        "status": "failed",
                        "requested": True,
                        "message": "OpenAI chat completion failed: Connection error.",
                    },
                    {
                        "provider": "claude",
                        "check": "polish",
                        "ok": False,
                        "status": "not_requested",
                        "requested": False,
                    },
                ],
            }
        )

        self.assertEqual(["director"], [item["check"] for item in diagnostics["failed_checks"]])
        self.assertEqual(["polish"], [item["check"] for item in diagnostics["unrequested_checks"]])
        self.assertEqual([], diagnostics["missing_config_groups"])
        self.assertEqual([], diagnostics["skipped_checks"])

    def test_provider_smoke_labels_openai_base_url_control(self) -> None:
        from argparse import Namespace

        from scripts.provider_smoke import _base_url_limit_label

        self.assertEqual("sdk_default", _base_url_limit_label(Namespace(no_openai_base_url=True, openai_base_url=None)))
        self.assertEqual("custom", _base_url_limit_label(Namespace(no_openai_base_url=False, openai_base_url="https://example.test/v1")))
        self.assertIsNone(_base_url_limit_label(Namespace(no_openai_base_url=False, openai_base_url=None)))

    def test_provider_smoke_schema_rejects_missing_limits(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "id": "provider_smoke_test",
                    "work_dir": ".tmp/test",
                    "providers": {},
                    "ok": True,
                    "runtime": {},
                    "report_path": ".tmp/test/provider_smoke_report.json",
                    "required_checks": [],
                },
                "provider_smoke_report.schema.json",
            )

    def test_required_check_summary_marks_unrequested_providers(self) -> None:
        summary = _required_check_summary(
            {
                "openai": {
                    "ok": False,
                    "status": "failed",
                    "checks": {
                        "director": {"ok": True, "status": "passed", "attempts": 1},
                        "chapter_generation": {"ok": False, "status": "failed", "attempts": 2, "error_type": "ModelCallError"},
                    },
                }
            }
        )

        self.assertEqual(6, len(summary))
        by_key = {(item["provider"], item["check"]): item for item in summary}
        self.assertEqual("passed", by_key[("openai", "director")]["status"])
        self.assertEqual(1, by_key[("openai", "director")]["attempts"])
        self.assertEqual("failed", by_key[("openai", "chapter_generation")]["status"])
        self.assertEqual("not_requested", by_key[("claude", "polish")]["status"])
        self.assertFalse(by_key[("notion", "read")]["requested"])

    def test_retry_provider_check_records_successful_attempt_count(self) -> None:
        calls = {"count": 0}

        def flaky() -> dict[str, object]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary")
            return {"ok": True, "status": "passed"}

        result = _retry_provider_check(flaky, retries=1)

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["attempts"])

    def test_retry_provider_check_sleeps_between_failed_attempts(self) -> None:
        calls = {"count": 0}

        def flaky() -> dict[str, object]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary")
            return {"ok": True, "status": "passed"}

        with patch("scripts.provider_smoke.time.sleep") as sleep:
            result = _retry_provider_check(flaky, retries=1, retry_delay_seconds=0.25)

        self.assertTrue(result["ok"])
        sleep.assert_called_once_with(0.25)

    def test_retry_provider_check_records_final_failed_attempt_count(self) -> None:
        def always_fails() -> dict[str, object]:
            raise RuntimeError("still failing")

        result = _retry_provider_check(always_fails, retries=2)

        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["status"])
        self.assertEqual(3, result["attempts"])

    def test_openai_smoke_continues_generation_check_after_director_failure(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_provider_smoke" / uuid.uuid4().hex
        init_runtime_state(
            snapshot_target=work_dir / "snapshot.json",
            memory_target=work_dir / "notion_memory.json",
            overwrite=True,
        )

        class FailingDirector:
            def __init__(self, **_: object) -> None:
                pass

            def __call__(self, *_: object) -> dict[str, object]:
                raise ModelCallError(
                    "OpenAI chat completion failed: Request timed out.",
                    provider="openai",
                    stage="director_decision",
                    model="gpt-test",
                    cause=TimeoutError("timeout"),
                )

        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.ModelDirector",
            FailingDirector,
        ), patch("scripts.provider_smoke.chat_completion", return_value="Scene text.") as completion:
            get_config.return_value = SimpleNamespace(
                openai_api_key="test-key",
                openai_model="gpt-test",
                openai_max_output_tokens=123,
            )

            result = _smoke_openai(work_dir, max_input_chars=500, scene_limit=1)

        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["checks"]["director"]["status"])
        self.assertEqual("passed", result["checks"]["chapter_generation"]["status"])
        self.assertEqual("rule_fallback", result["checks"]["chapter_generation"]["director_source"])
        self.assertEqual("smoke_compact_scene", result["checks"]["chapter_generation"]["generation_plan_source"])
        self.assertEqual(1, result["checks"]["chapter_generation"]["scene_limit"])
        completion.assert_called_once()
        self.assertEqual("chapter_generation", completion.call_args.kwargs["stage"])
        self.assertEqual(123, completion.call_args.kwargs["max_tokens"])

    def test_claude_smoke_reports_polish_subcheck(self) -> None:
        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.polish_chapter",
            return_value="polished chapter",
        ) as polish:
            get_config.return_value = SimpleNamespace(
                anthropic_api_key="test-anthropic",
                claude_model="claude-test",
                claude_max_tokens=77,
            )

            result = _smoke_claude()

        self.assertTrue(result["ok"])
        self.assertEqual("passed", result["checks"]["polish"]["status"])
        self.assertEqual("claude-test", result["checks"]["polish"]["model"])
        self.assertEqual(77, result["checks"]["polish"]["max_tokens"])
        polish.assert_called_once()

    def test_claude_smoke_reports_all_missing_config(self) -> None:
        with patch("scripts.provider_smoke.get_config") as get_config:
            get_config.return_value = SimpleNamespace(
                anthropic_api_key=None,
                claude_model=None,
                claude_max_tokens=77,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "one of ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN; one of ANTHROPIC_MODEL, CLAUDE_MODEL",
            ):
                _smoke_claude()

    def test_claude_smoke_retries_polish_subcheck(self) -> None:
        calls = {"count": 0}

        def flaky_polish(*_: object, **__: object) -> str:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary claude failure")
            return "polished chapter"

        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.polish_chapter",
            side_effect=flaky_polish,
        ):
            get_config.return_value = SimpleNamespace(
                anthropic_api_key="test-anthropic",
                claude_model="claude-test",
                claude_max_tokens=77,
            )

            result = _smoke_claude(retries=1)

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["checks"]["polish"]["attempts"])

    def test_notion_smoke_reports_read_and_skips_write_when_not_requested(self) -> None:
        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.query_database_pages",
            return_value=[{"id": "page-1"}],
        ):
            get_config.return_value = SimpleNamespace(
                notion_api_key="test-notion",
                notion_database_id="db-test",
            )

            result = _smoke_notion(write=False)

        self.assertTrue(result["ok"])
        self.assertEqual("passed", result["checks"]["read"]["status"])
        self.assertEqual(1, result["checks"]["read"]["read_count"])
        self.assertEqual("skipped", result["checks"]["writeback"]["status"])
        self.assertEqual("skipped", result["checks"]["readback"]["status"])

    def test_notion_smoke_reports_all_missing_config(self) -> None:
        with patch("scripts.provider_smoke.get_config") as get_config:
            get_config.return_value = SimpleNamespace(
                notion_api_key=None,
                notion_database_id=None,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "NOTION_API_KEY; one of NOTION_DATABASE_ID, NOVELAGENT_NOTION_DATABASE_ID",
            ):
                _smoke_notion(write=False)

    def test_notion_smoke_retries_read_subcheck(self) -> None:
        calls = {"count": 0}

        def flaky_read(*_: object, **__: object) -> list[dict[str, object]]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary notion read failure")
            return [{"id": "page-1"}]

        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.query_database_pages",
            side_effect=flaky_read,
        ):
            get_config.return_value = SimpleNamespace(
                notion_api_key="test-notion",
                notion_database_id="db-test",
            )

            result = _smoke_notion(write=False, retries=1)

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["checks"]["read"]["attempts"])
        self.assertEqual("skipped", result["checks"]["writeback"]["status"])

    def test_notion_smoke_skips_writeback_when_read_fails(self) -> None:
        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.query_database_pages",
            side_effect=RuntimeError("read failed"),
        ), patch("scripts.provider_smoke.NotionMemoryWriter") as writer:
            get_config.return_value = SimpleNamespace(
                notion_api_key="test-notion",
                notion_database_id="db-test",
            )

            result = _smoke_notion(write=True)

        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["checks"]["read"]["status"])
        self.assertEqual("skipped", result["checks"]["writeback"]["status"])
        self.assertEqual("skipped", result["checks"]["readback"]["status"])
        writer.assert_not_called()

    def test_notion_smoke_reports_writeback_and_readback_subchecks(self) -> None:
        class FakeWriter:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def __call__(self, updates: list[dict[str, object]]) -> dict[str, object]:
                return {
                    "written": 1,
                    "skipped": 0,
                    "item_mappings": [{"memory_id": updates[0]["id"], "status": "written"}],
                    "verification": {"status": "verified", "checked": 1, "passed": 1, "failed": 0},
                }

        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.query_database_pages",
            return_value=[{"id": "page-1"}],
        ), patch("scripts.provider_smoke.NotionMemoryWriter", FakeWriter):
            get_config.return_value = SimpleNamespace(
                notion_api_key="test-notion",
                notion_database_id="db-test",
            )

            result = _smoke_notion(write=True)

        self.assertTrue(result["ok"])
        self.assertEqual("passed", result["checks"]["read"]["status"])
        self.assertEqual("passed", result["checks"]["writeback"]["status"])
        self.assertEqual("passed", result["checks"]["readback"]["status"])
        self.assertEqual("verified", result["checks"]["readback"]["verification"]["status"])

    def test_notion_smoke_does_not_retry_writeback(self) -> None:
        class FailingWriter:
            calls = 0

            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def __call__(self, updates: list[dict[str, object]]) -> dict[str, object]:
                type(self).calls += 1
                raise RuntimeError("write failed")

        with patch("scripts.provider_smoke.get_config") as get_config, patch(
            "scripts.provider_smoke.query_database_pages",
            return_value=[{"id": "page-1"}],
        ), patch("scripts.provider_smoke.NotionMemoryWriter", FailingWriter):
            get_config.return_value = SimpleNamespace(
                notion_api_key="test-notion",
                notion_database_id="db-test",
            )

            result = _smoke_notion(write=True, retries=3)

        self.assertFalse(result["ok"])
        self.assertEqual(1, FailingWriter.calls)
        self.assertEqual(1, result["checks"]["writeback"]["attempts"])
        self.assertEqual("skipped", result["checks"]["readback"]["status"])


if __name__ == "__main__":
    unittest.main()
