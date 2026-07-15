from __future__ import annotations

import hashlib
import copy
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from api.contracts import ModelResponse
from core.autonomy.cli import _story_source_digest
from core.autonomy.arc import ArcPlanError, build_run_arc_plan
from core.autonomy.plans import build_source_snapshot, compile_instruction_plan
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.runner import AutonomyRunner, AutonomyRunnerError
from core.autonomy.session import AutonomySessionStore
from core.context_budget import ContextBudgetError, RunBudgetLimits
from core.delivery import DeliveryQueue, FileDeliveryAdapter, delivery_outcome
from core.engine.executor import AgentExecutor
from core.engine.persistence_v2 import verify_publication_receipt
from core.memory_v2 import (
    apply_genesis_event,
    canonical_memory_to_snapshot,
    create_genesis_memory_batch,
    load_memory_event_batches,
    replay_memory_events,
    save_canonical_memory,
    write_memory_event_batch,
)
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import current_model_call_runtime
from core.path_refs import resolve_path_ref
from core.runtime_paths import RuntimePaths
from core.schema import validate_schema
from core.story_project.authority import activate_event_authority, project_identity_sha256
from core.story_project.identity import ensure_project_identity, load_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path, infer_next_chapter
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.writer import StoryProjectWritebackConfig


def _profiles(
    book_id: str,
    delivery_uuid: str,
    *,
    chapters: int = 3,
    model_calls: int = 12,
) -> TrustedProfiles:
    return TrustedProfiles.from_dict(
        {
            "schema_version": "1.0",
            "profile_set_id": "autonomy-e2e-profiles",
            "story_projects": [
                {
                    "profile_id": "book",
                    "book_id": book_id,
                    "root_uuid": "story-root-e2e",
                }
            ],
            "provider_models": [
                {
                    "profile_id": "dry-provider",
                    "provider": "openai",
                    "endpoint_type": "official",
                    "model": "unused-dry-model",
                    "max_output_tokens": 4096,
                }
            ],
            "file_deliveries": [
                {
                    "profile_id": "required-export",
                    "target_kind": "file",
                    "root_uuid": delivery_uuid,
                    "path_template": "exports/chapter-{chapter_index}-{run_id}.json",
                    "requires_run_id": True,
                    "requires_chapter_id": True,
                }
            ],
            "budgets": [
                {
                    "profile_id": "session-budget",
                    "max_chapters": chapters,
                    "max_model_calls": model_calls,
                    "max_input_tokens": 100000,
                    "max_output_tokens": 50000,
                    "max_wall_seconds": 3600,
                }
            ],
            "quality_policies": [
                {
                    "profile_id": "minimal",
                    "policy": "minimal",
                    "minimum_score": 0,
                }
            ],
            "defaults": {
                "story_project": "book",
                "provider_model": "dry-provider",
                "file_delivery": "required-export",
                "budget": "session-budget",
                "quality_policy": "minimal",
            },
        }
    )


def _validation(_snapshot: dict, _chapter: str, _decision: dict) -> dict:
    return validate_schema(
        {
            "ok": True,
            "requested_focus": ["logic"],
            "executed_checks": ["logic"],
            "skipped_checks": [],
            "checks": [{"name": "logic", "ok": True, "problems": []}],
            "problems": [],
            "blocking_problem_count": 0,
            "warning_count": 0,
            "severity_counts": [],
            "deterministic_repair_count": 0,
            "manual_review_count": 0,
            "repair_action_counts": [],
        },
        "validation_result.schema.json",
    )


def _analysis(chapter: str, validation: dict) -> dict:
    return validate_schema(
        {
            "events": [{"text": "The route opens under pressure."}],
            "character_changes": [],
            "world_changes": [],
            "new_locations": [],
            "story_state": {
                "last_chapter_ending": chapter[-80:],
                "last_scene_location": "passage",
                "last_scene_characters": [],
                "open_threads": ["Reach the signal."],
                "required_opening_bridge": "",
            },
            "spatial_state": {
                "spaces": {},
                "connections": [],
                "character_positions": {},
                "blocked_paths": [],
                "last_transition": {},
            },
            "conflicts": [],
            "validation_ok": bool(validation.get("ok")),
            "summary": chapter[:80],
        },
        "analysis_result.schema.json",
    )


def _case(
    tmp_path: Path,
    *,
    chapters: int = 3,
    executor_dry_run: bool = True,
    model_calls: int = 12,
):
    book = tmp_path / "book"
    for directory in CORE_DIRECTORY_NAMES:
        (book / directory).mkdir(parents=True)
    identity = ensure_project_identity(book, book_id="book-autonomy-runner-e2e")
    paths = RuntimePaths.for_story_project(book)
    memory_root = paths.memory_dir / "v2"
    genesis = create_genesis_memory_batch(
        book_id=identity.book_id,
        title="Autonomy E2E",
        source_project_digest="1" * 64,
        context_digest="2" * 64,
        language="en",
        authority_epoch=1,
    )
    projection = apply_genesis_event(genesis["events"][0])
    write_memory_event_batch(memory_root / "events", genesis)
    save_canonical_memory(memory_root / "canonical_memory.json", projection)
    identity = activate_event_authority(
        book,
        expected_identity_sha256=project_identity_sha256(book),
        head_event_hash=projection["head_event_hash"],
    )
    paths.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    paths.snapshot_path.write_text(
        json.dumps(canonical_memory_to_snapshot(projection), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    delivery_uuid = str(uuid.uuid4())
    profiles = _profiles(
        identity.book_id,
        delivery_uuid,
        chapters=chapters,
        model_calls=model_calls,
    )
    external = tmp_path / "external"
    external.mkdir()
    roots = paths.root_map(book)

    def source_loader():
        current = load_project_identity(book)
        assert current is not None
        authority = current.authority or {}
        return build_source_snapshot(
            book_id=current.book_id,
            root_uuid="story-root-e2e",
            authority_epoch=int(authority["authority_epoch"]),
            authority_head_event_hash=authority["head_event_hash"],
            canonical_next_chapter=infer_next_chapter(book),
            source_digest=_story_source_digest(book, current.to_dict()),
        )

    source = source_loader()
    plan = compile_instruction_plan(
        f"连续写 {chapters}章",
        trusted_profiles=profiles,
        source_snapshot=source,
    )
    sessions = AutonomySessionStore(
        paths.runtime_dir / "autonomy",
        trusted_profiles=profiles,
        publication_root_map=roots,
    )
    queue = DeliveryQueue(paths.delivery_dir)
    executor_requests = []

    def executor_factory(request):
        executor_requests.append(request)
        current = load_project_identity(book)
        assert current is not None
        loader = build_generation_story_project_context_loader(
            story_project=book,
            chapter=request.chapter_index,
            project_identity=current,
            outline_override=request.outline_checkpoint,
        )
        return AgentExecutor(
            snapshot_path=paths.snapshot_path,
            memory_path=paths.memory_dir / "unused.json",
            memory_source="file",
            run_dir=paths.run_dir,
            chapter_dir=paths.chapter_dir,
            persistence_dir=paths.persistence_dir,
            dry_run=executor_dry_run,
            use_run_history=False,
            memory_loader=lambda: {},
            polisher=lambda chapter: chapter,
            validator=_validation,
            analyzer=_analysis,
            story_project_context_loader=loader,
            story_project_writeback=StoryProjectWritebackConfig(mode="apply"),
            quality_policy="minimal",
            enable_execution_provenance=True,
            run_budget_limits=request.runtime_context.expected_run_budget_limits(),
            file_delivery_profile=request.file_delivery_profile,
            delivery_queue=queue,
            autonomy_run_context=request.runtime_context,
        )

    runner = AutonomyRunner(
        sessions=sessions,
        source_snapshot_loader=source_loader,
        executor_factory=executor_factory,
        story_project_root=book,
        run_dir=paths.run_dir,
        persistence_dir=paths.persistence_dir,
        publication_root_map=roots,
        delivery_queue=queue,
        operator_delivery_roots={delivery_uuid: external},
        deterministic_stages=("polish",),
    )
    return {
        "book": book,
        "paths": paths,
        "profiles": profiles,
        "external": external,
        "delivery_uuid": delivery_uuid,
        "roots": roots,
        "plan": plan,
        "sessions": sessions,
        "queue": queue,
        "runner": runner,
        "executor_requests": executor_requests,
    }


def _workspace(name: str) -> Path:
    root = Path.cwd() / ".tmp" / "ar" / uuid.uuid4().hex[:8]
    root.mkdir(parents=True)
    return root


def _assert_three_chapter_runner_is_receipt_counted_and_exactly_delivered():
    case = _case(_workspace("three"), chapters=3)
    result = case["runner"].execute_plan(case["plan"])

    assert result["stopped_reason"] == "completed"
    assert result["session"]["completed_count"] == 3
    assert len(case["executor_requests"]) == 3
    assert len(list(case["external"].rglob("*.json"))) == 3
    assert all(canonical_outline_path(case["book"], index).is_file() for index in range(1, 4))

    ledger = case["sessions"].completion_ledger(result["session"]["session_id"])
    completions = ledger.rebuild()
    assert [item["chapter_index"] for item in completions] == [1, 2, 3]
    for chapter_index in range(1, 4):
        chain = case["sessions"].stage_receipts.load_chain(
            result["session"]["session_id"], chapter_index
        )
        assert [item["stage"] for item in chain] == [
            "outline",
            "scene_plan",
            "draft",
            "polish",
            "validator",
        ]

    prose_files = sorted((case["book"] / CORE_DIRECTORY_NAMES[2]).glob("*.md"))
    assert len(prose_files) == 3
    prose_hashes = {hashlib.sha256(path.read_bytes()).hexdigest() for path in prose_files}
    event_batches = load_memory_event_batches(
        case["paths"].memory_dir / "v2" / "events"
    )[1:]
    assert len(event_batches) == 3
    for batch in event_batches:
        body_hashes = {event["chapter_body_sha256"] for event in batch["events"]}
        assert len(body_hashes) == 1
        assert next(iter(body_hashes)) in prose_hashes

    for completion in completions:
        publication = ledger.load_publication(completion["publication_receipt_hash"])
        verification = verify_publication_receipt(
            publication, root_map=case["roots"]
        )
        assert verification["valid"] and verification["committed"]
        kinds = {item["kind"] for item in publication["artifacts"]}
        assert {"autonomy_outline_evidence", "autonomy_stage_evidence"} <= kinds
        final_record = json.loads(
            resolve_path_ref(
                publication["final_run"]["path_ref"], case["roots"]
            ).read_text(encoding="utf-8")
        )
        fulfillment_evidence = final_record["run"]["analysis"]["fulfillment_evidence"]
        evidence_payload = dict(fulfillment_evidence)
        evidence_hash = evidence_payload.pop("evidence_hash")
        assert evidence_hash == canonical_json_hash(evidence_payload)

    replay = replay_memory_events(case["paths"].memory_dir / "v2" / "events")
    assert replay["committed_chapter_count"] == 3

    session_id = result["session"]["session_id"]
    initial_arc = build_run_arc_plan(
        case["plan"], session_id=session_id, created_at="2026-07-14T00:00:00+00:00"
    )
    arc = case["sessions"].arc_plans.load(result["session"]["arc_plan_id"])
    assert arc["targets"][0]["planned"] == initial_arc["targets"][0]["planned"]
    assert arc["targets"][2]["planned"] != initial_arc["targets"][2]["planned"]
    assert all(target["fulfilled"] != target["planned"] for target in arc["targets"])
    assert all(
        target["differences"] == ["relationship", "escalation", "resource_cost"]
        for target in arc["targets"]
    )
    assert all(
        target["fulfillment_assessment"]["evidenced_fields"]
        == ["mainline", "foreshadowing"]
        for target in arc["targets"]
    )
    for target, initial_target in zip(arc["targets"][1:], initial_arc["targets"][1:]):
        changed = {
            field
            for field in target["planned"]
            if target["planned"][field] != initial_target["planned"][field]
        }
        assert changed == {"relationship", "escalation", "resource_cost"}
    assert [item["chapter_index"] for item in arc["adjustments"]] == [2, 3]
    assert len({item["revision"] for item in arc["adjustments"]}) == 2
    assert all(item["chapter_index"] > 1 for item in arc["adjustments"])
    assert all(
        {
            field
            for field in item["before"]
            if item["before"][field] != item["after"][field]
        }
        == {"relationship", "escalation", "resource_cost"}
        for item in arc["adjustments"]
    )

    immutable_targets = copy.deepcopy(arc["targets"])
    replayed = case["runner"].execute_plan(case["plan"])
    assert replayed["stopped_reason"] == "completed"
    assert case["sessions"].arc_plans.load(arc["arc_plan_id"])["targets"] == immutable_targets


class _FailOnceAdapter:
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False

    def deliver(self, job, context):
        if not self.failed:
            self.failed = True
            return delivery_outcome(
                "retryable_failed", code="injected", message="try again"
            )
        return self.delegate.deliver(job, context)


def _assert_required_delivery_failure_blocks_then_resume_succeeds_before_next_chapter(
):
    case = _case(_workspace("delivery-resume"), chapters=2)
    original_bind = case["runner"]._bind_plan_runtime
    flaky = []

    def bind_with_failure(plan):
        original_bind(plan)
        if not flaky:
            flaky.append(_FailOnceAdapter(case["runner"].delivery_adapter))
        case["runner"].delivery_adapter = flaky[0]

    case["runner"]._bind_plan_runtime = bind_with_failure

    first = case["runner"].execute_plan(case["plan"])
    assert first["stopped_reason"] == "required_delivery_blocked"
    assert first["session"]["completed_count"] == 1
    assert len(case["executor_requests"]) == 1

    case["runner"]._bind_plan_runtime = original_bind
    resumed = case["runner"].resume(first["session"]["session_id"])
    assert resumed["stopped_reason"] == "completed"
    assert resumed["session"]["completed_count"] == 2
    assert not resumed["session"]["delivery_blocked"]
    assert len(case["executor_requests"]) == 2
    assert len(list(case["external"].rglob("*.json"))) == 2


class AutonomyRunnerE2ETest(unittest.TestCase):
    def test_one_chapter_runner_reaches_committed_delivery_boundary(self):
        case = _case(_workspace("one"), chapters=1)
        result = case["runner"].execute_plan(case["plan"])
        self.assertEqual("completed", result["stopped_reason"])
        self.assertEqual(1, result["session"]["completed_count"])
        self.assertEqual(1, len(case["executor_requests"]))
        self.assertEqual(1, len(list(case["external"].rglob("*.json"))))

    def test_three_chapter_runner_is_receipt_counted_and_exactly_delivered(self):
        _assert_three_chapter_runner_is_receipt_counted_and_exactly_delivered()

    def test_required_delivery_failure_blocks_then_resume_succeeds_before_next_chapter(
        self,
    ):
        _assert_required_delivery_failure_blocks_then_resume_succeeds_before_next_chapter()

    def test_fulfillment_recovers_from_committed_prose_after_checkpoint_crash(self):
        class AbruptCrash(BaseException):
            pass

        case = _case(_workspace("arc-fulfillment-recovery"), chapters=1)
        original = case["runner"]._record_arc_fulfillment
        fired = {"value": False}

        def crash_once(*args, **kwargs):
            if not fired["value"]:
                fired["value"] = True
                raise AbruptCrash("after completion before arc fulfillment")
            return original(*args, **kwargs)

        with patch.object(
            case["runner"], "_record_arc_fulfillment", side_effect=crash_once
        ):
            with self.assertRaises(AbruptCrash):
                case["runner"].execute_plan(case["plan"])

        session_id = case["sessions"].resolve_session_id("latest")
        resumed = case["runner"].resume(session_id)
        self.assertEqual("completed", resumed["stopped_reason"])
        arc = case["sessions"].arc_plans.load(resumed["session"]["arc_plan_id"])
        target = arc["targets"][0]
        self.assertEqual(
            ["relationship", "escalation", "resource_cost"],
            target["differences"],
        )
        self.assertEqual(
            ["mainline", "foreshadowing"],
            target["fulfillment_assessment"]["evidenced_fields"],
        )
        self.assertEqual(64, len(target["fulfillment_assessment"]["fulfillment_evidence_hash"]))
        self.assertIn("正文:", target["fulfilled"]["mainline"])
        self.assertEqual(1, len(case["executor_requests"]))

    def test_arc_recovery_rejects_receipt_scope_or_existing_target_conflict(self):
        case = _case(_workspace("arc-evidence-binding"), chapters=1)
        result = case["runner"].execute_plan(case["plan"])
        session_id = result["session"]["session_id"]
        ledger = case["sessions"].completion_ledger(session_id)
        completion = ledger.rebuild()[0]
        publication = ledger.load_publication(completion["publication_receipt_hash"])

        wrong_target = dict(completion)
        wrong_target["planned_target_hash"] = "0" * 64
        with self.assertRaisesRegex(
            AutonomyRunnerError, "completion receipt is bound to another Arc target"
        ):
            case["runner"]._record_arc_fulfillment(
                session_id, wrong_target, publication_receipt=publication
            )

        wrong_publication = copy.deepcopy(publication)
        wrong_publication["unhashed_extra"] = "must not be trusted"
        with self.assertRaisesRegex(
            AutonomyRunnerError, "PublicationReceipt integrity validation failed"
        ):
            case["runner"]._record_arc_fulfillment(
                session_id, completion, publication_receipt=wrong_publication
            )

        with patch.object(
            case["sessions"].arc_plans,
            "record_fulfillment",
            side_effect=ArcPlanError(
                "arc_target_already_committed", "injected immutable conflict"
            ),
        ), self.assertRaisesRegex(ArcPlanError, "injected immutable conflict"):
            case["runner"]._reconcile_arc_fulfillment(session_id)

    def test_external_root_overlap_is_rejected_before_session_or_provider(self):
        case = _case(_workspace("overlap"), chapters=1)
        case["runner"].operator_delivery_roots[case["delivery_uuid"]] = case["book"]
        with self.assertRaisesRegex(Exception, "autonomy_operator_root_overlaps_runtime"):
            case["runner"].execute_plan(case["plan"])
        session_dirs = [
            item
            for item in (case["sessions"].root / "sessions").glob("session_*")
            if item.is_dir()
        ]
        self.assertEqual([], session_dirs)
        self.assertEqual([], case["executor_requests"])

    def test_model_receipt_survives_crash_before_stage_receipt_without_second_call(self):
        case = _case(_workspace("model-crash"), chapters=1, executor_dry_run=False)
        from core.engine import executor as executor_module

        real_pipeline = executor_module.run_chapter_pipeline
        network_calls = {"count": 0}

        def receipt_pipeline(input_pack, **kwargs):
            dry_kwargs = dict(kwargs)
            dry_kwargs["dry_run"] = True
            pipeline = real_pipeline(input_pack, **dry_kwargs)
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            assert runtime is not None
            call_id = runtime.new_call_id(provider="openai", stage="chapter_generation")

            def physical_call():
                network_calls["count"] += 1
                return ModelResponse(
                    str(pipeline["merged_chapter"]),
                    usage={"input_tokens": 10, "output_tokens": 20},
                    finish_reason="stop",
                    actual_model="fault-test-model",
                    endpoint_type="official",
                )

            response = runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="openai",
                model="fault-test-model",
                stage="chapter_generation",
                endpoint_type="official",
                request={"input_pack_sha256": hashlib.sha256(input_pack.encode()).hexdigest()},
                max_output_tokens=100,
                operation=physical_call,
                input_tokens=10,
            )
            pipeline["merged_chapter"] = response.text
            return pipeline

        original_factory = case["runner"].executor_factory
        crash_pending = {"value": True}

        def crashing_factory(request):
            executor = original_factory(request)
            if crash_pending["value"]:
                original_after = request.runtime_context.after_stage

                def after_stage(token, **kwargs):
                    if token.authorization["stage"] == "draft":
                        crash_pending["value"] = False
                        raise RuntimeError("crash after model receipt before stage receipt")
                    return original_after(token, **kwargs)

                request.runtime_context.after_stage = after_stage
            return executor

        case["runner"].executor_factory = crashing_factory
        with patch("core.engine.executor.run_chapter_pipeline", side_effect=receipt_pipeline):
            with self.assertRaisesRegex(RuntimeError, "after model receipt"):
                case["runner"].execute_plan(case["plan"])
            self.assertEqual(1, network_calls["count"])
            receipt_paths = list(case["paths"].run_dir.glob("executions/*/model_calls/receipts/*.json"))
            self.assertEqual(1, len(receipt_paths))

            session_id = case["sessions"].resolve_session_id("latest")
            resumed = case["runner"].resume(session_id)

        self.assertEqual("completed", resumed["stopped_reason"])
        self.assertEqual(1, network_calls["count"])
        receipt_paths = list(case["paths"].run_dir.glob("executions/*/model_calls/receipts/*.json"))
        self.assertEqual(1, len(receipt_paths))

    def test_model_call_budget_is_aggregate_across_chapters_and_recovery(self):
        case = _case(
            _workspace("aggregate-budget"),
            chapters=2,
            executor_dry_run=False,
            model_calls=1,
        )
        from core.engine import executor as executor_module

        real_pipeline = executor_module.run_chapter_pipeline
        network_calls = {"count": 0}

        def receipt_pipeline(input_pack, **kwargs):
            dry_kwargs = dict(kwargs)
            dry_kwargs["dry_run"] = True
            pipeline = real_pipeline(input_pack, **dry_kwargs)
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            assert runtime is not None
            call_id = runtime.new_call_id(
                provider="openai", stage="chapter_generation"
            )

            def physical_call():
                network_calls["count"] += 1
                return ModelResponse(
                    str(pipeline["merged_chapter"]),
                    usage={"input_tokens": 10, "output_tokens": 20},
                    finish_reason="stop",
                    actual_model="budget-test-model",
                    endpoint_type="official",
                )

            response = runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="openai",
                model="budget-test-model",
                stage="chapter_generation",
                endpoint_type="official",
                request={
                    "input_pack_sha256": hashlib.sha256(
                        input_pack.encode()
                    ).hexdigest()
                },
                max_output_tokens=100,
                operation=physical_call,
                input_tokens=10,
            )
            pipeline["merged_chapter"] = response.text
            return pipeline

        with patch(
            "core.engine.executor.run_chapter_pipeline", side_effect=receipt_pipeline
        ):
            with self.assertRaisesRegex(
                ContextBudgetError, "max_provider_calls exceeded"
            ):
                case["runner"].execute_plan(case["plan"])

        self.assertEqual(1, network_calls["count"])
        session_id = case["sessions"].resolve_session_id("latest")
        session = case["sessions"].status(session_id)
        self.assertEqual(1, session["completed_count"])
        receipt_paths = list(
            case["paths"].run_dir.glob(
                "executions/*/model_calls/receipts/*.json"
            )
        )
        statuses = sorted(
            json.loads(path.read_text(encoding="utf-8"))["status"]
            for path in receipt_paths
        )
        self.assertEqual(["budget_rejected", "succeeded"], statuses)

    def test_reparse_or_symlink_operator_root_is_rejected_at_construction(self):
        case = _case(_workspace("link-root"), chapters=1)
        link = case["external"].parent / "external-link"
        try:
            os.symlink(case["external"], link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaisesRegex(Exception, "autonomy_operator_root_invalid"):
            AutonomyRunner(
                sessions=case["sessions"],
                source_snapshot_loader=case["runner"].source_snapshot_loader,
                executor_factory=case["runner"].executor_factory,
                story_project_root=case["book"],
                run_dir=case["paths"].run_dir,
                persistence_dir=case["paths"].persistence_dir,
                publication_root_map=case["roots"],
                delivery_queue=case["queue"],
                operator_delivery_roots={case["delivery_uuid"]: link},
            )

    @unittest.skipUnless(
        os.environ.get("NOVELAGENT_RUN_50_CHAPTER_SIM") == "1",
        "set NOVELAGENT_RUN_50_CHAPTER_SIM=1 for the long deterministic acceptance run",
    )
    def test_fifty_chapter_deterministic_acceptance(self):
        case = _case(_workspace("fifty"), chapters=50)
        result = case["runner"].execute_plan(case["plan"])
        self.assertEqual("completed", result["stopped_reason"])
        self.assertEqual(50, result["session"]["completed_count"])
        self.assertEqual(50, len(case["executor_requests"]))
        self.assertEqual(50, len(list(case["external"].rglob("*.json"))))
        arc = case["sessions"].arc_plans.load(result["session"]["arc_plan_id"])
        self.assertEqual(49, len(arc["adjustments"]))
        self.assertEqual(
            list(range(2, 51)),
            [item["chapter_index"] for item in arc["adjustments"]],
        )
