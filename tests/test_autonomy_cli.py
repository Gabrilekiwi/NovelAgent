from __future__ import annotations

import argparse
import copy
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import main as cli
from core.autonomy.cli import autonomy_command_requested, run_autonomy_command
from tests.test_autonomy_plans import source_snapshot, trusted_profiles, workspace_case


def command_args(**overrides):
    values = {
        "instruction": None,
        "execute_plan": None,
        "session_status": None,
        "resume_session": None,
        "cancel_session": None,
        "abandon_session": None,
        "trusted_profiles": None,
        "autonomy_root_map": None,
        "dry_run": False,
        "persist_dry_run": False,
        "story_project": "auto",
        "_resolved_story_project_root": Path.cwd(),
        "run_dir": ".tmp/runtime/runs",
        "notion_sync": False,
        "notion_memory": False,
        "memory_writeback": "none",
        "steps": 1,
        "_steps_explicit": False,
        "reconcile_deliveries": False,
        "resolve_delivery": None,
        "inspect_delivery": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class AutonomyCliTest(unittest.TestCase):
    def test_parser_accepts_preview_execute_and_session_commands(self) -> None:
        parser = cli.build_parser()
        preview = parser.parse_args(
            [
                "--story-project",
                "auto",
                "--trusted-profiles",
                "profiles.json",
                "--instruction",
                "写 2章",
            ]
        )
        self.assertEqual("写 2章", preview.instruction)
        status = parser.parse_args(["--story-project", "auto", "--session-status"])
        self.assertEqual("latest", status.session_status)
        self.assertTrue(autonomy_command_requested(status))
        execute = parser.parse_args(
            [
                "--story-project",
                "auto",
                "--trusted-profiles",
                "profiles.json",
                "--execute-plan",
                "plan.json",
            ]
        )
        self.assertEqual("plan.json", execute.execute_plan)

    def test_command_validation_rejects_ambiguous_or_external_execution(self) -> None:
        with self.assertRaisesRegex(ValueError, "choose only one"):
            autonomy_command_requested(
                command_args(
                    instruction="写一章",
                    session_status="latest",
                    trusted_profiles="profiles.json",
                )
            )
        with self.assertRaisesRegex(ValueError, "Notion execution"):
            autonomy_command_requested(
                command_args(
                    instruction="写一章",
                    trusted_profiles="profiles.json",
                    notion_sync=True,
                )
            )

    def test_dry_execution_requires_explicit_persistence_consent(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires explicit --persist-dry-run"):
            autonomy_command_requested(
                command_args(
                    execute_plan="plan.json",
                    trusted_profiles="profiles.json",
                    autonomy_root_map="operator-roots.json",
                    dry_run=True,
                )
            )

    def test_steps_is_fail_closed_at_storyproject_autonomy_entry(self) -> None:
        parser = cli.build_parser()
        parsed = cli.parse_arguments(
            parser,
            [
                "--story-project",
                "auto",
                "--trusted-profiles",
                "profiles.json",
                "--instruction",
                "write two chapters",
                "--steps",
                "1",
            ],
        )
        self.assertTrue(parsed._steps_explicit)
        with self.assertRaisesRegex(ValueError, "reject deprecated --steps"):
            autonomy_command_requested(parsed)
        with self.assertRaisesRegex(ValueError, "immutable InstructionPlan"):
            autonomy_command_requested(
                command_args(
                    instruction="write two chapters",
                    trusted_profiles="profiles.json",
                    steps=2,
                )
            )

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.parse_arguments(
                cli.build_parser(),
                [
                    "--story-project",
                    "auto",
                    "--trusted-profiles",
                    "profiles.json",
                    "--instruction",
                    "write one chapter",
                    "--step=1",
                ],
            )

    def test_endpoint_mismatch_fails_before_runner_or_session_intent(self) -> None:
        with workspace_case("cli-endpoint") as temporary:
            root = Path(temporary)
            profile_path = root / "profiles.json"
            profile_path.write_text(
                json.dumps(trusted_profiles().payload, ensure_ascii=False),
                encoding="utf-8",
            )
            runtime = SimpleNamespace(runtime_dir=root / "runtime")
            preview_args = command_args(
                instruction="write 1 chapter",
                trusted_profiles=str(profile_path),
            )
            with patch(
                "core.autonomy.cli._capture_source_snapshot_from_args",
                return_value=source_snapshot(),
            ):
                preview = run_autonomy_command(
                    preview_args, story_runtime_paths=runtime
                )

            execute_args = command_args(
                execute_plan=preview["artifact"]["path"],
                trusted_profiles=str(profile_path),
                autonomy_root_map=str(root / "operator-roots.json"),
            )
            configured = SimpleNamespace(
                openai_model="trusted-model",
                openai_max_output_tokens=16000,
                openai_base_url="https://compatible.invalid/v1",
            )
            with patch(
                "core.autonomy.cli.get_config", return_value=configured
            ), patch("core.autonomy.cli._build_autonomy_runner") as build_runner:
                with self.assertRaisesRegex(
                    Exception, "autonomy_provider_endpoint_mismatch"
                ):
                    run_autonomy_command(
                        execute_args, story_runtime_paths=runtime
                    )
            build_runner.assert_not_called()
            session_root = runtime.runtime_dir / "sessions"
            self.assertEqual(
                [], list(session_root.glob("session_*")) if session_root.exists() else []
            )

    def test_unsupported_provider_preview_writes_no_plan_or_session(self) -> None:
        with workspace_case("cli-provider") as temporary:
            root = Path(temporary)
            payload = copy.deepcopy(trusted_profiles().payload)
            payload["provider_models"][0]["provider"] = "anthropic"
            payload["provider_models"][0]["model"] = "trusted-anthropic-model"
            profile_path = root / "profiles.json"
            profile_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            runtime = SimpleNamespace(runtime_dir=root / "runtime")
            args = command_args(
                instruction="write 1 chapter",
                trusted_profiles=str(profile_path),
            )
            with patch(
                "core.autonomy.cli._capture_source_snapshot_from_args",
                return_value=source_snapshot(),
            ):
                with self.assertRaisesRegex(
                    Exception, "autonomy_provider_unsupported"
                ):
                    run_autonomy_command(args, story_runtime_paths=runtime)
            self.assertEqual(
                [],
                list((runtime.runtime_dir / "plans").glob("*.json"))
                if (runtime.runtime_dir / "plans").exists()
                else [],
            )
            self.assertEqual(
                [],
                list((runtime.runtime_dir / "sessions").glob("session_*"))
                if (runtime.runtime_dir / "sessions").exists()
                else [],
            )
    def test_all_session_commands_require_explicit_story_project_locator(self) -> None:
        for command in (
            "session_status",
            "resume_session",
            "cancel_session",
            "abandon_session",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(
                ValueError, "explicit --story-project locator"
            ):
                autonomy_command_requested(
                    command_args(
                        story_project=None,
                        trusted_profiles="profiles.json",
                        **{command: "latest"},
                    )
                )

    def test_autonomy_commands_reject_existing_top_level_commands(self) -> None:
        conflicts = {
            "check": True,
            "check_json": True,
            "check_memory_v2": True,
            "report_runs": True,
            "recover_latest": True,
            "reconcile_persistence": True,
            "reconcile_deliveries": True,
            "inspect_delivery": "job-1",
            "resolve_delivery": "job-1",
            "init_runtime": True,
            "force_init_runtime": True,
            "review_latest": True,
            "review_list": True,
            "review_dashboard": True,
            "story_project_compat_report": True,
            "story_state_shadow_report": True,
            "activate_story_state": True,
            "inspect_story_project_runtime_from": "old-runtime",
            "migrate_story_project_runtime_from": "old-runtime",
        }
        for attribute, value in conflicts.items():
            with self.subTest(attribute=attribute), self.assertRaisesRegex(
                ValueError, "existing top-level commands"
            ):
                autonomy_command_requested(
                    command_args(
                        instruction="write one chapter",
                        trusted_profiles="profiles.json",
                        **{attribute: value},
                    )
                )

        for attribute, value in conflicts.items():
            with self.subTest(existing_command_alone=attribute):
                self.assertFalse(
                    autonomy_command_requested(command_args(**{attribute: value}))
                )

    def test_preview_then_execute_delegates_to_real_runner_path(self) -> None:
        with workspace_case("cli") as temporary:
            root = Path(temporary)
            profile_path = root / "profiles.json"
            profile_path.write_text(
                json.dumps(trusted_profiles().payload, ensure_ascii=False), encoding="utf-8"
            )
            runtime = SimpleNamespace(runtime_dir=root / "runtime")
            preview_args = command_args(
                instruction="连续写 2章",
                trusted_profiles=str(profile_path),
            )
            with patch(
                "core.autonomy.cli._capture_source_snapshot_from_args",
                return_value=source_snapshot(),
            ):
                preview = run_autonomy_command(
                    preview_args, story_runtime_paths=runtime
                )
            self.assertFalse(preview["executed"])
            plan_path = preview["artifact"]["path"]

            execute_args = command_args(
                execute_plan=plan_path,
                trusted_profiles=str(profile_path),
                autonomy_root_map=str(root / "operator-roots.json"),
                dry_run=True,
                persist_dry_run=True,
            )
            runner = Mock()
            runner.execute_plan.return_value = {
                "stopped_reason": "completed",
                "session": {"session_id": "session-from-runner"},
                "runs": [],
            }
            with patch(
                "core.autonomy.cli._capture_source_snapshot_from_args",
                return_value=source_snapshot(),
            ), patch("core.autonomy.cli._build_autonomy_runner", return_value=runner):
                first = run_autonomy_command(execute_args, story_runtime_paths=runtime)
                replay = run_autonomy_command(execute_args, story_runtime_paths=runtime)
            self.assertEqual(
                first["session"]["session_id"], replay["session"]["session_id"]
            )
            self.assertTrue(first["ok"])
            self.assertEqual(2, runner.execute_plan.call_count)

    def test_main_autonomy_command_exits_before_agent_executor(self) -> None:
        argv = [
            "main.py",
            "--story-project",
            "auto",
            "--trusted-profiles",
            "profiles.json",
            "--instruction",
            "写一章",
        ]
        result = {"ok": True, "command": "instruction_preview", "executed": False}
        with patch.object(cli.sys, "argv", argv), patch.object(
            cli, "_apply_story_project_runtime_defaults", return_value=SimpleNamespace()
        ), patch.object(cli, "_delivery_command_requested", return_value=False), patch.object(
            cli, "_run_autonomy_command", return_value=result
        ), patch.object(cli, "AgentExecutor") as executor, redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(0, raised.exception.code)
        executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
