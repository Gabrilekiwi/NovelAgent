from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.rules import build_rule_aware_input_pack, load_default_narrative_rule_pack
from core.state.input_pack import build_input_pack


FIXTURE_SNAPSHOT = Path("tests/fixtures/chapter_quality/snapshot.json")


class RuleAwareInputPackTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_rule_aware_input_pack" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self) -> dict:
        return json.loads(FIXTURE_SNAPSHOT.read_text(encoding="utf-8"))

    def test_original_build_input_pack_default_behavior_is_unchanged(self) -> None:
        snapshot = self._snapshot()

        original = build_input_pack(snapshot)
        new_default = build_input_pack(snapshot)

        self.assertEqual(original, new_default)
        self.assertNotIn("小说生成规则契约", new_default)
        self.assertNotIn("Narrative Rule Contract", new_default)

    def test_rule_aware_input_pack_contains_rule_section(self) -> None:
        input_pack = build_rule_aware_input_pack(self._snapshot(), use_default_rules=True)

        self.assertIn("小说生成规则契约", input_pack)
        self.assertIn("必须接住上一章结尾", input_pack)
        self.assertIn("只输出小说正文", input_pack)

    def test_default_injects_only_high_and_critical_generation_rules(self) -> None:
        input_pack = build_rule_aware_input_pack(self._snapshot(), use_default_rules=True)

        self.assertIn("必须接住上一章结尾", input_pack)
        self.assertIn("保持上一场景地点连续", input_pack)
        self.assertIn("只输出小说正文", input_pack)
        self.assertIn("章节结束必须改变故事状态", input_pack)
        self.assertNotIn("章节长度合理", input_pack)
        self.assertNotIn("避免重复和原地踏步", input_pack)

    def test_min_severity_medium_includes_medium_rules(self) -> None:
        input_pack = build_rule_aware_input_pack(
            self._snapshot(),
            use_default_rules=True,
            min_severity="medium",
        )

        self.assertIn("避免重复和原地踏步", input_pack)
        self.assertIn("章节长度合理", input_pack)
        self.assertIn("场景必须有具体行动", input_pack)

    def test_categories_filter_rules(self) -> None:
        input_pack = build_rule_aware_input_pack(
            self._snapshot(),
            use_default_rules=True,
            categories=["continuity"],
        )

        self.assertIn("必须接住上一章结尾", input_pack)
        self.assertIn("保持世界规则一致", input_pack)
        self.assertNotIn("保留上一场景人物", input_pack)
        self.assertNotIn("只输出小说正文", input_pack)

    def test_max_rules_limits_count_and_prioritizes_critical_rules(self) -> None:
        input_pack = build_rule_aware_input_pack(
            self._snapshot(),
            use_default_rules=True,
            max_rules=3,
        )

        self.assertLessEqual(input_pack.count("\n- ["), 3)
        self.assertIn("必须接住上一章结尾", input_pack)
        self.assertIn("只输出小说正文", input_pack)

    def test_does_not_modify_snapshot(self) -> None:
        snapshot = self._snapshot()
        before = copy.deepcopy(snapshot)

        build_rule_aware_input_pack(snapshot, use_default_rules=True)

        self.assertEqual(before, snapshot)

    def test_rule_pack_path_can_load_custom_rules(self) -> None:
        case_dir = self._case_dir("custom_rules")
        rule_pack_path = case_dir / "rules.json"
        rule_pack_path.write_text(
            json.dumps(load_default_narrative_rule_pack(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        input_pack = build_rule_aware_input_pack(self._snapshot(), rule_pack_path=rule_pack_path)

        self.assertIn("必须接住上一章结尾", input_pack)

    def test_cli_can_write_rule_aware_input_pack(self) -> None:
        output_path = self._case_dir("cli") / "input_pack.md"

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_aware_input_pack.py",
                "--snapshot",
                str(FIXTURE_SNAPSHOT),
                "--default-rules",
                "--out",
                str(output_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(output_path.exists())
        self.assertIn("小说生成规则契约", output_path.read_text(encoding="utf-8"))

    def test_cli_json_outputs_pure_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_aware_input_pack.py",
                "--snapshot",
                str(FIXTURE_SNAPSHOT),
                "--default-rules",
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("ok", payload["status"])
        self.assertGreater(payload["rules_injected"], 0)

    def test_cli_without_rules_produces_normal_input_pack(self) -> None:
        output_path = self._case_dir("plain") / "plain_input_pack.md"

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_aware_input_pack.py",
                "--snapshot",
                str(FIXTURE_SNAPSHOT),
                "--out",
                str(output_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(output_path.exists())
        self.assertNotIn("小说生成规则契约", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
