from __future__ import annotations

import copy
import unittest

from core.scene_continuity import empty_scene_state, validate_scene_transition
from core.state.authoritative import (
    empty_authoritative_state,
    validate_authoritative_state_delta,
)
from core.state.roster import ROSTER_INVALID_MUTATION


def _event(event_id: str, *, scene_index: int | None = None) -> dict:
    return {
        "event_id": event_id,
        "type": f"{event_id}_type",
        "subjects": [],
        "objects": [],
        "location": "shelter",
        "status": "completed",
        **({"scene_index": scene_index} if scene_index is not None else {}),
    }


def _scene_deltas(roster_mutation: dict) -> dict:
    return {
        "characters": [],
        "relationships": [],
        "rosters": [copy.deepcopy(roster_mutation)],
        "locations": [],
        "inventory": [],
        "counters": [],
    }


def _neutral_codes(findings: list[dict]) -> list[str]:
    aliases = {
        "invalid_roster_delta": ROSTER_INVALID_MUTATION,
        "invalid_roster_change": ROSTER_INVALID_MUTATION,
    }
    return sorted(
        aliases.get(str(item.get("code")), str(item.get("code")))
        for item in findings
    )


def _canonical_record(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = copy.deepcopy(value)
    result.pop("source_tier", None)
    return result


def _stable_roster(*, aliases: object = ("Main Team",)) -> dict:
    return {
        "roster_id": "main",
        "name": "Main Team",
        "aliases": aliases,
        "members": [{"member_id": "m1", "character_id": "c1"}],
        "unresolved_count": 0,
        "declared_count": 1,
        "computed_count": 1,
    }


class RosterReducerConformanceTests(unittest.TestCase):
    def test_scene_and_authority_share_canonical_transition_and_neutral_issues(self) -> None:
        evidence_a = {
            "source_kind": "user_approved_manifest",
            "source_path": ".novelagent/bootstrap/rosters.json",
            "sha256": "a" * 64,
        }
        evidence_b = {
            "source_kind": "user_approved_manifest",
            "source_path": ".novelagent/bootstrap/other.json",
            "sha256": "b" * 64,
        }
        cases = [
            {
                "name": "legacy_string_alias_is_canonicalized",
                "base": _stable_roster(aliases="Main Alias"),
                "mutation": {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["m2"],
                    "members": [{"member_id": "m2", "character_id": "c2"}],
                    "delta": 1,
                    "declared_count": 2,
                    "reason_event_id": "current",
                },
                "accepted": True,
                "codes": [],
                "aliases": ["Main Alias"],
            },
            {
                "name": "existing_join_cannot_add_alias",
                "base": _stable_roster(),
                "mutation": {
                    "roster_id": "main",
                    "aliases": ["Main Team", "New Alias"],
                    "operation": "join",
                    "member_ids": ["m2"],
                    "members": [{"member_id": "m2", "character_id": "c2"}],
                    "delta": 1,
                    "declared_count": 2,
                    "reason_event_id": "current",
                },
                "accepted": False,
                "codes": ["roster_identity_drift"],
                "aliases": ["Main Team"],
            },
            {
                "name": "existing_member_cannot_be_joined_again",
                "base": {
                    **_stable_roster(),
                    "members": [
                        {
                            "member_id": "m1",
                            "character_id": "c1",
                            "name": "Member One",
                            "status": "active",
                            "joined_chapter": 1,
                            "joined_event_id": "chapter-0001-event-001",
                        }
                    ],
                },
                "mutation": {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["m1"],
                    "members": [
                        {
                            "member_id": "m1",
                            "character_id": "c1",
                            "name": "Renamed Member",
                            "status": "missing",
                            "joined_chapter": 9,
                            "joined_event_id": "current",
                        }
                    ],
                    "delta": 0,
                    "declared_count": 1,
                    "reason_event_id": "current",
                },
                "accepted": False,
                "codes": ["roster_member_already_exists"],
            },
            {
                "name": "aggregate_member_can_be_resolved_without_changing_headcount",
                "base": {
                    "roster_id": "main",
                    "name": "Main Team",
                    "aliases": ["Main Team"],
                    "members": [],
                    "unresolved_count": 2,
                    "declared_count": 2,
                    "computed_count": 2,
                    "baseline_evidence": evidence_a,
                    "introduced_chapter": 1,
                },
                "mutation": {
                    "roster_id": "main",
                    "operation": "resolve",
                    "member_ids": ["m1"],
                    "members": [
                        {
                            "member_id": "m1",
                            "character_id": "c1",
                            "status": "active",
                            "resolved_event_id": "current",
                        }
                    ],
                    "unresolved_before": 2,
                    "unresolved_delta": -1,
                    "delta": 0,
                    "declared_count": 2,
                    "reason_event_id": "current",
                },
                "accepted": True,
                "codes": [],
                "member_ids": ["m1"],
                "unresolved_count": 1,
            },
            {
                "name": "missing_member_id_is_rejected",
                "base": None,
                "mutation": {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": [],
                    "members": [{"character_id": "c1"}],
                    "delta": 0,
                    "declared_count": 0,
                    "reason_event_id": "current",
                },
                "accepted": False,
                "codes": [ROSTER_INVALID_MUTATION],
            },
            {
                "name": "replace_identity_drift_is_rejected",
                "base": _stable_roster(),
                "mutation": {
                    "roster_id": "main",
                    "operation": "replace",
                    "member_ids": ["m1"],
                    "members": [{"member_id": "m1", "character_id": "c2"}],
                    "delta": 0,
                    "declared_count": 1,
                    "reason_event_id": "current",
                },
                "accepted": False,
                "codes": ["roster_member_identity_drift"],
            },
            {
                "name": "stable_members_cannot_regress_to_unresolved",
                "base": _stable_roster(),
                "mutation": {
                    "roster_id": "main",
                    "operation": "replace",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 0,
                    "unresolved_count": 1,
                    "delta": 0,
                    "declared_count": 1,
                    "reason_event_id": "current",
                },
                "accepted": False,
                "codes": ["roster_replace_not_idempotent"],
                "member_ids": ["m1"],
            },
            {
                "name": "existing_replace_may_be_exactly_idempotent",
                "base": _stable_roster(),
                "mutation": {
                    "roster_id": "main",
                    "name": "Main Team",
                    "aliases": ["Main Team"],
                    "operation": "replace",
                    "member_ids": ["m1"],
                    "members": [{"member_id": "m1", "character_id": "c1"}],
                    "unresolved_before": 0,
                    "unresolved_count": 0,
                    "delta": 0,
                    "declared_count": 1,
                    "reason_event_id": "current",
                },
                "accepted": True,
                "codes": [],
            },
            {
                "name": "historical_event_cannot_authorize_current_mutation",
                "base": _stable_roster(),
                "historical_event": _event("historical"),
                "mutation": {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["m2"],
                    "members": [{"member_id": "m2", "character_id": "c2"}],
                    "delta": 1,
                    "declared_count": 2,
                    "reason_event_id": "historical",
                },
                "accepted": False,
                "codes": ["invalid_authority_event_reference"],
            },
            {
                "name": "mutation_cannot_omit_current_event_reference",
                "base": _stable_roster(),
                "mutation": {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["m2"],
                    "members": [{"member_id": "m2", "character_id": "c2"}],
                    "delta": 1,
                    "declared_count": 2,
                },
                "accepted": False,
                "codes": ["missing_authority_event_reference"],
            },
            {
                "name": "baseline_audit_metadata_cannot_drift",
                "base": {
                    "roster_id": "main",
                    "name": "Main Team",
                    "aliases": ["Main Team"],
                    "members": [],
                    "unresolved_count": 17,
                    "declared_count": 17,
                    "computed_count": 17,
                    "baseline_evidence": evidence_a,
                    "introduced_chapter": 10,
                },
                "mutation": {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["m1"],
                    "members": [{"member_id": "m1", "character_id": "c1"}],
                    "delta": 1,
                    "declared_count": 18,
                    "baseline_evidence": evidence_b,
                    "introduced_chapter": 11,
                    "reason_event_id": "current",
                },
                "accepted": False,
                "codes": [
                    "roster_audit_metadata_drift",
                    "roster_baseline_evidence_drift",
                ],
            },
        ]
        for operation in ("leave", "dead", "missing"):
            cases.append(
                {
                    "name": f"{operation}_requires_existing_member",
                    "base": _stable_roster(),
                    "mutation": {
                        "roster_id": "main",
                        "operation": operation,
                        "member_ids": ["unknown-member"],
                        "members": [],
                        "delta": 0,
                        "declared_count": 1,
                        "reason_event_id": "current",
                    },
                    "accepted": False,
                    "codes": ["roster_member_not_found"],
                }
            )

        for case in cases:
            with self.subTest(case["name"]):
                scene_state = empty_scene_state()
                authority_state = empty_authoritative_state()
                base = case.get("base")
                if isinstance(base, dict):
                    scene_state["rosters"]["main"] = copy.deepcopy(base)
                    authority_state["roster"]["main"] = copy.deepcopy(base)
                historical = case.get("historical_event")
                if isinstance(historical, dict):
                    scene_state["completed_event_ids"].append(historical["event_id"])
                    scene_state["completed_events"].append(copy.deepcopy(historical))
                    authority_state["events"][historical["event_id"]] = copy.deepcopy(
                        historical
                    )
                current = _event("current", scene_index=2)
                mutation = {**copy.deepcopy(case["mutation"]), "scene_index": 2}
                scene_report, scene_after = validate_scene_transition(
                    scene_index=2,
                    state_before=scene_state,
                    events=[current],
                    deltas=_scene_deltas(mutation),
                    required_event_ids=["current"],
                )
                authority_report = validate_authoritative_state_delta(
                    base_state=authority_state,
                    state_delta={
                        "source_tier": "chapter_event",
                        "roster_changes": [mutation],
                        "events": [current],
                    },
                    chapter_text="",
                )

                self.assertEqual(case["accepted"], scene_report["accepted"])
                self.assertEqual(case["accepted"], authority_report["accepted"])
                self.assertEqual(
                    sorted(case["codes"]),
                    _neutral_codes(scene_report["findings"]),
                )
                self.assertEqual(
                    _neutral_codes(scene_report["findings"]),
                    _neutral_codes(authority_report["findings"]),
                )
                scene_record = scene_after["rosters"].get("main")
                authority_record = authority_report["state_after"]["roster"].get("main")
                self.assertEqual(
                    _canonical_record(scene_record),
                    _canonical_record(authority_record),
                )
                if "aliases" in case:
                    self.assertEqual(case["aliases"], scene_record["aliases"])
                if "member_ids" in case:
                    self.assertEqual(
                        case["member_ids"],
                        [item["member_id"] for item in scene_record["members"]],
                    )
                if "unresolved_count" in case:
                    self.assertEqual(
                        case["unresolved_count"],
                        scene_record["unresolved_count"],
                    )

    def test_story_standard_existing_mutations_still_require_current_event(self) -> None:
        aggregate = {
            "roster_id": "main",
            "name": "Main Team",
            "aliases": ["Main Team"],
            "members": [],
            "unresolved_count": 1,
            "declared_count": 1,
            "computed_count": 1,
            "baseline_evidence": {
                "source_kind": "manifest",
                "source_path": "rosters.json",
                "sha256": "a" * 64,
            },
            "introduced_chapter": 1,
        }
        for operation, base, mutation in (
            (
                "join",
                _stable_roster(),
                {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["m2"],
                    "members": [{"member_id": "m2", "character_id": "c2"}],
                    "delta": 1,
                    "declared_count": 2,
                },
            ),
            (
                "leave",
                _stable_roster(),
                {
                    "roster_id": "main",
                    "operation": "leave",
                    "member_ids": ["m1"],
                    "members": [],
                    "delta": -1,
                    "declared_count": 0,
                },
            ),
            (
                "resolve",
                aggregate,
                {
                    "roster_id": "main",
                    "operation": "resolve",
                    "member_ids": ["m1"],
                    "members": [{"member_id": "m1", "character_id": "c1"}],
                    "unresolved_before": 1,
                    "unresolved_delta": -1,
                    "delta": 0,
                    "declared_count": 1,
                },
            ),
        ):
            with self.subTest(operation):
                scene_state = empty_scene_state()
                authority_state = empty_authoritative_state()
                scene_state["rosters"]["main"] = copy.deepcopy(base)
                authority_state["roster"]["main"] = copy.deepcopy(base)

                scene_report, scene_after = validate_scene_transition(
                    scene_index=2,
                    state_before=scene_state,
                    events=[],
                    deltas=_scene_deltas(mutation),
                    required_event_ids=[],
                )
                authority_report = validate_authoritative_state_delta(
                    base_state=authority_state,
                    state_delta={
                        "source_tier": "story_project_standard",
                        "roster_changes": [mutation],
                        "events": [],
                    },
                    chapter_text="",
                )

                self.assertFalse(scene_report["accepted"])
                self.assertFalse(authority_report["accepted"])
                self.assertEqual(
                    ["missing_authority_event_reference"],
                    _neutral_codes(scene_report["findings"]),
                )
                self.assertEqual(
                    _neutral_codes(scene_report["findings"]),
                    _neutral_codes(authority_report["findings"]),
                )
                self.assertEqual(
                    _canonical_record(scene_after["rosters"]["main"]),
                    _canonical_record(
                        authority_report["state_after"]["roster"]["main"]
                    ),
                )

    def test_resolve_rejects_inexact_conversion_arithmetic(self) -> None:
        base = {
            "roster_id": "main",
            "name": "Main Team",
            "aliases": ["Main Team"],
            "members": [],
            "unresolved_count": 2,
            "declared_count": 2,
            "computed_count": 2,
            "baseline_evidence": {
                "source_kind": "manifest",
                "source_path": "rosters.json",
                "sha256": "a" * 64,
            },
            "introduced_chapter": 1,
        }
        cases = (
            ("stale_before", 1, -1, 0, 2, "roster_state_rollback"),
            (
                "wrong_unresolved_delta",
                2,
                0,
                0,
                2,
                "roster_resolution_arithmetic_mismatch",
            ),
            ("nonzero_total_delta", 2, -1, 1, 2, "roster_count_mismatch"),
            ("changed_headcount", 2, -1, 0, 3, "roster_count_mismatch"),
        )
        for (
            name,
            unresolved_before,
            unresolved_delta,
            delta,
            declared_count,
            expected_code,
        ) in cases:
            with self.subTest(name):
                event = _event("current", scene_index=2)
                mutation = {
                    "roster_id": "main",
                    "operation": "resolve",
                    "member_ids": ["m1"],
                    "members": [{"member_id": "m1", "character_id": "c1"}],
                    "unresolved_before": unresolved_before,
                    "unresolved_delta": unresolved_delta,
                    "delta": delta,
                    "declared_count": declared_count,
                    "reason_event_id": "current",
                    "scene_index": 2,
                }
                scene_state = empty_scene_state()
                authority_state = empty_authoritative_state()
                scene_state["rosters"]["main"] = copy.deepcopy(base)
                authority_state["roster"]["main"] = copy.deepcopy(base)
                scene_report, _ = validate_scene_transition(
                    scene_index=2,
                    state_before=scene_state,
                    events=[event],
                    deltas=_scene_deltas(mutation),
                    required_event_ids=["current"],
                )
                authority_report = validate_authoritative_state_delta(
                    base_state=authority_state,
                    state_delta={
                        "source_tier": "chapter_event",
                        "roster_changes": [mutation],
                        "events": [event],
                    },
                    chapter_text="",
                )

                self.assertFalse(scene_report["accepted"])
                self.assertFalse(authority_report["accepted"])
                self.assertIn(
                    expected_code,
                    _neutral_codes(scene_report["findings"]),
                )
                self.assertEqual(
                    _neutral_codes(scene_report["findings"]),
                    _neutral_codes(authority_report["findings"]),
                )

    def test_story_baseline_without_current_event_requires_structured_evidence(self) -> None:
        mutation = {
            "roster_id": "main",
            "name": "Main Team",
            "operation": "replace",
            "member_ids": [],
            "members": [],
            "unresolved_before": 0,
            "unresolved_count": 17,
            "delta": 17,
            "declared_count": 17,
        }
        missing = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta={
                "source_tier": "story_project_standard",
                "roster_changes": [mutation],
                "events": [],
            },
            chapter_text="",
        )
        malformed = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta={
                "source_tier": "story_project_standard",
                "roster_changes": [
                    {
                        **mutation,
                        "baseline_evidence": {
                            "source_kind": "manifest",
                            "source_path": "rosters.json",
                            "sha256": "not-a-sha",
                        },
                        "introduced_chapter": 10,
                    }
                ],
                "events": [],
            },
            chapter_text="",
        )
        valid_evidence = {
            "source_kind": "user_approved_manifest",
            "source_path": ".novelagent/bootstrap/rosters.json",
            "sha256": "A" * 64,
        }
        accepted = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta={
                "source_tier": "story_project_standard",
                "roster_changes": [
                    {
                        **mutation,
                        "baseline_evidence": valid_evidence,
                        "introduced_chapter": 10,
                    }
                ],
                "events": [],
            },
            chapter_text="",
        )

        self.assertFalse(missing["accepted"])
        self.assertIn(
            "missing_roster_baseline_evidence",
            _neutral_codes(missing["findings"]),
        )
        self.assertFalse(malformed["accepted"])
        self.assertIn(ROSTER_INVALID_MUTATION, _neutral_codes(malformed["findings"]))
        self.assertIn(
            "missing_roster_baseline_evidence",
            _neutral_codes(malformed["findings"]),
        )
        self.assertTrue(accepted["accepted"], accepted["findings"])
        self.assertEqual(
            "a" * 64,
            accepted["state_after"]["roster"]["main"]["baseline_evidence"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
