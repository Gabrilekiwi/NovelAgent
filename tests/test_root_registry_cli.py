from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main as cli
from core.autonomy.lease import BookLeaseStore
from core.engine.project_root_remap import (
    _ALLOWED_REGISTRIES,
    _assert_identity_preserved,
    remap_story_project_roots,
)
from core.engine.persistence import PersistenceLockError, persistence_run_lock
from core.engine.root_registry import (
    RootRegistryError,
    RootRegistryService,
    RootRemapBlockedError,
    directory_identity,
)
from core.engine.safe_paths import SafePathError
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.identity import ensure_project_identity


class RootRegistryCliTest(unittest.TestCase):
    def _case(self, name: str) -> tuple[Path, Path, Path, Path, dict]:
        base = Path.cwd() / ".tmp" / "test_root_registry_cli" / f"{name}_{uuid.uuid4().hex}"
        runtime = base / "runtime"
        story = base / "story"
        export = base / "export"
        runtime.mkdir(parents=True)
        story.mkdir()
        export.mkdir()
        transaction_root = runtime / "persistence"
        registry = RootRegistryService(transaction_root).ensure(
            {"runtime": runtime, "story_project": story, "export": export}
        )
        return base, transaction_root, story, export, registry

    def _project_case(
        self, name: str, *, all_registries: bool = True
    ) -> tuple[Path, Path, dict[str, dict]]:
        base = Path.cwd() / ".tmp" / "test_root_registry_cli" / f"{name}_{uuid.uuid4().hex}"
        story = base / "old-book"
        runtime = story / ".novelagent" / "runtime"
        main_root = runtime / "persistence"
        snapshot = runtime
        chapter = runtime / "chapters"
        delivery = runtime / "deliveries"
        for path in (main_root, chapter, delivery):
            path.mkdir(parents=True, exist_ok=True)
        ensure_project_identity(story)
        registries = {
            "main": RootRegistryService(main_root).ensure(
                {
                    "story_project": story,
                    "runtime": runtime,
                    "snapshot": snapshot,
                    "chapter_artifacts": chapter,
                    "delivery_store": delivery,
                }
            )
        }
        if all_registries:
            ea = runtime / "ea"
            ea.mkdir(parents=True)
            registries["ea"] = RootRegistryService(ea).ensure(
                {"story_project": story}, require_runtime=False
            )
            migration = story / ".novelagent" / "migration-v2" / "tx"
            migration.mkdir(parents=True)
            registries["migration"] = RootRegistryService(migration).ensure(
                {"story_project": story, "runtime": story / ".novelagent"}
            )
            memory = runtime / "memory" / "v2"
            history = memory / "history_revision_execution" / "persistence"
            history.mkdir(parents=True)
            registries["history"] = RootRegistryService(history).ensure(
                {"story_project": story, "runtime": memory}
            )
        return base, story, registries

    @staticmethod
    def _project_request(main: dict, new_story: Path) -> dict[str, dict]:
        return {
            "story_project": {
                "root_uuid": main["roots"]["story_project"]["root_uuid"],
                "path": new_story,
            },
            "runtime": {
                "root_uuid": main["roots"]["runtime"]["root_uuid"],
                "path": new_story / ".novelagent" / "runtime",
            },
        }

    @staticmethod
    def _run_project(
        new_story: Path,
        main: dict,
        *,
        request: dict[str, dict] | None = None,
        fault_injector=None,
    ) -> dict:
        return remap_story_project_roots(
            new_story_project=new_story,
            control_plane=new_story / ".novelagent" / "runtime" / "persistence",
            requested=request or RootRegistryCliTest._project_request(main, new_story),
            expected_revision=main["revision"],
            expected_registry_digest=main["registry_digest"],
            fault_injector=fault_injector,
        )

    @staticmethod
    def _rename_story(source: Path, target: Path) -> None:
        # Windows virus scanners can retain a just-closed lock file handle for
        # a few milliseconds.  The production operation is still one atomic
        # rename; this only removes that platform test-runner flake.
        for attempt in range(5):
            try:
                source.rename(target)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))

    @staticmethod
    def _write_v1_journal(
        persistence_root: Path,
        *,
        run_id: str,
        state: str,
        allowed_root: Path,
        valid_commit_marker: bool = False,
    ) -> Path:
        journal = persistence_root / run_id
        journal.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "state": state,
            "allowed_roots": [str(allowed_root.absolute())],
            "targets": [],
            "commit_marker": "commit.marker",
            "candidate_sha256": None,
        }
        (journal / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if valid_commit_marker:
            (journal / "commit.marker").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "committed_at": "2026-07-15T00:00:00+00:00",
                        "candidate_sha256": None,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return journal

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

    def test_main_remaps_non_project_data_root_without_moving_data(self) -> None:
        base, transaction_root, _story, export, before = self._case("data_root")
        marker = export / "operator-must-move-this.txt"
        marker.write_text("still here", encoding="utf-8")
        moved = base / "export-moved"
        moved.mkdir()
        root_uuid = before["roots"]["export"]["root_uuid"]
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            self._argv(
                transaction_root,
                before,
                root_id="export",
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
        self.assertEqual("registered_data_roots_only", payload["scope"])
        self.assertFalse(payload["runtime_control_plane_relocation_supported"])
        self.assertTrue(marker.is_file())
        self.assertFalse((moved / marker.name).exists())
        after = RootRegistryService(transaction_root).load()
        self.assertEqual(root_uuid, after["roots"]["export"]["root_uuid"])
        self.assertEqual(str(moved.absolute()), after["roots"]["export"]["path"])

    def test_single_registry_service_cannot_relocate_story_project_or_runtime(self) -> None:
        base, transaction_root, _story, _export, before = self._case("single_reject")
        target = base / "target"
        target.mkdir()
        service = RootRegistryService(transaction_root)
        with self.assertRaisesRegex(RootRegistryError, "project-level"):
            service.remap_roots(
                {"story_project": target},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )
        with self.assertRaisesRegex(RootRegistryError, "single registry"):
            service.remap_roots(
                {"runtime": target},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )
        self.assertEqual(before, service.load())

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

    def test_service_rejects_volume_root_and_network(self) -> None:
        _base, transaction_root, _story, _export, before = self._case("unsafe")
        service = RootRegistryService(transaction_root)
        with self.assertRaisesRegex(RootRegistryError, "too broad"):
            service.remap_roots(
                {"export": Path(Path.cwd().anchor)},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )
        with self.assertRaises((RootRegistryError, SafePathError)):
            service.remap_roots(
                {"export": Path(r"\\server\share\novelagent")},
                expected_revision=before["revision"],
                expected_registry_digest=before["registry_digest"],
            )

    def test_cli_rebinds_all_embedded_registries_after_same_volume_rename(self) -> None:
        base, old_story, registries = self._project_case("complete")
        marker = old_story / "chapter.md"
        marker.write_text("published bytes stay with rename\n", encoding="utf-8")
        new_story = base / "new-book"
        old_story.rename(new_story)
        main = registries["main"]
        request = self._project_request(main, new_story)
        argv = [
            "main.py",
            "--remap-roots",
            "--persistence-dir",
            str(new_story / ".novelagent" / "runtime" / "persistence"),
        ]
        for root_id, item in request.items():
            argv.extend(
                ["--remap-root", root_id, item["root_uuid"], str(item["path"])]
            )
        argv.extend(
            [
                "--expected-root-registry-revision",
                str(main["revision"]),
                "--expected-root-registry-digest",
                main["registry_digest"],
            ]
        )
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            cli, "AgentExecutor"
        ) as executor, contextlib.redirect_stdout(output), self.assertRaises(
            SystemExit
        ) as raised:
            cli.main()

        self.assertEqual(0, raised.exception.code)
        executor.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual("complete_story_project_rebind", payload["scope"])
        self.assertTrue(payload["all_registries_rebound"])
        self.assertTrue(payload["identity_preserving_same_volume_rename"])
        self.assertEqual(4, payload["registry_count"])
        self.assertEqual(
            sorted(path.as_posix() for path in _ALLOWED_REGISTRIES),
            sorted(item["relative_path"] for item in payload["registries"]),
        )
        self.assertEqual(
            "published bytes stay with rename\n", marker.with_name(marker.name).read_text(encoding="utf-8")
            if marker.exists()
            else (new_story / marker.name).read_text(encoding="utf-8"),
        )
        for item in payload["registries"]:
            after = RootRegistryService(
                new_story / Path(item["relative_path"]).parent
            ).load()
            self.assertEqual(item["registry_id"], after["registry_id"])
            for binding in after["roots"].values():
                path = Path(binding["path"])
                if path.is_relative_to(new_story):
                    self.assertNotIn(str(old_story), str(path))

    def test_project_remap_does_not_materialize_an_absent_ea_control_plane(self) -> None:
        base, old_story, registries = self._project_case(
            "ea_absent", all_registries=False
        )
        old_ea = old_story / ".novelagent" / "runtime" / "ea"
        self.assertFalse(old_ea.exists())
        new_story = base / "new-book"
        old_story.rename(new_story)

        report = self._run_project(new_story, registries["main"])

        self.assertTrue(report["all_registries_rebound"])
        self.assertFalse((new_story / ".novelagent" / "runtime" / "ea").exists())

    def test_ea_copy_split_brain_is_rejected(self) -> None:
        base, old_story, registries = self._project_case("ea_copy")
        new_story = base / "copied-book"
        shutil.copytree(old_story, new_story)
        copied_ea_before = RootRegistryService(
            new_story / ".novelagent" / "runtime" / "ea"
        ).load()

        with self.assertRaisesRegex(RootRegistryError, "old StoryProject path still exists"):
            self._run_project(new_story, registries["main"])

        self.assertEqual(
            copied_ea_before,
            RootRegistryService(new_story / ".novelagent" / "runtime" / "ea").load(),
        )
        self.assertEqual(
            registries["ea"],
            RootRegistryService(old_story / ".novelagent" / "runtime" / "ea").load(),
        )

    def test_old_main_persistence_missing_but_old_book_and_ea_remain_is_rejected(self) -> None:
        base, old_story, registries = self._project_case("old_book_remains")
        new_story = base / "copied-book"
        shutil.copytree(old_story, new_story)
        old_main = old_story / ".novelagent" / "runtime" / "persistence"
        new_main = new_story / ".novelagent" / "runtime" / "persistence"
        shutil.rmtree(new_main)
        old_main.rename(new_main)
        self.assertFalse(old_main.exists())
        self.assertTrue((old_story / ".novelagent" / "runtime" / "ea").exists())

        with self.assertRaisesRegex(RootRegistryError, "old StoryProject path still exists"):
            self._run_project(new_story, registries["main"])

    def test_copy_delete_identity_mismatch_is_rejected(self) -> None:
        base, old_story, registries = self._project_case("copy_delete")
        new_story = base / "copied-book"
        shutil.copytree(old_story, new_story)
        preserved_original = base / "original-held-elsewhere"
        old_story.rename(preserved_original)
        self.assertFalse(old_story.exists())

        with self.assertRaisesRegex(RootRegistryError, "directory identity changed"):
            self._run_project(new_story, registries["main"])

    def test_cross_volume_identity_mismatch_is_rejected(self) -> None:
        base, old_story, registries = self._project_case("cross_volume", all_registries=False)
        binding = dict(registries["main"]["roots"]["story_project"])
        identity = directory_identity(old_story)
        binding["directory_identity"] = {**identity, "device": identity["device"] + 1}
        with self.assertRaisesRegex(RootRegistryError, "across volumes"):
            _assert_identity_preserved(binding, old_story, label="StoryProject")

    def test_rogue_or_nested_control_plane_is_rejected(self) -> None:
        base, old_story, registries = self._project_case("rogue")
        new_story = base / "new-book"
        old_story.rename(new_story)
        rogue = new_story / ".novelagent" / "runtime" / "rogue-control"
        rogue.mkdir()
        shutil.copyfile(
            new_story / ".novelagent" / "runtime" / "persistence" / "root_registry.json",
            rogue / "root_registry.json",
        )
        with self.assertRaisesRegex(RootRegistryError, "rogue or nested"):
            self._run_project(new_story, registries["main"])

    def test_ea_pending_and_recovery_required_block_before_any_registry_changes(self) -> None:
        for state in ("p", "x"):
            with self.subTest(state=state):
                base, old_story, registries = self._project_case(f"ea_{state}")
                new_story = base / "new-book"
                self._rename_story(old_story, new_story)
                pending = (
                    new_story
                    / ".novelagent"
                    / "runtime"
                    / "ea"
                    / "r"
                    / state
                    / "entry.json"
                )
                pending.parent.mkdir(parents=True)
                pending.write_text("{}\n", encoding="utf-8")

                with self.assertRaisesRegex(
                    RootRemapBlockedError, "pending persistence"
                ):
                    self._run_project(new_story, registries["main"])
                self.assertEqual(
                    registries["main"],
                    RootRegistryService(
                        new_story / ".novelagent" / "runtime" / "persistence"
                    ).load(),
                )

    def test_local_pending_or_recovery_staging_and_orphan_are_blocked(self) -> None:
        names_and_paths = (
            ("pending", Path("registry/pending/run.json")),
            ("recovery", Path("registry/recovery_required/run.json")),
            ("staging", Path("staging/run/partial.bin")),
            ("orphan", Path("journals/run/manifest.json")),
        )
        for label, relative in names_and_paths:
            with self.subTest(label=label):
                base, old_story, registries = self._project_case(f"local_{label}")
                new_story = base / "new-book"
                self._rename_story(old_story, new_story)
                artifact = (
                    new_story
                    / ".novelagent"
                    / "runtime"
                    / "persistence"
                    / relative
                )
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(RootRemapBlockedError, "pending persistence"):
                    self._run_project(new_story, registries["main"])

    def test_actual_direct_v1_pending_and_orphan_journals_block(self) -> None:
        for label, state in (("prepared", "prepared"), ("recovery", "recovery_required")):
            with self.subTest(label=label):
                base, old_story, registries = self._project_case(
                    f"v1_{label}", all_registries=False
                )
                persistence = old_story / ".novelagent" / "runtime" / "persistence"
                self._write_v1_journal(
                    persistence,
                    run_id=f"chapter_1_{label}",
                    state=state,
                    allowed_root=old_story,
                )
                new_story = base / "new-book"
                self._rename_story(old_story, new_story)
                with self.assertRaisesRegex(
                    RootRemapBlockedError, "pending persistence"
                ):
                    self._run_project(new_story, registries["main"])

        base, old_story, registries = self._project_case(
            "v1_orphan", all_registries=False
        )
        orphan = (
            old_story
            / ".novelagent"
            / "runtime"
            / "persistence"
            / "chapter_2_orphan"
        )
        orphan.mkdir()
        new_story = base / "new-book"
        old_story.rename(new_story)
        with self.assertRaisesRegex(RootRemapBlockedError, "pending persistence"):
            self._run_project(new_story, registries["main"])

    def test_actual_direct_v1_terminal_journals_allow_remap(self) -> None:
        base, old_story, registries = self._project_case(
            "v1_terminal", all_registries=False
        )
        persistence = old_story / ".novelagent" / "runtime" / "persistence"
        self._write_v1_journal(
            persistence,
            run_id="chapter_1_completed",
            state="completed",
            allowed_root=old_story,
            valid_commit_marker=True,
        )
        self._write_v1_journal(
            persistence,
            run_id="chapter_2_rolled_back",
            state="rolled_back",
            allowed_root=old_story,
        )
        new_story = base / "new-book"
        old_story.rename(new_story)

        report = self._run_project(new_story, registries["main"])

        self.assertTrue(report["all_registries_rebound"])

    def test_direct_v1_marker_path_cannot_escape_to_matching_external_marker(self) -> None:
        base, old_story, registries = self._project_case(
            "v1_marker_escape", all_registries=False
        )
        persistence = old_story / ".novelagent" / "runtime" / "persistence"
        run_id = "chapter_escape_completed"
        journal = self._write_v1_journal(
            persistence,
            run_id=run_id,
            state="completed",
            allowed_root=old_story,
        )
        external_marker = base / "matching-external.marker"
        external_marker.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "committed_at": "2026-07-15T00:00:00+00:00",
                    "candidate_sha256": None,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest_path = journal / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["commit_marker"] = str(external_marker.absolute())
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        before_external = external_marker.read_bytes()
        new_story = base / "new-book"
        old_story.rename(new_story)

        with self.assertRaisesRegex(RootRemapBlockedError, "pending persistence"):
            self._run_project(new_story, registries["main"])
        self.assertEqual(before_external, external_marker.read_bytes())

    def test_nested_legacy_v1_requires_valid_terminal_manifest_and_marker(self) -> None:
        invalid_cases = (
            ("completed_missing_marker", "completed", False, False),
            ("rolled_back_with_marker", "rolled_back", True, False),
            ("malformed_manifest", "completed", False, True),
            ("orphan", None, False, False),
        )
        for label, state, marker, malformed in invalid_cases:
            with self.subTest(label=label):
                base, old_story, registries = self._project_case(
                    f"nested_v1_{label}", all_registries=False
                )
                runs = old_story / ".novelagent" / "runtime" / "runs"
                transactions = runs / "transactions"
                transactions.mkdir(parents=True)
                (runs / ".persistence.lock").write_bytes(b"\0")
                if state is None:
                    (transactions / "orphan_run").mkdir()
                else:
                    journal = self._write_v1_journal(
                        transactions,
                        run_id=f"run_{label}",
                        state=state,
                        allowed_root=old_story,
                        valid_commit_marker=marker,
                    )
                    if malformed:
                        payload = json.loads(
                            (journal / "manifest.json").read_text(encoding="utf-8")
                        )
                        payload.pop("allowed_roots")
                        (journal / "manifest.json").write_text(
                            json.dumps(payload, sort_keys=True) + "\n",
                            encoding="utf-8",
                        )
                new_story = base / "new-book"
                self._rename_story(old_story, new_story)
                with self.assertRaisesRegex(
                    RootRemapBlockedError, "invalid or pending legacy"
                ):
                    self._run_project(new_story, registries["main"])

    def test_nested_legacy_v1_valid_terminal_journals_allow_remap(self) -> None:
        base, old_story, registries = self._project_case(
            "nested_v1_terminal", all_registries=False
        )
        runs = old_story / ".novelagent" / "runtime" / "runs"
        transactions = runs / "transactions"
        transactions.mkdir(parents=True)
        (runs / ".persistence.lock").write_bytes(b"\0")
        self._write_v1_journal(
            transactions,
            run_id="nested_completed",
            state="completed",
            allowed_root=old_story,
            valid_commit_marker=True,
        )
        self._write_v1_journal(
            transactions,
            run_id="nested_rolled_back",
            state="rolled_back",
            allowed_root=old_story,
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_nested_legacy_v1_marker_path_cannot_escape(self) -> None:
        base, old_story, registries = self._project_case(
            "nested_v1_escape", all_registries=False
        )
        runs = old_story / ".novelagent" / "runtime" / "runs"
        transactions = runs / "transactions"
        transactions.mkdir(parents=True)
        (runs / ".persistence.lock").write_bytes(b"\0")
        run_id = "nested_escape_completed"
        journal = self._write_v1_journal(
            transactions,
            run_id=run_id,
            state="completed",
            allowed_root=old_story,
        )
        external = base / "matching-nested.marker"
        external.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "committed_at": "2026-07-15T00:00:00+00:00",
                    "candidate_sha256": None,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest_path = journal / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["commit_marker"] = "../../../../../../matching-nested.marker"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        before = external.read_bytes()
        new_story = base / "new-book"
        old_story.rename(new_story)

        with self.assertRaisesRegex(RootRemapBlockedError, "invalid or pending legacy"):
            self._run_project(new_story, registries["main"])
        self.assertEqual(before, external.read_bytes())

    def test_legacy_run_directory_lock_blocks_project_remap(self) -> None:
        base, old_story, registries = self._project_case(
            "legacy_writer_lock", all_registries=False
        )
        legacy_runs = old_story / ".novelagent" / "runtime" / "runs"
        legacy_runs.mkdir()
        new_story = base / "new-book"
        old_story.rename(new_story)
        moved_runs = new_story / ".novelagent" / "runtime" / "runs"

        with persistence_run_lock(moved_runs):
            with self.assertRaisesRegex(RootRemapBlockedError, "active writer"):
                self._run_project(new_story, registries["main"])

        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_active_autonomy_lease_blocks_even_when_expired(self) -> None:
        base, old_story, registries = self._project_case("active_lease")
        autonomy = old_story / ".novelagent" / "runtime" / "autonomy"
        BookLeaseStore(autonomy).acquire(
            book_id="book-remap",
            session_id="session-remap",
            plan_id="plan-remap",
            ttl_seconds=1,
            at="2020-01-01T00:00:00+00:00",
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        with self.assertRaisesRegex(RootRemapBlockedError, "sessions/leases"):
            self._run_project(new_story, registries["main"])

    def test_pending_autonomy_operation_blocks_project_remap(self) -> None:
        from core.autonomy.operations import AutonomyOperationStore

        base, old_story, registries = self._project_case("pending_operation")
        autonomy = old_story / ".novelagent" / "runtime" / "autonomy"
        AutonomyOperationStore(autonomy).begin(
            operation_type="execute",
            session_id="session-remap",
            book_id="book-remap",
            plan_id="plan-remap",
            plan_hash="a" * 64,
            expected_state="absent",
            expected_event_hash=None,
            expected_lease_hash=None,
            target_event_type="started",
            reason=None,
            lease_ttl_seconds=300,
            created_at="2026-07-15T00:00:00+00:00",
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        with self.assertRaisesRegex(RootRemapBlockedError, "operations/sessions/leases"):
            self._run_project(new_story, registries["main"])

    def test_active_autonomy_session_blocks_without_relying_on_a_lease(self) -> None:
        from core.autonomy.arc import build_run_arc_plan
        from core.autonomy.common import atomic_append_json
        from core.autonomy.session import build_session_event, build_session_genesis
        from tests.test_autonomy_plans import instruction_plan

        base, old_story, registries = self._project_case("active_session")
        autonomy = old_story / ".novelagent" / "runtime" / "autonomy"
        plan = instruction_plan(count=1)
        session_id = "session-remap"
        arc = build_run_arc_plan(plan, session_id=session_id)
        genesis = build_session_genesis(
            session_id=session_id,
            instruction_plan=plan,
            arc_plan=arc,
        )
        event = build_session_event(
            genesis=genesis,
            sequence=1,
            event_type="started",
            previous_event_hash=None,
        )
        session = autonomy / "sessions" / session_id
        atomic_append_json(session / "genesis.json", genesis)
        atomic_append_json(
            session / "events" / f"000001-{event['event_hash'][:20]}.json", event
        )
        atomic_append_json(
            autonomy / "sessions" / "latest.json",
            {
                "schema_version": "1.0",
                "session_id": session_id,
                "genesis_hash": genesis["genesis_hash"],
            },
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        with self.assertRaisesRegex(RootRemapBlockedError, "operations/sessions/leases"):
            self._run_project(new_story, registries["main"])

    def test_multi_registry_crash_recovers_forward_before_claiming_complete(self) -> None:
        base, old_story, registries = self._project_case("crash_recovery")
        new_story = base / "new-book"
        old_story.rename(new_story)

        def crash(event, index, _path):
            if event == "after_project_registry_replace" and index == 1:
                raise RuntimeError("simulated remap crash")

        with self.assertRaisesRegex(RuntimeError, "simulated remap crash"):
            self._run_project(
                new_story, registries["main"], fault_injector=crash
            )
        journal = new_story / ".novelagent" / "root-remap" / "transactions"
        transaction = next(path for path in journal.iterdir() if path.is_dir())
        self.assertTrue((transaction / "commit.marker").is_file())
        self.assertFalse((transaction / "completed.json").exists())
        self.assertEqual(2, len(list((transaction / "progress").glob("*.json"))))

        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])
        self.assertEqual(4, report["registry_count"])
        self.assertTrue((transaction / "completed.json").is_file())

    def test_crash_during_intent_staging_is_quarantined_and_reprepared(self) -> None:
        base, old_story, registries = self._project_case("prepare_crash")
        new_story = base / "new-book"
        old_story.rename(new_story)
        transactions = (
            new_story / ".novelagent" / "root-remap" / "transactions"
        )
        orphan = transactions / (".prepare-" + "a" * 32)
        orphan.mkdir(parents=True)
        (orphan / "partial.json").write_text("{}\n", encoding="utf-8")

        report = self._run_project(new_story, registries["main"])

        self.assertTrue(report["all_registries_rebound"])
        self.assertFalse(orphan.exists())
        self.assertTrue(
            (
                new_story
                / ".novelagent"
                / "root-remap"
                / "abandoned-prepares"
                / orphan.name
                / "partial.json"
            ).is_file()
        )

    def test_target_identity_is_rechecked_after_commit_marker(self) -> None:
        from core.engine import project_root_remap as remap_module

        base, old_story, registries = self._project_case("toctou")
        new_story = base / "new-book"
        old_story.rename(new_story)
        actual_identity = remap_module.directory_identity
        marker_published = {"value": False}

        def inject(event, _index, _path):
            if event == "after_project_remap_commit_marker":
                marker_published["value"] = True

        def changed_identity(path):
            value = actual_identity(path)
            if marker_published["value"] and Path(path).absolute() == new_story.absolute():
                return {**value, "inode": value["inode"] + 1}
            return value

        with patch.object(remap_module, "directory_identity", side_effect=changed_identity):
            with self.assertRaisesRegex(RootRegistryError, "identity changed"):
                self._run_project(
                    new_story, registries["main"], fault_injector=inject
                )

        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_registry_hash_is_rechecked_immediately_before_replace(self) -> None:
        base, old_story, registries = self._project_case(
            "registry_replace_toctou", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        main_path = (
            new_story
            / ".novelagent"
            / "runtime"
            / "persistence"
            / "root_registry.json"
        )
        original = main_path.read_bytes()

        def mutate_before_replace(event, _index, path):
            if event == "before_project_registry_replace":
                Path(path).write_bytes(Path(path).read_bytes() + b" ")

        with self.assertRaisesRegex(
            RootRegistryError, "drifted|changed before replace"
        ):
            self._run_project(
                new_story,
                registries["main"],
                fault_injector=mutate_before_replace,
            )
        self.assertEqual(original + b" ", main_path.read_bytes())

        main_path.write_bytes(original)
        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_external_logical_root_replacement_after_marker_fails_closed_and_recovers(self) -> None:
        base, old_story, registries = self._project_case(
            "external_target_swap", all_registries=False
        )
        external = base / "external-store"
        external.mkdir()
        main_root = old_story / ".novelagent" / "runtime" / "persistence"
        root_map = {
            root_id: Path(binding["path"])
            for root_id, binding in registries["main"]["roots"].items()
        }
        root_map["external_store"] = external
        main = RootRegistryService(main_root).ensure(root_map)
        new_story = base / "new-book"
        old_story.rename(new_story)
        held = base / "external-held"

        def replace_external(event, _index, _path):
            if event == "after_project_remap_commit_marker":
                external.rename(held)
                external.mkdir()

        with self.assertRaisesRegex(RootRegistryError, "physical identity changed"):
            self._run_project(new_story, main, fault_injector=replace_external)
        external.rmdir()
        held.rename(external)
        report = self._run_project(new_story, main)
        self.assertTrue(report["all_registries_rebound"])

    def test_embedded_logical_root_replacement_after_marker_fails_closed_and_recovers(self) -> None:
        base, old_story, registries = self._project_case(
            "embedded_target_swap", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        delivery = new_story / ".novelagent" / "runtime" / "deliveries"
        held = base / "deliveries-held"

        def replace_delivery(event, _index, _path):
            if event == "after_project_remap_commit_marker":
                delivery.rename(held)
                delivery.mkdir()

        with self.assertRaisesRegex(RootRegistryError, "physical identity changed"):
            self._run_project(
                new_story, registries["main"], fault_injector=replace_delivery
            )
        delivery.rmdir()
        held.rename(delivery)
        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_directory_identity_final_lstat_rejects_symlink_and_reparse_probe(self) -> None:
        base, _transaction_root, _story, export, _registry = self._case(
            "identity_final_lstat"
        )
        actual = os.lstat(export)
        probes = (
            (stat.S_IFLNK | 0o777, 0),
            (actual.st_mode, 0x0400),
        )
        for mode, attributes in probes:
            with self.subTest(mode=mode, attributes=attributes):
                fake = SimpleNamespace(
                    st_mode=mode,
                    st_dev=actual.st_dev,
                    st_ino=actual.st_ino,
                    st_file_attributes=attributes,
                )
                with patch(
                    "core.engine.root_registry.assert_safe_local_tree",
                    return_value=export,
                ), patch("core.engine.root_registry.os.lstat", return_value=fake):
                    with self.assertRaisesRegex(RootRegistryError, "final component"):
                        directory_identity(export)

    def test_replaced_prearmed_fence_never_creates_an_attacker_lock(self) -> None:
        base, old_story, registries = self._project_case(
            "fence_no_create", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        fence = new_story / ".novelagent" / "runtime" / ".root-remap-fence"
        original_fence = base / "original-fence"
        attacker = base / "attacker-fence"
        attacker.mkdir()
        sentinel = attacker / "sentinel.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        fence.rename(original_fence)
        attacker.rename(fence)

        with self.assertRaisesRegex(RootRegistryError, "fence directory identity changed"):
            self._run_project(new_story, registries["main"])
        self.assertEqual("unchanged\n", (fence / "sentinel.txt").read_text(encoding="utf-8"))
        self.assertFalse((fence / ".persistence.lock").exists())

        fence.rename(attacker)
        original_fence.rename(fence)
        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_project_identity_mutation_entrypoints_share_the_remap_fence(self) -> None:
        import hashlib

        from core.story_project.activation import activate_story_state
        from core.story_project.authority import activate_event_authority

        _base, story, _registries = self._project_case(
            "identity_writer_fence", all_registries=False
        )
        fence = story / ".novelagent" / "runtime" / ".root-remap-fence"
        identity_path = story / ".novelagent" / "project.json"
        expected_sha = hashlib.sha256(identity_path.read_bytes()).hexdigest()

        with persistence_run_lock(
            fence,
            require_existing_root=True,
            require_existing_lock=True,
        ):
            with self.subTest(writer="ensure_project_identity"):
                with self.assertRaises(PersistenceLockError):
                    ensure_project_identity(story)
            with self.subTest(writer="activate_story_state"):
                with patch(
                    "core.story_project.activation.validate_story_state_calibration_report",
                    return_value={},
                ), self.assertRaises(PersistenceLockError):
                    activate_story_state(story, {})
            with self.subTest(writer="activate_event_authority"):
                with self.assertRaises(PersistenceLockError):
                    activate_event_authority(
                        story,
                        expected_identity_sha256=expected_sha,
                        head_event_hash="a" * 64,
                    )

    def test_fence_swap_during_no_create_locking_changes_no_attacker_bytes(self) -> None:
        from core.engine import project_root_remap as remap_module

        base, old_story, registries = self._project_case(
            "fence_lock_race", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        fence = new_story / ".novelagent" / "runtime" / ".root-remap-fence"
        held = base / "held-fence"
        attacker = base / "attacker-existing-lock"
        attacker.mkdir()
        (attacker / ".persistence.lock").write_bytes(b"attacker-lock-bytes")
        (attacker / "sentinel.txt").write_text("outside unchanged\n", encoding="utf-8")
        expected_files = {
            path.name: path.read_bytes() for path in attacker.iterdir() if path.is_file()
        }
        actual_lock = remap_module.persistence_run_lock
        swapped = {"value": False}

        @contextlib.contextmanager
        def racing_lock(path, **kwargs):
            if Path(path).absolute() == fence.absolute() and not swapped["value"]:
                fence.rename(held)
                attacker.rename(fence)
                swapped["value"] = True
            with actual_lock(path, **kwargs) as acquired:
                yield acquired

        with patch.object(remap_module, "persistence_run_lock", racing_lock):
            with self.assertRaisesRegex(
                RootRegistryError, "fence directory identity changed"
            ):
                self._run_project(new_story, registries["main"])
        self.assertEqual(
            expected_files,
            {
                path.name: path.read_bytes()
                for path in fence.iterdir()
                if path.is_file()
            },
        )
        self.assertFalse(
            (new_story / ".novelagent" / "root-remap" / "transactions").exists()
        )

        fence.rename(attacker)
        held.rename(fence)
        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_lost_success_response_replays_the_unique_completed_intent(self) -> None:
        base, old_story, registries = self._project_case(
            "lost_response", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        first = self._run_project(new_story, registries["main"])
        main_path = new_story / ".novelagent" / "runtime" / "persistence"
        revision_after_first = RootRegistryService(main_path).load()["revision"]

        replay = self._run_project(new_story, registries["main"])

        self.assertEqual(first, replay)
        self.assertEqual(
            revision_after_first, RootRegistryService(main_path).load()["revision"]
        )

    def test_completed_intent_with_same_cas_basis_but_different_request_fails_closed(self) -> None:
        base, old_story, registries = self._project_case(
            "completed_request_conflict", all_registries=False
        )
        new_story = base / "new-book"
        self._rename_story(old_story, new_story)
        self._run_project(new_story, registries["main"])
        conflicting_request = {
            "story_project": {
                "root_uuid": registries["main"]["roots"]["story_project"][
                    "root_uuid"
                ],
                "path": new_story,
            }
        }

        with self.assertRaisesRegex(
            RootRegistryError, "conflicting completed project-remap intent"
        ):
            self._run_project(
                new_story,
                registries["main"],
                request=conflicting_request,
            )

    def test_multiple_exact_matching_completed_intents_fail_closed(self) -> None:
        base, old_story, registries = self._project_case(
            "multiple_completed_matches", all_registries=False
        )
        new_story = base / "new-book"
        self._rename_story(old_story, new_story)
        self._run_project(new_story, registries["main"])
        transactions = new_story / ".novelagent" / "root-remap" / "transactions"
        original = next(path for path in transactions.iterdir() if path.is_dir())
        new_transaction_id = "root-remap-" + "f" * 24
        if original.name == new_transaction_id:
            new_transaction_id = "root-remap-" + "e" * 24
        duplicate = transactions / new_transaction_id
        shutil.copytree(original, duplicate)

        intent_path = duplicate / "intent.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        intent["transaction_id"] = new_transaction_id
        intent["intent_hash"] = canonical_json_hash(
            intent, exclude_fields=("intent_hash",)
        )
        intent_path.write_text(
            json.dumps(intent, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        artifact_hash_fields = (
            (duplicate / "commit.marker", "marker_hash"),
            (duplicate / "completed.json", "completion_hash"),
        )
        for path, hash_field in artifact_hash_fields:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["transaction_id"] = new_transaction_id
            payload["intent_hash"] = intent["intent_hash"]
            payload[hash_field] = canonical_json_hash(
                payload, exclude_fields=(hash_field,)
            )
            path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        for path in sorted((duplicate / "progress").glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["transaction_id"] = new_transaction_id
            payload["intent_hash"] = intent["intent_hash"]
            payload["progress_hash"] = canonical_json_hash(
                payload, exclude_fields=("progress_hash",)
            )
            path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        with self.assertRaisesRegex(
            RootRegistryError, "multiple matching completed project-remap intents"
        ):
            self._run_project(new_story, registries["main"])

    def test_registry_inventory_is_rediscovered_in_the_final_toctou_window(self) -> None:
        base, old_story, registries = self._project_case(
            "registry_inventory_toctou", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)
        main_path = (
            new_story
            / ".novelagent"
            / "runtime"
            / "persistence"
            / "root_registry.json"
        )
        original = main_path.read_bytes()
        ea_root = new_story / ".novelagent" / "runtime" / "ea"

        def add_registry_before_replace(event, _index, _path):
            if event == "before_project_registry_replace":
                ea_root.mkdir()
                shutil.copyfile(main_path, ea_root / "root_registry.json")

        with self.assertRaisesRegex(RootRegistryError, "inventory changed"):
            self._run_project(
                new_story,
                registries["main"],
                fault_injector=add_registry_before_replace,
            )
        self.assertEqual(original, main_path.read_bytes())

        shutil.rmtree(ea_root)
        report = self._run_project(new_story, registries["main"])
        self.assertTrue(report["all_registries_rebound"])

    def test_rehashed_journal_cannot_escape_the_registry_allowlist(self) -> None:
        base, old_story, registries = self._project_case(
            "journal_escape", all_registries=False
        )
        new_story = base / "new-book"
        old_story.rename(new_story)

        def stop_after_intent(event, _index, _path):
            if event == "after_project_remap_intent_publish":
                raise RuntimeError("stop after durable intent")

        with self.assertRaisesRegex(RuntimeError, "durable intent"):
            self._run_project(
                new_story, registries["main"], fault_injector=stop_after_intent
            )
        transactions = new_story / ".novelagent" / "root-remap" / "transactions"
        transaction = next(path for path in transactions.iterdir() if path.is_dir())
        intent_path = transaction / "intent.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        sentinel = new_story.parent / "outside-registry.json"
        sentinel.write_text("do not replace\n", encoding="utf-8")
        intent["registries"][0]["relative_path"] = "../outside-registry.json"
        intent["registry_inventory"][0] = "../outside-registry.json"
        intent["registries"][0]["control_plane_identity"] = directory_identity(
            sentinel.parent
        )
        intent["intent_hash"] = canonical_json_hash(
            intent, exclude_fields=("intent_hash",)
        )
        intent_path.write_text(
            json.dumps(intent, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RootRegistryError, "canonical allowed"):
            self._run_project(new_story, registries["main"])
        self.assertEqual("do not replace\n", sentinel.read_text(encoding="utf-8"))

    def test_recovery_rejects_unindexed_staged_or_progress_artifacts(self) -> None:
        for artifact_directory in ("after", "progress"):
            with self.subTest(artifact_directory=artifact_directory):
                base, old_story, registries = self._project_case(
                    f"journal_index_{artifact_directory}", all_registries=False
                )
                new_story = base / "new-book"
                old_story.rename(new_story)

                def stop_after_intent(event, _index, _path):
                    if event == "after_project_remap_intent_publish":
                        raise RuntimeError("stop after durable intent")

                with self.assertRaisesRegex(RuntimeError, "durable intent"):
                    self._run_project(
                        new_story,
                        registries["main"],
                        fault_injector=stop_after_intent,
                    )
                transactions = (
                    new_story / ".novelagent" / "root-remap" / "transactions"
                )
                transaction = next(
                    path for path in transactions.iterdir() if path.is_dir()
                )
                (transaction / artifact_directory / "999.json").write_text(
                    "{}\n", encoding="utf-8"
                )

                with self.assertRaisesRegex(RootRegistryError, "index is inconsistent"):
                    self._run_project(new_story, registries["main"])


if __name__ == "__main__":
    unittest.main()
