from __future__ import annotations

import copy
import hashlib
import json
import threading
import unittest
import uuid
from pathlib import Path

from core.autonomy.outline import OutlineCheckpointStore, build_outline_checkpoint
from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    apply_genesis_event,
    apply_memory_patch,
    capture_historical_revision_dependency_inventory,
    create_genesis_memory_batch,
    create_memory_event_batch,
    create_memory_patch,
    replay_memory_events,
    write_memory_event_batch,
)
from core.memory_v2.storage import load_canonical_memory, save_canonical_memory
from core.path_refs import resolve_path_ref
from core.story_project.authority import (
    activate_event_authority,
    prepare_event_authority_advance,
    project_identity_sha256,
)
from core.story_project.history_revision_execution import (
    HistoricalRevisionExecutionError,
    execute_amend_transaction,
    execute_historical_revision_transaction,
    execute_import_transaction,
    execute_retcon_transaction,
)
from core.story_project.identity import (
    ensure_project_identity,
    load_project_identity,
    project_identity_path,
)
from core.story_project.mapper import SETTING_DIR_NAME
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.read_set import SOURCE_DIRECTORIES, capture_story_project_read_set
from core.runtime_paths import RuntimePaths


class HistoricalRevisionExecutionTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        root = (
            Path.cwd()
            / ".tmp"
            / "history-revision-execution"
            / f"{name[:18]}-{uuid.uuid4().hex[:10]}"
            / "中文路径"
        )
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _sha(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _write_identity(story: Path, identity) -> None:
        project_identity_path(story).write_text(
            json.dumps(identity.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def _fixture(self, name: str) -> dict:
        root = self._case_dir(name)
        story = root / "故事工程"
        for directory in SOURCE_DIRECTORIES:
            (story / directory).mkdir(parents=True)
        setting_path = story / SETTING_DIR_NAME / "world.md"
        setting_path.write_text("The northern gate is sealed.\n", encoding="utf-8")

        identity = ensure_project_identity(
            story, book_id=f"book-{uuid.uuid4().hex[:16]}"
        )
        memory_root = root / "记忆-v2"
        event_store = memory_root / "events"
        genesis = create_genesis_memory_batch(
            book_id=identity.book_id,
            title="History revision execution fixture",
            source_project_digest="1" * 64,
            context_digest="2" * 64,
            authority_epoch=1,
        )
        projection = apply_genesis_event(genesis["events"][0])
        write_memory_event_batch(event_store, genesis)
        save_canonical_memory(memory_root / "canonical_memory.json", projection)
        identity = activate_event_authority(
            story,
            expected_identity_sha256=project_identity_sha256(story),
            head_event_hash=projection["head_event_hash"],
        )

        prose: dict[int, Path] = {}
        previous_batch_hash = genesis["batch_hash"]
        for chapter in range(1, 4):
            prose_path = canonical_prose_path(story, chapter, f"Chapter {chapter}")
            prose_path.write_text(
                f"Chapter {chapter}: Alice carried the red key through gate {chapter}.\n",
                encoding="utf-8",
            )
            prose[chapter] = prose_path
            body = prose_path.read_text(encoding="utf-8")
            patch = create_memory_patch(
                patch_id=f"chapter-{chapter}",
                source_kind="committed_chapter",
                source_path=f"chapter:{chapter}",
                operations=[
                    {
                        "op": "update_current_state",
                        "value": {
                            "chapter_index": chapter + 1,
                            "last_committed_chapter_index": chapter,
                        },
                    },
                    {
                        "op": "update_world",
                        "value": {f"chapter_{chapter}_fact": f"red-key-{chapter}"},
                    },
                ],
            )
            updated, events = apply_memory_patch(
                projection,
                patch,
                reducer_version=CURRENT_REDUCER_VERSION,
                event_context={
                    "chapter_body": body,
                    "evidence_spans": [
                        {"start_char": 0, "end_char": len(body), "quote": body}
                    ],
                    "authority_epoch": 1,
                },
            )
            batch = create_memory_event_batch(
                book_id=identity.book_id,
                patch=patch,
                events=events,
                expected_revision=projection["revision"],
                previous_batch_hash=previous_batch_hash,
                source_project_digest=str(chapter) * 64,
                context_digest=str(chapter + 3) * 64,
                batch_kind="chapter",
                publication_status="committed",
                schema_version="2.2",
                reducer_version=CURRENT_REDUCER_VERSION,
            )
            write_memory_event_batch(event_store, batch)
            save_canonical_memory(memory_root / "canonical_memory.json", updated)
            identity = prepare_event_authority_advance(
                identity,
                expected_authority_epoch=1,
                expected_head_event_hash=projection["head_event_hash"],
                new_head_event_hash=updated["head_event_hash"],
            )
            self._write_identity(story, identity)
            projection = updated
            previous_batch_hash = batch["batch_hash"]

        revision_source = root / "修订依据.txt"
        revision_text = "Official correction: Alice carried the blue key through gate one."
        revision_source.write_text(revision_text, encoding="utf-8")
        alice = revision_text.index("Alice")
        return {
            "root": root,
            "story": story,
            "memory_root": memory_root,
            "event_store": event_store,
            "projection": projection,
            "prose": prose,
            "revision_source": revision_source,
            "setting_path": setting_path,
            "evidence_spans": [
                {"start_char": alice, "end_char": alice + 5, "quote": "Alice"}
            ],
        }

    def _kwargs(self, fixture: dict, transaction_id: str) -> dict:
        identity = load_project_identity(fixture["story"])
        projection = load_canonical_memory(
            fixture["memory_root"] / "canonical_memory.json"
        )
        next_chapter = int(projection["current_state"]["chapter_index"])
        read_set = capture_story_project_read_set(
            fixture["story"], next_chapter, project_identity=identity
        )
        inventory = capture_historical_revision_dependency_inventory(
            story_project_root=fixture["story"],
            book_id=identity.book_id,
            authority_epoch=1,
            head_event_hash=projection["head_event_hash"],
            historical_chapter_index=1,
            canonical_next_chapter_index=next_chapter,
        )
        historical = fixture["prose"][1]
        revision_source = fixture["revision_source"]
        return {
            "memory_root": fixture["memory_root"],
            "story_project_root": fixture["story"],
            "transaction_id": transaction_id,
            "historical_chapter_index": 1,
            "historical_chapter_path": historical,
            "expected_historical_chapter_sha256": self._sha(
                historical.read_bytes()
            ),
            "revision_source_path": revision_source,
            "expected_revision_source_sha256": self._sha(
                revision_source.read_bytes()
            ),
            "evidence_spans": copy.deepcopy(fixture["evidence_spans"]),
            "operations": [
                {"op": "update_world", "value": {"chapter_1_fact": "blue-key-1"}}
            ],
            "authority_epoch": 1,
            "expected_head_event_hash": projection["head_event_hash"],
            "expected_revision": projection["revision"],
            "source_project_digest": read_set["membership_fingerprint"],
            "context_digest": read_set["context_digest"],
            "dependency_inventory": inventory,
        }

    def _install_late_outline_unfenced(
        self,
        fixture: dict,
        kwargs: dict,
        *,
        suffix: str,
    ) -> None:
        # Deliberately bypass the cooperative dependency fence while still
        # publishing a complete revision+latest pair.  This exercises the
        # defensive inventory recheck against an out-of-contract filesystem
        # writer; the separate concurrency test below uses the real Store API.
        autonomy_root, checkpoint = self._late_outline_checkpoint(
            fixture, kwargs, suffix=suffix
        )
        chapter_dir = (
            autonomy_root
            / "outline_checkpoints"
            / checkpoint["session_id"]
            / f"chapter-{int(checkpoint['chapter_index']):06d}"
        )
        revisions = chapter_dir / "revisions"
        revisions.mkdir(parents=True, exist_ok=True)
        (revisions / f"{checkpoint['checkpoint_id']}.json").write_text(
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (chapter_dir / "latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "checkpoint_hash": checkpoint["checkpoint_hash"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _late_outline_checkpoint(
        self,
        fixture: dict,
        kwargs: dict,
        *,
        suffix: str,
    ) -> tuple[Path, dict]:
        identity = load_project_identity(fixture["story"])
        inventory = kwargs["dependency_inventory"]
        chapter = int(inventory["canonical_next_chapter_index"])
        checkpoint = build_outline_checkpoint(
            book_id=identity.book_id,
            session_id=f"late-session-{suffix}",
            plan_id=f"late-plan-{suffix}",
            arc_plan_id=f"late-arc-{suffix}",
            chapter_index=chapter,
            planned_target_hash="7" * 64,
            source_snapshot_hash="8" * 64,
            authority_epoch=int(kwargs["authority_epoch"]),
            authority_head_event_hash=str(kwargs["expected_head_event_hash"]),
            outline_input_digest="9" * 64,
            provider_profile="deterministic",
            execution_kind="deterministic",
            outline_text="A dependency that appeared after historical prepare.",
            canonical_relative_path=canonical_outline_path(
                fixture["story"], chapter
            ).relative_to(fixture["story"]).as_posix(),
            canonical_before_sha256=None,
            created_at="2026-07-15T00:00:00+00:00",
        )
        autonomy_root = RuntimePaths.for_story_project(
            fixture["story"]
        ).runtime_dir / "autonomy"
        return autonomy_root, checkpoint

    @staticmethod
    def _authority_bytes(fixture: dict) -> dict[str, object]:
        batches = {
            path.name: path.read_bytes()
            for path in sorted((fixture["event_store"] / "batches").glob("*.json"))
        }
        return {
            "identity": project_identity_path(fixture["story"]).read_bytes(),
            "canonical": (
                fixture["memory_root"] / "canonical_memory.json"
            ).read_bytes(),
            "batches": batches,
        }

    def test_generic_and_three_wrappers_commit_without_source_or_delivery_targets(self) -> None:
        cases = (
            (
                "generic",
                "amend",
                lambda kwargs, fault: execute_historical_revision_transaction(
                    revision_kind="amend", fault_injector=fault, **kwargs
                ),
            ),
            (
                "amend",
                "amend",
                lambda kwargs, fault: execute_amend_transaction(
                    fault_injector=fault, **kwargs
                ),
            ),
            (
                "import",
                "import",
                lambda kwargs, fault: execute_import_transaction(
                    fault_injector=fault, **kwargs
                ),
            ),
            (
                "retcon",
                "retcon",
                lambda kwargs, fault: execute_retcon_transaction(
                    fault_injector=fault, **kwargs
                ),
            ),
        )
        for label, kind, execute in cases:
            with self.subTest(case=label):
                fixture = self._fixture(f"api-{label}")
                kwargs = self._kwargs(fixture, f"api-{label}-001")
                prose_before = fixture["prose"][1].read_bytes()
                revision_before = fixture["revision_source"].read_bytes()
                applied_paths: list[Path] = []

                def observe(point, _index, path):
                    if point == "before_apply_target":
                        applied_paths.append(path.resolve())

                result = execute(kwargs, observe)
                self.assertEqual("completed", result["status"])
                self.assertEqual(kind, result["revision_kind"])
                self.assertFalse(result["idempotent"])
                self.assertEqual([], result["publication_receipt"]["delivery_jobs"])
                self.assertEqual([], result["publication_receipt"]["artifacts"])
                self.assertEqual(
                    project_identity_path(fixture["story"]).resolve(), applied_paths[-1]
                )
                self.assertNotIn(fixture["prose"][1].resolve(), applied_paths)
                self.assertNotIn(fixture["revision_source"].resolve(), applied_paths)
                self.assertEqual(prose_before, fixture["prose"][1].read_bytes())
                self.assertEqual(
                    revision_before, fixture["revision_source"].read_bytes()
                )
                replay = replay_memory_events(
                    fixture["event_store"], use_checkpoint=False
                )["projection"]
                self.assertEqual(result["current_projection"], replay)
                self.assertEqual(
                    replay["head_event_hash"],
                    load_project_identity(fixture["story"]).authority[
                        "head_event_hash"
                    ],
                )

                repeated = execute(kwargs, None)
                self.assertEqual("already_committed", repeated["status"])
                self.assertTrue(repeated["idempotent"])
                self.assertEqual(result["run_id"], repeated["run_id"])

    def test_pre_marker_failure_rolls_back_and_same_parameters_retry_once(self) -> None:
        fixture = self._fixture("pre-marker")
        kwargs = self._kwargs(fixture, "pre-marker-001")
        before = self._authority_bytes(fixture)
        prose_before = fixture["prose"][1].read_bytes()
        revision_before = fixture["revision_source"].read_bytes()
        fired = False

        def fail(point, index, _path):
            nonlocal fired
            if point == "after_apply_target" and index == 3 and not fired:
                fired = True
                raise OSError("simulated pre-marker crash")

        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_atomic_execution_incomplete",
        ):
            execute_amend_transaction(fault_injector=fail, **kwargs)
        self.assertEqual(before, self._authority_bytes(fixture))
        self.assertEqual(prose_before, fixture["prose"][1].read_bytes())
        self.assertEqual(revision_before, fixture["revision_source"].read_bytes())
        revision_root = (
            fixture["memory_root"] / "history_revisions" / "pre-marker-001"
        )
        self.assertFalse(
            revision_root.exists()
            and any(path.is_file() for path in revision_root.rglob("*"))
        )

        completed = execute_amend_transaction(**kwargs)
        self.assertEqual("completed", completed["status"])
        batches = [
            item
            for item in replay_memory_events(
                fixture["event_store"], use_checkpoint=False
            )["patch_index"]
            if item == "history_amend_pre-marker-001"
        ]
        self.assertEqual(1, len(batches))

    def test_every_post_marker_fault_recovers_forward_to_one_receipt(self) -> None:
        points = (
            "after_commit_marker",
            "before_publication_target",
            "after_publication_target",
            "before_publication_receipt",
            "after_publication_receipt",
        )
        for point in points:
            with self.subTest(point=point):
                fixture = self._fixture(f"post-{point}")
                transaction_id = f"post-{point.replace('_', '-')}-001"
                kwargs = self._kwargs(fixture, transaction_id)
                fired = False

                def fail(actual, _index, _path):
                    nonlocal fired
                    if actual == point and not fired:
                        fired = True
                        raise OSError(f"simulated fault at {point}")

                with self.assertRaisesRegex(
                    HistoricalRevisionExecutionError,
                    "historical_revision_atomic_execution_incomplete",
                ):
                    execute_retcon_transaction(fault_injector=fail, **kwargs)
                recovered = execute_retcon_transaction(**kwargs)
                self.assertEqual("recovered", recovered["status"])
                self.assertTrue(recovered["recovered"])
                self.assertTrue(recovered["already_committed"])
                self.assertEqual([], recovered["publication_receipt"]["delivery_jobs"])
                patch_id = f"history_retcon_{transaction_id}"
                replay = replay_memory_events(
                    fixture["event_store"], use_checkpoint=False
                )
                self.assertEqual(
                    1, sum(item == patch_id for item in replay["patch_index"])
                )
                receipt_dir = (
                    fixture["memory_root"]
                    / "history_revision_execution"
                    / "publication_receipts"
                )
                self.assertEqual(1, len(list(receipt_dir.glob("*.json"))))

    def test_completed_detection_rejects_same_transaction_with_other_request_and_uuid(self) -> None:
        fixture = self._fixture("idempotency")
        kwargs = self._kwargs(fixture, "idempotency-001")
        mismatched_uuid = dict(kwargs)
        mismatched_uuid["story_project_root_uuid"] = str(uuid.uuid4())
        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError, "story_project_root_uuid_mismatch"
        ):
            execute_import_transaction(**mismatched_uuid)

        first = execute_import_transaction(**kwargs)
        second = execute_import_transaction(**kwargs)
        self.assertEqual("completed", first["status"])
        self.assertEqual("already_committed", second["status"])

        conflict = copy.deepcopy(kwargs)
        conflict["operations"] = [
            {"op": "update_world", "value": {"chapter_1_fact": "green-key-1"}}
        ]
        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_idempotency_conflict",
        ):
            execute_import_transaction(**conflict)

    def test_story_project_read_set_is_rechecked_at_prepare_preapply_and_premarker(self) -> None:
        # Prepare: caller digests are stale before the pure prepare begins.
        fixture = self._fixture("cas-prepare")
        kwargs = self._kwargs(fixture, "cas-prepare-001")
        fixture["setting_path"].write_text("prepare drift\n", encoding="utf-8")
        with self.assertRaisesRegex(
            Exception, "historical_revision_context_digest_mismatch"
        ):
            execute_amend_transaction(**kwargs)
        self.assertEqual(4, len(list((fixture["event_store"] / "batches").glob("*.json"))))

        # Pre-apply: mutate after PersistenceV2 prepare but before commit.
        fixture = self._fixture("cas-preapply")
        kwargs = self._kwargs(fixture, "cas-preapply-001")
        setting_before = fixture["setting_path"].read_bytes()
        authority_before = self._authority_bytes(fixture)

        def drift_preapply(point, _index, _path):
            if point == "before_history_revision_commit":
                fixture["setting_path"].write_text("pre-apply drift\n", encoding="utf-8")

        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_atomic_execution_incomplete",
        ):
            execute_amend_transaction(fault_injector=drift_preapply, **kwargs)
        self.assertEqual(authority_before, self._authority_bytes(fixture))
        fixture["setting_path"].write_bytes(setting_before)
        self.assertEqual("completed", execute_amend_transaction(**kwargs)["status"])

        # Pre-marker: mutate after targets are applied; the read-set CAS must
        # make PersistenceV2 restore every authority target before returning.
        fixture = self._fixture("cas-premarker")
        kwargs = self._kwargs(fixture, "cas-premarker-001")
        setting_before = fixture["setting_path"].read_bytes()
        authority_before = self._authority_bytes(fixture)

        def drift_premarker(point, _index, _path):
            if point == "before_commit_marker":
                fixture["setting_path"].write_text("pre-marker drift\n", encoding="utf-8")

        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_atomic_execution_incomplete",
        ):
            execute_amend_transaction(fault_injector=drift_premarker, **kwargs)
        self.assertEqual(authority_before, self._authority_bytes(fixture))
        fixture["setting_path"].write_bytes(setting_before)
        self.assertEqual("completed", execute_amend_transaction(**kwargs)["status"])

    def test_revision_source_cas_is_rechecked_before_first_apply(self) -> None:
        fixture = self._fixture("revision-source-cas")
        kwargs = self._kwargs(fixture, "revision-source-cas-001")
        authority_before = self._authority_bytes(fixture)
        prose_before = fixture["prose"][1].read_bytes()
        revision_before = fixture["revision_source"].read_bytes()

        def drift_revision_source(point, _index, _path):
            if point == "before_history_revision_commit":
                fixture["revision_source"].write_text(
                    "A different correction appeared after prepare.", encoding="utf-8"
                )

        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_atomic_execution_incomplete",
        ):
            execute_retcon_transaction(
                fault_injector=drift_revision_source, **kwargs
            )
        self.assertEqual(authority_before, self._authority_bytes(fixture))
        self.assertEqual(prose_before, fixture["prose"][1].read_bytes())
        self.assertNotEqual(revision_before, fixture["revision_source"].read_bytes())

        fixture["revision_source"].write_bytes(revision_before)
        self.assertEqual("completed", execute_retcon_transaction(**kwargs)["status"])

    def test_receipt_pathrefs_resolve_only_to_memory_and_identity(self) -> None:
        fixture = self._fixture("pathrefs")
        result = execute_retcon_transaction(
            **self._kwargs(fixture, "pathrefs-001")
        )
        root_map = {
            "story_project": fixture["story"],
            "runtime": fixture["memory_root"],
        }
        paths = [
            resolve_path_ref(item["path_ref"], root_map).resolve()
            for item in result["publication_receipt"]["apply_targets"]
        ]
        self.assertEqual(project_identity_path(fixture["story"]).resolve(), paths[-1])
        self.assertNotIn(fixture["prose"][1].resolve(), paths)
        self.assertNotIn(fixture["revision_source"].resolve(), paths)
        for path in paths[:-1]:
            path.relative_to(fixture["memory_root"].resolve())

    def test_completed_receipt_and_canonical_tamper_fail_closed(self) -> None:
        fixture = self._fixture("receipt-tamper")
        kwargs = self._kwargs(fixture, "receipt-tamper-001")
        result = execute_amend_transaction(**kwargs)
        root_map = {
            "story_project": fixture["story"],
            "runtime": fixture["memory_root"],
        }
        receipt_path = resolve_path_ref(
            result["record"]["publication_receipt"]["path_ref"], root_map
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["receipt_hash"] = "0" * 64
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_receipt_invalid",
        ):
            execute_amend_transaction(**kwargs)

        fixture = self._fixture("canonical-tamper")
        kwargs = self._kwargs(fixture, "canonical-tamper-001")
        execute_amend_transaction(**kwargs)
        canonical_path = fixture["memory_root"] / "canonical_memory.json"
        canonical = load_canonical_memory(canonical_path)
        canonical["world"]["unreplayed_tamper"] = True
        save_canonical_memory(canonical_path, canonical)
        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_revision_canonical_drift",
        ):
            execute_amend_transaction(**kwargs)

    def test_dependency_inventory_is_rechecked_before_apply_and_marker(self) -> None:
        for point in ("before_apply_target", "before_commit_marker"):
            with self.subTest(point=point):
                fixture = self._fixture(f"inventory-{point}")
                transaction_id = f"inventory-{point.replace('_', '-')}-001"
                kwargs = self._kwargs(fixture, transaction_id)
                mutated = False

                def add_outline(event, _index, _path):
                    nonlocal mutated
                    if event == point and not mutated:
                        mutated = True
                        self._install_late_outline_unfenced(
                            fixture,
                            kwargs,
                            suffix=point.replace("_", "-"),
                        )

                with self.assertRaisesRegex(
                    HistoricalRevisionExecutionError,
                    "historical_revision_atomic_execution_incomplete",
                ):
                    execute_amend_transaction(
                        fault_injector=add_outline,
                        **kwargs,
                    )
                self.assertTrue(mutated)

                fresh = self._kwargs(fixture, transaction_id)
                self.assertNotEqual(
                    kwargs["dependency_inventory"]["inventory_hash"],
                    fresh["dependency_inventory"]["inventory_hash"],
                )
                self.assertEqual(
                    "completed", execute_amend_transaction(**fresh)["status"]
                )

    def test_real_outline_writer_is_fenced_through_history_commit_marker(self) -> None:
        fixture = self._fixture("inventory-concurrent")
        kwargs = self._kwargs(fixture, "inventory-concurrent-001")
        autonomy_root, checkpoint = self._late_outline_checkpoint(
            fixture, kwargs, suffix="concurrent"
        )
        store = OutlineCheckpointStore(
            autonomy_root,
            story_project_root=fixture["story"],
        )
        attempting = threading.Event()
        finished = threading.Event()
        writer_errors: list[Exception] = []
        writer: threading.Thread | None = None

        def publish_outline() -> None:
            attempting.set()
            try:
                store.create(checkpoint)
            except Exception as exc:
                writer_errors.append(exc)
            finally:
                finished.set()

        def race_at_marker(event, _index, _path) -> None:
            nonlocal writer
            if event != "before_commit_marker" or writer is not None:
                return
            writer = threading.Thread(target=publish_outline, daemon=True)
            writer.start()
            self.assertTrue(attempting.wait(timeout=2.0))
            self.assertFalse(
                finished.wait(timeout=0.2),
                "outline writer crossed the history dependency fence before marker",
            )

        committed = execute_amend_transaction(
            fault_injector=race_at_marker,
            **kwargs,
        )
        self.assertEqual("completed", committed["status"])
        self.assertIsNotNone(writer)
        writer.join(timeout=10.0)
        self.assertFalse(writer.is_alive())
        self.assertEqual(1, len(writer_errors))
        self.assertRegex(str(writer_errors[0]), "outline_checkpoint_authority_stale")
        self.assertIsNone(
            OutlineCheckpointStore(autonomy_root).load(
                checkpoint["session_id"], int(checkpoint["chapter_index"])
            )
        )

    def test_completed_replay_uses_embedded_evidence_not_ephemeral_source(self) -> None:
        fixture = self._fixture("ephemeral-source")
        kwargs = self._kwargs(fixture, "ephemeral-source-001")
        committed = execute_import_transaction(**kwargs)

        fixture["revision_source"].unlink()
        missing = execute_import_transaction(**kwargs)
        self.assertEqual("already_committed", missing["status"])
        self.assertEqual(committed["head_event_hash"], missing["head_event_hash"])

        fixture["revision_source"].write_text(
            "This later operator note is not the committed evidence.",
            encoding="utf-8",
        )
        drifted = execute_import_transaction(**kwargs)
        self.assertEqual("already_committed", drifted["status"])
        self.assertEqual(committed["head_event_hash"], drifted["head_event_hash"])

    def test_completed_replay_rejects_historical_prose_tamper(self) -> None:
        fixture = self._fixture("historical-prose-tamper")
        kwargs = self._kwargs(fixture, "historical-prose-tamper-001")
        self.assertEqual("completed", execute_amend_transaction(**kwargs)["status"])

        fixture["prose"][1].write_text(
            "Tampered published historical prose.\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            HistoricalRevisionExecutionError,
            "historical_chapter_source_drift",
        ):
            execute_amend_transaction(**kwargs)

    def test_old_completed_revision_replays_after_descendant_authority_advance(self) -> None:
        fixture = self._fixture("descendant-replay")
        first_kwargs = self._kwargs(fixture, "descendant-first-001")
        first = execute_amend_transaction(**first_kwargs)
        second_kwargs = self._kwargs(fixture, "descendant-second-001")
        second = execute_import_transaction(**second_kwargs)

        replayed = execute_amend_transaction(**first_kwargs)
        self.assertEqual("already_committed", replayed["status"])
        self.assertEqual(first["head_event_hash"], replayed["head_event_hash"])
        self.assertEqual(
            second["current_head_event_hash"],
            replayed["current_head_event_hash"],
        )
        self.assertNotEqual(
            replayed["head_event_hash"], replayed["current_head_event_hash"]
        )

    def test_windows_reserved_or_trailing_transaction_ids_are_rejected(self) -> None:
        fixture = self._fixture("windows-ids")
        for transaction_id in ("CON", "con.txt", "NUL", "COM1", "LPT9", "trailing."):
            with self.subTest(transaction_id=transaction_id):
                kwargs = self._kwargs(fixture, transaction_id)
                with self.assertRaisesRegex(
                    HistoricalRevisionExecutionError,
                    "historical_revision_transaction_id_invalid",
                ):
                    execute_amend_transaction(**kwargs)


if __name__ == "__main__":
    unittest.main()
