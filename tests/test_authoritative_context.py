from __future__ import annotations

import copy
import json
import unittest

from core.context_budget import ContextBudget
from core.prompt_compiler import (
    PROMPT_FINAL_REQUEST_HEADROOM_TOKENS,
    compile_prompt_contexts,
)
from core.state.authoritative_context import (
    AUTHORITATIVE_CONTEXT_SELECTION_KEY,
    AUTHORITATIVE_PLAN_SECTION_MAX_CHARS,
    AUTHORITATIVE_RECORD_COLLECTIONS,
    AUTHORITATIVE_REPAIR_SECTION_MAX_CHARS,
    AUTHORITATIVE_SCENE_SECTION_MAX_CHARS,
    authoritative_state_from_markdown,
    compact_authoritative_state_in_markdown,
    project_authoritative_state,
)
from core.structured_context import StructuredContextError, sha256_text


def _authoritative_state() -> dict:
    return {
        "schema_version": "1.0",
        "source_precedence": [
            "story_project_standard",
            "chapter_event",
            "model_inference",
        ],
        **{
            collection: {}
            for collection in AUTHORITATIVE_RECORD_COLLECTIONS
        },
    }


def _rendered_chars(value: dict) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _minimum_success_budget(state: dict, **kwargs) -> int:
    low = 1
    high = _rendered_chars(
        project_authoritative_state(
            state,
            max_chars=1_000_000,
            **kwargs,
        )
    )
    while low < high:
        middle = (low + high) // 2
        try:
            project_authoritative_state(
                state,
                max_chars=middle,
                **kwargs,
            )
        except StructuredContextError as exc:
            if exc.code not in {
                "required_authoritative_context_metadata_exceeds_budget",
                "required_authoritative_record_exceeds_budget",
                "required_authoritative_records_exceed_budget",
            }:
                raise
            low = middle + 1
        else:
            high = middle
    return low


def _first_truncating_projection(state: dict, **kwargs) -> tuple[int, dict]:
    full = project_authoritative_state(
        state,
        max_chars=1_000_000,
        **kwargs,
    )
    for budget in range(
        _minimum_success_budget(state, **kwargs),
        _rendered_chars(full) + 1,
    ):
        try:
            projected = project_authoritative_state(
                state,
                max_chars=budget,
                **kwargs,
            )
        except StructuredContextError:
            continue
        manifest = projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY]
        if manifest["selected_items"] and manifest["omitted_count"]:
            return budget, projected
    raise AssertionError("state has no successful truncating projection")


class AuthoritativeContextTests(unittest.TestCase):
    def test_plan_projection_accounts_for_protocol_and_headroom(self) -> None:
        state = _authoritative_state()
        state["events"] = {
            f"event-{index:03d}": {
                "event_id": f"event-{index:03d}",
                "status": "completed",
                "detail": "x" * 300,
            }
            for index in range(20)
        }
        source = "# Authoritative State\n" + json.dumps(state)
        budget = ContextBudget(
            provider="test",
            model="protocol-aware",
            model_context_window=9_000,
            output_reserve_tokens=0,
            protocol_overhead_tokens=0,
            safety_margin_tokens=0,
            max_input_tokens=7_000,
        )
        protocol = "P" * 200

        bundle = compile_prompt_contexts(
            source,
            budget=budget,
            exact_counter=len,
            stage_protocol_texts={"plan": (protocol,)},
        )
        authority = authoritative_state_from_markdown(bundle.plan.text)
        rechecked = budget.measure(
            bundle.plan.text,
            stage="plan",
            exact_counter=len,
            protocol_texts=(protocol,),
        )

        self.assertEqual(
            rechecked["budgeted_input_tokens"],
            bundle.plan.report["budgeted_input_tokens"],
        )
        self.assertLessEqual(
            rechecked["budgeted_input_tokens"],
            budget.hard_input_limit
            - PROMPT_FINAL_REQUEST_HEADROOM_TOKENS,
        )
        self.assertGreater(
            authority[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["omitted_count"],
            0,
        )

    def test_prompt_compiler_uses_stage_specific_authority_budgets(self) -> None:
        state = _authoritative_state()
        state["events"] = {
            f"chapter-0001-beat-{index:03d}": {
                "event_id": f"chapter-0001-beat-{index:03d}",
                "type": "checkpoint",
                "subjects": ["hero"],
                "objects": [f"object-{index}"],
                "location": "station",
                "status": "completed",
                "detail": "history " * 50,
            }
            for index in range(50)
        }
        source = "# Authoritative State\n" + json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        )

        bundle = compile_prompt_contexts(source)
        projected = {
            "plan": authoritative_state_from_markdown(bundle.plan.text),
            "scene": authoritative_state_from_markdown(bundle.scene.text),
            "repair": authoritative_state_from_markdown(bundle.repair.text),
        }
        limits = {
            "plan": AUTHORITATIVE_PLAN_SECTION_MAX_CHARS,
            "scene": AUTHORITATIVE_SCENE_SECTION_MAX_CHARS,
            "repair": AUTHORITATIVE_REPAIR_SECTION_MAX_CHARS,
        }
        for stage, authority in projected.items():
            with self.subTest(stage=stage):
                self.assertIsNotNone(authority)
                rendered = "# Authoritative State\n" + json.dumps(
                    authority,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.assertLessEqual(len(rendered), limits[stage])
        selected_counts = {
            stage: len(
                authority[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["selected_items"]
            )
            for stage, authority in projected.items()
        }
        self.assertLessEqual(selected_counts["scene"], selected_counts["repair"])
        self.assertLessEqual(selected_counts["repair"], selected_counts["plan"])

    def test_smaller_unused_stage_does_not_block_plan_context(self) -> None:
        state = _authoritative_state()
        state["events"] = {
            "open-event": {
                "event_id": "open-event",
                "type": "long_running_action",
                "subjects": ["hero"],
                "objects": [],
                "location": "station",
                "status": "ongoing",
                "detail": "x" * 6_000,
            }
        }
        source = "# Authoritative State\n" + json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        )

        bundle = compile_prompt_contexts(source)
        plan_authority = authoritative_state_from_markdown(bundle.plan.text)
        repair_baseline = authoritative_state_from_markdown(bundle.repair.text)

        self.assertIn("open-event", plan_authority["events"])
        self.assertNotIn("open-event", repair_baseline["events"])

    def test_selects_complete_record_by_stable_id_without_truncation(self) -> None:
        state = _authoritative_state()
        retained = {
            "character_id": "conflicting-payload-id",
            "canonical_name": "陆沉",
            "biography": "甲" * 500,
            "traits": ["冷静", "守序", {"底线": "不牺牲无辜者"}],
        }
        state["characters"] = {
            "char_lu_chen": retained,
            "char_other_a": {
                "character_id": "char_other_a",
                "canonical_name": "韩野",
                "biography": "乙" * 500,
            },
            "char_other_b": {
                "character_id": "char_other_b",
                "canonical_name": "赵铁",
                "biography": "丙" * 500,
            },
        }

        required = {"characters/char_lu_chen"}
        budget = _minimum_success_budget(
            state,
            query="陆沉",
            required_item_ids=required,
        )
        projected = project_authoritative_state(
            state,
            max_chars=budget,
            query="陆沉",
            required_item_ids=required,
        )

        self.assertEqual({"char_lu_chen": retained}, projected["characters"])
        self.assertEqual(
            ["characters/char_lu_chen"],
            projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["selected_items"],
        )
        self.assertEqual(
            2,
            projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["omitted_count"],
        )
        hash_input = copy.deepcopy(projected)
        projection_sha256 = hash_input[
            AUTHORITATIVE_CONTEXT_SELECTION_KEY
        ].pop("projection_sha256")
        self.assertEqual(
            sha256_text(
                json.dumps(
                    hash_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            projection_sha256,
        )

    def test_reprojection_is_rejected_to_prevent_compounded_record_loss(
        self,
    ) -> None:
        state = _authoritative_state()
        state["characters"] = {
            f"char_{index}": {
                "character_id": f"char_{index}",
                "canonical_name": f"角色{index}",
                "biography": chr(ord("A") + index) * 350,
            }
            for index in range(4)
        }

        first_required = {"characters/char_0"}
        first = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(
                state,
                required_item_ids=first_required,
            ),
            required_item_ids=first_required,
        )
        self.assertEqual(1, len(first["characters"]))
        self.assertEqual(
            3,
            first[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["omitted_count"],
        )

        with self.assertRaises(StructuredContextError) as raised:
            project_authoritative_state(
                first,
                max_chars=10_000,
            )
        self.assertEqual(
            "authoritative_context_already_projected",
            raised.exception.code,
        )

    def test_oversized_ongoing_event_fails_closed_and_reports_stable_id(
        self,
    ) -> None:
        state = _authoritative_state()
        state["events"] = {
            "event_open_giant": {
                "event_id": "event_open_giant",
                "status": "ongoing",
                "summary": "危" * 2_500,
            }
        }

        optional_state = copy.deepcopy(state)
        optional_state["events"]["event_open_giant"]["status"] = "completed"
        metadata_budget = _minimum_success_budget(optional_state)

        with self.assertRaises(StructuredContextError) as raised:
            project_authoritative_state(
                state,
                max_chars=metadata_budget,
            )

        self.assertEqual(
            "required_authoritative_record_exceeds_budget",
            raised.exception.code,
        )
        self.assertIn("events/event_open_giant", str(raised.exception))

    def test_rejects_unknown_or_invalid_record_collections(self) -> None:
        cases = {
            "unknown": {
                **_authoritative_state(),
                "unknown_collection": {
                    "unknown-1": {"id": "unknown-1"},
                },
            },
            "not_an_object": {
                **_authoritative_state(),
                "events": [
                    {
                        "event_id": "event-1",
                        "status": "completed",
                    }
                ],
            },
        }

        for name, state in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(StructuredContextError) as raised:
                    project_authoritative_state(
                        state,
                        max_chars=2_000,
                    )
                self.assertEqual(
                    "authoritative_context_invalid",
                    raised.exception.code,
                )

    def test_projection_is_deterministic_across_record_insertion_order(
        self,
    ) -> None:
        records = {
            name: {
                "character_id": name,
                "canonical_name": name,
                "biography": name * 160,
            }
            for name in ("alpha", "beta", "gamma")
        }
        first_state = _authoritative_state()
        first_state["characters"] = dict(records)
        second_state = _authoritative_state()
        second_state["characters"] = dict(reversed(list(records.items())))
        budget, first = _first_truncating_projection(first_state)
        second = project_authoritative_state(
            second_state,
            max_chars=budget,
        )

        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        self.assertGreater(
            first[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["omitted_count"],
            0,
        )

    def test_missing_or_ambiguous_explicit_required_id_fails_closed(
        self,
    ) -> None:
        state = _authoritative_state()
        state["characters"] = {
            "shared": {
                "character_id": "shared",
                "canonical_name": "Shared",
            }
        }
        state["events"] = {
            "shared": {
                "event_id": "shared",
                "type": "checkpoint",
                "status": "completed",
            }
        }

        for missing in ("characters/missing", "missing", "unknown/id"):
            with self.subTest(missing=missing):
                with self.assertRaises(StructuredContextError) as raised:
                    project_authoritative_state(
                        state,
                        max_chars=10_000,
                        required_item_ids={missing},
                    )
                self.assertEqual(
                    "required_authoritative_record_missing",
                    raised.exception.code,
                )
                self.assertIn(missing, str(raised.exception))

        with self.assertRaises(StructuredContextError) as raised:
            project_authoritative_state(
                state,
                max_chars=10_000,
                required_item_ids={"shared"},
            )
        self.assertEqual(
            "required_authoritative_record_ambiguous",
            raised.exception.code,
        )

        exact = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(
                state,
                required_item_ids={"characters/shared"},
            ),
            required_item_ids={"characters/shared"},
        )
        self.assertIn("shared", exact["characters"])
        self.assertNotIn("shared", exact["events"])

    def test_rejects_noncanonical_ids_and_scalar_records(
        self,
    ) -> None:
        cases = {}
        noncanonical = _authoritative_state()
        noncanonical["characters"] = {
            " hero": {
                "character_id": "hero",
            }
        }
        cases["noncanonical_id"] = noncanonical
        scalar = _authoritative_state()
        scalar["characters"] = {"hero": "not-an-object"}
        cases["scalar_record"] = scalar
        for name, state in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(StructuredContextError) as raised:
                    project_authoritative_state(
                        state,
                        max_chars=10_000,
                    )
                self.assertEqual(
                    "authoritative_context_invalid",
                    raised.exception.code,
                )

    def test_open_event_retains_dependency_closure_atomically(self) -> None:
        state = _authoritative_state()
        state["characters"] = {
            "hero": {
                "character_id": "hero",
                "canonical_name": "hero",
            },
            "distractor": {
                "character_id": "distractor",
                "canonical_name": "distractor",
                "detail": "noise" * 80,
            },
        }
        state["numeric_counters"] = {
            "erosion": {
                "counter_id": "erosion",
                "owner_id": "hero",
                "current_value": 6,
            }
        }
        state["inventory"] = {
            "locker:needle": {
                "inventory_id": "locker:needle",
                "item_id": "needle",
                "owner_id": "locker",
                "source_event_id": "open-event",
            }
        }
        state["locations"] = {
            "hero": {
                "entity_id": "hero",
                "location_id": "station",
            },
            "locker": {
                "entity_id": "locker",
                "location_id": "station",
            },
        }
        state["events"] = {
            "open-event": {
                "event_id": "open-event",
                "type": "inspection",
                "subjects": ["hero"],
                "objects": ["needle"],
                "location": "station",
                "status": "ongoing",
            }
        }
        budget = _minimum_success_budget(state)

        projected = project_authoritative_state(
            state,
            max_chars=budget,
        )

        self.assertIn("open-event", projected["events"])
        self.assertIn("hero", projected["characters"])
        self.assertIn("erosion", projected["numeric_counters"])
        self.assertIn("locker:needle", projected["inventory"])
        self.assertIn("hero", projected["locations"])
        self.assertIn("locker", projected["locations"])
        self.assertNotIn("distractor", projected["characters"])

        with self.assertRaises(StructuredContextError) as raised:
            project_authoritative_state(
                state,
                max_chars=budget - 1,
            )
        self.assertEqual(
            "required_authoritative_records_exceed_budget",
            raised.exception.code,
        )

    def test_open_event_dependency_does_not_recurse_into_provenance_history(
        self,
    ) -> None:
        state = _authoritative_state()
        state["characters"] = {
            "hero": {
                "character_id": "hero",
                "canonical_name": "hero",
            },
            "bystander": {
                "character_id": "bystander",
                "canonical_name": "bystander",
            },
        }
        state["numeric_counters"] = {
            "erosion": {
                "counter_id": "erosion",
                "owner_id": "hero",
                "source_event_id": "old-event",
                "current_value": 6,
            }
        }
        state["events"] = {
            "old-event": {
                "event_id": "old-event",
                "type": "old_checkpoint",
                "subjects": ["bystander"],
                "objects": [],
                "location": "station",
                "status": "completed",
                "detail": "history" * 80,
            },
            "open-event": {
                "event_id": "open-event",
                "type": "current_action",
                "subjects": ["hero"],
                "objects": [],
                "location": "station",
                "status": "ongoing",
            },
        }
        budget = _minimum_success_budget(state)

        projected = project_authoritative_state(
            state,
            max_chars=budget,
        )

        self.assertIn("open-event", projected["events"])
        self.assertIn("erosion", projected["numeric_counters"])
        self.assertNotIn("old-event", projected["events"])
        self.assertNotIn("bystander", projected["characters"])

    def test_open_event_provenance_matches_only_source_event_id(self) -> None:
        state = _authoritative_state()
        state["inventory"] = {
            "real-provenance": {
                "inventory_id": "real-provenance",
                "item_id": "needle",
                "source_event_id": "open-event",
            },
            "field-collision": {
                "inventory_id": "field-collision",
                "item_id": "open-event",
                "source_event_id": "different-event",
                "detail": "noise" * 100,
            },
        }
        state["events"] = {
            "open-event": {
                "event_id": "open-event",
                "status": "ongoing",
                "subjects": [],
                "objects": [],
            }
        }

        projected = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(state),
        )

        self.assertIn("real-provenance", projected["inventory"])
        self.assertNotIn("field-collision", projected["inventory"])

    def test_character_alias_expands_once_to_stable_id_state(self) -> None:
        state = _authoritative_state()
        state["characters"] = {
            "char-1": {
                "character_id": "char-1",
                "canonical_name": "Captain",
            }
        }
        state["relationships"] = {
            "char-1->char-2": {
                "relationship_id": "char-1->char-2",
                "source_character_id": "char-1",
                "target_character_id": "char-2",
            }
        }
        state["numeric_counters"] = {
            "erosion": {
                "counter_id": "erosion",
                "owner_id": "char-1",
                "current_value": 6,
            }
        }
        state["locations"] = {
            "char-1": {
                "entity_id": "char-1",
                "location_id": "station",
            }
        }
        state["events"] = {
            "open-event": {
                "event_id": "open-event",
                "status": "ongoing",
                "subjects": ["Captain"],
                "objects": [],
            }
        }

        projected = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(state),
        )

        self.assertIn("char-1", projected["characters"])
        self.assertIn("char-1->char-2", projected["relationships"])
        self.assertIn("erosion", projected["numeric_counters"])
        self.assertIn("char-1", projected["locations"])

    def test_markdown_embedded_json_references_are_mandatory(self) -> None:
        state = _authoritative_state()
        state["characters"] = {
            "hero": {
                "character_id": "hero",
                "canonical_name": "Hero",
            },
            "distractor": {
                "character_id": "distractor",
                "canonical_name": "Distractor",
                "detail": "noise" * 100,
            },
        }
        query = (
            "# StoryProject Chapter Blueprint\n"
            '{"focus":{"character_id":"hero"}}\n\n'
            "# Requirements\nContinue the assigned character."
        )
        required = {"characters/hero"}
        projected = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(
                state,
                required_item_ids=required,
                require_open_events=False,
            ),
            query=query,
            require_open_events=False,
        )

        self.assertIn("hero", projected["characters"])
        self.assertNotIn("distractor", projected["characters"])
        self.assertEqual(
            ["characters/hero"],
            projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["required_items"],
        )

    def test_completed_event_ties_prefer_explicitly_newer_chapter_event(
        self,
    ) -> None:
        state = _authoritative_state()
        state["events"] = {
            event_id: {
                "event_id": event_id,
                "type": "checkpoint",
                "subjects": [],
                "objects": [],
                "location": "station",
                "status": "completed",
                "detail": marker * 300,
            }
            for event_id, marker in (
                ("chapter-0001-beat-001", "A"),
                ("chapter-0002-beat-001", "B"),
            )
        }
        newest = {"events/chapter-0002-beat-001"}
        one_event_budget = _minimum_success_budget(
            state,
            required_item_ids=newest,
        )

        projected = project_authoritative_state(
            state,
            max_chars=one_event_budget,
        )

        self.assertIn("chapter-0002-beat-001", projected["events"])
        self.assertNotIn("chapter-0001-beat-001", projected["events"])

    def test_completed_event_ties_prefer_later_event_in_same_scene(
        self,
    ) -> None:
        state = _authoritative_state()
        state["events"] = {
            event_id: {
                "event_id": event_id,
                "type": "checkpoint",
                "subjects": [],
                "objects": [],
                "location": "station",
                "status": "completed",
                "detail": marker * 300,
            }
            for event_id, marker in (
                ("chapter-0002-scene-001-event-001", "A"),
                ("chapter-0002-scene-001-event-002", "B"),
            )
        }
        newest = {"events/chapter-0002-scene-001-event-002"}
        one_event_budget = _minimum_success_budget(
            state,
            required_item_ids=newest,
        )

        projected = project_authoritative_state(
            state,
            max_chars=one_event_budget,
        )

        self.assertIn(
            "chapter-0002-scene-001-event-002",
            projected["events"],
        )
        self.assertNotIn(
            "chapter-0002-scene-001-event-001",
            projected["events"],
        )

    def test_completed_beat_ties_use_beat_sequence_with_explicit_scene(
        self,
    ) -> None:
        state = _authoritative_state()
        state["events"] = {
            event_id: {
                "event_id": event_id,
                "chapter_index": 2,
                "scene_index": 4,
                "status": "completed",
                "detail": marker * 300,
            }
            for event_id, marker in (
                ("chapter-0002-beat-001", "A"),
                ("chapter-0002-beat-002", "B"),
            )
        }

        _budget, projected = _first_truncating_projection(state)

        self.assertIn("chapter-0002-beat-002", projected["events"])
        self.assertNotIn("chapter-0002-beat-001", projected["events"])

    def test_required_records_report_aggregate_overflow_separately(self) -> None:
        state = _authoritative_state()
        state["characters"] = {
            "alpha": {
                "character_id": "alpha",
                "canonical_name": "alpha",
                "detail": "A" * 300,
            },
            "beta": {
                "character_id": "beta",
                "canonical_name": "beta",
                "detail": "B" * 300,
            },
        }
        alpha = {"characters/alpha"}
        beta = {"characters/beta"}
        pair = alpha | beta
        alpha_min = _minimum_success_budget(
            state,
            required_item_ids=alpha,
        )
        beta_min = _minimum_success_budget(
            state,
            required_item_ids=beta,
        )
        pair_min = _minimum_success_budget(
            state,
            required_item_ids=pair,
        )
        self.assertGreater(pair_min, max(alpha_min, beta_min))
        project_authoritative_state(
            state,
            max_chars=pair_min - 1,
            required_item_ids=alpha,
        )
        project_authoritative_state(
            state,
            max_chars=pair_min - 1,
            required_item_ids=beta,
        )

        with self.assertRaises(StructuredContextError) as raised:
            project_authoritative_state(
                state,
                max_chars=pair_min - 1,
                required_item_ids=pair,
            )
        self.assertEqual(
            "required_authoritative_records_exceed_budget",
            raised.exception.code,
        )
        self.assertIn("characters/alpha", str(raised.exception))
        self.assertIn("characters/beta", str(raised.exception))

    def test_projected_input_is_rejected_before_manifest_can_be_trusted(
        self,
    ) -> None:
        state = _authoritative_state()
        state["characters"] = {
            name: {
                "character_id": name,
                "canonical_name": name,
                "detail": name * 220,
            }
            for name in ("alpha", "beta", "gamma")
        }
        first = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(
                state,
                required_item_ids={"characters/alpha"},
            ),
            required_item_ids={"characters/alpha"},
        )
        with self.assertRaises(StructuredContextError) as raised:
            project_authoritative_state(
                first,
                max_chars=10_000,
            )
        self.assertEqual(
            "authoritative_context_already_projected",
            raised.exception.code,
        )

    def test_raw_source_override_recovers_record_omitted_by_prior_projection(
        self,
    ) -> None:
        state = _authoritative_state()
        state["characters"] = {
            name: {
                "character_id": name,
                "canonical_name": name,
                "detail": name * 260,
            }
            for name in ("alpha", "beta", "gamma")
        }
        alpha_only = project_authoritative_state(
            state,
            max_chars=_minimum_success_budget(
                state,
                required_item_ids={"characters/alpha"},
            ),
            required_item_ids={"characters/alpha"},
        )
        self.assertNotIn("beta", alpha_only["characters"])
        markdown = "# Authoritative State\n" + json.dumps(
            alpha_only,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        recovered = compact_authoritative_state_in_markdown(
            markdown,
            max_section_chars=2_500,
            query=json.dumps(
                {"required_character_id": "characters/beta"},
            ),
            authoritative_state_source=state,
        )
        projected = json.loads(
            recovered.split("# Authoritative State\n", 1)[1]
        )

        self.assertIn("beta", projected["characters"])
        self.assertEqual(
            alpha_only[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["source_sha256"],
            projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY]["source_sha256"],
        )
        self.assertNotIn(
            "parent_projection_sha256",
            projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY],
        )


if __name__ == "__main__":
    unittest.main()
