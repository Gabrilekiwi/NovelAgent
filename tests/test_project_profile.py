from __future__ import annotations

import unittest

from core.project_profile import normalize_project_profile, project_language


class ProjectProfileTest(unittest.TestCase):
    def test_normalizes_explicit_and_derived_profile_terms(self) -> None:
        snapshot = {
            "project_profile": {
                "language": "zh-CN",
                "known_characters": ["陆砚"],
                "known_locations": ["第七码头"],
            },
            "characters": {"阿照": {}, "陆砚": {}},
            "world_state": {"locations": {"黑月集市": {}}},
            "spatial_state": {"spaces": {"旧天文馆": {}}, "character_positions": {"阿照": "第七码头"}},
            "story_state": {"last_scene_location": "第七码头", "last_scene_characters": ["陆砚"]},
        }

        profile = normalize_project_profile(snapshot)

        self.assertEqual("zh-CN", project_language(snapshot))
        self.assertEqual(["陆砚", "阿照"], profile["known_characters"])
        self.assertIn("第七码头", profile["known_locations"])
        self.assertIn("黑月集市", profile["known_locations"])
        self.assertIn("旧天文馆", profile["known_locations"])


if __name__ == "__main__":
    unittest.main()
