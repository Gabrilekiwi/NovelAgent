from __future__ import annotations

import inspect
import unittest

from modules.scene_repair.repairer import (
    REPAIR_STRATEGY_REGISTRY,
    RepairContext,
    apply_repair_plan,
)
import modules.scene_repair.repairer as repairer_module


def _plan(*steps: dict) -> dict:
    return {"steps": list(steps)}


def _step(index: int, action: str, parameters: dict | None = None) -> dict:
    return {"index": index, "priority": index, "action": action, "parameters": parameters or {}}


class ChineseRepairTest(unittest.TestCase):
    def test_registry_has_explicit_english_and_chinese_strategies(self) -> None:
        self.assertEqual({"en", "zh-CN"}, set(REPAIR_STRATEGY_REGISTRY))
        self.assertEqual(set(REPAIR_STRATEGY_REGISTRY["en"]), set(REPAIR_STRATEGY_REGISTRY["zh-CN"]))

    def test_chinese_repairs_use_only_known_parameters_and_no_english_templates(self) -> None:
        repaired = apply_repair_plan(
            "第三章\n\n门外的脚步逼近。",
            _plan(
                _step(1, "correct_chapter_index", {"expected": "4", "actual": "3"}),
                _step(2, "insert_opening_bridge", {"bridge": "警报声仍在站台上回荡", "location": "站台"}),
                _step(3, "add_character_location", {"character": "林默", "location": "站台"}),
            ),
            repair_context=RepairContext(language="zh-CN"),
        )

        self.assertIn("第四章", repaired)
        self.assertIn("警报声仍在站台上回荡。", repaired)
        self.assertIn("林默仍在站台。", repaired)
        for fragment in ("Chapter", "The ", "From ", "danger", "conflict", "scene"):
            self.assertNotIn(fragment, repaired)

    def test_unsafe_chinese_plot_repair_does_not_invent_a_generic_scene(self) -> None:
        chapter = "雨落在封锁线外。"
        repaired = apply_repair_plan(
            chapter,
            _plan(_step(1, "add_conflict_signal"), _step(2, "expand_scene")),
            language="zh-CN",
        )

        self.assertEqual(chapter, repaired)

    def test_known_conflict_hint_can_be_inserted_without_inventing_a_new_fact(self) -> None:
        repaired = apply_repair_plan(
            "雨落在封锁线外。",
            _plan(_step(1, "add_conflict_signal")),
            repair_context=RepairContext(
                language="zh-CN",
                known_conflict_hint="是否开启隔离闸门",
            ),
        )

        self.assertIn("冲突焦点仍是：是否开启隔离闸门。", repaired)

    def test_inactive_character_action_is_removed_without_new_plot_fact(self) -> None:
        repaired = apply_repair_plan(
            "林默打开闸门。周岚守在站台。",
            _plan(_step(1, "rewrite_inactive_character_action", {"character": "林默"})),
            language="zh-CN",
        )

        self.assertNotIn("林默打开闸门", repaired)
        self.assertEqual("周岚守在站台。", repaired)

    def test_repairer_contains_no_test_domain_serum_special_case(self) -> None:
        self.assertNotIn("serum", inspect.getsource(repairer_module).lower())


if __name__ == "__main__":
    unittest.main()
