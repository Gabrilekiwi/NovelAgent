from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import shutil
from types import ModuleType, SimpleNamespace
import sys
import unittest
import uuid
from unittest.mock import patch

from core.autonomy.common import canonical_hash
from core.execution_provenance import build_execution_provenance
from scripts.real_autonomy_e2e import (
    OPT_IN_ENV,
    OPT_IN_PREFIX,
    PROXY_MODE_ENV,
    RealAutonomyE2EError,
    _assert_no_notion_configuration,
    _build_release_failure_report,
    _require_release_authorization,
    _resolve_release_proxy_mode,
    _safe_failure_diagnostics,
    _validate_gate_count,
    _validate_provider_configuration,
    _validate_release_report,
    main,
    run_real_autonomy_e2e,
)


class _FakeCompletions:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        messages = kwargs["messages"]
        system = str(messages[0].get("content") or "")
        if "strict fiction continuity validator" in system:
            content = json.dumps({"problems": []})
        else:
            request = json.loads(str(messages[-1]["content"]))
            beats = [
                str(item.get("text") or "")
                for item in request.get("story_project_required_beats", [])
                if isinstance(item, dict)
            ]
            ending = str(request.get("story_project_ending_pressure") or "")
            required = "。".join(item for item in [*beats, ending] if item)
            content = (
                "警报响起后，主角沿着封闭通道继续前进。"
                + required
                + "。队伍在压力中核对线索、承担代价，并在新的危险逼近时作出明确选择。"
                + "他们没有回避冲突，而是把证据逐项确认后继续行动。" * 45
                + ending
            )
        choice = SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop",
        )
        return SimpleNamespace(
            choices=[choice],
            usage={"input_tokens": 80, "output_tokens": 120},
            model=kwargs["model"],
            id=f"mock-{len(self.calls)}",
        )


class _FakeOpenAI:
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        if kwargs.get("api_key") != "unit-test-openai-key":
            raise AssertionError("test harness did not pass the explicit credential")
        if "base_url" in kwargs:
            raise AssertionError("release harness must use the official endpoint")
        self.chat = SimpleNamespace(completions=_FakeCompletions(self.calls))


class _AuthenticationError(RuntimeError):
    status_code = 401


class _FailingCompletions:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        raise _AuthenticationError("provider response body must not survive")


class _FailingOpenAI(_FakeOpenAI):
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        if kwargs.get("api_key") != "unit-test-openai-key":
            raise AssertionError("test harness did not pass the explicit credential")
        if "base_url" in kwargs:
            raise AssertionError("release harness must use the official endpoint")
        self.chat = SimpleNamespace(completions=_FailingCompletions(self.calls))


def _clean_provenance(*, dirty: bool = False):
    return build_execution_provenance(
        code_bundle_hash="a" * 64,
        code_file_count=1,
        git_commit="b" * 40,
        git_dirty=dirty,
        prompt_hashes={},
        schema_hashes={},
        dependency_versions={},
        provider="openai",
        model="unit-test-release-model",
        config={
            "configured_models": {
                "openai": "unit-test-release-model",
                "anthropic": "unit-test-anthropic-model",
            }
        },
        feature_flags={"llm_validator": True},
        python_version="3.test",
        python_implementation="CPython",
    )


class RealAutonomyE2ETest(unittest.TestCase):
    def test_release_proxy_choice_is_explicit_and_fail_closed(self) -> None:
        self.assertEqual("direct", _resolve_release_proxy_mode(environ={}))
        with self.assertRaisesRegex(
            RealAutonomyE2EError, "release_proxy_mode_required"
        ):
            _resolve_release_proxy_mode(environ={"HTTPS_PROXY": "proxy.invalid"})
        self.assertEqual(
            "inherited",
            _resolve_release_proxy_mode(
                environ={
                    "HTTPS_PROXY": "proxy.invalid",
                    PROXY_MODE_ENV: "inherit",
                }
            ),
        )
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "proxy.invalid", PROXY_MODE_ENV: "clear"},
            clear=True,
        ):
            self.assertEqual("cleared", _resolve_release_proxy_mode())
            self.assertNotIn("HTTPS_PROXY", os.environ)

    def test_failure_diagnostics_are_message_free_allowlisted_and_hash_bound(self) -> None:
        class AuthenticationError(RuntimeError):
            status_code = 401

        class WrappedProviderError(RuntimeError):
            failure_category = "provider_call_uncertain"
            retryable = False
            attempts = 1

        try:
            try:
                raise AuthenticationError("secret provider body must not survive")
            except AuthenticationError as cause:
                raise WrappedProviderError("secret wrapper body") from cause
        except WrappedProviderError as error:
            diagnostics = _safe_failure_diagnostics(error)

        self.assertEqual(
            ["WrappedProviderError", "AuthenticationError"],
            [item["exception_type"] for item in diagnostics["exception_chain"]],
        )
        self.assertEqual(401, diagnostics["exception_chain"][1]["http_status"])
        wrapped = RealAutonomyE2EError(
            "autonomy_execution_failed",
            "safe summary",
            diagnostics=diagnostics,
            retain_report=True,
        )
        report = _build_release_failure_report(chapter_count=1, error=wrapped)
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("secret provider body", serialized)
        self.assertNotIn("secret wrapper body", serialized)
        self.assertEqual(
            report["report_hash"],
            canonical_hash(report, exclude_fields=("report_hash",)),
        )

    def test_execution_failure_is_redacted_retained_and_cleans_isolation(self) -> None:
        root = Path.cwd() / ".tmp" / "r" / uuid.uuid4().hex[:8]
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        lock_temp = root.parent / f"{root.name}-locks"
        lock_temp.mkdir()
        self.addCleanup(shutil.rmtree, lock_temp)
        output = root / "failed.json"
        fake_openai = ModuleType("openai")
        _FailingOpenAI.calls = []
        fake_openai.OpenAI = _FailingOpenAI
        environment = {
            OPT_IN_ENV: f"{OPT_IN_PREFIX}:1",
            "OPENAI_API_KEY": "unit-test-openai-key",
            "OPENAI_BASE_URL": "",
            "OPENAI_MODEL": "unit-test-release-model",
            "OPENAI_MAX_OUTPUT_TOKENS": "6000",
            "OPENAI_TIMEOUT_SECONDS": "30",
            "OPENAI_MAX_RETRIES": "",
            "OPENAI_STREAM": "0",
            "PROVIDER_MAX_ATTEMPTS": "1",
            "PROVIDER_RETRY_DEADLINE_SECONDS": "30",
            "NOVELAGENT_SKIP_DOTENV": "1",
        }
        with patch.dict(os.environ, environment, clear=True), patch.dict(
            sys.modules, {"openai": fake_openai}
        ), patch(
            "tempfile.tempdir", str(lock_temp)
        ), patch(
            "scripts.real_autonomy_e2e.capture_execution_provenance",
            return_value=_clean_provenance(),
        ), patch(
            "core.engine.executor._capture_execution_provenance_cached",
            return_value=_clean_provenance(),
        ):
            with self.assertRaisesRegex(
                RealAutonomyE2EError, "autonomy_execution_failed"
            ):
                run_real_autonomy_e2e(
                    chapter_count=1,
                    output_path=output,
                    confirmed=True,
                    work_parent=root,
                )

        retained = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(retained["ok"])
        self.assertTrue(retained["redacted"])
        self.assertTrue(retained["cleanup_completed"])
        self.assertEqual("runner_execute", retained["diagnostics"]["phase"])
        self.assertEqual("direct", retained["gate"]["proxy_mode"])
        self.assertEqual(1, retained["diagnostics"]["intent_count"])
        self.assertEqual(1, retained["diagnostics"]["receipt_count"])
        self.assertEqual(0, retained["diagnostics"]["uncertain_intent_count"])
        self.assertIn(
            401,
            [
                item.get("http_status")
                for item in retained["diagnostics"]["exception_chain"]
            ],
        )
        self.assertNotIn("provider response body", output.read_text(encoding="utf-8"))
        self.assertGreater(len(_FailingOpenAI.calls), 0)
        self.assertEqual(["failed.json"], sorted(item.name for item in root.iterdir()))

    def test_gate_count_accepts_only_release_tiers(self) -> None:
        for count in (1, 4, 10, 20, 50, 100):
            self.assertEqual(count, _validate_gate_count(count))
        for count in (0, 2, 3, 5, 11, 19, True):
            with self.subTest(count=count):
                with self.assertRaisesRegex(
                    RealAutonomyE2EError, "release_gate_count_invalid"
                ):
                    _validate_gate_count(count)

    def test_authorization_is_two_factor_count_bound_and_requires_direct_key(self) -> None:
        sentinel = f"{OPT_IN_PREFIX}:4"
        with self.assertRaisesRegex(
            RealAutonomyE2EError, "real_provider_opt_in_required"
        ):
            _require_release_authorization(
                4,
                confirmed=False,
                environ={OPT_IN_ENV: sentinel, "OPENAI_API_KEY": "key"},
            )
        with self.assertRaisesRegex(
            RealAutonomyE2EError, "real_provider_opt_in_required"
        ):
            _require_release_authorization(
                4,
                confirmed=True,
                environ={OPT_IN_ENV: f"{OPT_IN_PREFIX}:10", "OPENAI_API_KEY": "key"},
            )
        with self.assertRaisesRegex(RealAutonomyE2EError, "openai_not_configured"):
            _require_release_authorization(
                4,
                confirmed=True,
                environ={OPT_IN_ENV: sentinel, "OPENAI_API_KEY": ""},
            )
        _require_release_authorization(
            4,
            confirmed=True,
            environ={OPT_IN_ENV: sentinel, "OPENAI_API_KEY": "key"},
        )

    def test_every_notion_setting_and_flag_is_rejected(self) -> None:
        for name in (
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOVELAGENT_NOTION_DATABASE_ID",
            "NOTION_TIMEOUT_SECONDS",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    RealAutonomyE2EError, "notion_configuration_forbidden"
                ):
                    _assert_no_notion_configuration(environ={name: "configured"})
        with self.assertRaisesRegex(
            RealAutonomyE2EError, "notion_configuration_forbidden"
        ):
            _assert_no_notion_configuration(
                environ={}, argv=["--notion-write", "--chapters", "1"]
            )
        with patch.dict(os.environ, {}, clear=True), patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            self.assertEqual(
                2,
                main(
                    [
                        "--chapters",
                        "1",
                        "--out",
                        "unused.json",
                        "--notion-sync",
                    ]
                ),
            )
            self.assertIn("notion_configuration_forbidden", stderr.getvalue())

    def test_cli_and_official_endpoint_checks_fail_closed_before_provider_use(self) -> None:
        for count in (1, 4, 10, 20, 50):
            with self.subTest(count=count), patch.dict(
                os.environ, {}, clear=True
            ), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(
                    2,
                    main(
                        [
                            "--chapters",
                            str(count),
                            "--out",
                            "unused.json",
                            "--confirm-real-provider-calls",
                        ]
                    ),
                )
                self.assertIn("real_provider_opt_in_required", stderr.getvalue())
        with patch.dict(os.environ, {}, clear=True), patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            self.assertEqual(
                2,
                main(
                    [
                        "--chapters",
                        "2",
                        "--out",
                        "unused.json",
                        "--confirm-real-provider-calls",
                    ]
                ),
            )
            self.assertIn("release_gate_count_invalid", stderr.getvalue())

        safe = SimpleNamespace(
            openai_api_key="key",
            openai_base_url=None,
            openai_timeout_seconds=30,
            openai_max_output_tokens=6000,
            provider_max_attempts=1,
            openai_max_retries=0,
        )
        _validate_provider_configuration(safe)
        with self.assertRaisesRegex(
            RealAutonomyE2EError, "official_openai_endpoint_required"
        ):
            _validate_provider_configuration(
                SimpleNamespace(**{**safe.__dict__, "openai_base_url": "https://proxy.invalid"})
            )

    def test_dirty_release_and_existing_report_target_fail_before_provider(self) -> None:
        root = Path.cwd() / ".tmp" / "r" / uuid.uuid4().hex[:8]
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        environment = {
            OPT_IN_ENV: f"{OPT_IN_PREFIX}:1",
            "OPENAI_API_KEY": "unit-test-openai-key",
            "OPENAI_BASE_URL": "",
            "OPENAI_MODEL": "unit-test-release-model",
            "OPENAI_MAX_OUTPUT_TOKENS": "6000",
            "OPENAI_TIMEOUT_SECONDS": "30",
            "OPENAI_MAX_RETRIES": "",
            "OPENAI_STREAM": "0",
            "PROVIDER_MAX_ATTEMPTS": "1",
            "PROVIDER_RETRY_DEADLINE_SECONDS": "30",
            "NOVELAGENT_SKIP_DOTENV": "1",
        }
        dirty_output = root / "dirty.json"
        _FakeOpenAI.calls = []
        with patch.dict(os.environ, environment, clear=True), patch(
            "scripts.real_autonomy_e2e.capture_execution_provenance",
            return_value=_clean_provenance(dirty=True),
        ):
            with self.assertRaisesRegex(
                RealAutonomyE2EError, "release_worktree_not_clean"
            ):
                run_real_autonomy_e2e(
                    chapter_count=1,
                    output_path=dirty_output,
                    confirmed=True,
                    work_parent=root,
                )
        self.assertFalse(dirty_output.exists())
        self.assertEqual([], _FakeOpenAI.calls)

        existing = root / "existing.json"
        existing.write_text("operator-owned", encoding="utf-8")
        with patch.dict(os.environ, environment, clear=True), patch(
            "scripts.real_autonomy_e2e.capture_execution_provenance",
            return_value=_clean_provenance(),
        ):
            with self.assertRaisesRegex(
                RealAutonomyE2EError, "release_report_target_unavailable"
            ):
                run_real_autonomy_e2e(
                    chapter_count=1,
                    output_path=existing,
                    confirmed=True,
                    work_parent=root,
                )
        self.assertEqual("operator-owned", existing.read_text(encoding="utf-8"))
        self.assertEqual([], _FakeOpenAI.calls)

    def test_one_chapter_mock_provider_runs_full_gate_without_billing(self) -> None:
        root = Path.cwd() / ".tmp" / "r" / uuid.uuid4().hex[:8]
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        lock_temp = root.parent / f"{root.name}-locks"
        lock_temp.mkdir()
        self.addCleanup(shutil.rmtree, lock_temp)
        output = root / "redacted-report.json"
        fake_openai = ModuleType("openai")
        _FakeOpenAI.calls = []
        fake_openai.OpenAI = _FakeOpenAI
        environment = {
            OPT_IN_ENV: f"{OPT_IN_PREFIX}:1",
            "OPENAI_API_KEY": "unit-test-openai-key",
            "OPENAI_BASE_URL": "",
            "OPENAI_MODEL": "unit-test-release-model",
            "OPENAI_MAX_OUTPUT_TOKENS": "6000",
            "OPENAI_TIMEOUT_SECONDS": "30",
            "OPENAI_MAX_RETRIES": "",
            "OPENAI_STREAM": "0",
            "PROVIDER_MAX_ATTEMPTS": "1",
            "PROVIDER_RETRY_DEADLINE_SECONDS": "30",
            "NOVELAGENT_SKIP_DOTENV": "1",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "tempfile.tempdir", str(lock_temp)
        ):
            for name in list(os.environ):
                if "NOTION" in name.upper():
                    os.environ.pop(name, None)
            with patch.dict(sys.modules, {"openai": fake_openai}), patch(
                "core.delivery.create_database_page",
                side_effect=AssertionError("Notion create must never be called"),
            ) as notion_create, patch(
                "core.delivery.query_database_pages",
                side_effect=AssertionError("Notion query must never be called"),
            ) as notion_query, patch(
                "scripts.real_autonomy_e2e.capture_execution_provenance",
                return_value=_clean_provenance(),
            ), patch(
                "core.engine.executor._capture_execution_provenance_cached",
                return_value=_clean_provenance(),
            ):
                report = run_real_autonomy_e2e(
                    chapter_count=1,
                    output_path=output,
                    confirmed=True,
                    work_parent=root,
                )

        self.assertTrue(report["ok"])
        self.assertTrue(report["redacted"])
        self.assertEqual(1, report["gate"]["requested_chapters"])
        self.assertEqual(1, len(report["chapters"]))
        self.assertEqual(1, report["counts"]["required_file_deliveries"])
        self.assertGreaterEqual(report["chapters"][0]["prose_chars"], 3_000)
        self.assertLessEqual(report["chapters"][0]["prose_chars"], 4_500)
        self.assertGreater(report["slo"]["logical_model_calls"], 0)
        self.assertEqual(0, report["slo"]["provider_transport_retries"])
        self.assertEqual(0, report["slo"]["system_failures"])
        self.assertEqual("1.1", report["schema_version"])
        self.assertEqual("direct", report["gate"]["proxy_mode"])
        self.assertFalse(report["provenance"]["git_dirty"])
        self.assertEqual("b" * 40, report["provenance"]["git_commit"])
        self.assertEqual("a" * 64, report["provenance"]["code_bundle_hash"])
        self.assertEqual(1, len(report["provider"]["actual_model_hashes"]))
        self.assertGreater(len(_FakeOpenAI.calls), 0)
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(["redacted-report.json"], sorted(item.name for item in root.iterdir()))
        notion_create.assert_not_called()
        notion_query.assert_not_called()

        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "unit-test-openai-key",
            "unit-test-release-model",
            str(root.resolve()),
            "chapter_text",
            "prompt",
            "request_id",
        ):
            self.assertNotIn(forbidden, serialized)

        tampered = copy.deepcopy(report)
        tampered["counts"]["event_batches"] = 99
        with self.assertRaisesRegex(
            RealAutonomyE2EError, "release_report_hash_mismatch"
        ):
            _validate_release_report(tampered)


if __name__ == "__main__":
    unittest.main()
