from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.rules import (
    NarrativeRulePackError,
    get_enabled_rules,
    load_default_narrative_rule_pack,
    render_narrative_contract,
    validate_narrative_rule_pack,
)


REQUIRED_QUALITY_CODES = {
    "continues_previous_ending",
    "preserves_last_scene_location",
    "preserves_last_scene_characters",
    "advances_open_threads_or_conflicts",
    "avoids_premature_resolution",
    "no_meta_output",
    "language_consistency",
    "repetition_or_stalling",
    "chapter_length_reasonable",
}


class NarrativeRulePackTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_narrative_rules" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_default_rule_pack_loads_and_validates(self) -> None:
        rule_pack = load_default_narrative_rule_pack()

        self.assertIs(rule_pack, validate_narrative_rule_pack(rule_pack))
        self.assertEqual("default_narrative_rules", rule_pack["rule_pack_id"])
        self.assertGreaterEqual(len(rule_pack["rules"]), 10)
        self.assertGreaterEqual(len(get_enabled_rules(rule_pack)), 10)

    def test_schema_rejects_invalid_rule_pack(self) -> None:
        rule_pack = load_default_narrative_rule_pack()
        missing_rules = copy.deepcopy(rule_pack)
        missing_rules.pop("rules")

        with self.assertRaises(NarrativeRulePackError):
            validate_narrative_rule_pack(missing_rules)

        missing_code = copy.deepcopy(rule_pack)
        missing_code["rules"][0].pop("code")
        with self.assertRaises(NarrativeRulePackError):
            validate_narrative_rule_pack(missing_code)

    def test_all_rule_codes_are_unique(self) -> None:
        rule_pack = load_default_narrative_rule_pack()
        codes = [rule["code"] for rule in rule_pack["rules"]]

        self.assertEqual(len(codes), len(set(codes)))

    def test_required_pr6_quality_check_mappings_exist(self) -> None:
        rule_pack = load_default_narrative_rule_pack()
        mapped = {
            quality_code
            for rule in rule_pack["rules"]
            for quality_code in rule.get("quality_check_codes", [])
        }

        self.assertTrue(REQUIRED_QUALITY_CODES.issubset(mapped))

    def test_render_narrative_contract_contains_enabled_rules(self) -> None:
        rule_pack = load_default_narrative_rule_pack()

        rendered = render_narrative_contract(rule_pack)

        self.assertIn("必须接住上一章结尾", rendered)
        self.assertIn("只输出小说正文", rendered)
        self.assertIn("不要输出分析", rendered)
        self.assertGreaterEqual(rendered.count("### "), 10)

    def test_disabled_rules_are_excluded_by_default(self) -> None:
        rule_pack = copy.deepcopy(load_default_narrative_rule_pack())
        disabled_rule = copy.deepcopy(rule_pack["rules"][0])
        disabled_rule["code"] = "disabled_test_rule"
        disabled_rule["title"] = "禁用测试规则"
        disabled_rule["enabled"] = False
        rule_pack["rules"].append(disabled_rule)

        enabled_codes = {rule["code"] for rule in get_enabled_rules(rule_pack)}
        default_render = render_narrative_contract(rule_pack)
        full_render = render_narrative_contract(rule_pack, include_disabled=True)

        self.assertNotIn("disabled_test_rule", enabled_codes)
        self.assertNotIn("禁用测试规则", default_render)
        self.assertIn("禁用测试规则", full_render)

    def test_cli_can_validate_default_rule_pack(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_narrative_rules.py",
                "--rules",
                "rules/default_narrative_rule_pack.json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Narrative rules: ok", result.stdout)

    def test_cli_json_outputs_pure_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_narrative_rules.py",
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("ok", payload["status"])

    def test_cli_render_works(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_narrative_rules.py",
                "--render",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(
            "Narrative Rule Contract" in result.stdout
            or "小说生成规则契约" in result.stdout
        )

    def test_cli_render_out_writes_markdown(self) -> None:
        output_path = self._case_dir("render") / "narrative_contract.md"

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_narrative_rules.py",
                "--render",
                "--out",
                str(output_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("必须接住上一章结尾", content)


if __name__ == "__main__":
    unittest.main()
