from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import main as cli
from core.engine.root_registry import RootRegistryError, RootRegistryService
from core.engine.safe_paths import SafePathError


class RootRegistryCliTest(unittest.TestCase):
    def _case(self, name: str) -> tuple[Path, Path, Path, dict]:
        base = Path.cwd() / ".tmp" / "test_root_registry_cli" / f"{name}_{uuid.uuid4().hex}"
        runtime = base / "runtime"
        story = base / "story"
        runtime.mkdir(parents=True)
        story.mkdir()
        transaction_root = runtime / "persistence"
        registry = RootRegistryService(transaction_root).ensure(
            {"runtime": runtime, "story_project": story}
        )
        return base, transaction_root, story, registry

    def _argv(
        self,
        transaction_root: Path,
        registry: dict,
        *,
        root_id: str,
        root_uuid: str,
        target: Path,
        output_json: bool = False,
    ) -> list[str]:
        result = [
            "main.py",
            "--remap-roots",
            "--persistence-dir",
            str(transaction_root),
            "--remap-root",
            root_id,
            root_uuid,
            str(target),
            "--expected-root-registry-revision",
            str(registry["revision"]),
            "--expected-root-registry-digest",
            registry["registry_digest"],
        ]
        if output_json:
            result.append("--output-json")
        return result

    def test_main_remaps_existing_uuid_without_moving_data(self) -> None:
        base, transaction_root, story, before = self._case("success")
        marker = story / "operator-must-move-this.txt"
        marker.write_text("still here", encoding="utf-8")
        moved = base / "story-moved"
        moved.mkdir()
        root_uuid = before["roots"]["story_project"]["root_uuid"]
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            self._argv(
                transaction_root,
                before,
                root_id="story_project",
                root_uuid=root_uuid,
                target=moved,
            ),
        ), patch.object(cli, "AgentExecutor") as executor, contextlib.redirect_stdout(
            output
        ), self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(0, raised.exception.code)
        executor.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual("registered_data_roots_only", payload["scope"])
        self.assertFalse(payload["data_moved_or_copied"])
        self.assertFalse(payload["runtime_control_plane_relocation_supported"])
        self.assertTrue(marker.is_file())
        self.assertFalse((moved / marker.name).exists())
        after = RootRegistryService(transaction_root).load()
        self.assertEqual(root_uuid, after["roots"]["story_project"]["root_uuid"])
        self.assertEqual(str(moved.absolute()), after["roots"]["story_project"]["path"])
        self.assertEqual(before["revision"] + 1, after["revision"])

    def test_main_rejects_uuid_mismatch_before_registry_write(self) -> None:
        base, transaction_root, _story, before = self._case("uuid_mismatch")
        moved = base / "story-moved"
        moved.mkdir()
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            self._argv(
                transaction_root,
                before,
                root_id="story_project",
                root_uuid=str(uuid.uuid4()),
                target=moved,
                output_json=True,
            ),
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(1, raised.exception.code)
        self.assertIn("UUID mismatch", json.loads(output.getvalue())["error"]["message"])
        self.assertEqual(before, RootRegistryService(transaction_root).load())

    def test_main_rejects_stale_registry_cas_and_pending_transaction(self) -> None:
        base, transaction_root, _story, before = self._case("cas_pending")
        moved = base / "story-moved"
        moved.mkdir()
        root_uuid = before["roots"]["story_project"]["root_uuid"]
        stale_argv = self._argv(
            transaction_root,
            before,
            root_id="story_project",
            root_uuid=root_uuid,
            target=moved,
            output_json=True,
        )
        digest_index = stale_argv.index("--expected-root-registry-digest") + 1
        stale_argv[digest_index] = "0" * 64
        output = io.StringIO()

        with patch.object(sys, "argv", stale_argv), contextlib.redirect_stdout(
            output
        ), self.assertRaises(SystemExit) as stale:
            cli.main()

        self.assertEqual(1, stale.exception.code)
        self.assertIn("digest changed", json.loads(output.getvalue())["error"]["message"])
        self.assertEqual(before, RootRegistryService(transaction_root).load())

        pending = transaction_root / "registry" / "pending" / "run-1.json"
        pending.parent.mkdir(parents=True)
        pending.write_text("{}", encoding="utf-8")
        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            self._argv(
                transaction_root,
                before,
                root_id="story_project",
                root_uuid=root_uuid,
                target=moved,
                output_json=True,
            ),
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as blocked:
            cli.main()

        self.assertEqual(1, blocked.exception.code)
        self.assertIn("pending persistence transaction", output.getvalue())
        self.assertEqual(before, RootRegistryService(transaction_root).load())

    def test_command_requires_explicit_control_plane_and_canonical_uuid(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "--remap-roots",
                "--remap-root",
                "story_project",
                str(uuid.uuid4()),
                str(Path.cwd()),
                "--expected-root-registry-revision",
                "1",
                "--expected-root-registry-digest",
                "0" * 64,
            ]
        )
        args._runtime_path_explicit = {"persistence_dir": False}
        args._steps_explicit = False
        with self.assertRaisesRegex(ValueError, "explicit --persistence-dir"):
            cli._root_remap_command_requested(args)

        args.persistence_dir = ".tmp/runtime/persistence"
        args._runtime_path_explicit["persistence_dir"] = True
        args.remap_root[0][1] = args.remap_root[0][1].upper()
        with self.assertRaisesRegex(ValueError, "UUID must be canonical"):
            cli._root_remap_command_requested(args)

    def test_service_rejects_volume_root_network_and_runtime_control_plane(self) -> None:
        base, transaction_root, _story, before = self._case("unsafe")
        service = RootRegistryService(transaction_root)
        root_uuid = before["roots"]["story_project"]["root_uuid"]
        self.assertTrue(root_uuid)
        volume_root = Path(Path.cwd().anchor)

        with self.assertRaisesRegex(RootRegistryError, "too broad"):
            service.remap_roots(
                {"story_project": volume_root},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )
        with self.assertRaises((RootRegistryError, SafePathError)):
            service.remap_roots(
                {"story_project": Path(r"\\server\share\novelagent")},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )

        moved_runtime = base / "runtime-moved"
        moved_runtime.mkdir()
        with self.assertRaisesRegex(RootRegistryError, "control-plane relocation"):
            service.remap_roots(
                {"runtime": moved_runtime},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )
        self.assertEqual(before, service.load())


if __name__ == "__main__":
    unittest.main()
