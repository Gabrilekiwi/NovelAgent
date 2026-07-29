from __future__ import annotations

import unittest

from core.state.prose_state_alignment import extract_roster_count_claims


class RosterProsePatternTests(unittest.TestCase):
    def test_count_before_alias_binds_clause_subject_for_both_number_forms(
        self,
    ) -> None:
        claims = extract_roster_count_claims(
            "四名获救者被单独安置。17名火种成员全部在门外。",
            {
                "rescued": ["获救者"],
                "ember": ["火种成员"],
            },
        )

        self.assertEqual(
            [
                ("rescued", 4, "count_first_subject"),
                ("ember", 17, "count_first_subject"),
            ],
            [
                (claim.roster_id, claim.declared_count, claim.pattern_id)
                for claim in claims
            ],
        )

    def test_alias_then_short_pause_or_colon_reports_exact_status(self) -> None:
        claims = extract_roster_count_claims(
            "待检人员，四人。消防站幸存者：八人。",
            {
                "pending": ["待检人员"],
                "station": ["消防站幸存者"],
            },
        )

        self.assertEqual(
            [("pending", 4), ("station", 8)],
            [(claim.roster_id, claim.declared_count) for claim in claims],
        )
        self.assertEqual(
            ["paused_status", "status_colon"],
            [claim.pattern_id for claim in claims],
        )

    def test_count_before_alias_keeps_current_exactness_filters(self) -> None:
        aliases = {
            "rescued": ["获救者"],
            "ember": ["火种成员"],
        }
        examples = (
            "此前，四名获救者被单独安置。",
            "据说，四名获救者被单独安置。",
            "如果四名获救者被单独安置，就需要两间观察室。",
            "四名获救者左右。",
            "四名获救者吗？",
            "陆沉救出四名获救者。",
            "陆沉安排17名火种成员搬运物资。",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual([], extract_roster_count_claims(text, aliases))

    def test_paused_status_keeps_current_exactness_and_action_filters(self) -> None:
        aliases = {
            "pending": ["待检人员"],
            "station": ["消防站幸存者"],
        }
        examples = (
            "此前，待检人员，四人。",
            "据说待检人员，四人。",
            "如果待检人员，四人。",
            "待检人员，四人左右。",
            "待检人员，四人吗？",
            "待检人员，四人搬运担架。",
            "消防站幸存者：八人搬运物资。",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual([], extract_roster_count_claims(text, aliases))


if __name__ == "__main__":
    unittest.main()
