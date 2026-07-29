from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from core.engine.executor import AgentExecutor
from core.memory_v2 import (
    MemoryReducerError,
    apply_memory_events,
    apply_memory_patch,
    create_empty_typed_canonical_memory,
    create_memory_patch,
)
from core.schema import validate_schema
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path, canonical_prose_path
from core.story_project.runtime import build_generation_story_project_context_loader
from core.story_project.writer import StoryProjectWritebackConfig


@contextmanager
def _workspace_temp_directory():
    """Avoid Windows' restrictive 0o700 TemporaryDirectory ACL in long suites."""
    path = Path.cwd() / f".novelagent-v201-e2e-{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


class V201MultiChapterRegressionTests(unittest.TestCase):
    def test_two_chapter_scene_to_writeback_chain_replays_authority_and_hashes(
        self,
    ) -> None:
        with _workspace_temp_directory() as tmp_path:
            snapshot_path = tmp_path / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "chapter_index": 1,
                        "world_state": {"locations": {}},
                        "characters": {},
                        "timeline": [],
                        "project_profile": {"language": "en"},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            book = tmp_path / "book"
            for directory in CORE_DIRECTORY_NAMES:
                (book / directory).mkdir(parents=True)
            titles = {1: "First", 2: "Second"}
            for chapter_index, title in titles.items():
                canonical_outline_path(book, chapter_index).write_text(
                    "\n".join(
                        [
                            f"# {title}",
                            "",
                            f"core_event: chapter {chapter_index} forces a costly route choice",
                            "",
                            "## required_beats",
                            f"- chapter {chapter_index} opens the sealed route",
                            f"- chapter {chapter_index} records the survivor handoff",
                            "",
                            f"ending_pressure: chapter {chapter_index} alarm starts a countdown",
                        ]
                    ),
                    encoding="utf-8",
                )

            def director(snapshot, memory_context):
                return {
                    "chapter_index": snapshot["chapter_index"],
                    "goal": "v201_scene_writeback_e2e",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["logic"],
                    "max_repair_attempts": 0,
                    "notes": [],
                }

            def validator(snapshot, chapter, decision):
                return validate_schema(
                    {
                        "ok": True,
                        "requested_focus": ["logic"],
                        "executed_checks": ["logic"],
                        "skipped_checks": [],
                        "checks": [
                            {"name": "logic", "ok": True, "problems": []}
                        ],
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

            def analyzer(chapter, validation):
                return validate_schema(
                    {
                        "events": [{"text": chapter[:40]}],
                        "character_changes": [],
                        "world_changes": [],
                        "new_locations": [],
                        "story_state": {
                            "last_chapter_ending": chapter[-80:],
                            "last_scene_location": "",
                            "last_scene_characters": [],
                            "open_threads": [],
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

            def scene_completion(messages, **kwargs):
                request = json.loads(messages[-1]["content"])
                scene = request["scene"]
                scene_index = int(scene["index"])
                goal = str(scene.get("goal") or "advance the assigned beat")
                templates = {
                    1: (
                        "At dawn the scout maps a narrow approach while {goal}. "
                        "Fresh chalk arrows mark the safe turn, and the rear guard "
                        "locks the first barrier behind them."
                    ),
                    2: (
                        "Beneath the service lights, the medic changes priorities: "
                        "{goal}. She records every transferred name before sealing "
                        "the ledger inside a waterproof case."
                    ),
                    3: (
                        "A radio burst interrupts the crossing and forces {goal}. "
                        "The team disperses to separate cover as a new countdown "
                        "begins beyond the final shutter."
                    ),
                    4: (
                        "Rain floods the loading bay just as {goal}. Engineers brace "
                        "the ramp with cable and timber, leaving a visible route for "
                        "the last vehicle."
                    ),
                }
                prose = templates.get(
                    scene_index,
                    (
                        "The assigned unit completes {goal} through a distinct "
                        f"checkpoint numbered {scene_index}, then hands command to "
                        "the next unit without revisiting prior work."
                    ),
                ).format(goal=goal)
                if scene_index == len(request["chapter_plan"]["scenes"]):
                    prose += " " + str(request["story_project_ending_pressure"])
                return json.dumps(
                    {
                        "prose": prose,
                        "events": scene["planned_events"],
                        "deltas": {
                            "characters": [],
                            "relationships": [],
                            "rosters": [],
                            "locations": [],
                            "inventory": [],
                            "counters": [],
                        },
                        "continuity_note": (
                            f"Scene {scene_index} continues from the prior checkpoint."
                        ),
                    }
                )

            loader = build_generation_story_project_context_loader(
                story_project=book,
                chapter=1,
            )
            with patch(
                "modules.chapter_generator.pipeline.chat_completion",
                side_effect=scene_completion,
            ):
                loop = AgentExecutor(
                    snapshot_path=snapshot_path,
                    memory_path=tmp_path / "missing-memory.json",
                    run_dir=tmp_path / "runs",
                    chapter_dir=tmp_path / "chapters",
                    dry_run=False,
                    director=director,
                    validator=validator,
                    analyzer=analyzer,
                    story_project_context_loader=loader,
                    story_project_writeback=StoryProjectWritebackConfig(
                        mode="apply"
                    ),
                    quality_policy="minimal",
                ).run_loop(steps=2, persist=True)

            self.assertTrue(
                loop["succeeded"],
                {
                    "stopped_reason": loop["stopped_reason"],
                    "failure_reasons": loop["failure_reasons"],
                    "runs": [
                        {
                            "accepted": item.get("accepted"),
                            "committed": item.get("committed"),
                            "codes": [
                                problem.get("code")
                                for problem in (
                                    item.get("validation") or {}
                                ).get("problems", [])
                            ],
                            "persistence": (item.get("run") or {}).get(
                                "persistence"
                            ),
                            "writeback": (
                                (item.get("run") or {}).get("story_project") or {}
                            ).get("writeback"),
                        }
                        for item in loop["runs"]
                    ],
                },
            )
            self.assertEqual([True, True], [item["committed"] for item in loop["runs"]])
            all_authority_event_ids: set[str] = set()
            for result in loop["runs"]:
                run = result["run"]
                chapter_index = int(run["chapter_index"])
                pipeline = run["chapter"]["pipeline"]
                self.assertGreater(pipeline["scene_count"], 0)
                self.assertTrue(
                    pipeline["authoritative_state_validation"]["accepted"]
                )
                self.assertTrue(
                    all(
                        source.get("source_call_id")
                        for source in pipeline["scene_sources"]
                    )
                )
                stage_names = {
                    record["stage"]
                    for record in run["chapter"]["integrity_records"]
                }
                self.assertTrue(
                    {
                        "merge",
                        "writeback_canonicalization",
                        "final_gate",
                    }
                    <= stage_names
                )
                title = titles[chapter_index]
                prose_bytes = canonical_prose_path(
                    book,
                    chapter_index,
                    title,
                ).read_bytes()
                prose_sha256 = hashlib.sha256(prose_bytes).hexdigest()
                final_gate = run["chapter"]["final_artifact"]
                writeback = run["story_project"]["writeback"]
                self.assertEqual(final_gate["artifact_sha256"], prose_sha256)
                self.assertEqual(
                    final_gate["artifact_sha256"],
                    writeback["writeback_artifact_sha256"],
                )
                all_authority_event_ids.update(
                    result["snapshot"]["authoritative_state"]["events"]
                )

            self.assertTrue(
                {"chapter-0001-beat-001", "chapter-0002-beat-001"}
                <= all_authority_event_ids
            )

    def test_ten_chapter_authoritative_state_chain_replays_exactly(self) -> None:
        genesis = create_empty_typed_canonical_memory(book_id="v201-ten-chapter")
        current = copy.deepcopy(genesis)
        all_events: list[dict] = []

        for chapter_index in range(1, 11):
            previous_value = chapter_index - 1
            member_id = f"survivor_temp_{chapter_index:03d}"
            event_id = f"chapter-{chapter_index:04d}-event-001"
            chapter_body = (
                f"第{chapter_index}章证据：队伍接纳{member_id}，"
                f"侵蚀值从{previous_value}/100变为{chapter_index}/100。"
            )
            character_changes = (
                [
                    {
                        "character_id": "char_qian_ming",
                        "canonical_name": "钱明",
                        "aliases": ["王主管"],
                        "identity": "商场主管",
                        "active_motivations": [{"target_id": "char_qian_wife"}],
                    }
                ]
                if chapter_index == 1
                else []
            )
            relationship_changes = (
                [
                    {
                        "relationship_id": "rel_qian_ming_spouse",
                        "source_character_id": "char_qian_ming",
                        "target_character_id": "char_qian_wife",
                        "type": "spouse",
                        "status": "active",
                        "introduced_chapter": 1,
                        "source_event_id": event_id,
                    }
                ]
                if chapter_index == 1
                else []
            )
            delta = {
                "source_tier": "chapter_event",
                "character_changes": character_changes,
                "relationship_changes": relationship_changes,
                "roster_changes": [
                    {
                        "roster_id": "main_survivor_group",
                        "operation": "join",
                        "member_ids": [member_id],
                        "members": [
                            {
                                "member_id": member_id,
                                "descriptor": f"第{chapter_index}位幸存者",
                                "status": "active",
                                "joined_chapter": chapter_index,
                                "joined_event_id": event_id,
                            }
                        ],
                        "delta": 1,
                        "declared_count": chapter_index,
                    }
                ],
                "numeric_changes": [
                    {
                        "counter_id": "erosion",
                        "owner_id": "char_qian_ming",
                        "previous_value": previous_value,
                        "delta": 1,
                        "expected_value": chapter_index,
                        "declared_value": chapter_index,
                        "minimum": 0,
                        "maximum": 100,
                        "rule": "monotonic_non_decreasing",
                        "last_updated_chapter": chapter_index,
                        "source_event_id": event_id,
                    }
                ],
                "inventory_changes": [
                    {
                        "owner_id": "main_survivor_group",
                        "item_id": "medicine",
                        "previous_quantity": previous_value,
                        "delta": 1,
                        "declared_quantity": chapter_index,
                        "source_event_id": event_id,
                    }
                ],
                "location_changes": [
                    {
                        "entity_id": "main_survivor_group",
                        "before": f"zone_{previous_value:02d}",
                        "after": f"zone_{chapter_index:02d}",
                        "source_event_id": event_id,
                    }
                ],
                "events": [
                    {
                        "event_id": event_id,
                        "type": "survivor_joined",
                        "subjects": ["char_qian_ming"],
                        "objects": [member_id],
                        "location": f"zone_{chapter_index:02d}",
                        "status": "completed",
                    }
                ],
            }
            patch = create_memory_patch(
                patch_id=f"patch-v201-chapter-{chapter_index:04d}",
                source_kind="chapter",
                operations=[{"op": "update_authoritative_state", "value": delta}],
            )
            current, chapter_events = apply_memory_patch(
                current,
                patch,
                event_context={
                    "chapter_body": chapter_body,
                    "evidence_spans": [
                        {
                            "start_char": 0,
                            "end_char": len(chapter_body),
                            "quote": chapter_body,
                        }
                    ],
                    "authority_epoch": 1,
                },
            )
            all_events.extend(chapter_events)

            authority = current["authoritative_state"]
            self.assertEqual(
                chapter_index,
                authority["roster"]["main_survivor_group"]["computed_count"],
            )
            self.assertEqual(
                chapter_index,
                authority["numeric_counters"]["erosion"]["current_value"],
            )
            self.assertEqual(
                chapter_index,
                authority["inventory"]["main_survivor_group:medicine"]["quantity"],
            )
            self.assertEqual(
                f"zone_{chapter_index:02d}",
                authority["locations"]["main_survivor_group"]["location_id"],
            )

        replayed = apply_memory_events(
            genesis,
            all_events,
            reducer_version="memory-reducer-2.2",
        )
        self.assertEqual(current, replayed)
        self.assertEqual(10, len(all_events))
        self.assertEqual(
            "char_qian_ming",
            next(iter(current["authoritative_state"]["characters"])),
        )
        self.assertEqual(
            "spouse",
            current["authoritative_state"]["relationships"]["rel_qian_ming_spouse"]["type"],
        )

    def test_chapter_eleven_conflicts_are_blocked_without_mutating_chapter_ten(self) -> None:
        memory = create_empty_typed_canonical_memory(book_id="v201-conflict")
        authority = memory["authoritative_state"]
        authority["characters"]["char_qian_ming"] = {
            "character_id": "char_qian_ming",
            "canonical_name": "钱明",
            "aliases": ["王主管"],
            "identity": "商场主管",
            "source_tier": "chapter_event",
        }
        authority["relationships"]["rel_qian_ming_spouse"] = {
            "relationship_id": "rel_qian_ming_spouse",
            "source_character_id": "char_qian_ming",
            "target_character_id": "char_qian_wife",
            "type": "spouse",
            "status": "active",
            "source_tier": "chapter_event",
        }
        authority["roster"]["main_survivor_group"] = {
            "roster_id": "main_survivor_group",
            "members": [
                {
                    "member_id": f"survivor_temp_{index:03d}",
                    "descriptor": f"第{index}位幸存者",
                }
                for index in range(1, 11)
            ],
            "declared_count": 10,
            "computed_count": 10,
            "source_tier": "chapter_event",
        }
        authority["numeric_counters"]["erosion"] = {
            "counter_id": "erosion",
            "current_value": 10,
            "minimum": 0,
            "maximum": 100,
            "rule": "monotonic_non_decreasing",
            "source_tier": "chapter_event",
        }
        before = copy.deepcopy(memory)
        conflict_delta = {
            "source_tier": "chapter_event",
            "character_changes": [
                {
                    "character_id": "char_manager_reinterpreted",
                    "canonical_name": "王主管",
                    "aliases": [],
                    "identity": "临时安保",
                }
            ],
            "relationship_changes": [
                {
                    "relationship_id": "rel_qian_ming_spouse",
                    "source_character_id": "char_qian_ming",
                    "target_character_id": "char_qian_cousin",
                    "type": "cousin",
                    "status": "active",
                }
            ],
            "roster_changes": [
                {
                    "roster_id": "main_survivor_group",
                    "operation": "replace",
                    "member_ids": [
                        *(f"survivor_temp_{index:03d}" for index in range(1, 11)),
                        "survivor_temp_011",
                    ],
                    "members": [
                        {
                            "member_id": f"survivor_temp_{index:03d}",
                            "descriptor": (
                                "身份被偷换的成年人"
                                if index == 1
                                else f"第{index}位幸存者"
                            ),
                        }
                        for index in range(1, 12)
                    ],
                    "delta": 1,
                    "declared_count": 12,
                }
            ],
            "numeric_changes": [
                {
                    "counter_id": "erosion",
                    "previous_value": 10,
                    "delta": -8,
                    "expected_value": 2,
                    "declared_value": 2,
                }
            ],
            "inventory_changes": [],
            "location_changes": [],
            "events": [],
        }
        patch = create_memory_patch(
            patch_id="patch-v201-chapter-0011-conflict",
            source_kind="chapter",
            operations=[
                {
                    "op": "update_authoritative_state",
                    "value": conflict_delta,
                }
            ],
        )

        with self.assertRaises(MemoryReducerError) as captured:
            apply_memory_patch(
                memory,
                patch,
                event_context={
                    "chapter_body": "第十一章冲突证据",
                    "evidence_spans": [
                        {
                            "start_char": 0,
                            "end_char": 8,
                            "quote": "第十一章冲突证据",
                        }
                    ],
                    "authority_epoch": 1,
                },
            )

        message = str(captured.exception)
        for code in (
            "character_identity_drift",
            "character_relationship_drift",
            "roster_member_identity_drift",
            "roster_count_mismatch",
            "numeric_counter_rollback",
        ):
            self.assertIn(code, message)
        self.assertEqual(before, memory)


if __name__ == "__main__":
    unittest.main()
