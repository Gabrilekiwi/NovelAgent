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

from core.engine.executor import AgentExecutor, LoopExecutionError
from core.memory_v2 import (
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
def _case_directory():
    root = Path.cwd() / f".novelagent-v201-authority-e2e-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _director(snapshot, memory_context):
    del memory_context
    return {
        "chapter_index": snapshot["chapter_index"],
        "goal": "v201_nonempty_authority_chain",
        "actions": ["generate_chapter", "validate"],
        "validation_focus": ["logic"],
        "max_repair_attempts": 0,
        "notes": [],
    }


def _validator(snapshot, chapter, decision):
    del snapshot, chapter, decision
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


def _analyzer(chapter, validation):
    return validate_schema(
        {
            "events": [{"text": chapter[:80]}],
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


class V201AuthoritativeMultiChapterE2ETests(unittest.TestCase):
    def _prepare_book(
        self,
        root: Path,
        *,
        chapter_two_sentinel: bytes | None = None,
    ) -> tuple[Path, Path, dict[int, str]]:
        snapshot_path = root / "snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "chapter_index": 1,
                    "world_state": {"locations": {}},
                    "characters": {},
                    "timeline": [],
                    "project_profile": {"language": "zh-CN"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        book = root / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        titles = {1: "First", 2: "Second"}
        for chapter_index, title in titles.items():
            canonical_outline_path(book, chapter_index).write_text(
                "\n".join(
                    [
                        f"# {title}",
                        "",
                        f"core_event: 第{chapter_index}章推进避难所协作协议",
                        "",
                        "## required_beats",
                        f"- 第{chapter_index}章登记队伍名册",
                        f"- 第{chapter_index}章确保物资路线安全",
                        "",
                        f"ending_pressure: 第{chapter_index}章警报开始倒计时",
                    ]
                ),
                encoding="utf-8",
            )
        if chapter_two_sentinel is not None:
            canonical_prose_path(book, 2, titles[2]).write_bytes(
                chapter_two_sentinel
            )
        return snapshot_path, book, titles

    @staticmethod
    def _scene_completion(*, omit_chapter_two_relationship_event: bool = False):
        first_members = [
            {
                "member_id": f"member-{index:02d}",
                "descriptor": f"survivor {index}",
                "status": "active",
            }
            for index in range(1, 8)
        ]
        joined_members = [
            {
                "member_id": f"member-{index:02d}",
                "descriptor": f"survivor {index}",
                "status": "active",
            }
            for index in range(8, 13)
        ]

        def completion(messages, **kwargs):
            del kwargs
            request = json.loads(messages[-1]["content"])
            scene = request["scene"]
            events = copy.deepcopy(scene["planned_events"])
            source_event_id = str(events[0]["event_id"])
            chapter_index = int(source_event_id.split("-")[1])
            required_beats = " ".join(
                str(item.get("text") or "")
                for item in request["story_project_required_beats"]
            )
            ending_pressure = str(
                request.get("story_project_ending_pressure") or ""
            )

            if chapter_index == 1:
                prose = (
                    f"{required_beats}。双方完成登记并建立稳定协作，"
                    "队伍现在共有7人。物资与警戒计数同步入账。"
                    f"{ending_pressure}"
                )
                relationship_event_id = source_event_id
                relationship_before = None
                relationship_after = "provisional_alliance"
                roster_members = first_members
                roster_delta = 7
                roster_count = 7
                location_before = None
                location_after = "shelter-a"
                inventory_before, inventory_delta, inventory_after = 0, 4, 4
                counter_before, counter_delta, counter_after = 0, 1, 1
            else:
                prose = (
                    f"{required_beats}。五名幸存者完成登记后加入主队，"
                    "队伍现在共有12人。协作边界与物资消耗均已更新。"
                    f"{ending_pressure}"
                )
                relationship_event_id = (
                    "" if omit_chapter_two_relationship_event else source_event_id
                )
                relationship_before = "provisional_alliance"
                relationship_after = "field_alliance"
                roster_members = joined_members
                roster_delta = 5
                roster_count = 12
                location_before = "shelter-a"
                location_after = "shelter-b"
                inventory_before, inventory_delta, inventory_after = 4, -1, 3
                counter_before, counter_delta, counter_after = 1, 1, 2

            relationship = {
                "source_id": "leader",
                "target_id": "medic",
                "field": "cooperation_boundary",
                "before": relationship_before,
                "after": relationship_after,
                "reason": "the shelter pact changed in this scene",
            }
            if relationship_event_id:
                relationship["source_event_id"] = relationship_event_id

            return json.dumps(
                {
                    "prose": prose,
                    "events": events,
                    "deltas": {
                        "characters": [],
                        "relationships": [relationship],
                        "rosters": [
                            {
                                "roster_id": "main_team",
                                "operation": "join",
                                "member_ids": [
                                    member["member_id"]
                                    for member in roster_members
                                ],
                                "members": roster_members,
                                "delta": roster_delta,
                                "declared_count": roster_count,
                                "reason_event_id": source_event_id,
                            }
                        ],
                        "locations": [
                            {
                                "entity_id": "main_team",
                                "before": location_before,
                                "after": location_after,
                                "reason": "the team advanced along the supply route",
                                "source_event_id": source_event_id,
                            }
                        ],
                        "inventory": [
                            {
                                "owner_id": "main_team",
                                "item_id": "medicine",
                                "before": inventory_before,
                                "delta": inventory_delta,
                                "after": inventory_after,
                                "reason": "medicine was received or consumed",
                                "source_event_id": source_event_id,
                            }
                        ],
                        "counters": [
                            {
                                "counter_id": "alert_level",
                                "before": counter_before,
                                "delta": counter_delta,
                                "after": counter_after,
                                "reason": "the alarm pressure increased",
                                "source_event_id": source_event_id,
                            }
                        ],
                    },
                    "continuity_note": (
                        "All prose facts and ledger transitions share the "
                        "current Scene event."
                    ),
                },
                ensure_ascii=False,
            )

        return completion

    def _run(
        self,
        root: Path,
        *,
        omit_chapter_two_relationship_event: bool = False,
        chapter_two_sentinel: bytes | None = None,
        capture_loop_error: bool = False,
    ):
        snapshot_path, book, titles = self._prepare_book(
            root,
            chapter_two_sentinel=chapter_two_sentinel,
        )
        loader = build_generation_story_project_context_loader(
            story_project=book,
            chapter=1,
        )
        try:
            with patch(
                "modules.chapter_generator.pipeline.chat_completion",
                side_effect=self._scene_completion(
                    omit_chapter_two_relationship_event=(
                        omit_chapter_two_relationship_event
                    )
                ),
            ):
                loop = AgentExecutor(
                    snapshot_path=snapshot_path,
                    memory_path=root / "missing-memory.json",
                    run_dir=root / "runs",
                    chapter_dir=root / "chapters",
                    dry_run=False,
                    scene_limit=1,
                    director=_director,
                    validator=_validator,
                    analyzer=_analyzer,
                    story_project_context_loader=loader,
                    story_project_writeback=StoryProjectWritebackConfig(
                        mode="apply"
                    ),
                    quality_policy="minimal",
                ).run_loop(steps=2, persist=True)
        except LoopExecutionError as exc:
            if not capture_loop_error:
                raise
            loop = exc
        return loop, book, titles

    def test_two_chapter_nonempty_deltas_write_back_and_memory_replay(self) -> None:
        with _case_directory() as root:
            loop, book, titles = self._run(root)

            self.assertTrue(loop["succeeded"], loop)
            self.assertEqual(
                [True, True],
                [result["committed"] for result in loop["runs"]],
            )
            for result in loop["runs"]:
                run = result["run"]
                chapter_index = int(run["chapter_index"])
                prose = canonical_prose_path(
                    book,
                    chapter_index,
                    titles[chapter_index],
                ).read_bytes()
                prose_sha256 = hashlib.sha256(prose).hexdigest()
                final_gate = run["chapter"]["final_artifact"]
                writeback = run["story_project"]["writeback"]
                self.assertEqual(final_gate["artifact_sha256"], prose_sha256)
                self.assertEqual(
                    final_gate["artifact_sha256"],
                    writeback["writeback_artifact_sha256"],
                )
                authority_delta = result["analysis"][
                    "authoritative_state_delta"
                ]
                self.assertTrue(authority_delta["relationship_changes"])
                self.assertTrue(authority_delta["roster_changes"])
                self.assertTrue(authority_delta["location_changes"])
                self.assertTrue(authority_delta["inventory_changes"])
                self.assertTrue(authority_delta["numeric_changes"])

            authority = loop["runs"][-1]["snapshot"]["authoritative_state"]
            self.assertEqual(
                12,
                authority["roster"]["main_team"]["computed_count"],
            )
            self.assertEqual(
                "field_alliance",
                authority["relationships"]["leader->medic"][
                    "cooperation_boundary"
                ],
            )
            self.assertEqual(
                "shelter-b",
                authority["locations"]["main_team"]["location_id"],
            )
            self.assertEqual(
                3,
                authority["inventory"]["main_team:medicine"]["quantity"],
            )
            self.assertEqual(
                2,
                authority["numeric_counters"]["alert_level"]["current_value"],
            )
            self.assertIn(
                "队伍现在共有12人",
                loop["runs"][-1]["chapter"],
            )

            genesis = create_empty_typed_canonical_memory(
                book_id="v201-authority-e2e"
            )
            current = copy.deepcopy(genesis)
            memory_events: list[dict] = []
            for result in loop["runs"]:
                chapter = result["chapter"]
                chapter_index = int(result["run"]["chapter_index"])
                memory_patch = create_memory_patch(
                    patch_id=f"patch-v201-e2e-{chapter_index:04d}",
                    source_kind="chapter",
                    operations=[
                        {
                            "op": "update_authoritative_state",
                            "value": result["analysis"][
                                "authoritative_state_delta"
                            ],
                        }
                    ],
                )
                current, emitted = apply_memory_patch(
                    current,
                    memory_patch,
                    event_context={
                        "chapter_body": chapter,
                        "evidence_spans": [
                            {
                                "start_char": 0,
                                "end_char": len(chapter),
                                "quote": chapter,
                            }
                        ],
                        "authority_epoch": 1,
                    },
                )
                memory_events.extend(emitted)

            replayed = apply_memory_events(
                genesis,
                memory_events,
                reducer_version="memory-reducer-2.2",
            )
            self.assertEqual(current, replayed)
            replayed_authority = replayed["authoritative_state"]
            self.assertEqual(
                authority["roster"]["main_team"]["computed_count"],
                replayed_authority["roster"]["main_team"]["computed_count"],
            )
            self.assertEqual(
                authority["inventory"]["main_team:medicine"]["quantity"],
                replayed_authority["inventory"]["main_team:medicine"][
                    "quantity"
                ],
            )
            self.assertEqual(
                authority["numeric_counters"]["alert_level"]["current_value"],
                replayed_authority["numeric_counters"]["alert_level"][
                    "current_value"
                ],
            )

    def test_missing_current_scene_relationship_event_blocks_before_writeback(
        self,
    ) -> None:
        with _case_directory() as root:
            loop, book, titles = self._run(
                root,
                omit_chapter_two_relationship_event=True,
                capture_loop_error=True,
            )

            self.assertIsInstance(loop, LoopExecutionError)
            self.assertFalse(loop.session["succeeded"])
            self.assertGreaterEqual(len(loop.runs), 1)
            self.assertTrue(loop.runs[0]["committed"])
            self.assertFalse(
                canonical_prose_path(book, 2, titles[2]).exists(),
                "a failed chapter must not create its StoryProject prose target",
            )
            self.assertIn(
                "missing_authority_event_reference",
                str(loop.original),
            )


if __name__ == "__main__":
    unittest.main()
