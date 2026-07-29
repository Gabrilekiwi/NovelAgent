from __future__ import annotations

import json
import hashlib
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.contracts import ModelResponse
from core.engine.artifacts import save_chapter_pipeline_artifacts
from core.engine.executor import AgentExecutor, _unresolved_provider_calls
from core.engine.locked_chapter import (
    LockedChapterRecoveryError,
    _expected_scene_count,
    recover_locked_chapter,
)
from core.engine.locked_chapter_state import (
    LockedChapterStateError,
    active_locked_chapter_checkpoint,
    load_locked_chapter_resolutions,
)
from core.engine.run_record import (
    build_failed_run_record,
    build_run_id,
    load_latest_run_summary,
    validate_run_result,
)
from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import ModelCallStore, build_scene_generation_call_id
from core.state.snapshot import save_snapshot


class LockedChapterRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / ".tmp" / "test_locked_chapter" / f"case_{uuid.uuid4().hex}"
        self.story_root = self.root / "book"
        self.run_dir = self.story_root / ".novelagent" / "runtime" / "runs"
        self.snapshot_path = self.story_root / ".novelagent" / "runtime" / "snapshot.json"
        self.story_root.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        (self.story_root / "正文").mkdir()
        save_snapshot({"chapter_index": 1}, self.snapshot_path)
        self.book_id = "book-test-1"
        self.now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)

    def test_preserves_contiguous_scene_prefix_and_is_idempotent(self) -> None:
        execution_id = "execution_partial"
        scene_one = "陆沉压低身体贴着货架前进，远处的撞击声越来越近。" * 12
        scene_two = "备用灯在头顶闪烁，众人终于看清侧门后扑来的感染者。" * 12
        self._write_execution(
            execution_id,
            successful=[scene_one, scene_two],
            missing_stage="chapter_generation",
        )
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)

        result = self._recover()

        self.assertEqual("resume_scenes", result["action"])
        self.assertEqual(2, result["reusable_scene_count"])
        self.assertEqual(3, result["next_scene_index"])
        self.assertEqual([], _unresolved_provider_calls(self.run_dir))
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual([scene_one, scene_two], [item["text"] for item in checkpoint["scenes"]])

        second = self._recover()
        self.assertEqual("already_recovered", second["status"])
        self.assertEqual(1, len(list((self.run_dir / "locked_chapter_resolutions").glob("*.json"))))

    def test_cross_run_recovery_preserves_verified_scene_prefix(self) -> None:
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }

        def response(scene_index: int) -> str:
            return json.dumps(
                {
                    "prose": (
                        f"陆沉继续处理第{scene_index}场的连续行动，并保留可核验的来源证据。"
                        * 24
                    ),
                    "events": [
                        {
                            "event_id": (
                                f"chapter-001-scene-{scene_index:03d}-event-001"
                            ),
                            "type": f"scene_{scene_index}_completed",
                            "subjects": ["陆沉"],
                            "objects": [],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                    "deltas": empty_deltas,
                    "continuity_note": f"第{scene_index}场结束状态。",
                },
                ensure_ascii=False,
            )

        first_execution = "execution_cross_run_scene_1"
        self._write_execution(
            first_execution,
            successful=[response(1)],
            missing_stage="chapter_generation",
            call_ids=[build_scene_generation_call_id(1)],
            attempt_ids=["attempt-run-one-scene-one"],
        )
        self._write_failed_run(
            execution_id=first_execution,
            chapter="",
            scene_count=3,
        )
        first = self._recover()
        self.assertEqual("resume_scenes", first["action"])
        self.assertEqual(1, first["reusable_scene_count"])

        self.now += timedelta(minutes=5)
        second_execution = "execution_cross_run_scene_2"
        self._write_execution(
            second_execution,
            successful=[response(2)],
            missing_stage="chapter_generation",
            call_ids=[build_scene_generation_call_id(2)],
            attempt_ids=["attempt-run-two-scene-two"],
        )
        self._write_failed_run(
            execution_id=second_execution,
            chapter="",
            scene_count=3,
        )

        second = self._recover()

        self.assertEqual("resume_scenes", second["action"])
        self.assertEqual(2, second["reusable_scene_count"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
            language="zh-CN",
        )
        source_run_ids = [
            scene["source_provenance"]["source_run_id"]
            for scene in checkpoint["scenes"]
        ]
        self.assertEqual(2, len(set(source_run_ids)))
        self.assertEqual(checkpoint["source_run_id"], source_run_ids[1])
        self.assertNotEqual(checkpoint["source_run_id"], source_run_ids[0])

    def test_persisted_pipeline_accepts_scene_provenance_from_prior_same_chapter_run(
        self,
    ) -> None:
        scene_text = (
            "陆沉完成对消防站外围的终检，并逐项记录本场景的可核验证据。"
            * 40
        )
        event = {
            "event_id": "chapter-001-scene-001-event-001",
            "type": "scene_completed",
            "subjects": ["陆沉"],
            "objects": [],
            "location": "fire-station",
            "status": "completed",
        }
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        source_execution = "execution_prior_scene_source"
        source_call = build_scene_generation_call_id(1)
        self._write_execution(
            source_execution,
            successful=[
                json.dumps(
                    {
                        "prose": scene_text,
                        "events": [event],
                        "deltas": empty_deltas,
                        "continuity_note": "single Scene complete",
                    },
                    ensure_ascii=False,
                )
            ],
            missing_stage=None,
            call_ids=[source_call],
        )
        self._write_failed_run(
            execution_id=source_execution,
            chapter="",
            scene_count=1,
        )
        first = self._recover()
        self.assertEqual("resume_scenes", first["action"])
        first_checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        prior_scene = first_checkpoint["scenes"][0]
        prior_source_run_id = prior_scene["source_provenance"]["source_run_id"]

        self.now += timedelta(minutes=5)
        persisting_execution = "execution_persisting_prior_scene"
        self._write_execution(
            persisting_execution,
            successful=[],
            missing_stage=None,
        )
        self._write_rejected_single_scene_artifact_run(
            execution_id=persisting_execution,
            source_attempt_id=prior_scene["source_attempt_id"],
            source_call_id=source_call,
            source_provenance=prior_scene["source_provenance"],
            scene_text=scene_text,
        )

        second = self._recover()

        self.assertEqual("resume_scenes", second["action"])
        self.assertEqual(1, second["reusable_scene_count"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertNotEqual(prior_source_run_id, checkpoint["source_run_id"])
        self.assertEqual(
            prior_source_run_id,
            checkpoint["scenes"][0]["source_provenance"]["source_run_id"],
        )

    def test_expected_scene_count_prefers_pipeline_scene_count_over_beat_count(
        self,
    ) -> None:
        self.assertEqual(
            4,
            _expected_scene_count(
                {
                    "chapter": {"pipeline": {"scene_count": 4}},
                    "input_pack": {
                        "metadata": {
                            "story_project": {
                                "required_beat_count": 9,
                            }
                        }
                    },
                }
            ),
        )
        self.assertEqual(
            4,
            _expected_scene_count(
                {
                    "input_pack": {
                        "metadata": {
                            "story_project": {
                                "planned_scene_count": 4,
                                "required_beat_count": 9,
                            }
                        }
                    }
                }
            ),
        )

    def test_complete_draft_is_staged_for_validation_and_repair(self) -> None:
        execution_id = "execution_complete"
        draft = "陆沉关掉手电，在黑暗里听见冷库门后传来规律的抓挠声。" * 45
        self._write_execution(execution_id, successful=[], missing_stage="llm_validation")
        self._write_failed_run(execution_id=execution_id, chapter=draft, scene_count=3)

        result = self._recover()

        self.assertEqual("repair_draft", result["action"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual(draft, checkpoint["complete_draft"]["text"])
        self.assertEqual([], _unresolved_provider_calls(self.run_dir))

        executor = AgentExecutor(generator=lambda _input: self.fail("complete draft must not be regenerated"))
        state = {
            "chapter": draft,
            "chapter_pipeline": None,
            "validation": {"stale": True},
            "locked_chapter_action": "repair_draft",
        }
        executor._handle_generate(state, "input", {"chapter_index": 1})
        self.assertEqual(draft, state["chapter"])
        self.assertIsNone(state["validation"])

    def test_manual_draft_supersedes_failed_model_draft_without_mutating_evidence(self) -> None:
        execution_id = "execution_manual_draft"
        model_draft = "陆沉守在防爆门旁，按原有顺序核对每一个进入缓冲区的人。" * 45
        manual_draft = "陆沉先验伤，再让幸存者依次通过防爆门，最后核对十七人名册。" * 45
        self._write_execution(execution_id, successful=[], missing_stage="llm_validation")
        self._write_failed_run(execution_id=execution_id, chapter=model_draft, scene_count=3)
        draft_path = self.story_root / ".novelagent" / "runtime" / "manual_draft.md"
        draft_path.write_text(manual_draft, encoding="utf-8")

        result = recover_locked_chapter(
            story_project_root=self.story_root,
            run_dir=self.run_dir,
            snapshot_path=self.snapshot_path,
            expected_book_id=self.book_id,
            language="zh-CN",
            manual_draft_path=draft_path,
            clock=lambda: self.now,
            id_factory=lambda: "manualmarker",
        )

        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual("manual_repaired_draft_provided", result["reason"])
        self.assertEqual(manual_draft, checkpoint["complete_draft"]["text"])
        self.assertEqual("scene_repair", checkpoint["complete_draft"]["source_stage"])
        self.assertNotIn("source_attempt_id", checkpoint["complete_draft"])

    def test_manual_draft_must_stay_inside_story_project(self) -> None:
        execution_id = "execution_external_manual_draft"
        draft = "陆沉在防爆门旁完成验伤和名册核对，队伍随后进入缓冲区。" * 45
        self._write_execution(execution_id, successful=[], missing_stage="llm_validation")
        self._write_failed_run(execution_id=execution_id, chapter=draft, scene_count=3)
        outside = self.root / "outside.md"
        outside.write_text(draft, encoding="utf-8")

        with self.assertRaisesRegex(LockedChapterRecoveryError, "StoryProject root"):
            recover_locked_chapter(
                story_project_root=self.story_root,
                run_dir=self.run_dir,
                snapshot_path=self.snapshot_path,
                expected_book_id=self.book_id,
                language="zh-CN",
                manual_draft_path=outside,
                clock=lambda: self.now,
                id_factory=lambda: "outsidemarker",
            )

    def test_recovery_prefers_durable_polish_and_skips_duplicate_polish(self) -> None:
        execution_id = "execution_polished_before_validation_lock"
        original = "陆沉带领幸存者沿维护通道撤离，身后的金属门持续震动。" * 45
        polished = "陆沉压住急促呼吸，带领幸存者沿维护通道撤离，身后的金属门轰然震动。" * 45
        self._write_execution(
            execution_id,
            successful=[],
            missing_stage="llm_validation",
        )
        store = ModelCallStore(self.run_dir / "executions" / execution_id / "model_calls")
        attempt_id = "attempt-polish"
        created_at = self.now + timedelta(seconds=2)
        store.create_intent(
            call_id="call-polish",
            attempt_id=attempt_id,
            provider="anthropic",
            model="claude-test",
            stage="claude_polish",
            budget_reservation={"reserved_input_tokens": 10, "reserved_output_tokens": 100},
            request={"messages": [{"role": "user", "content": original}]},
            created_at=created_at,
        )
        relative = f"responses/{attempt_id}.txt"
        artifact = store.root / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(polished, encoding="utf-8")
        store.create_receipt(
            attempt_id,
            response=ModelResponse(
                polished,
                usage={"input_tokens": 10, "output_tokens": 20},
                finish_reason="end_turn",
                actual_model="claude-test",
                endpoint_type="official",
            ),
            response_artifact_ref=relative,
            status="succeeded",
            received_at=created_at,
        )
        self._write_failed_run(
            execution_id=execution_id,
            chapter=original,
            scene_count=3,
        )

        result = self._recover()

        self.assertEqual("repair_draft", result["action"])
        self.assertEqual("durable_polished_draft_available", result["reason"])
        self.assertEqual("claude_polish", result["draft_stage"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual(polished, checkpoint["complete_draft"]["text"])
        self.assertEqual("claude_polish", checkpoint["complete_draft"]["source_stage"])
        self.assertEqual(attempt_id, checkpoint["complete_draft"]["source_attempt_id"])

        executor = AgentExecutor(
            polisher=lambda _chapter: self.fail("durable polished draft must not be polished again")
        )
        state = {
            "chapter": polished,
            "validation": {"stale": True},
            "locked_chapter_action": "repair_draft",
            "locked_chapter_draft_stage": "claude_polish",
        }
        executor._handle_polish(state)
        self.assertEqual(polished, state["chapter"])
        self.assertIsNone(state["validation"])

        self.now += timedelta(minutes=5)
        repeated_execution_id = "execution_repeated_validation_lock"
        self._write_execution(
            repeated_execution_id,
            successful=[],
            missing_stage="llm_validation",
        )
        self._write_failed_run(
            execution_id=repeated_execution_id,
            chapter=polished,
            scene_count=3,
        )

        repeated = self._recover()

        self.assertEqual("durable_polished_draft_available", repeated["reason"])
        self.assertEqual("claude_polish", repeated["draft_stage"])
        repeated_checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual("claude_polish", repeated_checkpoint["complete_draft"]["source_stage"])
        self.assertEqual(attempt_id, repeated_checkpoint["complete_draft"]["source_attempt_id"])

    def test_recovered_scene_repair_is_not_polished_again(self) -> None:
        repaired = "陆沉已经完成针对验证问题的修订，正文只应继续验证而不应再次润色。" * 45
        executor = AgentExecutor(
            polisher=lambda _chapter: self.fail("durable repaired draft must not be polished again")
        )
        state = {
            "chapter": repaired,
            "validation": {"stale": True},
            "locked_chapter_action": "repair_draft",
            "locked_chapter_draft_stage": "scene_repair",
        }

        executor._handle_polish(state)

        self.assertEqual(repaired, state["chapter"])
        self.assertIsNone(state["validation"])

    def test_newer_terminal_budget_failure_supersedes_partial_checkpoint_with_complete_draft(self) -> None:
        execution_id = "execution_partial_before_terminal_failure"
        scene_one = "陆沉贴着冷库墙壁移动，远处的金属门正被感染者撞得变形。" * 12
        self._write_execution(
            execution_id,
            successful=[scene_one],
            missing_stage="chapter_generation",
        )
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)
        first = self._recover()
        self.assertEqual("resume_scenes", first["action"])

        self.now += timedelta(minutes=5)
        complete = "陆沉屏住呼吸检查门锁，确认众人仍有一条穿过冷库的生路。" * 45
        self._write_failed_run(
            execution_id="execution_terminal_budget_failure",
            chapter=complete,
            scene_count=3,
        )

        recovered = self._recover()

        self.assertEqual("recovered", recovered["status"])
        self.assertEqual("repair_draft", recovered["action"])
        self.assertEqual("complete_failed_draft_available", recovered["reason"])
        self.assertEqual(0, recovered["resolved_execution_count"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual(complete, checkpoint["complete_draft"]["text"])
        self.assertEqual([], checkpoint["resolved_execution_ids"])
        self.assertEqual(2, len(list((self.run_dir / "locked_chapter_resolutions").glob("*.json"))))

        third = self._recover()
        self.assertEqual("already_recovered", third["status"])
        self.assertEqual(2, len(list((self.run_dir / "locked_chapter_resolutions").glob("*.json"))))

    def test_terminal_local_failure_recovers_structured_scene_receipts(self) -> None:
        execution_id = "execution_terminal_local_budget_failure"
        scene_one = "陆沉贴着冷库墙壁前进，逐一确认队员的位置和撤离顺序。" * 12
        scene_two = "备用灯忽明忽暗，众人沿着维护通道继续向安全门转移。" * 12
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        responses = [
            json.dumps(
                {
                    "prose": scene_one,
                    "events": [
                        {
                            "event_id": "chapter-001-scene-001-event-001",
                            "type": "route_checked",
                            "subjects": ["char_lu_chen"],
                            "objects": [],
                            "location": "cold-storage",
                            "status": "completed",
                        }
                    ],
                    "deltas": empty_deltas,
                    "continuity_note": "队伍抵达维护通道入口。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "prose": scene_two,
                    "events": [
                        {
                            "event_id": "chapter-001-scene-002-event-001",
                            "type": "transfer_continued",
                            "subjects": ["char_lu_chen"],
                            "objects": ["main_survivor_group"],
                            "location": "maintenance-corridor",
                            "status": "completed",
                        }
                    ],
                    "deltas": empty_deltas,
                    "continuity_note": "队伍正在接近安全门。",
                },
                ensure_ascii=False,
            ),
        ]
        self._write_execution(
            execution_id,
            successful=responses,
            missing_stage=None,
        )
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)

        result = self._recover()

        self.assertEqual("resume_scenes", result["action"])
        self.assertEqual(2, result["reusable_scene_count"])
        self.assertEqual(3, result["next_scene_index"])
        self.assertEqual([], _unresolved_provider_calls(self.run_dir))
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual([scene_one, scene_two], [item["text"] for item in checkpoint["scenes"]])
        self.assertEqual(
            "chapter-001-scene-002-event-001",
            checkpoint["scenes"][1]["events"][0]["event_id"],
        )
        self.assertEqual(empty_deltas, checkpoint["scenes"][1]["deltas"])
        self.assertEqual(
            "队伍正在接近安全门。",
            checkpoint["scenes"][1]["continuity_note"],
        )

    def test_complete_structured_scene_sequence_is_replayed_instead_of_flattened(
        self,
    ) -> None:
        execution_id = "execution_complete_structured_sequence"
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        responses = [
            json.dumps(
                {
                    "prose": f"第{index}个场景保留结构化事件和状态变化，等待恢复后重新校验。" * 8,
                    "events": [
                        {
                            "event_id": f"chapter-001-scene-{index:03d}-event-001",
                            "type": "scene_completed",
                            "subjects": ["char_lu_chen"],
                            "objects": [],
                            "location": "fire-station",
                            "status": "completed",
                        }
                    ],
                    "deltas": empty_deltas,
                },
                ensure_ascii=False,
            )
            for index in range(1, 4)
        ]
        self._write_execution(
            execution_id,
            successful=responses,
            missing_stage=None,
        )
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)

        result = self._recover()

        self.assertEqual("resume_scenes", result["action"])
        self.assertEqual("complete_scene_sequence_requires_revalidation", result["reason"])
        self.assertEqual(3, result["reusable_scene_count"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertIsNone(checkpoint["complete_draft"])
        self.assertEqual(3, len(checkpoint["scenes"]))

    def test_rejected_run_recovers_hash_bound_final_scene_artifacts(self) -> None:
        execution_id = "execution_rejected_final_scenes"
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        scene_texts = [
            "陆沉核对侵蚀值仍为六，并确认所有人都留在消防站器材口外。" * 18,
            "韩野完成内侧复核后放行，双方仍保持各自独立的组织边界。" * 18,
        ]
        call_ids = [
            build_scene_generation_call_id(index)
            for index in range(1, len(scene_texts) + 1)
        ]
        events = [
            {
                "event_id": f"chapter-001-scene-{index:03d}-event-001",
                "type": "scene_completed",
                "subjects": ["陆沉"],
                "objects": [],
                "location": "消防站",
                "status": "completed",
            }
            for index in range(1, len(scene_texts) + 1)
        ]
        self._write_execution(
            execution_id,
            successful=[
                json.dumps(
                    {
                        "prose": text,
                        "events": [events[index - 1]],
                        "deltas": empty_deltas,
                        "continuity_note": f"scene {index} complete",
                    },
                    ensure_ascii=False,
                )
                for index, text in enumerate(scene_texts, start=1)
            ],
            missing_stage=None,
            call_ids=call_ids,
        )
        scenes = [
            {
                "index": index,
                "goal": f"scene {index}",
                "text": text,
                "source_call_id": call_ids[index - 1],
                "source_attempt_id": f"attempt-{index}",
                "events": [events[index - 1]],
                "deltas": empty_deltas,
                "continuity_note": f"scene {index} complete",
            }
            for index, text in enumerate(scene_texts, start=1)
        ]
        merged = "\n\n".join(scene_texts)
        first_end = len(scene_texts[0])
        pipeline = {
            "chapter_index": 1,
            "plan": {
                "goal": "recover rejected final scenes",
                "scenes": [
                    {
                        "index": index,
                        "type": "scene",
                        "goal": f"scene {index}",
                        "required_beats": [f"beat {index}"],
                    }
                    for index in range(1, 3)
                ],
            },
            "scene_drafts": scenes,
            "merged_chapter": merged,
            "scene_spans": [
                {
                    "index": 1,
                    "start_char": 0,
                    "end_char": first_end,
                    "chars": first_end,
                },
                {
                    "index": 2,
                    "start_char": first_end + 2,
                    "end_char": len(merged),
                    "chars": len(scene_texts[1]),
                },
            ],
            "stages": [
                {"name": "plan_chapter", "status": "completed"},
                {"name": "generate_scenes", "status": "completed"},
                {"name": "merge_scenes", "status": "completed"},
                {"name": "validate", "status": "completed"},
                {"name": "repair", "status": "skipped"},
                {"name": "commit", "status": "pending"},
            ],
            "integrity": {
                "final_gate": {
                    "accepted": True,
                    "artifact_sha256": hashlib.sha256(
                        merged.encode("utf-8")
                    ).hexdigest(),
                }
            },
        }
        run_id = build_run_id(1, self.now)
        pipeline["artifacts"] = save_chapter_pipeline_artifacts(
            pipeline=pipeline,
            validation=None,
            repair_deltas=[],
            run={"id": run_id, "chapter_index": 1},
            output_dir=self.run_dir / "chapter_pipeline",
        )
        self._write_failed_run(
            execution_id=execution_id,
            chapter=merged,
            scene_count=2,
            chapter_pipeline=pipeline,
            status="rejected",
        )

        result = self._recover()

        self.assertEqual("resume_scenes", result["action"])
        self.assertEqual("complete_scene_sequence_requires_revalidation", result["reason"])
        self.assertEqual(2, result["reusable_scene_count"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual(scene_texts, [scene["text"] for scene in checkpoint["scenes"]])
        self.assertEqual(
            "chapter-001-scene-002-event-001",
            checkpoint["scenes"][1]["events"][0]["event_id"],
        )
        self.assertEqual(empty_deltas, checkpoint["scenes"][1]["deltas"])
        provenance = checkpoint["scenes"][1]["source_provenance"]
        self.assertEqual(build_run_id(1, self.now), provenance["source_run_id"])
        self.assertEqual(execution_id, provenance["execution_id"])
        self.assertEqual(call_ids[1], provenance["call_id"])
        self.assertEqual("attempt-2", provenance["attempt_id"])
        store = ModelCallStore(
            self.run_dir / "executions" / execution_id / "model_calls"
        )
        receipt = store.load_receipt("attempt-2")
        self.assertEqual(receipt["receipt_hash"], provenance["receipt_hash"])
        self.assertEqual(
            receipt["response_artifact_hash"],
            provenance["response_artifact_hash"],
        )
        source_run_path = self.run_dir / f"{provenance['source_run_id']}.json"
        self.assertEqual(
            hashlib.sha256(source_run_path.read_bytes()).hexdigest(),
            provenance["source_run_sha256"],
        )
        self.assertIsNone(checkpoint["complete_draft"])

    def test_persisted_scene_artifact_rejects_nonexistent_attempt(self) -> None:
        self._write_rejected_single_scene_artifact_run(
            execution_id="execution_fake_persisted_scene",
            source_attempt_id="attempt-does-not-exist",
        )

        result = self._recover()

        self.assertNotEqual("resume_scenes", result["action"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertTrue(checkpoint is None or checkpoint["scenes"] == [])

    def test_persisted_scene_artifact_rejects_wrong_receipt_hash(self) -> None:
        execution_id = "execution_wrong_persisted_receipt"
        call_id = build_scene_generation_call_id(1)
        self._write_execution(
            execution_id,
            successful=["x"],
            missing_stage=None,
            call_ids=[call_id],
        )
        receipt = ModelCallStore(
            self.run_dir / "executions" / execution_id / "model_calls"
        ).load_receipt("attempt-1")
        self._write_rejected_single_scene_artifact_run(
            execution_id=execution_id,
            source_attempt_id="attempt-1",
            source_call_id=call_id,
            source_provenance={
                "source_run_id": build_run_id(1, self.now),
                "source_run_sha256": "c" * 64,
                "execution_id": execution_id,
                "call_id": call_id,
                "attempt_id": "attempt-1",
                "receipt_hash": "0" * 64,
                "response_artifact_hash": receipt["response_artifact_hash"],
            },
        )

        result = self._recover()

        self.assertNotEqual("resume_scenes", result["action"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertTrue(checkpoint is None or checkpoint["scenes"] == [])

    def test_valid_receipt_does_not_authorize_replaced_persisted_scene_text(
        self,
    ) -> None:
        execution_id = "execution_replaced_persisted_text"
        call_id = build_scene_generation_call_id(1)
        source_text = "陆沉逐项核对原始响应中的场景事实，确认记录没有被替换。" * 24
        persisted_text = "这是一段被替换的持久化场景正文，不能借用无关的有效收据。" * 24
        event = {
            "event_id": "chapter-001-scene-001-event-001",
            "type": "scene_completed",
            "subjects": ["陆沉"],
            "objects": [],
            "location": "fire-station",
            "status": "completed",
        }
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        self._write_execution(
            execution_id,
            successful=[
                json.dumps(
                    {
                        "prose": source_text,
                        "events": [event],
                        "deltas": empty_deltas,
                        "continuity_note": "single Scene complete",
                    },
                    ensure_ascii=False,
                )
            ],
            missing_stage=None,
            call_ids=[call_id],
        )
        self._write_rejected_single_scene_artifact_run(
            execution_id=execution_id,
            source_attempt_id="attempt-1",
            source_call_id=call_id,
            scene_text=persisted_text,
        )

        result = self._recover()

        self.assertEqual("repair_draft", result["action"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual([], checkpoint["scenes"])
        self.assertEqual(persisted_text, checkpoint["complete_draft"]["text"])

    def test_valid_receipt_does_not_authorize_replaced_continuity_note(
        self,
    ) -> None:
        execution_id = "execution_replaced_persisted_continuity"
        call_id = build_scene_generation_call_id(1)
        scene_text = "陆沉保留原始场景正文，并核对响应声明的连续性说明。" * 24
        source_note = "source continuity note"
        tampered_note = "tampered continuity note"
        event = {
            "event_id": "chapter-001-scene-001-event-001",
            "type": "scene_completed",
            "subjects": ["陆沉"],
            "objects": [],
            "location": "fire-station",
            "status": "completed",
        }
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        self._write_execution(
            execution_id,
            successful=[
                json.dumps(
                    {
                        "prose": scene_text,
                        "events": [event],
                        "deltas": empty_deltas,
                        "continuity_note": source_note,
                    },
                    ensure_ascii=False,
                )
            ],
            missing_stage=None,
            call_ids=[call_id],
        )
        self._write_rejected_single_scene_artifact_run(
            execution_id=execution_id,
            source_attempt_id="attempt-1",
            source_call_id=call_id,
            scene_text=scene_text,
            continuity_note=tampered_note,
        )

        result = self._recover()

        self.assertEqual("repair_draft", result["action"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertEqual([], checkpoint["scenes"])
        self.assertEqual(scene_text, checkpoint["complete_draft"]["text"])

    def test_active_checkpoint_rejects_rehashed_scene_content_substitution(
        self,
    ) -> None:
        marker_path, _source_run_path = self._write_structured_scene_checkpoint(
            execution_id="execution_rehashed_marker_content",
        )
        original = json.loads(marker_path.read_text(encoding="utf-8"))

        for field in ("text", "events", "deltas", "continuity_note"):
            with self.subTest(field=field):
                tampered = json.loads(json.dumps(original, ensure_ascii=False))
                scene = tampered["scenes"][0]
                if field == "text":
                    scene["text"] = "替换后的正文仍然拥有合法 marker 哈希，但不属于原始响应。" * 24
                    scene["sha256"] = hashlib.sha256(
                        scene["text"].encode("utf-8")
                    ).hexdigest()
                elif field == "events":
                    scene["events"][0]["type"] = "tampered_event"
                elif field == "deltas":
                    scene["deltas"]["characters"].append(
                        {
                            "entity_id": "陆沉",
                            "before": None,
                            "after": "invented",
                        }
                    )
                else:
                    scene["continuity_note"] = "tampered continuity note"
                tampered["resolution_hash"] = canonical_json_hash(
                    tampered,
                    exclude_fields=("resolution_hash",),
                    exclude_environment_fields=False,
                )
                marker_path.write_text(
                    json.dumps(tampered, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    LockedChapterStateError,
                    rf"content does not match.*{field}",
                ):
                    active_locked_chapter_checkpoint(
                        self.run_dir,
                        chapter_index=1,
                        expected_book_id=self.book_id,
                    )

    def test_legacy_marker_without_scene_provenance_is_readable_but_not_reusable(
        self,
    ) -> None:
        marker_path, _source_run_path = self._write_structured_scene_checkpoint(
            execution_id="execution_legacy_marker_without_provenance",
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["schema_version"] = "1.0"
        for scene in marker["scenes"]:
            scene.pop("source_provenance", None)
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        loaded = load_locked_chapter_resolutions(self.run_dir)

        self.assertEqual(1, len(loaded))
        self.assertEqual("1.0", loaded[0]["schema_version"])
        self.assertIsNone(
            active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
            )
        )

    def test_version_1_1_marker_is_readable_but_not_reusable(
        self,
    ) -> None:
        marker_path, _source_run_path = self._write_structured_scene_checkpoint(
            execution_id="execution_legacy_v11_source_provenance",
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["schema_version"] = "1.1"
        for scene in marker["scenes"]:
            scene["source_provenance"].pop("source_run_sha256")
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        loaded = load_locked_chapter_resolutions(self.run_dir)

        self.assertEqual(1, len(loaded))
        self.assertEqual("1.1", loaded[0]["schema_version"])
        self.assertIsNone(
            active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
            )
        )

    def test_version_1_2_marker_requires_source_run_hash(self) -> None:
        marker_path, _source_run_path = self._write_structured_scene_checkpoint(
            execution_id="execution_v12_missing_source_run_hash",
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["scenes"][0]["source_provenance"].pop("source_run_sha256")
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LockedChapterStateError,
            "exactly the required evidence fields",
        ):
            load_locked_chapter_resolutions(self.run_dir)

    def test_active_checkpoint_hashes_source_run_before_scope_parsing(
        self,
    ) -> None:
        marker_path, source_run_path = self._write_structured_scene_checkpoint(
            execution_id="execution_wrong_scene_scope",
        )
        original_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        original_source_run = json.loads(
            source_run_path.read_text(encoding="utf-8")
        )

        for scope in ("chapter_index", "book_id"):
            with self.subTest(scope=scope):
                marker_path.write_text(
                    json.dumps(original_marker, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                altered_source = json.loads(
                    json.dumps(original_source_run, ensure_ascii=False)
                )
                if scope == "chapter_index":
                    altered_source["run"]["chapter_index"] = 2
                else:
                    altered_source["run"]["story_project"]["book_id"] = (
                        "book-other"
                    )
                source_run_path.write_text(
                    json.dumps(altered_source, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    LockedChapterStateError,
                    "source run raw bytes do not match source_run_sha256",
                ):
                    active_locked_chapter_checkpoint(
                        self.run_dir,
                        chapter_index=1,
                        expected_book_id=self.book_id,
                    )

    def test_active_checkpoint_rejects_cross_bound_source_run_hash(
        self,
    ) -> None:
        marker_path, _source_run_path = self._write_structured_scene_checkpoint(
            execution_id="execution_cross_binding_original",
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))

        self.now += timedelta(minutes=5)
        other_execution = "execution_cross_binding_other"
        self._write_execution(
            other_execution,
            successful=[],
            missing_stage=None,
        )
        self._write_failed_run(
            execution_id=other_execution,
            chapter="",
            scene_count=1,
        )
        other_run_id = build_run_id(1, self.now)
        other_run_path = self.run_dir / f"{other_run_id}.json"
        self.assertTrue(other_run_path.is_file())

        marker["scenes"][0]["source_provenance"]["source_run_id"] = other_run_id
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LockedChapterStateError,
            "source run raw bytes do not match source_run_sha256",
        ):
            active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
            )

    def test_active_scene_checkpoint_reverifies_response_artifact_hash(self) -> None:
        execution_id = "execution_tampered_scene_response"
        self._write_execution(
            execution_id,
            successful=["可验证的场景响应证据。" * 20],
            missing_stage=None,
            call_ids=[build_scene_generation_call_id(1)],
        )
        self._write_failed_run(
            execution_id=execution_id,
            chapter="",
            scene_count=1,
        )
        self._recover()
        response_path = (
            self.run_dir
            / "executions"
            / execution_id
            / "model_calls"
            / "responses"
            / "attempt-1.txt"
        )
        response_path.write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(
            LockedChapterStateError,
            "source provenance is not verifiable",
        ):
            active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
            )

    def test_scene_boundary_retry_receipt_replaces_primary_candidate(self) -> None:
        execution_id = "execution_scene_boundary_retry"
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }

        def response(label: str, event_id: str) -> str:
            return json.dumps(
                {
                    "prose": f"{label}，该场景正文长度足以进入恢复检查点。" * 12,
                    "events": [
                        {
                            "event_id": event_id,
                            "type": "scene_completed",
                            "subjects": ["char_lu_chen"],
                            "objects": [],
                            "location": "fire-station",
                            "status": "completed",
                        }
                    ],
                    "deltas": empty_deltas,
                },
                ensure_ascii=False,
            )

        self._write_execution(
            execution_id,
            successful=[
                response("场景一的过期候选", "chapter-001-scene-001-event-001"),
                response("场景一的修复候选", "chapter-001-scene-001-event-001"),
                response("场景二的有效候选", "chapter-001-scene-002-event-001"),
            ],
            missing_stage=None,
            call_ids=[
                build_scene_generation_call_id(1),
                build_scene_generation_call_id(1, boundary_retry=1),
                build_scene_generation_call_id(2),
            ],
        )
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=2)

        result = self._recover()

        self.assertEqual("resume_scenes", result["action"])
        self.assertEqual(2, result["reusable_scene_count"])
        checkpoint = active_locked_chapter_checkpoint(
            self.run_dir,
            chapter_index=1,
            expected_book_id=self.book_id,
        )
        self.assertIn("场景一的修复候选", checkpoint["scenes"][0]["text"])
        self.assertNotIn("场景一的过期候选", checkpoint["scenes"][0]["text"])
        self.assertIn("场景二的有效候选", checkpoint["scenes"][1]["text"])

    def test_no_trustworthy_output_resets_failed_chapter_history(self) -> None:
        execution_id = "execution_empty"
        self._write_execution(execution_id, successful=[], missing_stage="chapter_generation")
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)

        result = self._recover()

        self.assertEqual("reset", result["action"])
        self.assertIsNone(
            active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
            )
        )
        self.assertIsNone(load_latest_run_summary(self.run_dir))
        self.assertEqual([], _unresolved_provider_calls(self.run_dir))

    def test_forced_reset_discards_a_trustworthy_scene_prefix(self) -> None:
        execution_id = "execution_forced_reset"
        self._write_execution(
            execution_id,
            successful=["陆沉发现这段已生成场景不再符合修正后的细纲。" * 16],
            missing_stage="chapter_generation",
        )
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)

        result = recover_locked_chapter(
            story_project_root=self.story_root,
            run_dir=self.run_dir,
            snapshot_path=self.snapshot_path,
            expected_book_id=self.book_id,
            language="zh-CN",
            force_reset=True,
            clock=lambda: self.now,
            id_factory=lambda: "forcedreset",
        )

        self.assertEqual("reset", result["action"])
        self.assertEqual("operator_requested_reset", result["reason"])
        self.assertEqual(0, result["reusable_scene_count"])
        self.assertEqual([], _unresolved_provider_calls(self.run_dir))
        self.assertIsNone(
            active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
            )
        )

    def test_missing_success_artifact_is_treated_as_untrustworthy_and_reset(self) -> None:
        execution_id = "execution_missing_artifact"
        self._write_execution(
            execution_id,
            successful=["陆沉听见黑暗中传来脚步声，立刻握紧了灭火器。" * 12],
            missing_stage="chapter_generation",
        )
        artifact = (
            self.run_dir
            / "executions"
            / execution_id
            / "model_calls"
            / "responses"
            / "attempt-1.txt"
        )
        artifact.unlink()
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)

        result = self._recover()

        self.assertEqual("reset", result["action"])
        self.assertEqual(0, result["reusable_scene_count"])

    def test_refuses_to_touch_chapter_with_formal_prose(self) -> None:
        execution_id = "execution_committed"
        self._write_execution(execution_id, successful=[], missing_stage="chapter_generation")
        self._write_failed_run(execution_id=execution_id, chapter="", scene_count=3)
        (self.story_root / "正文" / "第001章_已完成.md").write_text("已提交正文", encoding="utf-8")

        with self.assertRaisesRegex(LockedChapterRecoveryError, "formal prose"):
            self._recover()

        self.assertFalse((self.run_dir / "locked_chapter_resolutions").exists())
        self.assertEqual(1, len(_unresolved_provider_calls(self.run_dir)))

    def _write_structured_scene_checkpoint(
        self,
        *,
        execution_id: str,
    ) -> tuple[Path, Path]:
        scene_text = (
            "陆沉逐项核对场景响应、状态变化和连续性说明，确认四类证据来自同一次调用。"
            * 24
        )
        event = {
            "event_id": "chapter-001-scene-001-event-001",
            "type": "scene_completed",
            "subjects": ["陆沉"],
            "objects": [],
            "location": "消防站",
            "status": "completed",
        }
        deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        self._write_execution(
            execution_id,
            successful=[
                json.dumps(
                    {
                        "prose": scene_text,
                        "events": [event],
                        "deltas": deltas,
                        "continuity_note": "陆沉仍在消防站核验证据。",
                    },
                    ensure_ascii=False,
                )
            ],
            missing_stage=None,
            call_ids=[build_scene_generation_call_id(1)],
        )
        self._write_failed_run(
            execution_id=execution_id,
            chapter="",
            scene_count=1,
        )
        result = self._recover()
        self.assertEqual("resume_scenes", result["action"])
        # Explicit zh-CN and the deterministic language-neutral read path must
        # both resolve the same normalized provider response.
        for language in (None, "zh-CN"):
            checkpoint = active_locked_chapter_checkpoint(
                self.run_dir,
                chapter_index=1,
                expected_book_id=self.book_id,
                language=language,
            )
            self.assertEqual(scene_text, checkpoint["scenes"][0]["text"])

        marker_path = next(
            (self.run_dir / "locked_chapter_resolutions").glob(
                "resolution_*.json"
            )
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        source_run_path = self.run_dir / f"{marker['source_run_id']}.json"
        return marker_path, source_run_path

    def _recover(self) -> dict:
        return recover_locked_chapter(
            story_project_root=self.story_root,
            run_dir=self.run_dir,
            snapshot_path=self.snapshot_path,
            expected_book_id=self.book_id,
            language="zh-CN",
            clock=lambda: self.now,
            id_factory=lambda: "testmarker",
        )

    def _write_execution(
        self,
        execution_id: str,
        *,
        successful: list[str],
        missing_stage: str | None,
        call_ids: list[str] | None = None,
        attempt_ids: list[str] | None = None,
    ) -> None:
        if call_ids is not None and len(call_ids) != len(successful):
            raise ValueError("call_ids must match successful responses")
        if attempt_ids is not None and len(attempt_ids) != len(successful):
            raise ValueError("attempt_ids must match successful responses")
        store = ModelCallStore(self.run_dir / "executions" / execution_id / "model_calls")
        for index, text in enumerate(successful, start=1):
            attempt_id = (
                attempt_ids[index - 1]
                if attempt_ids is not None
                else f"attempt-{index}"
            )
            created_at = self.now + timedelta(seconds=index)
            store.create_intent(
                call_id=(call_ids[index - 1] if call_ids is not None else f"call-{index}"),
                attempt_id=attempt_id,
                provider="openai",
                model="gpt-test",
                stage="chapter_generation",
                budget_reservation={"reserved_input_tokens": 10, "reserved_output_tokens": 100},
                request={"messages": [{"role": "user", "content": f"scene {index}"}]},
                created_at=created_at,
            )
            relative = f"responses/{attempt_id}.txt"
            artifact = store.root / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(text, encoding="utf-8")
            store.create_receipt(
                attempt_id,
                response=ModelResponse(
                    text,
                    usage={},
                    finish_reason="stop",
                    actual_model="gpt-test",
                    endpoint_type="official",
                ),
                response_artifact_ref=relative,
                status="succeeded",
                received_at=created_at,
            )

        if missing_stage is not None:
            missing_index = len(successful) + 1
            store.create_intent(
                call_id="call-missing",
                attempt_id="attempt-missing",
                provider="openai",
                model="gpt-test",
                stage=missing_stage,
                budget_reservation={"reserved_input_tokens": 10, "reserved_output_tokens": 100},
                request={"messages": [{"role": "user", "content": "missing"}]},
                created_at=self.now + timedelta(seconds=missing_index),
            )

    def _write_failed_run(
        self,
        *,
        execution_id: str,
        chapter: str,
        scene_count: int,
        chapter_pipeline: dict | None = None,
        status: str = "failed",
    ) -> None:
        decision = {
            "chapter_index": 1,
            "goal": "continue_existing_arc",
            "actions": ["generate_chapter", "validate"],
            "validation_focus": ["logic"],
            "max_repair_attempts": 1,
            "notes": [],
        }
        metadata = {
            "kind": "chapter_input_pack",
            "chapter_index": 1,
            "chars": 5,
            "sections": ["chapter_index"],
            "decision": {
                "goal": decision["goal"],
                "actions": decision["actions"],
                "validation_focus": decision["validation_focus"],
                "max_repair_attempts": decision["max_repair_attempts"],
            },
            "snapshot": {
                "world_state_keys": [],
                "character_count": 0,
                "timeline_count": 0,
                "constraint_count": 0,
                "memory_source": "test",
                "memory_status": "ready",
                "memory_item_count": 0,
            },
            "memory_index": {
                "source": "test",
                "status": "ready",
                "item_count": 0,
                "indexed_item_count": 0,
                "source_mapping_count": 0,
                "last_run_present": False,
            },
            "recovery_context": {
                "available": False,
                "source_run_id": None,
                "status": None,
                "problem_count": 0,
                "executed_checks": [],
                "skipped_checks": [],
                "repair_attempts": 0,
            },
            "story_project": {
                "enabled": True,
                "chapter_index": 1,
                "required_beat_count": scene_count,
                "ending_pressure_present": True,
            },
        }
        record = build_failed_run_record(
            started_at=self.now,
            finished_at=self.now + timedelta(minutes=1),
            base_snapshot={"chapter_index": 1},
            runtime_snapshot={"chapter_index": 1},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision=decision,
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            input_pack_metadata=metadata,
            chapter=chapter,
            validation=None,
            repair_attempts=0,
            workflow_trace=[],
            error=RuntimeError("provider call uncertain"),
            chapter_pipeline=chapter_pipeline,
        )
        record["story_project"] = {
            "enabled": True,
            "mode": "apply",
            "root": str(self.story_root),
            "book_id": self.book_id,
            "chapter_index": 1,
            "chapter_blueprint": None,
            "source_paths": None,
            "source_resolution": None,
            "writeback": {
                "attempted": False,
                "applied": False,
                "partial": False,
                "dry_run": False,
                "overwrite": False,
                "targets": [],
                "blocked_reasons": [],
                "errors": [],
                "failed_targets": [],
                "diff_summary": {},
                "artifacts": {},
            },
        }
        record["status"] = status
        if isinstance(chapter_pipeline, dict) and isinstance(
            chapter_pipeline.get("artifacts"),
            dict,
        ):
            record["chapter"]["pipeline"]["artifacts"] = chapter_pipeline[
                "artifacts"
            ]
        record["execution_evidence"] = {
            "execution_id": execution_id,
            "provenance_hash": "a" * 64,
            "provenance_artifact_ref": None,
            "model_calls_ref": f"executions/{execution_id}/model_calls",
        }
        payload = {"run": record, "chapter": chapter}
        validate_run_result(payload)
        (self.run_dir / f"{record['id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_rejected_single_scene_artifact_run(
        self,
        *,
        execution_id: str,
        source_attempt_id: str,
        source_call_id: str | None = None,
        source_provenance: dict | None = None,
        scene_text: str | None = None,
        continuity_note: str = "single Scene complete",
    ) -> None:
        if scene_text is None:
            scene_text = (
                "陆沉完成对消防站外围的终检，并逐项记录本场景的可核验证据。"
                * 40
            )
        empty_deltas = {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        }
        scene = {
            "index": 1,
            "goal": "verify persisted Scene provenance",
            "text": scene_text,
            "source_attempt_id": source_attempt_id,
            "events": [
                {
                    "event_id": "chapter-001-scene-001-event-001",
                    "type": "scene_completed",
                    "subjects": ["陆沉"],
                    "objects": [],
                    "location": "fire-station",
                    "status": "completed",
                }
            ],
            "deltas": empty_deltas,
            "continuity_note": continuity_note,
        }
        if source_call_id is not None:
            scene["source_call_id"] = source_call_id
        if source_provenance is not None:
            scene["source_provenance"] = source_provenance
        pipeline = {
            "chapter_index": 1,
            "plan": {
                "goal": "verify persisted Scene provenance",
                "scenes": [
                    {
                        "index": 1,
                        "type": "scene",
                        "goal": "verify persisted Scene provenance",
                        "required_beats": ["verify source"],
                    }
                ],
            },
            "scene_drafts": [scene],
            "merged_chapter": scene_text,
            "scene_spans": [
                {
                    "index": 1,
                    "start_char": 0,
                    "end_char": len(scene_text),
                    "chars": len(scene_text),
                }
            ],
            "stages": [
                {"name": "plan_chapter", "status": "completed"},
                {"name": "generate_scenes", "status": "completed"},
                {"name": "merge_scenes", "status": "completed"},
                {"name": "validate", "status": "completed"},
                {"name": "repair", "status": "skipped"},
                {"name": "commit", "status": "pending"},
            ],
            "integrity": {
                "final_gate": {
                    "accepted": True,
                    "artifact_sha256": hashlib.sha256(
                        scene_text.encode("utf-8")
                    ).hexdigest(),
                }
            },
        }
        run_id = build_run_id(1, self.now)
        pipeline["artifacts"] = save_chapter_pipeline_artifacts(
            pipeline=pipeline,
            validation=None,
            repair_deltas=[],
            run={"id": run_id, "chapter_index": 1},
            output_dir=self.run_dir / "chapter_pipeline",
        )
        self._write_failed_run(
            execution_id=execution_id,
            chapter=scene_text,
            scene_count=1,
            chapter_pipeline=pipeline,
            status="rejected",
        )


if __name__ == "__main__":
    unittest.main()
