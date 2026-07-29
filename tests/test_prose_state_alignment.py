from __future__ import annotations

import unittest

from core.state.prose_state_alignment import (
    extract_roster_count_claims,
    validate_roster_count_claims,
)


def _state(count: int, *, name: str = "火种一号") -> dict:
    return {
        "roster": {
            "main_group": {
                "roster_id": "main_group",
                "name": name,
                "members": [
                    {"member_id": f"member-{index:02d}"}
                    for index in range(count)
                ],
                "declared_count": count,
                "computed_count": count,
            }
        }
    }


class RosterProseAlignmentTests(unittest.TestCase):
    def test_extracts_uniquely_bound_arabic_and_chinese_counts(self) -> None:
        text = "【火种一号：12人】消防站队伍共有二十三人。"

        claims = extract_roster_count_claims(
            text,
            {
                "main_group": ["火种一号"],
                "station_group": ["消防站"],
            },
        )

        self.assertEqual(
            [("main_group", 12), ("station_group", 23)],
            [(claim.roster_id, claim.declared_count) for claim in claims],
        )
        for claim in claims:
            self.assertEqual(claim.quote, text[claim.start_char : claim.end_char])
            self.assertEqual("high", claim.confidence)

    def test_explicit_mismatch_returns_authority_compatible_blocker(self) -> None:
        findings = validate_roster_count_claims(
            chapter_text="火种一号现有十一人。",
            state_before=_state(7),
            state_after=_state(12),
            roster_changes=[
                {
                    "roster_id": "main_group",
                    "operation": "join",
                    "delta": 5,
                    "declared_count": 12,
                }
            ],
        )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("roster_count_mismatch", finding["code"])
        self.assertTrue(finding["blocking"])
        self.assertEqual("prose_state_mismatch", finding["evidence"]["kind"])
        self.assertEqual(11, finding["evidence"]["declared_count"])
        self.assertEqual(12, finding["evidence"]["expected_count"])
        self.assertEqual(
            "火种一号现有十一人",
            finding["evidence"]["claim"]["quote"],
        )

    def test_ordered_transition_values_pass_when_final_claim_is_final(self) -> None:
        findings = validate_roster_count_claims(
            chapter_text="火种一号共有七人。五人加入后，火种一号现有十二人。",
            state_before=_state(7),
            state_after=_state(12),
            roster_changes=[
                {
                    "roster_id": "main_group",
                    "operation": "join",
                    "delta": 5,
                    "declared_count": 12,
                }
            ],
        )

        self.assertEqual([], findings)

    def test_prior_legal_count_need_not_be_repeated_after_transition(self) -> None:
        findings = validate_roster_count_claims(
            chapter_text="火种一号共有七人。随后又有五人正式加入。",
            state_before=_state(7),
            state_after=_state(12),
            roster_changes=[
                {
                    "roster_id": "main_group",
                    "operation": "join",
                    "delta": 5,
                    "declared_count": 12,
                }
            ],
        )

        self.assertEqual([], findings)

    def test_approximate_historical_conditional_and_question_counts_do_not_block(
        self,
    ) -> None:
        examples = (
            "此前，火种一号共有十一人。",
            "火种一号现有约十一人。",
            "火种一号现有十一人左右。",
            "如果火种一号共有十一人，就能守住大门。",
            "火种一号共有十一人吗？",
            "据说火种一号共有十一人。",
        )
        for text in examples:
            with self.subTest(text=text):
                findings = validate_roster_count_claims(
                    chapter_text=text,
                    state_before=_state(12),
                    state_after=_state(12),
                )
                self.assertEqual([], findings)

    def test_history_marker_does_not_hide_current_claim_in_later_sentence(
        self,
    ) -> None:
        findings = validate_roster_count_claims(
            chapter_text=(
                "此前他们只有五人。警报解除后，火种一号共有十一人。"
            ),
            state_before=_state(12),
            state_after=_state(12),
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("roster_count_mismatch", findings[0]["code"])
        self.assertEqual(11, findings[0]["evidence"]["declared_count"])

    def test_history_marker_before_comma_still_scopes_current_sentence(
        self,
    ) -> None:
        findings = validate_roster_count_claims(
            chapter_text="此前，火种一号共有十一人。",
            state_before=_state(12),
            state_after=_state(12),
        )

        self.assertEqual([], findings)

    def test_alias_owned_by_two_rosters_is_not_a_deterministic_claim(self) -> None:
        claims = extract_roster_count_claims(
            "幸存者队现有十二人。",
            {
                "main_group": ["幸存者队"],
                "station_group": ["幸存者队"],
            },
        )

        self.assertEqual([], claims)

    def test_generic_team_claim_binds_when_only_one_roster_exists(self) -> None:
        findings = validate_roster_count_claims(
            chapter_text="队伍现在共有十一人。",
            state_before=_state(12),
            state_after=_state(12),
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("roster_count_mismatch", findings[0]["code"])
        self.assertEqual(11, findings[0]["evidence"]["declared_count"])

    def test_generic_team_claim_stays_ambiguous_with_multiple_rosters(self) -> None:
        before = _state(12)
        before["roster"]["station_group"] = {
            "roster_id": "station_group",
            "name": "消防站队伍",
            "members": [{"member_id": "station-01"}],
            "computed_count": 1,
            "declared_count": 1,
        }

        findings = validate_roster_count_claims(
            chapter_text="队伍现在共有十一人。",
            state_before=before,
            state_after=before,
        )

        self.assertEqual([], findings)

    def test_compact_exact_status_is_supported_only_at_clause_end(self) -> None:
        aliases = {"main_group": ["火种一号"]}

        exact = extract_roster_count_claims("火种一号十二人。", aliases)
        participant_subset = extract_roster_count_claims(
            "火种一号十二人前往仓库。",
            aliases,
        )
        spaced_subset = extract_roster_count_claims(
            "火种一号十二人 前往仓库。",
            aliases,
        )

        self.assertEqual([12], [claim.declared_count for claim in exact])
        self.assertEqual([], participant_subset)
        self.assertEqual([], spaced_subset)


if __name__ == "__main__":
    unittest.main()
