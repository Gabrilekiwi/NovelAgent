from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

import main as cli
from core.schema import validate_schema
from core.story_project.identity import ProjectIdentity, project_identity_path
from core.story_project.managed_block import build_managed_projection, write_managed_block
from core.story_project.semantic_parser import (
    SEMANTIC_PARSER_VERSION,
    build_story_project_shadow_report,
    parse_story_project_semantic_state,
)


FIXTURES = Path("tests/fixtures/story_project_semantics/cases")


class StoryProjectSemanticParserTest(unittest.TestCase):
    def _identity(self, case: str) -> ProjectIdentity:
        root = (FIXTURES / case / "book").resolve()
        return ProjectIdentity(
            schema_version="1.0",
            book_id=f"fixture-{case}",
            created_at="2026-07-13T00:00:00Z",
            root_hint=str(root),
            ephemeral=True,
        )

    def _parse(self, case: str, chapter: int) -> dict:
        return parse_story_project_semantic_state(
            FIXTURES / case / "book",
            chapter,
            project_identity=self._identity(case),
        )

    def test_canonical_fixture_parses_authoritative_semantics_with_provenance(self) -> None:
        state = self._parse("synthetic_standard", 2)

        self.assertEqual(SEMANTIC_PARSER_VERSION, state["parser_version"])
        self.assertEqual("canonical-zh-1", state["layout_profile_version"])
        self.assertEqual("旧城站封闭闸门外", state["story_state"]["current_location"])
        self.assertEqual(
            "正常情况下只能从控制室解锁",
            state["world_state"]["locations"]["旧城站"]["封闭闸门"],
        )
        self.assertEqual(2, len(state["characters"]))
        self.assertEqual(2, len(state["timeline"]))
        self.assertEqual(2, len(state["foreshadowing"]))
        self.assertTrue(any("只能" in item["content"] for item in state["constraints"]))
        self.assertEqual([], state["conflicts"])

        provenance_paths = {item["field_path"] for item in state["provenance"]}
        self.assertIn("story_state.current_location", provenance_paths)
        self.assertIn("foreshadowing.fs-gate-signal", provenance_paths)
        self.assertTrue(any(path.startswith("characters.") and path.endswith(".status") for path in provenance_paths))
        self.assertTrue(any(path.startswith("constraints.") for path in provenance_paths))
        for item in state["provenance"]:
            self.assertEqual(64, len(item["source_sha256"]))
            self.assertLessEqual(item["start_char"], item["end_char"])
            self.assertEqual(SEMANTIC_PARSER_VERSION, item["parser_version"])

    def test_legacy_fixture_is_supported_but_append_block_is_evidence_only(self) -> None:
        state = self._parse("legacy_append_variant", 12)

        self.assertEqual("legacy-zh-1", state["layout_profile_version"])
        self.assertEqual("旧换气井入口", state["story_state"]["current_location"])
        self.assertEqual(2, len(state["foreshadowing"]))
        self.assertEqual("developing", state["foreshadowing"][0]["status"])
        self.assertEqual(11, state["timeline"][0]["chapter"])
        self.assertIn(
            "legacy_append_block_evidence_only",
            {item["code"] for item in state["parse_warnings"]},
        )
        self.assertNotIn("run_id", json.dumps(state["story_state"], ensure_ascii=False))

    def test_malformed_fixture_keeps_uncertain_text_non_authoritative_and_blocks_conflicts(self) -> None:
        state = self._parse("malformed_variant", 4)

        self.assertEqual("malformed-zh-1", state["layout_profile_version"])
        self.assertNotIn("current_location", state["story_state"])
        self.assertEqual(
            {"duplicate_managed_block", "same_authority_conflict"},
            {item["code"] for item in state["conflicts"]},
        )
        self.assertTrue(all(item["blocking"] for item in state["conflicts"]))
        self.assertEqual(
            {"foreshadowing_missing_stable_id", "timeline_chapter_unknown"},
            {item["code"] for item in state["parse_warnings"]},
        )
        self.assertEqual({}, state["world_state"])
        self.assertTrue(state["unsupported_excerpts"])
        self.assertTrue(all(item["authoritative"] is False for item in state["unsupported_excerpts"]))

        report = build_story_project_shadow_report(state, snapshot={"story_state": {}})
        validate_schema(report, "story_project_shadow_report.schema.json")
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["affects_generation"])
        self.assertFalse(report["affects_snapshot"])
        self.assertFalse(report["strict_eligible"])
        self.assertEqual(2, report["blocking_conflict_count"])
        self.assertIn("blocking_semantic_conflict", report["strict_blockers"])

    def test_parser_reads_the_full_source_and_digest_covers_tail_changes(self) -> None:
        source = FIXTURES / "synthetic_standard" / "book"
        target = Path(".tmp") / "test_semantic_parser" / uuid.uuid4().hex / "book"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        identity = ProjectIdentity(
            "1.0",
            "fixture-full-read",
            "2026-07-13T00:00:00Z",
            str(target.resolve()),
            ephemeral=True,
        )
        before = parse_story_project_semantic_state(target, 2, project_identity=identity)
        settings_path = target / "设定" / "地点" / "旧城站.md"
        with settings_path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + ("未经确认。" * 6000) + "\n- 文件尾部可核验事实\n")

        after = parse_story_project_semantic_state(target, 2, project_identity=identity)

        self.assertNotEqual(before["source_digest"], after["source_digest"])
        self.assertIn(
            "文件尾部可核验事实",
            after["world_state"]["settings"]["旧城站"]["facts"],
        )

    def test_valid_managed_projection_is_lower_priority_than_manual_tracking(self) -> None:
        source = FIXTURES / "synthetic_standard" / "book"
        target = Path(".tmp") / "test_semantic_parser" / uuid.uuid4().hex / "book"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        identity = ProjectIdentity(
            "1.0",
            "fixture-managed-priority",
            "2026-07-13T00:00:00Z",
            str(target.resolve()),
            ephemeral=True,
        )
        managed = build_managed_projection(
            scope="context",
            book_id=identity.book_id,
            run_id="run-managed",
            chapter=1,
            parser_version="shadow-1.0",
            base_revision="rev-1",
            base_source_digest="a" * 64,
            owned_fields=("story_state.current_location", "story_state.managed_only"),
            values={
                "story_state.current_location": "不得覆盖人工地点",
                "story_state.managed_only": "低优先级补充",
            },
        )
        context_path = target / "追踪" / "上下文.md"
        context_path.write_bytes(write_managed_block(context_path.read_bytes(), managed))

        state = parse_story_project_semantic_state(target, 2, project_identity=identity)

        self.assertEqual("旧城站封闭闸门外", state["story_state"]["current_location"])
        self.assertEqual("低优先级补充", state["story_state"]["managed_only"])
        managed_sources = [
            item for item in state["provenance"] if item["source_kind"] == "managed_projection"
        ]
        self.assertEqual(["story_state.managed_only"], [item["field_path"] for item in managed_sources])
        self.assertEqual("supporting", managed_sources[0]["authority_class"])

    def test_shadow_cli_is_generation_free_and_does_not_create_project_state(self) -> None:
        root = (FIXTURES / "synthetic_standard" / "book").resolve()
        self.assertFalse(project_identity_path(root).exists())
        output = io.StringIO()

        with patch.object(
            sys,
            "argv",
            [
                "main.py",
                "--story-project",
                str(root),
                "--chapter",
                "2",
                "--story-state-shadow-report",
            ],
        ), patch("main.AgentExecutor") as executor, contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(0, raised.exception.code)
        executor.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual("shadow", report["mode"])
        self.assertFalse(report["affects_generation"])
        self.assertFalse(report["affects_snapshot"])
        self.assertFalse(project_identity_path(root).exists())
        self.assertFalse((root / ".novelagent" / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
