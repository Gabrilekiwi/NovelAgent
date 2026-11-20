from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
import uuid

from api.contracts import MODEL_ENDPOINT_OFFICIAL, ModelResponse
from core.engine.chapter_candidate import (
    ChapterCandidateConflictError,
    ChapterCandidateExecutionError,
    ChapterCandidateExecutor,
    ChapterCandidateIntegrityError,
    ChapterCandidateStore,
)
from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_candidate_resolution import (
    ChapterCandidateProviderResolutionIntegrityError,
    candidate_provider_resolution_path,
    candidate_provider_uncertainty_sha256,
)
from core.engine.persistence import PersistenceLockError
from core.engine.provider_safety import list_unresolved_provider_calls
from core.execution_provenance import build_execution_provenance
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import (
    ProviderCallUncertainError,
    current_model_call_runtime,
)
from core.runtime_paths import RuntimePaths
from core.story_project.activation import (
    activate_story_state,
    build_story_state_calibration_report,
)
from core.story_project.identity import ensure_project_identity
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION


class _SimulatedCrash(BaseException):
    pass


class ChapterCandidateTest(unittest.TestCase):
    def setUp(self) -> None:
        source = Path("tests/fixtures/story_project_semantics/cases/synthetic_standard/book")
        self.book = (
            Path.cwd()
            / ".tmp"
            / "cc"
            / uuid.uuid4().hex[:8]
            / "book"
        )
        shutil.copytree(source, self.book)
        chapter_two = self.book / "大纲" / "细纲_第002章.md"
        chapter_two.write_text(
            chapter_two.read_text(encoding="utf-8").replace(
                "结尾压力：",
                "结尾压力：危险迫使两人立刻做出选择；",
                1,
            ),
            encoding="utf-8",
        )
        identity = ensure_project_identity(self.book)
        self.identity = activate_story_state(
            self.book,
            self._qualified_report(identity.book_id),
        )
        self.intent = {
            "goal": "林澈与周岚进入封闭区并确认信号来源",
            "validation_focus": ["continuity", "spatial", "logic"],
            "notes": ["保持第三人称限知视角"],
        }

    @staticmethod
    def _qualified_report(book_id: str) -> dict:
        return build_story_state_calibration_report(
            book_id=book_id,
            parser_version=SEMANTIC_PARSER_VERSION,
            semantic_schema_version=STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
            target_layout_profile_version="canonical-zh-1",
            evidence={
                "target_sample_count": 1,
                "format_variant_count": 2,
                "managed_round_trip_rate": 1.0,
                "required_field_exact_match_rate": 1.0,
                "authoritative_precision": 1.0,
                "supported_optional_recall": 0.95,
                "unsupported_structure_count": 1,
                "unsupported_structure_captured_count": 1,
                "consecutive_shadow_chapters": 10,
                "blocking_conflict_count": 0,
                "missing_provenance_fields": [],
            },
            generated_at="2026-09-03T00:00:00+00:00",
        )

    @staticmethod
    def _provenance():
        return build_execution_provenance(
            code_bundle_hash="1" * 64,
            code_file_count=1,
            git_commit=None,
            git_dirty=True,
            prompt_hashes={"prompts/chapter_prompt.md": "2" * 64},
            schema_hashes={"schemas/chapter_candidate_record.schema.json": "3" * 64},
            dependency_versions={},
            provider="test",
            model="test-model",
            config={"test_mode": True},
            feature_flags={"chapter_candidate": True},
        )

    def _executor(self, **kwargs) -> ChapterCandidateExecutor:
        return ChapterCandidateExecutor(
            story_project_root=self.book,
            repository_root=Path.cwd(),
            provenance_factory=self._provenance,
            **kwargs,
        )

    def _execute(self, executor: ChapterCandidateExecutor, *, key: str) -> dict:
        return executor.execute(
            chapter_index=2,
            confirmation=self._confirmation(),
            idempotency_key=key,
        )

    def _confirmation(
        self,
        *,
        chapter_index: int = 2,
        intent: dict | None = None,
    ) -> dict:
        trusted = ChapterCandidateContextBuilder(self.book).build(chapter_index)
        identity = trusted.story_project_context["project_identity"]
        payload = {
            "schema_version": "1.0",
            "record_type": "chapter_intent_confirmation",
            "book_id": identity["book_id"],
            "chapter_index": chapter_index,
            "confirmed": True,
            "card_sha256": "4" * 64,
            "context_sha256": trusted.context_sha256,
            "chapter_intent": dict(intent or self.intent),
        }
        payload["confirmation_sha256"] = canonical_json_hash(
            payload,
            exclude_environment_fields=False,
        )
        return payload

    def _formal_sources(self) -> dict[str, str]:
        result: dict[str, str] = {}
        allowed_prefixes = (
            ".novelagent/runtime/candidates/",
            ".novelagent/runtime/runs/.provider-boundary-lock/",
            ".novelagent/runtime/runs/executions/execution_candidate_",
            ".novelagent/runtime/runs/candidate_provider_resolutions/",
        )
        for path in sorted(item for item in self.book.rglob("*") if item.is_file()):
            relative = path.relative_to(self.book).as_posix()
            if relative.startswith(allowed_prefixes):
                continue
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def test_execute_contract_does_not_accept_caller_prepared_state(self) -> None:
        parameters = inspect.signature(ChapterCandidateExecutor.execute).parameters
        self.assertEqual(
            ["self", "chapter_index", "confirmation", "idempotency_key"],
            list(parameters),
        )
        for forbidden in ("story_project_context", "snapshot", "memory_context"):
            self.assertNotIn(forbidden, parameters)

    def test_completed_candidate_is_restart_readable_and_formal_state_is_untouched(self) -> None:
        before = self._formal_sources()
        prose = "林澈贴着闸门听见回声，周岚确认控制器刚从内侧启动。两人交换手势，沿北侧通道进入控制室。"
        result = self._execute(
            self._executor(generator=lambda _input: prose, execution_kind="deterministic"),
            key="author-click-001",
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual(hashlib.sha256(prose.encode("utf-8")).hexdigest(), result["prose"]["sha256"])
        self.assertEqual(before, self._formal_sources())

        paths = RuntimePaths.for_story_project(self.book)
        self.assertFalse(paths.snapshot_path.exists())
        for forbidden in (
            paths.chapter_dir,
            paths.review_dir,
            paths.persistence_dir,
            paths.delivery_dir,
            paths.memory_dir,
        ):
            self.assertFalse(forbidden.exists(), str(forbidden))
        self.assertFalse(any(paths.run_dir.glob("run_*.json")))
        self.assertFalse(any(paths.run_dir.rglob("publication_receipt*.json")))
        self.assertFalse(any(paths.run_dir.rglob("transaction*.json")))

        loaded = ChapterCandidateStore(self.book).load(result["candidate_id"])
        self.assertEqual("completed", loaded["status"])
        self.assertEqual(prose, loaded["prose"])
        self.assertIn("Do not output event ledgers", loaded["input_pack"])
        self.assertNotIn("structured Scene delta", loaded["input_pack"])
        self.assertEqual([], loaded["record"]["evidence"]["intent_hashes"])
        self.assertEqual([], loaded["record"]["evidence"]["receipt_hashes"])
        self.assertIsNone(loaded["record"]["evidence"]["selected_response"])
        source = loaded["intent"]["source"]
        author_confirmation = loaded["intent"]["author_confirmation"]
        self.assertEqual(source["context_sha256"], author_confirmation["context_sha256"])
        self.assertEqual(64, len(author_confirmation["confirmation_sha256"]))
        self.assertEqual("1.0", source["context_builder_version"])
        self.assertEqual("bypass", source["runtime_cache_policy"])
        self.assertIsNone(source["authority_projection_sha256"])
        self.assertEqual(
            source["context_sha256"],
            loaded["record"]["source_verification"]["before"]["context_sha256"],
        )
        self.assertEqual(
            loaded["intent"]["input"]["sha256"],
            loaded["record"]["source_verification"]["after"]["input_sha256"],
        )

    def test_tampered_confirmation_is_rejected_before_claim_or_generation(self) -> None:
        confirmation = self._confirmation()
        confirmation["chapter_intent"]["goal"] = "被篡改但没有重新绑定哈希的目标"
        generator_calls = 0

        def generator(_input: str) -> str:
            nonlocal generator_calls
            generator_calls += 1
            return "不应生成"

        with self.assertRaisesRegex(
            ChapterCandidateIntegrityError,
            "author confirmation hash mismatch",
        ):
            self._executor(
                generator=generator,
                execution_kind="deterministic",
            ).execute(
                chapter_index=2,
                confirmation=confirmation,
                idempotency_key="tampered-confirmation",
            )

        self.assertEqual(0, generator_calls)
        self.assertFalse(
            (RuntimePaths.for_story_project(self.book).runtime_dir / "candidates").exists()
        )

    def test_context_change_after_confirmation_is_rejected_before_claim_or_generation(self) -> None:
        confirmation = self._confirmation()
        outline = self.book / "大纲" / "细纲_第002章.md"
        outline.write_text(
            outline.read_text(encoding="utf-8") + "\n作者确认后修改了章纲。\n",
            encoding="utf-8",
        )
        generator_calls = 0

        def generator(_input: str) -> str:
            nonlocal generator_calls
            generator_calls += 1
            return "不应生成"

        with self.assertRaisesRegex(
            ChapterCandidateConflictError,
            "changed after author confirmation",
        ):
            self._executor(
                generator=generator,
                execution_kind="deterministic",
            ).execute(
                chapter_index=2,
                confirmation=confirmation,
                idempotency_key="stale-confirmation",
            )

        self.assertEqual(0, generator_calls)
        self.assertFalse(
            (RuntimePaths.for_story_project(self.book).runtime_dir / "candidates").exists()
        )

    def test_existing_formal_runtime_artifacts_remain_byte_identical(self) -> None:
        paths = RuntimePaths.for_story_project(self.book)
        protected = {
            paths.snapshot_path: '{"chapter_index": 99, "sentinel": "snapshot"}\n',
            paths.chapter_dir / "existing.md": "existing chapter artifact\n",
            paths.review_dir / "existing.json": '{"sentinel": "review"}\n',
            paths.persistence_dir / "existing.json": '{"sentinel": "persistence"}\n',
            paths.delivery_dir / "existing.json": '{"sentinel": "delivery"}\n',
            paths.memory_dir / "existing.json": '{"sentinel": "memory"}\n',
            paths.run_dir / "chapter_existing.json": '{"sentinel": "run"}\n',
        }
        for path, content in protected.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        before = self._formal_sources()

        result = self._execute(
            self._executor(
                generator=lambda _input: "林澈核对门禁记录，周岚守住通道，两人没有改动任何正式档案。",
                execution_kind="deterministic",
            ),
            key="author-click-existing-runtime",
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual(before, self._formal_sources())

    def test_receipted_model_result_replays_after_crash_without_second_provider_call(self) -> None:
        physical_calls = 0
        prose = "林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。"

        def generator(input_pack: str) -> str:
            nonlocal physical_calls
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            call_id = runtime.new_call_id(provider="test", stage="chapter_candidate_generation")

            def physical_call() -> ModelResponse:
                nonlocal physical_calls
                physical_calls += 1
                return ModelResponse(
                    prose,
                    usage={"input_tokens": 12, "output_tokens": 24},
                    finish_reason="stop",
                    request_id="request-1",
                    actual_model="test-model",
                    endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                )

            return runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="test",
                model="test-model",
                stage="chapter_candidate_generation",
                endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                request={"input_sha256": hashlib.sha256(input_pack.encode("utf-8")).hexdigest()},
                input_tokens=12,
                max_output_tokens=128,
                operation=physical_call,
            ).text

        def crash_after_receipt(event: str, _candidate_id: str) -> None:
            if event == "after_generation_before_record":
                raise _SimulatedCrash()

        with self.assertRaises(_SimulatedCrash):
            self._execute(
                self._executor(
                    generator=generator,
                    execution_kind="model",
                    fault_injector=crash_after_receipt,
                ),
                key="author-click-crash",
            )
        self.assertEqual(1, physical_calls)

        candidate_root = RuntimePaths.for_story_project(self.book).runtime_dir / "candidates"
        candidate_id = next(path.name for path in candidate_root.iterdir() if path.is_dir())
        self.assertFalse(ChapterCandidateStore(self.book).has_record(candidate_id))
        resumed = self._executor(generator=generator, execution_kind="model").resume(candidate_id)

        self.assertEqual("completed", resumed["status"])
        self.assertEqual(1, physical_calls)
        self.assertEqual(1, len(resumed["evidence"]["intent_hashes"]))
        self.assertEqual(1, len(resumed["evidence"]["receipt_hashes"]))
        self.assertEqual(["succeeded"], resumed["evidence"]["receipt_statuses"])
        selected = resumed["evidence"]["selected_response"]
        self.assertEqual(resumed["prose"]["sha256"], selected["prose_sha256"])
        self.assertEqual(resumed["evidence"]["receipt_hashes"][0], selected["receipt_hash"])

    def test_provider_uncertainty_freezes_until_author_abandons_exact_candidate(self) -> None:
        physical_calls = 0

        def uncertain_generator(input_pack: str) -> str:
            nonlocal physical_calls
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            call_id = runtime.new_call_id(provider="test", stage="chapter_candidate_generation")

            def timeout() -> ModelResponse:
                nonlocal physical_calls
                physical_calls += 1
                raise TimeoutError("provider outcome is unknown")

            return runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="test",
                model="test-model",
                stage="chapter_candidate_generation",
                endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                request={"input_sha256": hashlib.sha256(input_pack.encode("utf-8")).hexdigest()},
                input_tokens=12,
                max_output_tokens=128,
                operation=timeout,
            ).text

        with self.assertRaises(ChapterCandidateExecutionError) as raised:
            self._execute(
                self._executor(generator=uncertain_generator, execution_kind="model"),
                key="author-click-uncertain",
            )
        self.assertEqual("provider_uncertain", raised.exception.status)
        self.assertEqual(1, physical_calls)
        unresolved = list_unresolved_provider_calls(
            RuntimePaths.for_story_project(self.book).run_dir
        )
        self.assertEqual(1, len(unresolved))
        self.assertTrue(unresolved[0]["execution_id"].startswith("execution_candidate_"))

        with self.assertRaises(ProviderCallUncertainError):
            self._execute(
                self._executor(
                    generator=lambda _input: self.fail("a frozen project called the provider"),
                    execution_kind="model",
                ),
                key="author-click-must-freeze",
            )
        self.assertEqual(1, physical_calls)

        candidate_root = RuntimePaths.for_story_project(self.book).runtime_dir / "candidates"
        self.assertEqual(1, len([path for path in candidate_root.iterdir() if path.is_dir()]))
        stored = ChapterCandidateStore(self.book).load_record(raised.exception.candidate_id)
        self.assertEqual("provider_uncertain", stored["status"])
        self.assertEqual(1, len(stored["evidence"]["unresolved_calls"]))

        # Legacy chapter recovery markers have no authority to reconcile a
        # Candidate execution, even if they name its ids explicitly.
        marker = {
            "schema_version": "1.2",
            "id": "resolution_must_not_clear_candidate",
            "created_at": "2026-09-03T00:00:00+00:00",
            "book_id": self.identity.book_id,
            "chapter_index": 2,
            "action": "reset",
            "source_run_id": "legacy-run",
            "resolved_execution_ids": [unresolved[0]["execution_id"]],
            "resolved_attempt_ids": [unresolved[0]["attempt_id"]],
            "discarded_run_ids": [],
            "expected_scene_count": 1,
            "complete_draft": None,
            "scenes": [],
            "reason": "legacy_reset_must_not_reconcile_candidate_calls",
        }
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker_dir = RuntimePaths.for_story_project(self.book).run_dir / "locked_chapter_resolutions"
        marker_dir.mkdir(parents=True)
        (marker_dir / "resolution_must_not_clear_candidate.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.assertEqual(
            1,
            len(
                list_unresolved_provider_calls(
                    RuntimePaths.for_story_project(self.book).run_dir
                )
            ),
        )

        store = ChapterCandidateStore(self.book)
        candidate_id = raised.exception.candidate_id
        intent = store.load_intent(candidate_id)
        record = store.load_record(candidate_id)
        execution_id = intent["execution"]["execution_id"]
        uncertainty_sha256 = candidate_provider_uncertainty_sha256(
            execution_id,
            record["evidence"]["unresolved_calls"],
        )
        record_bytes = store.record_path(candidate_id).read_bytes()
        formal_before = self._formal_sources()

        with self.assertRaisesRegex(
            ChapterCandidateConflictError,
            "explicit author confirmation",
        ):
            self._executor(execution_kind="model").abandon_provider_uncertainty(
                candidate_id,
                expected_intent_hash=intent["intent_hash"],
                expected_record_hash=record["record_hash"],
                expected_uncertainty_sha256=uncertainty_sha256,
                author_confirmed=False,
            )
        with self.assertRaisesRegex(
            ChapterCandidateConflictError,
            "uncertainty changed",
        ):
            self._executor(execution_kind="model").abandon_provider_uncertainty(
                candidate_id,
                expected_intent_hash=intent["intent_hash"],
                expected_record_hash=record["record_hash"],
                expected_uncertainty_sha256="0" * 64,
                author_confirmed=True,
            )

        resolution = self._executor(
            generator=lambda _input: self.fail("abandon must never call the generator"),
            execution_kind="model",
        ).abandon_provider_uncertainty(
            candidate_id,
            expected_intent_hash=intent["intent_hash"],
            expected_record_hash=record["record_hash"],
            expected_uncertainty_sha256=uncertainty_sha256,
            author_confirmed=True,
        )
        repeated = self._executor(execution_kind="model").abandon_provider_uncertainty(
            candidate_id,
            expected_intent_hash=intent["intent_hash"],
            expected_record_hash=record["record_hash"],
            expected_uncertainty_sha256=uncertainty_sha256,
            author_confirmed=True,
        )

        self.assertEqual(resolution, repeated)
        self.assertEqual("abandon_uncertain_provider_result", resolution["action"])
        self.assertEqual(
            [],
            list_unresolved_provider_calls(
                RuntimePaths.for_story_project(self.book).run_dir
            ),
        )
        self.assertEqual(record_bytes, store.record_path(candidate_id).read_bytes())
        self.assertEqual("provider_uncertain", store.load_record(candidate_id)["status"])
        self.assertFalse(
            store.model_call_store(intent).has_receipt(
                record["evidence"]["unresolved_calls"][0]["attempt_id"]
            )
        )
        self.assertEqual(formal_before, self._formal_sources())

        for operation in ("resume", "cancel"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    ChapterCandidateConflictError,
                    "was abandoned",
                ):
                    getattr(
                        self._executor(
                            generator=lambda _input: self.fail(
                                "abandoned Candidate must never call the generator"
                            ),
                            execution_kind="model",
                        ),
                        operation,
                    )(candidate_id)

        next_result = self._execute(
            self._executor(
                generator=lambda _input: "林澈保留了未知调用证据，随后从新的候选继续写作。",
                execution_kind="deterministic",
            ),
            key="author-click-after-abandon",
        )
        self.assertEqual("completed", next_result["status"])
        self.assertEqual(1, physical_calls)
        self.assertEqual(formal_before, self._formal_sources())

        resolution_path = candidate_provider_resolution_path(
            RuntimePaths.for_story_project(self.book).run_dir,
            candidate_id,
        )
        tampered = json.loads(resolution_path.read_text(encoding="utf-8"))
        tampered["candidate_record_hash"] = "0" * 64
        tampered["resolution_hash"] = canonical_json_hash(
            tampered,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        resolution_path.write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ChapterCandidateProviderResolutionIntegrityError,
            "uncertain record",
        ):
            list_unresolved_provider_calls(RuntimePaths.for_story_project(self.book).run_dir)

    def test_candidate_tampering_is_detected_on_restart(self) -> None:
        result = self._execute(
            self._executor(
                generator=lambda _input: "林澈停在门前，确认回声来自封闭区内。",
                execution_kind="deterministic",
            ),
            key="author-click-tamper",
        )
        draft = (
            RuntimePaths.for_story_project(self.book).runtime_dir
            / result["prose"]["artifact_ref"]
        )
        draft.write_text("被篡改的正文", encoding="utf-8")
        with self.assertRaisesRegex(ChapterCandidateIntegrityError, "hash mismatch"):
            ChapterCandidateStore(self.book).load(result["candidate_id"])

    def test_author_can_cancel_claimed_candidate_without_touching_authority(self) -> None:
        before = self._formal_sources()

        def stop_before_provider(_input: str) -> str:
            raise _SimulatedCrash()

        with self.assertRaises(_SimulatedCrash):
            self._execute(
                self._executor(generator=stop_before_provider, execution_kind="deterministic"),
                key="author-click-cancel",
            )
        candidate_root = RuntimePaths.for_story_project(self.book).runtime_dir / "candidates"
        candidate_id = next(path.name for path in candidate_root.iterdir() if path.is_dir())

        cancelled = self._executor(
            generator=lambda _input: self.fail("cancel must not generate"),
            execution_kind="deterministic",
        ).cancel(candidate_id)

        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual(before, self._formal_sources())
        self.assertIsNone(ChapterCandidateStore(self.book).load(candidate_id)["prose"])

    def test_model_execution_cannot_succeed_without_durable_provider_evidence(self) -> None:
        with self.assertRaises(ChapterCandidateExecutionError) as raised:
            self._execute(
                self._executor(
                    generator=lambda _input: "林澈确认门后有人，却没有任何可核验的模型调用凭证。",
                    execution_kind="model",
                ),
                key="author-click-unreceipted-model",
            )

        self.assertEqual("failed", raised.exception.status)
        self.assertIn("model_call_evidence_missing", raised.exception.record["error"]["message"])
        self.assertEqual([], raised.exception.record["evidence"]["intent_hashes"])
        self.assertIsNone(raised.exception.record["prose"]["artifact_ref"])

    def test_model_prose_must_equal_a_hash_verified_successful_response(self) -> None:
        provider_text = "林澈从终端读出旧坐标，周岚随即标记了入口。"
        unrelated_text = "林澈走进另一条通道，这段文字并非模型返回的响应。"

        def generator(input_pack: str) -> str:
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            call_id = runtime.new_call_id(provider="test", stage="chapter_candidate_generation")
            runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="test",
                model="test-model",
                stage="chapter_candidate_generation",
                endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                request={"input_sha256": hashlib.sha256(input_pack.encode("utf-8")).hexdigest()},
                input_tokens=12,
                max_output_tokens=128,
                operation=lambda: ModelResponse(
                    provider_text,
                    usage={"input_tokens": 12, "output_tokens": 20},
                    finish_reason="stop",
                    request_id="request-bound-response",
                    actual_model="test-model",
                    endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                ),
            )
            return unrelated_text

        with self.assertRaises(ChapterCandidateExecutionError) as raised:
            self._execute(
                self._executor(generator=generator, execution_kind="model"),
                key="author-click-unbound-prose",
            )

        self.assertEqual("failed", raised.exception.status)
        self.assertIn("not bound", raised.exception.record["error"]["message"])
        self.assertIsNone(raised.exception.record["prose"]["artifact_ref"])
        self.assertIsNone(raised.exception.record["evidence"]["selected_response"])

    def test_provider_boundary_lock_rejects_a_concurrent_candidate(self) -> None:
        entered_provider = threading.Event()
        release_provider = threading.Event()
        physical_calls = 0
        prose = "林澈守住门口，周岚读取终端，两人确认信号仍在移动。"

        def blocking_generator(input_pack: str) -> str:
            nonlocal physical_calls
            runtime = current_model_call_runtime()
            self.assertIsNotNone(runtime)
            call_id = runtime.new_call_id(provider="test", stage="chapter_candidate_generation")

            def physical_call() -> ModelResponse:
                nonlocal physical_calls
                physical_calls += 1
                entered_provider.set()
                if not release_provider.wait(timeout=10):
                    raise RuntimeError("test did not release the simulated Provider")
                return ModelResponse(
                    prose,
                    usage={"input_tokens": 12, "output_tokens": 24},
                    finish_reason="stop",
                    request_id="request-concurrent",
                    actual_model="test-model",
                    endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                )

            return runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="test",
                model="test-model",
                stage="chapter_candidate_generation",
                endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                request={"input_sha256": hashlib.sha256(input_pack.encode("utf-8")).hexdigest()},
                input_tokens=12,
                max_output_tokens=128,
                operation=physical_call,
            ).text

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                self._execute,
                self._executor(generator=blocking_generator, execution_kind="model"),
                key="author-click-concurrent-first",
            )
            self.assertTrue(entered_provider.wait(timeout=5))
            try:
                with self.assertRaises(PersistenceLockError):
                    self._execute(
                        self._executor(
                            generator=lambda _input: self.fail(
                                "concurrent candidate crossed the Provider boundary"
                            ),
                            execution_kind="model",
                        ),
                        key="author-click-concurrent-second",
                    )
            finally:
                release_provider.set()
            first_result = first.result(timeout=10)

        self.assertEqual("completed", first_result["status"])
        self.assertEqual(1, physical_calls)

    def test_source_change_during_generation_preserves_draft_but_marks_it_stale(self) -> None:
        prose = "周岚刚接通控制器，林澈就发现原定路线已经改变。"

        def generate_while_author_edits(_input: str) -> str:
            outline = self.book / "大纲" / "细纲_第002章.md"
            outline.write_text(
                outline.read_text(encoding="utf-8") + "\n作者同时修改了章节意图。\n",
                encoding="utf-8",
            )
            return prose

        result = self._execute(
            self._executor(
                generator=generate_while_author_edits,
                execution_kind="deterministic",
            ),
            key="author-click-source-drift",
        )

        self.assertEqual("stale", result["status"])
        self.assertEqual("story_project_source_drift", result["error"]["code"])
        self.assertEqual(prose, ChapterCandidateStore(self.book).load(result["candidate_id"])["prose"])

    def test_candidate_artifact_presence_requires_plain_regular_files(self) -> None:
        store = ChapterCandidateStore(self.book)
        intent_candidate_id = f"candidate_{'a' * 40}"
        intent_path = store.intent_path(intent_candidate_id)
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.mkdir()

        with self.assertRaisesRegex(
            ChapterCandidateIntegrityError,
            "candidate artifact must be a plain regular file",
        ):
            store.has_intent(intent_candidate_id)

        record_candidate_id = f"candidate_{'b' * 40}"
        record_path = store.record_path(record_candidate_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.mkdir()

        with self.assertRaisesRegex(
            ChapterCandidateIntegrityError,
            "candidate artifact must be a plain regular file",
        ):
            store.has_record(record_candidate_id)

    def test_candidate_listing_rejects_record_without_intent(self) -> None:
        store = ChapterCandidateStore(self.book)
        candidate_id = f"candidate_{'c' * 40}"
        record_path = store.record_path(candidate_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ChapterCandidateIntegrityError,
            "candidate record exists without its Intent",
        ):
            store.list_for_chapter(
                book_id=self.identity.book_id,
                chapter_index=2,
            )

    def test_candidate_listing_wraps_directory_enumeration_failure(self) -> None:
        store = ChapterCandidateStore(self.book)
        store.root.mkdir(parents=True, exist_ok=True)

        with patch.object(
            type(store.root),
            "iterdir",
            side_effect=PermissionError("listing denied"),
        ):
            with self.assertRaisesRegex(
                ChapterCandidateIntegrityError,
                "cannot enumerate candidate storage",
            ):
                store.list_for_chapter(
                    book_id=self.identity.book_id,
                    chapter_index=2,
                )

    def test_source_change_before_resume_fails_claim_without_calling_generator(self) -> None:
        def stop_after_claim(_input: str) -> str:
            raise _SimulatedCrash()

        with self.assertRaises(_SimulatedCrash):
            self._execute(
                self._executor(
                    generator=stop_after_claim,
                    execution_kind="deterministic",
                ),
                key="author-click-resume-source-drift",
            )
        candidate_root = RuntimePaths.for_story_project(self.book).runtime_dir / "candidates"
        candidate_id = next(path.name for path in candidate_root.iterdir() if path.is_dir())
        outline = self.book / "大纲" / "细纲_第002章.md"
        outline.write_text(
            outline.read_text(encoding="utf-8") + "\n作者在恢复前修改了章节意图。\n",
            encoding="utf-8",
        )

        with self.assertRaises(ChapterCandidateExecutionError) as raised:
            self._executor(
                generator=lambda _input: self.fail(
                    "source-drifted resume called the generator"
                ),
                execution_kind="deterministic",
            ).resume(candidate_id)

        self.assertEqual("failed", raised.exception.status)
        self.assertEqual("story_project_source_drift", raised.exception.record["error"]["code"])
        self.assertFalse(raised.exception.record["source_verification"]["before"]["ok"])
        self.assertIsNone(raised.exception.record["prose"]["artifact_ref"])


if __name__ == "__main__":
    unittest.main()
