from __future__ import annotations

import unittest

from core.memory_v2 import (
    apply_memory_events,
    apply_memory_patch,
    create_empty_typed_canonical_memory,
    create_memory_patch,
)
from core.scene_continuity import empty_scene_state, validate_scene_transition
from core.state.authoritative import (
    adapt_scene_deltas_to_authoritative_delta,
    empty_authoritative_state,
    validate_authoritative_state,
    validate_authoritative_state_delta,
)


def _empty_deltas() -> dict[str, list[dict]]:
    return {
        "characters": [],
        "relationships": [],
        "rosters": [],
        "locations": [],
        "inventory": [],
        "counters": [],
    }


def _event(event_id: str, *, event_type: str) -> dict:
    return {
        "event_id": event_id,
        "type": event_type,
        "subjects": ["recorder"],
        "objects": ["main_roster"],
        "location": "shelter",
        "status": "completed",
    }


def _codes(report: dict) -> set[str]:
    return {
        str(item.get("code"))
        for item in report.get("findings") or []
        if isinstance(item, dict)
    }


def _memory_context() -> dict:
    return {
        "chapter_body": "roster evidence",
        "evidence_spans": [
            {
                "start_char": 0,
                "end_char": 6,
                "quote": "roster",
            }
        ],
        "authority_epoch": 1,
    }


def _round_trip_memory(delta: dict) -> dict:
    memory = create_empty_typed_canonical_memory(book_id="book-roster-chain")
    patch = create_memory_patch(
        patch_id="patch-roster-chain",
        source_kind="chapter",
        operations=[{"op": "update_authoritative_state", "value": delta}],
    )
    updated, events = apply_memory_patch(
        memory,
        patch,
        event_context=_memory_context(),
    )
    replayed = apply_memory_events(
        memory,
        events,
        reducer_version="memory-reducer-2.2",
    )
    assert updated == replayed
    return replayed["authoritative_state"]["roster"]["main_roster"]


class RosterAuthorityChainTests(unittest.TestCase):
    def test_stable_members_name_and_aliases_survive_scene_authority_and_memory(self) -> None:
        first_event = _event("chapter-0001-roster-baseline", event_type="roster_registered")
        second_event = _event("chapter-0001-roster-join", event_type="member_joined")
        initial = empty_scene_state()
        baseline_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "name": "火种一号",
                    "aliases": ["火种队", "主队"],
                    "operation": "replace",
                    "member_ids": ["member-lu", "member-su"],
                    "members": [
                        {"member_id": "member-lu", "character_id": "陆沉"},
                        {"member_id": "member-su", "character_id": "苏晴"},
                    ],
                    "unresolved_before": 0,
                    "unresolved_count": 0,
                    "delta": 2,
                    "declared_count": 2,
                    "reason_event_id": first_event["event_id"],
                }
            ],
        }
        first_report, after_first = validate_scene_transition(
            scene_index=1,
            state_before=initial,
            events=[first_event],
            deltas=baseline_delta,
            required_event_ids=[first_event["event_id"]],
        )
        join_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "operation": "join",
                    "member_ids": ["member-zhao"],
                    "members": [
                        {"member_id": "member-zhao", "character_id": "赵铁"}
                    ],
                    "delta": 1,
                    "declared_count": 3,
                    "reason_event_id": second_event["event_id"],
                }
            ],
        }
        second_report, after_second = validate_scene_transition(
            scene_index=2,
            state_before=after_first,
            events=[second_event],
            deltas=join_delta,
            required_event_ids=[second_event["event_id"]],
        )

        self.assertTrue(first_report["accepted"], first_report["findings"])
        self.assertTrue(second_report["accepted"], second_report["findings"])
        scene_roster = after_second["rosters"]["main_roster"]
        self.assertEqual("火种一号", scene_roster["name"])
        self.assertEqual(["火种队", "主队"], scene_roster["aliases"])
        self.assertEqual(3, scene_roster["computed_count"])
        self.assertEqual(0, scene_roster["unresolved_count"])

        authority_delta = adapt_scene_deltas_to_authoritative_delta(
            [
                {
                    "index": 1,
                    "events": [first_event],
                    "deltas": baseline_delta,
                },
                {
                    "index": 2,
                    "events": [second_event],
                    "deltas": join_delta,
                },
            ],
            base_state=empty_authoritative_state(),
        )
        authority_report = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta=authority_delta,
            chapter_text="",
        )

        self.assertTrue(authority_report["accepted"], authority_report["findings"])
        authority_roster = authority_report["state_after"]["roster"]["main_roster"]
        self.assertEqual(scene_roster, {
            key: authority_roster[key]
            for key in scene_roster
        })
        replayed_roster = _round_trip_memory(authority_delta)
        self.assertEqual("火种一号", replayed_roster["name"])
        self.assertEqual(["火种队", "主队"], replayed_roster["aliases"])
        self.assertEqual(3, replayed_roster["computed_count"])
        self.assertEqual(0, replayed_roster["unresolved_count"])

    def test_aggregate_roster_never_fabricates_member_ids_and_replays_exactly(self) -> None:
        baseline_event = _event("chapter-0001-headcount", event_type="headcount_registered")
        join_event = _event("chapter-0001-rescued", event_type="aggregate_joined")
        leave_event = _event("chapter-0001-departed", event_type="aggregate_left")
        missing_event = _event("chapter-0001-missing", event_type="aggregate_missing")
        resolve_event = _event(
            "chapter-0001-identified",
            event_type="aggregate_member_identified",
        )
        initial = empty_scene_state()
        baseline_evidence = {
            "source_kind": "user_approved_manifest",
            "source_path": ".novelagent/bootstrap/rosters.json",
            "sha256": "a" * 64,
        }
        baseline_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "name": "火种一号",
                    "aliases": ["火种队"],
                    "operation": "replace",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 0,
                    "unresolved_count": 17,
                    "delta": 17,
                    "declared_count": 17,
                    "baseline_evidence": baseline_evidence,
                    "introduced_chapter": 10,
                    "reason_event_id": baseline_event["event_id"],
                }
            ],
        }
        join_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "operation": "join",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 17,
                    "unresolved_delta": 4,
                    "delta": 4,
                    "declared_count": 21,
                    "reason_event_id": join_event["event_id"],
                }
            ],
        }
        leave_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "operation": "leave",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 21,
                    "unresolved_delta": -2,
                    "delta": -2,
                    "declared_count": 19,
                    "reason_event_id": leave_event["event_id"],
                }
            ],
        }
        missing_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "operation": "missing",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 19,
                    "unresolved_delta": -1,
                    "delta": -1,
                    "declared_count": 18,
                    "reason_event_id": missing_event["event_id"],
                }
            ],
        }
        resolve_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "operation": "resolve",
                    "member_ids": ["member-identified"],
                    "members": [
                        {
                            "member_id": "member-identified",
                            "character_id": "identified-survivor",
                            "status": "active",
                            "resolved_event_id": resolve_event["event_id"],
                        }
                    ],
                    "unresolved_before": 18,
                    "unresolved_delta": -1,
                    "delta": 0,
                    "declared_count": 18,
                    "reason_event_id": resolve_event["event_id"],
                }
            ],
        }

        reports: list[dict] = []
        state = initial
        for index, (event, delta) in enumerate(
            (
                (baseline_event, baseline_delta),
                (join_event, join_delta),
                (leave_event, leave_delta),
                (missing_event, missing_delta),
                (resolve_event, resolve_delta),
            ),
            start=1,
        ):
            report, state = validate_scene_transition(
                scene_index=index,
                state_before=state,
                events=[event],
                deltas=delta,
                required_event_ids=[event["event_id"]],
            )
            reports.append(report)
        self.assertTrue(all(report["accepted"] for report in reports), reports)
        scene_roster = state["rosters"]["main_roster"]
        self.assertEqual(
            ["member-identified"],
            [member["member_id"] for member in scene_roster["members"]],
        )
        self.assertEqual(17, scene_roster["unresolved_count"])
        self.assertEqual(18, scene_roster["computed_count"])
        self.assertEqual(baseline_evidence, scene_roster["baseline_evidence"])
        self.assertEqual(10, scene_roster["introduced_chapter"])

        authority_delta = adapt_scene_deltas_to_authoritative_delta(
            [
                {"index": 1, "events": [baseline_event], "deltas": baseline_delta},
                {"index": 2, "events": [join_event], "deltas": join_delta},
                {"index": 3, "events": [leave_event], "deltas": leave_delta},
                {"index": 4, "events": [missing_event], "deltas": missing_delta},
                {"index": 5, "events": [resolve_event], "deltas": resolve_delta},
            ],
            base_state=empty_authoritative_state(),
        )
        authority_report = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta=authority_delta,
            chapter_text="",
        )
        self.assertTrue(authority_report["accepted"], authority_report["findings"])
        validated_full_state = validate_authoritative_state(
            authority_report["state_after"]
        )
        self.assertEqual(
            baseline_evidence,
            validated_full_state["roster"]["main_roster"]["baseline_evidence"],
        )
        self.assertEqual(
            10,
            validated_full_state["roster"]["main_roster"]["introduced_chapter"],
        )
        replayed_roster = _round_trip_memory(authority_delta)
        self.assertEqual(
            ["member-identified"],
            [member["member_id"] for member in replayed_roster["members"]],
        )
        self.assertEqual(17, replayed_roster["unresolved_count"])
        self.assertEqual(18, replayed_roster["computed_count"])
        self.assertEqual("火种一号", replayed_roster["name"])
        self.assertEqual(["火种队"], replayed_roster["aliases"])
        self.assertEqual(baseline_evidence, replayed_roster["baseline_evidence"])
        self.assertEqual(10, replayed_roster["introduced_chapter"])

    def test_aggregate_before_arithmetic_name_drift_and_alias_conflict_are_blocking(self) -> None:
        state = empty_scene_state()
        state["rosters"] = {
            "main_roster": {
                "roster_id": "main_roster",
                "name": "火种一号",
                "aliases": ["主队"],
                "members": [],
                "unresolved_count": 17,
                "computed_count": 17,
                "declared_count": 17,
            },
            "station_roster": {
                "roster_id": "station_roster",
                "name": "消防站幸存者",
                "aliases": ["站内八人"],
                "members": [],
                "unresolved_count": 8,
                "computed_count": 8,
                "declared_count": 8,
            },
        }
        event = _event("chapter-0002-invalid", event_type="invalid_change")
        invalid_delta = {
            **_empty_deltas(),
            "rosters": [
                {
                    "roster_id": "main_roster",
                    "name": "消防站幸存者",
                    "aliases": ["站内八人"],
                    "operation": "join",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 16,
                    "unresolved_delta": -1,
                    "delta": 1,
                    "declared_count": 18,
                    "reason_event_id": event["event_id"],
                },
                {
                    "roster_id": "站内八人",
                    "name": "第三队",
                    "aliases": [],
                    "operation": "replace",
                    "member_ids": [],
                    "members": [],
                    "unresolved_before": 0,
                    "unresolved_count": 1,
                    "delta": 1,
                    "declared_count": 1,
                    "reason_event_id": event["event_id"],
                },
            ],
        }

        scene_report, _ = validate_scene_transition(
            scene_index=1,
            state_before=state,
            events=[event],
            deltas=invalid_delta,
            required_event_ids=[event["event_id"]],
        )
        scene_codes = _codes(scene_report)
        self.assertFalse(scene_report["accepted"])
        self.assertIn("roster_identity_drift", scene_codes)
        self.assertIn("roster_alias_conflict", scene_codes)
        self.assertIn("roster_state_rollback", scene_codes)
        self.assertIn("invalid_roster_delta", scene_codes)
        self.assertIn("roster_count_mismatch", scene_codes)
        self.assertTrue(
            any(
                finding["code"] == "roster_alias_conflict"
                and finding["evidence"].get("alias") == "站内八人"
                and finding["evidence"].get("incoming_roster_id") == "站内八人"
                for finding in scene_report["findings"]
            )
        )

        authority_base = empty_authoritative_state()
        authority_base["roster"] = {
            key: dict(value)
            for key, value in state["rosters"].items()
        }
        authority_report = validate_authoritative_state_delta(
            base_state=authority_base,
            state_delta={
                "source_tier": "chapter_event",
                "roster_changes": invalid_delta["rosters"],
                "events": [event],
            },
            chapter_text="",
        )
        authority_codes = _codes(authority_report)
        self.assertFalse(authority_report["accepted"])
        self.assertIn("roster_identity_drift", authority_codes)
        self.assertIn("roster_alias_conflict", authority_codes)
        self.assertIn("roster_state_rollback", authority_codes)
        self.assertIn("invalid_roster_change", authority_codes)
        self.assertIn("roster_count_mismatch", authority_codes)
        self.assertTrue(
            any(
                finding["code"] == "roster_alias_conflict"
                and finding["evidence"].get("alias") == "站内八人"
                and finding["evidence"].get("incoming_roster_id") == "站内八人"
                for finding in authority_report["findings"]
            )
        )


if __name__ == "__main__":
    unittest.main()
